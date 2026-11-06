"""The synthetic EventIds carried in unmatched_events: each belongs to one thing, every filter of state rows covers all of
them, and aggregations carrying in the same file do not read each other's rows."""
import os
import re
from glob import glob

import pandas as pd
import pytest
from src.atspm import SignalDataProcessor
from src.atspm.utils import STATE_EVENT_IDS

T0 = pd.Timestamp('2026-09-01 08:00:00')
PHASE, DETECTOR, PED_INPUT = 2, 3, 4
CONFIG = pd.DataFrame({'DeviceId': ['A'], 'Phase': [PHASE], 'Parameter': [DETECTOR], 'Function': ['Advance']})
HAS_DATA = {'name': 'has_data', 'params': {}}
TIMELINE = {'name': 'timeline', 'params': {'min_duration': 0.1, 'cushion_time': 60}}
ARRIVALS = [{'name': 'arrival_on_green', 'params': {'latency_offset_seconds': 0}}, {'name': 'platoon_ratio', 'params': {}}]
PED = [{'name': 'ped', 'params': {}}]
ALL_TOGETHER = [HAS_DATA, TIMELINE, *ARRIVALS, *PED]
KEYS = ['TimeStamp', 'DeviceId', 'Phase']
TABLES = ('arrival_on_green', 'platoon_ratio', 'ped')
ARRIVAL_CODES = {938, 939, 940, 941, 948}
SPILLBACK_CODES = {949, 950}  # Parameter holds the detector
PED_CODES = {937, 943, 944, 945, 946, 947}
SRC = os.path.join(os.path.dirname(__file__), '..', 'src', 'atspm')


def events(minutes=240):
    """A signal cycling each minute, with arrivals across the whole cycle and a ped button pressed every 3 minutes."""
    rows = []
    for m in range(minutes):
        t = m * 60
        rows += [(t + 5.1, 1, PHASE), (t + 40.2, 8, PHASE), (t + 44.3, 10, PHASE), (t + 12.4, 82, DETECTOR),
                 (t + 42.5, 82, DETECTOR), (t + 57.6, 82, DETECTOR), (t + 59.95, 7, 8)]
        if m % 3 == 0:
            rows.append((t + 20.8, 90, PED_INPUT))
        if m % 6 == 0:
            rows.append((t + 30.9, 21, PED_INPUT))
    frame = pd.DataFrame(rows, columns=['Seconds', 'EventId', 'Parameter'])
    frame['TimeStamp'] = T0 + pd.to_timedelta(frame.pop('Seconds'), unit='s')
    frame['DeviceId'] = 'A'
    return frame[['TimeStamp', 'DeviceId', 'EventId', 'Parameter']].sort_values('TimeStamp').reset_index(drop=True)


def run(frame, aggregations, carry='', **kwargs):
    settings = {'df_or_path': carry, 'max_days_old': 14}
    with SignalDataProcessor(raw_data=frame, detector_config=CONFIG, bin_size=15, verbose=0, unmatched_event_settings=settings,
                             aggregations=aggregations, **kwargs) as p:
        p.load()
        p.aggregate()
        tables = [row[0] for row in p.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        return {t: p.conn.execute(f'SELECT * FROM "{t}"').df() for t in tables}


def cut_at_minutes(frame, aggregations, minutes, **kwargs):
    edges = [T0] + [T0 + pd.Timedelta(minutes=m) for m in minutes] + [frame.TimeStamp.max() + pd.Timedelta(seconds=1)]
    carry, outputs = '', []
    for start, end in zip(edges[:-1], edges[1:]):
        out = run(frame[(frame.TimeStamp >= start) & (frame.TimeStamp < end)], aggregations, carry, **kwargs)
        carry = out['unmatched_events']
        outputs.append(out)
    return outputs, carry


def test_each_family_writes_only_its_own_codes():
    frame = events(60)
    assert set(run(frame, ARRIVALS)['unmatched_events'].EventId) <= ARRIVAL_CODES | SPILLBACK_CODES | {936}
    assert set(run(frame, PED)['unmatched_events'].EventId) <= PED_CODES | {936}
    assert set(run(frame, [{'name': 'arrival_pattern', 'params': {}}])['unmatched_events'].EventId) <= {960, 961, 962, 963, 936}
    # 942 only with remove_incomplete, for a table that writes a bin later than has_data, written once a bin
    both = run(frame, [HAS_DATA, *ARRIVALS, *PED, {'name': 'approach_delay', 'params': {}}], remove_incomplete=True)['unmatched_events']
    assert (both.EventId == 942).any()
    assert not both[both.EventId == 942].duplicated().any()


def test_parameter_holds_the_phase_or_the_input():
    carry = run(events(60), [*ARRIVALS, *PED])['unmatched_events']
    assert set(carry[carry.EventId.isin(ARRIVAL_CODES)].Parameter) == {PHASE}
    assert set(carry[carry.EventId.isin(PED_CODES)].Parameter) == {PED_INPUT}


@pytest.mark.parametrize('minutes', [
    [60, 120, 180],                  # on the bin
    [7, 66, 133, 222],        # every bin
])
def test_everything_in_one_run_equals_one_big_run(minutes):
    frame = events()
    big = run(frame, ALL_TOGETHER)
    outputs, _ = cut_at_minutes(frame, ALL_TOGETHER, minutes)
    for table in TABLES:
        cut = pd.concat([o[table] for o in outputs], ignore_index=True)
        assert not cut.duplicated(KEYS).any(), table
        assert len(cut) > 0, table
        pd.testing.assert_frame_equal(cut.sort_values(KEYS).reset_index(drop=True), big[table].sort_values(KEYS).reset_index(drop=True),
                                      check_dtype=False, check_exact=False, rtol=1e-5)
    # timeline: the state rows must not change it either
    columns = list(big['timeline'].columns)
    cut = pd.concat([o['timeline'] for o in outputs], ignore_index=True)
    alone, _ = cut_at_minutes(frame, [HAS_DATA, TIMELINE], minutes)
    pd.testing.assert_frame_equal(cut.sort_values(columns).reset_index(drop=True),
                                  pd.concat([o['timeline'] for o in alone], ignore_index=True).sort_values(columns).reset_index(drop=True))


def test_state_rows_are_not_invalidated_by_what_invalidates_an_interval():
    """A clock update and a silence both mark open intervals invalid, and neither may touch a state row."""
    frame = events(60)
    update = pd.DataFrame({'TimeStamp': [T0 + pd.Timedelta(minutes=59)], 'DeviceId': 'A', 'EventId': [181], 'Parameter': [0]})
    first = run(pd.concat([frame, update]), ALL_TOGETHER)['unmatched_events']
    later = events(60)
    later['TimeStamp'] += pd.Timedelta(hours=3)
    second = run(pd.concat([later, update.assign(TimeStamp=later.TimeStamp.max())]), ALL_TOGETHER, first)['unmatched_events']
    for carry in (first, second):
        state = carry[carry.EventId.between(938, 947)]
        assert len(state) > 0
        assert state.IsValid.all()


def test_a_state_row_with_a_false_flag_does_not_reach_an_interval():
    """timeline inherits a false IsValid from a previous row at the same moment, but only from interval starts."""
    frame = events(60)
    first = run(frame, [HAS_DATA, TIMELINE, *ARRIVALS, *PED])['unmatched_events']
    marked = first.copy()
    marked.loc[marked.EventId.between(938, 947), 'IsValid'] = False
    later = events(60)
    later['TimeStamp'] += pd.Timedelta(hours=1)
    plain = run(later, ALL_TOGETHER, first)['timeline']
    flagged = run(later, ALL_TOGETHER, marked)['timeline']
    columns = list(plain.columns)
    pd.testing.assert_frame_equal(plain.sort_values(columns).reset_index(drop=True), flagged.sort_values(columns).reset_index(drop=True))


def test_other_measures_do_not_see_the_state_rows():
    """timeline and yellow_red read raw_data_all, the state rows must not be in it."""
    frame = events(60)
    carry = run(frame, [HAS_DATA, TIMELINE, *ARRIVALS, *PED])['unmatched_events']
    later = frame.assign(TimeStamp=frame.TimeStamp + pd.Timedelta(hours=1))
    with SignalDataProcessor(raw_data=later, detector_config=CONFIG, bin_size=15, verbose=0, aggregations=ALL_TOGETHER,
                             unmatched_event_settings={'df_or_path': carry, 'max_days_old': 14}) as p:
        p.load()
        seen = {row[0] for row in p.conn.execute("SELECT DISTINCT EventId FROM raw_data_all").fetchall()}
    assert not seen & set(range(938, 948))


def test_old_unmatched_file_rows_keep_their_old_meaning():
    """Rows from before 943 to 947 existed, and an interval start at 901 to 904, are left to what reads them."""
    old = pd.DataFrame({'TimeStamp': [T0, T0], 'DeviceId': 'A', 'EventId': [1, 902], 'Parameter': [PHASE, PHASE], 'IsValid': [True, True]})
    out = run(events(30), [HAS_DATA, TIMELINE, *PED], old)
    assert {1, 902} <= set(out['unmatched_previous'].EventId)


def test_the_range_covers_every_code_in_the_code():
    low, high = (int(x) for x in re.fullmatch(r'BETWEEN (\d+) AND (\d+)', STATE_EVENT_IDS).groups())
    codes = set()
    paths = glob(os.path.join(SRC, 'queries', '*.sql')) + glob(os.path.join(SRC, '*.py'))
    for path in paths:
        with open(path) as f:
            text = f.read()
        codes |= {int(code) for code in re.findall(r'\b(9[2-9]\d)\b', text)}
        # Every filter of state rows goes through the one range
        assert not re.search(r'NOT BETWEEN 931 AND', text), path
    assert codes, 'no codes found'
    assert (low, high) == (min(codes), max(codes))

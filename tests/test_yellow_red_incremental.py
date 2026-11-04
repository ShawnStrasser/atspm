"""yellow_red follows bin_size and gives the same answer however its runs are cut.

A cycle runs from one green begin to the next. Each actuation belongs to its cycle, and its row to the bin of that cycle's
red begin. So a bin is not known until the last cycle with a red in it has finished, and its events wait for a later run
in unmatched_events (synthetic EventIds 951 to 954).
"""
import pandas as pd
import pytest
from src.atspm import SignalDataProcessor

T0 = pd.Timestamp('2026-09-01 08:00:00')
PHASE, DETECTOR = 2, 3
CONFIG = pd.DataFrame({'DeviceId': ['A'], 'Phase': [PHASE], 'Parameter': [DETECTOR], 'Function': ['Yellow_Red']})
YR = {'name': 'yellow_red', 'params': {'latency_offset_seconds': 0}}
KEYS = ['TimeStamp', 'DeviceId', 'Phase', 'Signal_State', 'Red_Offset']
YR_CODES = {951, 952, 953, 954}


def at(seconds, event, parameter=PHASE, device='A'):
    return (T0 + pd.Timedelta(seconds=seconds), device, event, parameter)


def frame(*rows):
    return pd.DataFrame(rows, columns=['TimeStamp', 'DeviceId', 'EventId', 'Parameter']).sort_values(['TimeStamp', 'EventId']).reset_index(drop=True)


def cycle(start, green=30, yellow=4):
    """Green begin, yellow begin and red begin of one cycle from start, as rows."""
    return [at(start, 1), at(start + green, 8), at(start + green + yellow, 10)]


def process(events, detectors=CONFIG, aggregations=None, carry=None, bin_size=15, **kwargs):
    settings = None
    if carry is not None:
        settings = {'df_or_path': '' if isinstance(carry, str) else carry, 'max_days_old': 14}
    with SignalDataProcessor(raw_data=events, detector_config=detectors, bin_size=bin_size, verbose=0, unmatched_event_settings=settings,
                             aggregations=aggregations or [YR], **kwargs) as p:
        p.load()
        p.aggregate()
        tables = [row[0] for row in p.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        return {t: p.conn.execute(f'SELECT * FROM "{t}"').df() for t in tables}


def cut_runs(events, cuts, detectors=CONFIG, aggregations=None, bin_size=15, **kwargs):
    edges = [events.TimeStamp.min()] + sorted(cuts) + [events.TimeStamp.max() + pd.Timedelta(seconds=1)]
    outputs, carry = [], 'first'
    for start, end in zip(edges, edges[1:]):
        result = process(events[(events.TimeStamp >= start) & (events.TimeStamp < end)], detectors, aggregations, carry, bin_size, **kwargs)
        carry = result['unmatched_events']
        outputs.append(result['yellow_red'])
    return pd.concat(outputs, ignore_index=True), carry


def seconds(*values):
    return [T0 + pd.Timedelta(seconds=v) for v in values]


def same_rows(a, b):
    assert not b.duplicated(KEYS).any(), 'a row was written twice'
    a, b = a.sort_values(KEYS).reset_index(drop=True), b.sort_values(KEYS).reset_index(drop=True)
    pd.testing.assert_frame_equal(a, b, check_dtype=False)


def steady(minutes=60, cycle_seconds=60):
    """A signal cycling steadily, with an actuation on green, on yellow and two on red in each cycle."""
    rows = []
    for start in range(0, minutes * 60, cycle_seconds):
        rows += cycle(start)
        rows += [at(start + 10.2, 82, DETECTOR), at(start + 31.3, 82, DETECTOR), at(start + 35.4, 82, DETECTOR), at(start + 50.6, 82, DETECTOR)]
    rows.append(at(minutes * 60 + 1, 1))  # the last cycle ends
    return frame(*rows)


# ---- bin_size

@pytest.mark.parametrize('bin_size, stamps', [(5, 12), (15, 4), (60, 1)])
def test_bins_follow_bin_size(bin_size, stamps):
    out = process(steady(60), bin_size=bin_size)['yellow_red']
    assert out.TimeStamp.nunique() == stamps
    assert all(t.minute % bin_size == 0 and t.second == 0 for t in out.TimeStamp)
    assert out.Count.sum() == 4 * 60  # the same actuations in any bin size


def test_a_row_goes_to_the_bin_of_its_red_begin():
    # Red begins at 14:50 and the actuation comes 20 s later, in the next bin by the clock
    events = frame(*cycle(856), at(910, 82, DETECTOR), at(1200, 1))
    out = process(events)['yellow_red']
    assert out.TimeStamp.tolist() == [T0]
    assert out.Red_Offset.tolist() == [20.0]


# ---- across runs

def test_a_red_interval_across_a_cut_keeps_all_its_actuations():
    events = frame(*cycle(0), at(40, 82, DETECTOR), at(50, 82, DETECTOR), at(100, 1), at(101, 7, 8), at(899.9, 7, 8))
    big = process(events, carry='first')
    cut, _ = cut_runs(events, seconds(45))
    assert cut.Count.sum() == 2
    same_rows(big['yellow_red'], cut)


def test_a_bin_waits_for_the_last_cycle_with_a_red_in_it():
    # The red begins in the first bin, and the cycle is still open when the first run reaches the end of that bin
    events = frame(*cycle(860), at(899, 82, DETECTOR), at(899.9, 7, 8), at(910, 82, DETECTOR), at(950, 1), at(1000, 7, 8))
    cut = seconds(900)
    run1 = process(events[events.TimeStamp < cut[0]], carry='first')
    assert len(run1['yellow_red']) == 0
    assert set(run1['unmatched_events'].EventId) >= YR_CODES
    run2 = process(events[events.TimeStamp >= cut[0]], carry=run1['unmatched_events'])
    out = run2['yellow_red'].sort_values('Red_Offset')
    assert out.TimeStamp.tolist() == [T0, T0]
    assert out.Red_Offset.tolist() == [5.0, 16.0]


def test_a_phase_resting_on_red_across_runs_holds_its_bin_until_the_next_green():
    events = frame(*cycle(100), at(200, 82, DETECTOR), at(1000, 7, 8), at(1000.5, 82, DETECTOR), at(2000, 7, 8), at(2500, 1), at(2600, 7, 8))
    big = process(events, carry='first')
    cut, _ = cut_runs(events, seconds(900, 1800))
    assert cut.Count.sum() == 2
    same_rows(big['yellow_red'], cut)


def test_a_finished_cycle_in_a_bin_with_data_to_its_end_is_written_by_that_run():
    # Green comes back before the end of the bin, and the phase has nothing open with a red in it
    events = frame(*cycle(100), at(150, 82, DETECTOR), at(200, 1), at(899.9, 7, 8))
    run1 = process(events, carry='first')
    assert run1['yellow_red'].TimeStamp.tolist() == [T0]
    # Only the open green is left waiting
    assert set(run1['unmatched_events'].EventId) == {951}


def test_a_late_actuation_shifted_by_latency_stays_in_its_cycle():
    # With 1.5 s latency an on event 1 s after green begin belongs to the cycle before, and the run stopped before it
    aggregation = {'name': 'yellow_red', 'params': {'latency_offset_seconds': 1.5}}
    events = frame(*cycle(0), at(100, 1), at(100.5, 7, 8), at(101, 82, DETECTOR), at(150, 1), at(899.9, 7, 8))
    big = process(events, aggregations=[aggregation], carry='first')
    assert big['yellow_red'].Count.sum() == 1
    cut, _ = cut_runs(events, seconds(100.7), aggregations=[aggregation])
    same_rows(big['yellow_red'], cut)


def test_a_cycle_missing_an_event_is_dropped_across_runs_as_in_one_run():
    # The first cycle has no yellow. It is thrown out, whether the cut falls in it or not
    events = frame(at(0, 1), at(34, 10), at(40, 82, DETECTOR), at(100, 1), at(130, 8), at(134, 10), at(140, 82, DETECTOR), at(200, 1), at(899.9, 7, 8))
    big = process(events, carry='first')
    assert big['yellow_red'].Red_Offset.tolist() == [6.0]
    for cut in (20, 37, 70, 137, 150):
        same_rows(big['yellow_red'], cut_runs(events, seconds(cut))[0])


@pytest.mark.parametrize('minutes', [
    [60],                                       # on the hour
    [15, 30, 45],                               # every bin
    [7, 22.5, 41.1],                            # inside bins
    [m + 0.31 for m in range(1, 60, 3)],        # many, inside cycles
])
def test_cuts_equal_one_big_run(minutes):
    events = steady(90)
    big = process(events, carry='first')
    cut, _ = cut_runs(events, [T0 + pd.Timedelta(minutes=m) for m in minutes])
    same_rows(big['yellow_red'], cut)


@pytest.mark.parametrize('bin_size', [5, 60])
def test_cuts_equal_one_big_run_at_other_bin_sizes(bin_size):
    events = steady(180)
    big = process(events, carry='first', bin_size=bin_size)
    cut, _ = cut_runs(events, [T0 + pd.Timedelta(minutes=m) for m in (17, 60, 121.5)], bin_size=bin_size)
    same_rows(big['yellow_red'], cut)


def test_written_bins_equal_a_run_with_no_file():
    events = steady(90)
    no_file = process(events)['yellow_red']
    cut, _ = cut_runs(events, [T0 + pd.Timedelta(minutes=m) for m in (15, 30, 45, 60, 75)])
    written = cut.TimeStamp.max()
    same_rows(no_file[no_file.TimeStamp <= written], cut)


def test_a_run_with_no_file_writes_all_it_has():
    # The open last cycle is written as it stands, as it always was
    events = frame(*cycle(0), at(40, 82, DETECTOR))
    assert process(events)['yellow_red'].Count.tolist() == [1.0]
    assert process(events, carry='first')['yellow_red'].empty


def test_an_empty_run_keeps_what_it_was_given():
    events = frame(*cycle(0), at(40, 82, DETECTOR), at(899.9, 7, 8))
    carry = process(events, carry='first')['unmatched_events']
    empty = process(events.iloc[0:0], carry=carry)
    assert len(empty['yellow_red']) == 0
    kept = lambda df: df[df.EventId.isin(YR_CODES)].sort_values(['EventId', 'TimeStamp']).reset_index(drop=True)
    pd.testing.assert_frame_equal(kept(carry), kept(empty['unmatched_events']), check_dtype=False)


def test_two_detectors_at_the_same_moment_across_a_cut_stay_two_vehicles():
    detectors = pd.DataFrame({'DeviceId': ['A', 'A'], 'Phase': [PHASE, PHASE], 'Parameter': [3, 4], 'Function': ['Yellow_Red', 'Yellow_Red']})
    events = frame(*cycle(0), at(40, 82, 3), at(40, 82, 4), at(899.9, 7, 8), at(950, 1), at(1000, 7, 8))
    cut, _ = cut_runs(events, seconds(900), detectors)
    assert cut.Count.tolist() == [2.0]


def test_min_red_offset_still_applies_across_runs():
    aggregation = {'name': 'yellow_red', 'params': {'latency_offset_seconds': 0, 'min_red_offset': -2}}
    events = steady(30)
    big = process(events, aggregations=[aggregation], carry='first')['yellow_red']
    assert big.Red_Offset.min() >= -2
    same_rows(big, cut_runs(events, seconds(610, 1205), aggregations=[aggregation])[0])


def test_the_carried_events_are_only_as_long_as_needed():
    # Ten bins of steady cycles: only the unfinished end is left over, not the whole history
    carry = cut_runs(steady(150), [T0 + pd.Timedelta(minutes=15 * i) for i in range(1, 10)])[1]
    assert (carry[carry.EventId.isin(YR_CODES)].TimeStamp >= T0 + pd.Timedelta(minutes=120)).all()


def test_timeline_in_the_list_does_not_change_it():
    # timeline keeps its own open rows, some of them real codes, in the same file
    has_data = {'name': 'has_data', 'params': {'no_data_min': 15, 'min_data_points': 1}}
    timeline = {'name': 'timeline', 'params': {'min_duration': 0.2, 'cushion_time': 60}}
    events = steady(60)
    cuts = [T0 + pd.Timedelta(minutes=m) for m in (15, 37.4, 45)]
    alone, _ = cut_runs(events, cuts)
    together, _ = cut_runs(events, cuts, aggregations=[has_data, timeline, YR])
    same_rows(alone, together)


def test_timeline_does_not_see_the_carried_events():
    has_data = {'name': 'has_data', 'params': {'no_data_min': 15, 'min_data_points': 1}}
    timeline = {'name': 'timeline', 'params': {'min_duration': 0.2, 'cushion_time': 60}}
    events = steady(60)
    cuts = [T0 + pd.Timedelta(minutes=m) for m in (15, 37.4, 45)]
    edges = [events.TimeStamp.min()] + cuts + [events.TimeStamp.max() + pd.Timedelta(seconds=1)]
    with_yr, without, carry_a, carry_b = [], [], '', ''
    for start, end in zip(edges, edges[1:]):
        chunk = events[(events.TimeStamp >= start) & (events.TimeStamp < end)]
        a = process(chunk, aggregations=[has_data, timeline, YR], carry=carry_a)
        b = process(chunk, aggregations=[has_data, timeline], carry=carry_b)
        carry_a, carry_b = a['unmatched_events'], b['unmatched_events']
        with_yr.append(a['timeline'])
        without.append(b['timeline'])
    columns = list(with_yr[0].columns)
    pd.testing.assert_frame_equal(pd.concat(with_yr).sort_values(columns).reset_index(drop=True), pd.concat(without).sort_values(columns).reset_index(drop=True))


def test_remove_incomplete_keeps_a_bin_written_in_a_later_run():
    # The bin of the red passed has_data in the first run, and the red only ends in the third
    has_data = {'name': 'has_data', 'params': {'no_data_min': 15, 'min_data_points': 1}}
    rows = [*cycle(100), at(200, 82, DETECTOR)]
    rows += [at(10 + 60 * i, 7, 8) for i in range(45)] + [at(899.9, 7, 8), at(1799.9, 7, 8)]
    rows += [at(2000, 1), at(2699.9, 7, 8)]
    events = frame(*rows)
    aggregations = [has_data, YR]
    big = process(events, aggregations=aggregations, carry='first', remove_incomplete=True)['yellow_red']
    cut, _ = cut_runs(events, seconds(900, 1800), aggregations=aggregations, remove_incomplete=True)
    assert cut.Count.tolist() == [1.0]
    same_rows(big, cut)

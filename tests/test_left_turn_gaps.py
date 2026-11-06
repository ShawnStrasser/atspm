"""left_turn_gaps, from how it should work.

On a through phase, the road is empty from when the last occupied lane goes off until any lane comes on, cut to the green and
yellow, event 1 to the next red. Each gap has a length, and the seconds of the green and yellow fall in slices of that length, 0.1 s
wide from 3 s to 10 s, with what is left at Gap_Length 0. So the share at a critical gap is Seconds at or above it over all Seconds.

The times here are seconds from T0.
"""
import random

import pandas as pd
import pytest
from src.atspm import SignalDataProcessor

T0 = pd.Timestamp('2026-09-01 08:00:00')
PHASE, LANE, LANE_2 = 2, 3, 4
BIN = 900
GAPS = {'name': 'left_turn_gaps', 'params': {}}
AOG = {'name': 'arrival_on_green', 'params': {}}
DELAY = {'name': 'approach_delay', 'params': {}}
HAS_DATA = {'name': 'has_data', 'params': {}}
KEYS = ['TimeStamp', 'DeviceId', 'Phase', 'Gap_Length']
OWN_CODES = {927, 928, 929, 930}


def at(seconds, event, parameter, device='A'):
    return (T0 + pd.Timedelta(seconds=seconds), device, event, parameter)


def frame(*rows):
    return pd.DataFrame(rows, columns=['TimeStamp', 'DeviceId', 'EventId', 'Parameter']).sort_values(['TimeStamp', 'EventId']).reset_index(drop=True)


def config(*detectors, device='A', function='Count'):
    return pd.DataFrame({'DeviceId': device, 'Phase': [p for p, _ in detectors], 'Parameter': [d for _, d in detectors], 'Function': function})


ONE_LANE = config((PHASE, LANE))
TWO_LANES = config((PHASE, LANE), (PHASE, LANE_2))


def car(on, off, lane=LANE, device='A'):
    return [at(on, 82, lane, device), at(off, 81, lane, device)]


def green(start, red, phase=PHASE, device='A'):
    return [at(start, 1, phase, device), at(red, 10, phase, device)]


def end_at(seconds, device='A'):
    """An event of a phase with no detector, to carry the data to a time."""
    return [at(seconds, 1, 15, device)]


def process(events, detectors=ONE_LANE, aggregations=None, carry=None, bin_size=15, max_days_old=14, **kwargs):
    settings = None
    if carry is not None:
        settings = {'df_or_path': '' if isinstance(carry, str) else carry, 'max_days_old': max_days_old}
    with SignalDataProcessor(raw_data=events, detector_config=detectors, bin_size=bin_size, verbose=0, unmatched_event_settings=settings,
                             aggregations=aggregations or [GAPS], **kwargs) as p:
        p.load()
        p.aggregate()
        tables = [row[0] for row in p.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        return {t: p.conn.execute(f'SELECT * FROM "{t}"').df() for t in tables}


def gaps(events, detectors=ONE_LANE, **kwargs):
    return process(events, detectors, **kwargs)['left_turn_gaps']


def read(table):
    """The rows as {(seconds of the bin from T0, Gap_Length): (Gaps, Seconds)}."""
    return {(int((r.TimeStamp - T0).total_seconds()), float(r.Gap_Length)): (int(r.Gaps), round(float(r.Seconds), 3)) for r in table.itertuples()}


def bin_0(events, detectors=ONE_LANE, **kwargs):
    """The rows of a run with one bin as {Gap_Length: (Gaps, Seconds)}."""
    return {length: row for (when, length), row in read(gaps(events, detectors, **kwargs)).items() if when == 0}


def share(table, critical):
    """The dashboard's calculation."""
    return table.Seconds[table.Gap_Length >= critical].astype(float).sum() / table.Seconds.astype(float).sum()


def sort(table, keys=KEYS):
    return table.sort_values(keys).reset_index(drop=True)


def cut_runs(events, cuts, detectors=ONE_LANE, aggregations=None, **kwargs):
    """The events cut into runs at the given seconds, each from the state of the one before. Returns each run's outputs."""
    edges = [events.TimeStamp.min()] + [T0 + pd.Timedelta(seconds=c) for c in sorted(cuts)] + [events.TimeStamp.max() + pd.Timedelta(seconds=1)]
    outputs, carry = [], ''
    for start, end in zip(edges, edges[1:]):
        window = events[(events.TimeStamp >= start) & (events.TimeStamp < end)].reset_index(drop=True)
        out = process(window, detectors, aggregations, carry, **kwargs)
        outputs.append(out)
        carry = out['unmatched_events']
    return outputs


def written(outputs, table='left_turn_gaps'):
    frames = [o[table] for o in outputs if table in o and len(o[table])]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=KEYS)


def own_state(out):
    state = out['unmatched_events']
    return state[state.EventId.isin(OWN_CODES)]


def same_rows(a, b, keys=KEYS):
    pd.testing.assert_frame_equal(sort(a, keys), sort(b, keys), check_dtype=False)


# ---------------------------------------------------------------- the design's table: green and yellow 0-30 s

def test_one_lane_gives_gaps_of_2_8_and_9():
    events = frame(*green(0, 30), *car(2, 12), *car(20, 21), *end_at(899.9))
    assert bin_0(events) == {0: (1, 13), 8: (1, 8), 9: (1, 9)}


def test_a_second_lane_inside_the_first_cars_time_changes_nothing():
    events = frame(*green(0, 30), *car(2, 12), *car(20, 21), *car(5, 6, LANE_2), *end_at(899.9))
    assert bin_0(events, TWO_LANES) == {0: (1, 13), 8: (1, 8), 9: (1, 9)}


def test_lanes_that_overlap_leave_the_road_busy():
    events = frame(*green(0, 30), *car(2, 12), *car(20, 21), *car(11, 20.5, LANE_2), *car(21, 22, LANE_2), *end_at(899.9))
    assert bin_0(events, TWO_LANES) == {0: (1, 22), 8: (1, 8)}


def test_a_car_sitting_on_the_zone_is_no_gap():
    events = frame(*green(0, 30), *car(1, 29), *end_at(899.9))
    assert bin_0(events) == {0: (2, 30)}


def test_a_bin_edge_inside_a_gap_keeps_17_large_seconds():
    """The first case with a bin edge at 16 s: 0 gaps and 4 s, then 2 gaps and 13 s, over 5.3 s."""
    base = BIN - 16
    events = frame(*green(base, base + 30), *car(base + 2, base + 12), *car(base + 20, base + 21), *end_at(1799.9))
    assert read(gaps(events)) == {(0, 0): (1, 12), (0, 8): (0, 4), (BIN, 0): (0, 1), (BIN, 8): (1, 4), (BIN, 9): (1, 9)}


# ---------------------------------------------------------------- the columns

def test_columns_and_types_and_nothing_else():
    events = frame(*green(0, 30), *car(2, 12), *end_at(899.9))
    with SignalDataProcessor(raw_data=events, detector_config=ONE_LANE, bin_size=15, verbose=0, aggregations=[GAPS]) as p:
        p.load()
        p.aggregate()
        described = p.conn.execute("DESCRIBE left_turn_gaps").fetchall()
    assert [(c[0], c[1]) for c in described] == [('TimeStamp', 'TIMESTAMP'), ('DeviceId', 'VARCHAR'), ('Phase', 'SMALLINT'),
                                                 ('Gap_Length', 'DOUBLE'), ('Gaps', 'INTEGER'), ('Seconds', 'FLOAT')]


def test_device_id_keeps_its_type():
    events = frame(*green(0, 30), *car(2, 12), *end_at(899.9))
    events['DeviceId'] = 7
    with SignalDataProcessor(raw_data=events, detector_config=ONE_LANE.assign(DeviceId=7), bin_size=15, verbose=0, aggregations=[GAPS]) as p:
        p.load()
        p.aggregate()
        input_type = p.conn.execute("SELECT typeof(DeviceId) FROM raw_data LIMIT 1").fetchall()[0][0]
        assert p.conn.execute("SELECT typeof(DeviceId) FROM left_turn_gaps LIMIT 1").fetchall()[0][0] == input_type


@pytest.mark.parametrize('params', [{'threshold': 5.3}, {'latency_offset_seconds': 0}])
def test_it_takes_no_parameters(params):
    with pytest.raises(ValueError, match='unknown parameters'):
        SignalDataProcessor(raw_data=frame(*end_at(1)), detector_config=ONE_LANE, bin_size=15, verbose=0, aggregations=[{'name': 'left_turn_gaps', 'params': params}])


# ---------------------------------------------------------------- a gap's start and end

def test_a_green_with_no_car_is_one_gap_of_all_of_it():
    events = frame(*car(-20, -15), *green(0, 40), *end_at(899.9))
    assert bin_0(events) == {10: (1, 40)}


def test_a_gap_starts_when_a_car_on_at_green_start_goes_off():
    events = frame(*car(-5, 3), *green(0, 30), *end_at(899.9))
    assert bin_0(events) == {0: (0, 3), 10: (1, 27)}


def test_a_gap_spanning_two_greens_counts_once_in_each_cut_to_the_green():
    events = frame(*car(-5, 3), *green(0, 20), *green(60, 80), *car(65, 66), *end_at(899.9))
    assert bin_0(events) == {0: (0, 4), 5: (1, 5), 10: (2, 31)}


def test_a_gap_cut_off_by_red_counts():
    events = frame(*green(0, 12), *car(2, 5), *end_at(899.9))
    assert bin_0(events) == {0: (1, 5), 7: (1, 7)}


def test_yellow_is_part_of_it():
    events = frame(at(0, 1, PHASE), at(20, 8, PHASE), at(24, 10, PHASE), *car(2, 3), *end_at(899.9))
    assert bin_0(events) == {0: (1, 3), 10: (1, 21)}


def test_time_on_red_is_in_no_row():
    events = frame(*green(0, 10), *car(4, 5), *car(50, 60), *end_at(899.9))
    assert bin_0(events) == {0: (0, 1), 4: (1, 4), 5: (1, 5)}


def test_repeated_ons_and_offs_change_nothing():
    plain = frame(*green(0, 30), *car(2, 12), *end_at(899.9))
    repeats = frame(*green(0, 30), *car(2, 12), at(5, 82, LANE), at(20, 81, LANE), *end_at(899.9))
    assert bin_0(repeats) == bin_0(plain)


def test_an_on_and_off_of_one_lane_in_one_stamp_change_nothing_on_an_empty_road():
    events = frame(*car(-9, -8), *green(0, 30), at(3, 82, LANE), at(3, 81, LANE), *end_at(899.9))
    assert bin_0(events) == {10: (1, 30)}


def test_an_on_and_off_of_one_lane_in_one_stamp_change_nothing_on_a_busy_road():
    events = frame(at(-9, 82, LANE), *green(0, 30), at(3, 82, LANE), at(3, 81, LANE), at(8, 81, LANE), *end_at(899.9))
    assert bin_0(events) == {0: (0, 8), 10: (1, 22)}


def test_a_lane_is_empty_until_its_first_on():
    """As the design's first gap of 2 s, in a green before the lane has logged anything."""
    events = frame(*green(0, 30), at(10, 82, LANE), at(11, 81, LANE), *end_at(899.9))
    assert bin_0(events) == {0: (0, 1), 10: (2, 29)}


def test_a_first_off_says_nothing_about_before_it():
    events = frame(*green(0, 30), at(10, 81, LANE), *end_at(899.9))
    assert bin_0(events) == {10: (1, 30)}


def test_a_zone_that_never_logged_reads_as_an_empty_road():
    """On the deferred list. It can't be told from a road with no cars while it is happening."""
    events = frame(*green(0, 30), *end_at(899.9))
    assert bin_0(events) == {10: (1, 30)}


# ---------------------------------------------------------------- the green

def test_end_of_yellow_reads_as_red_where_the_signal_logs_no_red_clearance():
    events = frame(at(0, 1, PHASE), at(20, 8, PHASE), at(24, 9, PHASE), *car(2, 3), at(60, 1, PHASE), at(90, 9, PHASE), *end_at(899.9))
    assert bin_0(events) == {0: (1, 3), 10: (2, 21 + 30)}


def test_both_9_and_10_at_one_moment_are_one_red():
    events = frame(at(0, 1, PHASE), at(24, 9, PHASE), at(24, 10, PHASE), *car(2, 3), *end_at(899.9))
    assert bin_0(events) == {0: (1, 3), 10: (1, 21)}


def test_a_second_green_with_no_red_changes_nothing():
    events = frame(at(0, 1, PHASE), at(10, 1, PHASE), at(20, 10, PHASE), *car(2, 3), *end_at(899.9))
    assert bin_0(events) == {0: (1, 3), 10: (1, 17)}


def test_a_red_with_no_green_before_it_adds_nothing():
    events = frame(at(5, 10, PHASE), *car(2, 3), *end_at(899.9))
    assert len(gaps(events)) == 0


def test_a_phase_with_no_green_in_a_bin_has_no_row():
    events = frame(*green(0, 30), *car(2, 3), *car(1000, 1001), *end_at(1799.9))
    assert {when for when, _ in read(gaps(events))} == {0}


def test_a_green_with_no_red_yet_runs_to_the_last_event_with_a_run_with_no_state():
    events = frame(at(0, 1, PHASE), *car(2, 3), *end_at(40))
    assert bin_0(events) == {0: (1, 3), 10: (0, 37)}


# ---------------------------------------------------------------- which detectors

def test_count_zones_are_used_where_the_phase_has_them():
    detectors = pd.concat([config((PHASE, LANE), function='Count'), config((PHASE, LANE_2), function='Presence')], ignore_index=True)
    events = frame(*green(0, 30), *car(2, 12), *car(14, 29, LANE_2), *end_at(899.9))
    assert bin_0(events, detectors) == {0: (1, 12), 10: (1, 18)}


def test_presence_is_used_where_the_phase_has_no_count_zone():
    detectors = pd.concat([config((PHASE, LANE), function='Presence'), config((6, LANE_2), function='Count')], ignore_index=True)
    events = frame(*green(0, 30), *car(2, 12), *car(14, 29, LANE_2), *end_at(899.9))
    assert bin_0(events, detectors) == {0: (1, 12), 10: (1, 18)}


def test_each_phase_chooses_for_itself():
    detectors = pd.concat([config((PHASE, LANE), function='Presence'), config((6, LANE_2), function='Count')], ignore_index=True)
    events = frame(*green(0, 30), *green(0, 30, 6), *car(2, 12), *car(14, 29, LANE_2), *end_at(899.9))
    table = gaps(events, detectors)
    assert set(table.Phase) == {2, 6}
    assert read(table[table.Phase == 6]) == {(0, 0): (1, 16), (0, 10): (1, 14)}


@pytest.mark.parametrize('function', ['Advance', 'Other'])
def test_other_zones_give_no_row(function):
    events = frame(*green(0, 30), *car(2, 12), *end_at(899.9))
    assert len(gaps(events, config((PHASE, LANE), function=function))) == 0


@pytest.mark.parametrize('phase', [1, 3])
def test_only_phases_2_4_6_and_8_have_rows(phase):
    events = frame(*green(0, 30, phase), *car(2, 12), *end_at(899.9))
    assert len(gaps(events, config((phase, LANE)))) == 0


@pytest.mark.parametrize('phase', [2, 4])
def test_each_through_phase_has_rows(phase):
    events = frame(*green(0, 30, phase), *car(2, 12), *end_at(899.9))
    assert set(gaps(events, config((phase, LANE))).Phase) == {phase}


def test_a_detector_on_two_phases_counts_on_each():
    events = frame(*green(0, 30), *green(10, 40, 6), *car(2, 12), *end_at(899.9))
    table = gaps(events, config((PHASE, LANE), (6, LANE)))
    assert read(table[table.Phase == PHASE]) == {(0, 0): (1, 12), (0, 10): (1, 18)}
    assert read(table[table.Phase == 6]) == {(0, 0): (0, 2), (0, 10): (1, 28)}


def test_a_detector_listed_twice_for_a_phase_counts_once():
    events = frame(*green(0, 30), *car(2, 12), *end_at(899.9))
    assert bin_0(events, pd.concat([ONE_LANE, ONE_LANE], ignore_index=True)) == bin_0(events)


# ---------------------------------------------------------------- the slices

@pytest.mark.parametrize('length, label', [(0.1, 0), (2.9, 0)])
def test_a_gap_falls_in_the_slice_of_its_length(length, label):
    events = frame(*green(0, length + 100), *car(-5, 0.5), *car(0.5 + length, length + 100.5), *end_at(899.9))
    table = gaps(events)
    row = table[table.Gaps == 1]
    assert list(row.Gap_Length) == [label]
    if label:
        assert float(row.Seconds.iloc[0]) == pytest.approx(length, abs=1e-3)


@pytest.mark.parametrize("tenths", [41, 53])
def test_a_threshold_typed_as_a_number_reads_its_own_slice(tenths):
    """A FLOAT 4.1 reads below 4.1."""
    length = tenths / 10
    events = frame(*green(0, 200), *car(-5, 1), *car(1 + length, 200.5), *end_at(899.9))
    table = gaps(events)
    assert table.Seconds[table.Gap_Length >= length].astype(float).sum() == pytest.approx(length, abs=1e-3)
    assert table.Seconds[table.Gap_Length >= length + 0.1].astype(float).sum() == 0


def test_seconds_add_up_to_the_green_and_yellow():
    events = frame(*green(0, 30), *green(50, 95), *green(BIN - 10, BIN + 30), *car(2, 12), *car(60, 61), *car(BIN + 1, BIN + 2), *end_at(1799.9))
    table = gaps(events)
    assert table.Seconds.astype(float).sum() == pytest.approx(30 + 45 + 40, abs=1e-3)
    assert table.Seconds[table.TimeStamp == T0].astype(float).sum() == pytest.approx(30 + 45 + 10, abs=1e-3)


def test_the_share_is_seconds_at_or_above_the_gap_over_all_seconds():
    events = frame(*green(0, 30), *car(2, 12), *car(20, 21), *end_at(899.9))
    table = gaps(events)
    assert share(table, 5.3) == pytest.approx(17 / 30)
    assert share(table, 8.5) == pytest.approx(9 / 30)
    assert share(table, 9.1) == 0


def test_short_gaps_are_counted_in_the_row_with_the_rest_of_the_time():
    events = frame(*green(0, 12), *car(1, 2), *car(3, 4), *car(5, 12), *end_at(899.9))
    assert bin_0(events) == {0: (3, 12)}


# ---------------------------------------------------------------- bins

def test_a_gap_over_three_bins_counts_where_it_ends_and_splits_its_seconds():
    events = frame(*car(-9, -8), *green(BIN - 100, 2 * BIN + 50), *car(2 * BIN + 40, 2 * BIN + 41), *end_at(3 * BIN - 0.1))
    assert read(gaps(events)) == {(0, 10): (0, 100), (BIN, 10): (0, BIN), (2 * BIN, 0): (0, 1), (2 * BIN, 10): (1, 40), (2 * BIN, 9): (1, 9)}


def test_a_gap_ending_exactly_on_a_bin_edge_counts_in_the_bin_with_its_seconds():
    events = frame(*car(-9, -8), *green(BIN - 20, BIN + 10), at(BIN, 82, LANE), at(BIN + 1, 81, LANE), *end_at(1799.9))
    assert read(gaps(events)) == {(0, 10): (1, 20), (BIN, 0): (0, 1), (BIN, 9): (1, 9)}


def test_bin_size_is_taken():
    events = frame(*green(290, 310), *car(292, 295), *end_at(599.9))
    assert read(gaps(events, bin_size=5)) == {(0, 0): (1, 5), (0, 10): (0, 5), (300, 10): (1, 10)}


def test_a_gap_keeps_the_slice_of_its_whole_length_on_both_sides_of_a_bin_edge():
    events = frame(*green(BIN - 3, BIN + 10), *car(BIN - 2, BIN - 1), *car(BIN + 5, BIN + 6), *end_at(1799.9))
    assert read(gaps(events)) == {(0, 0): (1, 2), (0, 6): (0, 1), (BIN, 6): (1, 5), (BIN, 0): (0, 1), (BIN, 4): (1, 4)}


# ---------------------------------------------------------------- across runs

def test_a_bin_waits_for_data_10_s_past_its_end():
    events = frame(*green(0, 30), *car(2, 12), *end_at(BIN + 9.9))
    assert len(gaps(events, carry='')) == 0
    events = frame(*green(0, 30), *car(2, 12), *end_at(BIN + 10))
    assert read(gaps(events, carry='')) == {(0, 0): (1, 12), (0, 10): (1, 18)}


def test_a_run_with_no_state_file_writes_all_it_has():
    events = frame(*green(0, 30), *car(2, 12), *end_at(40))
    assert read(gaps(events)) == {(0, 0): (1, 12), (0, 10): (1, 18)}


def test_a_gap_open_at_a_cut_is_measured_from_where_it_started():
    events = frame(*green(BIN - 7, BIN + 5), *car(BIN - 8, BIN - 6), *car(BIN - 1, BIN + 3), *end_at(1799.9), *end_at(2 * BIN + 10))
    outputs = cut_runs(events, [BIN - 3, BIN + 1])
    same_rows(written(outputs), process(events, carry='')['left_turn_gaps'])


def test_a_gap_open_past_10_s_at_a_cut_keeps_its_earlier_seconds():
    events = frame(*car(-9, -8), *green(0, 100), *car(90, 91), *end_at(BIN + 10))
    outputs = cut_runs(events, [20, 50, 70])
    assert read(written(outputs)) == {(0, 0): (0, 1), (0, 9): (1, 9), (0, 10): (1, 90)}


def test_a_long_empty_green_writes_its_bins_as_they_finish():
    events = frame(*car(-9, -8), *green(0, 3 * BIN + 20), *end_at(BIN + 10), *end_at(2 * BIN + 10), *end_at(3 * BIN + 30), *end_at(4 * BIN + 10))
    outputs = cut_runs(events, [BIN + 10.1, 2 * BIN + 10.1, 3 * BIN + 30.1])
    assert read(outputs[0]['left_turn_gaps']) == {(0, 10): (0, BIN)}
    assert read(outputs[1]['left_turn_gaps']) == {(BIN, 10): (0, BIN)}
    assert read(written(outputs)) == {(0, 10): (0, BIN), (BIN, 10): (0, BIN), (2 * BIN, 10): (0, BIN), (3 * BIN, 10): (1, 20)}


def test_a_car_sitting_across_a_cut_stays_on_the_road():
    events = frame(*car(-9, -8), *green(0, 60), *car(10, 50), *end_at(BIN + 10))
    outputs = cut_runs(events, [30])
    assert read(written(outputs)) == {(0, 0): (0, 40), (0, 10): (2, 20)}


def test_one_lane_off_at_a_cut_with_another_on_starts_no_gap():
    events = frame(*car(-9, -8), *green(0, 60), *car(10, 20), *car(15, 50, LANE_2), *end_at(BIN + 10))
    outputs = cut_runs(events, [18], TWO_LANES)
    assert read(written(outputs)) == {(0, 0): (0, 40), (0, 10): (2, 20)}


def test_a_lane_on_for_a_red_and_the_next_green_across_a_cut():
    events = frame(*car(-9, -8), *green(0, 10), at(40, 82, LANE), *green(60, 90), at(70, 81, LANE), *end_at(BIN + 10))
    outputs = cut_runs(events, [50])
    assert read(written(outputs)) == {(0, 0): (0, 10), (0, 10): (2, 30)}


def test_a_green_starting_just_before_a_cut():
    events = frame(*car(-9, -8), *green(29.9, 60), *car(35, 36), *end_at(BIN + 10))
    same_rows(written(cut_runs(events, [30])), process(events, carry='')['left_turn_gaps'])


def test_a_silent_device_writes_nothing_until_it_logs_again_and_then_the_same_rows():
    a = frame(*car(-9, -8), *green(0, 40), *car(10, 11), *end_at(2 * BIN + 100))
    b = frame(*end_at(0, 'B'), *end_at(BIN + 20, 'B'), *end_at(2 * BIN + 20, 'B'))
    events = pd.concat([a, b], ignore_index=True).sort_values(['TimeStamp', 'EventId']).reset_index(drop=True)
    outputs = cut_runs(events, [BIN + 30, 2 * BIN + 30])
    assert len(outputs[0]['left_turn_gaps']) == 0
    assert len(outputs[1]['left_turn_gaps']) == 0
    assert {when for when, _ in read(outputs[2]['left_turn_gaps'])} == {0}
    same_rows(written(outputs), process(events, carry='')['left_turn_gaps'])


def test_a_device_with_a_clock_far_ahead_does_not_write_the_bins_of_the_others():
    a = frame(*car(-9, -8), *green(0, 40), *car(10, 11), *end_at(BIN - 1))
    b = frame(*end_at(90 * BIN, 'B'))
    assert len(gaps(pd.concat([a, b], ignore_index=True), carry='')) == 0


def test_a_run_with_no_events_keeps_the_state():
    events = frame(*car(-9, -8), *green(0, 60), *car(10, 50), *end_at(30))
    first = process(events[events.TimeStamp < T0 + pd.Timedelta(seconds=31)], carry='')
    empty = process(events.iloc[0:0], carry=first['unmatched_events'])
    same_rows(own_state(empty), own_state(first), ['TimeStamp', 'EventId', 'Parameter'])
    assert 'left_turn_gaps' in empty and len(empty['left_turn_gaps']) == 0


def test_a_number_as_device_id_across_a_cut():
    events = frame(*car(-9, -8), *green(0, 60), *car(10, 50), *end_at(BIN + 10))
    events['DeviceId'] = 7
    detectors = ONE_LANE.assign(DeviceId=7)
    same_rows(written(cut_runs(events, [30], detectors)), process(events, detectors, carry='')['left_turn_gaps'])


def test_a_lane_stuck_on_past_max_days_old_stays_on_while_its_device_logs():
    day = 86400
    events = frame(at(-9, 82, LANE), *end_at(day), *end_at(2 * day), *end_at(3 * day), *green(3 * day + 10, 3 * day + 40), *end_at(3 * day + BIN + 20))
    outputs = cut_runs(events, [day + 1, 2 * day + 1, 3 * day + 1], max_days_old=2)
    assert read(written(outputs)) == {(3 * day, 0): (0, 30)}


def test_remove_incomplete_drops_a_bin_with_no_data_across_runs():
    """A green across a bin with no events, on a signal that logs each minute: has_data has no row for it."""
    minutes = [row for t in (*range(-4 * BIN, BIN, 60), *range(2 * BIN + 60, 3 * BIN, 60)) for row in end_at(t)]
    events = frame(*car(-9, -8), at(0, 1, PHASE), *car(10, 11), at(2 * BIN + 30, 10, PHASE), *minutes, *end_at(3 * BIN + 20))
    outputs = cut_runs(events, [BIN, 2 * BIN, 3 * BIN], aggregations=[HAS_DATA, GAPS], remove_incomplete=True)
    table = written(outputs)
    assert set(table.TimeStamp) == {T0, T0 + pd.Timedelta(seconds=2 * BIN)}
    assert set(table.columns) == {'TimeStamp', 'DeviceId', 'Phase', 'Gap_Length', 'Gaps', 'Seconds'}


def test_it_writes_only_its_own_codes():
    events = frame(*car(-9, -8), *green(0, 60), *car(10, 50), *end_at(30))
    state = process(events[events.TimeStamp < T0 + pd.Timedelta(seconds=31)], carry='')['unmatched_events']
    assert set(state.EventId) - {936} <= OWN_CODES


def test_a_state_holding_its_rows_raises_if_it_is_not_listed():
    events = frame(*car(-9, -8), *green(0, 60), *car(10, 50), *end_at(30))
    state = process(events[events.TimeStamp < T0 + pd.Timedelta(seconds=31)], carry='')['unmatched_events']
    with pytest.raises(ValueError, match='left_turn_gaps'):
        process(frame(*end_at(40)), carry=state, aggregations=[AOG])


def test_to_sql_gives_one_statement_to_run():
    with SignalDataProcessor(raw_data=frame(*end_at(1)), detector_config=ONE_LANE, bin_size=15, verbose=0, to_sql=True, aggregations=[GAPS]) as p:
        sql = p.run()['left_turn_gaps']
    assert sql.count('CREATE TABLE left_turn_gaps') == 1


# ---------------------------------------------------------------- beside the other tables

def traffic(seed, minutes=70, devices=('A', 'B')):
    """Random cycling and cars on two phases of two lanes each, at 0.1 s, with a long rest in green, and a car left sitting."""
    rng = random.Random(seed)
    rows = []
    for device in devices:
        for phase, lanes in ((2, (11, 12)), (6, (21, 22))):
            t = rng.uniform(0, 50)
            end = minutes * 60
            while t < end:
                length = rng.choice([rng.uniform(5, 40), rng.uniform(5, 40), rng.uniform(40, 120), rng.uniform(900, 1300) if rng.random() < 0.03 else 20])
                rows += [at(round(t, 1), 1, phase, device), at(round(t + length, 1), 10, phase, device)]
                t += length + rng.uniform(5, 90)
            for lane in lanes:
                t = rng.uniform(0, 60)
                while t < end:
                    on = rng.choice([0.2, 0.3, 1, 2, rng.uniform(1, 8), rng.uniform(20, 60) if rng.random() < 0.05 else 1])
                    rows += [at(round(t, 1), 82, lane, device), at(round(t + on, 1), 81, lane, device)]
                    if rng.random() < 0.01:
                        rows.append(at(round(t + on + 0.1, 1), 81, lane, device))
                    t += on + rng.choice([rng.uniform(0.1, 3), rng.uniform(1, 9), rng.uniform(3, 14), rng.uniform(5, 40)])
        rows.append(at(minutes * 60 + 200, 1, 15, device))
    return pd.DataFrame(rows, columns=['TimeStamp', 'DeviceId', 'EventId', 'Parameter']).drop_duplicates().sort_values(['TimeStamp', 'EventId']).reset_index(drop=True)


TRAFFIC_LANES = pd.concat([config((2, 11), (2, 12), (6, 21), (6, 22), device=d) for d in ('A', 'B')], ignore_index=True)


def by_hand(events, lanes=TRAFFIC_LANES, bin_seconds=BIN):
    """The rules in plain Python, with nothing shared with the query: one big run with no state."""
    out = {}

    def add(when, device, phase, length, gaps, ms):
        key = (when, device, phase, length)
        row = out.setdefault(key, [0, 0])
        row[0] += gaps
        row[1] += ms

    def label(ms, is_gap):
        if not is_gap or ms < 3000:
            return 0.0
        return 10.0 if ms >= 10000 else (ms // 100) / 10

    for (device, phase), group in lanes.groupby(['DeviceId', 'Phase']):
        detectors = set(group.Parameter)
        rows = events[events.DeviceId == device]
        last = int((rows.TimeStamp.max() - T0).total_seconds() * 1000 + 0.5)
        stream = sorted((int((r.TimeStamp - T0).total_seconds() * 1000 + 0.5), r.EventId, r.Parameter) for r in rows.itertuples()
                        if (r.EventId in (1, 9, 10) and r.Parameter == phase) or (r.EventId in (81, 82) and r.Parameter in detectors))
        both = {(when, parameter) for when, event, parameter in stream if event == 82 and (when, 81, parameter) in stream}
        stream = [row for row in stream if not (row[1] in (81, 82) and (row[0], row[2]) in both)]
        on, is_green, start, state = set(), False, None, None  # state: None red, False busy, True empty
        for i, (when, event, parameter) in enumerate(stream):
            if event == 82:
                on.add(parameter)
            elif event == 81:
                on.discard(parameter)
            else:
                is_green = event == 1
            if i + 1 < len(stream) and stream[i + 1][0] == when:
                continue
            new = None if not is_green else not on
            if new != state:
                if state is not None:
                    close(add, device, phase, start, when, state, True, label, bin_seconds)
                start, state = when, new
        if state is not None:
            close(add, device, phase, start, last, state, False, label, bin_seconds)
    return out


def close(add, device, phase, start, end, is_gap, ended, label, bin_seconds):
    if end <= start:
        return
    length = label(end - start, is_gap)
    size = bin_seconds * 1000
    for piece in range(start // size, (end - 1) // size + 1):
        add(piece * bin_seconds, device, phase, length, 0, min(end, (piece + 1) * size) - max(start, piece * size))
    if is_gap and ended:
        add((end - 1) // size * bin_seconds, device, phase, length, 1, 0)


@pytest.mark.parametrize('seed', [1])
def test_one_big_run_equals_the_rules_by_hand(seed):
    events = traffic(seed)
    table = gaps(events, TRAFFIC_LANES)
    got = {(int((r.TimeStamp - T0).total_seconds()), r.DeviceId, int(r.Phase), float(r.Gap_Length)): (int(r.Gaps), round(float(r.Seconds), 3)) for r in table.itertuples()}
    expected = {k: (v[0], round(v[1] / 1000, 3)) for k, v in by_hand(events).items()}
    assert got == expected


@pytest.mark.parametrize('seed', [1])
@pytest.mark.parametrize('cuts', [
    [BIN, 2 * BIN, 3 * BIN, 4 * BIN],
])
def test_cut_runs_equal_one_big_run_and_the_same_state(seed, cuts):
    events = traffic(seed)
    big = process(events, TRAFFIC_LANES, carry='')
    outputs = cut_runs(events, cuts, TRAFFIC_LANES)
    same_rows(written(outputs), big['left_turn_gaps'])
    same_rows(own_state(outputs[-1]), own_state(big), ['TimeStamp', 'DeviceId', 'EventId', 'Parameter'])


def test_each_bin_is_written_once_by_the_first_run_that_can():
    events = traffic(4)
    outputs = cut_runs(events, [BIN * i for i in range(1, 5)], TRAFFIC_LANES)
    assert len(outputs[0]['left_turn_gaps']) == 0
    for i, out in enumerate(outputs[1:], 1):
        assert (out['left_turn_gaps'].TimeStamp.max() - T0).total_seconds() == (i - 1) * BIN
    table = written(outputs)
    assert not table.duplicated(KEYS).any()


def test_listed_beside_others_nothing_changes():
    events = traffic(5)
    detectors = pd.concat([TRAFFIC_LANES, TRAFFIC_LANES.assign(Function='Advance')], ignore_index=True)
    cuts = [BIN * i for i in range(1, 5)]
    others = cut_runs(events, cuts, detectors, [AOG, DELAY])
    together = cut_runs(events, cuts, detectors, [AOG, DELAY, GAPS])
    for table in ('arrival_on_green', 'approach_delay'):
        same_rows(written(together, table), written(others, table), ['TimeStamp', 'DeviceId', 'Phase'])
    same_rows(written(together), written(cut_runs(events, cuts, detectors)))



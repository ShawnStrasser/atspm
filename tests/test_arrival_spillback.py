"""The spillback flag on advance detectors, from how it should work.

A queue that reaches an advance detector stops vehicles on it while the phase is red. When the light turns green the queue
moves, and its vehicles begin actuating the detector while the phase is green, so they read as arriving on green although
they all arrived on red. So a detector that stays ON for more than spillback_on_seconds gets a flag, and every actuation
that begins while it holds the flag counts as arriving on red, whatever the phase shows. The flag stays through the queue
clearing, about one vehicle every 2 seconds, and goes only after the detector has been OFF for spillback_gap_seconds.

The times here are seconds from T0. With DistanceFromStopBar 0 an arrival lands the moment the detector turns on.
"""
import random

import duckdb
import pandas as pd
import pytest
from src.atspm import SignalDataProcessor

T0 = pd.Timestamp('2026-09-01 08:00:00')
PHASE, DETECTOR, OTHER_DETECTOR = 2, 3, 4
T_ON, T_GAP = 6, 5
AOG = {'name': 'arrival_on_green', 'params': {'latency_offset_seconds': 0, 'spillback_on_seconds': T_ON, 'spillback_gap_seconds': T_GAP}}
AOG_OFF = {'name': 'arrival_on_green', 'params': {'latency_offset_seconds': 0, 'spillback_on_seconds': None}}
PLATOON = {'name': 'platoon_ratio', 'params': {}}
KEYS = ['TimeStamp', 'DeviceId', 'Phase']
FLAG, STILL_ON, HELD = 949, 950, 948


def at(seconds, event, parameter, device='A'):
    return (T0 + pd.Timedelta(seconds=seconds), device, event, parameter)


def frame(*rows):
    return pd.DataFrame(rows, columns=['TimeStamp', 'DeviceId', 'EventId', 'Parameter']).sort_values(['TimeStamp', 'EventId']).reset_index(drop=True)


def config(*detectors, distance=0.0):
    return pd.DataFrame({'DeviceId': 'A', 'Phase': [p for p, _ in detectors], 'Parameter': [d for _, d in detectors],
                         'Function': 'Advance', 'DistanceFromStopBar': distance})


ONE_DETECTOR = config((PHASE, DETECTOR))


def actuation(on, off, detector=DETECTOR):
    return [at(on, 82, detector), at(off, 81, detector)]


def actuations(*pairs, detector=DETECTOR):
    return [row for on, off in pairs for row in actuation(on, off, detector)]


def signal(green=60, yellow=100, red=104):
    return [at(0, 10, PHASE), at(green, 1, PHASE), at(yellow, 8, PHASE), at(red, 10, PHASE)]


END_OF_BIN = [at(899.9, 1, 15)]
# Stopped on the detector from 30 s, on red. Green at 60 s and it moves 12 s later, then the queue crosses every 2 s.
QUEUE_CLEARING = [(74 + 2 * i, 74.5 + 2 * i) for i in range(9)]


def queue_events():
    return frame(*signal(), *actuation(30, 72), *actuations(*QUEUE_CLEARING), *END_OF_BIN)


def process(events, detectors=ONE_DETECTOR, aggregations=None, carry=None, **kwargs):
    settings = None
    if carry is not None:
        settings = {'df_or_path': '' if isinstance(carry, str) else carry, 'max_days_old': 14}
    with SignalDataProcessor(raw_data=events, detector_config=detectors, bin_size=15, verbose=0, unmatched_event_settings=settings,
                             aggregations=aggregations or [AOG, PLATOON], **kwargs) as p:
        p.load()
        p.aggregate()
        tables = [row[0] for row in p.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        return {t: p.conn.execute(f'SELECT * FROM "{t}"').df() for t in tables}


def aog(events, detectors=ONE_DETECTOR, **kwargs):
    """The one row of arrival_on_green as (Total_Actuations, vehicles on green)."""
    out = process(events, detectors, **kwargs)['arrival_on_green']
    assert len(out) == 1
    row = out.iloc[0]
    return int(row.Total_Actuations), round(float(row.Percent_AOG) * row.Total_Actuations)


def cut_runs(events, cuts, detectors=ONE_DETECTOR, aggregations=None, **kwargs):
    """The events cut into runs at the given seconds. Returns the output tables concatenated, and the last carry."""
    edges = [events.TimeStamp.min()] + [T0 + pd.Timedelta(seconds=c) for c in sorted(cuts)] + [events.TimeStamp.max() + pd.Timedelta(seconds=1)]
    outputs, carry = {}, 'first'
    for start, end in zip(edges, edges[1:]):
        result = process(events[(events.TimeStamp >= start) & (events.TimeStamp < end)], detectors, aggregations, carry, **kwargs)
        carry = result['unmatched_events']
        for table in ('arrival_on_green', 'platoon_ratio'):
            if table in result:
                outputs.setdefault(table, []).append(result[table])
    return {t: pd.concat(v) for t, v in outputs.items()}, carry


def same_rows(a, b):
    a, b = a.sort_values(KEYS).reset_index(drop=True), b.sort_values(KEYS).reset_index(drop=True)
    assert not b.duplicated(KEYS).any(), 'a bin was written twice'
    pd.testing.assert_frame_equal(a, b, check_dtype=False, check_exact=False, rtol=1e-4)


# ---- what the flag does

def test_the_queue_clearing_on_green_counts_as_arriving_on_red():
    assert aog(queue_events(), aggregations=[AOG_OFF]) == (10, 9)  # the flaw: nine of the ten read as on green
    assert aog(queue_events()) == (10, 0)


def test_the_flag_changes_which_vehicles_are_on_green_not_how_many_vehicles_there_are():
    with_flag = process(queue_events())['arrival_on_green']
    without = process(queue_events(), aggregations=[AOG_OFF])['arrival_on_green']
    assert with_flag.Total_Actuations.tolist() == without.Total_Actuations.tolist() == [10]
    assert with_flag.TimeStamp.tolist() == without.TimeStamp.tolist()


def test_platoon_ratio_follows_and_its_green_time_does_not_change():
    with_flag = process(queue_events())['platoon_ratio'].iloc[0]
    without = process(queue_events(), aggregations=[AOG_OFF, PLATOON])['platoon_ratio'].iloc[0]
    assert with_flag.Green_Actuations == 0 and with_flag.Platoon_Ratio == 0 and with_flag.Arrival_Type == 1
    assert without.Green_Actuations == 9 and without.Platoon_Ratio > 1
    assert with_flag.Green_Seconds == without.Green_Seconds == 40


def test_traffic_flowing_does_not_clear_the_flag():
    # Vehicles keep crossing every 2 seconds for the whole of a long green: none of them resets it
    moving = [(74 + 2 * i, 74.5 + 2 * i) for i in range(100)]
    events = frame(*signal(yellow=300, red=304), *actuation(30, 72), *actuations(*moving), *END_OF_BIN)
    assert aog(events) == (101, 0)


def test_the_flag_goes_after_a_gap_and_later_vehicles_read_on_green_again():
    # The queue is gone at 90.5 s. The next vehicles come after a gap longer than the setting, in the same green.
    later = [(97, 97.5), (110, 110.5), (120, 120.5)]
    events = frame(*signal(yellow=130, red=134), *actuation(30, 72), *actuations(*QUEUE_CLEARING), *actuations(*later), *END_OF_BIN)
    assert aog(events) == (13, 3)


def test_vehicles_arriving_on_green_with_no_queue_stay_on_green():
    passing = [(62 + 3 * i, 62.4 + 3 * i) for i in range(12)]
    events = frame(*signal(), *actuations(*passing), *END_OF_BIN)
    assert aog(events) == (12, 12)


def test_each_detector_has_its_own_flag():
    # The queue is in one lane. The next lane keeps moving, and its vehicles arrive on green.
    detectors = config((PHASE, DETECTOR), (PHASE, OTHER_DETECTOR))
    other_lane = actuations(*[(70 + 4 * i, 70.4 + 4 * i) for i in range(5)], detector=OTHER_DETECTOR)
    events = frame(*signal(), *actuation(30, 72), *actuations(*QUEUE_CLEARING), *other_lane, *END_OF_BIN)
    assert aog(events, detectors) == (15, 5)


def test_a_detector_serving_two_phases_counts_flagged_on_both():
    detectors = config((2, DETECTOR), (6, DETECTOR))
    both = [at(0, 10, 2), at(0, 10, 6), at(60, 1, 2), at(60, 1, 6), at(100, 8, 2), at(100, 8, 6)]
    events = frame(*both, *actuation(30, 72), *actuations(*QUEUE_CLEARING), *END_OF_BIN)
    out = process(events, detectors)['arrival_on_green'].set_index('Phase')
    assert out.Total_Actuations.to_dict() == {2: 10, 6: 10}
    assert out.Percent_AOG.to_dict() == {2: 0.0, 6: 0.0}


def test_the_vehicle_that_trips_the_flag_keeps_its_own_reading():
    # The flag begins when the detector has been ON long enough, and that vehicle's actuation began before that.
    # A stall on green: its own actuation reads on green, and the one after it reads on red.
    events = frame(*signal(), *actuation(65, 80), *actuation(81, 81.4), *END_OF_BIN)
    assert aog(events) == (2, 1)


def test_a_flagged_arrival_is_on_red_where_it_lands():
    # A long setback: the flagged vehicle's detector event is on red, it reaches the stop bar on green, and still counts on red
    detectors = config((PHASE, DETECTOR), distance=220.0)
    events = frame(*signal(), *actuation(30, 56), *actuation(57, 57.5), *END_OF_BIN)
    assert aog(events, detectors) == (2, 0)
    assert aog(events, detectors, aggregations=[AOG_OFF]) == (2, 1)


# ---- the settings, at their edges

def test_on_for_exactly_the_setting_does_not_trip_it():
    events = frame(*signal(), *actuation(70, 70 + T_ON), *actuation(77, 77.5), *END_OF_BIN)
    assert aog(events) == (2, 2)


def test_on_for_just_over_the_setting_trips_it():
    events = frame(*signal(), *actuation(70, 70 + T_ON + 0.1), *actuation(77, 77.5), *END_OF_BIN)
    assert aog(events) == (2, 1)


def test_a_gap_of_exactly_the_setting_resets_it():
    events = frame(*signal(), *actuation(30, 72), *actuation(72 + T_GAP, 72 + T_GAP + 0.5), *END_OF_BIN)
    assert aog(events) == (2, 1)


def test_a_gap_just_under_the_setting_keeps_it():
    events = frame(*signal(), *actuation(30, 72), *actuation(72 + T_GAP - 0.1, 72 + T_GAP + 0.5), *END_OF_BIN)
    assert aog(events) == (2, 0)


def test_the_gap_runs_from_off_to_on_so_a_long_actuation_is_not_a_gap():
    # 25 seconds between the two on events, but the detector was ON for 24.5 of them
    events = frame(*signal(), *actuation(30, 72), *actuation(73, 98), *actuation(98.5, 99), *END_OF_BIN)
    assert aog(events) == (3, 0)


@pytest.mark.parametrize('t_on, t_gap, on_green', [
    (4, 5, 3),    # 5 s on trips it, so the next vehicle is flagged, and the 6 s gap after that resets it
    (6, 5, 4),    # the gaps of 6 s and 5 s no longer reset it
])
def test_both_settings_are_used(t_on, t_gap, on_green):
    aggregation = {'name': 'arrival_on_green', 'params': {'latency_offset_seconds': 0, 'spillback_on_seconds': t_on, 'spillback_gap_seconds': t_gap}}
    events = frame(*signal(), *actuation(62, 67), *actuation(68, 68.5), *actuation(74.5, 75), *actuation(80, 80.5), *END_OF_BIN)
    assert aog(events, aggregations=[aggregation])[1] == on_green


def test_off_gives_the_old_answer():
    events = queue_events()
    off = process(events, aggregations=[AOG_OFF])
    assert off['arrival_on_green'].Percent_AOG.tolist() == [pytest.approx(0.9)]


@pytest.mark.parametrize('params', [
    {'spillback_on_seconds': 0},
    {'spillback_on_seconds': -3},
])
def test_a_bad_setting_is_refused(params):
    with pytest.raises(ValueError, match='spillback'):
        SignalDataProcessor(raw_data=queue_events(), detector_config=ONE_DETECTOR, bin_size=15, verbose=0,
                            aggregations=[{'name': 'arrival_on_green', 'params': params}])


def test_the_settings_can_have_tenths():
    aggregation = {'name': 'arrival_on_green', 'params': {'spillback_on_seconds': 6.5, 'spillback_gap_seconds': 4.5}}
    events = frame(*signal(), *actuation(62, 68.4), *actuation(69, 69.5), *END_OF_BIN)
    assert aog(events, aggregations=[aggregation]) == (2, 2)


def test_defaults_are_used_when_settings_are_left_out():
    aggregation = {'name': 'arrival_on_green', 'params': {}}
    events = frame(*signal(), *actuation(30, 72), *actuations(*QUEUE_CLEARING), *END_OF_BIN)
    assert aog(events, aggregations=[aggregation]) == (10, 0)


# ---- stuck and quiet detectors, and logs that are not whole

def test_a_detector_stuck_on_counts_nothing_while_stuck_and_recovers_after_a_gap():
    events = frame(*signal(yellow=500, red=504), *actuation(70, 400), *actuation(402, 402.5), *actuation(420, 420.5), *END_OF_BIN)
    assert aog(events) == (3, 2)  # 70 s began on green, 402 s is flagged, and 420 s comes after the gap


def test_a_log_with_no_off_events_never_trips_it():
    ons = [at(62 + 3 * i, 82, DETECTOR) for i in range(12)]
    events = frame(*signal(), *ons, *END_OF_BIN)
    assert aog(events) == (12, 12)


def test_a_missing_off_is_not_a_long_actuation():
    # The off of the first vehicle was lost, so how long it was on is not known
    events = frame(*signal(), at(62, 82, DETECTOR), *actuation(80, 80.5), *actuation(82, 82.5), *END_OF_BIN)
    assert aog(events) == (3, 3)


def test_a_detector_with_an_off_and_no_on_at_the_start_is_ignored():
    events = frame(*signal(), at(61, 81, DETECTOR), *actuation(62, 62.5), *END_OF_BIN)
    assert aog(events) == (1, 1)


def test_two_detectors_with_events_at_the_same_moment_stay_two_vehicles():
    detectors = config((PHASE, DETECTOR), (PHASE, OTHER_DETECTOR))
    events = frame(*signal(), *actuation(62, 62.5), *actuation(62, 62.5, OTHER_DETECTOR), *END_OF_BIN)
    assert aog(events, detectors) == (2, 2)


def test_off_and_on_in_the_same_tenth_read_as_off_then_on():
    events = frame(*signal(), *actuation(30, 72), *actuation(72, 72.5), *END_OF_BIN)
    assert aog(events) == (2, 0)


def test_other_detector_functions_are_left_alone():
    detectors = pd.DataFrame({'DeviceId': 'A', 'Phase': [PHASE, PHASE], 'Parameter': [DETECTOR, OTHER_DETECTOR], 'Function': ['Advance', 'Presence']})
    events = frame(*signal(), *actuation(62, 62.5), *actuation(30, 72, OTHER_DETECTOR), *END_OF_BIN)
    assert aog(events, detectors, aggregations=[{'name': 'arrival_on_green', 'params': {'spillback_on_seconds': T_ON, 'spillback_gap_seconds': T_GAP}}]) == (1, 1)


# ---- across runs: what is carried

def queue_across_bins():
    """A queue on the detector across the end of the first bin, clearing into the second, then flowing, then gaps."""
    rows = [at(600, 10, PHASE), at(905, 1, PHASE), at(1100, 8, PHASE), at(1104, 10, PHASE)]
    rows += actuations((850, 850.5), (890, 925))                                  # stopped from 890 s to 925 s
    rows += actuations(*[(927 + 2 * i, 927.5 + 2 * i) for i in range(6)])         # the queue, last off at 937.5 s
    rows += actuations((941, 941.5))                                              # 3.5 s gap, still flagged
    rows += actuations((950, 950.5), (960, 960.5))                                # 8.5 s gap, reset
    rows += actuations((1000, 1009), (1010, 1010.5), (1030, 1030.5))              # trips and resets in the green
    rows += [at(1799.9, 1, 15)]
    return frame(*rows)


CUTS = [895, 892]


def test_the_scenario_reads_as_it_should():
    out = process(queue_across_bins(), carry='first')['arrival_on_green'].sort_values('TimeStamp')
    # The second bin: on green are 950, 960, 1000 (began before its own flag) and 1030. Flagged are the queue, 941 and 1010.
    assert out.Total_Actuations.tolist() == [2, 12]
    assert out.Percent_AOG.tolist()[1] == pytest.approx(4 / 12)


@pytest.mark.parametrize('cut', CUTS)
def test_one_cut_anywhere_equals_one_big_run(cut):
    events = queue_across_bins()
    big = process(events, carry='first')
    runs, _ = cut_runs(events, [cut])
    same_rows(big['arrival_on_green'], runs['arrival_on_green'])
    same_rows(big['platoon_ratio'], runs['platoon_ratio'])


def test_many_cuts_equal_one_big_run():
    events = queue_across_bins()
    big = process(events, carry='first')
    runs, _ = cut_runs(events, CUTS)
    same_rows(big['arrival_on_green'], runs['arrival_on_green'])
    same_rows(big['platoon_ratio'], runs['platoon_ratio'])


def state(carry, code):
    return carry[carry.EventId == code].sort_values('TimeStamp').reset_index(drop=True)


def test_a_detector_on_at_the_cut_is_carried_with_when_it_turned_on():
    events = queue_across_bins()
    _, carry = cut_runs(events[events.TimeStamp < T0 + pd.Timedelta(seconds=895)], [])
    still_on = state(carry, STILL_ON)
    assert still_on.TimeStamp.tolist() == [T0 + pd.Timedelta(seconds=890)]
    assert still_on.Parameter.tolist() == [DETECTOR]
    assert len(state(carry, FLAG)) == 0  # not on for long enough yet


def test_a_flagged_detector_in_its_gap_is_carried_with_when_it_turned_off():
    events = queue_across_bins()
    _, carry = cut_runs(events[events.TimeStamp < T0 + pd.Timedelta(seconds=939)], [])
    flag = state(carry, FLAG)
    assert flag.TimeStamp.tolist() == [T0 + pd.Timedelta(seconds=937.5)]
    assert flag.Parameter.tolist() == [DETECTOR]
    assert len(state(carry, STILL_ON)) == 0


def test_a_flagged_detector_on_at_the_cut_is_carried_as_both():
    events = queue_across_bins()
    _, carry = cut_runs(events[events.TimeStamp < T0 + pd.Timedelta(seconds=929.2)], [])
    assert state(carry, FLAG).Parameter.tolist() == [DETECTOR]
    assert state(carry, STILL_ON).TimeStamp.tolist() == [T0 + pd.Timedelta(seconds=929)]


def test_nothing_is_carried_once_the_gap_has_passed():
    # The detector is off and its last vehicle was more than the gap ago
    events = queue_across_bins()
    _, carry = cut_runs(events[events.TimeStamp < T0 + pd.Timedelta(seconds=1040)], [])
    assert len(state(carry, FLAG)) == 0 and len(state(carry, STILL_ON)) == 0


def test_a_flagged_arrival_past_the_results_end_stays_on_red_in_the_next_run():
    # The flagged vehicle reaches the detector just before the end of the first bin and the stop bar after it, on green
    detectors = config((PHASE, DETECTOR), distance=220.0)
    first = frame(at(600, 1, PHASE), *actuation(880, 895), *actuation(897.9, 898.4), at(899.9, 1, 7))
    second = frame(at(1000, 8, PHASE), at(1799.9, 1, 7))
    run1 = process(first, detectors, carry='first')
    held = state(run1['unmatched_events'], HELD)
    assert held.TimeStamp.tolist() == [T0 + pd.Timedelta(seconds=897.9 + round(220 / (35 * 1.467), 3))]
    assert held.Parameter.tolist() == [PHASE]  # the phase, as for the vehicles not flagged
    run2 = process(second, detectors, carry=run1['unmatched_events'])
    out = run2['arrival_on_green']
    assert out.Total_Actuations.tolist() == [1] and out.Percent_AOG.tolist() == [0.0]
    assert not run2['unmatched_events'].EventId.isin([HELD]).any()


def test_a_detector_on_across_a_whole_run_with_nothing_of_its_own():
    # Stuck ON through the whole of the middle run, which has events of other kinds only
    rows = [at(600, 1, PHASE), *actuation(850, 2000), at(899.9, 1, 7), at(1799.9, 1, 7), *actuation(2002, 2002.5), at(2100, 8, PHASE), at(2699.9, 1, 7)]
    events = frame(*rows)
    big = process(events, carry='first')
    runs, _ = cut_runs(events, [900, 1800])
    same_rows(big['arrival_on_green'], runs['arrival_on_green'])
    assert runs['arrival_on_green'].sort_values('TimeStamp').Percent_AOG.tolist() == [1.0, 0.0]


def test_an_empty_run_keeps_the_flag():
    events = queue_across_bins()
    first = events[events.TimeStamp < T0 + pd.Timedelta(seconds=929.2)]
    second = events[events.TimeStamp >= T0 + pd.Timedelta(seconds=929.2)]
    carry = process(first, carry='first')['unmatched_events']
    empty = process(first.iloc[0:0], carry=carry)
    kept = lambda df: df[df.EventId.isin([FLAG, STILL_ON])].sort_values(['EventId']).reset_index(drop=True)
    pd.testing.assert_frame_equal(kept(carry), kept(empty['unmatched_events']), check_dtype=False)
    last = process(second, carry=empty['unmatched_events'])
    no_gap = process(second, carry=carry)
    same_rows(no_gap['arrival_on_green'], last['arrival_on_green'])


def test_a_file_from_before_the_flag_still_loads():
    old = pd.DataFrame({'TimeStamp': [T0], 'DeviceId': 'A', 'EventId': [938], 'Parameter': [PHASE], 'IsValid': [True]})
    out = process(queue_events(), carry=old)['arrival_on_green']
    assert out.Total_Actuations.tolist() == [11] and out.Percent_AOG.tolist() == [0.0]


def test_a_run_with_no_unmatched_file_needs_no_carry():
    out = process(queue_events())
    assert 'unmatched_events' not in out
    assert out['arrival_on_green'].Percent_AOG.tolist() == [0.0]


def test_settings_off_carries_nothing_of_the_flag():
    carry = process(queue_events(), aggregations=[AOG_OFF], carry='first')['unmatched_events']
    assert not carry.EventId.isin([HELD, FLAG, STILL_ON]).any()


def test_remove_incomplete_keeps_flagged_bins_across_runs():
    has_data = {'name': 'has_data', 'params': {}}
    events = queue_across_bins()
    aggregations = [has_data, AOG, PLATOON]
    big = process(events, aggregations=aggregations, carry='first', remove_incomplete=True)
    runs, _ = cut_runs(events, [926, 1200], aggregations=aggregations, remove_incomplete=True)
    same_rows(big['arrival_on_green'], runs['arrival_on_green'])
    same_rows(big['platoon_ratio'], runs['platoon_ratio'])


def test_to_sql_renders_a_query_that_runs():
    with SignalDataProcessor(to_sql=True, bin_size=15, verbose=0, aggregations=[AOG, PLATOON]) as p:
        p.aggregate()
        query = p.sql_queries['arrival_on_green']
    assert 'unmatched_previous' not in query
    connection = duckdb.connect()
    events = queue_events()
    connection.register('events_frame', events)
    connection.execute('CREATE TABLE raw_data AS SELECT * FROM events_frame')
    connection.execute("""CREATE TABLE detector_config AS SELECT 'A' AS DeviceId, 2::INT16 AS Phase, 3::INT16 AS Parameter, 'Advance' AS Function,
        0.0::DOUBLE AS DistanceFromStopBar, NULL::DOUBLE AS MPH, NULL::DOUBLE AS LatencyCorrection""")
    connection.execute(query)
    assert connection.execute('SELECT Total_Actuations, Percent_AOG FROM arrival_on_green').fetchall() == [(10, 0.0)]

# ---- real events, with the flag at its defaults

REAL_EVENTS = 'tests/spillback_events_test_data.parquet'  # Two signals, 12:00 to 21:00 on Oct 27 2026, one with queues over its detectors
REAL_CONFIG = 'tests/spillback_config_test_data.parquet'
DEFAULTS = {'name': 'arrival_on_green', 'params': {'latency_offset_seconds': 0}}


def real_events():
    return duckdb.query(f"SELECT * FROM '{REAL_EVENTS}' ORDER BY TimeStamp").df()


def real_config():
    return duckdb.query(f"SELECT * FROM '{REAL_CONFIG}'").df()


def real_cut_runs(events, detectors, cuts):
    edges = [events.TimeStamp.min()] + sorted(cuts) + [events.TimeStamp.max() + pd.Timedelta(seconds=1)]
    outputs, carry = {'arrival_on_green': [], 'platoon_ratio': []}, 'first'
    for start, end in zip(edges, edges[1:]):
        result = process(events[(events.TimeStamp >= start) & (events.TimeStamp < end)], detectors, [DEFAULTS, PLATOON], carry)
        carry = result['unmatched_events']
        for table in outputs:
            outputs[table].append(result[table])
    return {t: pd.concat(v) for t, v in outputs.items()}, carry


def test_real_data_moves_vehicles_off_green():
    events, detectors = real_events(), real_config()
    on = process(events, detectors, [DEFAULTS])['arrival_on_green']
    off = process(events, detectors, [AOG_OFF])['arrival_on_green']
    assert on.Total_Actuations.sum() == off.Total_Actuations.sum()
    on_green = lambda df: (df.Percent_AOG * df.Total_Actuations).sum()
    assert on_green(on) < 0.95 * on_green(off)



def test_flag_off_gives_the_stored_values_of_udots_method():
    """What the flag changes is all that changes: with it off, the sample gives the values from before the flag existed."""
    has_data = {'name': 'has_data', 'params': {}}
    events = duckdb.query("SELECT * FROM 'tests/hires_test_data.parquet'").df()
    detectors = duckdb.query("SELECT * FROM 'tests/configs_test_data.parquet'").df()
    out = process(events, detectors, [has_data, AOG_OFF, PLATOON], remove_incomplete=True)
    for table in ('arrival_on_green', 'platoon_ratio'):
        stored = pd.read_parquet(f'tests/precalculated/{table}_no_spillback.parquet')
        same_rows(stored, out[table])

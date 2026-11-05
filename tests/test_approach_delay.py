"""approach_delay, from how it should work.

Each on event on an advance detector is one vehicle at the stop bar, at the same time and in the same bin as in
arrival_on_green. Delay_Seconds is measured: from arrival on red to the next green, if that green comes within 360 s.
Queued_Delay_Seconds is an estimate for a vehicle crossing on green or yellow under the spillback flag: half the time from the
later of red start and flag start to its green, if that is 360 s or less. Everything else adds nothing.

The times here are seconds from T0. With DistanceFromStopBar 0 an arrival lands the moment the detector turns on.
"""
import random

import duckdb
import pandas as pd
import pytest
from src.atspm import SignalDataProcessor

T0 = pd.Timestamp('2026-09-01 08:00:00')
PHASE, DETECTOR = 2, 3
LIMIT, BIN = 360, 900
DELAY = {'name': 'approach_delay', 'params': {}}
AOG = {'name': 'arrival_on_green', 'params': {}}
HAS_DATA = {'name': 'has_data', 'params': {}}
KEYS = ['TimeStamp', 'DeviceId', 'Phase']
COLUMNS = ['TimeStamp', 'DeviceId', 'Phase', 'Delay_Seconds', 'Queued_Delay_Seconds']
OWN_CODES = {957, 958, 959}


def at(seconds, event, parameter, device='A'):
    return (T0 + pd.Timedelta(seconds=seconds), device, event, parameter)


def frame(*rows):
    return pd.DataFrame(rows, columns=['TimeStamp', 'DeviceId', 'EventId', 'Parameter']).sort_values(['TimeStamp', 'EventId']).reset_index(drop=True)


def config(*detectors, device='A', **columns):
    return pd.DataFrame({'DeviceId': device, 'Phase': [p for p, _ in detectors], 'Parameter': [d for _, d in detectors],
                         'Function': 'Advance', 'DistanceFromStopBar': 0.0, **columns})


ONE_DETECTOR = config((PHASE, DETECTOR))


def actuation(on, off, detector=DETECTOR, device='A'):
    return [at(on, 82, detector, device), at(off, 81, detector, device)]


def signal(*cycles, phase=PHASE, device='A'):
    """Cycles of (green, yellow, red), with both 9 and 10 at the red."""
    rows = []
    for green, yellow, red in cycles:
        rows += [at(green, 1, phase, device), at(yellow, 8, phase, device), at(red, 9, phase, device), at(red, 10, phase, device)]
    return rows


def end_at(seconds):
    """An event of a phase with no detector, to carry the data to a time."""
    return [at(seconds, 1, 15)]


def process(events, detectors=ONE_DETECTOR, aggregations=None, carry=None, first_run=True, bin_size=15, **kwargs):
    settings = None
    if carry is not None:
        settings = {'df_or_path': '' if isinstance(carry, str) else carry, 'max_days_old': 14}
    with SignalDataProcessor(raw_data=events, detector_config=detectors, bin_size=bin_size, verbose=0, unmatched_event_settings=settings,
                             aggregations=aggregations or [DELAY], **kwargs) as p:
        p.load()
        p.aggregate()
        tables = [row[0] for row in p.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        return {t: p.conn.execute(f'SELECT * FROM "{t}"').df() for t in tables}


def delay(events, detectors=ONE_DETECTOR, **kwargs):
    return process(events, detectors, **kwargs)['approach_delay']


def one_row(events, detectors=ONE_DETECTOR, **kwargs):
    """The one row of approach_delay as (Delay_Seconds, Queued_Delay_Seconds)."""
    out = delay(events, detectors, **kwargs)
    assert len(out) == 1
    return round(float(out.iloc[0].Delay_Seconds), 3), round(float(out.iloc[0].Queued_Delay_Seconds), 3)


def sort(table, keys=KEYS):
    return table.sort_values(keys).reset_index(drop=True)


def cut_runs(events, cuts, detectors=ONE_DETECTOR, aggregations=None, **kwargs):
    """The events cut into runs at the given seconds, each from the state of the one before. Returns each run's outputs."""
    edges = [events.TimeStamp.min()] + [T0 + pd.Timedelta(seconds=c) for c in sorted(cuts)] + [events.TimeStamp.max() + pd.Timedelta(seconds=1)]
    outputs, carry = [], ''
    for start, end in zip(edges, edges[1:]):
        window = events[(events.TimeStamp >= start) & (events.TimeStamp < end)].reset_index(drop=True)
        out = process(window, detectors, aggregations, carry, **kwargs)
        outputs.append(out)
        carry = out['unmatched_events']
    return outputs


def written(outputs, table='approach_delay'):
    return pd.concat([o[table] for o in outputs if table in o and len(o[table])], ignore_index=True)


# ---------------------------------------------------------------- what a vehicle counts for

def test_the_four_kinds_of_vehicle_of_the_worked_example():
    """Travel is 4 s, red runs 08:04:00 to 08:15:20. C finds no green in 360 s, B waits on red, D crosses on green under the
    flag B set, A crosses on green with the flag gone. Each lands in the bin arrival_on_green counts it in."""
    detectors = config((PHASE, DETECTOR), DistanceFromStopBar=4 * 35 * 1.467)
    events = frame(at(0, 1, PHASE), at(236, 8, PHASE), at(240, 10, PHASE), at(920, 1, PHASE), at(950, 8, PHASE), at(954, 10, PHASE),
                   *actuation(296, 298),  # C, arrives 300
                   *actuation(886, 928),  # B, arrives 890, holds 42 s and sets the flag
                   *actuation(930, 932),  # D, arrives 934, 2 s after the off so under the flag
                   *actuation(940, 941),  # A, arrives 944, 8 s after the off so the flag has gone
                   *end_at(2400))
    out = process(events, detectors, [DELAY, AOG])
    rows = sort(out['approach_delay'])
    assert list(rows.Delay_Seconds) == pytest.approx([30, 0])
    assert list(rows.Queued_Delay_Seconds) == pytest.approx([0, 17])
    assert list(rows.TimeStamp) == [T0, T0 + pd.Timedelta(minutes=15)]
    aog = sort(out['arrival_on_green'])
    pd.testing.assert_frame_equal(rows[KEYS], aog[KEYS])
    assert list(aog.Total_Actuations) == [2, 2]
    per_vehicle = (rows.Delay_Seconds.sum() + rows.Queued_Delay_Seconds.sum()) / aog.Total_Actuations.sum()
    assert per_vehicle == pytest.approx(11.75)


def test_columns_and_types_and_nothing_else():
    events = frame(*signal((60, 100, 104)), *actuation(30, 31), *end_at(899.9))
    with SignalDataProcessor(raw_data=events, detector_config=ONE_DETECTOR, bin_size=15, verbose=0, aggregations=[DELAY]) as p:
        p.load()
        p.aggregate()
        described = p.conn.execute("DESCRIBE approach_delay").fetchall()
    assert [(c[0], c[1]) for c in described] == [('TimeStamp', 'TIMESTAMP'), ('DeviceId', 'VARCHAR'), ('Phase', 'SMALLINT'),
                                                 ('Delay_Seconds', 'FLOAT'), ('Queued_Delay_Seconds', 'FLOAT')]


def test_device_id_keeps_its_type():
    events = frame(*signal((60, 100, 104)), *actuation(30, 31), *end_at(899.9))
    events['DeviceId'] = 7
    detectors = ONE_DETECTOR.assign(DeviceId=7)
    with SignalDataProcessor(raw_data=events, detector_config=detectors, bin_size=15, verbose=0, aggregations=[DELAY]) as p:
        p.load()
        p.aggregate()
        input_type = p.conn.execute("SELECT typeof(DeviceId) FROM raw_data LIMIT 1").fetchall()[0][0]
        assert p.conn.execute("SELECT typeof(DeviceId) FROM approach_delay LIMIT 1").fetchall()[0][0] == input_type


def test_on_red_waits_to_the_next_green():
    events = frame(at(0, 10, PHASE), *signal((60, 100, 104)), *actuation(25.5, 26), *end_at(899.9))
    assert one_row(events) == (34.5, 0)


def test_on_green_and_on_yellow_add_nothing_and_still_have_a_row():
    events = frame(at(0, 10, PHASE), *signal((60, 100, 104)), *actuation(70, 71), *actuation(101, 102), *end_at(899.9))
    assert one_row(events) == (0, 0)


def test_a_vehicle_with_no_signal_state_yet_adds_nothing():
    events = frame(*signal((60, 100, 104)), *actuation(30, 31), *end_at(899.9))
    assert one_row(events) == (0, 0)


def test_a_green_at_the_arrival_is_on_green():
    events = frame(at(0, 10, PHASE), *signal((60, 100, 104)), *actuation(60, 61), *end_at(899.9))
    assert one_row(events) == (0, 0)


def test_a_green_at_exactly_360_s_counts_and_past_it_does_not():
    events = frame(at(0, 10, PHASE), *signal((400, 440, 444)), *actuation(40, 41), *actuation(39.9, 39.95), *end_at(899.9))
    assert one_row(events) == (360, 0)


def test_a_vehicle_with_no_green_in_the_data_adds_nothing():
    events = frame(at(0, 10, PHASE), *actuation(30, 31), *end_at(899.9))
    assert one_row(events) == (0, 0)


def test_only_advance_detectors_count():
    detectors = pd.concat([ONE_DETECTOR, config((PHASE, 4)).assign(Function='Presence')], ignore_index=True)
    events = frame(at(0, 10, PHASE), *signal((60, 100, 104)), *actuation(30, 31), *actuation(40, 41, 4), *end_at(899.9))
    assert one_row(events, detectors) == (30, 0)
    no_advance = config((PHASE, 4)).assign(Function='Presence')
    assert len(delay(events, no_advance)) == 0


def test_seconds_add_up_over_vehicles():
    events = frame(at(0, 10, PHASE), *signal((60, 100, 104)), *actuation(30, 31), *actuation(40, 41), *actuation(50, 51), *end_at(899.9))
    assert one_row(events) == (60, 0)


# ---------------------------------------------------------------- 9 as the end of yellow

def test_end_of_yellow_reads_as_red_where_the_signal_logs_no_red_clearance():
    """Signal 2B432 logs 9 and no 10 on any phase."""
    events = frame(at(0, 9, PHASE), at(60, 1, PHASE), at(100, 8, PHASE), at(104, 9, PHASE), at(164, 1, PHASE),
                   *actuation(30, 31), *actuation(110, 111), *end_at(899.9))
    assert one_row(events) == (30 + 54, 0)


def test_both_9_and_10_at_one_moment_are_one_red():
    both = frame(at(0, 9, PHASE), at(0, 10, PHASE), at(60, 1, PHASE), *actuation(30, 31), *end_at(899.9))
    only_ten = frame(at(0, 10, PHASE), at(60, 1, PHASE), *actuation(30, 31), *end_at(899.9))
    assert one_row(both) == one_row(only_ten) == (30, 0)


def test_the_estimate_reads_red_start_from_9_too():
    events = frame(at(10, 9, PHASE), at(60, 1, PHASE), *actuation(5, 66), *actuation(68, 69), *end_at(899.9))
    assert one_row(events) == (0, 25)


def test_arrival_on_green_is_not_changed_by_9():
    """It only asks whether a vehicle is on green, and reads 1, 8 and 10 alone."""
    events = frame(at(0, 1, PHASE), at(50, 9, PHASE), *actuation(60, 61), *end_at(899.9))
    assert process(events, aggregations=[AOG])['arrival_on_green'].Percent_AOG.iloc[0] == 1


# ---------------------------------------------------------------- under the flag

def queue_events(end=899.9):
    """Red from 0, a vehicle stopped on the detector from 30 s, green at 60 s and it moves at 72 s, a queue crossing after it."""
    return frame(at(0, 10, PHASE), *signal((60, 100, 104)), *actuation(30, 72), *actuation(74, 74.5), *end_at(end))


def test_on_green_under_the_flag_is_half_from_flag_start_to_green():
    assert one_row(queue_events()) == (30, 15)


def test_the_estimate_starts_at_red_if_the_flag_began_before_it():
    events = frame(at(20, 10, PHASE), at(60, 1, PHASE), *actuation(5, 72), *actuation(74, 74.5), *end_at(899.9))
    # the vehicle on the detector has no state at 5 s
    assert one_row(events) == (0, 20)


def test_a_flag_that_began_after_green_began_gives_0():
    events = frame(at(0, 10, PHASE), at(60, 1, PHASE), *actuation(61, 72), *actuation(74, 74.5), *end_at(899.9))
    assert one_row(events) == (0, 0)


def test_on_yellow_under_the_flag_uses_the_green_it_followed():
    events = frame(at(0, 10, PHASE), at(60, 1, PHASE), at(80, 8, PHASE), *actuation(30, 79), *actuation(81, 81.5), *end_at(899.9))
    assert one_row(events) == (30, 15)


def test_on_red_under_the_flag_is_its_measured_wait_alone():
    """The next red: the flag holds on through it, and the wait to the next green is measured."""
    events = frame(at(0, 10, PHASE), *signal((60, 100, 104), (130, 170, 174)), *actuation(30, 105), *actuation(107, 108), *end_at(899.9))
    assert one_row(events) == (30 + 23, 0)


def test_on_red_under_the_flag_past_360_s_adds_nothing():
    events = frame(at(0, 10, PHASE), *signal((60, 100, 104), (500, 540, 544)), *actuation(30, 105), *actuation(107, 108), *end_at(899.9))
    assert one_row(events) == (30, 0)


def test_an_estimate_at_360_s_counts_and_past_it_does_not():
    at_limit = frame(at(0, 10, PHASE), at(800, 1, PHASE), *actuation(80, 805), *actuation(806, 806.5), *end_at(899.9))
    past_limit = frame(at(0, 10, PHASE), at(800.1, 1, PHASE), *actuation(80, 805), *actuation(806, 806.5), *end_at(899.9))
    assert one_row(at_limit) == (0, 360)
    assert one_row(past_limit) == (0, 0)


def test_the_flag_goes_after_the_gap():
    events = frame(at(0, 10, PHASE), *signal((60, 100, 104)), *actuation(30, 72), *actuation(77, 77.5), *end_at(899.9))
    assert one_row(events) == (30, 0)


def test_spillback_off_gives_no_estimate():
    off = {'name': 'approach_delay', 'params': {'spillback_on_seconds': None}}
    out = delay(queue_events(), aggregations=[off])
    assert out.Queued_Delay_Seconds.iloc[0] == 0
    assert out.Delay_Seconds.iloc[0] == 30


def test_the_settings_of_the_flag_are_taken():
    events = queue_events()
    longer = {'name': 'approach_delay', 'params': {'spillback_on_seconds': 50}}
    assert delay(events, aggregations=[longer]).Queued_Delay_Seconds.iloc[0] == 0
    # 74 is 2 s after the off, so a gap of 2 s has gone by it
    short_gap = {'name': 'approach_delay', 'params': {'spillback_gap_seconds': 2}}
    assert delay(events, aggregations=[short_gap]).Queued_Delay_Seconds.iloc[0] == 0


@pytest.mark.parametrize('params', [{'spillback_on_seconds': 0}, {'spillback_gap_seconds': None}, {'spillback_on_second': 6}])
def test_bad_settings_raise(params):
    with pytest.raises(ValueError):
        SignalDataProcessor(raw_data=frame(), detector_config=ONE_DETECTOR, bin_size=15, verbose=0,
                            aggregations=[{'name': 'approach_delay', 'params': params}])


# ---------------------------------------------------------------- the arrival and its bin

@pytest.mark.parametrize('columns, seconds', [
    ({'DistanceFromStopBar': 513.45, 'MPH': 35.0}, 10.0),
    ({'DistanceFromStopBar': 293.4, 'MPH': 20.0}, 10.0),
    ({'DistanceFromStopBar': 513.45, 'MPH': 35.0, 'LatencyCorrection': 2.5}, 7.5),
])
def test_arrival_takes_distance_speed_and_latency(columns, seconds):
    events = frame(at(0, 10, PHASE), at(60, 1, PHASE), *actuation(30, 31), *end_at(899.9))
    detectors = pd.DataFrame({'DeviceId': ['A'], 'Phase': [PHASE], 'Parameter': [DETECTOR], 'Function': ['Advance'], **{k: [v] for k, v in columns.items()}})
    assert one_row(events, detectors) == (30 - seconds, 0)


@pytest.mark.parametrize('phase, seconds', [(2, 220 / (35 * 1.467)), (4, 120 / (35 * 1.467))])
def test_arrival_takes_the_defaults(phase, seconds):
    events = frame(at(0, 10, phase), at(60, 1, phase), *actuation(30, 31), *end_at(899.9))
    detectors = pd.DataFrame({'DeviceId': ['A'], 'Phase': [phase], 'Parameter': [DETECTOR], 'Function': ['Advance']})
    assert one_row(events, detectors) == (round(30 - seconds, 3), 0)


def test_latency_offset_is_used_where_the_config_has_none():
    events = frame(at(0, 10, PHASE), at(60, 1, PHASE), *actuation(30, 31), *end_at(899.9))
    with_offset = {'name': 'approach_delay', 'params': {'latency_offset_seconds': 3}}
    assert one_row(events, aggregations=[with_offset]) == (33, 0)


def test_a_vehicle_counts_in_the_bin_it_arrived_in_not_the_bin_its_wait_ended():
    events = frame(at(0, 10, PHASE), *signal((910, 940, 944)), *actuation(890, 891), *actuation(915, 916), *end_at(2000))
    rows = sort(delay(events))
    assert list(rows.TimeStamp) == [T0, T0 + pd.Timedelta(minutes=15)]
    assert list(rows.Delay_Seconds) == [20, 0]


def test_bin_size_is_taken():
    events = frame(at(0, 10, PHASE), *signal((400, 440, 444)), *actuation(30, 31), *actuation(320, 321), *end_at(899.9))
    rows = sort(delay(events, bin_size=5))
    assert list(rows.TimeStamp) == [T0, T0 + pd.Timedelta(minutes=5)]
    assert list(rows.Delay_Seconds) == [0, 80]  # 370 s is past the limit


def test_same_bins_as_arrival_on_green_whatever_the_detector():
    detectors = config((PHASE, DETECTOR), (PHASE, 4), DistanceFromStopBar=[300.0, 40.0], LatencyCorrection=[1.0, 4.0])
    rows = [at(0, 10, PHASE), *signal((60, 100, 104), (900, 940, 944))]
    random.seed(5)
    for t in sorted(random.uniform(1, 1700) for _ in range(120)):
        rows += actuation(round(t, 1), round(t, 1) + 0.5, random.choice([DETECTOR, 4]))
    out = process(frame(*rows, *end_at(1799.9)), detectors, [DELAY, AOG])
    pd.testing.assert_frame_equal(sort(out['approach_delay'])[KEYS], sort(out['arrival_on_green'])[KEYS])


def test_each_table_stands_alone():
    """Listed together, neither reads the other's rows or changes its result."""
    events = queue_events(2000)
    both = cut_runs(events, [60, 75], aggregations=[DELAY, AOG])
    alone = cut_runs(events, [60, 75], aggregations=[DELAY])
    other = cut_runs(events, [60, 75], aggregations=[AOG])
    pd.testing.assert_frame_equal(written(both), written(alone))
    pd.testing.assert_frame_equal(written(both, 'arrival_on_green'), written(other, 'arrival_on_green'))


# ---------------------------------------------------------------- what an incremental run writes, and when

def test_a_bin_waits_for_data_360_s_past_its_end():
    short_of_it = frame(at(0, 10, PHASE), *signal((60, 100, 104)), *actuation(30, 31), *end_at(1259.9))
    assert [len(o['approach_delay']) for o in cut_runs(short_of_it, [900])] == [0, 0]
    reaching_it = frame(at(0, 10, PHASE), *signal((60, 100, 104)), *actuation(30, 31), *end_at(1260))
    assert [len(o['approach_delay']) for o in cut_runs(reaching_it, [900])] == [0, 1]


def test_a_run_with_no_state_file_writes_all_it_has():
    events = frame(at(0, 10, PHASE), *signal((60, 100, 104)), *actuation(30, 31), *end_at(100))
    assert one_row(events) == (30, 0)


def test_a_vehicle_with_no_signal_state_is_settled_across_a_cut():
    events = frame(*signal((60, 100, 104)), *actuation(30, 31), *end_at(1500))
    assert len(written(cut_runs(events, [45, 900]))) == 1


def test_a_first_run_with_a_state_file_holds_its_bin():
    events = frame(at(0, 10, PHASE), *signal((60, 100, 104)), *actuation(30, 31), *end_at(899.9))
    out = process(events, carry='')
    assert len(out['approach_delay']) == 0
    assert OWN_CODES & set(out['unmatched_events'].EventId)


def test_a_wait_across_a_cut_is_measured_from_the_arrival():
    events = frame(at(0, 10, PHASE), *signal((930, 970, 974)), *actuation(890, 891), *end_at(2000))
    rows = written(cut_runs(events, [900]))
    assert list(rows.TimeStamp) == [T0]
    assert list(rows.Delay_Seconds) == [40]


def test_green_and_flag_starts_carry_for_the_estimate():
    """Red, flag and green fall in one run, and the vehicle under the flag in the next."""
    events = frame(at(0, 10, PHASE), at(860, 1, PHASE), at(960, 8, PHASE), at(964, 10, PHASE), *actuation(830, 895), *actuation(897, 897.5), *end_at(2000))
    rows = sort(written(cut_runs(events, [896])))
    assert rows.Queued_Delay_Seconds.sum() == 15
    assert rows.Delay_Seconds.sum() == 30


def test_a_detector_still_on_at_the_cut_sets_the_flag_from_its_on():
    events = frame(at(0, 10, PHASE), at(860, 1, PHASE), *actuation(830, 905), *actuation(907, 907.5), *end_at(2400))
    rows = sort(written(cut_runs(events, [900])))
    assert rows.Queued_Delay_Seconds.sum() == 15


def test_a_vehicle_under_the_flag_arriving_after_the_cut_keeps_its_flag_start():
    """It turned the detector on before the cut and reaches the stop bar after it, and the green starts after the cut too."""
    detectors = config((PHASE, DETECTOR), DistanceFromStopBar=10 * 35 * 1.467)
    events = frame(at(0, 10, PHASE), at(905, 1, PHASE), at(960, 8, PHASE), at(964, 10, PHASE),
                   *actuation(800, 897), *actuation(898, 898.5), *end_at(2400))
    rows = written(cut_runs(events, [899], detectors))
    assert rows.Queued_Delay_Seconds.sum() == pytest.approx((905 - 800) / 2)
    assert rows.Delay_Seconds.sum() == pytest.approx(905 - 810)


def test_a_detector_on_two_phases_counts_once_on_each_across_a_cut():
    """It reaches phase 2's stop bar before the cut and phase 6's after it, under the flag."""
    detectors = config((2, DETECTOR), (6, DETECTOR), DistanceFromStopBar=[0.0, 10 * 35 * 1.467])
    events = frame(at(0, 10, 2), at(0, 10, 6), at(850, 1, 2), at(905, 1, 6), *actuation(800, 895), *actuation(897, 897.5), *end_at(2400))
    big = sort(process(events, detectors)['approach_delay'])
    for cut in (896, 898, 900, 905, 906, 908):
        rows = sort(written(cut_runs(events, [cut], detectors)))
        pd.testing.assert_frame_equal(rows, big, check_dtype=False)


def test_a_silent_device_settles_when_the_data_moves_on():
    """Its vehicle has no green in 360 s, and its bin is written by a run in which it logs nothing."""
    other = config((PHASE, DETECTOR), device='B')
    detectors = pd.concat([ONE_DETECTOR, other], ignore_index=True)
    events = frame(at(0, 10, PHASE), *signal((60, 100, 104)), *actuation(30, 31), *actuation(120, 121),
                   *[at(t, 10, PHASE, 'B') for t in range(600, 2400, 60)])
    runs = cut_runs(events, [900, 1800], detectors)
    rows = written(runs)
    a = rows[rows.DeviceId == 'A']
    assert list(a.Delay_Seconds) == [30]
    assert len(runs[0]['approach_delay']) == 0
    assert len(runs[1]['approach_delay'][runs[1]['approach_delay'].DeviceId == 'A']) == 1


def test_a_run_with_no_events_keeps_the_state():
    events = frame(at(0, 10, PHASE), *actuation(890, 891), *end_at(899.9))
    first = process(events, carry='')
    held = first['unmatched_events']
    empty = frame().astype({'TimeStamp': 'datetime64[ns]', 'DeviceId': 'object', 'EventId': 'int16', 'Parameter': 'int16'})
    second = process(empty, carry=held)
    assert len(second['approach_delay']) == 0
    own = lambda t: sort(t[t.EventId.isin(OWN_CODES)], ['TimeStamp', 'EventId', 'Parameter'])
    pd.testing.assert_frame_equal(own(second['unmatched_events']), own(held), check_dtype=False)
    third = process(frame(*signal((930, 970, 974)), *end_at(2000)), carry=second['unmatched_events'])
    assert list(third['approach_delay'].Delay_Seconds) == [40]


def test_a_number_as_device_id_across_a_cut():
    events = frame(at(0, 10, PHASE), *signal((930, 970, 974)), *actuation(890, 891), *actuation(900, 930.5), *actuation(931, 931.5), *end_at(2400))
    events['DeviceId'] = 7
    detectors = ONE_DETECTOR.assign(DeviceId=7)
    rows = sort(written(cut_runs(events, [900, 1500], detectors)))
    pd.testing.assert_frame_equal(rows, sort(delay(events, detectors)), check_dtype=False)
    assert rows.Delay_Seconds.sum() == 40 + 30
    assert rows.Queued_Delay_Seconds.sum() == 15


def test_a_sum_bigger_than_one_state_row_holds():
    """A 1-minute bin holds at most 60 s of offset in a row, far less than this bin's sum."""
    rows = [at(0, 10, PHASE), at(420, 1, PHASE), at(1100, 1, 15)]
    for i in range(200):
        rows += actuation(60 + i * 0.25, 60 + i * 0.25 + 0.1)
    events = frame(*rows)
    big = delay(events, bin_size=1, carry='')
    cut = written(cut_runs(events, [90, 200], bin_size=1))
    pd.testing.assert_frame_equal(sort(cut), sort(big), check_dtype=False)
    assert cut.Delay_Seconds.sum() == pytest.approx(sum(360 - i * 0.25 for i in range(200)), rel=1e-5)
    assert cut.Delay_Seconds.sum() > 60_000


def test_remove_incomplete_drops_a_bin_with_no_data_across_runs():
    has_data = {'name': 'has_data', 'params': {}}
    aggregations = [has_data, DELAY]
    # The signal logs each minute, then nothing through the second bin, so has_data has no row for it, and a vehicle projected into it has no row
    detectors = config((PHASE, DETECTOR), DistanceFromStopBar=20 * 35 * 1.467)
    events = frame(at(0, 10, PHASE), *signal((60, 100, 104)), *actuation(30, 31), *actuation(890, 890.1),
                   *[at(t, 10, 15) for t in (*range(-2700, 900, 60), *range(1800, 3000, 100))])  # 3 bins before T0: has_data's history
    big = process(events, detectors, aggregations, remove_incomplete=True)
    assert list(sort(big['approach_delay']).TimeStamp) == [T0]
    for cuts in ([900, 1800], [450, 1000, 1900, 2500]):
        rows = written(cut_runs(events, cuts, detectors, aggregations, remove_incomplete=True))
        pd.testing.assert_frame_equal(sort(rows), sort(big['approach_delay']), check_dtype=False)


def test_it_writes_only_its_own_codes():
    held = process(queue_events(), carry='')['unmatched_events']
    assert set(held.EventId) <= OWN_CODES | {936}
    assert held.IsValid.all()


def test_other_measures_do_not_see_the_state_rows():
    held = process(queue_events(), carry='')['unmatched_events']
    with SignalDataProcessor(raw_data=frame(*end_at(3000)), detector_config=ONE_DETECTOR, bin_size=15, verbose=0, aggregations=[DELAY],
                             unmatched_event_settings={'df_or_path': held, 'max_days_old': 14}) as p:
        p.load()
        seen = {row[0] for row in p.conn.execute("SELECT DISTINCT EventId FROM raw_data_all").fetchall()}
    assert OWN_CODES & set(held.EventId)
    assert not OWN_CODES & seen


def test_a_state_holding_its_rows_raises_if_it_is_not_listed():
    held = process(queue_events(), carry='')['unmatched_events']
    later = frame(*end_at(3000))
    with pytest.raises(ValueError, match='approach_delay'):
        process(later, aggregations=[AOG], carry=held)


def test_to_sql_gives_one_statement_to_run():
    with SignalDataProcessor(bin_size=15, verbose=0, to_sql=True, aggregations=[DELAY]) as p:
        p.load()
        p.aggregate()
        assert 'approach_delay' in p.sql_queries['approach_delay']


# ---------------------------------------------------------------- against a calculation written from the rules, one vehicle at a time

def stream(seed, device='A', minutes=95):
    """Cycles with long reds, phases 2 and 4 on three detectors (one a second lane, one short of the stop bar), queues."""
    r = random.Random(seed)
    rows, t = [at(0, 10, 2, device), at(0, 10, 4, device)], 0.0
    while t < minutes * 60:
        for phase in (2, 4):
            green = t + r.choice([5, 20, 40])
            yellow = green + r.choice([10, 30, 60])
            red = yellow + 4
            rows += [at(round(green, 1), 1, phase, device), at(round(yellow, 1), 8, phase, device), at(round(red, 1), 9, phase, device)]
            if device == 'A':
                rows.append(at(round(red, 1), 10, phase, device))
            t = red + (500 if r.random() < 0.04 else 0)
    t = 0.0
    while t < minutes * 60:
        detector = r.choice([3, 4, 5])
        t += r.expovariate(1 / 5)
        queue = r.random() < 0.06
        hold = r.choice([9, 30]) if queue else 0.4
        rows.append(at(round(t, 1), 82, detector, device))
        if r.random() > 0.01:  # a lost off, now and then
            rows.append(at(round(t + hold, 1), 81, detector, device))
        if queue:  # the queue crossing after it
            for i in range(1, 6):
                rows += actuation(round(t + hold + 2 * i, 1), round(t + hold + 2 * i + 0.5, 1), detector, device)
    return rows


STREAM_DETECTORS = pd.DataFrame({'DeviceId': 'A', 'Phase': [2, 2, 4], 'Parameter': [3, 4, 5], 'Function': 'Advance',
                                 'DistanceFromStopBar': [300.0, 300.0, 10.0], 'MPH': [35.0, 35.0, 35.0], 'LatencyCorrection': [0.0, 0.0, 2.0]})


def two_devices(seed):
    rows = stream(seed) + stream(seed + 100, 'B')
    return frame(*rows).drop_duplicates(['TimeStamp', 'DeviceId', 'EventId', 'Parameter']).reset_index(drop=True), \
        pd.concat([STREAM_DETECTORS, STREAM_DETECTORS.assign(DeviceId='B')], ignore_index=True)


def by_hand(events, detectors, on_ms=6000, gap_ms=5000, bin_ms=BIN * 1000, limit_ms=LIMIT * 1000):
    """Every vehicle, from the rules. Times in milliseconds from T0. Results stop at the end of the data, or the end of its
    bin if that is within a second, as in arrival_on_green."""
    ms = lambda t: round((t - T0).total_seconds() * 1000)
    last_event = ms(events.TimeStamp.max())
    bin_end = (last_event // bin_ms + 1) * bin_ms
    results_end = bin_end if bin_end - last_event <= 1000 else last_event + 1
    total = {}
    for device, config_rows in detectors.groupby('DeviceId'):
        rows = events[events.DeviceId == device]
        phase_rows = sorted((ms(r.TimeStamp), {9: 10}.get(r.EventId, r.EventId), r.Parameter) for r in rows[rows.EventId.isin([1, 8, 9, 10])].itertuples())
        vehicles = []
        for c in config_rows.itertuples():
            offset = round(1000 * (c.DistanceFromStopBar / (c.MPH * 1.467) - c.LatencyCorrection))
            flag_start, last_end, on_at = None, None, None
            detector_rows = sorted((ms(r.TimeStamp), 0 if r.EventId == 81 else 1) for r in rows[(rows.Parameter == c.Parameter) & rows.EventId.isin([81, 82])].itertuples())
            for t, is_on in detector_rows:
                if is_on:
                    if last_end is not None and t - last_end >= gap_ms:
                        flag_start = None
                    vehicles.append((t + offset, c.Phase, flag_start))
                    on_at, last_end = t, t
                elif on_at is not None:
                    if t - on_at > on_ms and flag_start is None:
                        flag_start = on_at
                    on_at, last_end = None, t
        for arrival, phase, flag_start in vehicles:
            if arrival >= results_end:
                continue
            state = last_green = red_before_green = last_red = next_green = None
            for t, event, p in phase_rows:
                if p != phase:
                    continue
                if t <= arrival:
                    state = event
                    if event == 1:
                        last_green, red_before_green = t, last_red
                    if event == 10:
                        last_red = t
                elif event == 1:
                    next_green = t
                    break
            add_delay = add_queued = 0
            if state == 10 and next_green is not None and next_green - arrival <= limit_ms:
                add_delay = next_green - arrival
            elif state in (1, 8) and flag_start is not None and last_green is not None:
                start = flag_start if red_before_green is None else max(flag_start, red_before_green)
                if (last_green - start) / 2 <= limit_ms:
                    add_queued = max(last_green - start, 0) / 2
            key = (T0 + pd.Timedelta(milliseconds=arrival // bin_ms * bin_ms), device, phase)
            so_far = total.get(key, (0, 0))
            total[key] = (so_far[0] + add_delay / 1000, so_far[1] + add_queued / 1000)
    return total


@pytest.mark.parametrize('seed', [1, 2, 3])
def test_one_big_run_equals_the_rules_vehicle_by_vehicle(seed):
    events, detectors = two_devices(seed)
    rows = delay(events, detectors)
    expected = by_hand(events, detectors)
    got = {(r.TimeStamp, r.DeviceId, r.Phase): (r.Delay_Seconds, r.Queued_Delay_Seconds) for r in rows.itertuples()}
    assert set(got) == set(expected)
    assert len(got) > 20
    assert sum(v[0] for v in got.values()) > 0 and sum(v[1] for v in got.values()) > 0
    for key, (measured, queued) in got.items():
        assert measured == pytest.approx(expected[key][0], rel=1e-5, abs=0.01), key
        assert queued == pytest.approx(expected[key][1], rel=1e-5, abs=0.01), key


@pytest.mark.parametrize('seed', [1, 2, 3])
@pytest.mark.parametrize('cuts', [
    list(range(900, 5700, 900)),                       # on the bin
    [120, 777, 1500, 1503, 2222, 3000, 4100, 5000],    # inside a bin
    [5, 6, 7, 2000],                                   # very short runs
], ids=['on the bin', 'inside a bin', 'short runs'])
def test_cut_runs_equal_one_big_run_and_the_same_state(seed, cuts):
    events, detectors = two_devices(seed)
    aggregations = [DELAY, AOG]
    big = process(events, detectors, aggregations, carry='')
    outputs = cut_runs(events, cuts, detectors, aggregations)
    rows = written(outputs)
    assert len(rows) > 10
    assert not rows.duplicated(KEYS).any()
    pd.testing.assert_frame_equal(sort(rows), sort(big['approach_delay']), check_dtype=False, check_exact=True)
    own = lambda t: sort(t[t.EventId.isin(OWN_CODES)], ['TimeStamp', 'DeviceId', 'EventId', 'Parameter'])
    pd.testing.assert_frame_equal(own(outputs[-1]['unmatched_events']), own(big['unmatched_events']), check_dtype=False)
    # and the vehicles are arrival_on_green's
    aog = big['arrival_on_green'].merge(rows, on=KEYS, how='right')
    assert aog.Total_Actuations.notna().all()


def test_every_bin_is_written_by_the_first_run_that_can():
    events, detectors = two_devices(4)
    edges = list(range(300, 5700, 300))
    outputs = cut_runs(events, edges, detectors)
    last_events = [events[events.TimeStamp < T0 + pd.Timedelta(seconds=e)].TimeStamp.max() for e in edges]
    checked = 0
    for i, out in enumerate(outputs[:len(edges)]):
        for stamp in out['approach_delay'].TimeStamp:
            reach = stamp + pd.Timedelta(seconds=BIN + LIMIT)
            assert last_events[i] >= reach
            assert i == 0 or last_events[i - 1] < reach
            checked += 1
    assert checked > 10


def test_the_state_is_small():
    """Sums and waiting vehicles, not the vehicles of the bin."""
    events, detectors = two_devices(5)
    state = cut_runs(events, [3600], detectors)[0]['unmatched_events']
    vehicles = len(events[(events.EventId == 82) & (events.TimeStamp >= T0 + pd.Timedelta(minutes=45)) & (events.TimeStamp < T0 + pd.Timedelta(seconds=3600))])
    assert len(state[state.EventId.isin(OWN_CODES)]) < vehicles / 2


def test_no_vehicle_waits_in_the_state_past_the_limit():
    waiting = lambda end: process(frame(at(0, 10, PHASE), *actuation(30, 31), *end_at(end)), carry='')['unmatched_events'].query('EventId == 958 and Parameter == @PHASE')
    assert len(waiting(389.9)) == 1
    assert len(waiting(390)) == 0


def test_cut_runs_read_their_state_back_from_files(tmp_path):
    """Parquet and CSV hold the state's times as well as a DataFrame does."""
    events, detectors = two_devices(6)
    big = process(events, detectors, carry='')['approach_delay']
    for extension in ('parquet', 'csv'):
        path = str(tmp_path / f'state.{extension}')
        edges = [events.TimeStamp.min()] + [T0 + pd.Timedelta(seconds=c) for c in range(900, 5700, 900)] + [events.TimeStamp.max() + pd.Timedelta(seconds=1)]
        rows = []
        for i, (start, end) in enumerate(zip(edges, edges[1:])):
            window = events[(events.TimeStamp >= start) & (events.TimeStamp < end)].reset_index(drop=True)
            output = str(tmp_path / extension / str(i))
            SignalDataProcessor(raw_data=window, detector_config=detectors, bin_size=15, verbose=0, aggregations=[DELAY], output_dir=output,
                                output_format='parquet', output_to_separate_folders=False,
                                unmatched_event_settings={'df_or_path': path, 'max_days_old': 14, 'first_run': i == 0}).run()
            rows.append(duckdb.sql(f"SELECT * FROM '{output}/approach_delay.parquet'").df())
        pd.testing.assert_frame_equal(sort(pd.concat(rows, ignore_index=True)), sort(big), check_dtype=False)

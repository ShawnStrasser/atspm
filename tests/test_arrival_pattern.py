"""arrival_pattern is yellow_red for advance detectors: each on event is moved forward to the stop bar, belongs to the cycle it
arrives in, and its row to the bin of that cycle's red begin. The rows are the same however its runs are cut, with what is
waiting kept in unmatched_events (synthetic EventIds 960 to 963).
"""
import duckdb
import pandas as pd
import pytest
from src.atspm import SignalDataProcessor

T0 = pd.Timestamp('2026-09-01 08:00:00')
PHASE, DETECTOR = 2, 3
# 440.1 feet at 30 MPH is 10 seconds
CONFIG = pd.DataFrame({'DeviceId': ['A'], 'Phase': [PHASE], 'Parameter': [DETECTOR], 'Function': ['Advance'],
                       'DistanceFromStopBar': [440.1], 'MPH': [30.0], 'LatencyCorrection': [0.0]})
AP = {'name': 'arrival_pattern', 'params': {}}
FINE = {'name': 'arrival_pattern', 'params': {'offset_slice_seconds': 0.5}}
KEYS = ['TimeStamp', 'DeviceId', 'Phase', 'Signal_State', 'Red_Offset']
CODES = {960, 961, 962, 963}


def at(seconds, event, parameter=PHASE):
    return (T0 + pd.Timedelta(seconds=seconds), 'A', event, parameter)


def frame(*rows):
    return pd.DataFrame(rows, columns=['TimeStamp', 'DeviceId', 'EventId', 'Parameter']).sort_values(['TimeStamp', 'EventId']).reset_index(drop=True)


def cycle(start, green=30, yellow=4):
    return [at(start, 1), at(start + green, 8), at(start + green + yellow, 10)]


def process(events, detectors=CONFIG, aggregations=None, carry=None, bin_size=15, **kwargs):
    settings = None
    if carry is not None:
        settings = {'df_or_path': '' if isinstance(carry, str) else carry, 'max_days_old': 14}
    with SignalDataProcessor(raw_data=events, detector_config=detectors, bin_size=bin_size, verbose=0, unmatched_event_settings=settings,
                             aggregations=aggregations or [AP], **kwargs) as p:
        p.load()
        p.aggregate()
        tables = [row[0] for row in p.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        return {t: p.conn.execute(f'SELECT * FROM "{t}"').df() for t in tables}


def cut_runs(events, cuts, detectors=CONFIG, aggregations=None, bin_size=15, table='arrival_pattern', **kwargs):
    edges = [events.TimeStamp.min()] + sorted(cuts) + [events.TimeStamp.max() + pd.Timedelta(seconds=1)]
    outputs, carry = [], 'first'
    for start, end in zip(edges, edges[1:]):
        result = process(events[(events.TimeStamp >= start) & (events.TimeStamp < end)], detectors, aggregations, carry, bin_size, **kwargs)
        carry = result['unmatched_events']
        outputs.append(result[table])
    return pd.concat(outputs, ignore_index=True), carry


def seconds(*values):
    return [T0 + pd.Timedelta(seconds=v) for v in values]


def same_rows(a, b):
    assert not b.duplicated(KEYS).any(), 'a row was written twice'
    a, b = a.sort_values(KEYS).reset_index(drop=True), b.sort_values(KEYS).reset_index(drop=True)
    pd.testing.assert_frame_equal(a, b, check_dtype=False)


def steady(minutes=60, cycle_seconds=60):
    """A signal cycling steadily, with vehicles arriving across the whole cycle."""
    rows = []
    for start in range(0, minutes * 60, cycle_seconds):
        rows += cycle(start)
        rows += [at(start + 2.2, 82, DETECTOR), at(start + 21.3, 82, DETECTOR), at(start + 25.4, 82, DETECTOR), at(start + 40.6, 82, DETECTOR), at(start + 45.1, 82, DETECTOR)]
    rows.append(at(minutes * 60 + 1, 1))
    return frame(*rows)


# ---- the rows

def test_columns_are_those_of_yellow_red():
    out = process(steady(10))['arrival_pattern']
    assert list(out.columns) == ['TimeStamp', 'DeviceId', 'Phase', 'Signal_State', 'Red_Offset', 'Count']


def test_each_vehicle_is_moved_forward_to_the_stop_bar():
    # On at 5, 21 and 40 s arrive at 15, 31 and 50 s: on green, on yellow, on red. Red begins at 34 s.
    events = frame(*cycle(0), at(5, 82, DETECTOR), at(21, 82, DETECTOR), at(40, 82, DETECTOR), at(100, 1))
    out = process(events, aggregations=[FINE])['arrival_pattern'].sort_values('Red_Offset')
    assert out.Signal_State.tolist() == [1, 8, 10]
    assert out.Red_Offset.tolist() == [-19.0, -3.0, 16.0]


def test_a_vehicle_belongs_to_the_cycle_it_arrives_in():
    # On at 95 s, before the next green at 100 s, and arrives at 105 s in that cycle, whose red begins at 134 s
    events = frame(*cycle(0), *cycle(100), at(95, 82, DETECTOR), at(200, 1))
    out = process(events, aggregations=[FINE])['arrival_pattern']
    assert out.Signal_State.tolist() == [1]
    assert out.Red_Offset.tolist() == [-29.0]


def test_a_row_goes_to_the_bin_of_its_red_begin():
    # Red begins at 14:50 and the vehicle arrives 20 s later, in the next bin by the clock
    events = frame(*cycle(856), at(900, 82, DETECTOR), at(1200, 1))
    out = process(events, aggregations=[FINE])['arrival_pattern']
    assert out.TimeStamp.tolist() == [T0]
    assert out.Red_Offset.tolist() == [20.0]


@pytest.mark.parametrize('bin_size, stamps', [(5, 12), (15, 4)])
def test_bins_follow_bin_size(bin_size, stamps):
    out = process(steady(60), bin_size=bin_size)['arrival_pattern']
    assert out.TimeStamp.nunique() == stamps
    assert out.Count.sum() == 5 * 60  # the same vehicles in any bin size


def test_the_slice_is_5_seconds_unless_told_otherwise():
    # Arrivals 6, 8 and 9 s after the red begin
    events = frame(*cycle(0), at(30, 82, DETECTOR), at(32, 82, DETECTOR), at(33, 82, DETECTOR), at(100, 1))
    out = process(events)['arrival_pattern'].sort_values('Red_Offset')
    assert out.Red_Offset.tolist() == [5.0, 10.0]
    assert out.Count.tolist() == [1.0, 2.0]
    two = {'name': 'arrival_pattern', 'params': {'offset_slice_seconds': 2}}
    assert sorted(process(events, aggregations=[two])['arrival_pattern'].Red_Offset) == [6.0, 8.0, 10.0]


@pytest.mark.parametrize('value', [0, -1])
def test_a_slice_that_is_no_length_raises(value):
    with pytest.raises(ValueError, match='offset_slice_seconds'):
        SignalDataProcessor(raw_data=steady(1), detector_config=CONFIG, bin_size=15, verbose=0,
                            aggregations=[{'name': 'arrival_pattern', 'params': {'offset_slice_seconds': value}}])


def test_each_config_value_takes_the_same_default_as_arrival_on_green():
    # No distance or speed: 220 feet at 35 MPH on phase 2, 4.285 s. latency_offset_seconds takes a second off that.
    detectors = pd.DataFrame({'DeviceId': ['A'], 'Phase': [PHASE], 'Parameter': [DETECTOR], 'Function': ['Advance']})
    events = frame(*cycle(0), at(40, 82, DETECTOR), at(100, 1))
    params = {'offset_slice_seconds': 0.001}
    out = process(events, detectors, [{'name': 'arrival_pattern', 'params': params}])['arrival_pattern']
    assert out.Red_Offset.tolist() == pytest.approx([10.285], abs=0.001)
    out = process(events, detectors, [{'name': 'arrival_pattern', 'params': {**params, 'latency_offset_seconds': 1}}])['arrival_pattern']
    assert out.Red_Offset.tolist() == pytest.approx([9.285], abs=0.001)


def test_only_advance_detectors_count():
    detectors = pd.concat([CONFIG, CONFIG.assign(Parameter=4, Function='Yellow_Red')])
    events = frame(*cycle(0), at(5, 82, DETECTOR), at(6, 82, 4), at(100, 1))
    both = process(events, detectors, [AP, {'name': 'yellow_red', 'params': {'latency_offset_seconds': 0}}])
    assert both['arrival_pattern'].Count.sum() == 1
    assert both['yellow_red'].Count.sum() == 1


def test_no_spillback_handling():
    # A vehicle starting while its detector has been on a long time is still where the phase shows
    events = frame(*cycle(0), at(1, 82, DETECTOR), at(20, 81, DETECTOR), at(21, 82, DETECTOR), at(100, 1))
    assert sorted(process(events)['arrival_pattern'].Signal_State) == [1, 8]


def test_a_detector_on_two_phases_counts_on_each():
    detectors = pd.concat([CONFIG, CONFIG.assign(Phase=6)])
    events = frame(*cycle(0), *[(t, d, e, 6) for t, d, e, _ in cycle(0)], at(5, 82, DETECTOR), at(100, 1), at(100, 1, 6))
    assert sorted(process(events, detectors)['arrival_pattern'].Phase) == [2, 6]


# ---- across runs

@pytest.mark.parametrize('minutes', [
    [60],                                       # on the hour
    [15, 30, 45],        # many, inside cycles
])
def test_cuts_equal_one_big_run(minutes):
    events = steady(90)
    big = process(events, carry='first')['arrival_pattern']
    assert len(big) > 0
    cut, _ = cut_runs(events, [T0 + pd.Timedelta(minutes=m) for m in minutes])
    same_rows(big, cut)



def test_a_vehicle_not_arrived_when_the_run_ends_comes_back_in_the_next():
    # On at 95 s, arriving at 105 s. The run stops at 96 s, and the green it arrives in comes after.
    events = frame(*cycle(0), *cycle(100), at(95, 82, DETECTOR), at(96, 7, 8), at(200, 1), at(899.9, 7, 8))
    big = process(events, carry='first', aggregations=[FINE])['arrival_pattern']
    assert big.Red_Offset.tolist() == [-29.0]
    same_rows(big, cut_runs(events, seconds(98), aggregations=[FINE])[0])


def test_a_vehicle_before_the_first_green_ever_seen_comes_back_in_the_next_run():
    # On at 95 s, arriving at 105 s, and the first green the phase has shown comes at 100 s, after the cut
    events = frame(at(95, 82, DETECTOR), at(96, 7, 8), *cycle(100), at(200, 1), at(899.9, 7, 8))
    big = process(events, carry='first', aggregations=[FINE])['arrival_pattern']
    assert big.Count.sum() == 1
    same_rows(big, cut_runs(events, seconds(98), aggregations=[FINE])[0])


def test_a_vehicle_before_any_green_with_time_to_spare_is_not_kept():
    # On at 0 s, arriving at 10 s. A green still to come is after that, so it never had a cycle and is not kept for ever.
    events = frame(at(0, 82, DETECTOR), at(50, 7, 8))
    carry = process(events, carry='first')['unmatched_events']
    assert not set(carry.EventId) & CODES


def test_a_latency_longer_than_the_trip_moves_a_vehicle_back_and_cuts_still_agree():
    detectors = CONFIG.assign(LatencyCorrection=13.0)  # 3 s back, into the cycle before a green that has already been seen
    events = steady(30)
    big = process(events, detectors, carry='first')['arrival_pattern']
    assert big.Count.sum() > 0
    same_rows(big, cut_runs(events, [T0 + pd.Timedelta(minutes=m) for m in (5, 15.03, 20)], detectors)[0])


def test_an_empty_run_keeps_what_it_was_given():
    events = frame(*cycle(0), at(40, 82, DETECTOR), at(899.9, 7, 8))
    carry = process(events, carry='first')['unmatched_events']
    assert set(carry.EventId) >= CODES
    empty = process(events.iloc[0:0], carry=carry)
    assert len(empty['arrival_pattern']) == 0
    kept = lambda df: df[df.EventId.isin(CODES)].sort_values(['EventId', 'TimeStamp']).reset_index(drop=True)
    pd.testing.assert_frame_equal(kept(carry), kept(empty['unmatched_events']), check_dtype=False)


def test_an_empty_run_keeps_a_vehicle_waiting_for_its_first_green():
    events = frame(at(95, 82, DETECTOR), at(96, 7, 8))
    carry = process(events, carry='first')['unmatched_events']
    assert 963 in set(carry.EventId)
    assert 963 in set(process(events.iloc[0:0], carry=carry)['unmatched_events'].EventId)


def test_the_carried_events_are_only_as_long_as_needed():
    carry = cut_runs(steady(150), [T0 + pd.Timedelta(minutes=15 * i) for i in range(1, 10)])[1]
    assert (carry[carry.EventId.isin(CODES)].TimeStamp >= T0 + pd.Timedelta(minutes=120)).all()


def test_listed_with_yellow_red_each_keeps_its_own_rows():
    detectors = pd.concat([CONFIG, CONFIG.assign(Parameter=4, Function='Yellow_Red')])
    rows = [at(t, 82, 4) for t in range(11, 3600, 60)]
    events = pd.concat([steady(60), frame(*rows)]).sort_values(['TimeStamp', 'EventId']).reset_index(drop=True)
    yellow_red = {'name': 'yellow_red', 'params': {'latency_offset_seconds': 0}}
    cuts = seconds(900, 1234, 2700)
    alone = cut_runs(events, cuts, detectors)[0]
    yr_alone = cut_runs(events, cuts, detectors, [yellow_red], table='yellow_red')[0]
    together = cut_runs(events, cuts, detectors, [AP, yellow_red])[0]
    yr_together = cut_runs(events, cuts, detectors, [AP, yellow_red], table='yellow_red')[0]
    assert len(alone) and len(yr_alone)
    same_rows(alone, together)
    same_rows(yr_alone, yr_together)


def test_timeline_in_the_list_neither_changes_it_nor_sees_its_rows():
    has_data = {'name': 'has_data', 'params': {}}
    timeline = {'name': 'timeline', 'params': {'min_duration': 0.2, 'cushion_time': 60}}
    events = steady(60)
    cuts = [T0 + pd.Timedelta(minutes=m) for m in (15, 37.4, 45)]
    same_rows(cut_runs(events, cuts)[0], cut_runs(events, cuts, aggregations=[has_data, timeline, AP])[0])
    with_it = cut_runs(events, cuts, aggregations=[has_data, timeline, AP], table='timeline')[0]
    without = cut_runs(events, cuts, aggregations=[has_data, timeline], table='timeline')[0]
    columns = list(with_it.columns)
    pd.testing.assert_frame_equal(with_it.sort_values(columns).reset_index(drop=True), without.sort_values(columns).reset_index(drop=True))


def test_remove_incomplete_keeps_a_bin_written_in_a_later_run():
    # The bin of the red passed has_data in the first run, and its cycle only ends in the third
    has_data = {'name': 'has_data', 'params': {}}
    rows = [*cycle(100), at(190, 82, DETECTOR)]
    rows += [at(10 + 60 * i, 7, 8) for i in range(45)] + [at(899.9, 7, 8), at(1799.9, 7, 8)]
    rows += [at(2000, 1), at(2699.9, 7, 8)]
    events = frame(*rows)
    aggregations = [has_data, AP]
    big = process(events, aggregations=aggregations, carry='first', remove_incomplete=True)['arrival_pattern']
    cut, _ = cut_runs(events, seconds(900, 1800), aggregations=aggregations, remove_incomplete=True)
    assert cut.Count.tolist() == [1.0]
    same_rows(big, cut)


def test_to_sql_renders_a_query_that_runs():
    with SignalDataProcessor(to_sql=True, bin_size=15, verbose=0, aggregations=[FINE]) as p:
        p.aggregate()
        query = p.sql_queries['arrival_pattern']
    assert 'unmatched_previous' not in query
    connection = duckdb.connect()
    connection.register('events_frame', frame(*cycle(0), at(30, 82, DETECTOR)))
    connection.execute('CREATE TABLE raw_data AS SELECT * FROM events_frame')
    connection.register('config_frame', CONFIG)
    connection.execute('CREATE TABLE detector_config AS SELECT * FROM config_frame')
    connection.execute(query)
    assert connection.execute('SELECT Red_Offset, Count FROM arrival_pattern').fetchall() == [(6.0, 1.0)]


# ---- real events

def real():
    return (duckdb.query("SELECT * FROM 'tests/arrival_events_test_data.parquet' ORDER BY TimeStamp").df(),
            duckdb.query("SELECT * FROM 'tests/arrival_config_test_data.parquet'").df())



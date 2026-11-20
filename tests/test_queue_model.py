"""arrival_on_green's queue model (queue_model=True), from how it should work.

A vehicle that crosses an advance detector at speed on green, and then stops behind cars still leaving the stop bar, did
not arrive on green without a queue. The model queues each detector's vehicles at the stop bar in the order they arrive:
service starts 5 s after green begins and ends 3 s after the green ends, at 2 s a car a lane (the standard 2n + 5). A car
that waits 4 s or more is queued and counts as an arrival on red. Its arrival is its on plus the config's TravelTime, or
without one at least 2 s plus the distance at 70 ft/s after its on, the travel time measured at ODOT lanes. The lanes
between the detector and the stop bar (counted from the lane numbers in Lanes) hold lanes x distance / 25 ft cars: of the
cars that arrive before service, only that many join, and a car the queue would put past the detector that did not sit on
it is not queued. A detector whose RedDepartures (share of its lanes' stop bar count zone offs in red) is over 0.25 is left
out and takes the flag alone.

The times here are seconds from T0. Green begins at 60 s (service from 65 s), yellow at 100 s, red clearance at 104 s.
"""
import duckdb
import pandas as pd
import pytest
from src.atspm import SignalDataProcessor


T0 = pd.Timestamp('2026-09-01 08:00:00')
PHASE, DETECTOR = 2, 3
QUEUE = {'name': 'arrival_on_green', 'params': {'latency_offset_seconds': 0, 'queue_model': True}}
FLAG_ONLY = {'name': 'arrival_on_green', 'params': {'latency_offset_seconds': 0}}
PLATOON = {'name': 'platoon_ratio', 'params': {}}
KEYS = ['TimeStamp', 'DeviceId', 'Phase']
END_OF_BIN = [(T0 + pd.Timedelta(seconds=899.9), 'A', 1, 15)]


def at(seconds, event, parameter=PHASE, device='A'):
    return (T0 + pd.Timedelta(seconds=seconds), device, event, parameter)


def frame(*rows):
    return pd.DataFrame(rows, columns=['TimeStamp', 'DeviceId', 'EventId', 'Parameter']).sort_values(['TimeStamp', 'EventId']).reset_index(drop=True)


def config(distance, lanes=None, travel_time=None, latency=None, red_departures=None):
    c = pd.DataFrame({'DeviceId': ['A'], 'Phase': [PHASE], 'Parameter': [DETECTOR], 'Function': ['Advance'], 'DistanceFromStopBar': [float(distance)]})
    for column, value in (('Lanes', lanes), ('TravelTime', travel_time), ('LatencyCorrection', latency), ('RedDepartures', red_departures)):
        if value is not None:
            c[column] = [value]
    return c


def cars(*ons, seconds_on=0.5):
    return [row for on in ons for row in (at(on, 82, DETECTOR), at(on + seconds_on, 81, DETECTOR))]


SIGNAL = [at(0, 10), at(60, 1), at(100, 8), at(104, 10)]


def process(events, detectors, aggregations=None, carry=None):
    settings = None if carry is None else {'df_or_path': '' if isinstance(carry, str) else carry, 'max_days_old': 14}
    with SignalDataProcessor(raw_data=events, detector_config=detectors, bin_size=15, verbose=0, unmatched_event_settings=settings,
                             aggregations=aggregations or [QUEUE, PLATOON]) as p:
        p.load()
        p.aggregate()
        tables = [row[0] for row in p.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        return {t: p.conn.execute(f'SELECT * FROM "{t}"').df() for t in tables}


def on_green(events, detectors, aggregation=QUEUE):
    """(vehicles, vehicles on green) of the one bin"""
    out = process(events, detectors, [aggregation])['arrival_on_green']
    assert len(out) == 1
    return int(out.Total_Actuations[0]), round(float(out.Percent_AOG[0]) * int(out.Total_Actuations[0]))


# ---- what the model does

def test_a_car_on_green_behind_the_queue_is_queued_and_one_after_it_is_not():
    # 100 ft: projected 1.9 s after the on, modelled 3.4 s after. Three cars on red leave at 65, 67 and 69 s. The car on at
    # 61 s reaches the bar on green at 62.9 s, but its turn is 71 s: queued. The car on at 70 s arrives at 73.4 s as the
    # queue has gone.
    events = frame(*SIGNAL, *cars(40, 42, 44, 61, 70), *END_OF_BIN)
    assert on_green(events, config(100), FLAG_ONLY) == (5, 2)
    assert on_green(events, config(100)) == (5, 1)


def test_two_lanes_clear_twice_as_fast():
    # Four cars on red. One lane: they leave at 65, 67, 69, 71 s and the car modelled at 66.4 s waits until 73 s. Two
    # lanes: 65, 66, 67, 68 s, and it leaves at 69 s, 2.6 s after it arrives.
    events = frame(*SIGNAL, *cars(40, 42, 44, 46, 63), *END_OF_BIN)
    assert on_green(events, config(100)) == (5, 0)
    assert on_green(events, config(100, lanes='1,2')) == (5, 1)
    # Lanes holds lane numbers, as atspm-detector writes them: "2" is one lane, lane 2
    assert on_green(events, config(100, lanes='2')) == (5, 0)
    assert on_green(events, config(100, lanes=2)) == (5, 0)


def test_a_queue_that_would_reach_past_the_detector_with_no_car_on_it_is_not_believed():
    # 300 ft holds 12 cars: five on red, and the car modelled at 67.3 s leaves at 75 s, queued. At 60 ft the space holds
    # 2.4 cars, so the model's queue would stand over the detector, yet the car crossed it in half a second.
    events = frame(*SIGNAL, *cars(40, 42, 44, 46, 48, 61), *END_OF_BIN)
    assert on_green(events, config(300)) == (6, 0)
    assert on_green(events, config(60)) == (6, 1)


def test_the_model_takes_the_measured_travel_time_not_35_mph():
    # 150 ft: 35 MPH puts a car at the bar 2.9 s after its on, the model 4.1 s after (2 s + 150 ft at 70 ft/s). Three cars
    # on red leave at 65, 67 and 69 s. The car on at 63.2 s is due at 67.3 s and leaves at 71 s, 3.7 s later: not queued.
    # At 35 MPH it would have been due at 66.1 s and waited 4.9 s. The car on at 62 s is due at 66.1 s: 4.9 s, queued.
    signal = SIGNAL + [at(160, 1), at(200, 8), at(204, 10)]
    assert on_green(frame(*signal, *cars(40, 42, 44, 63.2), *END_OF_BIN), config(150)) == (4, 1)
    assert on_green(frame(*signal, *cars(40, 42, 44, 62.0), *END_OF_BIN), config(150)) == (4, 0)


def test_a_measured_travel_time_replaces_the_estimate_and_no_latency_comes_off():
    # The same cars at 150 ft. A TravelTime of 2.9 s puts the car on at 63.2 s at the bar at 66.1 s: it waits 4.9 s,
    # queued. One of 4.1 s puts it there at 67.3 s, not queued. Both times were measured between logged events, so a
    # LatencyCorrection of 1.5 s, already in them, changes neither.
    signal = SIGNAL + [at(160, 1), at(200, 8), at(204, 10)]
    events = frame(*signal, *cars(40, 42, 44, 63.2), *END_OF_BIN)
    assert on_green(events, config(150, travel_time=2.9)) == (4, 0)
    assert on_green(events, config(150, travel_time=4.1)) == (4, 1)
    assert on_green(events, config(150, travel_time=4.1, latency=1.5)) == (4, 1)
    assert on_green(events, config(150, latency=1.5)) == (4, 1)


def test_a_detector_whose_lane_empties_on_red_is_left_to_the_flag():
    # Where over a fifth of the lane's stop bar count offs come in red (a permissive left, a right turn on red), the queue
    # empties without a green the model sees, so the detector is left out and gives the flag-only answer.
    events = frame(*SIGNAL, *cars(40, 42, 44, 61, 70), *END_OF_BIN)
    flag = on_green(events, config(100), FLAG_ONLY)
    assert on_green(events, config(100, red_departures=0.35)) == flag == (5, 2)
    assert on_green(events, config(100, red_departures=0.25)) == (5, 1)
    assert on_green(events, config(100, red_departures=float('nan'))) == (5, 1)


def test_each_green_is_its_own_queue():
    # Twelve cars on red at 300 ft cannot all leave in the 38 s of service: the ones left over count as arrivals on red,
    # and the next cycle starts empty. A car on green in the next cycle with nothing ahead of it is on green.
    signal = SIGNAL + [at(160, 1), at(200, 8), at(204, 10)]
    events = frame(*signal, *cars(*[20 + 3 * i for i in range(12)], 170), *END_OF_BIN)
    assert on_green(events, config(300)) == (13, 1)


def test_the_flag_still_counts_and_the_model_adds_to_it():
    # A car sits on the detector from 40 s to 70 s: the flag. The model does not change what the flag says.
    events = frame(*SIGNAL, at(40, 82, DETECTOR), at(70, 81, DETECTOR), *cars(72, 74, 90), *END_OF_BIN)
    flag = on_green(events, config(200), FLAG_ONLY)
    both = on_green(events, config(200))
    assert both[0] == flag[0] and both[1] <= flag[1]


def test_off_is_the_default_and_gives_the_flag_only_answer():
    events = frame(*SIGNAL, *cars(40, 42, 44, 61, 70), *END_OF_BIN)
    off = {'name': 'arrival_on_green', 'params': {'latency_offset_seconds': 0, 'queue_model': False}}
    assert on_green(events, config(100), off) == on_green(events, config(100), FLAG_ONLY)


@pytest.mark.parametrize('params, message', [
    ({'queue_model': 'yes'}, 'True or False'),
    ({'queue_model': True, 'spillback_on_seconds': None}, 'spillback flag'),
])
def test_a_bad_setting_is_refused(params, message):
    aggregation = {'name': 'arrival_on_green', 'params': {'latency_offset_seconds': 0, **params}}
    with pytest.raises(ValueError, match=message):
        SignalDataProcessor(raw_data=frame(*SIGNAL), detector_config=config(100), bin_size=15, verbose=0, aggregations=[aggregation])


def test_to_sql_renders_a_query_that_runs_on_a_config_with_lanes_travel_time_and_red_departures():
    events = frame(*SIGNAL, *cars(40, 42, 44, 61, 70), *END_OF_BIN)
    with SignalDataProcessor(to_sql=True, bin_size=15, verbose=0, aggregations=[QUEUE]) as p:
        p.aggregate()
        query = p.sql_queries['arrival_on_green']
    connection = duckdb.connect()
    connection.register('events_frame', events)
    connection.execute('CREATE TABLE raw_data AS SELECT * FROM events_frame')
    connection.execute("""CREATE TABLE detector_config AS SELECT 'A' AS DeviceId, 2::INT16 AS Phase, 3::INT16 AS Parameter, 'Advance' AS Function,
        100.0::DOUBLE AS DistanceFromStopBar, NULL::DOUBLE AS MPH, NULL::DOUBLE AS LatencyCorrection, '1' AS Lanes, NULL::DOUBLE AS TravelTime,
        NULL::DOUBLE AS RedDepartures""")
    connection.execute(query)
    assert connection.execute('SELECT Total_Actuations, Percent_AOG FROM arrival_on_green').fetchall() == [(5, pytest.approx(0.2))]


# ---- runs cut anywhere give what one run gives

def cut_runs(events, detectors, cuts, aggregations=None):
    edges = [events.TimeStamp.min()] + sorted(cuts) + [events.TimeStamp.max() + pd.Timedelta(seconds=1)]
    outputs, carry = {'arrival_on_green': [], 'platoon_ratio': []}, 'first'
    for start, end in zip(edges, edges[1:]):
        result = process(events[(events.TimeStamp >= start) & (events.TimeStamp < end)], detectors, aggregations, carry)
        carry = result['unmatched_events']
        for table in outputs:
            outputs[table].append(result[table])
    return {t: pd.concat(v) for t, v in outputs.items()}, carry


def same_rows(a, b):
    a, b = a.sort_values(KEYS).reset_index(drop=True), b.sort_values(KEYS).reset_index(drop=True)
    assert not b.duplicated(KEYS).any(), 'a bin was written twice'
    pd.testing.assert_frame_equal(a, b, check_dtype=False, check_exact=False, rtol=1e-4)


def queues_across_bins():
    """Cycles of 100 s for half an hour, a queue each red and cars on green behind it, so cuts land inside queues."""
    rows = [at(0, 10)]
    for c in range(18):
        g = 60 + 100 * c
        rows += [at(g, 1), at(g + 40, 8), at(g + 44, 10)]
        rows += cars(*[g - 50 + 4 * i for i in range(8)], g + 1, g + 3, g + 9, g + 25)
    return frame(*rows, at(2699.9, 1, 15))


@pytest.mark.parametrize('cut', [61.0, 64.5, 66.0, 69.0, 900.0, 903.2, 960.5, 1003.0])
def test_one_cut_anywhere_equals_one_big_run(cut):
    events, detectors = queues_across_bins(), config(150)
    whole = process(events, detectors, carry='first')
    runs, _ = cut_runs(events, detectors, [T0 + pd.Timedelta(seconds=cut)])
    for table in ('arrival_on_green', 'platoon_ratio'):
        same_rows(whole[table], runs[table])


def test_many_cuts_equal_one_big_run():
    events, detectors = queues_across_bins(), config(150)
    whole = process(events, detectors, carry='first')
    runs, _ = cut_runs(events, detectors, [T0 + pd.Timedelta(seconds=s) for s in range(37, 2700, 73)])
    for table in ('arrival_on_green', 'platoon_ratio'):
        same_rows(whole[table], runs[table])


def test_an_unfinished_queue_is_carried_and_finished_by_the_next_run():
    events, detectors = queues_across_bins(), config(150)
    first = process(events[events.TimeStamp < T0 + pd.Timedelta(seconds=962)], detectors, carry='first')
    carried = first['unmatched_events']
    queue = carried[carried.EventId == 965]
    assert len(queue) and (queue.Parameter % 1000 == DETECTOR).all()
    assert ((queue.Parameter // 8000) % 2 == 0).any()   # some wait to be counted by the next run


def test_a_long_green_does_not_hold_results_back():
    # Rest in green for 50 minutes with a car a minute: each car is known the moment it is 6 s past its departure, so a run
    # cut at 30 minutes writes the same bins as one without the model and carries only the car at the cut.
    events = frame(at(0, 1), *cars(*range(30, 3000, 60)), at(3000, 8), at(3004, 10), at(3599.9, 1, 15))
    part = events[events.TimeStamp < T0 + pd.Timedelta(minutes=30)]
    first = process(part, config(200), carry='first')
    flag = process(part, config(200), [FLAG_ONLY, PLATOON], carry='first')
    assert list(first['arrival_on_green'].TimeStamp) == list(flag['arrival_on_green'].TimeStamp)
    queue = first['unmatched_events'][first['unmatched_events'].EventId == 965]
    assert list(queue.TimeStamp) == [T0 + pd.Timedelta(seconds=1770)]
    whole = process(events, config(200), carry='first')
    runs, _ = cut_runs(events, config(200), [T0 + pd.Timedelta(minutes=30)])
    same_rows(whole['arrival_on_green'], runs['arrival_on_green'])


# ---- real events

REAL_EVENTS = 'tests/spillback_events_test_data.parquet'  # Two signals, 12:00 to 21:00 on Oct 27 2026, one with queues over its detectors
REAL_CONFIG = 'tests/spillback_config_test_data.parquet'


def real():
    return duckdb.query(f"SELECT * FROM '{REAL_EVENTS}' ORDER BY TimeStamp").df(), duckdb.query(f"SELECT * FROM '{REAL_CONFIG}'").df()


def test_real_data_moves_vehicles_off_green_and_keeps_the_totals():
    events, detectors = real()
    model = process(events, detectors, [QUEUE])['arrival_on_green']
    flag = process(events, detectors, [FLAG_ONLY])['arrival_on_green']
    assert model.Total_Actuations.sum() == flag.Total_Actuations.sum()
    green = lambda df: (df.Percent_AOG * df.Total_Actuations).sum()
    assert green(model) < 0.95 * green(flag)
    both = model.merge(flag, on=KEYS, suffixes=('', '_flag'))
    assert len(both) == len(model) == len(flag)
    assert (both.Percent_AOG <= both.Percent_AOG_flag + 1e-6).all()


def test_real_data_cut_every_15_minutes_and_at_odd_times_equals_one_big_run():
    events, detectors = real()
    whole = process(events, detectors, carry='first')
    start = events.TimeStamp.min().floor('15min')
    cuts = [start + pd.Timedelta(minutes=15 * i) for i in range(1, 36)] + [start + pd.Timedelta(minutes=95, seconds=7.3), start + pd.Timedelta(minutes=241, seconds=44.1)]
    runs, _ = cut_runs(events, detectors, sorted(set(cuts)))
    for table in ('arrival_on_green', 'platoon_ratio'):
        same_rows(whole[table], runs[table])


def test_turning_the_model_on_after_a_run_without_it_works():
    events, detectors = real()
    cut = events.TimeStamp.min().floor('15min') + pd.Timedelta(hours=3)
    first = process(events[events.TimeStamp < cut], detectors, [FLAG_ONLY, PLATOON], 'first')
    second = process(events[events.TimeStamp >= cut], detectors, [QUEUE, PLATOON], first['unmatched_events'])
    whole = process(events, detectors, [QUEUE, PLATOON], 'first')
    later = whole['arrival_on_green'][whole['arrival_on_green'].TimeStamp >= cut + pd.Timedelta(minutes=15)]
    mine = second['arrival_on_green'][second['arrival_on_green'].TimeStamp >= cut + pd.Timedelta(minutes=15)]
    same_rows(later, mine)


def test_turning_the_model_off_after_a_run_with_it_counts_every_vehicle_once():
    events, detectors = real()
    cut = events.TimeStamp.min().floor('15min') + pd.Timedelta(hours=3)
    first = process(events[events.TimeStamp < cut], detectors, [QUEUE, PLATOON], 'first')
    assert first['unmatched_events'].EventId.isin([964, 965]).any()
    second = process(events[events.TimeStamp >= cut], detectors, [FLAG_ONLY, PLATOON], first['unmatched_events'])
    whole = process(events, detectors, [FLAG_ONLY, PLATOON], 'first')['arrival_on_green']
    both = pd.concat([first['arrival_on_green'], second['arrival_on_green']])
    assert not both.duplicated(KEYS).any()
    assert both.Total_Actuations.sum() == whole.Total_Actuations.sum()
    later = whole[whole.TimeStamp >= cut + pd.Timedelta(minutes=15)]
    same_rows(later, second['arrival_on_green'][second['arrival_on_green'].TimeStamp >= cut + pd.Timedelta(minutes=15)])

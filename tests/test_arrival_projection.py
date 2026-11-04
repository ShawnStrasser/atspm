"""arrival_on_green and platoon_ratio: arrivals projected to the stop bar with the detector's distance and speed,
and what is carried between incremental runs (synthetic EventIds 938 to 942 in unmatched_events)."""
import random

import duckdb
import numpy as np
import pandas as pd
import pytest
from src.atspm import SignalDataProcessor

# Real events (1, 8, 10, 82 and enough others to fill the log) of 4 signals, 6 hours of Oct 5 2026, and their
# advance detectors with the setbacks and speeds of ODOT's detector classifier.
REAL_EVENTS = 'tests/arrival_events_test_data.parquet'
REAL_CONFIG = 'tests/arrival_config_test_data.parquet'

T0 = pd.Timestamp('2026-09-01 08:00:00')
KEYS = ['TimeStamp', 'DeviceId', 'Phase']
AOG = {'name': 'arrival_on_green', 'params': {'latency_offset_seconds': 0}}
PLATOON = {'name': 'platoon_ratio', 'params': {}}
TWO_TABLES = [AOG, PLATOON]
TRAVEL_220 = 220 / (35 * 1.467)  # 4.28 seconds
TRAVEL_120 = 120 / (35 * 1.467)  # 2.34 seconds


def at(seconds, event, parameter, device='A'):
    return (T0 + pd.Timedelta(seconds=seconds), device, event, parameter)


def frame(*rows):
    return pd.DataFrame(rows, columns=['TimeStamp', 'DeviceId', 'EventId', 'Parameter'])


def config(*rows, **columns):
    """Rows of (Phase, Parameter) on device A, as advance detectors. columns are extra columns, a list per row."""
    df = pd.DataFrame({'DeviceId': 'A', 'Phase': [r[0] for r in rows], 'Parameter': [r[1] for r in rows], 'Function': 'Advance'})
    for name, values in columns.items():
        df[name] = values
    return df


def process(events, detectors, aggregations=None, carry=None, **kwargs):
    """One run. Returns {table: DataFrame}. carry: None for no unmatched settings, 'first' for a first
    incremental run, or the unmatched_events of the previous run."""
    settings = None
    if carry is not None:
        settings = {'df_or_path': '' if isinstance(carry, str) else carry, 'max_days_old': 14}
    with SignalDataProcessor(raw_data=events, detector_config=detectors, bin_size=15, verbose=0,
                             unmatched_event_settings=settings, aggregations=aggregations or TWO_TABLES, **kwargs) as p:
        p.load()
        p.aggregate()
        tables = [row[0] for row in p.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        return {t: p.conn.execute(f'SELECT * FROM "{t}"').df() for t in tables}


def cut_runs(events, detectors, cuts, aggregations=None, **kwargs):
    """The events cut into runs at the given times. Returns the output tables concatenated, and the last carry."""
    edges = [events.TimeStamp.min()] + sorted(cuts) + [events.TimeStamp.max() + pd.Timedelta(seconds=1)]
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


def real_events():
    return duckdb.query(f"SELECT * FROM '{REAL_EVENTS}' ORDER BY TimeStamp").df()


def real_config():
    return duckdb.query(f"SELECT * FROM '{REAL_CONFIG}'").df()


# ---- the arrival time

def test_old_four_column_config_takes_the_defaults():
    # Green on phases 2 and 4 from 10 s to 20 s. An on event at 7 s lands in green on phase 2 (220 ft is 4.28 s)
    # but before green on phase 4 (120 ft is 2.34 s). The one at 12 s is on green on both.
    events = frame(at(10, 1, 2), at(20, 8, 2), at(10, 1, 4), at(20, 8, 4),
                   at(7, 82, 3), at(12, 82, 3),
                   at(7, 82, 5), at(12, 82, 5),
                   at(899.9, 1, 7))
    out = process(events, config((2, 3), (4, 5)))['arrival_on_green'].set_index('Phase')
    assert out.loc[2, 'Total_Actuations'] == 2 and out.loc[2, 'Percent_AOG'] == 1.0
    assert out.loc[4, 'Total_Actuations'] == 2 and out.loc[4, 'Percent_AOG'] == 0.5


def test_phases_2_and_6_take_220_feet_and_the_others_120():
    for phase, travel in [(2, TRAVEL_220), (6, TRAVEL_220), (1, TRAVEL_120), (4, TRAVEL_120), (8, TRAVEL_120)]:
        # Arrivals straddle the moment green begins by 20 ms either side of the expected travel time
        events = frame(at(100, 1, phase), at(100 - travel - 0.02, 82, 3), at(100 - travel + 0.02, 82, 3), at(200, 8, phase), at(899.9, 1, 15))
        out = process(events, config((phase, 3)))['arrival_on_green']
        assert out.Total_Actuations.tolist() == [2] and out.Percent_AOG.tolist() == [0.5], phase


@pytest.mark.parametrize('columns, travel', [
    ({'DistanceFromStopBar': [440.0], 'MPH': [70.0]}, 440 / (70 * 1.467)),
    ({'DistanceFromStopBar': [440.0]}, 440 / (35 * 1.467)),                         # no speed column
    ({'MPH': [20.0]}, 220 / (20 * 1.467)),                                          # no distance column
    ({'DistanceFromStopBar': [np.nan], 'MPH': [20.0]}, 220 / (20 * 1.467)),         # this row has no distance
    ({'DistanceFromStopBar': [440.0], 'MPH': [None]}, 440 / (35 * 1.467)),          # this row has no speed
    ({'distance_from_stop_bar': [440.0], 'mph': [70.0]}, 440 / (70 * 1.467)),       # names ignore case and underscores
    ({'DistanceFromStopBar': [0.0], 'MPH': [35.0]}, 0.0),                           # a stop bar detector does not move
    ({'DistanceFromStopBar': [440.0], 'MPH': [0.0]}, 440 / (35 * 1.467)),           # zero speed is not a speed
    ({'DistanceFromStopBar': ['n/a'], 'MPH': ['fast']}, 220 / (35 * 1.467)),        # text is not a number
])
def test_each_value_comes_from_the_config_or_its_default(columns, travel):
    events = frame(at(100, 1, 2), at(100 - travel - 0.02, 82, 3), at(100 - travel + 0.02, 82, 3), at(200, 8, 2), at(899.9, 1, 15))
    out = process(events, config((2, 3), **columns))['arrival_on_green']
    assert out.Total_Actuations.tolist() == [2] and out.Percent_AOG.tolist() == [0.5]


def test_detectors_on_one_phase_each_use_their_own_row():
    # Detector 3 has its own distance, detector 4 has only a speed, detector 5 has neither
    detectors = config((2, 3), (2, 4), (2, 5), DistanceFromStopBar=[440.0, None, None], MPH=[None, 20.0, None])
    t3, t4, t5 = 440 / (35 * 1.467), 220 / (20 * 1.467), TRAVEL_220
    events = frame(at(100, 1, 2), at(200, 8, 2), at(899.9, 1, 15),
                   at(100 - t3 + 0.02, 82, 3), at(100 - t4 + 0.02, 82, 4), at(100 - t5 + 0.02, 82, 5),
                   at(100 - t3 - 0.02, 82, 3), at(100 - t4 - 0.02, 82, 4), at(100 - t5 - 0.02, 82, 5))
    out = process(events, detectors)['arrival_on_green']
    assert out.Total_Actuations.tolist() == [6] and out.Percent_AOG.tolist() == [0.5]


def test_config_columns_load_from_files(tmp_path):
    detectors = config((2, 3), DistanceFromStopBar=[440.0], MPH=[70.0])
    detectors.to_csv(tmp_path / 'c.csv', index=False)
    detectors.to_parquet(tmp_path / 'c.parquet')
    travel = 440 / (70 * 1.467)
    events = frame(at(100, 1, 2), at(100 - travel + 0.02, 82, 3), at(100 - travel - 0.02, 82, 3), at(200, 8, 2), at(899.9, 1, 15))
    for path in ('c.csv', 'c.parquet'):
        out = process(events, str(tmp_path / path))['arrival_on_green']
        assert out.Percent_AOG.tolist() == [0.5], path


def test_latency_is_taken_off_the_arrival_time():
    # UDOT subtracts its latency correction from the projected time, and latency_offset_seconds is that value
    events = frame(at(100, 1, 2), at(100 - TRAVEL_220 + 0.5, 82, 3), at(200, 8, 2), at(899.9, 1, 15))
    assert process(events, config((2, 3)))['arrival_on_green'].Percent_AOG.tolist() == [1.0]
    latency = {'name': 'arrival_on_green', 'params': {'latency_offset_seconds': 1}}
    out = process(events, config((2, 3)), [latency])['arrival_on_green']
    assert out.Total_Actuations.tolist() == [1] and out.Percent_AOG.tolist() == [0.0]  # now it arrives on red


# Green from 100 s to 200 s, and on events 0.5 s after an arrival would reach green with no latency at all
def latency_events(detector=3, travel=TRAVEL_220):
    return frame(at(100, 1, 2), at(100 - travel + 0.5, 82, detector), at(200, 8, 2), at(899.9, 1, 15))


@pytest.mark.parametrize('columns, percent', [
    ({'LatencyCorrection': [1.0]}, 0.0),                # the detector's own moves it back onto red
    ({'LatencyCorrection': [0.0]}, 1.0),                # a real zero beats a run-wide value
    ({'latency_correction': [1.0]}, 0.0),               # names ignore case and underscores
    ({'LatencyCorrection': [None]}, None),              # blank falls back
    ({'LatencyCorrection': ['n/a']}, None),             # text is not a number, and falls back
    ({}, None),                                         # no column falls back
])
@pytest.mark.parametrize('run_wide', [0, 1])
def test_latency_is_per_detector_with_the_setting_as_fallback(columns, percent, run_wide):
    expected = percent if percent is not None else (0.0 if run_wide else 1.0)
    aggregation = {'name': 'arrival_on_green', 'params': {'latency_offset_seconds': run_wide}}
    out = process(latency_events(), config((2, 3), **columns), [aggregation])['arrival_on_green']
    assert out.Percent_AOG.tolist() == [expected]


def test_detectors_on_one_phase_each_use_their_own_latency():
    detectors = config((2, 3), (2, 4), (2, 5), LatencyCorrection=[1.0, 0.25, None])
    events = pd.concat([latency_events(3), latency_events(4), latency_events(5)]).drop_duplicates()
    out = process(events, detectors, [{'name': 'arrival_on_green', 'params': {'latency_offset_seconds': 2}}])['arrival_on_green']
    # Detector 3 lands on red, 4 on green, and 5 falls back to 2 seconds and lands on red
    assert out.Total_Actuations.tolist() == [3] and out.Percent_AOG.tolist() == [pytest.approx(1 / 3)]


def test_latency_alone_needs_no_setting():
    aggregation = {'name': 'arrival_on_green', 'params': {}}
    out = process(latency_events(), config((2, 3), LatencyCorrection=[1.0]), [aggregation])['arrival_on_green']
    assert out.Percent_AOG.tolist() == [0.0]
    assert process(latency_events(), config((2, 3)), [aggregation])['arrival_on_green'].Percent_AOG.tolist() == [1.0]


# ---- bins with vehicles and none on green

def test_bin_with_vehicles_and_none_on_green_reads_zero():
    # The first bin has green and all of its vehicles on red. The second has no green at all.
    events = frame(at(100, 1, 2), at(110, 8, 2), at(200, 82, 3), at(210, 82, 3), at(1000, 82, 3), at(1799.9, 1, 15))
    out = process(events, config((2, 3)))
    aog = out['arrival_on_green'].sort_values('TimeStamp')
    assert aog.Total_Actuations.tolist() == [2, 1] and aog.Percent_AOG.tolist() == [0.0, 0.0]
    # A phase with green and no vehicles on it is a platoon ratio of zero. A phase with no green has no ratio at all.
    platoon = out['platoon_ratio']
    assert platoon.TimeStamp.tolist() == [T0]
    row = platoon.iloc[0]
    assert row.Green_Actuations == 0 and row.Platoon_Ratio == 0 and row.Arrival_Type == 1 and row.Green_Seconds == 10


def test_bin_with_no_vehicles_has_no_row():
    events = frame(at(100, 1, 2), at(110, 8, 2), at(200, 82, 3), at(1000, 1, 2), at(1100, 8, 2), at(1799.9, 1, 15))
    assert process(events, config((2, 3)))['arrival_on_green'].TimeStamp.tolist() == [T0]


def test_zero_bins_match_across_runs():
    events = frame(at(100, 1, 2), at(110, 8, 2), at(200, 82, 3), at(899.9, 1, 15),
                   at(1000, 82, 3), at(1799.9, 1, 15), at(1900, 1, 2), at(1910, 82, 3), at(1950, 8, 2), at(2699.9, 1, 15))
    detectors = config((2, 3))
    runs, _ = cut_runs(events, detectors, [T0 + pd.Timedelta(minutes=m) for m in (15, 30)])
    big = process(events, detectors, carry='first')
    assert runs['arrival_on_green'].Percent_AOG.tolist().count(0.0) == 2
    same_rows(big['arrival_on_green'], runs['arrival_on_green'])
    same_rows(big['platoon_ratio'], runs['platoon_ratio'])


def test_to_sql_renders_a_query_for_a_table_with_the_three_columns():
    with SignalDataProcessor(to_sql=True, bin_size=15, verbose=0, aggregations=TWO_TABLES) as p:
        p.aggregate()
        query = p.sql_queries['arrival_on_green']
    assert 'DistanceFromStopBar' in query and 'unmatched_previous' not in query
    events = frame(at(100, 1, 2), at(100, 82, 3), at(200, 8, 2), at(899.9, 1, 15))
    connection = duckdb.connect()
    connection.register('events_frame', events)
    connection.execute("CREATE TABLE raw_data AS SELECT * FROM events_frame")
    connection.execute("""CREATE TABLE detector_config AS SELECT 'A' AS DeviceId, 2::INT16 AS Phase, 3::INT16 AS Parameter,
        'Advance' AS Function, NULL::DOUBLE AS DistanceFromStopBar, NULL::DOUBLE AS MPH, NULL::DOUBLE AS LatencyCorrection""")
    connection.execute(query)
    assert connection.execute("SELECT Total_Actuations, Percent_AOG FROM arrival_on_green").fetchall() == [(1, 1.0)]


def test_other_measures_ignore_the_new_config_columns():
    events = duckdb.query(f"SELECT * FROM '{REAL_EVENTS}' WHERE TimeStamp < '2026-10-05 06:30:00'").df()
    detectors = real_config()
    aggregations = [{'name': 'actuations', 'params': {}},
                    {'name': 'yellow_red', 'params': {'latency_offset_seconds': 1.5, 'min_red_offset': -8}},
                    {'name': 'split_failures', 'params': {'red_time': 5, 'red_occupancy_threshold': 0.80, 'green_occupancy_threshold': 0.80, 'by_approach': True, 'by_cycle': False}}]
    new = process(events, detectors, aggregations)
    old = process(events, detectors[['DeviceId', 'Phase', 'Parameter', 'Function']], aggregations)
    for table in ('actuations', 'yellow_red', 'split_failures'):
        pd.testing.assert_frame_equal(
            new[table].sort_values(list(new[table].columns)).reset_index(drop=True),
            old[table].sort_values(list(old[table].columns)).reset_index(drop=True))


# ---- across bins and runs

def test_arrival_belongs_to_the_bin_it_lands_in():
    # On at 14:58 into the bin, arriving 15:02.3: it is the second bin's
    events = frame(at(10, 1, 2), at(897.9, 82, 3), at(1000, 8, 2), at(1799.9, 1, 7))
    out = process(events, config((2, 3)))['arrival_on_green']
    assert out.TimeStamp.tolist() == [T0 + pd.Timedelta(minutes=15)] and out.Total_Actuations.tolist() == [1]


def test_arrival_past_the_end_is_held_and_written_by_the_next_run():
    first = frame(at(10, 1, 2), at(897.9, 82, 3), at(899.9, 1, 7))
    second = frame(at(1000, 8, 2), at(1799.9, 1, 7))
    run1 = process(first, config((2, 3)), carry='first')
    assert len(run1['arrival_on_green']) == 0
    state = run1['unmatched_events'].set_index('EventId')
    assert state.loc[938, 'Parameter'] == 2  # held, with its phase
    assert state.loc[938, 'TimeStamp'] == T0 + pd.Timedelta(seconds=897.9 + round(TRAVEL_220, 3))
    assert state.loc[939, 'TimeStamp'] == T0 + pd.Timedelta(minutes=15)  # green when results stopped
    run2 = process(second, config((2, 3)), carry=run1['unmatched_events'])
    out = run2['arrival_on_green']
    assert out.TimeStamp.tolist() == [T0 + pd.Timedelta(minutes=15)]
    assert out.Total_Actuations.tolist() == [1] and out.Percent_AOG.tolist() == [1.0]  # the green from the first run counts
    assert not run2['unmatched_events'].EventId.isin([938]).any()


def test_green_resting_across_a_whole_run():
    # Green begins in the first bin and ends in the third. The middle bin has no phase events at all.
    events = frame(at(600, 1, 2), at(899.9, 1, 7), at(1000, 82, 3), at(1799.9, 1, 7), at(2000, 8, 2), at(2699.9, 1, 7))
    big = process(events, config((2, 3)), carry='first')
    runs, _ = cut_runs(events, config((2, 3)), [T0 + pd.Timedelta(minutes=15 * i) for i in (1, 2)])
    same_rows(big['platoon_ratio'], runs['platoon_ratio'])
    row = runs['platoon_ratio'].iloc[0]
    assert row.Green_Seconds == 900 and row.Green_Ratio == 1 and row.Percent_AOG == 1


def test_a_run_short_of_the_end_of_its_bin_leaves_that_bin_for_the_next_run():
    # The first run stops 5 minutes into the second bin. All of the second bin comes from the second run.
    events = frame(at(10, 1, 2), at(100, 82, 3), at(899.9, 1, 7), at(1000, 82, 3), at(1190, 8, 2), at(1200, 1, 2), at(1300, 82, 3), at(1799.9, 1, 7))
    cut = T0 + pd.Timedelta(seconds=1250)
    run1 = process(events[events.TimeStamp < cut], config((2, 3)), carry='first')
    assert run1['arrival_on_green'].TimeStamp.tolist() == [T0]
    assert set(run1['unmatched_events'].EventId) >= {938, 939, 940}
    run2 = process(events[events.TimeStamp >= cut], config((2, 3)), carry=run1['unmatched_events'])
    assert run2['arrival_on_green'].TimeStamp.tolist() == [T0 + pd.Timedelta(minutes=15)]
    same_rows(process(events, config((2, 3)), carry='first')['platoon_ratio'],
              pd.concat([run1['platoon_ratio'], run2['platoon_ratio']]))


def test_run_with_no_unmatched_file_writes_all_it_has():
    events = frame(at(10, 1, 2), at(100, 82, 3), at(200, 8, 2))  # stops well short of the end of the bin
    assert process(events, config((2, 3)))['arrival_on_green'].Total_Actuations.tolist() == [1]
    assert len(process(events, config((2, 3)), carry='first')['arrival_on_green']) == 0


def test_empty_run_keeps_what_it_was_given():
    first = frame(at(10, 1, 2), at(897.9, 82, 3), at(899.9, 1, 7))
    carry = process(first, config((2, 3)), carry='first')['unmatched_events']
    empty = process(first.iloc[0:0], config((2, 3)), carry=carry)
    assert len(empty['arrival_on_green']) == 0
    kept = lambda df: df[df.EventId.between(938, 942)].sort_values(['EventId']).reset_index(drop=True)
    pd.testing.assert_frame_equal(kept(carry), kept(empty['unmatched_events']), check_dtype=False)


def test_two_lanes_at_the_same_moment_stay_two_vehicles():
    first = frame(at(10, 1, 2), at(897.9, 82, 3), at(897.9, 82, 4), at(899.9, 1, 7))
    second = frame(at(1000, 8, 2), at(1799.9, 1, 7))
    carry = process(first, config((2, 3), (2, 4)), carry='first')['unmatched_events']
    assert process(second, config((2, 3), (2, 4)), carry=carry)['arrival_on_green'].Total_Actuations.tolist() == [2]


def test_older_unmatched_file_still_loads():
    # A file written before these markers, as timeline left it
    old = pd.DataFrame({'TimeStamp': [T0], 'DeviceId': 'A', 'EventId': [1], 'Parameter': [2], 'IsValid': [True]})
    events = frame(at(100, 82, 3), at(200, 8, 2), at(899.9, 1, 7))
    assert process(events, config((2, 3)), carry=old)['arrival_on_green'].Percent_AOG.tolist() == [0.0]  # the old green is not read


@pytest.mark.parametrize('seed', [1, 2, 3])
def test_real_data_at_random_cuts_equals_one_big_run(seed):
    events, detectors = real_events(), real_config()
    big = process(events, detectors, carry='first')
    span = (events.TimeStamp.max() - events.TimeStamp.min()).total_seconds()
    rng = random.Random(seed)
    cuts = [events.TimeStamp.min() + pd.Timedelta(seconds=round(rng.uniform(0, span), 1)) for _ in range(9)]
    # None within a second of the end of a bin: such a run counts as having reached it
    cuts = [c for c in cuts if (c.ceil('15min') - c).total_seconds() > 1]
    runs, _ = cut_runs(events, detectors, cuts)
    same_rows(big['arrival_on_green'], runs['arrival_on_green'])
    same_rows(big['platoon_ratio'], runs['platoon_ratio'])


def test_real_data_with_old_config_at_bin_cuts_equals_one_big_run_and_a_run_with_no_file():
    events, detectors = real_events(), real_config()[['DeviceId', 'Phase', 'Parameter', 'Function']]
    start = events.TimeStamp.min().floor('15min')
    runs, carry = cut_runs(events, detectors, [start + pd.Timedelta(minutes=15 * i) for i in range(1, 24)])
    for big in (process(events, detectors, carry='first'), process(events, detectors)):
        same_rows(big['arrival_on_green'], runs['arrival_on_green'])
        same_rows(big['platoon_ratio'], runs['platoon_ratio'])
    # Only what lands in the next few seconds, and the phases green at the end, is left over
    assert len(carry[carry.EventId == 938]) < 100


def test_remove_incomplete_keeps_a_bin_written_after_its_run():
    # One quiet signal: the first run stops well short of the end of the bin, which has_data passed
    has_data = {'name': 'has_data', 'params': {'no_data_min': 15, 'min_data_points': 1}}
    events = frame(at(10, 1, 2), at(100, 82, 3), at(200, 8, 2), at(910, 1, 2), at(1000, 82, 3), at(1100, 8, 2), at(1799.9, 1, 7))
    aggregations = [has_data, AOG, PLATOON]
    big = process(events, config((2, 3)), aggregations, carry='first', remove_incomplete=True)
    runs, _ = cut_runs(events, config((2, 3)), [T0 + pd.Timedelta(minutes=15)], aggregations, remove_incomplete=True)
    assert len(runs['arrival_on_green']) == 2
    same_rows(big['arrival_on_green'], runs['arrival_on_green'])
    same_rows(big['platoon_ratio'], runs['platoon_ratio'])


def test_other_measures_are_the_same_with_the_carry_in_the_file():
    """timeline, yellow_red and the rest read raw_data_all, which must not hand them 938 to 942."""
    events, detectors = real_events(), real_config()
    has_data = {'name': 'has_data', 'params': {'no_data_min': 15, 'min_data_points': 1}}
    others = [has_data, {'name': 'timeline', 'params': {'min_duration': 0.2, 'cushion_time': 60}},
              {'name': 'yellow_red', 'params': {'latency_offset_seconds': 1.5, 'min_red_offset': -8}}]
    edges = [events.TimeStamp.min().floor('15min') + pd.Timedelta(hours=h) for h in range(0, 7)]
    with_carry, without_carry = None, None
    for start, end in zip(edges, edges[1:]):
        chunk = events[(events.TimeStamp >= start) & (events.TimeStamp < end)]
        a = process(chunk, detectors, others + TWO_TABLES, carry='first' if with_carry is None else with_carry)
        b = process(chunk, detectors, others, carry='first' if without_carry is None else without_carry)
        with_carry, without_carry = a['unmatched_events'], b['unmatched_events']
        for table in ('timeline', 'yellow_red'):
            pd.testing.assert_frame_equal(
                a[table].sort_values(list(a[table].columns)).reset_index(drop=True),
                b[table].sort_values(list(b[table].columns)).reset_index(drop=True))
        columns = list(without_carry.columns)
        pd.testing.assert_frame_equal(
            with_carry[~with_carry.EventId.between(938, 942) & ~with_carry.EventId.between(948, 950)].sort_values(columns).reset_index(drop=True),
            without_carry.sort_values(columns).reset_index(drop=True), check_dtype=False)

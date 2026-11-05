"""drop_fall_back_hour: events in 1:00-2:00 am on the first Sunday of November are dropped before anything is computed,
since the two passes through that hour when clocks fall back cannot be separated."""
import duckdb
import pandas as pd
import pytest
from src.atspm import SignalDataProcessor

SUNDAY = pd.Timestamp('2026-11-01')  # the first Sunday of November 2026
CONFIG = pd.DataFrame({'DeviceId': ['A', 'A'], 'Phase': [2, 2], 'Parameter': [3, 4], 'Function': ['Advance', 'Yellow_Red']})
HAS_DATA = {'name': 'has_data', 'params': {}}
ALL_AGGREGATIONS = [
    HAS_DATA,
    {'name': 'actuations', 'params': {}},
    {'name': 'arrival_on_green', 'params': {'latency_offset_seconds': 0}},
    {'name': 'platoon_ratio', 'params': {}},
    {'name': 'coordination', 'params': {}},
    {'name': 'ped', 'params': {}},
    {'name': 'split_failures', 'params': {'red_time': 5, 'red_occupancy_threshold': 0.80, 'green_occupancy_threshold': 0.80, 'by_approach': True, 'by_cycle': False}},
    {'name': 'splits', 'params': {}},
    {'name': 'terminations', 'params': {}},
    {'name': 'yellow_red', 'params': {'latency_offset_seconds': 1.5, 'min_red_offset': -8}},
    {'name': 'timeline', 'params': {'min_duration': 0.2, 'cushion_time': 60, 'maxtime': True}},
    {'name': 'ped_delay', 'params': {}},
    {'name': 'coordination_agg', 'params': {}},
    {'name': 'phase_wait', 'params': {'preempt_recovery_seconds': 120, 'assumed_cycle_length': 140, 'skip_multiplier': 1.5}},
]


def events_around(day, minutes=240, start_hour=0):
    """A signal cycling each minute from start_hour, with arrivals, a call, a ped press and a termination."""
    rows = []
    start = day + pd.Timedelta(hours=start_hour)
    for m in range(minutes):
        t = start + pd.Timedelta(minutes=m)
        for seconds, event, parameter in [(5.1, 1, 2), (40.2, 8, 2), (44.3, 10, 2), (12.4, 82, 3), (42.5, 82, 3), (20.5, 82, 4),
                                          (20.8, 90, 2), (30.9, 21, 2), (45.2, 4, 2), (46.2, 43, 2), (50.3, 44, 2)]:
            rows.append((t + pd.Timedelta(seconds=seconds), 'A', event, parameter))
    return pd.DataFrame(rows, columns=['TimeStamp', 'DeviceId', 'EventId', 'Parameter'])


def run(events, aggregations=None, **kwargs):
    with SignalDataProcessor(raw_data=events, detector_config=CONFIG, bin_size=15, verbose=0, controller_type='maxtime',
                             aggregations=aggregations or ALL_AGGREGATIONS, **kwargs) as p:
        p.load()
        p.aggregate()
        tables = [row[0] for row in p.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        return {t: p.conn.execute(f'SELECT * FROM "{t}"').df() for t in tables}


@pytest.mark.parametrize('day', ['2026-11-01', '2025-11-02', '2030-11-03', '2027-11-07'])
def test_the_hour_goes_on_the_first_sunday_of_any_year(day):
    day = pd.Timestamp(day)
    out = run(events_around(day), [HAS_DATA])
    hours = out['raw_data'].TimeStamp.dt.hour.unique()
    assert sorted(hours) == [0, 2, 3]
    assert out['raw_data'].TimeStamp.dt.date.unique().tolist() == [day.date()]


@pytest.mark.parametrize('day', [
    '2026-11-08',  # the second Sunday of November
    '2026-11-02',  # the Monday after
    '2026-10-25',  # a Sunday in October
    '2026-03-08',  # the spring change has no repeated hour
])
def test_other_days_keep_the_hour(day):
    out = run(events_around(pd.Timestamp(day)), [HAS_DATA])
    assert sorted(out['raw_data'].TimeStamp.dt.hour.unique()) == [0, 1, 2, 3]


def test_the_edges_of_the_hour():
    times = ['00:59:59.900', '01:00:00', '01:30:00', '01:59:59.900', '02:00:00']
    events = pd.DataFrame({'TimeStamp': [pd.Timestamp(f'2026-11-01 {t}') for t in times], 'DeviceId': 'A', 'EventId': 82, 'Parameter': 3})
    kept = sorted(run(events, [HAS_DATA])['raw_data'].TimeStamp.dt.strftime('%H:%M:%S.%f'))
    assert kept == ['00:59:59.900000', '02:00:00.000000']


def test_turned_off_keeps_the_hour():
    out = run(events_around(SUNDAY), [HAS_DATA], drop_fall_back_hour=False)
    assert sorted(out['raw_data'].TimeStamp.dt.hour.unique()) == [0, 1, 2, 3]
    assert len(out['has_data']) == 16


def test_has_data_has_no_row_in_the_hour():
    has_data = run(events_around(SUNDAY), [HAS_DATA])['has_data'].TimeStamp
    assert has_data.dt.hour.value_counts().sort_index().to_dict() == {0: 4, 2: 4, 3: 4}


@pytest.mark.parametrize('source', ['frame', 'parquet', 'csv'])
def test_every_kind_of_source(source, tmp_path):
    events = events_around(SUNDAY)
    if source == 'parquet':
        events.to_parquet(tmp_path / 'e.parquet')
        events = str(tmp_path / 'e.parquet')
    elif source == 'csv':
        events.to_csv(tmp_path / 'e.csv', index=False)
        events = str(tmp_path / 'e.csv')
    assert sorted(run(events, [HAS_DATA])['raw_data'].TimeStamp.dt.hour.unique()) == [0, 2, 3]


def test_source_with_other_column_names():
    events = events_around(SUNDAY).rename(columns={'TimeStamp': 'timestamp', 'DeviceId': 'signal', 'EventId': 'event_code', 'Parameter': 'event_param'})
    assert sorted(run(events, [HAS_DATA])['raw_data'].TimeStamp.dt.hour.unique()) == [0, 2, 3]


def test_no_aggregation_has_a_bin_or_event_in_the_hour():
    """Against the same run on the same events with the hour kept, which shows each table does have rows there."""
    events = events_around(SUNDAY)
    dropped, kept = run(events), run(events, drop_fall_back_hour=False)
    in_hour = lambda stamps: ((stamps >= SUNDAY + pd.Timedelta(hours=1)) & (stamps < SUNDAY + pd.Timedelta(hours=2)))
    checked = []
    for table, frame in dropped.items():
        for column in ('TimeStamp', 'Raw_TimeStamp', 'StartTime', 'EndTime'):
            if column in frame.columns and in_hour(kept[table][column]).any():
                rows = frame[in_hour(frame[column])]
                if table == 'ped':
                    # A known input keeps its zero rows, as in any outage, and join has_data leaves them out
                    assert (rows[['PedServices', 'PedActuation', 'Unique_Actuations']] == 0).all(axis=None)
                elif table in ('coordination_agg', 'actuations') and column == 'TimeStamp':
                    assert frame[in_hour(frame.TimeStamp)].select_dtypes('number').fillna(0).eq(0).all(axis=None) or rows.empty
                else:
                    # An arrival just before the hour can land just inside it, nothing else can
                    if table in ('arrival_on_green', 'platoon_ratio'):
                        assert (rows.TimeStamp == SUNDAY + pd.Timedelta(hours=1)).all()
                    else:
                        assert rows.empty, (table, column)
                checked.append((table, column))
    assert {'has_data', 'terminations', 'timeline', 'ped', 'arrival_on_green', 'raw_data'} <= {t for t, _ in checked}


def test_ped_without_volumes_has_no_row_in_the_hour():
    """With its quiet rows left out, ped writes nothing in the hour. With the hour kept it has rows there."""
    events = events_around(SUNDAY)
    aggregations = [HAS_DATA, {'name': 'ped', 'params': {'return_volumes': False}}]
    in_hour = lambda stamps: ((stamps >= SUNDAY + pd.Timedelta(hours=1)) & (stamps < SUNDAY + pd.Timedelta(hours=2)))
    assert in_hour(run(events, aggregations, drop_fall_back_hour=False)['ped'].TimeStamp).any()
    assert not in_hour(run(events, aggregations)['ped'].TimeStamp).any()


def test_an_interval_across_the_hour_is_not_valid():
    """The hour reads as a data gap, so what timeline saw before it does not join to what it saw after."""
    timeline = run(events_around(SUNDAY), [HAS_DATA, {'name': 'timeline', 'params': {'min_duration': 0.2, 'cushion_time': 60}}])['timeline']
    across = timeline[(timeline.StartTime < SUNDAY + pd.Timedelta(hours=1)) & (timeline.EndTime >= SUNDAY + pd.Timedelta(hours=2))]
    assert len(across) > 0
    assert not across.IsValid.any()


@pytest.mark.parametrize('return_volumes', [True, False])
def test_incremental_runs_through_the_hour_equal_one_big_run(return_volumes):
    """15-minute runs, four of which have no events left. They must not fail or lose anything carried."""
    events = events_around(SUNDAY).sort_values('TimeStamp')
    aggregations = [HAS_DATA, {'name': 'ped', 'params': {'return_volumes': return_volumes}},
                    {'name': 'arrival_on_green', 'params': {'latency_offset_seconds': 0}}, {'name': 'platoon_ratio', 'params': {}},
                    {'name': 'timeline', 'params': {'min_duration': 0.2, 'cushion_time': 60}}]
    settings = lambda carry: {'df_or_path': carry, 'max_days_old': 14}
    big = run(events, aggregations, unmatched_event_settings=settings(''))
    carry, tables = '', {}
    edges = pd.date_range(SUNDAY, SUNDAY + pd.Timedelta(hours=4), freq='15min')
    for start, end in zip(edges[:-1], edges[1:]):
        out = run(events[(events.TimeStamp >= start) & (events.TimeStamp < end)], aggregations, unmatched_event_settings=settings(carry))
        carry = out['unmatched_events']
        for table in ('ped', 'arrival_on_green', 'platoon_ratio'):
            tables.setdefault(table, []).append(out[table])
    keys = ['TimeStamp', 'DeviceId', 'Phase']
    for table, frames in tables.items():
        cut = pd.concat(frames, ignore_index=True)
        assert not cut.duplicated(keys).any(), table
        whole = big[table]
        pd.testing.assert_frame_equal(cut.sort_values(keys).reset_index(drop=True), whole.sort_values(keys).reset_index(drop=True),
                                      check_dtype=False, check_exact=False, rtol=1e-5)


def test_to_sql_is_left_alone():
    """to_sql reads your own raw_data table, so the setting has nothing to act on."""
    sql = SignalDataProcessor(raw_data=events_around(SUNDAY), bin_size=15, verbose=0, to_sql=True, aggregations=[HAS_DATA]).run()['has_data']
    assert 'dayofweek' not in sql

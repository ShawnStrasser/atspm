"""Events stamped in the same tenth of a second sort the same way whatever order they arrive in, and the way the
controller would have done them. Each test runs its events in both input orders."""
import pandas as pd
import pytest

from src.atspm import SignalDataProcessor

T0 = pd.Timestamp('2026-09-01 08:00:00')
NO_DETECTORS = pd.DataFrame(columns=['DeviceId', 'Phase', 'Parameter', 'Function'])
TIMELINE = {'name': 'timeline', 'params': {'min_duration': 0, 'cushion_time': 60, 'max_event_gap_seconds': None}}
HAS_DATA = {'name': 'has_data', 'params': {'no_data_min': 15, 'min_data_points': 1}}


def events(rows, order):
    """rows: (seconds after T0, EventId, Parameter)."""
    df = pd.DataFrame(rows, columns=['s', 'EventId', 'Parameter'])
    df['TimeStamp'] = T0 + pd.to_timedelta(df.pop('s'), unit='s')
    df['DeviceId'] = 'A'
    df = df[['TimeStamp', 'DeviceId', 'EventId', 'Parameter']]
    return df if order == 'as_listed' else df.iloc[::-1].reset_index(drop=True)


def run(rows, order, aggregations, table, config=NO_DETECTORS, **kwargs):
    with SignalDataProcessor(raw_data=events(rows, order), detector_config=config, bin_size=15, verbose=0,
                             aggregations=[HAS_DATA] + aggregations, **kwargs) as p:
        p.load()
        p.aggregate()
        return p.conn.query(f'SELECT * FROM {table}').df()


def intervals(df, event_class):
    df = df[df.EventClass == event_class].sort_values('StartTime')
    return [((a - T0).total_seconds(), (b - T0).total_seconds()) for a, b in zip(df.StartTime, df.EndTime)]


@pytest.fixture(params=['as_listed', 'reversed'])
def order(request):
    return request.param


def test_press_in_the_tenth_of_a_walk_is_not_a_delay(order):
    # 21 sorts before 90, so the press is one made during the walk, not one the walk answered in no time
    rows = [(1, 21, 2), (8, 22, 2), (60, 21, 2), (60, 90, 2), (67, 22, 2)]
    assert intervals(run(rows, order, [TIMELINE], 'timeline'), 'Ped Delay') == []


def test_press_in_the_tenth_of_a_walk_leaves_the_press_before_it(order):
    rows = [(1, 21, 2), (8, 22, 2), (40, 90, 2), (60, 21, 2), (60, 90, 2), (67, 22, 2)]
    assert intervals(run(rows, order, [TIMELINE], 'timeline'), 'Ped Delay') == [(40, 60)]


def test_press_in_the_tenth_of_flashing_dont_walk_waits_for_the_next_walk(order):
    # 22 sorts before 90, so this press starts a wait instead of being served by the 22 beside it
    rows = [(1, 90, 2), (2, 21, 2), (8, 22, 2), (8, 90, 2), (60, 21, 2), (67, 22, 2)]
    timeline = run(rows, order, [TIMELINE], 'timeline')
    assert intervals(timeline, 'Ped Delay') == [(1, 2), (8, 60)]


@pytest.mark.parametrize('start, end, event_class', [(43, 44, 'Phase Call'), (21, 23, 'Ped Service'), (41, 42, 'Phase Hold')])
def test_start_and_end_in_one_tenth_are_a_momentary_interval(order, start, end, event_class):
    rows = [(10, start, 2), (10, end, 2)]
    timeline = run(rows, order, [TIMELINE], 'timeline')
    assert intervals(timeline, event_class) == [(10, 10)]
    assert timeline[timeline.EventClass == event_class].IsValid.all()


def test_restore_and_fault_in_one_tenth_leave_the_fault_open(order):
    rows = [(5, 87, 3), (10, 83, 3), (10, 87, 3), (20, 83, 3)]
    assert intervals(run(rows, order, [TIMELINE], 'timeline'), 'Stuck On') == [(5, 10), (10, 20)]


def test_toggle_off_and_on_in_one_tenth_leave_it_on(order):
    rows = [(5, 180, 1), (10, 180, 0), (10, 180, 1), (20, 180, 0)]
    assert intervals(run(rows, order, [TIMELINE], 'timeline'), 'Stop Time Input') == [(5, 10), (10, 20)]


def test_windows_starting_together_use_the_latest_end(order):
    # A silence and a clock update window both start at 10. The yellow starts when the silence ends, inside the clock window
    rows = [(10, 1, 7), (15, 181, 0), (15, 8, 2), (15.5, 9, 2)]
    params = dict(TIMELINE['params'], max_event_gap_seconds=1)
    timeline = run(rows, order, [{'name': 'timeline', 'params': params}], 'timeline')
    assert not timeline[timeline.EventClass == 'Yellow'].IsValid.any()


def test_two_cycle_lengths_at_one_time_use_the_longer(order):
    # The wait is 100 seconds: a skip for a 60 second cycle, not for a 120 second one
    rows = [(0, 316, 60), (0, 316, 120), (10, 43, 2), (110, 1, 2), (111, 44, 2), (120, 7, 2)]
    wait = {'name': 'phase_wait', 'params': {'preempt_recovery_seconds': 120, 'assumed_cycle_length': 150, 'skip_multiplier': 1.5, 'tsp_skip_multiplier': 2.0}}
    df = run(rows, order, [TIMELINE, wait], 'phase_wait', controller_type='maxtime')
    assert len(df) == 1 and df.TotalSkips.iloc[0] == 0


SPLIT_FAILURES = {'name': 'split_failures', 'params': {'red_time': 5, 'red_occupancy_threshold': 0.8, 'green_occupancy_threshold': 0.7, 'by_approach': True, 'by_cycle': True}}


def test_detector_on_two_phases_is_not_two_events_in_a_row(order):
    config = pd.DataFrame({'DeviceId': 'A', 'Phase': [2, 4], 'Parameter': 5, 'Function': 'Presence'})
    rows = [(s, e, p) for p in (2, 4) for s, e in [(2, 1), (22, 8), (26, 10), (60, 1)]] + [(0, 82, 5), (1, 81, 5), (3, 82, 5), (13, 81, 5)]
    df = run(rows, order, [SPLIT_FAILURES], 'split_failures', config=config)
    assert sorted(df.Phase) == [2, 4]
    assert df.Green_Occupancy.tolist() == pytest.approx([0.5, 0.5])


def test_saved_detector_state_is_taken_by_the_same_sort(order, tmp_path):
    # The detector goes off in the tenth the green starts. The cycle's first row is the green, with the detector still
    # on, and its last is the off event, with the detector off, and both are saved for the next run.
    config = pd.DataFrame({'DeviceId': ['A'], 'Phase': [2], 'Parameter': [5], 'Function': ['Presence']})
    rows = [(0, 82, 5), (10, 1, 2), (10, 81, 5)]
    settings = {'df_or_path': str(tmp_path / 'none.parquet'), 'split_fail_df_or_path': str(tmp_path / 'none_sf.parquet'), 'max_days_old': 14}
    df = run(rows, order, [SPLIT_FAILURES], 'sf_unmatched', config=config, unmatched_event_settings=settings)
    saved = df[df.TimeStamp == T0 + pd.Timedelta(seconds=10, milliseconds=-1)]
    assert sorted(saved.EventId) == [81, 82]

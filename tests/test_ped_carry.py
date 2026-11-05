"""ped: the default time lapse, the first press, and each input's last press carried between incremental runs
(synthetic EventId 937 in unmatched_events). A run with an unmatched file writes a bin only once it reaches the end of
it. Each behaviour runs with quiet rows left out (return_volumes False) and with volumes, the two ways ped writes. The
volumes themselves are tested in test_ped_volumes.py."""
import jinja2
import numpy as np
import pandas as pd
import pytest
import duckdb
from src.atspm import SignalDataProcessor

REAL_EVENTS = 'tests/ped_events_test_data.parquet'  # real ped events (21, 67, 90) of 20 signals, Sep 18-21 2026
WITH_VOLUMES = [False, True]
T0 = pd.Timestamp('2026-09-01 08:00:00')
KEYS = ['TimeStamp', 'DeviceId', 'Phase']


def presses(*seconds, device='A', parameter=2, event=90):
    """Events at the given number of seconds after T0."""
    return pd.DataFrame({
        'TimeStamp': [T0 + pd.Timedelta(seconds=s) for s in seconds],
        'DeviceId': device,
        'EventId': event,
        'Parameter': parameter,
    })


def aggregation(volumes=False, **params):
    return {'name': 'ped', 'params': {'return_volumes': volumes, **params}}


def process(events, aggregations, carry=None, settings=None, **kwargs):
    """One run. Returns {table: DataFrame} for the output tables and unmatched_events.

    carry: None for no unmatched settings at all, 'first' for a first incremental run, or the previous carry.
    """
    if carry is not None:
        settings = {'df_or_path': '' if isinstance(carry, str) else carry, 'max_days_old': kwargs.pop('max_days_old', 14)}
    with SignalDataProcessor(raw_data=events, bin_size=15, verbose=0, unmatched_event_settings=settings, aggregations=aggregations, **kwargs) as p:
        p.load()
        p.aggregate()
        tables = [row[0] for row in p.conn.execute("SELECT table_name FROM information_schema.tables").fetchall()]
        return {t: p.conn.execute(f"SELECT * FROM {t}").df() for t in tables if t not in ('raw_data', 'raw_data_all', 'unmatched_previous')}


def reach_bin_end(events):
    """The events plus another signal's event in the last second of their last bin, so a run reaches the end of it."""
    last = events.TimeStamp.max().floor('15min') + pd.Timedelta(minutes=15, milliseconds=-100)
    device = 999999 if pd.api.types.is_numeric_dtype(events.DeviceId) else 'Z'
    marker = pd.DataFrame({'TimeStamp': [last], 'DeviceId': device, 'EventId': 82, 'Parameter': 1})
    return pd.concat([events, marker], ignore_index=True)


def incremental(events, cuts, aggregations, **kwargs):
    """Runs the events in pieces, carrying unmatched_events. Returns the outputs of every run and the last carry.
    The last run reaches the end of its bin, to write everything, as one big run does."""
    edges = [events.TimeStamp.min()] + sorted(cuts) + [events.TimeStamp.max() + pd.Timedelta(seconds=1)]
    carry, outputs = 'first', []
    for i, (start, end) in enumerate(zip(edges[:-1], edges[1:])):
        chunk = events[(events.TimeStamp >= start) & (events.TimeStamp < end)]
        if i == len(edges) - 2:
            chunk = reach_bin_end(chunk)
        out = process(chunk, aggregations, carry=carry, **kwargs)
        carry = out['unmatched_events']
        outputs.append(out)
    return outputs, carry


def rows(frames):
    """The rows of all runs, with no bin written twice."""
    frame = pd.concat(frames, ignore_index=True)
    assert not frame.duplicated(KEYS).any(), 'a bin was written twice'
    return frame.sort_values(KEYS).reset_index(drop=True)


def unique_total(frames):
    return sum(int(f['Unique_Actuations'].sum()) for f in frames)


@pytest.fixture(scope='module')
def real_events():
    return duckdb.sql(f"SELECT * FROM '{REAL_EVENTS}'").df()


#########################################
### Batch and incremental agree
#########################################

@pytest.mark.parametrize('volumes', WITH_VOLUMES)
@pytest.mark.parametrize('seed', [1, 2, 3])
def test_real_data_batch_equals_incremental_at_random_cuts(real_events, volumes, seed):
    rng = np.random.default_rng(seed)
    start, end = real_events.TimeStamp.min(), real_events.TimeStamp.max()
    seconds = rng.integers(1, int((end - start).total_seconds()), 40)
    cuts = sorted({start + pd.Timedelta(seconds=int(s), milliseconds=int(rng.integers(0, 10)) * 100) for s in seconds})
    # None within a second of the end of a bin: such a run counts as having reached it
    cuts = [c for c in cuts if (c.ceil('15min') - c).total_seconds() > 1]

    batch = rows([process(real_events, [aggregation(volumes)])['ped']])
    outputs, _ = incremental(real_events, cuts, [aggregation(volumes)])

    assert batch['Unique_Actuations'].sum() > 1000
    pd.testing.assert_frame_equal(rows([o['ped'] for o in outputs]), batch)


@pytest.mark.parametrize('volumes', WITH_VOLUMES)
def test_real_data_batch_equals_incremental_at_three_hour_windows(real_events, volumes):
    start = real_events.TimeStamp.min().floor('3h')
    cuts = list(pd.date_range(start + pd.Timedelta(hours=3), real_events.TimeStamp.max(), freq='3h'))
    batch = rows([process(real_events, [aggregation(volumes)])['ped']])
    outputs, _ = incremental(real_events, cuts, [aggregation(volumes)])
    pd.testing.assert_frame_equal(rows([o['ped'] for o in outputs]), batch)


def test_real_data_is_not_all_kept(real_events):
    """Guards the tests above: the count must actually drop repeat presses."""
    batch = process(real_events, [aggregation()])['ped']
    assert 0.2 < batch['Unique_Actuations'].sum() / batch['PedActuation'].sum() < 0.8


#########################################
### Edge cases, batch and across a cut
#########################################

@pytest.mark.parametrize('volumes', WITH_VOLUMES)
class TestCounting:
    def test_first_press_ever_is_counted(self, volumes):
        assert unique_total([process(presses(100), [aggregation(volumes)])['ped']]) == 1

    def test_first_press_of_first_incremental_run_is_counted(self, volumes):
        assert unique_total([process(reach_bin_end(presses(100)), [aggregation(volumes)], carry='first')['ped']]) == 1

    def test_default_is_fifteen_seconds(self, volumes):
        events = presses(0, 10, 40)  # 10 s repeat dropped, 40 s counted
        assert unique_total([process(events, [aggregation(volumes)])['ped']]) == 2

    @pytest.mark.parametrize('seconds, expected', [(5, 3), (15, 2), (30, 1), (2.5, 3), (0, 3)])
    def test_setting_is_used(self, volumes, seconds, expected):
        events = presses(0, 10, 30)
        out = process(events, [aggregation(volumes, seconds_between_actuations=seconds)])['ped']
        assert unique_total([out]) == expected

    def test_press_at_the_limit_is_not_counted(self, volumes):
        assert unique_total([process(presses(0, 15), [aggregation(volumes)])['ped']]) == 1

    def test_press_just_after_the_limit_is_counted(self, volumes):
        assert unique_total([process(presses(0, 15.1), [aggregation(volumes)])['ped']]) == 2

    def test_press_at_the_limit_across_a_cut_is_not_counted(self, volumes):
        outputs, _ = incremental(presses(0, 15), [T0 + pd.Timedelta(seconds=5)], [aggregation(volumes)])
        assert unique_total([o['ped'] for o in outputs]) == 1

    def test_press_just_after_the_limit_across_a_cut_is_counted(self, volumes):
        outputs, _ = incremental(presses(0, 15.1), [T0 + pd.Timedelta(seconds=5)], [aggregation(volumes)])
        assert unique_total([o['ped'] for o in outputs]) == 2

    def test_run_of_presses_crossing_a_cut_counts_once(self, volumes):
        """Each press is within the limit of the one before, so only the first counts, wherever the cut falls."""
        events = presses(*range(0, 120, 10))
        for cut in (5, 35, 95):
            outputs, _ = incremental(events, [T0 + pd.Timedelta(seconds=cut)], [aggregation(volumes)])
            assert unique_total([o['ped'] for o in outputs]) == 1

    def test_run_crossing_several_runs_with_empty_run_between(self, volumes):
        out1 = process(presses(0, 10), [aggregation(volumes)], carry='first')
        out2 = process(presses(0, 10).iloc[:0], [aggregation(volumes)], carry=out1['unmatched_events'])
        out3 = process(reach_bin_end(presses(20)), [aggregation(volumes)], carry=out2['unmatched_events'])
        assert unique_total([out1['ped'], out2['ped'], out3['ped']]) == 1

    def test_carry_is_per_input_and_device(self, volumes):
        first = pd.concat([presses(0), presses(0, parameter=4), presses(0, device='B')])
        second = pd.concat([presses(5), presses(30, parameter=4), presses(5, device='C')])
        out1 = process(first, [aggregation(volumes)], carry='first')
        out2 = process(reach_bin_end(second), [aggregation(volumes)], carry=out1['unmatched_events'])
        # A input 2's second press is a repeat, A input 4's is past the limit, C has no earlier press
        counted = out2['ped'].query('Unique_Actuations > 0').sort_values(['DeviceId', 'Phase'])
        assert list(zip(counted.DeviceId, counted.Phase, counted.Unique_Actuations)) == [('A', 2, 1), ('A', 4, 2), ('B', 2, 1), ('C', 2, 1)]

    def test_carried_press_is_not_counted_again(self, volumes):
        out1 = process(reach_bin_end(presses(0)), [aggregation(volumes)], carry='first')
        assert unique_total([out1['ped']]) == 1
        out2 = process(reach_bin_end(presses(1000)), [aggregation(volumes)], carry=out1['unmatched_events'])
        assert unique_total([out2['ped']]) == 1
        bins = out2['ped'].query('Unique_Actuations > 0').TimeStamp.tolist()
        assert bins == [(T0 + pd.Timedelta(seconds=1000)).floor('15min')]

    def test_same_press_seen_again_is_not_counted_twice(self, volumes):
        """A press carried and also in the next run, from a window that overlaps the one before."""
        out1 = process(presses(0, 100), [aggregation(volumes)], carry='first')
        out2 = process(reach_bin_end(presses(100, 300)), [aggregation(volumes)], carry=out1['unmatched_events'])
        assert unique_total([out2['ped']]) == 3

    def test_press_older_than_the_carried_one_is_judged_on_its_own(self, volumes):
        """A late press has nothing known before it, and its bin waits to be written like the rest of the input's.
        The next one is still within the limit of the carried press."""
        out1 = process(presses(1000), [aggregation(volumes)], carry='first')
        out2 = process(reach_bin_end(presses(950, 1005)), [aggregation(volumes)], carry=out1['unmatched_events'])
        assert unique_total([out2['ped']]) == 2

    def test_press_older_than_a_bin_already_written_is_not_written(self, volumes):
        """A row is never written twice, so a late press in a bin already written has nowhere to go."""
        out1 = process(reach_bin_end(presses(1000)), [aggregation(volumes)], carry='first')
        out2 = process(reach_bin_end(presses(500, 2000)), [aggregation(volumes)], carry=out1['unmatched_events'])
        assert out2['ped'].query('Unique_Actuations > 0').TimeStamp.tolist() == [(T0 + pd.Timedelta(seconds=2000)).floor('15min')]


#########################################
### The carry itself
#########################################

def carried(carry):
    return carry[carry.EventId == 937].sort_values(['DeviceId', 'Parameter']).reset_index(drop=True)


def test_carry_is_one_row_per_input_with_its_last_press():
    events = pd.concat([presses(0, 5, 50), presses(7, parameter=4), presses(9, device='B')])
    row = carried(process(events, [aggregation()], carry='first')['unmatched_events'])
    assert list(zip(row.DeviceId, row.Parameter)) == [('A', 2), ('A', 4), ('B', 2)]
    assert row.TimeStamp.tolist() == [T0 + pd.Timedelta(seconds=s) for s in (50, 7, 9)]
    assert row.IsValid.all()


@pytest.mark.parametrize('volumes', WITH_VOLUMES)
def test_one_row_per_input_across_runs_and_a_repeat_is_not_counted(volumes):
    aggs = [aggregation(volumes)]
    out1 = process(presses(0), aggs, carry='first')
    assert len(carried(out1['unmatched_events'])) == 1
    out2 = process(presses(5), aggs, carry=out1['unmatched_events'])
    assert len(carried(out2['unmatched_events'])) == 1
    assert unique_total([out2['ped']]) == 0


def test_input_with_no_press_keeps_its_row():
    out1 = process(presses(0), [aggregation()], carry='first')
    out2 = process(presses(10, device='B'), [aggregation()], carry=out1['unmatched_events'])
    assert list(carried(out2['unmatched_events']).DeviceId) == ['A', 'B']
    assert carried(out2['unmatched_events']).TimeStamp[0] == T0


def test_row_older_than_max_days_old_is_dropped():
    out1 = process(presses(0), [aggregation()], carry='first')
    later = presses(0, device='B')
    later['TimeStamp'] += pd.Timedelta(days=20)
    out2 = process(later, [aggregation()], carry=out1['unmatched_events'])
    assert list(carried(out2['unmatched_events']).DeviceId) == ['B']


def test_empty_run_keeps_the_carry():
    out1 = process(presses(0), [aggregation()], carry='first')
    out2 = process(presses(0).iloc[:0], [aggregation()], carry=out1['unmatched_events'])
    pd.testing.assert_frame_equal(carried(out2['unmatched_events']), carried(out1['unmatched_events']), check_dtype=False)


def test_carry_saved_to_a_file_without_timeline(tmp_path):
    path = str(tmp_path / 'unmatched.parquet')
    counts = []
    for i, events in enumerate((presses(0), presses(5), reach_bin_end(presses(100)))):
        SignalDataProcessor(
            raw_data=events, bin_size=15, verbose=0, output_dir=str(tmp_path), output_to_separate_folders=False,
            output_format='parquet', output_file_prefix=f'{i}_',
            unmatched_event_settings={'df_or_path': path, 'max_days_old': 14, 'first_run': True},
            aggregations=[aggregation()],
        ).run()
        counts.append(int(pd.read_parquet(tmp_path / f'{i}_ped.parquet')['Unique_Actuations'].sum()))
        assert (pd.read_parquet(path).EventId == 937).sum() == 1
    assert counts == [0, 0, 2]  # the bin is written by the run that reaches its end, with all of its counts


def test_carry_with_timeline_keeps_timeline_rows():
    events = pd.concat([presses(0), presses(1, event=1), presses(5)])
    aggs = [
        aggregation(),  # listed before timeline, which creates unmatched_events
        {'name': 'has_data', 'params': {'no_data_min': 15, 'min_data_points': 1}},
        {'name': 'timeline', 'params': {'min_duration': 0, 'cushion_time': 0}},
    ]
    carry = process(events, aggs, carry='first')['unmatched_events']
    assert (carry.EventId == 937).sum() == 1
    assert ((carry.EventId == 1) & (carry.Parameter == 2)).sum() == 1
    assert (carry.EventId == 936).sum() == 1


#########################################
### Older carry files
#########################################

@pytest.mark.parametrize('volumes', WITH_VOLUMES)
@pytest.mark.parametrize('has_isvalid', [True, False])
def test_old_carry_file_loads_and_counts_first_press(tmp_path, volumes, has_isvalid):
    """A file written before EventId 937 existed: other carried rows, no last press."""
    old = pd.DataFrame({
        'TimeStamp': [T0 - pd.Timedelta(seconds=5), T0 - pd.Timedelta(seconds=5)],
        'DeviceId': ['A', 'A'],
        'EventId': [1, 936],
        'Parameter': [2, 0],
        'IsValid': [True, True],
    })
    if not has_isvalid:
        old = old.drop(columns='IsValid')
    path = tmp_path / 'old.parquet'
    old.to_parquet(path)
    out = process(reach_bin_end(presses(0, 5, 40)), [aggregation(volumes)], settings={'df_or_path': str(path), 'max_days_old': 14, 'first_run': True})
    assert unique_total([out['ped']]) == 2
    assert len(carried(out['unmatched_events'])) == 1


#########################################
### Settings that must keep working
#########################################

def test_no_unmatched_settings_saves_nothing():
    assert 'unmatched_events' not in process(presses(0), [aggregation()])


def test_known_detectors_alone_adds_no_unmatched_events():
    """known_detectors also makes a run incremental. Without timeline there was never an unmatched_events output."""
    aggs = [aggregation(), {'name': 'actuations', 'params': {'known_detectors_df_or_path': None}}]
    out = process(presses(0, 100), aggs)
    assert 'unmatched_events' not in out
    assert unique_total([out['ped']]) == 2


def test_known_detectors_with_timeline_still_carries():
    """How ODOT's first window runs: no unmatched settings yet, but the next run reads unmatched_events."""
    aggs = [
        aggregation(),
        {'name': 'has_data', 'params': {'no_data_min': 15, 'min_data_points': 1}},
        {'name': 'timeline', 'params': {'min_duration': 0, 'cushion_time': 0}},
        {'name': 'actuations', 'params': {'known_detectors_df_or_path': None}},
    ]
    out1 = process(presses(0), aggs)
    assert len(carried(out1['unmatched_events'])) == 1
    aggs[3] = {'name': 'actuations', 'params': {'known_detectors_df_or_path': out1['known_detectors']}}
    out2 = process(presses(5), aggs, carry=out1['unmatched_events'])
    assert unique_total([out2['ped']]) == 0


def test_split_failure_file_alone_works(tmp_path):
    """A main unmatched file was not asked for, so there is nothing to read the last press from."""
    settings = {'split_fail_df_or_path': str(tmp_path / 'sf.parquet'), 'max_days_old': 14, 'first_run': True}
    out = process(presses(0, 100), [aggregation()], settings=settings)
    assert unique_total([out['ped']]) == 2
    assert 'unmatched_events' not in out


@pytest.mark.parametrize('volumes', WITH_VOLUMES)
def test_to_sql_renders_with_default(volumes):
    sql = SignalDataProcessor(raw_data=presses(0), bin_size=15, verbose=0, to_sql=True, aggregations=[aggregation(volumes)]).run()['ped']
    assert "q.Press) > 15000" in sql
    assert 'unmatched_previous' not in sql


@pytest.mark.parametrize('old', ['full_ped', 'unique_ped', 'not_a_table'])
def test_a_name_that_is_no_table_fails_the_same_way(old):
    """The two old names were removed in 3.0.0 and are now names like any other unknown one."""
    with pytest.raises(jinja2.TemplateNotFound):
        SignalDataProcessor(raw_data=presses(0), bin_size=15, verbose=0, aggregations=[{'name': old, 'params': {}}]).run()


#########################################
### Other aggregations do not see the new rows
#########################################

def test_other_aggregations_are_unchanged_by_the_carry():
    """Runs the same pieces with and without the ped aggregations."""
    data = duckdb.sql("SELECT * FROM 'tests/hires_test_data.parquet'").df()
    config = duckdb.sql("SELECT * FROM 'tests/configs_test_data.parquet'").df()
    others = [
        {'name': 'has_data', 'params': {'no_data_min': 5, 'min_data_points': 3}},
        {'name': 'arrival_on_green', 'params': {'latency_offset_seconds': 0}},
        {'name': 'yellow_red', 'params': {'latency_offset_seconds': 1.5, 'min_red_offset': -8}},
        {'name': 'timeline', 'params': {'min_duration': 0.2, 'cushion_time': 60, 'maxtime': True}},
        {'name': 'phase_wait', 'params': {'preempt_recovery_seconds': 120, 'assumed_cycle_length': 140, 'skip_multiplier': 1.5}},
        {'name': 'coordination_agg', 'params': {}},
    ]
    cuts = list(pd.date_range(data.TimeStamp.min().ceil('30min'), data.TimeStamp.max(), freq='30min'))
    kwargs = {'detector_config': config, 'controller_type': 'maxtime'}
    plain, _ = incremental(data, cuts, others, **kwargs)
    with_ped, carry = incremental(data, cuts, others + [aggregation(volumes=True)], **kwargs)
    assert (carry.EventId == 937).sum() > 0
    for run_plain, run_ped in zip(plain, with_ped):
        for table, frame in run_plain.items():
            other = run_ped[table]
            if table == 'unmatched_events':
                other = other[~other.EventId.between(943, 947) & (other.EventId != 937)]
            sort = list(frame.columns)
            pd.testing.assert_frame_equal(
                frame.sort_values(sort).reset_index(drop=True),
                other.sort_values(sort).reset_index(drop=True),
                check_dtype=False,
            )

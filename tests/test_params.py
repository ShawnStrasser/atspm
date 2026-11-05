"""What a run does with its settings: names are checked, the caller's dicts are left alone, and what is listed is written."""
import os
import re

import duckdb
import jinja2
import pandas as pd
import pytest
from jinja2 import meta

from src.atspm import SignalDataProcessor
from src.atspm.signal_data_processor import AGGREGATION_PARAMS

T0 = pd.Timestamp('2026-10-27 08:00:00')
EMPTY_CONFIG = pd.DataFrame(columns=['DeviceId', 'Phase', 'Parameter', 'Function'])
HAS_DATA = {'name': 'has_data', 'params': {'no_data_min': 15, 'min_data_points': 1}}
TIMELINE = {'name': 'timeline', 'params': {'min_duration': 0.1, 'cushion_time': 60, 'maxtime': True}}


def events(rows):
    """(seconds after T0, device, event, parameter) as raw_data."""
    return pd.DataFrame([{'TimeStamp': T0 + pd.Timedelta(seconds=s), 'DeviceId': d, 'EventId': e, 'Parameter': p}
                         for s, d, e, p in rows])


def run(raw, aggregations, tables=(), config=EMPTY_CONFIG, **settings):
    with SignalDataProcessor(raw_data=raw, detector_config=config, bin_size=15, verbose=0, aggregations=aggregations, **settings) as p:
        p.load()
        p.aggregate()
        return {t: p.conn.sql(f'SELECT * FROM {t}').df() for t in tables}


@pytest.mark.parametrize('settings', [
    {'memory_limit': '4GB'},
    {'unmatched_events_settings': {'df_or_path': 'x'}},
    {'unmatched_event_settings': {'df_or_paths': 'x', 'max_days_old': 2}},
    {'aggregations': [HAS_DATA, {'name': 'ped', 'params': {'seconds_between_actuation': 60}}]},
    {'aggregations': [{'name': 'actuations', 'params': {'fill_in_mising': True}}]},
    {'aggregations': [{'name': 'detector_health', 'params': {'stor': 'x'}}]},
])
def test_misspelled_name_raises_at_start(settings):
    settings = {'aggregations': [HAS_DATA], **settings}
    with pytest.raises(ValueError, match='unknown|Unknown'):
        SignalDataProcessor(raw_data=events([(0, 'A', 1, 2)]), bin_size=15, verbose=0, **settings)


def test_the_list_of_names_matches_the_queries():
    """Every parameter a query reads is in AGGREGATION_PARAMS, unless the processor sets it itself."""
    folder = os.path.join(os.path.dirname(__file__), '..', 'src', 'atspm', 'queries')
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(folder))

    def names(template, seen):
        if template in seen:
            return set()
        seen.add(template)
        tree = env.parse(env.loader.get_source(env, template)[0])
        found = set(meta.find_undeclared_variables(tree))
        for other in meta.find_referenced_templates(tree):
            found |= names(other, seen)
        return found

    set_by_processor = {'bin_size', 'from_table', 'unmatched', 'incremental_run', 'carry_bins', 'controller_type', 'remove_incomplete',
                        'known_detectors_found', 'ped_carried', 'coord_state_found', 'event_gap_schedule', 'yellow_red_carried', 'carry_alarms'}
    for name, known in AGGREGATION_PARAMS.items():
        read = names(f'{name}.sql', set()) - set_by_processor
        assert read <= known, (name, read - known)
    assert 'live' in AGGREGATION_PARAMS['timeline']


def test_caller_dicts_are_left_alone(tmp_path):
    state = tmp_path / 'state.parquet'
    pd.DataFrame({'TimeStamp': [T0], 'DeviceId': ['A'], 'EventId': [936], 'Parameter': [0], 'IsValid': [True]}).to_parquet(state)
    settings = {'df_or_path': str(state), 'max_days_old': 2}
    aggregations = [HAS_DATA, TIMELINE, {'name': 'actuations', 'params': {'fill_in_missing': True, 'known_detectors_df_or_path': None, 'known_detectors_max_days_old': 7}},
                    {'name': 'arrival_on_green', 'params': {}}]
    before = repr(settings), repr(aggregations)
    raw = events([(i, 'A', 82, 5) for i in range(1000, 1010)])
    for _ in range(3):  # a warm worker reusing one dict: the third run used to find its state gone
        run(raw, aggregations, unmatched_event_settings=settings)
        assert (repr(settings), repr(aggregations)) == before


def test_listed_tables_are_written_whatever_the_controller_type():
    raw = events([(i, 'A', 300 + i % 3, 5) for i in range(1000, 1060)] + [(1000, 'A', 131, 1), (1010, 'A', 132, 90)])
    out = run(raw, [{'name': 'splits', 'params': {}}, {'name': 'coordination', 'params': {}}], tables=['splits', 'coordination'])
    assert len(out['splits']) and len(out['coordination'])


def test_has_data_can_be_listed_last_with_remove_incomplete():
    raw = events([(i, 'A', 82, 5) for i in range(1000, 1010)])
    listed = [{'name': 'actuations', 'params': {}}, HAS_DATA]
    assert run(raw, listed, tables=['actuations'], remove_incomplete=True)['actuations'].Total.sum() == 10
    assert run(raw, listed[::-1], tables=['actuations'], remove_incomplete=True)['actuations'].Total.sum() == 10


STATE_SETTINGS = {'max_days_old': 2}
RAW = events([(1000, 'A', 1, 2), (1030, 'A', 7, 2)])


@pytest.mark.parametrize('settings', [
    {'df_or_path': 'no_such_folder/state.parquet'},
    {'split_fail_df_or_path': 'no_such_folder/sf.parquet'},
    {'df_or_path': 'az://container/state.parquet'},
])
def test_missing_state_file_raises(settings):
    with pytest.raises(FileNotFoundError, match='does not exist'):
        SignalDataProcessor(raw_data=RAW, bin_size=15, verbose=0, aggregations=[HAS_DATA, TIMELINE],
                            unmatched_event_settings={**settings, **STATE_SETTINGS})


def test_missing_known_detectors_file_raises():
    with pytest.raises(FileNotFoundError, match='known_detectors_df_or_path'):
        SignalDataProcessor(raw_data=RAW, bin_size=15, verbose=0, aggregations=[
            {'name': 'actuations', 'params': {'fill_in_missing': True, 'known_detectors_df_or_path': 'no_such_folder/known.parquet'}}])


@pytest.mark.parametrize('first', ['', None])
def test_blank_state_says_first_run(first, capsys):
    with SignalDataProcessor(raw_data=RAW, detector_config=EMPTY_CONFIG, bin_size=15, verbose=1, aggregations=[HAS_DATA, TIMELINE],
                             unmatched_event_settings={'df_or_path': first, **STATE_SETTINGS}) as p:
        p.load()
        p.aggregate()
        assert p.conn.sql('SELECT count(*) FROM unmatched_events').fetchall()[0][0] > 0
    assert 'First run' in capsys.readouterr().out


def test_first_run_setting_lets_a_file_be_written(tmp_path):
    path, out = str(tmp_path / 'state.parquet'), str(tmp_path / 'out')
    kwargs = dict(detector_config=EMPTY_CONFIG, bin_size=15, verbose=0, aggregations=[HAS_DATA, TIMELINE], output_dir=out, output_format='parquet',
                  output_to_separate_folders=False)
    SignalDataProcessor(raw_data=RAW, unmatched_event_settings={'df_or_path': path, 'first_run': True, **STATE_SETTINGS}, **kwargs).run()
    assert os.path.exists(path)
    # The file is read from then on, whether or not the setting is still there
    later = events([(2000, 'A', 1, 2), (2030, 'A', 7, 2)])
    SignalDataProcessor(raw_data=later, unmatched_event_settings={'df_or_path': path, **STATE_SETTINGS}, **kwargs).run()
    assert os.path.exists(path)


def test_empty_frame_of_state_is_read_as_state():
    empty = pd.DataFrame(columns=['TimeStamp', 'DeviceId', 'EventId', 'Parameter', 'IsValid'])
    out = run(RAW, [HAS_DATA, TIMELINE], tables=['unmatched_events'], unmatched_event_settings={'df_or_path': empty, **STATE_SETTINGS})
    assert len(out['unmatched_events'])


def test_split_failure_state_loads_when_the_main_state_is_a_first_run(tmp_path):
    sf = tmp_path / 'sf.parquet'
    pd.DataFrame({'TimeStamp': [T0], 'DeviceId': ['A'], 'EventId': [82], 'Detector': [5], 'Phase': [2]}).to_parquet(sf)
    config = pd.DataFrame({'DeviceId': ['A'], 'Phase': [2], 'Parameter': [5], 'Function': ['Presence']})
    sf_params = {'red_time': 5, 'red_occupancy_threshold': 0.8, 'green_occupancy_threshold': 0.8, 'by_approach': True, 'by_cycle': False}
    out = run(RAW, [{'name': 'split_failures', 'params': sf_params}], tables=['sf_unmatched'], config=config,
              unmatched_event_settings={'df_or_path': None, 'split_fail_df_or_path': str(sf), **STATE_SETTINGS})
    assert (out['sf_unmatched'].EventId == 82).any()


STATE_ACTUATIONS = [{'name': 'actuations', 'params': {}}]


def run_on(path, raw, aggregations=None, config=EMPTY_CONFIG, **settings):
    """One run, saving to a state file: what a worker does."""
    p = SignalDataProcessor(raw_data=raw, detector_config=config, bin_size=15, verbose=0, aggregations=aggregations or [HAS_DATA, TIMELINE],
                            unmatched_event_settings={'df_or_path': str(path), 'max_days_old': 14, 'first_run': True, **settings})
    with p:
        p.load()
        p.aggregate()
        p.conn.execute(f"COPY unmatched_events TO '{path}'")
        return p.conn.sql('SELECT * FROM unmatched_events').df()


@pytest.mark.parametrize('aggregations', [[HAS_DATA, TIMELINE], STATE_ACTUATIONS], ids=['timeline', 'without_timeline'])
def test_input_older_than_the_state_raises(tmp_path, aggregations):
    path = tmp_path / 'state.parquet'
    window = events([(0, 'A', 1, 2), (30, 'A', 7, 2), (60, 'A', 82, 5)])
    run_on(path, window, aggregations)
    for again in (window, events([(30, 'A', 1, 2)]), events([(-600, 'A', 1, 2)])):
        with pytest.raises(ValueError, match='older than the state'):
            run_on(path, again, aggregations)
    # The next window follows on, and a device with no events held yet is new
    run_on(path, events([(900, 'A', 1, 2), (-600, 'B', 1, 2)]), aggregations)


def test_input_older_than_the_split_failure_state_raises(tmp_path):
    sf = tmp_path / 'sf.parquet'
    pd.DataFrame({'TimeStamp': [T0 + pd.Timedelta(seconds=30)], 'DeviceId': ['A'], 'EventId': [82], 'Detector': [5], 'Phase': [2]}).to_parquet(sf)
    settings = {'split_fail_df_or_path': str(sf), 'max_days_old': 14}
    with pytest.raises(ValueError, match='older than the state'):
        SignalDataProcessor(raw_data=events([(0, 'A', 1, 2)]), detector_config=EMPTY_CONFIG, bin_size=15, verbose=0, aggregations=[],
                            unmatched_event_settings=settings).load()


@pytest.mark.parametrize('held, listed, missing', [
    ([HAS_DATA, TIMELINE], [HAS_DATA], 'timeline'),
    ([{'name': 'ped', 'params': {}}], STATE_ACTUATIONS, 'ped'),
    ([{'name': 'arrival_on_green', 'params': {'latency_offset_seconds': 0}}], STATE_ACTUATIONS, 'arrival_on_green'),
    ([{'name': 'yellow_red', 'params': {'latency_offset_seconds': 0}}], STATE_ACTUATIONS, 'yellow_red'),
    ([HAS_DATA, TIMELINE, {'name': 'coordination_agg', 'params': {}}], [HAS_DATA, TIMELINE], 'coordination_agg'),
])
def test_state_of_a_table_left_out_of_the_list_raises(tmp_path, held, listed, missing):
    path = tmp_path / 'state.parquet'
    first = events([(0, 'A', 131, 3), (1, 'A', 1, 2), (2, 'A', 8, 2), (3, 'A', 10, 2), (4, 'A', 90, 2), (5, 'A', 82, 5)])
    config = pd.DataFrame({'DeviceId': ['A', 'A'], 'Phase': [2, 2], 'Parameter': [5, 5], 'Function': ['Advance', 'Yellow_Red']})
    run_on(path, first, held, config)
    next_window = events([(900, 'A', 1, 2)])
    with pytest.raises(ValueError, match=f"state holds rows of .*{missing}"):
        run_on(path, next_window, listed, config)
    run_on(path, next_window, [*held, {'name': 'terminations', 'params': {}}], config)  # a longer list is a first run for what it adds


def test_the_rows_every_table_shares_do_not_need_a_table(tmp_path):
    path = tmp_path / 'state.parquet'
    pd.DataFrame({'TimeStamp': [T0, T0], 'DeviceId': ['A', 'A'], 'EventId': [936, 942], 'Parameter': [0, 0], 'IsValid': [True, True]}).to_parquet(path)
    run_on(path, events([(900, 'A', 82, 5)]), STATE_ACTUATIONS)


COORDINATION = [HAS_DATA, TIMELINE, {'name': 'coordination_agg', 'params': {}}]


def coordination_run(path, raw):
    with SignalDataProcessor(raw_data=raw, detector_config=EMPTY_CONFIG, bin_size=15, verbose=0, aggregations=COORDINATION,
                             unmatched_event_settings={'df_or_path': str(path), 'max_days_old': 14, 'first_run': True}) as p:
        p.load()
        p.aggregate()
        p.conn.execute(f"COPY unmatched_events TO '{path}'")
        return p.conn.sql('SELECT * FROM coordination_agg').df().set_index('DeviceId')


@pytest.mark.parametrize('quiet', [events([(1000, 'B', 1, 2)]), events([(1000, 'B', 1, 2)]).iloc[:0]], ids=['one_signal', 'no_events_at_all'])
def test_a_signal_silent_for_a_run_keeps_its_pattern(tmp_path, quiet):
    path = tmp_path / 'state.parquet'
    coordination_run(path, events([(10, 'A', 131, 3), (20, 'A', 132, 90), (10, 'B', 131, 4), (20, 'B', 1, 2)]))
    coordination_run(path, quiet)
    after = coordination_run(path, events([(2000, 'A', 1, 2), (2000, 'B', 1, 2)]))
    assert after.loc['A', 'Pattern'] == 3 and after.loc['A', 'CycleLength'] == 90 and after.loc['B', 'Pattern'] == 4


def test_a_signal_with_data_takes_its_new_pattern_not_the_kept_one(tmp_path):
    path = tmp_path / 'state.parquet'
    coordination_run(path, events([(10, 'A', 131, 3)]))
    coordination_run(path, events([(1000, 'A', 131, 5)]))
    state = pd.read_parquet(path).query('EventId == 931')
    assert state.Parameter.tolist() == [5]

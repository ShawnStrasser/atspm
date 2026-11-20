"""conn= mode: the package runs on an app's own DuckDB connection, reads the raw events and detector config from its tables,
and writes outputs and state to tables in one schema there, in one transaction per run. Here it must equal the DataFrame mode
row for row, keep device subsets apart, leave the app's connection as it found it and write nothing when a run fails."""
from collections import defaultdict

import duckdb
import pandas as pd
import pytest

from src.atspm import SignalDataProcessor

RAW = duckdb.query("select * from 'tests/hires_test_data.parquet'").df()
CONFIG = duckdb.query("select * from 'tests/configs_test_data.parquet'").df()
START, END = pd.Timestamp('2024-05-13 15:00:00'), pd.Timestamp('2024-05-13 18:00:00')
DEVICES = sorted(RAW['DeviceId'].unique().tolist())

# ODOT's list, but for detector_health (tested on its own below)
AGGREGATIONS = [
    {'name': 'has_data', 'params': {}},
    {'name': 'actuations', 'params': {'fill_in_missing': True, 'known_detectors_max_days_old': 2}},
    {'name': 'arrival_on_green', 'params': {'latency_offset_seconds': 0}},
    {'name': 'platoon_ratio', 'params': {}},
    {'name': 'approach_delay', 'params': {'latency_offset_seconds': 0}},
    {'name': 'arrival_pattern', 'params': {'offset_slice_seconds': 5}},
    {'name': 'left_turn_gaps', 'params': {}},
    {'name': 'coordination', 'params': {}},
    {'name': 'coordination_agg', 'params': {}},
    {'name': 'split_failures', 'params': {'red_time': 5, 'red_occupancy_threshold': 0.80, 'green_occupancy_threshold': 0.70,
                                          'by_approach': True, 'by_cycle': False}},
    {'name': 'splits', 'params': {}},
    {'name': 'terminations', 'params': {}},
    {'name': 'yellow_red', 'params': {'latency_offset_seconds': 1.5, 'min_red_offset': -8}},
    {'name': 'timeline', 'params': {'min_duration': 0.2, 'cushion_time': 60}},
    {'name': 'ped', 'params': {'seconds_between_actuations': 15}},
    {'name': 'ped_delay', 'params': {}},
    {'name': 'phase_wait', 'params': {'preempt_recovery_seconds': 120, 'assumed_cycle_length': 150, 'skip_multiplier': 1.5,
                                      'tsp_skip_multiplier': 2.0}},
]
SETTINGS = {'bin_size': 15, 'remove_incomplete': True, 'controller_type': 'maxtime', 'verbose': 0}
INTERNAL = {'raw_data', 'detector_config', 'unmatched_previous', 'sf_unmatched_previous', 'sf_final'}
STATE = ('unmatched_events', 'sf_unmatched')


def windows(minutes, start=START, end=END):
    edges = list(pd.date_range(start, end, freq=f'{minutes}min'))
    return list(zip(edges[:-1], edges[1:]))


def _aggregations():
    return [{**a, 'params': dict(a['params'])} for a in AGGREGATIONS]


def _ordered(frame):
    return frame.sort_values(list(frame.columns)).reset_index(drop=True)


def _same(a, b, label=''):
    a, b = a.copy(), b.copy()
    assert set(a.columns) == set(b.columns), label
    b = b[list(a.columns)]
    pd.testing.assert_frame_equal(_ordered(a), _ordered(b), check_dtype=False, obj=label)


def dataframe_runs(cuts, raw=RAW):
    """As ODOT runs it: DataFrames in, every table read back off processor.conn, state handed to the next run as DataFrames."""
    unmatched, sf, outputs = '', '', defaultdict(list)
    for a, b in cuts:
        chunk = raw[(raw['TimeStamp'] >= a) & (raw['TimeStamp'] < b)]
        with SignalDataProcessor(raw_data=chunk, detector_config=CONFIG, aggregations=_aggregations(), **SETTINGS,
                                 unmatched_event_settings={'df_or_path': unmatched, 'split_fail_df_or_path': sf, 'max_days_old': 14}) as p:
            p.load()
            p.aggregate()
            names = [r[0] for r in p.conn.sql("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()]
            tables = {n: p.conn.sql(f'SELECT * FROM "{n}"').df() for n in names if n not in INTERNAL}
        unmatched, sf = tables.pop('unmatched_events'), tables.pop('sf_unmatched')
        for name, frame in tables.items():
            outputs[name].append(frame)
    out = {name: pd.concat(frames, ignore_index=True) for name, frames in outputs.items()}
    out['unmatched_events'], out['sf_unmatched'] = unmatched, sf
    return out


def app_database(path=None, decoys=True):
    """An app's database: its events and config under its own names, and tables of its own that share the package's names."""
    app = duckdb.connect(str(path) if path else ':memory:')
    app.register('raw_frame', RAW.assign(Source='feed'))
    app.register('config_frame', CONFIG)
    app.execute("CREATE TABLE events AS SELECT * FROM raw_frame ORDER BY TimeStamp")
    app.execute("CREATE TABLE signal_detectors AS SELECT * FROM config_frame")
    app.unregister('raw_frame')
    app.unregister('config_frame')
    if decoys:
        for name in ('timeline', 'unmatched_events', 'has_data', 'raw_store', 'health_store', 'sf_final'):
            app.execute(f"CREATE TABLE {name} (app_column INTEGER); INSERT INTO {name} VALUES (42)")
    return app


def conn_runs(app, cuts, schema='atspm', **kwargs):
    for a, b in cuts:
        SignalDataProcessor(conn=app, start=a, end=b, aggregations=_aggregations(), schema=schema, **SETTINGS, **kwargs).run()


def app_tables(app, schema='atspm'):
    names = [r[0] for r in app.execute("SELECT table_name FROM duckdb_tables() WHERE schema_name = ? AND database_name = current_database()",
                                       [schema]).fetchall()]
    return {n: app.execute(f'SELECT * FROM "{schema}"."{n}"').df() for n in names}


@pytest.fixture(scope='module')
def reference():
    return dataframe_runs(windows(15))


@pytest.fixture(scope='module')
def conn_result():
    app = app_database()
    before = _snapshot(app)
    conn_runs(app, windows(15))
    return app, before


def _snapshot(app):
    return {
        'database': app.execute("SELECT current_database()").fetchall(),
        'databases': app.execute("SELECT database_name FROM duckdb_databases() ORDER BY 1").fetchall(),
        'main': {n: app.execute(f'SELECT * FROM main."{n}"').fetchall()
                 for (n,) in app.execute("SELECT table_name FROM duckdb_tables() WHERE schema_name = 'main' AND database_name = current_database()").fetchall()},
        'settings': app.execute("SELECT name, value FROM duckdb_settings() WHERE name IN ('memory_limit', 'threads', 'temp_directory', 'search_path')").fetchall(),
    }


# ---- equals the DataFrame mode

def test_fifteen_minute_runs_equal_the_dataframe_mode_on_every_output_and_the_state(reference, conn_result):
    app, _ = conn_result
    got = app_tables(app)
    assert set(got) == set(reference), (sorted(got), sorted(reference))
    for name, frame in reference.items():
        assert len(frame) > 0 or name in ('splits', 'terminations', 'sf_unmatched'), name
        _same(got[name], frame, name)


def test_one_run_equals_the_dataframe_mode():
    app = app_database()
    conn_runs(app, [(START, END)])
    got, reference = app_tables(app), dataframe_runs([(START, END)])
    assert set(got) == set(reference)
    for name, frame in reference.items():
        _same(got[name], frame, name)


def test_outputs_carry_the_dataframe_modes_column_types(reference, conn_result):
    app, _ = conn_result
    for name in ('arrival_on_green', 'approach_delay', 'unmatched_events', 'timeline'):
        types = dict(app.execute(f"SELECT column_name, data_type FROM duckdb_columns() WHERE schema_name = 'atspm' AND table_name = ?", [name]).fetchall())
        with SignalDataProcessor(raw_data=RAW.head(1000), detector_config=CONFIG, aggregations=_aggregations(), **SETTINGS,
                                 unmatched_event_settings={'df_or_path': '', 'max_days_old': 14}) as p:
            p.load()
            p.aggregate()
            expected = {c: t for c, t, *_ in p.conn.execute(f'DESCRIBE "{name}"').fetchall()}
        assert types == expected, name


# ---- the app's connection is left as it was

def test_the_apps_connection_tables_and_settings_are_untouched(conn_result):
    app, before = conn_result
    after = _snapshot(app)
    assert after == before
    assert app.execute("SELECT count(*) FROM duckdb_tables() WHERE temporary").fetchall()[0][0] == 0
    assert app.execute("SELECT count(*) FROM events").fetchall()[0][0] == len(RAW)


def test_only_the_schema_gets_new_tables(conn_result):
    app, before = conn_result
    schemas = {r[0] for r in app.execute("SELECT DISTINCT schema_name FROM duckdb_tables() WHERE database_name = current_database()").fetchall()}
    assert schemas == {'main', 'atspm'}
    assert set(before['main']) == {r[0] for r in app.execute(
        "SELECT table_name FROM duckdb_tables() WHERE schema_name = 'main' AND database_name = current_database()").fetchall()}


def test_a_file_database_stays_usable_and_open(tmp_path):
    app = app_database(tmp_path / 'app.duckdb')
    conn_runs(app, windows(60)[:1])
    app.execute("CHECKPOINT")
    app.execute("INSERT INTO main.timeline VALUES (7)")
    assert app.execute("SELECT count(*) FROM atspm.arrival_on_green").fetchall()[0][0] > 0
    app.close()
    again = duckdb.connect(str(tmp_path / 'app.duckdb'))
    assert again.execute("SELECT count(*) FROM atspm.unmatched_events").fetchall()[0][0] > 0


# ---- state: found, saved, and kept apart by device

def test_the_first_run_finds_no_state_and_the_next_finds_it():
    app = app_database()
    conn_runs(app, windows(15)[:1])
    first = app.execute("SELECT count(*) FROM atspm.unmatched_events").fetchall()[0][0]
    assert first > 0
    conn_runs(app, windows(15)[1:2])
    assert app.execute("SELECT count(*) FROM atspm.arrival_on_green").fetchall()[0][0] > 0


def test_a_window_already_run_raises_and_writes_nothing():
    app = app_database()
    conn_runs(app, windows(15)[:2])
    before = app_tables(app)
    with pytest.raises(ValueError, match='older than the state'):
        conn_runs(app, windows(15)[1:2])
    for name, frame in app_tables(app).items():
        _same(frame, before[name], name)


def test_device_subsets_run_apart_equal_all_devices_at_once():
    app = app_database()
    cuts = windows(30)
    conn_runs(app, cuts, schema='together')
    groups = [DEVICES[:1], DEVICES[1:]]
    for a, b in cuts:
        for group in groups:
            SignalDataProcessor(conn=app, start=a, end=b, devices=group, aggregations=_aggregations(), schema='apart', **SETTINGS).run()
    together, apart = app_tables(app, 'together'), app_tables(app, 'apart')
    assert set(together) == set(apart)
    for name, frame in together.items():
        _same(apart[name], frame, name)


def test_a_device_subset_leaves_the_other_devices_state_alone():
    app = app_database()
    conn_runs(app, windows(30)[:1])
    others = app.execute("SELECT * FROM atspm.unmatched_events WHERE DeviceId <> ? ORDER BY ALL", [DEVICES[0]]).fetchall()
    assert others
    a, b = windows(30)[1]
    SignalDataProcessor(conn=app, start=a, end=b, devices=DEVICES[:1], aggregations=_aggregations(), **SETTINGS).run()
    assert app.execute("SELECT * FROM atspm.unmatched_events WHERE DeviceId <> ? ORDER BY ALL", [DEVICES[0]]).fetchall() == others
    assert app.execute("SELECT DISTINCT DeviceId FROM atspm.arrival_on_green WHERE TimeStamp >= ?", [a]).fetchall() == [(DEVICES[0],)]


# ---- a failed run writes nothing, and its retry equals a clean run

def test_a_failure_while_saving_writes_nothing_and_the_retry_writes_no_duplicates(monkeypatch):
    app = app_database()
    cuts = windows(15)[:3]
    conn_runs(app, cuts[:1])
    before = app_tables(app)
    calls = {'n': 0}
    original = SignalDataProcessor._write_state

    def fail_on_state(self, *args, **kwargs):
        calls['n'] += 1
        raise RuntimeError('disk full')
    monkeypatch.setattr(SignalDataProcessor, '_write_state', fail_on_state)
    with pytest.raises(RuntimeError, match='disk full'):
        conn_runs(app, cuts[1:2])
    assert calls['n'] == 1
    after = app_tables(app)
    assert set(after) == set(before)
    for name, frame in after.items():
        _same(frame, before[name], name)
    assert app.execute("SELECT count(*) FROM duckdb_databases()").fetchall() == [(len(_snapshot(app)['databases']),)]
    monkeypatch.setattr(SignalDataProcessor, '_write_state', original)
    conn_runs(app, cuts[1:])
    clean = app_database()
    conn_runs(clean, cuts)
    for name, frame in app_tables(clean).items():
        _same(app_tables(app)[name], frame, name)


def test_a_failure_while_aggregating_leaves_nothing_attached(monkeypatch):
    app = app_database()
    databases = _snapshot(app)['databases']
    monkeypatch.setattr(SignalDataProcessor, 'aggregate', lambda self: (_ for _ in ()).throw(RuntimeError('boom')))
    with pytest.raises(RuntimeError, match='boom'):
        conn_runs(app, windows(15)[:1])
    assert _snapshot(app)['databases'] == databases
    assert app.execute("SELECT count(*) FROM duckdb_tables() WHERE schema_name = 'atspm'").fetchall()[0][0] == 0


# ---- finding the tables

def test_named_tables_and_sql_work_as_well_as_found_ones():
    found, named = app_database(), app_database()
    conn_runs(found, windows(60)[:1])
    a, b = windows(60)[0]
    SignalDataProcessor(conn=named, start=a, end=b, raw_data='events', detector_config='main.signal_detectors',
                        aggregations=_aggregations(), **SETTINGS).run()
    for name, frame in app_tables(found).items():
        _same(app_tables(named)[name], frame, name)


def test_a_table_named_raw_data_is_taken_first():
    app = app_database(decoys=False)
    app.execute("CREATE TABLE raw_data AS SELECT * FROM events WHERE DeviceId = ?", [DEVICES[0]])
    conn_runs(app, windows(60)[:1])
    assert app.execute("SELECT DISTINCT DeviceId FROM atspm.actuations").fetchall() == [(DEVICES[0],)]


def test_several_candidates_raise_with_their_names():
    app = app_database()
    app.execute("CREATE TABLE events_copy AS SELECT * FROM events")
    with pytest.raises(ValueError, match='events_copy'):
        conn_runs(app, windows(60)[:1])


def test_no_events_table_raises():
    app = duckdb.connect()
    app.execute("CREATE TABLE something (a INTEGER)")
    with pytest.raises(ValueError, match='raw events'):
        conn_runs(app, windows(60)[:1])


def test_the_window_and_devices_filter_the_events():
    app = app_database()
    a, b = windows(15)[2]
    with SignalDataProcessor(conn=app, start=a, end=b, devices=DEVICES[1:], aggregations=[], bin_size=15, verbose=0) as p:
        p.load()
        lo, hi, devices = p.conn.execute("SELECT min(TimeStamp), max(TimeStamp), list(DISTINCT DeviceId ORDER BY DeviceId) FROM raw_data").fetchall()[0]
    assert a <= lo and hi < b and devices == DEVICES[1:]


def test_an_empty_window_writes_its_state_and_no_error():
    app = app_database()
    conn_runs(app, windows(15)[:1])
    late = END + pd.Timedelta(hours=1)
    SignalDataProcessor(conn=app, start=late, end=late + pd.Timedelta(minutes=15), aggregations=_aggregations(), **SETTINGS).run()
    assert app.execute("SELECT count(*) FROM atspm.unmatched_events").fetchall()[0][0] > 0


def test_a_setting_that_raises_after_the_attach_leaves_nothing_attached():
    app = app_database()
    databases = _snapshot(app)['databases']
    with pytest.raises(ValueError, match='unknown parameters'):
        SignalDataProcessor(conn=app, start=START, end=END, verbose=0, aggregations=[{'name': 'detector_health', 'params': {'nope': 1}}])
    with pytest.raises(AssertionError, match='has_data'):
        SignalDataProcessor(conn=app, start=START, end=END, verbose=0, remove_incomplete=True, aggregations=[])
    with pytest.raises(ValueError, match='raw events'):
        SignalDataProcessor(conn=duckdb.connect(), start=START, end=END, verbose=0, aggregations=[])
    assert _snapshot(app)['databases'] == databases


# ---- settings that do not fit conn mode

@pytest.mark.parametrize('settings, message', [
    ({}, 'start and end'),
    ({'start': START}, 'start and end'),
    ({'start': START, 'end': START}, 'after start'),
    ({'start': START + pd.Timedelta(minutes=7), 'end': END}, 'bin'),
    ({'start': START, 'end': END, 'output_dir': 'x'}, 'output_dir'),
    ({'start': START, 'end': END, 'to_sql': True}, 'to_sql'),
    ({'start': START, 'end': END, 'unmatched_event_settings': {'df_or_path': ''}}, 'df_or_path'),
    ({'start': START, 'end': END, 'schema': 'main'}, 'schema'),
])
def test_settings_that_do_not_fit_raise(settings, message):
    with pytest.raises(ValueError, match=message):
        SignalDataProcessor(conn=duckdb.connect(), aggregations=_aggregations(), bin_size=15, verbose=0, **settings)


@pytest.mark.parametrize('settings', [{'start': START}, {'devices': [1]}, {'schema': 'x'}])
def test_conn_settings_without_conn_raise(settings):
    with pytest.raises(ValueError, match='conn'):
        SignalDataProcessor(raw_data=RAW, aggregations=[], verbose=0, **settings)


def test_max_days_old_can_be_set():
    app = app_database()
    a, b = windows(15)[0]
    SignalDataProcessor(conn=app, start=a, end=b, aggregations=_aggregations(), unmatched_event_settings={'max_days_old': 3}, **SETTINGS).run()
    assert app.execute("SELECT count(*) FROM atspm.unmatched_events").fetchall()[0][0] > 0


# ---- detector_health: its state and its rows kept in the app's tables

def test_detector_health_across_midnight_with_workers_equals_the_dataframe_mode(tmp_path):
    from tests.test_detector_health_bins import DETECTORS as STUCK, MIDNIGHT, T0, _all_rows, _runs, _stuck_raw
    raw = _stuck_raw(3)
    raw = raw[raw['TimeStamp'] < MIDNIGHT + pd.Timedelta(minutes=45)]
    cuts = [MIDNIGHT - pd.Timedelta(minutes=15), MIDNIGHT, MIDNIGHT + pd.Timedelta(minutes=15)]
    reference = _runs(raw, cuts, STUCK, tmp_path, 'r', findings=True, workers=2)
    app = duckdb.connect(str(tmp_path / 'app.duckdb'))
    app.register('raw_frame', raw)
    app.register('detectors_frame', STUCK)
    app.execute("CREATE TABLE events AS SELECT * FROM raw_frame; CREATE TABLE health_detectors AS SELECT * FROM detectors_frame")
    edges = [T0] + cuts + [MIDNIGHT + pd.Timedelta(minutes=45)]
    for a, b in zip(edges[:-1], edges[1:]):
        SignalDataProcessor(conn=app, start=a, end=b, bin_size=15, verbose=0, aggregations=[
            {'name': 'detector_health', 'params': {'detectors': 'health_detectors', 'findings': True, 'workers': 2}}]).run()
    got = app_tables(app)
    assert {'detector_health', 'detector_health_findings', 'health_state', 'health_rows'} <= set(got)
    assert len(got['detector_health']) > 0
    _same(got['detector_health'], _all_rows(reference), 'detector_health')
    findings = pd.concat([t['detector_health_findings'] for t in reference if 'detector_health_findings' in t], ignore_index=True)
    _same(got['detector_health_findings'], findings, 'detector_health_findings')
    # the rows kept are those of the runs the state lists, which the next whole-day check needs
    listed = set(app.execute("SELECT Value::BIGINT FROM atspm.health_state WHERE EventId = 9605").fetchall())
    assert set(app.execute("SELECT DISTINCT Run FROM atspm.health_rows").fetchall()) <= listed


def test_detector_health_with_a_device_subset_raises():
    with pytest.raises(ValueError, match='devices'):
        SignalDataProcessor(conn=duckdb.connect(), start=START, end=END, devices=DEVICES[:1], verbose=0,
                            aggregations=[{'name': 'detector_health', 'params': {}}])


# ---- as docs/APP_INTEGRATION.md tells apps to use it

def test_state_moved_from_files_carries_on_as_if_never_moved(tmp_path, reference):
    cuts = windows(15)
    first = dataframe_runs(cuts[:4])
    first['unmatched_events'].to_parquet(tmp_path / 'unmatched_events.parquet')
    first['sf_unmatched'].to_parquet(tmp_path / 'sf_unmatched.parquet')
    app = app_database()
    app.execute(f"""CREATE SCHEMA atspm;
        CREATE TABLE atspm.unmatched_events AS SELECT * FROM '{(tmp_path / 'unmatched_events.parquet').as_posix()}';
        CREATE TABLE atspm.sf_unmatched AS SELECT * FROM '{(tmp_path / 'sf_unmatched.parquet').as_posix()}'""")
    conn_runs(app, cuts[4:])
    got = app_tables(app)
    for name in ('arrival_on_green', 'approach_delay', 'timeline', 'split_failures', 'ped'):
        _same(pd.concat([first[name], got[name]], ignore_index=True), reference[name], name)
    _same(got['unmatched_events'], reference['unmatched_events'], 'unmatched_events')


def test_the_queue_model_runs_the_same_with_conn():
    aggregations = [{'name': 'arrival_on_green', 'params': {'queue_model': True}}, {'name': 'platoon_ratio', 'params': {}},
                    {'name': 'approach_delay', 'params': {'queue_model': True}}]
    unmatched, outputs = '', defaultdict(list)
    for a, b in windows(15):
        chunk = RAW[(RAW['TimeStamp'] >= a) & (RAW['TimeStamp'] < b)]
        with SignalDataProcessor(raw_data=chunk, detector_config=CONFIG, bin_size=15, verbose=0, aggregations=aggregations,
                                 unmatched_event_settings={'df_or_path': unmatched, 'max_days_old': 14}) as p:
            p.load()
            p.aggregate()
            for name in ('arrival_on_green', 'platoon_ratio', 'approach_delay'):
                outputs[name].append(p.conn.sql(f'SELECT * FROM {name}').df())
            unmatched = p.conn.sql('SELECT * FROM unmatched_events').df()
    app = app_database()
    for a, b in windows(15):
        SignalDataProcessor(conn=app, start=a, end=b, bin_size=15, verbose=0, aggregations=aggregations).run()
    got = app_tables(app)
    assert 'Queue_Delay_Seconds' in got['approach_delay']
    for name, frames in outputs.items():
        _same(got[name], pd.concat(frames, ignore_index=True), name)

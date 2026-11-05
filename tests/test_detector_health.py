"""The detector_health aggregation on atspm-detector's incremental health (tests A1 to A8 of the design)."""
import builtins
import os
import re

import duckdb
import pandas as pd
import pytest

from src.atspm import SignalDataProcessor
from src.atspm.detector_health import _judge_due

H = pytest.importorskip('atspm_detector.incremental', exc_type=ImportError)
from atspm_detector import health
from atspm_detector.bins import bins

RAW = duckdb.query("select * from 'tests/hires_test_data.parquet'").df()
CONFIG = duckdb.query("select * from 'tests/configs_test_data.parquet'").df()
START, END = pd.Timestamp('2024-05-13 15:00:00'), pd.Timestamp('2024-05-13 18:00:00')


def _detectors():
  """A detectors table as predict() would give, one row per detector."""
  with SignalDataProcessor(detector_config=CONFIG, raw_data=RAW.head(1), verbose=0, aggregations=[]) as p:
    p.load()
    return p.conn.execute("""
      SELECT DeviceId, Parameter AS Detector, min(Phase) AS phase,
             CASE arg_min(Function, Phase) WHEN 'Stopbar Count' THEN 'Count' ELSE arg_min(Function, Phase) END AS function,
             1 AS lanes
      FROM detector_config GROUP BY 1, 2""").df()


DETECTORS = _detectors()


def _params(**health_params):
  return {'name': 'detector_health', 'params': {'detectors': DETECTORS, **health_params}}


def _chunk(start, end, data=RAW):
  return data[(data['TimeStamp'] >= pd.Timestamp(start)) & (data['TimeStamp'] < pd.Timestamp(end))]


def _chunks(minutes, data=RAW, start=START, end=END):
  edges = pd.date_range(start, end, freq=f'{minutes}min')
  return [_chunk(a, b, data) for a, b in zip(edges[:-1], edges[1:])]


def _run(raw, *aggregations, **kwargs):
  """Load and aggregate; returns every table the connection can see, as save() would, as frames."""
  with SignalDataProcessor(raw_data=raw, bin_size=15, verbose=0, aggregations=list(aggregations), **kwargs) as p:
    p.load()
    p.aggregate()
    names = [r[0] for r in p.conn.sql("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()]
    return {n: p.conn.execute(f'SELECT * FROM "{n}"').df() for n in names if n not in ('raw_data', 'detector_config')}


def _ordered(frame):
  return frame.sort_values(list(frame.columns)).reset_index(drop=True)


def _same(a, b):
  pd.testing.assert_frame_equal(_ordered(a), _ordered(b), check_dtype=False)


def _reference(raw, start, end):
  """atspm-detector on its own, over the same window."""
  con = duckdb.connect()
  con.register('events', raw.rename(columns={'TimeStamp': 'Timestamp'}))
  con.register('detectors', DETECTORS)
  return health(con, 'events', 'detectors', start, end, bin_origin='clock', summary=True)


def _store_rows(path):
  con = duckdb.connect(str(path), read_only=True)
  try:
    names = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
    return {n: _ordered(con.execute(f'SELECT * FROM "{n}"').df()) for n in names}
  finally:
    con.close()


# ---- A1, A2: one run and chunked runs equal atspm-detector on its own

def test_a1_one_shot_equals_the_package_on_its_own():
  out = _run(RAW, _params(findings=True))
  findings, status = _reference(RAW, END - pd.Timedelta(days=3), END)
  assert set(findings['DetectorType']) == {'Vehicle', 'Ped'}
  assert (out['detector_health_findings']['TimeStamp'] == END).all()
  _same(out['detector_health_findings'].drop(columns='TimeStamp'), findings)
  _same(out['detector_health'], bins(duckdb.connect(), findings, status, END - pd.Timedelta(days=3), END))


def test_a2_fifteen_minute_runs_equal_one_run(tmp_path):
  store = str(tmp_path / 'health.duckdb')
  chunks = _chunks(15)
  assert len(chunks) == 12
  for chunk in chunks[:-1]:
    assert 'detector_health' not in _run(chunk, _params(store=store, judge=False))
  chunked = _run(chunks[-1], _params(store=store, judge='every_run', findings=True))
  one = _run(RAW, _params(judge='every_run', findings=True))
  for table in ('detector_health', 'detector_health_findings'):
    _same(chunked[table], one[table])


# ---- the optional tables

def test_an_empty_one_shot_has_the_optional_tables_too():
  out = _run(RAW.head(0), _params(findings=True))
  assert set(out) == {'detector_health', 'detector_health_findings'}
  assert all(len(frame) == 0 and frame.columns[0] == 'TimeStamp' for frame in out.values())


# ---- A3: judge cadence

def _shifted(hours):
  return RAW.assign(TimeStamp=RAW['TimeStamp'] + pd.Timedelta(hours=hours))


def _cadence_runs(tmp_path, cadence):
  """Half-hour runs from 22:30 to 01:00, crossing midnight: which run ends judged."""
  data = pd.concat([_shifted(6),                                # 21:00 to 00:00
                    _chunk('2024-05-14 00:00', '2024-05-14 01:00', _shifted(9))])
  store = str(tmp_path / f'{cadence}.duckdb')
  judged = []
  edges = pd.date_range('2024-05-13 22:30', '2024-05-14 01:00', freq='30min')
  _run(_chunk('2024-05-13 21:00', edges[0], data), _params(store=store, judge=False))
  for a, b in zip(edges[:-1], edges[1:]):
    out = _run(_chunk(a, b, data), _params(store=store, judge=cadence, findings=True))
    if 'detector_health' in out:
      judged.append(b)
      assert out['detector_health_findings']['TimeStamp'].eq(b).all()
  return judged


@pytest.mark.parametrize('cadence, expected', [
  ('daily', ['2024-05-14 00:00']),
  ('hourly', ['2024-05-13 23:00', '2024-05-14 00:00', '2024-05-14 01:00']),
  ('every_run', ['2024-05-13 23:00', '2024-05-13 23:30', '2024-05-14 00:00', '2024-05-14 00:30', '2024-05-14 01:00']),
  (False, []),
])
def test_a3_cadence(tmp_path, cadence, expected):
  assert _cadence_runs(tmp_path, cadence) == [pd.Timestamp(t) for t in expected]


@pytest.mark.parametrize('cadence, end, meta, due', [
  ('daily', '2024-05-14 00:15', {'last_judge_end': '2024-05-13 00:00:00'}, True),     # no run ended at midnight
  ('daily', '2024-05-14 00:30', {'last_judge_end': '2024-05-14 00:15:00'}, False),    # the first run after it judged
  ('daily', '2024-05-14 00:00', {}, True),                                            # the store's first run
  ('daily', '2024-05-13 18:00', {}, False),
  ('daily', '2024-05-14 00:15', {'through': '2024-05-13 23:50:00'}, True),            # the store has never judged
  ('hourly', '2024-05-14 03:15', {'last_judge_end': '2024-05-14 02:00:00'}, True),
])
def test_a3_first_run_after_the_hour(cadence, end, meta, due):
  assert _judge_due(cadence, pd.Timestamp(end), meta) is due


# ---- A4: what save() writes

@pytest.mark.parametrize('output_format', ['csv', 'parquet', 'json'])
def test_a4_save_writes_only_the_output_tables(tmp_path, output_format):
  SignalDataProcessor(raw_data=RAW, bin_size=15, verbose=0, output_dir=str(tmp_path), output_format=output_format,
                      output_to_separate_folders=False, aggregations=[_params(findings=True)]).run()
  assert sorted(os.listdir(tmp_path)) == [f'detector_health.{output_format}', f'detector_health_findings.{output_format}']


def test_a4_a_run_that_does_not_judge_writes_nothing(tmp_path):
  out = tmp_path / 'out'
  SignalDataProcessor(raw_data=_chunks(15)[0], bin_size=15, verbose=0, output_dir=str(out), output_format='parquet',
                      output_to_separate_folders=False,
                      aggregations=[_params(store=str(tmp_path / 'store.duckdb'))]).run()
  assert os.listdir(out) == []


def test_a4_no_store_or_temp_table_stays_behind():
  out = _run(RAW, _params(findings=True))
  assert set(out) == {'detector_health', 'detector_health_findings'}
  for table, frame in out.items():
    assert frame.columns[0] == 'TimeStamp'


# ---- A5: every other aggregation is unaffected

OTHERS = [
  {'name': 'has_data', 'params': {'no_data_min': 5, 'min_data_points': 3}},
  {'name': 'actuations', 'params': {'fill_in_missing': True}},
  {'name': 'arrival_on_green', 'params': {'latency_offset_seconds': 0}},
  {'name': 'yellow_red', 'params': {'latency_offset_seconds': 1.5, 'min_red_offset': -8}},
  {'name': 'timeline', 'params': {'min_duration': 0.2, 'cushion_time': 60}},
  {'name': 'phase_wait', 'params': {'preempt_recovery_seconds': 120, 'assumed_cycle_length': 140, 'skip_multiplier': 1.5}},
  {'name': 'coordination_agg', 'params': {}},
]


def _incremental_outputs(folder, extra):
  """The incremental runs, unmatched events and known detectors carried from file to file: every file they wrote."""
  folder.mkdir()
  unmatched, known = str(folder / 'unmatched.parquet'), str(folder / 'known.parquet')
  for i, chunk in enumerate(_chunks(30)):
    aggregations = [dict(a, params=dict(a['params'])) for a in OTHERS] + extra
    aggregations[1]['params'].update(known_detectors_df_or_path=known, known_detectors_max_days_old=2)
    SignalDataProcessor(raw_data=chunk, detector_config=CONFIG, bin_size=15, verbose=0, output_dir=str(folder),
                        output_format='parquet', output_to_separate_folders=False, output_file_prefix=f'{i}_',
                        unmatched_event_settings={'df_or_path': unmatched, 'max_days_old': 14},
                        aggregations=aggregations).run()
    # each run reads its state back, so a difference here reaches the next run's outputs as well
    assert os.path.exists(unmatched) and os.path.exists(known)
    for name in ('unmatched', 'known'):
      pd.read_parquet(folder / f'{name}.parquet').to_parquet(folder / f'{i}_{name}_copy.parquet')
  return {f: pd.read_parquet(folder / f) for f in os.listdir(folder) if 'detector_health' not in f}


def test_a5_other_outputs_are_identical_with_and_without_detector_health(tmp_path):
  without = _incremental_outputs(tmp_path / 'without', [])
  store = str(tmp_path / 'store.duckdb')
  with_health = _incremental_outputs(tmp_path / 'with', [_params(store=store, judge='every_run')])
  assert set(with_health) == set(without)
  assert any('timeline' in f for f in without) and any('unmatched' in f for f in without)
  for name, frame in without.items():
    _same(with_health[name], frame)
  assert os.path.exists(tmp_path / 'with' / '5_detector_health.parquet')


# ---- A6: the old parameters and the missing extra

@pytest.mark.parametrize('old', ['data', 'device_groups', 'decompose_params', 'anomaly_params', 'return_last_n_days'])
def test_a6_old_parameters_explain_themselves(old):
  with pytest.raises(ValueError, match='traffic-anomaly was removed'):
    SignalDataProcessor(raw_data=RAW, aggregations=[{'name': 'detector_health', 'params': {old: None}}])


@pytest.mark.parametrize('params, message', [
  ({'nonsense': 1}, 'unknown parameters'),
  ({'judge': 'weekly'}, 'judge must be'),
  ({'judge': False}, 'keeps nothing'),
  ({'clock_ahead_minutes': -1}, 'clock_ahead_minutes'),
  ({'outputs': ['weekly']}, 'unknown parameters'),
  ({'outputs': '15min'}, 'unknown parameters'),
])
def test_a6_bad_parameters(params, message):
  with pytest.raises(ValueError, match=message):
    SignalDataProcessor(raw_data=RAW, aggregations=[{'name': 'detector_health', 'params': params}])


def test_a6_missing_extra_names_it(monkeypatch):
  real_import = builtins.__import__

  def no_package(name, *args, **kwargs):
    if name.startswith('atspm_detector'):
      raise ModuleNotFoundError(name)
    return real_import(name, *args, **kwargs)
  monkeypatch.setattr(builtins, '__import__', no_package)
  with pytest.raises(ImportError, match=r'atspm\[detector-health\]'):
    _run(_chunks(15)[0], _params())


def test_to_sql_is_not_supported():
  with SignalDataProcessor(to_sql=True, bin_size=15, verbose=0, aggregations=[_params()]) as p:
    with pytest.raises(ValueError, match='not supported'):
      p.aggregate()


# ---- A7: detector_config as the detectors

def test_a7_detector_config_fallback(monkeypatch):
  seen = {}
  real_update = H.update

  def spy(con, events, detectors, **kwargs):
    seen['rows'] = con.execute(f"SELECT * FROM {detectors}").df()
    return real_update(con, events, detectors, **kwargs)
  monkeypatch.setattr(H, 'update', spy)
  first = CONFIG.sort_values(['DeviceId', 'Parameter', 'Phase']).iloc[0]
  config = pd.concat([CONFIG, pd.DataFrame({'DeviceId': [first.DeviceId], 'Phase': [first.Phase + 1],
                                            'Parameter': [first.Parameter], 'Function': ['Other']})])
  out = _run(RAW, {'name': 'detector_health', 'params': {}}, detector_config=config)
  rows = seen['rows']
  assert 'Count' in set(rows['function']) and 'Stopbar Count' not in set(rows['function'])
  assert set(rows.loc[rows['function'] == 'Count', 'configured_function']) == {'Stopbar Count'}
  assert (rows['lanes'] == 1).all()
  assert not rows.duplicated(['DeviceId', 'Detector']).any()
  doubled = rows[(rows['DeviceId'] == first.DeviceId) & (rows['Detector'] == first.Parameter)].iloc[0]
  assert doubled['phase'] == first.Phase and doubled['configured_function'] == first.Function
  assert len(out['detector_health']) > 0


def test_a7_needs_detectors_from_somewhere():
  with pytest.raises(ValueError, match='detector_config'):
    _run(RAW, {'name': 'detector_health', 'params': {}})


def test_detectors_from_a_path(tmp_path):
  path = tmp_path / 'detectors.parquet'
  DETECTORS.to_parquet(path)
  _same(_run(RAW, _params(detectors=str(path)))['detector_health'], _run(RAW, _params())['detector_health'])


def test_detectors_missing_a_column():
  with pytest.raises(ValueError, match='missing columns'):
    _run(RAW, _params(detectors=DETECTORS.drop(columns='lanes')))


# ---- A8: a failed run

def test_a8_a_failed_update_leaves_the_store_and_the_next_run_equals_a_clean_one(tmp_path, monkeypatch):
  chunks = _chunks(60)
  store = str(tmp_path / 'failed.duckdb')
  _run(chunks[0], _params(store=store, judge=False))
  before = _store_rows(store)

  def fail(*args, **kwargs):
    raise RuntimeError('out of disk')
  with monkeypatch.context() as m:
    m.setattr(H, '_trim', fail)
    with SignalDataProcessor(raw_data=chunks[1], bin_size=15, verbose=0, aggregations=[_params(store=store)]) as p:
      p.load()
      with pytest.raises(RuntimeError, match='out of disk'):
        p.aggregate()
      assert not p.conn.execute("SELECT 1 FROM duckdb_databases() WHERE database_name = 'health_store'").fetchall()
      assert not p.conn.execute("SELECT 1 FROM duckdb_tables() WHERE table_name = 'health_detectors'").fetchall()
  after = _store_rows(store)
  assert set(after) == set(before)
  for table in before:
    pd.testing.assert_frame_equal(after[table], before[table])

  clean = str(tmp_path / 'clean.duckdb')
  _run(chunks[0], _params(store=clean, judge=False))
  _run(chunks[1], _params(store=clean, judge=False))
  _run(chunks[1], _params(store=store, judge=False))
  ends = [_run(chunks[2], _params(store=s, judge='every_run'))['detector_health'] for s in (store, clean)]
  _same(ends[0], ends[1])


# ---- every update passes upto, and the log shows what it held back

def test_a_wrong_clock_is_held_back_and_logged_and_does_not_move_the_others(tmp_path, capsys):
  wrong = RAW[RAW['DeviceId'] == 454].assign(TimeStamp=lambda f: f['TimeStamp'] + pd.DateOffset(years=1))
  others = RAW[RAW['DeviceId'] != 454]
  with SignalDataProcessor(raw_data=pd.concat([others, wrong]), bin_size=15, verbose=1,
                           aggregations=[_params(store=str(tmp_path / 's.duckdb'), judge='every_run', findings=True)]) as p:
    p.load()
    p.aggregate()
    bad = p.conn.execute("SELECT * FROM detector_health_findings").df()
  assert re.search(f"signal 454: [0-9]+ events stamped at or after {END}", capsys.readouterr().out)
  assert (bad['TimeStamp'] == END).all()
  _same(bad, _run(others, _params(findings=True))['detector_health_findings'])


def test_a_clock_a_little_ahead_keeps_its_rows(tmp_path, capsys):
  chunk = _chunks(15)[0]
  ahead = chunk.assign(TimeStamp=chunk['TimeStamp'].where(chunk['DeviceId'] != 454, chunk['TimeStamp'] + pd.Timedelta(minutes=5)))
  with SignalDataProcessor(raw_data=ahead, bin_size=15, verbose=1,
                           aggregations=[_params(store=str(tmp_path / 's.duckdb'), judge='every_run', findings=True)]) as p:
    p.load()
    p.aggregate()
    judged = p.conn.execute("SELECT DISTINCT TimeStamp FROM detector_health_findings").fetchall()
  assert 'events stamped' not in capsys.readouterr().out
  assert judged == [(pd.Timestamp('2024-05-13 15:30:00').to_pydatetime(),)]


def test_running_an_old_chunk_again_is_late_logged_and_still_judges(tmp_path, capsys):
  chunks = _chunks(60)
  store = str(tmp_path / 's.duckdb')
  _run(chunks[0], _params(store=store, judge=False))
  _run(chunks[1], _params(store=store, judge=False))
  with SignalDataProcessor(raw_data=chunks[0], bin_size=15, verbose=1,
                           aggregations=[_params(store=store, judge='every_run', findings=True)]) as p:
    p.load()
    p.aggregate()
    judged = p.conn.execute("SELECT DISTINCT TimeStamp FROM detector_health_findings").fetchall()
  assert 'at or before what the store already holds' in capsys.readouterr().out
  assert judged == [(pd.Timestamp('2024-05-13 17:00:00').to_pydatetime(),)]


# ---- an empty run and an old store

EMPTY = RAW.head(0)


def test_an_empty_one_shot_has_the_tables_without_rows():
  out = _run(EMPTY, _params(findings=True))
  assert list(out['detector_health'].columns) == ['TimeStamp', 'DeviceId', 'DetectorType', 'Detector', 'Label', 'Score']
  assert list(out['detector_health_findings'].columns) == ['TimeStamp', 'DeviceId', 'DetectorType', 'Detector', 'Start', 'End',
                                                           'Label', 'Confidence', 'Detail']
  assert len(out['detector_health']) == 0 and len(out['detector_health_findings']) == 0


def test_an_empty_run_leaves_the_store_alone(tmp_path):
  store = tmp_path / 's.duckdb'
  assert _run(EMPTY, _params(store=str(store))) == {}
  assert not store.exists()


def _age_store(path, version):
  con = duckdb.connect(str(path))
  con.execute("UPDATE health_meta SET Value = ? WHERE Key = 'extractor_version'", [version])
  con.close()


def test_a_store_from_an_older_extractor_is_set_aside(tmp_path):
  store = tmp_path / 's.duckdb'
  _run(_chunks(60)[0], _params(store=str(store), judge=False))
  _age_store(store, '0')
  with pytest.warns(UserWarning, match='started a new store'):
    _run(_chunks(60)[1], _params(store=str(store), judge=False))
  assert (tmp_path / 's.duckdb.v0').exists() and store.exists()
  assert pd.Timestamp(_store_rows(store)['health_meta'].set_index('Key')['Value']['through']) >= START + pd.Timedelta(hours=1)


def test_an_old_store_can_raise_instead(tmp_path):
  store = tmp_path / 's.duckdb'
  _run(_chunks(60)[0], _params(store=str(store), judge=False))
  _age_store(store, '0')
  with pytest.raises(ValueError, match='start a new store'):
    _run(_chunks(60)[1], _params(store=str(store), judge=False, restart_old_store=False))
  assert not (tmp_path / 's.duckdb.v0').exists()

"""The detector_health aggregation on atspm-detector's health: its parameters, one run, what save() writes, and that nothing else changes."""
import builtins
import os
import re

import duckdb
import pandas as pd
import pytest

from src.atspm import SignalDataProcessor

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


def _run(raw, *aggregations, bin_size=15, **kwargs):
  """Load and aggregate; returns every table the connection can see, as save() would, as frames."""
  with SignalDataProcessor(raw_data=raw, bin_size=bin_size, verbose=0, aggregations=list(aggregations), **kwargs) as p:
    p.load()
    p.aggregate()
    names = [r[0] for r in p.conn.sql("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()]
    return {n: p.conn.execute(f'SELECT * FROM "{n}"').df() for n in names if n not in ('raw_data', 'detector_config')}


def _ordered(frame):
  return frame.sort_values(list(frame.columns)).reset_index(drop=True)


def _same(a, b):
  pd.testing.assert_frame_equal(_ordered(a), _ordered(b), check_dtype=False)


def _save_run(raw, aggregation, out, prefix='', output_format='parquet'):
  SignalDataProcessor(raw_data=raw, bin_size=15, verbose=0, output_dir=str(out), output_format=output_format, output_file_prefix=prefix,
                      output_to_separate_folders=False, aggregations=[aggregation]).run()


def _reference(raw, start, end):
  """atspm-detector on its own, over the same window."""
  con = duckdb.connect()
  con.register('events', raw.rename(columns={'TimeStamp': 'Timestamp'}))
  con.register('detectors', DETECTORS)
  return health(con, 'events', 'detectors', start, end, bin_origin='clock', summary=True)


# ---- A1: one run equals atspm-detector on its own

def test_a1_one_shot_equals_the_package_on_its_own():
  out = _run(RAW, _params(findings=True))
  findings, status = _reference(RAW, END - pd.Timedelta(days=3), END)
  assert set(findings['DetectorType']) == {'Vehicle', 'Ped'}
  assert (out['detector_health_findings']['TimeStamp'] == END).all()
  _same(out['detector_health_findings'].drop(columns='TimeStamp'), findings)
  _same(out['detector_health'], bins(duckdb.connect(), findings, status, END - pd.Timedelta(days=3), END))


# ---- the findings table

def test_an_empty_one_shot_has_the_findings_table_too():
  out = _run(RAW.head(0), _params(findings=True))
  assert set(out) == {'detector_health', 'detector_health_findings'}
  assert all(len(frame) == 0 and frame.columns[0] == 'TimeStamp' for frame in out.values())


# ---- A4: what save() writes

@pytest.mark.parametrize('output_format', ['csv', 'parquet', 'json'])
def test_a4_save_writes_only_the_output_tables(tmp_path, output_format):
  SignalDataProcessor(raw_data=RAW, bin_size=15, verbose=0, output_dir=str(tmp_path), output_format=output_format,
                      output_to_separate_folders=False, aggregations=[_params(findings=True)]).run()
  assert sorted(os.listdir(tmp_path)) == [f'detector_health.{output_format}', f'detector_health_findings.{output_format}']


def test_a4_save_writes_state_and_rows_to_their_paths(tmp_path):
  state, rows, out = tmp_path / 'state.parquet', tmp_path / 'rows', tmp_path / 'out'
  chunks = _chunks(15)
  _save_run(chunks[0], _params(state='', history=str(rows)), out, prefix='0_')
  assert sorted(os.listdir(out)) == ['0_detector_health.parquet', '0_health_state.parquet']
  assert len(os.listdir(rows)) == 1
  os.replace(out / '0_health_state.parquet', state)
  before = state.stat().st_mtime_ns
  _save_run(chunks[1], _params(state=str(state), history=str(rows)), out, prefix='1_')
  assert sorted(os.listdir(out)) == ['0_detector_health.parquet', '1_detector_health.parquet']
  assert len(os.listdir(rows)) == 2 and state.stat().st_mtime_ns > before and not (tmp_path / 'state.parquet.tmp').exists()
  assert list(pd.read_parquet(state).columns) == ['TimeStamp', 'DeviceId', 'EventId', 'Parameter', 'Value']


def test_a4_state_through_a_csv_path(tmp_path):
  state, rows, out = tmp_path / 'state.csv', tmp_path / 'rows', tmp_path / 'out'
  chunks = _chunks(15)
  _save_run(chunks[0], _params(state='', history=str(rows)), out, output_format='csv')
  os.replace(out / 'health_state.csv', state)
  _save_run(chunks[1], _params(state=str(state), history=str(rows)), out, output_format='csv')
  assert (pd.read_csv(state)['EventId'] == 9602).sum() == 1


def test_a4_no_store_or_temp_table_stays_behind():
  out = _run(RAW, _params(findings=True))
  assert set(out) == {'detector_health', 'detector_health_findings'}
  for table, frame in out.items():
    assert frame.columns[0] == 'TimeStamp'


# ---- A5: every other aggregation is unaffected, with a state too

OTHERS = [
  {'name': 'has_data', 'params': {}},
  {'name': 'actuations', 'params': {'fill_in_missing': True}},
  {'name': 'arrival_on_green', 'params': {'latency_offset_seconds': 0}},
  {'name': 'yellow_red', 'params': {'latency_offset_seconds': 1.5, 'min_red_offset': -8}},
  {'name': 'timeline', 'params': {'min_duration': 0.2, 'cushion_time': 60}},
  {'name': 'phase_wait', 'params': {'preempt_recovery_seconds': 120, 'assumed_cycle_length': 140, 'skip_multiplier': 1.5}},
  {'name': 'coordination_agg', 'params': {}},
]


def _incremental_outputs(folder, health=False):
  """The incremental runs, unmatched events carried from file to file: every file they wrote but detector health's."""
  folder.mkdir()
  unmatched = str(folder / 'unmatched.parquet')
  state = str(folder.parent / 'health_state.parquet')
  for i, chunk in enumerate(_chunks(30)):
    extra = [_params(state=state if os.path.exists(state) else '', history=str(folder.parent / 'rows'))] if health else []
    aggregations = [dict(a, params=dict(a['params'])) for a in OTHERS] + extra
    aggregations[1]['params'].update(known_detectors_max_days_old=2)
    SignalDataProcessor(raw_data=chunk, detector_config=CONFIG, bin_size=15, verbose=0, output_dir=str(folder),
                        output_format='parquet', output_to_separate_folders=False, output_file_prefix=f'{i}_',
                        unmatched_event_settings={'df_or_path': unmatched, 'max_days_old': 14, 'first_run': True},
                        aggregations=aggregations).run()
    # each run reads its state back, so a difference here reaches the next run's outputs as well
    assert os.path.exists(unmatched)
    pd.read_parquet(unmatched).to_parquet(folder / f'{i}_unmatched_copy.parquet')
    if health and i == 0:
      os.replace(folder / '0_health_state.parquet', state)
  return {f: pd.read_parquet(folder / f) for f in os.listdir(folder) if 'health' not in f}


def test_a5_other_outputs_are_identical_with_and_without_detector_health(tmp_path):
  without = _incremental_outputs(tmp_path / 'without')
  with_health = _incremental_outputs(tmp_path / 'with', health=True)
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
  ({'state': ''}, 'needs history'),
  ({'clock_ahead_minutes': -1}, 'clock_ahead_minutes'),
  ({'outputs': ['weekly']}, 'unknown parameters'),
  ({'outputs': '15min'}, 'unknown parameters'),
])
def test_a6_bad_parameters(params, message):
  with pytest.raises(ValueError, match=message):
    SignalDataProcessor(raw_data=RAW, aggregations=[{'name': 'detector_health', 'params': params}])


@pytest.mark.parametrize('old, replacement', [('judge', 'first run past midnight'), ('store', "'state'"), ('restart_old_store', "'state': ''")])
def test_a6_replaced_parameters_name_the_replacement(old, replacement):
  with pytest.raises(ValueError, match=f'{old} is gone.*{replacement}'):
    SignalDataProcessor(raw_data=RAW, aggregations=[{'name': 'detector_health', 'params': {old: 'x'}}])


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

def test_a8_a_failed_run_leaves_nothing_on_the_connection_and_its_retry_equals_a_clean_run(tmp_path, monkeypatch):
  chunks = _chunks(60)
  history = str(tmp_path / 'rows')
  first = _run(chunks[0], _params(state='', history=history))['health_state']
  with monkeypatch.context() as m:
    def fail(*args, **kwargs):
      raise RuntimeError('out of disk')
    m.setattr('atspm_detector.live._live_step', fail)
    with SignalDataProcessor(raw_data=chunks[1], bin_size=15, verbose=0, aggregations=[_params(state=first, history=history)]) as p:
      p.load()
      with pytest.raises(RuntimeError, match='out of disk'):
        p.aggregate()
      assert not p.conn.execute("SELECT 1 FROM duckdb_databases() WHERE database_name = 'health_store'").fetchall()
      assert not p.conn.execute("SELECT 1 FROM duckdb_tables() WHERE table_name = 'health_detectors' OR starts_with(table_name, 'hl_')").fetchall()
  retry = _run(chunks[1], _params(state=first, history=history))
  clean = _run(chunks[1], _params(state=first, history=history))
  _same(retry['detector_health'], clean['detector_health'])
  _same(retry['health_state'].query('EventId != 9605'), clean['health_state'].query('EventId != 9605'))


# ---- one run: every update passes upto, and the log shows what it held back

def test_a_wrong_clock_is_held_back_and_logged_and_does_not_move_the_others(capsys):
  wrong = RAW[RAW['DeviceId'] == 454].assign(TimeStamp=lambda f: f['TimeStamp'] + pd.DateOffset(years=1))
  others = RAW[RAW['DeviceId'] != 454]
  with SignalDataProcessor(raw_data=pd.concat([others, wrong]), bin_size=15, verbose=1,
                           aggregations=[_params(findings=True)]) as p:
    p.load()
    p.aggregate()
    bad = p.conn.execute("SELECT * FROM detector_health_findings").df()
  assert re.search(f"signal 454: [0-9]+ events stamped at or after {END}", capsys.readouterr().out)
  assert (bad['TimeStamp'] == END).all()
  _same(bad, _run(others, _params(findings=True))['detector_health_findings'])


def test_a_clock_a_little_ahead_keeps_its_rows(capsys):
  chunk = _chunks(15)[0]
  ahead = chunk.assign(TimeStamp=chunk['TimeStamp'].where(chunk['DeviceId'] != 454, chunk['TimeStamp'] + pd.Timedelta(minutes=5)))
  with SignalDataProcessor(raw_data=ahead, bin_size=15, verbose=1,
                           aggregations=[_params(findings=True)]) as p:
    p.load()
    p.aggregate()
    judged = p.conn.execute("SELECT DISTINCT TimeStamp FROM detector_health_findings").fetchall()
  assert 'events stamped' not in capsys.readouterr().out
  assert judged == [(pd.Timestamp('2024-05-13 15:30:00').to_pydatetime(),)]


# ---- an empty run and a state

EMPTY = RAW.head(0)


def test_an_empty_one_shot_has_the_tables_without_rows():
  out = _run(EMPTY, _params(findings=True))
  assert list(out['detector_health'].columns) == ['TimeStamp', 'DeviceId', 'DetectorType', 'Detector', 'Label', 'Score']
  assert list(out['detector_health_findings'].columns) == ['TimeStamp', 'DeviceId', 'DetectorType', 'Detector', 'Start', 'End',
                                                           'Label', 'Confidence', 'Detail']
  assert len(out['detector_health']) == 0 and len(out['detector_health_findings']) == 0


def test_an_empty_run_keeps_the_state_and_writes_all_columns(tmp_path):
  history = str(tmp_path / 'rows')
  first = _run(_chunks(15)[0], _params(state='', history=history))
  out = _run(EMPTY, _params(state=first['health_state'], history=history))
  assert set(out) == {'detector_health', 'health_state', 'health_rows'}
  assert len(out['detector_health']) == 0 and len(out['health_rows']) == 0
  assert list(out['health_rows'].columns) == list(first['health_rows'].columns) and len(first['health_rows'].columns) == 50
  _same(out['health_state'], first['health_state'])


def test_a_missing_state_path_raises_and_empty_text_is_a_first_run(tmp_path):
  gone = str(tmp_path / 'gone.parquet')
  for raw in (_chunks(15)[0], EMPTY):
    with pytest.raises(FileNotFoundError, match="Pass state '' only for a first run"):
      _run(raw, _params(state=gone, history=str(tmp_path / 'rows')))
  assert set(_run(_chunks(15)[0], _params(state='', history=str(tmp_path / 'rows')))) == {'detector_health', 'health_state', 'health_rows'}


def test_a_state_needs_15_minute_bins(tmp_path):
  with pytest.raises(ValueError, match='15-minute bins'):
    _run(_chunks(15)[0], _params(state='', history=str(tmp_path / 'rows')), bin_size=60)

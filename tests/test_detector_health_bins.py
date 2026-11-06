"""detector_health as bins: with a state one run equals many at any cuts, each run its own bin, a whole-day check at the first run past midnight, each bin once, a stuck ON outlives the window."""
import os

import duckdb
import numpy as np
import pandas as pd
import pytest

from src.atspm import SignalDataProcessor

from atspm_detector import incremental as H

COLUMNS = ['TimeStamp', 'DeviceId', 'DetectorType', 'Detector', 'Label', 'Score']
KEY = ['TimeStamp', 'DeviceId', 'DetectorType', 'Detector', 'Label']
SHIFT = pd.Timedelta(days=883)      # the bundled log, stamped in October 2026
RAW = duckdb.query("select * from 'tests/hires_test_data.parquet'").df().assign(TimeStamp=lambda f: f['TimeStamp'] + SHIFT)
CONFIG = duckdb.query("select * from 'tests/configs_test_data.parquet'").df()
START, END = pd.Timestamp('2024-05-13 15:00:00') + SHIFT, pd.Timestamp('2024-05-13 18:00:00') + SHIFT

T0 = pd.Timestamp('2026-09-28 00:00:00')
ON, OFF = T0 + pd.Timedelta(days=1, hours=7), T0 + pd.Timedelta(days=2, hours=10, minutes=7, seconds=30)
DETECTORS = pd.DataFrame({'DeviceId': [1], 'Detector': [9], 'phase': [2], 'function': ['Presence'], 'lanes': [1]})


def _config_detectors():
  with SignalDataProcessor(detector_config=CONFIG, raw_data=RAW.head(1), verbose=0, aggregations=[]) as p:
    p.load()
    return p.conn.execute("""
      SELECT DeviceId, Parameter AS Detector, min(Phase) AS phase,
             CASE arg_min(Function, Phase) WHEN 'Stopbar Count' THEN 'Count' ELSE arg_min(Function, Phase) END AS function,
             1 AS lanes
      FROM detector_config GROUP BY 1, 2""").df()


REAL_DETECTORS = _config_detectors()


def _stuck_raw(days=3):
  """A signal cycling every 200 s, detector 9 pulsed each cycle, ped input 2 ON from ON to OFF with phase 2 walking every cycle."""
  rows = []
  for t in pd.date_range(T0, T0 + pd.Timedelta(days=days), freq='200s', inclusive='left'):
    rows += [(t, 1, 2), (t + pd.Timedelta(seconds=60), 81, 3)]
    if ON < t < OFF:
      rows += [(t + pd.Timedelta(seconds=1), 21, 2), (t + pd.Timedelta(seconds=8), 22, 2), (t + pd.Timedelta(seconds=21), 23, 2)]
    rows += [(t + pd.Timedelta(seconds=30), 82, 9), (t + pd.Timedelta(seconds=32), 81, 9)]
  rows += [(ON, 90, 2), (ON + pd.Timedelta(seconds=0.1), 45, 2), (OFF, 89, 2)]
  frame = pd.DataFrame(rows, columns=['TimeStamp', 'EventId', 'Parameter']).assign(DeviceId=1)
  return frame[['TimeStamp', 'DeviceId', 'EventId', 'Parameter']].astype({'EventId': 'int16', 'Parameter': 'int16'})


def _params(detectors=None, **health_params):
  return {'name': 'detector_health', 'params': {'detectors': REAL_DETECTORS if detectors is None else detectors, **health_params}}


def _run(raw, *aggregations, bin_size=15, **kwargs):
  with SignalDataProcessor(raw_data=raw, bin_size=bin_size, verbose=0, aggregations=list(aggregations), **kwargs) as p:
    p.load()
    p.aggregate()
    names = [r[0] for r in p.conn.sql("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()]
    return {n: p.conn.execute(f'SELECT * FROM "{n}"').df() for n in names if n not in ('raw_data', 'detector_config')}


def _ordered(frame):
  return frame.sort_values(COLUMNS).reset_index(drop=True)


def _runs(raw, cuts, detectors, tmp_path, name, **health_params):
  """Runs of raw cut at the times given, each carrying only the last run's state and the saved rows: the tables of each run."""
  edges = [raw['TimeStamp'].min()] + sorted(cuts) + [raw['TimeStamp'].max() + pd.Timedelta(seconds=1)]
  rows, state, out = tmp_path / name, '', []
  rows.mkdir()
  for i, (a, b) in enumerate(zip(edges[:-1], edges[1:])):
    got = _run(raw[(raw['TimeStamp'] >= a) & (raw['TimeStamp'] < b)], _params(detectors, state=state, history=str(rows), **health_params))
    if len(got['health_rows']):
      got['health_rows'].to_parquet(rows / f'r{i:04d}.parquet')
    state = got['health_state']
    out.append(got)
  return out


def _all_rows(tables):
  return pd.concat([t['detector_health'] for t in tables], ignore_index=True)



# ---- the table

def test_the_table_has_the_six_columns_in_bins_and_nothing_else_is_written():
  out = _run(RAW, _params())
  assert set(out) == {'detector_health'}
  b = out['detector_health']
  assert list(b.columns) == COLUMNS and len(b) > 0
  assert (b['TimeStamp'].dt.minute % 15 == 0).all() and b['TimeStamp'].between(START - pd.Timedelta(days=3), END, inclusive='left').all()
  assert not b.duplicated(KEY).any()
  assert b.groupby(['TimeStamp', 'DeviceId', 'DetectorType', 'Detector'])['Score'].sum().max() <= 1 + 1e-5


def test_the_findings_table_only_when_asked():
  out = _run(RAW, _params(findings=True))
  assert set(out) == {'detector_health', 'detector_health_findings'}
  assert list(out['detector_health_findings'].columns) == ['TimeStamp', 'DeviceId', 'DetectorType', 'Detector', 'Start', 'End', 'Label', 'Confidence', 'Detail']
  assert (out['detector_health_findings']['TimeStamp'] == END).all()


@pytest.mark.parametrize('old', ['summary', 'outputs'])
def test_the_old_table_choices_are_gone(old):
  with pytest.raises(ValueError, match='unknown parameters'):
    SignalDataProcessor(raw_data=RAW, aggregations=[{'name': 'detector_health', 'params': {old: True}}])


def test_an_empty_score_stays_empty_in_the_table():
  """A signal with almost nothing to judge: not enough data is a row with no Score, not a zero or a NaN."""
  few = _stuck_raw(1)
  few = few[(few['EventId'] != 82) | (few['TimeStamp'] < T0 + pd.Timedelta(hours=1))]
  few = few[few['TimeStamp'] < T0 + pd.Timedelta(hours=1)]
  with SignalDataProcessor(raw_data=few, bin_size=15, verbose=0, aggregations=[_params(DETECTORS)]) as p:
    p.load()
    p.aggregate()
    rows = p.conn.execute("SELECT Label, Score, isnan(Score) FROM detector_health").fetchall()
  assert all(s is None or not nan for _, s, nan in rows)


def test_an_empty_run_has_the_table_without_rows():
  out = _run(RAW.head(0), _params(findings=True))
  assert set(out) == {'detector_health', 'detector_health_findings'} and all(len(f) == 0 for f in out.values())
  assert list(out['detector_health'].columns) == COLUMNS


@pytest.mark.parametrize('output_format', ['csv', 'parquet'])
def test_save_writes_the_tables(tmp_path, output_format):
  SignalDataProcessor(raw_data=RAW, bin_size=15, verbose=0, output_dir=str(tmp_path), output_format=output_format,
                      output_to_separate_folders=False, aggregations=[_params(findings=True)]).run()
  assert sorted(os.listdir(tmp_path)) == [f'detector_health.{output_format}', f'detector_health_findings.{output_format}']


def test_bin_size_is_the_processors():
  b = _run(RAW, _params(), bin_size=60)['detector_health']
  assert (b['TimeStamp'].dt.minute == 0).all()
  fine = _run(RAW, _params())['detector_health']
  hours = lambda f, n: f['Score'].sum() * n / 60
  assert hours(b, 60) == pytest.approx(hours(fine, 15), rel=0.01)


# ---- a state: one run and many runs

MIDNIGHT = T0 + pd.Timedelta(days=1)


@pytest.fixture(scope='module')
def two_days():
  raw = _stuck_raw(3)
  return raw[raw['TimeStamp'] < MIDNIGHT + pd.Timedelta(minutes=45)]



def test_each_run_its_own_bin_and_late_finds_only_inside_the_previous_day(two_days, tmp_path):
  cuts = [MIDNIGHT - pd.Timedelta(minutes=15), MIDNIGHT, MIDNIGHT + pd.Timedelta(minutes=15)]
  tables = _runs(two_days, cuts, DETECTORS, tmp_path, 'r', findings=True)
  edges = [two_days['TimeStamp'].min()] + cuts
  checks = []
  for a, t in zip(edges, tables):
    rows = t['detector_health']
    if a > edges[0] and rows['TimeStamp'].lt(a - pd.Timedelta(minutes=15)).any():
      late = rows[rows['TimeStamp'] < a - pd.Timedelta(minutes=15)]
      assert late['TimeStamp'].ge(MIDNIGHT - pd.Timedelta(days=1)).all() and late['TimeStamp'].lt(MIDNIGHT).all()
      checks.append(a)
  assert len(checks) == 1 and checks[0] >= MIDNIGHT
  assert [('detector_health_findings' in t) for t in tables].count(True) == 1     # only the run past midnight
  assert not _all_rows(tables).duplicated(KEY).any()


def test_the_first_runs_write_nothing_until_the_first_check(two_days, tmp_path):
  first = two_days[two_days['TimeStamp'] < T0 + pd.Timedelta(hours=6)]
  got = _run(first, _params(DETECTORS, state='', history=str(tmp_path / 'rows')))
  assert set(got) == {'detector_health', 'health_state', 'health_rows'} and len(got['detector_health']) == 0


def test_a_retry_from_the_last_state_writes_the_same_rows_again(two_days, tmp_path):
  cut = MIDNIGHT - pd.Timedelta(minutes=15)
  rows = tmp_path / 'rows'
  rows.mkdir()
  before = _run(two_days[two_days['TimeStamp'] < cut], _params(DETECTORS, state='', history=str(rows)))
  before['health_rows'].to_parquet(rows / 'before.parquet')
  last = two_days[two_days['TimeStamp'] >= cut]
  tries = [_run(last, _params(DETECTORS, state=before['health_state'], history=str(rows)))['detector_health'] for _ in range(2)]
  assert len(tries[0]) > 0
  pd.testing.assert_frame_equal(_ordered(tries[0]), _ordered(tries[1]), check_dtype=False)


def test_a_listed_run_missing_from_history_raises_before_the_check(two_days, tmp_path):
  cut = MIDNIGHT - pd.Timedelta(minutes=15)
  before = _run(two_days[two_days['TimeStamp'] < cut], _params(DETECTORS, state='', history=str(tmp_path / 'rows')))
  with pytest.raises(ValueError, match='lacks the rows'):
    _run(two_days[two_days['TimeStamp'] >= cut], _params(DETECTORS, state=before['health_state'], history=str(tmp_path / 'rows')))


# ---- a stuck ON outlives the window

def _stuck_rows(frame, kind, detector):
  return frame[(frame['DetectorType'] == kind) & (frame['Detector'] == detector) & (frame['Label'] == 'stuck on')].sort_values('TimeStamp')


def test_a_button_stuck_past_the_window_keeps_its_bins_until_the_off(tmp_path):
  cuts = [T0 + pd.Timedelta(days=k, minutes=k * 7) for k in range(1, 3)]  # daily runs
  bins = _all_rows(_runs(_stuck_raw(), cuts, DETECTORS, tmp_path, 'r', horizon_days=1))
  assert not bins.duplicated(KEY).any()
  rows = _stuck_rows(bins, 'Ped', 2)
  assert rows['TimeStamp'].iloc[0] == ON and rows['TimeStamp'].iloc[-1] == OFF.floor('15min')
  assert rows['TimeStamp'].diff().dropna().eq(pd.Timedelta(minutes=15)).all()
  assert rows['Score'].sum() / 4 == pytest.approx((OFF - ON) / pd.Timedelta(hours=1), rel=0.01)


def test_the_open_on_is_in_the_state_not_in_the_unmatched_events_file(tmp_path):
  raw = _stuck_raw(3)
  rows, state, unmatched = tmp_path / 'rows', tmp_path / 'state.parquet', str(tmp_path / 'unmatched.parquet')
  rows.mkdir()
  edges = [T0, T0 + pd.Timedelta(days=1), T0 + pd.Timedelta(days=2), T0 + pd.Timedelta(days=2, hours=6)]  # the last run ends before the OFF
  for a, b in zip(edges[:-1], edges[1:]):
    chunk = raw[(raw['TimeStamp'] >= a) & (raw['TimeStamp'] < b)]
    params = _params(DETECTORS, state=str(state) if state.exists() else '', history=str(rows), horizon_days=1)
    SignalDataProcessor(raw_data=chunk, detector_config=None, bin_size=15, verbose=0, output_dir=str(tmp_path / 'out'), output_format='parquet',
                        output_to_separate_folders=False, unmatched_event_settings={'df_or_path': unmatched, 'max_days_old': 1, 'first_run': True},
                        aggregations=[params, {'name': 'ped', 'params': {}}]).run()
    if not state.exists():
      os.replace(tmp_path / 'out' / 'health_state.parquet', state)
  carried = pd.read_parquet(state).query('EventId == 9232')
  assert [(t, p // 1000) for t, p in zip(carried['TimeStamp'], carried['Parameter'])] == [(ON, 2)]
  assert not pd.read_parquet(unmatched)['EventId'].between(9200, 9499).any()

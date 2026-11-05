"""detector_health as bins: one run equals many at any cuts, each bin once, past rows never change, a stuck ON outlives the window, the findings table."""
import os

import duckdb
import numpy as np
import pandas as pd
import pytest

from src.atspm import SignalDataProcessor

H = pytest.importorskip('atspm_detector.incremental', exc_type=ImportError)

COLUMNS = ['TimeStamp', 'DeviceId', 'DetectorType', 'Detector', 'Label', 'Score']
KEY = ['TimeStamp', 'DeviceId', 'DetectorType', 'Detector', 'Label']
SHIFT = pd.Timedelta(days=883)      # the bundled log, stamped in October 2026
RAW = duckdb.query("select * from 'tests/hires_test_data.parquet'").df().assign(TimeStamp=lambda f: f['TimeStamp'] + SHIFT)
CONFIG = duckdb.query("select * from 'tests/configs_test_data.parquet'").df()
START, END = pd.Timestamp('2024-05-13 15:00:00') + SHIFT, pd.Timestamp('2024-05-13 18:00:00') + SHIFT

T0 = pd.Timestamp('2026-09-28 00:00:00')
ON, OFF = T0 + pd.Timedelta(days=1, hours=7), T0 + pd.Timedelta(days=5, hours=3, minutes=7, seconds=30)
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


def _stuck_raw(days=7):
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


def _runs(raw, cuts, *args, **health_params):
  """Runs of raw cut at the times given, on one store: the rows of detector_health of every run that wrote it, each in the order written."""
  edges = [raw['TimeStamp'].min() - pd.Timedelta(seconds=1)] + sorted(cuts) + [raw['TimeStamp'].max() + pd.Timedelta(seconds=1)]
  out = []
  for a, b in zip(edges[:-1], edges[1:]):
    chunk = raw[(raw['TimeStamp'] > a) & (raw['TimeStamp'] <= b)]
    if len(chunk):
      got = _run(chunk, _params(*args, **health_params)).get('detector_health')
      if got is not None:
        out.append(got)
  return out


def _random_cuts(raw, n, seed):
  rng = np.random.default_rng(seed)
  a, b = raw['TimeStamp'].min(), raw['TimeStamp'].max()
  return [a + (b - a) * x for x in rng.random(n)]


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


def test_a_run_that_reaches_no_verdict_writes_nothing(tmp_path):
  out = tmp_path / 'out'
  SignalDataProcessor(raw_data=RAW[RAW['TimeStamp'] < START + pd.Timedelta(minutes=15)], bin_size=15, verbose=0, output_dir=str(out), output_format='parquet',
                      output_to_separate_folders=False, aggregations=[_params(store=str(tmp_path / 's.duckdb'))]).run()
  assert os.listdir(out) == []


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


# ---- one run and many runs

@pytest.mark.parametrize('seed', [1, 2, 3])
def test_hourly_one_long_run_equals_runs_at_random_cuts(tmp_path, seed):
  whole = _runs(RAW, [], store=str(tmp_path / 'whole.duckdb'), judge='hourly')
  assert len(whole) == 1 and len(whole[0]) > 0 and whole[0]['TimeStamp'].max() == END - pd.Timedelta(minutes=15)
  cut = _runs(RAW, _random_cuts(RAW, 9, seed), store=str(tmp_path / 'cut.duckdb'), judge='hourly')
  pd.testing.assert_frame_equal(_ordered(pd.concat(cut)), _ordered(whole[0]), check_dtype=False)


def test_no_store_gives_the_same_bins_as_a_store(tmp_path):
  store = _runs(RAW, [], store=str(tmp_path / 'whole.duckdb'), judge='hourly')[0]
  one_shot = _run(RAW, _params(judge='hourly'))['detector_health']
  pd.testing.assert_frame_equal(_ordered(one_shot), _ordered(store), check_dtype=False)


def test_a_run_short_of_the_verdict_time_leaves_it_for_the_next_run(tmp_path):
  store = str(tmp_path / 's.duckdb')
  short = RAW[RAW['TimeStamp'] < START + pd.Timedelta(minutes=58)]
  assert 'detector_health' not in _run(short, _params(store=store, judge='hourly'))
  rest = RAW[(RAW['TimeStamp'] >= START + pd.Timedelta(minutes=58)) & (RAW['TimeStamp'] < START + pd.Timedelta(minutes=70))]
  assert _run(rest, _params(store=store, judge='hourly'))['detector_health']['TimeStamp'].max() == START + pd.Timedelta(minutes=45)


def test_each_run_writes_only_new_bins_and_past_rows_never_change(tmp_path):
  cuts = [START + pd.Timedelta(hours=1, minutes=5), START + pd.Timedelta(hours=2, minutes=5)]
  parts = _runs(RAW, cuts, store=str(tmp_path / 's.duckdb'), judge='hourly')
  assert len(parts) >= 2
  for earlier, later in zip(parts[:-1], parts[1:]):
    assert earlier['TimeStamp'].max() < later['TimeStamp'].min()


def test_a_second_run_of_the_same_events_writes_nothing_new(tmp_path):
  store = str(tmp_path / 's.duckdb')
  first = _run(RAW, _params(store=store, judge='hourly'))['detector_health']
  again = _run(RAW, _params(store=store, judge='hourly'))
  assert len(first) > 0 and ('detector_health' not in again or len(again['detector_health']) == 0)


# ---- a stuck ON outlives the window

def _stuck_rows(frame, kind, detector):
  return frame[(frame['DetectorType'] == kind) & (frame['Detector'] == detector) & (frame['Label'] == 'stuck on')].sort_values('TimeStamp')


@pytest.mark.parametrize('cuts', [[], [T0 + pd.Timedelta(days=k, minutes=k * 7) for k in range(1, 7)]], ids=['one_run', 'daily_runs'])
def test_a_button_stuck_past_the_window_keeps_its_bins_until_the_off(tmp_path, cuts):
  parts = _runs(_stuck_raw(), cuts, DETECTORS, store=str(tmp_path / 's.duckdb'), judge='daily', horizon_days=1)
  bins = pd.concat(parts, ignore_index=True)
  assert not bins.duplicated(KEY).any()
  rows = _stuck_rows(bins, 'Ped', 2)
  assert rows['TimeStamp'].iloc[0] == ON and rows['TimeStamp'].iloc[-1] == OFF.floor('15min')
  assert rows['TimeStamp'].diff().dropna().eq(pd.Timedelta(minutes=15)).all()
  assert rows['Score'].sum() / 4 == pytest.approx((OFF - ON) / pd.Timedelta(hours=1))


def test_the_open_on_is_in_the_store_not_in_the_unmatched_events_file(tmp_path):
  raw = _stuck_raw(4)
  store, unmatched = str(tmp_path / 's.duckdb'), str(tmp_path / 'unmatched.parquet')
  for k in range(1, 4):
    chunk = raw[(raw['TimeStamp'] >= T0 + pd.Timedelta(days=k - 1)) & (raw['TimeStamp'] < T0 + pd.Timedelta(days=k))]
    SignalDataProcessor(raw_data=chunk, detector_config=None, bin_size=15, verbose=0, output_dir=str(tmp_path / 'out'), output_format='parquet',
                        output_to_separate_folders=False, unmatched_event_settings={'df_or_path': unmatched, 'max_days_old': 1},
                        aggregations=[_params(DETECTORS, store=store, horizon_days=1, judge='daily'), {'name': 'ped', 'params': {}}]).run()
  con = duckdb.connect(store, read_only=True)
  carried = con.execute("SELECT TimeStamp, Parameter FROM health_carry WHERE EventId = 9232").fetchall()
  con.close()
  assert [(t, p // 1000) for t, p in carried] == [(ON.to_pydatetime(), 2)]
  assert not pd.read_parquet(unmatched)['EventId'].between(9200, 9499).any()

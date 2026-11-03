"""TEMPORARY, to be deleted when atspm-detector's incremental judge gives the findings and status tables itself.

Until then these wrap what the module already has: the store, the update and the judge are real, and the tables come from
atspm_detector.findings. Each wrap switches itself off as soon as the real parameter exists."""
import inspect
from types import SimpleNamespace

import pandas as pd
from atspm_detector import findings
from atspm_detector import health_duckdb as HD
from atspm_detector import incremental as H


def _windows(con, detectors, store, start, end):
  """Each signal's judged window, by the judge's own rules."""
  H._judge_plan(con, detectors, store, start, end, HD._options(None), False, None)
  try:
    return con.execute(f"""
      SELECT DeviceId, make_timestamp((s_ns::BIGINT - s_ns::BIGINT % {HD.NSQ}) // 1000) AS "start",
             make_timestamp(e_ns::BIGINT // 1000) AS "end" FROM {HD.PREFIX}wall""").df()
  finally:
    con.execute(f"DROP TABLE IF EXISTS {HD.PREFIX}wall")


def install(monkeypatch):
  real_judge = H.judge
  if 'summary' not in inspect.signature(real_judge).parameters:
    def judge(con, detectors, *, store, end, options=None, ped=True, ped_options=None, ped_assign=False, workers=1,
              summary=False):
      r = real_judge(con, detectors, store=store, end=end, options=options, ped=ped, ped_options=ped_options,
                     ped_assign=ped_assign)
      horizon = pd.Timedelta(days=float(H._meta(con, store)['horizon_days']))
      start = pd.Timestamp(end) - horizon
      with H._transaction(con):
        H._set_meta(con, store, last_judge_end=pd.Timestamp(end).isoformat())
      win = _windows(con, detectors, store, start, end)
      pin = findings.event_windows(con, f'{store}.health_ped_events', start=start, end=end, ped=True) if ped else None
      out = findings.tables(con, r.detectors, r.periods, r.ped, r.ped_periods, win, pin, summary=True)
      return SimpleNamespace(findings=out[0], status=out[1] if summary else None)
    monkeypatch.setattr(H, 'judge', judge)

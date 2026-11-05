"""The detector_health aggregation: atspm-detector's vehicle and ped health in time bins, kept up to date from each run's raw_data.

Each run adds its events to a health store (a DuckDB file, or a store in memory for a one-shot run). Verdicts fall on the
clock (each midnight, or each hour), wherever the runs are cut: a run cuts its events at each verdict's time, judges the store
over the last horizon_days there, and writes the bins released since the verdict before, each bin once. The store is
detached and every temporary table dropped before save(), which writes every table it can see."""
import os
import warnings

import pandas as pd

from .data_loader import _quote_path, _strip_wrapping_quotes
from .utils import v_print

STORE = 'health_store'
DETECTORS = 'health_detectors'
CADENCES = ('daily', 'hourly', 'every_run', False)
DEFAULTS = {
    'store': None,                  # path of the store file; None: one run over this run's raw_data, always judged
    'detectors': None,              # DataFrame or path: DeviceId, Detector, phase, function, lanes [, p_*]
                                    # [, configured_function]; None: detector_config, as the agency set it
    'horizon_days': 3,              # how far back a verdict looks, and how much the store keeps
    'judge': 'daily',               # 'daily' | 'hourly' | 'every_run' | False (only add to the store)
    'findings': False,              # also write detector_health_findings: the findings of the run's last verdict
    'ped': True,
    'ped_assign': False,            # ped movement model (onnxruntime)
    'options': None,                # atspm-detector vehicle health overrides
    'ped_options': None,            # atspm-detector ped health overrides
    'workers': 1,
    'clock_ahead_minutes': 60,      # a signal this far past the typical signal's last event has a wrong clock
    'restart_old_store': True,      # a store from an older extractor is renamed <file>.v<N>; False raises instead
}
REMOVED = ('data', 'device_groups', 'decompose_params', 'anomaly_params', 'return_last_n_days')
BINS = 'TimeStamp TIMESTAMP, DeviceId {device}, DetectorType VARCHAR, Detector INTEGER, Label VARCHAR, Score DOUBLE'
FINDINGS = ('TimeStamp TIMESTAMP, DeviceId {device}, DetectorType VARCHAR, Detector INTEGER, Start TIMESTAMP, '
            '"End" TIMESTAMP, Label VARCHAR, Confidence DOUBLE, Detail VARCHAR')


def detector_health_params(params):
    """The parameters with the defaults filled in, checked before any work starts."""
    removed = [k for k in REMOVED if k in params]
    if removed:
        raise ValueError(f"detector_health parameters {removed} no longer exist: traffic-anomaly was removed in atspm "
                         "3.0.0 and detector_health is now atspm-detector's health. See the README.")
    unknown = sorted(set(params) - set(DEFAULTS))
    if unknown:
        raise ValueError(f"detector_health: unknown parameters {unknown}; it takes {sorted(DEFAULTS)}")
    full = {**DEFAULTS, **params}
    if full['judge'] not in CADENCES:
        raise ValueError(f"detector_health judge must be one of {CADENCES}, not {full['judge']!r}")
    if full['store'] is None and not full['judge']:
        raise ValueError("detector_health: judge=False with no store keeps nothing; give a store path")
    if not isinstance(full['clock_ahead_minutes'], int) or full['clock_ahead_minutes'] < 0:
        raise ValueError("detector_health clock_ahead_minutes must be a whole number of minutes, 0 or more")
    return full


def _load_package():
    try:
        from atspm_detector import incremental
    except ImportError as e:
        raise ImportError("detector_health needs atspm-detector with incremental health: "
                          "pip install atspm[detector-health]") from e
    return incremental


def _has_table(conn, name):
    return conn.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [name]).fetchall()[0][0] > 0


def _register_detectors(conn, detectors):
    """The temp table the health code reads: the given detectors, else detector_config in the same columns."""
    if detectors is None:
        if not _has_table(conn, 'detector_config'):
            raise ValueError("detector_health needs a 'detectors' parameter or a detector_config")
        # A detector with several rows takes its lowest phase
        conn.execute(f"""
            CREATE OR REPLACE TEMP TABLE {DETECTORS} AS
            SELECT DeviceId, Detector, phase,
                   CASE configured_function WHEN 'Stopbar Count' THEN 'Count' ELSE configured_function END AS function,
                   1 AS lanes, configured_function
            FROM (SELECT DeviceId, Parameter AS Detector, min(Phase) AS phase,
                         arg_min(Function, (Phase, Function)) AS configured_function
                  FROM detector_config GROUP BY DeviceId, Parameter)
        """)
        return
    if isinstance(detectors, str):
        source = _quote_path(_strip_wrapping_quotes(detectors))
        conn.execute(f"CREATE OR REPLACE TEMP TABLE {DETECTORS} AS SELECT * FROM {source}")
    else:
        conn.register(f'{DETECTORS}_input', detectors)
        conn.execute(f"CREATE OR REPLACE TEMP TABLE {DETECTORS} AS SELECT * FROM {DETECTORS}_input")
        conn.unregister(f'{DETECTORS}_input')
    columns = {r[0].lower() for r in conn.execute(f"DESCRIBE SELECT * FROM {DETECTORS}").fetchall()}
    missing = [c for c in ('DeviceId', 'Detector', 'phase', 'function', 'lanes') if c.lower() not in columns]
    if missing:
        raise ValueError(f"detector_health detectors table is missing columns {missing}")


def _attached(conn):
    return len(conn.execute("SELECT 1 FROM duckdb_databases() WHERE database_name = ?", [STORE]).fetchall()) > 0


def _attach_store(H, conn, params):
    """Attach the store. A store from an older extractor holds rows this code cannot read, so it is set aside."""
    def attach():
        H.attach_store(conn, params['store'], name=STORE, horizon_days=params['horizon_days'], options=params['options'],
                       ped=params['ped'])
    try:
        attach()
    except ValueError:
        old = _store_meta(conn).get('extractor_version') if _attached(conn) else None
        if params['store'] is None or not params['restart_old_store'] or old in (None, H.EXTRACTOR_VERSION):
            raise
        H.detach_store(conn, STORE)
        os.replace(params['store'], f"{params['store']}.v{old}")
        warnings.warn(f"detector_health: {params['store']} was written by extractor version {old}, this package has "
                      f"{H.EXTRACTOR_VERSION}. Renamed it {params['store']}.v{old} and started a new store.")
        attach()


def _store_meta(conn):
    return dict(conn.execute(f"SELECT Key, Value FROM {STORE}.health_meta").fetchall())


def _covered_until(conn, clock_ahead_minutes):
    """The end of the run's last 15-minute bin (None for an empty run). Signals more than clock_ahead_minutes past the
    typical signal's last event do not count: one wrong clock must not move the time the whole store works to."""
    last = conn.execute(f"""
        WITH last AS (SELECT DeviceId, max(TimeStamp) AS t FROM raw_data GROUP BY DeviceId)
        SELECT max(t) FROM last
        WHERE t <= (SELECT quantile_disc(t, 0.5) FROM last) + INTERVAL ({clock_ahead_minutes}) MINUTE
    """).fetchall()[0][0]
    return None if last is None else _next_quarter(pd.Timestamp(last))


def _next_quarter(t):
    return t.floor('15min') + pd.Timedelta(minutes=15)


def _typical_events(conn):
    """The typical signal's first and last event of the run, as one wrong clock must not move them."""
    first, last = conn.execute("""
        SELECT quantile_disc(a, 0.5), quantile_disc(z, 0.5)
        FROM (SELECT min(TimeStamp) AS a, max(TimeStamp) AS z FROM raw_data GROUP BY DeviceId)
    """).fetchall()[0]
    return pd.Timestamp(first), pd.Timestamp(last)


def _judge_due(cadence, end, meta):
    """Whether this run reaches a new day or hour, measured from the last judge."""
    if not cadence:
        return False
    if cadence == 'every_run':
        return True
    unit = 'D' if cadence == 'daily' else 'h'
    last = meta.get('last_judge_end', meta.get('through'))
    if last is None:
        return end == end.floor(unit)
    return end.floor(unit) > pd.Timestamp(last).floor(unit)


def _verdict_times(cadence, meta, first_event, last_event):
    """The clock's verdict times this run reaches: as for the bins of the other aggregations, a run whose last event is within a
    second of a time reached it; otherwise that verdict waits for the next run, so all events before it have come. The times
    follow from the last verdict, not from where runs were cut, so one long run and many short ones judge at the same times
    on the same events."""
    if cadence not in ('daily', 'hourly'):
        return []
    meta = {'through': first_event, **meta}
    last_event = last_event + pd.Timedelta(seconds=1)
    if not _judge_due(cadence, last_event, meta):
        return []
    unit = pd.Timedelta(days=1) if cadence == 'daily' else pd.Timedelta(hours=1)
    last = pd.Timestamp(meta.get('last_judge_end', meta['through']))
    return list(pd.date_range(last.floor(unit) + unit, last_event, freq=unit))


def _log_held_back(stats, upto, verbose):
    """These counts are the only sign of a clock running ahead, a wrong clock or the repeated hour of a clock change.
    upto None: a later part of the same run takes the rows held back."""
    for what, rows in (('stamped at or after ' + str(upto), stats.events_after_upto if upto is not None else {}),
                       ('with an impossible or long-past time', stats.events_bad_stamp),
                       ('at or before what the store already holds', stats.events_late)):
        for device, n in rows.items():
            v_print(f"detector_health: signal {device}: {n} events {what}, not processed", verbose)


def _write_table(conn, name, columns, frame, end, device_type):
    """The output table in fixed types, so a judge with no rows looks like any other, with TimeStamp for save(): the
    frame's own, else the time of the verdict."""
    conn.execute(f"CREATE OR REPLACE TABLE {name} ({columns.format(device=device_type)})")
    if frame is None:
        return
    conn.register(f'{name}_input', frame)
    if 'TimeStamp' in frame.columns:
        conn.execute(f"INSERT INTO {name} BY NAME SELECT * FROM {name}_input")
    else:
        conn.execute(f"INSERT INTO {name} BY NAME SELECT ?::TIMESTAMP AS TimeStamp, * FROM {name}_input", [end])
    conn.unregister(f'{name}_input')


def _judge(H, conn, params, end, bin_size):
    return H.judge(conn, DETECTORS, store=STORE, end=end, options=params['options'], ped=params['ped'],
                   ped_options=params['ped_options'], ped_assign=params['ped_assign'], workers=params['workers'],
                   outputs=('tables', 'bins'), bin_minutes=bin_size)


def _output_tables(params, bins=None, findings=None):
    """The tables a run with a verdict writes: name, columns, rows."""
    yield 'detector_health', BINS, bins
    if params['findings']:
        yield 'detector_health_findings', FINDINGS, findings


def _update_to(H, conn, params, upto, after):
    """Add raw_data's rows before upto (from after on: the earlier ones went in before the verdict at after)."""
    if after is None:
        source = 'raw_data'
    else:
        source = f'{DETECTORS}_rows'
        conn.execute(f"CREATE OR REPLACE TEMP VIEW {source} AS SELECT * FROM raw_data WHERE TimeStamp >= TIMESTAMP '{after}'")
    return H.update(conn, source, DETECTORS, store=STORE, upto=upto, options=params['options'], ped=params['ped'])


def run_detector_health(conn, params, verbose=1, bin_size=15):
    """Add this run's raw_data to the store and write detector_health, the bins released by the verdicts this run reaches, and
    detector_health_findings when its findings param is True. A run that reaches no verdict writes neither."""
    H = _load_package()
    if not _has_table(conn, 'raw_data'):
        raise ValueError("detector_health reads raw_data: give raw_data and call load() first")
    device_type = next(r[1] for r in conn.execute("DESCRIBE SELECT DeviceId FROM raw_data").fetchall())
    end = _covered_until(conn, params['clock_ahead_minutes'])
    one_shot = params['store'] is None
    if end is None:                 # nothing to add, and a new store would take its DeviceId type from no data
        if one_shot:
            for name, columns, frame in _output_tables(params):
                _write_table(conn, name, columns, frame, None, device_type)
        v_print("detector_health: no raw data in this run", verbose)
        return
    try:
        _register_detectors(conn, params['detectors'])
        _attach_store(H, conn, params)
        meta = _store_meta(conn)
        bins, findings, judged, after = [], None, None, None
        for time in _verdict_times(params['judge'], meta, *_typical_events(conn)):
            stats = _update_to(H, conn, params, time, after)
            _log_held_back(stats, None, verbose)
            after = time
            if stats.through is not None and stats.through < time:
                result = _judge(H, conn, params, time, bin_size)
                bins, findings, judged = bins + [result.bins], result.findings, time
        stats = _update_to(H, conn, params, end, after)
        _log_held_back(stats, end, verbose)
        if stats.through is None:
            return
        if stats.through >= end:    # the store is already past this run: its rows were late
            end = _next_quarter(stats.through)
        if (one_shot or params['judge'] == 'every_run') and judged != end:
            result = _judge(H, conn, params, end, bin_size)
            bins, findings, judged = bins + [result.bins], result.findings, end
        if judged is not None:
            rows = [b for b in bins if len(b)]
            for name, columns, frame in _output_tables(params, pd.concat(rows, ignore_index=True) if rows else None, findings):
                _write_table(conn, name, columns, frame, judged, device_type)
    finally:
        conn.execute(f"DROP TABLE IF EXISTS {DETECTORS}")
        conn.execute(f"DROP VIEW IF EXISTS {DETECTORS}_rows")
        if _attached(conn):
            H.detach_store(conn, STORE)

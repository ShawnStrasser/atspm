"""The detector_health aggregation: atspm-detector's vehicle and ped health in time bins.

With state given, a run every 15 minutes writes its own bins, and the first run past a midnight also runs the whole-day check and
writes what it finds in the previous day. What the next run needs is two tables on the connection, health_state and health_rows.
With no state, one run over this run's raw_data, judged at each midnight and at its end. Every temporary table is dropped before
save(), which writes every table it can see."""
import pandas as pd

from .data_loader import _quote_path, _strip_wrapping_quotes
from .utils import v_print

STORE = 'health_store'
DETECTORS = 'health_detectors'
DEFAULTS = {
    'detectors': None,              # DataFrame or path: DeviceId, Detector, phase, function, lanes [, p_*]
                                    # [, configured_function]; None: detector_config, as the agency set it
    'state': None,                  # last run's health_state, or a path to it; '': first run; None: one run over this raw_data
    'history': None,                # folder or glob of saved health_rows, or a function giving one; read only by a whole-day check
    'horizon_days': 3,              # how far back a whole-day check looks
    'findings': False,              # also write detector_health_findings, at runs with a whole-day check
    'ped': True,
    'ped_assign': False,            # ped movement model (onnxruntime)
    'options': None,                # atspm-detector vehicle health overrides
    'ped_options': None,            # atspm-detector ped health overrides
    'workers': 4,
    'clock_ahead_minutes': 60,      # a signal this far past the typical signal's last event has a wrong clock; also the longest a whole-day check waits
}
REPLACED = {
    'judge': "the whole-day check now comes by itself at the first run past midnight, and every run writes its own bins: leave it out",
    'store': "use 'state' (the last run's health_state) and 'history' (the folder of saved health_rows)",
    'restart_old_store': "pass 'state': '' to start again",
}
REMOVED = ('data', 'device_groups', 'decompose_params', 'anomaly_params', 'return_last_n_days')
BINS = 'TimeStamp TIMESTAMP, DeviceId {device}, DetectorType VARCHAR, Detector BIGINT, Label VARCHAR, Score DOUBLE'
FINDINGS = ('TimeStamp TIMESTAMP, DeviceId {device}, DetectorType VARCHAR, Detector INTEGER, Start TIMESTAMP, '
            '"End" TIMESTAMP, Label VARCHAR, Confidence DOUBLE, Detail VARCHAR')


def detector_health_params(params):
    """The parameters with the defaults filled in, checked before any work starts."""
    removed = [k for k in REMOVED if k in params]
    if removed:
        raise ValueError(f"detector_health parameters {removed} no longer exist: traffic-anomaly was removed in atspm "
                         "3.0.0 and detector_health is now atspm-detector's health. See the README.")
    replaced = [k for k in REPLACED if k in params]
    if replaced:
        raise ValueError("detector_health: " + "; ".join(f"{k} is gone: {REPLACED[k]}" for k in replaced))
    unknown = sorted(set(params) - set(DEFAULTS))
    if unknown:
        raise ValueError(f"detector_health: unknown parameters {unknown}; it takes {sorted(DEFAULTS)}")
    full = {**DEFAULTS, **params}
    if full['state'] is not None and full['history'] is None:
        raise ValueError("detector_health: a state needs history, the folder of saved health_rows")
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


def _verdict_times(first_event, last_event):
    """The midnights this run reaches: as for the bins of the other aggregations, a run whose last event is within a second of one
    reached it."""
    day = pd.Timedelta(days=1)
    return list(pd.date_range(first_event.floor(day) + day, last_event + pd.Timedelta(seconds=1), freq=day))


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


def _run_live(conn, params, bin_size, device_type):
    """A run with state as tables: detector_health, health_state, health_rows, and detector_health_findings at a whole-day check when asked."""
    if bin_size != 15:
        raise ValueError(f"detector_health with a state writes 15-minute bins; this run has bin_size {bin_size}")
    try:
        from atspm_detector import live
    except ImportError as e:
        raise ImportError("detector_health needs atspm-detector with incremental health: "
                          "pip install atspm[detector-health]") from e
    found, checked = None, None
    if conn.execute("SELECT count(*) FROM raw_data").fetchall()[0][0] == 0:     # the state stays as it is, and the run writes no rows
        live.empty_run(conn, params['state'])
    else:
        try:
            _register_detectors(conn, params['detectors'])
            temp = conn.execute("SELECT current_setting('temp_directory')").fetchall()[0][0]
            found, checked = live.live_run(conn, 'raw_data', DETECTORS, state=params['state'], history=params['history'], horizon_days=params['horizon_days'],
                                           ped=params['ped'], options=params['options'], ped_options=params['ped_options'], ped_assign=params['ped_assign'],
                                           workers=params['workers'], clock_ahead_minutes=params['clock_ahead_minutes'], temp_dir=temp or None)
        finally:
            conn.execute(f"DROP TABLE IF EXISTS {DETECTORS}")
    for name, table in (('detector_health', 'hl_final'), ('health_state', 'hl_state'), ('health_rows', 'hl_rows')):
        conn.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM {table}")
        conn.execute(f"DROP TABLE {table}")
    if params['findings'] and checked is not None:
        _write_table(conn, 'detector_health_findings', FINDINGS, found, checked, device_type)


def run_detector_health(conn, params, verbose=1, bin_size=15):
    """With a state: _run_live. With none: judge this run's raw_data at each midnight and at its end, and write detector_health and,
    when its findings param is True, detector_health_findings."""
    H = _load_package()
    if not _has_table(conn, 'raw_data'):
        raise ValueError("detector_health reads raw_data: give raw_data and call load() first")
    device_type = next(r[1] for r in conn.execute("DESCRIBE SELECT DeviceId FROM raw_data").fetchall())
    if params['state'] is not None:
        return _run_live(conn, params, bin_size, device_type)
    end = _covered_until(conn, params['clock_ahead_minutes'])
    if end is None:                 # nothing to add, and a new store would take its DeviceId type from no data
        for name, columns, frame in _output_tables(params):
            _write_table(conn, name, columns, frame, None, device_type)
        v_print("detector_health: no raw data in this run", verbose)
        return
    try:
        _register_detectors(conn, params['detectors'])
        H.attach_store(conn, None, name=STORE, horizon_days=params['horizon_days'], options=params['options'], ped=params['ped'])
        bins, findings, judged, after = [], None, None, None
        for time in _verdict_times(*_typical_events(conn)):
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
        if judged != end:
            result = _judge(H, conn, params, end, bin_size)
            bins, findings, judged = bins + [result.bins], result.findings, end
        rows = [b for b in bins if len(b)]
        for name, columns, frame in _output_tables(params, pd.concat(rows, ignore_index=True) if rows else None, findings):
            _write_table(conn, name, columns, frame, judged, device_type)
    finally:
        conn.execute(f"DROP TABLE IF EXISTS {DETECTORS}")
        conn.execute(f"DROP VIEW IF EXISTS {DETECTORS}_rows")
        if len(conn.execute("SELECT 1 FROM duckdb_databases() WHERE database_name = ?", [STORE]).fetchall()):
            H.detach_store(conn, STORE)

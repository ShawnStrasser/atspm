"""conn mode: SignalDataProcessor on an app's own DuckDB connection.

The processor works on a cursor of the app's connection, in an in-memory database of its own that it attaches under a unique
name and USEs, so its tables never meet the app's and the app's connection keeps its default database. It reads the raw events
and the detector config where they are in the app's database, and the state it left there the run before. save() appends the
outputs and replaces the state in one schema of the app's database in one transaction: a failed run writes nothing. close()
detaches what the run attached and closes the cursor, never the app's connection."""
import os
import re

import pandas as pd

from .data_loader import TableSource, _COLUMN_VARIANTS, _normalize_column_name, _quote_identifier

CONN_SETTINGS = ('start', 'end', 'devices', 'schema')
NOT_IN_CONN_MODE = ('output_dir', 'output_to_separate_folders', 'output_format', 'output_file_prefix', 'to_sql')
RESERVED_SCHEMAS = {'main', 'temp', 'information_schema', 'pg_catalog'}
# The tables of the work database that are inputs or steps, not outputs
INTERNAL_TABLES = {'raw_data', 'detector_config', 'unmatched_previous', 'sf_unmatched_previous', 'sf_final'}
# State: the rows of this run's devices are replaced. health_rows is history: added to, and kept while the state lists its run.
STATE_TABLES = ('unmatched_events', 'sf_unmatched', 'health_state')
HISTORY_TABLE = 'health_rows'
HEALTH_RUN_ID = 9605        # atspm-detector's code for a run the health state lists
RAW_COLUMNS = {'TimeStamp', 'DeviceId', 'EventId', 'Parameter'}
CONFIG_COLUMNS = {'deviceid', 'phase', 'parameter', 'function'}
PATH_LIKE = re.compile(r'[\\/*]|\.(parquet|csv|json)$', re.IGNORECASE)


def check_settings(settings):
    """Raises on settings that do not fit conn mode, before anything is attached. Returns start and end as Timestamps."""
    if settings.get('start') is None or settings.get('end') is None:
        raise ValueError("conn mode needs start and end: the window of events to run, start <= TimeStamp < end")
    start, end = pd.Timestamp(settings['start']), pd.Timestamp(settings['end'])
    if end <= start:
        raise ValueError(f"end ({end}) must come after start ({start})")
    bin_size = settings.get('bin_size', 15)
    for name, t in (('start', start), ('end', end)):
        if (t - t.normalize()) % pd.Timedelta(minutes=bin_size) != pd.Timedelta(0):
            raise ValueError(f"{name} ({t}) must fall on a {bin_size}-minute bin: runs cut inside a bin write it twice")
    for name in NOT_IN_CONN_MODE:
        if settings.get(name):
            raise ValueError(f"{name} does not apply with conn: results go to tables in the schema '{settings.get('schema', 'atspm')}'")
    extra = sorted(set(settings.get('unmatched_event_settings') or {}) - {'max_days_old'})
    if extra:
        raise ValueError(f"With conn the state is kept in the schema's tables and found there: unmatched_event_settings takes only "
                         f"max_days_old, not {extra}")
    schema = settings.get('schema', 'atspm')
    if not isinstance(schema, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', schema) or schema.lower() in RESERVED_SCHEMAS:
        raise ValueError(f"schema must be a plain name of a schema of its own, not {schema!r}: the run replaces state tables in it")
    for agg in settings.get('aggregations') or []:
        if agg['name'] != 'detector_health':
            continue
        if settings.get('devices') is not None:
            raise ValueError("detector_health keeps one state for all devices and judges clocks against the typical signal: "
                             "with conn, run it with devices left out")
        given = sorted({'state', 'history'} & set(agg.get('params') or {}))
        if given:
            raise ValueError(f"With conn, detector_health's state and history are kept in the schema's tables: leave out {given}")
    return start, end


def check_no_conn_settings(settings):
    given = [name for name in CONN_SETTINGS if settings.get(name) is not None]
    if given:
        raise ValueError(f"{given} need conn, the app's DuckDB connection to run on")


class ConnMode:
    """What a processor needs to run on an app's connection: its cursor, the names it attached, and the app's tables."""

    def __init__(self, app, start, end, devices, schema, verbose):
        self.cursor = app.cursor()
        self.app_db = self.cursor.execute("SELECT current_database()").fetchall()[0][0]
        tag = os.urandom(6).hex()
        self.work_db, self.raw_store = f'atspm_work_{tag}', f'atspm_raw_{tag}'
        self.start, self.end, self.devices, self.schema, self.verbose = start, end, devices, schema, verbose
        self.devices_table = None
        self.cursor.execute(f"ATTACH ':memory:' AS {self.work_db}; USE {self.work_db}")

    # ---- the app's tables

    def _quoted(self, schema, table):
        return f'{_quote_identifier(self.app_db)}.{_quote_identifier(schema)}.{_quote_identifier(table)}'

    def target(self, table):
        return self._quoted(self.schema, table)

    def schema_ref(self):
        return f'{_quote_identifier(self.app_db)}.{_quote_identifier(self.schema)}'

    def _relations(self):
        """(schema, name, columns) of each table and view of the app's database, but for the schemas atspm writes to."""
        rows = self.cursor.execute("""
            SELECT schema_name, table_name, list(column_name ORDER BY column_index)
            FROM duckdb_columns() WHERE database_name = ? GROUP BY ALL""", [self.app_db]).fetchall()
        ours = {s for s, t, cols in rows if t == 'unmatched_events' and 'IsValid' in cols}
        return [(s, t, cols) for s, t, cols in rows if s not in ours and s != self.schema]

    def _named(self, name):
        """A table or view of the app's database by its name, as a TableSource; None if there is none."""
        parts = [p.strip('"') for p in name.split('.')]
        if not 1 <= len(parts) <= 3:
            return None
        rows = self.cursor.execute("""
            SELECT database_name, schema_name, table_name FROM duckdb_columns()
            WHERE table_name = ? AND (? IS NULL OR schema_name = ?) AND (? IS NULL OR database_name = ?)
              AND database_name NOT IN ('system', 'temp') AND database_name NOT LIKE 'atspm_%' GROUP BY ALL""",
            [parts[-1], *([parts[-2]] * 2 if len(parts) > 1 else [None] * 2), *([parts[0]] * 2 if len(parts) == 3 else [None] * 2)]).fetchall()
        if len(parts) < 3:
            rows = [r for r in rows if r[0] == self.app_db] or rows
        if len(rows) > 1 and len(parts) == 1:
            rows = [r for r in rows if r[1] == 'main'] or rows
        if len(rows) != 1:
            return None
        database, schema, table = rows[0]
        return TableSource(f'{_quote_identifier(database)}.{_quote_identifier(schema)}.{_quote_identifier(table)}', f'{schema}.{table}')

    def source(self, given, kind):
        """raw_data or detector_config: a DataFrame or path as without conn, a name of the app's table, or None to find it."""
        if given is not None and not isinstance(given, str):
            return given
        if isinstance(given, str):
            named = self._named(given)
            if named is not None:
                return named
            if PATH_LIKE.search(given):
                return given
            raise ValueError(f"{kind}: no table or view named {given!r} in the app's database")
        return self._find(kind)

    def _find(self, kind):
        """The app's table of raw events or of detector config: one named raw_data or detector_config with the columns, else the only
        one with the columns. Several raise: name the one to read."""
        def fits(columns):
            names = {_normalize_column_name(c) for c in columns}
            if kind == 'raw_data':
                return RAW_COLUMNS <= {_COLUMN_VARIANTS[n] for n in names if n in _COLUMN_VARIANTS} and 'function' not in names
            return CONFIG_COLUMNS <= names
        fitting = [(s, t) for s, t, cols in self._relations() if fits(cols)]
        exact = [(s, t) for s, t in fitting if t.lower() == kind]
        chosen = exact if exact else fitting
        if len(chosen) == 1:
            schema, table = chosen[0]
            return TableSource(self._quoted(schema, table), f'{schema}.{table}')
        names = ', '.join(f'{s}.{t}' for s, t in chosen)
        if chosen:
            raise ValueError(f"Several tables could be the {'raw events' if kind == 'raw_data' else 'detector config'}: {names}. "
                             f"Name one with {kind}=")
        if kind == 'raw_data':
            raise ValueError("No table of raw events in the app's database (columns TimeStamp, DeviceId, EventId, Parameter): "
                             "name one with raw_data=")
        return None

    def make_devices_table(self, raw):
        """A temp table of the devices to run, in the type of the raw events' DeviceId. None: every device."""
        if self.devices is None:
            return None
        if isinstance(raw, TableSource):
            columns = self.cursor.execute(f"DESCRIBE SELECT * FROM {raw.ref}").fetchall()
            device_type = next(t for c, t, *_ in columns if _COLUMN_VARIANTS.get(_normalize_column_name(c)) == 'DeviceId')
        else:
            device_type = 'VARCHAR' if isinstance(list(self.devices)[0], str) else 'BIGINT'
        self.cursor.execute(f"CREATE OR REPLACE TEMP TABLE atspm_devices AS SELECT DISTINCT unnest(?::{device_type}[]) AS DeviceId",
                            [list(self.devices)])
        self.devices_table = 'temp.atspm_devices'
        return self.devices_table

    def _exists(self, table):
        return self.cursor.execute("SELECT count(*) FROM duckdb_tables() WHERE database_name = ? AND schema_name = ? AND table_name = ?",
                                   [self.app_db, self.schema, table]).fetchall()[0][0] > 0

    def state(self, table):
        """Last run's state of the devices to run, or '' for a first run."""
        if not self._exists(table):
            return ''
        devices = f" WHERE DeviceId IN (SELECT DeviceId FROM {self.devices_table})" if self.devices_table else ''
        return TableSource(f"(SELECT * FROM {self.target(table)}{devices})", f'{self.schema}.{table}')

    def relation(self, table):
        """A relation on the cursor of a table of the schema, for atspm-detector; None if there is none."""
        return self.cursor.sql(f"SELECT * FROM {self.target(table)}") if self._exists(table) else None

    # ---- the commit

    def work_tables(self):
        return [r[0] for r in self.cursor.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = current_database() AND schema_name = 'main'").fetchall()]

    def _ensure(self, table):
        """The schema's table for a work table, made with its columns, or given the columns it lacks."""
        target = self.target(table)
        if not self._exists(table):
            self.cursor.execute(f"CREATE TABLE {target} AS SELECT * FROM {_quote_identifier(table)} LIMIT 0")
            return target
        have = {r[0].lower() for r in self.cursor.execute(f"DESCRIBE SELECT * FROM {target}").fetchall()}
        for column, column_type, *_ in self.cursor.execute(f"DESCRIBE {_quote_identifier(table)}").fetchall():
            if column.lower() not in have:
                self.cursor.execute(f"ALTER TABLE {target} ADD COLUMN {_quote_identifier(column)} {column_type}")
        return target

    def append(self, table):
        columns = {r[0] for r in self.cursor.execute(f"DESCRIBE {_quote_identifier(table)}").fetchall()}
        order = next((f' ORDER BY {c}' for c in ('TimeStamp', 'StartTime') if c in columns), '')
        self.cursor.execute(f"INSERT INTO {self._ensure(table)} BY NAME SELECT * FROM {_quote_identifier(table)}{order}")

    def replace_state(self, table):
        target = self._ensure(table)
        devices = f" WHERE DeviceId IN (SELECT DeviceId FROM {self.devices_table})" if self.devices_table else ''
        self.cursor.execute(f"DELETE FROM {target}{devices}")
        self.cursor.execute(f"INSERT INTO {target} BY NAME SELECT * FROM {_quote_identifier(table)}")

    def keep_listed_history(self):
        """health_rows of the runs health_state lists, as a whole-day check reads only those."""
        self.cursor.execute(f"""DELETE FROM {self.target(HISTORY_TABLE)}
            WHERE Run NOT IN (SELECT Value::BIGINT FROM {self.target('health_state')} WHERE EventId = {HEALTH_RUN_ID})""")

    # ---- leaving

    def close(self):
        """Back to the app's database, everything attached detached, the cursor closed. The app's connection stays open."""
        cursor, self.cursor = self.cursor, None
        if cursor is None:
            return
        try:
            try:
                cursor.execute("ROLLBACK")
            except Exception:
                pass
            cursor.execute(f"USE {_quote_identifier(self.app_db)}")
            attached = {r[0] for r in cursor.execute("SELECT database_name FROM duckdb_databases()").fetchall()}
            # atspm-detector's stores, should a failed detector_health run have left them
            for name in (self.work_db, self.raw_store, 'health_store', 'health_check'):
                if name in attached:
                    cursor.execute(f"DETACH {_quote_identifier(name)}")
        finally:
            cursor.close()

import os
from .utils import v_print

_CANONICAL_COLUMNS = ['TimeStamp', 'DeviceId', 'EventId', 'Parameter']
_COLUMN_VARIANTS = {
    'timestamp': 'TimeStamp',
    'deviceid': 'DeviceId',
    'device': 'DeviceId',
    'signal': 'DeviceId',
    'signalid': 'DeviceId',
    'location': 'DeviceId',
    'intersection': 'DeviceId',
    'trafficsignal': 'DeviceId',
    'name': 'DeviceId',
    'devicename': 'DeviceId',
    'eventid': 'EventId',
    'eventcode': 'EventId',
    'parameter': 'Parameter',
    'eventparameter': 'Parameter',
    'eventparam': 'Parameter',
}


def _normalize_column_name(name):
    name = str(name).strip()
    return ''.join(ch for ch in name if ch not in {' ', '_'}).lower()


def _quote_identifier(name):
    """Quote identifiers for SQL statements."""
    safe_name = str(name).replace('"', '""')
    return f'"{safe_name}"'


def _quote_path(path):
    escaped = str(path).replace("'", "''")
    return f"'{escaped}'"


def _strip_wrapping_quotes(path):
    value = str(path)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _contiguous_frame(frame):
    """A pandas frame whose numeric or time columns are views with negative strides (df.iloc[::-1], say) is copied first.
    DuckDB 2.0 cannot scan those columns under pandas 3 and raises 'bad cast'."""
    columns = getattr(frame, 'dtypes', None)
    if not hasattr(columns, 'items') or not hasattr(frame, 'copy'):
        return frame
    for name, dtype in columns.items():
        if getattr(dtype, 'kind', None) in tuple('biufmM'):
            values = frame[name].to_numpy(copy=False)
            if getattr(values, 'flags', None) is not None and not values.flags['C_CONTIGUOUS']:
                return frame.copy()
    return frame


def _get_columns_from_dataframe_like(source):
    # Arrow tables also have .columns, but it holds the column arrays rather than their names
    if hasattr(source, 'column_names'):
        return list(source.column_names)
    if hasattr(source, 'columns'):
        return list(source.columns)
    raise ValueError("Unable to determine columns from the provided DataFrame-like source.")


class TableSource:
    """A table, view or subquery already on the connection, given by the SQL that names it: read where it is, with no copy
    through Python. conn mode hands the app's tables over as these."""
    def __init__(self, ref, label=None):
        self.ref = ref
        self.label = label or ref

    def __repr__(self):
        return f"TableSource({self.label})"


def _get_columns_from_relation(conn, ref):
    return [col[0] for col in conn.execute(f"SELECT * FROM {ref} LIMIT 0").description]


def _get_columns_from_path(conn, path):
    reference = _quote_path(path)
    cursor = conn.execute(f"SELECT * FROM {reference} LIMIT 0")
    description = cursor.description
    if description is None:
        return []
    return [col[0] for col in description]


def _choose_column(existing, candidate, canonical):
    if not existing:
        return candidate
    if candidate.lower() == canonical.lower() and existing.lower() != canonical.lower():
        return candidate
    return existing


def _resolve_canonical_columns(columns, source_label, required=None):
    if required is None:
        required = _CANONICAL_COLUMNS
    mapping = {}
    for raw_name in columns:
        if raw_name is None:
            continue
        normalized = _normalize_column_name(raw_name)
        canonical = _COLUMN_VARIANTS.get(normalized)
        if canonical:
            mapping[canonical] = _choose_column(mapping.get(canonical), str(raw_name), canonical)
    missing = [col for col in required if col not in mapping]
    if missing:
        raise ValueError(
            f"Source '{source_label}' must include columns for {', '.join(missing)}. "
            f"Available columns: {', '.join(str(c) for c in columns)}"
        )
    return mapping


_CAST_TYPES = {
    'TimeStamp': 'DATETIME',
    'EventId': 'INT16',
    'Parameter': 'INT16',
}


def _build_column_expression(mapping, key, use_alias=True):
    column_name = mapping[key]
    quoted = _quote_identifier(column_name)
    cast_type = _CAST_TYPES.get(key)
    expr = f"{quoted}::{cast_type}" if cast_type else f"{quoted}"
    if use_alias:
        return f"{expr} as {key}"
    return expr


# Optional detector_config columns: UDOT's three, then two for arrival_on_green's queue model. Lanes is the lane numbers an
# Advance detector covers, as atspm-detector writes them ("2", "1,2"); TravelTime is the seconds free-flowing cars take from
# its on to the stop line. Each is matched ignoring case, spaces and underscores, and is NULL if the config does not have it.
_OPTIONAL_CONFIG_COLUMNS = {'DistanceFromStopBar': 'DOUBLE', 'MPH': 'DOUBLE', 'LatencyCorrection': 'DOUBLE',
                            'Lanes': 'VARCHAR', 'TravelTime': 'DOUBLE', 'RedDepartures': 'DOUBLE'}


def _optional_config_select(columns):
    """The optional detector_config columns, each as its type. A value that is not a number becomes NULL."""
    names = {_normalize_column_name(name): name for name in reversed(list(columns))}
    selects = []
    for column, sql_type in _OPTIONAL_CONFIG_COLUMNS.items():
        source = names.get(column.lower())
        value = f"TRY_CAST({_quote_identifier(source)} AS {sql_type})" if source is not None else f"NULL::{sql_type}"
        selects.append(f"{value} AS {column}")
    return ", ".join(selects)


def _build_select_columns(column_map, include=None, extras=None):
    columns = include if include is not None else _CANONICAL_COLUMNS
    parts = [_build_column_expression(column_map, col) for col in columns if col in column_map]
    if extras:
        parts.extend(extras)
    return ", ".join(parts)

def _fall_back_hour(timestamp):
    """SQL that is true in 1:00-2:00 am on the first Sunday of November, the hour US clocks repeat."""
    return (f"(month({timestamp}) = 11 AND day({timestamp}) <= 7 AND dayofweek({timestamp}) = 0 "
            f"AND hour({timestamp}) = 1)")


def load_data(conn,
              verbose,
              raw_data=None,
              detector_config=None,
              unmatched_events=None,
              use_unmatched=False,
              known_detector_days=None,
              drop_fall_back_hour=True,
              window=None,
              devices_table=None,
              raw_store='raw_store'):
    """window: (start, end), the events to read, start <= TimeStamp < end. devices_table: a table of the DeviceIds to read, for
    raw_data and detector_config. raw_store: the name of the in-memory database raw_data is kept in."""

    # Reference point for the max_days_old filters below. A run whose raw data is empty has no
    # MIN(TimeStamp), and a bare None would be interpolated into SQL as the string 'None'. The
    # epoch keeps carried-over unmatched state instead of dropping it, matching how
    # SignalDataProcessor dates an empty run.
    min_timestamp = '1970-01-01 00:00:00'

    if raw_data is not None:
        if isinstance(raw_data, TableSource):
            v_print(f"Loading raw data from {raw_data.label}", verbose, 2)
            source_reference = raw_data.ref
            source_columns = _get_columns_from_relation(conn, raw_data.ref)
            source_label = raw_data.label
        elif isinstance(raw_data, str):
            raw_path = _strip_wrapping_quotes(raw_data)
            v_print("Loading raw data from path", verbose, 2)
            source_reference = _quote_path(raw_path)
            source_columns = _get_columns_from_path(conn, raw_path)
            source_label = raw_path
        else:
            v_print("Loading raw data from DataFrame", verbose, 2)
            raw_data = _contiguous_frame(raw_data)
            source_reference = "raw_data"
            source_columns = _get_columns_from_dataframe_like(raw_data)
            source_label = "raw_data DataFrame"

        column_map = _resolve_canonical_columns(source_columns, source_label)
        select_clause = _build_select_columns(column_map)

        # When clocks fall back the two passes through 1:00-2:00 share one set of timestamps and cannot be told
        # apart, so neither is kept. Everything after this reads raw_data, so to all of it the hour has no data.
        fall_back_filter = ""
        if drop_fall_back_hour:
            timestamp = _build_column_expression(column_map, 'TimeStamp', use_alias=False)
            fall_back_filter = f"AND {_fall_back_hour(timestamp)} IS NOT TRUE"
        # The window and the devices on the source's own columns, so DuckDB can skip what it does not need
        if window is not None:
            stamp = _quote_identifier(column_map['TimeStamp'])
            fall_back_filter += f" AND {stamp} >= TIMESTAMP '{window[0]}' AND {stamp} < TIMESTAMP '{window[1]}'"
        if devices_table is not None:
            fall_back_filter += f" AND {_quote_identifier(column_map['DeviceId'])} IN (SELECT DeviceId FROM {devices_table})"

        # raw_data sits for the whole run, the biggest table by far. Its own compressed in-memory database holds it in
        # about a sixth of the memory (compression happens at the checkpoint), and it reads about as fast.
        load_sql = f"""
        ATTACH ':memory:' AS {raw_store} (COMPRESS);
        CREATE TABLE {raw_store}.raw_data AS
        SELECT DISTINCT {select_clause}
        FROM {source_reference}
        WHERE EventId >= 0 AND EventId <= 32767 AND Parameter >= 0 AND Parameter <= 32767
        {fall_back_filter};
        CHECKPOINT {raw_store};
        CREATE VIEW raw_data AS SELECT * FROM {raw_store}.raw_data;
        """

        conn.query(load_sql)
        # Get the minimum timestamp from the raw data, keeping the epoch fallback when empty
        raw_min_timestamp = conn.query("SELECT MIN(TimeStamp) FROM raw_data").fetchall()[0][0]
        if raw_min_timestamp is not None:
            min_timestamp = raw_min_timestamp

    # Load Configurations (if provided)
    if detector_config is not None:
        if isinstance(detector_config, TableSource):
            config_columns = _get_columns_from_relation(conn, detector_config.ref)
            config_reference = detector_config.ref
        elif isinstance(detector_config, str):
            config_columns = _get_columns_from_path(conn, detector_config)
            config_reference = f"'{detector_config}'"
        else:
            config_columns = _get_columns_from_dataframe_like(detector_config)
            config_reference = "detector_config"
        conn.query(f"""
        CREATE TABLE detector_config AS
        SELECT DeviceId as DeviceId, Phase::INT16 as Phase, Parameter::INT16 as Parameter, Function::STRING as Function,
            {_optional_config_select(config_columns)}
        FROM {config_reference}
        {f"WHERE DeviceId IN (SELECT DeviceId FROM {devices_table})" if devices_table is not None else ""}
        """)


    # Load unmatched_events (if provided)
    try:
        # Adding try-except block in case unmatched_events timestamp is not in the correct format to automatically convert it
        # check if unmatched_events is provided and that the file exists
        if use_unmatched:
            max_days_old = unmatched_events['max_days_old']
            # Iterate over the strings/dataframes in unmatched_events dictionary
            for key, value in unmatched_events.items():
                if key in ('max_days_old', 'first_run'):
                    continue
                if value is None:
                    continue
                if isinstance(value, TableSource):
                    reference = value.ref
                    source_columns = _get_columns_from_relation(conn, value.ref)
                    source_label = value.label
                elif isinstance(value, str):
                    source_path = _strip_wrapping_quotes(value)
                    # Nothing to read on a first run. SignalDataProcessor has raised on any other missing file.
                    if not os.path.exists(source_path):
                        v_print(f"Skipping {key} - file does not exist yet: {source_path}", verbose, 2)
                        continue
                    reference = _quote_path(source_path)
                    source_columns = _get_columns_from_path(conn, source_path)
                    source_label = source_path
                else:
                    # Create a pointer for DuckDB
                    reference = 'unmatched_df'
                    unmatched_df = value
                    source_columns = _get_columns_from_dataframe_like(value)
                    source_label = f"unmatched_events[{key}]"

                required_columns = _CANONICAL_COLUMNS if key == 'df_or_path' else ['TimeStamp', 'DeviceId', 'EventId']
                column_map = _resolve_canonical_columns(source_columns, source_label, required_columns)
                timestamp_select = _build_column_expression(column_map, 'TimeStamp')
                timestamp_filter = _build_column_expression(column_map, 'TimeStamp', use_alias=False)
                device_select = _build_column_expression(column_map, 'DeviceId')
                eventid_select = _build_column_expression(column_map, 'EventId')
                where_clause = f" WHERE {timestamp_filter} > TIMESTAMP '{min_timestamp}' - INTERVAL '{max_days_old} days'"

                if key == 'df_or_path':
                    # An alarm or flash state holds until the device's next one, and that can be any time later.
                    # A detector stays on the known list for as long as actuations was told.
                    event_id = _build_column_expression(column_map, 'EventId', use_alias=False)
                    known_days = max_days_old if known_detector_days is None else known_detector_days
                    where_clause = (f" WHERE CASE WHEN {event_id} IN (173, 174, 175) THEN TRUE"
                                    f" WHEN {event_id} = 956 THEN {timestamp_filter} > TIMESTAMP '{min_timestamp}' - INTERVAL '{known_days} days'"
                                    f" ELSE {timestamp_filter} > TIMESTAMP '{min_timestamp}' - INTERVAL '{max_days_old} days' END")
                    parameter_select = _build_column_expression(column_map, 'Parameter')
                    # Check if IsValid column exists (for incremental processing with has_data validity tracking)
                    has_isvalid = 'isvalid' in [c.lower() for c in source_columns]
                    if has_isvalid:
                        isvalid_select = "IsValid::BOOLEAN AS IsValid"
                    else:
                        # Backward compatibility: if no IsValid column, assume all are valid
                        isvalid_select = "TRUE AS IsValid"
                    load_sql = f"""
                    CREATE TABLE unmatched_previous AS
                    SELECT {timestamp_select}, {device_select}, {eventid_select}, {parameter_select}, {isvalid_select}
                    FROM {reference} {where_clause};
                    CREATE VIEW raw_data_all AS
                    SELECT TimeStamp, DeviceId, EventId, Parameter FROM raw_data
                    UNION ALL
                    SELECT TimeStamp, DeviceId, EventId, Parameter FROM unmatched_previous
                    WHERE EventId NOT BETWEEN 927 AND 930 AND EventId NOT BETWEEN 938 AND 954 AND EventId NOT BETWEEN 956 AND 965 AND EventId NOT BETWEEN 970 AND 999; -- only left_turn_gaps, arrival_on_green, platoon_ratio, ped, yellow_red, actuations, approach_delay, arrival_pattern and has_data read these
                    """
                elif key == 'split_fail_df_or_path':
                    load_sql = f"""
                    CREATE TABLE sf_unmatched_previous AS
                    SELECT {timestamp_select}, {device_select}, {eventid_select}, Detector::INT16 as Detector, Phase::INT16 as Phase
                    FROM {reference} {where_clause}
                    """
                else:
                    raise ValueError(f"Unmatched events key '{key}' not recognized.")
                v_print(f"Loading unmatched events:  \n{reference}\n", verbose, 2)
                v_print(f'Executing SQL to load unmatched events: \n{load_sql}', verbose, 2)
                conn.query(load_sql)

    except Exception as e:
        print("*"*50)
        print("Error when loading unmatched_events! Here are some tips:")
        print("Loading from a CSV file may cause errors if the timestamp is not in the correct format. Try saving data in Parquet instead.")
        print("*"*50)
        raise e

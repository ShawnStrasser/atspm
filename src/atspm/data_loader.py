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


def _get_columns_from_dataframe_like(source):
    if hasattr(source, 'columns'):
        return list(source.columns)
    if hasattr(source, 'column_names'):
        return list(source.column_names)
    raise ValueError("Unable to determine columns from the provided DataFrame-like source.")


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


def _build_select_columns(column_map, include=None, extras=None):
    columns = include if include is not None else _CANONICAL_COLUMNS
    parts = [_build_column_expression(column_map, col) for col in columns if col in column_map]
    if extras:
        parts.extend(extras)
    return ", ".join(parts)

def load_data(conn,
              verbose,
              raw_data=None,
              detector_config=None,
              unmatched_events=None,
              use_unmatched=False,
              known_detectors=None,
              use_known_detectors=False):

    if raw_data is not None:
        if isinstance(raw_data, str):
            raw_path = _strip_wrapping_quotes(raw_data)
            v_print("Loading raw data from path", verbose, 2)
            source_reference = _quote_path(raw_path)
            source_columns = _get_columns_from_path(conn, raw_path)
            source_label = raw_path
        else:
            v_print("Loading raw data from DataFrame", verbose, 2)
            source_reference = "raw_data"
            source_columns = _get_columns_from_dataframe_like(raw_data)
            source_label = "raw_data DataFrame"

        column_map = _resolve_canonical_columns(source_columns, source_label)
        select_clause = _build_select_columns(column_map)

        load_sql = f"""
        CREATE TABLE raw_data AS
        SELECT DISTINCT {select_clause}
        FROM {source_reference}
        WHERE EventId >= 0 AND EventId <= 32767 AND Parameter >= 0 AND Parameter <= 32767
        """

        conn.query(load_sql)
        # Get the minimum timestamp from the raw data
        min_timestamp = conn.query("SELECT MIN(TimeStamp) FROM raw_data").fetchone()[0]

    # Load Configurations (if provided)
    load_sql = """
        CREATE TABLE detector_config AS
        SELECT DeviceId as DeviceId, Phase::INT16 as Phase, Parameter::INT16 as Parameter, Function::STRING as Function
        """
    if detector_config is not None:
        if isinstance(detector_config, str):
            conn.query(f"{load_sql} FROM '{detector_config}'")
        else:
            conn.query(f"{load_sql} FROM detector_config")


    # Load unmatched_events (if provided)
    try:
        # Adding try-except block in case unmatched_events timestamp is not in the correct format to automatically convert it
        # check if unmatched_events is provided and that the file exists
        if use_unmatched:
            max_days_old = unmatched_events['max_days_old']
            unmatched_events.pop('max_days_old')
            # Iterate over the strings/dataframes in unmatched_events dictionary
            for key, value in unmatched_events.items():
                if isinstance(value, str):
                    source_path = _strip_wrapping_quotes(value)
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
                    parameter_select = _build_column_expression(column_map, 'Parameter')
                    load_sql = f"""
                    CREATE TABLE unmatched_previous AS
                    SELECT {timestamp_select}, {device_select}, {eventid_select}, {parameter_select}
                    FROM {reference} {where_clause};
                    CREATE VIEW raw_data_all AS
                    SELECT * FROM raw_data
                    UNION ALL
                    SELECT * FROM unmatched_previous;
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
    
    # Load known_detectors (if provided)
    try:
        if use_known_detectors:
            max_days_old = known_detectors.get('max_days_old', 2)  # Default to 2 days if not specified
            
            known_detectors_reference = known_detectors.get('df_or_path')
            
            if isinstance(known_detectors_reference, str):
                reference = known_detectors_reference
            else:
                # Create a pointer for DuckDB
                reference = 'known_detectors_df'
                known_detectors_df = known_detectors_reference
            
            # Create WHERE clause to filter out old records
            where_clause = f" WHERE LastSeen::DATETIME > TIMESTAMP '{min_timestamp}' - INTERVAL '{max_days_old} days'"
            
            # Load the known_detectors_previous table
            load_sql = f"""
            CREATE TABLE known_detectors_previous AS
            SELECT DeviceId as DeviceId, Detector as Detector, LastSeen::DATETIME as LastSeen
            FROM {reference} {where_clause};
            """
            
            v_print(f"Loading known detectors from: \n{reference}\n", verbose, 2)
            v_print(f'Executing SQL to load known detectors: \n{load_sql}', verbose, 2)
            conn.query(load_sql)
            
    except Exception as e:
        print("*"*50)
        print("Error when loading known_detectors! Here are some tips:")
        print("Loading from a CSV file may cause errors if the timestamp is not in the correct format. Try saving data in Parquet instead.")
        print("Make sure known_detectors table has DeviceId, Detector, and LastSeen columns.")
        print("*"*50)
        raise e

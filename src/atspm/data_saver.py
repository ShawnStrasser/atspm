import os
from .utils import v_print

def _save_rows(conn, history, verbose):
    """One new file per run in the history folder, named by the run. A run with no rows has no file."""
    run = conn.execute("SELECT max(Run) FROM health_rows").fetchall()[0][0]
    if run is None:
        return
    folder, name = (history, '*.parquet') if os.path.isdir(history) or '*' not in history else os.path.split(history)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name.replace('*', str(run))).replace('\\', '/')
    v_print(f"Saving health rows to {path}", verbose, 2)
    conn.query(f"COPY (SELECT * FROM health_rows) TO '{path}'")


def _save_state(conn, path, verbose):
    """Replaces the state in one step, so a failed save leaves the last state whole."""
    v_print(f"Saving health state to {path}", verbose, 2)
    kind = 'CSV' if path.lower().endswith('.csv') else 'PARQUET'
    conn.query(f"COPY (SELECT * FROM health_state) TO '{path}.tmp' (FORMAT {kind})")
    os.replace(f"{path}.tmp", path)


def save_data(**kwargs):
    unmatched_events_path = None
    sf_unmatched_events_path = None
    # Extract parameters from kwargs
    output_dir = kwargs['output_dir']
    output_to_separate_folders = kwargs['output_to_separate_folders']
    output_format = kwargs['output_format']
    # check if output_file_prefix is in kwargs, if not set to empty string
    if 'output_file_prefix' not in kwargs:
        prefix = ''
    else:
        prefix = kwargs['output_file_prefix']
    conn = kwargs['conn']
    if kwargs['unmatched_event_settings'] is not None:
        if 'df_or_path' in kwargs['unmatched_event_settings']:
            unmatched_events_path = kwargs['unmatched_event_settings']['df_or_path']
            unmatched_events_path = unmatched_events_path.strip("'")
        else:
            unmatched_events_path = None
        if 'split_fail_df_or_path' in kwargs['unmatched_event_settings']:
            sf_unmatched_events_path = kwargs['unmatched_event_settings']['split_fail_df_or_path']
            sf_unmatched_events_path = sf_unmatched_events_path.strip("'")
        else:
            sf_unmatched_events_path = None
            
    # Make output directory if it does not exist
    os.makedirs(output_dir, exist_ok=True)

    # Get all table names in the database
    table_names = conn.sql("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    v_print(f"Saving the following tables: {table_names}", kwargs['verbose'], 2)

    # Detector health's tables in the order that lets a failed save be retried: its output, its rows, its state last
    health_order = {'detector_health': 1, 'health_rows': 2, 'health_state': 3}
    table_names = sorted(table_names, key=lambda t: health_order.get(t[0], 0))
    health_params = next((a['params'] for a in kwargs.get('aggregations') or [] if a['name'] == 'detector_health'), {})

    # Iterate over all tables
    for table_name in table_names:
        table_name = table_name[0]
        # skip if table name is raw_data or detector_config or internal state tables
        if table_name in ['raw_data', 'detector_config', 'unmatched_previous', 'sf_unmatched_previous', 'sf_final']:
            continue
            
        # Save unmatched data to path given, if any
        if table_name == 'unmatched_events' and isinstance(unmatched_events_path, str):
            v_print(f"Saving unmatched events to {unmatched_events_path}", kwargs['verbose'], 2)
            query = f"COPY (SELECT * FROM unmatched_events) TO '{unmatched_events_path}'"
            v_print(query, kwargs['verbose'], 2)
            conn.query(query)
            continue

        # Save split failures unmatched data to path given, if any
        if table_name == 'sf_unmatched' and isinstance(sf_unmatched_events_path, str):
            v_print(f"Saving split failures unmatched events to {sf_unmatched_events_path}", kwargs['verbose'], 2)
            query = f"COPY (SELECT * FROM sf_unmatched) TO '{sf_unmatched_events_path}'"
            v_print(query, kwargs['verbose'], 2)
            conn.query(query)
            continue
            
        # Detector health's state and rows to the paths given, if any
        if table_name == 'health_rows' and isinstance(health_params.get('history'), str):
            _save_rows(conn, health_params['history'], kwargs['verbose'])
            continue
        if table_name == 'health_state' and isinstance(health_params.get('state'), str) and health_params['state']:
            _save_state(conn, health_params['state'], kwargs['verbose'])
            continue

        if output_to_separate_folders:
            final_path = f"{table_name}/{prefix}"
            # Create a directory for the table if it does not exist
            os.makedirs(f"{output_dir}/{table_name}", exist_ok=True)
        else:
            final_path = f"{prefix}{table_name}"

        # Order exports consistently: timeline by StartTime, everything else by TimeStamp
        order_clause = ' ORDER BY StartTime' if table_name == 'timeline' else ' ORDER BY TimeStamp'
        # Query to select all data from the table
        query = f"COPY (SELECT * FROM {table_name}{order_clause}) TO '{output_dir}/{final_path}.{output_format}'"
        conn.query(query)
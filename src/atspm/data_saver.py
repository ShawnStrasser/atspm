import os
from .utils import v_print

def save_data(**kwargs):
    unmatched_events_path = None
    sf_unmatched_events_path = None
    known_detectors_path = None
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
            
    if kwargs.get('known_detectors_settings') is not None:
        if 'df_or_path' in kwargs['known_detectors_settings']:
            known_detectors_path = kwargs['known_detectors_settings']['df_or_path']
            known_detectors_path = known_detectors_path.strip("'")
        else:
            known_detectors_path = None

    # Make output directory if it does not exist
    os.makedirs(output_dir, exist_ok=True)

    # Get all table names in the database
    table_names = conn.sql("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    v_print(f"Saving the following tables: {table_names}", kwargs['verbose'], 2)

    # Iterate over all tables
    for table_name in table_names:
        table_name = table_name[0]
        # skip if table name is raw_data or detector_config or internal state tables
        if table_name in ['raw_data', 'detector_config', 'unmatched_previous', 'sf_unmatched_previous', 'sf_final', 'current_detectors', 'known_detectors_previous']:
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
            
        # Save known detectors to path given, if any
        if table_name == 'known_detectors' and isinstance(known_detectors_path, str):
            v_print(f"Saving known detectors to {known_detectors_path}", kwargs['verbose'], 2)
            query = f"COPY (SELECT * FROM known_detectors) TO '{known_detectors_path}'"
            v_print(query, kwargs['verbose'], 2)
            conn.query(query)
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
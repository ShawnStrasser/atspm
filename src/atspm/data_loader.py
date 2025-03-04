import os
from .utils import v_print

def load_data(conn,
              verbose,
              raw_data=None,
              detector_config=None,
              unmatched_events=None,
              use_unmatched=False,
              known_detectors=None,
              use_known_detectors=False):

    # Load Raw Data
    load_sql = """
        CREATE TABLE raw_data AS
        SELECT DISTINCT TimeStamp::DATETIME as TimeStamp, DeviceId as DeviceId, EventId::INT16 as EventId, Parameter::INT16 as Parameter
        """
    # From statment is used to load data from a file or a string (as DataFrame)
    if raw_data is not None:
        if isinstance(raw_data, str):
            v_print("Loading raw data from path", verbose, 2)
            load_sql += f" FROM '{raw_data}'"
        else:
            v_print("Loading raw data from DataFrame", verbose, 2)
            load_sql += " FROM raw_data"
        # Filter out out of range values for EventId and Parameter (to avoid errors when loading data in case of anomalies/errors)
        load_sql += " WHERE EventId >= 0 AND EventId <= 32767 AND Parameter >= 0 AND Parameter <= 32767"
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
            # Create a WHERE clause to filter out old events
            where_clause = f" WHERE TimeStamp::DATETIME > TIMESTAMP '{min_timestamp}' - INTERVAL '{max_days_old} days'"
            # Iterate over the strings/dataframes in unmatched_events dictionary
            for key, value in unmatched_events.items():
                if isinstance(value, str):
                    reference = value
                else:
                    # Create a pointer for DuckDB
                    reference = 'unmatched_df'
                    unmatched_df = value

                # Create view that unions the previous unmatched events with the new ones
                if key == 'df_or_path':
                    load_sql = f"""
                    CREATE TABLE unmatched_previous AS
                    SELECT TimeStamp::DATETIME as TimeStamp, DeviceId as DeviceId, EventId::INT16 as EventId, Parameter::INT16 as Parameter
                    FROM {reference} {where_clause};
                    CREATE VIEW raw_data_all AS
                    SELECT * FROM raw_data
                    UNION ALL
                    SELECT * FROM unmatched_previous;
                    """
                elif key == 'split_fail_df_or_path':
                    load_sql = f"""
                    CREATE TABLE sf_unmatched_previous AS
                    SELECT TimeStamp::DATETIME as TimeStamp, DeviceId as DeviceId, EventId::INT16 as EventId, Detector::INT16 as Detector, Phase::INT16 as Phase
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
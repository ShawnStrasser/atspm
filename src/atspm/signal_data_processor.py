import duckdb
import time
import traffic_anomaly
from .data_loader import load_data
from .data_aggregator import aggregate_data
from .data_saver import save_data
from .utils import round_down_15
from .utils import v_print
import os


class SignalDataProcessor:
    '''
    Main class in the atspm package, used to process signal data by turning raw hi-res data into aggregated data.

    This class handles the entire pipeline of loading raw data, performing various aggregations,
    and saving the results. It supports both one-time processing and incremental processing with
    unmatched event handling. Most the inputs here are optional, depending on the desired processing.

    Attributes
    ----------
    raw_data : str or DataFrame
        The raw data to be processed, either as a file path or a DataFrame.
    detector_config : str or DataFrame
        The detector configuration, either as a file path or a DataFrame.
    bin_size : int
        The size of the time bins for aggregation, in minutes.
    output_dir : str
        The directory where the output files will be saved.
    output_to_separate_folders : bool
        If True, output files will be saved in separate folders.
    output_format : str
        The format of the output files. Options are "csv", "parquet", "json".
    output_file_prefix : str
        Prefix to be added to all output file names.
    remove_incomplete : bool
        If True, removes periods with incomplete data based on the 'has_data' aggregation.
    unmatched_event_settings : dict, optional
        Settings for handling unmatched events in incremental processing. Includes:
        - df_or_path: str, path to save/load unmatched events
        - split_fail_df_or_path: str, path to save/load unmatched split failure events
        - max_days_old: int, maximum age of unmatched events to consider
    to_sql : bool
        If True, returns SQL strings instead of executing queries.
    verbose : int
        Controls the verbosity of output. 0: only errors, 1: performance, 2: debug statements.
    aggregations : list of dict
        A list of dictionaries, each containing the name of an aggregation function and its parameters.
        Supported aggregations include: 'has_data', 'actuations', 'arrival_on_green', 'communications',
        'coordination', 'coordination_agg', 'ped', 'unique_ped', 'full_ped', 'split_failures', 'splits', 'terminations',
        'yellow_red', 'timeline', 'ped_delay', 'phase_wait', and potentially others.

    Methods
    -------
    load()
        Loads the raw data and detector configuration into DuckDB tables.
    aggregate()
        Runs all specified aggregations on the loaded data.
    save()
        Saves the processed data to the specified output directory and format.
    close()
        Closes the database connection.
    run()
        Executes the complete data processing pipeline: load, aggregate, save, and close.
        If to_sql is True, returns the SQL queries instead of executing them.

    Example
    -------
    # Recommended: Use context manager (automatically closes connection)
    with SignalDataProcessor(
        raw_data=sample_data.data,
        detector_config=sample_data.config,
        bin_size=15,
        verbose=1,
        aggregations=[
            {'name': 'has_data', 'params': {'no_data_min': 5, 'min_data_points': 3}},
            {'name': 'actuations', 'params': {}},
        ]
    ) as processor:
        processor.load()
        processor.aggregate()
        # Access results via processor.conn before exiting
    
    # Alternative: Call close() explicitly
    processor = SignalDataProcessor(...)
    try:
        processor.load()
        processor.aggregate()
    finally:
        processor.close()
    '''

    def __init__(self, **kwargs):
        """Initializes the SignalDataProcessor with the provided keyword arguments."""
        # Optional parameters
        self.raw_data = None
        self.detector_config = None
        self.unmatched_event_settings = None # For incremental processing of timeline, split failure, arrival on green, and yellow red)
        self.unmatched_found = False
        self.known_detectors_settings = None # For incremental processing of actuations to track detectors with zero counts
        self.known_detectors_found = False
        self.incremental_run = False
        self.binned_actuations = None # For detector_health aggregation
        self.device_groups = None # For detector_health aggregation if groups are provided
        self.remove_incomplete = False
        self.to_sql = False
        self.verbose = 1 # 0: only print errors, 1: print performance, 2: print debug statements
        
        # Extract parameters from kwargs
        for key, value in kwargs.items():
            setattr(self, key, value)

        # Check for valid bin_size and no_data_min combo
        if self.remove_incomplete:
            # raise error if 'has_data' aggregation is not in aggregations
            assert any(d['name'] == 'has_data' for d in self.aggregations), "Remove_incomplete requires 'has_data' aggregation!"
            # extract has_data parameters
            no_data_min = next(x['params']['no_data_min'] for x in self.aggregations if x['name'] == 'has_data')
            assert self.bin_size % no_data_min == 0, "bin_size / no_data_min must be a whole number"
            # Make sure that has_data is the first aggregation
            idx = [d['name'] for d in self.aggregations].index('has_data')
            self.aggregations.insert(0, self.aggregations.pop(idx))

        # Check format of unmatched_event_settings
        # Duckdb needs quotes if it is a file path, but not if it is a dataframe
        if self.unmatched_event_settings is not None:
            self.incremental_run = True
            self.unmatched_found = True
            for key, value in self.unmatched_event_settings.items():
                if key == 'max_days_old':
                    continue
                if isinstance(value, str):
                    if os.path.exists(value) and value != '':
                        self.unmatched_event_settings[key] = f"'{value}'"
                    else:
                        v_print(f"Warning, {key} file '{value}' does not exist or is blank. This is expected only for the first run.", self.verbose)
                        self.unmatched_found = False
                elif value is None:
                    v_print(f"Warning, {key} file is None. This is expected only for the first run.", self.verbose)
                    self.unmatched_found = False

        # Check for known_detectors parameters in actuations aggregation
        # If found, extract them and create known_detectors_settings
        for agg in self.aggregations:
            if agg['name'] == 'actuations' and 'params' in agg:
                params = agg['params']
                if 'known_detectors_df_or_path' in params:
                    if self.known_detectors_settings is None:
                        self.known_detectors_settings = {}
                    # Use get() and then remove via dict comprehension to avoid modifying original params
                    self.known_detectors_settings['df_or_path'] = params.get('known_detectors_df_or_path')
                    self.known_detectors_settings['max_days_old'] = params.get('known_detectors_max_days_old', 2)  # Default to 2 days
                    # Remove these keys from params without modifying the original dict in-place
                    # by creating a filtered copy that will be used later
                    agg['params'] = {k: v for k, v in params.items() 
                                     if k not in ('known_detectors_df_or_path', 'known_detectors_max_days_old')}
                    
                    # Set incremental_run and known_detectors_found flags
                    if not self.incremental_run:
                        self.incremental_run = True
                    self.known_detectors_found = True
                    
                    # Format the df_or_path for DuckDB
                    value = self.known_detectors_settings['df_or_path']
                    if isinstance(value, str):
                        if os.path.exists(value) and value != '':
                            self.known_detectors_settings['df_or_path'] = f"'{value}'"
                        else:
                            v_print(f"Warning, df_or_path file '{value}' does not exist or is blank. This is expected only for the first run.", self.verbose)
                            self.known_detectors_found = False
                    elif value is None or (isinstance(value, str) and value == ''):
                        v_print(f"Warning, df_or_path file is None or empty. This is expected only for the first run.", self.verbose)
                        self.known_detectors_found = False
                    break

        # Check format of known_detectors_settings (for backward compatibility)
        # Similar to unmatched_event_settings
        if self.known_detectors_settings is not None and not hasattr(self, 'known_detectors_found'):
            if not self.incremental_run:
                self.incremental_run = True
            self.known_detectors_found = True
            for key, value in self.known_detectors_settings.items():
                if key == 'max_days_old':
                    continue
                if isinstance(value, str):
                    if os.path.exists(value) and value != '':
                        self.known_detectors_settings[key] = f"'{value}'"
                    else:
                        v_print(f"Warning, {key} file '{value}' does not exist or is blank. This is expected only for the first run.", self.verbose)
                        self.known_detectors_found = False
                elif value is None:
                    v_print(f"Warning, {key} file is None. This is expected only for the first run.", self.verbose)
                    self.known_detectors_found = False

        # Check if detector_health is in aggregations
        if any(d['name'] == 'detector_health' for d in self.aggregations):
            try:
                idx = [d['name'] for d in self.aggregations].index('detector_health')
                self.binned_actuations = self.aggregations[idx]['params']['data']
                self.device_groups = self.aggregations[idx]['params']['device_groups']
            except KeyError:
                raise ValueError("detector_health aggregation requires 'data' and 'device_groups' parameters. 'device_groups' can be set to None.")
     
        # Establish a connection to the database
        self.conn = duckdb.connect()
        # Track whether connection has been closed
        self._closed = False
        # Track whether data has been loaded
        self.data_loaded = False

        # Use connection to get current timestamp
        # This is a placeholder for when to_sql is True. After the class is instantiated, 
        # timestamps need to be set by the user for the full_ped query to work.
        self.max_timestamp = self.conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
        self.min_timestamp = self.max_timestamp

    def load(self):
        """Loads raw data and detector configuration into DuckDB tables."""
        if self.data_loaded:
            v_print("Data already loaded! Reinstantiate the class to reload data.", self.verbose)
            return
        if self.to_sql:
            v_print("to_sql option is True, data will not be loaded.", self.verbose)
            return
        try:
            load_data(self.conn,
                    self.verbose,
                    self.raw_data,
                    self.detector_config,
                    self.unmatched_event_settings,
                    self.unmatched_found,
                    self.known_detectors_settings,
                    self.known_detectors_found)
            # delete self.raw_data and self.detector_config to free up memory
            self.data_loaded = True
            if self.raw_data is not None:
                self.min_timestamp = self.conn.execute("SELECT MIN(timestamp) FROM raw_data").fetchone()[0]
                self.max_timestamp = self.conn.execute("SELECT MAX(timestamp) FROM raw_data").fetchone()[0]
                v_print(f'Data loaded from {self.min_timestamp} to {self.max_timestamp}', self.verbose)
            # free up memory
            del self.raw_data
            del self.detector_config
        except Exception as e:
            v_print('*'*50, self.verbose)
            v_print('WARNING: problem loading data!', self.verbose)
            v_print('Make sure raw_data column names are: TimeStamp, DeviceId, EventId, Parameter', self.verbose)
            v_print('Make sure detector_config column names are: DeviceId, Phase, Parameter, Function', self.verbose)
            v_print('*'*50, self.verbose)
            raise e
        
    def aggregate(self):
        """Runs all aggregations."""
        # Instantiate a dictionary to store runtimes
        self.runtimes = {}
        self.sql_queries = {} # for storing sql string when to_sql is True

        # Create unmatched_events table if unmatched_events is not None
        # This table will be used to insert unmatched events, to be saved and reloaded in the next run
        #if self.unmatched_event_settings is not None:
        #    v_print("Creating unmatched_events table", self.verbose, 2)
        #    self.conn.query(f"CREATE TABLE unmatched_events AS SELECT * AS aggregation FROM raw_data WHERE 1=0")

        for aggregation in self.aggregations:
            start_time = time.time()
            v_print(f"\nRunning {aggregation['name']} aggregation...", self.verbose, 2)

            #######################
            ### Detector Health ###
            ### Does Not Use aggregate_data function
            ### Relies on traffic-anomaly package instead
            # Decompose data
            if aggregation['name'] == 'detector_health':
                if self.to_sql:
                    raise ValueError("to_sql option is  supported for detector_health")
                decomp = traffic_anomaly.decompose(
                    self.binned_actuations,
                    **aggregation['params']['decompose_params']
                )
                del self.binned_actuations
                self.binned_actuations = None  # Clear reference
                # Join groups to decomp
                if self.device_groups is not None:
                    device_groups = self.device_groups # DuckDB needs a direct pointer to see the table
                    decomp = self.conn.sql("SELECT * FROM decomp NATURAL JOIN device_groups").df()
                    del device_groups  # Clear local reference after DuckDB query
                    # Exclude group_grouping_columns in anomaly_params
                    exclude_col = ', '.join(["'{}'".format(x) for x in aggregation['params']['anomaly_params']['group_grouping_columns']])
                    exclude_col = f"EXCLUDE ({exclude_col})"
                    
                else:
                    exclude_col = ""
                # Find Anomalies
                anomaly_df = traffic_anomaly.anomaly(
                    decomposed_data=decomp,
                    **aggregation['params']['anomaly_params']
                )
                del decomp  # Free memory after anomaly calculation
                # Extract max date from anomaly table and subtract return_last_n_days
                sql = f"""
                    SELECT CAST(MAX(TimeStamp)::DATE - INTERVAL '{aggregation['params']['return_last_n_days']-1}' DAY AS VARCHAR) AS max_date_minus_one
                    FROM anomaly_df
                    """
                max_date = self.conn.query(sql).fetchone()[0]

                # Save anomaly table to DuckDB
                query = f"""CREATE OR REPLACE TABLE detector_health AS
                        SELECT * {exclude_col}
                        FROM anomaly_df
                        WHERE TimeStamp >= '{max_date}'
                        """
                self.conn.execute(query)
                del anomaly_df  # Free memory after saving to DuckDB
                # no external sql file like other aggregations, so just continue
                end_time = time.time()
                self.runtimes[aggregation['name']] = end_time - start_time
                continue
            else:
                # Dependencies: ped_delay, phase_wait, and coordination_agg require the timeline table to exist
                if aggregation['name'] in ('ped_delay', 'phase_wait', 'coordination_agg') and not self.to_sql:
                    has_timeline = self.conn.execute(
                        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'timeline'"
                    ).fetchone()[0]
                    if has_timeline == 0:
                        raise ValueError(f"{aggregation['name']} aggregation requires the timeline table. Run timeline first.")
                
                # Dependencies: coordination_agg also requires the has_data table to exist
                if aggregation['name'] == 'coordination_agg' and not self.to_sql:
                    has_has_data = self.conn.execute(
                        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'has_data'"
                    ).fetchone()[0]
                    if has_has_data == 0:
                        raise ValueError(f"{aggregation['name']} aggregation requires the has_data table. Run has_data first.")

                # Get parameters from the aggregation, or defaults
                # Add the bin_size from init
                params = aggregation.get('params', {}).copy()  # Need to copy to avoid modifying the original
                params['bin_size'] = self.bin_size
                params['from_table'] = 'raw_data'
                params['remove_incomplete'] = self.remove_incomplete
                
                #######################
                ### Phase Wait ###
                # Set defaults for phase_wait aggregation
                if aggregation['name'] == 'phase_wait':
                    if 'preempt_recovery_seconds' not in params:
                        params['preempt_recovery_seconds'] = 120
                    if 'assumed_cycle_length' not in params:
                        params['assumed_cycle_length'] = 140
                    if 'skip_multiplier' not in params:
                        params['skip_multiplier'] = 1.5

                #######################
                ### Full Pedestrian ###
                # Add min_timestamp and max_timestamp to params if detector_faults or full_ped
                if aggregation['name'] == 'full_ped':
                    # Round min_timestamp down to nearest bin_size
                    params['min_timestamp'] = round_down_15(self.min_timestamp)
                    params['max_timestamp'] = round_down_15(self.max_timestamp)

                #######################
                ### Unmatched Events ##
                # If unmatched_event_settings is supplied, then change the from_table for timeline, split_failures, arrival_on_green, and yellow_red
                # These are views that have the relateded unmatched events unioned to them
                if self.incremental_run and aggregation['name'] in ['timeline', 'arrival_on_green', 'yellow_red', 'split_failures']:
                    params['incremental_run'] = True #lets the aggregation know to save unmatched events for next run
                    if self.unmatched_found:
                        v_print(f"Incremental run using previous events for {aggregation['name']}", self.verbose, 2)
                        params['unmatched'] = True #lets the aggregation know to use the unmatched events from previous run
                        # split_failures uses its own view
                        if aggregation['name'] != 'split_failures':
                            params['from_table'] = 'raw_data_all'
                    else:
                        v_print(f"First Run For {aggregation['name']}", self.verbose, 2)
                        params['unmatched'] = False
                
                # Add known_detectors_found flag for actuations aggregation
                if aggregation['name'] == 'actuations':
                    params['known_detectors_found'] = self.known_detectors_found
                
                # Add coord_state_found flag for coordination_agg aggregation (uses unmatched events)
                if aggregation['name'] == 'coordination_agg':
                    params['coord_state_found'] = self.unmatched_found
                
                # Output sql or execute query
                self.sql_queries[aggregation['name']] = aggregate_data(
                    self.conn,
                    aggregation['name'],
                    self.to_sql,
                    **params
                )
                
            end_time = time.time()
            # Store the runtime
            self.runtimes[aggregation['name']] = end_time - start_time
            
        # Print out runtimes
        v_print(f"\n\nTotal aggregation runtime: {sum(self.runtimes.values()):.2f} seconds.", self.verbose)
        v_print("\nIndividual Query Runtimes:", self.verbose)
        for name, runtime in self.runtimes.items():
            v_print(f"{name}: {runtime:.2f} seconds", self.verbose)

        # After all aggregations are finished, create and update the known_detectors table
        # This combines detectors from the current batch with previous known detectors
        if self.known_detectors_settings is not None and any(agg['name'] == 'actuations' for agg in self.aggregations):
            v_print("Creating/updating known_detectors table", self.verbose, 2)
            
            # Create a query to extract all detectors from raw_data and update LastSeen timestamp
            current_detectors_query = """
            CREATE OR REPLACE TABLE current_detectors AS
            SELECT DISTINCT
                DeviceId,
                Parameter as Detector,
                MAX(TimeStamp) as LastSeen
            FROM raw_data
            WHERE EventID = 82
            GROUP BY DeviceId, Detector;
            """
            self.conn.query(current_detectors_query)
            
            # Create or update the known_detectors table
            if self.known_detectors_found:
                # Merge current detectors with previously known detectors
                merge_query = """
                CREATE OR REPLACE TABLE known_detectors AS
                SELECT 
                    u.DeviceId,
                    u.Detector,
                    COALESCE(MAX(kd.LastSeen), MAX(cd.LastSeen)) as LastSeen
                FROM 
                    (SELECT DISTINCT DeviceId, Detector FROM known_detectors_previous 
                     UNION 
                     SELECT DISTINCT DeviceId, Detector FROM current_detectors) u
                LEFT JOIN known_detectors_previous kd ON u.DeviceId = kd.DeviceId AND u.Detector = kd.Detector
                LEFT JOIN current_detectors cd ON u.DeviceId = cd.DeviceId AND u.Detector = cd.Detector
                GROUP BY u.DeviceId, u.Detector;
                """
            else:
                # Just use current detectors if no history is available
                merge_query = """
                CREATE OR REPLACE TABLE known_detectors AS
                SELECT 
                    DeviceId,
                    Detector,
                    LastSeen
                FROM current_detectors;
                """
            
            self.conn.query(merge_query)
            v_print("Known detectors table updated", self.verbose, 2)
    
    def save(self):
        """Saves the processed data."""
        if self.to_sql:
            v_print("to_sql option is True, data will not be saved.", self.verbose)
            return
        save_data(**self.__dict__)

    def __enter__(self):
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit. Closes the database connection."""
        self.close()
        return False
    
    def close(self):
        """Closes the database connection. Safe to call multiple times."""
        if self._closed:
            return
        if hasattr(self, 'conn') and self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn = None
        self._closed = True

    def run(self):
        """Runs the complete data processing pipeline."""
        self.load()
        self.aggregate()
        if self.to_sql:
            return self.sql_queries
        self.save()
        self.close()

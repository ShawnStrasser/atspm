import duckdb
import ibis
import time
import traffic_anomaly
from .data_loader import load_data, _quote_path, _strip_wrapping_quotes
from .data_aggregator import aggregate_data, render_query
from .data_saver import save_data
from .utils import round_down_15
from .utils import v_print
import os

# Define aggregation dependencies: key requires values to run first
# Example: 'phase_wait' requires 'timeline' to exist
AGGREGATION_DEPENDENCIES = {
    'timeline': ['has_data'],  # timeline needs has_data for IsValid check
    'phase_wait': ['timeline'],  # phase_wait uses ASOF join on cycle length events from timeline
    'ped_delay': ['timeline'],
    'coordination_agg': ['timeline', 'has_data'],
    'platoon_ratio': ['arrival_on_green'],  # platoon_ratio divides Percent_AOG by the green ratio
}


# Default timeline max_event_gap_seconds: the longest silence, by the time of day it starts, before a device
# is treated as having dropped out. Overnight a controller resting in green with no traffic logs nothing for
# minutes at a time, so the threshold steps up through the evening and back down in the early morning.
DEFAULT_MAX_EVENT_GAP_SECONDS = {'05:00': 300, '06:00': 120, '21:00': 300, '23:00': 900}


def _event_gap_schedule(value):
    """timeline max_event_gap_seconds as [[minute of day, seconds], ...] sorted by minute, or None if disabled.

    Accepts None (disabled), a number of seconds for all day, or a dict of 'HH:MM' start times to seconds.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = {'00:00': value}
    if not isinstance(value, dict) or not value:
        raise ValueError("max_event_gap_seconds must be None, a number of seconds, or a dict of 'HH:MM' to seconds")
    schedule = []
    for start, seconds in value.items():
        try:
            hour, minute = (int(x) for x in str(start).split(':'))
        except ValueError:
            raise ValueError(f"max_event_gap_seconds start time '{start}' must be 'HH:MM'") from None
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError(f"max_event_gap_seconds start time '{start}' must be 'HH:MM'")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
            raise ValueError(f"max_event_gap_seconds threshold for '{start}' must be a positive number of seconds")
        schedule.append([hour * 60 + minute, seconds])
    schedule.sort()
    if len({minute for minute, _ in schedule}) < len(schedule):
        raise ValueError("max_event_gap_seconds has the same start time more than once")
    return schedule


def _to_arrow(relation):
    """A DuckDB relation as an Arrow table (to_arrow_table replaced fetch_arrow_table in newer DuckDB)."""
    if hasattr(relation, 'to_arrow_table'):
        return relation.to_arrow_table()
    return relation.fetch_arrow_table()


def _validate_aggregation_dependencies(aggregations, verbose=1):
    """
    Validate that all required dependencies are present in the aggregation list.
    Raises ValueError if a dependency is missing.
    """
    agg_names = [a['name'] for a in aggregations]
    
    for name in agg_names:
        if name in AGGREGATION_DEPENDENCIES:
            required_deps = AGGREGATION_DEPENDENCIES[name]
            missing_deps = [dep for dep in required_deps if dep not in agg_names]
            if missing_deps:
                raise ValueError(
                    f"Aggregation '{name}' requires the following aggregations to be included: {missing_deps}. "
                    f"Please add them to your aggregations list."
                )


def _sort_aggregations_by_dependency(aggregations, verbose=1):
    """
    Sort aggregations so dependencies run first.
    Uses a simple approach: repeatedly move items that have unmet dependencies to run after their dependencies.
    """
    agg_names = [a['name'] for a in aggregations]
    agg_dict = {a['name']: a for a in aggregations}
    
    # Only consider dependencies for aggregations that are actually in the list
    relevant_deps = {}
    for name in agg_names:
        if name in AGGREGATION_DEPENDENCIES:
            # Only include dependencies that are also in the aggregation list
            deps = [d for d in AGGREGATION_DEPENDENCIES[name] if d in agg_names]
            if deps:
                relevant_deps[name] = deps
    
    # Simple topological sort
    sorted_names = []
    remaining = agg_names.copy()
    
    # Keep track of iterations to prevent infinite loops
    max_iterations = len(remaining) * len(remaining)
    iteration = 0
    
    while remaining and iteration < max_iterations:
        iteration += 1
        for name in remaining[:]:  # Copy list to allow modification during iteration
            deps = relevant_deps.get(name, [])
            # Check if all dependencies are already in sorted_names
            if all(d in sorted_names for d in deps):
                sorted_names.append(name)
                remaining.remove(name)
                break
    
    # If we couldn't sort (circular dependency), just return original order
    if remaining:
        v_print(f"Warning: Could not resolve aggregation dependencies, using original order", verbose)
        return aggregations
    
    # Rebuild the aggregation list in sorted order
    sorted_aggregations = [agg_dict[name] for name in sorted_names]
    
    # Log if order changed
    if sorted_names != agg_names:
        v_print(f"Reordered aggregations for dependencies: {sorted_names}", verbose, 2)
    
    return sorted_aggregations


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
        self.sf_unmatched_found = False  # Separate flag for split_failures unmatched file
        self.known_detectors_settings = None # For incremental processing of actuations to track detectors with zero counts
        self.known_detectors_found = False
        self.incremental_run = False
        self.event_gap_schedule = None  # Set from the timeline params when timeline runs
        self.binned_actuations = None # For detector_health aggregation
        self.device_groups = None # For detector_health aggregation if groups are provided
        self.remove_incomplete = False
        self.to_sql = False
        self.verbose = 1 # 0: only print errors, 1: print performance, 2: print debug statements
        self.controller_type = '' # Controller type: '' (default), 'maxtime' or 'siemens' (case-insensitive)
        
        # Extract parameters from kwargs
        for key, value in kwargs.items():
            setattr(self, key, value)

        # Normalize controller_type to lowercase for consistent comparison
        if hasattr(self, 'controller_type') and self.controller_type:
            self.controller_type = str(self.controller_type).lower()

        # MAXTIME-specific measures that should be skipped for non-MAXTIME controllers
        MAXTIME_MEASURES = {'splits', 'coordination'}
        
        # Filter out MAXTIME-specific measures if controller_type is not 'maxtime'
        if hasattr(self, 'aggregations') and self.aggregations and self.controller_type != 'maxtime':
            original_count = len(self.aggregations)
            self.aggregations = [agg for agg in self.aggregations if agg['name'] not in MAXTIME_MEASURES]
            removed_count = original_count - len(self.aggregations)
            if removed_count > 0:
                removed_names = [agg['name'] for agg in kwargs.get('aggregations', []) if agg['name'] in MAXTIME_MEASURES]
                v_print(f"Skipped {removed_count} MAXTIME-specific measure(s): {removed_names}. Set controller_type='maxtime' to enable.", self.verbose)

        # Validate and sort aggregations by dependency order
        if hasattr(self, 'aggregations') and self.aggregations:
            # First validate that all required dependencies are present
            _validate_aggregation_dependencies(self.aggregations, self.verbose)
            # Then sort by dependency order (e.g., has_data before timeline, timeline before phase_wait)
            self.aggregations = _sort_aggregations_by_dependency(self.aggregations, self.verbose)

        # Check for valid bin_size and no_data_min combo
        if self.remove_incomplete:
            # raise error if 'has_data' aggregation is not in aggregations
            assert any(d['name'] == 'has_data' for d in self.aggregations), "Remove_incomplete requires 'has_data' aggregation!"
            # extract has_data parameters
            no_data_min = next(x['params']['no_data_min'] for x in self.aggregations if x['name'] == 'has_data')
            assert self.bin_size % no_data_min == 0, "bin_size / no_data_min must be a whole number"

        # Check format of unmatched_event_settings
        # Duckdb needs quotes if it is a file path, but not if it is a dataframe
        if self.unmatched_event_settings is not None:
            self.incremental_run = True
            self.unmatched_found = True
            self.sf_unmatched_found = True  # Separate flag for split_failures
            for key, value in self.unmatched_event_settings.items():
                if key == 'max_days_old':
                    continue
                if isinstance(value, str):
                    if os.path.exists(value) and value != '':
                        self.unmatched_event_settings[key] = f"'{value}'"
                    else:
                        v_print(f"Warning, {key} file '{value}' does not exist or is blank. This is expected only for the first run.", self.verbose)
                        # Set the appropriate flag based on which file is missing
                        if key == 'split_fail_df_or_path':
                            self.sf_unmatched_found = False
                        else:
                            self.unmatched_found = False
                elif value is None:
                    v_print(f"Warning, {key} file is None. This is expected only for the first run.", self.verbose)
                    # Set the appropriate flag based on which file is missing
                    if key == 'split_fail_df_or_path':
                        self.sf_unmatched_found = False
                    else:
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
            # Keep the inputs only on self, which aggregate() clears, so a large frame is not held
            # through the aggregation list for the life of the processor. Copies leave the caller's
            # list and dicts untouched.
            detector_health = self.aggregations[idx]
            self.aggregations = list(self.aggregations)
            self.aggregations[idx] = {**detector_health, 'params': {
                k: v for k, v in detector_health['params'].items() if k not in ('data', 'device_groups')
            }}
     
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
        # A DuckDB relation (e.g. sample_data) belongs to the connection that created it and cannot
        # be scanned from this one, so hand it over as Arrow, which stays columnar.
        if isinstance(self.raw_data, duckdb.DuckDBPyRelation):
            self.raw_data = _to_arrow(self.raw_data)
        if isinstance(self.detector_config, duckdb.DuckDBPyRelation):
            self.detector_config = _to_arrow(self.detector_config)
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
                self.min_timestamp, self.max_timestamp = self.conn.execute(
                    "SELECT MIN(timestamp), MAX(timestamp) FROM raw_data"
                ).fetchone()
                # Handle empty raw_data: use epoch timestamps so aggregations run with correct schema
                if self.min_timestamp is None:
                    self.min_timestamp = self.conn.execute("SELECT TIMESTAMP '1970-01-01 00:00:00'").fetchone()[0]
                    self.max_timestamp = self.min_timestamp
                    v_print('Empty raw_data detected!', self.verbose)
                else:
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
        
    def _invalidate_timeline_data_gaps(self):
        """Marks timeline rows and unmatched events invalid when they span a bin missing from has_data.

        Every bin an interval spans is checked, not just the ones it starts and ends in. has_data only
        covers the current run, so incremental runs carry a marker per device in unmatched_events
        (synthetic EventId 935) holding its last bin with data. The next run treats that bin as known and
        checks from it onward, which exposes a gap lying between runs or across runs that were skipped.
        Bins before the marker were checked by earlier runs, with the result kept in IsValid.
        """
        bin_interval = f"INTERVAL '{self.bin_size} minutes'"
        has_previous = self.conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'unmatched_previous'"
        ).fetchone()[0]
        if has_previous:
            previous_markers = "SELECT DeviceId, MAX(TimeStamp) AS MarkerBin FROM unmatched_previous WHERE EventId = 935 GROUP BY DeviceId"
        else:
            previous_markers = "SELECT DeviceId, TimeStamp AS MarkerBin FROM has_data WHERE FALSE"

        # Bins known to have data, and per device where checking starts (FirstBin) and how far the
        # current data reaches (LastBin). Without a marker, checking starts at the first bin of this run.
        self.conn.execute(f"""
            CREATE OR REPLACE TEMP TABLE gap_markers AS {previous_markers};
            CREATE OR REPLACE TEMP TABLE gap_known_bins AS
                SELECT DeviceId, TimeStamp FROM has_data
                UNION
                SELECT DeviceId, MarkerBin FROM gap_markers;
            CREATE OR REPLACE TEMP TABLE gap_bounds AS
                SELECT d.DeviceId,
                       COALESCE(m.MarkerBin, (SELECT TIME_BUCKET({bin_interval}, MIN(TimeStamp)) FROM raw_data)) AS FirstBin,
                       h.LastBin
                FROM (SELECT DISTINCT DeviceId FROM timeline UNION SELECT DISTINCT DeviceId FROM unmatched_events) d
                LEFT JOIN gap_markers m USING (DeviceId)
                LEFT JOIN (SELECT DeviceId, MAX(TimeStamp) AS LastBin FROM has_data GROUP BY DeviceId) h USING (DeviceId);
        """)

        # Completed intervals: check from the start bin (or FirstBin if later) through the end bin
        self.conn.execute(f"""
            UPDATE timeline SET IsValid = FALSE
            WHERE rowid IN (
                SELECT s.rid
                FROM (
                    SELECT t.rowid AS rid, t.DeviceId,
                           UNNEST(generate_series(
                               GREATEST(TIME_BUCKET({bin_interval}, t.StartTime), COALESCE(b.FirstBin, TIME_BUCKET({bin_interval}, t.StartTime))),
                               TIME_BUCKET({bin_interval}, t.EndTime),
                               {bin_interval})) AS bin_ts
                    FROM timeline t
                    LEFT JOIN gap_bounds b USING (DeviceId)
                    WHERE t.IsValid = TRUE AND t.EndTime IS NOT NULL
                ) s
                ANTI JOIN gap_known_bins k ON k.DeviceId = s.DeviceId AND k.TimeStamp = s.bin_ts
            );
        """)

        # Unmatched events: check from the start bin (or FirstBin if later) through the last bin with
        # data. Nothing past that is judged yet, the next run does it from the marker.
        self.conn.execute(f"""
            UPDATE unmatched_events SET IsValid = FALSE
            WHERE rowid IN (
                SELECT s.rid
                FROM (
                    SELECT u.rowid AS rid, u.DeviceId,
                           UNNEST(generate_series(
                               GREATEST(TIME_BUCKET({bin_interval}, u.TimeStamp), COALESCE(b.FirstBin, TIME_BUCKET({bin_interval}, u.TimeStamp))),
                               GREATEST(TIME_BUCKET({bin_interval}, u.TimeStamp), COALESCE(b.LastBin, TIME_BUCKET({bin_interval}, u.TimeStamp))),
                               {bin_interval})) AS bin_ts
                    FROM unmatched_events u
                    LEFT JOIN gap_bounds b USING (DeviceId)
                    WHERE u.IsValid = TRUE
                ) s
                ANTI JOIN gap_known_bins k ON k.DeviceId = s.DeviceId AND k.TimeStamp = s.bin_ts
            );
        """)

        if has_previous:
            # Rows whose start came from an earlier run inherit that run's verdict, whether the row was
            # completed in this run or is still unmatched
            self.conn.execute("""
                UPDATE timeline t SET IsValid = FALSE
                WHERE t.IsValid = TRUE
                  AND EXISTS (
                    SELECT 1 FROM unmatched_previous u
                    WHERE u.DeviceId = t.DeviceId AND u.TimeStamp = t.StartTime AND u.IsValid = FALSE
                      AND u.EventId NOT BETWEEN 931 AND 936 -- state markers, not interval starts
                  );
                UPDATE unmatched_events e SET IsValid = FALSE
                WHERE e.IsValid = TRUE
                  AND EXISTS (
                    SELECT 1 FROM unmatched_previous u
                    WHERE u.DeviceId = e.DeviceId AND u.TimeStamp = e.TimeStamp
                      AND u.EventId = e.EventId AND u.Parameter = e.Parameter AND u.IsValid = FALSE
                  );
            """)

        if self.incremental_run:
            # Save each device's last bin with data for the next run. A device with no data in this run
            # keeps its previous marker, so the gap is still measured from where its data stopped.
            self.conn.execute("""
                INSERT INTO unmatched_events
                SELECT TimeStamp, DeviceId, 935 AS EventId, 0 AS Parameter, TRUE AS IsValid
                FROM (
                    SELECT DeviceId, MAX(TimeStamp) AS TimeStamp FROM has_data GROUP BY DeviceId
                    UNION ALL
                    SELECT DeviceId, MarkerBin FROM gap_markers WHERE DeviceId NOT IN (SELECT DeviceId FROM has_data)
                );
            """)

        self.conn.execute("DROP TABLE gap_markers; DROP TABLE gap_known_bins; DROP TABLE gap_bounds;")

    def _invalidate_unmatched_event_gaps(self):
        """Marks unmatched events invalid when a device silence follows them, and saves each device's last event.

        timeline.sql already invalidates completed intervals that overlap a silence (event_gaps.sql). An event
        still unmatched at a silence will overlap it once matched, so it is marked invalid now and the next
        run inherits that through unmatched_previous.IsValid. Incremental runs save each device's last event
        time (synthetic EventId 936) so the next run measures a silence that crosses runs from it. A device
        with no data in this run keeps its previous marker.
        """
        has_previous = self.conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'unmatched_previous'"
        ).fetchone()[0] > 0
        event_gaps = render_query('event_gaps', unmatched=has_previous, event_gap_schedule=self.event_gap_schedule)
        self.conn.execute(f"""
            CREATE OR REPLACE TEMP TABLE event_gaps AS {event_gaps};
            UPDATE unmatched_events u SET IsValid = FALSE
            WHERE u.IsValid = TRUE
              AND u.EventId NOT BETWEEN 931 AND 936 -- state markers, not interval starts
              AND EXISTS (
                SELECT 1 FROM event_gaps g
                WHERE g.DeviceID = u.DeviceId AND g.StartTime >= u.TimeStamp
              );
            DROP TABLE event_gaps;
        """)

        if self.incremental_run:
            previous_markers = (
                "UNION ALL SELECT DeviceId, TimeStamp FROM unmatched_previous WHERE EventId = 936" if has_previous else ""
            )
            self.conn.execute(f"""
                INSERT INTO unmatched_events
                SELECT MAX(TimeStamp), DeviceId, 936 AS EventId, 0 AS Parameter, TRUE AS IsValid
                FROM (SELECT DeviceId, TimeStamp FROM raw_data {previous_markers})
                GROUP BY DeviceId;
            """)

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
                # traffic-anomaly builds ibis expressions; compiling them to SQL and running that here
                # keeps weeks of binned actuations inside DuckDB, which can spill to disk. Executing
                # them instead converts every intermediate result to pandas, and at tens of millions
                # of rows the string columns alone run to gigabytes per copy.
                self._register_detector_health_table('detector_health_input', self.binned_actuations)
                self.binned_actuations = None  # Clear reference
                decomp = traffic_anomaly.decompose(
                    self._ibis_table('detector_health_input'),
                    **aggregation['params']['decompose_params']
                )
                # Join groups to decomp
                if self.device_groups is not None:
                    self._register_detector_health_table('detector_health_groups', self.device_groups)
                    groups = self._ibis_table('detector_health_groups')
                    shared = [c for c in decomp.columns if c in groups.columns]
                    decomp = decomp.join(groups, shared)
                    # Exclude group_grouping_columns in anomaly_params
                    exclude_col = ', '.join(["'{}'".format(x) for x in aggregation['params']['anomaly_params']['group_grouping_columns']])
                    exclude_col = f"EXCLUDE ({exclude_col})"

                else:
                    exclude_col = ""
                # Find Anomalies
                anomaly_sql = traffic_anomaly.anomaly(
                    decomposed_data=decomp,
                    return_sql=True,
                    dialect='duckdb',
                    **aggregation['params']['anomaly_params']
                )
                # Keep the last return_last_n_days calendar days. The cutoff comes from the input so the
                # anomaly results can be filtered as they are written, rather than held in full just
                # to find their latest date.
                cutoff = self.conn.execute(f"""
                    SELECT MAX(TimeStamp)::DATE - INTERVAL '{aggregation['params']['return_last_n_days']-1}' DAY
                    FROM detector_health_input
                    """).fetchone()[0]
                # A nanosecond pandas TimeStamp arrives as TIMESTAMP_NS; store plain TIMESTAMP, as the
                # results always were when they came back through pandas
                timestamp_type = self.conn.execute(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'detector_health_input' AND column_name = 'TimeStamp'"
                ).fetchone()[0]
                replace_col = ("REPLACE (TimeStamp::TIMESTAMP AS TimeStamp)"
                               if timestamp_type in ('TIMESTAMP_NS', 'TIMESTAMP_MS', 'TIMESTAMP_S') else "")
                query = f"""CREATE OR REPLACE TABLE detector_health AS
                        SELECT * {exclude_col} {replace_col}
                        FROM ({anomaly_sql})
                        WHERE TimeStamp >= ?
                        """
                self.conn.execute(query, [cutoff])
                self._drop_detector_health_tables()
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
                params['controller_type'] = self.controller_type  # Global controller type setting
                
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
                    # TSP delays service without skipping it, so waits that saw an
                    # adjustment are judged against a looser threshold rather than
                    # excluded the way preempts are.
                    if 'tsp_skip_multiplier' not in params:
                        params['tsp_skip_multiplier'] = 2.0

                #######################
                ### Timeline ###
                # live mode keeps incomplete events in timeline output.
                # Supports `live_transform` as a backward-compatible alias.
                if aggregation['name'] == 'timeline':
                    if 'live' not in params:
                        params['live'] = bool(params.get('live_transform', False))
                    self.event_gap_schedule = _event_gap_schedule(
                        params.pop('max_event_gap_seconds', DEFAULT_MAX_EVENT_GAP_SECONDS))
                    params['event_gap_schedule'] = self.event_gap_schedule

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
                # coordination_agg and phase_wait also use unmatched events to store previous coordination state
                if self.incremental_run and aggregation['name'] in ['timeline', 'arrival_on_green', 'yellow_red', 'split_failures', 'coordination_agg', 'phase_wait']:
                    params['incremental_run'] = True #lets the aggregation know to save unmatched events for next run
                    # split_failures uses its own unmatched file (sf_unmatched), others use the main unmatched file
                    if aggregation['name'] == 'split_failures':
                        unmatched_available = self.sf_unmatched_found
                    else:
                        unmatched_available = self.unmatched_found
                    
                    if unmatched_available:
                        v_print(f"Incremental run using previous events for {aggregation['name']}", self.verbose, 2)
                        params['unmatched'] = True #lets the aggregation know to use the unmatched events from previous run
                        # split_failures, coordination_agg, and phase_wait use their own logic
                        if aggregation['name'] not in ['split_failures', 'coordination_agg', 'phase_wait']:
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
                
                # After has_data aggregation, NO merge needed for incremental - validity is tracked per-event
                # via the IsValid column in unmatched_events
                
                # After timeline aggregation, check if has_data table exists and mark events
                # spanning missing data periods as invalid
                if aggregation['name'] == 'timeline' and not self.to_sql:
                    has_data_exists = self.conn.execute(
                        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'has_data'"
                    ).fetchone()[0]
                    
                    if has_data_exists:
                        v_print("Marking timeline events with missing has_data as invalid", self.verbose, 2)
                        self._invalidate_timeline_data_gaps()

                    # timeline.sql invalidates completed intervals that overlap a controller clock update (181)
                    # window of 5 seconds either side. An unmatched event starting before a window ends will
                    # overlap it once matched, so mark it invalid now and the next incremental run inherits
                    # that through unmatched_previous.IsValid
                    self.conn.execute("""
                        UPDATE unmatched_events u
                        SET IsValid = FALSE
                        WHERE u.IsValid = TRUE
                          AND u.EventId NOT BETWEEN 931 AND 936 -- state markers, not interval starts
                          AND EXISTS (
                            SELECT 1 FROM raw_data r
                            WHERE r.EventId = 181
                              AND r.DeviceId = u.DeviceId
                              AND r.TimeStamp + INTERVAL 5 SECOND > u.TimeStamp
                          );
                    """)

                    # Same for a Siemens hourly log restart (1000): its window ends at the restart, so an
                    # event still open afterwards overlaps it if it started at or before the restart
                    if self.controller_type == 'siemens':
                        self.conn.execute("""
                            UPDATE unmatched_events u
                            SET IsValid = FALSE
                            WHERE u.IsValid = TRUE
                              AND u.EventId NOT BETWEEN 931 AND 936
                              AND EXISTS (
                                SELECT 1 FROM raw_data r
                                WHERE r.EventId = 1000
                                  AND r.DeviceId = u.DeviceId
                                  AND r.TimeStamp >= u.TimeStamp
                              );
                        """)

                    if self.event_gap_schedule:
                        self._invalidate_unmatched_event_gaps()

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
                    GREATEST(
                        COALESCE(MAX(kd.LastSeen), TIMESTAMP '1970-01-01 00:00:00'),
                        COALESCE(MAX(cd.LastSeen), TIMESTAMP '1970-01-01 00:00:00')
                    ) as LastSeen
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
    
    def _register_detector_health_table(self, name, source):
        """Expose a DataFrame, or a file path DuckDB can read, as a view for detector_health."""
        if isinstance(source, str):
            path = _quote_path(_strip_wrapping_quotes(source))
            self.conn.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM {path}")
        else:
            self.conn.register(name, source)

    def _drop_detector_health_tables(self):
        """Remove the detector_health input views, releasing any DataFrame registered behind them."""
        for name in ('detector_health_input', 'detector_health_groups'):
            try:
                self.conn.unregister(name)
            except Exception:
                pass  # not a registered DataFrame
            self.conn.execute(f"DROP VIEW IF EXISTS {name}")

    def _ibis_table(self, name):
        """An unbound ibis table with the schema of a view in this connection, so the traffic-anomaly
        expressions built on it compile to SQL that reads that view."""
        arrow_schema = _to_arrow(self.conn.sql(f"SELECT * FROM {name} LIMIT 0")).schema
        return ibis.table(ibis.Schema.from_pyarrow(arrow_schema), name=name)

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

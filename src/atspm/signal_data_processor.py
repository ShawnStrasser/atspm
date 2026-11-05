import duckdb
import time
from .data_loader import load_data
from .data_aggregator import aggregate_data, render_query
from .data_saver import save_data
from .detector_health import detector_health_params, run_detector_health
from .utils import COMPLETE_BINS, STATE_EVENT_IDS, WRITE_BINS_AT_ONCE
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

# Aggregations that read what arrival_on_green carries between runs (see arrival_on_green_carry.sql).
ARRIVAL_CARRY_AGGREGATIONS = ('arrival_on_green', 'platoon_ratio')

# An advance detector ON for more than this many seconds has a queue over it, and keeps the flag until it has been OFF
# for the gap (see arrival_spillback.sql). spillback_on_seconds=None turns the flag off.
DEFAULT_SPILLBACK_ON_SECONDS = 6
DEFAULT_SPILLBACK_GAP_SECONDS = 5

# The default time lapse before a new unique ped actuation is counted.
DEFAULT_SECONDS_BETWEEN_ACTUATIONS = 15

# arrival_pattern's Red_Offset is rounded to this. Advance detectors are far busier than yellow_red ones: on 150 ODOT signals
# over one day, 1,445 rows per phase-day at 5 seconds and 3,963 at half a second.
DEFAULT_OFFSET_SLICE_SECONDS = 5

# Aggregations that keep what they have not written yet in the main unmatched file, each with its own rows.
OWN_CARRY_AGGREGATIONS = (*ARRIVAL_CARRY_AGGREGATIONS, 'ped', 'yellow_red', 'arrival_pattern', 'approach_delay', 'left_turn_gaps')

# timeline's instant events (see timeline.sql)
INSTANT_EVENT_CLASSES = ('Pattern Change', 'Cycle Length Change', 'Offset Change', 'Interval Advance', 'Clock Update', 'Power Failure', 'Power Restored')

# Every setting a run takes. A misspelled name would otherwise do nothing, with no message. detector_health checks its own.
SETTINGS = {'raw_data', 'detector_config', 'bin_size', 'output_dir', 'output_to_separate_folders', 'output_format',
            'output_file_prefix', 'remove_incomplete', 'drop_fall_back_hour', 'unmatched_event_settings', 'to_sql', 'verbose',
            'aggregations', 'controller_type'}
STATE_SETTINGS = {'df_or_path', 'split_fail_df_or_path', 'max_days_old', 'first_run'}
AGGREGATION_PARAMS = {
    'has_data': set(),
    'actuations': {'fill_in_missing', 'known_detectors_max_days_old'},
    'arrival_on_green': {'latency_offset_seconds', 'spillback_on_seconds', 'spillback_gap_seconds'},
    'approach_delay': {'latency_offset_seconds', 'spillback_on_seconds', 'spillback_gap_seconds'},
    'left_turn_gaps': set(),
    'platoon_ratio': set(),
    'yellow_red': {'latency_offset_seconds', 'min_red_offset'},
    'arrival_pattern': {'latency_offset_seconds', 'offset_slice_seconds'},
    'split_failures': {'red_time', 'red_occupancy_threshold', 'green_occupancy_threshold', 'by_approach', 'by_cycle'},
    'terminations': set(),
    'splits': set(),
    'coordination': set(),
    'coordination_agg': set(),
    'ped': {'seconds_between_actuations', 'return_volumes'},
    'ped_delay': set(),
    'phase_wait': {'preempt_recovery_seconds', 'assumed_cycle_length', 'skip_multiplier', 'tsp_skip_multiplier'},
    'timeline': {'maxtime', 'min_duration', 'cushion_time', 'max_event_gap_seconds', 'live', 'live_transform'},
}


# The table each synthetic row of the main unmatched file belongs to. Any other row is a timeline interval still open. 936 and 942
# are every table's.
STATE_TABLES = {**dict.fromkeys(range(927, 931), 'left_turn_gaps'), **dict.fromkeys(range(931, 935), 'coordination_agg'), 935: 'timeline', 955: 'timeline',
                **dict.fromkeys((937, 943, 944, 945, 946, 947), 'ped'),
                **dict.fromkeys((938, 939, 940, 941, 948, 949, 950), 'arrival_on_green'),
                **dict.fromkeys((951, 952, 953, 954), 'yellow_red'), 956: 'actuations',
                **dict.fromkeys((957, 958, 959), 'approach_delay'), **dict.fromkeys((960, 961, 962, 963), 'arrival_pattern'), **dict.fromkeys(range(970, 1000), 'has_data'), 936: None, 942: None}


def _check_names(settings):
    """Raises on a setting or aggregation parameter this package does not have."""
    unknown = sorted(set(settings) - SETTINGS)
    if unknown:
        raise ValueError(f"Unknown settings {unknown}; it takes {sorted(SETTINGS)}")
    unknown = sorted(set(settings.get('unmatched_event_settings') or {}) - STATE_SETTINGS)
    if unknown:
        raise ValueError(f"unmatched_event_settings: unknown names {unknown}; it takes {sorted(STATE_SETTINGS)}")
    for agg in settings.get('aggregations') or []:
        known = AGGREGATION_PARAMS.get(agg['name'])
        unknown = [] if known is None else sorted(set(agg.get('params', {})) - known)
        if unknown:
            raise ValueError(f"{agg['name']}: unknown parameters {unknown}; it takes {sorted(known)}")
        if agg['name'] == 'split_failures' and agg.get('params', {}).get('by_approach') is False:
            raise ValueError("split_failures: by_approach=False is not supported; only by_approach=True works")


def _state_given(name, value, first_run, verbose):
    """Whether there is earlier state to read. '' or None says this is a first run, and so does first_run for a file that does
    not exist yet. Any other missing file raises: carrying on would read as a first run, and the rows would be quietly wrong."""
    if value is None or (isinstance(value, str) and value == ''):
        v_print(f"First run: no earlier {name}.", verbose)
        return False
    if isinstance(value, str) and not os.path.exists(value):
        if first_run:
            v_print(f"First run: {name} file '{value}' does not exist yet.", verbose)
            return False
        raise FileNotFoundError(f"{name} file '{value}' does not exist. For a first run, add 'first_run': True to "
                                "unmatched_event_settings, or pass '' or None.")
    return True


def _spillback_params(params):
    """arrival_on_green or approach_delay params with both spillback settings, checked. A copy, leaving the caller's dict alone."""
    params = {**params}
    params.setdefault('spillback_on_seconds', DEFAULT_SPILLBACK_ON_SECONDS)
    params.setdefault('spillback_gap_seconds', DEFAULT_SPILLBACK_GAP_SECONDS)
    for name in ('spillback_on_seconds', 'spillback_gap_seconds'):
        value = params[name]
        if value is None and name == 'spillback_on_seconds':
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
            raise ValueError(f"{name} must be a positive number of seconds" + (", or None to turn the flag off" if name == 'spillback_on_seconds' else ""))
    return params


def _arrival_pattern_params(params):
    """arrival_pattern params with the slice size, checked. A copy, leaving the caller's dict alone."""
    params = {**params}
    value = params.setdefault('offset_slice_seconds', DEFAULT_OFFSET_SLICE_SECONDS)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        raise ValueError("offset_slice_seconds must be a positive number of seconds")
    return params


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
        The detector configuration, either as a file path or a DataFrame. Columns: DeviceId, Phase, Parameter, Function,
        and optionally DistanceFromStopBar (feet), MPH and LatencyCorrection (seconds), which arrival_on_green, approach_delay and arrival_pattern use to project arrivals to the stop bar.
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
        If True, removes bins the 'has_data' aggregation scores under 0.5, or has no row for.
    drop_fall_back_hour : bool
        If True (the default), events in 1:00-2:00 am on the first Sunday of November are dropped when raw_data is
        loaded, because the two passes through that hour when clocks fall back cannot be separated. Assumes US
        daylight saving rules and raw_data in local time. Set False if your data is not.
    unmatched_event_settings : dict, optional
        Settings for handling unmatched events in incremental processing. Includes:
        - df_or_path: str, path to save/load unmatched events
        - split_fail_df_or_path: str, path to save/load unmatched split failure events
        - max_days_old: int, maximum age of unmatched events to consider
        - first_run: bool, these files are not there yet. Without it a missing file raises. '' or None in place of a path also says first run
    to_sql : bool
        If True, returns SQL strings instead of executing queries.
    verbose : int
        Controls the verbosity of output. 0: only errors, 1: performance, 2: debug statements.
    aggregations : list of dict
        A list of dictionaries, each containing the name of an aggregation function and its parameters.
        A table is written when listed here, and no others.
        Supported aggregations include: 'has_data', 'actuations', 'arrival_on_green', 'approach_delay', 'left_turn_gaps',
        'coordination', 'coordination_agg', 'ped', 'split_failures', 'splits', 'terminations',
        'yellow_red', 'arrival_pattern', 'timeline', 'ped_delay', 'phase_wait', and potentially others.

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
            {'name': 'has_data', 'params': {}},
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
        _check_names(kwargs)
        # Optional parameters
        self.raw_data = None
        self.detector_config = None
        self.unmatched_event_settings = None # For incremental processing of timeline, split failure, arrival on green, and yellow red)
        self.unmatched_found = False
        self.sf_unmatched_found = False  # Separate flag for split_failures unmatched file
        self.known_detector_days = None # Set from the actuations params, how long a silent detector stays on the list
        self.incremental_run = False
        self.event_gap_schedule = None  # Set from the timeline params when timeline runs
        self.remove_incomplete = False
        self.drop_fall_back_hour = True # Drop events in the hour US clocks repeat when they fall back
        self.to_sql = False
        self.verbose = 1 # 0: only print errors, 1: print performance, 2: print debug statements
        self.controller_type = '' # Controller type: '' (default), 'maxtime' or 'siemens' (case-insensitive)
        
        # Extract parameters from kwargs
        for key, value in kwargs.items():
            setattr(self, key, value)

        # Copies: a run changes these, and the caller may hand the same dicts to its next run
        if self.unmatched_event_settings is not None:
            self.unmatched_event_settings = dict(self.unmatched_event_settings)
        if hasattr(self, 'aggregations'):
            self.aggregations = [{**agg, 'params': dict(agg.get('params', {}))} for agg in self.aggregations]

        # Normalize controller_type to lowercase for consistent comparison
        if hasattr(self, 'controller_type') and self.controller_type:
            self.controller_type = str(self.controller_type).lower()

        # Validate and sort aggregations by dependency order
        if hasattr(self, 'aggregations') and self.aggregations:
            # First validate that all required dependencies are present
            _validate_aggregation_dependencies(self.aggregations, self.verbose)
            # Then sort by dependency order (e.g., has_data before timeline, timeline before phase_wait)
            self.aggregations = _sort_aggregations_by_dependency(self.aggregations, self.verbose)

        if self.remove_incomplete:
            # raise error if 'has_data' aggregation is not in aggregations
            assert any(d['name'] == 'has_data' for d in self.aggregations), "Remove_incomplete requires 'has_data' aggregation!"
            # Every other aggregation is then joined to it, wherever it was listed
            self.aggregations.sort(key=lambda agg: agg['name'] != 'has_data')

        # Check format of unmatched_event_settings
        # Duckdb needs quotes if it is a file path, but not if it is a dataframe
        if self.unmatched_event_settings is not None:
            self.incremental_run = True
            self.unmatched_found = True
            self.sf_unmatched_found = True  # Separate flag for split_failures
            first_run = self.unmatched_event_settings.get('first_run', False)
            for key, value in self.unmatched_event_settings.items():
                if key in ('max_days_old', 'first_run'):
                    continue
                if _state_given(key, value, first_run, self.verbose):
                    if isinstance(value, str):
                        self.unmatched_event_settings[key] = f"'{value}'"
                elif key == 'split_fail_df_or_path':
                    self.sf_unmatched_found = False
                else:
                    self.unmatched_found = False

        # With fill_in_missing, state keeps each device's detectors, so one silent for a whole run still gets its zero rows
        for agg in self.aggregations:
            if agg['name'] == 'actuations' and agg['params'].get('fill_in_missing') and 'df_or_path' in (self.unmatched_event_settings or {}):
                self.known_detector_days = agg['params'].get('known_detectors_max_days_old', 2)

        # Check the detector_health parameters now rather than after the other aggregations have run. A copy leaves
        # the caller's list and dicts untouched.
        self.aggregations = [
            {**agg, 'params': detector_health_params(agg.get('params', {}))} if agg['name'] == 'detector_health'
            else {**agg, 'params': _spillback_params(agg.get('params', {}))} if agg['name'] in ('arrival_on_green', 'approach_delay')
            else {**agg, 'params': _arrival_pattern_params(agg.get('params', {}))} if agg['name'] == 'arrival_pattern'
            else agg
            for agg in self.aggregations
        ]
     
        # Establish a connection to the database
        self.conn = duckdb.connect()
        # Track whether connection has been closed
        self._closed = False
        # Track whether data has been loaded
        self.data_loaded = False

        # Use connection to get current timestamp
        # This is a placeholder until data is loaded, and stays when to_sql is True.
        self.max_timestamp = self.conn.execute("SELECT CURRENT_TIMESTAMP").fetchall()[0][0]
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
                    self.unmatched_found or self.sf_unmatched_found,
                    self.known_detector_days,
                    self.drop_fall_back_hour)
            # delete self.raw_data and self.detector_config to free up memory
            self.data_loaded = True
            if self.raw_data is not None:
                self.min_timestamp, self.max_timestamp = self.conn.execute(
                    "SELECT MIN(timestamp), MAX(timestamp) FROM raw_data"
                ).fetchall()[0]
                # Handle empty raw_data: use epoch timestamps so aggregations run with correct schema
                if self.min_timestamp is None:
                    self.min_timestamp = self.conn.execute("SELECT TIMESTAMP '1970-01-01 00:00:00'").fetchall()[0][0]
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
            v_print('Make sure detector_config column names are: DeviceId, Phase, Parameter, Function (and optionally DistanceFromStopBar, MPH, LatencyCorrection)', self.verbose)
            v_print('*'*50, self.verbose)
            raise e
        self._check_input_follows_state()
        self._check_state_tables_listed()

    def _check_state_tables_listed(self):
        """Raises if the main unmatched file holds rows of a table not in this run's list. The run replaces the file, so a later
        run of that table would start with nothing from before."""
        if not self._table_exists('unmatched_previous'):
            return
        held = self.conn.execute("SELECT DISTINCT EventId FROM unmatched_previous").fetchall()
        tables = {STATE_TABLES.get(event_id, 'timeline') for event_id, in held} - {None}
        missing = sorted(tables - {agg['name'] for agg in self.aggregations})
        if missing:
            raise ValueError(f"The state holds rows of {missing}, which are not in aggregations. This run would drop them, and the next run of "
                             "those tables would come out wrong. List them, or give this list its own state file.")

    def _check_input_follows_state(self):
        """Raises if a device's input does not start after the last event the state holds for it. That input was run already,
        so its rows would be written again. Runs that follow on never get here: a run's events all come before the next one's.
        Also raises on a cut inside a bin, if a listed table would write that bin again."""
        held = []
        if self._table_exists('unmatched_previous'):
            held.append("SELECT DeviceId, TimeStamp FROM unmatched_previous WHERE EventId = 936")
        if self._table_exists('sf_unmatched_previous'):
            held.append("SELECT DeviceId, TimeStamp FROM sf_unmatched_previous")
        if not held or not self._table_exists('raw_data'):
            return
        def first_device(condition):
            return self.conn.execute(f"""
                SELECT DeviceId, First_Event, Last_Held, COUNT(*) OVER ()
                FROM (SELECT DeviceId, MIN(TimeStamp) AS First_Event FROM raw_data GROUP BY DeviceId)
                JOIN (SELECT DeviceId, MAX(TimeStamp) AS Last_Held FROM ({' UNION ALL '.join(held)}) GROUP BY DeviceId) USING (DeviceId)
                WHERE {condition}
                ORDER BY DeviceId LIMIT 1
            """).fetchall()

        older = first_device("First_Event <= Last_Held")
        if older:
            device, first, last, devices = older[0]
            raise ValueError(f"raw_data is older than the state: device {device} starts at {first}, and the state holds an event of it "
                             f"at {last} ({devices} devices like this). That input has been run already. To run it again, start from the state saved before it.")
        written_twice = [agg['name'] for agg in self.aggregations if agg['name'] in WRITE_BINS_AT_ONCE]
        if written_twice:
            bin_interval = f"INTERVAL '{self.bin_size} minutes'"
            inside = first_device(f"TIME_BUCKET({bin_interval}, First_Event) = TIME_BUCKET({bin_interval}, Last_Held)")
            if inside:
                device, first, last, devices = inside[0]
                raise ValueError(f"Runs must be cut on the bin for {written_twice}, which would write this bin twice: device {device} starts at {first}, "
                                 f"in the same {self.bin_size}-minute bin as its last event in the state, at {last} ({devices} devices like this).")

    def _invalidate_timeline_data_gaps(self):
        """Marks timeline rows and unmatched events invalid when they span a bin missing from has_data.

        Every bin an interval spans is checked, not just the ones it starts and ends in. has_data only
        covers the current run, so incremental runs carry a marker per device in unmatched_events
        (synthetic EventId 935) holding its last bin with data. The next run treats that bin as known and
        checks from it onward, which exposes a gap lying between runs or across runs that were skipped.
        Bins before the marker were checked by earlier runs, with the result kept in IsValid.
        """
        bin_interval = f"INTERVAL '{self.bin_size} minutes'"
        # An instant event has no real duration: its end is only the cushion, which can reach a bin the run has not seen
        interval_end = f"CASE WHEN t.EventClass IN {INSTANT_EVENT_CLASSES} THEN t.StartTime ELSE t.EndTime END"
        has_previous = self.conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'unmatched_previous'"
        ).fetchall()[0][0]
        if has_previous:
            previous_markers = "SELECT DeviceId, MAX(TimeStamp) AS MarkerBin FROM unmatched_previous WHERE EventId = 935 GROUP BY DeviceId"
        else:
            previous_markers = f"SELECT DeviceId, TimeStamp AS MarkerBin FROM {COMPLETE_BINS} WHERE FALSE"

        # Bins known to have data, and per device where checking starts (FirstBin) and how far the
        # current data reaches (LastBin). Without a marker, checking starts at the first bin of this run.
        self.conn.execute(f"""
            CREATE OR REPLACE TEMP TABLE gap_markers AS {previous_markers};
            CREATE OR REPLACE TEMP TABLE gap_known_bins AS
                SELECT DeviceId, TimeStamp FROM {COMPLETE_BINS}
                UNION
                SELECT DeviceId, MarkerBin FROM gap_markers
                UNION
                -- Not finished yet, so nothing is known against them
                SELECT s.DeviceId, UNNEST(GENERATE_SERIES(s.TimeStamp, s.TimeStamp + INTERVAL 1 DAY, {bin_interval})) FROM has_data_state s WHERE s.EventId = 974;
            CREATE OR REPLACE TEMP TABLE gap_bounds AS
                SELECT d.DeviceId,
                       COALESCE(m.MarkerBin, (SELECT TIME_BUCKET({bin_interval}, MIN(TimeStamp)) FROM raw_data)) AS FirstBin,
                       h.LastBin
                FROM (SELECT DISTINCT DeviceId FROM timeline UNION SELECT DISTINCT DeviceId FROM unmatched_events) d
                LEFT JOIN gap_markers m USING (DeviceId)
                LEFT JOIN (SELECT DeviceId, MAX(TimeStamp) AS LastBin FROM {COMPLETE_BINS} GROUP BY DeviceId) h USING (DeviceId);
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
                               TIME_BUCKET({bin_interval}, {interval_end}),
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
            self.conn.execute(f"""
                UPDATE timeline t SET IsValid = FALSE
                WHERE t.IsValid = TRUE
                  AND EXISTS (
                    SELECT 1 FROM unmatched_previous u
                    WHERE u.DeviceId = t.DeviceId AND u.TimeStamp = t.StartTime AND u.IsValid = FALSE
                      AND u.EventId NOT {STATE_EVENT_IDS} -- state markers, not interval starts
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
            self.conn.execute(f"""
                INSERT INTO unmatched_events
                SELECT TimeStamp, DeviceId, 935 AS EventId, 0 AS Parameter, TRUE AS IsValid
                FROM (
                    SELECT DeviceId, MAX(TimeStamp) AS TimeStamp FROM {COMPLETE_BINS} GROUP BY DeviceId
                    UNION ALL
                    SELECT DeviceId, MarkerBin FROM gap_markers WHERE DeviceId NOT IN (SELECT DeviceId FROM {COMPLETE_BINS})
                );
            """)

        self.conn.execute("DROP TABLE gap_markers; DROP TABLE gap_known_bins; DROP TABLE gap_bounds;")

    def _invalidate_unmatched_event_gaps(self):
        """Marks unmatched events invalid when a device silence follows them.

        timeline.sql already invalidates completed intervals that overlap a silence (event_gaps.sql). An event
        still unmatched at a silence will overlap it once matched, so it is marked invalid now and the next
        run inherits that through unmatched_previous.IsValid. The next run measures a silence that crosses
        runs from each device's last event (synthetic EventId 936).
        """
        has_previous = self.conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'unmatched_previous'"
        ).fetchall()[0][0] > 0
        event_gaps = render_query('event_gaps', unmatched=has_previous, event_gap_schedule=self.event_gap_schedule)
        self.conn.execute(f"""
            CREATE OR REPLACE TEMP TABLE event_gaps AS {event_gaps};
            UPDATE unmatched_events u SET IsValid = FALSE
            WHERE u.IsValid = TRUE
              AND u.EventId NOT {STATE_EVENT_IDS} -- state markers, not interval starts
              AND EXISTS (
                SELECT 1 FROM event_gaps g
                WHERE g.DeviceID = u.DeviceId AND g.StartTime >= u.TimeStamp
              );
            DROP TABLE event_gaps;
        """)

    def _save_last_events(self):
        """Saves each device's last event (synthetic EventId 936). A device with no data in this run keeps its previous one."""
        previous = "UNION ALL SELECT DeviceId, TimeStamp FROM unmatched_previous WHERE EventId = 936" if self._table_exists('unmatched_previous') else ""
        self._add_to_unmatched_events(f"""
            SELECT MAX(TimeStamp) AS TimeStamp, DeviceId, 936::INT16 AS EventId, 0::INT16 AS Parameter, TRUE AS IsValid
            FROM (SELECT DeviceId, TimeStamp FROM raw_data {previous})
            GROUP BY DeviceId
        """)

    def _save_last_phase_calls(self):
        """Saves each device's last phase call event (synthetic EventId 955), so the next run knows whether calls were
        being logged during a Phase Wait that started before it. A device with none in this run keeps its previous one."""
        previous = "UNION ALL SELECT DeviceId, TimeStamp FROM unmatched_previous WHERE EventId = 955" if self._table_exists('unmatched_previous') else ""
        self.conn.execute(f"""
            INSERT INTO unmatched_events
            SELECT MAX(TimeStamp), DeviceId, 955 AS EventId, 0 AS Parameter, TRUE AS IsValid
            FROM (SELECT DeviceId, TimeStamp FROM raw_data WHERE EventId IN (43, 44) {previous})
            GROUP BY DeviceId;
        """)

    def _save_known_detectors(self):
        """Saves each detector's last on event (synthetic EventId 956), read by actuations. One silent in this run keeps its previous one."""
        previous = "UNION ALL SELECT TimeStamp, DeviceId, Parameter FROM unmatched_previous WHERE EventId = 956" if self._table_exists('unmatched_previous') else ""
        self._add_to_unmatched_events(f"""
            SELECT MAX(TimeStamp) AS TimeStamp, DeviceId, 956::INT16 AS EventId, Parameter, TRUE AS IsValid
            FROM (SELECT TimeStamp, DeviceId, Parameter FROM raw_data WHERE EventId = 82 {previous})
            GROUP BY DeviceId, Parameter
        """)

    def _table_exists(self, name):
        return self.conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?", [name]
        ).fetchall()[0][0] > 0

    def _save_arrival_carry(self):
        """Saves what arrival_on_green and platoon_ratio have not written yet to unmatched_events.

        See arrival_on_green_carry.sql.
        """
        params = next(agg for agg in self.aggregations if agg['name'] == 'arrival_on_green').get('params', {})
        carry = render_query(
            'arrival_on_green_carry',
            **params,
            bin_size=self.bin_size,
            from_table='raw_data',
            carry_bins=True,
            unmatched=self._table_exists('unmatched_previous'),
        )
        self._add_to_unmatched_events(carry)

    def _save_red_cycles_carry(self, name):
        """Saves the cycles yellow_red or arrival_pattern has not finished writing to unmatched_events.

        See yellow_red_carry.sql.
        """
        params = next(agg for agg in self.aggregations if agg['name'] == name).get('params', {})
        self._add_to_unmatched_events(render_query(
            f'{name}_carry',
            **params,
            bin_size=self.bin_size,
            from_table='raw_data',
            carry_bins=True,
            unmatched=self._table_exists('unmatched_previous'),
        ))

    def _save_approach_delay_carry(self):
        """Saves what approach_delay has not written yet to unmatched_events (synthetic EventId 957 to 959).

        See approach_delay_vehicles.sql. A run with no events changes nothing, and keeps them for the next run.
        """
        if self.conn.execute("SELECT COUNT(*) FROM raw_data").fetchall()[0][0] > 0:
            params = next(agg for agg in self.aggregations if agg['name'] == 'approach_delay').get('params', {})
            self._add_to_unmatched_events(render_query(
                'approach_delay_carry',
                **params,
                bin_size=self.bin_size,
                from_table='raw_data',
                carry_bins=True,
                unmatched=self._table_exists('unmatched_previous'),
            ))
        elif self._table_exists('unmatched_previous'):
            self._add_to_unmatched_events(
                "SELECT TimeStamp, DeviceId, EventId, Parameter, IsValid FROM unmatched_previous WHERE EventId BETWEEN 957 AND 959"
            )

    def _save_left_turn_gaps_carry(self):
        """Saves what left_turn_gaps has not written yet to unmatched_events (synthetic EventId 927 to 930).

        See left_turn_gaps_stretches.sql. A run with no events changes nothing, and keeps them for the next run.
        """
        if self.conn.execute("SELECT COUNT(*) FROM raw_data").fetchall()[0][0] > 0:
            self._add_to_unmatched_events(render_query(
                'left_turn_gaps_carry',
                bin_size=self.bin_size,
                from_table='raw_data',
                carry_bins=True,
                unmatched=self._table_exists('unmatched_previous'),
            ))
        elif self._table_exists('unmatched_previous'):
            self._add_to_unmatched_events(
                "SELECT TimeStamp, DeviceId, EventId, Parameter, IsValid FROM unmatched_previous WHERE EventId BETWEEN 927 AND 930"
            )

    def _save_complete_bins(self):
        """Saves the bins has_data passed from where results stopped onward, for a later run's remove_incomplete.

        See complete_bins_carry.sql.
        """
        self._add_to_unmatched_events(render_query(
            'complete_bins_carry',
            bin_size=self.bin_size,
            carry_bins=True,
            unmatched=self._table_exists('unmatched_previous'),
            red_cycles_carried=any(agg['name'] in ('yellow_red', 'arrival_pattern') for agg in self.aggregations),
            approach_delay_carried=any(agg['name'] == 'approach_delay' for agg in self.aggregations),
            left_turn_gaps_carried=any(agg['name'] == 'left_turn_gaps' for agg in self.aggregations),
        ))

    def _ped_carried(self, params):
        """Whether ped reads the bin counts carried from an earlier run.

        An empty run has no dependable DeviceId type and writes nothing, so it leaves them out and
        _save_ped_carry keeps them for the next run.
        """
        if self.to_sql or not params.get('unmatched', False):
            return False
        carried = self.conn.execute("SELECT COUNT(*) FROM unmatched_previous WHERE EventId BETWEEN 943 AND 947").fetchall()[0][0]
        return carried > 0 and self.conn.execute("SELECT COUNT(*) FROM raw_data").fetchall()[0][0] > 0

    def _save_ped_carry(self, params):
        """Saves what ped needs from this run to unmatched_events (synthetic EventId 943 to 947). See ped_state.sql."""
        if self.conn.execute("SELECT COUNT(*) FROM raw_data").fetchall()[0][0] > 0:
            self._add_to_unmatched_events(render_query('ped_carry', **params))
        elif self._table_exists('unmatched_previous'):
            self._add_to_unmatched_events(
                "SELECT TimeStamp, DeviceId, EventId, Parameter, IsValid FROM unmatched_previous WHERE EventId BETWEEN 943 AND 947"
            )

    def _add_to_unmatched_events(self, rows):
        """Adds rows to unmatched_events. A run without timeline has no such table yet, so it is created here if a
        main unmatched file was asked for."""
        if self._table_exists('unmatched_events'):
            self.conn.execute(f"INSERT INTO unmatched_events {rows}")
        elif 'df_or_path' in (self.unmatched_event_settings or {}):
            self.conn.execute(f"CREATE TABLE unmatched_events AS {rows}")

    def _save_last_ped_presses(self):
        """Saves each input's last press to unmatched_events (synthetic EventId 937), one row per input.

        The next run's ped counts its first press against it. An input with no press in
        this run keeps its previous row until max_days_old drops it. A run without timeline has no
        unmatched_events yet, so the table is created if a main unmatched file was asked for.
        """
        previous_presses = (
            "UNION ALL SELECT TimeStamp, DeviceId, Parameter FROM unmatched_previous WHERE EventId = 937"
            if self._table_exists('unmatched_previous') else ""
        )
        self._add_to_unmatched_events(f"""
            SELECT MAX(TimeStamp) AS TimeStamp, DeviceId, 937::INT16 AS EventId, Parameter, TRUE AS IsValid
            FROM (SELECT TimeStamp, DeviceId, Parameter FROM raw_data WHERE EventId = 90 {previous_presses})
            GROUP BY DeviceId, Parameter
        """)

    def aggregate(self):
        """Runs all aggregations."""
        # Instantiate a dictionary to store runtimes
        self.runtimes = {}
        self.sql_queries = {} # for storing sql string when to_sql is True
        ped_params = None # ped's params, for saving its carry

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
            ### Relies on the atspm-detector package instead
            if aggregation['name'] == 'detector_health':
                if self.to_sql:
                    raise ValueError("to_sql option is not supported for detector_health")
                run_detector_health(self.conn, aggregation['params'], self.verbose, self.bin_size)
                # no external sql file like other aggregations, so just continue
                end_time = time.time()
                self.runtimes[aggregation['name']] = end_time - start_time
                continue
            else:
                # Dependencies: ped_delay, phase_wait, and coordination_agg require the timeline table to exist
                if aggregation['name'] in ('ped_delay', 'phase_wait', 'coordination_agg') and not self.to_sql:
                    has_timeline = self.conn.execute(
                        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'timeline'"
                    ).fetchall()[0][0]
                    if has_timeline == 0:
                        raise ValueError(f"{aggregation['name']} aggregation requires the timeline table. Run timeline first.")
                
                # Dependencies: coordination_agg also requires the has_data table to exist
                if aggregation['name'] == 'coordination_agg' and not self.to_sql:
                    has_has_data = self.conn.execute(
                        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'has_data'"
                    ).fetchall()[0][0]
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
                ### Pedestrian ###
                if aggregation['name'] == 'ped' and 'seconds_between_actuations' not in params:
                    params['seconds_between_actuations'] = DEFAULT_SECONDS_BETWEEN_ACTUATIONS

                #######################
                ### Unmatched Events ##
                # If unmatched_event_settings is supplied, then change the from_table for timeline, split_failures, arrival_on_green, approach_delay, left_turn_gaps and yellow_red
                # These are views that have the relateded unmatched events unioned to them
                # coordination_agg and phase_wait also use unmatched events to store previous coordination state
                # ped uses them to store each input's last press and what it has not written yet
                # arrival_on_green and platoon_ratio use them to store what they have not written yet, and yellow_red its unfinished cycles
                if self.incremental_run and aggregation['name'] in ['timeline', 'split_failures', 'coordination_agg', 'phase_wait', *OWN_CARRY_AGGREGATIONS]:
                    params['incremental_run'] = True #lets the aggregation know to save unmatched events for next run
                    # split_failures uses its own unmatched file (sf_unmatched), others use the main unmatched file
                    if aggregation['name'] == 'split_failures':
                        unmatched_available = self.sf_unmatched_found
                    elif aggregation['name'] in OWN_CARRY_AGGREGATIONS:
                        # These ran without the main unmatched file before, so must not assume it was given
                        unmatched_available = self.unmatched_found and 'df_or_path' in (self.unmatched_event_settings or {})
                    else:
                        unmatched_available = self.unmatched_found

                    if unmatched_available:
                        v_print(f"Incremental run using previous events for {aggregation['name']}", self.verbose, 2)
                        params['unmatched'] = True #lets the aggregation know to use the unmatched events from previous run
                        # split_failures, coordination_agg, phase_wait and the ones with their own rows use their own logic
                        if aggregation['name'] not in ['split_failures', 'coordination_agg', 'phase_wait', *OWN_CARRY_AGGREGATIONS]:
                            params['from_table'] = 'raw_data_all'
                    else:
                        v_print(f"First Run For {aggregation['name']}", self.verbose, 2)
                        params['unmatched'] = False

                # A bin of split_failures waits for the run that reaches its end
                if aggregation['name'] == 'split_failures':
                    params['carry_bins'] = True

                # Without a main unmatched file nothing can wait for a later run
                if aggregation['name'] in OWN_CARRY_AGGREGATIONS:
                    params['carry_bins'] = 'df_or_path' in (self.unmatched_event_settings or {})

                # has_data keeps its rows in the main unmatched file, and a run with no such file writes all it has
                if aggregation['name'] == 'has_data':
                    params['carry_bins'] = 'df_or_path' in (self.unmatched_event_settings or {})
                    params['unmatched'] = params['carry_bins'] and self.unmatched_found

                if aggregation['name'] == 'ped':
                    params['ped_carried'] = self._ped_carried(params)
                    ped_params = params

                # Add known_detectors_found flag for actuations aggregation
                if aggregation['name'] == 'actuations':
                    params['known_detectors_found'] = self.known_detector_days is not None and self._table_exists('unmatched_previous')
                
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
                    ).fetchall()[0][0]
                    
                    if has_data_exists:
                        v_print("Marking timeline events with missing has_data as invalid", self.verbose, 2)
                        self._invalidate_timeline_data_gaps()

                    if self.incremental_run:
                        self._save_last_phase_calls()

                    # timeline.sql invalidates completed intervals that overlap a controller clock update (181)
                    # window of 5 seconds either side. An unmatched event starting before a window ends will
                    # overlap it once matched, so mark it invalid now and the next incremental run inherits
                    # that through unmatched_previous.IsValid
                    self.conn.execute(f"""
                        UPDATE unmatched_events u
                        SET IsValid = FALSE
                        WHERE u.IsValid = TRUE
                          AND u.EventId NOT {STATE_EVENT_IDS} -- state markers, not interval starts
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
                        self.conn.execute(f"""
                            UPDATE unmatched_events u
                            SET IsValid = FALSE
                            WHERE u.IsValid = TRUE
                              AND u.EventId NOT {STATE_EVENT_IDS}
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

        # After the loop, because timeline creates unmatched_events wherever it falls in the list
        if 'df_or_path' in (self.unmatched_event_settings or {}) and not self.to_sql and any(agg['name'] == 'arrival_on_green' for agg in self.aggregations):
            self._save_arrival_carry()
        if 'df_or_path' in (self.unmatched_event_settings or {}) and not self.to_sql and any(agg['name'] == 'yellow_red' for agg in self.aggregations):
            self._save_red_cycles_carry('yellow_red')
        if 'df_or_path' in (self.unmatched_event_settings or {}) and not self.to_sql and any(agg['name'] == 'arrival_pattern' for agg in self.aggregations):
            self._save_red_cycles_carry('arrival_pattern')
        if 'df_or_path' in (self.unmatched_event_settings or {}) and not self.to_sql and any(agg['name'] == 'approach_delay' for agg in self.aggregations):
            self._save_approach_delay_carry()
        if 'df_or_path' in (self.unmatched_event_settings or {}) and not self.to_sql and any(agg['name'] == 'left_turn_gaps' for agg in self.aggregations):
            self._save_left_turn_gaps_carry()
        if self.incremental_run and not self.to_sql and ped_params is not None:
            self._save_last_ped_presses()
            self._save_ped_carry(ped_params)
        if self.remove_incomplete and 'df_or_path' in (self.unmatched_event_settings or {}) and not self.to_sql \
                and any(agg['name'] in OWN_CARRY_AGGREGATIONS for agg in self.aggregations):
            self._save_complete_bins()
        if self.incremental_run and not self.to_sql:
            self._save_last_events()
        if self._table_exists('has_data_state'):
            self._add_to_unmatched_events("SELECT * FROM has_data_state")
            self.conn.execute("DROP TABLE has_data_state")
        if self.known_detector_days is not None and not self.to_sql:
            self._save_known_detectors()

        # Print out runtimes
        v_print(f"\n\nTotal aggregation runtime: {sum(self.runtimes.values()):.2f} seconds.", self.verbose)
        v_print("\nIndividual Query Runtimes:", self.verbose)
        for name, runtime in self.runtimes.items():
            v_print(f"{name}: {runtime:.2f} seconds", self.verbose)

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

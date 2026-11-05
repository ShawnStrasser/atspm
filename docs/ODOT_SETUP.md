# ODOT ATSPM Configuration (Planned February 2026)

This guide describes the planned ODOT production setup for the ATSPM Aggregation package, effective February 2026. Compared to previous configurations, this setup will have a new schema for `timeline` and adds the `ped_delay`, `coordination_agg` and `phase_wait` tables, and omits timeline events to reduce data size. There is also now a `controller_type` parameter which ODOT will set to "maxtime".

## Overview

- **New tables added:**
  - `ped_delay`: Pedestrian delay metrics
  - `coordination_agg`: Aggregated coordination metrics
  - `phase_wait`: Phase wait time analysis
- **Timeline event filtering:**
  - The following timeline event types are removed: PhaseHold (41, 42), PhaseOmit (46, 47), and PedOmit (48, 49). These events take up significant space in the timeline output and have not been found useful for ODOT operations or reporting.

## How to Filter Timeline Events

Before passing raw data to the processor, filter out the excluded event types:

```python
# Remove phase-related timeline events that are not useful for ODOT
EXCLUDED_EVENT_IDS = [41, 42, 46, 47, 48, 49]
raw_data_filtered = deviceEventsData[~deviceEventsData['EventId'].isin(EXCLUDED_EVENT_IDS)]
```

## ODOT Aggregations Configuration

```python
# Set up all parameters
metricslist = ["actuations", "arrival_on_green", "coordination", "split_failures", "splits", "terminations", "yellow_red", "timeline", "ped_delay", "phase_wait", "coordination_agg"]
params = {
    # Global Settings
    'raw_data': raw_data_filtered,  # dataframe or file path to csv/parquet/json
    'detector_config': configDataDf,
    'bin_size': bin_size,
    'remove_incomplete': True,
    'controller_type': 'maxtime',
    'unmatched_event_settings': { 
        'df_or_path': unmatchedEventsData,
        'split_fail_df_or_path': splitfail_UnmatchedEventsData,
        'max_days_old': 14},
    'verbose': 1,
    # Performance Measure Settings
    'aggregations': [
        {'name': 'has_data', 'params': {}}, # remove_incomplete drops bins it scores under 0.5
        {'name': 'actuations', 'params': {
            'fill_in_missing': True,
            'known_detectors_max_days_old': 2
        }},
        {'name': 'arrival_on_green', 'params': {'latency_offset_seconds': 0}},  # DistanceFromStopBar, MPH and LatencyCorrection from detector_config if it has them, see the README
        {'name': 'coordination', 'params': {}},
        {'name': 'split_failures', 'params': {'red_time': 5, 'red_occupancy_threshold': 0.80, 'green_occupancy_threshold': 0.70, 'by_approach': True, 'by_cycle': False}},
        {'name': 'splits', 'params': {}},
        {'name': 'terminations', 'params': {}},
        {'name': 'yellow_red', 'params': {'latency_offset_seconds': 1.5, 'min_red_offset': -8}},
        {'name': 'timeline', 'params': {'min_duration': 0.2, 'cushion_time': 60}},
        {'name': 'ped_delay', 'params': {}},
        {'name': 'phase_wait', 'params': {
            'preempt_recovery_seconds': 120,
            'assumed_cycle_length': 150,
            'skip_multiplier': 1.5,
            'tsp_skip_multiplier': 2.0
        }},
        {'name': 'coordination_agg', 'params': {}},
    ]
}

from atspm import SignalDataProcessor

# Initialize processor with all parameters
processor = SignalDataProcessor(**params)
processor.load()
processor.aggregate()

# And then query the tables as usual
```

## Output Tables

The following tables will be produced (see the [README Output Schemas section](../README.md#output-schemas) for detailed column definitions):

| Table              | Description                        |
|--------------------|------------------------------------|
| has_data           | Data completeness by time bin      |
| actuations         | Detector actuations by bin         |
| arrival_on_green   | Arrival on green metrics           |
| coordination       | MAXTIME coordination data          |
| ped                | Pedestrian counts and volumes      |
| split_failures     | Split failure detection            |
| splits             | MAXTIME split timing data          |
| terminations       | Phase termination reasons          |
| yellow_red         | Yellow and red time metrics        |
| timeline           | Timeline events for visualization  |
| ped_delay          | Pedestrian delay metrics           |
| phase_wait         | Phase wait time analysis           |
| coordination_agg   | Coordination metrics (aggregated)  |

## Detector Health

`detector_health` needs `pip install atspm[detector-health]`. It is not in the list above; to run it in the same processor, add it to `aggregations`:

```python
{'name': 'detector_health', 'params': {
    'detectors': detectors_data,   # atspm-detector predict() answers; leave out to use detector_config
    'state': health_state,         # the last run's health_state; '' on the first run
    'history': health_rows_folder, # the folder of saved health_rows
    'horizon_days': 3,
    'workers': 4,                  # the midnight run: about 280 s and 6.5 GB in all; with 1, 530 to 670 s and 2.9 GB
}},
```

Every run writes `detector_health` for its own 15-minute bin. The first run past midnight also writes what the whole-day check finds in the previous day, with past `TimeStamp`s. Nothing needs scheduling: midnight is read from the event stamps. First runs write no rows until the first whole-day check. Its checks read none of the events filtered out above. See the [README](../README.md#detector-health).

`detector_health` has the columns `TimeStamp`, `DeviceId`, `DetectorType`, `Detector` (BIGINT), `Label`, `Score`. After each run, from `processor.conn`, in this order:

1. Save `detector_health`. De-duplicate it on `TimeStamp`, `DeviceId`, `DetectorType`, `Detector` and `Label`, as a retried run writes the same rows.
2. Append `health_rows` as one new parquet file in `health_rows_folder`. It is only appended and read through the path, never loaded back as a table.
3. Save `health_state` last. It is loaded back every run, as a DataFrame or a parquet or CSV path. With plain `pd.read_csv`, use `dtype={'DeviceId': str}` for ids of digits.

If a run fails, retry it from the last saved state. A run the state lists whose rows are missing from the folder raises an error. Cut events on the quarter hour by stamp, or expect a larger state: 299 MB of CSV statewide, against 16 MB.

## Key Configuration Notes

1. **Controller Type**: Set to `'maxtime'` for ODOT's traffic controller systems
2. **Bin Size**: 15-minute aggregation intervals
3. **Verbose Level**: Set to `1` for standard logging output
4. **Unmatched Events**: Configured with a 14-day lookback window for handling unmatched detector events
5. **Timeline Filtering**: Events 41, 42, 46, 47, 48, 49 are excluded before processing to reduce data volume
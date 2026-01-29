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
        {'name': 'has_data', 'params': {'no_data_min': 5, 'min_data_points': 3}}, # remove bins with less than 10 rows every 3 minutes
        {'name': 'actuations', 'params': {
            'fill_in_missing': True,
            'known_detectors_df_or_path': known_detectors_data,
            'known_detectors_max_days_old': 2
        }},
        {'name': 'arrival_on_green', 'params': {'latency_offset_seconds': 0}},
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
            'skip_multiplier': 1.5
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
| communications     | Communication event summary        |
| coordination       | MAXTIME coordination data          |
| ped                | Pedestrian actuation summary       |
| split_failures     | Split failure detection            |
| splits             | MAXTIME split timing data          |
| terminations       | Phase termination reasons          |
| yellow_red         | Yellow and red time metrics        |
| timeline           | Timeline events for visualization  |
| ped_delay          | Pedestrian delay metrics           |
| phase_wait         | Phase wait time analysis           |
| coordination_agg   | Coordination metrics (aggregated)  |

## Key Configuration Notes

1. **Controller Type**: Set to `'maxtime'` for ODOT's traffic controller systems
2. **Bin Size**: 15-minute aggregation intervals
3. **Verbose Level**: Set to `1` for standard logging output
4. **Unmatched Events**: Configured with a 14-day lookback window for handling unmatched detector events
5. **Timeline Filtering**: Events 41, 42, 46, 47, 48, 49 are excluded before processing to reduce data volume
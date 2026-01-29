
# ODOT ATSPM Configuration (Planned February 2026)

This guide describes the planned ODOT production setup for the ATSPM Aggregation package, effective February 2026. Compared to previous configurations, this setup will have a new schema for `timeline` and adds the `ped_delay`, `coordination_agg` and `phase_wait` tables, and omits timeline events to reduce data size. There is also now a `controller_type` parameter which ODOT will set to "maxtime".

## Overview

- **New tables added:**
  - `ped_delay`: 
  - `coordination_agg`: Aggregated coordination metrics
  - `phase_wait`: Phase wait time analysis
- **Timeline event filtering:**
  - The following timeline event types are removed: PhaseHold (41, 42), PhaseOmit (46, 47), and PedOmit (48, 49). These events take up significant space in the timeline output and have not been found useful for ODOT operations or reporting.

## How to Filter Timeline Events

Before passing raw data to the processor, filter out the excluded event types:

```python
# Remove phase-related timeline events that are not useful for ODOT
EXCLUDED_EVENT_IDS = [41, 42, 46, 47, 48, 49]
raw_data_filtered = raw_data[~raw_data['EventId'].isin(EXCLUDED_EVENT_IDS)]
```

## ODOT Aggregations Configuration

```python
aggregations = [
    {'name': 'has_data', 'params': {'no_data_min': 5, 'min_data_points': 3}},
    {'name': 'actuations', 'params': {
        'fill_in_missing': True,
        'known_detectors_df_or_path': known_detectors_data,
        'known_detectors_max_days_old': 2
    }},
    {'name': 'arrival_on_green', 'params': {'latency_offset_seconds': 0}},
    {'name': 'coordination', 'params': {}},
    {'name': 'split_failures', 'params': {
        'red_time': 5,
        'red_occupancy_threshold': 0.80,
        'green_occupancy_threshold': 0.70,
        'by_approach': True,
        'by_cycle': False
    }},
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
```

## Processor Usage

```python
from atspm import SignalDataProcessor

processor = SignalDataProcessor(
    raw_data=raw_data_filtered,
    detector_config=detector_configs,
    bin_size=15,
    remove_incomplete=True,
    controller_type='maxtime',
    aggregations=aggregations
)
processor.run()
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
| timeline           | Timeline events for visualization  |
| ped_delay          | Pedestrian delay metrics           |
| phase_wait         | Phase wait time analysis           |
| coordination_agg   | Coordination metrics (aggregated)  |

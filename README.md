# ATSPM Aggregation

<!-- Package Info -->
[![PyPI](https://img.shields.io/pypi/v/atspm)](https://pypi.org/project/atspm/)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/atspm)](https://pypi.org/project/atspm/)
[![PyPI - Downloads](https://img.shields.io/pypi/dm/atspm)](https://pypi.org/project/atspm/)

<!-- Repository Info -->
[![GitHub License](https://img.shields.io/github/license/ShawnStrasser/atspm)](https://github.com/ShawnStrasser/atspm/blob/main/LICENSE)
[![GitHub issues](https://img.shields.io/github/issues/ShawnStrasser/atspm)](https://github.com/ShawnStrasser/atspm/issues)
[![GitHub stars](https://img.shields.io/github/stars/ShawnStrasser/atspm)](https://github.com/ShawnStrasser/atspm/stargazers)

<!-- Status -->
[![Unit Tests](https://github.com/ShawnStrasser/atspm/actions/workflows/pr-tests.yml/badge.svg)](https://github.com/ShawnStrasser/atspm/actions/workflows/pr-tests.yml)
[![codecov](https://codecov.io/gh/ShawnStrasser/atspm/badge.svg)](https://codecov.io/gh/ShawnStrasser/atspm)

`atspm` is a cutting-edge, lightweight Python package that transforms raw traffic signal controller event logs into aggregate performance measures and troubleshooting data which help transportation agencies continuously monitor and optimize signal timing performance, detect issues, and take proactive actions - all in real-time. `atspm` may be used by itself, embedded inside an ATMS application, or installed on an edge device.

## What Makes ATSPM Different?
Traditional traffic signal optimization tools like Synchro rely on periodic manual data collection and simulation models. In contrast, `atspm` offers:

*   **Real-Time Data:** Uses data directly collected from signal controllers at intersections.
*   **Continuous Monitoring:** Allows agencies to generate performance data for any time range, diagnosing problems before they escalate.
*   **Proactive Management:** Enables agencies to solve issues before they lead to major traffic disruptions, rather than relying on infrequent manual studies or citizen complaints.
*   **Cost Efficiency:** With over 330,000 traffic signals in the US, continuous monitoring reduces the need for costly manual interventions (typically $4,500 per intersection every 3-5 years).

The Python `atspm` project is inspired by UDOT ATSPM, which is a full-stack application for intersection-level visualization. This package focuses on **aggregation and analytics**, enabling a system-wide monitoring approach. Both projects are complementary and can be deployed together.

This project focuses only on transforming event logs into performance measures and troubleshooting data; it does not include data visualization. Feel free to submit feature requests or bug reports or to reach out with questions or comments. Contributions are welcome! 

## Table of Contents

- [What Makes ATSPM Different?](#what-makes-atspm-different)
- [Features](#features)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Usage Example](#usage-example)
- [Performance Measures](#performance-measures)
- [Release Notes](CHANGELOG.md)
- [Future Plans](#future-plans)
- [Contributing](#contributing)
- [License](#license)

## Features

- Transforms event logs into aggregate performance measures and troubleshooting metrics
- Supports incremental processing for real-time data (ie. every 15 minutes)
- Runs locally using the powerful [DuckDB](https://duckdb.org/) analytical SQL engine.
- Output to user-defined folder structure and file format (csv/parquet/json), or query DuckDB tables directly
- Deployed in production by Oregon DOT since July 2024

## Installation

```bash
pip install atspm
```
Or pinned to a specific version:
```bash
pip install atspm==2.x.x 
```
`atspm` works on Python 3.10-3.12 and is tested on Ubuntu, Windows, and MacOS.

## Quick Start

The best place to start is with these self-contained example uses in Colab!<br>
[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/drive/14SPXPjpwbBEPpjKBN5s4LoqtHWSllvip?usp=sharing)

## Usage Example

The first step in running the tool is to define the parameters that will dictate how the data is processed. The parameters include global settings for input data, output formats, and options to select specific performance measures.

### Exhaustive Parameter List

```python
from atspm import SignalDataProcessor, sample_data

params = {
    # --- Global Settings ---
    'raw_data': sample_data.data,           # Path (CSV/Parquet/JSON) or Pandas DataFrame
    'detector_config': sample_data.config,  # Path (CSV/Parquet/JSON) or Pandas DataFrame
    'bin_size': 15,                         # Aggregation interval in minutes
    'output_dir': 'test_folder',            # Directory to save results
    'output_format': 'csv',                 # 'csv', 'parquet', or 'json'
    'output_file_prefix': 'run1_',          # Optional prefix for output files
    'output_to_separate_folders': True,     # Save each measure in its own subfolder
    'remove_incomplete': True,              # Remove bins with insufficient data (requires 'has_data' agg)
    'verbose': 1,                           # 0: Errors only, 1: Performance, 2: Debug
    'to_sql': False,                        # If True, returns SQL strings instead of executing

    # --- Incremental Processing Settings ---
    'unmatched_event_settings': {
        'df_or_path': 'unmatched.csv',           # Track unmatched timeline events
        'split_fail_df_or_path': 'sf_unmatched.csv', # Track unmatched split failures
        'max_days_old': 7                        # Max age for tracking unmatched events
    },

    # --- Performance Measures (Aggregations) ---
    'aggregations': [
        {
            'name': 'has_data', 
            'params': {
                'no_data_min': 5,           # Min minutes of data required per bin
                'min_data_points': 3        # Min events required per sub-bin
            }
        },
        {
            'name': 'actuations', 
            'params': {
                'fill_in_missing': True,    # Zero-fill missing detector intervals
                'known_detectors_df_or_path': 'known_detectors.csv', # For zero-filling
                'known_detectors_max_days_old': 2
            }
        },
        {
            'name': 'arrival_on_green', 
            'params': {
                'latency_offset_seconds': 0 # Adjust for detector-to-controller latency
            }
        },
        {
            'name': 'split_failures', 
            'params': {
                'red_time': 5,              # Min red time to consider a split failure
                'red_occupancy_threshold': 0.80,
                'green_occupancy_threshold': 0.80,
                'by_approach': True         # Aggregate by approach instead of detector
            }
        },
        {
            'name': 'yellow_red', 
            'params': {
                'latency_offset_seconds': 0
            }
        },
        {
            'name': 'timeline', 
            'params': {
                'maxtime': True,            # Include MAXTIME-specific events
                'min_duration': 1,          # Filter out events shorter than n seconds
                'cushion_time': 1           # Padding for instant events (seconds)
            }
        },
        {
            'name': 'full_ped', 
            'params': {
                'seconds_between_actuations': 15, # Min time between unique peds
                'return_volumes': True            # Estimate pedestrian volumes
            }
        },
        {
            'name': 'phase_wait',
            'params': {
                'preempt_recovery_seconds': 120, # Time after preempt ends to exclude
                'assumed_cycle_length': 140,     # Fallback cycle length (Free mode)
                'skip_multiplier': 1.5           # Threshold for skipped phases
            }
        },
        {'name': 'ped_delay', 'params': {}},
        {'name': 'terminations', 'params': {}},
        {'name': 'splits', 'params': {}},
        {'name': 'coordination', 'params': {}}, # MAXTIME-specific
        {'name': 'coordination_agg', 'params': {}} # General coordination state (Pattern, Cycle, etc.)
    ]
}

# Running the Processor
# Using 'with' ensures the DuckDB connection is closed automatically
with SignalDataProcessor(**params) as processor:
    processor.load()      # Load raw data into DuckDB
    processor.aggregate() # Run performance measures
    processor.save()      # Save to output_dir

# Alternatively, use the .run() method to perform all steps at once
processor = SignalDataProcessor(**params)
processor.run()
```

### Retrieving Results as a DataFrame
You can query the internal DuckDB database directly. Note that the connection must be open to query data:
```python
processor = SignalDataProcessor(**params)
processor.load()
results = processor.conn.query("SELECT * FROM actuations ORDER BY TimeStamp").df()
print(results.head())
processor.conn.close() # Manually close if not using 'with'
```

### Visualization Options
The data produced by `atspm` can be visualized using Power BI, Plotly, or other platforms. For example, see the [Oregon DOT ATSPM Dashboard](https://app.powerbigov.us/view?r=eyJrIjoiNzhmNTUzNDItMzkzNi00YzZhLTkyYWQtYzM1OGExMDk3Zjk1IiwidCI6IjI4YjBkMDEzLTQ2YmMtNGE2NC04ZDg2LTFjOGEzMWNmNTkwZCJ9).

> **Note:** Use [Parquet](https://parquet.apache.org/) format in production for significantly better performance and smaller file sizes.

## Performance Measures

`atspm` produces two types of outputs:

- Binned aggregate tables, where each row represents a `bin_size`-minute interval
- A non-binned `timeline` table with start/end times for key events

### Binned aggregate measures (per `bin_size` interval)

All of the following tables include a `TimeStamp` column aligned to the start of each aggregation bin (for example, 15 minutes):

- **Has Data** (`has_data`): Marks intervals where each controller produced sufficient data (proxy for controller online/communications health). Also used to filter incomplete periods for other measures.
- **Actuations** (`actuations`): Detector actuations per detector and interval (with optional zero-filling of missing intervals).
- **Arrival on Green** (`arrival_on_green`): Percentage of detector actuations that occur during green by phase.
- **Yellow and Red Actuations** (`yellow_red`): Distribution of detector actuations relative to the start of red, including red offset and signal state.
- **Split Failures** (`split_failures`): Green and red occupancies by phase (and optionally detector/approach) and a count of cycles that meet split-failure thresholds; can be returned either per cycle or aggregated into time bins.
- **Terminations** (`terminations`): Counts of GapOut, MaxOut, and ForceOff terminations by phase.
- **Splits** (`splits`): MAXTIME-specific split events (cycle length/split services) aggregated by interval.
- **Coordination** (`coordination`): Raw events for Pattern, Cycle Length, Actual Cycle Length (MAXTIME), and Actual Offset (MAXTIME). Includes both `Raw_TimeStamp` and binned `TimeStamp`.
- **Coordination Aggregate** (`coordination_agg`): Binned coordination state per interval. Provides the active Pattern, Cycle Length, Actual Cycle Length (MAXTIME), and Actual Offset (MAXTIME) for every bin using fill-forward logic.
- **Pedestrian Services** (`full_ped`): Pedestrian services, actuations, and (optionally) estimated pedestrian volumes derived from push-button actuations.
- **Ped Delay** (`ped_delay`): Average pedestrian delay and sample counts per phase and interval, derived from `timeline`.
- **Phase Wait** (`phase_wait`): Average wait time for a phase to turn green after a call, with filtering for preempts and skipped phases (wait > 1.5x cycle length).
- **Detector Health** (`detector_health`): Time-series anomaly scores for detector actuations (using the `traffic-anomaly` package), typically run on binned `actuations` data.

### Timeline events (non-binned)

The **`timeline`** table is an event-level dimension for troubleshooting and visualization and is *not* binned into `bin_size` intervals. Each row includes:

- `DeviceId`
- `StartTime` / `EndTime`
- `Duration` (seconds between `StartTime` and `EndTime`)
- `EventClass` (for example, Green, Yellow, Ped Service, Split, Preempt)
- `EventValue` (phase/overlap or a coded value, depending on `EventClass`)
- `IsValid` (whether the start/end pair is complete)

Passing `maxtime=True` to the `timeline` aggregation adds MAXTIME-only events such as splits and alarm group events (Event 175).

The table below lists all `EventClass` values and their associated `EventValue` ranges produced by the `timeline` aggregation.

<details>
<summary>Timeline EventClass and EventValue reference</summary>

| EventClass | EventValue |
| --- | --- |
| Green | 1-16 |
| Yellow | 1-16 |
| Red | 1-16 |
| Ped Service | 1-16 |
| Ped Delay | 1-16 |
| Ped Omit | 1-16 |
| Phase Call | 1-16 |
| Phase Hold | 1-16 |
| Phase Omit | 1-16 |
| FYA | 1-16 |
| Advance Warning Phase | 1-16 |
| Overlap Green | 1-16 |
| Overlap Trail Green | 1-16 |
| Overlap Yellow | 1-16 |
| Overlap Red | 1-16 |
| Overlap Ped | 1-16 |
| Advance Warning Overlap | 1-16 |
| Split | 1-16 |
| Pattern Change | 0-255 |
| Cycle Length Change | 0-255 |
| Coord | 0-255 |
| Preempt | 1-16 |
| TSP Call | 1-16 |
| TSP Adjustment | 1-16 |
| TSP Checkin | 1-16 |
| TSP Service | 1-16 |
| TSP Detector | 1-16 |
| Watchdog | NULL |
| Stuck Off | 1-128 |
| Stuck On | 1-128 |
| Erratic | 1-128 |
| Transition | NULL |
| Transition Shortway | NULL |
| Transition Longway | NULL |
| Transition Dwell | NULL |
| Cycle Fault | NULL |
| Coord Fault | NULL |
| Coord Fail | NULL |
| Cycle Fail | NULL |
| MMU Flash | NULL |
| Local Flash | NULL |
| Flash - Other | NULL |
| Flash - Not Flash | NULL |
| Flash - Automatic | NULL |
| Flash - Local Manual | NULL |
| Flash - Fault Monitor | NULL |
| Flash - MMU | NULL |
| Flash - Startup | NULL |
| Flash - Preempt | NULL |
| Alarm Group State | NULL |
| Power Failure | NULL |
| Power Restored | NULL |
| Stop Time Input | NULL |
| Manual Control | NULL |
| Aux Switch | 1-64 |
| Interval Advance | NULL |
| Special Function | 1-64 |

</details>

Detailed documentation for each measure is coming soon.

## Output Schemas

The following tables are produced by the aggregation process, depending on the configured list of aggregations.

### Primary Performance Measures

#### actuations
*   `TimeStamp` (DATETIME): Bin start time.
*   `DeviceId` (INTEGER or TEXT): Unique identifier for the controller (type depends on input).
*   `Detector` (INT16): Detector number.
*   `Total` (INT16): Total count of actuations.

#### arrival_on_green
*   `TimeStamp` (DATETIME): Bin start time.
*   `DeviceId` (INTEGER or TEXT): Unique identifier for the controller (type depends on input).
*   `Phase` (INT16): Phase number.
*   `Total_Actuations` (BIGINT): Total actuations on the advance detector.
*   `Percent_AOG` (FLOAT): Fraction of actuations arriving on green (0.0 - 1.0).

#### communications
*   `TimeStamp` (DATETIME): Bin start time.
*   `DeviceId` (INTEGER or TEXT): Unique identifier for the controller (type depends on input).
*   `EventId` (INT16): Event ID being tracked (e.g., 400).
*   `Average` (FLOAT): Average value of the parameter.

#### coordination
*   `TimeStamp` (DATETIME): Bin start time.
*   `Raw_TimeStamp` (DATETIME): Exact timestamp of the event.
*   `DeviceId` (INTEGER or TEXT): Unique identifier for the controller (type depends on input).
*   `EventId` (INT16): Event ID (131, 132, 316, 318).
*   `Parameter` (INT16): Value associated with the event (Pattern, Cycle Length, etc.).

#### coordination_agg
*   `TimeStamp` (DATETIME): Bin start time.
*   `DeviceId` (INTEGER or TEXT): Unique identifier for the controller (type depends on input).
*   `Pattern` (INT16): Pattern number in effect.
*   `CycleLength` (INT16): Cycle length in effect.
*   `ActualCycleLength` (INT16): Measured cycle length.
*   `ActualOffset` (INT16): Measured offset.

#### full_ped
*   `TimeStamp` (DATETIME): Bin start time.
*   `DeviceId` (INTEGER or TEXT): Unique identifier for the controller (type depends on input).
*   `Phase` (INT16): Phase number.
*   `PedServices` (INT16): Count of ped services.
*   `PedActuation` (INT16): Count of ped actuations.
*   `Unique_Actuations` (INT16): Count of unique ped actuations (filtered for duplicates).
*   `Estimated_Volumes` (FLOAT): Estimated pedestrian volume (only present if `return_volumes=True`).

#### has_data
*   `TimeStamp` (DATETIME): Bin start time where data exists.
*   `DeviceId` (INTEGER or TEXT): Unique identifier for the controller (type depends on input).

#### ped
*   `TimeStamp` (DATETIME): Bin start time.
*   `DeviceId` (INTEGER or TEXT): Unique identifier for the controller (type depends on input).
*   `Phase` (INT16): Phase number.
*   `PedServices` (INT16): Count of ped services.
*   `PedActuation` (INT16): Count of ped actuations.

#### ped_delay
*   `TimeStamp` (DATETIME): Bin start time (rounded from EndTime of walk).
*   `DeviceId` (INTEGER or TEXT): Unique identifier for the controller (type depends on input).
*   `Phase` (INT16): Phase number.
*   `AvgPedDelay` (FLOAT): Average delay in seconds.
*   `Samples` (BIGINT): Number of ped delay events.

#### phase_wait
*   `TimeStamp` (DATETIME): Bin start time.
*   `DeviceId` (INTEGER or TEXT): Unique identifier for the controller (type depends on input).
*   `Phase` (INT16): Phase number.
*   `AvgPhaseWait` (FLOAT): Average wait time in seconds.
*   `TotalSkips` (BIGINT): Count of skipped phases.

#### splits
*   `TimeStamp` (DATETIME): Bin start time.
*   `DeviceId` (INTEGER or TEXT): Unique identifier for the controller (type depends on input).
*   `EventId` (INT16): Split event ID (300-317).
*   `Services` (INT16): Number of times the split occurred.
*   `Average_Split` (FLOAT): Average split duration.

#### split_failures
*   `TimeStamp` (DATETIME): Bin start time.
*   `DeviceId` (INTEGER or TEXT): Unique identifier for the controller (type depends on input).
*   `Detector` (INT16): Detector number (if `by_approach=False`).
*   `Phase` (INT16): Phase number.
*   `Green_Time` (FLOAT): Average Green time.
*   `Green_Occupancy` (FLOAT): Average Green occupancy (0.0 - 1.0).
*   `Red_Occupancy` (FLOAT): Average Red occupancy (0.0 - 1.0).
*   `Split_Failure` (INT16): Count of split failures.

#### terminations
*   `TimeStamp` (DATETIME): Bin start time.
*   `DeviceId` (INTEGER or TEXT): Unique identifier for the controller (type depends on input).
*   `Phase` (INT16): Phase number.
*   `PerformanceMeasure` (TEXT): 'GapOut', 'MaxOut', or 'ForceOff'.
*   `Total` (INT16): Count of terminations.

#### timeline
*   `StartTime` (DATETIME): Event start time.
*   `EndTime` (DATETIME): Event end time (or next event time).
*   `Duration` (FLOAT): Duration in seconds.
*   `DeviceId` (INTEGER or TEXT): Unique identifier for the controller (type depends on input).
*   `IsValid` (BOOLEAN): Validity flag for the interval.
*   `EventClass` (TEXT): Description of the event class.
*   `EventValue` (INTEGER): Parameter value (Phase, Detector, etc.).

#### unique_ped
*   `TimeStamp` (DATETIME): Bin start time.
*   `DeviceId` (INTEGER or TEXT): Unique identifier for the controller (type depends on input).
*   `Phase` (INT16): Phase number.
*   `Unique_Actuations` (INT16): Count of unique actuations.

#### yellow_red
*   `TimeStamp` (DATETIME): Bin start time.
*   `DeviceId` (INTEGER or TEXT): Unique identifier for the controller (type depends on input).
*   `Phase` (INT16): Phase number.
*   `Signal_State` (INTEGER): Signal state during actuation.
*   `Red_Offset` (FLOAT): Time into red (seconds).
*   `Count` (FLOAT): Number of actuations.

### Supporting State Tables
(Used primarily for incremental processing)

#### known_detectors
*   `DeviceId` (INTEGER or TEXT): Unique identifier for the controller (type depends on input).
*   `Detector` (INTEGER): Detector number.
*   `LastSeen` (DATETIME): Timestamp when detector was last seen.

#### sf_unmatched
(Split Failures Unmatched)
*   `TimeStamp` (DATETIME): Timestamp of the event.
*   `DeviceId` (INTEGER or TEXT): Unique identifier for the controller (type depends on input).
*   `EventId` (INT16): Event ID.
*   `Detector` (INT16): Detector number.
*   `Phase` (INT16): Phase number.

#### unmatched_events
*   `TimeStamp` (DATETIME): Timestamp of the event.
*   `DeviceId` (INTEGER or TEXT): Unique identifier for the controller (type depends on input).
*   `EventId` (INT16): Event ID.
*   `Parameter` (INT16): Parameter value.
## Release Notes

See [CHANGELOG.md](CHANGELOG.md) for a full history of changes.

## Future Plans

- Integration with [Ibis](https://ibis-project.org/) for compatibility with any SQL backend.
- Implement use of detector distance to stopbar for Arrival on Green calculations.
- Develop comprehensive documentation for each performance measure.

## Contributing

Ideas and contributions are welcome! Please feel free to submit a Pull Request. Note that GitHub Actions will automatically run unit tests on your code.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

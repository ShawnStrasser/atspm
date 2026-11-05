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
pip install atspm==3.x.x
```
`atspm` works on Python 3.11-3.15 with DuckDB 2 and is tested on Ubuntu, Windows, and MacOS.

The `detector_health` aggregation needs one more package, on Python 3.11-3.14:
```bash
pip install atspm[detector-health]
```

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
    'detector_config': sample_data.config,  # Path (CSV/Parquet/JSON) or Pandas DataFrame. Columns: DeviceId, Phase, Parameter,
                                            # Function, and optionally DistanceFromStopBar, MPH and LatencyCorrection (see Arrival on Green)
    'bin_size': 15,                         # Aggregation interval in minutes
    'output_dir': 'test_folder',            # Directory to save results
    'output_format': 'csv',                 # 'csv', 'parquet', or 'json'
    'output_file_prefix': 'run1_',          # Optional prefix for output files
    'output_to_separate_folders': True,     # Save each measure in its own subfolder
    'remove_incomplete': True,              # Remove bins with insufficient data (requires 'has_data' agg)
    'drop_fall_back_hour': True,            # Drop events in the hour US clocks repeat each November (default shown, see The fall clock change)
    'verbose': 1,                           # 0: Errors only, 1: Performance, 2: Debug
    'to_sql': False,                        # If True, returns SQL strings instead of executing
    'controller_type': 'maxtime',           # Global: '' (default), 'maxtime' or 'siemens' (case-insensitive)
                                            # When 'maxtime': phase_wait uses ActualCycleLength
                                            # When not 'maxtime': splits & coordination are skipped
                                            # When 'siemens': timeline invalidates intervals across hourly log restarts

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
                'fill_in_missing': True,    # Zero-fill silent detectors in bins where the device has data
                'known_detectors_df_or_path': 'known_detectors.csv', # For zero-filling
                'known_detectors_max_days_old': 2
            }
        },
        {
            'name': 'arrival_on_green', 
            'params': {
                'latency_offset_seconds': 0, # Taken off each arrival time for detectors with no LatencyCorrection
                'spillback_on_seconds': 6,   # A detector ON for more than this has a queue over it (default shown), None to turn this off
                'spillback_gap_seconds': 5   # and its actuations count as on red until it has been OFF for this long (default shown)
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
                'cushion_time': 1,          # Padding for instant events (seconds)
                'max_event_gap_seconds': {'05:00': 300, '06:00': 120, '21:00': 300, '23:00': 900},  # Longest device silence by time of day (default shown); a number for all day, None to disable
                'live': False               # If True, keep incomplete events as IsValid=False with common EndTime. This is for troubleshooting.
            }
        },
        {
            'name': 'ped', 
            'params': {
                'seconds_between_actuations': 15, # Min time between unique peds (default shown)
                'return_volumes': True            # Estimate pedestrian volumes
            }
        },
        {
            'name': 'phase_wait',
            'params': {
                'preempt_recovery_seconds': 120, # Time after preempt ends to exclude
                'assumed_cycle_length': 140,     # Fallback cycle length (Free mode)
                'skip_multiplier': 1.5,          # Threshold for skipped phases
                'tsp_skip_multiplier': 2.0       # Same, for waits that saw a TSP adjustment
            }
        },
        {'name': 'ped_delay', 'params': {}},
        {'name': 'platoon_ratio', 'params': {}},  # Requires arrival_on_green
        {'name': 'terminations', 'params': {}},
        {'name': 'splits', 'params': {}},        # MAXTIME-specific (skipped if controller_type != 'maxtime')
        {'name': 'coordination', 'params': {}},  # MAXTIME-specific (skipped if controller_type != 'maxtime')
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

`atspm` writes the tables named in the `aggregations` list you give it, and no others: each table below is written when listed in `aggregations`. Everything a run does comes from that list and its params, so nothing is kept between runs but the carry files and health store you choose to pass. There are two types of output:

- Binned aggregate tables, where each row represents a `bin_size`-minute interval
- A non-binned `timeline` table with start/end times for key events

### Binned aggregate measures (per `bin_size` interval)

All of the following tables include a `TimeStamp` column aligned to the start of each aggregation bin (for example, 15 minutes):

- **Has Data** (`has_data`): Marks intervals where each controller produced sufficient data (proxy for controller online/communications health). Also used to filter incomplete periods for other measures.
- **Actuations** (`actuations`): Detector actuations per detector and interval (with optional zero-filling of missing intervals).
- **Arrival on Green** (`arrival_on_green`): Percentage of vehicles reaching the stop bar during green by phase, with each advance detector actuation projected to the stop bar. See [Arrival on Green](#arrival-on-green).
- **Platoon Ratio** (`platoon_ratio`): HCM platoon ratio by phase and interval, `Rp = Percent_AOG / (g/C)`, with the green ratio estimated from phase green time within each bin and the HCM arrival type (1–6). Unlike arrival on green, it is comparable across phases with different splits. Requires `arrival_on_green`.
- **Yellow and Red Actuations** (`yellow_red`): Distribution of detector actuations relative to the start of red, including red offset and signal state. A row belongs to the `bin_size` bin of its cycle's red begin. Incremental runs give the same rows as one big run: a bin is written once the last cycle with a red in it has finished, so with runs cut on the bin each one comes out a run later, and the events it waits for are kept in `unmatched_events` (synthetic `EventId` 951 to 954).
- **Split Failures** (`split_failures`): Green and red occupancies by phase (and optionally detector/approach) and a count of cycles that meet split-failure thresholds; can be returned either per cycle or aggregated into time bins. Incremental runs give the same rows as one big run: a bin is written once, whole, by the first run that reaches the end of it, by the same rule as [Arrival on Green](#arrival-on-green), and `sf_unmatched` holds the events a later run needs. A missing detector off is filled in half way to the next on, but no earlier than the start of that on's bin, and a cycle is done at its first red barrier, so a run never needs an event it has not seen.
- **Terminations** (`terminations`): Counts of GapOut, MaxOut, and ForceOff terminations by phase.
- **Splits** (`splits`): MAXTIME-specific split events (cycle length/split services) aggregated by interval.
- **Coordination** (`coordination`): Raw events for Pattern, Cycle Length, Actual Cycle Length (MAXTIME), and Actual Offset (MAXTIME). Includes both `Raw_TimeStamp` and binned `TimeStamp`.
- **Coordination Aggregate** (`coordination_agg`): Binned coordination state per interval. Provides the active Pattern, Cycle Length, Actual Cycle Length (MAXTIME), and Actual Offset (MAXTIME) for every bin using fill-forward logic.
- **Pedestrian** (`ped`): Pedestrian services, push-button presses, unique presses and estimated pedestrian volumes, all in one row per phase and interval. `return_volumes=False` (default True) leaves out `Estimated_Volumes` and the rows of quiet bins. A press is a unique actuation if it comes more than `seconds_between_actuations` (default 15) after the previous press on that input, or if no earlier press is known. See [Pedestrian counts and volumes](#pedestrian-counts-and-volumes).
- **Ped Delay** (`ped_delay`): Average pedestrian delay and sample counts per phase and interval, derived from `timeline`.
- **Phase Wait** (`phase_wait`): Average wait time for a phase to turn green after a call, with filtering for preempts and skipped phases (wait > 1.5x cycle length). A wait is left out when the device logged no call on any phase during it or in the 15 minutes before it ended, since the call state is then unknown. For MAXTIME controllers, set `controller_type='maxtime'` globally to use the more accurate ActualCycleLength.
- **Detector Health** (`detector_health`): How much of each `bin_size` interval each vehicle detector and ped button was failing, and with what problem, from `atspm-detector`. Needs `pip install atspm[detector-health]`. See [Detector health](#detector-health).

### Arrival on Green

`arrival_on_green` and `platoon_ratio` follow the UDOT method (ATSPM Methods and Assumptions Manual 4.3, Purdue Coordination Diagram, Step 1). A detector sits upstream of the stop bar, so each detector on event (`EventId` 82) on an advance detector is moved forward to when the vehicle reaches the stop bar:

```
arrival = detector on + distance / (speed * 1.467) - latency
```

The arrival time is used for everything after that. An arrival is on green if the phase is green at that moment, and it counts in the bin it lands in, so `Total_Actuations` for a bin differs slightly from the `actuations` table, which keeps the detector's own times.

A bin with vehicles and none on green reads 0, including a bin where the phase never turned green. A bin with no vehicles has no row. `platoon_ratio` reads 0 in a bin with green and none of its vehicles on green, and has no row for a bin with no green, where the ratio has no meaning.

Distance, speed and latency come from three optional `detector_config` columns, in UDOT's names:

| Column | Meaning | If missing |
| --- | --- | --- |
| `DistanceFromStopBar` | Feet from the leading edge of the detector to the stop bar | 220 for phases 2 and 6, 120 for all others |
| `MPH` | Speed in miles per hour. UDOT uses the speed limit | 35 |
| `LatencyCorrection` | Seconds from the vehicle crossing the detector to the controller logging it | `latency_offset_seconds`, or 0 without it |

Each value is judged row by row: a row may have one and not the others, and whatever is blank, not a number, a negative distance or a speed of zero takes its default. A latency of 0 in the column is a real 0, and beats `latency_offset_seconds`. A config with only `DeviceId`, `Phase`, `Parameter` and `Function` keeps working and takes the defaults for every detector. Column names ignore case, spaces and underscores. With `to_sql=True` your own `detector_config` table needs all three columns, which may be NULL.

**Queues over the detector.** UDOT's method has no step for a queue that reaches the advance detector. Vehicles stop on it while the phase is red. When the phase turns green the queue moves, and its vehicles begin actuating the detector on green, so they read as arriving on green although they all arrived on red. So an advance detector that stays ON for more than `spillback_on_seconds` (default 6) gets a flag, and every actuation that begins while it holds the flag counts as an arrival on red, whatever the phase shows. `Total_Actuations` does not change, and `platoon_ratio` follows through `Percent_AOG`.

The flag goes only when the detector has been OFF for `spillback_gap_seconds` (default 5), measured from off to the next on, not when traffic starts moving, because the queue clears at about one vehicle every 2 seconds. Each detector has its own flag. The flag begins when the detector has been ON long enough, so the actuation that trips it keeps its own reading. A detector with no off events never gets the flag. `spillback_on_seconds=None` turns all of this off and gives UDOT's method alone. The two defaults come from three days of real events on 846 signals, with no ground truth to tune against: more than 6 seconds on is a car at walking pace or stopped, and 5 seconds of empty lane is well past the gap between vehicles in a queue that is clearing.

**Incremental runs** give the same rows as one big run wherever the runs are cut, for both measures. A bin is written once, whole, by the first run whose data reaches the end of it, and that is a run whose last event, across all signals, is within a second of the end of the bin. A run that stops short of that leaves the bin it stopped in for the next run. Vehicles that arrive after results stop, the phase events from there on, the phases still green, the detectors holding the flag and the detectors still on are kept in `unmatched_events` (synthetic `EventId` 938 to 941 and 948 to 950, and 942 with `remove_incomplete`), so `unmatched_event_settings` with `df_or_path` is all that is needed, with or without `timeline`. With runs cut on the bin and enough signals to fill the last second, every bin is written by the run that reaches it. With a few quiet signals the last event can be short of the end of the bin, and that bin is written one run later, with the same values. A run with no unmatched settings has nothing after it, so it writes all it has, up to its last event. Arrivals past that are not counted.

Two limits: a run that really stops inside the last second of a bin is taken as having reached the end of it, and a latency larger than the travel time can move an arrival back into a bin already written.

### Pedestrian counts and volumes

`Estimated_Volumes` in `ped` is the published estimate, with its rolling hour taken over a bin and the 3 bins before it:

```
hour = unique actuations in this bin and the 3 before it
Estimated_Volumes = (0.7167 * hour + 0.0599 * hour^2) * (unique actuations in this bin / hour) + 1.1063 / 4
```

Looking back is what lets a bin be final the moment it is written. It used to look at the next 3 bins, which the end of a run does not have, so the last 3 bins of every run read low and could only be fixed by changing a row already written. On 758 ODOT signals over 2 days (Oct 5 and 6 2026), against the forward-looking form on the same counts:

| | Measured |
| --- | --- |
| Peds in a day, network | Up 0.00% and 0.02%. Every pair of presses within the hour counts once either way, so only the ends of the data differ |
| Peds in a day, one input with 10 or more | 90% unchanged, 99% within 0.3%, largest 4.2% |
| Peak hour (4 bins in a row) | Same size on average, 80% within about 5%. It starts in the same bin 59% of the time and within 15 minutes 77% of the time |
| Shape of the day | The same volumes sit a few minutes later: the middle of an input's day moves by a median of 3.7 minutes, and network hours read up to 8.5% lower on the morning rise and up to 7.7% higher on the evening fall |
| Each bin | 95% of bins with peds move by 1 ped or less. The largest moves, up to 40, are on the busiest bins, where a burst reads higher at its end and lower at its start |

**Incremental runs** give the same rows as one big run wherever the runs are cut, for `ped`, with or without `timeline`, when `unmatched_event_settings` has `df_or_path`. A bin is written once, whole, by the first run that reaches the end of it, the same rule as [Arrival on Green](#arrival-on-green), so nothing is held back from a run that reaches the end of its last bin and no past row ever changes. Each input's last press (synthetic `EventId` 937), the unique actuations of the last 3 bins written and of any bin not written yet (943), each input's next bin to write (944) and last activity (945), and the presses and ped services of any bin not written yet (946, 947) are kept in `unmatched_events`. A run with no unmatched settings has nothing after it, so it writes all it has.

With volumes on, an input has a row in every bin from its first activity (a press or a ped service) on, quiet bins included, with an estimate of just the intercept. That goes on through runs where it does nothing, until `max_days_old` passes with no activity, and it starts again at its next activity. One big run has no such limit, so it also has the intercept rows of an input quiet for longer than `max_days_old`, and that is the one difference between it and incremental runs. A device that stops logging keeps its intercept rows the same way. Join `has_data` or set `remove_incomplete` to leave them out. `return_volumes=False` leaves out all rows that count nothing.

Two limits: a run that really stops inside the last second of a bin is taken as having reached the end of it, and a press in a bin already written is too late to count. Volumes assume a `bin_size` of 15. The first run from an older unmatched file has no earlier bins to read, so its first 3 bins read a little low, once.

### The fall clock change

When clocks fall back, local time runs through 1:00 to 2:00 am twice on the first Sunday of November. Events from the two passes share one set of timestamps and cannot be separated, so durations and phase sequences built on them are wrong. ODOT's staging data of Nov 1 2026 had about 100 copies of each event in that hour, and false phase conflicts followed. So both passes are thrown out.

`drop_fall_back_hour`, on by default, drops every event from 1:00:00 up to but not including 2:00:00 on that day when `raw_data` is loaded, before anything is computed. Every aggregation reads `raw_data`, so to all of them the hour has no data, the same as an outage: `has_data` has no row for it, `timeline` marks an interval across it invalid, and with `remove_incomplete` no measure has a row in it. Without `remove_incomplete` a few rows can touch the hour, as with any outage: a vehicle that crossed a detector just before 1:00 arrives just after it, and a known ped input keeps its zero rows.

It assumes US rules (since 2007, the change is at 2:00 am on the first Sunday of November) and that `TimeStamp` is local time. If your data is in UTC or another fixed offset, is from somewhere with other rules, or has already been cleaned, turn it off:

```python
SignalDataProcessor(raw_data=events, drop_fall_back_hour=False, ...)
```

The spring change skips an hour and repeats none, so nothing is needed then. `to_sql=True` loads nothing, so the setting does not apply there, and your own `raw_data` table is read as it is.

### Detector health

`detector_health` runs `atspm-detector`'s checks for vehicle detectors (stuck on, silent, counts falling, too-fast actuations and so on) and ped buttons (stuck on, machine-regular presses, a sudden stop and so on) in one pass over `raw_data`. It needs no other aggregation. Install it with `pip install atspm[detector-health]`.

```python
{'name': 'detector_health', 'params': {
    'store': 'C:/atspm/health_store.duckdb',  # None: one run over this run's raw_data, always judged
    'detectors': 'detectors.parquet',         # DataFrame or path; None: detector_config
    'horizon_days': 3,                        # how far back a verdict looks, and how much the store keeps
    'judge': 'daily',                         # 'daily' | 'hourly' | 'every_run' | False (only add to the store)
    'findings': False,                        # also write detector_health_findings
    'ped': True,                              # a store's ped setting cannot change later
    'ped_assign': False,                      # ped movement model (onnxruntime)
    'options': None, 'ped_options': None,     # atspm-detector vehicle and ped overrides
    'workers': 1,
    'clock_ahead_minutes': 60,                # see below
    'restart_old_store': True,                # see below
}}
```

- **Store**: each run adds its events to a DuckDB file holding small summary rows and whatever is still open (a detector that is ON, a green still running). The store keeps `horizon_days` and no more, a run that fails leaves it unchanged, and the answers equal one run over the same events however they were cut into runs. This state lives in the store, not in `unmatched_events`. Only one run can write to a store at a time. A store from an older `atspm-detector` that changed what the rows mean is renamed `<file>.v<N>` and a new one started, with a warning; it fills up again as data arrives. With `restart_old_store=False` that raises an error instead.
- **Verdicts**: `daily` judges at each midnight and `hourly` at each hour, on the clock, wherever runs are cut: a run whose last event is within a second of such a time has reached it, and a run that reaches several judges at each of them. `every_run` judges at the end of each run, so there the cuts matter. A verdict looks back over `horizon_days`, and writes the bins since the verdict before. A run that reaches no verdict writes no `detector_health` file. With no store the run also judges at its end. A verdict that comes later can find a problem in bins already written: those rows stay as they are, and the problem comes as new rows with their past `TimeStamp`, so read the table by `TimeStamp`, not by file. No bin, detector and label is written twice. The more often the verdicts, the more of this: `daily` is what the numbers below were measured on.
- **Stuck past the window**: a button or detector a verdict rated stuck ON keeps its bins for as long as it stays ON, however long, as the open ON stays in the store. While its signal logs nothing (a button: while its phase walks nothing) it has no rows, as nothing is known of it then, and they start again if it is still ON. A run with no store knows nothing before its `raw_data`.
- **Reading the bins**: sum `Score` per detector over the period and divide by 4 for 15-minute bins: that is the hours of complete failure in the period, whatever its length. Do not average: 3 hours of total failure averages 0.125 over a day, 0.042 over 3 days and 0.018 over 7. Good is no hours, Suspect under 1 hour, Bad 1 hour or more, the same for any period. Borderline findings have no rows, so they read Good. A `Score` left empty means not enough data, not a problem.
- **Detectors**: pass `predict()`'s answers from `atspm-detector` where you can. Without them `detector_config` is used, which holds the agency's settings rather than what the detectors are found to do: `Function` is taken as named, with `Stopbar Count` read as `Count`, every detector counts as one lane, and a detector with several rows takes its lowest phase.
- **Clocks**: only rows stamped before the end of the run's last 15-minute bin are processed. A signal whose last event is more than `clock_ahead_minutes` past the typical signal's last event is treated as having a wrong clock and does not move that time, so its later rows are left out and counted. Rows left out this way, rows with an impossible time, and rows the store already covers (the repeated hour of the fall clock change, for example) are printed per signal at `verbose=1`. That log is the only sign of them: such a signal otherwise just looks quiet in the results.

`to_sql=True` is not supported, and `remove_incomplete` does not apply. The `traffic-anomaly` parameters (`data`, `device_groups`, `decompose_params`, `anomaly_params`, `return_last_n_days`) were removed in 3.0.0 and raise an error.

### Timeline events (non-binned)

The **`timeline`** table is an event-level dimension for troubleshooting and visualization and is *not* binned into `bin_size` intervals. Each row includes:

- `DeviceId`
- `StartTime` / `EndTime`
- `Duration` (seconds between `StartTime` and `EndTime`)
- `EventClass` (for example, Green, Yellow, Ped Service, Split, Preempt)
- `EventValue` (phase/overlap or a coded value, depending on `EventClass`)
- `IsValid` (whether the start/end pair is complete; intervals that overlap a controller clock update, a Siemens hourly log restart, or a device silence are also marked invalid)

When `has_data` is available, an interval is also marked invalid if any `bin_size` bin it spans has no `has_data` row for that device, since events may have been missed while the device was not reporting. This works the same for incremental runs: each run saves a marker per device in the unmatched events (synthetic `EventId` 935) holding the last bin that had data, so a gap between or across runs is still detected when the interval finally ends.

`has_data` can only see an outage that empties a whole sub-bin, so the timeline also looks for device silences: two consecutive events from a device, of any `EventId`, further apart than `max_event_gap_seconds`. An interval paired across a silence (a green, phase call or phase wait bridging an 11-minute outage, say) is marked invalid. A controller resting in green overnight can log nothing for many minutes, so the threshold depends on the time of day the silence starts. The default is 120 seconds from 06:00, 300 from 21:00, 900 from 23:00 and 300 from 05:00. These were set from a full day of high-resolution data for 420 signals, where silences over 120 seconds outside outages were almost all overnight. Pass a dict of `'HH:MM'` start times to seconds for a different schedule, a single number for all day, or `None` to turn the check off. Incremental runs save each device's last event time in the unmatched events (synthetic `EventId` 936), so a silence across runs is found and the result matches a single pass over the same data.

Clock Update events have no reliable size, since the controller's time correction (`EventValue`) is optional and often 0. Any interval overlapping a 10-second window around each clock update, from 5 seconds before to 5 seconds after, is therefore marked invalid. The Clock Update row itself is shown like other point-in-time events, starting at the update and lasting `cushion_time` seconds.

With `controller_type='siemens'`, the timeline also handles the hourly log restart. Siemens controllers stop logging at the top of each hour and resume a few seconds later with `EventId` 1000 and a snapshot of the current state stamped with the restart time, so an interval running across the hour has its true start or end lost. Any interval overlapping the window from the device's last event before the restart (no earlier than the top of the hour) through the restart itself is marked invalid. Event 1000 is vendor-specific, so this only applies when the controller type is set.

Passing `maxtime=True` to the `timeline` aggregation adds MAXTIME-only events such as splits and alarm group events (Event 175).
Passing `live=True` keeps incomplete timeline events (normally dropped), marks them `IsValid=False`, and assigns a common `EndTime` at the current bin boundary. This is useful for near-real-time dashboards.

### Aggregation Dependencies

Some aggregations require other aggregations to be included in your processing run. The processor will automatically validate these dependencies and raise an error if a required dependency is missing:

| Aggregation | Required Dependencies |
| --- | --- |
| `timeline` | `has_data` |
| `coordination_agg` | `timeline`, `has_data` |
| `phase_wait` | `timeline` |
| `ped_delay` | `timeline` |
| `platoon_ratio` | `arrival_on_green` |

The processor automatically sorts aggregations to ensure dependencies run first, so you don't need to worry about the order in your aggregations list.

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
| Offset Change | 0-255 |
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
| Ped Detector Failed | 1-255 |
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
| Clock Update | 0-255 |
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
*   `Total_Actuations` (INT16): Vehicles from the advance detectors arriving at the stop bar in this bin.
*   `Percent_AOG` (FLOAT): Fraction of those arriving on green (0.0 - 1.0). A bin with vehicles and none on green reads 0, and a bin with no vehicles has no row.

#### platoon_ratio
*   `TimeStamp` (DATETIME): Bin start time.
*   `DeviceId` (INTEGER or TEXT): Unique identifier for the controller (type depends on input).
*   `Phase` (INT16): Phase number.
*   `Total_Actuations` (INT16): Vehicles from the advance detectors arriving at the stop bar in this bin (from `arrival_on_green`).
*   `Green_Actuations` (INT16): Of those, the ones that arrived during green.
*   `Percent_AOG` (FLOAT): Fraction of actuations arriving on green (from `arrival_on_green`), equal to `Green_Actuations / Total_Actuations`.
*   `Green_Seconds` (FLOAT): Seconds the phase displayed green within the bin. Green intervals that straddle a bin boundary are split between bins, so this column sums exactly across consecutive bins.
*   `Green_Ratio` (FLOAT): `Green_Seconds` divided by bin length (g/C when the phase cycles continuously).
*   `Platoon_Ratio` (FLOAT): `Percent_AOG / Green_Ratio`. 1.0 = random arrivals; > 1 = platoons arriving on green; < 1 = arriving on red. Bins with no green for the phase are excluded.
*   `Arrival_Type` (UTINYINT): HCM arrival type from `Platoon_Ratio`: 1 (≤ 0.50), 2 (≤ 0.85), 3 (≤ 1.15), 4 (≤ 1.50), 5 (≤ 2.00), 6 (> 2.00).

`Platoon_Ratio` is a ratio of two ratios, so it cannot be averaged across bins: the mean of the per-bin values is not the value for the period, and it comes out biased high because bins with a small `Green_Ratio` inflate the quotient. To aggregate over any longer period (an hour, a day, a time of day), sum the four raw columns and divide once:

```sql
SELECT SUM(Green_Actuations) / SUM(Total_Actuations)
       / (SUM(Green_Seconds) / (COUNT(*) * <bin_size> * 60.0)) AS Platoon_Ratio
FROM platoon_ratio
GROUP BY DeviceId, Phase
```

`Green_Actuations` and `Green_Seconds` are output for exactly this purpose.

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

#### detector_health
One row per bin per problem; no row means no problem found. A row is written once, by the first verdict to find it, and never changes, so a new row can carry a past `TimeStamp`; the latest bin is the live status. Written only by runs that reach a verdict.
*   `TimeStamp` (DATETIME): Bin start time.
*   `DeviceId` (INTEGER or TEXT): Unique identifier for the controller (type depends on input).
*   `DetectorType` (TEXT): `Vehicle` or `Ped`. The two kinds of number can overlap.
*   `Detector` (INTEGER): Detector, or ped input for a button.
*   `Label` (TEXT): The problem, or `not_enough_data` (one row a day).
*   `Score` (DOUBLE): How much of the bin the detector was failing: about 1 for a complete failure over the whole bin, 0.5 for half a bin of it. Problems at the same moment share that moment's worst value, so the rows of a bin add up to at most 1. Empty on `not_enough_data`.

#### detector_health_findings
Written by `detector_health` when its `findings` param is True (default False), for looking into a detector. The findings of the run's last verdict: one row per finding, with notes and borderline findings too.
*   `TimeStamp` (DATETIME): Time of the verdict.
*   `DeviceId` (INTEGER or TEXT): Unique identifier for the controller (type depends on input).
*   `DetectorType` (TEXT): `Vehicle` or `Ped`.
*   `Detector` (INTEGER): Detector, or ped input for a button. Empty on a note about the whole signal.
*   `Start` (DATETIME): Start of the finding.
*   `End` (DATETIME): End of the finding.
*   `Label` (TEXT): What was found.
*   `Confidence` (DOUBLE): 0 to 1, the check's own severity: 0.5 at its suspect limit, 0.8 at its bad limit.
*   `Detail` (TEXT): The evidence.

#### has_data
*   `TimeStamp` (DATETIME): Bin start time where data exists.
*   `DeviceId` (INTEGER or TEXT): Unique identifier for the controller (type depends on input).

#### ped
*   `TimeStamp` (DATETIME): Bin start time.
*   `DeviceId` (INTEGER or TEXT): Unique identifier for the controller (type depends on input).
*   `Phase` (INT16): `Parameter` of the event: the phase of a service, the ped input of a press. The two are read as one number.
*   `PedServices` (INT16): Ped services: walks begun (`EventId` 21) and ped overlap walks begun (67).
*   `PedActuation` (INT16): Button presses (`EventId` 90), repeat presses included.
*   `Unique_Actuations` (INT16): Unique presses: those more than `seconds_between_actuations` (default 15) after the previous press on that input, or with no earlier press known.
*   `Estimated_Volumes` (FLOAT): Estimated pedestrians from this bin and the 3 before it. Left out by `return_volumes=False` (default True). See [Pedestrian counts and volumes](#pedestrian-counts-and-volumes).

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
*   `MaxPhaseWait` (FLOAT): Longest wait in seconds.
*   `TotalSkips` (BIGINT): Count of skipped phases.
*   `Samples` (INT32): Waits averaged in this bin. A wait left out for a preempt is not one.

To roll the average up over a longer period, weight it: `SUM(AvgPhaseWait * Samples) / SUM(Samples)`. A plain average of the bins gives a bin with one wait the same say as a bin with twenty.

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
*   `Green_Time` (FLOAT): Average green time, in seconds per cycle.
*   `Green_Occupancy` (FLOAT): Average green occupancy (0.0 - 1.0).
*   `Red_Occupancy` (FLOAT): Average red occupancy (0.0 - 1.0).
*   `Split_Failure` (INT16): Cycles that failed.
*   `Cycles` (INT16): Cycles finished in this bin, the count behind the three averages and `Split_Failure`. Left out with `by_cycle=True` (default False), where each row is one cycle.

`Cycles` lets a longer period roll up exactly from this table alone:

```sql
SELECT DeviceId, Phase,
       SUM(Split_Failure) / SUM(Cycles) AS Share_Failed,
       SUM(Green_Time * Cycles) / SUM(Cycles) AS Green_Time,
       SUM(Green_Occupancy * Cycles) / SUM(Cycles) AS Green_Occupancy,
       SUM(Red_Occupancy * Cycles) / SUM(Cycles) AS Red_Occupancy
FROM split_failures
GROUP BY DeviceId, Phase
```

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
The events a later run works out `split_failures` from: each detector's last event, and the events of a phase's unfinished cycle from its green on, with `Detector` 0 on the phase events.
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

Rows with `EventId` 931 to 955 are state carried between runs, not controller events. Each number has one meaning:

| `EventId` | Written by | Holds | `Parameter` |
| --- | --- | --- | --- |
| 931 to 934 | `coordination_agg` | Pattern, cycle length, actual cycle length, actual offset | The value |
| 935, 936 | `timeline` | The last bin with data and the last event of each device | 0 |
| 937 | `ped` | Each input's last press, one row per input | Input |
| 938 | `arrival_on_green`, `platoon_ratio` | A vehicle arriving after results stopped, at its projected time | Phase |
| 939, 940, 941 | `arrival_on_green`, `platoon_ratio` | Green, yellow and red clearance begins not written yet. 939 also marks a phase green when results stopped | Phase |
| 942 | `arrival_on_green`, `platoon_ratio`, `ped` or `yellow_red` with `remove_incomplete` | A bin not written yet that passed `has_data` | 0 |
| 943 | `ped` | One unique actuation, stamped with its bin, in a bin not written yet or the last 3 written | Input |
| 944, 945 | `ped` | Each input's next bin to write, and its last activity | Input |
| 946, 947 | `ped` | One press, or one ped service, stamped with its bin, in a bin not written yet | Input |
| 948 | `arrival_on_green`, `platoon_ratio` | A vehicle arriving after results stopped that began while its detector held the spillback flag | Phase |
| 949 | `arrival_on_green`, `platoon_ratio` | A detector holding the spillback flag, stamped when it last turned off, or when it turned on if it is on | Detector |
| 950 | `arrival_on_green`, `platoon_ratio` | A detector on at the end of the data, stamped when it turned on | Detector |
| 951, 952, 953 | `yellow_red` | Green, yellow and red clearance begins of a cycle not finished writing | Phase |
| 954 | `yellow_red` | An actuation of a cycle not finished writing, already moved back by the latency | Phase |
| 955 | `timeline` | The last phase call event of each device | 0 |

## Release Notes

See [CHANGELOG.md](docs/CHANGELOG.md) for a full history of changes.

## Future Plans

- Develop comprehensive documentation for each performance measure.

## Contributing

Ideas and contributions are welcome! Please feel free to submit a Pull Request. Note that GitHub Actions will automatically run unit tests on your code.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

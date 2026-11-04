# Release Notes

### Version 3.0.0 (October 29, 2026)

#### Breaking Changes:

- **DuckDB 2 and Python 3.11+**: `atspm` now requires `duckdb>=2.0.0` (declared directly; it used to arrive through Ibis) and Python 3.11 or newer, and is tested on Python 3.11 to 3.15. Queries that read a single row now use `fetchall()`, since DuckDB 2.0 relations no longer have `fetchone()`.
- **No more Ibis**: `ibis-framework` and `pyarrow_hotfix` are no longer dependencies.
- **`detector_health` is now `atspm-detector`'s health; `traffic-anomaly` is removed**: the aggregation no longer scores binned actuations. It reads `raw_data` and finds failing vehicle detectors and ped buttons in the same pass, with `pip install atspm[detector-health]` (`atspm-detector>=2.0.0`; a missing package raises an error naming the extra). Each run adds its events to a health store file that keeps `horizon_days` (default 3), and verdicts are written `daily` by default, or `hourly`, on `every_run`, or never. With no store, one run is judged on its own. The output is `detector_health`, one row per finding with `TimeStamp`, `DeviceId`, `DetectorType`, `Detector`, `Start`, `End`, `Label`, `Confidence` and `Detail`, plus `detector_health_status` with `summary=True` and the optional `detector_health_15min` and `detector_health_active` with `outputs`. The old parameters (`data`, `device_groups`, `decompose_params`, `anomaly_params`, `return_last_n_days`) raise an error. No other aggregation changes: their outputs, `unmatched_events` and `known_detectors` were compared across incremental runs with and without `detector_health`, under DuckDB 1.5 and 2.0. See the README's Detector health section.
- **`Percent_AOG` and `Platoon_Ratio` change for every user: arrivals are now projected to the stop bar.** `arrival_on_green` used the time a vehicle crossed the advance detector, as if the detector were at the stop bar. It now follows the UDOT method (ATSPM Methods and Assumptions Manual 4.3, Purdue Coordination Diagram, Step 1): `arrival = detector on + distance / (speed * 1.467) - latency`. The arrival decides both whether the vehicle is on green and which bin it counts in, so `Total_Actuations` moves too. `detector_config` gains three optional columns, `DistanceFromStopBar` (feet), `MPH` and `LatencyCorrection` (seconds), and each value that is missing from a row takes its default: 35 MPH, 220 feet for phases 2 and 6, 120 feet for all others, and `latency_offset_seconds` for the latency, which now defaults to 0. Old configs keep working and take the defaults. On two days of 60 ODOT signals with the defaults, `Percent_AOG` over all bins goes from 67.2% to 68.0% on phases 2 and 6 and from 38.6% to 37.3% on the others, and `Platoon_Ratio` from 1.01 to 1.02 and from 2.13 to 2.08. Single phases with 500 or more vehicles move by up to 7 points. Values already stored are not comparable with new ones. With `to_sql=True`, your own `detector_config` table needs all three columns.
- **`arrival_on_green` and `platoon_ratio` write each bin once, whole**: a bin is written by the first run whose data reaches its end, so the rows equal one big run wherever runs are cut. A run whose last event is more than a second short of the end of a bin leaves that bin for the next run instead of writing part of it. Incremental runs carry what is not written yet in `unmatched_events` (synthetic `EventId` 938 to 942, see the README's table). They no longer need `timeline` in the run, and no longer read the green `timeline` carries. The first run from an older unmatched file has no carried green, so a phase green across that one cut counts as not green until its next green. A green now ends at red clearance begin (10) in `platoon_ratio` when the yellow begin was lost, as it already did in `arrival_on_green`, rather than running on to the next green.
- **A bin with vehicles and none on green now reads 0 in `arrival_on_green`**, where it used to have no row, so an unweighted average of `Percent_AOG` was too high. `platoon_ratio` reads 0 in such a bin too if the phase had any green. On about 2 days of 60 ODOT signals, 21% of rows read 0, holding 2% of the vehicles, with a median of 3 vehicles a bin. `Percent_AOG` weighted by vehicles goes from 63.9% to 62.6%, and unweighted from 57.3% to 45.1%. Weighting by `Total_Actuations` is how to roll these up.
- **`Unique_Actuations` and `Estimated_Volumes` change meaning**: `seconds_between_actuations` had no default, so without it `unique_ped` and `full_ped` only removed presses at the same instant, and now it defaults to 15. The first press of every run was dropped because it had no earlier press, and now a press with no earlier one known counts. Incremental runs carry each input's last press in `unmatched_events` (synthetic `EventId` 937, one row per input), so these counts equal one big run wherever the runs are cut. Older unmatched files still load: their first press on each input counts. `seconds_between_actuations` may now be a decimal. On one day of ODOT data run in 3-hour windows that gave no setting, `Unique_Actuations` falls from 95% to 41% of presses and `Estimated_Volumes` falls to about a quarter. Values already stored cannot be repaired from the old counts.
- **`unique_ped` and `full_ped` write each bin once, whole**, by the same rule as `arrival_on_green`: with a main unmatched file, a run whose last event is more than a second short of the end of a bin leaves that bin for the next run, where a bin cut by a run used to get two rows that had to be added up. A press in a bin already written is too late to count. They carry what they need in `unmatched_events` (synthetic `EventId` 943 to 947, see the README's table). A run that reaches the end of its last bin holds nothing back.
- **`Estimated_Volumes` now reads a bin and the 3 bins before it, not the 3 after it**: the last 3 bins of every run counted the bins past its end as zero and were never corrected. On 758 ODOT signals over 2 days run in 3-hour windows, the last 3 bins of a window read 8%, 16% and 24% low and the day's total 4% low. Now the rows equal one big run wherever the runs are cut, and no past row changes. The formula is otherwise the same, and against the forward-looking values on the same counts, day totals move by at most 0.02% across the network and 0.3% for 99% of inputs, the peak hour keeps its size on average and starts within 15 minutes 77% of the time, the middle of an input's day comes a median of 3.7 minutes later, and 95% of bins with peds move by 1 ped or less. See the README's Pedestrian counts and volumes section. **Rows change too**: an input now has rows from its first activity, not the start of the run, and through runs where it does nothing until `max_days_old` passes, so `full_ped` has more zero rows, 40% more rows on the same data in 3-hour windows. `Estimated_Volumes` already stored is not comparable with new values. The first run from an older unmatched file reads the bins before it as quiet.
- **`Percent_AOG` and `Platoon_Ratio` change again: a queue over the advance detector now counts as arriving on red.** UDOT's method has no step for this. Vehicles that stop on the detector on red begin actuating it when the queue moves, on green, and read as arriving on green. An advance detector ON for more than the new `spillback_on_seconds` (default 6) now gets a flag, and every actuation that begins while it holds the flag is an arrival on red, until the detector has been OFF for the new `spillback_gap_seconds` (default 5). `Total_Actuations` does not change. `spillback_on_seconds=None` gives the old values, exactly. On three days of 846 ODOT signals (Oct 26-28 2026, 50.8 million vehicles), `Percent_AOG` weighted by vehicles goes from 61.2% to 56.4%: phases 2 and 6 from about 69% to 65%, phases 4 and 8 from 35% to 27%, left turns from about 28% to 21%. From 3 to 6 pm the move is about 7 points and from midnight to 5 am about 1. Across settings from 4 to 10 seconds the network move runs from 3.3 to 7.7 points. Incremental runs carry the flag in `unmatched_events` (synthetic `EventId` 948 to 950, see the README's table) and equal one big run.
- **`yellow_red` now gives the same rows in incremental runs as in one big run, and follows `bin_size`.** It used to lose the actuations of any cycle cut by a run: on 12 signals over one day cut every 15 minutes it lost 7.9% of its actuations, and with cuts inside a bin it wrote rows twice. Its bins were also always 15 minutes. A bin is now written once the last cycle with a red in it has finished, and the unfinished cycles wait in `unmatched_events` (synthetic `EventId` 951 to 954), so with runs cut on the bin each one comes out a run later. It no longer reads the open rows `timeline` carries. A run with no unmatched settings gives the same rows as before at 15-minute bins.
- **`drop_fall_back_hour`, on by default, drops events in 1:00-2:00 am on the first Sunday of November before anything is computed**, in every aggregation, and `has_data` has no row for that hour. Clocks repeat that hour, the two passes cannot be separated, and ODOT's staging data of Nov 1 2026 had about 100 copies of each event in it, with false phase conflicts following. It assumes US rules and local timestamps, and `drop_fall_back_hour=False` turns it off. See the README's The fall clock change section.

#### Bug Fixes / Improvements:

- **pandas 3**: `pytz` is now a dependency. pandas 3 no longer installs it, and DuckDB needs it to return time zone aware timestamps, so `SignalDataProcessor()` failed on creation without it.
- The error for `to_sql=True` with `detector_health` now says it is not supported (it read "is  supported").
- Removed the misspelled `[tools.setuptools]` section from `pyproject.toml`, which setuptools always ignored; the package contents are unchanged.
- **Faster and lighter, with the same results**: on 60 real signals over 3 hours, a run with all aggregations went from 2.2 to 1.5 seconds and from 670 to 390 MB at its peak. On all 664 signals it went from 30 to 16 seconds and spills much less to disk. `raw_data` is now a view of a table in a compressed in-memory database, so it can be read as before but not written to or dropped as a table.
- **Two runs of the same data now give the same rows**: events stamped in the same tenth of a second sorted either way from run to run, and on main about 1 or 2 of 9,533 `ped_delay` rows on 60 signals over 2 days, the same `timeline` rows and 7 or 8 `sf_unmatched` rows differed between runs. Ties now break on `EventId`, or `Parameter` for on and off toggles. A press in the tenth of a walk (21) or of flashing don't walk (22) sorts after it, so it is a press during that service and not a delay of zero. That is measured: over 2 days of 758 signals, the walk that answered a press came 2 or 3 tenths after it about 19,000 times, 1 tenth after it 620 times and in the same tenth 9 times. A start sorts before its end in the same tenth, a detector off before an on and a restore before a fault; those are inferred, since they did not tie in the 2-day sample. Cycle lengths stamped together use the longer, a detector on two phases no longer reads its copies as two events in a row, and `platoon_ratio` adds green time in whole milliseconds, which moves 5 of 39,153 `Platoon_Ratio` values by about 0.0000002. Repeated runs at 6 threads and runs with the input in a different order are identical.

### Version 2.6.1 (October 12, 2026)

#### Bug Fixes / Improvements:

- **Timeline intervals across a device silence (false phase skips)**: an interval paired across a stretch where the device logged nothing kept `IsValid=True` unless the stretch emptied a whole `has_data` sub-bin. With `has_data` at `no_data_min=15, min_data_points=1`, outages under 15 minutes were never caught. On 2026-10-07 one signal logged nothing for 11.5 minutes; Green 2/6, Overlap Green, Phase Call and a 794.6 s Phase Wait 4 bridged the outage, and `phase_wait` reported a skip with `MaxPhaseWait=794.6`. Across 19 days of one agency's data, about 18% of reported skips were outages like this. The timeline now marks any interval that overlaps a device silence as `IsValid=False`. A silence is two consecutive events from a device, of any `EventId`, further apart than the new timeline parameter `max_event_gap_seconds`. The threshold depends on the time of day the silence starts, because controllers resting in green overnight can log nothing for minutes. The default is 120 s from 06:00, 300 s from 21:00, 900 s from 23:00 and 300 s from 05:00. It takes a dict of `'HH:MM'` to seconds, a single number for all day, or `None` to turn it off. Incremental runs carry each device's last event time in the unmatched events (synthetic `EventId` 936), so results match a single pass. `phase_wait` already ignores invalid waits, so these no longer count as skips or inflate `AvgPhaseWait`/`MaxPhaseWait`. `has_data` is unchanged.
- **`actuations` zero-fill during data outages**: `fill_in_missing` filled every known detector with 0 for every bin from the earliest to the latest actuation across all devices, without checking whether the device sent anything in that bin. A device with a feed outage got zero counts on all its detectors, which `detector_health` then flagged as failed. Detectors are now zero-filled only in bins where their device has at least one raw event; bins with no events for a device produce no rows for it. Detectors known only from `known_detectors` are still zero-filled while their device reports.

### Version 2.6.0 (October 6, 2026)

#### New Features:

- **Siemens controller type (`controller_type='siemens'`)**: Siemens controllers write a new log each hour. Logging stops at the top of the hour and resumes a few seconds later (sometimes 30 or more) with EventId 1000 and a snapshot of the current state, all stamped with the restart time. Events in between are lost, so an interval running across the hour had its true start or end replaced by the snapshot time, showing up as short or long yellow and red clearances every hour. With the Siemens controller type, the timeline marks any interval overlapping the window from the device's last event before the restart (no earlier than the top of the hour) through the restart itself as `IsValid=False`, including still-open intervals carried into the next incremental run. Event 1000 is vendor-specific, so nothing changes for other controller types.

### Version 2.5.1 (September 28, 2026)

#### Bug Fixes / Improvements:

- **Detector health out of memory on large inputs**: `detector_health` passed every step (decompose, the `device_groups` join, anomaly) back through pandas, making several full copies with string columns as Python objects, and ran out of memory at around 70M rows. The traffic-anomaly steps are now compiled to SQL and run in the processor's DuckDB connection, which can spill to disk. Output is unchanged. `data` also accepts a file path (e.g. parquet), and the processor no longer holds its own reference to the input DataFrame.
- **Empty raw data with carried-over state**: a run with no raw data failed with a timestamp conversion error when loading unmatched events or known detectors. It now falls back to the epoch, as `SignalDataProcessor` already did for an empty run.
- **DuckDB relations as input**: relations such as `sample_data.data` belong to DuckDB's default connection and failed to load. They are now converted to Arrow first, and Arrow tables report their column names correctly.
- **Less sorting and scanning**: removed `ORDER BY`s when tables are created (exports still sort) and combined the raw data MIN/MAX lookups into one query.
- Declared `ibis-framework[duckdb]` as a dependency, now imported directly.

### Version 2.5.0 (September 17, 2026)

#### New Features:

- **Platoon Ratio (`platoon_ratio`)**: New aggregation implementing the HCM platoon ratio, `Rp = Percent_AOG / (g/C)`, by phase and bin, plus the HCM arrival type (1–6). The green ratio is measured from phase green intervals (EventId 1 → 8) clipped to bin boundaries, with open intervals at the edges of the data extended to the bin boundary so results are exact under bin-aligned incremental processing. Depends on `arrival_on_green` (registered in `AGGREGATION_DEPENDENCIES`). Contributed by @Abimbola08 in #26, closes #6.
- **Raw counts in `platoon_ratio` output**: Added `Green_Actuations` and `Green_Seconds` columns. `Platoon_Ratio` is a ratio of two ratios and cannot be averaged across bins, so rolling it up to an hour, a day or a time of day requires summing the raw parts and dividing once. Those two columns plus `Total_Actuations` make that possible without hardcoding the bin size downstream, and because greens are clipped at bin boundaries, `Green_Seconds` sums exactly across consecutive bins. See the `platoon_ratio` schema in the README for the aggregation query.
- **Timeline clock update, offset change and ped detector failure**: Controller Clock Updated (181) is now a timeline event, Clock Update, shown as a point-in-time event lasting `cushion_time` with `EventValue` holding the reported correction in seconds. Because the controller stamps every event with its own clock, any interval overlapping a window from 5 seconds before to 5 seconds after a clock update is marked `IsValid=False`. Offset change and ped detector failure were added as timeline events as well.

#### Bug Fixes / Improvements:

- **Timeline validity across data gaps in incremental runs**: A timeline interval should be invalid when the device has no `has_data` row for any bin it spans. Single-pass runs checked every bin, but incremental runs only checked the bins the interval started and ended in, so a device that dropped out mid-window and returned hours later produced a valid interval with a duration of hours, and `phase_wait` counted the Phase Wait as a skipped phase. The opposite case is also fixed: a still-open event had its start bin re-checked against each new run's `has_data`, where it can never appear, so any interval open across two or more runs was marked invalid even with continuous data.

### Version 2.4.0 (August 2026)

#### Bug Fixes / Improvements:

- **Relaxed phase skip threshold when TSP adjusted the cycle (`tsp_skip_multiplier`)**: Phase waits overlapping a preempt are dropped outright, but TSP delays service without skipping it, so at a signal calling TSP every cycle the 1.5x multiplier reported skips for phases that were actually served. Rather than dropping those waits, which would blind the check at signals that have TSP, the wait is kept and judged against the new `tsp_skip_multiplier`, defaulting to 2.0.

### Version 2.3.3 (2026)

#### Bug Fixes / Improvements:

- **Duration sanity check on phase yellow/red timeline validity**: Phase yellow (8/9) and red (10/11) clearance intervals longer than 10 seconds are implausible and indicate bad data, so they are marked `IsValid=False`. Implemented as an optional `max_duration` argument on the `paired_event` macro; overlap yellow/red (63/64) are unaffected.

### Version 2.3.2 (2026)

#### Bug Fixes / Improvements:

- **Overlap yellow timeline validity**: Fixed validity determination for overlap yellow intervals.

### Version 2.3.1 (2026)

#### Bug Fixes / Improvements:

- **Overlap red timeline validity**: Fixed validity determination for overlap red intervals.

### Version 2.3.0 (February 23, 2026)

#### Bug Fixes / Improvements:

- **Known detector retention fix (`known_detectors_max_days_old`)**: Fixed incremental `actuations` zero-fill behavior so `known_detectors.LastSeen` is correctly updated to the most recent timestamp across current and prior runs. This prevents detectors from aging out early (for example, dropping around 24 hours when configured for 2 days) and restores expected retention for detector health flatline continuity.
- **Timeline live mode**: Added `live` option to `timeline` aggregation. When `live=True`, incomplete timeline events are emitted instead of dropped, marked `IsValid=False`, and assigned one common `EndTime` aligned to the current bin end; they can still be matched in subsequent incremental runs.
- **Tests**: Added regression test for known-detector retention and timeline live-mode tests for incomplete event behavior.

### Version 2.2.0 (January 26, 2026)

#### Bug Fixes / Improvements:

- **Phase Wait ASOF join for accurate skip detection**: `phase_wait` now uses ASOF join on cycle length events instead of binned aggregation. This prevents false positive skip detection when cycle length changes mid-phase-wait by using the `GREATEST` of start and end cycle lengths.
- **MAXTIME ActualCycleLength support**: Added `controller_type` as a global setting. When set to `'maxtime'`, `phase_wait` uses the more accurate `ActualCycleLength` events (EventId 316) directly from raw data via ASOF join. 
- **Incremental processing**: Added support for incremental runs for `phase_wait` and `coordination_agg`, carrying forward previous state via synthetic events in `unmatched_events`.
- **Dependency validation and Sorting**: The processor now automatically validates and sorts aggregations based on their requirements (e.g., `timeline` after `has_data`).
- **Global Settings and Performance**: Added `controller_type` as a global parameter. Non-MAXTIME controllers now automatically skip MAXTIME-specific measures (`splits`, `coordination`).
- **Documentation**: Added aggregation dependencies table and updated README to reflect new global parameters and internal logic improvements.

### Version 2.1.3 (January 20, 2026)

#### Bug Fixes / Improvements:

Fixed bug where phase wait events were marked as valid even when the underlying phase call was not valid.

### Version 2.1.2 (January 15, 2026)

#### Bug Fixes / Improvements:

Bug fix to data processing by adding IsValid column tracking and updating logic for timeline events. This allows marking timeline events as invalid if they span over a missing data period. Introduce tests to validate behavior with and without has_data filtering.

### Version 2.1.1 (January 14, 2026)

#### Bug Fixes / Improvements:
- Fixed crash when an empty dataframe is provided as `raw_data`. The processor now handles empty input gracefully by using epoch timestamps, allowing all aggregations to complete with empty outputs and correct schemas.
- Fixed SQL parser error when `remove_incomplete=True` caused queries with SQL comments to break the `NATURAL JOIN has_data` wrapper.
- Added unit test `test_empty_dataframe_input` covering all aggregations with empty input.

### Version 2.1.0 (January 6, 2026)

#### New Features:
- Added **Coordination Aggregate (`coordination_agg`)**: Binned state for active Pattern, Cycle Length, Actual Cycle Length (MAXTIME), and Actual Offset (MAXTIME). Includes incremental support with fill-forward logic.
- Added **Phase Wait (`phase_wait`)**: Average wait time for green after a phase call. Features configurable preempt recovery time, assumed cycle length for free mode, and skip threshold.
- Added **Cycle Length Change (132)** to the `timeline` aggregation.

### Version 2.0.0 (December 9, 2025)

#### Breaking Changes / Features:
- Timeline now outputs `EventClass`/`EventValue` (bucketed `TimeStamp` removed) and retains `IsValid`; use `maxtime=True` to include MAXTIME-only events (Splits and Alarm event 175).
- The `IsValid` column is true when the data appears complete, but if there are missing events then it is False.
- Removed `splits_only` in favor of the `maxtime` flag for timeline.
- Added `ped_delay` aggregation that averages pedestrian delay from timeline using `EndTime` buckets at the configured `bin_size`.
- Documented the timeline dimension table in this README (previously available via `SignalDataProcessor.timeline_description`).

### Version 1.9.4 (November 24, 2025)

#### Bug Fixes / Improvements:
- Better housekeeping to reduce memory usage and added optional context manager support for `SignalDataProcessor`.

### Version 1.9.3 (November 15, 2025)

#### Bug Fixes / Improvements:
- Added unit test for detector health, fixed a table-name bug, and updated dependencies.

### Version 1.9.2 (November 15, 2025)

#### Bug Fixes / Improvements:
- Bug fix to work with the new `traffic-anomaly` API.

### Version 1.9.1 (March 4, 2025)

#### Bug Fixes / Improvements:

Filling in missing time periods for detectors with zero actuations didn't work for incremental processing, this has been fixed by tracking a list of known detectors between each run, similar to the unmatched event tracking. So how it works is you provide a dataframe or file path of known detectors, it will filter out detectors last seen more than n days ago, and then will fill in missing time periods with zeros for the remaining detectors.

```python
known_detectors_df='path/to/known_detectors.csv'
# or supply Pandas DataFrame directly

from atspm import SignalDataProcessor, sample_data

# Set up all parameters
params = {
    # Global Settings
    'raw_data': sample_data.data,
    'bin_size': 15, 
    # Performance Measures
    'aggregations': [
        {'name': 'actuations', 'params': {
                'fill_in_missing': True,
                'known_detectors_df_or_path': known_detectors_df,
                'known_detectors_max_days_old': 2
        }}
    ]
}
```

After you run the processor, here's how to query the known detectors table:

```python
processor = SignalDataProcessor(**params)
processor.load()
processor.aggregate()
# get all table names from the database
known_detectors_df = processor.conn.query("SELECT * FROM known_detectors;").df()
```

Here's what the known detectors table could look like:

| DeviceId | Detector | LastSeen |
|----------|----------|----------|
| 1        | 1        | 2025-03-04 00:00:00 |
| 1        | 2        | 2025-03-04 00:00:00 |
| 2        | 1        | 2025-03-04 00:00:00 |

### Version 1.9.0 (February 19, 2025)

#### New Features:

Added option to fill in missing time periods for detector actuations with zeros. This makes it clearer when there are no actuations for a detector vs no data due to comm loss. Having zero-value actuation time periods also allows detector health to better identify anomalies due to stuck on/off detectors. 

New timeline events:
- Pedestrian Delay (from button push to walk)
- Overlap Events
- Detector faults including stuck off and other
- Phase Hold
- Phase Omit
- Ped Omit
- Stop Time

Also updated tests to include these new features. This is a lot of new events to process, so be sure to test thoroughly before deploying to production.

### Version 1.8.4 (September 12, 2024)

#### Bug Fixes / Improvements:
Fixed a timestamp conversion issue when reading unmatched events from a csv file. Updated the unit tests to catch this issue in the future. 

### Version 1.8.3 (September 5, 2024)

#### Bug Fixes / Improvements:
- Fixed estimated volumes for full_ped. Previously, it was converting 15-minute ped data to hourly by applying a rolling sum, then applying the quadratic transform to get volumes, and then converted back to 15-minute by undoing the rolling sum. The bug had to do with the data not always being ordered correctly before undoing the rolling sum. However, this update removes the undo rolling sum altogether and replaces it with multiplying hourly volumes by the ratio of 15-minute data to hourly data (more detail coming in the docs eventually). It seems to work much better now.

### Version 1.8.2 (August 29, 2024)

#### Bug Fixes / Improvements:
- Fixed issue when passing unmatched events as a dataframe instead of a file path.
- Added more tests for incremental runs when using dataframes. This is to mimic the ODOT production environment.

### Version 1.8.0 (August 28, 2024)

#### Bug Fixes / Improvements:
- Removed unused code from yellow_red for efficiency, but it's still not passing tests for incremental processing.

#### New Features:
- Added special functions and advance warning to timeline events.

### Version 1.7.0 (August 22, 2024)

#### Bug Fixes / Improvements:
- Fixed issue with incremental processing where cycles at the processing boundary were getting thrown out. This was NOT fixed yet for yellow_red!
- Significant changes to split_failures to make incremental processing more robust. For example, cycle timestamps are now tied to the end of the red period, not the start of the green period. 

#### New Features:
- Support for incremental processing added for split_failures & arrival_on_green. (yellow_red isn't passing tests yet)
- Added phase green, yellow & all red to timeline. 

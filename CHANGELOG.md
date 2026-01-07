# Release Notes

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

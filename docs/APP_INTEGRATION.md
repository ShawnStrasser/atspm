# Integrating atspm into an app

This guide is for apps that keep their signal data in DuckDB. You pass `atspm` your connection, and it reads the events and detector config from your tables. It writes the results and its own state back to tables in one schema, and finds that state again on the next run. Your app only needs to schedule the runs.

Apps that hold data in pandas can keep passing DataFrames as before. See the README's [Usage Example](../README.md#usage-example).

## What your database needs

**Raw events:** a table or view with these columns:

| Column | Type | Notes |
|---|---|---|
| `TimeStamp` | TIMESTAMP | Local controller time |
| `DeviceId` | any | The same type everywhere, including in the config |
| `EventId` | integer | Indiana hi-res event code |
| `Parameter` | integer | |

`atspm` also accepts common variants of these names, such as `SignalId`, `EventCode` and `EventParam`. Extra columns are ignored.

**Detector config:** a table or view with `DeviceId`, `Phase`, `Parameter` (the detector channel) and `Function` (`Advance`, `Presence`, `Count`, `Yellow_Red`, ...). The optional columns are:
- `DistanceFromStopBar`, `MPH` and `LatencyCorrection`;
- `Lanes`, `TravelTime` and `RedDepartures`, which the Queue Model reads. [atspm-detector](https://github.com/ShawnStrasser/atspm-detector) can export all three.

**How atspm finds them:** it uses the table or view named `raw_data` or `detector_config` if one has these columns, and otherwise the only one that does. If several fit, the run raises and names them; pass `raw_data='my_events'` or `detector_config='main.my_config'`. With no config table, the measures that need one write no rows.

## One run

```python
import duckdb
from atspm import SignalDataProcessor

AGGREGATIONS = [
    {'name': 'has_data', 'params': {}},
    {'name': 'actuations', 'params': {'fill_in_missing': True}},
    {'name': 'arrival_on_green', 'params': {'queue_model': True}},   # Queue Model: recommended
    {'name': 'platoon_ratio', 'params': {}},
    {'name': 'approach_delay', 'params': {'queue_model': True}},     # Queue Model delay: recommended
    {'name': 'split_failures', 'params': {'red_time': 5, 'red_occupancy_threshold': 0.80,
                                          'green_occupancy_threshold': 0.70, 'by_approach': True, 'by_cycle': False}},
    {'name': 'timeline', 'params': {'min_duration': 0.2, 'cushion_time': 60}},
    {'name': 'ped', 'params': {'seconds_between_actuations': 15}},
    # ... any others, as in the README
]

app = duckdb.connect('app.duckdb')     # your app's connection

SignalDataProcessor(
    conn=app,
    start='2026-11-19 08:00',
    end='2026-11-19 08:15',            # events with start <= TimeStamp < end
    bin_size=15,
    remove_incomplete=True,
    controller_type='maxtime',
    aggregations=AGGREGATIONS,
    # devices=['1001', '1002'],        # default: every device
    # schema='atspm',                  # default
    # unmatched_event_settings={'max_days_old': 14},   # default
).run()
```

`run()` reads the window, runs every measure, commits the results and the new state, and cleans up. It raises on any error. In that case nothing was written, and you can run the same window again.

## Scheduling

Run every `bin_size` minutes, each window starting where the last one ended:

```python
import pandas as pd

def run_due_windows(app, last_end, lag=pd.Timedelta(minutes=5), bin_minutes=15):
    """Runs every whole window from last_end up to now - lag. Returns the new last_end to store."""
    bin_size = pd.Timedelta(minutes=bin_minutes)
    until = (pd.Timestamp.now() - lag).floor(bin_size)
    while last_end + bin_size <= until:
        SignalDataProcessor(conn=app, start=last_end, end=last_end + bin_size, bin_size=bin_minutes,
                            remove_incomplete=True, aggregations=AGGREGATIONS, verbose=0).run()
        last_end += bin_size
    return last_end
```

- **On the bin:** `start` and `end` must fall on bin boundaries counted from midnight. Anything else raises.
- **Behind your ingest lag:** `end` must be late enough that every event before it has arrived. An event that arrives after its window has run is never read. Keep `lag` above your feed's worst delay.
- **Longer windows** (an hour, or a backlog) are fine: the results are the same however the runs are cut.
- **Running a window twice** raises `raw_data is older than the state`, and nothing is written. Store `last_end` in your own table, or derive it from your schedule.
- **One run at a time per database.** Your app can keep reading and writing its own tables during a run.

## The schema

All results and state go to one schema in your database, `atspm` by default. `atspm` creates it on the first run.

**Output tables**, one per measure, such as `atspm.arrival_on_green`, `atspm.approach_delay` and `atspm.timeline`:
- Each run adds its new rows.
- A bin is written once, by the first run that has all its data, so you never update or deduplicate rows.
- Columns and types are those in the README's [Output Schemas](../README.md#output-schemas). A table is created on the first run that writes it.
- A column added in a newer version is added to the existing table, and older rows read NULL.

**State tables.** `atspm` reads and replaces these itself; your app should not change them:

| Table | Holds |
|---|---|
| `unmatched_events` | Events waiting for a partner, and every measure's unfinished work |
| `sf_unmatched` | `split_failures`' unfinished cycles |
| `health_state` | `detector_health`'s state |
| `health_rows` | `detector_health`'s rows for its whole-day check. Rows the state no longer needs are deleted each run |

- **First run:** with no state tables, a run is a first run.
- **Device subsets:** each run replaces only the state rows of its own devices, so separate runs over separate subsets equal one run over all of them.
- **Two measure lists:** keep each in its own schema. A run raises if its schema's state holds rows of a measure it does not run.

**Reading results:** query the tables as you would any other, for example:

```sql
SELECT DeviceId, Phase, date_trunc('hour', TimeStamp) AS Hour,
       sum(Percent_AOG * Total_Actuations) / sum(Total_Actuations) AS Percent_AOG
FROM atspm.arrival_on_green
GROUP BY ALL;
```

## Atomic runs and your connection

- **All or nothing:** a run adds its outputs and replaces its state in one transaction. A run that fails, or a process that dies, leaves everything as it was before the run.
- **Your connection is left as it was:** the run works on a cursor of your connection, in a private in-memory database, and detaches it at the end. Your tables, default database and settings are untouched, and the connection stays open.
- **Memory and threads:** the run uses your connection's DuckDB settings (`memory_limit`, `threads`, `temp_directory`). Set them there if you need limits.

## detector_health

- **Detectors table:** name your table of [atspm-detector](https://github.com/ShawnStrasser/atspm-detector) answers in `detectors`, for example `'detectors': 'main.detector_answers'`. Leave it out to fall back to `detector_config`.
- **State and history:** they are kept in the schema (`health_state`, `health_rows`), so leave the `state` and `history` parameters out.
- **Devices:** `detector_health` judges each signal's clock against the typical signal, so it runs over all devices; leave `devices` out.
- **The whole-day check** runs by itself at the first run past midnight. `workers` sets how many processes it uses.

## Moving from file state

If your app already ran `atspm` with state files, load them into the schema once, before the first `conn` run:

```sql
CREATE SCHEMA atspm;
CREATE TABLE atspm.unmatched_events AS SELECT * FROM 'state/unmatched_events.parquet';
CREATE TABLE atspm.sf_unmatched AS SELECT * FROM 'state/sf_unmatched.parquet';
CREATE TABLE atspm.health_state AS SELECT * FROM 'state/health_state.parquet';
CREATE TABLE atspm.health_rows AS SELECT * FROM 'health_rows/*.parquet';
```

Leave out any table you do not have. Then start at the window after the last one your app ran.

## Limits

- One `atspm` run at a time per database.
- One measure list per schema.
- `detector_health` cannot run on a subset of devices.
- `output_dir`, `to_sql` and the file paths of `unmatched_event_settings` do not apply with `conn`, and raise.

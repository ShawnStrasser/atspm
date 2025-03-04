# ATSPM Aggregation

`atspm` is a cutting-edge, lightweight Python package that transforms raw traffic signal controller event logs into meaningful Traffic Signal Performance Measures (TSPMs). These measures help transportation agencies continuously monitor and optimize signal timing perfomance, detect issues, and take proative actions - all in real-time. 



## What makes ATSPM Different from Traditional Methods like Synchro?
Unlike traditional traffic signal optimization tools like Synchro, which rely on periodic manual data collection and simulation models, ATSPM uses real-time data directly collected from signal controllers installed at intersections (inside the ITS cabinets). This real-time reporting capability allows agencies to generate performance data for any selected time range, making it ideal for continuously monitoring signal perfromance and diagnosing problems before they escalate.

Traditional signal retiming projects often depend on infrequent manual traffic studies and citizen complaints to detect problems. This reactive approach can delay maintencance, increase congestion and compromise road safety. On the other hand, ATSPMs enable proactive management by continuously collecting data and monitoring traffic signal performance, allowing agencies to solve issues before they lead to major traffic disruptions.

The Python `atspm` project is inspired by UDOT ATSPM, (https://github.com/udotdevelopment/ATSPM) which is a full stack application for collecting data from signal controllers and visualizing it at the intersection level for detailed real-time troubleshooting and analysis. This atspm package focuses instead on aggregation and analytics, enabling more of a system-wide monitoring approach. Both projects are complimentary and can be deployed together.

With over 330,000 traffic signals operating in the US, agencies typically retime these signals every three to five years at a cost of around $4,500 per intersetion. ATSPMs provide a significant improvement over this traditional model by offering continuous performance monitoring, reducing the need for costly manual interventions. (https://ops.fhwa.dot.gov/publications/fhwahop20002/ch2.htm)

This project focuses only on transforming event logs into performance measures and troubleshooting data, it does include data visualization. Feel free to submit feature requests or bug reports or to reach out with questions or comments. Contributions are welcome! 

## Table of Contents

- [What Makes ATSPM Different from Traditional Methods like Synchro?](#what-makes-atspm-different-from-traditional-methods-like-synchro)
- [Features](#features)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Usage Example](#usage-example)
  - [1. Installing the Package](#1-installing-the-package)
  - [2. Setting Parameters](#2-setting-parameters)
  - [3. Performance Measures](#3-performance-measures)
  - [4. Running the Processor](#4-running-the-processor)
  - [5. Retrieving Results as a DataFrame](#5-retrieving-results-as-a-dataframe)
  - [6. Advanced Usage - Detector Health and Pedestrian Volumes](#6-advanced-usage---detector-health-and-pedestrian-volumes)
  - [7. Visualization Options](#7-visualization-options)
- [Performance Measures](#performance-measures)
- [Release Notes](#release-notes)
  - [Version 1.9.0 (February 19, 2025)](#version-190-february-19-2025)
  - [Version 1.8.4 (September 12, 2024)](#version-184-september-12-2024)
  - [Version 1.8.3 (September 5, 2024)](#version-183-september-5-2024)
  - [Version 1.8.2 (August 29, 2024)](#version-182-august-29-2024)
  - [Version 1.8.0 (August 28, 2024)](#version-180-august-28-2024)
  - [Version 1.7.0 (August 22, 2024)](#version-170-august-22-2024)
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
pip install atspm==1.x.x 
```
`atspm` works on Python 3.10-3.12 and is tested on Ubuntu, Windows, and MacOS.

## Quick Start

The best place to start is with these self-contained example uses in Colab!<br>
[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/drive/14SPXPjpwbBEPpjKBN5s4LoqtHWSllvip?usp=sharing)

## Usage Example

### 1. Installing the Package
In this section, we will walk through an example of using the `atspm` package to get started. This can easily be done using pip as shown above

### 2. Setting Parameters

The first step in running the tool is to define the parameters that will dictate how the data is processed. The parameters include global settings for input data, output formats, and options to select specific performance measures.

- **Raw Data**: In the provided example, the raw event log data is provided through `sample_data.data`. In a real-world scenario, this would be a DataFrame or file path (CSV/Parquet/JSON) containing traffic event logs.
- **Detector Configuration**: The `detector_config` defines how the detectors at the intersections are configured (e.g., their location, type).
- **Bin Size**: Data is aggregated in 15-minute intervals (or bins), which is typical for analyzing traffic signals.
- **Output Directory**: The results will be saved in a directory called `test_folder`. This could be customized based on the user's need.
- **Output Format**: The output format is defined as CSV (`output_format: 'csv'`), but the package also supports other formats like Parquet or JSON.

#### Example Parameters:

```python
params = {
    'raw_data': sample_data.data,  # Path to raw event data
    'detector_config': sample_data.config,
    'bin_size': 15,  # 15-minute aggregation bins
    'output_dir': 'test_folder',  # Output directory for results
    'output_format': 'csv',  # Output format (CSV/Parquet/JSON)
    'output_file_prefix': 'prefix_',  # Optional file prefix
    'remove_incomplete': True,  # Remove periods with incomplete data
    'verbose': 1,  # Verbosity level (1: performance logging)
    'aggregations': [  # Performance measures to calculate
        {'name': 'has_data', 'params': {'no_data_min': 5, 'min_data_points': 3}},
        {'name': 'actuations', 'params': {}},
        {'name': 'arrival_on_green', 'params': {'latency_offset_seconds': 0}},
        {'name': 'split_failures', 'params': {'red_time': 5, 'red_occupancy_threshold': 0.80, 'green_occupancy_threshold': 0.80}},
        # ... other performance measures
    ]
}
```
### 3. Performance Measures

The core of `atspm` is calculating various traffic signal performance measures from the raw event log data. Each measure is based on specific traffic signal controller events such as vehicle actuations, pedestrian button presses, or signal changes (green, yellow, red).

#### Some Key Performance Measures Include:

- **Actuations**: This tracks how many times vehicles trigger detectors at the intersection.
- **Arrival on Green**: This measures the percentage of vehicles that arrive at an intersection when the signal is green, which is a key indicator of signal timing efficiency.
- **Split Failures**: This measures the number of cycles where a vehicle was unable to pass through the intersection during the green phase (indicating potential issues with signal timing).
- **Pedestrian Actuations and Volumes**: Measures how often pedestrian buttons are pressed and estimates pedestrian volumes at crossings.

Each of these measures can be configured in the `params` dictionary. You can also add or remove measures based on your analysis needs.

#### Example Configuration for Split Failures:

```python
{
    'name': 'split_failures',
    'params': {
        'red_time': 5,  # Minimum red time for a split failure
        'red_occupancy_threshold': 0.80,  # Threshold for red signal occupancy
        'green_occupancy_threshold': 0.80,  # Threshold for green signal occupancy
        'by_approach': True  # Aggregate split failures by approach
    }
}
```
### 4. Running the Processor

After setting the parameters, the next step is to run the data processor. This involves loading the raw data, performing the aggregations, and saving the results.

```python
processor = SignalDataProcessor(**params)
processor.load()  # Load raw event data
processor.aggregate()  # Perform data aggregation
processor.save()  # Save aggregated results to the output folder
The `aggregate()` function computes the defined performance measures, while `save()` outputs the results to the specified folder.

After running the code, your output folder (e.g., `test_folder/`) will contain the results of the analysis, with the data split into subdirectories based on the performance measures.
```
#### Output Example:

```bash
test_folder/
├── actuations/
├── arrival_on_green/
├── split_failures/
└── ...
```
Inside each folder, there will be a CSV file named `prefix_.csv` with the aggregated performance data. In production, the prefix could be named using the date/time of the run. Or you can output everything to a single folder.

### 5. Retrieving Results as a DataFrame
You can also manually query the results from the internal database and retrieve the data as a Pandas DataFrame for further analysis:
```python
# Query results from the processor and convert to a Pandas DataFrame
results = processor.conn.query("SELECT * FROM actuations ORDER BY TimeStamp").df()
print(results.head())
```

### 6. Advanced Usage - Detector health and Pedestrian Volumes
### Detector Health

Once you've collected a significant amount of data (e.g., 5 weeks), you can run advanced measures like **detector health**, which uses time series decomposition for anomaly detection. This feature allows you to identify malfunctioning detectors and impute missing data.

### Pedestrian Volumes

The package can also estimate pedestrian volumes from push button actuations using the methodology established in traffic studies. This is especially useful for understanding pedestrian activity at intersections.

#### Example for Pedestrian Volumes:

```python
params = {
    'raw_data': 'path/to/ped_data.parquet',
    'bin_size': 15,  # Binned at 15-minute intervals
    'aggregations': [
        {'name': 'full_ped', 'params': {'seconds_between_actuations': 15, 'return_volumes': True}},
    ]
}

processor = SignalDataProcessor(**params)
processor.load()
processor.aggregate()

```
The output will provide an estimated count of pedestrian volumes at various intersections.

### 7. Visualization Options

The data produced by `atspm` can easily be visualized using tools like Power BI, Plotly, or other data visualization platforms. This allows users to create dashboards showing key traffic metrics such as pedestrian volumes, signal timings, and detector health.

#### Example Plot: Pedestrian Volumes Map

You can generate interactive maps of pedestrian volumes using `plotly` to create a visual representation of pedestrian activity:

```python
import plotly.graph_objects as go

fig = go.Figure(data=go.Scattermapbox(
    lon=ped_data['Longitude'],
    lat=ped_data['Latitude'],
    text=ped_data['Name'] + '<br>Pedestrian Volume: ' + ped_data['PedVolumes'].astype(str),
    mode='markers',
    marker=dict(
        size=ped_data['PedVolumes'] / 50,
        color=ped_data['PedVolumes'],
        colorscale='Viridis'
    )
))

fig.update_layout(mapbox=dict(style='outdoors', zoom=5))
fig.show()
```

A good way to use the data is to output as parquet to separate folders, and then a data visualization tool like Power BI can read in all the files in each folder and create a dashboard. For example, see: [Oregon DOT ATSPM Dashboard](https://app.powerbigov.us/view?r=eyJrIjoiNzhmNTUzNDItMzkzNi00YzZhLTkyYWQtYzM1OGExMDk3Zjk1IiwidCI6IjI4YjBkMDEzLTQ2YmMtNGE2NC04ZDg2LTFjOGEzMWNmNTkwZCJ9)

Use of CSV files in production should be avoided, instead use [Parquet](https://parquet.apache.org/) file format, which is significantly faster, smaller, and enforces datatypes.

## Performance Measures

The following performance measures are included:

- Actuations
- Arrival on Green
- Communications (MAXVIEW Specific, otherwise "Has Data" tells when controller generated data)
- Coordination (MAXTIME Specific)
- Detector Health
- Pedestrian Actuations, Services, and Estimated Volumes
- Split Failures
- Splits (MAXTIME Specific)
- Terminations
- Timeline Events
- Yellow and Red Actuations

*Coming Soon:*
- Total Pedestrian Delay
- Pedestrian Detector Health

Detailed documentation for each measure is coming soon.

## Release Notes

### Version 1.9.1 (March 4, 2025)

#### Bug Fixes / Improvements:

Filling in missing time periods for detectors with zero actuations didn't work for incremental processing, this has been fixed by tracking a list of known detectors between each run, similar to the unmatched event tracking. So how it works is you provide a dataframe or file path of known detectors, it will filter out detectors last seen more than n days ago, and then will fill in missing time periods with zeros for the remaining detectors.

```python
known_detectors_df='path/to/known_detectors.csv'
# or supply Pandas DataFrame directly

from src.atspm import SignalDataProcessor, sample_data

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
- Pedestrian Delay (from button bush to walk)
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

## Future Plans

- Integration with [Ibis](https://ibis-project.org/) for compatibility with any SQL backend.
- Implement use of detector distance to stopbar for Arrival on Green calculations.
- Develop comprehensive documentation for each performance measure.

## Contributing

Ideas and contributions are welcome! Please feel free to submit a Pull Request. Note that GitHub Actions will automatically run unit tests on your code.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

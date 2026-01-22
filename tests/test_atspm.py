import pytest
import pandas as pd
import os
import shutil
from src.atspm import SignalDataProcessor
import duckdb
import numpy
import toml
from src.atspm import __version__
from pandas.api.types import is_integer_dtype, is_float_dtype

def test_version_consistency():
  """Test that the version in __init__.py matches the one in pyproject.toml"""
  # Read version from pyproject.toml
  with open('pyproject.toml', 'r') as f:
    pyproject = toml.load(f)
  pyproject_version = pyproject['project']['version']

  # Compare versions
  assert __version__ == pyproject_version, f"Version mismatch: __init__.py has {__version__}, pyproject.toml has {pyproject_version}"

# Define the parameters for testing
TEST_PARAMS = {
  'raw_data': duckdb.query("select * from 'tests/hires_test_data.parquet'").df(),
  'detector_config': duckdb.query("select * from 'tests/configs_test_data.parquet'").df(),
  'bin_size': 15,
  'output_dir': 'tests/test_output',
  'output_to_separate_folders': False,
  'output_format': 'parquet',
  'output_file_prefix': 'test_',
  'remove_incomplete': True,
  'to_sql': False,
  'verbose': 0,
  'aggregations': [
      {'name': 'has_data', 'params': {'no_data_min': 5, 'min_data_points': 3}},
      {'name': 'actuations', 'params': {}},
      {'name': 'arrival_on_green', 'params': {'latency_offset_seconds': 0}},
      {'name': 'communications', 'params': {'event_codes': '400,503,502'}},
      {'name': 'coordination', 'params': {}},
      {'name': 'ped', 'params': {}},
      {'name': 'unique_ped', 'params': {'seconds_between_actuations': 15}},
      {'name': 'full_ped', 'params': {'seconds_between_actuations': 15, 'return_volumes':True}},
      {'name': 'split_failures', 'params': {'red_time': 5, 'red_occupancy_threshold': 0.80, 'green_occupancy_threshold': 0.80, 'by_approach': True, 'by_cycle': False}},
      {'name': 'splits', 'params': {}},
      {'name': 'terminations', 'params': {}},
      {'name': 'yellow_red', 'params': {'latency_offset_seconds': 1.5, 'min_red_offset': -8}},
      {'name': 'timeline', 'params': {'min_duration': 0.2, 'cushion_time':60, 'maxtime': True}}, # events shorter than 0.2 seconds are removed. coord pattern change events assigned duration of 60s (for visualization)
      {'name': 'ped_delay', 'params': {}},
      {'name': 'phase_wait', 'params': {'preempt_recovery_seconds': 120, 'assumed_cycle_length': 140, 'skip_multiplier': 1.5}},
      {'name': 'coordination_agg', 'params': {}},  # Requires has_data and timeline
  ]
}

# Define aggregations that can be run incrementally
# coordination_agg requires fill-forward of state across time buckets, which doesn't work well incrementally
INCREMENTAL_AGGREGATIONS = [agg for agg in TEST_PARAMS['aggregations'] if agg['name'] not in ['unique_ped', 'full_ped', 'yellow_red']]

@pytest.fixture(scope="module")
def processor_output():
  """Fixture to run the SignalDataProcessor once for all tests"""
  processor = SignalDataProcessor(**TEST_PARAMS)
  processor.run()
  yield
  # Cleanup after all tests are done
  shutil.rmtree(TEST_PARAMS['output_dir'])

def compare_dataframes(df1, df2):
  """Compare two dataframes, ignoring row order"""
  # Align numeric dtypes so nullable ints and floats compare cleanly
  df1_aligned, df2_aligned = df1.copy(), df2.copy()
  for col in set(df1_aligned.columns).intersection(df2_aligned.columns):
    if is_integer_dtype(df1_aligned[col]) and is_float_dtype(df2_aligned[col]):
      df1_aligned[col] = df1_aligned[col].astype('float64')
    elif is_float_dtype(df1_aligned[col]) and is_integer_dtype(df2_aligned[col]):
      df2_aligned[col] = df2_aligned[col].astype('float64')

  df1_sorted = df1_aligned.sort_values(by=list(df1_aligned.columns)).reset_index(drop=True)
  df2_sorted = df2_aligned.sort_values(by=list(df2_aligned.columns)).reset_index(drop=True)
  pd.testing.assert_frame_equal(df1_sorted, df2_sorted)

def round_specific_columns(df, columns_to_round, tenths=2):
  """Round specific columns in a dataframe to the nearest multiple of tenths."""
  for col in columns_to_round:
      if col in df.columns:
          df[col] = (df[col] / (0.1 * tenths)).round() * (0.1 * tenths)
  return df

def compare_dataframes_with_tolerance(df1, df2, tolerance):
  """
  Compare two dataframes, ignoring row order, applying rounding to specific columns,
  and allowing for a percentage of different datapoints.
  """
  # Columns to round (adjust as needed)
  columns_to_round = ['Green_Occupancy', 'Red_Occupancy']

  # Apply rounding
  df1 = round_specific_columns(df1, columns_to_round)
  df2 = round_specific_columns(df2, columns_to_round)

  # Sort dataframes
  df1_sorted = df1.sort_values(by=list(df1.columns)).reset_index(drop=True)
  df2_sorted = df2.sort_values(by=list(df2.columns)).reset_index(drop=True)

  # Compare dataframes
  comparison = df1_sorted.compare(df2_sorted)
  
  # Calculate the percentage of different datapoints
  total_datapoints = df1.size
  different_datapoints = comparison.size
  difference_percentage = different_datapoints / total_datapoints

  # Check if the difference is within the tolerance
  assert difference_percentage <= tolerance, f"Dataframes differ by {difference_percentage:.2%}, which is more than the allowed {tolerance:.2%}"



@pytest.mark.parametrize("aggregation", TEST_PARAMS['aggregations'], ids=lambda x: x['name'])
def test_aggregation(processor_output, aggregation):
  """Test each aggregation individually"""
  agg_name = aggregation['name']
  output_file = os.path.join(TEST_PARAMS['output_dir'], f"{TEST_PARAMS['output_file_prefix']}{agg_name}.parquet")
  precalc_file = f"tests/precalculated/{agg_name}.parquet"

  assert os.path.exists(output_file), f"Output file for {agg_name} not found"
  assert os.path.exists(precalc_file), f"Precalculated file for {agg_name} not found"

  output_df = pd.read_parquet(output_file)
  precalc_df = pd.read_parquet(precalc_file)

  compare_dataframes(output_df, precalc_df)

def test_all_files_generated():
  """Test that all expected files are generated"""
  expected_files = [f"{TEST_PARAMS['output_file_prefix']}{agg['name']}.parquet" for agg in TEST_PARAMS['aggregations']]
  for file in expected_files:
      assert os.path.exists(os.path.join(TEST_PARAMS['output_dir'], file)), f"File {file} not generated"

def test_context_manager():
  """Test that using the context manager produces correct results"""
  # Use has_data aggregation as a simple test case
  with SignalDataProcessor(
    raw_data=TEST_PARAMS['raw_data'],
    detector_config=TEST_PARAMS['detector_config'],
    bin_size=TEST_PARAMS['bin_size'],
    verbose=0,
    aggregations=[
      {'name': 'has_data', 'params': {'no_data_min': 5, 'min_data_points': 3}},
    ]
  ) as processor:
    processor.load()
    processor.aggregate()
    # Get results while still in context (before connection closes)
    result_df = processor.conn.sql("SELECT * FROM has_data").df()
  
  # After exiting context, connection should be closed
  assert processor._closed, "Connection should be closed after exiting context manager"
  
  # Compare with precalculated data
  precalc_file = "tests/precalculated/has_data.parquet"
  assert os.path.exists(precalc_file), f"Precalculated file {precalc_file} not found"
  precalc_df = pd.read_parquet(precalc_file)
  
  compare_dataframes(result_df, precalc_df)

@pytest.fixture(scope="module")
def incremental_processor_output():
  """Fixture to run the SignalDataProcessor incrementally"""
  data = duckdb.query("select * from 'tests/hires_test_data.parquet'").df()

  chunks = {
      '1_chunk': duckdb.sql("select * from data where timestamp >= '2024-05-13 15:00:00' and timestamp < '2024-05-13 15:15:00'").df(),
      '2_chunk': duckdb.sql("select * from data where timestamp >= '2024-05-13 15:15:00' and timestamp < '2024-05-13 15:30:00'").df(),
      '3_chunk': duckdb.sql("select * from data where timestamp >= '2024-05-13 15:30:00' and timestamp < '2024-05-13 15:45:00'").df(),
      '4_chunk': duckdb.sql("select * from data where timestamp >= '2024-05-13 15:45:00' and timestamp < '2024-05-13 16:00:00'").df(),
      '5_chunk': duckdb.sql("select * from data where timestamp >= '2024-05-13 16:00:00' and timestamp < '2024-05-13 16:15:00'").df(),
      '6_chunk': duckdb.sql("select * from data where timestamp >= '2024-05-13 16:15:00' and timestamp < '2024-05-13 16:30:00'").df(),
      '7_chunk': duckdb.sql("select * from data where timestamp >= '2024-05-13 16:30:00' and timestamp < '2024-05-13 16:45:00'").df(),
      '8_chunk': duckdb.sql("select * from data where timestamp >= '2024-05-13 16:45:00' and timestamp < '2024-05-13 17:00:00'").df(),
      '9_chunk': duckdb.sql("select * from data where timestamp >= '2024-05-13 17:00:00' and timestamp < '2024-05-13 17:15:00'").df(),
      '10_chunk': duckdb.sql("select * from data where timestamp >= '2024-05-13 17:15:00' and timestamp < '2024-05-13 17:30:00'").df(),
      '11_chunk': duckdb.sql("select * from data where timestamp >= '2024-05-13 17:30:00' and timestamp < '2024-05-13 17:45:00'").df(),
      '12_chunk': duckdb.sql("select * from data where timestamp >= '2024-05-13 17:45:00' and timestamp < '2024-05-13 18:00:00'").df(),
  }

  output_dir = 'tests/test_incremental_output'
  os.makedirs(output_dir, exist_ok=True)

  for i, chunk in chunks.items():
      #if i != '1_chunk':
      #   continue
      params = TEST_PARAMS.copy()
      params.update({
          'raw_data': chunk,
          'output_dir': f'{output_dir}',
          'output_file_prefix': f"{i}_",
          'unmatched_event_settings': {
            'df_or_path': f"{output_dir}/unmatched.parquet", # for timeline, arrival_on_green & yellow_red
            'split_fail_df_or_path': f"{output_dir}/sf_unmatched.parquet", # just for split_failures
            'max_days_old': 14}, # remove unmatched events older than 14 days
          'aggregations': INCREMENTAL_AGGREGATIONS  # Use only incremental aggregations
      })
      processor = SignalDataProcessor(**params)
      processor.run()

  yield output_dir
  # Cleanup after all tests are done
  shutil.rmtree(output_dir)

@pytest.mark.parametrize("aggregation", INCREMENTAL_AGGREGATIONS, ids=lambda x: x['name'])
def test_incremental_aggregation(incremental_processor_output, aggregation):
  """Test each aggregation for incremental runs"""
  agg_name = aggregation['name']
  output_files = [os.path.join(incremental_processor_output, f"{i}_chunk_{agg_name}.parquet") for i in range(1, 13)]
  precalc_file = f"tests/precalculated/{agg_name}.parquet"

  for file in output_files:
    assert os.path.exists(file), f"Incremental output file {file} not found"
  assert os.path.exists(precalc_file), f"Precalculated file for {agg_name} not found"

  incremental_dfs = [pd.read_parquet(file) for file in output_files]
  combined_df = pd.concat(incremental_dfs).drop_duplicates().reset_index(drop=True)
  
  precalc_df = pd.read_parquet(precalc_file)

  # due to how split_failures imputes missing actuations there are some differences in incremental runs
  if agg_name == 'split_failures':
    compare_dataframes_with_tolerance(combined_df, precalc_df, tolerance=0.04)
  else:
    compare_dataframes(combined_df, precalc_df)

# REPLICATING ODOT'S PRODUCTION ENVIRONMENT
@pytest.fixture(scope="module")
def incremental_processor_output_with_dataframes():
  """Fixture to run the SignalDataProcessor incrementally using dataframes"""
  data = duckdb.query("select * from 'tests/hires_test_data.parquet'").df()
  configs = duckdb.query("select * from 'tests/configs_test_data.parquet'").df()
  unmatched_df = ''
  sf_unmatched_df = ''
  known_detectors_df = ''

  chunks = {
      '1_chunk': duckdb.sql("select * from data where timestamp >= '2024-05-13 15:00:00' and timestamp < '2024-05-13 15:15:00'").df(),
      '2_chunk': duckdb.sql("select * from data where timestamp >= '2024-05-13 15:15:00' and timestamp < '2024-05-13 15:30:00'").df(),
      '3_chunk': duckdb.sql("select * from data where timestamp >= '2024-05-13 15:30:00' and timestamp < '2024-05-13 15:45:00'").df(),
      '4_chunk': duckdb.sql("select * from data where timestamp >= '2024-05-13 15:45:00' and timestamp < '2024-05-13 16:00:00'").df(),
      '5_chunk': duckdb.sql("select * from data where timestamp >= '2024-05-13 16:00:00' and timestamp < '2024-05-13 16:15:00'").df(),
      '6_chunk': duckdb.sql("select * from data where timestamp >= '2024-05-13 16:15:00' and timestamp < '2024-05-13 16:30:00'").df(),
      '7_chunk': duckdb.sql("select * from data where timestamp >= '2024-05-13 16:30:00' and timestamp < '2024-05-13 16:45:00'").df(),
      '8_chunk': duckdb.sql("select * from data where timestamp >= '2024-05-13 16:45:00' and timestamp < '2024-05-13 17:00:00'").df(),
      '9_chunk': duckdb.sql("select * from data where timestamp >= '2024-05-13 17:00:00' and timestamp < '2024-05-13 17:15:00'").df(),
      '10_chunk': duckdb.sql("select * from data where timestamp >= '2024-05-13 17:15:00' and timestamp < '2024-05-13 17:30:00'").df(),
      '11_chunk': duckdb.sql("select * from data where timestamp >= '2024-05-13 17:30:00' and timestamp < '2024-05-13 17:45:00'").df(),
      '12_chunk': duckdb.sql("select * from data where timestamp >= '2024-05-13 17:45:00' and timestamp < '2024-05-13 18:00:00'").df(),
  }

  results = {}

  for i, chunk in chunks.items():
    params = TEST_PARAMS.copy()
    params.update({
        'raw_data': chunk,
        'detector_config': configs,
        'unmatched_event_settings': {
            'df_or_path': unmatched_df,
            'split_fail_df_or_path': sf_unmatched_df,
            'max_days_old': 14
        },
        'verbose': 0,
        'aggregations': INCREMENTAL_AGGREGATIONS
    })
    
    # Modify the actuations aggregation to include fill_in_missing parameter
    for agg in params['aggregations']:
      if agg['name'] == 'actuations':
        agg['params'] = {
            'fill_in_missing': True,
            'known_detectors_df_or_path': known_detectors_df,
            'known_detectors_max_days_old': 2
        }
        
    processor = SignalDataProcessor(**params)
    processor.load()
    processor.aggregate()

    # Store results for each aggregation
    for agg in INCREMENTAL_AGGREGATIONS:
        agg_name = agg['name']
        if agg_name not in results:
            results[agg_name] = []
        results[agg_name].append(processor.conn.sql(f"select * from {agg_name}").df())

    # Update unmatched dataframes for next iteration
    unmatched_df = processor.conn.sql("select * from unmatched_events").df()
    sf_unmatched_df = processor.conn.sql("select * from sf_unmatched").df()
    
    # Update known detectors dataframe for next iteration
    known_detectors_df = processor.conn.sql("select * from known_detectors").df()

    # Write unmatched dataframes to disk as csv
    unmatched_df.to_csv('unmatched_df_temp.csv')
    sf_unmatched_df.to_csv('sf_unmatched_df_temp.csv')
    known_detectors_df.to_csv('known_detectors_df_temp.csv')

    # Read unmatched dataframes back from disk
    unmatched_df = pd.read_csv('unmatched_df_temp.csv')
    sf_unmatched_df = pd.read_csv('sf_unmatched_df_temp.csv')
    known_detectors_df = pd.read_csv('known_detectors_df_temp.csv')

  return results

@pytest.fixture(scope="module")
def oneshot_processor_output_with_fill_in_missing():
  """Fixture to run the SignalDataProcessor in oneshot mode with fill_in_missing for actuations"""
  data = duckdb.query("select * from 'tests/hires_test_data.parquet'").df()
  
  # Create a copy of the test parameters
  params = TEST_PARAMS.copy()
  
  # Update with our specific test needs
  params.update({
      'raw_data': data,
      'verbose': 0,
      'remove_incomplete': False,
  })
  
  # Find the actuations aggregation
  actuations_agg = None
  for agg in params['aggregations']:
    if agg['name'] == 'actuations':
      actuations_agg = agg.copy()  # Make a copy to avoid modifying the original
      break
  
  # Set the fill_in_missing parameter
  actuations_agg['params'] = {'fill_in_missing': True}
  
  # Create a new list with just the actuations aggregation
  params['aggregations'] = [actuations_agg]
  
  # Run the processor
  processor = SignalDataProcessor(**params)
  processor.load()
  processor.aggregate()
  
  # Get the results
  result = processor.conn.sql("select * from actuations").df()
  
  # Clean up
  processor.conn.close()
  
  return result

@pytest.mark.parametrize("aggregation", INCREMENTAL_AGGREGATIONS, ids=lambda x: x['name'])
def test_incremental_aggregation_with_dataframes(incremental_processor_output_with_dataframes, aggregation):
  """Test each aggregation for incremental runs using dataframes"""
  agg_name = aggregation['name']
  incremental_results = incremental_processor_output_with_dataframes[agg_name]
  
  # Use actuations_zeros.parquet for actuations comparison
  if agg_name == 'actuations':
    precalc_file = f"tests/precalculated/actuations_zeros_incremental.parquet"
  else:
    precalc_file = f"tests/precalculated/{agg_name}.parquet"

  assert len(incremental_results) == 12, f"Expected 12 chunks of results for {agg_name}"
  assert os.path.exists(precalc_file), f"Precalculated file for {agg_name} not found"

  combined_df = pd.concat(incremental_results).drop_duplicates().reset_index(drop=True)
  precalc_df = pd.read_parquet(precalc_file)

  # due to how split_failures imputes missing actuations there are some differences in incremental runs
  if agg_name == 'split_failures':
    compare_dataframes_with_tolerance(combined_df, precalc_df, tolerance=0.04)
  else:
    compare_dataframes(combined_df, precalc_df)

def test_oneshot_actuations_with_fill_in_missing(oneshot_processor_output_with_fill_in_missing):
  """Test actuations with fill_in_missing in oneshot mode"""
  # Get the results from the fixture
  actuations_df = oneshot_processor_output_with_fill_in_missing
  
  # Load the precalculated file
  precalc_file = "tests/precalculated/actuations_zeros_oneshot.parquet"
  assert os.path.exists(precalc_file), f"Precalculated file {precalc_file} not found"
  
  precalc_df = pd.read_parquet(precalc_file)
  
  # Compare the dataframes
  compare_dataframes(actuations_df, precalc_df)

def test_ped_delay_output(processor_output):
  """Test pedestrian delay aggregation output"""
  output_file = os.path.join(TEST_PARAMS['output_dir'], f"{TEST_PARAMS['output_file_prefix']}ped_delay.parquet")
  precalc_file = "tests/precalculated/ped_delay.parquet"

  assert os.path.exists(output_file), "Ped delay output file not found"
  assert os.path.exists(precalc_file), "Precalculated ped delay file not found"

  output_df = pd.read_parquet(output_file)
  precalc_df = pd.read_parquet(precalc_file)

  compare_dataframes(output_df, precalc_df)

@pytest.fixture(scope="module")
def detector_health_output():
  """Fixture to run detector_health aggregation"""
  data = duckdb.query("select * from 'tests/hires_test_data.parquet'").df()
  configs = duckdb.query("select * from 'tests/configs_test_data.parquet'").df()
  
  # First, run actuations aggregation with fill_in_missing=True
  actuations_params = {
    'raw_data': data,
    'detector_config': configs,
    'bin_size': 15,
    'verbose': 0,
    'aggregations': [
      {'name': 'actuations', 'params': {'fill_in_missing': True}},
    ]
  }
  
  processor = SignalDataProcessor(**actuations_params)
  processor.load()
  processor.aggregate()
  
  # Get the actuations data
  actuations = processor.conn.query("SELECT * FROM actuations ORDER BY TimeStamp").df()
  processor.close()
  
  # Set common parameters (matching the notebook)
  common_params = {
    'datetime_column': 'TimeStamp',
    'value_column': 'Total',
    'entity_grouping_columns': ['DeviceId', 'Detector']
  }
  
  # Set median decomposition parameters
  decompose_params = {
    **common_params,
    'freq_minutes': 15,
    'min_time_of_day_samples': 0,  # 0 just for testing, in prod set to 14
    'rolling_window_enable': False
  }
  
  # Set anomaly detection parameters
  anomaly_params = {
    **common_params,
    'entity_threshold': 6.0,
    'group_threshold': 3.0,
    'GEH': True,
    'log_adjust_negative': True
  }
  
  # Combine all parameters
  detector_health_params = {
    'aggregations': [
      {
        'name': 'detector_health',
        'params': {
          'data': actuations,
          'device_groups': None,
          'return_last_n_days': 1,
          'decompose_params': decompose_params,
          'anomaly_params': anomaly_params
        }
      },
    ]
  }
  
  # Run detector_health aggregation
  processor = SignalDataProcessor(**detector_health_params)
  processor.aggregate()
  
  # Get the results
  result = processor.conn.query("SELECT * FROM detector_health ORDER BY TimeStamp").df()
  
  # Clean up
  processor.close()
  
  return result

def test_detector_health(detector_health_output):
  """Test detector_health aggregation"""
  # Get the results from the fixture
  detector_health_df = detector_health_output
  
  # Load the precalculated file
  precalc_file = "tests/precalculated/detector_health.parquet"
  assert os.path.exists(precalc_file), f"Precalculated file {precalc_file} not found"
  
  precalc_df = pd.read_parquet(precalc_file)
  
  # Compare the dataframes
  compare_dataframes(detector_health_df, precalc_df)


# =============================================================================
# PHASE WAIT LOGIC TESTS
# =============================================================================

@pytest.fixture(scope="module")
def phase_wait_synthetic_output():
  """
  Fixture to test Phase Wait logic with synthetic data covering all scenarios:
  1. Simple 43→1: Phase call followed by green
  2. 43→44: Dropped call (should NOT create Phase Wait)
  3. 43→44→43→1: Dropped call followed by new call that succeeds
  4. Persistent call: 43→1→7→1 (creates 2 Phase Waits)
  5. Persistent call: 43→1→7→1→7→1 (creates 3 Phase Waits)
  6. 43→1→7→44→43→1: Call drops then new call (2 Phase Waits)
  7. Multiple drops: 43→44→43→44→43→1 (only last 43 counts)
  8. Multi-device: Same logic on different device
  9. Chunk boundary: Tests state tracking across chunks
  10. Green without call: Should NOT create Phase Wait
  """
  # Import the synthetic test data
  from tests.phase_wait_test_data import raw_df, expected_df

  # Run the timeline aggregation
  params = {
    'raw_data': raw_df,
    'bin_size': 15,
    'verbose': 0,
    'aggregations': [
      {'name': 'timeline', 'params': {'min_duration': 0.0, 'cushion_time': 60, 'maxtime': True}},
    ]
  }

  processor = SignalDataProcessor(**params)
  processor.load()
  processor.aggregate()

  # Get the timeline results
  result = processor.conn.query("SELECT * FROM timeline WHERE EventClass = 'Phase Wait' AND IsValid = TRUE").df()
  processor.close()

  return result, expected_df


def test_phase_wait_synthetic(phase_wait_synthetic_output):
  """Test Phase Wait logic with synthetic data covering all scenarios"""
  actual_df, expected_df = phase_wait_synthetic_output

  # Prepare dataframes for comparison
  actual_sorted = actual_df[['DeviceId', 'StartTime', 'EndTime', 'Duration', 'EventValue']].sort_values(
      ['DeviceId', 'StartTime']).reset_index(drop=True)
  expected_sorted = expected_df[['DeviceId', 'StartTime', 'EndTime', 'Duration', 'EventValue']].sort_values(
      ['DeviceId', 'StartTime']).reset_index(drop=True)

  # Align dtypes
  actual_sorted['Duration'] = actual_sorted['Duration'].astype('float32')
  expected_sorted['Duration'] = expected_sorted['Duration'].astype('float32')
  actual_sorted['EventValue'] = actual_sorted['EventValue'].astype('int16')
  expected_sorted['EventValue'] = expected_sorted['EventValue'].astype('int16')

  # Compare
  pd.testing.assert_frame_equal(actual_sorted, expected_sorted, check_dtype=False)


@pytest.fixture(scope="module")
def phase_wait_incremental_output():
  """
  Fixture to test Phase Wait logic with incremental processing.
  Splits synthetic data into chunks to test state tracking (902, 903, 904 events).
  """
  from tests.phase_wait_test_data import raw_df, expected_df
  from datetime import datetime, timedelta

  # Define chunk boundaries based on timestamps in synthetic data
  # The data runs from 08:00:00 to ~08:20:10
  # We'll split into 3 chunks to test state tracking
  base_time = datetime(2024, 5, 14, 8, 0, 0)

  chunks = {
    '1_chunk': raw_df[raw_df['TimeStamp'] < base_time + timedelta(minutes=8)].copy(),   # 08:00:00 - 08:07:59
    '2_chunk': raw_df[(raw_df['TimeStamp'] >= base_time + timedelta(minutes=8)) &
                       (raw_df['TimeStamp'] < base_time + timedelta(minutes=15))].copy(),  # 08:08:00 - 08:14:59
    '3_chunk': raw_df[raw_df['TimeStamp'] >= base_time + timedelta(minutes=15)].copy(),  # 08:15:00+
  }

  output_dir = 'tests/test_phase_wait_incremental'
  os.makedirs(output_dir, exist_ok=True)

  all_results = []

  for i, (chunk_name, chunk_data) in enumerate(chunks.items()):
    params = {
      'raw_data': chunk_data,
      'bin_size': 15,
      'verbose': 0,
      'output_dir': output_dir,
      'output_file_prefix': f"{chunk_name}_",
      'output_format': 'parquet',
      'output_to_separate_folders': False,
      'unmatched_event_settings': {
        'df_or_path': f"{output_dir}/unmatched.parquet",
        'max_days_old': 14
      },
      'aggregations': [
        {'name': 'timeline', 'params': {'min_duration': 0.0, 'cushion_time': 60, 'maxtime': True}},
      ]
    }
    
    processor = SignalDataProcessor(**params)
    processor.run()

  # Combine all chunk results
  for chunk_name in chunks.keys():
    output_file = f"{output_dir}/{chunk_name}_timeline.parquet"
    if os.path.exists(output_file):
      df = pd.read_parquet(output_file)
      all_results.append(df)

  combined_df = pd.concat(all_results).drop_duplicates().reset_index(drop=True)

  # Filter to valid Phase Wait events
  phase_wait_df = combined_df[(combined_df['EventClass'] == 'Phase Wait') & (combined_df['IsValid'] == True)]

  yield phase_wait_df, expected_df

  # Cleanup
  shutil.rmtree(output_dir)


def test_phase_wait_incremental(phase_wait_incremental_output):
  """Test Phase Wait logic with incremental processing"""
  actual_df, expected_df = phase_wait_incremental_output

  # Prepare dataframes for comparison
  actual_sorted = actual_df[['DeviceId', 'StartTime', 'EndTime', 'Duration', 'EventValue']].sort_values(
      ['DeviceId', 'StartTime']).reset_index(drop=True)
  expected_sorted = expected_df[['DeviceId', 'StartTime', 'EndTime', 'Duration', 'EventValue']].sort_values(
      ['DeviceId', 'StartTime']).reset_index(drop=True)

  # Align dtypes
  actual_sorted['Duration'] = actual_sorted['Duration'].astype('float32')
  expected_sorted['Duration'] = expected_sorted['Duration'].astype('float32')
  actual_sorted['EventValue'] = actual_sorted['EventValue'].astype('int16')
  expected_sorted['EventValue'] = expected_sorted['EventValue'].astype('int16')

  # Compare
  pd.testing.assert_frame_equal(actual_sorted, expected_sorted, check_dtype=False)


def test_invalid_phase_call_creates_invalid_phase_wait():
  """
  BUG FIX TEST: Invalid Phase Call should create Invalid Phase Wait events.
  
  This test verifies the fix for a bug where Phase Wait events derived from
  invalid Phase Calls were incorrectly marked as IsValid=True.
  
  A Phase Call (event 43) is "invalid" when it is not properly terminated
  with a Phase Drop (event 44) before the next Phase Call (event 43).
  
  Scenario tested (Phase 10 from synthetic data):
  - First 43 at 1300s: No 44 before next 43 at 1400s → invalid Phase Call
  - This creates a Phase Wait event that should have IsValid=False
  - Second 43 at 1400s: Properly terminated with 44 → valid Phase Call
  - This creates a Phase Wait event that should have IsValid=True
  """
  from tests.phase_wait_test_data import raw_df, expected_invalid_df

  # Run the timeline aggregation
  params = {
    'raw_data': raw_df,
    'bin_size': 15,
    'verbose': 0,
    'aggregations': [
      {'name': 'timeline', 'params': {'min_duration': 0.0, 'cushion_time': 60, 'maxtime': True}},
    ]
  }

  processor = SignalDataProcessor(**params)
  processor.load()
  processor.aggregate()

  # Get ALL Phase Wait events for Phase 10 (the bug fix scenario)
  result = processor.conn.query("""
    SELECT * FROM timeline 
    WHERE EventClass = 'Phase Wait' 
      AND EventValue = 10
    ORDER BY StartTime
  """).df()
  processor.close()

  # Verify we have 2 Phase Wait events for Phase 10 (1 invalid + 1 valid)
  assert len(result) == 2, f"Expected 2 Phase Wait events for Phase 10, got {len(result)}"

  # Check that we have exactly 1 invalid and 1 valid Phase Wait
  invalid_waits = result[result['IsValid'] == False]
  valid_waits = result[result['IsValid'] == True]

  assert len(invalid_waits) == 1, f"Expected 1 invalid Phase Wait event, got {len(invalid_waits)}"
  assert len(valid_waits) == 1, f"Expected 1 valid Phase Wait event, got {len(valid_waits)}"

  # Verify the invalid Phase Wait event matches expected data
  actual_invalid = invalid_waits[['DeviceId', 'StartTime', 'EndTime', 'Duration', 'EventValue']].sort_values(
      ['StartTime']).reset_index(drop=True)
  expected_invalid = expected_invalid_df[['DeviceId', 'StartTime', 'EndTime', 'Duration', 'EventValue']].sort_values(
      ['StartTime']).reset_index(drop=True)

  # Align dtypes
  actual_invalid['Duration'] = actual_invalid['Duration'].astype('float32')
  expected_invalid['Duration'] = expected_invalid['Duration'].astype('float32')
  actual_invalid['EventValue'] = actual_invalid['EventValue'].astype('int16')
  expected_invalid['EventValue'] = expected_invalid['EventValue'].astype('int16')

  pd.testing.assert_frame_equal(actual_invalid, expected_invalid, check_dtype=False)


def test_empty_dataframe_input():
  """Test that the SignalDataProcessor handles empty input gracefully.

  When an empty dataframe is provided, all aggregations should complete
  without errors and produce empty output tables.
  """
  # Create an empty dataframe with the expected columns
  empty_df = pd.DataFrame(columns=['TimeStamp', 'DeviceId', 'EventId', 'Parameter'])

  # Get detector config from the test data
  detector_config = duckdb.query("select * from 'tests/configs_test_data.parquet'").df()

  # Use all the same aggregations as TEST_PARAMS
  all_aggregations = [
    {'name': 'has_data', 'params': {'no_data_min': 5, 'min_data_points': 3}},
    {'name': 'actuations', 'params': {}},
    {'name': 'arrival_on_green', 'params': {'latency_offset_seconds': 0}},
    {'name': 'communications', 'params': {'event_codes': '400,503,502'}},
    {'name': 'coordination', 'params': {}},
    {'name': 'ped', 'params': {}},
    {'name': 'unique_ped', 'params': {'seconds_between_actuations': 15}},
    {'name': 'full_ped', 'params': {'seconds_between_actuations': 15, 'return_volumes': True}},
    {'name': 'split_failures', 'params': {'red_time': 5, 'red_occupancy_threshold': 0.80, 'green_occupancy_threshold': 0.80, 'by_approach': True, 'by_cycle': False}},
    {'name': 'splits', 'params': {}},
    {'name': 'terminations', 'params': {}},
    {'name': 'yellow_red', 'params': {'latency_offset_seconds': 1.5, 'min_red_offset': -8}},
    {'name': 'timeline', 'params': {'min_duration': 0.2, 'cushion_time': 60, 'maxtime': True}},
    {'name': 'ped_delay', 'params': {}},
    {'name': 'phase_wait', 'params': {'preempt_recovery_seconds': 120, 'assumed_cycle_length': 140, 'skip_multiplier': 1.5}},
    {'name': 'coordination_agg', 'params': {}},
  ]

  # Try running the processor with an empty dataframe
  processor = SignalDataProcessor(
    raw_data=empty_df,
    detector_config=detector_config,
    bin_size=15,
    verbose=0,
    remove_incomplete=True,
    aggregations=all_aggregations
  )

  # Load and aggregate should complete without errors
  processor.load()
  processor.aggregate()

  # Verify each aggregation output table exists and is empty
  for agg in all_aggregations:
    agg_name = agg['name']
    result_df = processor.conn.sql(f"SELECT * FROM {agg_name}").df()
    assert len(result_df) == 0, f"{agg_name} should be empty when raw_data is empty"

  # Clean up
  processor.close()


# =============================================================================
# PHASE WAIT BUG FIX TESTS
# =============================================================================

def test_phase_wait_preempt_overlap():
    """
    Test that Phase Wait events overlapping with preempt windows are filtered out.
    
    Bug: The current logic only checks if Phase Wait STARTS during preempt window.
    This misses cases where:
    - Phase Wait starts BEFORE preempt but overlaps with it
    - Phase Wait completely contains the preempt event
    
    Proper overlap detection: ranges overlap if Start1 < End2 AND Start2 < End1
    """
    from datetime import datetime, timedelta
    
    base = datetime(2024, 6, 1, 8, 0, 0)
    
    # Synthetic events:
    # - Phase call at 0s, green at 240s (4 min wait) for phase 1
    # - Preempt starts at 60s, ends at 100s (preempt window: 60s to 220s with 120s recovery)
    # - The Phase Wait (0s -> 240s) CONTAINS the preempt window but starts BEFORE it
    # - This should be filtered out but current code doesn't catch it
    raw_events = [
        # Phase 1: Long wait containing preempt
        (base, 1001, 43, 1),                                    # Phase call at 0s
        (base + timedelta(seconds=240), 1001, 1, 1),            # Green at 240s  
        (base + timedelta(seconds=250), 1001, 7, 1),            # Green end at 250s
        (base + timedelta(seconds=250.2), 1001, 44, 1),         # Call drop
        
        # Preempt: 60s to 100s (inside the phase wait period)
        (base + timedelta(seconds=60), 1001, 102, 1),           # Preempt start
        (base + timedelta(seconds=100), 1001, 104, 1),          # Preempt end
        
        # Phase 2: Normal wait NOT overlapping preempt (for control)
        (base + timedelta(seconds=300), 1001, 43, 2),           # Phase call at 300s
        (base + timedelta(seconds=330), 1001, 1, 2),            # Green at 330s (30s wait)
        (base + timedelta(seconds=340), 1001, 7, 2),            # Green end
        (base + timedelta(seconds=340.2), 1001, 44, 2),         # Call drop
    ]
    
    raw_df = pd.DataFrame(raw_events, columns=['TimeStamp', 'DeviceId', 'EventId', 'Parameter'])
    raw_df['DeviceId'] = raw_df['DeviceId'].astype('int64')
    raw_df['EventId'] = raw_df['EventId'].astype('int16')
    raw_df['Parameter'] = raw_df['Parameter'].astype('int16')
    
    params = {
        'raw_data': raw_df,
        'bin_size': 15,
        'verbose': 0,
        'aggregations': [
            {'name': 'has_data', 'params': {'no_data_min': 15, 'min_data_points': 1}},
            {'name': 'timeline', 'params': {'min_duration': 0.0, 'cushion_time': 60, 'maxtime': True}},
            {'name': 'phase_wait', 'params': {'preempt_recovery_seconds': 120, 'assumed_cycle_length': 180, 'skip_multiplier': 1.5}},
            {'name': 'coordination_agg', 'params': {}}
        ]
    }
    
    processor = SignalDataProcessor(**params)
    processor.load()
    processor.aggregate()
    
    # Get phase_wait aggregation results
    phase_wait_agg = processor.conn.query("SELECT * FROM phase_wait ORDER BY TimeStamp, Phase").df()
    
    # Phase 1 wait should be EXCLUDED (overlaps with preempt)
    # Phase 2 wait should be INCLUDED (no overlap)
    phase1_waits = phase_wait_agg[phase_wait_agg['Phase'] == 1]
    phase2_waits = phase_wait_agg[phase_wait_agg['Phase'] == 2]
    
    processor.close()
    
    # Phase 1 should have 0 records (filtered due to preempt overlap)
    assert len(phase1_waits) == 0, (
        f"Phase 1 wait should be filtered (overlaps with preempt), but got {len(phase1_waits)} records.\n"
        f"The Phase Wait (0s->240s) contains the preempt (60s->100s) but wasn't filtered.\n"
        f"Phase 1 data: {phase1_waits.to_string()}"
    )
    
    # Phase 2 should have 1 record (normal wait, no preempt overlap)
    assert len(phase2_waits) == 1, (
        f"Phase 2 wait should be included (no preempt overlap), got {len(phase2_waits)} records"
    )


def test_phase_wait_invalid_event_propagation():
    """
    Test that Phase Wait events spanning over invalid events are marked invalid.
    
    Bug: A Phase Wait marked valid can span over invalid Green/Yellow events, 
    which means the timing data during that period is unreliable.
    
    If there are invalid events (due to missing data) during a Phase Wait period,
    the Phase Wait should also be marked invalid because:
    1. Missing data could have included a green for the waiting phase
    2. The wait duration calculation is potentially incorrect
    """
    from datetime import datetime, timedelta
    
    base = datetime(2024, 6, 1, 8, 0, 0)
    
    # Synthetic events:
    # - Phase 1: Phase call at 0s, green at 200s (200s wait)
    # - During this wait, there are invalid events for phase 2 (two Green starts in a row)
    #   The paired_event macro marks Green as invalid when NextEventId != 7
    # - Phase 1's wait spans over these invalid events and should be marked invalid
    #
    # Also include a control case: Phase 3 with a wait that doesn't overlap invalid events
    raw_events = [
        # Phase 1: Long wait that will span over invalid events
        (base, 1002, 43, 1),                                    # Phase call at 0s
        (base + timedelta(seconds=200), 1002, 1, 1),            # Green at 200s
        (base + timedelta(seconds=210), 1002, 7, 1),            # Green end
        (base + timedelta(seconds=210.2), 1002, 44, 1),         # Call drop
        
        # Phase 2: Create INVALID Green (Green start followed by Green start, not Green end)
        # This simulates a data gap where the Green end (7) was lost
        (base + timedelta(seconds=50), 1002, 1, 2),             # Green start at 50s (INVALID: NextEvent=1)
        (base + timedelta(seconds=100), 1002, 1, 2),            # Another Green start at 100s (creates invalid pair)
        (base + timedelta(seconds=150), 1002, 7, 2),            # Green end at 150s
        
        # Phase 3: Normal wait AFTER the invalid period (control case)
        (base + timedelta(seconds=250), 1002, 43, 3),           # Phase call at 250s
        (base + timedelta(seconds=280), 1002, 1, 3),            # Green at 280s (30s wait)
        (base + timedelta(seconds=290), 1002, 7, 3),            # Green end
        (base + timedelta(seconds=290.2), 1002, 44, 3),         # Call drop
    ]
    
    raw_df = pd.DataFrame(raw_events, columns=['TimeStamp', 'DeviceId', 'EventId', 'Parameter'])
    raw_df['DeviceId'] = raw_df['DeviceId'].astype('int64')
    raw_df['EventId'] = raw_df['EventId'].astype('int16')
    raw_df['Parameter'] = raw_df['Parameter'].astype('int16')
    
    params = {
        'raw_data': raw_df,
        'bin_size': 15,
        'verbose': 0,
        'aggregations': [
            {'name': 'timeline', 'params': {'min_duration': 0.0, 'cushion_time': 60, 'maxtime': True}},
        ]
    }
    
    processor = SignalDataProcessor(**params)
    processor.load()
    processor.aggregate()
    
    # Check for invalid events
    invalid_events = processor.conn.query("""
        SELECT * FROM timeline WHERE IsValid = FALSE
    """).df()
    
    # Get Phase Wait events 
    phase_waits = processor.conn.query("""
        SELECT * FROM timeline 
        WHERE EventClass = 'Phase Wait'
        ORDER BY StartTime
    """).df()
    
    processor.close()
    
    # We need at least one invalid event for this test to be meaningful
    assert len(invalid_events) > 0, (
        f"Expected at least one invalid event in the timeline, got {len(invalid_events)}.\n"
        f"This test requires invalid events to exist for overlap checking."
    )
    
    # Get the invalid event time range
    invalid_start = invalid_events['StartTime'].min()
    invalid_end = invalid_events['EndTime'].max()
    
    # Get Phase 1 wait events
    phase1_waits = phase_waits[phase_waits['EventValue'] == 1]
    
    # Check if Phase 1 wait overlaps with invalid events period
    if len(phase1_waits) > 0:
        pw1 = phase1_waits.iloc[0]
        pw1_start = pw1['StartTime']
        pw1_end = pw1['EndTime']
        
        # Check for overlap: ranges overlap if Start1 < End2 AND Start2 < End1
        overlaps = pw1_start < invalid_end and invalid_start < pw1_end
        
        if overlaps:
            # If Phase 1 wait overlaps with invalid events, it should be marked invalid
            assert pw1['IsValid'] == False, (
                f"Phase 1 wait spans over invalid events and should be marked IsValid=False.\n"
                f"Phase Wait: {pw1_start} -> {pw1_end}\n"
                f"Invalid period: {invalid_start} -> {invalid_end}\n"
                f"But IsValid={pw1['IsValid']}"
            )

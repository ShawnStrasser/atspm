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
  'remove_incomplete': False,
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

if __name__ == "__main__":
  pytest.main([__file__])

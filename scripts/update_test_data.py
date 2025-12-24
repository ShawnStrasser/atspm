import os
import sys
import shutil
import duckdb
import pandas as pd

# Add src to path so we can import atspm if not installed
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from atspm import SignalDataProcessor

# Define the parameters for testing (matching tests/test_atspm.py)
TEST_PARAMS = {
    'raw_data': duckdb.query("select * from 'tests/hires_test_data.parquet'").df(),
    'detector_config': duckdb.query("select * from 'tests/configs_test_data.parquet'").df(),
    'bin_size': 15,
    'output_dir': 'tests/test_output_temp',
    'output_to_separate_folders': False,
    'output_format': 'parquet',
    'output_file_prefix': 'test_',
    'remove_incomplete': False,
    'to_sql': False,
    'verbose': 1,
    'aggregations': [
        #{'name': 'has_data', 'params': {'no_data_min': 5, 'min_data_points': 3}},
        #{'name': 'actuations', 'params': {}},
        #{'name': 'arrival_on_green', 'params': {'latency_offset_seconds': 0}},
        #{'name': 'communications', 'params': {'event_codes': '400,503,502'}},
        #{'name': 'coordination', 'params': {}},
        #{'name': 'ped', 'params': {}},
        #{'name': 'unique_ped', 'params': {'seconds_between_actuations': 15}},
        #{'name': 'full_ped', 'params': {'seconds_between_actuations': 15, 'return_volumes':True}},
        #{'name': 'split_failures', 'params': {'red_time': 5, 'red_occupancy_threshold': 0.80, 'green_occupancy_threshold': 0.80, 'by_approach': True, 'by_cycle': False}},
        #{'name': 'splits', 'params': {}},
        #{'name': 'terminations', 'params': {}},
        #{'name': 'yellow_red', 'params': {'latency_offset_seconds': 1.5, 'min_red_offset': -8}},
        {'name': 'timeline', 'params': {'min_duration': 0.2, 'cushion_time':60, 'maxtime': True}},
        #{'name': 'ped_delay', 'params': {}},
    ],
    'remove_incomplete': False,
}

def update_precalculated():
    print("Running SignalDataProcessor to generate new precalculated data...")
    
    # Ensure output dir is clean
    if os.path.exists(TEST_PARAMS['output_dir']):
        shutil.rmtree(TEST_PARAMS['output_dir'])
    
    # Run processor
    processor = SignalDataProcessor(**TEST_PARAMS)
    processor.run()
    
    # Update all precalculated files
    for agg in TEST_PARAMS['aggregations']:
        agg_name = agg['name']
        generated_file = os.path.join(TEST_PARAMS['output_dir'], f"test_{agg_name}.parquet")
        target_file = f'tests/precalculated/{agg_name}.parquet'
        
        if os.path.exists(generated_file):
            print(f"Updating {target_file}...")
            shutil.copy(generated_file, target_file)
        else:
            print(f"Warning: Generated file {generated_file} not found for {agg_name}.")
    
    # Cleanup
    shutil.rmtree(TEST_PARAMS['output_dir'])
    print("Update complete.")

if __name__ == "__main__":
    update_precalculated()

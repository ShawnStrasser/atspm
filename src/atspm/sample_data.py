# sample_data.py inside the atspm package
import duckdb
import os

# Assuming this file is in the same directory as the `data` directory
_data_dir = os.path.join(os.path.dirname(__file__), 'data')

class SampleData:
    """
    Lazy-loaded sample data class.
    
    Data is only loaded from disk when first accessed, avoiding unnecessary
    memory usage if sample_data is imported but not used.
    
    Returns DuckDB relations which can be used directly with SignalDataProcessor
    or converted to pandas DataFrames with .df() if needed.
    """
    def __init__(self):
        self._config = None
        self._data = None

    @property
    def config(self):
        """Detector configuration sample data (lazy-loaded). Returns a DuckDB relation."""
        if self._config is None:
            path = os.path.join(_data_dir, 'sample_config.parquet')
            self._config = duckdb.read_parquet(path)
        return self._config
    
    @property
    def data(self):
        """Raw hi-res sample data (lazy-loaded). Returns a DuckDB relation."""
        if self._data is None:
            path = os.path.join(_data_dir, 'sample_raw_data.parquet')
            self._data = duckdb.read_parquet(path)
        return self._data

# Create an instance of the class (data not loaded until accessed)
sample_data = SampleData()
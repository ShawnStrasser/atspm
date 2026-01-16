"""
Test that timeline events spanning periods with missing data are marked as invalid.

This test creates synthetic data with a large time gap where data is missing.
The key test is:
- Without has_data filtering: events appear valid (IsValid=True)
- With has_data filtering: events spanning missing data should be marked invalid (IsValid=False)
"""

import pytest
import pandas as pd
import duckdb
from datetime import datetime, timedelta
from src.atspm import SignalDataProcessor


def create_synthetic_data():
    """
    Create synthetic raw data with a large gap in the middle (from user's real example).
    
    Timeline:
    - 09:53:00 - Some events occur (Green start/end, Phase Call events)
    - LARGE GAP - No data for ~12 hours
    - 22:14:58 - Green Start for a Phase Wait that started at 09:53:50
    
    With 15-minute bins and no_data_min=15, min_data_points=1:
    - Only bins 09:45 and 22:00 will have has_data
    - All bins in between are missing
    - The Phase Wait event from 09:53:50 to 22:14:58 spans many missing bins
    """
    data = """DeviceId,TimeStamp,EventId,Parameter
test,2026-01-07 09:53:00.400,1,8
test,2026-01-07 09:53:00.600,44,8
test,2026-01-07 09:53:33.300,7,8
test,2026-01-07 09:53:50.500,43,8
test,2026-01-07 22:14:58.500,1,8"""
    
    df = pd.read_csv(pd.io.common.StringIO(data), parse_dates=['TimeStamp'])
    return df


class TestTimelineHasDataIntegration:
    """Test that timeline events spanning missing data periods are marked invalid."""
    
    def test_timeline_without_has_data_phase_wait_is_valid(self):
        """
        Test that WITHOUT has_data checking, the Phase Wait event spanning the gap is marked as VALID.
        This is the baseline behavior - the event is "valid" because start/end pair matched.
        """
        raw_data = create_synthetic_data()
        
        # Run only timeline aggregation (no has_data)
        with SignalDataProcessor(
            raw_data=raw_data,
            detector_config=pd.DataFrame(columns=['DeviceId', 'Phase', 'Parameter', 'Function']),
            bin_size=15,
            verbose=0,
            remove_incomplete=False,
            aggregations=[
                {'name': 'timeline', 'params': {'min_duration': 0.1, 'cushion_time': 60}},
            ]
        ) as processor:
            processor.load()
            processor.aggregate()
            
            # Get the Phase Wait timeline result
            result = processor.conn.sql("""
                SELECT DeviceId, StartTime, EndTime, Duration, IsValid, EventClass, EventValue
                FROM timeline
                WHERE EventClass = 'Phase Wait'
            """).df()
        
        # Verify the Phase Wait event exists and is marked as valid (no has_data check)
        assert len(result) == 1, f"Expected 1 Phase Wait event, got {len(result)}"
        assert result.iloc[0]['IsValid'] == True, "Phase Wait event should be valid without has_data check"
        assert result.iloc[0]['Duration'] > 40000, "Duration should be ~44468 seconds (12+ hours)"
    
    def test_timeline_with_has_data_phase_wait_is_invalid(self):
        """
        Test that WITH has_data checking, the Phase Wait event spanning the gap is marked as INVALID.
        This tests the new functionality where events spanning missing data are marked invalid.
        """
        raw_data = create_synthetic_data()
        
        # Run has_data and timeline aggregations
        with SignalDataProcessor(
            raw_data=raw_data,
            detector_config=pd.DataFrame(columns=['DeviceId', 'Phase', 'Parameter', 'Function']),
            bin_size=15,
            verbose=0,
            remove_incomplete=False,  # We're not removing, just marking invalid
            aggregations=[
                # Simple has_data: 15 minute bins, at least 1 data point per bin
                {'name': 'has_data', 'params': {'no_data_min': 15, 'min_data_points': 1}},
                {'name': 'timeline', 'params': {'min_duration': 0.1, 'cushion_time': 60}},
            ]
        ) as processor:
            processor.load()
            processor.aggregate()
            
            # Debug: Check what has_data looks like
            has_data_result = processor.conn.sql("SELECT * FROM has_data ORDER BY TimeStamp").df()
            print(f"\nhas_data table:\n{has_data_result}")
            
            # Get the Phase Wait timeline result
            result = processor.conn.sql("""
                SELECT DeviceId, StartTime, EndTime, Duration, IsValid, EventClass, EventValue
                FROM timeline
                WHERE EventClass = 'Phase Wait'
            """).df()
            
            print(f"\nTimeline Phase Wait result:\n{result}")
        
        # Verify has_data only has 2 bins (09:45 and 22:00)
        assert len(has_data_result) == 2, f"Expected 2 has_data bins, got {len(has_data_result)}"
        
        # Verify the Phase Wait event exists and is marked as INVALID
        assert len(result) == 1, f"Expected 1 Phase Wait event, got {len(result)}"
        assert result.iloc[0]['IsValid'] == False, \
            f"Phase Wait event should be INVALID because it spans many missing data periods. Got IsValid={result.iloc[0]['IsValid']}"


if __name__ == '__main__':
    # Run tests directly for debugging
    test = TestTimelineHasDataIntegration()
    print("Running test_timeline_without_has_data_phase_wait_is_valid...")
    test.test_timeline_without_has_data_phase_wait_is_valid()
    print("PASSED!\n")
    
    print("Running test_timeline_with_has_data_phase_wait_is_invalid...")
    try:
        test.test_timeline_with_has_data_phase_wait_is_invalid()
        print("PASSED!")
    except AssertionError as e:
        print(f"FAILED: {e}")

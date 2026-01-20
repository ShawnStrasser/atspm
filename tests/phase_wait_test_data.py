"""
Synthetic test data for Phase Wait logic validation.

This file generates raw event data and expected precalculated results
for testing the Phase Wait feature in the timeline aggregation.

Scenarios covered:
1. Simple 43→1: Phase call directly followed by green (valid Phase Wait)
2. 43→44: Dropped call (should NOT create Phase Wait)
3. 43→44→43→1: Dropped call followed by new call that succeeds (only second 43 counts)
4. Persistent call: 43→1→7→1 (call persists across multiple greens - creates 2 Phase Waits)
5. Persistent call: 43→1→7→1→7→1 (call persists for 3 greens - creates 3 Phase Waits)
6. 43→1→7→44→43→1: Persistent call that drops then new call (1st+3rd 43 create Phase Waits)
7. Chunk boundary: 43 at end of chunk, 1 at start of next (tests 902 state tracking)
8. Multiple phases: Same logic applied to different phases
9. Multiple devices: Same logic across different devices
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta

# Use a test device ID that doesn't conflict with existing test data
DEVICE_A = 999  # Main test device
DEVICE_B = 998  # Secondary device for multi-device test

# Base timestamp for synthetic data
BASE_TIME = datetime(2024, 5, 14, 8, 0, 0)  # May 14, 2024 at 8:00 AM

def ts(seconds_offset):
    """Helper to create timestamp with offset in seconds from base time"""
    return BASE_TIME + timedelta(seconds=seconds_offset)

# Event IDs
PHASE_CALL = 43
PHASE_DROP = 44
GREEN_START = 1
GREEN_END = 7
PHASE_WAIT_MARKER = 902  # For state tracking

# ============================================================================
# RAW EVENT DATA - All scenarios
# ============================================================================

raw_events = []

# =============================================================================
# SCENARIO 1: Simple 43→1 (Phase 1, Device A)
# Expected: One Phase Wait from 0s to 30s = 30 seconds duration
# =============================================================================
raw_events.extend([
    (ts(0), DEVICE_A, PHASE_CALL, 1),    # Phase call at 0s
    (ts(30), DEVICE_A, GREEN_START, 1),  # Green at 30s
    (ts(40), DEVICE_A, GREEN_END, 1),    # Green end at 40s
    (ts(40.2), DEVICE_A, PHASE_DROP, 1), # Call drops at 40.2s
])

# =============================================================================
# SCENARIO 2: 43→44 dropped call (Phase 2, Device A)  
# Expected: NO Phase Wait event - the call was dropped before green
# =============================================================================
raw_events.extend([
    (ts(100), DEVICE_A, PHASE_CALL, 2),    # Phase call at 100s
    (ts(105), DEVICE_A, PHASE_DROP, 2),    # Call drops at 105s (right turn)
    # No green for this phase in this cycle - call was cancelled
])

# =============================================================================
# SCENARIO 3: 43→44→43→1 (Phase 3, Device A)
# First call dropped, second call succeeds
# Expected: One Phase Wait from 215s to 250s = 35 seconds
# (The first 43→44 is ignored, only second 43 counts)
# =============================================================================
raw_events.extend([
    (ts(200), DEVICE_A, PHASE_CALL, 3),    # First call at 200s
    (ts(210), DEVICE_A, PHASE_DROP, 3),    # Dropped at 210s
    (ts(215), DEVICE_A, PHASE_CALL, 3),    # Second call at 215s
    (ts(250), DEVICE_A, GREEN_START, 3),   # Green at 250s
    (ts(260), DEVICE_A, GREEN_END, 3),     # Green end at 260s
    (ts(260.2), DEVICE_A, PHASE_DROP, 3),  # Call drops at 260.2s
])

# =============================================================================
# SCENARIO 4: Persistent call 43→1→7→1 (Phase 4, Device A)
# Call persists across two green phases
# Expected: Two Phase Wait events:
#   - First: 300s to 330s = 30 seconds (from call to first green)
#   - Second: 340s to 400s = 60 seconds (from green end to second green)
# =============================================================================
raw_events.extend([
    (ts(300), DEVICE_A, PHASE_CALL, 4),    # Phase call at 300s
    (ts(330), DEVICE_A, GREEN_START, 4),   # First green at 330s
    (ts(340), DEVICE_A, GREEN_END, 4),     # First green end at 340s
    # NO phase drop - call persists!
    (ts(400), DEVICE_A, GREEN_START, 4),   # Second green at 400s
    (ts(410), DEVICE_A, GREEN_END, 4),     # Second green end at 410s
    (ts(410.2), DEVICE_A, PHASE_DROP, 4),  # Now call drops at 410.2s
])

# =============================================================================
# SCENARIO 5: Persistent call 43→1→7→1→7→1 (Phase 5, Device A)
# Call persists across THREE green phases
# Expected: Three Phase Wait events:
#   - First: 500s to 530s = 30 seconds
#   - Second: 540s to 600s = 60 seconds
#   - Third: 610s to 670s = 60 seconds
# =============================================================================
raw_events.extend([
    (ts(500), DEVICE_A, PHASE_CALL, 5),    # Phase call at 500s
    (ts(530), DEVICE_A, GREEN_START, 5),   # First green at 530s
    (ts(540), DEVICE_A, GREEN_END, 5),     # First green end at 540s
    # Persistent call continues...
    (ts(600), DEVICE_A, GREEN_START, 5),   # Second green at 600s
    (ts(610), DEVICE_A, GREEN_END, 5),     # Second green end at 610s
    # Still persistent...
    (ts(670), DEVICE_A, GREEN_START, 5),   # Third green at 670s
    (ts(680), DEVICE_A, GREEN_END, 5),     # Third green end at 680s
    (ts(680.2), DEVICE_A, PHASE_DROP, 5),  # Call finally drops at 680.2s
])

# =============================================================================
# SCENARIO 6: 43→1→7→44→43→1 (Phase 6, Device A)
# Persistent call that drops, then new call
# Expected: Two Phase Wait events:
#   - First: 700s to 730s = 30 seconds (from first 43 to first 1)
#   - Second: 760s to 800s = 40 seconds (from new 43 to second 1)
# Note: The 7→44 means call dropped, so no Phase Wait from 740s
# =============================================================================
raw_events.extend([
    (ts(700), DEVICE_A, PHASE_CALL, 6),    # First call at 700s
    (ts(730), DEVICE_A, GREEN_START, 6),   # First green at 730s
    (ts(740), DEVICE_A, GREEN_END, 6),     # First green end at 740s
    (ts(745), DEVICE_A, PHASE_DROP, 6),    # Call drops at 745s (NOT persistent!)
    (ts(760), DEVICE_A, PHASE_CALL, 6),    # New call at 760s
    (ts(800), DEVICE_A, GREEN_START, 6),   # Second green at 800s
    (ts(810), DEVICE_A, GREEN_END, 6),     # Second green end at 810s
    (ts(810.2), DEVICE_A, PHASE_DROP, 6),  # Call drops at 810.2s
])

# =============================================================================
# SCENARIO 7: Multiple dropped calls before success (Phase 7, Device A)
# 43→44→43→44→43→1 pattern
# Expected: One Phase Wait from 920s to 970s = 50 seconds
# (First two calls dropped, only third call counts)
# =============================================================================
raw_events.extend([
    (ts(900), DEVICE_A, PHASE_CALL, 7),    # First call at 900s (will drop)
    (ts(903), DEVICE_A, PHASE_DROP, 7),    # Dropped
    (ts(910), DEVICE_A, PHASE_CALL, 7),    # Second call at 910s (will drop)
    (ts(915), DEVICE_A, PHASE_DROP, 7),    # Dropped
    (ts(920), DEVICE_A, PHASE_CALL, 7),    # Third call at 920s (will succeed)
    (ts(970), DEVICE_A, GREEN_START, 7),   # Green at 970s
    (ts(980), DEVICE_A, GREEN_END, 7),     # Green end at 980s
    (ts(980.2), DEVICE_A, PHASE_DROP, 7),  # Call drops at 980.2s
])

# =============================================================================
# SCENARIO 8: Same logic on Device B (Phase 1, Device B)
# Simple 43→1 to verify multi-device handling
# Expected: One Phase Wait from 50s to 80s = 30 seconds
# =============================================================================
raw_events.extend([
    (ts(50), DEVICE_B, PHASE_CALL, 1),     # Phase call at 50s
    (ts(80), DEVICE_B, GREEN_START, 1),    # Green at 80s
    (ts(90), DEVICE_B, GREEN_END, 1),      # Green end at 90s
    (ts(90.2), DEVICE_B, PHASE_DROP, 1),   # Call drops at 90.2s
])

# =============================================================================
# SCENARIO 9: Chunk boundary test (Phase 8, Device A)
# 43 near "end of chunk" (simulated), 1 in "next chunk"
# This tests the 902 state tracking for incremental processing
# For the oneshot test, this should still produce one Phase Wait
# Expected: One Phase Wait from 1000s to 1100s = 100 seconds
# =============================================================================
raw_events.extend([
    # Chunk boundary would be around 1050s in incremental mode
    (ts(1000), DEVICE_A, PHASE_CALL, 8),   # Phase call at 1000s (before boundary)
    (ts(1100), DEVICE_A, GREEN_START, 8),  # Green at 1100s (after boundary)
    (ts(1110), DEVICE_A, GREEN_END, 8),    # Green end at 1110s
    (ts(1110.2), DEVICE_A, PHASE_DROP, 8), # Call drops at 1110.2s
])

# =============================================================================
# SCENARIO 10: No call before green (Phase 9, Device A)
# Green without any preceding phase call - should NOT create Phase Wait
# This tests that we don't create spurious events
# =============================================================================
raw_events.extend([
    (ts(1200), DEVICE_A, GREEN_START, 9),  # Green without call
    (ts(1210), DEVICE_A, GREEN_END, 9),    # Green end
    # No phase call, no phase wait event expected
])

# =============================================================================
# SCENARIO 11: Invalid Phase Call → Invalid Phase Wait (Phase 10, Device A)
# BUG FIX TEST: When a Phase Call (43) is not properly terminated with a Phase Drop (44)
# before the next Phase Call (43), the Phase Call is "invalid".
# Phase Wait events derived from an invalid Phase Call should also be invalid.
#
# Pattern: 43 → 1 → 7 → 43 → 1 → 7 → 44
# - First 43 at 1300s: No 44 before next 43, so invalid Phase Call
# - Second 43 at 1400s: Properly terminated with 44 at 1520s, so valid Phase Call
#
# Expected: 
# - Phase Wait from 1300s→1330s = 30 seconds, IsValid=FALSE (invalid phase call)
# - NO persistent Phase Wait because new 43 resets the call state
# - Phase Wait from 1400s→1430s = 30 seconds, IsValid=TRUE (valid phase call)
# =============================================================================
raw_events.extend([
    (ts(1300), DEVICE_A, PHASE_CALL, 10),   # First call at 1300s (NO 44 before next 43 = invalid!)
    (ts(1330), DEVICE_A, GREEN_START, 10),  # Green at 1330s
    (ts(1340), DEVICE_A, GREEN_END, 10),    # Green end at 1340s
    # NO phase drop! But next event is another 43, which resets the call
    (ts(1400), DEVICE_A, PHASE_CALL, 10),   # Second call at 1400s (overwrites first, this one is valid)
    (ts(1430), DEVICE_A, GREEN_START, 10),  # Green at 1430s
    (ts(1440), DEVICE_A, GREEN_END, 10),    # Green end at 1440s
    (ts(1520), DEVICE_A, PHASE_DROP, 10),   # Call drops at 1520s (valid termination)
])

# Create the raw data DataFrame
raw_df = pd.DataFrame(raw_events, columns=['TimeStamp', 'DeviceId', 'EventId', 'Parameter'])
raw_df = raw_df.sort_values(['TimeStamp', 'DeviceId', 'EventId']).reset_index(drop=True)

# ============================================================================
# EXPECTED PRECALCULATED OUTPUT - Phase Wait events only
# ============================================================================

expected_phase_wait = [
    # Scenario 1: Simple 43→1
    (DEVICE_A, ts(0), ts(30), 30.0, True, 'Phase Wait', 1),
    
    # Scenario 2: 43→44 - NO EVENT (dropped call)
    
    # Scenario 3: 43→44→43→1 - Only second call counts
    (DEVICE_A, ts(215), ts(250), 35.0, True, 'Phase Wait', 3),
    
    # Scenario 4: Persistent call - Two events
    (DEVICE_A, ts(300), ts(330), 30.0, True, 'Phase Wait', 4),
    (DEVICE_A, ts(340), ts(400), 60.0, True, 'Phase Wait', 4),
    
    # Scenario 5: Persistent call - Three events
    (DEVICE_A, ts(500), ts(530), 30.0, True, 'Phase Wait', 5),
    (DEVICE_A, ts(540), ts(600), 60.0, True, 'Phase Wait', 5),
    (DEVICE_A, ts(610), ts(670), 60.0, True, 'Phase Wait', 5),
    
    # Scenario 6: Call drops then new call - Two events
    (DEVICE_A, ts(700), ts(730), 30.0, True, 'Phase Wait', 6),
    (DEVICE_A, ts(760), ts(800), 40.0, True, 'Phase Wait', 6),
    
    # Scenario 7: Multiple drops before success - One event
    (DEVICE_A, ts(920), ts(970), 50.0, True, 'Phase Wait', 7),
    
    # Scenario 8: Device B - One event
    (DEVICE_B, ts(50), ts(80), 30.0, True, 'Phase Wait', 1),
    
    # Scenario 9: Chunk boundary - One event
    (DEVICE_A, ts(1000), ts(1100), 100.0, True, 'Phase Wait', 8),
    
    # Scenario 10: No call - NO EVENT
    
    # Scenario 11: Invalid Phase Call → Only the valid Phase Wait is expected here
    # The two INVALID Phase Waits from 1300-1330 and 1340-1400 are NOT included
    # because this list only contains valid (IsValid=True) events
    (DEVICE_A, ts(1400), ts(1430), 30.0, True, 'Phase Wait', 10),
]

# Expected INVALID Phase Wait events (for bug fix test)
# These are Phase Wait events that should have IsValid=False
expected_invalid_phase_wait = [
    # Scenario 11: Invalid Phase Call creates invalid Phase Wait events
    # The first 43 at 1300s has no 44 before the next 43 at 1400s, so it's invalid
    (DEVICE_A, ts(1300), ts(1330), 30.0, False, 'Phase Wait', 10),   # Invalid: 43→1
]

expected_df = pd.DataFrame(expected_phase_wait, columns=[
    'DeviceId', 'StartTime', 'EndTime', 'Duration', 'IsValid', 'EventClass', 'EventValue'
])

expected_invalid_df = pd.DataFrame(expected_invalid_phase_wait, columns=[
    'DeviceId', 'StartTime', 'EndTime', 'Duration', 'IsValid', 'EventClass', 'EventValue'
])

# Match dtypes to actual precalculated format
expected_df['DeviceId'] = expected_df['DeviceId'].astype('int64')
expected_df['Duration'] = expected_df['Duration'].astype('float32')
expected_df['EventValue'] = expected_df['EventValue'].astype('int16')

expected_invalid_df['DeviceId'] = expected_invalid_df['DeviceId'].astype('int64')
expected_invalid_df['Duration'] = expected_invalid_df['Duration'].astype('float32')
expected_invalid_df['EventValue'] = expected_invalid_df['EventValue'].astype('int16')

if __name__ == '__main__':
    print("=== RAW SYNTHETIC DATA (Phase Events) ===")
    print(raw_df.to_string())
    print(f"\nTotal raw events: {len(raw_df)}")
    
    print("\n\n=== EXPECTED VALID PHASE WAIT EVENTS ===")
    print(expected_df.to_string())
    print(f"\nTotal expected valid Phase Wait events: {len(expected_df)}")
    
    print("\n\n=== EXPECTED INVALID PHASE WAIT EVENTS ===")
    print(expected_invalid_df.to_string())
    print(f"\nTotal expected invalid Phase Wait events: {len(expected_invalid_df)}")


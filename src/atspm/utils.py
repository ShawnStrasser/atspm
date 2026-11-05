# Synthetic EventIds that hold state between runs in unmatched_events, not interval starts
# 927 to 930 left_turn_gaps, 931 to 934 coordination_agg, 935, 936 and 955 timeline, 937 and 943 to 947 ped,
# 938 to 941 and 948 to 950 arrival_on_green and platoon_ratio, 942 remove_incomplete, 951 to 954 yellow_red, 956 actuations,
# 957 to 959 approach_delay, 970 to 999 has_data
STATE_EVENT_IDS = 'BETWEEN 927 AND 999'

# remove_incomplete keeps a bin at this Score or more. The bins has_data reads as having data, with nothing else from its row.
COMPLETE_BINS = '(SELECT TimeStamp, DeviceId FROM has_data WHERE Score >= 0.5)'

# Tables that write whatever bins a run has, with nothing kept for a later run. Cut inside a bin, a run writes that bin again.
WRITE_BINS_AT_ONCE = ('actuations', 'terminations', 'splits', 'coordination_agg', 'phase_wait', 'ped_delay')

# custom function for printing depending on verbose setting
def v_print(msg, verbose=1, level=1):
    if verbose >= level:
        print(msg)
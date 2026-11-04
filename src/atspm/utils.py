# Synthetic EventIds that hold state between runs in unmatched_events, not interval starts
# 931 to 934 coordination_agg, 935 and 936 timeline, 937 and 943 to 947 unique_ped and full_ped,
# 938 to 941 and 948 to 950 arrival_on_green and platoon_ratio, 942 remove_incomplete, 951 to 954 yellow_red
STATE_EVENT_IDS = 'BETWEEN 931 AND 954'

# custom function for printing depending on verbose setting
def v_print(msg, verbose=1, level=1):
    if verbose >= level:
        print(msg)
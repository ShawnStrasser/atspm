--What arrival_pattern passes to the next run through unmatched_events, as yellow_red_carry.sql does for yellow_red:
--960 green, 961 yellow, 962 red clearance begins and 963 a vehicle at its arrival at the stop bar, with the phase as Parameter.
{% set arrival_pattern = True %}
{% include 'yellow_red_carry.sql' %}

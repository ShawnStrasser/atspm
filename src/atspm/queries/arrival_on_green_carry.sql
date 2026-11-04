--What arrival_on_green and platoon_ratio pass to the next run through unmatched_events, as synthetic EventIds:
--938 arrivals that land after results stopped, at their projected time, with the phase as Parameter
--939, 940, 941 green, yellow and red clearance begins from there on, with the phase as Parameter
--939 also marks a phase green when results stopped, stamped with that time, so the next run takes its green from there
--942 bins from there on that has_data passed. has_data only sees the current run, and a later run writes them
--A run with no raw data has no results_end and keeps everything it was given.
--Written in SQL for DuckDB. This is a jinja2 template, with variables inside curly braces.


WITH {% include 'arrival_phase_events.sql' %},

{% include 'arrival_projection.sql' %},

ends AS (SELECT TimeStamp AS Results_End FROM results_end)

SELECT TimeStamp, DeviceId, 938::INT16 AS EventId, Phase::INT16 AS Parameter, TRUE AS IsValid
FROM arrivals, ends
WHERE Results_End IS NULL OR TimeStamp >= Results_End

UNION ALL

SELECT TimeStamp, DeviceId,
    (CASE EventId WHEN 1 THEN 939 WHEN 8 THEN 940 ELSE 941 END)::INT16 AS EventId,
    Phase::INT16 AS Parameter,
    TRUE AS IsValid
FROM phase_events, ends
WHERE Results_End IS NULL OR TimeStamp >= Results_End

UNION ALL

SELECT ANY_VALUE(Results_End), DeviceId, 939::INT16 AS EventId, Phase::INT16 AS Parameter, TRUE AS IsValid
FROM phase_events, ends
WHERE TimeStamp < Results_End
GROUP BY DeviceId, Phase
HAVING ARG_MAX(EventId, (TimeStamp, EventId)) = 1

{%- if remove_incomplete %}

UNION ALL

SELECT DISTINCT b.TimeStamp, b.DeviceId, 942::INT16 AS EventId, 0::INT16 AS Parameter, TRUE AS IsValid
FROM (
    SELECT TimeStamp, DeviceId FROM has_data
    {%- if unmatched %}
    UNION ALL
    SELECT TimeStamp, DeviceId FROM unmatched_previous WHERE EventId = 942
    {%- endif %}
) b, ends
WHERE Results_End IS NULL OR b.TimeStamp >= Results_End
{%- endif %}

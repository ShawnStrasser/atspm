--Bins from where results stopped onward that has_data passed, for remove_incomplete, as synthetic EventId 942 in
--unmatched_events. has_data only sees the current run, and a later run writes these bins. yellow_red can write a bin
--from before results stopped, once the last cycle with a red in it has finished, so a device keeps its bins from its
--first red not written yet.
--A run with no raw data has no results_end and keeps everything it was given.
--Written in SQL for DuckDB. This is a jinja2 template, with variables inside curly braces.


WITH {% include 'results_end.sql' %},

ends AS (SELECT TimeStamp AS Results_End FROM results_end)

SELECT DISTINCT b.TimeStamp, b.DeviceId, 942::INT16 AS EventId, 0::INT16 AS Parameter, TRUE AS IsValid
FROM (
    SELECT TimeStamp, DeviceId FROM has_data
    {%- if unmatched %}
    UNION ALL
    SELECT TimeStamp, DeviceId FROM unmatched_previous WHERE EventId = 942
    {%- endif %}
) b, ends
{%- if yellow_red_carried %}
LEFT JOIN (
    SELECT DeviceId, MIN(time_bucket(INTERVAL '{{bin_size}} minutes', TimeStamp)) AS First_Held
    FROM unmatched_events
    WHERE EventId = 953
    GROUP BY DeviceId
) h ON b.DeviceId = h.DeviceId
{%- endif %}
WHERE Results_End IS NULL OR b.TimeStamp >= Results_End
{%- if yellow_red_carried %}
   OR b.TimeStamp >= h.First_Held
{%- endif %}

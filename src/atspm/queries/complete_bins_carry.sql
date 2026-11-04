--Bins from where results stopped onward that has_data passed, for remove_incomplete, as synthetic EventId 942 in
--unmatched_events. has_data only sees the current run, and a later run writes these bins.
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
WHERE Results_End IS NULL OR b.TimeStamp >= Results_End

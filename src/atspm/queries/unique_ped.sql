--Aggregate Unique Pedestrian Actuations in 15-minute Bins
--Written in SQL for DuckDB. This is a jinja2 template, with variables inside curly braces.

--Unique actuations are counted in unique_ped_counts.sql, see that file for how. Each bin is written once, whole, by
--the run that reaches its end, see ped_state.sql.

WITH {% include 'ped_state.sql' %}

SELECT
    u.TimeStamp,
    u.DeviceId,
    u.Phase,
    u.Unique_Actuations::int16 AS Unique_Actuations
FROM unique_counts u
JOIN inputs i ON i.DeviceId = u.DeviceId AND i.Phase = u.Phase
CROSS JOIN results_end e
WHERE u.TimeStamp >= i.Next_Bin
  AND u.TimeStamp < e.TimeStamp

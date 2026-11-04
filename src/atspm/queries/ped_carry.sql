--What the next run needs to carry on the ped counts, as rows of unmatched_events. See ped_state.sql.
--Written in SQL for DuckDB. This is a jinja2 template, with variables inside curly braces.


WITH {% include 'ped_state.sql' %},

--The first bin this run did not write. The end of a run with no earlier bin to wait for can fall inside a bin it wrote.
next_bin AS (
    SELECT TIME_BUCKET(interval '{{bin_size}} minutes', TimeStamp - INTERVAL 1 MILLISECOND) + INTERVAL '{{bin_size}} minutes' AS TimeStamp
    FROM results_end
)

SELECT c.TimeStamp, c.DeviceId, 943::INT16 AS EventId, c.Phase AS Parameter, TRUE AS IsValid
FROM (
    SELECT TimeStamp, DeviceId, Phase, UNNEST(range(Unique_Actuations))
    FROM unique_counts
) c, next_bin n
WHERE c.TimeStamp >= n.TimeStamp - INTERVAL '{{bin_size * 3}} minutes'

UNION ALL

SELECT GREATEST(i.Next_Bin, n.TimeStamp), i.DeviceId, 944::INT16, i.Phase, TRUE
FROM inputs i, next_bin n

UNION ALL

SELECT Last_Activity, DeviceId, 945::INT16, Phase, TRUE
FROM inputs

UNION ALL

SELECT e.TimeStamp, e.DeviceId, (CASE WHEN e.Is_Press THEN 946 ELSE 947 END)::INT16, e.Phase, TRUE
FROM ped_events e, next_bin n
WHERE e.TimeStamp >= n.TimeStamp

--Aggregate Pedestrian Services, Actuations, Unique Actuations and Estimated Volumes
--Written in SQL for DuckDB. This is a jinja2 template, with variables inside curly braces.

--Unique actuations come from ped_unique_counts.sql, see that file for how they are counted.
--Each bin is written once, whole, by the run that reaches its end, see ped_state.sql.

WITH {% include 'ped_state.sql' %},

ped_counts AS (
    SELECT
        TimeStamp,
        DeviceId,
        Phase,
        (COUNT(*) FILTER (WHERE NOT Is_Press))::int16 AS PedServices,
        (COUNT(*) FILTER (WHERE Is_Press))::int16 AS PedActuation
    FROM ped_events
    GROUP BY ALL
),

ped_combined AS (
    SELECT
        TimeStamp,
        DeviceId,
        Phase,
        COALESCE(PedServices, 0)::int16 AS PedServices,
        COALESCE(PedActuation, 0)::int16 AS PedActuation,
        COALESCE(Unique_Actuations, 0)::int16 AS Unique_Actuations
    FROM ped_counts
    FULL JOIN unique_counts USING (TimeStamp, DeviceId, Phase)
)

--if variable return_volumes is False, return the combined table below
{% if return_volumes == False %}
SELECT c.*
FROM ped_combined c
JOIN inputs i ON i.DeviceId = c.DeviceId AND i.Phase = c.Phase
CROSS JOIN results_end e
WHERE c.TimeStamp >= i.Next_Bin
  AND c.TimeStamp < e.TimeStamp

--if variable return_volumes is True, return the volumes table below
{% else %}
,
last_bin AS (
    SELECT TIME_BUCKET(interval '{{bin_size}} minutes', TimeStamp - INTERVAL 1 MILLISECOND) AS TimeStamp
    FROM results_end
),

--Every known input in every bin from the first not written yet through the last this run writes, quiet bins included,
--starting 3 bins sooner for the rolling sum
all_times AS (
    SELECT
        UNNEST(generate_series(
            i.Next_Bin - INTERVAL '{{bin_size * 3}} minutes',
            l.TimeStamp,
            INTERVAL '{{bin_size}} minutes'
        )) AS TimeStamp,
        i.DeviceId,
        i.Phase,
        i.Next_Bin
    FROM inputs i, last_bin l
),
filled_in AS (
    SELECT
        a.TimeStamp,
        a.DeviceId,
        a.Phase,
        a.Next_Bin,
        COALESCE(c.PedServices, 0)::int16 AS PedServices,
        COALESCE(c.PedActuation, 0)::int16 AS PedActuation,
        COALESCE(c.Unique_Actuations, 0)::int16 AS Unique_Actuations
    FROM all_times a
    LEFT JOIN ped_combined c
        ON c.TimeStamp = a.TimeStamp AND c.DeviceId = a.DeviceId AND c.Phase = a.Phase
),

--A bin and the 3 before it, the same in every run because the 3 before it are carried. Looking at the 3 bins after it
--would leave the last 3 bins of a run reading low, with no way to fix them but changing a row already written.
add_rolling_sum AS (
    SELECT
        *,
        SUM(Unique_Actuations) OVER (
            PARTITION BY DeviceId, Phase
            ORDER BY TimeStamp
            ROWS BETWEEN 3 PRECEDING AND CURRENT ROW
        )::DOUBLE AS Unique_Actuations_Rolling
    FROM filled_in
),

--Transform to volumes
--Estimated_Hourly = A90C * Hourly_Actuations + A90C_squared * Hourly_Actuations^2 + Intercept
--where A90C = 0.7167, A90C_squared = 0.0599, Intercept = 1.1063
--See docs for explanation. NOTE: Need to write docs!
volumes AS (
    SELECT
        *,
        (0.7167 * Unique_Actuations_Rolling + 0.0599 * Unique_Actuations_Rolling^2)::FLOAT AS Estimated_Hourly,
        CASE WHEN Unique_Actuations_Rolling = 0 THEN 0 ELSE Unique_Actuations / Unique_Actuations_Rolling END AS Ratio
    FROM add_rolling_sum
)
SELECT
    TimeStamp,
    DeviceId,
    Phase,
    PedServices,
    PedActuation,
    Unique_Actuations,
    (Estimated_Hourly * Ratio + (1.1063 / 4))::FLOAT AS Estimated_Volumes
FROM volumes
WHERE TimeStamp >= Next_Bin

{% endif %}

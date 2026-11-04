--Arrival on Green
--Written in SQL for DuckDB. This is a jinja2 template, with variables inside curly braces.
--
--Each detector on event on an advance detector is projected to the vehicle's arrival at the stop bar. The
--arrival belongs to the bin it lands in, and is on green if the phase is green at that moment: from green
--begin (1) until yellow begin (8) or red clearance begin (10). A bin with vehicles and none on green reads 0.


WITH {% include 'arrival_phase_events.sql' %},

{% include 'arrival_projection.sql' %},

with_signal_state AS (
    SELECT *,
        LAST_VALUE(CASE WHEN EventId < 82 THEN EventId END IGNORE NULLS) OVER (
            PARTITION BY DeviceId, Phase ORDER BY TimeStamp, EventId
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS Signal_State
    FROM (
        SELECT TimeStamp, DeviceId, EventId, Phase FROM phase_events
        UNION ALL
        SELECT TimeStamp, DeviceId, 82::INT16 AS EventId, Phase FROM arrivals
    )
),

binned AS (
    SELECT time_bucket(INTERVAL '{{bin_size}} minutes', TimeStamp) AS Bin,
        DeviceId,
        Phase,
        Signal_State = 1 AS On_Green
    FROM with_signal_state
    WHERE EventId = 82
      AND TimeStamp < (SELECT TimeStamp FROM results_end)
)

SELECT Bin AS TimeStamp,
    DeviceId,
    Phase,
    COUNT(*)::INT16 AS Total_Actuations,
    (COUNT(*) FILTER (WHERE On_Green))::FLOAT / COUNT(*) AS Percent_AOG
FROM binned
GROUP BY Bin, DeviceId, Phase

--What left_turn_gaps passes to the next run through unmatched_events, see left_turn_gaps_stretches.sql for the codes.
--Written in SQL for DuckDB. This is a jinja2 template, with variables inside curly braces.

WITH {% include 'left_turn_gaps_stretches.sql' %},

-- A stretch not ended yet is left out: the next run works it out again, from where it began
held AS (
    SELECT p.Bin, p.DeviceId, (p.Phase + 100 * p.Tenths)::INT16 AS Parameter,
        SUM(p.Milliseconds)::BIGINT AS Milliseconds,
        SUM(p.Gaps)::BIGINT AS Gaps
    FROM bin_parts p
    JOIN device_ends e ON p.DeviceId = e.DeviceId
    WHERE p.Has_Ended AND p.Bin >= e.Next_Bin
    GROUP BY p.Bin, p.DeviceId, p.Phase, p.Tenths
)

SELECT e.Last_Event AS TimeStamp, l.DeviceId, 927::INT16 AS EventId, l.Detector AS Parameter, TRUE AS IsValid
FROM (
    SELECT DeviceId, Detector, ARG_MAX(Is_On, TimeStamp) AS Is_On
    FROM lane_events
    GROUP BY DeviceId, Detector
) l
JOIN device_ends e ON l.DeviceId = e.DeviceId
WHERE l.Is_On

UNION ALL

SELECT Start_Time, DeviceId, 928::INT16, Phase, TRUE
FROM stretches
WHERE End_Time IS NULL AND Is_Empty IS NOT NULL

UNION ALL

SELECT Bin + to_microseconds(Milliseconds), DeviceId, 929::INT16, Parameter, TRUE
FROM held
WHERE Milliseconds > 0

UNION ALL

SELECT Bin + to_microseconds(Gaps), DeviceId, 930::INT16, Parameter, TRUE
FROM held
WHERE Gaps > 0

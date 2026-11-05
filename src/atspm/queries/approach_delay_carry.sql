--What approach_delay passes to the next run through unmatched_events, see approach_delay_vehicles.sql for the codes.
--Written in SQL for DuckDB. This is a jinja2 template, with variables inside curly braces.
{% set max_wait_seconds = 360 %}

WITH {% include 'approach_delay_vehicles.sql' %},

open_bins AS (
    SELECT Bin, DeviceId, Phase,
        SUM(Delay_Milliseconds)::BIGINT AS Delay_Milliseconds,
        SUM(Queued_Half_Milliseconds)::BIGINT AS Queued_Half_Milliseconds
    FROM bin_parts
    GROUP BY Bin, DeviceId, Phase
    HAVING Bin + INTERVAL '{{bin_size}} minutes' + INTERVAL {{max_wait_seconds}} SECOND > (SELECT Last_Event FROM data_end)
),

-- From here on the phase events are all kept: no arrival still to come, or still waiting, can be earlier
phase_from AS (
    SELECT p.DeviceId, p.Phase,
        LEAST(e.Last_Event + to_milliseconds(COALESCE(r.Back_Milliseconds, 0)), w.First_Wait) AS From_Time
    FROM (SELECT DISTINCT DeviceId, Phase FROM phase_events) p
    CROSS JOIN data_end e
    LEFT JOIN (
        SELECT DeviceId, Phase,
        {%- if spillback_on_seconds %}
            LEAST(0, MIN(Offset_Milliseconds), MIN(Offset_Milliseconds - Longest_Offset)) AS Back_Milliseconds
        FROM detector_reach
        {%- else %}
            LEAST(0, MIN(Offset_Milliseconds)) AS Back_Milliseconds
        FROM advance_detectors
        {%- endif %}
        GROUP BY DeviceId, Phase
    ) r ON p.DeviceId = r.DeviceId AND p.Phase = r.Phase
    LEFT JOIN (
        SELECT DeviceId, Phase, MIN(TimeStamp) AS First_Wait
        FROM judged
        WHERE Waiting AND NOT Waiting_Under_Flag
        GROUP BY DeviceId, Phase
    ) w ON p.DeviceId = w.DeviceId AND p.Phase = w.Phase
),

-- Before that, only what the signal stood at: a red alone, or a green or yellow with its green and the red before it
phase_before AS (
    SELECT s.DeviceId, s.Phase, f.From_Time,
        ARG_MAX(s.EventId, (s.TimeStamp, s.EventId)) FILTER (WHERE s.TimeStamp < f.From_Time) AS State,
        MAX(s.TimeStamp) FILTER (WHERE s.TimeStamp < f.From_Time) AS State_Time,
        MAX(s.TimeStamp) FILTER (WHERE s.EventId = 1 AND s.TimeStamp < f.From_Time) AS Green_Time,
        ARG_MAX(s.Last_Red, s.TimeStamp) FILTER (WHERE s.EventId = 1 AND s.TimeStamp < f.From_Time) AS Red_Time
    FROM signal_stream s
    JOIN phase_from f
        ON s.DeviceId = f.DeviceId
       AND s.Phase = f.Phase
    WHERE s.EventId < 82
    GROUP BY s.DeviceId, s.Phase, f.From_Time
){%- if spillback_on_seconds %},

-- What each detector was doing when the data ended
detector_ends AS (
    SELECT *
    FROM detector_flags
    QUALIFY ROW_NUMBER() OVER (PARTITION BY DeviceId, Parameter ORDER BY TimeStamp DESC, Step DESC) = 1
),

flag_holders AS (
    SELECT d.*
    FROM detector_ends d, data_end e
    WHERE (d.Spillback OR d.Held_Long)
      AND (d.Still_On OR e.Last_Event IS NULL OR DATE_DIFF('millisecond', d.End_Time, e.Last_Event) < {{spillback_gap_seconds}} * 1000)
)
{%- endif %}

-- The sums. A row has the length of its bin in microseconds to hold one, and a bigger one takes more rows.
SELECT Bin + to_microseconds(LEAST(Sum_Units - Piece * Room, Room)) AS TimeStamp, DeviceId, 957::INT16 AS EventId, Parameter, TRUE AS IsValid
FROM (
    SELECT Bin, DeviceId, Phase::INT16 AS Parameter, Delay_Milliseconds AS Sum_Units, {{bin_size}} * 60000000 - 1 AS Room
    FROM open_bins
    UNION ALL
    SELECT Bin, DeviceId, (Phase + 1000)::INT16, Queued_Half_Milliseconds, {{bin_size}} * 60000000 - 1
    FROM open_bins
    WHERE Queued_Half_Milliseconds > 0
), LATERAL UNNEST(range(0, Sum_Units // Room + 1)) AS pieces(Piece)

UNION ALL

SELECT TimeStamp, DeviceId, 958::INT16, Phase::INT16, TRUE
FROM judged
WHERE Waiting AND NOT Waiting_Under_Flag

UNION ALL

SELECT p.TimeStamp, p.DeviceId, 958::INT16, (p.Phase + 1000 * p.EventId)::INT16, TRUE
FROM phase_events p
JOIN phase_before b
    ON p.DeviceId = b.DeviceId
   AND p.Phase = b.Phase
WHERE p.TimeStamp >= b.From_Time
   OR (p.TimeStamp = b.State_Time AND p.EventId = b.State)
   OR (b.State <> 10 AND ((p.EventId = 1 AND p.TimeStamp = b.Green_Time) OR (p.EventId = 10 AND p.TimeStamp = b.Red_Time)))
{%- if spillback_on_seconds %}

UNION ALL

SELECT TimeStamp, DeviceId, 959::INT16, Parameter, TRUE
FROM detector_ends
WHERE Still_On

UNION ALL

SELECT End_Time, DeviceId, 959::INT16, (Parameter + 1000)::INT16, TRUE
FROM flag_holders

UNION ALL

SELECT Flag_Time, DeviceId, 959::INT16, (Parameter + 2000)::INT16, TRUE
FROM (
    SELECT COALESCE(Flag_Start, Flag_Began, TimeStamp) AS Flag_Time, DeviceId, Parameter FROM flag_holders
    UNION
    SELECT Flag_Start, DeviceId, Detector FROM judged WHERE Waiting_Under_Flag
)

UNION ALL

SELECT On_Time, DeviceId, 959::INT16, (Detector + 3000)::INT16, TRUE
FROM judged
WHERE Waiting_Under_Flag AND Phase = First_Phase
{%- endif %}

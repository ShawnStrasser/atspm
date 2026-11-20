--What approach_delay passes to the next run through unmatched_events, see approach_delay_vehicles.sql for the codes.
--With queue_model, phase events also carry from the first vehicle the Queue Model needs again, or its green, or a green not
--ended.
--Written in SQL for DuckDB. This is a jinja2 template, with variables inside curly braces.
{% set max_wait_seconds = 360 %}

WITH {% include 'approach_delay_vehicles.sql' %},

{%- if queue_model %}
-- The on from which each detector's vehicles come back for the Queue Model: the stretch of a vehicle whose wait is not known
-- yet, or that waits whole
queue_carry_from AS (
    SELECT DeviceId, Detector, Phase, MIN(Stretch_On) AS Carry_On
    FROM (
        SELECT DeviceId, Detector, Phase, Stretch_On FROM queue_delays WHERE NOT Queue_Known
        UNION ALL
        SELECT DeviceId, Detector, Phase, Queue_Stretch_On FROM judged WHERE Queue_Pending
    )
    GROUP BY DeviceId, Detector, Phase
),

queue_carried AS (
    SELECT a.TimeStamp, a.On_Time, a.DeviceId, a.Detector, a.Phase, a.Spillback, a.Sat, a.Still_On, j.DeviceId IS NOT NULL AS Pending
    FROM arrivals a
    JOIN queue_carry_from f
        ON a.DeviceId = f.DeviceId
       AND a.Detector = f.Detector
       AND a.Phase = f.Phase
       AND a.On_Time >= f.Carry_On
    LEFT JOIN (SELECT DeviceId, Detector, Phase, On_Time FROM judged WHERE Queue_Pending) j
        ON a.DeviceId = j.DeviceId
       AND a.Detector = j.Detector
       AND a.Phase = j.Phase
       AND a.On_Time = j.On_Time
    WHERE a.Modelled AND NOT a.Unresolved
),

{%- endif %}
open_bins AS (
    SELECT Bin, DeviceId, Phase,
        SUM(Delay_Milliseconds)::BIGINT AS Delay_Milliseconds,
        SUM(Queued_Half_Milliseconds)::BIGINT AS Queued_Half_Milliseconds{% if queue_model %},
        SUM(Queue_Half_Milliseconds)::BIGINT AS Queue_Half_Milliseconds{% endif %}
    FROM bin_parts
    GROUP BY Bin, DeviceId, Phase
    HAVING Bin + INTERVAL '{{bin_size}} minutes' + INTERVAL {{max_wait_seconds}} SECOND > (SELECT Last_Event FROM data_end)
        OR (Bin, DeviceId, Phase) IN (SELECT (Bin, DeviceId, Phase) FROM held_bins)
),

-- From here on the phase events are all kept: no arrival still to come, or still waiting, can be earlier
phase_from AS (
    SELECT p.DeviceId, p.Phase,
        LEAST(e.Last_Event + to_milliseconds(COALESCE(r.Back_Milliseconds, 0)), w.First_Wait{% if queue_model %}, g.First_Queue{% endif %}) AS From_Time
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
        WHERE (Waiting AND NOT Waiting_Under_Flag) OR Held{% if queue_model %} OR Queue_Pending{% endif %}
        GROUP BY DeviceId, Phase
    ) w ON p.DeviceId = w.DeviceId AND p.Phase = w.Phase
    {%- if queue_model %}
    LEFT JOIN (
        -- The vehicles the Queue Model needs again, and their greens, and a green not ended that later vehicles may join
        SELECT DeviceId, Phase, MIN(LEAST(TimeStamp, On_Time, Green)) AS First_Queue
        FROM (
            SELECT c.DeviceId, c.Phase, c.TimeStamp, c.On_Time, q.Green
            FROM queue_carried c
            LEFT JOIN queue_delays q
                ON c.DeviceId = q.DeviceId
               AND c.Detector = q.Detector
               AND c.Phase = q.Phase
               AND c.On_Time = q.On_Time
            UNION ALL
            SELECT DeviceId, Phase, NULL, NULL, Green FROM queue_windows WHERE Service_End IS NULL
        )
        GROUP BY DeviceId, Phase
    ) g ON p.DeviceId = g.DeviceId AND p.Phase = g.Phase
    {%- endif %}
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

{%- if int_keys %}
SELECT x.TimeStamp, k.DeviceId, x.EventId, x.Parameter, x.IsValid
FROM (
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
    {%- if queue_model %}
    UNION ALL
    SELECT Bin, DeviceId, (Phase + 2000)::INT16, Queue_Half_Milliseconds, {{bin_size}} * 60000000 - 1
    FROM open_bins
    WHERE Queue_Half_Milliseconds > 0
    {%- endif %}
), LATERAL UNNEST(range(0, Sum_Units // Room + 1)) AS pieces(Piece)

UNION ALL

SELECT TimeStamp, DeviceId, 958::INT16, Phase::INT16, TRUE
FROM judged
WHERE Waiting AND NOT Waiting_Under_Flag AND NOT Held{% if queue_model %} AND NOT Queue_Pending{% endif %}
{%- if queue_model %}

UNION ALL

SELECT On_Time, DeviceId, 959::INT16,
    (Detector + 1000 * (5 + Spillback::INT + 2 * Sat::INT + 4 * Still_On::INT + 8 * (NOT Pending)::INT))::INT16, TRUE
FROM queue_carried
{%- endif %}

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

SELECT d.TimeStamp, d.DeviceId, 959::INT16, (d.Parameter + CASE WHEN h.DeviceId IS NULL THEN 0 ELSE 4000 END)::INT16, TRUE
FROM detector_ends d
LEFT JOIN held_vehicles h
    ON d.Unresolved
   AND d.DeviceId = h.DeviceId
   AND d.Parameter = h.Detector
WHERE d.Still_On

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
{%- if int_keys %}
) x
JOIN device_keys k ON k.Dev = x.DeviceId
{%- endif %}

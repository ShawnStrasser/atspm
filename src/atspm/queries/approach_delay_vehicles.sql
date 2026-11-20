{%- set approach_delay = true -%}
-- Included by approach_delay.sql and approach_delay_carry.sql. A list of CTEs, not a query.
--
-- A run with a main unmatched file carries, with a number as Parameter and 1000 times a kind added to it:
-- 957 a sum of a bin not written yet, stamped with its bin plus one microsecond for each unit, with the phase as number. Kind 0
--     Delay_Seconds in milliseconds, kind 1 Queued_Delay_Seconds in half milliseconds. A sum too big for its bin takes more rows.
-- 958 with the phase as number: kind 0 a vehicle waiting, at its arrival. Kinds 1, 8 and 10 the green, yellow and red it or a
--     later vehicle may need, that being the event's number.
-- 959 with the detector as number: kind 0 on at the end of the data, stamped when it turned on. Kind 1 holding the flag, stamped
--     when it last turned off or, if it is on, when it turned on. Kind 2 when a flag began. Kind 3 the on of a vehicle under the
--     flag that is not at every stop bar of its detector yet. Kind 4 in place of kind 0 when its vehicle is held, not counted
--     yet: it is not known whether it trips the flag, and that could change what it adds (see held_vehicles).
--     Kinds 5 and up, with queue_model: a vehicle the Queue Model needs again, as arrival_on_green's 965 (arrival_projection.sql)
--     with 5000 added: stamped at its on, the detector plus 1000 if flagged, 2000 if it sat, 4000 if its detector was still on,
--     8000 if it is counted already and here only for the queue.
-- 957 kind 2, with queue_model: Queue_Delay_Seconds in half milliseconds.
--
-- With queue_model, Queue_Delay_Seconds is each vehicle's wait by the Queue Model (arrival_queue.sql): its departure less its
-- arrival there, or, when service ends before its turn, the start of the next green's service less its arrival. A vehicle
-- under the Spillback Flag, at a detector the model leaves out (RedDepartures over 0.25), one the model drops or calls in an
-- impossible queue, or one that would wait more than the limit, takes Delay_Seconds plus Queued_Delay_Seconds instead. The
-- wait is from the model's own arrival (the on plus the travel time); the bin is the vehicle's, from UDOT's projection, as
-- for the other columns. Measured against the stop bar count zones at 438 ODOT advance detectors, it took the error in a
-- 15-minute bin's delay from 39.8% to 20.1% (see the README).
-- A vehicle the model stands by whose wait, or whose UDOT delay, is not known yet is carried whole (Queue_Pending), and its
-- bin waits; so does every vehicle whose detector is still on and may yet be under the flag.
{% include 'arrival_phase_events.sql' %},
{%- if unmatched %}

-- What an earlier run of approach_delay kept, with the signals numbered (see arrival_phase_events.sql)
previous AS (
    SELECT TimeStamp, {% if int_keys %}Dev AS DeviceId{% else %}DeviceId{% endif %}, EventId, Parameter
    FROM unmatched_previous{% if int_keys %} JOIN device_keys USING (DeviceId){% endif %}
    WHERE EventId BETWEEN 957 AND 959
),
{%- endif %}
{%- if unmatched and spillback_on_seconds %}

detector_state AS (
    SELECT TimeStamp, DeviceId, Parameter // 1000 AS Kind, (Parameter % 1000)::INT16 AS Detector
    FROM previous
    WHERE EventId = 959
),
{%- endif %}

{% include 'arrival_projection.sql' %},

data_end AS (SELECT MAX(TimeStamp) AS Last_Event FROM {{from_table}}),
{%- if queue_model %}

{% include 'arrival_queue.sql' %},

queue_next AS (
    SELECT DeviceId, Phase, Green, LEAD(Service_Start) OVER (PARTITION BY DeviceId, Phase ORDER BY Green) AS Next_Service_Start
    FROM queue_windows
),

-- Each modelled vehicle's wait in milliseconds, NULL where the model does not stand by it. Queue_Known: known for good.
queue_delays AS (
    SELECT q.DeviceId, q.Detector, q.Phase, q.On_Time, q.Stretch_On, q.Green,
        CASE WHEN NOT q.Queue_Usable THEN NULL
             WHEN q.Service_End IS NULL OR q.Departure_Ms <= epoch_ms(q.Service_End) THEN q.Departure_Ms - epoch_ms(q.Queue_Time)
             ELSE epoch_ms(n.Next_Service_Start) - epoch_ms(q.Queue_Time) END AS Queue_Wait_Ms,
        q.Final AND NOT COALESCE(q.Queue_Usable AND q.Departure_Ms > epoch_ms(q.Service_End) AND n.Next_Service_Start IS NULL
                                 AND q.Queue_Time + INTERVAL {{max_wait_seconds}} SECOND > e.Last_Event, FALSE) AS Queue_Known
    FROM queue_results q
    LEFT JOIN queue_next n
        ON q.DeviceId = n.DeviceId
       AND q.Phase = n.Phase
       AND q.Green = n.Green
    CROSS JOIN data_end e
),
{%- endif %}
{%- if spillback_on_seconds %}

-- A detector on two phases can reach one stop bar before the other. Its vehicle under the flag waits for the later one, and is
-- saved once, so that each phase counts it once.
detector_reach AS (
    SELECT *,
        MAX(Offset_Milliseconds) OVER (PARTITION BY DeviceId, Parameter) AS Longest_Offset,
        MIN(Phase) OVER (PARTITION BY DeviceId, Parameter) AS First_Phase
    FROM advance_detectors
),
{%- endif %}

all_arrivals AS (
    SELECT TimeStamp, DeviceId, Phase, Spillback, Flag_Start, On_Time, Detector, Unresolved FROM arrivals{% if queue_model %} WHERE Counted{% endif %}
    {%- if unmatched %}
    UNION ALL
    SELECT TimeStamp, DeviceId, Parameter::INT16, FALSE, NULL, NULL, NULL, FALSE
    FROM previous
    WHERE EventId = 958 AND Parameter < 1000
    {%- if spillback_on_seconds %}
    UNION ALL
    SELECT h.TimeStamp + to_milliseconds(c.Offset_Milliseconds), h.DeviceId, c.Phase, TRUE, b.TimeStamp, h.TimeStamp, h.Detector, FALSE
    FROM (SELECT * FROM detector_state WHERE Kind = 3) h
    JOIN advance_detectors c
        ON h.DeviceId = c.DeviceId
       AND h.Detector = c.Parameter
    ASOF LEFT JOIN (SELECT * FROM detector_state WHERE Kind = 2) b
        ON h.DeviceId = b.DeviceId
       AND h.Detector = b.Detector
       AND h.TimeStamp >= b.TimeStamp
    {%- endif %}
    {%- if not queue_model %}
    -- A state from a run with the queue model on: a vehicle it carried whole counts as it arrives
    UNION ALL
    SELECT q.TimeStamp + to_milliseconds(c.Offset_Milliseconds), q.DeviceId, c.Phase, ((q.Parameter - 5000) // 1000) % 2 = 1, NULL, q.TimeStamp,
        (q.Parameter % 1000)::INT16, FALSE
    FROM previous q
    JOIN advance_detectors c
        ON q.DeviceId = c.DeviceId
       AND q.Parameter % 1000 = c.Parameter
    WHERE q.EventId = 959 AND q.Parameter >= 5000 AND ((q.Parameter - 5000) // 8000) % 2 = 0
    {%- endif %}
    {%- endif %}
),

signal_stream AS (
    SELECT *,
        LAST_VALUE(CASE WHEN EventId = 10 THEN TimeStamp END IGNORE NULLS) OVER (
            PARTITION BY DeviceId, Phase ORDER BY TimeStamp, EventId
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS Last_Red
    FROM (
        SELECT TimeStamp, DeviceId, Phase, EventId, FALSE AS Spillback, NULL::TIMESTAMP AS Flag_Start, NULL::TIMESTAMP AS On_Time, NULL::INT16 AS Detector,
            FALSE AS Unresolved
        FROM phase_events
        UNION ALL
        SELECT TimeStamp, DeviceId, Phase, 82::INT16, Spillback, Flag_Start, On_Time, Detector, Unresolved
        FROM all_arrivals
    )
),

-- Each vehicle with the signal as it stood: the green before it, the red before that green, and the green after it
vehicles AS (
    SELECT *
    FROM (
        SELECT *,
            LAST_VALUE(CASE WHEN EventId < 82 THEN EventId END IGNORE NULLS) OVER up_to_here AS Signal_State,
            LAST_VALUE(CASE WHEN EventId = 1 THEN TimeStamp END IGNORE NULLS) OVER up_to_here AS Last_Green,
            LAST_VALUE(CASE WHEN EventId = 1 THEN Last_Red END IGNORE NULLS) OVER up_to_here AS Red_Before_Green,
            FIRST_VALUE(CASE WHEN EventId = 1 THEN TimeStamp END IGNORE NULLS) OVER (
                PARTITION BY DeviceId, Phase ORDER BY TimeStamp, EventId
                ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING
            ) AS Next_Green
        FROM signal_stream
        WINDOW up_to_here AS (
            PARTITION BY DeviceId, Phase ORDER BY TimeStamp, EventId
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        )
    )
    WHERE EventId = 82
),

-- A vehicle whose detector is still on, and not under the flag, trips the flag if its off comes more than spillback_on_seconds
-- after its on. Its delay is measured either way, but under the flag on green or yellow it gets the estimate, from its own on,
-- which is more than 0 only if a green began after its on. So it is held, on every phase of its detector, where it lands after
-- the data end or on green or yellow after a green that began after its on, until that is known (see arrival_spillback.sql).
-- Its bin waits too.
held_vehicles AS (
    {%- if carry_bins and spillback_on_seconds %}
    SELECT v.DeviceId, v.Detector
    FROM vehicles v
    CROSS JOIN data_end e
    WHERE v.Unresolved
    GROUP BY v.DeviceId, v.Detector
    HAVING {% if queue_model %}TRUE{% else %}BOOL_OR(v.TimeStamp > e.Last_Event OR (v.Signal_State IN (1, 8) AND v.Last_Green > v.On_Time)){% endif %}
    {%- else %}
    SELECT DeviceId, Detector FROM vehicles WHERE FALSE
    {%- endif %}
),

-- Waiting: not at the stop bar yet, or on red with no green so far and less than the limit gone by.
judged AS (
    SELECT v.*,
        time_bucket(INTERVAL '{{bin_size}} minutes', v.TimeStamp) AS Bin,
        h.DeviceId IS NOT NULL AS Held,
        {%- if carry_bins %}
        {%- if spillback_on_seconds %}
        v.Spillback AND v.On_Time + to_milliseconds(r.Longest_Offset) > e.Last_Event AS Waiting_Under_Flag,
        r.First_Phase,
        {%- else %}
        FALSE AS Waiting_Under_Flag,
        {%- endif %}
        v.TimeStamp > e.Last_Event
            OR COALESCE(v.Signal_State = 10 AND v.Next_Green IS NULL AND v.TimeStamp + INTERVAL {{max_wait_seconds}} SECOND > e.Last_Event, FALSE) AS Waiting,
        {%- else %}
        FALSE AS Waiting_Under_Flag,
        FALSE AS Waiting,
        {%- endif %}
        CASE WHEN v.Signal_State = 10 AND DATE_DIFF('millisecond', v.TimeStamp, v.Next_Green) <= {{max_wait_seconds}} * 1000
             THEN DATE_DIFF('millisecond', v.TimeStamp, v.Next_Green) ELSE 0 END AS Delay_Milliseconds,
        -- Half milliseconds, the time from flag or red to green being halved
        CASE WHEN v.Spillback AND v.Signal_State IN (1, 8)
                  AND DATE_DIFF('millisecond', GREATEST(v.Flag_Start, v.Red_Before_Green), v.Last_Green) <= 2 * {{max_wait_seconds}} * 1000
             THEN GREATEST(DATE_DIFF('millisecond', GREATEST(v.Flag_Start, v.Red_Before_Green), v.Last_Green), 0) ELSE 0 END AS Queued_Half_Milliseconds{% if queue_model %},
        q.Stretch_On AS Queue_Stretch_On,
        {%- if carry_bins %}
        q.DeviceId IS NOT NULL AND NOT v.Spillback AND NOT Held AND (NOT q.Queue_Known OR Waiting) AS Queue_Pending,
        {%- else %}
        FALSE AS Queue_Pending,
        {%- endif %}
        CASE WHEN NOT v.Spillback AND q.Queue_Wait_Ms <= {{max_wait_seconds}} * 1000 THEN ROUND(2 * GREATEST(q.Queue_Wait_Ms, 0))::BIGINT
             ELSE 2 * Delay_Milliseconds + Queued_Half_Milliseconds END AS Queue_Half_Milliseconds{% endif %}
    FROM vehicles v
    CROSS JOIN data_end e
    LEFT JOIN held_vehicles h
        ON v.Unresolved
       AND v.DeviceId = h.DeviceId
       AND v.Detector = h.Detector
    {%- if queue_model %}
    LEFT JOIN queue_delays q
        ON v.DeviceId = q.DeviceId
       AND v.Detector = q.Detector
       AND v.Phase = q.Phase
       AND v.On_Time = q.On_Time
    {%- endif %}
    {%- if carry_bins and spillback_on_seconds %}
    LEFT JOIN detector_reach r
        ON v.DeviceId = r.DeviceId
       AND v.Detector = r.Parameter
       AND v.Phase = r.Phase
    {%- endif %}
),

-- The bins a held vehicle lands in wait for it, whatever the time
held_bins AS (
    SELECT DISTINCT Bin, DeviceId, Phase FROM judged WHERE Held{% if queue_model %} OR Queue_Pending{% endif %}
),

-- What each bin has so far: the vehicles settled in this run and the sums carried from earlier ones
bin_parts AS (
    SELECT Bin, DeviceId, Phase, Delay_Milliseconds, Queued_Half_Milliseconds{% if queue_model %}, Queue_Half_Milliseconds{% endif %}
    FROM judged
    {%- if carry_bins %}
    WHERE NOT Waiting_Under_Flag AND NOT Waiting AND NOT Held{% if queue_model %} AND NOT Queue_Pending{% endif %}
    {%- else %}
    WHERE TimeStamp < (SELECT TimeStamp FROM results_end)
    {%- endif %}
    {%- if unmatched %}
    UNION ALL
    SELECT time_bucket(INTERVAL '{{bin_size}} minutes', TimeStamp),
        DeviceId,
        (Parameter % 1000)::INT16,
        CASE WHEN Parameter < 1000 THEN DATE_DIFF('microsecond', time_bucket(INTERVAL '{{bin_size}} minutes', TimeStamp), TimeStamp) ELSE 0 END,
        CASE WHEN Parameter // 1000 = 1 THEN DATE_DIFF('microsecond', time_bucket(INTERVAL '{{bin_size}} minutes', TimeStamp), TimeStamp) ELSE 0 END{% if queue_model %},
        CASE WHEN Parameter // 1000 = 2 THEN DATE_DIFF('microsecond', time_bucket(INTERVAL '{{bin_size}} minutes', TimeStamp), TimeStamp) ELSE 0 END{% endif %}
    FROM previous
    WHERE EventId = 957
    {%- endif %}
)

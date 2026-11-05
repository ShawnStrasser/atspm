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
--     flag that is not at every stop bar of its detector yet.
{% include 'arrival_phase_events.sql' %},
{%- if unmatched and spillback_on_seconds %}

detector_state AS (
    SELECT TimeStamp, DeviceId, Parameter // 1000 AS Kind, (Parameter % 1000)::INT16 AS Detector
    FROM unmatched_previous
    WHERE EventId = 959
),
{%- endif %}

{% include 'arrival_projection.sql' %},

data_end AS (SELECT MAX(TimeStamp) AS Last_Event FROM {{from_table}}),
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
    SELECT TimeStamp, DeviceId, Phase, Spillback, Flag_Start, On_Time, Detector FROM arrivals
    {%- if unmatched %}
    UNION ALL
    SELECT TimeStamp, DeviceId, Parameter::INT16, FALSE, NULL, NULL, NULL
    FROM unmatched_previous
    WHERE EventId = 958 AND Parameter < 1000
    {%- if spillback_on_seconds %}
    UNION ALL
    SELECT h.TimeStamp + to_milliseconds(c.Offset_Milliseconds), h.DeviceId, c.Phase, TRUE, b.TimeStamp, h.TimeStamp, h.Detector
    FROM (SELECT * FROM detector_state WHERE Kind = 3) h
    JOIN advance_detectors c
        ON h.DeviceId = c.DeviceId
       AND h.Detector = c.Parameter
    ASOF LEFT JOIN (SELECT * FROM detector_state WHERE Kind = 2) b
        ON h.DeviceId = b.DeviceId
       AND h.Detector = b.Detector
       AND h.TimeStamp >= b.TimeStamp
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
        SELECT TimeStamp, DeviceId, Phase, EventId, FALSE AS Spillback, NULL::TIMESTAMP AS Flag_Start, NULL::TIMESTAMP AS On_Time, NULL::INT16 AS Detector
        FROM phase_events
        UNION ALL
        SELECT TimeStamp, DeviceId, Phase, 82::INT16, Spillback, Flag_Start, On_Time, Detector
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

-- Waiting: not at the stop bar yet, or on red with no green so far and less than the limit gone by.
judged AS (
    SELECT v.*,
        time_bucket(INTERVAL '{{bin_size}} minutes', v.TimeStamp) AS Bin,
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
             THEN GREATEST(DATE_DIFF('millisecond', GREATEST(v.Flag_Start, v.Red_Before_Green), v.Last_Green), 0) ELSE 0 END AS Queued_Half_Milliseconds
    FROM vehicles v
    CROSS JOIN data_end e
    {%- if carry_bins and spillback_on_seconds %}
    LEFT JOIN detector_reach r
        ON v.DeviceId = r.DeviceId
       AND v.Detector = r.Parameter
       AND v.Phase = r.Phase
    {%- endif %}
),

-- What each bin has so far: the vehicles settled in this run and the sums carried from earlier ones
bin_parts AS (
    SELECT Bin, DeviceId, Phase, Delay_Milliseconds, Queued_Half_Milliseconds
    FROM judged
    {%- if carry_bins %}
    WHERE NOT Waiting_Under_Flag AND NOT Waiting
    {%- else %}
    WHERE TimeStamp < (SELECT TimeStamp FROM results_end)
    {%- endif %}
    {%- if unmatched %}
    UNION ALL
    SELECT time_bucket(INTERVAL '{{bin_size}} minutes', TimeStamp),
        DeviceId,
        (Parameter % 1000)::INT16,
        CASE WHEN Parameter < 1000 THEN DATE_DIFF('microsecond', time_bucket(INTERVAL '{{bin_size}} minutes', TimeStamp), TimeStamp) ELSE 0 END,
        CASE WHEN Parameter >= 1000 THEN DATE_DIFF('microsecond', time_bucket(INTERVAL '{{bin_size}} minutes', TimeStamp), TimeStamp) ELSE 0 END
    FROM unmatched_previous
    WHERE EventId = 957
    {%- endif %}
)

-- Included by arrival_on_green.sql, arrival_on_green_carry.sql and approach_delay_vehicles.sql. A list of CTEs, not a query.
{% include 'advance_detectors.sql' %},
{%- if spillback_on_seconds %}

{% include 'arrival_spillback.sql' %},
{%- endif %}

-- One row per vehicle per phase at its time of arrival at the stop bar, plus the ones an earlier run held because
-- they arrive after its results stopped (already projected, with the phase as Parameter: 938, or 948 if flagged).
-- Spillback says the vehicle counts as arriving on red whatever the phase shows. Unresolved says its detector is still on and
-- it may yet turn out to be under the flag. approach_delay adds its own held vehicles later.
{%- if queue_model %}
-- With the queue model (arrival_queue.sql), each vehicle also has its on, its arrival for the model (Queue_Time), whether it
-- sat on the detector more than spillback_on_seconds (Sat) and whether its detector was still on when the data ended
-- (Still_On). An earlier run's vehicles come back as 964 (938 the model queued) and as 965, a vehicle in a queue the model
-- had not finished: stamped at its on, with the detector as Parameter, plus 1000 if flagged, 2000 if it sat, 4000 if its
-- detector was still on (this run sees its off and so whether it sat), and 8000 if it was counted already and is here only
-- for the queue. Modelled: in the queue model (not at a detector advance_detectors.sql leaves out). Counted: counted here.
-- approach_delay keeps its own as 959 with 5000 added to that Parameter (see approach_delay_vehicles.sql).
{%- endif %}
arrivals AS (
    SELECT d.TimeStamp + to_milliseconds(c.Offset_Milliseconds) AS TimeStamp,
        c.DeviceId,
        c.Phase,
        {%- if spillback_on_seconds %}
        d.Spillback,
        d.Unresolved,
        d.Parameter AS Detector{% if approach_delay %},
        d.Flag_Start,
        d.TimeStamp AS On_Time{% endif %}{% if queue_model %},{% if not approach_delay %}
        d.TimeStamp AS On_Time,{% endif %}
        d.TimeStamp + to_milliseconds(c.Queue_Offset_Milliseconds) AS Queue_Time,
        d.Held_Long OR (d.Still_On AND DATE_DIFF('millisecond', d.TimeStamp, (SELECT MAX(TimeStamp) FROM {{from_table}})) > {{spillback_on_seconds}} * 1000) AS Sat,
        d.Still_On,
        c.Queue_Model AS Modelled,
        TRUE AS Counted,
        NULL::BOOLEAN AS Queued_Before{% endif %}
    FROM detector_flags d
    JOIN advance_detectors c
        ON d.DeviceId = c.DeviceId
       AND d.Parameter = c.Parameter
    WHERE d.Is_New
    {%- else %}
        FALSE AS Spillback,
        FALSE AS Unresolved,
        NULL::INT16 AS Detector{% if approach_delay %},
        NULL::TIMESTAMP AS Flag_Start,
        NULL::TIMESTAMP AS On_Time{% endif %}
    FROM {{from_table}} d
    {%- if int_keys %}
    JOIN device_keys k ON d.DeviceId = k.DeviceId
    {%- endif %}
    JOIN advance_detectors c
        ON {% if int_keys %}k.Dev{% else %}d.DeviceId{% endif %} = c.DeviceId
       AND d.Parameter = c.Parameter
    WHERE d.EventId = 82
    {%- endif %}
    {%- if unmatched and not approach_delay %}
    UNION ALL
    SELECT TimeStamp, {% if int_keys %}Dev{% else %}DeviceId{% endif %} AS DeviceId, Parameter::INT16 AS Phase, EventId = 948{% if not queue_model %} OR EventId = 964{% endif %} AS Spillback, FALSE AS Unresolved, NULL::INT16 AS Detector{% if queue_model %},
        NULL::TIMESTAMP AS On_Time, NULL::TIMESTAMP AS Queue_Time, FALSE AS Sat, FALSE AS Still_On, FALSE AS Modelled, TRUE AS Counted,
        EventId = 964 AS Queued_Before{% endif %}
    FROM unmatched_previous{% if int_keys %} JOIN device_keys USING (DeviceId){% endif %}
    WHERE EventId IN (938, 948, 964)
    {%- if not queue_model %}
    -- A state from a run with the queue model on: a 964 was queued, so on red. A 965 not counted yet counts as it arrives.
    UNION ALL
    SELECT q.TimeStamp + to_milliseconds(c.Offset_Milliseconds), c.DeviceId, c.Phase, (q.Parameter // 1000) % 2 = 1, FALSE, (q.Parameter % 1000)::INT16
    FROM unmatched_previous q
    {%- if int_keys %}
    JOIN device_keys k ON q.DeviceId = k.DeviceId
    {%- endif %}
    JOIN advance_detectors c
        ON {% if int_keys %}k.Dev{% else %}q.DeviceId{% endif %} = c.DeviceId
       AND q.Parameter % 1000 = c.Parameter
    WHERE q.EventId = 965 AND (q.Parameter // 8000) % 2 = 0
    {%- endif %}
    {%- endif %}
    {%- if unmatched and queue_model %}
    UNION ALL
    SELECT q.TimeStamp + to_milliseconds(c.Offset_Milliseconds),
        c.DeviceId,
        c.Phase,
        (q.Parameter // 1000) % 2 = 1,
        FALSE,
        (q.Parameter % 1000)::INT16,{% if approach_delay %}
        NULL::TIMESTAMP,{% endif %}
        q.TimeStamp,
        q.TimeStamp + to_milliseconds(c.Queue_Offset_Milliseconds),
        (q.Parameter // 2000) % 2 = 1
            OR COALESCE(f.Held_Long OR (f.Still_On AND DATE_DIFF('millisecond', f.TimeStamp, (SELECT MAX(TimeStamp) FROM {{from_table}})) > {{spillback_on_seconds}} * 1000), FALSE),
        COALESCE(f.Still_On, FALSE),
        TRUE,
        (q.Parameter // 8000) % 2 = 0,
        NULL::BOOLEAN
    FROM {% if approach_delay %}(SELECT TimeStamp, DeviceId, Parameter - 5000 AS Parameter FROM unmatched_previous WHERE EventId = 959 AND Parameter >= 5000){% else %}unmatched_previous{% endif %} q
    {%- if int_keys %}
    JOIN device_keys k ON q.DeviceId = k.DeviceId
    {%- endif %}
    JOIN advance_detectors c
        ON {% if int_keys %}k.Dev{% else %}q.DeviceId{% endif %} = c.DeviceId
       AND q.Parameter % 1000 = c.Parameter
    LEFT JOIN detector_flags f
        ON (q.Parameter // 4000) % 2 = 1
       AND c.DeviceId = f.DeviceId
       AND q.Parameter % 1000 = f.Parameter
       AND q.TimeStamp = f.TimeStamp
       AND f.Step = 2
       AND NOT f.Is_New
    {%- if not approach_delay %}
    WHERE q.EventId = 965
    {%- endif %}
    {%- endif %}
)

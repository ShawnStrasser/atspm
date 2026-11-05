-- Included by arrival_on_green.sql, arrival_on_green_carry.sql and approach_delay_vehicles.sql. A list of CTEs, not a query.
{% include 'advance_detectors.sql' %},
{%- if spillback_on_seconds %}

{% include 'arrival_spillback.sql' %},
{%- endif %}

-- One row per vehicle per phase at its time of arrival at the stop bar, plus the ones an earlier run held because
-- they arrive after its results stopped (already projected, with the phase as Parameter: 938, or 948 if flagged).
-- Spillback says the vehicle counts as arriving on red whatever the phase shows. approach_delay adds its own held vehicles later.
arrivals AS (
    SELECT d.TimeStamp + to_milliseconds(c.Offset_Milliseconds) AS TimeStamp,
        d.DeviceId,
        c.Phase,
        {%- if spillback_on_seconds %}
        d.Spillback{% if approach_delay %},
        d.Flag_Start,
        d.TimeStamp AS On_Time,
        d.Parameter AS Detector{% endif %}
    FROM detector_flags d
    JOIN advance_detectors c
        ON d.DeviceId = c.DeviceId
       AND d.Parameter = c.Parameter
    WHERE d.Is_New
    {%- else %}
        FALSE AS Spillback{% if approach_delay %},
        NULL::TIMESTAMP AS Flag_Start,
        NULL::TIMESTAMP AS On_Time,
        NULL::INT16 AS Detector{% endif %}
    FROM {{from_table}} d
    JOIN advance_detectors c
        ON d.DeviceId = c.DeviceId
       AND d.Parameter = c.Parameter
    WHERE d.EventId = 82
    {%- endif %}
    {%- if unmatched and not approach_delay %}
    UNION ALL
    SELECT TimeStamp, DeviceId, Parameter::INT16 AS Phase, EventId = 948 AS Spillback
    FROM unmatched_previous
    WHERE EventId IN (938, 948)
    {%- endif %}
)

-- Included by arrival_on_green.sql, arrival_on_green_carry.sql, platoon_ratio.sql and approach_delay_vehicles.sql. A list of CTEs, not a query.
--
{% include 'results_end.sql' %},

-- Green begin (1), yellow begin (8) and red clearance begin (10) for phases with an advance detector, plus the
-- ones an earlier run kept: 939 green began (or the phase was green when results stopped), 940 yellow began,
-- 941 red clearance began. approach_delay keeps its own, and reads end of yellow (9) as the start of red too, since not every
-- signal logs 10. Where one logs both they carry the same stamp, measured on 445,738 cycles, and count as one.
phase_events AS (
    SELECT {% if approach_delay %}DISTINCT {% endif %}r.TimeStamp,
        r.DeviceId,
        {%- if approach_delay %}
        (CASE r.EventId WHEN 9 THEN 10 ELSE r.EventId END)::INT16 AS EventId,
        {%- else %}
        r.EventId::INT16 AS EventId,
        {%- endif %}
        r.Parameter::INT16 AS Phase
    FROM {{from_table}} r
    JOIN (SELECT DISTINCT DeviceId, Phase
          FROM detector_config
          WHERE Function = 'Advance') c
        ON r.DeviceId = c.DeviceId
       AND c.Phase = r.Parameter
    WHERE r.EventId IN (1, 8, {% if approach_delay %}9, {% endif %}10)
    {%- if unmatched and approach_delay %}
    UNION ALL
    SELECT TimeStamp, DeviceId, (Parameter // 1000)::INT16, (Parameter % 1000)::INT16
    FROM unmatched_previous
    WHERE EventId = 958 AND Parameter >= 1000
    {%- elif unmatched %}
    UNION ALL
    SELECT TimeStamp,
        DeviceId,
        (CASE EventId WHEN 939 THEN 1 WHEN 940 THEN 8 ELSE 10 END)::INT16 AS EventId,
        Parameter::INT16 AS Phase
    FROM unmatched_previous
    WHERE EventId IN (939, 940, 941)
    {%- endif %}
)

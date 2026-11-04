-- Included by arrival_on_green.sql, arrival_on_green_carry.sql and platoon_ratio.sql. A list of CTEs, not a query.
--
{% include 'results_end.sql' %},

-- Green begin (1), yellow begin (8) and red clearance begin (10) for phases with an advance detector, plus the
-- ones an earlier run kept: 939 green began (or the phase was green when results stopped), 940 yellow began,
-- 941 red clearance began.
phase_events AS (
    SELECT r.TimeStamp,
        r.DeviceId,
        r.EventId::INT16 AS EventId,
        r.Parameter::INT16 AS Phase
    FROM {{from_table}} r
    JOIN (SELECT DISTINCT DeviceId, Phase
          FROM detector_config
          WHERE Function = 'Advance') c
        ON r.DeviceId = c.DeviceId
       AND c.Phase = r.Parameter
    WHERE r.EventId IN (1, 8, 10)
    {%- if unmatched %}
    UNION ALL
    SELECT TimeStamp,
        DeviceId,
        (CASE EventId WHEN 939 THEN 1 WHEN 940 THEN 8 ELSE 10 END)::INT16 AS EventId,
        Parameter::INT16 AS Phase
    FROM unmatched_previous
    WHERE EventId IN (939, 940, 941)
    {%- endif %}
)

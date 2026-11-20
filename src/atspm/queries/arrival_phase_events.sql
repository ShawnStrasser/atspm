-- Included by arrival_on_green.sql, arrival_on_green_carry.sql, platoon_ratio.sql and approach_delay_vehicles.sql. A list of CTEs, not a query.
--
{% include 'results_end.sql' %},
{%- if int_keys %}

-- Each signal as a number for arrival_on_green's and approach_delay's chains: sorts and joins on it are cheaper than on the DeviceId text.
-- Mapped back to the DeviceId where results are written. Signals an earlier run carried are numbered too.
device_keys AS (
    SELECT DeviceId, (ROW_NUMBER() OVER (ORDER BY DeviceId))::INT AS Dev
    FROM (SELECT DISTINCT DeviceId FROM detector_config WHERE Function = 'Advance'
          {%- if unmatched %}
          UNION SELECT DeviceId FROM unmatched_previous
          {%- endif %})
),
{%- endif %}

-- Green begin (1), yellow begin (8) and red clearance begin (10) for phases with an advance detector, plus the
-- ones an earlier run kept: 939 green began (or the phase was green when results stopped), 940 yellow began,
-- 941 red clearance began. approach_delay keeps its own, and reads end of yellow (9) as the start of red too, since not every
-- signal logs 10. Where one logs both they carry the same stamp, measured on 445,738 cycles, and count as one.
phase_events AS (
    SELECT {% if approach_delay %}DISTINCT {% endif %}r.TimeStamp,
        {% if int_keys %}c.Dev{% else %}r.DeviceId{% endif %} AS DeviceId,
        {%- if approach_delay %}
        (CASE r.EventId WHEN 9 THEN 10 ELSE r.EventId END)::INT16 AS EventId,
        {%- else %}
        r.EventId::INT16 AS EventId,
        {%- endif %}
        r.Parameter::INT16 AS Phase
    FROM {{from_table}} r
    JOIN (SELECT DISTINCT DeviceId, Phase{% if int_keys %}, Dev{% endif %}
          FROM detector_config{% if int_keys %} JOIN device_keys USING (DeviceId){% endif %}
          WHERE Function = 'Advance') c
        ON r.DeviceId = c.DeviceId
       AND c.Phase = r.Parameter
    WHERE r.EventId IN (1, 8, {% if approach_delay %}9, {% endif %}10)
    {%- if unmatched and approach_delay %}
    UNION ALL
    SELECT TimeStamp, {% if int_keys %}Dev AS DeviceId{% else %}DeviceId{% endif %}, (Parameter // 1000)::INT16, (Parameter % 1000)::INT16
    FROM unmatched_previous{% if int_keys %} JOIN device_keys USING (DeviceId){% endif %}
    WHERE EventId = 958 AND Parameter >= 1000
    {%- elif unmatched %}
    UNION ALL
    SELECT TimeStamp,
        {% if int_keys %}Dev{% else %}DeviceId{% endif %} AS DeviceId,
        (CASE EventId WHEN 939 THEN 1 WHEN 940 THEN 8 ELSE 10 END)::INT16 AS EventId,
        Parameter::INT16 AS Phase
    FROM unmatched_previous{% if int_keys %} JOIN device_keys USING (DeviceId){% endif %}
    WHERE EventId IN (939, 940, 941)
    {%- endif %}
)

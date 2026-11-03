-- Included by arrival_on_green.sql, arrival_on_green_carry.sql and platoon_ratio.sql. A list of CTEs, not a query.
--
-- results_end: results stop here, and anything from here on waits for a later run. A bin is only written
-- once all of it is known, by the first run whose data reaches its end, so one bin is one row wherever runs
-- are cut. A run whose last event is within a second of the end of a bin reached the end of that bin. Otherwise
-- the bin it stopped in waits for the next run. A run with nothing after it (no unmatched events file) writes
-- all it has instead.
results_end AS (
    SELECT CASE
        WHEN Bin_Start + INTERVAL '{{bin_size}} minutes' - Last_Event <= INTERVAL 1 SECOND
            THEN Bin_Start + INTERVAL '{{bin_size}} minutes'
        {%- if carry_arrivals %}
        ELSE Bin_Start
        {%- else %}
        ELSE Last_Event + INTERVAL 1 MILLISECOND
        {%- endif %}
    END AS TimeStamp
    FROM (
        SELECT MAX(TimeStamp) AS Last_Event,
            time_bucket(INTERVAL '{{bin_size}} minutes', MAX(TimeStamp)) AS Bin_Start
        FROM raw_data
    )
),

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

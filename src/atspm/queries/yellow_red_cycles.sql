-- Included by red_offsets.sql and yellow_red_carry.sql, for yellow_red or arrival_pattern. A list of CTEs, not a query.
--
-- A cycle runs from one green begin to the next. Each actuation belongs to its cycle and its row to the bin of the
-- cycle's red begin, so a bin is only known once the last cycle with a red in it has finished. The cycles an earlier
-- run could not finish come back here (951 green, 952 yellow, 953 red clearance, 954 actuation, already moved back by
-- the latency, all with the phase as Parameter). arrival_pattern reads advance detectors, each on event moved forward
-- to its arrival at the stop bar, and keeps its own rows: 960 green, 961 yellow, 962 red clearance, 963 arrival.
{%- set first_code = 960 if arrival_pattern else 951 %}
{%- set detector_function = 'Advance' if arrival_pattern else 'Yellow_Red' %}
{%- if arrival_pattern %}
{% include 'advance_detectors.sql' %},
{%- endif %}
{%- if carry_bins %}
{% include 'results_end.sql' %},

-- The on events still to come are later than this once moved by the latency or the trip, so a cycle ending by then is whole.
known_events AS (
    {%- if arrival_pattern %}
    SELECT MAX(TimeStamp) + to_milliseconds(LEAST((SELECT MIN(Offset_Milliseconds) FROM advance_detectors), 0)) AS Known_Until
    {%- else %}
    SELECT MAX(TimeStamp) - INTERVAL (GREATEST({{latency_offset_seconds}}, 0) * 1000) MILLISECOND AS Known_Until
    {%- endif %}
    FROM {{from_table}}
),
{%- endif %}

cycle_events AS (
    {%- if arrival_pattern %}
    -- Vehicles at their arrival at the stop bar, with the phase of each
    SELECT d.TimeStamp + to_milliseconds(c.Offset_Milliseconds) AS TimeStamp,
        d.DeviceId,
        d.EventId::INT16 AS EventId,
        c.Phase
    FROM {{from_table}} d
    JOIN advance_detectors c
        ON d.DeviceId = c.DeviceId
       AND d.Parameter = c.Parameter
    WHERE d.EventId = 82
    {%- else %}
    -- Detector on events moved back by the latency, with the phase of each
    SELECT TimeStamp - INTERVAL ({{latency_offset_seconds}} * 1000) MILLISECOND AS TimeStamp,
        DeviceId,
        EventId::INT16 AS EventId,
        Phase::INT16 AS Phase
    FROM {{from_table}}
    NATURAL JOIN detector_config
    WHERE EventId = 82 AND Function = 'Yellow_Red'
    {%- endif %}

    UNION ALL

    -- Green begin, yellow begin and red clearance begin. The join leaves out phases with no such detector.
    SELECT r.TimeStamp,
        r.DeviceId,
        r.EventId::INT16 AS EventId,
        r.Parameter::INT16 AS Phase
    FROM {{from_table}} r
    JOIN (SELECT DISTINCT DeviceId, Phase
          FROM detector_config
          WHERE Function = '{{detector_function}}') c
        ON r.DeviceId = c.DeviceId
       AND c.Phase = r.Parameter
    WHERE r.EventId IN (1, 8, 10)
    {%- if unmatched %}

    UNION ALL

    SELECT TimeStamp,
        DeviceId,
        (CASE EventId WHEN {{first_code}} THEN 1 WHEN {{first_code + 1}} THEN 8 WHEN {{first_code + 2}} THEN 10 ELSE 82 END)::INT16 AS EventId,
        Parameter::INT16 AS Phase
    FROM unmatched_previous
    WHERE EventId BETWEEN {{first_code}} AND {{first_code + 3}}
    {%- endif %}
),

numbered_events AS (
    SELECT *,
        COUNT(*) FILTER (WHERE EventId = 1) OVER (PARTITION BY DeviceId, Phase ORDER BY TimeStamp, EventId) AS Cycle_Number
    FROM cycle_events
),

cycles AS (
    SELECT DeviceId,
        Phase,
        Cycle_Number,
        MIN(TimeStamp) FILTER (WHERE EventId = 1) AS Green_Begin,
        MIN(TimeStamp) FILTER (WHERE EventId = 10) AS Red_Begin,
        -- Events missing or doubled in a cycle throw the cycle out. Events before the first green have no cycle.
        Cycle_Number > 0
            AND COUNT(*) FILTER (WHERE EventId = 1) = 1
            AND COUNT(*) FILTER (WHERE EventId = 8) = 1
            AND COUNT(*) FILTER (WHERE EventId = 10) = 1 AS Is_Valid
    FROM numbered_events
    GROUP BY DeviceId, Phase, Cycle_Number
){%- if carry_bins %},

cycles_finished AS (
    SELECT *,
        COALESCE(LEAD(Green_Begin) OVER (PARTITION BY DeviceId, Phase ORDER BY Cycle_Number)
                 <= (SELECT Known_Until FROM known_events), FALSE) AS Is_Finished
    FROM cycles
    WHERE Cycle_Number > 0
),

-- Each phase's results stop at the bin of its first red in a cycle still open, or where results stop anyway
phase_limits AS (
    SELECT DeviceId,
        Phase,
        LEAST((SELECT TimeStamp FROM results_end),
              MIN(time_bucket(INTERVAL '{{bin_size}} minutes', Red_Begin)) FILTER (WHERE NOT Is_Finished)) AS Write_Until
    FROM cycles_finished
    GROUP BY DeviceId, Phase
)
{%- endif %}

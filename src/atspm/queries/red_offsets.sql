-- Included by yellow_red.sql and arrival_pattern.sql, which say how Red_Offset is rounded.
-- Each event in a whole cycle, as the seconds from that cycle's red begin, in the bin of that red begin.

WITH {% include 'yellow_red_cycles.sql' %},

labelled AS (
    -- The signal state of each event, the last of green (1), yellow (8) and red clearance (10) so far in its cycle
    SELECT e.*,
        MAX(CASE WHEN EventId < 81 THEN EventId ELSE 0 END) OVER (PARTITION BY DeviceId, Phase, Cycle_Number ORDER BY TimeStamp, EventId)::INT16 AS Signal_State
    FROM numbered_events e
),

offsets AS (
    SELECT time_bucket(INTERVAL '{{bin_size}} minutes', c.Red_Begin) AS TimeStamp,
        l.DeviceId,
        l.Phase::INT16 AS Phase,
        l.Signal_State,
        {{red_offset}} AS Red_Offset,
        COUNT(*)::float AS Count
    FROM labelled l
    JOIN cycles c USING (DeviceId, Phase, Cycle_Number)
    {%- if carry_bins %}
    JOIN phase_limits p USING (DeviceId, Phase)
    {%- endif %}
    WHERE l.EventId = 82
      AND c.Is_Valid
      {%- if carry_bins %}
      AND time_bucket(INTERVAL '{{bin_size}} minutes', c.Red_Begin) < p.Write_Until
      {%- endif %}
    GROUP BY 1, 2, 3, 4, 5
)

SELECT TimeStamp, DeviceId, Phase, Signal_State, Red_Offset, Count
FROM offsets

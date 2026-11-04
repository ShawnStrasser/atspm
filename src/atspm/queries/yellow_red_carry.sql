--What yellow_red passes to the next run through unmatched_events: the events of each phase from the first cycle it has
--not finished writing, as synthetic EventIds with the phase as Parameter: 951 green, 952 yellow and 953 red clearance
--begins, and 954 an actuation already moved back by the latency. A run with no raw data keeps everything it was given.
--Written in SQL for DuckDB. This is a jinja2 template, with variables inside curly braces.

WITH {% include 'yellow_red_cycles.sql' %},

first_kept AS (
    SELECT c.DeviceId,
        c.Phase,
        MIN(c.Cycle_Number) AS Cycle_Number
    FROM cycles_finished c
    JOIN phase_limits p USING (DeviceId, Phase)
    WHERE NOT c.Is_Finished
       OR time_bucket(INTERVAL '{{bin_size}} minutes', c.Red_Begin) >= p.Write_Until
    GROUP BY c.DeviceId, c.Phase
)

SELECT e.TimeStamp,
    e.DeviceId,
    (CASE e.EventId WHEN 1 THEN 951 WHEN 8 THEN 952 WHEN 10 THEN 953 ELSE 954 END)::INT16 AS EventId,
    e.Phase::INT16 AS Parameter,
    TRUE AS IsValid
FROM numbered_events e
JOIN first_kept k USING (DeviceId, Phase)
WHERE e.Cycle_Number >= k.Cycle_Number

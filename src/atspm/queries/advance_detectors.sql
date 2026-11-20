-- Included by arrival_projection.sql and yellow_red_cycles.sql. A single CTE, not a query.
--
-- UDOT's arrival time (ATSPM Methods and Assumptions Manual 4.3, Purdue Coordination Diagram, Step 1):
--     arrival = detector on + distance / (speed * 1.467) - latency
-- with distance in feet from the leading edge of the detector to the stop bar and speed in MPH. Each value
-- missing from the config takes its default: 35 MPH, and 220 feet for phases 2 and 6, 120 feet for all others.
-- Latency is the detector's LatencyCorrection in seconds, or latency_offset_seconds where it has none.
-- The loader always creates these columns, as NULL if the config does not have them.
--
-- With queue_model on, arrival_on_green's queue model (arrival_queue.sql) also takes from here:
--   - Lanes: how many lanes the detector covers, counted from the lane numbers in Lanes as atspm-detector writes them
--     ("2" is one lane, lane 2; "1,2" is two). 1 where missing.
--   - Storage: the cars those lanes hold between the detector and the stop bar, 25 feet a car.
--   - Its own arrival time: the on plus the travel time. The travel time is TravelTime (seconds a free-flowing car takes
--     from the detector to the stop line, measured) where the config has one, else the distance at the speed above but
--     at least 2 seconds plus the distance at 70 feet a second. That line fits the travel time free-flowing cars took
--     from the detector to the stop bar at 277 ODOT lanes with a stop bar count: 3.1 s at 75 feet, where 35 MPH gives
--     1.5 s, and 4.1 s at 150 feet, against 2.9 s. Past 385 feet the speed gives the longer time and is used. No latency
--     comes off: both times were measured between logged events, so any latency is already in them.
--   - Queue_Model: FALSE where RedDepartures (the share of the offs of the stop bar count zones in the detector's lanes
--     that come 2 seconds or more after red starts, as atspm-detector measures it) is over 0.25. Cars leaving on red
--     there (a permissive left, a right turn on red) empty a queue the Queue Model cannot see empty. At 438 ODOT advance
--     detectors, turning it off over 0.25 made 6 of 7 better: only the Spillback Flag runs there.
-- Only then does a config for SQL run by hand need the Lanes, TravelTime and RedDepartures columns.
advance_detectors AS (
    SELECT DeviceId,
        Parameter,
        Phase::INT16 AS Phase,
        ROUND(1000.0 * (Distance / Feet_Per_Second - Latency))::BIGINT AS Offset_Milliseconds{% if queue_model %},
        ROUND(1000.0 * COALESCE(
            Travel_Time,
            GREATEST(Distance / Feet_Per_Second, 2.0 + Distance / 70.0)))::BIGINT AS Queue_Offset_Milliseconds,
        Queue_Model,
        Lanes,
        Lanes * Distance / 25.0 AS Storage{% endif %}
    FROM (
        SELECT {% if int_keys %}Dev AS DeviceId{% else %}DeviceId{% endif %}, Parameter, Phase,
            COALESCE(
                CASE WHEN isfinite(DistanceFromStopBar) AND DistanceFromStopBar >= 0 THEN DistanceFromStopBar END,
                CASE WHEN Phase IN (2, 6) THEN 220.0 ELSE 120.0 END) AS Distance,
            COALESCE(CASE WHEN isfinite(MPH) AND MPH > 0 THEN MPH END, 35.0) * 1.467 AS Feet_Per_Second,
            COALESCE(CASE WHEN isfinite(LatencyCorrection) THEN LatencyCorrection END, {{latency_offset_seconds | default(0)}}) AS Latency{% if queue_model %},
            GREATEST(len(regexp_extract_all(COALESCE(Lanes::VARCHAR, ''), '[^,\s]+')), 1)::INT AS Lanes,
            CASE WHEN isfinite(TravelTime) AND TravelTime > 0 THEN TravelTime END AS Travel_Time,
            NOT COALESCE(isfinite(RedDepartures) AND RedDepartures > 0.25, FALSE) AS Queue_Model{% endif %}
        FROM detector_config{% if int_keys %} JOIN device_keys USING (DeviceId){% endif %}
        WHERE Function = 'Advance'
    )
)

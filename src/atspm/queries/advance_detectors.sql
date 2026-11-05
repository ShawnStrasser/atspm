-- Included by arrival_projection.sql and yellow_red_cycles.sql. A single CTE, not a query.
--
-- UDOT's arrival time (ATSPM Methods and Assumptions Manual 4.3, Purdue Coordination Diagram, Step 1):
--     arrival = detector on + distance / (speed * 1.467) - latency
-- with distance in feet from the leading edge of the detector to the stop bar and speed in MPH. Each value
-- missing from the config takes its default: 35 MPH, and 220 feet for phases 2 and 6, 120 feet for all others.
-- Latency is the detector's LatencyCorrection in seconds, or latency_offset_seconds where it has none.
-- The loader always creates these columns, as NULL if the config does not have them.
advance_detectors AS (
    SELECT DeviceId,
        Parameter,
        Phase::INT16 AS Phase,
        ROUND(1000.0 * (
            COALESCE(
                CASE WHEN isfinite(DistanceFromStopBar) AND DistanceFromStopBar >= 0 THEN DistanceFromStopBar END,
                CASE WHEN Phase IN (2, 6) THEN 220.0 ELSE 120.0 END)
            / (COALESCE(CASE WHEN isfinite(MPH) AND MPH > 0 THEN MPH END, 35.0) * 1.467)
            - COALESCE(CASE WHEN isfinite(LatencyCorrection) THEN LatencyCorrection END, {{latency_offset_seconds | default(0)}})
        ))::BIGINT AS Offset_Milliseconds
    FROM detector_config
    WHERE Function = 'Advance'
)

-- Coordination Aggregation
-- Aggregates coordination-related events from the timeline into time buckets
-- This provides per-time-bucket coordination state (Pattern, CycleLength, ActualCycleLength, ActualOffset)
-- 
-- Events:
-- 131: Pattern Change (Parameter = pattern number)
-- 132: Cycle Length Change (Parameter = cycle length in seconds)
-- 316: Actual Cycle Length (Parameter = actual cycle length in seconds)
-- 318: Actual Cycle Offset (Parameter = actual offset in seconds)
--
-- Note: 316 and 318 events may not be present in all systems. If missing, values will be null/zero.
-- Note: When multiple events occur at the same timestamp, the highest value is used as tie-breaker for determinism.
-- 
-- Incremental Processing:
-- For incremental runs, the previous coordination state is stored as synthetic events in unmatched_events:
-- 931: Pattern, 932: CycleLength, 933: ActualCycleLength, 934: ActualOffset
-- These are read from unmatched_previous and injected at a sentinel timestamp to enable fill-forward.

-- Get pattern changes from timeline - get the last pattern value for each time bucket
WITH pattern_agg AS (
    SELECT 
        TIME_BUCKET(INTERVAL '{{bin_size}} minutes', StartTime) AS TimeStamp,
        DeviceId,
        LAST(EventValue ORDER BY StartTime, EventValue)::INT16 AS Pattern
    FROM timeline
    WHERE EventClass = 'Pattern Change'
    GROUP BY 1, 2
),

-- Get cycle length changes from timeline - get the last cycle length value for each time bucket
cycle_length_agg AS (
    SELECT 
        TIME_BUCKET(INTERVAL '{{bin_size}} minutes', StartTime) AS TimeStamp,
        DeviceId,
        LAST(EventValue ORDER BY StartTime, EventValue)::INT16 AS CycleLength
    FROM timeline
    WHERE EventClass = 'Cycle Length Change'
    GROUP BY 1, 2
),

-- Get actual cycle length events (316) from raw data - last value per time bucket
actual_cycle_length_agg AS (
    SELECT 
        TIME_BUCKET(INTERVAL '{{bin_size}} minutes', TimeStamp) AS TimeStamp,
        DeviceId,
        LAST(Parameter ORDER BY TimeStamp, Parameter)::INT16 AS ActualCycleLength
    FROM {{from_table}}
    WHERE EventId = 316
    GROUP BY 1, 2
),

-- Get actual cycle offset events (318) from raw data - last value per time bucket
actual_offset_agg AS (
    SELECT 
        TIME_BUCKET(INTERVAL '{{bin_size}} minutes', TimeStamp) AS TimeStamp,
        DeviceId,
        LAST(Parameter ORDER BY TimeStamp, Parameter)::INT16 AS ActualOffset
    FROM {{from_table}}
    WHERE EventId = 318
    GROUP BY 1, 2
),

-- Create time buckets based on all coordination events
-- Using has_data table as source of all DeviceId/TimeStamp combinations
time_buckets AS (
    SELECT DISTINCT
        TimeStamp,
        DeviceId
    FROM has_data
),

{% if coord_state_found %}
-- For incremental runs: extract previous state from synthetic events in unmatched_previous
-- Synthetic EventIds: 931=Pattern, 932=CycleLength, 933=ActualCycleLength, 934=ActualOffset
min_timestamp AS (
    SELECT MIN(TimeStamp) - INTERVAL '1 second' AS SentinelTimeStamp FROM has_data
),

previous_state_injected AS (
    -- Pivot the synthetic events back into a row per DeviceId, injected at sentinel timestamp
    SELECT 
        (SELECT SentinelTimeStamp FROM min_timestamp) AS TimeStamp,
        DeviceId,
        MAX(CASE WHEN EventId = 931 THEN Parameter END)::INT16 AS Pattern,
        MAX(CASE WHEN EventId = 932 THEN Parameter END)::INT16 AS CycleLength,
        MAX(CASE WHEN EventId = 933 THEN Parameter END)::INT16 AS ActualCycleLength,
        MAX(CASE WHEN EventId = 934 THEN Parameter END)::INT16 AS ActualOffset
    FROM unmatched_previous
    WHERE EventId IN (931, 932, 933, 934)
    GROUP BY DeviceId
),
{% endif %}

-- Fill forward values using window functions
-- First, join all aggregated values to time buckets
combined AS (
{% if coord_state_found %}
    -- Start with injected previous state for fill-forward seeding
    SELECT * FROM previous_state_injected
    UNION ALL
{% endif %}
    SELECT
        tb.TimeStamp,
        tb.DeviceId,
        p.Pattern,
        cl.CycleLength,
        acl.ActualCycleLength,
        ao.ActualOffset
    FROM time_buckets tb
    LEFT JOIN pattern_agg p ON tb.TimeStamp = p.TimeStamp AND tb.DeviceId = p.DeviceId
    LEFT JOIN cycle_length_agg cl ON tb.TimeStamp = cl.TimeStamp AND tb.DeviceId = cl.DeviceId
    LEFT JOIN actual_cycle_length_agg acl ON tb.TimeStamp = acl.TimeStamp AND tb.DeviceId = acl.DeviceId
    LEFT JOIN actual_offset_agg ao ON tb.TimeStamp = ao.TimeStamp AND tb.DeviceId = ao.DeviceId
),

-- Fill forward missing values using LAST_VALUE with IGNORE NULLS
filled AS (
    SELECT
        TimeStamp,
        DeviceId,
        COALESCE(
            LAST_VALUE(Pattern IGNORE NULLS) OVER (PARTITION BY DeviceId ORDER BY TimeStamp ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
            0
        )::INT16 AS Pattern,
        COALESCE(
            LAST_VALUE(CycleLength IGNORE NULLS) OVER (PARTITION BY DeviceId ORDER BY TimeStamp ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
            0
        )::INT16 AS CycleLength,
        COALESCE(
            LAST_VALUE(ActualCycleLength IGNORE NULLS) OVER (PARTITION BY DeviceId ORDER BY TimeStamp ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
            0
        )::INT16 AS ActualCycleLength,
        COALESCE(
            LAST_VALUE(ActualOffset IGNORE NULLS) OVER (PARTITION BY DeviceId ORDER BY TimeStamp ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
            0
        )::INT16 AS ActualOffset
    FROM combined
)

-- Filter out the sentinel row and return only real time buckets
SELECT
    f.TimeStamp,
    f.DeviceId,
    f.Pattern,
    f.CycleLength,
    f.ActualCycleLength,
    f.ActualOffset
FROM filled f
INNER JOIN time_buckets tb ON f.TimeStamp = tb.TimeStamp AND f.DeviceId = tb.DeviceId
ORDER BY f.TimeStamp, f.DeviceId

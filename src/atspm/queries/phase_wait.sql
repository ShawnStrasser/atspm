-- Phase Wait Time Aggregation
-- Calculates binned average phase wait times per phase with preempt exclusion
-- 
-- This measure filters phase wait events from the timeline and:
-- 1. Excludes phase waits occurring during or within 2 minutes after a preempt
-- 2. Identifies skipped phases (wait time > 1.5× cycle length)
-- 3. Handles free mode (cycle length = 0) using 140s assumed cycle length
--
-- Note: CycleLength and Pattern are available in the coordination_agg table

-- Get cycle length changes from timeline
WITH cycle_lengths AS (
    SELECT 
        DeviceId,
        StartTime AS ChangeTime,
        EventValue AS CycleLength
    FROM timeline
    WHERE EventClass = 'Cycle Length Change'
),

-- Get preempt intervals and extend by 2 minutes
preempt_intervals AS (
    SELECT
        DeviceId,
        StartTime AS PreemptStart,
        -- Variable 'preempt_recovery_seconds' defines the exclusion window after preempt ends
        -- If EndTime is NULL (unmatched preempt), apply recovery time to StartTime
        COALESCE(EndTime, StartTime) + INTERVAL '{{preempt_recovery_seconds}}' SECOND AS ExclusionEnd
    FROM timeline
    WHERE EventClass = 'Preempt' AND IsValid
),

-- Get all phase wait events from timeline
phase_waits_raw AS (
    SELECT
        DeviceId,
        StartTime,
        EndTime,
        Duration,
        EventValue AS Phase
    FROM timeline
    WHERE EventClass = 'Phase Wait' AND IsValid
),

-- Flag phase waits that occur during preempt exclusion windows
phase_waits_flagged AS (
    SELECT
        pw.*,
        -- Flag TRUE if this phase wait starts during any preempt exclusion window
        COALESCE(bool_or(
            pw.StartTime >= p.PreemptStart AND pw.StartTime < p.ExclusionEnd
        ), FALSE) AS PreemptFlag
    FROM phase_waits_raw pw
    LEFT JOIN preempt_intervals p ON pw.DeviceId = p.DeviceId
    GROUP BY pw.DeviceId, pw.StartTime, pw.EndTime, pw.Duration, pw.Phase
),

-- Join with most recent cycle length using ASOF join
-- Pattern is not needed here; it's available in coordination_agg table
phase_waits_with_cycle AS (
    SELECT
        pw.*,
        -- Handle free mode: if cycle length is 0 or NULL, use assumed_cycle_length (default 140s)
        COALESCE(NULLIF(cl.CycleLength, 0), {{assumed_cycle_length}}) AS EffectiveCycleLength
    FROM phase_waits_flagged pw
    ASOF LEFT JOIN cycle_lengths cl 
        ON pw.DeviceId = cl.DeviceId 
        AND pw.StartTime >= cl.ChangeTime
),

-- Calculate skipped phase flag
phase_waits_classified AS (
    SELECT
        *,
        -- Skipped if wait time exceeds skip_multiplier (default 1.5) * effective cycle length
        CASE WHEN Duration > (EffectiveCycleLength * {{skip_multiplier}}) THEN 1 ELSE 0 END AS IsSkipped
    FROM phase_waits_with_cycle
    WHERE NOT PreemptFlag  -- Exclude phase waits during preempt windows
)

-- Final aggregation by time bucket, device, and phase
SELECT
    TIME_BUCKET(INTERVAL '{{bin_size}} minutes', EndTime) AS TimeStamp,
    DeviceId,
    Phase,
    AVG(Duration) AS AvgPhaseWait,
    SUM(IsSkipped) AS TotalSkips
FROM phase_waits_classified
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3

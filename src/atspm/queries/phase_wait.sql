-- Phase Wait Time Aggregation
-- Calculates binned average phase wait times per phase with preempt exclusion
-- 
-- This measure filters phase wait events from the timeline and:
-- 1. Excludes phase waits occurring during or within N minutes after a preempt
-- 2. Identifies skipped phases (wait time > 1.5× cycle length)
-- 3. Relaxes that threshold for waits that saw a TSP adjustment, which delays
--    service without skipping it
-- 4. Handles free mode (cycle length = 0) using assumed cycle length
-- 5. Uses MAX of cycle lengths at start and end of phase wait to avoid
--    false skip detection when cycle length changes mid-wait
--
-- Requires: timeline (must be run before this aggregation)
--
-- Parameters:
--   bin_size: Aggregation interval in minutes
--   preempt_recovery_seconds: Time after preempt ends to exclude phase waits
--   assumed_cycle_length: Fallback cycle length when in free mode (cycle = 0)
--   skip_multiplier: Threshold multiplier for skipped phase detection
--   tsp_skip_multiplier: Threshold multiplier used in place of skip_multiplier when
--                        a TSP adjustment occurred during the wait
--   controller_type: Controller type string (case-insensitive). When 'maxtime', 
--                    uses ActualCycleLength (EventId 316) instead of CycleLength (EventId 132)

-- Get cycle length changes from timeline
-- For MAXTIME controllers, use ActualCycleLength events (316) from raw data
-- For others, use Cycle Length Change events from timeline
WITH cycle_lengths AS (
{% if controller_type|default('')|lower == 'maxtime' %}
    -- MAXTIME: Use ActualCycleLength events (316) directly from raw data
    -- These are more accurate than the programmed cycle length (132)
    SELECT 
        DeviceId,
        TimeStamp AS ChangeTime,
        Parameter AS CycleLength
    FROM {{from_table}}
    WHERE EventId = 316
{% else %}
    -- Non-MAXTIME: Use Cycle Length Change events from timeline
    SELECT 
        DeviceId,
        StartTime AS ChangeTime,
        EventValue AS CycleLength
    FROM timeline
    WHERE EventClass = 'Cycle Length Change' AND IsValid
{% endif %}
{% if incremental_run|default(false) and unmatched|default(false) %}
    -- For incremental runs: inject previous cycle length state from unmatched_previous
    -- Synthetic EventId 932 stores the last known CycleLength
    -- Synthetic EventId 933 stores the last known ActualCycleLength (MAXTIME)
    UNION ALL
    SELECT 
        DeviceId,
        TimeStamp AS ChangeTime,
        Parameter AS CycleLength
    FROM unmatched_previous
    WHERE EventId = {% if controller_type|default('')|lower == 'maxtime' %}933{% else %}932{% endif %}
{% endif %}
),

-- Get preempt intervals and extend by recovery time
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

-- Get TSP adjustment events. Unlike a preempt these are not excluded: TSP delays
-- service rather than skipping it, so a wait that saw one is judged against a
-- higher multiplier instead of being dropped. Only the event time is needed - the
-- timeline's EndTime for these runs to the next adjustment on the same phase, which
-- says nothing about how long the adjustment itself lasted.
tsp_adjustments AS (
    SELECT
        DeviceId,
        StartTime AS AdjustTime
    FROM timeline
    WHERE EventClass = 'TSP Adjustment' AND IsValid
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

-- Flag phase waits that overlap with preempt exclusion windows
-- Proper overlap detection: ranges overlap if Start1 < End2 AND Start2 < End1
phase_waits_flagged AS (
    SELECT
        pw.*,
        -- Flag TRUE if this phase wait overlaps with any preempt exclusion window
        -- Overlap occurs when: PW.Start < Preempt.ExclusionEnd AND Preempt.PreemptStart < PW.End
        COALESCE(bool_or(
            pw.StartTime < p.ExclusionEnd AND p.PreemptStart < pw.EndTime
        ), FALSE) AS PreemptFlag
    FROM phase_waits_raw pw
    LEFT JOIN preempt_intervals p ON pw.DeviceId = p.DeviceId
    GROUP BY pw.DeviceId, pw.StartTime, pw.EndTime, pw.Duration, pw.Phase
),

-- Join with cycle length using ASOF joins to get cycle length at BOTH start and end
-- This handles the edge case where cycle length changes mid-wait:
-- e.g., phase wait starts at cycle=120, cycle changes to 60 mid-wait, wait ends
-- Without this, the 60s cycle would cause false skip detection
phase_waits_with_cycle AS (
    SELECT
        pw.*,
        -- Get cycle length active at the START of the phase wait
        COALESCE(NULLIF(cl_start.CycleLength, 0), {{assumed_cycle_length}}) AS StartCycleLength,
        -- Get cycle length active at the END of the phase wait
        COALESCE(NULLIF(cl_end.CycleLength, 0), {{assumed_cycle_length}}) AS EndCycleLength,
        -- TRUE when a TSP adjustment landed inside the wait. The ASOF join returns
        -- the latest adjustment at or before EndTime, so comparing that one against
        -- StartTime is enough to know whether any fell in the window - and it stays
        -- linear, where joining on the whole window would be quadratic. At a signal
        -- calling TSP every cycle that product is millions of rows per device.
        COALESCE(tsp.AdjustTime >= pw.StartTime, FALSE) AS TspFlag
    FROM phase_waits_flagged pw
    -- ASOF join: find the most recent cycle length change before or at StartTime
    ASOF LEFT JOIN cycle_lengths cl_start 
        ON pw.DeviceId = cl_start.DeviceId 
        AND pw.StartTime >= cl_start.ChangeTime
    -- ASOF join: find the most recent cycle length change before or at EndTime
    ASOF LEFT JOIN cycle_lengths cl_end
        ON pw.DeviceId = cl_end.DeviceId
        AND pw.EndTime >= cl_end.ChangeTime
    -- ASOF join: find the most recent TSP adjustment before or at EndTime
    ASOF LEFT JOIN tsp_adjustments tsp
        ON pw.DeviceId = tsp.DeviceId
        AND pw.EndTime >= tsp.AdjustTime
    WHERE NOT pw.PreemptFlag
),

-- Calculate skipped phase flag using the MAXIMUM of start and end cycle lengths
-- This prevents false positives when cycle length decreases mid-wait
phase_waits_classified AS (
    SELECT
        *,
        -- Use the MAX of start and end cycle lengths to be conservative
        -- This ensures we don't flag a phase as skipped just because
        -- the cycle length dropped after the phase started waiting
        GREATEST(StartCycleLength, EndCycleLength) AS EffectiveCycleLength,
        -- A wait that saw a TSP adjustment is held to the looser threshold. TSP
        -- pushes service later in the cycle, so where calls are frequent the
        -- ordinary multiplier reports skips for phases that were in fact served.
        CASE WHEN Duration > (GREATEST(StartCycleLength, EndCycleLength) *
                 CASE WHEN TspFlag THEN {{tsp_skip_multiplier}} ELSE {{skip_multiplier}} END)
             THEN 1 ELSE 0 END AS IsSkipped
    FROM phase_waits_with_cycle
)

-- Final aggregation by time bucket, device, and phase
SELECT
    TIME_BUCKET(INTERVAL '{{bin_size}} minutes', EndTime) AS TimeStamp,
    DeviceId,
    Phase,
    AVG(Duration) AS AvgPhaseWait,
    MAX(Duration) AS MaxPhaseWait,
    SUM(IsSkipped) AS TotalSkips
FROM phase_waits_classified
GROUP BY 1, 2, 3

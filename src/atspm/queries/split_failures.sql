--Aggregate split failures
--Written in SQL for DuckDB. This is a jinja2 template, with variables inside curly braces.
CREATE TABLE sf_intermediate AS
WITH step1 as (
-- detector_with_phase
-- Join Phase Number to each Detector Event
-- Rename parameter as Detector to avoid cofusion
SELECT r.TimeStamp,
    r.DeviceId, 
    r.EventId, 
    r.Parameter as Detector, 
    CAST(c.Phase AS UTINYINT) AS Phase
FROM {{from_table}} r
JOIN detector_config c ON 
    r.DeviceId = c.DeviceId 
    AND r.Parameter = c.Parameter
WHERE 
    r.EventId IN(81,82)
    and c.Function = 'Presence'
),


step2 as (
-- impute_actuations
/* 
This is to fill in missing detector on/off events through interpolation
Sometimes detector on events (82) occur in rapic succession without an off event separating them,
    When the gap between two-in-a-row ON events is <= 2000 milliseconds,
    add an OFF event with timestamp matching the next ON event.
If for two-in-a-row ON events more than 2000 milliseconds apart, an OFF event is added half-way between them
For two-in-a-row OFF events, an ON event is added half-way between them regardless of the gap size
*/
WITH lagged as (
        SELECT *,
            LAG(EventId) OVER (PARTITION BY DeviceId, Detector ORDER BY TimeStamp) as PrevEventId,
            datediff('MILLISECOND', LAG(TimeStamp) OVER (PARTITION BY DeviceId, Detector ORDER BY TimeStamp), TimeStamp) AS PrevDiff
        FROM step1
    ),
    interpolate_OFFS_rapid AS (
        SELECT TimeStamp, DeviceId, 81 AS EventId, Detector, Phase
        FROM lagged
        WHERE PrevDiff <= 2000
            and EventId = 82
            and PrevEventId = 82
    ),
    interpolate_OFFs as (
        SELECT
            "TimeStamp" - INTERVAL (PrevDiff / 2) MILLISECOND as TimeStamp,
            DeviceId,
            81 as EventId,
            Detector,
            Phase
        FROM lagged
        WHERE PrevDiff > 2000
            and EventId = 82
            and PrevEventId = 82
    ),
    interpolate_ONs AS (
        SELECT "TimeStamp" - INTERVAL (PrevDiff / 2) MILLISECOND as TimeStamp,
            DeviceId,
            82 AS EventId,
            Detector,
            Phase
        FROM lagged
        WHERE EventId = 81 AND PrevEventId = 81
    ),
    combined AS (
        SELECT * FROM step1
        UNION ALL
        SELECT * FROM interpolate_OFFS_rapid
        UNION ALL
        SELECT * FROM interpolate_OFFs
        UNION ALL
        SELECT * FROM interpolate_ONs
    ),
    --The first detector event needs to be an ON event for later logic to work
    --Therefore this step adds an ON event if the first event is an OFF event
    on_before_off AS (
        SELECT TimeStamp - INTERVAL 1 MILLISECOND as TimeStamp,--just right before the off event to make ordering work but not mess up occuapncy
            DeviceId,
            82 as EventId,
            Detector,
            Phase
        FROM lagged
        WHERE PrevEventId IS NULL AND EventId = 81 --no earlier event, so this is the detector's first
    )
    SELECT TimeStamp,
        DeviceId,
        EventId,
        Detector,
        Phase
    FROM combined
    UNION ALL
    SELECT * FROM on_before_off
),

--Step 3 depends on the value of by_approach (handled by the Jinja2 template)
{% if by_approach %}
step3 AS (
    -- combine_detectors_ByApproach
    -- This is for Approach-based Split Failures
    -- Combines detector actuations for all detectors assigned to each phase
    -- Detector state is determined as an OR condition, so it's on if ANY detectors are on
    WITH with_values AS (
        SELECT *,
            CASE
                WHEN EventId=82 THEN 1
                WHEN EventId=81 THEN -1
            END as Value
        FROM step2
    ),
    with_cumulative AS (
        SELECT *,
            SUM(Value) OVER (PARTITION BY DeviceId, Phase ORDER BY TimeStamp) as CumulativeSum
        FROM with_values
    ),
    with_state AS (
        SELECT *,
            CASE
                WHEN CumulativeSum > 0 THEN 82
                ELSE 81
            END as State
        FROM with_cumulative
    ),
    with_3a AS (
        SELECT TimeStamp,
            DeviceId,
            State as EventId,
            0 as Detector,
            Phase
        FROM with_state
    ),

    -- phase_with_detector_ByApproach
    -- Combined phase and detector data without duplicating phase data (no need since detector/phases are combined already)
    phase AS (
        SELECT r.TimeStamp as TimeStamp, 
            r.DeviceId as DeviceId,
            r.EventId as EventId,
            0 as Detector,
            r.Parameter AS Phase 
        FROM {{from_table}} r
        -- Join is for efficiency, to filter out phases that are not in the detector_config table
        JOIN (SELECT
                DISTINCT DeviceId, Phase
                FROM detector_config
                WHERE Function = 'Presence'
                ) c 
        ON r.DeviceId = c.DeviceId 
           AND c.Phase = r.Parameter
        WHERE r.EventId IN (1,8,10)
    )
    SELECT * FROM phase
    UNION ALL
    SELECT TimeStamp,
        DeviceId, 
        EventId, 
        Detector,
        Phase
    FROM with_3a
),
{% else %}
step3 AS (
    -- phase_with_detector_ByLane
    -- Duplicate phase events for each unique detector
    -- This is to be able to do group by operations later using the Detector field, so each detector has it's own phase events to be grouped with
    -- Detector colum is the detector number for detector events AND associated phase events for that detector
    WITH detectors AS (
        SELECT DISTINCT DeviceId,
            Detector,
            Phase FROM step2
    ),
    phase AS (
        SELECT r.TimeStamp as TimeStamp, 
            r.DeviceId as DeviceId,
            r.EventId as EventId,
            0 as Detector,
            r.Parameter AS Phase 
        FROM {{from_table}} r
        -- Join is for efficiency, to filter out phases that are not in the detector_config table
        JOIN (SELECT
                DISTINCT DeviceId, Phase
                FROM detector_config
                WHERE Function = 'Presence'
                ) c 
        ON r.DeviceId = c.DeviceId 
           AND c.Phase = r.Parameter
        WHERE r.EventId IN (1,8,10)
    ), 
    duplicated_phase AS (
        SELECT TimeStamp, 
            DeviceId, 
            EventId, 
            Detector,
            Phase 
        FROM phase
        NATURAL JOIN detectors
    )
    SELECT * FROM duplicated_phase
    UNION ALL
    SELECT TimeStamp,
        DeviceId, 
        EventId, 
        Detector,
        Phase
    FROM step2
),
{% endif %}


--Union previous unmatched data with current data
{% if unmatched %}
step3b AS (
    SELECT * FROM step3
    UNION ALL
    SELECT * FROM sf_unmatched_previous
),
{% endif %}

step4 AS (
    -- with_barrier
    --Add 5 second after red time barrier for Split Failures
    WITH red_time_barrier AS (
        SELECT 
            "TimeStamp" + INTERVAL '{{red_time}}' SECOND as TimeStamp,
            DeviceId,
            11 as EventId,
            Detector,
            Phase
        FROM 
            {% if unmatched %} step3b {% else %} step3 {% endif %}
        WHERE 
            EventId = 10
            --and ensure that the timestamp plus red_time is less than the max timestamp
            and "TimeStamp" + INTERVAL '{{red_time}}' SECOND < (
                --This is to ensure the red barrier will not be added past the cutoff time for data being processed
                --The max timestamp gets rounded down to the nearest bin_size interval and then that interval is added to it to get the max time to get the cutoff time
                SELECT TIME_BUCKET(interval '{{bin_size}} minutes', MaxTime) + INTERVAL '{{bin_size}} minutes' as MaxTime
                FROM
                    (SELECT MAX(TimeStamp) MaxTime FROM {{from_table}}) q
                )
    )
    SELECT * FROM {% if unmatched %} step3b {% else %} step3 {% endif %}
    UNION ALL
    SELECT * FROM red_time_barrier  
),

step5 AS (
    -- with_cycle
    -- Group by cycle and label events as occuring during green, yellow, or red
    WITH step1a as (
        SELECT *,
            CASE WHEN EventId < 81 THEN EventId ELSE 0 END AS Signal_State_Mask,
            CASE WHEN EventId = 81 THEN 0 WHEN EventId =82 THEN 1 END AS Detector_State_Change
        FROM step4
    ),
    step2a as (
        SELECT *,
            CAST(COUNT(*) FILTER (WHERE EventId = 1) OVER device_order AS UINTEGER) AS Cycle_Number,
            -- The detector's state is its latest on or off event. Events tied on TimeStamp and EventId are the same state.
            CAST(LAST_VALUE(Detector_State_Change IGNORE NULLS) OVER device_order AS BOOL) AS Detector_State,
            -- Same sort as above, so saved here rather than sorted again for the time between events below
            LEAD(TimeStamp) OVER device_order AS NextTimeStamp
            FROM step1a
            WINDOW device_order AS (PARTITION BY DeviceId, Detector, Phase ORDER BY TimeStamp, EventId)
    )
    SELECT TimeStamp,
        DeviceId, 
        EventId, 
        Detector,
        Phase,
        Cycle_Number,
        CAST(MAX(Signal_State_Mask) OVER (PARTITION BY DeviceId, Detector, Phase, Cycle_Number ORDER BY TimeStamp, EventId) AS UTINYINT) AS Signal_State,
        Detector_State,
        NextTimeStamp
    FROM step2a  
)
SELECT * FROM step5 --step5
;


--Now to save data from incomplete cycles for the next run
{% if incremental_run %}
CREATE TABLE sf_unmatched AS
WITH max_cycle AS (
    SELECT DeviceId, Detector, Phase, MAX(Cycle_Number) Cycle_Number
    FROM sf_intermediate
    GROUP BY ALL
),
incomplete_cycles as (
    SELECT DeviceId, Detector, Phase, Cycle_Number
    FROM sf_intermediate
    NATURAL JOIN max_cycle
    WHERE Cycle_Number > 0 --0 cycle means it didn't start with a green event
    GROUP BY ALL
    HAVING 
        COUNT(CASE WHEN EventId = 1 THEN EventId END) = 1 --ensure cycle started on green
        and COUNT(CASE WHEN EventId = 11 THEN EventId END) = 0 --ensure cycle is not complete (no red barrier yet)
),
detector_states as (
    SELECT TimeStamp, DeviceId, Detector, Phase, Cycle_Number, Detector_State,
        ROW_NUMBER() OVER (PARTITION BY DeviceId, Detector, Phase, Cycle_Number ORDER BY TimeStamp) as row_num,
        ROW_NUMBER() OVER (PARTITION BY DeviceId, Detector, Phase, Cycle_Number ORDER BY TimeStamp DESC) as row_num_desc
    FROM sf_intermediate
    NATURAL JOIN max_cycle
),
final_detector_states as (
    SELECT 
        TimeStamp - INTERVAL 1 MILLISECOND as TimeStamp,--subtract a millisec to have it be before the phase event
        DeviceId,
        CASE WHEN Detector_State THEN 82 ELSE 81 END as EventId,
        Detector, Phase
    FROM detector_states
    WHERE row_num = 1 OR row_num_desc = 1
)
SELECT TimeStamp, DeviceId, EventId, Detector, Phase--, Cycle_Number, Signal_State, Detector_State
FROM sf_intermediate
NATURAL JOIN incomplete_cycles
UNION ALL
SELECT * FROM final_detector_states
;
{% endif %}



--Now to calculate the split failures from the intermediate data
CREATE TABLE sf_final AS
WITH step6 AS (
    -- time_diff
    -- Calc time diff between events
    SELECT TimeStamp, 
        DeviceId, 
        EventId, 
        Detector,
        Phase,
        Cycle_Number, 
        Signal_State, 
        Detector_State, 
        CAST(DATEDIFF('MILLISECOND', TimeStamp, NextTimeStamp) AS INT) AS TimeDiff
    FROM sf_intermediate
),
valid_cycles AS (
    -- Generate VALID CYCLES to remove cycles with missing data for current run, and save incomplete cycle data for next run
    -- Remove cycles with missing data, and Sum the detector on/off time over each phase state
    SELECT DeviceId,
        Detector,
        Phase, 
        Cycle_Number
    FROM step6
    WHERE Cycle_Number > 0 --0 cycle means it didn't start with a green event
    GROUP BY DeviceId, Detector, Phase, Cycle_Number
    HAVING 
        COUNT(CASE WHEN EventId = 8 THEN EventId END) = 1 --ensure 1 yellow change event in cycle
        and COUNT(CASE WHEN EventId = 11 THEN EventId END) = 1 --ensure 1 red barrier
        --(cycles are deliniated by begin green, so they already are guaranteed to only have 1 green event)
        and COUNT(CASE WHEN Detector_State IS NULL THEN 1 END) = 0 --ensure no missing detector states
        --and COUNT(CASE WHEN TimeDIff IS NULL THEN 1 END) = 0 --ensure no missing time diffs
),

--Remove invalid cycles, set timestamps the same for each cycle, and sum the time for each state
step7 AS (
    SELECT MAX(TimeStamp) as TimeStamp, 
        DeviceId, 
        Detector,
        Phase,
        Cycle_Number, 
        Signal_State, 
        Detector_State, 
        CAST(SUM(TimeDiff) AS INT) AS TotalTimeDiff
    FROM step6
    NATURAL JOIN valid_cycles
    WHERE Signal_State IN (1,10) OR EventId=11 --only green and red states are used for split failures, but also keep the red barrier to set max timestamp consistently
    GROUP BY ALL --DeviceId, Detector, Phase, Cycle_Number, Signal_State, Detector_State  
),

--Add up time for states in the cycle
step8 AS (
    WITH step1b AS (
        --timestamps are already the same at this point but need to be in an aggregate function for the group by
        SELECT MAX(TimeStamp) as TimeStamp, DeviceId, Detector, Phase, Cycle_Number,
            CAST(SUM(CASE WHEN Detector_State = TRUE AND Signal_State = 1 THEN TotalTimeDiff ELSE 0 END) AS INT) AS Green_ON,
            CAST(SUM(CASE WHEN Detector_State = FALSE AND Signal_State = 1 THEN TotalTimeDiff ELSE 0 END) AS INT) AS Green_OFF,
            CAST(SUM(CASE WHEN Detector_State = TRUE AND Signal_State = 10 THEN TotalTimeDiff ELSE 0 END) AS INT) AS Red_5_ON --red_5_OFF is just the inverse
        FROM step7
        GROUP BY ALL --DeviceId, Detector, Phase, Cycle_Number
    )
    SELECT TimeStamp, DeviceId, Detector, Phase,
        CAST(Green_ON + Green_OFF AS FLOAT) / 1000 AS Green_Time,
        CAST(Green_ON AS FLOAT) / (Green_ON + Green_OFF) AS Green_Occupancy,
        CAST(Red_5_ON AS FLOAT) / {{red_time}}000 AS Red_Occupancy
    FROM step1b
)

{% if not by_cycle %}
SELECT 
    time_bucket(interval '{{bin_size}} minutes', TimeStamp) as TimeStamp,
    DeviceId,
    {% if not by_approach %}
    Detector::int16 as Detector,
    {% endif %}
    Phase::int16 as Phase,
    AVG(Green_Time)::float as Green_Time,
    AVG(Green_Occupancy)::float as Green_Occupancy,
    AVG(Red_Occupancy)::float as Red_Occupancy,
    SUM(CASE WHEN 
        Red_Occupancy>={{red_occupancy_threshold}}
        AND Green_Occupancy>={{green_occupancy_threshold}}
        THEN 1 ELSE 0 END)::int16 AS Split_Failure
FROM step8
GROUP BY ALL
;
{% else %}
SELECT
    TimeStamp,
    DeviceId,
    {% if not by_approach %}
    Detector::int16 as Detector,
    {% endif %}
    Phase::int16 as Phase,
    Green_Time,
    Green_Occupancy,
    Red_Occupancy,
    (CASE WHEN 
        Red_Occupancy>={{red_occupancy_threshold}}
        AND Green_Occupancy>={{green_occupancy_threshold}}
        THEN 1 ELSE 0 END)::int16 AS Split_Failure
FROM step8
;
{% endif %}

--Clean up
DROP TABLE sf_intermediate
;

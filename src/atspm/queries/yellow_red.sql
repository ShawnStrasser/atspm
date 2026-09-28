--Actuations on yellow and red
--Written in SQL for DuckDB. This is a jinja2 template, with variables inside curly braces.

WITH view1 AS (
    -- Shift timestamps back to account for latency and join Phase Number to each Detector On Event
    SELECT TimeStamp - INTERVAL ({{latency_offset_seconds}} * 1000) MILLISECOND as TimeStamp,
        DeviceId, 
        EventId::int16 AS EventId, 
        Phase::int16 AS Phase
    FROM {{from_table}}
    NATURAL JOIN detector_config
    WHERE EventId = 82 AND Function = 'Yellow_Red'
),

view2 AS (
    -- phase_with_detector_ByApproach
    -- Combined phase and detector data without duplicating phase data (no need since detector/phases are combined already)
    WITH 
    phase AS (
        SELECT r.TimeStamp as TimeStamp, 
            r.DeviceId as DeviceId,
            r.EventId::int16 as EventId,
            r.Parameter::int16 AS Phase 
        FROM {{from_table}} r
        -- Join is for efficiency, to filter out phases that are not in the detector_config table
        JOIN (SELECT
                DISTINCT DeviceId, Phase
                FROM detector_config
                WHERE Function = 'Yellow_Red'
                ) c 
        ON r.DeviceId = c.DeviceId 
           AND c.Phase = r.Parameter
        WHERE r.EventId IN (1,8,10)
        )
    SELECT * FROM phase
    UNION ALL
    SELECT * FROM view1
),

view3 AS (
    -- with_cycle
    -- Group by cycle and label events as occuring during green, yellow, or red
    WITH step1 as (
        SELECT *,
            CASE WHEN EventId = 1 THEN 1 ELSE 0 END AS Cycle_Number_Mask,
            CASE WHEN EventId < 81 THEN EventId ELSE 0 END AS Signal_State_Mask
        FROM view2
    ),
    step2 as (
        SELECT *,
            SUM(Cycle_Number_Mask) OVER (PARTITION BY DeviceId, Phase ORDER BY TimeStamp, EventId) AS Cycle_Number            FROM step1
    )
    SELECT TimeStamp,
        DeviceId, 
        EventId, 
        Phase,
        Cycle_Number,
        MAX(Signal_State_Mask) OVER (PARTITION BY DeviceId, Phase, Cycle_Number ORDER BY TimeStamp, EventId)::int16 AS Signal_State
    FROM step2
),

view4 AS (
    -- valid_cycles
    -- remove cycles where there is not exactly one EventId equal to 1, 8, and 10
    -- this is to remove cycles where there is missing data for yellow_red actuations. 
    WITH step1 AS(
        SELECT
            DeviceId,
            Phase,
            Cycle_Number,
            COUNT(CASE WHEN EventId = 1 THEN EventId END) as Green_Count,
            COUNT(CASE WHEN EventId = 8 THEN EventId END) as Yellow_Count,
            COUNT(CASE WHEN EventId = 10 THEN EventId END) as Red_Count
        FROM view3
        GROUP BY DeviceId, Phase, Cycle_Number),

    step2 AS(
        SELECT DeviceId, Phase, Cycle_Number
        FROM step1
        WHERE Green_Count = 1 AND Yellow_Count = 1 AND Red_Count = 1
    )
    select * from view3
    NATURAL JOIN step2
),

view5 AS (
    -- red_offset
    -- Select Begin Red Timestamps for each cycle
    WITH begin_reds AS(
        SELECT
            TimeStamp as Red_TimeStamp,
            DeviceId, 
            Phase, 
            Cycle_Number
        FROM view4
        WHERE EventId = 10 and Cycle_Number > 0
    ),
    renamed_table AS (
        SELECT 
            time_bucket(interval '15 minutes', Red_TimeStamp) as New_TimeStamp,
            DeviceId,
            Phase,
            Signal_State,
            --Red Offset is rounded to nearest half second. This was based on visual observation of the smoothness of rounding from 0.2 seconds to 1 second, seemed to look good
            ROUND(DATEDIFF('MILLISECOND', Red_TimeStamp, TimeStamp)::float * 2 / 1000) / 2 AS Red_Offset,
            COUNT(*)::float as Count
        FROM view4
        NATURAL JOIN begin_reds
        WHERE EventId=82
        GROUP BY
            New_TimeStamp,
            DeviceId,
            Phase,
            Signal_State,
            Red_Offset
    )
    SELECT 
        New_TimeStamp AS TimeStamp,
        DeviceId,
        Phase::int16 AS Phase,
        Signal_State,
        Red_Offset,
        Count
    FROM renamed_table
    {% if min_red_offset is defined %}
    WHERE Red_Offset >= {{ min_red_offset }}
    {% endif %}

)


SELECT * FROM view5
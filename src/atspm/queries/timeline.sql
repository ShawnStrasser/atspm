--Timeline Events for Troubleshooting by Visualizing What Happened
--Written in SQL for DuckDB. This is a jinja2 template, with variables inside curly braces.
--jinja2 macros are used to reduce code duplication for common patterns.

-- Pair matching start/end events into intervals with a validity flag.
{% macro paired_event(name, start_event, end_event, partition_cols='DeviceID, Parameter', order_by='TimeStamp') -%}
{{ name }}1 AS
	(
	SELECT *,
		LEAD(TimeStamp) OVER (PARTITION BY {{ partition_cols }} ORDER BY {{ order_by }}) AS EndTime,
		LEAD(EventId) OVER (PARTITION BY {{ partition_cols }} ORDER BY {{ order_by }}) AS NextEventId
	FROM {{from_table}}               
	WHERE EventId IN ({{ start_event }}, {{ end_event }})
	),
{{ name }} AS
	(
	SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime,
		   CASE WHEN EventId = {{ start_event }} AND NextEventId = {{ end_event }} THEN TRUE ELSE FALSE END AS IsValid
	FROM {{ name }}1
	WHERE EventId = {{ start_event }} OR (EventId = {{ end_event }} AND NextEventId = {{ end_event }})
	)
{%- endmacro %}

-- Capture on/off style toggles as intervals with validity flags.
{% macro parameter_toggle(name, event_id, on_value=1, off_value=0, partition_cols='DeviceID, EventId') -%}
{{ name }}1 AS
	(
	SELECT *,
		LEAD(TimeStamp) OVER (PARTITION BY {{ partition_cols }} ORDER BY TimeStamp) AS EndTime,
		LEAD(Parameter) OVER (PARTITION BY {{ partition_cols }} ORDER BY TimeStamp) AS NextParameter
	FROM {{from_table}}               
	WHERE EventId = {{ event_id }}
	),
{{ name }} AS
	(
	SELECT TimeStamp, DeviceID, EventId, Parameter, EndTime,
		   CASE WHEN Parameter = {{ on_value }} AND NextParameter = {{ off_value }} THEN TRUE ELSE FALSE END AS IsValid
	FROM {{ name }}1
	WHERE Parameter = {{ on_value }} OR (Parameter = {{ off_value }} AND NextParameter = {{ off_value }})
	)
{%- endmacro %}

-- Create a point-in-time event with a cushioned EndTime and validity flag.
{% macro instant_event(name, event_id) -%}
{{ name }} AS
	(
	SELECT TimeStamp, DeviceID, EventId, Parameter,
	       TimeStamp + INTERVAL ({{cushion_time}}) SECOND AS EndTime,
	       TRUE AS IsValid
	FROM {{from_table}}
	WHERE EventId = {{ event_id }}
	)
{%- endmacro %}

WITH
-- Paired event macros
{{ paired_event('AdvanceWarningOverlap', 71, 72, order_by='TimeStamp, EventId') }},
{{ paired_event('AdvanceWarningPhase', 55, 56, order_by='TimeStamp, EventId') }},
{{ paired_event('FYA', 32, 33) }},
{{ paired_event('Green', 1, 7) }},
{{ paired_event('Yellow', 8, 9) }},
{{ paired_event('Red', 10, 11) }},
{{ paired_event('OverlapPed', 67, 65) }},
{{ paired_event('Ped', 21, 23) }},
{{ paired_event('PhaseCall', 43, 44) }},
{{ paired_event('PhaseHold', 41, 42) }},
{{ paired_event('PhaseOmit', 46, 47) }},
{{ paired_event('PedOmit', 48, 49) }},
{{ paired_event('Preempt', 102, 104, order_by='TimeStamp, EventId') }},
{{ paired_event('SpecialFunction', 176, 177) }},
{{ paired_event('TSP_Checkin', 112, 115, order_by='TimeStamp, EventId') }},
{{ paired_event('TSP_Detector', 94, 93, order_by='TimeStamp, EventId') }},
{{ paired_event('TSP_Service', 118, 119, order_by='TimeStamp, EventId') }},


-- Parameter toggle macros
{{ parameter_toggle('AuxSwitch', 202) }},
{{ parameter_toggle('StopTime', 180) }},
{{ parameter_toggle('ManualControl', 178) }},

-- Instant event macros
{{ instant_event('Coord', 131) }},
{{ instant_event('IntervalAdvance', 179) }},
{{ instant_event('PowerFailure', 182) }},
{{ instant_event('PowerRestored', 184) }},
{{ instant_event('CycleLengthChange', 132) }},


-- Phase Wait Logic
-- Pairs Phase Call (43) with the next Green Start (1) for same phase
-- Measures how long a phase waits after being called until it gets green
-- Uses EventId 902 for unmatched state tracking across incremental chunks
-- Note: Fresh 43s come from raw_data, 902s come from unmatched_previous
{% if unmatched %}
-- Incremental mode with unmatched events: Select 43s and 1s from raw_data (fresh),
-- and 902s from unmatched_previous. This prevents re-matching 43s that were already
-- matched in a previous chunk (those 43s are in unmatched_previous for PhaseCall).
PhaseWait_Source AS (
    SELECT * FROM raw_data WHERE EventId IN (43, 1)
    UNION ALL
    SELECT * FROM unmatched_previous WHERE EventId IN (902, 1)
),
PhaseWait1 AS (
    SELECT *,
        LEAD(TimeStamp) OVER (PARTITION BY DeviceID, Parameter ORDER BY TimeStamp, EventId) AS EndTime,
        LEAD(EventId) OVER (PARTITION BY DeviceID, Parameter ORDER BY TimeStamp, EventId) AS NextEventId
    FROM PhaseWait_Source
    WHERE EventId IN (43, 1, 902)
),
{% else %}
-- Batch mode or first chunk: Use from_table directly
PhaseWait1 AS (
    SELECT *,
        LEAD(TimeStamp) OVER (PARTITION BY DeviceID, Parameter ORDER BY TimeStamp, EventId) AS EndTime,
        LEAD(EventId) OVER (PARTITION BY DeviceID, Parameter ORDER BY TimeStamp, EventId) AS NextEventId
    FROM {{from_table}}
    WHERE EventId IN (43, 1, 902)
),
{% endif %}
PhaseWait_Matched AS (
    -- Matched: 43 or 902 followed by 1, output as Phase Wait (901)
    SELECT TimeStamp, DeviceID, 901 AS EventID, Parameter, EndTime, TRUE AS IsValid
    FROM PhaseWait1
    WHERE EventId IN (43, 902) AND NextEventId = 1
),
PhaseWait_Unmatched AS (
    -- Unmatched: 43 or 902 at end of data (no next event), output as 902 for state tracking
    SELECT TimeStamp, DeviceID, 902 AS EventID, Parameter, NULL::TIMESTAMP AS EndTime, FALSE AS IsValid
    FROM PhaseWait1
    WHERE EventId IN (43, 902) AND NextEventId IS NULL
),
PhaseWait AS (
    SELECT * FROM PhaseWait_Matched
    UNION ALL
    SELECT * FROM PhaseWait_Unmatched
),


-- Core derived event windows
Transition1 AS /* intermediate step */
	(
	SELECT *,
	LEAD(TimeStamp) OVER (PARTITION BY DeviceID ORDER BY TimeStamp, Parameter DESC) AS EndTime
	FROM {{from_table}}               
	WHERE EventId = 150 AND Parameter IN (0, 1, 2, 3, 4)
	),
Transition AS
	(
	SELECT *
	FROM Transition1
	WHERE Parameter IN (2, 3, 4)
	),
{{ paired_event('Splits', 0, 12) }},
TSP_AdjustEvents AS /* intermediate step */
	(
	SELECT *,
	LEAD(TimeStamp) OVER (PARTITION BY DeviceID, Parameter ORDER BY TimeStamp, EventId) AS EndTime
	FROM {{from_table}}               
	WHERE EventId IN (113, 114, 115) -- 113=early green, 114=extend green, 115=checkout
	),
TSP_Early_Green_Adjust AS
	(
	SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, TRUE AS IsValid
	FROM TSP_AdjustEvents
	WHERE EventId = 113
	),
TSP_Extend_Green_Adjust AS
	(
	SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, TRUE AS IsValid
	FROM TSP_AdjustEvents
	WHERE EventId = 114
	),
Fault1 AS /* intermediate step */
	(
	SELECT *,
	LEAD(TimeStamp) OVER (PARTITION BY DeviceID, Parameter ORDER BY TimeStamp) AS EndTime,
	LEAD(EventId) OVER (PARTITION BY DeviceID, Parameter ORDER BY TimeStamp) AS NextEventId
	FROM {{from_table}}               
	WHERE EventId IN (83, 84, 85, 86, 87, 88) -- 83 = detector restored
	),
Fault AS
	(
	SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime,
	       CASE WHEN EventId IN (84, 85, 86, 87, 88) AND NextEventId = 83 THEN TRUE ELSE FALSE END AS IsValid
	FROM Fault1
	WHERE EventID IN (84, 85, 86, 87, 88) 
	   OR (EventId = 83 AND NextEventId = 83)
	),
PedDelay1 AS /* intermediate step */
(
	SELECT *,
		LAG(EventId) OVER (PARTITION BY DeviceID, Parameter ORDER BY TimeStamp) AS PrevEvent,
		MIN(CASE WHEN EventId IN (21,22) THEN TimeStamp END)
		OVER (PARTITION BY DeviceID, Parameter 
				ORDER BY TimeStamp 
				ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING) AS WalkStart
	FROM {{from_table}}
	WHERE EventId IN (90, 21, 22)
	),
PedDelay AS (
	SELECT TimeStamp AS StartTime,
			DeviceID,
			EventId,
			Parameter,
			WalkStart AS EndTime, TRUE AS IsValid
	FROM PedDelay1
	WHERE EventId = 90 AND (PrevEvent=22 OR PrevEvent IS NULL OR WalkStart IS NULL)
	),
--Unmatched eventid 90 is accounted for above, 21 is covered by Ped Services, but 22 is not covered by any other event
--so this is just to capture the unmatched event 22 for incremental processing
PedDelayUnmatched AS (
	SELECT TimeStamp AS StartTime,
			DeviceID,
			EventId,
			Parameter,
			WalkStart AS EndTime, FALSE AS IsValid
	FROM PedDelay1
	WHERE EventId = 22 AND WalkStart IS NULL
	),
OverlapEvents AS /* intermediate step */
(
  SELECT
    *,
    LEAD(TimeStamp) OVER (PARTITION BY DeviceID, Parameter ORDER BY TimeStamp, EventId) AS EndTime,
    LEAD(EventId) OVER (PARTITION BY DeviceID, Parameter ORDER BY TimeStamp, EventId) AS NextEventId
  FROM {{from_table}}
  WHERE EventId BETWEEN 61 AND 66
),
OverlapGreen AS (
  -- Overlap Green: starts with EventId 61
  SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime,
         CASE WHEN NextEventId <> 61 THEN TRUE ELSE FALSE END AS IsValid
  FROM OverlapEvents
  WHERE EventId = 61
),
OverlapTrailGreen AS (
  -- Overlap Trail Green: starts with EventId 62
  SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime,
         CASE WHEN NextEventId <> 62 THEN TRUE ELSE FALSE END AS IsValid
  FROM OverlapEvents
  WHERE EventId = 62
),
OverlapYellow AS (
  -- Overlap Yellow: starts with EventId 63
  SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime,
         CASE WHEN NextEventId <> 63 THEN TRUE ELSE FALSE END AS IsValid
  FROM OverlapEvents
  WHERE EventId = 63
),
OverlapRed AS (
  -- Overlap Red: starts with EventId 64
  SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime,
         CASE WHEN NextEventId <> 64 THEN TRUE ELSE FALSE END AS IsValid
  FROM OverlapEvents
  WHERE EventId = 64
),
AlarmStatus1 AS /* intermediate step */
	(
	SELECT *,
	LEAD(TimeStamp) OVER (PARTITION BY DeviceID, EventId ORDER BY TimeStamp, Parameter DESC) AS EndTime
	FROM {{from_table}}
	WHERE EventId IN (173, 174{% if maxtime|default(false) %}, 175{% endif %})
	),
AlarmStatus AS
	(
	SELECT TimeStamp, DeviceID, EventId, Parameter,
	       COALESCE(EndTime, TimeStamp + INTERVAL ({{cushion_time}}) SECOND) AS EndTime,
	       TRUE AS IsValid
	FROM AlarmStatus1
	),
alarm_definitions AS (
  SELECT * FROM (VALUES
    (174, 0, 'Cycle Fault', 1, 'bitmap'),
    (174, 1, 'Coord Fault', 2, 'bitmap'),
    (174, 2, 'Coord Fail', 4, 'bitmap'),
    (174, 3, 'Cycle Fail', 8, 'bitmap'),
    (174, 4, 'MMU Flash', 16, 'bitmap'),
    (174, 5, 'Local Flash', 32, 'bitmap'),
    (173, 1, 'Flash - Other', 1, 'enum'),
    (173, 2, 'Flash - Not Flash', 2, 'enum'),
    (173, 3, 'Flash - Automatic', 3, 'enum'),
    (173, 4, 'Flash - Local Manual', 4, 'enum'),
    (173, 5, 'Flash - Fault Monitor', 5, 'enum'),
    (173, 6, 'Flash - MMU', 6, 'enum'),
    (173, 7, 'Flash - Startup', 7, 'enum'),
    (173, 8, 'Flash - Preempt', 8, 'enum')
  ) AS t(event_id, bit_position, alarm_type, bit_mask, alarm_class)
),
categories AS (
  SELECT * FROM (VALUES
    (901, 'Phase Wait'),
    (902, 'Phase Wait'),
    (150, 'Transition'),
    (102, 'Preempt'),
    (84, 'Other'),
    (85, 'Watchdog'),
    (86, 'Stuck Off'),
    (87, 'Stuck On'),
    (88, 'Erratic'),
    (21, 'Ped Service'),
    (131, 'Pattern Change'),
    (0, 'Split'),
    (43, 'Phase Call'),
    (32, 'FYA'),
    (67, 'Overlap Ped'),
    (112, 'TSP Call'),
    (113, 'TSP Adjustment'),
    (114, 'TSP Adjustment'),
    (118, 'TSP Service'),
    (119, 'TSP Service'),
    (94, 'TSP Detector'),
    (93, 'TSP Detector'),
    (1, 'Green'),
    (8, 'Yellow'),
    (10, 'Red'),
    (176, 'Special Function'),
    (55, 'Advance Warning Phase'),
    (71, 'Advance Warning Overlap'),
    (90, 'Ped Delay'),
    (61, 'Overlap Green'),
    (62, 'Overlap Trail Green'),
    (63, 'Overlap Yellow'),
    (64, 'Overlap Red'),
    (41, 'Phase Hold'),
    (46, 'Phase Omit'),
    (48, 'Ped Omit'),
    (178, 'Manual Control'),
    (179, 'Interval Advance'),
    (180, 'Stop Time Input'),
    (182, 'Power Failure'),
    (184, 'Power Restored'),
    (202, 'Aux Switch'),
    (132, 'Cycle Length Change'),
    (22, 'Ped Delay')--just for unmatched events, 22 is begin FDW, but 21 (begin walk) is already covered
  ) AS t(EventId, EventClass)
)

SELECT *
FROM (
  SELECT
    t.DeviceID AS DeviceId,
    t.EventID AS EventId, --to be dropped after extracting unmatched events
    t.Parameter AS Parameter, --same as above
    t.TimeStamp AS StartTime,
    t.EndTime,
    DATE_DIFF('millisecond', t.TimeStamp, t.EndTime)::FLOAT / 1000 AS Duration,
    t.IsValid AS IsValid,
    CASE
      WHEN t.EventId = 175 THEN 'Alarm Group State' --MAXTIME SPECIFIC
      WHEN a.alarm_type IS NOT NULL THEN a.alarm_type
      WHEN c.EventClass = 'Transition' THEN
        CASE t.Parameter
          WHEN 3 THEN 'Transition Shortway'
          WHEN 2 THEN 'Transition Longway'
          WHEN 4 THEN 'Transition Dwell'
          ELSE 'Transition'
        END
      ELSE c.EventClass
    END AS EventClass,
    CASE
      WHEN t.EventId = 175 THEN t.Parameter
      WHEN a.alarm_type IS NOT NULL THEN NULL
      WHEN c.EventClass = 'Split' THEN t.Parameter
      WHEN c.EventClass IN ('Ped Service', 'FYA', 'Phase Call', 'Preempt', 'TSP Call', 'TSP Checkin', 
                            'TSP Adjustment', 'TSP Service', 'TSP Detector', 'Overlap Ped', 
                            'Pattern Change', 'Erratic', 'Stuck On', 'Stuck Off', 'Other',
                            'Green', 'Yellow', 'Red', 'Special Function', 'Advance Warning Phase', 
                            'Advance Warning Overlap', 'Ped Delay', 'Overlap Green', 
                            'Overlap Trail Green', 'Overlap Yellow', 'Overlap Red',
                            'Phase Hold', 'Phase Omit', 'Ped Omit', 'Aux Switch',
                            'Manual Control', 'Stop Time Input', 'Interval Advance', 'Phase Wait',
                            'Cycle Length Change') THEN t.Parameter
      ELSE NULL
    END AS EventValue
  FROM 
  (
    SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, TRUE AS IsValid FROM Transition
    UNION ALL
    SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM PhaseWait
    UNION ALL
    SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM Preempt
    UNION ALL 
    SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM TSP_Checkin
    UNION ALL
    SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM TSP_Service
    UNION ALL
    SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM TSP_Detector
    UNION ALL
    SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM TSP_Early_Green_Adjust
    UNION ALL
    SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM TSP_Extend_Green_Adjust
    UNION ALL
    SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM Fault  
    UNION ALL
    SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM Ped
    UNION ALL 
    SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM OverlapPed
    UNION ALL
    SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM Coord
    UNION ALL
    SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM PhaseCall
    UNION ALL
    SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM FYA
    UNION ALL
    SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM Splits
  	UNION ALL
  	SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM Green
  	UNION ALL
  	SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM Yellow
  	UNION ALL
  	SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM Red
  	UNION ALL
  	SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM OverlapGreen
  	UNION ALL
  	SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM OverlapTrailGreen
  	UNION ALL
  	SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM OverlapYellow
  	UNION ALL
  	SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM OverlapRed
    UNION ALL
    SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM SpecialFunction
    UNION ALL
    SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM AdvanceWarningPhase
    UNION ALL
    SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM AdvanceWarningOverlap
    UNION ALL
    SELECT StartTime AS TimeStamp, DeviceID, EventId, Parameter, EndTime, IsValid FROM PedDelay
    UNION ALL
    SELECT StartTime AS TimeStamp, DeviceID, EventId, Parameter, EndTime, IsValid FROM PedDelayUnmatched
    UNION ALL
    SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM PhaseHold
    UNION ALL
    SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM PhaseOmit
    UNION ALL
    SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM PedOmit
    UNION ALL
    SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM StopTime
    UNION ALL
    SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM AuxSwitch
    UNION ALL
    SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM PowerFailure
    UNION ALL
    SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM PowerRestored
    UNION ALL
    SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM ManualControl
    UNION ALL
    SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM IntervalAdvance
    UNION ALL
    SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM CycleLengthChange
    UNION ALL
    SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime, IsValid FROM AlarmStatus
  ) t
  LEFT JOIN alarm_definitions a ON t.EventId = a.event_id AND 
    ((a.alarm_class = 'bitmap' AND (t.Parameter & a.bit_mask) > 0) OR
     (a.alarm_class = 'enum' AND t.Parameter = a.bit_mask))
  LEFT JOIN categories c ON t.EventId = c.EventId
) final
WHERE EventClass IS NOT NULL

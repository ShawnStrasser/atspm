--Timeline Events for Troubleshooting by Visualizing What Happened
--Written in SQL for DuckDB. This is a jinja2 template, with variables inside curly braces.

WITH Transition1 AS 
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
	WHERE Parameter IN (2, 3, 4) --AND EndTime IS NOT NULL AND DATE_DIFF('second', TimeStamp, EndTime) < 3600
	),
Preempt1 AS
	(
	SELECT *,
	LEAD(TimeStamp) OVER (PARTITION BY DeviceID, Parameter ORDER BY TimeStamp, EventId) AS EndTime
	FROM {{from_table}}               
	WHERE EventId IN (102, 104)
	),
Preempt AS
	(
	SELECT *
	FROM Preempt1
	WHERE EventID = 102 --AND EndTime IS NOT NULL AND DATE_DIFF('second', TimeStamp, EndTime) < (3600 * 4)
	),
TSP1 AS
	(
	SELECT *,
	LEAD(TimeStamp) OVER (PARTITION BY DeviceID, Parameter ORDER BY TimeStamp, EventId) AS EndTime
	FROM {{from_table}}               
	WHERE EventId IN (112, 115)
	),
TSP AS 
	(
	SELECT *
	FROM TSP1
	WHERE EventID = 112 --AND EndTime IS NOT NULL AND DATE_DIFF('second', TimeStamp, EndTime) < 180 
	),
TSP_adjust_1 AS
	(
	SELECT *,
	LEAD(TimeStamp) OVER (PARTITION BY DeviceID, Parameter ORDER BY TimeStamp, EventId) AS EndTime
	FROM {{from_table}}               
	WHERE EventId IN (113, 114)
	),
TSP_adjust AS
	(
	SELECT *
	FROM TSP_adjust_1
	WHERE EventID = 113 --AND EndTime IS NOT NULL AND DATE_DIFF('second', TimeStamp, EndTime) < 120
	),
Fault1 AS
	(
	SELECT *,
	LEAD(TimeStamp) OVER (PARTITION BY DeviceID, Parameter ORDER BY TimeStamp) AS EndTime
	FROM {{from_table}}               
	WHERE EventId IN (83, 84, 85, 86, 87, 88)
	),
Fault AS
	(
	SELECT *
	FROM Fault1
	WHERE EventID IN (84, 85, 86, 87, 88) --AND EndTime IS NOT NULL 
	  --AND DATE_DIFF('second', TimeStamp, EndTime) > 0 --??
	  --AND DATE_DIFF('second', TimeStamp, EndTime) < (3600 * 12)
	),
Ped2 AS
	(
	SELECT *,
	LEAD(TimeStamp) OVER (PARTITION BY DeviceID, Parameter ORDER BY TimeStamp) AS EndTime
	FROM {{from_table}}               
	WHERE EventId IN (21, 23)
	),
Ped3 AS
	(
	SELECT *
	FROM Ped2
	WHERE EventId = 21 --AND DATE_DIFF('second', TimeStamp, EndTime) < 120
	),
oPed2 AS
	(
	SELECT *,
	LEAD(TimeStamp) OVER (PARTITION BY DeviceID, Parameter ORDER BY TimeStamp) AS EndTime
	FROM {{from_table}}               
	WHERE EventId IN (67, 65)
	),
oPed3 AS
	(
	SELECT * 
	FROM oPed2
	WHERE EventId = 67 --AND DATE_DIFF('second', TimeStamp, EndTime) < 120
	),
Coord AS
	( 
	SELECT *,
	TimeStamp + INTERVAL ({{cushion_time}}) SECOND AS EndTime
	FROM {{from_table}}
	WHERE EventId = 131
	),
Splits AS
	(
	SELECT {{from_table}}.TimeStamp + INTERVAL ({{from_table}}.Parameter * -1) SECOND AS TimeStamp,
	{{from_table}}.DeviceID, 
	{{from_table}}.EventID,
	{{from_table}}.Parameter,
	{{from_table}}.TimeStamp AS EndTime
	FROM {{from_table}}
	WHERE {{from_table}}.EventId BETWEEN 300 AND 315
	),
PhaseCall1 AS
	(
	SELECT *,
	LEAD(TimeStamp) OVER (PARTITION BY DeviceID, Parameter ORDER BY TimeStamp) AS EndTime
	FROM {{from_table}}               
	WHERE EventId IN (43, 44)
	),
PhaseCall AS
	(
	SELECT PhaseCall1.TimeStamp, PhaseCall1.DeviceID, PhaseCall1.EventID, 
	       PhaseCall1.Parameter, PhaseCall1.EndTime
	FROM PhaseCall1
	WHERE PhaseCall1.EventId = 43
	), 
PedDelay1 AS (
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
			WalkStart AS EndTime
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
			WalkStart AS EndTime
	FROM PedDelay1
	WHERE EventId = 22 AND WalkStart IS NULL
	),
PhaseHold1 AS
    (
    SELECT *,
    LEAD(TimeStamp) OVER (PARTITION BY DeviceID, Parameter ORDER BY TimeStamp) AS EndTime
    FROM {{from_table}}               
    WHERE EventId IN (41, 42)
    ),
PhaseHold AS
    (
    SELECT PhaseHold1.TimeStamp, PhaseHold1.DeviceID, PhaseHold1.EventID, 
           PhaseHold1.Parameter, PhaseHold1.EndTime
    FROM PhaseHold1
    WHERE PhaseHold1.EventId = 41
    ),
PhaseOmit1 AS
    (
    SELECT *,
    LEAD(TimeStamp) OVER (PARTITION BY DeviceID, Parameter ORDER BY TimeStamp) AS EndTime
    FROM {{from_table}}               
    WHERE EventId IN (46, 47) and Parameter <= 16
    ),
PhaseOmit AS
    (
    SELECT PhaseOmit1.TimeStamp, PhaseOmit1.DeviceID, PhaseOmit1.EventID, 
           PhaseOmit1.Parameter, PhaseOmit1.EndTime
    FROM PhaseOmit1
    WHERE PhaseOmit1.EventId = 46
    ),
PedOmit1 AS
    (
    SELECT *,
    LEAD(TimeStamp) OVER (PARTITION BY DeviceID, Parameter ORDER BY TimeStamp) AS EndTime
    FROM {{from_table}}               
    WHERE EventId IN (48, 49)
    ),
PedOmit AS
    (
    SELECT PedOmit1.TimeStamp, PedOmit1.DeviceID, PedOmit1.EventID, 
           PedOmit1.Parameter, PedOmit1.EndTime
    FROM PedOmit1
    WHERE PedOmit1.EventId = 48
    ),	
FYA1 AS
	(
	SELECT *,
	LEAD(TimeStamp) OVER (PARTITION BY DeviceID, Parameter ORDER BY TimeStamp) AS EndTime
	FROM {{from_table}}               
	WHERE EventId IN (32, 33)
	),
FYA AS 
	(
	SELECT FYA1.TimeStamp, FYA1.DeviceID, FYA1.EventID, 
	       FYA1.Parameter, FYA1.EndTime  
	FROM FYA1
	WHERE FYA1.EventId = 32
	),
Green1 AS
	(
	SELECT *,
	LEAD(TimeStamp) OVER (PARTITION BY DeviceID, Parameter ORDER BY TimeStamp) AS EndTime
	FROM {{from_table}}               
	WHERE EventId IN (1, 7)
	),
Green AS
	(
	SELECT Green1.TimeStamp, Green1.DeviceID, Green1.EventID, 
	       Green1.Parameter, Green1.EndTime
	FROM Green1
	WHERE Green1.EventId = 1
	),
Yellow1 AS
	(
	SELECT *,
	LEAD(TimeStamp) OVER (PARTITION BY DeviceID, Parameter ORDER BY TimeStamp) AS EndTime
	FROM {{from_table}}               
	WHERE EventId IN (8, 9)
	),
Yellow AS
	(
	SELECT Yellow1.TimeStamp, Yellow1.DeviceID, Yellow1.EventID, 
	       Yellow1.Parameter, Yellow1.EndTime
	FROM Yellow1
	WHERE Yellow1.EventId = 8
	),
Red1 AS
	(
	SELECT *,
	LEAD(TimeStamp) OVER (PARTITION BY DeviceID, Parameter ORDER BY TimeStamp) AS EndTime
	FROM {{from_table}}               
	WHERE EventId IN (10, 11)
	),
Red AS
	(
	SELECT Red1.TimeStamp, Red1.DeviceID, Red1.EventID, 
	       Red1.Parameter, Red1.EndTime
	FROM Red1
	WHERE Red1.EventId = 10
	),
-- New Overlap Events CTE: pulls all events that belong to overlap states
OverlapEvents AS (
  SELECT
    *,
    LEAD(TimeStamp) OVER (PARTITION BY DeviceID, Parameter ORDER BY TimeStamp, EventId) AS EndTime
  FROM {{from_table}}
  WHERE EventId BETWEEN 61 AND 66
),
OverlapGreen AS (
  -- Overlap Green: starts with EventId 61
  SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime
  FROM OverlapEvents
  WHERE EventId = 61
),
OverlapTrailGreen AS (
  -- Overlap Trail Green: starts with EventId 62
  SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime
  FROM OverlapEvents
  WHERE EventId = 62
),
OverlapYellow AS (
  -- Overlap Yellow: starts with EventId 63
  SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime
  FROM OverlapEvents
  WHERE EventId = 63
),
OverlapRed AS (
  -- Overlap Red: starts with EventId 64
  SELECT TimeStamp, DeviceID, EventID, Parameter, EndTime
  FROM OverlapEvents
  WHERE EventId = 64
),
SpecialFunction1 AS
	(
	SELECT *,
	LEAD(TimeStamp) OVER (PARTITION BY DeviceID, Parameter ORDER BY TimeStamp) AS EndTime
	FROM {{from_table}}               
	WHERE EventId IN (176,177)
	),
SpecialFunction AS
	(
	SELECT SpecialFunction1.TimeStamp, SpecialFunction1.DeviceID, SpecialFunction1.EventID, 
	       SpecialFunction1.Parameter, SpecialFunction1.EndTime
	FROM SpecialFunction1
	WHERE SpecialFunction1.EventId = 176
	),
AdvanceWarningPhase1 AS
	(
	SELECT *,
	LEAD(TimeStamp) OVER (PARTITION BY DeviceID, Parameter ORDER BY TimeStamp) AS EndTime
	FROM {{from_table}}               
	WHERE EventId IN (55,56)
	),
AdvanceWarningPhase AS
	(
	SELECT AdvanceWarningPhase1.TimeStamp, AdvanceWarningPhase1.DeviceID, AdvanceWarningPhase1.EventID, 
	       AdvanceWarningPhase1.Parameter, AdvanceWarningPhase1.EndTime
	FROM AdvanceWarningPhase1
	WHERE AdvanceWarningPhase1.EventId = 55
	),
AdvanceWarningOverlap1 AS
	(
	SELECT *,
	LEAD(TimeStamp) OVER (PARTITION BY DeviceID, Parameter ORDER BY TimeStamp) AS EndTime
	FROM {{from_table}}               
	WHERE EventId IN (71,72)
	),
AdvanceWarningOverlap AS
	(
	SELECT AdvanceWarningOverlap1.TimeStamp, AdvanceWarningOverlap1.DeviceID, AdvanceWarningOverlap1.EventID, 
	       AdvanceWarningOverlap1.Parameter, AdvanceWarningOverlap1.EndTime
	FROM AdvanceWarningOverlap1
	WHERE AdvanceWarningOverlap1.EventId = 71
	),
StopTime1 AS
	(
	SELECT *,
	LEAD(TimeStamp) OVER (PARTITION BY DeviceID, Parameter ORDER BY TimeStamp) AS EndTime
	FROM {{from_table}}               
	WHERE EventId = 180
	),
StopTime AS
	(
	SELECT *
	FROM StopTime1
	WHERE Parameter = 1
	),
categories AS (
  SELECT * FROM (VALUES
    (150, 'Transition'),
    (102, 'Preempt'),
	(81, 'Other'),
	(85, 'Watchdog'),
	(86, 'Stuck Off'),
    (87, 'Stuck On'),
    (88, 'Erratic'),
    (21, 'Ped Service'),
    (131, 'Pattern Change'),
    (300, 'Splits'),
    (301, 'Splits'),
    (302, 'Splits'),
    (303, 'Splits'),
    (304, 'Splits'),
    (305, 'Splits'),
    (306, 'Splits'),
    (307, 'Splits'),
    (43, 'Phase Call'),
    (32, 'FYA'),
    (67, 'Overlap Ped'),
    (112, 'TSP Call'),
    (113, 'TSP Adjustment'),
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
	(22, 'unmatched')--just for unmatched events, 22 is begin FDW, but 21 (begin walk) is already covered
  ) AS t(EventId, Category)
)

SELECT
  TIME_BUCKET(INTERVAL '{{bin_size}} minutes', t.TimeStamp) AS TimeStamp,
  t.DeviceID AS DeviceId,
  t.EventID AS EventId, --to be dropped after extracting unmatched events
  t.Parameter AS Parameter, --same as above
  t.TimeStamp AS StartTime,
  t.EndTime,
  DATE_DIFF('millisecond', t.TimeStamp, t.EndTime)::FLOAT / 1000 AS Duration,
  --c.Category AS Category_Basic,
  CASE
-- Add to the CASE statement where other categories are listed:
    WHEN c.Category IN ('Ped Service', 'FYA', 'Phase Call', 'Preempt', 'TSP Call', 
                       'TSP Adjustment', 'Overlap Ped', 'Pattern Change', 'Erratic', 
                       'Stuck On', 'Stuck Off', 'Watchdog', 'Other', 'Green', 'Yellow', 
                       'Red', 'Special Function', 'Advance Warning Phase', 
                       'Advance Warning Overlap', 'Ped Delay', 'Overlap Green', 
                       'Overlap Trail Green', 'Overlap Yellow', 'Overlap Red',
                       'Phase Hold', 'Phase Omit', 'Ped Omit')
		THEN c.Category || ' ' || t.Parameter
    WHEN c.Category = 'Splits'
      THEN c.Category || ' ' || (t.EventId - 299)  
    WHEN c.Category = 'Transition' THEN
      CASE t.Parameter
        WHEN 3 THEN 'Transition Shortway'
        WHEN 2 THEN 'Transition Longway'
        WHEN 4 THEN 'Transition Dwell'
        ELSE c.Category
      END
    ELSE c.Category
  END AS Category
FROM 
(
  SELECT * FROM Transition
  UNION ALL
  SELECT * FROM Preempt
  UNION ALL 
  SELECT * FROM TSP
  UNION ALL
  SELECT * FROM TSP_adjust
  UNION ALL
  SELECT * FROM Fault  
  UNION ALL
  SELECT * FROM Ped3
  UNION ALL 
  SELECT * FROM oPed3
  UNION ALL
  SELECT * FROM Coord
  UNION ALL
  SELECT * FROM PhaseCall
  UNION ALL
  SELECT * FROM FYA
  UNION ALL 
  SELECT * FROM Splits
  {% if not splits_only %}
	UNION ALL
	SELECT * FROM Green
	UNION ALL
	SELECT * FROM Yellow
	UNION ALL
	SELECT * FROM Red
	UNION ALL
	SELECT * FROM OverlapGreen
	UNION ALL
	SELECT * FROM OverlapTrailGreen
	UNION ALL
	SELECT * FROM OverlapYellow
	UNION ALL
	SELECT * FROM OverlapRed
  {% endif %}
  UNION ALL
  SELECT * FROM SpecialFunction
  UNION ALL
  SELECT * FROM AdvanceWarningPhase
  UNION ALL
  SELECT * FROM AdvanceWarningOverlap
  UNION ALL
  SELECT * FROM PedDelay
  UNION ALL
  SELECT * FROM PedDelayUnmatched
  UNION ALL
  SELECT * FROM PhaseHold
  UNION ALL
  SELECT * FROM PhaseOmit
  UNION ALL
  SELECT * FROM PedOmit
  UNION ALL
  SELECT * FROM StopTime
) t
LEFT JOIN categories c ON t.EventId = c.EventId
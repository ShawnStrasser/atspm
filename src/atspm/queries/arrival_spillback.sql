-- Included by arrival_projection.sql when the spillback flag is on. A list of CTEs, not a query.
--
-- A queue that reaches an advance detector stops vehicles on it while the phase is red. When the phase turns green the
-- queue moves, and its vehicles begin actuating the detector on green although they all arrived on red. So a detector
-- ON for more than spillback_on_seconds gets a flag, and every actuation that begins while it holds the flag is an
-- arrival on red. The flag goes when the detector has been OFF for spillback_gap_seconds, not when traffic moves,
-- because the queue clears at about one vehicle every 2 seconds.
--
-- An earlier run's state comes back here: 949 the flag was on, stamped when the detector last turned off or, if it was
-- on, when it turned on. 950 the detector was on, stamped when it turned on. Both with the detector as Parameter.
detector_stream AS (
    -- Step orders events of one moment: state from an earlier run, then off, then on. A vehicle's on is a new arrival.
    SELECT r.TimeStamp,
        r.DeviceId,
        r.Parameter::INT16 AS Parameter,
        (CASE r.EventId WHEN 81 THEN 1 ELSE 2 END)::UTINYINT AS Step,
        r.EventId = 82 AS Is_New
    FROM {{from_table}} r
    SEMI JOIN (SELECT DISTINCT DeviceId, Parameter
               FROM detector_config
               WHERE Function = 'Advance') c
        ON r.DeviceId = c.DeviceId
       AND r.Parameter = c.Parameter
    WHERE r.EventId IN (81, 82)
    {%- if unmatched %}
    UNION ALL
    SELECT TimeStamp,
        DeviceId,
        Parameter::INT16 AS Parameter,
        (CASE EventId WHEN 949 THEN 0 ELSE 2 END)::UTINYINT AS Step,
        FALSE AS Is_New
    FROM unmatched_previous
    WHERE EventId IN (949, 950)
    {%- endif %}
),

-- One row per actuation: when it ended, and whether it held long enough for the flag. A flag from an earlier run is
-- one more such actuation. With its off lost, an actuation is not known to be long, and ended as far as is known when
-- it began. With nothing after it, it is still on.
detector_actuations AS (
    SELECT TimeStamp, DeviceId, Parameter, Step, Is_New,
        Next_Step IS NULL AND Step = 2 AS Still_On,
        CASE WHEN Step = 2 AND Next_Step = 1 THEN Next_Time ELSE TimeStamp END AS End_Time,
        Step = 0 OR (Next_Step = 1 AND DATE_DIFF('millisecond', TimeStamp, Next_Time) > {{spillback_on_seconds}} * 1000) AS Held_Long
    FROM (
        SELECT *,
            LEAD(Step) OVER (PARTITION BY DeviceId, Parameter ORDER BY TimeStamp, Step) AS Next_Step,
            LEAD(TimeStamp) OVER (PARTITION BY DeviceId, Parameter ORDER BY TimeStamp, Step) AS Next_Time
        FROM detector_stream
    )
    WHERE Step <> 1
),

-- A gap of spillback_gap_seconds with the detector OFF starts a new group of actuations
detector_groups AS (
    SELECT *,
        COUNT(*) FILTER (WHERE Starts_Group) OVER (
            PARTITION BY DeviceId, Parameter ORDER BY TimeStamp, Step
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS Group_Number
    FROM (
        SELECT *,
            COALESCE(DATE_DIFF('millisecond',
                LAG(End_Time) OVER (PARTITION BY DeviceId, Parameter ORDER BY TimeStamp, Step),
                TimeStamp) >= {{spillback_gap_seconds}} * 1000, TRUE) AS Starts_Group
        FROM detector_actuations
    )
),

-- An actuation beginning after one that held long, in the same group, begins with the flag on
detector_flags AS (
    SELECT *,
        COALESCE(BOOL_OR(Held_Long) OVER (
            PARTITION BY DeviceId, Parameter, Group_Number ORDER BY TimeStamp, Step
            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
        ), FALSE) AS Spillback
    FROM detector_groups
)

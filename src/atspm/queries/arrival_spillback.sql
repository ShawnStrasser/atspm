-- Included by arrival_projection.sql when the spillback flag is on. A list of CTEs, not a query.
--
-- A queue that reaches an advance detector stops vehicles on it while the phase is red. When the phase turns green the
-- queue moves, and its vehicles begin actuating the detector on green although they all arrived on red. So a detector
-- ON for more than spillback_on_seconds gets a flag, and every actuation that begins while it holds the flag is an
-- arrival on red. The flag goes when the detector has been OFF for spillback_gap_seconds, not when traffic moves,
-- because the queue clears at about one vehicle every 2 seconds.
--
-- The actuation that trips the flag is a car stopped on the detector, so it is an arrival on red too, if its off comes
-- within own_limit_seconds of its on. One on for longer is a detector fault more than a car, and keeps its own reading. So
-- whether a vehicle still on at the end of the data trips the flag is known once its off is seen, or once the data reach
-- own_limit_seconds past its on. Until then it is unresolved, and arrival_hold.sql and approach_delay hold it.
{%- set own_limit_seconds = 360 %}
--
-- An earlier run's state comes back here: 949 the flag was on, stamped when the detector last turned off or, if it was
-- on, when it turned on. 950 the detector was on, stamped when it turned on. Both with the detector as Parameter, plus 1000
-- on a 950 whose vehicle is not counted yet. approach_delay keeps its own, in detector_state, along with when the flag began.
{%- if 'flags' in (shared_stages or []) %}
-- Built once for this run by arrival_shared.sql
detector_flags AS (SELECT * FROM _arrival_flags)
{%- else %}
detector_stream AS (
    -- Step orders events of one moment: state from an earlier run, then off, then on. A vehicle's on is a new arrival.
    SELECT r.TimeStamp,
        {% if int_keys %}c.Dev{% else %}r.DeviceId{% endif %} AS DeviceId,
        r.Parameter::INT16 AS Parameter,
        (CASE r.EventId WHEN 81 THEN 1 ELSE 2 END)::UTINYINT AS Step,
        r.EventId = 82 AS Is_New{% if approach_delay %},
        NULL::TIMESTAMP AS Flag_Began{% endif %}
    FROM {{from_table}} r
    {% if int_keys %}JOIN{% else %}SEMI JOIN{% endif %} (SELECT DISTINCT DeviceId, Parameter{% if int_keys %}, Dev{% endif %}
               FROM detector_config{% if int_keys %} JOIN device_keys USING (DeviceId){% endif %}
               WHERE Function = 'Advance') c
        ON r.DeviceId = c.DeviceId
       AND r.Parameter = c.Parameter
    WHERE r.EventId IN (81, 82)
    {%- if unmatched and approach_delay %}
    UNION ALL
    SELECT s.TimeStamp,
        s.DeviceId,
        s.Detector,
        (CASE s.Kind WHEN 1 THEN 0 ELSE 2 END)::UTINYINT,
        s.Kind = 4,
        CASE WHEN s.Kind = 1 THEN b.TimeStamp END
    FROM (SELECT * FROM detector_state WHERE Kind IN (0, 1, 4)) s
    ASOF LEFT JOIN (SELECT * FROM detector_state WHERE Kind = 2) b
        ON s.DeviceId = b.DeviceId
       AND s.Detector = b.Detector
       AND s.TimeStamp >= b.TimeStamp
    {%- elif unmatched %}
    UNION ALL
    SELECT TimeStamp,
        {% if int_keys %}Dev{% else %}DeviceId{% endif %} AS DeviceId,
        (Parameter % 1000)::INT16 AS Parameter,
        (CASE EventId WHEN 949 THEN 0 ELSE 2 END)::UTINYINT AS Step,
        EventId = 950 AND Parameter >= 1000 AS Is_New
    FROM unmatched_previous{% if int_keys %} JOIN device_keys USING (DeviceId){% endif %}
    WHERE EventId IN (949, 950)
    {%- endif %}
),

-- One row per actuation: when it ended, and whether it held long enough for the flag. A flag from an earlier run is
-- one more such actuation. With its off lost, an actuation is not known to be long, and ended as far as is known when
-- it began. With nothing after it, it is still on. Own_Long: long, and not past the limit, so it is under its own flag.
-- Past_Limit: still on, with the data past the limit, so it is not.
detector_actuations AS (
    SELECT TimeStamp, DeviceId, Parameter, Step, Is_New,{% if approach_delay %} Flag_Began,{% endif %}
        Next_Step IS NULL AND Step = 2 AS Still_On,
        CASE WHEN Step = 2 AND Next_Step = 1 THEN Next_Time ELSE TimeStamp END AS End_Time,
        COALESCE(Step = 0 OR (Next_Step = 1 AND DATE_DIFF('millisecond', TimeStamp, Next_Time) > {{spillback_on_seconds}} * 1000), FALSE) AS Held_Long,
        COALESCE(Next_Step = 1 AND DATE_DIFF('millisecond', TimeStamp, Next_Time) > {{spillback_on_seconds}} * 1000
            AND DATE_DIFF('millisecond', TimeStamp, Next_Time) <= {{own_limit_seconds}} * 1000, FALSE) AS Own_Long,
        COALESCE(Next_Step IS NULL AND Step = 2
            AND DATE_DIFF('millisecond', TimeStamp, (SELECT MAX(TimeStamp) FROM {{from_table}})) > {{own_limit_seconds}} * 1000, FALSE) AS Past_Limit
    FROM (
        SELECT *,
            LEAD(Step) OVER (PARTITION BY DeviceId, Parameter ORDER BY TimeStamp, Step) AS Next_Step,
            LEAD(TimeStamp) OVER (PARTITION BY DeviceId, Parameter ORDER BY TimeStamp, Step) AS Next_Time
        FROM detector_stream
    )
    WHERE Step <> 1
),

-- A gap of spillback_gap_seconds with the detector OFF starts a new group of actuations. Row: place in the detector's
-- order.
detector_groups AS (
    SELECT *{% if approach_delay %},
        COUNT(*) FILTER (WHERE Starts_Group) OVER (
            PARTITION BY DeviceId, Parameter ORDER BY TimeStamp, Step
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS Group_Number{% endif %}
    FROM (
        SELECT *,
            COALESCE(DATE_DIFF('millisecond', LAG(End_Time) OVER detector_order, TimeStamp) >= {{spillback_gap_seconds}} * 1000, TRUE) AS Starts_Group,
            ROW_NUMBER() OVER detector_order AS Row
        FROM detector_actuations
        WINDOW detector_order AS (PARTITION BY DeviceId, Parameter ORDER BY TimeStamp, Step)
    )
),

-- An actuation that begins after one that held long, in the same group, is under the flag, and so is one that is Own_Long:
-- the last that held long before it comes at or after the one that began its group.
-- For approach_delay, the flag began when the first of them turned on, or when an earlier run says. Unresolved: still on,
-- not under the flag yet and not past the limit, so whether it is under the flag is not known yet.
detector_flags AS (
    SELECT *,
        Still_On AND NOT Spillback AND NOT Past_Limit AS Unresolved
    FROM (
        SELECT *,
            Own_Long OR COALESCE(
                MAX(CASE WHEN Held_Long THEN Row END) OVER (detector_order ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
                >= MAX(CASE WHEN Starts_Group THEN Row END) OVER (detector_order ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
                FALSE) AS Spillback{% if approach_delay %},
            COALESCE(MIN(CASE WHEN Held_Long THEN COALESCE(Flag_Began, TimeStamp) END) OVER (
                PARTITION BY DeviceId, Parameter, Group_Number ORDER BY TimeStamp, Step
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            ), CASE WHEN Own_Long THEN TimeStamp END) AS Flag_Start{% endif %}
        FROM detector_groups
        WINDOW detector_order AS (PARTITION BY DeviceId, Parameter ORDER BY TimeStamp, Step)
    )
)
{%- endif %}

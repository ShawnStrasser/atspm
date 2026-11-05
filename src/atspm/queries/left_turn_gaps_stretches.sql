{%- set shortest_slice_milliseconds = 3000 -%}
{%- set longest_slice_seconds = 10 -%}
-- Included by left_turn_gaps.sql and left_turn_gaps_carry.sql. A list of CTEs, not a query.
--
-- A run with a main unmatched file carries, with a slice as phase + 100 times the tenths of its Gap_Length:
-- 927 a lane on at the end of the data, stamped with its device's last event, with the detector as number.
-- 928 a green not ended at the end of the data, stamped when its stretch of busy or empty road began, with the phase as number.
-- 929 the Seconds of a slice of a bin not written yet, stamped with its bin plus one microsecond for each millisecond.
-- 930 the Gaps of a slice of a bin not written yet, stamped with its bin plus one microsecond for each.

-- A phase with a count zone uses its count zones, and otherwise its presence zones. 'Count' sorts first.
lanes AS (
    SELECT DISTINCT DeviceId, Phase, Parameter AS Detector
    FROM detector_config
    WHERE Phase IN (2, 4, 6, 8) AND Function IN ('Count', 'Presence')
    QUALIFY Function = MIN(Function) OVER (PARTITION BY DeviceId, Phase)
),

-- A bin is written once its device's data reach 10 seconds past its end. A gap across the end of the bin has then ended or
-- reached the longest slice, so every gap in the bin has its slice. That is per device, so a wrong clock moves only its own.
device_ends AS (
    SELECT DeviceId,
        MAX(TimeStamp) AS Last_Event,
        time_bucket(INTERVAL '{{bin_size}} minutes', MAX(TimeStamp) - INTERVAL {{longest_slice_seconds}} SECOND) AS Next_Bin,
        time_bucket(INTERVAL '{{bin_size}} minutes', MAX(TimeStamp) FILTER (WHERE From_State) - INTERVAL {{longest_slice_seconds}} SECOND) AS Done_Before
    FROM (
        SELECT DeviceId, TimeStamp, FALSE AS From_State
        FROM {{from_table}}
        WHERE DeviceId IN (SELECT DeviceId FROM lanes)
        {%- if unmatched %}
        UNION ALL
        SELECT DeviceId, TimeStamp, TRUE
        FROM unmatched_previous
        WHERE EventId = 936
        {%- endif %}
    )
    GROUP BY DeviceId
),

-- Each lane's on and off, after any lane on when the last run ended. An on and an off in one stamp leave it as it was.
lane_events AS (
    SELECT r.TimeStamp, r.DeviceId, r.Parameter AS Detector, MAX(r.EventId) = 82 AS Is_On, FALSE AS From_State
    FROM {{from_table}} r
    SEMI JOIN lanes l ON r.DeviceId = l.DeviceId AND r.Parameter = l.Detector
    WHERE r.EventId IN (81, 82)
    GROUP BY r.TimeStamp, r.DeviceId, r.Parameter
    HAVING COUNT(*) = 1
    {%- if unmatched %}
    UNION ALL
    SELECT u.TimeStamp, u.DeviceId, u.Parameter, TRUE, TRUE
    FROM unmatched_previous u
    SEMI JOIN lanes l ON u.DeviceId = l.DeviceId AND u.Parameter = l.Detector
    WHERE u.EventId = 927
    {%- endif %}
),

lane_steps AS (
    SELECT *,
        Is_On::INT - LAG(Is_On::INT, 1, 0) OVER (PARTITION BY DeviceId, Detector ORDER BY TimeStamp) AS Step
    FROM lane_events
),

-- Everything that can change the road on a phase. End of yellow (9) starts red as well as red clearance begin (10), since a signal
-- may log no 10.
phase_steps AS (
    SELECT s.TimeStamp, s.DeviceId, l.Phase, s.Step, NULL::BOOLEAN AS Is_Green
    FROM lane_steps s
    JOIN lanes l ON s.DeviceId = l.DeviceId AND s.Detector = l.Detector
    WHERE s.Step <> 0 AND NOT s.From_State

    UNION ALL

    SELECT r.TimeStamp, r.DeviceId, r.Parameter::INT16, 0, r.EventId = 1
    FROM {{from_table}} r
    SEMI JOIN lanes l ON r.DeviceId = l.DeviceId AND r.Parameter = l.Phase
    WHERE r.EventId IN (1, 9, 10)
    {%- if unmatched %}

    UNION ALL

    SELECT TimeStamp, DeviceId, Parameter, 0, TRUE
    FROM unmatched_previous
    WHERE EventId = 928

    UNION ALL

    -- A lane on when the last run ended counts from the start of the stretch that run left open
    SELECT COALESCE(g.TimeStamp, s.TimeStamp), s.DeviceId, l.Phase, s.Step, NULL
    FROM lane_steps s
    JOIN lanes l ON s.DeviceId = l.DeviceId AND s.Detector = l.Detector
    LEFT JOIN unmatched_previous g ON g.EventId = 928 AND s.DeviceId = g.DeviceId AND l.Phase = g.Parameter
    WHERE s.From_State
    {%- endif %}
),

-- At each moment, whether the road is empty, or NULL on red
road AS (
    SELECT TimeStamp, DeviceId, Phase,
        CASE WHEN COALESCE(LAST_VALUE(Is_Green IGNORE NULLS) OVER up_to_here, FALSE) THEN SUM(Step) OVER up_to_here = 0 END AS Is_Empty
    FROM (
        -- Green and red in one stamp reads as red
        SELECT TimeStamp, DeviceId, Phase, SUM(Step) AS Step, BOOL_AND(Is_Green) AS Is_Green
        FROM phase_steps
        GROUP BY TimeStamp, DeviceId, Phase
    )
    WINDOW up_to_here AS (PARTITION BY DeviceId, Phase ORDER BY TimeStamp ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
),

-- Stretches of busy or empty road in a green, each from one change to the next. The last may not have ended yet.
stretches AS (
    SELECT TimeStamp AS Start_Time, DeviceId, Phase, Is_Empty,
        LEAD(TimeStamp) OVER (PARTITION BY DeviceId, Phase ORDER BY TimeStamp) AS End_Time
    FROM (
        SELECT *, Is_Empty IS DISTINCT FROM LAG(Is_Empty) OVER (PARTITION BY DeviceId, Phase ORDER BY TimeStamp) AS Changed
        FROM road
    )
    WHERE Changed
),

-- Each stretch split at bin edges, leaving out bins already written. All of an empty stretch is in the slice of its whole length.
-- Busy road and gaps under the shortest slice are in the slice of 0.
pieces AS (
    SELECT s.DeviceId, s.Phase, b.Bin,
        CASE WHEN NOT s.Is_Empty OR s.Length_Milliseconds < {{shortest_slice_milliseconds}} THEN 0
             ELSE LEAST(s.Length_Milliseconds // 100, {{longest_slice_seconds}} * 10) END AS Tenths,
        DATE_DIFF('millisecond', GREATEST(s.Start_Time, b.Bin), LEAST(s.Until, b.Bin + INTERVAL '{{bin_size}} minutes')) AS Milliseconds,
        (s.Is_Empty AND s.End_Time IS NOT NULL AND b.Bin = time_bucket(INTERVAL '{{bin_size}} minutes', s.End_Time - INTERVAL 1 MICROSECOND))::INT AS Gaps,
        s.End_Time IS NOT NULL AS Has_Ended
    FROM (
        SELECT s.*, e.Done_Before,
            COALESCE(s.End_Time, e.Last_Event) AS Until,
            DATE_DIFF('millisecond', s.Start_Time, COALESCE(s.End_Time, e.Last_Event)) AS Length_Milliseconds
        FROM stretches s
        JOIN device_ends e ON s.DeviceId = e.DeviceId
        WHERE s.Is_Empty IS NOT NULL
          AND COALESCE(s.End_Time, e.Last_Event) > s.Start_Time
    ) s, LATERAL UNNEST(generate_series(
        GREATEST(time_bucket(INTERVAL '{{bin_size}} minutes', s.Start_Time), s.Done_Before),
        time_bucket(INTERVAL '{{bin_size}} minutes', s.Until - INTERVAL 1 MICROSECOND),
        INTERVAL '{{bin_size}} minutes'
    )) AS b(Bin)
),

-- What each bin has so far: this run's pieces and the sums carried from earlier ones
bin_parts AS (
    SELECT Bin, DeviceId, Phase, Tenths, Milliseconds, Gaps, Has_Ended
    FROM pieces
    {%- if unmatched %}
    UNION ALL
    SELECT time_bucket(INTERVAL '{{bin_size}} minutes', TimeStamp),
        DeviceId,
        (Parameter % 100)::INT16,
        Parameter // 100,
        CASE WHEN EventId = 929 THEN DATE_DIFF('microsecond', time_bucket(INTERVAL '{{bin_size}} minutes', TimeStamp), TimeStamp) ELSE 0 END,
        CASE WHEN EventId = 930 THEN DATE_DIFF('microsecond', time_bucket(INTERVAL '{{bin_size}} minutes', TimeStamp), TimeStamp) ELSE 0 END,
        TRUE
    FROM unmatched_previous
    WHERE EventId IN (929, 930)
    {%- endif %}
)

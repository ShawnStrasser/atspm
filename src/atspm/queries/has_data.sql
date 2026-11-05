-- has_data: at most one row per signal and finished bin. Score = 1 - minutes of the bin in an unexpected gap / minutes of the bin. No row reads as Score 0.
--
-- Only all events together count (EventId <= 218). A quiet detector or a lack of calls is not data loss while anything else is logged.
-- A gap is a run of whole clock minutes with no event at all. It is unexpected when it runs over 4 times the signal's recent longest gap, the
-- longest in its last 4 bins with events, and over 2 minutes. All of a gap's minutes in a bin count once it has run that long by the
-- end of that bin. A bin with no unexpected gap reads 1, however quiet.
-- A flash status (173 with Parameter other than 2) clears the memory, as a signal in flash can be silent. Until 4 bins with events are remembered, nothing is judged.
--
-- A bin is finished by the first run whose data reaches its end, as in results_end.sql. A score reads nothing later than the end of
-- its bin, so a run cut anywhere gives the same rows, written once. A run with no unmatched events file writes all it has. An event that arrives
-- after its bin was written counts in the first bin not written. A signal is known from its first event until it has logged nothing for max_days_old.
--
-- Carried in unmatched_events as synthetic events (TimeStamp, DeviceId, EventId, Parameter):
--   970  last event (TimeStamp)                                                                   always
--   974  first bin not written (TimeStamp)                                                        always
--   997  each of the last 4 finished bins with events: order only (TimeStamp), longest gap, minutes, -1 = flash status (Parameter)
-- Only while a bin is part way through, all with TimeStamp = that bin unless noted:
--   984 longest gap so far, minutes, 993 minutes in unexpected gaps so far, 991 flash status seen (TimeStamp = that event)
-- This query returns Kind 'score' rows and Kind 'state' rows.
-- Written in SQL for DuckDB. This is a jinja2 template, with variables inside curly braces.

WITH
state AS MATERIALIZED (
    {%- if unmatched %}
    SELECT TimeStamp, DeviceId, EventId::INT AS EventId, Parameter::INT AS Parameter FROM unmatched_previous WHERE EventId BETWEEN 970 AND 999
    {%- else %}
    SELECT TimeStamp, DeviceId, EventId::INT AS EventId, Parameter::INT AS Parameter FROM {{from_table}} WHERE FALSE
    {%- endif %}
),
ev AS MATERIALIZED (
    SELECT DeviceId, TimeStamp, EventId::INT AS EventId, Parameter::INT AS Parameter,
           GREATEST(time_bucket(INTERVAL '{{bin_size}} minutes', TimeStamp), (SELECT MAX(TimeStamp) FROM state WHERE EventId = 974)) AS Bin
    FROM {{from_table}} WHERE EventId <= 218
),
-- Bins before this are finished: the same rule as results_end.sql
clock AS MATERIALIZED (
    SELECT GREATEST(
        CASE WHEN Bin_Start + INTERVAL '{{bin_size}} minutes' - Last_Event <= INTERVAL 1 SECOND
             THEN Bin_Start + INTERVAL '{{bin_size}} minutes'
             {%- if carry_bins %} ELSE Bin_Start{% else %} ELSE Bin_Start + INTERVAL '{{bin_size}} minutes'{% endif %} END,
        (SELECT MAX(TimeStamp) FROM state WHERE EventId = 974)) AS Finish
    FROM (SELECT MAX(TimeStamp) AS Last_Event, time_bucket(INTERVAL '{{bin_size}} minutes', MAX(TimeStamp)) AS Bin_Start FROM {{from_table}})
),
flash_bin AS MATERIALIZED (
    SELECT DeviceId, Bin, MIN(F) AS F FROM (
        SELECT DeviceId, Bin, TimeStamp AS F FROM ev WHERE EventId = 173 AND Parameter <> 2
        UNION ALL
        SELECT DeviceId, time_bucket(INTERVAL '{{bin_size}} minutes', TimeStamp), TimeStamp FROM state WHERE EventId = 991
    ) GROUP BY 1, 2
),
seen AS (SELECT DeviceId, TimeStamp AS Last_T FROM state WHERE EventId = 970),
open_bin AS MATERIALIZED (SELECT DeviceId, MAX(TimeStamp) AS Open_Bin FROM state WHERE EventId = 974 GROUP BY 1),
gaps AS MATERIALIZED (
    SELECT DeviceId, Bin, TimeStamp AS B, A FROM (
        SELECT DeviceId, Bin, TimeStamp, Seed, LAG(TimeStamp) OVER (PARTITION BY DeviceId ORDER BY TimeStamp) AS A
        FROM (
            SELECT DeviceId, Bin, TimeStamp, FALSE AS Seed FROM ev
            UNION ALL
            SELECT DeviceId, NULL, Last_T, TRUE FROM seen
        )
    ) WHERE NOT Seed
),
-- Per bin: its longest gap, in empty minutes. A gap from before the bin counts whole. A signal's first event has no gap before it.
bin_stats AS MATERIALIZED (
    SELECT DeviceId, Bin, MAX(M) AS M FROM (
        SELECT DeviceId, Bin, MAX(CASE WHEN A IS NOT NULL THEN LEAST(GREATEST(EPOCH(DATE_TRUNC('minute', B) - DATE_TRUNC('minute', A)) / 60 - 1, 0), 32767) END)::INT AS M FROM gaps GROUP BY 1, 2
        UNION ALL
        SELECT DeviceId, TimeStamp, Parameter FROM state WHERE EventId = 984
    ) GROUP BY 1, 2
),
-- Each bin with a gap to go by: its longest gap, capped at 6 hours, or -1 for a flash status
memory AS MATERIALIZED (
    SELECT DeviceId, Bin, L, SUM((L < 0)::INT) OVER (PARTITION BY DeviceId ORDER BY Bin ROWS UNBOUNDED PRECEDING) AS Since_Flash FROM (
        SELECT s.DeviceId, s.Bin, CASE WHEN f.F IS NOT NULL THEN -1 ELSE LEAST(s.M, 360) END AS L
        FROM bin_stats s LEFT JOIN flash_bin f ON f.DeviceId = s.DeviceId AND f.Bin = s.Bin
        WHERE s.M IS NOT NULL OR f.F IS NOT NULL
        UNION ALL
        SELECT DeviceId, TimeStamp, Parameter FROM state WHERE EventId = 997
    )
),
-- What each bin goes by: the longest of the last 4 bins, once there are 4, none from before a flash status. Windows only here: joins into this
-- materialized table fired a DuckDB 2.0.0 internal error ("dict-surviving cache") in about 1.5% of runs.
chain AS MATERIALIZED (
    SELECT DeviceId, Bin,
           CASE WHEN COUNT(CASE WHEN L >= 0 THEN 1 END) OVER (w ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING) = 4
                THEN MAX(CASE WHEN L >= 0 THEN L END) OVER (w ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING) END AS Prev_G,
           CASE WHEN COUNT(CASE WHEN L >= 0 THEN 1 END) OVER (w ROWS BETWEEN 3 PRECEDING AND CURRENT ROW) = 4
                THEN MAX(CASE WHEN L >= 0 THEN L END) OVER (w ROWS BETWEEN 3 PRECEDING AND CURRENT ROW) END AS Mem_G
    FROM memory
    WINDOW w AS (PARTITION BY DeviceId, Since_Flash ORDER BY Bin)
),
-- What the memory says as this run starts
carried AS MATERIALIZED (
    SELECT DeviceId, FIRST(Mem_G ORDER BY Bin DESC) AS Mem_G FROM chain
    WHERE Bin < (SELECT MAX(TimeStamp) FROM state WHERE EventId = 974) GROUP BY 1
),
last_event AS MATERIALIZED (
    SELECT DeviceId, MAX(T) AS Last_T FROM (SELECT DeviceId, B AS T FROM gaps UNION ALL SELECT DeviceId, Last_T FROM seen) GROUP BY 1
),
-- Every gap to judge: those closed in this run, and each signal's gap still open at the end of it. S to E is its run of empty minutes.
silences AS MATERIALIZED (
    SELECT DeviceId, A, B, DATE_TRUNC('minute', A) + INTERVAL 1 MINUTE AS S, DATE_TRUNC('minute', B) AS E, time_bucket(INTERVAL '{{bin_size}} minutes', A) AS Bin_A,
           time_bucket(INTERVAL '{{bin_size}} minutes', LEAST(B, (SELECT Finish FROM clock) + INTERVAL '{{bin_size}} minutes')) AS Bin_B
    FROM (
        SELECT DeviceId, A, B FROM gaps WHERE A IS NOT NULL
        UNION ALL
        SELECT DeviceId, Last_T,
               {%- if carry_bins %} TIMESTAMP '9999-01-01'{% else %} (SELECT MAX(TimeStamp) FROM {{from_table}}){% endif %} FROM last_event
    )
),
-- A gap inside one bin needs one test. Only one that crosses the end of a bin is split, one row per bin.
parts AS MATERIALIZED (
    SELECT k.DeviceId, k.S, k.E, k.B, k.Bin_A, k.Bin_A AS Bin, c.Prev_G AS G
    FROM silences k JOIN chain c ON c.DeviceId = k.DeviceId AND c.Bin = k.Bin_A
    WHERE k.Bin_A = k.Bin_B AND c.Prev_G IS NOT NULL AND EPOCH(k.E - k.S) / 60 > GREATEST(4 * c.Prev_G, 2)
    UNION ALL
    SELECT DeviceId, S, E, B, Bin_A, Bin, CASE WHEN Bin = Bin_A THEN G_First ELSE G_Later END
    FROM (
        SELECT k.DeviceId, k.S, k.E, k.B, k.Bin_A,
               UNNEST(GENERATE_SERIES(k.Bin_A, k.Bin_B, INTERVAL '{{bin_size}} minutes')) AS Bin,
               c.Prev_G AS G_First, CASE WHEN c.Bin IS NOT NULL THEN c.Mem_G ELSE m.Mem_G END AS G_Later
        FROM silences k
        LEFT JOIN chain c ON c.DeviceId = k.DeviceId AND c.Bin = k.Bin_A
        LEFT JOIN carried m ON m.DeviceId = k.DeviceId
        WHERE k.Bin_A < k.Bin_B
    )
),
-- Whole minutes, so one big run and many small ones add up the same
lost AS MATERIALIZED (
    SELECT DeviceId, Bin, SUM(Lost)::INT AS Lost FROM (
        SELECT p.DeviceId, p.Bin, GREATEST(EPOCH(LEAST(p.E, p.Bin + INTERVAL '{{bin_size}} minutes') - GREATEST(p.S, p.Bin)) / 60, 0) AS Lost
        FROM parts p LEFT JOIN open_bin o ON o.DeviceId = p.DeviceId
        WHERE p.G IS NOT NULL AND p.Bin >= COALESCE(o.Open_Bin, p.Bin_A)
          AND EPOCH(LEAST(p.E, p.Bin + INTERVAL '{{bin_size}} minutes') - p.S) / 60 > GREATEST(4 * p.G, 2)
          -- a gap not closed by this run counts only for the bins this run finished
          AND (p.B < TIMESTAMP '9999-01-01' OR p.Bin < (SELECT Finish FROM clock))
        UNION ALL
        SELECT DeviceId, TimeStamp, Parameter FROM state WHERE EventId = 993
    ) GROUP BY 1, 2
),
-- A signal has rows while it is remembered. One that has aged out starts again at its next event.
device_start AS MATERIALIZED (
    SELECT DeviceId, COALESCE(MAX(o.Open_Bin), MIN(First_Bin)) AS Start_Bin FROM (
        SELECT DeviceId, MIN(Bin) AS First_Bin FROM ev GROUP BY 1
        UNION ALL
        SELECT DeviceId, NULL FROM seen
    ) d LEFT JOIN open_bin o USING (DeviceId) GROUP BY 1
),
grid AS (
    SELECT DeviceId, UNNEST(GENERATE_SERIES(Start_Bin, (SELECT Finish FROM clock) - INTERVAL '{{bin_size}} minutes', INTERVAL '{{bin_size}} minutes')) AS Bin
    FROM device_start WHERE Start_Bin < (SELECT Finish FROM clock)
),
state_rows AS (
    SELECT Last_T AS TimeStamp, DeviceId, 970 AS EventId, 0 AS Parameter FROM last_event
    UNION ALL
    SELECT (SELECT Finish FROM clock), DeviceId, 974, 0 FROM last_event
    UNION ALL
    -- The last 4 finished bins, kept in order. Past a day the exact time no longer matters, since 4 times the longest gap remembered is a day.
    SELECT GREATEST(k.Bin, a.Last_T - INTERVAL 1 DAY - k.K * INTERVAL '{{bin_size}} minutes'), k.DeviceId, 997, k.L::INT
    FROM (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY DeviceId ORDER BY Bin DESC) AS K FROM memory WHERE Bin < (SELECT Finish FROM clock)
    ) k JOIN last_event a ON a.DeviceId = k.DeviceId
    WHERE k.K <= 4
    UNION ALL
    SELECT F, DeviceId, 991, 0 FROM flash_bin WHERE Bin >= (SELECT Finish FROM clock)
    UNION ALL
    SELECT Bin, DeviceId, 984, M FROM bin_stats WHERE Bin >= (SELECT Finish FROM clock) AND M IS NOT NULL
    UNION ALL
    SELECT Bin, DeviceId, 993, Lost FROM lost WHERE Bin >= (SELECT Finish FROM clock) AND Lost > 0
),
scored AS (
    SELECT g.Bin, g.DeviceId, (1 - LEAST(COALESCE(l.Lost, 0), {{bin_size}}) / {{bin_size}}.0)::FLOAT AS Score
    FROM grid g LEFT JOIN lost l ON l.DeviceId = g.DeviceId AND l.Bin = g.Bin
)
-- No row is written at Score 0, so no row means Score 0, the way a missing has_data row reads today.
SELECT 'score' AS Kind, Bin AS TimeStamp, DeviceId, Score, NULL::INT AS EventId, NULL::INT AS Parameter FROM scored WHERE Score > 0
UNION ALL
SELECT 'state', TimeStamp, DeviceId, NULL, EventId, Parameter FROM state_rows

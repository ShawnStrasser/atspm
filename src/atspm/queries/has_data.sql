-- has_data: at most one row per signal and finished bin, with a Score above 0 and up to 1 for how complete its events are. No row reads as 0.
--
-- Order rule: each phase logs green, yellow and red in a fixed order, so a step that skips a state proves those events were lost.
--     Share kept = phase events seen / (phase events seen + proved missing).
-- Stop rule: all events, phase calls and detector events are three streams. A stream silent for over 4 times its usual longest gap (the
--     median of the longest gap in each of its last 4 bins with events) has stopped, and what it was logging counts as missing for that
--     silence. Share kept = all events seen / (all events seen + stopped missing).
-- Score = the smaller share. A bin with no evidence of either kind reads 1, however quiet it was.
--
-- Reads EventId <= 218. Order rule: 1 7 8 9 10 11, by phase. Stop rule: every event / 43 44 / 82. Event 173 (flash status) ends order
-- checks across it, and one that does not read Parameter 2 clears what the streams remember, as a signal in flash can be silent.
--
-- A bin is finished by the first run whose data reaches its end, as in results_end.sql. A score reads nothing later than the end of
-- its bin, so a run cut anywhere gives the same rows, written once. A run with no unmatched events file writes all it has. An event that arrives after its bin was written counts in the first
-- bin not written. A signal is known from its first event until it has logged nothing for max_days_old.
--
-- Carried in unmatched_events as synthetic events (TimeStamp, DeviceId, EventId, Parameter). For stream c = 0 all, 1 calls, 2 detectors:
--   970+c  last event of the stream (TimeStamp), events in its last bin not half quiet (Parameter)                always
--   974    first bin not written (TimeStamp)                                                                     always, one per signal
--   979    one row per phase: last event (TimeStamp), Parameter = phase * 512 + states seen today * 8 + position (7 = do not judge)
--   997+c  each of the stream's last 4 finished bins with events: order only (TimeStamp), longest gap, seconds (Parameter)
-- Only while a bin is part way through, all with TimeStamp = that bin unless noted:
--   980+c events so far, 984+c longest gap so far, 994+c longest gap so far inside the bin, 987 stopped missing (all stream),
--   988 all events so far, 989 phase events so far, 990 proved missing, 992 stopped missing (other streams),
--   991 flash status seen (TimeStamp = that event)
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
flash AS MATERIALIZED (SELECT DeviceId, TimeStamp AS F, Parameter, Bin FROM ev WHERE EventId = 173),
flash_bin AS MATERIALIZED (
    SELECT DeviceId, Bin, MIN(F) AS F FROM (
        SELECT DeviceId, Bin, F FROM flash WHERE Parameter <> 2
        UNION ALL
        SELECT DeviceId, time_bucket(INTERVAL '{{bin_size}} minutes', TimeStamp), TimeStamp FROM state WHERE EventId = 991
    ) GROUP BY 1, 2
),

-- ---------- Stop rule ----------
st_last AS (SELECT DeviceId, EventId - 970 AS Stream, TimeStamp AS Last_T, Parameter AS R FROM state WHERE EventId BETWEEN 970 AND 972),
open_bin AS MATERIALIZED (SELECT DeviceId, MAX(TimeStamp) AS Open_Bin FROM state WHERE EventId = 974 GROUP BY 1),
gaps AS MATERIALIZED (
    SELECT DeviceId, Stream, Bin, TimeStamp AS B, A FROM (
        SELECT DeviceId, Stream, Bin, TimeStamp, Seed, LAG(TimeStamp) OVER (PARTITION BY DeviceId, Stream ORDER BY TimeStamp) AS A
        FROM (
            SELECT DeviceId, Bin, TimeStamp, FALSE AS Seed, 0 AS Stream FROM ev
            UNION ALL
            SELECT DeviceId, Bin, TimeStamp, FALSE, CASE EventId WHEN 82 THEN 2 ELSE 1 END FROM ev WHERE EventId IN (43, 44, 82)
            UNION ALL
            SELECT DeviceId, NULL, Last_T, TRUE, Stream FROM st_last
        )
    ) WHERE NOT Seed
),
-- Per stream and bin: events, longest gap (a gap from before the bin counts whole), longest gap counting only its part inside the bin
bin_stats AS MATERIALIZED (
    SELECT DeviceId, Stream, Bin, SUM(N)::INT AS N, MAX(M) AS M, MAX(Q) AS Q, MAX(Last_In) AS Last_In FROM (
        SELECT DeviceId, Stream, Bin, COUNT(*) AS N,
               MAX(LEAST(CEIL(EPOCH(B - COALESCE(A, Bin))), 32767))::INT AS M,
               MAX(LEAST(CEIL(EPOCH(B - GREATEST(COALESCE(A, Bin), Bin))), 32767))::INT AS Q,
               MAX(B) AS Last_In
        FROM gaps GROUP BY 1, 2, 3
        UNION ALL
        SELECT DeviceId, EventId - 980, TimeStamp, Parameter, NULL, NULL, NULL FROM state WHERE EventId BETWEEN 980 AND 982
        UNION ALL
        SELECT DeviceId, EventId - 984, TimeStamp, 0, Parameter, NULL, NULL FROM state WHERE EventId BETWEEN 984 AND 986
        UNION ALL
        SELECT DeviceId, EventId - 994, TimeStamp, 0, NULL, Parameter, NULL FROM state WHERE EventId BETWEEN 994 AND 996
        UNION ALL
        SELECT DeviceId, Stream, time_bucket(INTERVAL '{{bin_size}} minutes', Last_T), 0, NULL, NULL, Last_T FROM st_last
    ) GROUP BY 1, 2, 3 HAVING SUM(N) > 0
),
-- Per stream, each bin with events and each bin carried: what it logged if it was not half quiet, counting the start and the end of the
-- bin, so a bin cut short by a stop, a blip and a resume leave that as it was; and its longest gap, capped at 6 hours. A flash status clears both.
stream_bins AS MATERIALIZED (
    SELECT DeviceId, Stream, Bin,
           CASE WHEN F IS NOT NULL THEN 0 WHEN Quiet < {{bin_size}} * 30 THEN LEAST(N, 32767) END AS Own_R,
           CASE WHEN F IS NOT NULL THEN 0 ELSE LEAST(COALESCE(M, 0), 21600) END AS L
    FROM (
        SELECT s.*, f.F, GREATEST(COALESCE(s.Q, 0), CEIL(EPOCH(s.Bin + INTERVAL '{{bin_size}} minutes' - s.Last_In))) AS Quiet
        FROM bin_stats s LEFT JOIN flash_bin f ON f.DeviceId = s.DeviceId AND f.Bin = s.Bin
    )
    UNION ALL
    SELECT DeviceId, EventId - 997, TimeStamp, NULL, Parameter FROM state WHERE EventId BETWEEN 997 AND 999
    UNION ALL
    SELECT DeviceId, Stream, TIMESTAMP '1970-01-01', R, NULL FROM st_last
),
-- What each bin goes by. G is the usual longest gap: the median of the last 4 bins with events. Windows only here: joins into this
-- materialized table fired a DuckDB 2.0.0 internal error ("dict-surviving cache") in about 1.5% of runs.
chain AS MATERIALIZED (
    SELECT DeviceId, Stream, Bin,
           LAST_VALUE(Own_R IGNORE NULLS) OVER (w ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS Prev_R,
           LAST_VALUE(Own_R IGNORE NULLS) OVER (w ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS Mem_R,
           MEDIAN(L) OVER (w ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING) AS Prev_G,
           MEDIAN(L) OVER (w ROWS BETWEEN 3 PRECEDING AND CURRENT ROW) AS Mem_G
    FROM stream_bins
    WINDOW w AS (PARTITION BY DeviceId, Stream ORDER BY Bin)
),
-- What the state remembers as this run starts
carried AS MATERIALIZED (
    SELECT DeviceId, Stream, ARG_MAX(Mem_R, Bin) AS Mem_R, ARG_MAX(Mem_G, Bin) AS Mem_G FROM chain
    WHERE Bin < (SELECT MAX(TimeStamp) FROM state WHERE EventId = 974) GROUP BY 1, 2
),
-- Every silence to judge: gaps closed in this run, and each stream's silence still open at the end of it
stream_last AS MATERIALIZED (
    SELECT DeviceId, Stream, MAX(T) AS Last_T FROM (
        SELECT DeviceId, Stream, B AS T FROM gaps UNION ALL SELECT DeviceId, Stream, Last_T FROM st_last
    ) GROUP BY 1, 2
),
silences AS MATERIALIZED (
    SELECT DeviceId, Stream, A, B, time_bucket(INTERVAL '{{bin_size}} minutes', A) AS Bin_A,
           time_bucket(INTERVAL '{{bin_size}} minutes', LEAST(B, (SELECT Finish FROM clock) + INTERVAL '{{bin_size}} minutes')) AS Bin_B
    FROM (
        SELECT DeviceId, Stream, A, B FROM gaps WHERE A IS NOT NULL
        UNION ALL
        SELECT DeviceId, Stream, Last_T,
               {%- if carry_bins %} TIMESTAMP '9999-01-01'{% else %} (SELECT MAX(TimeStamp) FROM {{from_table}}){% endif %} FROM stream_last
    )
),
-- A silence inside one bin needs one test. Only one that crosses the end of a bin is split, one row per bin.
parts AS MATERIALIZED (
    SELECT k.DeviceId, k.Stream, k.A, k.B, k.Bin_A, k.Bin_A AS Bin, c.Prev_R AS R, c.Prev_G AS G
    FROM silences k JOIN chain c ON c.DeviceId = k.DeviceId AND c.Stream = k.Stream AND c.Bin = k.Bin_A
    WHERE k.Bin_A = k.Bin_B AND EPOCH(k.B - k.A) > 4 * c.Prev_G
    UNION ALL
    SELECT DeviceId, Stream, A, B, Bin_A, Bin,
           CASE WHEN Bin = Bin_A THEN R_First ELSE R_Later END, CASE WHEN Bin = Bin_A THEN G_First ELSE G_Later END
    FROM (
        SELECT k.DeviceId, k.Stream, k.A, k.B, k.Bin_A,
               UNNEST(GENERATE_SERIES(k.Bin_A, k.Bin_B, INTERVAL '{{bin_size}} minutes')) AS Bin,
               c.Prev_R AS R_First, c.Prev_G AS G_First, COALESCE(c.Mem_R, m.Mem_R) AS R_Later, COALESCE(c.Mem_G, m.Mem_G) AS G_Later
        FROM silences k
        LEFT JOIN chain c ON c.DeviceId = k.DeviceId AND c.Stream = k.Stream AND c.Bin = k.Bin_A
        LEFT JOIN carried m ON m.DeviceId = k.DeviceId AND m.Stream = k.Stream
        WHERE k.Bin_A < k.Bin_B
    )
),
stopped AS MATERIALIZED (
    SELECT DeviceId, Bin,
           COALESCE(SUM(Miss) FILTER (WHERE Stream = 0), 0)::INT AS Stop_All, COALESCE(SUM(Miss) FILTER (WHERE Stream > 0), 0)::INT AS Stop_Parts
    FROM (
        SELECT p.DeviceId, p.Stream, p.Bin,
               GREATEST(1, ROUND(p.R * EPOCH(LEAST(p.B, p.Bin + INTERVAL '{{bin_size}} minutes') - GREATEST(p.A, p.Bin)) / ({{bin_size}} * 60.0))) AS Miss
        FROM parts p
        LEFT JOIN open_bin o ON o.DeviceId = p.DeviceId
        LEFT JOIN flash_bin f ON f.DeviceId = p.DeviceId AND f.Bin = p.Bin
        WHERE p.R > 0 AND p.G > 0
          AND p.Bin >= COALESCE(o.Open_Bin, p.Bin_A)
          AND EPOCH(LEAST(p.B, p.Bin + INTERVAL '{{bin_size}} minutes') - p.A) > 4 * p.G
          -- a silence not closed by this run counts only for the bins this run finished
          AND (p.B < TIMESTAMP '9999-01-01' OR p.Bin < (SELECT Finish FROM clock))
          AND NOT (f.F IS NOT NULL AND LEAST(p.B, p.Bin + INTERVAL '{{bin_size}} minutes') > f.F)
    ) GROUP BY 1, 2
),

-- ---------- Order rule ----------
ring_all AS MATERIALIZED (
    SELECT DeviceId, Ch, TimeStamp, Pos, Bits, Seed,
           LAG(Pos) OVER w AS Prev_Pos, LAG(TimeStamp) OVER w AS Prev_T,
           BIT_OR(Bits) OVER (PARTITION BY DeviceId, Ch, TimeStamp::DATE ORDER BY TimeStamp, Tie ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS Seen_Before,
           ROW_NUMBER() OVER (PARTITION BY DeviceId, Ch ORDER BY TimeStamp DESC, Tie DESC) AS Newest
    FROM (
        SELECT DeviceId, Parameter AS Ch, TimeStamp, FALSE AS Seed,
               CASE EventId WHEN 1 THEN 0 WHEN 7 THEN 1 WHEN 8 THEN 2 WHEN 9 THEN 3 WHEN 10 THEN 4 ELSE 5 END AS Pos,
               1 << (CASE EventId WHEN 1 THEN 0 WHEN 7 THEN 1 WHEN 8 THEN 2 WHEN 9 THEN 3 WHEN 10 THEN 4 ELSE 5 END) AS Bits,
               -- end of red is logged before the next green when they share a tenth of a second
               CASE EventId WHEN 11 THEN -1 WHEN 1 THEN 0 WHEN 7 THEN 1 WHEN 8 THEN 2 WHEN 9 THEN 3 ELSE 4 END AS Tie
        FROM ev WHERE EventId IN (1, 7, 8, 9, 10, 11) AND Parameter BETWEEN 1 AND 63
        UNION ALL
        SELECT DeviceId, Parameter >> 9, TimeStamp, TRUE, Parameter & 7, (Parameter >> 3) & 63, -2 FROM state WHERE EventId = 979
    )
    WINDOW w AS (PARTITION BY DeviceId, Ch ORDER BY TimeStamp, Tie)
),
ring_broken AS MATERIALIZED (
    -- Missing = the states between the last one and this one, in phase order, that this phase has logged today. A phase that
    -- never logs a state (no red clearance, say) is not expected to.
    SELECT r.DeviceId, GREATEST(time_bucket(INTERVAL '{{bin_size}} minutes', r.TimeStamp), (SELECT MAX(TimeStamp) FROM state WHERE EventId = 974)) AS Bin,
           SUM(BIT_COUNT((Between_Bits | (Between_Bits >> 6)) & 63 & COALESCE(r.Seen_Before, 0)))::INT AS Proved_Missing
    FROM (
        SELECT *, ((1 << ((Pos - Prev_Pos + 11) % 6)) - 1) << ((Prev_Pos + 7) % 6) AS Between_Bits FROM ring_all
        WHERE NOT Seed AND Prev_Pos < 6 AND Pos <> (Prev_Pos + 1) % 6 AND TimeStamp > Prev_T
    ) r
    WHERE NOT EXISTS (SELECT 1 FROM flash f WHERE f.DeviceId = r.DeviceId AND f.F > r.Prev_T AND f.F <= r.TimeStamp)
    GROUP BY 1, 2
),

-- ---------- Bin totals ----------
totals AS MATERIALIZED (
    SELECT DeviceId, Bin, SUM(Seen)::INT AS Seen, SUM(Ring_Seen)::INT AS Ring_Seen, SUM(Proved)::INT AS Proved, SUM(Stop_All)::INT AS Stop_All, SUM(Stop_Parts)::INT AS Stop_Parts FROM (
        SELECT DeviceId, Bin, COUNT(*) AS Seen, COUNT(*) FILTER (WHERE EventId IN (1, 7, 8, 9, 10, 11) AND Parameter BETWEEN 1 AND 63) AS Ring_Seen,
               0 AS Proved, 0 AS Stop_All, 0 AS Stop_Parts
        FROM ev GROUP BY 1, 2
        UNION ALL SELECT DeviceId, Bin, 0, 0, Proved_Missing, 0, 0 FROM ring_broken
        UNION ALL SELECT DeviceId, Bin, 0, 0, 0, Stop_All, Stop_Parts FROM stopped
        UNION ALL SELECT DeviceId, TimeStamp, Parameter, 0, 0, 0, 0 FROM state WHERE EventId = 988
        UNION ALL SELECT DeviceId, TimeStamp, 0, Parameter, 0, 0, 0 FROM state WHERE EventId = 989
        UNION ALL SELECT DeviceId, TimeStamp, 0, 0, Parameter, 0, 0 FROM state WHERE EventId = 990
        UNION ALL SELECT DeviceId, TimeStamp, 0, 0, 0, Parameter, 0 FROM state WHERE EventId = 987
        UNION ALL SELECT DeviceId, TimeStamp, 0, 0, 0, 0, Parameter FROM state WHERE EventId = 992
    ) GROUP BY 1, 2
),
-- A signal has rows while a stream remembers an event. One that has aged out starts again at its next event.
device_start AS MATERIALIZED (
    SELECT DeviceId, COALESCE(MAX(o.Open_Bin), MIN(First_Bin)) AS Start_Bin FROM (
        SELECT DeviceId, MIN(Bin) AS First_Bin FROM ev GROUP BY 1
        UNION ALL
        SELECT DeviceId, NULL FROM st_last
    ) d LEFT JOIN open_bin o USING (DeviceId) GROUP BY 1
),
grid AS (
    SELECT DeviceId, UNNEST(GENERATE_SERIES(Start_Bin, (SELECT Finish FROM clock) - INTERVAL '{{bin_size}} minutes', INTERVAL '{{bin_size}} minutes')) AS Bin
    FROM device_start WHERE Start_Bin < (SELECT Finish FROM clock)
),

-- ---------- What the next run needs ----------
stream_end AS MATERIALIZED (
    SELECT DeviceId, Stream, ARG_MAX(Mem_R, Bin) AS R FROM chain
    WHERE Bin < (SELECT Finish FROM clock) GROUP BY 1, 2
),
state_rows AS (
    -- A stopped stream stays remembered while the signal logs anything else: it ages out only with the signal. Past a day the exact
    -- start no longer matters, since the longest gap remembered is 6 hours and 4 times that is a day.
    SELECT CASE WHEN l.Stream = 0 THEN l.Last_T ELSE GREATEST(l.Last_T, a.Last_T - INTERVAL 1 DAY) END AS TimeStamp, l.DeviceId, 970 + l.Stream AS EventId, COALESCE(e.R, 0)::INT AS Parameter
    FROM stream_last l LEFT JOIN stream_end e USING (DeviceId, Stream)
    LEFT JOIN stream_last a ON a.DeviceId = l.DeviceId AND a.Stream = 0
    UNION ALL
    SELECT (SELECT Finish FROM clock), DeviceId, 974, 0 FROM stream_last WHERE Stream = 0
    UNION ALL
    -- The last 4 finished bins of each stream's longest gap, kept in order and restamped the same way
    SELECT GREATEST(k.Bin, a.Last_T - INTERVAL 1 DAY - k.K * INTERVAL '{{bin_size}} minutes'), k.DeviceId, 997 + k.Stream, k.L::INT
    FROM (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY DeviceId, Stream ORDER BY Bin DESC) AS K FROM stream_bins WHERE L IS NOT NULL AND Bin < (SELECT Finish FROM clock)
    ) k JOIN stream_last a ON a.DeviceId = k.DeviceId AND a.Stream = 0
    WHERE k.K <= 4
    UNION ALL
    SELECT r.TimeStamp, r.DeviceId, 979,
           (r.Ch << 9) + ((COALESCE(r.Seen_Before, 0) | r.Bits) << 3)
           + CASE WHEN r.Pos > 5 OR COALESCE(lf.F, TIMESTAMP '1970-01-01') > r.TimeStamp THEN 7 ELSE r.Pos END
    FROM ring_all r
    LEFT JOIN (SELECT DeviceId, MAX(F) AS F FROM flash GROUP BY 1) lf USING (DeviceId)
    WHERE r.Newest = 1
    UNION ALL
    SELECT F, DeviceId, 991, 0 FROM flash_bin WHERE Bin >= (SELECT Finish FROM clock)
    UNION ALL
    SELECT Bin, DeviceId, 980 + Stream, LEAST(N, 32767) FROM bin_stats WHERE Bin >= (SELECT Finish FROM clock)
    UNION ALL
    SELECT Bin, DeviceId, 984 + Stream, M FROM bin_stats WHERE Bin >= (SELECT Finish FROM clock) AND M IS NOT NULL
    UNION ALL
    SELECT Bin, DeviceId, 994 + Stream, Q FROM bin_stats WHERE Bin >= (SELECT Finish FROM clock) AND Q IS NOT NULL
    UNION ALL
    SELECT Bin, DeviceId, 987, LEAST(Stop_All, 32767) FROM totals WHERE Bin >= (SELECT Finish FROM clock) AND Stop_All > 0
    UNION ALL
    SELECT Bin, DeviceId, 992, LEAST(Stop_Parts, 32767) FROM totals WHERE Bin >= (SELECT Finish FROM clock) AND Stop_Parts > 0
    UNION ALL
    SELECT Bin, DeviceId, 988, LEAST(Seen, 32767) FROM totals WHERE Bin >= (SELECT Finish FROM clock) AND Seen > 0
    UNION ALL
    SELECT Bin, DeviceId, 989, LEAST(Ring_Seen, 32767) FROM totals WHERE Bin >= (SELECT Finish FROM clock) AND Ring_Seen > 0
    UNION ALL
    SELECT Bin, DeviceId, 990, LEAST(Proved, 32767) FROM totals WHERE Bin >= (SELECT Finish FROM clock) AND Proved > 0
),
scored AS (
    SELECT g.Bin, g.DeviceId,
           LEAST(CASE WHEN COALESCE(t.Ring_Seen, 0) + COALESCE(t.Proved, 0) = 0 THEN 1.0 ELSE t.Ring_Seen::DOUBLE / (t.Ring_Seen + t.Proved) END,
                 -- All events stopping is every stream stopping: count it once, by the larger figure
                 CASE WHEN COALESCE(t.Seen, 0) + GREATEST(COALESCE(t.Stop_All, 0), COALESCE(t.Stop_Parts, 0)) = 0 THEN 1.0
                      ELSE t.Seen::DOUBLE / (t.Seen + GREATEST(t.Stop_All, t.Stop_Parts)) END)::FLOAT AS Score
    FROM grid g LEFT JOIN totals t ON t.DeviceId = g.DeviceId AND t.Bin = g.Bin
)
-- No row is written at Score 0, so no row means Score 0, the way a missing has_data row reads today.
SELECT 'score' AS Kind, Bin AS TimeStamp, DeviceId, Score, NULL::INT AS EventId, NULL::INT AS Parameter FROM scored WHERE Score > 0
UNION ALL
SELECT 'state', TimeStamp, DeviceId, NULL, EventId, Parameter FROM state_rows

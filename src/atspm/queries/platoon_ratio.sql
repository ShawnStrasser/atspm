--Platoon Ratio
--Written in SQL for DuckDB. This is a jinja2 template, with variables inside curly braces.
--
--Platoon ratio (HCM 7th ed., Eq. 19-?; Exhibit 19-14) expresses the quality of progression
--for a phase independently of its green ratio:
--
--    Rp = P / (g/C)
--
--where P is the proportion of vehicles arriving on green (Percent_AOG from the
--arrival_on_green aggregation) and g/C is the effective green ratio. Rp = 1.0 means arrivals
--are uniformly random; Rp > 1 means platoons are arriving on green (good progression);
--Rp < 1 means they are arriving on red (poor progression).
--
--g/C is estimated per bin as the seconds the phase displayed green divided by the seconds in
--the bin, which equals g/C whenever the phase cycles continuously within the bin. Green
--intervals are clipped at bin boundaries so a green that straddles two bins is split between
--them. Bins where the phase never turned green are excluded (g/C = 0 is undefined).
--
--Arrival_Type is the HCM arrival-type classification of Rp (1 = very poor ... 6 = exceptional).
--
--Requires the arrival_on_green aggregation to have run first (declared in AGGREGATION_DEPENDENCIES).


WITH phase_events AS (
    -- Green begin (1) and yellow begin (8) events for phases that have an advance detector,
    -- so the phase set matches arrival_on_green exactly.
    SELECT r.TimeStamp,
        r.DeviceId,
        r.EventId,
        r.Parameter::int16 AS Phase
    FROM {{from_table}} r
    JOIN (SELECT DISTINCT DeviceId, Phase
          FROM detector_config
          WHERE Function = 'Advance') c
        ON r.DeviceId = c.DeviceId
       AND c.Phase = r.Parameter
    WHERE r.EventId IN (1, 8)
),

sequenced AS (
    SELECT *,
        LAG(EventId)     OVER (PARTITION BY DeviceId, Phase ORDER BY TimeStamp, EventId) AS PrevEventId,
        LEAD(EventId)    OVER (PARTITION BY DeviceId, Phase ORDER BY TimeStamp, EventId) AS NextEventId,
        LEAD(TimeStamp)  OVER (PARTITION BY DeviceId, Phase ORDER BY TimeStamp, EventId) AS NextTimeStamp
    FROM phase_events
),

green_intervals AS (
    -- Closed greens: green begin followed by the next phase event. If the data ends while the
    -- phase is still green (no following event), the green is assumed to run to the end of its
    -- bin. This keeps per-bin green time exact across incremental runs whose chunks align with
    -- bins, instead of dropping the straddling green from the earlier chunk.
    SELECT DeviceId, Phase,
        TimeStamp AS GreenStart,
        COALESCE(NextTimeStamp,
                 time_bucket(INTERVAL '{{bin_size}} minutes', TimeStamp) + INTERVAL '{{bin_size}} minutes') AS GreenEnd
    FROM sequenced
    WHERE EventId = 1

    UNION ALL

    -- Opening greens: a yellow begin with no preceding green begin in the data (first event of a
    -- chunk) means the phase was already green; assume it was green from the start of its bin.
    SELECT DeviceId, Phase,
        time_bucket(INTERVAL '{{bin_size}} minutes', TimeStamp) AS GreenStart,
        TimeStamp AS GreenEnd
    FROM sequenced
    WHERE EventId = 8 AND (PrevEventId IS NULL OR PrevEventId = 8)
),

green_by_bin AS (
    -- Split every green interval across the bins it overlaps and sum the clipped seconds
    SELECT
        b.bin AS TimeStamp,
        g.DeviceId,
        g.Phase,
        SUM(
            DATE_DIFF('millisecond',
                GREATEST(g.GreenStart, b.bin),
                LEAST(g.GreenEnd, b.bin + INTERVAL '{{bin_size}} minutes')
            )::DOUBLE / 1000.0
        ) AS Green_Seconds
    FROM green_intervals g,
        LATERAL (
            SELECT UNNEST(generate_series(
                time_bucket(INTERVAL '{{bin_size}} minutes', g.GreenStart),
                time_bucket(INTERVAL '{{bin_size}} minutes', g.GreenEnd - INTERVAL 1 MILLISECOND),
                INTERVAL '{{bin_size}} minutes'
            )) AS bin
        ) b
    WHERE g.GreenEnd > g.GreenStart
    GROUP BY b.bin, g.DeviceId, g.Phase
),

platoon AS (
    SELECT
        a.TimeStamp,
        a.DeviceId,
        a.Phase,
        a.Total_Actuations,
        a.Percent_AOG,
        (gb.Green_Seconds / ({{bin_size}} * 60.0))::FLOAT AS Green_Ratio,
        (a.Percent_AOG / (gb.Green_Seconds / ({{bin_size}} * 60.0)))::FLOAT AS Platoon_Ratio
    FROM arrival_on_green a
    JOIN green_by_bin gb
        ON a.TimeStamp = gb.TimeStamp
       AND a.DeviceId = gb.DeviceId
       AND a.Phase = gb.Phase
    WHERE gb.Green_Seconds > 0
)

SELECT *,
    CASE
        WHEN Platoon_Ratio <= 0.50 THEN 1
        WHEN Platoon_Ratio <= 0.85 THEN 2
        WHEN Platoon_Ratio <= 1.15 THEN 3
        WHEN Platoon_Ratio <= 1.50 THEN 4
        WHEN Platoon_Ratio <= 2.00 THEN 5
        ELSE 6
    END::UTINYINT AS Arrival_Type
FROM platoon

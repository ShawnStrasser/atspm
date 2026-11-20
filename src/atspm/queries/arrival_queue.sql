-- Included by arrival_hold.sql when queue_model is on. A list of CTEs, not a query.
--
-- The queue model: did a vehicle that reached the stop bar on green find a queue still there? The spillback flag
-- (arrival_spillback.sql) sees a queue that reaches the advance detector. A shorter queue it cannot see: the vehicle
-- crosses the detector at speed, on green, and stops behind cars still leaving the stop bar. So the vehicles each advance
-- detector counted are queued at the stop bar in the order they arrive, and served one lane at a time:
--   - Service starts 5 seconds after green begins and ends 3 seconds after that green ends (its first yellow or red
--     clearance begin). Each car takes 2 seconds a lane: the standard 2n + 5 queue clearance (start-up 5 s, 2 s per car).
--   - A car leaves at its arrival, the start of service or 2 seconds a lane after the car ahead, whichever is latest.
--     One that waits 4 seconds or more, or is not served before service ends, is queued and counts as an arrival on red. A
--     stop is speed under 5 MPH (Emtenan and Day 2020), and a car that waits 4 seconds behind the queue fell under it.
--   - Its arrival is its on plus the config's TravelTime (or without one at least 2 seconds plus the distance at 70 feet a
--     second; advance_detectors.sql).
--   - A detector whose RedDepartures is over 0.25 is left out: only the Spillback Flag runs there (advance_detectors.sql).
--   - The lanes between the detector and the stop bar hold Storage cars. Of the cars that arrive before service starts,
--     only that many join the queue. The rest are queued and do not make it longer: the detector cannot count cars it
--     never reaches, and a queue counted past the space it has is one the controller served without a green it logs
--     (a permissive left turn, a right turn on red).
--   - For the same reason a car the queue ahead of it would put past the detector, but that did not sit on the detector
--     more than spillback_on_seconds, is not queued.
-- Each green is its own queue: nothing carries to the next one. Measured against stop bar counts at 277 ODOT lanes, the
-- model takes wrong calls from 74 to 41.2 per 1,000 vehicles counted on green; how it works with the flag is in the README.
--
-- A vehicle's result is final once the data reach 6 seconds past its departure, or past the end of service of a green
-- that has ended, so a car that sat is known by then too. A vehicle whose detector is still on and may yet be under the
-- flag is never final. arrival_on_green_carry.sql carries what is not, and the cars ahead of it in its queue, as 965.
-- A run with nothing after it takes everything as final.

-- Service windows: green begin (a repeat before any end is the same green) to its first yellow or red clearance begin.
-- Service_End is NULL while the green has not ended.
queue_windows AS (
    SELECT DeviceId, Phase,
        TimeStamp AS Green,
        TimeStamp + INTERVAL 5 SECOND AS Service_Start,
        Green_End + INTERVAL 3 SECOND AS Service_End,
        COALESCE(Green_End + INTERVAL 3 SECOND, TIMESTAMP '9999-12-31') AS Window_End
    FROM (
        SELECT *,
            LAG(EventId) OVER (PARTITION BY DeviceId, Phase ORDER BY TimeStamp, EventId) AS Previous_Event,
            FIRST_VALUE(CASE WHEN EventId <> 1 THEN TimeStamp END IGNORE NULLS) OVER (
                PARTITION BY DeviceId, Phase ORDER BY TimeStamp, EventId
                ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING
            ) AS Green_End
        FROM phase_events
    )
    WHERE EventId = 1 AND Previous_Event IS DISTINCT FROM 1
),

{%- if 'queue' in (shared_stages or []) %}

-- Built once for this run by arrival_shared.sql
queue_results AS (SELECT * FROM _arrival_queue),
{%- else %}

-- Each vehicle in the window of the first service that ends at or after its arrival. Served: that green began within
-- 10 minutes of it. A vehicle with no green that soon is not queued.
queue_cars AS (
    SELECT a.DeviceId, a.Detector, a.Phase, a.On_Time, a.Queue_Time, a.Sat, a.Unresolved,
        c.Lanes, c.Storage,
        w.Green, w.Service_Start, w.Service_End,
        w.Green IS NOT NULL AND w.Green <= a.Queue_Time + INTERVAL 600 SECOND AS Served,
        a.Queue_Time < w.Service_Start AS Before_Service
    FROM (SELECT * FROM arrivals WHERE Modelled) a
    JOIN advance_detectors c
        ON a.DeviceId = c.DeviceId
       AND a.Detector = c.Parameter
       AND a.Phase = c.Phase
    ASOF LEFT JOIN queue_windows w
        ON a.DeviceId = w.DeviceId
       AND a.Phase = w.Phase
       AND a.Queue_Time <= w.Window_End
),

-- Each green's cars in arrival order, in one window order throughout (a green's served cars, or its unserved ones):
--   - Rank: place in arrival order. Cars arriving before service starts come first, so the first Storage of them join
--     the queue (Kept) and the rest do not: the cars ahead of a car that joins after service starts are its Rank less
--     those that did not (Position).
--   - Departure of each car that joins: Earliest - 2/Lanes s x Position, at its running highest (Run_Max), plus 2/Lanes s
--     x Position. A car whose own term is that highest left an empty queue behind the car ahead: it starts a stretch of
--     the queue that does not depend on any car before it (Starts). Within a stretch Run_Max holds, so its cars leave
--     2/Lanes s apart.
queue_ranked AS (
    SELECT *,
        NOT (Before_Service AND Rank >= Storage) AS Kept,
        Rank - CASE WHEN Before_Service THEN 0 ELSE N_Before - LEAST(N_Before, CEIL(Storage)::BIGINT) END AS Position
    FROM (
        SELECT *,
            ROW_NUMBER() OVER cars_in_order - 1 AS Rank,
            COUNT(*) FILTER (WHERE Before_Service) OVER (PARTITION BY DeviceId, Detector, Phase, Green, Served) AS N_Before
        FROM queue_cars
        WINDOW cars_in_order AS (PARTITION BY DeviceId, Detector, Phase, Green, Served ORDER BY Queue_Time, On_Time)
    )
),

queue_departures AS (
    SELECT *, CASE WHEN Kept THEN Run_Max + 2000.0 / Lanes * Position END AS Departure_Ms
    FROM (
    SELECT *,
        CASE WHEN Kept THEN MAX(CASE WHEN Kept THEN Key_Ms END) OVER (cars_in_order ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) END AS Run_Max,
        Kept AND Key_Ms >= COALESCE(MAX(CASE WHEN Kept THEN Key_Ms END) OVER (cars_in_order ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), Key_Ms) AS Starts,
        COALESCE(SUM(Kept::INT) OVER (cars_in_order ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS Joined_Ahead
    FROM (
        SELECT *, epoch_ms(GREATEST(Queue_Time, Service_Start)) - 2000.0 / Lanes * Position AS Key_Ms
        FROM queue_ranked
    )
    WINDOW cars_in_order AS (PARTITION BY DeviceId, Detector, Phase, Green, Served ORDER BY Queue_Time, On_Time)
    )
),

-- The on and the Position of the car that starts each car's stretch
queue_stretches AS (
    SELECT *,
        LAST_VALUE(CASE WHEN Starts THEN On_Time END IGNORE NULLS) OVER stretch AS Stretch_On,
        LAST_VALUE(CASE WHEN Starts THEN Position END IGNORE NULLS) OVER stretch AS Stretch_Position
    FROM queue_departures
    WINDOW stretch AS (PARTITION BY DeviceId, Detector, Phase, Green, Served ORDER BY Queue_Time, On_Time
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
),

-- Cars still in the queue when each car arrives: the ones ahead that joined, less those gone by then. A car that did not
-- join arrived before service, when none had gone. For one that joined, every car before its stretch has gone, and of
-- its stretch the cars whose departure, Run_Max + 2/Lanes s x Position, is at or before its arrival (Last_Gone, found from
-- the spacing and checked against that same sum).
queue_waiting AS (
    SELECT *,
        CASE WHEN NOT Kept THEN Joined_Ahead
             ELSE Joined_Ahead - Stretch_Position - GREATEST(LEAST(Last_Gone, Position) - Stretch_Position + 1, 0) END AS In_Queue
    FROM (
        SELECT *,
            CASE WHEN Run_Max + 2000.0 / Lanes * (Guess + 1) <= Arrival_Ms THEN Guess + 1
                 WHEN Run_Max + 2000.0 / Lanes * Guess <= Arrival_Ms THEN Guess
                 ELSE Guess - 1 END AS Last_Gone
        FROM (
            SELECT *,
                epoch_ms(Queue_Time)::DOUBLE AS Arrival_Ms,
                FLOOR((epoch_ms(Queue_Time) - Run_Max) / (2000.0 / Lanes))::BIGINT AS Guess
            FROM queue_stretches
            WHERE Served
        )
    )
),

queue_results AS (
    SELECT c.DeviceId, c.Detector, c.Phase, c.On_Time, c.Green,
        COALESCE(c.Departure_Ms, epoch_ms(c.Queue_Time)) AS Departure_Ms,
        COALESCE(c.Stretch_On, c.On_Time) AS Stretch_On,
        {%- if approach_delay %}
        -- For approach_delay's Queue_Delay_Seconds: the car's arrival, its window's end of service, and whether its departure is
        -- one the model stands by (served, joined the queue, and not in a queue longer than the lanes hold)
        c.Queue_Time,
        c.Service_End,
        COALESCE(c.Served AND c.Kept AND NOT (c.In_Queue > c.Storage AND NOT c.Sat), FALSE) AS Queue_Usable,
        {%- endif %}
        COALESCE(
            (NOT c.Kept
                OR c.Departure_Ms - epoch_ms(c.Queue_Time) >= 4000
                OR c.Departure_Ms > epoch_ms(c.Service_End))
            AND NOT (c.In_Queue > c.Storage AND NOT c.Sat),
            FALSE) AS Queued,
        {%- if carry_bins %}
        NOT c.Unresolved AND (
            (c.Served AND (c.Service_End + INTERVAL 6 SECOND <= l.Last_Event
                           OR COALESCE(c.Departure_Ms, epoch_ms(c.Queue_Time)) + 6000 <= epoch_ms(l.Last_Event)))
            OR (c.Green IS NOT NULL AND NOT c.Served)
            OR (c.Green IS NULL AND c.Queue_Time + INTERVAL 600 SECOND <= l.Last_Event)
        ) IS TRUE AS Final
        {%- else %}
        TRUE AS Final
        {%- endif %}
    FROM (
        SELECT * FROM queue_waiting
        UNION ALL BY NAME
        SELECT * REPLACE (NULL::DOUBLE AS Departure_Ms, NULL::TIMESTAMP AS Stretch_On, NULL::BOOLEAN AS Kept), NULL::BIGINT AS In_Queue
        FROM queue_stretches WHERE Served IS NOT TRUE
    ) c
    CROSS JOIN (SELECT MAX(TimeStamp) AS Last_Event FROM {{from_table}}) l
),
{%- endif %}

-- Each vehicle with its result. One the model has not seen (an earlier run counted its result) is final.
arrivals_queued AS (
    SELECT a.*,
        COALESCE(a.Queued_Before, q.Queued, FALSE) AS Queued,
        COALESCE(q.Final, TRUE) AS Final
    FROM arrivals a
    LEFT JOIN queue_results q
        ON a.Modelled
       AND a.DeviceId = q.DeviceId
       AND a.Detector = q.Detector
       AND a.Phase = q.Phase
       AND a.On_Time = q.On_Time
)

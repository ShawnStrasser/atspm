--What arrival_on_green and platoon_ratio pass to the next run through unmatched_events, as synthetic EventIds:
--938 arrivals that land after results stopped, at their projected time, with the phase as Parameter
--948 the same for an arrival under the spillback flag
--939, 940, 941 green, yellow and red clearance begins from there on, with the phase as Parameter
--939 also marks a phase green when results stopped, stamped with that time, so the next run takes its green from there
--949 a detector holding the spillback flag at the end of the data, stamped when it last turned off or, if it is on,
--when it turned on, with the detector as Parameter. Left out once the gap has passed, since the flag has gone.
--950 a detector on at the end of the data, stamped when it turned on, with the detector as Parameter, plus 1000 if its
--vehicle is held (see arrival_hold.sql): it is not counted yet, and the next run counts it
--With the queue model (arrival_queue.sql):
--964 the same as 938 for an arrival the model queued
--965 a vehicle in a queue the model has not finished with, and every vehicle after it in that queue from the car that
--starts its stretch, stamped at its on, with the detector as Parameter, plus 1000 if flagged, 2000 if it sat on the
--detector, 4000 if its detector was still on, 8000 if it is counted already (here or as a 938, 948 or 964)
--Phase events then carry from the green of the first of those vehicles, or of a green not ended, if that is earlier.
--Results stop at results_end, or for a phase a held vehicle reaches before that, at the start of its bin.
--A run with no raw data has no results_end and keeps everything it was given.
--Written in SQL for DuckDB. This is a jinja2 template, with variables inside curly braces.


WITH {% include 'arrival_phase_events.sql' %},

{% include 'arrival_projection.sql' %},

{% include 'arrival_hold.sql' %},

ends AS (SELECT TimeStamp AS Results_End FROM results_end),

-- Where results stop for each phase
phase_results_end AS (
    SELECT p.DeviceId, p.Phase, COALESCE(e.Phase_End, r.Results_End) AS Results_End
    FROM (SELECT DISTINCT DeviceId, Phase FROM with_signal_state) p
    CROSS JOIN ends r
    LEFT JOIN phase_ends e
        ON p.DeviceId = e.DeviceId
       AND p.Phase = e.Phase
){%- if queue_model %},

-- The on from which each detector's vehicles wait for the queue model of the next run
queue_carry_from AS (
    SELECT DeviceId, Detector, Phase, MIN(Stretch_On) AS Carry_On
    FROM queue_results
    WHERE NOT Final
    GROUP BY DeviceId, Detector, Phase
),

queue_carried AS (
    SELECT a.*
    FROM with_signal_state a
    JOIN queue_carry_from f
        ON a.DeviceId = f.DeviceId
       AND a.Detector = f.Detector
       AND a.Phase = f.Phase
       AND a.On_Time >= f.Carry_On
    WHERE a.EventId = 82 AND a.Modelled
),

-- Where each phase's events carry from: results end, or the green of a vehicle the next run models, or a green not ended
phase_carry_from AS (
    SELECT p.DeviceId, p.Phase,
        CASE WHEN p.Results_End IS NOT NULL THEN LEAST(p.Results_End, COALESCE(g.First_Green, p.Results_End)) END AS Results_End
    FROM phase_results_end p
    LEFT JOIN (
        SELECT DeviceId, Phase, MIN(Green) AS First_Green
        FROM (
            SELECT q.DeviceId, q.Phase, q.Green
            FROM queue_results q
            JOIN queue_carry_from f
                ON q.DeviceId = f.DeviceId
               AND q.Detector = f.Detector
               AND q.Phase = f.Phase
               AND q.On_Time >= f.Carry_On
            UNION ALL
            SELECT DeviceId, Phase, Green FROM queue_windows WHERE Service_End IS NULL
        )
        GROUP BY DeviceId, Phase
    ) g
        ON p.DeviceId = g.DeviceId
       AND p.Phase = g.Phase
){%- endif %}{%- if spillback_on_seconds %},

-- What each detector was doing when the data ended
detector_ends AS (
    SELECT *
    FROM detector_flags
    QUALIFY ROW_NUMBER() OVER (PARTITION BY DeviceId, Parameter ORDER BY TimeStamp DESC, Step DESC) = 1
),

last_event AS (SELECT MAX(TimeStamp) AS Last_Event FROM {{from_table}})
{%- endif %}

{%- if int_keys %}
SELECT x.TimeStamp, k.DeviceId, x.EventId, x.Parameter, x.IsValid
FROM (
{%- endif %}
SELECT a.TimeStamp, a.DeviceId, (CASE WHEN a.Spillback THEN 948{% if queue_model %} WHEN a.Queued THEN 964{% endif %} ELSE 938 END)::INT16 AS EventId, a.Phase::INT16 AS Parameter, TRUE AS IsValid
FROM with_signal_state a
JOIN phase_results_end e
    ON a.DeviceId = e.DeviceId
   AND a.Phase = e.Phase
LEFT JOIN held h
    ON a.Unresolved
   AND a.DeviceId = h.DeviceId
   AND a.Detector = h.Detector
WHERE a.EventId = 82
  AND (e.Results_End IS NULL OR a.TimeStamp >= e.Results_End)
  AND h.DeviceId IS NULL{% if queue_model %}
  AND a.Counted
  AND a.Final{% endif %}
{%- if queue_model %}

UNION ALL

SELECT On_Time, DeviceId, 965::INT16 AS EventId,
    (Detector + 1000 * Spillback::INT + 2000 * Sat::INT + 4000 * Still_On::INT + 8000 * (Final OR NOT Counted)::INT)::INT16 AS Parameter,
    TRUE AS IsValid
FROM queue_carried
WHERE NOT Unresolved
{%- endif %}

UNION ALL

SELECT p.TimeStamp, p.DeviceId,
    (CASE p.EventId WHEN 1 THEN 939 WHEN 8 THEN 940 ELSE 941 END)::INT16 AS EventId,
    p.Phase::INT16 AS Parameter,
    TRUE AS IsValid
FROM phase_events p
JOIN {% if queue_model %}phase_carry_from{% else %}phase_results_end{% endif %} e
    ON p.DeviceId = e.DeviceId
   AND p.Phase = e.Phase
WHERE e.Results_End IS NULL OR p.TimeStamp >= e.Results_End

UNION ALL

SELECT ANY_VALUE(e.Results_End), p.DeviceId, 939::INT16 AS EventId, p.Phase::INT16 AS Parameter, TRUE AS IsValid
FROM phase_events p
JOIN {% if queue_model %}phase_carry_from{% else %}phase_results_end{% endif %} e
    ON p.DeviceId = e.DeviceId
   AND p.Phase = e.Phase
WHERE p.TimeStamp < e.Results_End
GROUP BY p.DeviceId, p.Phase
HAVING ARG_MAX(p.EventId, (p.TimeStamp, p.EventId)) = 1
{%- if spillback_on_seconds %}

UNION ALL

SELECT End_Time, DeviceId, 949::INT16 AS EventId, Parameter, TRUE AS IsValid
FROM detector_ends, last_event
WHERE (Spillback OR Held_Long)
  AND (Still_On OR Last_Event IS NULL OR DATE_DIFF('millisecond', End_Time, Last_Event) < {{spillback_gap_seconds}} * 1000)

UNION ALL

SELECT d.TimeStamp, d.DeviceId, 950::INT16 AS EventId, (d.Parameter + CASE WHEN h.DeviceId IS NULL THEN 0 ELSE 1000 END)::INT16, TRUE AS IsValid
FROM detector_ends d
LEFT JOIN held h
    ON d.Unresolved
   AND d.DeviceId = h.DeviceId
   AND d.Parameter = h.Detector
WHERE d.Still_On
{%- endif %}
{%- if int_keys %}
) x
JOIN device_keys k ON k.Dev = x.DeviceId
{%- endif %}

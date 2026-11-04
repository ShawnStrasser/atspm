--What arrival_on_green and platoon_ratio pass to the next run through unmatched_events, as synthetic EventIds:
--938 arrivals that land after results stopped, at their projected time, with the phase as Parameter
--948 the same for an arrival that began while its detector held the spillback flag
--939, 940, 941 green, yellow and red clearance begins from there on, with the phase as Parameter
--939 also marks a phase green when results stopped, stamped with that time, so the next run takes its green from there
--949 a detector holding the spillback flag at the end of the data, stamped when it last turned off or, if it is on,
--when it turned on, with the detector as Parameter. Left out once the gap has passed, since the flag has gone.
--950 a detector on at the end of the data, stamped when it turned on, with the detector as Parameter
--A run with no raw data has no results_end and keeps everything it was given.
--Written in SQL for DuckDB. This is a jinja2 template, with variables inside curly braces.


WITH {% include 'arrival_phase_events.sql' %},

{% include 'arrival_projection.sql' %},

ends AS (SELECT TimeStamp AS Results_End FROM results_end){%- if spillback_on_seconds %},

-- What each detector was doing when the data ended
detector_ends AS (
    SELECT *
    FROM detector_flags
    QUALIFY ROW_NUMBER() OVER (PARTITION BY DeviceId, Parameter ORDER BY TimeStamp DESC, Step DESC) = 1
),

last_event AS (SELECT MAX(TimeStamp) AS Last_Event FROM {{from_table}})
{%- endif %}

SELECT TimeStamp, DeviceId, (CASE WHEN Spillback THEN 948 ELSE 938 END)::INT16 AS EventId, Phase::INT16 AS Parameter, TRUE AS IsValid
FROM arrivals, ends
WHERE Results_End IS NULL OR TimeStamp >= Results_End

UNION ALL

SELECT TimeStamp, DeviceId,
    (CASE EventId WHEN 1 THEN 939 WHEN 8 THEN 940 ELSE 941 END)::INT16 AS EventId,
    Phase::INT16 AS Parameter,
    TRUE AS IsValid
FROM phase_events, ends
WHERE Results_End IS NULL OR TimeStamp >= Results_End

UNION ALL

SELECT ANY_VALUE(Results_End), DeviceId, 939::INT16 AS EventId, Phase::INT16 AS Parameter, TRUE AS IsValid
FROM phase_events, ends
WHERE TimeStamp < Results_End
GROUP BY DeviceId, Phase
HAVING ARG_MAX(EventId, (TimeStamp, EventId)) = 1
{%- if spillback_on_seconds %}

UNION ALL

SELECT End_Time, DeviceId, 949::INT16 AS EventId, Parameter, TRUE AS IsValid
FROM detector_ends, last_event
WHERE (Spillback OR Held_Long)
  AND (Still_On OR Last_Event IS NULL OR DATE_DIFF('millisecond', End_Time, Last_Event) < {{spillback_gap_seconds}} * 1000)

UNION ALL

SELECT TimeStamp, DeviceId, 950::INT16 AS EventId, Parameter, TRUE AS IsValid
FROM detector_ends
WHERE Still_On
{%- endif %}

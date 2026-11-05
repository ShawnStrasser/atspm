-- Included by ped.sql and ped_carry.sql. A list of CTEs, not a query.
-- What the ped counts know when a run ends: this run's events, and what earlier runs carried in unmatched_events.
--
-- Each bin is written once, whole, by the run that reaches its end (see results_end.sql). The carry is:
-- 943 one row per unique actuation, stamped with its bin, for bins not written yet and the last 3 written,
--     since Estimated_Volumes reads a bin and the 3 before it
-- 944 per input, the next bin to write
-- 945 per input, its last activity (a press or a ped service)
-- 946 one row per press, stamped with its bin, for bins not written yet
-- 947 one row per ped service, stamped with its bin, for bins not written yet


{% include 'results_end.sql' %},

unique_counts AS (
    SELECT TimeStamp, DeviceId, Phase, SUM(Unique_Actuations)::BIGINT AS Unique_Actuations
    FROM (
        SELECT * FROM (
            {% include 'ped_unique_counts.sql' %}
        )
        {% if ped_carried %}
        UNION ALL
        SELECT TimeStamp, DeviceId, Parameter::int16 AS Phase, COUNT(*) AS Unique_Actuations
        FROM unmatched_previous
        WHERE EventId = 943
        GROUP BY ALL
        {% endif %}
    )
    GROUP BY ALL
),

--Presses (90) and ped services (21, 67) per bin, the carried rows counted the same as this run's events
ped_events AS (
    SELECT
        TIME_BUCKET(interval '{{bin_size}} minutes', TimeStamp) AS TimeStamp,
        DeviceId,
        Parameter::int16 AS Phase,
        EventId = 90 AS Is_Press
    FROM {{from_table}}
    WHERE EventId IN (21, 67, 90)
    {% if ped_carried %}
    UNION ALL
    SELECT TimeStamp, DeviceId, Parameter::int16 AS Phase, EventId = 946 AS Is_Press
    FROM unmatched_previous
    WHERE EventId IN (946, 947)
    {% endif %}
),

--Every known input and the first bin not written yet. An input starts at its first activity, wherever the runs were
--cut, and goes on through quiet bins. Its last-activity row (945) stops being refreshed when it goes quiet, so
--max_days_old drops that row and the input ends.
inputs AS (
    SELECT
        DeviceId,
        Phase,
        {% if ped_carried %}
        COALESCE(c.Next_Bin, r.First_Bin) AS Next_Bin,
        GREATEST(c.Last_Activity, r.Last_Activity) AS Last_Activity
        {% else %}
        r.First_Bin AS Next_Bin,
        r.Last_Activity
        {% endif %}
    FROM (
        SELECT
            DeviceId,
            Parameter::int16 AS Phase,
            TIME_BUCKET(interval '{{bin_size}} minutes', MIN(TimeStamp)) AS First_Bin,
            MAX(TimeStamp) AS Last_Activity
        FROM {{from_table}}
        WHERE EventId IN (21, 67, 90)
        GROUP BY ALL
    ) r
    {% if ped_carried %}
    FULL JOIN (
        SELECT a.DeviceId, a.Parameter::int16 AS Phase, n.TimeStamp AS Next_Bin, a.TimeStamp AS Last_Activity
        FROM unmatched_previous a
        JOIN unmatched_previous n
            ON n.EventId = 944 AND n.DeviceId = a.DeviceId AND n.Parameter = a.Parameter
        WHERE a.EventId = 945
    ) c USING (DeviceId, Phase)
    {% endif %}
)

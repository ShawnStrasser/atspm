--Aggregate Unique Pedestiran Actuations in 15-minute Bins
--Written in SQL for DuckDB. This is a jinja2 template, with variables inside curly braces.

--The intention of "unique" actuations is that back-to-back actuations from the same person are removed
--The default time lapse before a new unique actuation for a given ped phase may be counted is 15 seconds
--see this study: https://doi.org/10.1177/0361198121994126

--A press counts if it is more than seconds_between_actuations after the previous press on that input, or
--if no earlier press is known. Incremental runs read the previous run's last press on each input (synthetic
--EventId 937), so a run of presses crossing runs is treated the same as in one big run. That carried press
--only sets the gap for the next press, and it is not counted again. It is ignored by a press older than it.

SELECT
    TimeStamp,
    DeviceId,
    Phase,
    COUNT(*)::int16 as Unique_Actuations
FROM
    (SELECT
        TIME_BUCKET(interval '{{bin_size}} minutes', r.TimeStamp) as TimeStamp,
        r.DeviceId,
        r.Parameter::int16 as Phase,
        r.TimeStamp AS Press,
        GREATEST(
            LAG(r.TimeStamp) OVER (PARTITION BY r.DeviceId, r.Parameter ORDER BY r.TimeStamp)
            {% if unmatched %}, CASE WHEN c.TimeStamp <= r.TimeStamp THEN c.TimeStamp END{% endif %}
        ) AS Previous_Press
    FROM
        {{from_table}} r
    {% if unmatched %}
    LEFT JOIN unmatched_previous c
        ON c.EventId = 937 AND c.DeviceId::VARCHAR = r.DeviceId::VARCHAR AND c.Parameter = r.Parameter
    {% endif %}
    WHERE
        r.EventId = 90
    ) q
WHERE
    q.Previous_Press IS NULL
    OR DATEDIFF('MILLISECOND', q.Previous_Press, q.Press) > {{ (seconds_between_actuations | float * 1000) | int }}
GROUP BY
    TimeStamp, DeviceId, Phase

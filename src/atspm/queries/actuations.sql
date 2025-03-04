--Aggregate Detector Actuations
--Written in SQL for DuckDB. This is a jinja2 template, with variables inside curly braces.

WITH base_counts AS (
    SELECT
        TIME_BUCKET(interval '{{bin_size}} minutes', TimeStamp) as TimeStamp,
        DeviceId,
        Parameter::int16 as Detector,
        COUNT(*)::int16 AS Total
    FROM {{from_table}}
    WHERE EventID = 82
    GROUP BY ALL
)
{% if fill_in_missing | default(false) %}
,time_series AS (
    SELECT UNNEST(
        GENERATE_SERIES(
            (SELECT MIN(TimeStamp)::TIMESTAMP FROM base_counts),
            (SELECT MAX(TimeStamp)::TIMESTAMP FROM base_counts),
            INTERVAL '{{bin_size}} minutes'
        )
    ) as TimeStamp
),
device_detectors AS (
    {% if known_detectors_found | default(false) %}
    -- Combine current detectors with previously known detectors
    SELECT DISTINCT
        DeviceId,
        Detector::int16 as Detector
    FROM (
        SELECT DeviceId, Detector FROM base_counts
        UNION 
        SELECT DeviceId, Detector FROM known_detectors_previous
    )
    {% else %}
    -- Just use current detectors if no history is available
    SELECT DISTINCT
        DeviceId,
        Detector::int16 as Detector
    FROM base_counts
    {% endif %}
)
SELECT 
    t.TimeStamp,
    d.DeviceId,
    d.Detector::int16 as Detector,
    COALESCE(b.Total, 0::int16) as Total
FROM time_series t
CROSS JOIN device_detectors d
LEFT JOIN base_counts b 
    ON t.TimeStamp = b.TimeStamp 
    AND d.DeviceId = b.DeviceId 
    AND d.Detector = b.Detector
ORDER BY d.DeviceId, d.Detector, t.TimeStamp
{% else %}
SELECT *
FROM base_counts
{% endif %}


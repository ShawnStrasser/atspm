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
-- Bins where each device sent any event. Missing detector counts are only filled with 0
-- in these bins, so a device with a data outage gets no rows rather than zero counts.
,device_bins AS (
    SELECT DISTINCT
        TIME_BUCKET(interval '{{bin_size}} minutes', TimeStamp) as TimeStamp,
        DeviceId
    FROM {{from_table}}
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
FROM device_bins t
JOIN device_detectors d
    ON t.DeviceId = d.DeviceId
LEFT JOIN base_counts b 
    ON t.TimeStamp = b.TimeStamp 
    AND d.DeviceId = b.DeviceId 
    AND d.Detector = b.Detector
{% else %}
SELECT *
FROM base_counts
{% endif %}


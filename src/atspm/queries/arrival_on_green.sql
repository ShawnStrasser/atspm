--Arrival on Green
--Written in SQL for DuckDB. This is a jinja2 template, with variables inside curly braces.
--
--Each detector on event on an advance detector is projected to the vehicle's arrival at the stop bar. The
--arrival belongs to the bin it lands in, and is on green if the phase is green at that moment: from green
--begin (1) until yellow begin (8) or red clearance begin (10). A bin with vehicles and none on green reads 0.
--An actuation that trips the spillback flag, or begins while its detector holds it, is an arrival on red, see
--arrival_spillback.sql. A vehicle whose detector is still on can hold back its phase's bin, see arrival_hold.sql.
--With queue_model, a vehicle that waits behind a queue short of the detector is an arrival on red too, see
--arrival_queue.sql, and one the model has not finished with holds back its phase's bin.


WITH {% include 'arrival_phase_events.sql' %},

{% include 'arrival_projection.sql' %},

{% include 'arrival_hold.sql' %},

binned AS (
    SELECT time_bucket(INTERVAL '{{bin_size}} minutes', a.TimeStamp) AS Bin,
        a.DeviceId,
        a.Phase,
        a.Signal_State = 1 AND NOT a.Spillback{% if queue_model %} AND NOT a.Queued{% endif %} AS On_Green
    FROM with_signal_state a
    LEFT JOIN phase_ends e
        ON a.DeviceId = e.DeviceId
       AND a.Phase = e.Phase
    WHERE a.EventId = 82{% if queue_model %}
      AND a.Counted{% endif %}
      AND a.TimeStamp < COALESCE(e.Phase_End, (SELECT TimeStamp FROM results_end))
)

{%- if int_keys %}
SELECT b.TimeStamp, k.DeviceId, b.Phase, b.Total_Actuations, b.Percent_AOG
FROM (
{%- endif %}
SELECT Bin AS TimeStamp,
    DeviceId,
    Phase,
    COUNT(*)::INT16 AS Total_Actuations,
    (COUNT(*) FILTER (WHERE On_Green))::FLOAT / COUNT(*) AS Percent_AOG
FROM binned
GROUP BY Bin, DeviceId, Phase
{%- if int_keys %}
) b
JOIN device_keys k ON k.Dev = b.DeviceId
{%- endif %}

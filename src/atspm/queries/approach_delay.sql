--Approach Delay
--Written in SQL for DuckDB. This is a jinja2 template, with variables inside curly braces.
--
--Each detector on event on an advance detector is projected to the vehicle's arrival at the stop bar, as in
--arrival_on_green, and counts in the bin it lands in. Delay_Seconds follows UDOT's Approach Delay: an arrival on red waits
--until the next green. UDOT gives a vehicle on green or yellow no delay. A vehicle crossing on green or yellow under the
--spillback flag (see arrival_spillback.sql) sat in a queue no one saw, so Queued_Delay_Seconds estimates its wait as half
--the time from the later of red start and flag start to its green. That has no published method. A wait or an estimate
--over 360 seconds adds to neither column.
--A vehicle's bin is written once data reach 360 seconds past the end of the bin, when no vehicle of it can still be waiting,
--and none of its vehicles is held for a detector still on (see approach_delay_vehicles.sql).
--With queue_model, Queue_Delay_Seconds is the delay by the Queue Model, the whole delay and not added to the other two; it
--falls back to Delay_Seconds plus Queued_Delay_Seconds where the model does not run (see approach_delay_vehicles.sql).
{% set max_wait_seconds = 360 %}

WITH {% include 'approach_delay_vehicles.sql' %}

{%- if int_keys %}
SELECT b.* REPLACE (k.DeviceId AS DeviceId)
FROM (
{%- endif %}
SELECT Bin AS TimeStamp,
    DeviceId,
    Phase,
    (SUM(Delay_Milliseconds) / 1000.0)::FLOAT AS Delay_Seconds,
    (SUM(Queued_Half_Milliseconds) / 2000.0)::FLOAT AS Queued_Delay_Seconds{% if queue_model %},
    (SUM(Queue_Half_Milliseconds) / 2000.0)::FLOAT AS Queue_Delay_Seconds{% endif %}
FROM bin_parts
{%- if carry_bins %}
WHERE Bin + INTERVAL '{{bin_size}} minutes' + INTERVAL {{max_wait_seconds}} SECOND <= (SELECT Last_Event FROM data_end)
  AND (Bin, DeviceId, Phase) NOT IN (SELECT (Bin, DeviceId, Phase) FROM held_bins)
{%- endif %}
GROUP BY Bin, DeviceId, Phase
{%- if int_keys %}
) b
JOIN device_keys k ON k.Dev = b.DeviceId
{%- endif %}

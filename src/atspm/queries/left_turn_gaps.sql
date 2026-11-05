--Left-Turn Gaps
--Written in SQL for DuckDB. This is a jinja2 template, with variables inside curly braces.
--
--The gaps in the traffic on each through phase, for the left turn opposing it, in slices of gap length. A dashboard then
--picks any critical gap, or one for each approach. Every second of the phase's green and yellow is in one row of its bin:
--a gap's seconds in the slice of its length, 0.1 s wide from 3 to 10 seconds, and all other time at Gap_Length 0. So the
--share at a critical gap is SUM(Seconds) at or above it over SUM(Seconds). A gap counts in Gaps in the bin it ends in.

WITH {% include 'left_turn_gaps_stretches.sql' %}

SELECT p.Bin AS TimeStamp,
    p.DeviceId,
    p.Phase,
    -- DOUBLE, as a FLOAT 4.1 reads as less than 4.1
    p.Tenths::DOUBLE / 10 AS Gap_Length,
    SUM(p.Gaps)::INT AS Gaps,
    (SUM(p.Milliseconds) / 1000.0)::FLOAT AS Seconds
FROM bin_parts p
{%- if carry_bins %}
JOIN device_ends e ON p.DeviceId = e.DeviceId
WHERE p.Bin < e.Next_Bin
{%- endif %}
GROUP BY p.Bin, p.DeviceId, p.Phase, p.Tenths

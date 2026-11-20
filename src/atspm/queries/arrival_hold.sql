-- Included by arrival_on_green.sql and arrival_on_green_carry.sql, after arrival_projection.sql. A list of CTEs, not a query.
{%- if queue_model %}

{% include 'arrival_queue.sql' %},
{%- endif %}

-- Each arrival with the signal as it stood when it reached the stop bar
{%- if 'state' in (shared_stages or []) %}
-- Built once for this run by arrival_shared.sql
with_signal_state AS (SELECT * FROM _arrival_state),
{%- else %}
with_signal_state AS (
    SELECT *,
        LAST_VALUE(CASE WHEN EventId < 82 THEN EventId END IGNORE NULLS) OVER (
            PARTITION BY DeviceId, Phase ORDER BY TimeStamp, EventId
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS Signal_State
    FROM (
        SELECT TimeStamp, DeviceId, EventId, Phase, FALSE AS Spillback, FALSE AS Unresolved, NULL::INT16 AS Detector{% if queue_model %},
            FALSE AS Queued, TRUE AS Final, FALSE AS Counted, FALSE AS Modelled, NULL::TIMESTAMP AS On_Time, FALSE AS Sat, FALSE AS Still_On{% endif %}
        FROM phase_events
        UNION ALL
        SELECT TimeStamp, DeviceId, 82::INT16 AS EventId, Phase, Spillback, Unresolved, Detector{% if queue_model %},
            Queued, Final, Counted, Modelled, On_Time, Sat, Still_On{% endif %}
        FROM {% if queue_model %}arrivals_queued{% else %}arrivals{% endif %}
    )
),
{%- endif %}

-- An unresolved vehicle, its detector still on (see arrival_spillback.sql), trips the flag if its off comes more than
-- spillback_on_seconds after its on, and then it is an arrival on red. Where it lands after results stop, or on green,
-- that changes what it adds, so it waits for the run that sees its off, through its detector's 950. On red or yellow
-- before results stop it reads the same either way and counts now. A run with nothing after it counts it as it stands.
-- With the queue model every unresolved vehicle waits, since the model needs it too.
held AS (
    {%- if spillback_on_seconds and carry_bins %}
    SELECT a.DeviceId, a.Detector
    FROM with_signal_state a
    CROSS JOIN results_end r
    WHERE a.EventId = 82 AND a.Unresolved
    GROUP BY a.DeviceId, a.Detector
    HAVING {% if queue_model %}TRUE{% else %}BOOL_OR(r.TimeStamp IS NULL OR a.TimeStamp >= r.TimeStamp OR a.Signal_State = 1){% endif %}
    {%- else %}
    SELECT DeviceId, Detector FROM arrivals WHERE FALSE
    {%- endif %}
),

-- Results of a phase that a held vehicle reaches before results stop stop at the start of its bin, so that bin waits
-- for the run that counts it, and so do they at a vehicle the queue model has not finished with. Other phases keep
-- results_end.
phase_ends AS (
    SELECT DeviceId, Phase, MIN(time_bucket(INTERVAL '{{bin_size}} minutes', TimeStamp)) AS Phase_End
    FROM (
        SELECT a.DeviceId, a.Phase, a.TimeStamp
        FROM with_signal_state a
        JOIN held h
            ON a.DeviceId = h.DeviceId
           AND a.Detector = h.Detector
        WHERE a.EventId = 82
          AND a.Unresolved
        {%- if queue_model %}
        UNION ALL
        SELECT DeviceId, Phase, TimeStamp
        FROM with_signal_state
        WHERE EventId = 82 AND Counted AND NOT Final
        {%- endif %}
    )
    WHERE TimeStamp < (SELECT TimeStamp FROM results_end)
    GROUP BY DeviceId, Phase
)

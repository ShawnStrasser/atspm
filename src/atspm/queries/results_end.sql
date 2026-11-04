-- Included where results are written once per bin. A single CTE, not a query.
--
-- results_end: results stop here, and anything from here on waits for a later run. A bin is only written
-- once all of it is known, by the first run whose data reaches its end, so one bin is one row wherever runs
-- are cut. A run whose last event is within a second of the end of a bin reached the end of that bin. Otherwise
-- the bin it stopped in waits for the next run. A run with nothing after it (no unmatched events file) writes
-- all it has instead.
results_end AS (
    SELECT CASE
        WHEN Bin_Start + INTERVAL '{{bin_size}} minutes' - Last_Event <= INTERVAL 1 SECOND
            THEN Bin_Start + INTERVAL '{{bin_size}} minutes'
        {%- if carry_bins %}
        ELSE Bin_Start
        {%- else %}
        ELSE Last_Event + INTERVAL 1 MILLISECOND
        {%- endif %}
    END AS TimeStamp
    FROM (
        SELECT MAX(TimeStamp) AS Last_Event,
            time_bucket(INTERVAL '{{bin_size}} minutes', MAX(TimeStamp)) AS Bin_Start
        FROM raw_data
    )
)

-- Device silences: consecutive events from a device further apart than the threshold for the time of day
-- the silence starts. Included by timeline.sql and run on its own by SignalDataProcessor.
-- Every event in raw_data counts, whatever its EventId. raw_data_all is not used, since its carried
-- unmatched events are old interval starts, not the device's last event. Incremental runs add the
-- previous run's per-device last event time (synthetic EventId 936), so a silence across runs is seen.
-- event_gap_schedule is a list of [minute of day, seconds], sorted by minute. Each threshold applies from
-- its minute until the next one, and the last wraps past midnight.
SELECT DeviceID, PrevTimeStamp AS StartTime, TimeStamp AS EndTime
FROM (
	SELECT DeviceID, TimeStamp,
	       LAG(TimeStamp) OVER (PARTITION BY DeviceID ORDER BY TimeStamp) AS PrevTimeStamp
	FROM (
		SELECT DeviceID, TimeStamp FROM raw_data
		{% if unmatched %}
		UNION ALL
		SELECT DeviceID, TimeStamp FROM unmatched_previous WHERE EventId = 936
		{% endif %}
	)
)
WHERE DATE_DIFF('millisecond', PrevTimeStamp, TimeStamp) > 1000 * CASE
	{%- for minute, seconds in event_gap_schedule|reverse %}
	WHEN HOUR(PrevTimeStamp) * 60 + MINUTE(PrevTimeStamp) >= {{ minute }} THEN {{ seconds }}
	{%- endfor %}
	ELSE {{ event_gap_schedule[-1][1] }} END

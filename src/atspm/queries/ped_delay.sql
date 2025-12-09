-- Average pedestrian delay derived from the timeline aggregation.
-- Buckets are based on EndTime (when walk starts), rounded down to bin_size.
SELECT
  TIME_BUCKET(INTERVAL '{{bin_size}} minutes', EndTime) AS TimeStamp,
  DeviceId,
  EventValue AS Phase,
  AVG(Duration) AS AvgPedDelay,
  COUNT(*) AS Samples
FROM timeline
WHERE EventClass = 'Ped Delay'
  AND IsValid
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3

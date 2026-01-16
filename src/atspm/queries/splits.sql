--Aggregate Phase Splits in 15-minute Bins
--Written in SQL for DuckDB. This is a jinja2 template, with variables inside curly braces.
--MAXTIME Specific!

SELECT 
	TIME_BUCKET(interval '{{bin_size}} minutes', TimeStamp) as TimeStamp,
	DeviceId,
	EventId::int16 AS EventId,
	COUNT(*)::int16 AS Services, 
	AVG(Parameter)::float AS Average_Split
FROM 
	{{from_table}}
WHERE 
	EventID BETWEEN 300 AND 317
GROUP BY ALL
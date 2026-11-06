"""event_gaps.sql sorts only the first and last event of each stretch of time. It must find what sorting every event finds."""
import duckdb
import pandas as pd
import pytest
from src.atspm.data_aggregator import render_query

EVERY_EVENT = """
    SELECT DeviceId, PrevTimeStamp AS StartTime, TimeStamp AS EndTime
    FROM (SELECT DeviceId, TimeStamp, LAG(TimeStamp) OVER (PARTITION BY DeviceId ORDER BY TimeStamp) AS PrevTimeStamp
          FROM ({source}))
    WHERE DATE_DIFF('millisecond', PrevTimeStamp, TimeStamp) > 1000 * CASE {cases} ELSE {last} END
"""
SEED_SQL = """
    SELECT DeviceId, TIMESTAMP '2026-09-01 00:00:00' + to_milliseconds(CAST(t AS BIGINT)) AS TimeStamp
    FROM (SELECT 'D' || (i % 4) AS DeviceId,
                 -- Events every 100 ms to a few minutes apart, on and just off the edges of the thresholds
                 SUM(CASE hash(i) % 40 WHEN 0 THEN 119999 WHEN 1 THEN 120000 WHEN 2 THEN 120001 WHEN 3 THEN 300000 WHEN 4 THEN 300001
                                       WHEN 5 THEN 900001 WHEN 6 THEN 0 ELSE (hash(i) % 900 + 1) * 100 END)
                     OVER (PARTITION BY i % 4 ORDER BY i) AS t
          FROM range(40000) r(i))
"""


def brute_force(conn, schedule, source):
    cases = ' '.join(f"WHEN HOUR(PrevTimeStamp) * 60 + MINUTE(PrevTimeStamp) >= {m} THEN {s}" for m, s in reversed(schedule))
    return conn.execute(EVERY_EVENT.format(source=source, cases=cases, last=schedule[-1][1]) + ' ORDER BY ALL').fetchall()


@pytest.mark.parametrize('schedule', [
    [[300, 300], [360, 120], [1260, 300], [1380, 900]],  # the default
    [[0, 60]],
])
def test_same_silences_as_sorting_every_event(schedule):
    conn = duckdb.connect()
    conn.execute(f"CREATE TABLE raw_data AS {SEED_SQL}")
    expected = brute_force(conn, schedule, "SELECT DeviceId, TimeStamp FROM raw_data")
    assert expected, 'the events should have silences to find'
    query = render_query('event_gaps', unmatched=False, event_gap_schedule=schedule)
    assert conn.execute(query + ' ORDER BY ALL').fetchall() == expected


def test_same_silences_with_the_last_event_from_the_previous_run():
    schedule = [[0, 120]]
    conn = duckdb.connect()
    conn.execute(f"CREATE TABLE raw_data AS {SEED_SQL}")
    conn.execute("""CREATE TABLE unmatched_previous AS
        SELECT MIN(TimeStamp) - INTERVAL 10 MINUTE AS TimeStamp, DeviceId, 936 AS EventId FROM raw_data GROUP BY DeviceId
        UNION ALL
        SELECT MIN(TimeStamp), DeviceId, 1 FROM raw_data GROUP BY DeviceId""")
    expected = brute_force(conn, schedule, """SELECT DeviceId, TimeStamp FROM raw_data
        UNION ALL SELECT DeviceId, TimeStamp FROM unmatched_previous WHERE EventId = 936""")
    query = render_query('event_gaps', unmatched=True, event_gap_schedule=schedule)
    rows = conn.execute(query + ' ORDER BY ALL').fetchall()
    assert rows == expected
    assert sum(1 for device, start, end in rows if end == start + pd.Timedelta(minutes=10)) >= 4

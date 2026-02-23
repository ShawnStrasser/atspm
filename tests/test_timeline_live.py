import pandas as pd
from datetime import datetime, timedelta

from src.atspm import SignalDataProcessor


def _build_raw_df(events):
    df = pd.DataFrame(events, columns=["TimeStamp", "DeviceId", "EventId", "Parameter"])
    df["DeviceId"] = df["DeviceId"].astype("int64")
    df["EventId"] = df["EventId"].astype("int16")
    df["Parameter"] = df["Parameter"].astype("int16")
    return df


def _build_processor(raw_data, unmatched_df, live=None):
    timeline_params = {"min_duration": 0.0, "cushion_time": 60}
    if live is not None:
        timeline_params["live"] = live

    return SignalDataProcessor(
        raw_data=raw_data,
        detector_config=pd.DataFrame(columns=["DeviceId", "Phase", "Parameter", "Function"]),
        bin_size=15,
        verbose=0,
        remove_incomplete=False,
        unmatched_event_settings={"df_or_path": unmatched_df, "max_days_old": 14},
        aggregations=[
            {"name": "has_data", "params": {"no_data_min": 15, "min_data_points": 1}},
            {"name": "timeline", "params": timeline_params},
        ],
    )


def test_timeline_live_keeps_incomplete_and_uses_bin_endtime():
    base = datetime(2026, 1, 1, 10, 45, 0)
    chunk1 = _build_raw_df(
        [
            (base + timedelta(minutes=5), 1001, 43, 1),   # 10:50 unmatched
            (base + timedelta(minutes=11), 1001, 61, 2),  # 10:56 unmatched
        ]
    )

    empty_unmatched = pd.DataFrame(columns=["TimeStamp", "DeviceId", "EventId", "Parameter", "IsValid"])
    p1 = _build_processor(chunk1, empty_unmatched, live=True)
    p1.load()
    p1.aggregate()
    timeline1 = p1.conn.sql("SELECT * FROM timeline ORDER BY StartTime, EventClass, EventValue").df()
    unmatched1 = p1.conn.sql("SELECT * FROM unmatched_events ORDER BY TimeStamp, EventId").df()
    p1.close()

    expected_end = chunk1["TimeStamp"].max().floor("15min") + pd.Timedelta(minutes=15)

    assert len(unmatched1) > 0, "Expected unmatched events to be captured for incremental processing"
    assert len(timeline1) == len(unmatched1), "Expected live mode to keep incomplete timeline events"
    assert timeline1["EndTime"].nunique() == 1, "All incomplete live rows should use one common EndTime"
    assert timeline1["EndTime"].iloc[0] == expected_end
    assert (timeline1["IsValid"] == False).all(), "Incomplete live rows must be marked invalid"

    chunk2 = _build_raw_df(
        [
            (base + timedelta(minutes=17), 1001, 44, 1),  # closes prior 43
            (base + timedelta(minutes=18), 1001, 64, 2),  # closes prior 61
        ]
    )

    p2 = _build_processor(chunk2, unmatched1, live=True)
    p2.load()
    p2.aggregate()
    timeline2 = p2.conn.sql("SELECT * FROM timeline ORDER BY StartTime, EventClass, EventValue").df()
    p2.close()

    prior_call = timeline2[
        (timeline2["EventClass"] == "Phase Call")
        & (timeline2["StartTime"] == pd.Timestamp("2026-01-01 10:50:00"))
    ]
    assert len(prior_call) == 1, "Expected prior unmatched Phase Call to be reloaded and matched next run"
    assert prior_call.iloc[0]["EndTime"] == pd.Timestamp("2026-01-01 11:02:00")


def test_timeline_live_defaults_to_false_when_omitted():
    base = datetime(2026, 1, 1, 10, 45, 0)
    chunk = _build_raw_df(
        [
            (base + timedelta(minutes=5), 1001, 43, 1),
            (base + timedelta(minutes=11), 1001, 61, 2),
        ]
    )

    empty_unmatched = pd.DataFrame(columns=["TimeStamp", "DeviceId", "EventId", "Parameter", "IsValid"])
    processor = _build_processor(chunk, empty_unmatched, live=None)
    processor.load()
    processor.aggregate()
    timeline = processor.conn.sql("SELECT * FROM timeline").df()
    processor.close()

    assert len(timeline) == 0, "Without live=True, incomplete timeline events should still be removed"

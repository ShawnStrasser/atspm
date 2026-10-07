import pandas as pd

from src.atspm import SignalDataProcessor


def _build_raw_df(events):
    df = pd.DataFrame(events, columns=["TimeStamp", "DeviceId", "EventId", "Parameter"])
    df["TimeStamp"] = pd.to_datetime(df["TimeStamp"])
    df["DeviceId"] = df["DeviceId"].astype("int64")
    df["EventId"] = df["EventId"].astype("int16")
    df["Parameter"] = df["Parameter"].astype("int16")
    return df


def _build_processor(raw_data, controller_type="siemens", unmatched_df=None):
    kwargs = {}
    if unmatched_df is not None:
        kwargs["unmatched_event_settings"] = {"df_or_path": unmatched_df, "max_days_old": 14}
    return SignalDataProcessor(
        raw_data=raw_data,
        detector_config=pd.DataFrame(columns=["DeviceId", "Phase", "Parameter", "Function"]),
        bin_size=15,
        verbose=0,
        remove_incomplete=False,
        controller_type=controller_type,
        aggregations=[
            {"name": "has_data", "params": {"no_data_min": 15, "min_data_points": 1}},
            {"name": "timeline", "params": {"min_duration": 0, "cushion_time": 60}},
        ],
        **kwargs,
    )


def _run(raw, controller_type="siemens"):
    with _build_processor(raw, controller_type) as processor:
        processor.load()
        processor.aggregate()
        return processor.conn.query("SELECT * FROM timeline").df()


def _lookup(timeline, event_class, start_time, event_value):
    row = timeline[
        (timeline["EventClass"] == event_class)
        & (timeline["StartTime"] == pd.Timestamp(start_time))
        & (timeline["EventValue"] == event_value)
    ]
    assert len(row) == 1, f"expected one {event_class} {event_value} at {start_time}, got {len(row)}"
    return row.iloc[0]


# Patterns from Siemens hourly logs, where events between the top of the hour and the restart (1000) are lost
RAW = _build_raw_df(
    [
        # Device 1: yellow starts before the hour, its end is lost and the snapshot at the restart ends it
        ("2026-10-04 22:59:55.9", 1, 8, 6),
        ("2026-10-04 22:59:56.0", 1, 82, 1),
        ("2026-10-04 23:00:03.8", 1, 9, 6),
        ("2026-10-04 23:00:03.8", 1, 1000, 0),
        # Device 1: a yellow away from the hour is unaffected
        ("2026-10-04 22:30:20.0", 1, 8, 2),
        ("2026-10-04 22:30:24.0", 1, 9, 2),
        # Device 2: last event well before the hour, so the window starts at the top of the hour
        ("2026-10-04 21:59:30.0", 2, 8, 4),
        ("2026-10-04 21:59:34.0", 2, 9, 4),
        ("2026-10-04 21:59:40.5", 2, 81, 3),
        # Device 2: yellow already running is restamped with the snapshot at the restart, so it looks short
        ("2026-10-04 22:00:03.0", 2, 8, 2),
        ("2026-10-04 22:00:03.0", 2, 1000, 0),
        ("2026-10-04 22:00:04.2", 2, 9, 2),
        # Device 2: a yellow starting after the restart is unaffected
        ("2026-10-04 22:00:10.0", 2, 8, 6),
        ("2026-10-04 22:00:14.0", 2, 9, 6),
        # Device 3: restart 26.7 seconds after the hour
        ("2026-10-04 20:59:52.2", 3, 8, 2),
        ("2026-10-04 20:59:56.2", 3, 9, 2),
        ("2026-10-04 21:00:26.7", 3, 8, 1),
        ("2026-10-04 21:00:26.7", 3, 1000, 0),
        ("2026-10-04 21:00:28.3", 3, 9, 1),
    ]
)


def test_siemens_log_restart_invalidates_intervals_across_the_gap():
    timeline = _run(RAW)

    assert _lookup(timeline, "Yellow", "2026-10-04 22:59:55.9", 6)["IsValid"] == False
    assert _lookup(timeline, "Yellow", "2026-10-04 22:00:03.0", 2)["IsValid"] == False
    assert _lookup(timeline, "Yellow", "2026-10-04 21:00:26.7", 1)["IsValid"] == False

    assert _lookup(timeline, "Yellow", "2026-10-04 22:30:20.0", 2)["IsValid"] == True
    assert _lookup(timeline, "Yellow", "2026-10-04 21:59:30.0", 4)["IsValid"] == True
    assert _lookup(timeline, "Yellow", "2026-10-04 22:00:10.0", 6)["IsValid"] == True
    assert _lookup(timeline, "Yellow", "2026-10-04 20:59:52.2", 2)["IsValid"] == True


def test_event_1000_is_ignored_without_siemens_controller_type():
    timeline = _run(RAW, controller_type="")

    assert _lookup(timeline, "Yellow", "2026-10-04 22:59:55.9", 6)["IsValid"] == True
    assert _lookup(timeline, "Yellow", "2026-10-04 22:00:03.0", 2)["IsValid"] == True
    assert _lookup(timeline, "Yellow", "2026-10-04 21:00:26.7", 1)["IsValid"] == True


def test_siemens_log_restart_across_incremental_runs():
    empty_unmatched = pd.DataFrame(columns=["TimeStamp", "DeviceId", "EventId", "Parameter", "IsValid"])
    chunk1 = _build_raw_df(
        [
            ("2026-10-04 22:45:00.0", 1, 82, 1),
            ("2026-10-04 22:59:55.9", 1, 8, 6),   # yellow open at the end of the run, ends after the restart
            ("2026-10-04 22:59:56.0", 1, 82, 1),
        ]
    )
    p1 = _build_processor(chunk1, unmatched_df=empty_unmatched)
    p1.load()
    p1.aggregate()
    unmatched1 = p1.conn.query("SELECT * FROM unmatched_events").df()
    p1.close()

    chunk2 = _build_raw_df(
        [
            ("2026-10-04 23:00:03.8", 1, 9, 6),
            ("2026-10-04 23:00:03.8", 1, 8, 2),   # restamped yellow in the snapshot, open at the end of the run
            ("2026-10-04 23:00:03.8", 1, 1000, 0),
            ("2026-10-04 23:00:10.0", 1, 8, 4),   # yellow after the restart, open at the end of the run
        ]
    )
    p2 = _build_processor(chunk2, unmatched_df=unmatched1)
    p2.load()
    p2.aggregate()
    timeline2 = p2.conn.query("SELECT * FROM timeline").df()
    unmatched2 = p2.conn.query("SELECT * FROM unmatched_events").df()
    p2.close()

    assert _lookup(timeline2, "Yellow", "2026-10-04 22:59:55.9", 6)["IsValid"] == False

    chunk3 = _build_raw_df(
        [
            ("2026-10-04 23:00:08.0", 1, 9, 2),
            ("2026-10-04 23:00:14.0", 1, 9, 4),
        ]
    )
    p3 = _build_processor(chunk3, unmatched_df=unmatched2)
    p3.load()
    p3.aggregate()
    timeline3 = p3.conn.query("SELECT * FROM timeline").df()
    p3.close()

    assert _lookup(timeline3, "Yellow", "2026-10-04 23:00:03.8", 2)["IsValid"] == False
    assert _lookup(timeline3, "Yellow", "2026-10-04 23:00:10.0", 4)["IsValid"] == True

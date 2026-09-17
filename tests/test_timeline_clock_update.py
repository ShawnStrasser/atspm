import pandas as pd

from src.atspm import SignalDataProcessor


def _build_raw_df(events):
    df = pd.DataFrame(events, columns=["TimeStamp", "DeviceId", "EventId", "Parameter"])
    df["TimeStamp"] = pd.to_datetime(df["TimeStamp"])
    df["DeviceId"] = df["DeviceId"].astype("int64")
    df["EventId"] = df["EventId"].astype("int16")
    df["Parameter"] = df["Parameter"].astype("int16")
    return df


def _build_processor(raw_data, unmatched_df=None):
    kwargs = {}
    if unmatched_df is not None:
        kwargs["unmatched_event_settings"] = {"df_or_path": unmatched_df, "max_days_old": 14}
    return SignalDataProcessor(
        raw_data=raw_data,
        detector_config=pd.DataFrame(columns=["DeviceId", "Phase", "Parameter", "Function"]),
        bin_size=15,
        verbose=0,
        remove_incomplete=False,
        aggregations=[
            {"name": "has_data", "params": {"no_data_min": 15, "min_data_points": 1}},
            {"name": "timeline", "params": {"min_duration": 0, "cushion_time": 60}},
        ],
        **kwargs,
    )


def _lookup(timeline, event_class, start_time, event_value=None):
    row = timeline[
        (timeline["EventClass"] == event_class)
        & (timeline["StartTime"] == pd.Timestamp(start_time))
    ]
    if event_value is not None:
        row = row[row["EventValue"] == event_value]
    assert len(row) == 1, f"expected one {event_class} at {start_time}, got {len(row)}"
    return row.iloc[0]


def test_clock_update_invalidates_intervals_overlapping_window():
    raw = _build_raw_df(
        [
            # Clock update with a 3 second correction; its window is 10:00:05 to 10:00:15
            ("2026-01-01 10:00:10", 1, 181, 3),
            # Phase 2 green spans the whole window
            ("2026-01-01 10:00:00", 1, 1, 2),
            ("2026-01-01 10:00:30", 1, 7, 2),
            # Phase 3 green ends inside the window, before the clock update
            ("2026-01-01 09:59:50", 1, 1, 3),
            ("2026-01-01 10:00:07", 1, 7, 3),
            # Phase 4 green starts inside the window, after the clock update
            ("2026-01-01 10:00:12", 1, 1, 4),
            ("2026-01-01 10:00:40", 1, 7, 4),
            # Phase 6 green ends exactly where the window starts
            ("2026-01-01 09:59:00", 1, 1, 6),
            ("2026-01-01 10:00:05", 1, 7, 6),
            # Phase 8 green starts exactly where the window ends
            ("2026-01-01 10:00:15", 1, 1, 8),
            ("2026-01-01 10:00:45", 1, 7, 8),
            # Instant events whose cushioned EndTime covers the window stay valid
            ("2026-01-01 10:00:05", 1, 131, 2),
            ("2026-01-01 10:00:05", 1, 133, 45),
            # A clock update on another device does not affect device 1
            ("2026-01-01 10:05:00", 2, 181, 0),
            ("2026-01-01 10:04:00", 1, 1, 1),
            ("2026-01-01 10:06:00", 1, 7, 1),
            # Ped detector 3 failed then restored
            ("2026-01-01 10:10:00", 1, 91, 3),
            ("2026-01-01 10:12:00", 1, 92, 3),
        ]
    )

    with _build_processor(raw) as processor:
        processor.load()
        processor.aggregate()
        timeline = processor.conn.query("SELECT * FROM timeline").df()

    assert _lookup(timeline, "Green", "2026-01-01 10:00:00", 2)["IsValid"] == False
    assert _lookup(timeline, "Green", "2026-01-01 09:59:50", 3)["IsValid"] == False
    assert _lookup(timeline, "Green", "2026-01-01 10:00:12", 4)["IsValid"] == False
    assert _lookup(timeline, "Green", "2026-01-01 09:59:00", 6)["IsValid"] == True
    assert _lookup(timeline, "Green", "2026-01-01 10:00:15", 8)["IsValid"] == True
    assert _lookup(timeline, "Green", "2026-01-01 10:04:00", 1)["IsValid"] == True

    # Clock updates are shown like other instant events: starting at the event, lasting cushion_time,
    # and keeping the correction as EventValue
    clock = _lookup(timeline, "Clock Update", "2026-01-01 10:00:10")
    assert clock["EndTime"] == pd.Timestamp("2026-01-01 10:01:10")
    assert clock["Duration"] == 60.0
    assert clock["EventValue"] == 3
    assert clock["IsValid"] == True
    assert _lookup(timeline, "Clock Update", "2026-01-01 10:05:00")["EventValue"] == 0

    assert _lookup(timeline, "Pattern Change", "2026-01-01 10:00:05")["IsValid"] == True
    offset = _lookup(timeline, "Offset Change", "2026-01-01 10:00:05")
    assert offset["EventValue"] == 45
    assert offset["IsValid"] == True

    ped_fault = _lookup(timeline, "Ped Detector Failed", "2026-01-01 10:10:00")
    assert ped_fault["EventValue"] == 3
    assert ped_fault["Duration"] == 120.0
    assert ped_fault["IsValid"] == True


def test_clock_update_invalidates_interval_matched_in_next_incremental_run():
    empty_unmatched = pd.DataFrame(columns=["TimeStamp", "DeviceId", "EventId", "Parameter", "IsValid"])
    chunk1 = _build_raw_df(
        [
            ("2026-01-01 10:50:00", 1, 1, 2),    # green starts, ends next chunk
            ("2026-01-01 10:55:00", 1, 181, 1),  # clock update while it is still open
            ("2026-01-01 10:55:03", 1, 1, 6),    # green starts inside the clock update window
            ("2026-01-01 10:56:00", 1, 1, 4),    # green starts after the clock update window
        ]
    )
    p1 = _build_processor(chunk1, empty_unmatched)
    p1.load()
    p1.aggregate()
    unmatched1 = p1.conn.query("SELECT * FROM unmatched_events").df()
    p1.close()

    chunk2 = _build_raw_df(
        [
            ("2026-01-01 11:02:00", 1, 7, 2),
            ("2026-01-01 11:03:00", 1, 7, 4),
            ("2026-01-01 11:04:00", 1, 7, 6),
        ]
    )
    p2 = _build_processor(chunk2, unmatched1)
    p2.load()
    p2.aggregate()
    timeline = p2.conn.query("SELECT * FROM timeline").df()
    p2.close()

    assert _lookup(timeline, "Green", "2026-01-01 10:50:00", 2)["IsValid"] == False
    assert _lookup(timeline, "Green", "2026-01-01 10:55:03", 6)["IsValid"] == False
    assert _lookup(timeline, "Green", "2026-01-01 10:56:00", 4)["IsValid"] == True

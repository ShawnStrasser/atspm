import pandas as pd

from src.atspm import SignalDataProcessor


def test_overlap_red_requires_overlap_off_terminal_event():
    df = pd.DataFrame(
        [
            ("2026-04-13 20:05:35.900", "test", 61, 10),
            ("2026-04-13 20:05:42.800", "test", 63, 10),
            ("2026-04-13 20:05:46.300", "test", 64, 10),
            ("2026-04-13 20:10:08.000", "test", 61, 10),
            ("2026-04-13 20:10:14.100", "test", 63, 10),
            ("2026-04-13 20:10:17.600", "test", 64, 10),
            ("2026-04-13 20:10:18.600", "test", 65, 10),
        ],
        columns=["TimeStamp", "DeviceId", "EventId", "Parameter"],
    )
    df["TimeStamp"] = pd.to_datetime(df["TimeStamp"])

    with SignalDataProcessor(
        raw_data=df,
        detector_config=pd.DataFrame(columns=["DeviceId", "Phase", "Parameter", "Function"]),
        bin_size=15,
        verbose=0,
        aggregations=[
            {"name": "has_data", "params": {"no_data_min": 15, "min_data_points": 1}},
            {"name": "timeline", "params": {"maxtime": True, "min_duration": 0, "cushion_time": 60}},
        ],
    ) as processor:
        processor.load()
        processor.aggregate()
        overlap_red = processor.conn.query(
            """
            SELECT StartTime, EndTime, EventClass, EventValue, IsValid
            FROM timeline
            WHERE EventClass = 'Overlap Red'
            ORDER BY StartTime
            """
        ).df()

    invalid_red = overlap_red[overlap_red["IsValid"] == False]
    valid_red = overlap_red[overlap_red["IsValid"] == True]

    assert len(invalid_red) == 1
    assert invalid_red.iloc[0]["StartTime"] == pd.Timestamp("2026-04-13 20:05:46.300")
    assert invalid_red.iloc[0]["EndTime"] == pd.Timestamp("2026-04-13 20:10:08.000")
    assert invalid_red.iloc[0]["EventValue"] == 10

    assert len(valid_red) == 1
    assert valid_red.iloc[0]["StartTime"] == pd.Timestamp("2026-04-13 20:10:17.600")
    assert valid_red.iloc[0]["EndTime"] == pd.Timestamp("2026-04-13 20:10:18.600")
    assert valid_red.iloc[0]["EventValue"] == 10


def test_phase_yellow_red_duration_sanity_check():
    """Phase yellow/red intervals longer than 10 seconds are bad data, so IsValid must be False.

    Overlap yellow/red have no such limit and must be left alone.
    """
    df = pd.DataFrame(
        [
            # Normal phase 2 clearance: 4s yellow, 2s red
            ("2026-04-13 20:00:00.000", "test", 8, 2),
            ("2026-04-13 20:00:04.000", "test", 9, 2),
            ("2026-04-13 20:00:04.000", "test", 10, 2),
            ("2026-04-13 20:00:06.000", "test", 11, 2),
            # Implausible clearance: 12s yellow, 15s red
            ("2026-04-13 20:01:00.000", "test", 8, 2),
            ("2026-04-13 20:01:12.000", "test", 9, 2),
            ("2026-04-13 20:01:12.000", "test", 10, 2),
            ("2026-04-13 20:01:27.000", "test", 11, 2),
            # Exactly at the 10s threshold, still valid
            ("2026-04-13 20:02:00.000", "test", 8, 2),
            ("2026-04-13 20:02:10.000", "test", 9, 2),
            # Overlap 5: long yellow/red, not subject to the limit
            ("2026-04-13 20:03:00.000", "test", 61, 5),
            ("2026-04-13 20:03:05.000", "test", 63, 5),
            ("2026-04-13 20:03:20.000", "test", 64, 5),
            ("2026-04-13 20:03:45.000", "test", 65, 5),
        ],
        columns=["TimeStamp", "DeviceId", "EventId", "Parameter"],
    )
    df["TimeStamp"] = pd.to_datetime(df["TimeStamp"])

    with SignalDataProcessor(
        raw_data=df,
        detector_config=pd.DataFrame(columns=["DeviceId", "Phase", "Parameter", "Function"]),
        bin_size=15,
        verbose=0,
        aggregations=[
            {"name": "has_data", "params": {"no_data_min": 15, "min_data_points": 1}},
            {"name": "timeline", "params": {"maxtime": True, "min_duration": 0, "cushion_time": 60}},
        ],
    ) as processor:
        processor.load()
        processor.aggregate()
        events = processor.conn.query(
            """
            SELECT EventClass, StartTime, Duration, IsValid
            FROM timeline
            WHERE EventClass IN ('Yellow', 'Red', 'Overlap Yellow', 'Overlap Red')
            ORDER BY EventClass, StartTime
            """
        ).df()

    def lookup(event_class, start_time):
        row = events[
            (events["EventClass"] == event_class)
            & (events["StartTime"] == pd.Timestamp(start_time))
        ]
        assert len(row) == 1, f"expected one {event_class} at {start_time}, got {len(row)}"
        return row.iloc[0]

    assert lookup("Yellow", "2026-04-13 20:00:00")["IsValid"] == True
    assert lookup("Red", "2026-04-13 20:00:04")["IsValid"] == True

    assert lookup("Yellow", "2026-04-13 20:01:00")["IsValid"] == False
    assert lookup("Red", "2026-04-13 20:01:12")["IsValid"] == False

    # Exactly 10 seconds is the boundary and stays valid
    assert lookup("Yellow", "2026-04-13 20:02:00")["IsValid"] == True

    # Overlaps are unaffected by the phase clearance limit
    overlap_yellow = lookup("Overlap Yellow", "2026-04-13 20:03:05")
    assert overlap_yellow["Duration"] == 15.0
    assert overlap_yellow["IsValid"] == True

    overlap_red = lookup("Overlap Red", "2026-04-13 20:03:20")
    assert overlap_red["Duration"] == 25.0
    assert overlap_red["IsValid"] == True

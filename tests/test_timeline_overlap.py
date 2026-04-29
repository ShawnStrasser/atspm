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

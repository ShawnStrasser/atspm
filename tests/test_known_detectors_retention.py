from datetime import datetime, timedelta

import pandas as pd

from src.atspm import SignalDataProcessor


def _events_df(events):
    df = pd.DataFrame(events, columns=["TimeStamp", "DeviceId", "EventId", "Parameter"])
    df["DeviceId"] = df["DeviceId"].astype("int64")
    df["EventId"] = df["EventId"].astype("int16")
    df["Parameter"] = df["Parameter"].astype("int16")
    return df


def test_known_detectors_max_days_old_applies_to_true_last_seen():
    """
    Regression test for incremental zero-fill retention.

    With known_detectors_max_days_old=2, a detector that last actuated ~24h ago
    should still appear in actuations with Total=0 (for detector-health flatline continuity).
    """
    base = datetime(2024, 1, 1, 0, 0, 0)
    detector_dead = 10
    detector_alive = 20

    detector_config = pd.DataFrame(
        [
            (1, 1, detector_dead, "Advance Detector"),
            (1, 1, detector_alive, "Advance Detector"),
        ],
        columns=["DeviceId", "Phase", "Parameter", "Function"],
    )

    chunks = [
        # Day 0: both detectors active
        _events_df(
            [
                (base + timedelta(minutes=5), 1, 82, detector_dead),
                (base + timedelta(minutes=10), 1, 82, detector_alive),
            ]
        ),
        # Day 1: both detectors active again (should refresh LastSeen for detector_dead)
        _events_df(
            [
                (base + timedelta(days=1, minutes=5), 1, 82, detector_dead),
                (base + timedelta(days=1, minutes=10), 1, 82, detector_alive),
            ]
        ),
        # Day 2: detector_dead has failed; detector_alive still reports
        _events_df(
            [
                (base + timedelta(days=2, minutes=10), 1, 82, detector_alive),
            ]
        ),
    ]

    known_detectors_df_or_path = ""
    outputs = []

    for chunk in chunks:
        params = {
            "raw_data": chunk,
            "detector_config": detector_config,
            "bin_size": 60,
            "verbose": 0,
            "aggregations": [
                {
                    "name": "actuations",
                    "params": {
                        "fill_in_missing": True,
                        "known_detectors_df_or_path": known_detectors_df_or_path,
                        "known_detectors_max_days_old": 2,
                    },
                }
            ],
        }
        processor = SignalDataProcessor(**params)
        processor.load()
        processor.aggregate()
        outputs.append(processor.conn.sql("SELECT * FROM actuations").df())
        known_detectors_df_or_path = processor.conn.sql("SELECT * FROM known_detectors").df()
        processor.close()

    day_1_output = outputs[1]
    day_2_output = outputs[2]

    day_1_live = day_1_output[
        (day_1_output["DeviceId"] == 1) & (day_1_output["Detector"] == detector_dead)
    ]
    assert len(day_1_live) == 1
    assert int(day_1_live.iloc[0]["Total"]) == 1

    day_2_flatline = day_2_output[
        (day_2_output["DeviceId"] == 1) & (day_2_output["Detector"] == detector_dead)
    ]
    assert len(day_2_flatline) == 1, (
        "Detector should still be present for zero-fill ~24h after last actuation "
        "when known_detectors_max_days_old=2."
    )
    assert int(day_2_flatline.iloc[0]["Total"]) == 0

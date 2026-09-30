from datetime import datetime, timedelta

import pandas as pd

from src.atspm import SignalDataProcessor

BASE = datetime(2024, 1, 1, 0, 0, 0)


def _events_df(events):
    df = pd.DataFrame(events, columns=["TimeStamp", "DeviceId", "EventId", "Parameter"])
    df["DeviceId"] = df["DeviceId"].astype("int64")
    df["EventId"] = df["EventId"].astype("int16")
    df["Parameter"] = df["Parameter"].astype("int16")
    return df


def _run(raw, known_detectors=None):
    params = {"fill_in_missing": True}
    if known_detectors is not None:
        params["known_detectors_df_or_path"] = known_detectors
    processor = SignalDataProcessor(
        raw_data=raw,
        bin_size=15,
        verbose=0,
        aggregations=[{"name": "actuations", "params": params}],
    )
    processor.load()
    processor.aggregate()
    acts = processor.conn.sql("SELECT * FROM actuations").df()
    known = processor.conn.sql("SELECT * FROM known_detectors").df() if known_detectors is not None else None
    return acts, known


def _bin(i):
    return pd.Timestamp(BASE + timedelta(minutes=15 * i))


def test_fill_in_missing_only_in_bins_where_device_has_data():
    events = []
    for i in range(4):  # device 1 reports in bins 0-3
        t = BASE + timedelta(minutes=15 * i, seconds=30)
        events.append((t, 1, 82, 1))
        if i != 2:  # detector 2 silent in bin 2 while the device still reports
            events.append((t, 1, 82, 2))
    for i in range(2):  # device 2 reports only in bins 0-1, then a feed outage
        t = BASE + timedelta(minutes=15 * i, seconds=30)
        events.append((t, 2, 82, 1))
    acts, _ = _run(_events_df(events))

    silent = acts[(acts.DeviceId == 1) & (acts.Detector == 2) & (acts.TimeStamp == _bin(2))]
    assert len(silent) == 1 and silent.Total.iloc[0] == 0
    assert len(acts[acts.DeviceId == 1]) == 8

    # No rows (zero-filled or otherwise) for device 2 during its outage
    dev2 = acts[acts.DeviceId == 2]
    assert sorted(dev2.TimeStamp) == [_bin(0), _bin(1)]


def test_known_detector_zero_filled_only_while_device_reports():
    # Run 1: detectors 1 and 2 seen at device 1
    run1 = _events_df([
        (BASE + timedelta(seconds=30), 1, 82, 1),
        (BASE + timedelta(seconds=40), 1, 82, 2),
    ])
    _, known = _run(run1, known_detectors="")

    # Run 2: device 1 reports in bins 4-5 through detector 1 and a non-detector event,
    # detector 2 (known only from run 1) is silent; nothing at all in bin 6
    run2 = _events_df([
        (BASE + timedelta(minutes=60, seconds=30), 1, 82, 1),
        (BASE + timedelta(minutes=75, seconds=30), 1, 1, 2),
    ])
    acts, _ = _run(run2, known_detectors=known)

    det2 = acts[acts.Detector == 2].sort_values("TimeStamp")
    assert list(det2.TimeStamp) == [_bin(4), _bin(5)]
    assert (det2.Total == 0).all()
    assert list(acts[acts.Detector == 1].sort_values("TimeStamp").Total) == [1, 0]

"""
Timeline intervals that straddle a gap in a device's data must be marked invalid, whether the data is
processed in one pass or incrementally, and wherever the gap falls relative to the incremental windows.
Intervals over continuous data must stay valid, however long they are and however many windows they cross.

Device 1 is the device under test. Device 2 reports continuously, so a gap on device 1 is that controller
dropping out rather than a whole-system outage (except where a test says otherwise).
"""

import pandas as pd
import pytest

from src.atspm import SignalDataProcessor

DAY = "2026-09-16"
WINDOW_HOURS = 3
EMPTY_UNMATCHED = pd.DataFrame(columns=["TimeStamp", "DeviceId", "EventId", "Parameter", "IsValid"])


def _ts(time):
    return pd.Timestamp(f"{DAY} {time}")


def _heartbeat(device, start, end, gaps=()):
    """Detector on/off every 30 seconds from start to end, leaving out anything inside the gaps."""
    times = pd.date_range(_ts(start), _ts(end), freq="30s", inclusive="left")
    for gap_start, gap_end in gaps:
        times = times[(times < _ts(gap_start)) | (times >= _ts(gap_end))]
    return [(t, device, 82 if i % 2 == 0 else 81, 5) for i, t in enumerate(times)]


def _build_raw_df(events):
    df = pd.DataFrame(events, columns=["TimeStamp", "DeviceId", "EventId", "Parameter"])
    df["TimeStamp"] = pd.to_datetime(df["TimeStamp"])
    df["DeviceId"] = df["DeviceId"].astype("int64")
    df["EventId"] = df["EventId"].astype("int16")
    df["Parameter"] = df["Parameter"].astype("int16")
    return df.sort_values("TimeStamp").reset_index(drop=True)


def _phase4_wait(call_time, green_time):
    """Phase 4 served once, then called at call_time and not served again until green_time."""
    call, green = _ts(call_time), _ts(green_time)
    return [
        (call - pd.Timedelta(seconds=94), 1, 1, 4),
        (call - pd.Timedelta(seconds=64), 1, 7, 4),
        (call - pd.Timedelta(seconds=63), 1, 44, 4),
        (call, 1, 43, 4),
        (green, 1, 1, 4),
        (green + pd.Timedelta(seconds=1), 1, 44, 4),
        (green + pd.Timedelta(seconds=30), 1, 7, 4),
        # Other intervals open across the same period: phase 2 green and overlap 1 green
        (call - pd.Timedelta(seconds=20), 1, 1, 2),
        (green + pd.Timedelta(seconds=40), 1, 7, 2),
        (call - pd.Timedelta(seconds=20), 1, 61, 1),
        (green + pd.Timedelta(seconds=40), 1, 63, 1),
        (green + pd.Timedelta(seconds=44), 1, 64, 1),
        (green + pd.Timedelta(seconds=46), 1, 65, 1),
    ]


def _dataset(call_time, green_time, gap=None, system_wide=False):
    gaps = [gap] if gap else []
    events = _heartbeat(1, "09:00:00", "18:00:00", gaps)
    events += _heartbeat(2, "09:00:00", "18:00:00", gaps if system_wide else ())
    # 316 = MAXTIME actual cycle length, used by phase_wait to judge skips
    events += [(_ts("09:00:01"), 1, 316, 120), (_ts("09:00:01"), 2, 316, 120)]
    events += _phase4_wait(call_time, green_time)
    return _build_raw_df(events)


def _process(raw, unmatched_df=None):
    kwargs = {}
    if unmatched_df is not None:
        kwargs["unmatched_event_settings"] = {"df_or_path": unmatched_df, "max_days_old": 14}
    processor = SignalDataProcessor(
        raw_data=raw,
        detector_config=pd.DataFrame(columns=["DeviceId", "Phase", "Parameter", "Function"]),
        bin_size=15,
        verbose=0,
        remove_incomplete=False,
        controller_type="maxtime",
        aggregations=[
            {"name": "has_data", "params": {"no_data_min": 15, "min_data_points": 1}},
            {"name": "timeline", "params": {"min_duration": 0, "cushion_time": 60}},
            {"name": "phase_wait", "params": {}},
        ],
        **kwargs,
    )
    with processor:
        processor.load()
        processor.aggregate()
        timeline = processor.conn.query("SELECT * FROM timeline").df()
        phase_wait = processor.conn.query("SELECT * FROM phase_wait").df()
        unmatched = None
        if unmatched_df is not None:
            unmatched = processor.conn.query("SELECT * FROM unmatched_events").df()
    return timeline, phase_wait, unmatched


def _run_single_pass(raw):
    timeline, phase_wait, _ = _process(raw)
    return timeline, phase_wait


def _run_incremental(raw):
    """Process in consecutive 3-hour windows, handing unmatched events from each window to the next.
    Windows with no raw data at all are skipped, as a scheduler would when there is nothing to process."""
    timelines, phase_waits = [], []
    unmatched = EMPTY_UNMATCHED
    start = raw["TimeStamp"].min().floor(f"{WINDOW_HOURS}h")
    while start <= raw["TimeStamp"].max():
        end = start + pd.Timedelta(hours=WINDOW_HOURS)
        chunk = raw[(raw["TimeStamp"] >= start) & (raw["TimeStamp"] < end)]
        if len(chunk):
            timeline, phase_wait, unmatched = _process(chunk, unmatched)
            timelines.append(timeline)
            phase_waits.append(phase_wait)
        start = end
    return pd.concat(timelines, ignore_index=True), pd.concat(phase_waits, ignore_index=True)


RUNNERS = {"single_pass": _run_single_pass, "incremental": _run_incremental}


def _lookup(timeline, event_class, start_time, event_value):
    row = timeline[
        (timeline["DeviceId"] == 1)
        & (timeline["EventClass"] == event_class)
        & (timeline["StartTime"] == start_time)
        & (timeline["EventValue"] == event_value)
    ]
    assert len(row) == 1, f"expected one {event_class} at {start_time}, got {len(row)}"
    return row.iloc[0]


def _straddling_intervals(timeline, call_time):
    call = _ts(call_time)
    return {
        "Phase Wait": _lookup(timeline, "Phase Wait", call, 4),
        "Phase Call": _lookup(timeline, "Phase Call", call, 4),
        "Green": _lookup(timeline, "Green", call - pd.Timedelta(seconds=20), 2),
        "Overlap Green": _lookup(timeline, "Overlap Green", call - pd.Timedelta(seconds=20), 1),
    }


def _total_skips(phase_wait):
    rows = phase_wait[(phase_wait["DeviceId"] == 1) & (phase_wait["Phase"] == 4)]
    return int(rows["TotalSkips"].sum())


# (call_time, green_time, gap) - incremental windows are 09-12, 12-15 and 15-18
GAP_CASES = {
    # 1. gap entirely inside one window
    "gap_inside_one_window": ("09:50:34", "11:05:46", ("09:55:00", "11:01:00")),
    # 2. the real case: gap covers the tail of one window and the head of the next
    "gap_across_window_boundary": ("11:11:34", "14:11:46", ("11:11:40", "14:11:40")),
    # 3. gap swallows the whole 12-15 window
    "gap_swallows_whole_window": ("11:40:34", "15:10:46", ("11:40:40", "15:10:40")),
}


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("case", GAP_CASES)
def test_interval_straddling_gap_is_invalid(case, runner):
    call_time, green_time, gap = GAP_CASES[case]
    timeline, phase_wait = RUNNERS[runner](_dataset(call_time, green_time, gap))

    for event_class, row in _straddling_intervals(timeline, call_time).items():
        assert row["EndTime"] > _ts(gap[1]), f"{event_class} should end after the gap"
        assert row["IsValid"] == False, f"{event_class} straddles a data gap but is marked valid"

    # End to end: the invalid wait must not be counted as a skipped phase
    assert _total_skips(phase_wait) == 0


@pytest.mark.parametrize("runner", RUNNERS)
def test_interval_straddling_system_wide_gap_is_invalid(runner):
    """Every device silent for whole windows, so incrementally those windows are never processed."""
    call_time, green_time, gap = "09:50:34", "16:40:46", ("09:50:40", "16:40:40")
    timeline, phase_wait = RUNNERS[runner](_dataset(call_time, green_time, gap, system_wide=True))

    for event_class, row in _straddling_intervals(timeline, call_time).items():
        assert row["IsValid"] == False, f"{event_class} straddles a data gap but is marked valid"
    assert _total_skips(phase_wait) == 0


# (call_time, green_time) with continuous data throughout
NO_GAP_CASES = {
    # 5. multi-hour intervals crossing two window boundaries
    "long_interval": ("09:30:34", "16:30:46"),
    # 6. short interval crossing one window boundary
    "crosses_window_boundary": ("11:59:34", "12:00:46"),
}


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("case", NO_GAP_CASES)
def test_interval_over_continuous_data_stays_valid(case, runner):
    call_time, green_time = NO_GAP_CASES[case]
    timeline, phase_wait = RUNNERS[runner](_dataset(call_time, green_time))

    for event_class, row in _straddling_intervals(timeline, call_time).items():
        assert row["IsValid"] == True, f"{event_class} has continuous data but is marked invalid"

    # The long wait is genuine, so it is a skip; the short one is not
    assert _total_skips(phase_wait) == (1 if case == "long_interval" else 0)


@pytest.mark.parametrize("case", {**GAP_CASES, **{k: v + (None,) for k, v in NO_GAP_CASES.items()}})
def test_incremental_matches_single_pass(case):
    call_time, green_time, gap = {**GAP_CASES, **{k: v + (None,) for k, v in NO_GAP_CASES.items()}}[case]
    raw = _dataset(call_time, green_time, gap)
    cols = ["DeviceId", "StartTime", "EndTime", "EventClass", "EventValue", "IsValid"]
    single = _run_single_pass(raw)[0][cols].sort_values(cols).reset_index(drop=True)
    incremental = _run_incremental(raw)[0][cols].sort_values(cols).reset_index(drop=True)
    pd.testing.assert_frame_equal(single, incremental, check_dtype=False)

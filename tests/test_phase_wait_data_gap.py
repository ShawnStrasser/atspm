"""
Phase waits (and the other timeline intervals open alongside them) that straddle a short data gap must not be
counted as phase skips or inflate Avg/MaxPhaseWait.

Reported 2026-10-07 on device c9a6132b: the controller logged nothing from 13:53:45 to 14:05:15 while other
signals kept reporting. With has_data at no_data_min=15, min_data_points=1 (the ATSPM_Report config) both the
13:45 and 14:00 bins have events, so the has_data gap check saw nothing. Phase Wait 4 ran 13:53:25.5 to
14:06:40.1 (794.6 s), stayed IsValid=True, and phase_wait reported MaxPhaseWait=794.6 and TotalSkips=1.

The gaps here are all shorter than a 15-minute bin, so only an event-level check can see them. Normal waits in
the same bins as the gap must stay valid: only the intervals that actually span the gap are invalid.

Device 1 is the device under test. Device 2 reports continuously, so a gap on device 1 is that controller
dropping out rather than a whole-system outage.
"""

import pandas as pd
import pytest

from src.atspm import SignalDataProcessor

DAY = "2026-10-07"
WINDOW_HOURS = 3
CYCLE = 120
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


def _normal_wait(call):
    """Phase 4 called and served 20 seconds later, well clear of the gap."""
    return [
        (call, 1, 43, 4),
        (call + pd.Timedelta(seconds=20), 1, 1, 4),
        (call + pd.Timedelta(seconds=21), 1, 44, 4),
        (call + pd.Timedelta(seconds=50), 1, 7, 4),
    ]


def _bridged_wait(call_time, green_time):
    """Phase 4 called at call_time and next served at green_time, as the timeline sees it when the
    controller is silent in between. Phase 2 green and overlap 1 green are open across the same period."""
    call, green = _ts(call_time), _ts(green_time)
    return [
        (call - pd.Timedelta(seconds=94), 1, 1, 4),
        (call - pd.Timedelta(seconds=64), 1, 7, 4),
        (call - pd.Timedelta(seconds=63), 1, 44, 4),
        (call, 1, 43, 4),
        (green, 1, 1, 4),
        (green + pd.Timedelta(seconds=1), 1, 44, 4),
        (green + pd.Timedelta(seconds=30), 1, 7, 4),
        (call - pd.Timedelta(seconds=20), 1, 1, 2),
        (green + pd.Timedelta(seconds=40), 1, 7, 2),
        (call - pd.Timedelta(seconds=20), 1, 61, 1),
        (green + pd.Timedelta(seconds=40), 1, 63, 1),
        (green + pd.Timedelta(seconds=44), 1, 64, 1),
        (green + pd.Timedelta(seconds=46), 1, 65, 1),
    ]


def _normal_calls(case):
    """One normal wait about six minutes before the bridged call and one about four minutes after its green,
    so each lands in a bin the gap touches (or the bin next to it)."""
    call_time, green_time, _ = case
    return (_ts(call_time) - pd.Timedelta(minutes=6), _ts(green_time) + pd.Timedelta(minutes=4))


def _dataset(case):
    call_time, green_time, gap = case
    events = _heartbeat(1, "09:00:00", "18:00:00", [gap])
    events += _heartbeat(2, "09:00:00", "18:00:00")
    # 316 = MAXTIME actual cycle length, used by phase_wait to judge skips
    events += [(_ts("09:00:01"), 1, 316, CYCLE), (_ts("09:00:01"), 2, 316, CYCLE)]
    events += _bridged_wait(call_time, green_time)
    for call in _normal_calls(case):
        events += _normal_wait(call)
    return _build_raw_df(events)


def _process(raw, unmatched_df=None, max_event_gap_seconds="default"):
    kwargs = {}
    timeline_params = {"maxtime": True, "min_duration": 0.1, "cushion_time": 60}
    if max_event_gap_seconds != "default":
        timeline_params["max_event_gap_seconds"] = max_event_gap_seconds
    if unmatched_df is not None:
        kwargs["unmatched_event_settings"] = {"df_or_path": unmatched_df, "max_days_old": 14}
    # Same aggregation settings ATSPM_Report runs with
    processor = SignalDataProcessor(
        raw_data=raw,
        detector_config=pd.DataFrame(columns=["DeviceId", "Phase", "Parameter", "Function"]),
        bin_size=15,
        verbose=0,
        remove_incomplete=False,
        controller_type="maxtime",
        aggregations=[
            {"name": "has_data", "params": {"no_data_min": 15, "min_data_points": 1}},
            {"name": "timeline", "params": timeline_params},
            {"name": "phase_wait", "params": {"preempt_recovery_seconds": 120, "assumed_cycle_length": 140,
                                              "skip_multiplier": 1.5, "tsp_skip_multiplier": 2.0}},
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
    """Process in consecutive 3-hour windows, handing unmatched events from each window to the next."""
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


def _bridging_intervals(timeline, call_time):
    call = _ts(call_time)
    return {
        "Phase Wait": _lookup(timeline, "Phase Wait", call, 4),
        "Phase Call": _lookup(timeline, "Phase Call", call, 4),
        "Green": _lookup(timeline, "Green", call - pd.Timedelta(seconds=20), 2),
        "Overlap Green": _lookup(timeline, "Overlap Green", call - pd.Timedelta(seconds=20), 1),
    }


def _phase4(phase_wait):
    return phase_wait[(phase_wait["DeviceId"] == 1) & (phase_wait["Phase"] == 4)]


# (call_time, green_time, gap) - incremental windows are 09-12, 12-15 and 15-18; bins are 15 minutes
GAP_CASES = {
    # 1. the reported incident: 11.5-minute gap across the 13:45/14:00 bin edge, inside one window
    "incident_across_bin_edge": ("13:53:25.5", "14:06:40.1", ("13:53:45", "14:05:15")),
    # 2. 10-minute gap entirely inside one 15-minute bin
    "inside_one_bin": ("10:30:50", "10:41:30", ("10:31:00", "10:41:00")),
    # 3. 9-minute gap across a bin edge that is also an incremental window boundary
    "across_window_boundary": ("14:54:50", "15:04:30", ("14:55:00", "15:04:00")),
}


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("case", GAP_CASES)
def test_interval_straddling_short_gap_is_invalid(case, runner):
    call_time, green_time, gap = GAP_CASES[case]
    timeline, _ = RUNNERS[runner](_dataset(GAP_CASES[case]))

    for event_class, row in _bridging_intervals(timeline, call_time).items():
        assert row["EndTime"] > _ts(gap[1]), f"{event_class} should end after the gap"
        assert row["IsValid"] == False, f"{event_class} straddles a data gap but is marked valid"


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("case", GAP_CASES)
def test_short_gap_is_not_a_skip(case, runner):
    _, green_time, _ = GAP_CASES[case]
    _, phase_wait = RUNNERS[runner](_dataset(GAP_CASES[case]))
    rows = _phase4(phase_wait)

    assert int(rows["TotalSkips"].sum()) == 0, "a wait across a data gap was counted as a skip"
    green_bin = _ts(green_time).floor("15min")
    bin_row = rows[rows["TimeStamp"] == green_bin]
    assert len(bin_row) == 0 or bin_row["MaxPhaseWait"].max() < 1.5 * CYCLE, (
        "MaxPhaseWait was inflated by a wait across a data gap"
    )


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("case", GAP_CASES)
def test_normal_waits_near_gap_stay_valid(case, runner):
    """Only intervals that span the gap are invalid, not everything in the bins it touches."""
    timeline, phase_wait = RUNNERS[runner](_dataset(GAP_CASES[case]))

    for call in _normal_calls(GAP_CASES[case]):
        row = _lookup(timeline, "Phase Wait", call, 4)
        assert row["IsValid"] == True, f"normal wait at {call} does not span the gap but is marked invalid"
        wait_bin = (call + pd.Timedelta(seconds=20)).floor("15min")
        assert (_phase4(phase_wait)["TimeStamp"] == wait_bin).any(), f"phase_wait lost the {wait_bin} bin"


@pytest.mark.parametrize("runner", RUNNERS)
def test_brief_silence_stays_valid(runner):
    """A quiet spell of about a minute is normal operation, not a data gap."""
    case = ("10:30:50", "10:32:10", ("10:31:00", "10:32:00"))
    timeline, phase_wait = RUNNERS[runner](_dataset(case))

    for event_class, row in _bridging_intervals(timeline, case[0]).items():
        assert row["IsValid"] == True, f"{event_class} spans a brief silence but is marked invalid"
    assert int(_phase4(phase_wait)["TotalSkips"].sum()) == 0


@pytest.mark.parametrize("case", GAP_CASES)
def test_incremental_matches_single_pass(case):
    raw = _dataset(GAP_CASES[case])
    cols = ["DeviceId", "StartTime", "EndTime", "EventClass", "EventValue", "IsValid"]
    single = _run_single_pass(raw)[0][cols].sort_values(cols).reset_index(drop=True)
    incremental = _run_incremental(raw)[0][cols].sort_values(cols).reset_index(drop=True)
    pd.testing.assert_frame_equal(single, incremental, check_dtype=False)


# ---------------------------------------------------------------------------------------------------------
# Time of day thresholds. Overnight a controller resting in green logs nothing for minutes, so the default
# allows 120 s from 06:00, 300 s from 21:00, 900 s from 23:00 and 300 s from 05:00.
# ---------------------------------------------------------------------------------------------------------

def _silence_dataset(silence_start, seconds):
    """Device 1 logs every 10 s for 30 minutes either side of a silence of exactly `seconds`, which starts at
    its last event at silence_start. Phase 2 green runs across the silence. Device 2 reports throughout."""
    start = _ts(silence_start)
    end = start + pd.Timedelta(seconds=seconds)
    before = pd.date_range(start - pd.Timedelta(minutes=30), start, freq="10s")
    after = pd.date_range(end, end + pd.Timedelta(minutes=30), freq="10s")
    events = [(t, 1, 82, 5) for t in before.append(after)]
    events += [(t, 2, 82, 5) for t in pd.date_range(before[0], after[-1], freq="10s")]
    events += [(start - pd.Timedelta(seconds=15), 1, 1, 2), (end + pd.Timedelta(seconds=15), 1, 7, 2)]
    return _build_raw_df(events)


def _run_split(raw, cuts, max_event_gap_seconds="default"):
    """Process in runs split at the given times, handing unmatched events from each run to the next.
    A run with no data at all is skipped, as a scheduler would when there is nothing to process."""
    edges = [raw["TimeStamp"].min()] + [_ts(c) if isinstance(c, str) else c for c in cuts] + [raw["TimeStamp"].max() + pd.Timedelta(seconds=1)]
    timelines, phase_waits = [], []
    unmatched = EMPTY_UNMATCHED
    for lo, hi in zip(edges, edges[1:]):
        chunk = raw[(raw["TimeStamp"] >= lo) & (raw["TimeStamp"] < hi)]
        if len(chunk):
            timeline, phase_wait, unmatched = _process(chunk, unmatched, max_event_gap_seconds)
            timelines.append(timeline)
            phase_waits.append(phase_wait)
    return pd.concat(timelines, ignore_index=True), pd.concat(phase_waits, ignore_index=True)


# (silence start, seconds, expected IsValid of the green across it)
TIME_OF_DAY_CASES = {
    "day_119": ("10:00:00", 119, True),
    "day_121": ("10:00:00", 121, False),
    "day_exactly_120": ("10:00:00", 120, True),
    "evening_shoulder_290": ("21:30:00", 290, True),
    "evening_shoulder_310": ("21:30:00", 310, False),
    "night_890": ("02:00:00", 890, True),
    "night_910": ("02:00:00", 910, False),
    "morning_shoulder_290": ("05:30:00", 290, True),
    "morning_shoulder_310": ("05:30:00", 310, False),
    # The threshold is the one in force when the silence starts, not when it ends
    "starts_night_ends_morning": ("04:55:00", 600, True),
    "starts_day_ends_evening": ("20:58:00", 200, False),
}


@pytest.mark.parametrize("case", TIME_OF_DAY_CASES)
def test_time_of_day_threshold(case):
    silence_start, seconds, expected = TIME_OF_DAY_CASES[case]
    raw = _silence_dataset(silence_start, seconds)
    green_start = _ts(silence_start) - pd.Timedelta(seconds=15)
    split_inside = _ts(silence_start) + pd.Timedelta(seconds=seconds / 2)

    for timeline in (_run_single_pass(raw)[0], _run_split(raw, [split_inside])[0]):
        assert _lookup(timeline, "Green", green_start, 2)["IsValid"] == expected


def test_interval_touching_silence_edges_stays_valid():
    """Intervals that end at the last event before a silence, or start at the first event after it, do not
    span it."""
    start, end = _ts("10:00:00"), _ts("10:10:00")
    raw = _build_raw_df(
        [(t, 1, 82, 5) for t in pd.date_range(start - pd.Timedelta(minutes=5), start, freq="10s")]
        + [(t, 1, 82, 5) for t in pd.date_range(end, end + pd.Timedelta(minutes=5), freq="10s")]
        + [(start - pd.Timedelta(seconds=30), 1, 1, 2), (start, 1, 7, 2),
           (end, 1, 1, 4), (end + pd.Timedelta(seconds=30), 1, 7, 4)]
    )
    timeline = _run_single_pass(raw)[0]
    assert _lookup(timeline, "Green", start - pd.Timedelta(seconds=30), 2)["IsValid"] == True
    assert _lookup(timeline, "Green", end, 4)["IsValid"] == True


def test_device_silent_for_a_whole_run():
    """A short run in which device 1 sends nothing must not lose where its silence began."""
    case = ("11:57:50", "12:08:30", ("11:58:00", "12:08:00"))
    raw = _dataset(case)
    cols = ["DeviceId", "StartTime", "EndTime", "EventClass", "EventValue", "IsValid"]
    single = _run_single_pass(raw)[0][cols].sort_values(cols).reset_index(drop=True)
    split = _run_split(raw, ["12:00:00", "12:05:00"])[0]
    assert _lookup(split, "Phase Wait", _ts("11:57:50"), 4)["IsValid"] == False
    pd.testing.assert_frame_equal(single, split[cols].sort_values(cols).reset_index(drop=True), check_dtype=False)


def test_silence_inside_one_run_invalidates_interval_ending_in_next_run():
    """The whole silence is in the first run, but the green across it only ends in the second run. The
    second run sees no silence, so the first run must mark the still-open green start invalid."""
    raw = _silence_dataset("10:00:00", 300)
    green_start = _ts("10:00:00") - pd.Timedelta(seconds=15)
    split = _run_split(raw, [_ts("10:05:05")])[0]
    assert _lookup(_run_single_pass(raw)[0], "Green", green_start, 2)["IsValid"] == False
    assert _lookup(split, "Green", green_start, 2)["IsValid"] == False


@pytest.mark.parametrize("case", GAP_CASES)
def test_split_inside_gap_matches_single_pass(case):
    call_time, green_time, gap = GAP_CASES[case]
    raw = _dataset(GAP_CASES[case])
    cols = ["DeviceId", "StartTime", "EndTime", "EventClass", "EventValue", "IsValid"]
    single = _run_single_pass(raw)[0][cols].sort_values(cols).reset_index(drop=True)
    mid = _ts(gap[0]) + (_ts(gap[1]) - _ts(gap[0])) / 2
    split = _run_split(raw, [mid])[0][cols].sort_values(cols).reset_index(drop=True)
    pd.testing.assert_frame_equal(single, split, check_dtype=False)


def test_event_gap_check_can_be_disabled_or_flat():
    raw = _dataset(GAP_CASES["incident_across_bin_edge"])
    call = GAP_CASES["incident_across_bin_edge"][0]
    disabled = _process(raw, max_event_gap_seconds=None)[0]
    assert _lookup(disabled, "Phase Wait", _ts(call), 4)["IsValid"] == True
    # One number applies all day; the 11.5-minute incident is under 1000 s
    flat = _process(raw, max_event_gap_seconds=1000)[0]
    assert _lookup(flat, "Phase Wait", _ts(call), 4)["IsValid"] == True
    custom = _process(raw, max_event_gap_seconds={"00:00": 1000, "13:00": 600})[0]
    assert _lookup(custom, "Phase Wait", _ts(call), 4)["IsValid"] == False


@pytest.mark.parametrize("value", ["120", {"25:00": 120}, {"06:00": 0}, {"6am": 120}, {}, True])
def test_invalid_max_event_gap_seconds(value):
    raw = _dataset(GAP_CASES["inside_one_bin"])
    with pytest.raises(ValueError):
        _process(raw, max_event_gap_seconds=value)

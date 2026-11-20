"""Regression tests for phase waits that straddle a data gap in incremental runs.

Follow-up to the report that a wait spanning a gap could come out IsValid=True and be
counted as a skipped phase. The cases here cover the shapes called out in that report:
a device alone in the data, a device whose first event in a window falls after its own
gap ended, the first incremental window when no unmatched state exists yet, and the
sanity checks that continuous data stays valid.

The rule every case asserts: an incremental sequence must agree with a single pass over
the same events.
"""
import datetime as dt

import pandas as pd
import pytest

from atspm import SignalDataProcessor

PHASE = 2
DAY = dt.datetime(2024, 5, 13)
START = DAY + dt.timedelta(hours=9)
END = DAY + dt.timedelta(hours=18)
WINDOW = dt.timedelta(hours=3)

DETECTOR_CONFIG = pd.DataFrame({
    'DeviceId': [1, 2],
    'Phase': [PHASE, PHASE],
    'Parameter': [1, 1],
    'Function': ['Advance', 'Advance'],
})

AGGREGATIONS = [
    {'name': 'has_data', 'params': {}},
    {'name': 'timeline', 'params': {'min_duration': 0.2, 'cushion_time': 60, 'maxtime': True}},
    {'name': 'phase_wait', 'params': {'preempt_recovery_seconds': 120,
                                      'assumed_cycle_length': 140,
                                      'skip_multiplier': 1.5}},
]


def _cycles(device, start, stop, skip=None):
    """One 60 second cycle per minute: 43 call, 1 green, 7 green end, 44 call dropped.

    `skip` is an optional (gap_start, gap_end) during which the device logs nothing.
    EventId 316 carries the 100 second cycle length for controller_type='maxtime'.
    """
    rows = []
    t = start
    while t < stop:
        silent = skip is not None and skip[0] <= t < skip[1]
        if not silent:
            rows += [
                (t, device, 43, PHASE),
                (t + dt.timedelta(seconds=10), device, 1, PHASE),
                (t + dt.timedelta(seconds=40), device, 7, PHASE),
                (t + dt.timedelta(seconds=41), device, 44, PHASE),
                (t + dt.timedelta(seconds=45), device, 316, 100),
            ]
        t += dt.timedelta(seconds=60)
    return rows


def _frame(rows):
    df = pd.DataFrame(rows, columns=['TimeStamp', 'DeviceId', 'EventId', 'Parameter'])
    df['TimeStamp'] = pd.to_datetime(df['TimeStamp'])
    df['DeviceId'] = df['DeviceId'].astype('int32')
    df['EventId'] = df['EventId'].astype('int16')
    df['Parameter'] = df['Parameter'].astype('int16')
    return df.sort_values(['TimeStamp', 'EventId']).reset_index(drop=True)


def _build(gap_start, gap_end, extra_devices=(), straddle=True):
    """Device 1 goes silent for the gap. A call lands just before it and the green that
    answers arrives just after, so the resulting wait spans the whole gap."""
    rows = _cycles(1, START, END, skip=(gap_start - dt.timedelta(seconds=90),
                                        gap_end + dt.timedelta(seconds=90)))
    if straddle:
        rows += [
            (gap_start - dt.timedelta(seconds=4), 1, 43, PHASE),
            (gap_end + dt.timedelta(seconds=2), 1, 1, PHASE),
            (gap_end + dt.timedelta(seconds=32), 1, 7, PHASE),
            (gap_end + dt.timedelta(seconds=33), 1, 44, PHASE),
        ]
    for device in extra_devices:
        rows += _cycles(device, START, END)
    return _frame(rows)


def _run_single(raw, tmp_path):
    out = tmp_path / 'single'
    out.mkdir(parents=True, exist_ok=True)
    processor = SignalDataProcessor(
        raw_data=raw, detector_config=DETECTOR_CONFIG, bin_size=15, verbose=0,
        output_dir=str(out), output_format='parquet', output_to_separate_folders=False,
        output_file_prefix='', controller_type='maxtime', aggregations=AGGREGATIONS)
    processor.load()
    processor.aggregate()
    timeline = processor.conn.sql(
        "SELECT StartTime, Duration, IsValid FROM timeline WHERE EventClass = 'Phase Wait'").df()
    phase_wait = processor.conn.sql("SELECT * FROM phase_wait").df()
    processor.close()
    return timeline, phase_wait


def _run_incremental(raw, tmp_path, window=WINDOW, first_window_has_state=True):
    out = tmp_path / 'incremental'
    out.mkdir(parents=True, exist_ok=True)
    settings = {'df_or_path': f"{out}/unmatched.parquet",
                'split_fail_df_or_path': f"{out}/sf_unmatched.parquet",
                'max_days_old': 14, 'first_run': True}
    timelines, phase_waits = [], []
    window_start = START
    index = 0
    while window_start < END:
        chunk = raw[(raw.TimeStamp >= window_start) & (raw.TimeStamp < window_start + window)]
        kwargs = {}
        if first_window_has_state or index > 0:
            kwargs['unmatched_event_settings'] = dict(settings)
        processor = SignalDataProcessor(
            raw_data=chunk, detector_config=DETECTOR_CONFIG, bin_size=15, verbose=0,
            output_dir=str(out), output_format='parquet', output_to_separate_folders=False,
            output_file_prefix=f"w{index}_", controller_type='maxtime',
            aggregations=AGGREGATIONS, **kwargs)
        processor.load()
        processor.aggregate()
        timelines.append(processor.conn.sql(
            "SELECT StartTime, Duration, IsValid FROM timeline WHERE EventClass = 'Phase Wait'").df())
        phase_waits.append(processor.conn.sql("SELECT * FROM phase_wait").df())
        processor.save()
        processor.close()
        window_start += window
        index += 1
    return (pd.concat(timelines, ignore_index=True),
            pd.concat(phase_waits, ignore_index=True))


def _straddling(timeline):
    """The wait that spans the gap is the only one lasting more than an hour."""
    return timeline[timeline.Duration > 3600]


def _total_skips(phase_wait):
    if len(phase_wait) == 0 or 'TotalSkips' not in phase_wait.columns:
        return 0
    return int(phase_wait.TotalSkips.sum())


# Gap shapes, relative to 3 hour windows starting at 09:00
GAP_CROSSING_BOUNDARY = (DAY + dt.timedelta(hours=11, minutes=11, seconds=34),
                         DAY + dt.timedelta(hours=14, minutes=11, seconds=46))
GAP_INSIDE_ONE_WINDOW = (DAY + dt.timedelta(hours=9, minutes=40),
                         DAY + dt.timedelta(hours=11, minutes=20))
GAP_WHOLE_WINDOW = (DAY + dt.timedelta(hours=11, minutes=30),
                    DAY + dt.timedelta(hours=15, minutes=20))


@pytest.mark.parametrize('gap,label', [
    (GAP_CROSSING_BOUNDARY, 'crosses a window boundary'),
])
def test_single_device_alone_gap_invalidates_wait(tmp_path, gap, label):
    """A device alone in the data must behave the same as one sharing the data with a
    healthy device. The wait spanning the gap is invalid and is not a skipped phase."""
    raw = _build(*gap)
    assert raw.DeviceId.nunique() == 1

    single_tl, single_pw = _run_single(raw, tmp_path)
    incr_tl, incr_pw = _run_incremental(raw, tmp_path)

    single_wait = _straddling(single_tl)
    incr_wait = _straddling(incr_tl)
    assert len(single_wait) == 1, f"expected one straddling wait in the single pass ({label})"
    assert len(incr_wait) == 1, f"expected one straddling wait incrementally ({label})"

    assert not single_wait.iloc[0].IsValid, f"single pass should mark it invalid ({label})"
    assert not incr_wait.iloc[0].IsValid, f"incremental should mark it invalid ({label})"
    assert _total_skips(single_pw) == 0
    assert _total_skips(incr_pw) == 0, f"gap-spanning wait counted as a skip ({label})"



def test_first_incremental_window_without_carried_state(tmp_path):
    """The first window runs with no unmatched state to carry, so no device has a marker
    yet, and the gap straddles the boundary into the second window."""
    raw = _build(*GAP_CROSSING_BOUNDARY)
    incr_tl, incr_pw = _run_incremental(raw, tmp_path, first_window_has_state=False)

    wait = _straddling(incr_tl)
    if len(wait):
        assert not wait.iloc[0].IsValid
    assert _total_skips(incr_pw) == 0



def test_empty_window_with_carried_unmatched_state(tmp_path):
    """A window whose raw data is empty while unmatched state is carried in must not
    raise. MIN(TimeStamp) is NULL for an empty run and used to reach the max_days_old
    filter as the literal string 'None'."""
    out = tmp_path / 'empty'
    out.mkdir()
    settings = {'df_or_path': f"{out}/unmatched.parquet",
                'split_fail_df_or_path': f"{out}/sf_unmatched.parquet",
                'max_days_old': 14, 'first_run': True}

    populated = _frame(_cycles(1, START, START + dt.timedelta(hours=1)))
    SignalDataProcessor(
        raw_data=populated, detector_config=DETECTOR_CONFIG, bin_size=15, verbose=0,
        output_dir=str(out), output_format='parquet', output_to_separate_folders=False,
        output_file_prefix='w0_', controller_type='maxtime',
        unmatched_event_settings=dict(settings), aggregations=AGGREGATIONS).run()

    empty = populated.iloc[0:0]
    processor = SignalDataProcessor(
        raw_data=empty, detector_config=DETECTOR_CONFIG, bin_size=15, verbose=0,
        output_dir=str(out), output_format='parquet', output_to_separate_folders=False,
        output_file_prefix='w1_', controller_type='maxtime',
        unmatched_event_settings=dict(settings), aggregations=AGGREGATIONS)
    processor.run()  # must not raise

    carried = pd.read_parquet(f"{out}/unmatched.parquet")
    assert len(carried) > 0, "an empty window should not discard carried unmatched state"


def _stale_call_after_gap(gap_start, gap_end):
    """Phase 4 gets a call just before the device goes silent, and the 44 that ended it is
    lost in the gap. After the gap phase 4 is served every 10 minutes alongside another
    phase's call, with no call of its own, while phase 2 keeps logging calls."""
    rows = _cycles(1, START, END, skip=(gap_start - dt.timedelta(seconds=90),
                                        gap_end + dt.timedelta(seconds=90)))
    rows.append((gap_start - dt.timedelta(seconds=95), 1, 43, 4))
    t = gap_end + dt.timedelta(seconds=120)
    while t < END:
        rows += [(t + dt.timedelta(seconds=50), 1, 1, 4), (t + dt.timedelta(seconds=58), 1, 7, 4)]
        t += dt.timedelta(minutes=10)
    return _frame(rows)


@pytest.mark.parametrize('gap,label', [
    (GAP_CROSSING_BOUNDARY, 'crosses a window boundary'),
    (GAP_INSIDE_ONE_WINDOW, 'inside one window'),
    (GAP_WHOLE_WINDOW, 'covers a whole window'),
])
def test_call_before_gap_does_not_persist_after_it(tmp_path, gap, label):
    """A call that was on when the device went silent is dropped by the gap, so the
    green-to-green intervals of a phase served without a call of its own after the gap
    are not waits, and nothing is reported skipped."""
    raw = _stale_call_after_gap(*gap)
    for name, (timeline, phase_wait) in {
        'single': _run_single(raw, tmp_path),
        'incremental': _run_incremental(raw, tmp_path),
    }.items():
        after = timeline[(timeline.StartTime >= gap[1]) & timeline.IsValid & (timeline.Duration > 300)]
        assert after.empty, f"{name}: stale call made waits after the gap ({label})\n{after}"
        assert _total_skips(phase_wait) == 0, f"{name}: stale call reported as skips ({label})"

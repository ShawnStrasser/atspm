"""Samples in phase_wait: the waits averaged in a bin, so AvgPhaseWait rolls up exactly as SUM(AvgPhaseWait * Samples) / SUM(Samples)."""
import pandas as pd
import pytest

from src.atspm import SignalDataProcessor

T0 = pd.Timestamp('2026-10-27 15:00:00')
WAIT = {'name': 'phase_wait', 'params': {'preempt_recovery_seconds': 120, 'assumed_cycle_length': 140, 'skip_multiplier': 1.5}}
HAS_DATA = {'name': 'has_data', 'params': {'no_data_min': 15, 'min_data_points': 1}}
TIMELINE = {'name': 'timeline', 'params': {'min_duration': 0.0, 'cushion_time': 60, 'max_event_gap_seconds': None}}


def wait(phase, call, green):
    """A call at second call, its green at second green. The wait is green - call seconds, in the bin of green."""
    return [(call, 43, phase), (green, 1, phase), (green + 5, 7, phase), (green + 5.5, 44, phase)]


def events(*groups):
    rows = [r for g in groups for r in g]
    return pd.DataFrame([{'TimeStamp': T0 + pd.Timedelta(seconds=s), 'DeviceId': 'A', 'EventId': e, 'Parameter': p} for s, e, p in rows]).sort_values('TimeStamp')


def run(raw, carry=None):
    params = dict(raw_data=raw, bin_size=15, verbose=0, aggregations=[HAS_DATA, TIMELINE, WAIT])
    if carry is not None:
        params['unmatched_event_settings'] = {'df_or_path': carry, 'max_days_old': 14}
    with SignalDataProcessor(**params) as p:
        p.load()
        p.aggregate()
        out = p.conn.execute('SELECT * FROM phase_wait').df().sort_values(['TimeStamp', 'Phase']).reset_index(drop=True)
        kept = p.conn.execute('SELECT * FROM unmatched_events').df() if carry is not None else None
        kinds = dict(p.conn.execute('DESCRIBE phase_wait').fetchall()[i][:2] for i in range(len(out.columns)))
    return out, kept, kinds


def test_samples_is_the_waits_in_the_bin():
    # Phase 2: 10, 20 and 60 s in the first bin, 30 s in the second. Phase 4: 40 s in the first.
    raw = events(wait(2, 100, 110), wait(2, 200, 220), wait(2, 300, 360), wait(2, 1000, 1030), wait(4, 400, 440))
    out, _, kinds = run(raw)
    assert out[['Phase', 'Samples']].values.tolist() == [[2, 3], [4, 1], [2, 1]]
    assert kinds['Samples'] == 'INTEGER'
    assert out.AvgPhaseWait.tolist() == pytest.approx([30, 40, 30])


def test_the_average_rolls_up_exactly():
    durations = [10, 20, 60, 80]  # 3 waits in the first bin, 1 in the second
    calls = [100, 200, 300, 1000]
    raw = events(*[wait(2, c, c + d) for c, d in zip(calls, durations)])
    out, _, _ = run(raw)
    assert len(out) == 2
    rolled = (out.AvgPhaseWait * out.Samples).sum() / out.Samples.sum()
    assert rolled == pytest.approx(42.5)
    assert out.AvgPhaseWait.mean() == pytest.approx(55)  # what a plain average of the bins would read


def test_a_wait_left_out_for_a_preempt_is_not_a_sample():
    preempt = [(500, 102, 1), (520, 104, 1)]
    raw = events(wait(2, 100, 110), wait(2, 490, 530), preempt)
    out, _, _ = run(raw)
    assert out.Samples.tolist() == [1]
    assert out.AvgPhaseWait.tolist() == pytest.approx([10])


def test_a_run_cut_inside_a_bin_raises():
    """phase_wait writes the waits that ended in a run, so a run cut inside a bin would give that bin two rows."""
    groups = [wait(2, 100, 110), wait(2, 200, 220), wait(2, 400, 460), wait(2, 500, 505)]
    _, kept, _ = run(events(*groups[:2]), carry='')
    with pytest.raises(ValueError, match='cut on the bin'):
        run(events(*groups[2:]), carry=kept)

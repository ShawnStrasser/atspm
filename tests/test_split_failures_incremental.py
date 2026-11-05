"""split_failures cut into runs gives the same answer as one big run, on events written by hand."""
import pandas as pd
import pytest

from src.atspm import SignalDataProcessor

T0 = pd.Timestamp('2026-10-27 15:00:00')
CONFIG = pd.DataFrame({'DeviceId': ['A', 'A'], 'Phase': [2, 2], 'Parameter': [5, 6], 'Function': ['Presence', 'Presence']})


def sf(by_cycle=True):
    return [{'name': 'split_failures', 'params': {'red_time': 5, 'red_occupancy_threshold': 0.8, 'green_occupancy_threshold': 0.8, 'by_approach': True, 'by_cycle': by_cycle}}]


def events(rows):
    return pd.DataFrame([{'TimeStamp': T0 + pd.Timedelta(seconds=s), 'DeviceId': 'A', 'EventId': e, 'Parameter': p} for s, e, p in rows])


def process(raw, carry=None, keep=False, by_cycle=True):
    params = dict(raw_data=raw, detector_config=CONFIG, bin_size=15, remove_incomplete=False, verbose=0, aggregations=sf(by_cycle))
    if keep:
        params['unmatched_event_settings'] = {'split_fail_df_or_path': carry if carry is not None else '', 'max_days_old': 14}
    p = SignalDataProcessor(**params)
    p.load()
    p.aggregate()
    out = p.conn.sql('SELECT * FROM split_failures').df()
    saved = p.conn.sql('SELECT * FROM sf_unmatched').df() if keep else None
    p.conn.close()
    return out, saved


def in_runs(rows, cuts, by_cycle=True):
    """Runs cut at these seconds, the last one past all the events."""
    raw = events(rows)
    parts, carry, last = [], None, T0 - pd.Timedelta(days=1)
    for cut in cuts:
        end = T0 + pd.Timedelta(seconds=cut)
        part, carry = process(raw[(raw.TimeStamp >= last) & (raw.TimeStamp < end)], carry, True, by_cycle)
        parts.append(part)
        last = end
    return pd.concat(parts).sort_values('TimeStamp').reset_index(drop=True)


def one_run(rows, by_cycle=True):
    return process(events(rows), by_cycle=by_cycle)[0].sort_values('TimeStamp').reset_index(drop=True)


def same(rows, cuts, by_cycle=True):
    one, runs = one_run(rows, by_cycle), in_runs(rows, cuts, by_cycle)
    pd.testing.assert_frame_equal(one, runs, check_dtype=False)
    return one


# Green at each 100 s, yellow 30 s later, red clearance 4 s after that, so each cycle ends at 39 s past its 100 s. And a
# last event in the final second of each 15-minute bin, so a run cut at the end of a bin has reached it.
CYCLES = [(s, e, 2) for start in range(100, 1800, 100) for s, e in ((start, 1), (start + 30, 8), (start + 34, 10))]
REACHED = [(899.9, 200, 1), (1799.9, 200, 1)]
ENDS = [900, 1801]
LOST_EVENT_CYCLES = 3


def at(frame, seconds):
    return frame[frame.TimeStamp == T0 + pd.Timedelta(seconds=seconds)]


def test_second_lane_still_on_when_the_first_goes_off_across_a_cut():
    # Lane 5 is on over the cut at 900 s and lane 6 stays on after lane 5 goes off, so the phase's detection stays on. A
    # count that restarts each run took lane 5's off as the phase going off.
    rows = CYCLES + REACHED + [(895, 82, 5), (898, 82, 6), (905, 81, 5), (938, 81, 6)]
    one = same(rows, ENDS)
    assert at(one, 939).Green_Occupancy.iloc[0] == pytest.approx(1.0)


def test_failure_over_a_cut_counts_once():
    # The cycle from 900 s to 939 s has its green and all of its detection on both sides of a cut
    rows = CYCLES + REACHED + [(899, 82, 5), (940, 81, 5)]
    for cut in (900, 915, 930, 937):
        one = same(rows, [cut, 1801])
        assert at(one, 939).Split_Failure.iloc[0] == 1


def test_cut_inside_a_bin_writes_each_bin_once():
    rows = CYCLES + REACHED + [(310, 82, 5), (340, 81, 5), (880, 82, 6), (1000, 81, 6)]
    for by_cycle in (True, False):
        same(rows, [400, 915, 1250, 1801], by_cycle=by_cycle)


def test_missing_off_half_way_inside_the_bin():
    # Half-way, at 1000 s, is inside the second bin, so it stands
    rows = CYCLES + REACHED + [(950, 82, 5), (1050, 82, 5), (1055, 81, 5)]
    one = same(rows, [1020, 1801])
    assert at(one, 1039).Green_Occupancy.iloc[0] == 0


def test_file_saved_by_atspm_2_is_read():
    # Combined state as Detector 0 is left out: it would read as a lane that never goes off
    old = pd.DataFrame([{'TimeStamp': T0 + pd.Timedelta(seconds=850), 'DeviceId': 'A', 'EventId': 82, 'Detector': 0, 'Phase': 2}])
    raw = events([r for r in CYCLES if r[0] >= 900] + REACHED[1:] + [(950, 82, 5), (955, 81, 5)])
    out, _ = process(raw, old, True)
    assert at(out, 1039).Green_Occupancy.iloc[0] == 0


def test_real_events_cut_at_every_quarter_hour_and_inside_bins():
    import duckdb
    data = duckdb.sql("SELECT * FROM 'tests/hires_test_data.parquet'").df()
    config = duckdb.sql("SELECT * FROM 'tests/configs_test_data.parquet'").df()

    def run(raw, carry=None, keep=False):
        params = dict(raw_data=raw, detector_config=config, bin_size=15, remove_incomplete=False, verbose=0, aggregations=sf())
        if keep:
            params['unmatched_event_settings'] = {'split_fail_df_or_path': carry if carry is not None else '', 'max_days_old': 14}
        p = SignalDataProcessor(**params)
        p.load()
        p.aggregate()
        out = p.conn.sql('SELECT * FROM split_failures').df()
        saved = p.conn.sql('SELECT * FROM sf_unmatched').df() if keep else None
        p.conn.close()
        return out, saved

    start, keys = pd.Timestamp('2024-05-13 15:00:00'), ['TimeStamp', 'DeviceId', 'Phase']
    one = run(data)[0].sort_values(keys).reset_index(drop=True)
    assert len(one)
    for minutes in ([15 * i for i in range(1, 13)], [7, 22, 45, 53, 90, 97, 150, 180]):
        parts, carry, last = [], None, start
        for m in minutes:
            end = start + pd.Timedelta(minutes=m)
            part, carry = run(data[(data.TimeStamp >= last) & (data.TimeStamp < end)], carry, True)
            parts.append(part)
            last = end
        runs = pd.concat(parts).sort_values(keys).reset_index(drop=True)
        # A missing off and a lost green at a cut can't be known by the run before it, see the changelog
        lost = runs.merge(one, how='outer', on=keys, suffixes=('', '_one')).query('Green_Time != Green_Time_one or Green_Occupancy != Green_Occupancy_one or Red_Occupancy != Red_Occupancy_one')
        assert len(lost) <= LOST_EVENT_CYCLES, lost

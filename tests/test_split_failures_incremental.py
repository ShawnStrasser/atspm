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


def test_missing_off_stops_at_the_start_of_the_bin_of_the_next_on():
    # On at 700 s, off lost, on at 950 s. Half-way is 825 s, but a run cut at 900 s has written the first bin with the
    # detector on to its end. So it goes off at 900 s in one big run as well.
    rows = CYCLES + REACHED + [(700, 82, 5), (950, 82, 5), (955, 81, 5)]
    one = same(rows, ENDS)
    assert at(one, 839).Green_Occupancy.iloc[0] == pytest.approx(1.0)
    assert at(one, 939).Green_Occupancy.iloc[0] == 0


def test_missing_off_half_way_inside_the_bin():
    # Half-way, at 1000 s, is inside the second bin, so it stands
    rows = CYCLES + REACHED + [(950, 82, 5), (1050, 82, 5), (1055, 81, 5)]
    one = same(rows, [1020, 1801])
    assert at(one, 1039).Green_Occupancy.iloc[0] == 0


def test_lost_green_keeps_the_finished_cycle():
    # The green at 900 s was lost, so the yellow and red from 930 s follow the cycle from 800 s. That cycle finished at
    # 839 s, and a run stopping at 900 s could not have known.
    rows = [r for r in CYCLES if r[0] != 900] + REACHED + [(500, 82, 5), (505, 81, 5)]
    one = same(rows, ENDS)
    assert len(at(one, 839)) == 1
    assert len(at(one, 939)) == 0


def test_file_saved_by_atspm_2_is_read():
    # Combined state as Detector 0 is left out: it would read as a lane that never goes off
    old = pd.DataFrame([{'TimeStamp': T0 + pd.Timedelta(seconds=850), 'DeviceId': 'A', 'EventId': 82, 'Detector': 0, 'Phase': 2}])
    raw = events([r for r in CYCLES if r[0] >= 900] + REACHED[1:] + [(950, 82, 5), (955, 81, 5)])
    out, _ = process(raw, old, True)
    assert at(out, 1039).Green_Occupancy.iloc[0] == 0


# ---- Cycles: how many cycles a bin's averages and failures are made of

ON_AND_OFF = [(50, 82, 5), (60, 81, 5), (310, 82, 5), (345, 81, 5), (880, 82, 6), (1000, 81, 6), (1205, 82, 5), (1300, 81, 5)]


def test_cycles_is_the_cycles_finished_in_the_bin():
    # A cycle belongs to the bin of its red barrier, 39 s after its green: greens at 100 s to 800 s, then 900 s to 1700 s
    out = one_run(CYCLES + REACHED + ON_AND_OFF, by_cycle=False)
    assert out.Cycles.tolist() == [8, 9]
    assert list(out.columns)[-1] == 'Cycles' and out.Cycles.dtype == 'int16'


def test_a_cycle_left_out_is_not_counted():
    # No yellow in the cycle of 300 s, so it is thrown out of the averages too
    rows = [r for r in CYCLES if r != (330, 8, 2)] + REACHED + ON_AND_OFF
    out, cycles = one_run(rows, by_cycle=False), one_run(rows, by_cycle=True)
    assert out.Cycles.tolist() == [7, 9]
    assert out.Cycles.sum() == len(cycles)


def test_cycles_rolls_the_table_up_exactly():
    """The averages and the share of failures of a longer period, from the binned rows alone, equal the rows of each cycle."""
    rows = CYCLES + REACHED + ON_AND_OFF + [(1405, 82, 5), (1450, 81, 5), (1509, 82, 6), (1550, 81, 6)]
    out, cycles = one_run(rows, by_cycle=False), one_run(rows, by_cycle=True)
    for column in ('Green_Time', 'Green_Occupancy', 'Red_Occupancy'):
        assert (out[column].astype(float) * out.Cycles).sum() / out.Cycles.sum() == pytest.approx(cycles[column].astype(float).mean(), rel=1e-6)
    assert out.Split_Failure.sum() / out.Cycles.sum() == cycles.Split_Failure.mean()
    assert 0 < cycles.Green_Occupancy.mean() < 1


def test_a_row_per_cycle_has_no_cycles_column():
    assert 'Cycles' not in one_run(CYCLES + REACHED + ON_AND_OFF, by_cycle=True).columns


@pytest.mark.parametrize('cuts', [ENDS, [400, 915, 1250, 1801], [900, 915, 930, 937, 1801]])
def test_cycles_is_the_same_wherever_runs_are_cut(cuts):
    one = same(CYCLES + REACHED + ON_AND_OFF, cuts, by_cycle=False)  # every column, Cycles too
    assert one.Cycles.sum() == 17


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
        pd.testing.assert_frame_equal(one, runs, check_dtype=False)

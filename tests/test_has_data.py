"""has_data, from how it should work.

Score is the share of a bin's time not inside an unexpected gap with no events at all, above 0 and up to 1, and no row reads as 0. Only all
events together count. A gap is a run of minutes with no event, unexpected over 4 times the signal's recent longest gap. remove_incomplete keeps a bin at Score 0.5 or more.
"""
import pandas as pd
import pytest
from src.atspm import SignalDataProcessor

T0 = pd.Timestamp('2026-09-01 08:00:00')
HAS_DATA = {'name': 'has_data', 'params': {}}
CONFIG = pd.DataFrame({'DeviceId': 'A', 'Phase': [2, 2], 'Parameter': [3, 3], 'Function': ['Count', 'Advance']})
OWN_CODES = set(range(970, 1000))


def cycles(start_minute, end_minute, device='A'):
    """A signal logging one cycle a minute, with the last event half a second before the minute ends."""
    rows = []
    for minute in range(start_minute, end_minute):
        t = T0 + pd.Timedelta(minutes=minute)
        for seconds, event, parameter in ((0, 1, 2), (5, 82, 3), (6, 81, 3), (20, 7, 2), (25, 8, 2), (30, 9, 2), (33, 10, 2), (35, 11, 2),
                                          (40, 43, 2), (59.5, 82, 3)):
            rows.append((t + pd.Timedelta(seconds=seconds), device, event, parameter))
    return pd.DataFrame(rows, columns=['TimeStamp', 'DeviceId', 'EventId', 'Parameter'])


def without(events, start_minute, end_minute):
    a, b = T0 + pd.Timedelta(minutes=start_minute), T0 + pd.Timedelta(minutes=end_minute)
    return events[(events.TimeStamp < a) | (events.TimeStamp >= b)].reset_index(drop=True)


def process(events, aggregations=None, state=None, **kwargs):
    settings = None if state is None else {'df_or_path': state, 'max_days_old': 14, 'first_run': True}
    with SignalDataProcessor(raw_data=events, detector_config=CONFIG, bin_size=15, verbose=0, unmatched_event_settings=settings,
                             aggregations=aggregations or [HAS_DATA], **kwargs) as p:
        p.load()
        p.aggregate()
        tables = [row[0] for row in p.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        out = {t: p.conn.execute(f'SELECT * FROM "{t}"').df() for t in tables}
        if 'has_data' in tables:
            out['has_data_types'] = p.conn.execute("DESCRIBE has_data").fetchall()
        if state is not None and 'unmatched_events' in tables:
            p.conn.execute(f"COPY unmatched_events TO '{state}' (FORMAT PARQUET)")
        return out


def scores(out):
    df = out['has_data']
    return {(row.TimeStamp - T0) / pd.Timedelta(minutes=1): row.Score for row in df.itertuples()}


def cut_runs(events, cuts, tmp_path, aggregations=None, **kwargs):
    """One process per piece in all but name: only the state file carries over."""
    state = str(tmp_path / 'state.parquet')
    edges = [events.TimeStamp.min(), *cuts, events.TimeStamp.max() + pd.Timedelta(seconds=1)]
    outs = [process(events[(events.TimeStamp >= a) & (events.TimeStamp < b)].reset_index(drop=True), aggregations, state=state, **kwargs)
            for a, b in zip(edges, edges[1:])]
    return outs, state


def table_of(outs, name):
    return pd.concat([o[name] for o in outs if len(o[name])]).sort_values(by=list(outs[0][name].columns)).reset_index(drop=True)


def bins(*minutes):
    return [T0 + pd.Timedelta(minutes=m) for m in minutes]


def test_columns_and_a_clean_signal_reads_1():
    out = process(cycles(0, 60))
    assert out['has_data_types'][0][:2] == ('TimeStamp', 'TIMESTAMP')
    assert [c[0] for c in out['has_data_types']] == ['TimeStamp', 'DeviceId', 'Score']
    assert out['has_data_types'][2][1] == 'FLOAT'
    assert scores(out) == {0: 1, 15: 1, 30: 1, 45: 1}


def test_a_lost_bin_has_no_row_and_no_row_reads_0():
    out = process(without(cycles(0, 120), 60, 75))
    s = scores(out)
    assert 60 not in s
    assert sorted(s) == [0, 15, 30, 45, 75, 90, 105]
    assert out['has_data'].Score.gt(0).all() and out['has_data'].Score.le(1).all()


def test_part_of_a_bin_lost_lowers_its_score_by_the_share_lost():
    assert scores(process(without(cycles(0, 120), 60, 65)))[60] == pytest.approx(2 / 3, abs=0.03)
    assert scores(process(without(cycles(0, 120), 60, 70)))[60] == pytest.approx(1 / 3, abs=0.03)


def test_a_quiet_signal_with_no_history_reads_1():
    # Two events an hour apart: nothing says this one usually logs more
    events = pd.DataFrame([(T0, 'A', 82, 3), (T0 + pd.Timedelta(minutes=59, seconds=59.5), 'A', 82, 3)],
                          columns=['TimeStamp', 'DeviceId', 'EventId', 'Parameter'])
    assert scores(process(events)) == {0: 1, 15: 1, 30: 1, 45: 1}


def test_only_time_in_a_gap_with_no_events_lowers_the_score():
    # Detectors and calls gone, or a state skipped: other events keep coming, so nothing is lost
    events = cycles(0, 120)
    in_hour = events.TimeStamp >= T0 + pd.Timedelta(minutes=60)
    no_detectors = events[~(in_hour & events.EventId.isin([43, 44, 81, 82]))].reset_index(drop=True)
    assert set(scores(process(no_detectors)).values()) == {1}
    skip = (events.EventId == 8) & (events.TimeStamp == T0 + pd.Timedelta(minutes=20, seconds=25))
    assert set(scores(process(events[~skip].reset_index(drop=True))).values()) == {1}


def test_score_is_time_not_counts():
    # Half the events gone evenly leaves no gap; a block gone is a gap, scored by its time
    events = cycles(0, 120)
    thinned = events[~((events.TimeStamp >= T0 + pd.Timedelta(minutes=60)) & (events.TimeStamp < T0 + pd.Timedelta(minutes=75))
                       & (events.EventId.isin([82, 9, 10, 11])))]
    assert scores(process(thinned.reset_index(drop=True)))[60] == 1
    assert scores(process(without(events, 60, 67)))[60] == pytest.approx(8 / 15, abs=0.03)


def test_a_gap_in_the_signals_usual_pattern_reads_1():
    # One burst every 10 minutes all along: its 10 minute gaps are its pattern
    rows = [(T0 + pd.Timedelta(minutes=m, seconds=s), 'A', 1, 2) for m in range(0, 180, 10) for s in (0, 5, 10)]
    events = pd.DataFrame(rows, columns=['TimeStamp', 'DeviceId', 'EventId', 'Parameter'])
    assert set(scores(process(events)).values()) == {1}


def test_flash_memory_is_cleared_across_runs(tmp_path):
    events = without(cycles(0, 120), 60, 90)
    flash = pd.DataFrame([(T0 + pd.Timedelta(minutes=60), 'A', 173, 1), (T0 + pd.Timedelta(minutes=60, seconds=1), 'A', 43, 2)], columns=events.columns)
    events = pd.concat([events, flash]).sort_values('TimeStamp').reset_index(drop=True)
    outs, _ = cut_runs(events, [T0 + pd.Timedelta(minutes=c) for c in range(15, 120, 15)], tmp_path)
    s = {(r.TimeStamp - T0) / pd.Timedelta(minutes=1): r.Score for o in outs for r in o['has_data'].itertuples()}
    assert s[60] == 1 and s[75] == 1


def test_a_signal_in_flash_is_silent_without_loss():
    events = without(cycles(0, 120), 60, 90)
    flash = pd.DataFrame([(T0 + pd.Timedelta(minutes=60), 'A', 173, 1), (T0 + pd.Timedelta(minutes=60, seconds=1), 'A', 43, 2),
                          (T0 + pd.Timedelta(minutes=60, seconds=2), 'A', 82, 3)], columns=events.columns)
    s = scores(process(pd.concat([events, flash]).sort_values('TimeStamp').reset_index(drop=True)))
    assert s[60] == 1 and s[75] == 1


def test_vendor_events_do_not_count():
    events = without(cycles(0, 120), 60, 75)
    vendor = pd.DataFrame([(T0 + pd.Timedelta(minutes=60 + i), 'A', 300, 1) for i in range(15)], columns=events.columns)
    s = scores(process(pd.concat([events, vendor]).sort_values('TimeStamp').reset_index(drop=True)))
    assert 60 not in s


def test_remove_incomplete_keeps_a_bin_at_half_or_more():
    actuations = {'name': 'actuations', 'params': {}}
    kept = process(without(cycles(0, 120), 60, 65), [HAS_DATA, actuations], remove_incomplete=True)
    dropped = process(without(cycles(0, 120), 60, 70), [HAS_DATA, actuations], remove_incomplete=True)
    assert bins(60)[0] in set(kept['actuations'].TimeStamp)
    assert bins(60)[0] in set(dropped['has_data'].TimeStamp)
    assert bins(60)[0] not in set(dropped['actuations'].TimeStamp)


ALL_TABLES = [HAS_DATA, {'name': 'actuations', 'params': {}}, {'name': 'arrival_on_green', 'params': {}}, {'name': 'approach_delay', 'params': {}},
              {'name': 'left_turn_gaps', 'params': {}}, {'name': 'platoon_ratio', 'params': {}}, {'name': 'terminations', 'params': {}},
              {'name': 'splits', 'params': {}}, {'name': 'yellow_red', 'params': {'latency_offset_seconds': 0, 'min_red_offset': -8}},
              {'name': 'ped', 'params': {}},
              {'name': 'split_failures', 'params': {'red_time': 5, 'red_occupancy_threshold': 0.8, 'green_occupancy_threshold': 0.8,
                                                    'by_approach': True, 'by_cycle': False}},
              {'name': 'timeline', 'params': {'min_duration': 0.2, 'cushion_time': 60}}, {'name': 'coordination_agg', 'params': {}},
              {'name': 'ped_delay', 'params': {}}, {'name': 'phase_wait', 'params': {}}]


@pytest.mark.parametrize('state', [False, True], ids=['one_run', 'with_state'])
def test_score_reaches_no_other_table(state, tmp_path):
    tables = [agg for agg in ALL_TABLES if not (state and agg['name'] == 'split_failures')]  # it has its own file
    out = process(cycles(0, 60), tables, state=str(tmp_path / 's.parquet') if state else None, remove_incomplete=True)
    assert {'has_data', 'actuations', 'timeline', 'coordination_agg'} <= set(out)
    for name, table in out.items():
        if name not in ('has_data', 'has_data_types'):
            assert 'Score' not in table.columns, name


def test_no_state_tables_are_left_to_save():
    out = process(cycles(0, 60), [HAS_DATA], state=None)
    assert not [t for t in out if t.startswith('has_data_') and t != 'has_data_types']


@pytest.mark.parametrize('cuts', [[15], [15, 30, 45, 60, 75, 90, 105]], ids=['one', 'every_bin'])
def test_runs_cut_anywhere_equal_one_big_run(cuts, tmp_path):
    events = pd.concat([without(cycles(0, 120), 60, 65)])
    big_state = str(tmp_path / 'big.parquet')
    big = process(events, state=big_state)
    outs, state = cut_runs(events, [T0 + pd.Timedelta(minutes=c) for c in cuts], tmp_path)
    pd.testing.assert_frame_equal(table_of(outs, 'has_data'), big['has_data'].sort_values(by=list(big['has_data'].columns)).reset_index(drop=True))
    one = pd.read_parquet(big_state).sort_values(['EventId', 'TimeStamp', 'Parameter']).reset_index(drop=True)
    last = pd.read_parquet(state).sort_values(['EventId', 'TimeStamp', 'Parameter']).reset_index(drop=True)
    keep = [c for c in one.columns]
    finished = one.EventId.isin(OWN_CODES)
    assert finished.any()
    pd.testing.assert_frame_equal(one.loc[finished, keep].reset_index(drop=True), last.loc[last.EventId.isin(OWN_CODES), keep].reset_index(drop=True), check_dtype=False)


def test_each_bin_is_written_once_by_the_run_reaching_its_end(tmp_path):
    events = cycles(0, 60)
    outs, _ = cut_runs(events, [T0 + pd.Timedelta(minutes=20), T0 + pd.Timedelta(minutes=30)], tmp_path)
    assert [sorted(((o['has_data'].TimeStamp - T0) / pd.Timedelta(minutes=1)).tolist()) for o in outs] == [[0], [15], [30, 45]]


@pytest.mark.parametrize('tables', [[HAS_DATA, {'name': 'actuations', 'params': {}}],
                                    [HAS_DATA, {'name': 'arrival_on_green', 'params': {}}, {'name': 'left_turn_gaps', 'params': {}}]],
                         ids=['actuations', 'carried_tables'])
def test_tables_that_depend_on_it_equal_one_big_run_across_runs(tables, tmp_path):
    events = without(cycles(0, 120), 60, 70)
    big = process(events, tables, state=str(tmp_path / 'big.parquet'), remove_incomplete=True)
    outs, _ = cut_runs(events, [T0 + pd.Timedelta(minutes=c) for c in range(15, 120, 15)], tmp_path, tables, remove_incomplete=True)
    for agg in tables:
        name = agg['name']
        one = big[name].sort_values(by=list(big[name].columns)).reset_index(drop=True)
        pd.testing.assert_frame_equal(table_of(outs, name), one, check_dtype=False)


def test_a_run_with_no_events_changes_nothing(tmp_path):
    events = cycles(0, 30)
    state = str(tmp_path / 's.parquet')
    process(events, state=state)
    before = pd.read_parquet(state).sort_values(['EventId', 'TimeStamp', 'Parameter']).reset_index(drop=True)
    out = process(events.iloc[0:0], state=state)
    assert out['has_data'].empty
    after = pd.read_parquet(state).sort_values(['EventId', 'TimeStamp', 'Parameter']).reset_index(drop=True)
    pd.testing.assert_frame_equal(before, after, check_dtype=False)


def test_one_run_writes_all_it_has_and_does_not_read_its_end_as_a_stop():
    s = scores(process(cycles(0, 22)))
    assert s == {0: 1, 15: 1}


def test_with_a_state_file_the_bin_it_stopped_in_waits(tmp_path):
    assert sorted(scores(process(cycles(0, 22), state=str(tmp_path / 's.parquet')))) == [0]


def test_codes_stay_out_of_the_timeline_input(tmp_path):
    state = str(tmp_path / 's.parquet')
    timeline = {'name': 'timeline', 'params': {'min_duration': 0.2, 'cushion_time': 60}}
    process(cycles(0, 15), [HAS_DATA, timeline], state=state)
    assert OWN_CODES & set(pd.read_parquet(state).EventId)
    out = process(cycles(15, 30), [HAS_DATA, timeline], state=state)
    assert 'Score' not in out['timeline'].columns
    assert not out['timeline'].EventClass.isna().any()


def test_the_state_of_has_data_raises_if_it_is_not_listed(tmp_path):
    state = str(tmp_path / 's.parquet')
    process(cycles(0, 15), state=state)
    with pytest.raises(ValueError, match='has_data'):
        process(cycles(15, 30), [{'name': 'actuations', 'params': {}}], state=state)


@pytest.mark.parametrize('name', ['no_data_min', 'min_data_points'])
def test_old_settings_raise(name):
    with pytest.raises(ValueError, match=name):
        SignalDataProcessor(raw_data=cycles(0, 15), bin_size=15, verbose=0, aggregations=[{'name': 'has_data', 'params': {name: 5}}])


def test_to_sql_gives_only_the_three_columns():
    with SignalDataProcessor(to_sql=True, bin_size=15, verbose=0, aggregations=[HAS_DATA]) as p:
        sql = p.run()['has_data']
    assert "Kind = 'score'" in sql and 'CREATE TABLE has_data' in sql

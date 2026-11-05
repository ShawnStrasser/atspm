"""Estimated_Volumes in ped: a bin's estimate reads that bin and the 3 before it, which earlier runs carry, so it is
final when written. The rows must be the same however the data was cut into runs, including one big run."""
import numpy as np
import pandas as pd
import pytest
import duckdb
from src.atspm import SignalDataProcessor
from tests.test_ped_carry import incremental, process, reach_bin_end, REAL_EVENTS

VOLUMES = [{'name': 'ped', 'params': {}}]
KEYS = ['TimeStamp', 'DeviceId', 'Phase']
INTERCEPT = 1.1063 / 4


def at(hours, minutes=0, seconds=0.0):
    """A time on the test day."""
    return pd.Timestamp('2026-09-01') + pd.Timedelta(hours=hours, minutes=minutes, seconds=seconds)


def events_at(*times, device='A', parameter=2, event=90):
    return pd.DataFrame({'TimeStamp': list(times), 'DeviceId': device, 'EventId': event, 'Parameter': parameter})


def end_marker(time, device='Z'):
    """A vehicle event that says how far the data reaches, without touching any ped input."""
    return events_at(time, device=device, parameter=1, event=82)


def volume(actuations, rolling):
    """The published formula for one bin."""
    return (0.7167 * rolling + 0.0599 * rolling ** 2) * (actuations / rolling if rolling else 0) + INTERCEPT


def written(frames):
    """Rows of all runs. A bin written twice would show as a duplicate, so that is checked here."""
    frame = pd.concat(frames, ignore_index=True)
    assert not frame.duplicated(KEYS).any(), 'a bin was written twice'
    frame = frame.copy()
    frame['DeviceId'] = frame['DeviceId'].astype(str)
    return frame.sort_values(KEYS).reset_index(drop=True)


def assert_same_as_batch(events, cuts, aggregations=VOLUMES, name='ped', **kwargs):
    outputs, carry = incremental(events, cuts, aggregations, **kwargs)
    actual = written([o[name] for o in outputs])
    expected = written([process(reach_bin_end(events), aggregations, carry='first', **kwargs)[name]])
    pd.testing.assert_frame_equal(actual, expected, check_dtype=True)
    # One big run with no unmatched file writes the same, up to where the data reaches
    alone = written([process(events, aggregations, **{k: v for k, v in kwargs.items() if k != 'max_days_old'})[name]])
    pd.testing.assert_frame_equal(actual, alone[alone.TimeStamp <= actual.TimeStamp.max()].reset_index(drop=True))
    return actual, outputs, carry


def run_cuts(events, cuts, aggregations=VOLUMES, **kwargs):
    """The events cut at the given times, with no helper event added, so the last run stops where the data stops."""
    edges = [events.TimeStamp.min()] + sorted(cuts) + [events.TimeStamp.max() + pd.Timedelta(seconds=1)]
    carry, outputs = 'first', []
    for start, end in zip(edges[:-1], edges[1:]):
        out = process(events[(events.TimeStamp >= start) & (events.TimeStamp < end)], aggregations, carry=carry, **kwargs)
        carry = out['unmatched_events']
        outputs.append(out)
    return outputs, carry


@pytest.fixture(scope='module')
def real_events():
    return duckdb.sql(f"SELECT * FROM '{REAL_EVENTS}'").df()


#########################################
### Batch and incremental agree
#########################################

@pytest.mark.parametrize('seed', [1, 2, 3, 4, 5])
def test_real_data_at_random_cuts(real_events, seed):
    rng = np.random.default_rng(seed)
    start, end = real_events.TimeStamp.min(), real_events.TimeStamp.max()
    seconds = rng.integers(1, int((end - start).total_seconds()), 40)
    cuts = sorted({start + pd.Timedelta(seconds=int(s), milliseconds=int(rng.integers(0, 10)) * 100) for s in seconds})
    cuts = [c for c in cuts if (c.ceil('15min') - c).total_seconds() > 1]  # such a run counts as having reached the bin's end
    actual, _, _ = assert_same_as_batch(real_events, cuts)
    assert len(actual) > 10000
    assert actual['Estimated_Volumes'].max() > 5


@pytest.mark.parametrize('hours', [1, 3])
def test_real_data_at_even_windows(real_events, hours):
    start = real_events.TimeStamp.min().floor(f'{hours}h')
    cuts = list(pd.date_range(start + pd.Timedelta(hours=hours), real_events.TimeStamp.max(), freq=f'{hours}h'))
    assert_same_as_batch(real_events, cuts)


def test_real_data_runs_shorter_than_the_rolling_sum(real_events):
    """Runs of 10 minutes finish about one bin each, so most runs write one bin or none."""
    one_day = real_events[real_events.TimeStamp < real_events.TimeStamp.min() + pd.Timedelta(hours=8)]
    start = one_day.TimeStamp.min().floor('15min')
    cuts = list(pd.date_range(start + pd.Timedelta(minutes=10), one_day.TimeStamp.max(), freq='10min'))
    assert_same_as_batch(one_day, cuts)


def test_real_data_with_every_run_saved_to_a_file(real_events, tmp_path):
    """Through parquet files, with an integer DeviceId, the way a scheduled job reads and writes."""
    events = real_events.copy()
    codes = {device: i + 1000 for i, device in enumerate(sorted(events.DeviceId.unique()))}
    events['DeviceId'] = events.DeviceId.map(codes).astype('int32')
    start = events.TimeStamp.min().floor('6h')
    edges = list(pd.date_range(start, events.TimeStamp.max() + pd.Timedelta(hours=6), freq='6h'))
    frames = []
    for i, (a, b) in enumerate(zip(edges[:-1], edges[1:])):
        chunk = events[(events.TimeStamp >= a) & (events.TimeStamp < b)]
        SignalDataProcessor(
            raw_data=chunk, bin_size=15, verbose=0,
            output_dir=str(tmp_path), output_to_separate_folders=False, output_format='parquet',
            output_file_prefix=f'{i}_', aggregations=VOLUMES,
            unmatched_event_settings={'df_or_path': str(tmp_path / 'carry.parquet'), 'max_days_old': 14},
        ).run()
        frames.append(pd.read_parquet(tmp_path / f'{i}_ped.parquet'))
    batch = process(events, VOLUMES, carry='first')['ped']
    pd.testing.assert_frame_equal(written(frames), written([batch]))


#########################################
### Which bins a run writes
#########################################

class TestWriting:
    @pytest.mark.parametrize('end, last_written', [
        (at(2, 59, 59.9), at(2, 45)),  # the last bin is finished, so nothing is held back
        (at(2, 59, 59), at(2, 45)),    # still reaches the last second of the bin
        (at(2, 59, 58.9), at(2, 30)),  # bin 2:45 is not known to be finished
        (at(2, 50), at(2, 30)),
        (at(3, 0), at(2, 45)),         # an event in bin 3:00 shows 2:45 is finished, and 3:00 itself is not
    ])
    def test_last_written_bin(self, end, last_written):
        events = pd.concat([events_at(at(0, 5)), end_marker(end)])
        out = process(events, VOLUMES, carry='first')['ped']
        assert out.TimeStamp.max() == last_written

    def test_a_run_with_no_file_writes_all_it_has(self):
        events = pd.concat([events_at(at(0, 5)), end_marker(at(2, 50))])
        assert process(events, VOLUMES)['ped'].TimeStamp.max() == at(2, 45)

    def test_values_use_the_bin_and_the_3_before_it(self):
        events = pd.concat([
            events_at(at(1, 1), at(1, 5)),  # 2 in bin 1:00
            events_at(at(1, 16)),           # 1 in bin 1:15
            events_at(at(1, 31), at(1, 35), at(1, 40)),  # 3 in bin 1:30
            events_at(at(1, 46)),           # 1 in bin 1:45
            events_at(at(2, 1)),            # 1 in bin 2:00
            end_marker(at(2, 14, 59.9)),
        ])
        out = process(events, VOLUMES)['ped'].sort_values('TimeStamp').reset_index(drop=True)
        assert out.TimeStamp.tolist() == [at(1, 0), at(1, 15), at(1, 30), at(1, 45), at(2, 0)]
        np.testing.assert_allclose(
            out.Estimated_Volumes,
            [volume(2, 2), volume(1, 3), volume(3, 6), volume(1, 7), volume(1, 6)],
            rtol=1e-6,
        )
        assert out.Unique_Actuations.tolist() == [2, 1, 3, 1, 1]

    @pytest.mark.parametrize('cut', [at(1, 15), at(1, 30), at(1, 45), at(2), at(1, 22, 30), at(1, 52)])
    def test_the_rolling_sum_crosses_every_cut(self, cut):
        events = pd.concat([events_at(at(1, 1), at(1, 5)), events_at(at(1, 16)), events_at(at(1, 31), at(1, 35), at(1, 40)),
                            events_at(at(1, 46)), events_at(at(2, 1)), end_marker(at(2, 14, 59.9))])
        before, after = events[events.TimeStamp < cut], events[events.TimeStamp >= cut]
        if (cut.minute % 15) == 0:
            before = pd.concat([before, end_marker(cut - pd.Timedelta(milliseconds=100))])
        first = process(before, VOLUMES, carry='first')
        second = process(after, VOLUMES, carry=first['unmatched_events'])
        out = written([first['ped'], second['ped']])
        np.testing.assert_allclose(
            out.Estimated_Volumes,
            [volume(2, 2), volume(1, 3), volume(3, 6), volume(1, 7), volume(1, 6)],
            rtol=1e-6,
        )

    def test_nothing_reads_low_at_the_end_of_a_run(self):
        """The last 3 bins of a run were low when they looked ahead. A bin here is final when written."""
        events = pd.concat([events_at(*[at(1, m) for m in range(0, 45, 3)]), end_marker(at(1, 44, 59.9))])
        cut = process(events, VOLUMES, carry='first')['ped']
        longer = process(pd.concat([events, events_at(*[at(1, m) for m in range(45, 120, 3)]), end_marker(at(1, 59, 59.9))]), VOLUMES, carry='first')['ped']
        written_later = longer[longer.TimeStamp <= cut.TimeStamp.max()]
        pd.testing.assert_frame_equal(written([cut]), written([written_later]))

    def test_past_rows_are_written_once_and_each_run_starts_after_the_last(self):
        events = pd.concat([events_at(at(h, 7)) for h in range(8)] + [end_marker(at(h, 59, 59.9)) for h in range(9)])
        _, outputs, _ = assert_same_as_batch(events, [at(h) for h in range(1, 9)])
        spans = [(o['ped'].TimeStamp.min(), o['ped'].TimeStamp.max()) for o in outputs if len(o['ped'])]
        for (_, end), (start, _) in zip(spans[:-1], spans[1:]):
            assert start == end + pd.Timedelta(minutes=15)

    def test_no_bin_is_held_back_at_windows_on_the_bin(self):
        """Every run that reaches the end of its window writes all of it, with values final."""
        events = pd.concat([events_at(*[at(h, m) for m in (7, 22, 37, 52)]) for h in range(4)] + [end_marker(at(h, 59, 59.9)) for h in range(4)])
        _, outputs, _ = assert_same_as_batch(events, [at(h) for h in range(1, 4)])
        for h, out in enumerate(outputs):
            assert out['ped'].TimeStamp.tolist()[-1] == at(h, 45) or sorted(out['ped'].TimeStamp)[-1] == at(h, 45)


#########################################
### Edge cases against one big run
#########################################

class TestEdgeCases:
    def test_quiet_input_keeps_its_rows_and_intercept(self):
        events = pd.concat([events_at(at(0, 5)), end_marker(at(8, 59, 59.9))])
        actual, outputs, _ = assert_same_as_batch(events, [at(h) for h in (1, 3, 5, 7)])
        rows = actual[actual.DeviceId == 'A']
        assert rows.TimeStamp.tolist() == list(pd.date_range(at(0), at(8, 45), freq='15min'))
        quiet = rows[rows.TimeStamp > at(0)]
        np.testing.assert_allclose(quiet.Estimated_Volumes, INTERCEPT, rtol=1e-6)
        assert (quiet[['PedServices', 'PedActuation', 'Unique_Actuations']] == 0).all(axis=None)

    def test_input_with_services_and_no_presses_keeps_its_rows(self):
        """A ped phase on recall logs services and never a press."""
        events = pd.concat([events_at(at(0, 5), at(4, 5), event=21), end_marker(at(6, 59, 59.9))])
        actual, _, _ = assert_same_as_batch(events, [at(1), at(2), at(5)])
        np.testing.assert_allclose(actual.Estimated_Volumes, INTERCEPT, rtol=1e-6)
        assert actual.TimeStamp.min() == at(0)
        assert actual.TimeStamp.max() == at(6, 45)
        assert actual.PedServices.sum() == 2

    @pytest.mark.parametrize('cuts', [[at(1), at(2)], [at(2, 10)], [at(2, 40)], [at(3)]])
    def test_input_appearing_midway_starts_at_its_first_activity(self, cuts):
        events = pd.concat([
            events_at(at(0, 5)),
            events_at(at(2, 32), device='B'),
            end_marker(at(4, 59, 59.9)),
        ])
        actual, _, _ = assert_same_as_batch(events, cuts)
        assert actual[actual.DeviceId == 'B'].TimeStamp.min() == at(2, 30)
        assert actual[actual.DeviceId == 'A'].TimeStamp.min() == at(0)

    def test_services_only_input_first_seen_in_a_held_bin(self):
        """It has no carried press to show where it began, so the next-bin row has to."""
        events = pd.concat([events_at(at(2, 32), event=21), events_at(at(0, 5), device='B'), end_marker(at(4, 59, 59.9))])
        actual, _, _ = assert_same_as_batch(events, [at(2, 33)])
        assert actual[actual.DeviceId == 'A'].TimeStamp.min() == at(2, 30)
        assert actual[actual.DeviceId == 'A'].PedServices.sum() == 1

    @pytest.mark.parametrize('cut', [at(1, 0, 25), at(1, 0, 35), at(1, 1, 5), at(1, 15, 0.1)])
    def test_cut_inside_a_press_run(self, cut):
        """Presses 10 s apart across the end of a bin count once, wherever the cut falls."""
        run = [at(1) + pd.Timedelta(seconds=s) for s in range(0, 100, 10)]
        events = pd.concat([events_at(*run), events_at(at(1, 14, 55), at(1, 15, 5)), end_marker(at(3, 59, 59.9))])
        assert_same_as_batch(events, [cut])

    @pytest.mark.parametrize('cut', [at(1, 7), at(1, 22, 30), at(1, 37, 0.1), at(1, 44, 58)])
    def test_cut_inside_a_bin_adds_up_the_bin(self, cut):
        events = pd.concat([
            events_at(*[at(1) + pd.Timedelta(minutes=m) for m in range(0, 50, 2)]),
            events_at(*[at(1) + pd.Timedelta(minutes=m, seconds=5) for m in range(0, 50, 2)], event=21),
            end_marker(at(3, 59, 59.9)),
        ])
        actual, _, _ = assert_same_as_batch(events, [cut])
        assert actual.Estimated_Volumes.max() > 5
        assert actual.PedServices.sum() == 25

    def test_skipped_run_is_filled_like_one_big_run(self):
        events = pd.concat([events_at(at(0, 5)), events_at(at(9, 5)), end_marker(at(0, 59, 59.9)), end_marker(at(11, 59, 59.9))])
        actual, _, _ = assert_same_as_batch(events, [at(1), at(9)])
        assert len(actual) == (at(11, 45) - at(0)) / pd.Timedelta(minutes=15) + 1

    def test_empty_run_changes_nothing(self):
        out1 = process(pd.concat([events_at(at(0, 5), at(2, 50)), end_marker(at(2, 57))]), VOLUMES, carry='first')
        out2 = process(events_at(at(0)).iloc[:0], VOLUMES, carry=out1['unmatched_events'])
        assert len(out2['ped']) == 0
        sort = ['TimeStamp', 'DeviceId', 'EventId', 'Parameter']
        pd.testing.assert_frame_equal(
            out2['unmatched_events'].sort_values(sort).reset_index(drop=True),
            out1['unmatched_events'].sort_values(sort).reset_index(drop=True),
            check_dtype=False,
        )
        out3 = process(end_marker(at(3, 14, 59.9)), VOLUMES, carry=out2['unmatched_events'])
        assert out3['ped'].TimeStamp.min() == at(2, 45)
        assert out3['ped'].query('TimeStamp == @at(2, 45)').Unique_Actuations.tolist() == [1]

    def test_old_carry_file_with_no_count_rows(self, tmp_path):
        """The first run after the upgrade: old carried rows and last presses, no 943 to 947. Bins before it read as
        quiet, once."""
        old = pd.DataFrame({
            'TimeStamp': [at(0, 59, 50), at(0, 59, 50), at(0, 59, 50)],
            'DeviceId': ['A', 'A', 'Z'],
            'EventId': [1, 937, 936],
            'Parameter': [2, 2, 0],
            'IsValid': [True, True, True],
        })
        path = tmp_path / 'old.parquet'
        old.to_parquet(path)
        events = pd.concat([events_at(at(1, 0, 5), at(1, 20)), events_at(at(2, 5), device='B'), end_marker(at(2, 59, 59.9))])
        out = process(events, VOLUMES, settings={'df_or_path': str(path), 'max_days_old': 14})
        a = out['ped'].query("DeviceId == 'A'").sort_values('TimeStamp').reset_index(drop=True)
        assert a.TimeStamp.min() == at(1, 0)  # its first activity, though that press repeats the carried one
        np.testing.assert_allclose(a.Estimated_Volumes[:2], [INTERCEPT, volume(1, 1)], rtol=1e-6)
        assert out['ped'].query("DeviceId == 'B'").TimeStamp.tolist() == [at(2, 0), at(2, 15), at(2, 30), at(2, 45)]
        assert (out['unmatched_events'].EventId == 945).sum() == 2

    def test_return_volumes_false_still_leaves_quiet_rows_out(self):
        events = pd.concat([events_at(at(0, 5)), events_at(at(2, 5)), end_marker(at(2, 59, 59.9))])
        aggregations = [{'name': 'ped', 'params': {'return_volumes': False}}]
        actual, _, _ = assert_same_as_batch(events, [at(1), at(2, 3)], aggregations)
        assert 'Estimated_Volumes' not in actual.columns
        assert actual.TimeStamp.tolist() == [at(0), at(2)]


#########################################
### max_days_old
#########################################

class TestMaxDaysOld:
    def test_quiet_input_ends_after_max_days_old(self):
        """Its last-activity row is no longer carried, so it stops getting rows. One big run has no such limit."""
        out = process(pd.concat([events_at(at(0, 5)), end_marker(at(0, 59, 59.9))]), VOLUMES, carry='first', max_days_old=2)
        for day in range(1, 5):
            start = at(0) + pd.Timedelta(days=day)
            out = process(end_marker(start + pd.Timedelta(minutes=59, seconds=59.9)), VOLUMES,
                          carry=out['unmatched_events'], max_days_old=2)
            if day <= 1:
                assert not out['ped'].query("DeviceId == 'A'").empty
            else:
                assert out['ped'].query("DeviceId == 'A'").empty
        assert not out['unmatched_events'].query("EventId in [944, 945]").shape[0]

    def test_an_input_back_after_max_days_old_starts_again(self):
        out = process(pd.concat([events_at(at(0, 5)), end_marker(at(0, 59, 59.9))]), VOLUMES, carry='first', max_days_old=2)
        back = at(0) + pd.Timedelta(days=5)
        out = process(pd.concat([events_at(back + pd.Timedelta(minutes=5)), end_marker(back + pd.Timedelta(minutes=59, seconds=59.9))]),
                      VOLUMES, carry=out['unmatched_events'], max_days_old=2)
        rows = out['ped'].query("DeviceId == 'A'").sort_values('TimeStamp')
        assert rows.TimeStamp.iloc[0] == back
        np.testing.assert_allclose(rows.Estimated_Volumes.iloc[0], volume(1, 1), rtol=1e-6)


#########################################
### remove_incomplete
#########################################

HAS_DATA = {'name': 'has_data', 'params': {'no_data_min': 15, 'min_data_points': 1}}


@pytest.mark.parametrize('return_volumes', [True, False])
def test_remove_incomplete_keeps_a_bin_written_after_its_run(return_volumes):
    # One quiet signal: the first run stops well short of the end of the bin, which has_data passed
    events = pd.concat([events_at(at(1, 1), at(1, 13)), events_at(at(1, 20), at(1, 50)), end_marker(at(1, 59, 59.9))])
    aggregations = [HAS_DATA, {'name': 'ped', 'params': {'return_volumes': return_volumes}}]
    big = process(events, aggregations, carry='first', remove_incomplete=True)['ped']
    first = process(events[events.TimeStamp < at(1, 14)], aggregations, carry='first', remove_incomplete=True)
    assert first['ped'].empty
    assert (first['unmatched_events'].EventId == 942).sum() == 1
    second = process(events[events.TimeStamp >= at(1, 14)], aggregations, carry=first['unmatched_events'], remove_incomplete=True)
    pd.testing.assert_frame_equal(written([second['ped']]), written([big]))
    assert at(1) in set(second['ped'].TimeStamp)


def test_remove_incomplete_leaves_out_a_bin_with_no_data():
    events = pd.concat([events_at(at(1, 1)), events_at(at(2, 1)), end_marker(at(1, 14, 59)), end_marker(at(2, 14, 59.9))])
    out = process(events, [HAS_DATA, {'name': 'ped', 'params': {}}], remove_incomplete=True)['ped']
    assert sorted(out.query("DeviceId == 'A'").TimeStamp) == [at(1), at(2)]


#########################################
### Against the formula on real data
#########################################

def test_the_published_formula_on_real_data(real_events):
    """The formula, recalculated from Unique_Actuations, gives the column."""
    frame = written([process(real_events, VOLUMES)['ped']])
    rolling = frame.groupby(['DeviceId', 'Phase'])['Unique_Actuations'].rolling(4, min_periods=1).sum().reset_index(level=[0, 1], drop=True).sort_index()
    hourly = 0.7167 * rolling + 0.0599 * rolling ** 2
    ratio = (frame['Unique_Actuations'] / rolling).fillna(0)
    expected = hourly * ratio + INTERCEPT
    np.testing.assert_allclose(frame['Estimated_Volumes'].astype(float), expected, rtol=1e-5)
    assert frame['Unique_Actuations'].sum() < frame['PedActuation'].sum()
    # Every input has every bin from its first activity on
    spans = frame.groupby(['DeviceId', 'Phase']).TimeStamp.agg(['min', 'max', 'count'])
    assert (spans['count'] == (spans['max'] - spans['min']) / pd.Timedelta(minutes=15) + 1).all()


def test_first_window_with_no_settings_then_a_carry(real_events):
    """How ODOT's scheduled job starts: known detectors and timeline but no unmatched settings until it has a carry."""
    has_data = {'name': 'has_data', 'params': {'no_data_min': 15, 'min_data_points': 1}}
    timeline = {'name': 'timeline', 'params': {'min_duration': 0.1, 'cushion_time': 60}}
    def aggregations(known):
        return [has_data, timeline, {'name': 'actuations', 'params': {'known_detectors_df_or_path': known}}, {'name': 'ped', 'params': {}}]
    events = real_events[real_events.TimeStamp < real_events.TimeStamp.min() + pd.Timedelta(hours=9)]
    start = events.TimeStamp.min().floor('3h')
    edges = list(pd.date_range(start, events.TimeStamp.max() + pd.Timedelta(hours=3), freq='3h'))
    carry, known, frames = None, None, []
    for a, b in zip(edges[:-1], edges[1:]):
        chunk = pd.concat([events[(events.TimeStamp >= a) & (events.TimeStamp < b)], end_marker(b - pd.Timedelta(milliseconds=100))])
        out = process(chunk, aggregations(known), carry=carry)
        carry, known = out['unmatched_events'], out['known_detectors']
        frames.append(out['ped'])
    big = process(pd.concat([events, end_marker(edges[-1] - pd.Timedelta(milliseconds=100))]), VOLUMES)['ped']
    pd.testing.assert_frame_equal(written(frames), written([big]))


@pytest.mark.parametrize('return_volumes', [True, False])
def test_to_sql_query_runs_on_a_table_of_your_own(return_volumes):
    events = pd.concat([events_at(at(1, 1), at(1, 20)), end_marker(at(1, 59, 59.9))])
    aggregations = [{'name': 'ped', 'params': {'return_volumes': return_volumes}}]
    query = SignalDataProcessor(raw_data=events, bin_size=15, verbose=0, to_sql=True, aggregations=aggregations).run()['ped']
    connection = duckdb.connect()
    connection.register('events_frame', events)
    connection.execute("CREATE TABLE raw_data AS SELECT TimeStamp, DeviceId, EventId::INT16 AS EventId, Parameter::INT16 AS Parameter FROM events_frame")
    connection.execute(query)
    frame = connection.execute("SELECT * FROM ped").df()
    pd.testing.assert_frame_equal(written([frame]), written([process(events, aggregations)['ped']]), check_dtype=False)


def test_volumes_default_stays_on():
    assert 'Estimated_Volumes' in process(events_at(at(1), at(1, 2)), VOLUMES)['ped'].columns

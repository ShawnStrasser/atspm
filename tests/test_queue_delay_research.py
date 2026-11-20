"""approach_delay's Queue Model (queue_model=True) against the research it came from.

The same twelve real ODOT advance detectors as tests/test_queue_model_research.py, 13:00 to 20:00 on Nov 12 2026. The
expected Queue_Delay_Seconds per 15-minute bin (tests/precalculated/queue_research_delay.parquet) come from the delay study's
own code, the code scored against stop bar count zone truth at 438 advance detectors, not from this package: each car's
Queue Model departure less its arrival, or UDOT's delay plus the Spillback Flag's estimate where the flag is up, where
RedDepartures is over 0.25, or where the model drops the car or calls its queue impossible. The file also holds the study's
UDOT delay (udot) and estimate (est), which Delay_Seconds and Queued_Delay_Seconds must keep. One car of the study's own run
left exactly as service ended and read as not served through float error; the file was made with that comparison exact
(qdelay/golden_fix.py in the study folder), which moves one bin.

With queue_model off approach_delay is as it was and has no Queue_Delay_Seconds column.
"""
import duckdb
import pandas as pd
import pytest
from tests.test_queue_model import process, KEYS

EVENTS = 'tests/queue_research_events.parquet'
CONFIG = 'tests/queue_research_config.parquet'
EXPECTED = 'tests/precalculated/queue_research_delay.parquet'
QUEUE = {'name': 'approach_delay', 'params': {'latency_offset_seconds': 0, 'queue_model': True}}
FLAG_ONLY = {'name': 'approach_delay', 'params': {'latency_offset_seconds': 0}}
# The study counts in tenths of a second and the package in milliseconds: a bin of a few hundred cars can differ by this much
TOLERANCE_SECONDS = 0.5


@pytest.fixture(scope='module')
def data():
    events = duckdb.query(f"SELECT * FROM '{EVENTS}' ORDER BY TimeStamp, DeviceId, EventId, Parameter").df()
    return events, pd.read_parquet(CONFIG), pd.read_parquet(EXPECTED)


def matches(out, expected, last_may_wait=False):
    """Every bin of the research equal. With last_may_wait, runs cut at the end of the data may hold back the last bins."""
    assert not out.duplicated(KEYS).any(), 'a bin was written twice'
    got = out.astype({'Phase': int}).merge(expected.astype({'Phase': int}), on=KEYS, how='right', indicator=True)
    missing = got[got._merge == 'right_only']
    if last_may_wait:
        missing = missing[missing.TimeStamp < expected.TimeStamp.max()]
    assert missing.empty, f"bins of the research not written:\n{missing.head(10).to_string()}"
    both = got[got._merge == 'both']
    gap = (both.Queue_Delay_Seconds_x - both.Queue_Delay_Seconds_y).abs()
    assert gap.max() <= TOLERANCE_SECONDS, f'{(gap > TOLERANCE_SECONDS).sum()} bins differ:\n{both[gap > TOLERANCE_SECONDS].head(10).to_string()}'
    for column, study in (('Delay_Seconds', 'udot'), ('Queued_Delay_Seconds', 'est')):
        assert (both[column] - both[study]).abs().max() <= TOLERANCE_SECONDS, column


def cut_runs(events, config, minutes, aggregation=QUEUE):
    edges = list(pd.date_range(events.TimeStamp.min().floor('h'), events.TimeStamp.max().ceil('h'), freq=f'{minutes}min'))
    parts, carry = [], 'first'
    for start, end in zip(edges, edges[1:]):
        result = process(events[(events.TimeStamp >= start) & (events.TimeStamp < end)], config, [aggregation], carry)
        carry = result['unmatched_events']
        parts.append(result['approach_delay'])
    return pd.concat(parts, ignore_index=True)


def test_one_run_gives_the_research_delay_in_every_bin(data):
    events, config, expected = data
    matches(process(events, config, [QUEUE])['approach_delay'], expected)


@pytest.mark.parametrize('minutes', [60, 15])
def test_runs_cut_every_hour_or_15_minutes_give_the_research_numbers(data, minutes):
    events, config, expected = data
    matches(cut_runs(events, config, minutes), expected, last_may_wait=True)


@pytest.mark.parametrize('minutes', [60, 15])
def test_runs_cut_equal_one_run_exactly(data, minutes):
    events, config, _ = data
    one = process(events, config, [QUEUE], 'first')['approach_delay']
    cut = cut_runs(events, config, minutes)
    both = one.merge(cut, on=KEYS, suffixes=('', '_cut'))
    assert not cut.duplicated(KEYS).any() and len(both) == len(cut) == len(one)
    for column in ('Delay_Seconds', 'Queued_Delay_Seconds', 'Queue_Delay_Seconds'):
        assert (both[column] - both[f'{column}_cut']).abs().max() < 1e-3, column


def test_delay_seconds_and_the_estimate_are_unchanged_by_the_queue_model(data):
    events, config, _ = data
    model = process(events, config, [QUEUE])['approach_delay']
    flag = process(events, config, [FLAG_ONLY])['approach_delay']
    assert 'Queue_Delay_Seconds' not in flag.columns
    pd.testing.assert_frame_equal(model.drop(columns='Queue_Delay_Seconds').sort_values(KEYS).reset_index(drop=True),
                                  flag.sort_values(KEYS).reset_index(drop=True))


def test_where_the_queue_model_is_off_it_is_udot_plus_the_estimate(data):
    events, config, _ = data
    out = process(events, config, [QUEUE])['approach_delay'].astype({'Phase': int})
    off = config[config.RedDepartures > 0.25][['DeviceId', 'Phase']].astype({'Phase': int})
    left_out = out.merge(off, on=['DeviceId', 'Phase'])
    assert len(left_out)
    assert ((left_out.Delay_Seconds + left_out.Queued_Delay_Seconds - left_out.Queue_Delay_Seconds).abs() < 1e-2).all()


@pytest.mark.parametrize('first, then', [(QUEUE, FLAG_ONLY), (FLAG_ONLY, QUEUE)])
def test_turning_the_queue_model_on_or_off_between_runs_keeps_delay_seconds(data, first, then):
    events, config, _ = data
    edges = list(pd.date_range('2026-11-12 13:00', '2026-11-12 20:00', freq='15min'))
    parts, carry = [], 'first'
    for start, end in zip(edges, edges[1:]):
        result = process(events[(events.TimeStamp >= start) & (events.TimeStamp < end)], config, [first if start.hour < 16 else then], carry)
        carry = result['unmatched_events']
        parts.append(result['approach_delay'][KEYS + ['Delay_Seconds', 'Queued_Delay_Seconds']])
    cut = pd.concat(parts, ignore_index=True)
    one = process(events, config, [FLAG_ONLY], 'first')['approach_delay']
    assert not cut.duplicated(KEYS).any()
    both = one.merge(cut, on=KEYS, how='outer', suffixes=('', '_cut'), indicator=True)
    assert (both._merge == 'both').all()
    for column in ('Delay_Seconds', 'Queued_Delay_Seconds'):
        assert (both[column] - both[f'{column}_cut']).abs().max() < 1e-3, column

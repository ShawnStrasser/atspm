"""arrival_on_green's Queue Model (queue_model=True) against the research it came from.

Twelve real ODOT advance detectors from the stop bar count study, 13:00 to 20:00 on Nov 12 2026, signals anonymized: one,
two and three lanes, with and without a measured TravelTime, and two whose RedDepartures is over 0.25 (Queue Model off,
Spillback Flag only). The expected vehicles on green per 15-minute bin (tests/precalculated/queue_research.parquet) come
from the study's own Queue Model, the code scored against stop bar counts at 438 advance detectors, not from this package.
Any change that keeps these tests passing gives the results the research measured, in one run and in runs cut every hour
or every 15 minutes.
"""
import duckdb
import pandas as pd
import pytest
from tests.test_queue_model import process, KEYS

EVENTS = 'tests/queue_research_events.parquet'
CONFIG = 'tests/queue_research_config.parquet'
EXPECTED = 'tests/precalculated/queue_research.parquet'
QUEUE = {'name': 'arrival_on_green', 'params': {'latency_offset_seconds': 0, 'queue_model': True}}
FLAG_ONLY = {'name': 'arrival_on_green', 'params': {'latency_offset_seconds': 0}}


@pytest.fixture(scope='module')
def data():
    events = duckdb.query(f"SELECT * FROM '{EVENTS}' ORDER BY TimeStamp, DeviceId, EventId, Parameter").df()
    return events, pd.read_parquet(CONFIG), pd.read_parquet(EXPECTED)


def on_green(out):
    out = out.assign(Vehicles_On_Green=(out.Percent_AOG * out.Total_Actuations).round().astype(int))
    return out[KEYS + ['Total_Actuations', 'Vehicles_On_Green']].astype({'Phase': int, 'Total_Actuations': int})


def matches(out, expected, last_may_wait=False):
    """Every bin equal to the research. With last_may_wait, runs cut at the end of the data may hold back the last bin,
    whose queues the next run would finish."""
    assert not out.duplicated(KEYS).any(), 'a bin was written twice'
    got = on_green(out).merge(expected.astype({'Phase': int}), on=KEYS, how='outer', suffixes=('', '_research'), indicator=True)
    both = got[got._merge == 'both']
    held = got[got._merge == 'right_only']
    if last_may_wait:
        held = held[held.TimeStamp < expected.TimeStamp.max()]
    assert held.empty and not (got._merge == 'left_only').any(), f"bins in one and not the other:\n{got[got._merge != 'both'].head(10).to_string()}"
    bad = both[(both.Total_Actuations != both.Total_Actuations_research) | (both.Vehicles_On_Green != both.Vehicles_On_Green_research)]
    assert bad.empty, f'{len(bad)} bins differ from the research:\n{bad.head(10).to_string()}'


def cut_runs(events, config, minutes):
    edges = list(pd.date_range(events.TimeStamp.min().floor('h'), events.TimeStamp.max().ceil('h'), freq=f'{minutes}min'))
    parts, carry = [], 'first'
    for start, end in zip(edges, edges[1:]):
        result = process(events[(events.TimeStamp >= start) & (events.TimeStamp < end)], config, [QUEUE], carry)
        carry = result['unmatched_events']
        parts.append(result['arrival_on_green'])
    return pd.concat(parts)


def test_one_run_gives_the_research_vehicles_on_green_in_every_bin(data):
    events, config, expected = data
    matches(process(events, config, [QUEUE])['arrival_on_green'], expected)


@pytest.mark.parametrize('minutes', [60, 15])
def test_runs_cut_every_hour_or_15_minutes_give_the_research_numbers(data, minutes):
    events, config, expected = data
    out = cut_runs(events, config, minutes)
    assert not out.duplicated(KEYS).any(), 'a bin was written twice'
    matches(out, expected, last_may_wait=True)


def test_the_queue_model_moves_vehicles_off_green_only_where_it_runs(data):
    events, config, _ = data
    model = on_green(process(events, config, [QUEUE])['arrival_on_green'])
    flag = on_green(process(events, config, [FLAG_ONLY])['arrival_on_green'])
    both = model.merge(flag, on=KEYS, suffixes=('', '_flag'))
    assert (both.Total_Actuations == both.Total_Actuations_flag).all()
    off = config[config.RedDepartures > 0.25][['DeviceId', 'Phase']].astype({'Phase': int})
    left_out = both.merge(off, on=['DeviceId', 'Phase'])
    assert len(left_out) and (left_out.Vehicles_On_Green == left_out.Vehicles_On_Green_flag).all()
    runs = both.merge(off, on=['DeviceId', 'Phase'], how='left', indicator=True)
    runs = runs[runs._merge == 'left_only']
    assert runs.Vehicles_On_Green.sum() < 0.95 * runs.Vehicles_On_Green_flag.sum()

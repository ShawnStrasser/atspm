"""An alarm is reported at once with 60 seconds, and again with its true length when the next alarm event arrives."""
import pandas as pd
import pytest
from src.atspm import SignalDataProcessor

T0 = pd.Timestamp('2026-09-01 08:00:00')
TIMELINE = {'name': 'timeline', 'params': {'min_duration': 0.2, 'cushion_time': 60}}
COLUMNS = ['TimeStamp', 'DeviceId', 'EventId', 'Parameter']


def cycles(start_minute, end_minute):
    return [(T0 + pd.Timedelta(minutes=m), 'A', 1, 2) for m in range(start_minute, end_minute)]


def run(rows, state):
    events = pd.DataFrame(rows, columns=COLUMNS)
    settings = {'df_or_path': state, 'max_days_old': 14, 'first_run': True} if state else None
    with SignalDataProcessor(raw_data=events, bin_size=15, verbose=0, unmatched_event_settings=settings,
                             aggregations=[{'name': 'has_data', 'params': {}}, TIMELINE]) as p:
        p.load()
        p.aggregate()
        if state:
            p.conn.execute(f"COPY unmatched_events TO '{state}' (FORMAT PARQUET)")
        return p.conn.execute("SELECT * FROM timeline WHERE EventClass = 'MMU Flash' ORDER BY StartTime, Duration").df()


ALARM_ON = (T0 + pd.Timedelta(minutes=10), 'A', 174, 16)
ALARM_OFF = (T0 + pd.Timedelta(minutes=20), 'A', 174, 0)


def test_prompt_row_then_final_row_across_runs(tmp_path):
    state = str(tmp_path / 's.parquet')
    first = run(cycles(0, 15) + [ALARM_ON], state)
    assert first[['Duration', 'EventValue']].values.tolist() == [[60.0, 0]]
    assert run(cycles(15, 18), state).empty  # written once: no second prompt while it stays open
    final = run(cycles(18, 30) + [ALARM_OFF], state)
    assert len(final) == 1 and final.Duration.iloc[0] == 600 and pd.isna(final.EventValue.iloc[0])
    assert final.StartTime.iloc[0] == first.StartTime.iloc[0]


def test_an_alarm_that_ends_in_its_run_has_only_the_final_row(tmp_path):
    out = run(cycles(0, 30) + [ALARM_ON, ALARM_OFF], str(tmp_path / 's.parquet'))
    assert len(out) == 1 and out.Duration.iloc[0] == 600 and pd.isna(out.EventValue.iloc[0])


def test_each_bit_of_one_event_has_its_own_rows(tmp_path):
    state = str(tmp_path / 's.parquet')
    events = pd.DataFrame(cycles(0, 15) + [(T0 + pd.Timedelta(minutes=10), 'A', 174, 16 + 1)], columns=COLUMNS)
    with SignalDataProcessor(raw_data=events, bin_size=15, verbose=0, unmatched_event_settings={'df_or_path': state, 'max_days_old': 14, 'first_run': True},
                             aggregations=[{'name': 'has_data', 'params': {}}, TIMELINE]) as p:
        p.load()
        p.aggregate()
        got = p.conn.execute("SELECT EventClass, Duration, EventValue FROM timeline WHERE EventValue = 0 ORDER BY 1").fetchall()
    assert got == [('Cycle Fault', 60.0, 0), ('MMU Flash', 60.0, 0)]


def test_not_flash_gets_no_prompt_row(tmp_path):
    events = pd.DataFrame(cycles(0, 15) + [(T0 + pd.Timedelta(minutes=10), 'A', 173, 2)], columns=COLUMNS)
    with SignalDataProcessor(raw_data=events, bin_size=15, verbose=0, unmatched_event_settings={'df_or_path': str(tmp_path / 's.parquet'), 'max_days_old': 14, 'first_run': True},
                             aggregations=[{'name': 'has_data', 'params': {}}, TIMELINE]) as p:
        p.load()
        p.aggregate()
        assert p.conn.execute("SELECT COUNT(*) FROM timeline WHERE EventClass LIKE 'Flash%'").fetchone()[0] == 0


def test_one_run_with_no_state_has_no_prompt_row():
    out = run(cycles(0, 15) + [ALARM_ON], None)
    assert len(out) == 1 and pd.isna(out.EventValue.iloc[0])


def test_split_failures_by_detector_raises():
    with pytest.raises(ValueError, match='only by_approach=True works'):
        SignalDataProcessor(raw_data=pd.DataFrame(cycles(0, 15), columns=COLUMNS), bin_size=15, verbose=0,
                            aggregations=[{'name': 'split_failures', 'params': {'red_time': 5, 'red_occupancy_threshold': 0.8, 'green_occupancy_threshold': 0.8, 'by_approach': False}}])

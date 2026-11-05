"""atspm's measures on atspm-detector's to_atspm_config output: the bundled atspm-detector sample and atspm's own samples."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import atspm_detector as dc
from atspm_detector.check import SAMPLE, read_parquet

from src.atspm import SignalDataProcessor


@pytest.fixture(scope="module")
def sample_pred():
    return dc.predict(SAMPLE)


def run_atspm(events: pd.DataFrame, config: pd.DataFrame):
    aggs = [{"name": "has_data", "params": {}},
            {"name": "actuations", "params": {}},
            {"name": "arrival_on_green", "params": {"latency_offset_seconds": 0, "spillback_on_seconds": None}},
            {"name": "split_failures", "params": {"red_time": 5, "red_occupancy_threshold": 0.80,
                                                  "green_occupancy_threshold": 0.80, "by_approach": True,
                                                  "by_cycle": False}},
            {"name": "yellow_red", "params": {"latency_offset_seconds": 0}},
            {"name": "platoon_ratio", "params": {}}]
    with SignalDataProcessor(raw_data=events, detector_config=config, bin_size=15, remove_incomplete=False,
                             verbose=0, aggregations=aggs) as p:
        p.load()
        p.aggregate()
        return {a["name"]: p.conn.query(f"SELECT * FROM {a['name']}").df() for a in aggs}


def atspm_events(path) -> pd.DataFrame:
    ev = read_parquet(path)
    ev.columns = ["TimeStamp" if c.lower() == "timestamp" else c for c in ev.columns]
    return ev[["TimeStamp", "DeviceId", "EventId", "Parameter"]]


def aog_by_hand(ev: pd.DataFrame, config: pd.DataFrame) -> pd.DataFrame:
    """Share of Advance ON events arriving while their phase shows green (last of events 1 / 8 / 10 before it = 1).
    Each ON is moved to the stop bar first (by the config's distance, else 220 ft on phases 2 and 6, else 120 ft,
    at 35 MPH), and arrivals past the end of the results are left out."""
    ev = ev.assign(DeviceId=ev.DeviceId.astype(str), EventId=ev.EventId.astype("int64"),
                   Parameter=ev.Parameter.astype("int64"))
    adv = config[config.Function.eq("Advance")].assign(DeviceId=lambda x: x.DeviceId.astype(str),
                                                       Parameter=lambda x: x.Parameter.astype("int64"))
    on = ev[ev.EventId.eq(82)].merge(adv, on=["DeviceId", "Parameter"])
    dist = on.Phase.astype(int).isin([2, 6]).map({True: 220.0, False: 120.0})
    if "DistanceFromStopBar" in on:
        d = pd.to_numeric(on.DistanceFromStopBar, errors="coerce")
        dist = d.where(np.isfinite(d) & (d >= 0), dist)
    ms = (dist / (35 * 1.467) * 1000).round()
    on = on.assign(TimeStamp=pd.to_datetime(on.TimeStamp) + pd.to_timedelta(ms, unit="ms"))
    last = pd.to_datetime(ev.TimeStamp).max()
    bin_end = last.floor("15min") + pd.Timedelta(minutes=15)
    end = bin_end if bin_end - last <= pd.Timedelta(seconds=1) else last + pd.Timedelta(milliseconds=1)
    on = on[on.TimeStamp < end]
    ph = ev[ev.EventId.isin([1, 8, 10])].rename(columns={"Parameter": "Phase", "EventId": "state"})
    ph = ph.assign(Phase=ph.Phase.astype(int))
    rows = []
    for (dv, p), g in on.groupby(["DeviceId", "Phase"]):
        s = ph[(ph.DeviceId == dv) & (ph.Phase == p)].sort_values(["TimeStamp", "state"])
        ts = s.TimeStamp.astype("datetime64[ns]").to_numpy().astype("int64")
        tg = g.TimeStamp.astype("datetime64[ns]").to_numpy().astype("int64")
        k = np.searchsorted(ts, tg, side="right") - 1
        st = np.where(k >= 0, s.state.to_numpy()[np.clip(k, 0, None)], 0)
        rows.append({"DeviceId": dv, "Phase": int(p), "n": len(g), "green": int((st == 1).sum())})
    return pd.DataFrame(rows, columns=["DeviceId", "Phase", "n", "green"])


def test_end_to_end_bundled(sample_pred):
    cfg = dc.to_atspm_config(sample_pred)
    ev = atspm_events(SAMPLE)
    t = run_atspm(ev, cfg)
    aog, sf, act = t["arrival_on_green"], t["split_failures"], t["actuations"]
    assert len(aog) > 0
    assert set(aog.Phase.astype(int)) == set(cfg.Phase[cfg.Function.eq("Advance")].astype(int))
    assert aog.Percent_AOG.between(0, 1).all()
    hand = aog_by_hand(ev, cfg)
    a = (aog.assign(green=aog.Percent_AOG * aog.Total_Actuations).groupby("Phase")
         .agg(n=("Total_Actuations", "sum"), green=("green", "sum")).reset_index())
    m = a.merge(hand, on="Phase", suffixes=("_atspm", "_hand"))
    assert len(m) == len(a) and (m.n_atspm == m.n_hand).all()
    assert np.allclose(m.green_atspm / m.n_atspm, m.green_hand / m.n_hand, atol=1e-6)
    assert len(sf) > 0
    assert set(sf.Phase.astype(int)) <= set(cfg.Phase[cfg.Function.eq("Presence")].astype(int))
    for c in ("Green_Occupancy", "Red_Occupancy"):
        assert sf[c].between(0, 1).all()
    assert (sf.Green_Time > 0).all() and (sf.Split_Failure >= 0).all()
    tot = act.groupby("Detector").Total.sum()
    nact = sample_pred.set_index("Detector").n_actuations.astype(int)
    assert (tot.reindex(nact.index).fillna(0).astype(int) == nact).all()
    assert ("Yellow_Red" in set(cfg.Function)) == (len(t["yellow_red"]) > 0)
    assert len(t["platoon_ratio"]) > 0 and (t["platoon_ratio"].Platoon_Ratio > 0).all()


def test_end_to_end_options(sample_pred):
    ev = atspm_events(SAMPLE)
    t = run_atspm(ev, dc.to_atspm_config(sample_pred, extra_columns=True))       # atspm ignores the extras
    assert len(t["arrival_on_green"]) > 0
    t = run_atspm(ev, dc.to_atspm_config(sample_pred, device_id_dtype="string"))
    assert len(t["arrival_on_green"]) > 0
    t = run_atspm(ev, dc.to_atspm_config(sample_pred.iloc[0:0]))                 # empty config: measures empty
    assert len(t["arrival_on_green"]) == 0 and len(t["actuations"]) > 0


def test_reads_distance_and_count_zones():
    """arrival_on_green moves an arrival by the distance; left_turn_gaps reads a zone named Count."""
    t0 = pd.Timestamp("2026-09-01 08:00:00")
    pred = pd.DataFrame({"DeviceId": ["A", "A", "A"], "Detector": [1, 2, 3], "phase_pred": [2, 2, 4],
                         "function_pred": ["Advance", "Count", "Advance"], "phase_prob": 0.9, "function_prob": 0.9,
                         "distance_ft": [513.45, 0.0, np.nan]})
    cfg = dc.to_atspm_config(pred)
    rows = []
    for m in range(0, 60):
        t = t0 + pd.Timedelta(minutes=m)
        s = lambda x: t + pd.Timedelta(seconds=x)
        # Phase 2 green from 30 to 50 s. The on at 25 s reaches the stop bar 10 s later, at 35 s, on green, with 513.45 ft at 35 MPH
        rows += [(t, 10, 2), (s(25), 82, 1), (s(26), 81, 1), (s(30), 1, 2), (s(32), 82, 2), (s(33), 81, 2),
                 (s(50), 8, 2), (s(54), 10, 2), (s(55), 1, 1), (s(56), 8, 1), (s(57), 10, 1), (s(58), 82, 2),
                 (s(58.5), 81, 2)]
    ev = pd.DataFrame(rows, columns=["TimeStamp", "EventId", "Parameter"]).assign(DeviceId="A")[
        ["TimeStamp", "DeviceId", "EventId", "Parameter"]]
    with SignalDataProcessor(raw_data=ev, detector_config=cfg, bin_size=15, verbose=0,
                             aggregations=[{"name": "has_data", "params": {}},
                                           {"name": "arrival_on_green", "params": {}},
                                           {"name": "left_turn_gaps", "params": {}}]) as p:
        p.load()
        p.aggregate()
        aog = p.conn.execute("SELECT Phase, SUM(Percent_AOG * Total_Actuations) / SUM(Total_Actuations) "
                             "FROM arrival_on_green GROUP BY 1").fetchall()
        gaps = p.conn.execute("SELECT COUNT(*) FROM left_turn_gaps").fetchall()[0][0]
    assert dict(aog)[2] == pytest.approx(1.0)  # with the default 220 ft it reaches the bar at 29.3 s, on red: 0
    assert gaps > 0


@pytest.mark.parametrize("raw,cfgp", [("src/atspm/data/sample_raw_data.parquet", "src/atspm/data/sample_config.parquet"),
                                      ("tests/hires_test_data.parquet", "tests/configs_test_data.parquet")],
                         ids=["atspm_sample_data", "atspm_test_data"])
def test_atspm_samples(raw, cfgp):
    ev = atspm_events(raw)
    pred = dc.predict(ev.rename(columns={"TimeStamp": "Timestamp"}))
    ours = dc.to_atspm_config(pred, device_id_dtype=ev.DeviceId.dtype)
    t = run_atspm(ev, ours)
    for k in ("arrival_on_green", "split_failures", "yellow_red"):
        assert len(t[k]) > 0, f"{k} empty with the exported config"
    assert t["arrival_on_green"].Percent_AOG.between(0, 1).all()
    assert t["split_failures"].Red_Occupancy.between(0, 1).all()

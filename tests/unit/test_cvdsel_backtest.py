# -*- coding: utf-8 -*-
"""사전등록 CVDSEL — 백테스트 지표가 스캐너(`cvd_selection.selection.evaluate`)와 같은 값인지 고정한다.

백테스트는 `features.stock_features`로 벡터화했고 스캐너는 종목 하나씩 계산한다.
두 정의가 갈라지면 백테스트 판정을 스캐너에 옮길 수 없다(§5).
"""

import importlib.util
import os
import sys
from datetime import date

import numpy as np
import pandas as pd
import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (_ROOT, os.path.join(_ROOT, "tools"), os.path.join(_ROOT, "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import cvdsel_backtest as cb                                  # noqa: E402
from cvd_selection.research.features import stock_features    # noqa: E402
from cvd_selection.selection import evaluate                  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "cvdsel_test_selection", os.path.join(_ROOT, "cvd_selection", "tests", "test_selection.py"))
ts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ts)


# ── 사전등록 값 ─────────────────────────────────────────────

def test_사전등록_기간과_임계값():
    assert (cb.SIGNAL_START, cb.SIGNAL_END) == (date(2010, 7, 1), date(2021, 11, 30))
    assert cb.H_MAIN == 20 and cb.H_AUX == (5, 60)
    assert cb.SEED == 20261025 and cb.BLOCK == 20
    assert (cb.MIN_AVG_A20, cb.MIN_DR5, cb.MIN_POS5, cb.MIN_RVOL, cb.MIN_CLV, cb.MAX_ATRP,
            cb.MIN_DDR) == (100e8, 5.0, 3, 1.3, 0.7, 4.0, 10.0)


def test_신호가_후보를_고른_기간에_닿지_않는다():
    assert cb.SIGNAL_END < date(2022, 1, 1)


# ── 지표: 벡터화 == 스캐너 ──────────────────────────────────

def _frame(x, code="000001"):
    bars = x.daily[code]
    fl = x.flows[code]
    return pd.DataFrame({
        "date": pd.to_datetime([b["date"] for b in bars]),
        "close": [b["close"] for b in bars], "high": [b["high"] for b in bars],
        "low": [b["low"] for b in bars], "volume": [b["volume"] for b in bars],
        "value": [b["value"] for b in bars],
        "buy": [fl[b["date"]].buy if b["date"] in fl else np.nan for b in bars],
        "sell": [fl[b["date"]].sell if b["date"] in fl else np.nan for b in bars],
    })


@pytest.mark.parametrize("kw", [{}, {"flow": (40, 60)}, {"d_volume": 1_200_000}])
def test_일봉_지표가_스캐너와_같다(kw):
    x = ts._scenario(**kw)
    # 다른 날 체결량도 넣어 DR이 창 밖 값을 쓰지 않는지 본다
    x.flows["000001"][x.calendar[-7]] = ts._SD(10, 90)
    x.flows["000001"][x.day] = ts._SD(0, 100)         # 당일 흐름 — 둘 다 쓰지 않아야 한다
    r = evaluate("000001", x)
    g = stock_features(_frame(x)).iloc[-1]
    assert g.dr5 == pytest.approx(r["dr5"])
    assert g.dr2 == pytest.approx(r["dr2"])
    assert g.pos5 == r["pos5"]
    assert bool(g.delta_prev_pos) == (r["delta_prev"] > 0)
    assert g.rvol == pytest.approx(r["rvol"])          # 1분봉 합계 = 일봉인 시나리오
    assert g.avg_a20 == pytest.approx(r["avg_a20"])
    assert g.clv == pytest.approx(r["clv"])
    assert g.atrp14 == pytest.approx(r["atrp14"])
    assert bool(g.trend) == (r["krx_close"] > r["ma20"] > r["ma60"])
    assert bool(g.ma20_up) == (r["ma20"] > r["ma20_lag"])


def test_체결량_결측이면_DR이_없다():
    x = ts._scenario()
    del x.flows["000001"][x.calendar[-3]]
    g = stock_features(_frame(x)).iloc[-1]
    assert np.isnan(g.dr5) and np.isnan(g.pos5)


# ── 집합 ───────────────────────────────────────────────────

def _panel(n=20000, seed=1):
    rng = np.random.default_rng(seed)
    return pd.DataFrame({
        "avg_a20": rng.choice([50e8, 200e8], n), "dr5": rng.normal(5, 8, n),
        "dr20": rng.normal(-5, 8, n), "pos5": rng.integers(0, 6, n).astype(float),
        "dr2": rng.normal(0, 10, n), "delta_prev_pos": rng.choice([True, False, None], n),
        "rvol": rng.uniform(0.5, 3, n), "clv": rng.uniform(0, 1, n),
        "trend": rng.random(n) < .4, "ma20_up": rng.random(n) < .5,
        "above_ma60": rng.random(n) < .6, "atrp14": rng.uniform(1, 9, n),
    }).assign(above_ma60=lambda d: d.above_ma60 | d.trend)   # C>MA20>MA60 ⇒ C>MA60


def test_명세_집합은_두_완화_집합에_포함된다():
    S = cb.sets(_panel())
    assert not (S["S0"] & ~S["S1"]).any()
    assert not (S["S0"] & ~S["S3"]).any()
    assert not (S["S1"] & ~S["S13"]).any() and not (S["S3"] & ~S["S13"]).any()
    assert S["S1"].sum() > S["S0"].sum() and S["S3"].sum() > S["S0"].sum()


def test_S2는_유동성과_흐름개선만_본다():
    d = _panel()
    S = cb.sets(d)
    want = (d.avg_a20 >= 100e8) & ((d.dr5 - d.dr20) >= 10)
    assert (S["S2"] == want.to_numpy()).all()


def test_결측_지표는_불통과():
    d = _panel(10)
    d.loc[:, "rvol"] = np.nan
    S = cb.sets(d)
    assert not S["S0"].any() and not S["S1"].any()


# ── 스캐너 전향 기록(cvd_selection.prereg) == 백테스트 집합 ──

def test_스캐너_전향기록_집합이_백테스트와_같다():
    from cvd_selection.prereg import cvdsel_sets
    rng = np.random.default_rng(7)
    n = 5000
    rows = []
    for _ in range(n):
        ma60 = 100.0
        ma20 = ma60 * rng.uniform(0.95, 1.05)
        rows.append({
            "avg_a20": rng.choice([50e8, 100e8, 200e8]), "dr5": rng.normal(5, 6),
            "dr20": rng.choice([rng.normal(-5, 8), None]), "pos5": int(rng.integers(0, 6)),
            "dr2": rng.normal(1, 5), "delta_prev": rng.normal(0, 5),
            "rvol": rng.uniform(0.8, 2.5), "clv": rng.uniform(0, 1),
            "krx_close": ma60 * rng.uniform(0.95, 1.08), "ma20": ma20, "ma60": ma60,
            "ma20_lag": ma20 * rng.uniform(0.98, 1.02), "atrp14": rng.uniform(2, 6)})
    d = pd.DataFrame(rows)
    panel = pd.DataFrame({
        "avg_a20": d.avg_a20, "dr5": d.dr5, "dr20": d.dr20.astype(float), "pos5": d.pos5,
        "dr2": d.dr2, "delta_prev_pos": d.delta_prev > 0, "rvol": d.rvol, "clv": d.clv,
        "trend": (d.krx_close > d.ma20) & (d.ma20 > d.ma60), "ma20_up": d.ma20 > d.ma20_lag,
        "above_ma60": d.krx_close > d.ma60, "atrp14": d.atrp14})
    S = cb.sets(panel)
    got = [cvdsel_sets(r) for r in rows]
    for k in ("S0", "S1", "S3", "S2"):
        assert [g[k] for g in got] == list(S[k]), k
    assert sum(S["S0"]) > 0 and sum(S["S2"]) > 0

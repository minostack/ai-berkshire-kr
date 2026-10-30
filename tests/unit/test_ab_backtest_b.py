#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`tools/ab_backtest_b.py` — 백테스트가 스캐너와 같은 신호를 보는가.

사전등록(`docs/PREREGISTRATION_B.md` §1)은 "스캐너 정의를 그대로 쓴다"고
약속했다. 벡터화한 지표가 `b_metrics`와 한 봉이라도 다르면 백테스트 결과는
스캐너가 내는 후보에 대한 증거가 아니다.
"""

import os
import sys
from datetime import date, timedelta

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

import ab_backtest_b as bt  # noqa: E402
import ab_candidate_scanner as ab  # noqa: E402


def _random_bars(n, seed):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
    high = close * (1 + rng.uniform(0, 0.03, n))
    vol = rng.integers(1_000, 100_000, n).astype(float)
    vol[rng.integers(0, n, 10)] *= 8
    value = close * vol
    return high, close, vol, value


@pytest.mark.parametrize("n,seed", [(40, 1), (60, 2), (260, 3), (400, 4)])
def test_벡터화_지표는_b_metrics와_같다(n, seed):
    high, close, vol, value = _random_bars(n, seed)
    m = bt.compute_metrics(high, close, vol, value)
    start = date(2020, 1, 1)
    bars = [{"date": start + timedelta(days=i), "high": high[i], "close": close[i],
             "volume": vol[i], "value": value[i]} for i in range(n)]
    for i in range(n):
        ref = ab.b_metrics(bars[:i + 1])
        if ref is None:
            assert np.isnan(m["rvol"][i]) and np.isnan(m["prox"][i])
            continue
        assert m["rvol"][i] == pytest.approx(ref["rvol"])
        assert m["prox"][i] == pytest.approx(ref["prox_52w"])
        assert m["history"][i] == ref["history_days"]
        if ref["avg_value_20d"] is None:
            assert np.isnan(m["avgval"][i])
        else:
            assert m["avgval"][i] == pytest.approx(ref["avg_value_20d"])
        # 게이트도 같은 판정
        g = bt.gate_masks({k: m[k][i:i + 1] for k in m})["B"][0]
        assert g == (ab.b_gate(ref) == [])


def test_재신호_억제는_직전_채택_기준_20거래일():
    idx = np.array([10, 11, 29, 30, 31, 55])
    assert bt.suppress(idx, 20).tolist() == [10, 30, 55]


def test_순수익_식은_CostModel과_같다():
    from cvd_obv.labels.costs import CostModel
    cm = CostModel.from_config()
    got = bt.net_return(np.array([10000.0]), np.array([10500.0]),
                        float(cm.commission_rate), float(cm.tax_rate_sell),
                        float(cm.slippage_rate))[0]
    assert got == pytest.approx(cm.net_return(10000, 10500))


def test_BH_FDR():
    assert bt.bh_fdr([0.01, 0.04]) == pytest.approx([0.02, 0.04])
    assert bt.bh_fdr([0.03, 0.02]) == pytest.approx([0.03, 0.03])


def test_판정_규칙():
    s = {"ci95": [0.001, 0.01], "mean": 0.005, "mde": 0.004}
    assert bt.verdict(s, 0.01) == "SIGNIFICANT_EDGE"
    assert bt.verdict(s, 0.06) == "NO_SIGNIFICANT_EDGE"
    assert bt.verdict({"ci95": [-0.02, -0.001], "mean": -0.01, "mde": 0.004}, 0.01) \
        == "SIGNIFICANT_NEGATIVE"
    assert bt.verdict({"ci95": [-0.01, 0.01], "mean": 0.001, "mde": 0.004}, 0.5) \
        == "UNDERPOWERED"


def test_블록_부트스트랩은_재현된다():
    x = np.arange(100, dtype=float)
    a = bt.block_bootstrap(x, 20, 50, 7)
    b = bt.block_bootstrap(x, 20, 50, 7)
    assert np.array_equal(a, b) and len(a) == 50

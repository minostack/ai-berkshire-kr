# -*- coding: utf-8 -*-
"""사전등록 TRUECVD 백테스트의 벡터화가 V1.5와 같은 값을 내는지 고정.

OBV는 V1.5 `calc_obv`가 유일한 정의다. 16만 (종목,날)을 돌리려고 numpy로 다시
쓴 부분이 어긋나면 여기서 멈춘다.
"""

import os
import sys
from datetime import date

import numpy as np
import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (os.path.join(_ROOT, "tools"), os.path.join(_ROOT, "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import truecvd_backtest as tc                   # noqa: E402

from cvd_obv.baseline.scanner_v15 import load_scanner_v15  # noqa: E402

SC = load_scanner_v15()


def _bars(close, volume):
    return [{"dt": f"2026{(i // 28) + 1:02d}{(i % 28) + 1:02d}",
             "open": c, "high": c, "low": c, "close": c, "volume": int(v)}
            for i, (c, v) in enumerate(zip(close, volume))]


# ── OBV ──────────────────────────────────────────────────────

def test_OBV는_V1_5와_같은_값이다():
    rng = np.random.default_rng(1)
    close = 1000 + np.cumsum(rng.normal(0, 12, 120))
    volume = rng.integers(1_000, 500_000, 120).astype(float)
    got = tc.obv_series(close, volume)
    for end in (30, 60, 119):
        want = SC.calc_obv(_bars(close[:end + 1], volume[:end + 1]))
        assert got["obv"][end] == pytest.approx(want["obv"]), end
        assert got["obv_ma20"][end] == pytest.approx(want["obv_ma20"]), end
        assert got["normalized_slope"][end] == pytest.approx(
            want["normalized_slope"]), end


def test_OBV는_같은_종가를_무시한다():
    close = np.array([100.0, 100.0, 101.0, 101.0, 100.0, 100.0, 102.0])
    volume = np.full(7, 1000.0)
    got = tc.obv_series(close, volume, lookback=2)
    # +0, +1000, +0, -1000, +0, +1000 → 누적 0,0,1000,1000,0,0,1000
    assert got["obv"][6] == pytest.approx(1000.0)


def test_봉이_부족하면_OBV가_NaN이다():
    got = tc.obv_series(np.arange(1.0, 5.0), np.full(4, 10.0))
    assert np.all(np.isnan(got["obv"]))


def test_OBV_MA20은_20봉부터_나온다():
    rng = np.random.default_rng(2)
    close = 1000 + np.cumsum(rng.normal(0, 5, 40))
    volume = np.full(40, 1000.0)
    got = tc.obv_series(close, volume)
    assert np.isnan(got["obv_ma20"][18])
    assert np.isfinite(got["obv_ma20"][19])


# ── 창 흐름 ──────────────────────────────────────────────────

def test_창_흐름은_가중평균이다():
    f = np.array([0.1] * 5 + [-0.1] * 5)
    w = np.array([100.0] * 5 + [300.0] * 5)
    got = tc.window_flow(f, 10, w)
    want = (0.1 * 500 + (-0.1) * 1500) / 2000
    assert got["nd"][9] == pytest.approx(want)
    assert got["bull"][9] == pytest.approx(0.5)
    assert got["days"][9] == 10


def test_창_흐름은_빈_날을_제외하고_센다():
    f = np.array([0.2, np.nan, 0.2, 0.2, -0.1, 0.1, 0.1, np.nan, 0.1, 0.1])
    got = tc.window_flow(f, 10)
    assert got["days"][9] == 8
    assert got["bull"][9] == pytest.approx(7 / 8)


def test_창이_덜_차면_NaN이다():
    got = tc.window_flow(np.array([0.1, 0.2, 0.3]), 10)
    assert np.all(np.isnan(got["nd"]))


def test_가중치가_없으면_단순평균이다():
    f = np.array([0.1] * 5 + [0.3] * 5)
    got = tc.window_flow(f, 10)
    assert got["nd"][9] == pytest.approx(0.2)


# ── 사전등록 상수 ────────────────────────────────────────────

def test_사전등록_상수가_문서와_같다():
    assert (tc.SIGNAL_START, tc.SIGNAL_END) == (date(2010, 6, 8), date(2026, 10, 9))
    assert (tc.PRIMARY_H, tc.SECONDARY_H) == (2, 5)
    assert tc.SEED == 20261019
    assert (tc.V15_WINDOW, tc.V15_BULL_MIN) == (10, 0.50)
    assert tc.OBV_LOOKBACK == 5


def test_가속_정의는_가져다_쓴다():
    """같은 정의가 두 벌 생기지 않게 import해서 쓴다."""
    import accel_vidya_backtest as av
    assert tc.av.accel_from_flow is av.accel_from_flow
    assert tc.av.rolling_mean is av.rolling_mean
    assert (av.RECENT_DAYS, av.PRIOR_DAYS) == (3, 7)

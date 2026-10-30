# -*- coding: utf-8 -*-
"""사전등록 MALEN — MA 길이 **하나만** 움직이는지 고정한다.

MA는 설계마다 다른 자리에 들어간다(가속=과열 차단, 일반=상회 요구+OBV MA).
한 설계 안에서 자리마다 길이가 어긋나거나, 유동성 창이 따라 움직이면
"MA 길이 효과"가 아니라 뒤섞인 효과를 재게 된다.
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

import accel_vidya_backtest as av              # noqa: E402
import malen_backtest as ml                    # noqa: E402
import truecvd_backtest as tc                  # noqa: E402


# ── 사전등록 값 ─────────────────────────────────────────────

def test_비교하는_길이는_10_20_60이고_기준선은_20이다():
    assert ml.MA_LENGTHS == (10, 20, 60)
    assert ml.BASELINE_MA == 20
    assert ml.BASELINE_MA in ml.MA_LENGTHS


def test_흐름_창은_10번에서_확정한_값으로_고정이다():
    assert (ml.RECENT_DAYS, ml.PRIOR_DAYS, ml.V15_WINDOW) == (5, 10, 15)


def test_기간과_시드는_사전등록_값이다():
    assert ml.SIGNAL_START == date(2010, 6, 15)
    assert ml.SIGNAL_END == date(2026, 10, 9)
    assert ml.LOAD_FROM == date(2009, 6, 1)
    assert (ml.PRIMARY_H, ml.SECONDARY_H) == (2, 5)
    assert ml.SEED == 20261021 and ml.RESAMPLES == 4000


def test_주가설은_넷이고_전부_기준선_대비_짝차다():
    assert ml.ORDER == ["H1", "H2", "H3", "H4"]
    lens = [ml.SPEC[k][2] for k in ml.ORDER]
    assert lens == [10, 60, 10, 60]
    designs = [ml.SPEC[k][1] for k in ml.ORDER]
    assert designs == ["accel", "accel", "v15daily", "v15daily"]
    # MA10 − MA60은 주가설이 아니다(따라 나오는 차)
    assert ml.BASELINE_MA not in lens


# ── MA 길이가 먹는 자리 ─────────────────────────────────────

def test_obv_ma도_같은_길이로_바뀐다():
    rng = np.random.default_rng(5)
    close = 1000 + np.cumsum(rng.normal(0, 10, 200))
    vol = rng.integers(1_000, 500_000, 200).astype(float)
    a = tc.obv_series(close, vol, ma_days=10)
    b = tc.obv_series(close, vol, ma_days=60)
    assert not np.allclose(a["obv_ma20"][100], b["obv_ma20"][100])
    # obv 자체와 기울기는 MA 길이와 무관하다
    assert np.allclose(a["obv"], b["obv"], equal_nan=True)
    assert np.allclose(a["normalized_slope"], b["normalized_slope"],
                       equal_nan=True)


def test_obv_series_기본값은_20이다():
    rng = np.random.default_rng(6)
    close = 1000 + np.cumsum(rng.normal(0, 10, 120))
    vol = rng.integers(1_000, 500_000, 120).astype(float)
    assert np.allclose(tc.obv_series(close, vol)["obv_ma20"],
                       tc.obv_series(close, vol, ma_days=20)["obv_ma20"],
                       equal_nan=True)


def test_build_panel_ma_인자를_비우면_20이다():
    import inspect
    p = inspect.signature(tc.build_panel).parameters
    assert p["ma_days"].default is None
    assert tc.MA_DAYS == 20


def test_유동성_창은_MA를_따라가지_않는다():
    """유동성은 이동평균선이 아니라 규모 측정이다 — 20일 고정."""
    import inspect
    src = inspect.getsource(tc.build_panel)
    assert "av.rolling_mean(value, LIQ_DAYS)" in src
    assert "av.rolling_mean(close, ma_n)" in src
    assert "obv_series(close, volume, ma_days=ma_n)" in src
    assert tc.LIQ_DAYS == 20


def test_한_설계_안에서는_모든_MA_자리가_같은_길이다():
    """`ma_n` 하나가 종가 MA와 OBV MA에 함께 들어가야 한다."""
    import inspect
    src = inspect.getsource(tc.build_panel)
    # ma_n을 쓰는 자리는 둘(종가 MA, OBV MA)이고 다른 상수가 끼지 않는다
    assert src.count("ma_n") >= 3
    assert "av.rolling_mean(close, MA_DAYS)" not in src


# ── 짝 차이 ─────────────────────────────────────────────────

def _panel(gate_a, gate_b, ret):
    S, T = np.asarray(ret).shape
    cal = [date(2026, 5, 1 + k) for k in range(T)]
    base = {"net": {2: np.asarray(ret, float)}, "in_period": np.ones(T, bool),
            "cal": cal, "cal_arr": np.array(cal, dtype="datetime64[D]"),
            "member": np.ones((S, T), bool), "idx_ret": {2: np.zeros(T)}}
    A = dict(base, gate={"accel": np.asarray(gate_a, bool)},
             ok={"accel": np.ones((S, T), bool)})
    B = dict(base, gate={"accel": np.asarray(gate_b, bool)},
             ok={"accel": np.ones((S, T), bool)})
    return A, B


def test_짝차는_두_집단_평균의_차다():
    import nxtbasis_backtest as nx
    ret = [[0.10, 0.0], [0.02, 0.0]]
    A, B = _panel([[True, False], [True, False]],
                  [[True, False], [False, False]], ret)
    got = nx.basis_diff(A, B, "accel", 2)
    assert got["x"] == pytest.approx([(0.10 + 0.02) / 2 - 0.10])


def test_같은_길이면_짝차는_0이다():
    import nxtbasis_backtest as nx
    rng = np.random.default_rng(11)
    ret = rng.normal(0, 0.02, (5, 15))
    g = rng.random((5, 15)) < 0.6
    g[:, g.sum(axis=0) == 0] = True
    A, B = _panel(g, g, ret)
    assert np.allclose(nx.basis_diff(A, B, "accel", 2)["x"], 0.0)


# ── 길이가 실제로 게이트를 바꾸는가 ─────────────────────────

def test_긴_MA는_상회_요구를_더_엄격하게_만든다():
    """상승 추세에서 종가 > MA는 긴 쪽이 더 쉽게 통과한다 — 방향 확인용."""
    close = np.arange(100, 200, dtype=float)      # 단조 상승
    ma10 = av.rolling_mean(close, 10)
    ma60 = av.rolling_mean(close, 60)
    i = 80
    assert close[i] > ma10[i] > ma60[i]           # 둘 다 통과, MA60이 더 아래


def test_하락_추세에서는_긴_MA가_더_엄격하다():
    close = np.arange(200, 100, -1, dtype=float)  # 단조 하락
    ma10 = av.rolling_mean(close, 10)
    ma60 = av.rolling_mean(close, 60)
    i = 80
    assert close[i] < ma10[i] < ma60[i]           # 둘 다 미달, MA60이 더 멀다


def test_과열_차단은_짧은_MA에서_거의_안_걸린다():
    """가속의 MA 역할 — 짧을수록 기준선이 가격을 바짝 따라간다."""
    close = np.arange(100, 200, dtype=float)
    i = 80
    assert close[i] <= av.rolling_mean(close, 10)[i] * 1.10     # 안 걸린다
    assert close[i] > av.rolling_mean(close, 60)[i] * 1.10      # 걸린다

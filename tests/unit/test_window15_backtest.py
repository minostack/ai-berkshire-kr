# -*- coding: utf-8 -*-
"""사전등록 WINDOW15 — 창 말고는 아무것도 달라지지 않았는지 고정한다.

이 검정의 답은 "두 패널이 **창만** 다른가"에 전부 달려 있다. 게이트·기간·
모집단·비용 중 하나라도 같이 움직이면 창 효과가 아니라 뒤섞인 효과를 재게 된다.
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
import truecvd_backtest as tc                  # noqa: E402
import window15_backtest as w15                # noqa: E402


# ── 사전등록 값 ─────────────────────────────────────────────

def test_비교하는_두_창은_10과_15다():
    assert w15.W10 == (3, 7, 10)
    assert w15.W15 == (5, 10, 15)
    assert w15.W10[0] + w15.W10[1] == 10
    assert w15.W15[0] + w15.W15[1] == 15


def test_기간과_시드는_사전등록_값이다():
    assert w15.SIGNAL_START == date(2010, 6, 15)   # 이력 시작 + 15거래일
    assert w15.SIGNAL_END == date(2026, 10, 9)     # 8번과 동일
    assert w15.LOAD_FROM == date(2009, 6, 1)
    assert w15.UN_START == date(2025, 4, 12)
    assert (w15.PRIMARY_H, w15.SECONDARY_H) == (2, 5)
    assert w15.SEED == 20261021
    assert w15.RESAMPLES == 4000


def test_주가설은_여섯이고_창차_둘은_양측이다():
    assert w15.ORDER == ["H1", "H2", "H3", "H4", "H5", "H6"]
    kinds = {k: w15.SPEC[k][2] for k in w15.ORDER}
    assert kinds["H1"] == kinds["H2"] == "diff"
    assert [kinds[k] for k in ("H3", "H4", "H5", "H6")] == \
        ["uni", "idx", "uni", "idx"]


def test_두_설계를_모두_본다():
    designs = {w15.SPEC[k][1] for k in w15.ORDER}
    assert designs == {"accel", "v15daily"}


# ── build_panel 기본값 보호 (8번 사전등록) ──────────────────

def test_build_panel_창_인자를_비우면_8번_그대로다():
    import inspect
    p = inspect.signature(tc.build_panel).parameters
    for name in ("recent_days", "prior_days", "v15_window"):
        assert p[name].default is None, name
    assert tc.V15_WINDOW == 10
    assert (av.RECENT_DAYS, av.PRIOR_DAYS) == (3, 7)


def test_창_인자는_accel과_일반_설계에_함께_먹는다():
    """하나만 바뀌면 '창만 다르다'가 깨진다 — 호출부를 직접 확인한다."""
    import inspect
    src = inspect.getsource(tc.build_panel)
    assert "av.accel_from_flow(f_cal, r_n, p_n)" in src
    assert "window_flow(f_cal, v15, w_cal)" in src
    assert 'wf["days"] >= v15' in src


# ── 계열 선택 ───────────────────────────────────────────────

def _panel(gate15, gate10, ret, idx=None):
    S, T = np.asarray(ret).shape
    cal = [date(2026, 4, 1 + k) for k in range(T)]
    base = {"net": {2: np.asarray(ret, float)}, "in_period": np.ones(T, bool),
            "cal": cal, "cal_arr": np.array(cal, dtype="datetime64[D]"),
            "member": np.ones((S, T), bool),
            "idx_ret": {2: np.zeros(T) if idx is None else np.asarray(idx)}}
    P15 = dict(base, gate={"accel": np.asarray(gate15, bool)},
               ok={"accel": np.ones((S, T), bool)})
    P10 = dict(base, gate={"accel": np.asarray(gate10, bool)},
               ok={"accel": np.ones((S, T), bool)})
    return P15, P10


def test_창차_계열은_두_집단_평균의_차다():
    ret = [[0.10, 0.0], [0.02, 0.0]]
    P15, P10 = _panel([[True, False], [True, False]],
                      [[True, False], [False, False]], ret)
    got = w15._series(P15, P10, "diff", "accel", 2)
    assert got["x"] == pytest.approx([(0.10 + 0.02) / 2 - 0.10])


def test_창차_계열에는_벤치마크가_상쇄된다():
    """지수를 아무 값으로 바꿔도 창차는 같아야 한다(사전등록 §3)."""
    ret = [[0.10, 0.03], [0.02, 0.01]]
    g15 = [[True, True], [True, False]]
    g10 = [[True, False], [False, True]]
    a = w15._series(*_panel(g15, g10, ret, idx=[0.0, 0.0]), "diff", "accel", 2)
    b = w15._series(*_panel(g15, g10, ret, idx=[0.5, -0.9]), "diff", "accel", 2)
    assert np.allclose(a["x"], b["x"])


def test_유니버스_지수_계열은_창15_패널만_본다():
    ret = [[0.10, 0.0], [0.00, 0.0]]
    P15, P10 = _panel([[True, False], [False, False]],
                      [[False, False], [True, False]], ret, idx=[0.04, 0.0])
    uni = w15._series(P15, P10, "uni", "accel", 2)
    idx = w15._series(P15, P10, "idx", "accel", 2)
    # 창15는 0.10을 골랐다. 유니버스 평균은 (0.10+0.00)/2 = 0.05
    assert uni["x"][0] == pytest.approx(0.05)
    assert idx["x"][0] == pytest.approx(0.06)


def test_한쪽_창에_통과가_없는_날은_창차에서_뺀다():
    ret = [[0.10, 0.05]]
    P15, P10 = _panel([[True, True]], [[True, False]], ret)
    got = w15._series(P15, P10, "diff", "accel", 2)
    assert len(got["x"]) == 1 and got["skipped_days"] == 1


# ── 창이 실제로 통과 집합을 바꾸는가 ────────────────────────

def test_창15는_앞쪽_거래일을_더_많이_버린다():
    """연속 흐름 요구가 10 → 15로 늘면 워밍업이 5거래일 길어진다."""
    f = np.full(30, 0.1)
    assert av.accel_from_flow(f, 3, 7)["ok"][9]
    assert not av.accel_from_flow(f, 5, 10)["ok"][9]
    assert av.accel_from_flow(f, 5, 10)["ok"][14]


def test_일반_설계_창도_15면_days가_15를_요구한다():
    f = np.full(20, 0.1)
    w = np.ones(20)
    assert tc.window_flow(f, 10, w)["days"][9] == 10
    assert tc.window_flow(f, 15, w)["days"][13] == 0      # 아직 못 채웠다
    assert tc.window_flow(f, 15, w)["days"][14] == 15

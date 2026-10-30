# -*- coding: utf-8 -*-
"""사전등록 NXTBASIS 백테스트 — 기준 합성·커버리지 규칙·차이 계열을 고정.

이 검정의 답은 "UN = J + NX를 어떻게 만들었는가"에 전부 달려 있다.
결측을 J로 떨어뜨리는 규칙(사전등록 §1.1)이 어긋나면 여기서 멈춘다.
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

import nxtbasis_backtest as nb                  # noqa: E402
import truecvd_backtest as tc                   # noqa: E402

D = [date(2025, 3, d) for d in (4, 5, 6, 7)]


# ── UN 합성 ──────────────────────────────────────────────────

def test_UN은_J와_NX의_합이다():
    j = {"KRX:A": {D[0]: (100, 60)}}
    nx = {"KRX:A": {D[0]: (40, 20)}}
    un, st = nb.make_un_sides(j, nx)
    assert un["KRX:A"][D[0]] == (140, 80)
    assert st == {"pairs": 1, "with_nx": 1, "without_nx": 0}


def test_NX가_없는_날은_J와_같다():
    j = {"KRX:A": {D[0]: (100, 60), D[1]: (10, 90)}}
    nx = {"KRX:A": {D[0]: (40, 20)}}
    un, st = nb.make_un_sides(j, nx)
    assert un["KRX:A"][D[1]] == (10, 90)
    assert st["with_nx"] == 1 and st["without_nx"] == 1


def test_NX가_0_0인_날도_J와_같다():
    j = {"KRX:A": {D[0]: (100, 60)}}
    nx = {"KRX:A": {D[0]: (0, 0)}}
    un, st = nb.make_un_sides(j, nx)
    assert un["KRX:A"][D[0]] == (100, 60)
    assert st["without_nx"] == 1


def test_NX만_있는_날은_UN에_들어가지_않는다():
    """기준이 비대칭해지면 안 된다 — J 행이 없는 날은 두 기준 모두 흐름 없음."""
    j = {"KRX:A": {D[0]: (100, 60)}}
    nx = {"KRX:A": {D[0]: (40, 20), D[1]: (99, 99)}}
    un, _ = nb.make_un_sides(j, nx)
    assert set(un["KRX:A"]) == {D[0]}


def test_J에만_있는_종목도_UN에_남는다():
    j = {"KRX:A": {D[0]: (100, 60)}, "KRX:B": {D[0]: (5, 5)}}
    un, _ = nb.make_un_sides(j, {"KRX:A": {D[0]: (1, 1)}})
    assert set(un) == {"KRX:A", "KRX:B"}
    assert un["KRX:B"][D[0]] == (5, 5)


def test_흐름은_같은_날_비율이라_분할에_영향받지_않는다():
    """UN 흐름 = (bJ+bN−sJ−sN)/합. 수치를 직접 고정한다."""
    j = {"KRX:A": {D[0]: (100, 60)}}
    nx = {"KRX:A": {D[0]: (10, 30)}}
    un, _ = nb.make_un_sides(j, nx)
    b, s = un["KRX:A"][D[0]]
    assert (b - s) / (b + s) == pytest.approx((110 - 90) / 200)


# ── 커버리지 규칙 (§2) ───────────────────────────────────────

def _cov_fixture():
    """3종목. D0 커버리지 1/3, D1 2/3, D2 3/3, D3 3/3."""
    j = {f"KRX:{c}": {d: (10, 10) for d in D} for c in "ABC"}
    nx = {
        "KRX:A": {D[0]: (1, 1), D[1]: (1, 1), D[2]: (1, 1), D[3]: (1, 1)},
        "KRX:B": {D[1]: (1, 1), D[2]: (1, 1), D[3]: (1, 1)},
        "KRX:C": {D[2]: (1, 1), D[3]: (1, 1)},
    }
    return j, nx


def test_커버리지는_그날_J_행이_있는_종목을_분모로_쓴다():
    j, nx = _cov_fixture()
    cov = nb.nx_coverage(j, nx, D)
    assert cov["share"][D[0]] == pytest.approx(1 / 3)
    assert cov["share"][D[1]] == pytest.approx(2 / 3)
    assert cov["share"][D[2]] == pytest.approx(1.0)


def test_신호_시작은_90퍼센트_최초_도달에_워밍업을_더한다(monkeypatch):
    j, nx = _cov_fixture()
    cal = D + [date(2025, 3, d) for d in (10, 11, 12)]
    cov = nb.nx_coverage(j, nx, cal)
    assert cov["first_cross"] == D[2]
    monkeypatch.setattr(nb, "WARMUP_DAYS", 2)
    assert nb.signal_start_from(cov, cal) == cal[cal.index(D[2]) + 2]


def test_구성종목_분모는_비구성종목을_빼고_센다():
    """사전등록 §2의 '전체'를 구성종목으로 확정했다 — 그 처리를 고정한다."""
    j, nx = _cov_fixture()
    j["KRX:Z"] = {d: (10, 10) for d in D}        # NXT 없는 비구성종목
    cov_all = nb.nx_coverage(j, nx, D)
    assert cov_all["share"][D[2]] == pytest.approx(3 / 4)
    is_member = nb.member_filter(
        {"KOSPI200": {f"KRX:{c}": [(D[0], None)] for c in "ABC"}})
    cov_mem = nb.nx_coverage(j, nx, D, is_member)
    assert cov_mem["share"][D[2]] == pytest.approx(1.0)
    assert cov_all["first_cross"] is None and cov_mem["first_cross"] == D[2]


def test_구성종목_구간은_끝이_열린_구간이다():
    is_member = nb.member_filter({"X": {"KRX:A": [(D[0], D[2])]}})
    assert is_member("KRX:A", D[0]) and is_member("KRX:A", D[1])
    assert not is_member("KRX:A", D[2])
    assert not is_member("KRX:B", D[0])


def test_구간_끝이_None이면_계속_구성종목이다():
    is_member = nb.member_filter({"X": {"KRX:A": [(D[0], None)]}})
    assert is_member("KRX:A", date(2030, 1, 1))


def test_커버리지가_문턱에_닿지_않으면_멈춘다():
    j = {f"KRX:{c}": {D[0]: (10, 10)} for c in "ABC"}
    cov = nb.nx_coverage(j, {"KRX:A": {D[0]: (1, 1)}}, D)
    assert cov["first_cross"] is None
    with pytest.raises(SystemExit):
        nb.signal_start_from(cov, D)


def test_문턱_자체는_사전등록_값이다():
    assert nb.COVERAGE_MIN == 0.90
    assert nb.WARMUP_DAYS == 10
    assert nb.SIGNAL_END == date(2026, 10, 9)
    assert nb.SEED == 20261019


# ── 차이 계열 ────────────────────────────────────────────────

def _panel(gate_un, gate_j, ret, in_period=None):
    S, T = np.asarray(ret).shape
    ip = np.ones(T, bool) if in_period is None else np.asarray(in_period)
    base = {"net": {2: np.asarray(ret, float)}, "in_period": ip,
            "cal": [date(2025, 4, 1 + k) for k in range(T)]}
    Pu = dict(base, gate={"accel": np.asarray(gate_un, bool)},
              ok={"accel": np.ones((S, T), bool)})
    Pj = dict(base, gate={"accel": np.asarray(gate_j, bool)},
              ok={"accel": np.ones((S, T), bool)})
    return Pu, Pj


def test_차이는_두_집단_평균의_차다():
    ret = [[0.10, 0.00], [0.02, 0.00]]
    Pu, Pj = _panel([[True, False], [True, False]],
                    [[True, False], [False, False]], ret)
    got = nb.basis_diff(Pu, Pj, "accel", 2)
    assert got["x"] == pytest.approx([(0.10 + 0.02) / 2 - 0.10])


def test_한쪽_기준에_통과가_없는_날은_뺀다():
    ret = [[0.10, 0.05], [0.02, 0.01]]
    Pu, Pj = _panel([[True, True], [False, False]],
                    [[True, False], [False, False]], ret)
    got = nb.basis_diff(Pu, Pj, "accel", 2)
    assert len(got["x"]) == 1 and got["skipped_days"] == 1


def test_두_기준이_같으면_차이는_정확히_0이다():
    """상쇄 논증(§3)의 수치 확인 — 벤치마크가 아니라 집단이 같아서 0이다."""
    rng = np.random.default_rng(3)
    ret = rng.normal(0, 0.02, (6, 20))
    g = rng.random((6, 20)) < 0.5
    g[:, g.sum(axis=0) == 0] = True
    Pu, Pj = _panel(g, g, ret)
    got = nb.basis_diff(Pu, Pj, "accel", 2)
    assert np.allclose(got["x"], 0.0)


def test_신호_구간_밖은_세지_않는다():
    ret = [[0.10, 0.20]]
    Pu, Pj = _panel([[True, True]], [[True, True]], ret,
                    in_period=[True, False])
    assert len(nb.basis_diff(Pu, Pj, "accel", 2)["x"]) == 1


# ── 게이트 일치·흐름 일치 ───────────────────────────────────

def test_게이트_일치도는_교집합_나누기_합집합이다():
    Pu, Pj = _panel([[True, True], [True, False]],
                    [[True, False], [False, False]],
                    [[0.0, 0.0], [0.0, 0.0]])
    g = nb.gate_agreement(Pu, Pj, "accel")
    assert (g["both"], g["only_un"], g["only_j"]) == (1, 2, 0)
    assert g["jaccard"] == pytest.approx(1 / 3)


def test_흐름_일치_통계는_부호_불일치를_센다():
    j = {"KRX:A": {D[0]: (60, 40), D[1]: (60, 40)}}
    nx = {"KRX:A": {D[0]: (0, 0), D[1]: (0, 200)}}   # 둘째 날은 부호가 뒤집힌다
    un, _ = nb.make_un_sides(j, nx)
    st = nb.flow_agreement(j, un, D[0], D[1])
    assert st["pairs"] == 2
    assert st["sign_mismatch"] == pytest.approx(0.5)


# ── 공유 코드를 고친 뒤의 약속 ──────────────────────────────

def test_truecvd_기본값은_그대로다():
    """`build_panel`에 인자를 붙였다 — 비우면 TRUECVD 사전등록 값이어야 한다."""
    import inspect
    p = inspect.signature(tc.build_panel).parameters
    for name in ("signal_start", "signal_end", "load_from", "bars", "sides"):
        assert p[name].default is None, name
    assert p["market_div"].default == "J"
    assert tc.SIGNAL_START == date(2010, 6, 8)
    assert tc.SIGNAL_END == date(2026, 10, 9)
    assert tc.LOAD_FROM == date(2009, 6, 1)


def test_load_sides는_기본이_J다():
    import inspect
    assert inspect.signature(tc.load_sides).parameters["market_div"].default == "J"

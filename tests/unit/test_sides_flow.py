# -*- coding: utf-8 -*-
"""체결량 흐름 기준 sides-1.0 — 구간별로 어느 시장 구분을 고르는지 고정한다.

이 선택이 틀리면 스캐너가 **구간마다 다른 시장의 흐름**을 섞어 본다.
2026-10-20부터 매일 받는 구분이 `UN` 하나로 바뀌었고 `J`·`NX`는 과거 행만
남아 있어(`docs/BAR_STANDARD.md`), 한 구분만 읽으면 반드시 끊긴다.
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

from cvd_obv import sides_flow as sf           # noqa: E402
import accel_vidya_backtest as av              # noqa: E402

BEFORE = date(2024, 6, 3)                      # NXT 개시 전
AFTER = date(2026, 10, 16)                     # 개시 후


# ── SidesDay ────────────────────────────────────────────────

def test_흐름은_매수매도_차를_합으로_나눈_값이다():
    d = sf.SidesDay(60, 40, sf.SOURCE_UN, False)
    assert d.total == 100
    assert d.f == pytest.approx(0.2)


def test_합이_0이면_흐름이_없다():
    assert sf.SidesDay(0, 0, sf.SOURCE_J, False).f is None


def test_합을_거래량으로_쓰지_않는다는_것을_이름으로_구분한다():
    """`total`은 체결량 합이지 거래량이 아니다 — 속성 이름이 섞이면 안 된다."""
    d = sf.SidesDay(7, 3, sf.SOURCE_UN, False)
    assert not hasattr(d, "volume")
    assert d.total == 10


# ── 구간별 구분 선택 ────────────────────────────────────────

class _FakeTable:
    def __init__(self, rows):
        self._rows = rows

    def column(self, name):
        idx = ["security_id", "trading_date", "buy_qty", "sell_qty",
               "market_div", "quality_flags"].index(name)

        class _C:
            def __init__(self, vals):
                self._v = vals

            def to_pylist(self):
                return self._v
        return _C([r[idx] for r in self._rows])


def _patch(monkeypatch, rows):
    monkeypatch.setattr(sf, "read_trade_sides",
                        lambda **kw: _FakeTable(rows))


def test_NXT_개시_전에는_J를_쓴다(monkeypatch):
    _patch(monkeypatch, [("KRX:A", BEFORE, 60, 40, "J", [])])
    out = sf.read_sides_flow(["KRX:A"], BEFORE, BEFORE)
    assert out["A"][BEFORE].source == sf.SOURCE_J
    assert out["A"][BEFORE].f == pytest.approx(0.2)


def test_개시_후에는_저장된_UN을_먼저_쓴다(monkeypatch):
    _patch(monkeypatch, [
        ("KRX:A", AFTER, 60, 40, "J", []),
        ("KRX:A", AFTER, 10, 90, "NX", []),
        ("KRX:A", AFTER, 70, 130, "UN", []),
    ])
    out = sf.read_sides_flow(["KRX:A"], AFTER, AFTER)
    assert out["A"][AFTER].source == sf.SOURCE_UN
    assert (out["A"][AFTER].buy, out["A"][AFTER].sell) == (70, 130)


def test_UN이_없으면_J와_NX를_합친다(monkeypatch):
    _patch(monkeypatch, [
        ("KRX:A", AFTER, 60, 40, "J", []),
        ("KRX:A", AFTER, 10, 90, "NX", []),
    ])
    out = sf.read_sides_flow(["KRX:A"], AFTER, AFTER)
    assert out["A"][AFTER].source == sf.SOURCE_JNX
    assert (out["A"][AFTER].buy, out["A"][AFTER].sell) == (70, 130)


def test_UN도_NX도_없으면_J_그대로다(monkeypatch):
    _patch(monkeypatch, [("KRX:A", AFTER, 60, 40, "J", [])])
    out = sf.read_sides_flow(["KRX:A"], AFTER, AFTER)
    assert out["A"][AFTER].source == sf.SOURCE_J
    assert (out["A"][AFTER].buy, out["A"][AFTER].sell) == (60, 40)


def test_NX만_있는_날은_쓰지_않는다(monkeypatch):
    """기준이 비대칭해진다 — NXT만의 흐름을 KRX 일봉에 붙이지 않는다."""
    _patch(monkeypatch, [("KRX:A", AFTER, 10, 90, "NX", [])])
    assert sf.read_sides_flow(["KRX:A"], AFTER, AFTER) == {}


def test_둘_다_0인_행은_흐름_없음이다(monkeypatch):
    from cvd_obv.contracts.market_schema import TRADE_SIDE_ZERO_FLAG
    _patch(monkeypatch, [("KRX:A", AFTER, 0, 0, "UN", [TRADE_SIDE_ZERO_FLAG])])
    assert sf.read_sides_flow(["KRX:A"], AFTER, AFTER) == {}


def test_잠정_행은_표시가_남는다(monkeypatch):
    from cvd_obv.ingestion.kis_trade_sides import FLAG_PROVISIONAL
    _patch(monkeypatch, [("KRX:A", AFTER, 60, 40, "UN", [FLAG_PROVISIONAL])])
    out = sf.read_sides_flow(["KRX:A"], AFTER, AFTER)
    assert out["A"][AFTER].provisional is True


def test_합성할_때_한쪽만_잠정이어도_잠정이다(monkeypatch):
    from cvd_obv.ingestion.kis_trade_sides import FLAG_PROVISIONAL
    _patch(monkeypatch, [
        ("KRX:A", AFTER, 60, 40, "J", []),
        ("KRX:A", AFTER, 10, 90, "NX", [FLAG_PROVISIONAL]),
    ])
    assert sf.read_sides_flow(["KRX:A"], AFTER, AFTER)["A"][AFTER].provisional


def test_돌려주는_키는_코드다(monkeypatch):
    _patch(monkeypatch, [("KRX:005930", AFTER, 1, 1, "UN", [])])
    assert set(sf.read_sides_flow(["KRX:005930"], AFTER, AFTER)) == {"005930"}


# ── flow_series ─────────────────────────────────────────────

def test_캘린더_격자로_옮긴다():
    cal = [date(2026, 10, d) for d in (14, 15, 16)]
    days = {cal[0]: sf.SidesDay(60, 40, sf.SOURCE_UN, False),
            cal[2]: sf.SidesDay(20, 80, sf.SOURCE_UN, True)}
    f, w, prov = sf.flow_series(days, cal)
    assert f[0] == pytest.approx(0.2)
    assert np.isnan(f[1])                       # 흐름 없는 날은 NaN
    assert f[2] == pytest.approx(-0.6)
    assert w[0] == 100 and w[2] == 100
    assert list(prov) == [False, False, True]


def test_캘린더에_없는_날은_버린다():
    cal = [date(2026, 10, 14)]
    days = {date(2026, 10, 13): sf.SidesDay(1, 1, sf.SOURCE_UN, False)}
    f, _, _ = sf.flow_series(days, cal)
    assert np.isnan(f[0])


# ── 가속 창 파라미터 (사전등록 보호) ────────────────────────

def test_accel_from_flow_기본값은_accel_1_0_그대로다():
    """인자를 비우면 최근3+이전7=10 — VIDYA·TRUECVD 사전등록이 쓴 정의다."""
    rng = np.random.default_rng(7)
    f = rng.normal(0, 0.2, 60)
    got = av.accel_from_flow(f)
    assert (got["recent_days"], got["prior_days"], got["need_days"]) == (3, 7, 10)
    same = av.accel_from_flow(f, 3, 7)
    assert np.allclose(got["accel"], same["accel"], equal_nan=True)
    assert np.array_equal(got["ok"], same["ok"])


def test_창을_15로_주면_최근5_이전10이다():
    f = np.arange(40, dtype=float) / 100.0
    got = av.accel_from_flow(f, 5, 10)
    assert got["need_days"] == 15
    # 등차수열이면 최근5 평균 − 이전10 평균 = 간격 × (5+10)/2 = 0.01 × 7.5
    assert got["accel"][20] == pytest.approx(0.075)


def test_창이_짧으면_앞쪽은_판정하지_않는다():
    f = np.full(20, 0.1)
    got = av.accel_from_flow(f, 5, 10)
    assert not got["ok"][13] and got["ok"][14]
    assert np.isnan(got["accel"][13])


def test_흐름이_끊기면_연속_조건에서_떨어진다():
    f = np.full(30, 0.1)
    f[10] = np.nan
    got = av.accel_from_flow(f, 5, 10)
    assert not got["ok"][20]                    # 10번 결측이 창 안
    assert got["ok"][25]                        # 창이 지나가면 다시 선다


def test_최근_이전이_0이하면_멈춘다():
    with pytest.raises(ValueError):
        av.accel_from_flow(np.zeros(20), 0, 10)


# ── 스캐너 게이트 (sides-1.0) ───────────────────────────────

import cvd_obv_sides_scan as sc                # noqa: E402


def _row(accel=None, cvdobv=None, cvm=0.0, liq=1e9):
    return {"code": "A", "name": "가", "accel": accel, "cvdobv": cvdobv,
            "price": {"close": 100.0, "ma20": 100.0,
                      "close_vs_ma20_pct": cvm,
                      "above_ma20": (cvm is not None and cvm > 0)},
            "liquidity": {"value": liq, "days_used": 20}}


def _acc(a=0.05, recent=0.03):
    return {"accel": a, "flow_recent": recent, "flow_prior": recent - a,
            "recent_days": 5, "prior_days": 10}


def _cvd(nd=0.05, bull=0.6, slope=0.2, obv=10.0, ma=5.0):
    return {"normalized_delta": nd, "bull_ratio": bull, "days_used": 15,
            "obv": obv, "obv_ma20": ma, "obv_normalized_slope": slope}


def test_설계_상수는_15거래일이다():
    assert (sc.RECENT_DAYS, sc.PRIOR_DAYS, sc.WINDOW) == (5, 10, 15)
    assert sc.DESIGN_VERSION == "sides-1.0"


def test_가속_모두_충족하면_후보다():
    st, why = sc.gate_accel(_row(accel=_acc(), cvm=1.0))
    assert st == "BUY_CANDIDATE" and why == []


def test_가속_흐름이_없으면_부족이다():
    st, _ = sc.gate_accel(_row(accel=None))
    assert st == "DATA_INSUFFICIENT"


def test_가속_A가_0이하면_관찰이다():
    st, why = sc.gate_accel(_row(accel=_acc(a=-0.01), cvm=1.0))
    assert st == "WATCH" and any("가속" in w for w in why)


def test_가속_과열이면_관찰이다():
    st, why = sc.gate_accel(_row(accel=_acc(), cvm=sc.OVERHEAT_MAX + 0.1))
    assert st == "WATCH" and any("과열" in w for w in why)


def test_가속은_MA20_아래를_막지_않는다():
    """accel-1.0은 과열만 막는다 — 눌린 종목을 고르는 설계다."""
    st, why = sc.gate_accel(_row(accel=_acc(), cvm=-8.0))
    assert st == "BUY_CANDIDATE" and why == []


def test_일반_모두_충족하면_후보다():
    st, why = sc.gate_cvdobv(_row(cvdobv=_cvd(), cvm=2.0))
    assert st == "BUY_CANDIDATE" and why == []


def test_일반은_MA20_아래를_막는다():
    st, why = sc.gate_cvdobv(_row(cvdobv=_cvd(), cvm=-2.0))
    assert st == "WATCH" and any("MA20" in w for w in why)


def test_일반_매수우위일_비율_하한은_50퍼센트다():
    assert sc.BULL_MIN == 0.50
    st, _ = sc.gate_cvdobv(_row(cvdobv=_cvd(bull=0.50), cvm=2.0))
    assert st == "BUY_CANDIDATE"
    st, why = sc.gate_cvdobv(_row(cvdobv=_cvd(bull=0.49), cvm=2.0))
    assert st == "WATCH" and any("매수우위" in w for w in why)


def test_일반_사유_문구에_값을_넣지_않는다():
    """사유별 집계가 값마다 쪼개지면 안 된다."""
    a = sc.gate_cvdobv(_row(cvdobv=_cvd(bull=0.40), cvm=2.0))[1]
    b = sc.gate_cvdobv(_row(cvdobv=_cvd(bull=0.33), cvm=2.0))[1]
    assert a == b


def test_일반_OBV가_MA20_아래면_관찰이다():
    st, why = sc.gate_cvdobv(_row(cvdobv=_cvd(obv=1.0, ma=5.0), cvm=2.0))
    assert st == "WATCH" and any("OBV" in w for w in why)


def test_유동성이_없으면_둘_다_관찰이다():
    assert sc.gate_accel(_row(accel=_acc(), cvm=1.0, liq=None))[0] == "WATCH"
    assert sc.gate_cvdobv(_row(cvdobv=_cvd(), cvm=2.0, liq=None))[0] == "WATCH"


def test_배점은_60_20_20이다():
    assert sc.SCORE_WEIGHTS == (60, 20, 20)


# ── MA 길이 옵션 (사전등록 MALEN 반영) ──────────────────────

def test_MA_기본값은_20이다():
    """MALEN(대장 11번)에서 10·60이 20을 이기지 못했다 — 기본값을 바꾸지 않는다."""
    assert sc.MA_DAYS == 20
    assert sc.LIQ_DAYS == 20


def test_build_rows는_MA_길이를_인자로_받는다():
    import inspect
    p = inspect.signature(sc.build_rows).parameters
    assert p["ma_days"].default == sc.MA_DAYS


def test_게이트_사유에_실제_MA_길이가_적힌다():
    r = _row(cvdobv=_cvd(), cvm=-2.0)
    r["price"]["ma_days"] = 10
    why = sc.gate_cvdobv(r)[1]
    assert any("MA10" in w for w in why)
    assert not any("MA20" in w for w in why)


def test_과열_사유에도_실제_MA_길이가_적힌다():
    r = _row(accel=_acc(), cvm=sc.OVERHEAT_MAX + 1)
    r["price"]["ma_days"] = 60
    why = sc.gate_accel(r)[1]
    assert any("MA60" in w for w in why)


def test_ma_days가_없는_옛_행도_20으로_읽는다():
    """하위호환 — 예전 JSON에는 ma_days 키가 없다."""
    r = _row(cvdobv=_cvd(), cvm=-2.0)
    r["price"].pop("ma_days", None)
    assert any("MA20" in w for w in sc.gate_cvdobv(r)[1])

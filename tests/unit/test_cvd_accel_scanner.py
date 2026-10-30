#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`tools/cvd_accel_scanner.py` 단위 테스트 — 가속 설계가 검증본과 같은가.

이 테스트가 지키는 것은 세 가지다.

1. **일별 흐름 f_d가 V1.5 계산식과 같다.** 가속 설계의 입력이 기준 구현과
   갈리면 백테스트(+45.1%)와 실거래 스캔이 다른 지표를 보게 된다.
2. **게이트가 백테스트에 쓴 5개 조건 그대로다.** 특히 `종가 > MA20`,
   `OBV > OBV MA20`, `매수우위일 ≥ 50%`를 **일부러 넣지 않았다**는 사실을
   회귀로 고정한다 — 누군가 "당연히 있어야 한다"며 되돌리면 이 설계의
   존재 이유(선행성)가 사라진다.
3. **진행 중인 당일은 흐름에 섞이지 않는다.** 검증은 완료된 정규장으로만
   했으므로 장중 부분값이 들어가면 같은 설계가 아니다.
"""

import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

import cvd_accel_scanner as ac  # noqa: E402

from cvd_obv.baseline import fixtures as fx  # noqa: E402
from cvd_obv.baseline.scanner_v15 import load_scanner_v15  # noqa: E402

END_DATE = "20260916"
AS_OF = datetime(2026, 9, 16, 15, 35, 0)     # 정규장 확정 직후
WINDOW = ac.RECENT_DAYS + ac.PRIOR_DAYS      # 10거래일


@pytest.fixture(scope="module")
def sc():
    return load_scanner_v15()


@pytest.fixture(scope="module")
def case(sc):
    """합성 분봉·일봉 — 완료 거래일 10일을 채우려고 12일을 만든다."""
    return fx.build_case(sc, "bullish", END_DATE, period=1, bdays=WINDOW + 2)


# ─────────────────────────────────────────────────────────────
# 1. 일별 흐름 f_d
# ─────────────────────────────────────────────────────────────

def test_일별_흐름은_V15_calc_cvd의_정규화Delta와_같다(sc, case):
    """f_d를 따로 구현하지 않았음을 확인한다 — 하루치 봉의 normalized_delta다."""
    bars = [b for b in case["minute_bars"]
            if b["bar_end"] <= AS_OF and b["session"] == sc._SESSION_REGULAR]
    flows = ac.daily_flows(sc, bars, period=1, as_of=AS_OF)
    assert flows, "합성 데이터에서 흐름이 하나도 안 나왔다"

    by_day = {}
    for b in bars:
        by_day.setdefault(b["dt"][:8], []).append(b)
    for day, info in flows.items():
        direct = sc.calc_cvd(by_day[day], requested_days=None, period=1,
                             as_of=AS_OF, session=sc._SESSION_REGULAR,
                             min_bars=1, absent_is_error=False)
        assert info["f"] == direct["normalized_delta"]
        assert -1.0 <= info["f"] <= 1.0


def test_흐름은_완료된_정규장만_쓴다(sc, case):
    """장중(11:00)에 돌리면 당일은 창에서 빠진다."""
    intraday = datetime(2026, 9, 16, 11, 0, 0)
    bars = [b for b in case["minute_bars"]
            if b["bar_end"] <= intraday and b["session"] == sc._SESSION_REGULAR]
    flows = ac.daily_flows(sc, bars, period=1, as_of=intraday)
    assert END_DATE not in flows
    assert flows, "직전 거래일들은 남아 있어야 한다"

    acc = ac.accel_from_flows(flows, ac.RECENT_DAYS, ac.PRIOR_DAYS)
    assert acc is not None
    assert acc["flow_as_of"] < END_DATE


def test_애프터마켓_봉은_흐름에_섞이지_않는다(sc, case):
    """검증은 정규장으로만 했다 — 세션이 섞이면 같은 지표가 아니다."""
    reg = [b for b in case["minute_bars"]
           if b["bar_end"] <= AS_OF and b["session"] == sc._SESSION_REGULAR]
    mixed = [b for b in case["minute_bars"] if b["bar_end"] <= AS_OF]
    assert len(mixed) > len(reg), "합성 데이터에 애프터마켓 봉이 있어야 한다"

    only_reg = ac.daily_flows(sc, reg, period=1, as_of=AS_OF)
    with_after = ac.daily_flows(sc, mixed, period=1, as_of=AS_OF)
    # analyze_accel은 정규장만 넘긴다. 섞으면 값이 달라진다는 것을 보여 둔다.
    # 당일(15:35 기준)은 애프터마켓 봉이 아직 없으므로 비교 대상이 아니다 —
    # 애프터마켓 봉이 실제로 붙은 날에서 확인한다.
    differing = [d for d in only_reg
                 if with_after[d]["bars"] > only_reg[d]["bars"]]
    assert differing, "애프터마켓 봉이 붙은 완료 거래일이 있어야 한다"
    for day in differing:
        assert only_reg[day]["f"] != with_after[day]["f"]


# ─────────────────────────────────────────────────────────────
# 2. 가속 정의
# ─────────────────────────────────────────────────────────────

def _flows(values, start_day=1):
    return {f"202609{start_day + i:02d}": {"f": v, "bars": 380, "complete": True}
            for i, v in enumerate(values)}


def test_가속은_최근평균_빼기_이전평균이다():
    flows = _flows([0.0] * 7 + [0.3, 0.3, 0.3])      # 이전 7일 0, 최근 3일 0.3
    acc = ac.accel_from_flows(flows, 3, 7)
    assert acc["flow_prior"] == pytest.approx(0.0)
    assert acc["flow_recent"] == pytest.approx(0.3)
    assert acc["accel"] == pytest.approx(0.3)
    assert len(acc["window_days"]) == 10


def test_창보다_오래된_날은_쓰지_않는다():
    """11일이 있으면 가장 오래된 하루는 버린다(최근 10일 고정)."""
    flows = _flows([9.0] + [0.0] * 7 + [0.1, 0.1, 0.1])
    acc = ac.accel_from_flows(flows, 3, 7)
    assert acc["flow_prior"] == pytest.approx(0.0)   # 9.0이 섞이면 실패한다
    assert len(acc["window_days"]) == 10


def test_거래일_부족이면_가속이_없다():
    assert ac.accel_from_flows(_flows([0.1] * 9), 3, 7) is None


def test_창_크기를_바꾸면_다른_분할이_된다():
    flows = _flows([0.0] * 5 + [0.2] * 5)
    acc = ac.accel_from_flows(flows, 5, 5)
    assert acc["flow_recent"] == pytest.approx(0.2)
    assert acc["accel"] == pytest.approx(0.2)


# ─────────────────────────────────────────────────────────────
# 3. 게이트 — 5개 조건, 그리고 **없는** 조건
# ─────────────────────────────────────────────────────────────

def _row(accel=0.05, recent=0.02, cvm=3.0, liq=5.0e9,
         quality="SUCCESS", above_ma20=True, obv_above=True, code="900001"):
    acc = None
    if accel is not None:
        acc = {"accel": accel, "flow_recent": recent, "flow_prior": recent - accel,
               "window_days": [f"202609{i:02d}" for i in range(1, 11)],
               "flow_as_of": "20260910", "min_bars": 380, "incomplete_days": [],
               "flow_series": []}
    return {
        "code": code, "name": "테스트", "period": 1,
        "recent_days": 3, "prior_days": 7,
        "accel": acc,
        "cvd_quality": {"data_status": quality, "status_reasons": []},
        "price": {"close_vs_ma20_pct": cvm, "above_ma20": above_ma20},
        "liquidity": {"value": liq},
        "obv": {"obv": 2.0 if obv_above else 1.0, "obv_ma20": 1.5},
    }


def test_다섯_조건을_모두_채우면_후보다():
    g = ac.compute_gate(_row())
    assert g["status"] == "BUY_CANDIDATE"
    assert g["gate_reasons"] == []


def test_가속이_0이하면_후보가_아니다():
    g = ac.compute_gate(_row(accel=-0.01, recent=0.02))
    assert g["status"] == "WATCH"
    assert any("가속" in r for r in g["gate_reasons"])


def test_최근흐름이_음수면_가속이_양수여도_후보가_아니다():
    """음수에서 덜 음수로 가는 것은 가속이지만 매수 우위가 아니다."""
    g = ac.compute_gate(_row(accel=0.05, recent=-0.02))
    assert g["status"] == "WATCH"
    assert any("매수 우위" in r for r in g["gate_reasons"])


def test_과열이면_후보가_아니다():
    g = ac.compute_gate(_row(cvm=12.0))
    assert g["status"] == "WATCH"
    assert any("과열" in r for r in g["gate_reasons"])
    # 상한을 풀면 통과한다(민감도 확인 경로)
    assert ac.compute_gate(_row(cvm=12.0), overheat_max=None)["status"] \
        == "BUY_CANDIDATE"


def test_유동성_결측이면_후보가_아니다():
    g = ac.compute_gate(_row(liq=None))
    assert any("유동성" in r for r in g["gate_reasons"])


def test_흐름_미확보나_품질미달은_데이터부족이다():
    assert ac.compute_gate(_row(accel=None))["status"] == "DATA_INSUFFICIENT"
    assert ac.compute_gate(_row(quality="PARTIAL"))["status"] == "DATA_INSUFFICIENT"


def test_MA20_아래여도_OBV가_아래여도_후보가_된다():
    """**의도적 제외의 회귀 테스트.**

    `종가 > MA20`과 `OBV > OBV MA20`을 게이트에 되돌리면 이 테스트가 깨진다.
    두 조건은 "이미 올랐다"를 확인하기 때문에 선행성을 잡으려는 설계에서
    일부러 뺐다(신호 전 상승 종목 비율 97.8% → 55.2%).
    """
    g = ac.compute_gate(_row(cvm=-4.0, above_ma20=False, obv_above=False))
    assert g["status"] == "BUY_CANDIDATE", g["gate_reasons"]


# ─────────────────────────────────────────────────────────────
# 4. 점수·정렬
# ─────────────────────────────────────────────────────────────

def test_점수는_가속60_흐름20_유동성20_백분위다():
    rows = [_row(accel=0.01, recent=0.01, liq=1e9, code="900001"),
            _row(accel=0.02, recent=0.02, liq=2e9, code="900002"),
            _row(accel=0.03, recent=0.03, liq=3e9, code="900003")]
    out = {r["code"]: r for r in ac.finalize(list(rows))}
    assert out["900003"]["total"] == pytest.approx(100.0)   # 세 항목 모두 1위
    assert out["900001"]["total"] == pytest.approx(0.0)     # 세 항목 모두 꼴찌
    assert out["900002"]["total"] == pytest.approx(50.0)    # 중간(0.5×100)


def test_가속_비중이_흐름보다_크다():
    """가속 백분위 60 · 최근흐름 20 — 배점이 뒤집히면 다른 설계다."""
    rows = [_row(accel=0.03, recent=0.01, liq=1e9, code="900001"),
            _row(accel=0.01, recent=0.03, liq=1e9, code="900002")]
    out = {r["code"]: r for r in ac.finalize(list(rows))}
    assert out["900001"]["total"] > out["900002"]["total"]


def test_동점이면_거래대금이_큰_쪽이_앞선다():
    rows = [_row(accel=0.02, recent=0.02, liq=1e9, code="900002"),
            _row(accel=0.02, recent=0.02, liq=9e9, code="900001")]
    ranked = ac.finalize(list(rows))
    assert [r["code"] for r in ranked] == ["900001", "900002"]


def test_미통과는_후보_아래로_내려간다():
    rows = [_row(accel=0.05, recent=0.05, cvm=30.0, code="900001"),  # 과열
            _row(accel=0.01, recent=0.01, code="900002")]
    ranked = ac.finalize(list(rows))
    assert ranked[0]["code"] == "900002"
    assert ranked[0]["status"] == "BUY_CANDIDATE"
    assert ranked[1]["status"] == "WATCH"


# ─────────────────────────────────────────────────────────────
# 5. 종목 하나 통째로 — 네트워크 없이
# ─────────────────────────────────────────────────────────────

def test_analyze_accel은_네트워크_없이_돈다(sc, case):
    original = fx.stub_fetchers(sc, [case])
    try:
        r = ac.analyze_accel(sc, case["code"], token="TEST_NO_TOKEN",
                             name=case["name"], period=1, as_of=AS_OF)
    finally:
        fx.restore_fetchers(sc, original)

    assert r is not None
    assert r["accel"] is not None
    assert r["accel"]["flow_as_of"] == END_DATE      # 15:35 → 당일 확정
    assert len(r["accel"]["window_days"]) == WINDOW
    assert r["cvd_quality"]["data_status"] in ("SUCCESS", "PARTIAL")
    # 참고 지표는 계산하되 게이트에는 쓰지 않는다
    assert "obv" in r and "price" in r
    assert r["period"] == 1


def test_기본값이_검증본과_같다():
    """설계 상수를 조용히 바꾸지 못하게 못 박는다."""
    assert (ac.RECENT_DAYS, ac.PRIOR_DAYS) == (3, 7)
    assert ac.OVERHEAT_MAX == 10.0
    assert ac.DEFAULT_PERIOD == 1

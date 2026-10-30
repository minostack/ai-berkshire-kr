# -*- coding: utf-8 -*-
"""후보 판정 게이트 — 유의하지만 거래할 가치가 없는 결과를 거른다.

여기서 지키려는 것 두 가지.

1. **축을 평균 내지 않는다.** 경제성이 0이면 통계가 아무리 좋아도 FAIL이다.
2. **모르는 것을 통과로 세지 않는다.** 자료가 없으면 PASS가 아니라 UNKNOWN
   이고, 전체는 INSUFFICIENT가 된다.
"""

import pytest

from cvd_obv.domain.errors import ConfigError
from cvd_obv.statistics.economic_gate import (
    FAIL, PASS, UNKNOWN, GateThresholds, annualize, evaluate_candidate,
)


def _full(**over):
    """모든 축을 통과하는 후보. 시험하려는 축만 덮어쓴다."""
    base = {
        "name": "PRICE+OBV vs PRICE",
        "horizon_td": 20,
        "effect": 0.02,            # 2%p — 문턱을 넉넉히 넘는 크기
        "ci_low": 0.01, "ci_high": 0.03,
        "q_value": 0.02,
        "mde": 0.01,
        "cost_per_round_trip": 0.001,
        "replication": {"kind": "REPLICATION", "same_sign": True,
                        "significant": True, "correlation": 0.2},
        "median_daily_value_krw": 5e9,
        "participation_rate": 0.002,
        "brier_score": 0.240,
        "brier_base_rate": 0.250,
    }
    base.update(over)
    return base


# ── 연율 환산 ────────────────────────────────────────────────

def test_연율은_보유기간_횟수만큼_곱한다():
    """20일 보유면 연 12.25회다(245/20). 복리로 부풀리지 않는다."""
    assert annualize(0.000802, 20) == pytest.approx(0.000802 * 245 / 20)


def test_보유기간이_없으면_환산하지_않는다():
    assert annualize(0.01, 0) is None
    assert annualize(None, 20) is None


# ── 전체 판정 ────────────────────────────────────────────────

def test_다_통과하면_PASS다():
    r = evaluate_candidate(_full())
    assert r["verdict"] == PASS
    assert r["blocked_by"] == []
    assert set(r["axes"].values()) == {PASS}


def test_문턱을_결과에_함께_적는다():
    """문턱을 적지 않으면 다음 사람이 어떤 기준으로 통과했는지 모른다."""
    r = evaluate_candidate(_full())
    assert r["thresholds"]["alpha_q"] == 0.05
    assert r["thresholds"]["min_annual_excess_bp"] == 100.0


# ── 통계 축 ──────────────────────────────────────────────────

def test_구간이_0을_포함하면_FAIL이다():
    r = evaluate_candidate(_full(ci_low=-0.001, ci_high=0.03))
    assert r["axes"]["statistical"] == FAIL
    assert r["verdict"] == FAIL


def test_q값이_문턱을_넘으면_FAIL이다():
    r = evaluate_candidate(_full(q_value=0.20))
    assert r["axes"]["statistical"] == FAIL


def test_검출_한계_아래면_유의해도_FAIL이다():
    """관측/MDE가 1 미만이면 '잡을 수 없었던 크기'를 잡았다는 뜻이다.

    선택지 C 후보가 정확히 이 자리(0.86~0.97)에 있었다.
    """
    r = evaluate_candidate(_full(effect=0.008, mde=0.01,
                                 ci_low=0.001, ci_high=0.015))
    assert r["axes"]["statistical"] == FAIL
    assert "MDE" in r["reasons"]["statistical"]


def test_MDE가_없으면_통과가_아니라_UNKNOWN이다():
    r = evaluate_candidate(_full(mde=None))
    assert r["axes"]["statistical"] == UNKNOWN
    assert r["verdict"] == "INSUFFICIENT"


# ── 경제성 축 ────────────────────────────────────────────────

def test_실제_후보는_경제성에서_걸린다():
    """h=20에 +0.0802%p, 왕복 비용 0.05%p면 비용 후 연 3.7bp다.

    이 게이트가 있어야 하는 이유가 이 한 줄이다 — q=0.020으로 FDR을
    통과한 결과가 경제성에서 멈춘다.
    """
    r = evaluate_candidate(_full(effect=0.000802, mde=0.00083,
                                 ci_low=0.000248, ci_high=0.001408,
                                 cost_per_round_trip=0.0005))
    assert r["axes"]["economic"] == FAIL
    assert r["verdict"] == FAIL
    assert "economic" in r["blocked_by"]


def test_비용을_모르면_비용_전_수치로_통과시키지_않는다():
    r = evaluate_candidate(_full(cost_per_round_trip=None))
    assert r["axes"]["economic"] == UNKNOWN
    assert r["verdict"] == "INSUFFICIENT"


def test_비용이_효과보다_크면_FAIL이다():
    r = evaluate_candidate(_full(effect=0.002, cost_per_round_trip=0.005))
    assert r["axes"]["economic"] == FAIL


# ── 안정성 축 ────────────────────────────────────────────────

def test_재현_결과가_없으면_UNKNOWN이다():
    r = evaluate_candidate(_full(replication=None))
    assert r["axes"]["stability"] == UNKNOWN


def test_상관을_재지_않았으면_독립이라_가정하지_않는다():
    """ρ를 재기 전에 '다른 표본에서 나왔다'고 말할 수 없다."""
    r = evaluate_candidate(_full(replication={
        "kind": "REPLICATION", "same_sign": True, "significant": True}))
    assert r["axes"]["stability"] == UNKNOWN
    assert "상관" in r["reasons"]["stability"]


def test_상관이_너무_높으면_재현으로_치지_않는다():
    r = evaluate_candidate(_full(replication={
        "kind": "REPLICATION", "same_sign": True, "significant": True,
        "correlation": 0.85}))
    assert r["axes"]["stability"] == FAIL


def test_부호가_뒤집히면_FAIL이다():
    r = evaluate_candidate(_full(replication={
        "kind": "REPLICATION", "same_sign": False, "significant": True,
        "correlation": 0.1}))
    assert r["axes"]["stability"] == FAIL


# ── 실행가능성 축 ────────────────────────────────────────────

def test_거래대금이_작으면_FAIL이다():
    r = evaluate_candidate(_full(median_daily_value_krw=1e8))
    assert r["axes"]["executability"] == FAIL


def test_참여율이_높으면_FAIL이다():
    """내 주문이 그 종목 거래의 5%면 내가 곧 가격이다."""
    r = evaluate_candidate(_full(participation_rate=0.05))
    assert r["axes"]["executability"] == FAIL


# ── 확률 품질 축 ─────────────────────────────────────────────

def test_base_rate를_못_이기면_FAIL이다():
    r = evaluate_candidate(_full(brier_score=0.260, brier_base_rate=0.250))
    assert r["axes"]["probability"] == FAIL


def test_Brier_비교가_없으면_UNKNOWN이다():
    r = evaluate_candidate(_full(brier_score=None))
    assert r["axes"]["probability"] == UNKNOWN


# ── 합성 규칙 ────────────────────────────────────────────────

def test_FAIL이_UNKNOWN보다_강하다():
    """모르는 축이 있어도, 확실히 떨어진 축이 있으면 FAIL이다."""
    r = evaluate_candidate(_full(median_daily_value_krw=1e8, mde=None))
    assert r["verdict"] == FAIL
    assert r["blocked_by"] == ["executability"]


def test_떨어진_이유가_축별로_남는다():
    r = evaluate_candidate(_full(q_value=0.9, participation_rate=0.5))
    assert set(r["blocked_by"]) == {"statistical", "executability"}
    assert all(r["reasons"][a] for a in r["axes"])


# ── 문턱 검증 ────────────────────────────────────────────────

def test_문턱을_바꿔_끼울_수_있다():
    """사전 등록에서 정하라고 만든 값이다."""
    loose = GateThresholds(min_annual_excess_bp=1.0)
    c = _full(effect=0.000802, mde=0.0008, ci_low=0.000248,
              ci_high=0.001408, cost_per_round_trip=0.0005)
    assert evaluate_candidate(c)["axes"]["economic"] == FAIL
    assert evaluate_candidate(c, loose)["axes"]["economic"] == PASS


def test_말이_안_되는_문턱은_거부한다():
    with pytest.raises(ConfigError):
        GateThresholds(alpha_q=1.5)
    with pytest.raises(ConfigError):
        GateThresholds(min_annual_excess_bp=-10)

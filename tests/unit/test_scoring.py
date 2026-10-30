#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""100점 고정 배점 scoring 단위 테스트 — 가이드 8.2, 8.3."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

import daily_scanner as ds


# ─────────────────────────────────────────────────────────────
# 배점 합계·집계 로직 (가이드 8.3)
# ─────────────────────────────────────────────────────────────

def _full_areas(all_points: bool):
    """trend20+momentum20+volume20+foreign20+pattern10+risk10 = 100.
    all_points=True면 전부 만점, False면 전부 0점(결측 없음)."""
    specs = [("trend", 20), ("momentum", 20), ("volume_liquidity", 20),
             ("foreign_institution", 20), ("pattern_breakout", 10), ("risk", 10)]
    return [{"area": a, "points": m if all_points else 0, "max": m, "missing": []}
            for a, m in specs]


def test_area_weights_sum_to_100():
    areas = _full_areas(True)
    assert sum(a["max"] for a in areas) == 100


def test_all_conditions_met_gives_100():
    result = ds.combine_score(_full_areas(True))
    assert result["status"] == "SUCCESS"
    assert result["total_score"] == 100
    assert result["band"] == "우선 검토"


def test_all_conditions_failed_gives_0():
    result = ds.combine_score(_full_areas(False))
    assert result["status"] == "SUCCESS"
    assert result["total_score"] == 0
    assert result["band"] == "우선순위 낮음"


def test_display_bands_boundaries():
    def band_for(score):
        areas = _full_areas(True)
        # 첫 영역의 points를 조절해 known_points를 원하는 값으로 맞춘다
        areas[0]["points"] = score - sum(a["points"] for a in areas[1:])
        return ds.combine_score(areas)["band"]

    assert band_for(85) == "우선 검토"
    assert band_for(75) == "후보"
    assert band_for(65) == "관찰"
    assert band_for(50) == "중립"
    assert band_for(49) == "우선순위 낮음"


def test_missing_component_marks_incomplete_not_zero():
    areas = _full_areas(True)
    areas[3]["missing"] = ["foreign_5d_sum"]  # foreign_institution 영역 결측
    result = ds.combine_score(areas)
    assert result["status"] == "INCOMPLETE"
    assert result["total_score"] is None
    assert "foreign_institution.foreign_5d_sum" in result["missing_components"]
    # 결측 영역(20점)을 제외한 나머지 80점만 "확인 가능한 가중치"로 잡아야 함
    assert result["available_weight"] == 80


def test_multiple_bull_patterns_still_capped_at_area_max():
    # detect_candle_patterns가 여러 강세 패턴을 동시에 내도 pattern_breakout
    # 영역은 5점(패턴 조건 1개)을 넘지 않는다 — 함수 자체가 가산이 아니라
    # "1개 이상이면 5점" 이진 판정이므로 구조적으로 상한을 넘을 수 없다.
    opens  = [10] * 30 + [110, 99, 100]
    highs  = [10] * 30 + [112, 100, 109]
    lows   = [10] * 30 + [99, 98, 99]
    closes = [10] * 30 + [100, 100, 108]  # 샛별형 패턴 유도
    result = ds.score_pattern_breakout(closes, highs, opens, lows)
    assert result["points"] <= result["max"] == 10


# ─────────────────────────────────────────────────────────────
# 영역별 계산 로직 — 이미 검증된 순수 함수(calc_rsi 등)로 기대값을
# 독립 산출해 score_* 함수의 집계/임계값 적용이 맞는지 교차검증한다.
# ─────────────────────────────────────────────────────────────

def test_score_trend_all_pass_on_clean_uptrend():
    closes = [100 + i * 0.5 for i in range(130)]  # 단조 증가 -> 정배열
    result = ds.score_trend(closes)
    assert result["points"] == 20
    assert result["missing"] == []


def test_score_trend_all_fail_on_clean_downtrend():
    closes = [200 - i * 0.5 for i in range(130)]  # 단조 감소 -> 역배열, MA20 하락, C<MA120
    result = ds.score_trend(closes)
    assert result["points"] == 0


def test_score_trend_insufficient_data_reports_missing():
    closes = [100 + i for i in range(10)]  # MA120은커녕 MA60도 부족
    result = ds.score_trend(closes)
    assert "ma_alignment(MA5/20/60/120)" in result["missing"]
    assert "ma120" in result["missing"]


def test_score_momentum_matches_direct_calc():
    closes = [100 + (i % 20) * 0.3 for i in range(60)]
    highs  = [c + 1 for c in closes]
    lows   = [c - 1 for c in closes]
    rsi  = ds.calc_rsi(closes)
    macd = ds.calc_macd(closes)
    stoch = ds.calc_stochastic(highs, lows, closes)
    expected = 0
    if rsi is not None and 50 <= rsi <= 70:
        expected += 8
    if macd.get("hist") is not None and macd["hist"] > 0:
        expected += 6
    if stoch.get("%K") is not None and stoch.get("%D") is not None \
            and stoch["%K"] > stoch["%D"] and stoch["%K"] < 80:
        expected += 6
    result = ds.score_momentum(closes, highs, lows)
    assert result["points"] == expected
    assert result["points"] <= result["max"]


def test_score_volume_liquidity_ratio_and_turnover():
    volumes = [1000] * 20 + [2500]  # 2.5배 -> 10점
    result = ds.score_volume_liquidity(volumes, turnover_krw=12_000_000_000, turnover_is_estimate=True)
    assert result["points"] == 20  # 거래량 2배 이상(10) + 거래대금 100억 이상(10)
    result_low = ds.score_volume_liquidity(volumes, turnover_krw=1_000_000_000, turnover_is_estimate=False)
    assert result_low["points"] == 10  # 거래대금 미달


def test_score_foreign_institution_all_conditions():
    result = ds.score_foreign_institution(foreign_5d=100, institution_5d=50,
                                           foreign_today=10, institution_today=5)
    assert result["points"] == 20


def test_score_foreign_institution_partial():
    # 외국인 5일합만 양수, 나머지는 미충족
    result = ds.score_foreign_institution(foreign_5d=100, institution_5d=-50,
                                           foreign_today=10, institution_today=-5)
    assert result["points"] == 5


def test_score_foreign_institution_missing_data():
    result = ds.score_foreign_institution(None, None, None, None)
    assert result["points"] == 0
    assert set(result["missing"]) == {"foreign_5d_sum", "institution_5d_sum", "today_foreign_institution"}


def test_score_risk_low_volatility_near_ma20():
    n = 30
    closes = [100.0] * n
    highs  = [101.0] * n  # TR 항상 1~2 -> ATR/종가 << 5%
    lows   = [99.0] * n
    result = ds.score_risk(closes, highs, lows)
    assert result["points"] == 10


def test_score_risk_only_atr_fails_gives_partial_points():
    n = 30
    closes = [100.0] * n
    highs  = [130.0] * n
    lows   = [70.0] * n
    result = ds.score_risk(closes, highs, lows)
    # MA20 이격(|100-100|/100=0%)은 통과하므로 5점은 받아야 함
    assert result["points"] == 5

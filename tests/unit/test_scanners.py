#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""스캐너 순수 계산식 단위 테스트 — 가이드 7.1~7.4, 8.2(ATR)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

import daily_scanner as ds


# ─────────────────────────────────────────────────────────────
# 7.1 volume-surge
# ─────────────────────────────────────────────────────────────

def test_volume_surge_ratio_five_times():
    # 직전 20일 각 100주, 오늘 500주 -> 5배
    volumes = [100] * 20 + [500]
    assert ds.calc_volume_surge_ratio(volumes) == 5.0


def test_volume_surge_ratio_insufficient_history():
    volumes = [100] * 15 + [500]  # 21개 미만
    assert ds.calc_volume_surge_ratio(volumes) is None


def test_volume_surge_ratio_zero_previous_avg_is_none():
    volumes = [0] * 20 + [500]  # 이전 평균 0 -> 무한대 대신 None
    assert ds.calc_volume_surge_ratio(volumes) is None


# ─────────────────────────────────────────────────────────────
# 7.2 new-high
# ─────────────────────────────────────────────────────────────

def test_new_high_excludes_todays_high():
    # 오늘 고가가 기준값에 섞이면 안 됨 — 직전 5개 중 최고 100, 오늘 종가 100은 미돌파
    highs  = [90, 95, 100, 98, 97, 150]  # 마지막(오늘) 150은 기준에서 제외돼야 함
    closes = [0, 0, 0, 0, 0, 100]
    assert ds.calc_new_high_breakout(closes, highs, lookback=5) is False


def test_new_high_breakout_true_when_close_exceeds_reference():
    highs  = [90, 95, 100, 98, 97, 105]
    closes = [0, 0, 0, 0, 0, 101]  # 직전 5개 최고 100 초과
    assert ds.calc_new_high_breakout(closes, highs, lookback=5) is True


def test_new_high_equal_price_is_not_strict_breakout():
    highs  = [90, 95, 100, 98, 97, 105]
    closes = [0, 0, 0, 0, 0, 100]  # 기준값과 동일가 -> strict breakout 아님
    assert ds.calc_new_high_breakout(closes, highs, lookback=5) is False


def test_new_high_insufficient_history_is_none():
    highs  = [90, 95, 100]
    closes = [0, 0, 101]
    assert ds.calc_new_high_breakout(closes, highs, lookback=5) is None


def test_new_high_oldest_bar_is_the_high_boundary_case():
    # 이전 lookback개 중 "가장 오래된" 봉이 최고가인 경계 사례
    highs  = [200, 90, 95, 100, 98, 105]  # 인덱스 0(가장 오래된)이 최고
    closes = [0, 0, 0, 0, 0, 199]
    assert ds.calc_new_high_breakout(closes, highs, lookback=5) is False
    closes2 = [0, 0, 0, 0, 0, 201]
    assert ds.calc_new_high_breakout(closes2, highs, lookback=5) is True


# ─────────────────────────────────────────────────────────────
# 7.3 gap-up
# ─────────────────────────────────────────────────────────────

def test_gap_pct_three_percent():
    assert abs(ds.calc_gap_pct(103, 100) - 3.0) < 1e-9


def test_gap_pct_boundary_inclusive_via_caller():
    # 공식 자체는 정확한 3.0을 반환해야 하며, >= 비교는 호출자가 담당
    assert abs(ds.calc_gap_pct(103, 100) - 3.0) < 1e-9


def test_gap_pct_zero_prev_close_is_none():
    assert ds.calc_gap_pct(103, 0) is None


def test_gap_pct_none_prev_close_is_none():
    assert ds.calc_gap_pct(103, None) is None


# ─────────────────────────────────────────────────────────────
# 7.4 oversold
# ─────────────────────────────────────────────────────────────

def test_oversold_streak_three_days_declining():
    closes = [100, 90, 80, 70]  # 70<80<90<100 -> 3일 연속 하락
    assert ds.calc_oversold_streak(closes, days=3) is True


def test_oversold_streak_flat_breaks_streak():
    closes = [100, 90, 90, 70]  # 중간 보합 -> 연속 하락 아님
    assert ds.calc_oversold_streak(closes, days=3) is False


def test_oversold_streak_up_breaks_streak():
    closes = [100, 90, 95, 70]
    assert ds.calc_oversold_streak(closes, days=3) is False


def test_oversold_streak_invalid_days_is_none():
    assert ds.calc_oversold_streak([100, 90, 80], days=0) is None


def test_oversold_streak_insufficient_data_is_none():
    assert ds.calc_oversold_streak([100, 90], days=5) is None


def test_return_nd_matches_expected_pct():
    closes = [100, 95, 90, 85, 80, 76]  # 5일 수익률: 76/100-1 = -24%
    assert abs(ds.calc_return_nd(closes, days=5) - (-24.0)) < 1e-9


# ─────────────────────────────────────────────────────────────
# 8.2 ATR14 (Wilder)
# ─────────────────────────────────────────────────────────────

def _reference_atr(highs, lows, closes, period=14):
    """calc_atr14와 독립적으로 다시 구현한 Wilder ATR (교차검증용)."""
    trs = []
    for i in range(1, len(closes)):
        h, l, cp = highs[i], lows[i], closes[i - 1]
        trs.append(max(h - l, abs(h - cp), abs(l - cp)))
    atr = sum(trs[:period]) / period
    for t in trs[period:]:
        atr = (atr * (period - 1) + t) / period
    return atr


def test_atr14_matches_independent_reference():
    n = 30
    highs  = [110 + (i % 5) for i in range(n)]
    lows   = [90 - (i % 3) for i in range(n)]
    closes = [100 + (i % 7) for i in range(n)]
    expected = _reference_atr(highs, lows, closes, period=14)
    actual = ds.calc_atr14(highs, lows, closes, period=14)
    assert abs(actual - expected) < 1e-9


def test_atr14_insufficient_data_is_none():
    assert ds.calc_atr14([100, 101], [99, 98], [100, 100], period=14) is None


def test_atr14_constant_range_is_stable():
    n = 20
    highs  = [110.0] * n
    lows   = [90.0] * n
    closes = [100.0] * n
    # TR은 항상 max(20, 10, 10)=20 이므로 ATR도 항상 20
    assert ds.calc_atr14(highs, lows, closes, period=14) == 20.0

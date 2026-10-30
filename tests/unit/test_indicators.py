#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/daily_scanner.py의 순수 계산 함수 단위 테스트.

daily_scanner_kiwoom_migration_guide.md 6.1~6.4, 5.2절의 테스트 벡터를 그대로
사용한다. 기대값은 계산 대상 함수와 독립적으로(별도 로직으로) 산출한다 —
같은 구현을 복사해 비교하면 구현 버그가 그대로 "통과"로 보일 수 있다.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

import daily_scanner as ds


# ─────────────────────────────────────────────────────────────
# RSI (Wilder) — 가이드 6.1
# ─────────────────────────────────────────────────────────────

def _reference_wilder_rsi(closes, period=14):
    """calc_rsi와 별개로 다시 구현한 Wilder RSI (교차검증용)."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(d if d > 0 else 0.0)
        losses.append(-d if d < 0 else 0.0)
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    idx = period
    while idx < len(gains):
        ag = (ag * (period - 1) + gains[idx]) / period
        al = (al * (period - 1) + losses[idx]) / period
        idx += 1
    if ag == 0 and al == 0:
        return 50.0
    if al == 0:
        return 100.0
    if ag == 0:
        return 0.0
    rs = ag / al
    return 100 - (100 / (1 + rs))


def test_rsi_monotonic_up_is_100():
    closes = [100 + i for i in range(20)]  # 연속 상승
    assert ds.calc_rsi(closes, period=14) == 100.0


def test_rsi_monotonic_down_is_0():
    closes = [200 - i for i in range(20)]  # 연속 하락
    assert ds.calc_rsi(closes, period=14) == 0.0


def test_rsi_flat_is_50():
    closes = [100.0] * 20  # 완전 보합
    assert ds.calc_rsi(closes, period=14) == 50.0


def test_rsi_matches_independent_wilder_reference():
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84,
              46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41,
              46.22, 45.64]
    expected = _reference_wilder_rsi(closes, period=14)
    actual = ds.calc_rsi(closes, period=14)
    assert abs(actual - round(expected, 2)) < 0.05


def test_rsi_insufficient_data_returns_none():
    assert ds.calc_rsi([1, 2, 3], period=14) is None


def test_rsi_invalid_period_returns_none():
    assert ds.calc_rsi([1, 2, 3, 4, 5], period=0) is None


# ─────────────────────────────────────────────────────────────
# 추정 매수/매도 압력 (구 CVD) — 가이드 6.2
# ─────────────────────────────────────────────────────────────

def _flat_bar():
    return (100.0, 100.0, 100.0, 100.0, 1000)  # O,H,L,C,V — range 0


def test_volume_pressure_bullish_delta():
    # O=100,H=110,L=90,C=105,V=1000 -> pressure=(105-100)/20=0.25 -> delta=250
    fo, fh, fl, fc, fv = _flat_bar()
    opens  = [fo, 100]
    highs  = [fh, 110]
    lows   = [fl, 90]
    closes = [fc, 105]
    vols   = [fv, 1000]
    result = ds.calc_volume_pressure(opens, highs, lows, closes, vols)
    assert result["pressure"] == 250


def test_volume_pressure_bearish_delta():
    # 같은 범위, O=105,C=100 -> delta=-250
    fo, fh, fl, fc, fv = _flat_bar()
    opens  = [fo, 105]
    highs  = [fh, 110]
    lows   = [fl, 90]
    closes = [fc, 100]
    vols   = [fv, 1000]
    result = ds.calc_volume_pressure(opens, highs, lows, closes, vols)
    assert result["pressure"] == -250


def test_volume_pressure_doji_zero_range_is_zero_delta():
    # 고저 동일(range=0) -> pressure=0 -> delta=0
    opens  = [100, 100]
    highs  = [100, 100]
    lows   = [100, 100]
    closes = [100, 100]
    vols   = [1000, 1000]
    result = ds.calc_volume_pressure(opens, highs, lows, closes, vols)
    assert result["pressure"] == 0


def test_volume_pressure_zero_volume_is_zero_delta():
    fo, fh, fl, fc, fv = _flat_bar()
    opens  = [fo, 100]
    highs  = [fh, 110]
    lows   = [fl, 90]
    closes = [fc, 105]
    vols   = [fv, 0]
    result = ds.calc_volume_pressure(opens, highs, lows, closes, vols)
    assert result["pressure"] == 0


def test_volume_pressure_array_length_and_ma20_boundary():
    n = 25
    opens  = [100.0] * n
    highs  = [110.0] * n
    lows   = [90.0] * n
    closes = [105.0] * n  # 매 봉 동일한 양의 pressure
    vols   = [1000] * n
    result = ds.calc_volume_pressure(opens, highs, lows, closes, vols)
    # 20개 이상 데이터가 있으면 MA20이 계산되어야 한다
    assert result["pressure_ma20"] is not None
    # 매 봉 pressure=0.25, delta=250 누적이므로 n봉 후 값은 n*250
    assert result["pressure"] == n * 250


def test_volume_pressure_insufficient_data_returns_none():
    result = ds.calc_volume_pressure([100], [110], [90], [105], [1000])
    assert result["pressure"] is None
    assert result["data_status"] == "INSUFFICIENT_DATA"


def test_volume_pressure_invalid_ohlc_flagged_not_guessed():
    # low > high인 비정상 봉 — 값을 조작해 채우지 않고 이슈로만 기록
    fo, fh, fl, fc, fv = _flat_bar()
    opens  = [fo, 100]
    highs  = [fh, 90]   # high < low (비정상)
    lows   = [fl, 110]
    closes = [fc, 105]
    vols   = [fv, 1000]
    result = ds.calc_volume_pressure(opens, highs, lows, closes, vols)
    assert result["pressure"] == 0  # 해당 봉은 압력 0으로 처리
    assert result["data_issues"] == 1


# ─────────────────────────────────────────────────────────────
# 샛별형 (Morning Star) — 가이드 6.3
# ─────────────────────────────────────────────────────────────

def test_morning_star_minimal_pattern_detected():
    # 첫 3봉은 채움용(패턴 계산에 관여하지 않음), 마지막 3봉이 실제 패턴
    opens  = [10, 10, 110, 99, 100]
    highs  = [10, 10, 112, 100, 109]
    lows   = [10, 10, 99,  98,  99]
    closes = [10, 10, 100, 100, 108]
    patterns = ds.detect_candle_patterns(opens, highs, lows, closes)
    names = [p[0] for p in patterns]
    assert any("샛별형" in n for n in names)


def test_morning_star_second_body_too_large_not_detected():
    # 둘째 몸통이 첫째의 30% 초과 -> 감지되지 않아야 함
    opens  = [10, 10, 110, 90, 100]
    highs  = [10, 10, 112, 100, 109]
    lows   = [10, 10, 99,  85,  99]
    closes = [10, 10, 100, 100, 108]  # body2 = |100-90| = 10 > body1*0.3=3
    patterns = ds.detect_candle_patterns(opens, highs, lows, closes)
    names = [p[0] for p in patterns]
    assert not any("샛별형" in n for n in names)


def test_morning_star_third_close_below_midpoint_not_detected():
    opens  = [10, 10, 110, 99, 100]
    highs  = [10, 10, 112, 100, 103]
    lows   = [10, 10, 99,  98,  99]
    closes = [10, 10, 100, 100, 102]  # midpoint=(110+100)/2=105, 102<105
    patterns = ds.detect_candle_patterns(opens, highs, lows, closes)
    names = [p[0] for p in patterns]
    assert not any("샛별형" in n for n in names)


def test_morning_star_first_body_zero_not_detected():
    # 첫째 몸통이 0이면(body1==0) 분모/판정 자체가 성립하지 않아야 함
    opens  = [10, 10, 100, 99, 100]
    highs  = [10, 10, 105, 100, 109]
    lows   = [10, 10, 95,  98,  99]
    closes = [10, 10, 100, 100, 108]  # c1==o1 -> body1=0
    patterns = ds.detect_candle_patterns(opens, highs, lows, closes)
    names = [p[0] for p in patterns]
    assert not any("샛별형" in n for n in names)


# ─────────────────────────────────────────────────────────────
# Slow Stochastic (14,3,3) — 가이드 6.4
# ─────────────────────────────────────────────────────────────

def _synthetic_series(n):
    """고저가 벌어지는 합성 시계열 — FastK가 항상 정의되도록 한다."""
    closes = [100 + (i % 10) for i in range(n)]
    highs  = [c + 5 for c in closes]
    lows   = [c - 5 for c in closes]
    return highs, lows, closes


def test_stochastic_below_k_period_is_none():
    highs, lows, closes = _synthetic_series(13)  # k_period=14 미만
    result = ds.calc_stochastic(highs, lows, closes)
    assert result["%K"] is None and result["%D"] is None


def test_stochastic_fastk_available_but_slowk_not_yet():
    # 14~15개: FastK는 나오지만 SlowK(smooth_k=3)는 16개부터 유효
    highs, lows, closes = _synthetic_series(15)
    result = ds.calc_stochastic(highs, lows, closes)
    assert result["%K"] is None  # SlowK 워밍업 미충족


def test_stochastic_slowk_available_at_16():
    highs, lows, closes = _synthetic_series(16)
    result = ds.calc_stochastic(highs, lows, closes)
    assert result["%K"] is not None
    assert result["%D"] is None  # SlowD(d_period=3)는 18개부터


def test_stochastic_slowd_available_at_18():
    highs, lows, closes = _synthetic_series(18)
    result = ds.calc_stochastic(highs, lows, closes)
    assert result["%K"] is not None
    assert result["%D"] is not None


def test_stochastic_zero_range_gives_50():
    n = 20
    closes = [100.0] * n
    highs  = [100.0] * n
    lows   = [100.0] * n  # 매 구간 최고=최저 -> FastK=50 고정
    result = ds.calc_stochastic(highs, lows, closes)
    assert result["%K"] == 50.0
    assert result["%D"] == 50.0


# ─────────────────────────────────────────────────────────────
# 이동평균 정배열 판정 — 가이드 5.2
# ─────────────────────────────────────────────────────────────

def test_ma_alignment_insufficient_data_when_any_missing():
    ma = {"ma5": 100, "ma20": 95, "ma60": None, "ma120": 80}
    assert ds.ma_alignment_status(ma) == "INSUFFICIENT_DATA"


def test_ma_alignment_all_missing_is_insufficient_not_aligned():
    # all([])==True 함정 회귀 방지 — 전부 없을 때 ALIGNED로 잘못 판정되면 안 됨
    ma = {"ma5": None, "ma20": None, "ma60": None, "ma120": None}
    assert ds.ma_alignment_status(ma) == "INSUFFICIENT_DATA"


def test_ma_alignment_aligned():
    ma = {"ma5": 130, "ma20": 120, "ma60": 110, "ma120": 100}
    assert ds.ma_alignment_status(ma) == "ALIGNED"


def test_ma_alignment_not_aligned():
    ma = {"ma5": 100, "ma20": 120, "ma60": 110, "ma120": 130}
    assert ds.ma_alignment_status(ma) == "NOT_ALIGNED"


# ─────────────────────────────────────────────────────────────
# 구조화 신호 코드 — 가이드 9단계("중립/상승/하락·결측을 구조화 필드로
# 관리하고 문자열은 출력에만 사용"). calc_obv/calc_volume_pressure가
# 반환하는 trend/divergence/vs_ma20은 고정된 코드값이어야 하며, 완성된
# 한국어 문장이어서는 안 된다(문자열을 다시 파싱하지 않도록).
# ─────────────────────────────────────────────────────────────

def test_obv_trend_is_a_code_not_a_sentence():
    closes = [100 + i for i in range(10)]   # 단조 증가 -> OBV도 상승
    volumes = [1000] * 10
    result = ds.calc_obv(closes, volumes)
    assert result["trend"] == ds.TREND_UP
    assert result["trend"] in (ds.TREND_UP, ds.TREND_DOWN, ds.TREND_FLAT, ds.TREND_UNKNOWN)


def test_obv_divergence_bullish_code():
    # 가격은 하락, OBV는 상승하도록 구성 -> 강세 다이버전스 코드
    closes = [100 - i * 0.1 for i in range(25)]
    volumes = [1000 + i * 50 for i in range(25)]  # 하락일에도 거래량이 커지게
    result = ds.calc_obv(closes, volumes)
    if result["divergence"] is not None:
        assert result["divergence"] in (ds.DIVERGENCE_BULLISH, ds.DIVERGENCE_BEARISH)
        assert "다이버전스" not in str(result["divergence"])  # 완성 문장이 아니라 코드


def test_obv_vs_ma20_is_above_or_below_not_a_sentence():
    closes = [100 + i for i in range(25)]
    volumes = [1000] * 25
    result = ds.calc_obv(closes, volumes)
    assert result["vs_ma20"] in ("above", "below")


def test_volume_pressure_trend_and_divergence_are_codes():
    n = 25
    opens  = [100.0] * n
    highs  = [110.0] * n
    lows   = [90.0] * n
    closes = [105.0] * n  # 항상 양의 pressure -> 누적 우상향
    vols   = [1000] * n
    result = ds.calc_volume_pressure(opens, highs, lows, closes, vols)
    assert result["trend"] in (ds.TREND_UP, ds.TREND_DOWN, ds.TREND_FLAT, ds.TREND_UNKNOWN)
    assert result["vs_ma20"] in ("above", "below", None)
    if result["divergence"] is not None:
        assert result["divergence"] in (ds.DIVERGENCE_BULLISH, ds.DIVERGENCE_BEARISH)


def test_pattern_strength_is_a_code_not_korean_text():
    # 샛별형 패턴 고정 fixture (test_morning_star_minimal_pattern_detected와 동일)
    opens  = [10, 10, 110, 99, 100]
    highs  = [10, 10, 112, 100, 109]
    lows   = [10, 10, 99,  98,  99]
    closes = [10, 10, 100, 100, 108]
    patterns = ds.detect_candle_patterns(opens, highs, lows, closes)
    star = [p for p in patterns if "샛별형" in p[0]][0]
    assert star[1] == ds.PATTERN_BULLISH
    assert star[1] != "강세"  # 한국어 문자열이 아니라 코드여야 함

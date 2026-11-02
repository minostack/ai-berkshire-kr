#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
당일 투자 후보 스캐너 — 기술적 분석 + 증권사 API
daily-scanner 스킬이 자동 호출합니다.

사용법:
    python3 tools/daily_scanner.py check-market          # 장 운영 여부 확인
    python3 tools/daily_scanner.py check-broker          # API 연결 상태 확인
    python3 tools/daily_scanner.py quote 005930          # KIS 현재가/기본정보 조회
    python3 tools/daily_scanner.py volume-surge          # 거래량 급증 종목
    python3 tools/daily_scanner.py new-high              # 신고가 돌파 종목
    python3 tools/daily_scanner.py gap-up                # 갭 상승 종목
    python3 tools/daily_scanner.py oversold              # 과매도 반등 후보
    python3 tools/daily_scanner.py indicators 005930     # 기술적 지표 계산
    python3 tools/daily_scanner.py patterns 005930       # 캔들 패턴 인식
    python3 tools/daily_scanner.py kis-realtime 005930   # quote의 옛 이름

데이터 원천 (2026-11-01 KIS 전환 — 적재 원천과 같은 KIS):
    일봉:   로컬 정규장 일봉(봉 기준 bars-1.0) → KIS 일봉 FHKST03010100(기준 밖) → Yahoo(기준 밖)
    시세:   KIS 현재가 FHKST01010100 / 수급: KIS 투자자 FHKST01010900(30일치 한 번에)
    순위:   KIS 거래량순위 FHPST01710000(거래증가율) — volume-surge 후보 풀
    신고가: 로컬 기준 일봉으로 직접 계산(코스피200·코스닥150) — KIS에 종가 기준 N일
            신고가 순위 TR이 없다(FHPST01870000은 '근접' 순위라 정의가 다르다)
    키움 함수(kiwoom_*)는 다른 도구(closing_bet_scanner --source kiwoom 등)용으로만 남는다

증권사 API 키 설정 — 프로젝트 루트 .env 파일 (.env.example 참고):
    KIS:  KIS_APP_KEY=... / KIS_APP_SECRET=...
    키움: KIWOOM_APP_KEY=... / KIWOOM_APP_SECRET=... (이 도구의 명령은 쓰지 않음)
"""

import argparse
import json
import os
import subprocess
import sys
import math
import io
import random
import tempfile
import time
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, Union

# ── .env 로더: 프로젝트 루트의 .env를 os.environ에 주입 ──
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))
except ImportError:
    pass  # python-dotenv 미설치 시 OS 환경변수만 사용

_TIMEOUT = 20

# 봉 기준 bars-1.0(`src/cvd_obv/bar_standard.py`) — 일봉은 로컬 정규장·수정주가 봉
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))


def _today_str() -> str:
    """오늘 날짜(YYYY-MM-DD)를 호출 시점 기준으로 반환한다.

    이전에는 모듈 import 시점에 한 번만 계산한 `_TODAY` 상수를 재사용했다
    (가이드 9단계 — "실행 중 현재 시각을 매번 평가"). 프로세스가 자정을
    넘겨 계속 실행되거나(장시간 백그라운드 스캔 등) 테스트에서 모듈을
    한 번만 import하고 여러 날짜를 시뮬레이션할 때 값이 고착되는 문제를
    막기 위해 매 호출마다 다시 계산한다.
    """
    return datetime.now().strftime("%Y-%m-%d")


# ─────────────────────────────────────────────────────────────
# 공통 유틸
# ─────────────────────────────────────────────────────────────

def _curl(url: str, headers: list = None) -> str:
    cmd = ["curl", "-s", "--noproxy", "*",
           "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)"]
    if headers:
        for h in headers:
            cmd += ["-H", h]
    cmd.append(url)
    result = subprocess.run(cmd, capture_output=True, timeout=_TIMEOUT)
    if result.returncode != 0:
        raise ConnectionError(f"요청 실패: {url}")
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return result.stdout.decode("euc-kr", errors="replace")


def _curl_json(url: str, headers: list = None) -> Union[dict, list]:
    return json.loads(_curl(url, headers))


def _fmt(v, suffix="") -> str:
    if v is None:
        return "-"
    try:
        v = float(str(v).replace(",", ""))
        if abs(v) >= 1e12:
            return f"{v/1e12:.1f}조{suffix}"
        if abs(v) >= 1e8:
            return f"{v/1e8:.0f}억{suffix}"
        return f"{v:,.0f}{suffix}"
    except (ValueError, TypeError):
        return str(v)


# ─────────────────────────────────────────────────────────────
# 기술적 지표 계산 (순수 Python — 외부 라이브러리 불필요)
# ─────────────────────────────────────────────────────────────

def calc_rsi(closes: list, period: int = 14) -> Optional[float]:
    """RSI 계산 — Wilder 방식(재귀 평활).

    기존 구현은 전체 delta 중 양수만 모아 마지막 period개를 단순 평균해
    상승일·하락일을 따로 추출하면서 기간이 왜곡되는 문제가 있었다(가이드
    6.1). 아래는 표준 Wilder RSI: 최초 avg_gain/avg_loss는 첫 period개
    delta의 단순평균, 이후는 지수적으로(가중치 1/period) 갱신한다.

    loss만 0이면 100, gain만 0이면 0, 둘 다 0(완전 보합)이면 프로젝트
    규칙으로 50을 반환한다. 반올림은 최종 결과에서만 한다.
    """
    if period < 1 or len(closes) < period + 1:
        return None

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_gain == 0 and avg_loss == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def calc_macd(closes: list, fast=12, slow=26, signal=9) -> dict:
    """MACD 계산."""
    def ema(data, period):
        if len(data) < period:
            return None
        k = 2 / (period + 1)
        val = sum(data[:period]) / period
        for p in data[period:]:
            val = p * k + val * (1 - k)
        return val

    if len(closes) < slow:
        return {"macd": None, "signal": None, "hist": None}

    fast_ema  = ema(closes, fast)
    slow_ema  = ema(closes, slow)
    if fast_ema is None or slow_ema is None:
        return {"macd": None, "signal": None, "hist": None}

    macd_val = fast_ema - slow_ema

    # Signal은 최근 N일 MACD의 EMA
    macd_series = []
    for i in range(slow, len(closes) + 1):
        fe = ema(closes[:i], fast)
        se = ema(closes[:i], slow)
        if fe and se:
            macd_series.append(fe - se)

    sig_val  = ema(macd_series, signal) if len(macd_series) >= signal else None
    hist_val = (macd_val - sig_val) if sig_val else None

    return {
        "macd":   round(macd_val, 4) if macd_val else None,
        "signal": round(sig_val, 4)  if sig_val  else None,
        "hist":   round(hist_val, 4) if hist_val else None,
    }


def calc_bollinger(closes: list, period=20, std_mult=2.0) -> dict:
    """볼린저 밴드 계산."""
    if len(closes) < period:
        return {"upper": None, "middle": None, "lower": None, "pct_b": None}

    recent   = closes[-period:]
    middle   = sum(recent) / period
    variance = sum((x - middle) ** 2 for x in recent) / period
    std      = math.sqrt(variance)
    upper    = middle + std_mult * std
    lower    = middle - std_mult * std
    last     = closes[-1]
    pct_b    = (last - lower) / (upper - lower) if upper != lower else 0.5

    return {
        "upper":  round(upper, 2),
        "middle": round(middle, 2),
        "lower":  round(lower, 2),
        "pct_b":  round(pct_b, 4),
    }


def calc_ma(closes: list, periods: list = [5, 20, 60, 120]) -> dict:
    """이동평균 계산."""
    result = {}
    for p in periods:
        if len(closes) >= p:
            result[f"ma{p}"] = round(sum(closes[-p:]) / p, 2)
        else:
            result[f"ma{p}"] = None
    return result


def ma_alignment_status(ma: dict, periods: list = [5, 20, 60, 120]) -> str:
    """정배열 판정 상태 (가이드 5.2).

    `all([])`가 True가 되는 함정 때문에 기존 구현은 MA가 전부 없어도
    "정배열"로 표시될 수 있었다(비교 대상이 하나도 없으면 all()이 그냥
    True를 반환). periods 전부가 존재해야만 ALIGNED/NOT_ALIGNED를
    판정하고, 하나라도 없으면 INSUFFICIENT_DATA를 반환한다.
    """
    values = [ma.get(f"ma{p}") for p in periods]
    if any(v is None for v in values):
        return "INSUFFICIENT_DATA"
    if all(values[i] > values[i + 1] for i in range(len(values) - 1)):
        return "ALIGNED"
    return "NOT_ALIGNED"


# ─────────────────────────────────────────────────────────────
# 스캐너 순수 계산식 (가이드 7단계 — 4단계: 실제 조건 계산)
# 각 함수는 API 호출 없이 이미 수집된 배열만으로 판정한다. 오늘 봉이
# 배열의 마지막 원소라고 가정한다(완료 봉 여부는 호출자가 결정).
# ─────────────────────────────────────────────────────────────

def calc_volume_surge_ratio(volumes: list) -> Optional[float]:
    """가이드 7.1: 이전 20개 완료 봉 평균 대비 오늘 거래량 비율.

    previous_avg = sum(volumes[-21:-1]) / 20   (오늘을 제외한 직전 20개)
    ratio = volumes[-1] / previous_avg

    이전 20개 확보 못하면 None. previous_avg==0이면 비율을 무한대로
    만들지 않고 None(판정 불가)을 반환한다.
    """
    if len(volumes) < 21:
        return None
    previous_avg = sum(volumes[-21:-1]) / 20
    if previous_avg == 0:
        return None
    return volumes[-1] / previous_avg


def calc_new_high_breakout(closes: list, highs: list, lookback: int) -> Optional[bool]:
    """가이드 7.2: 오늘 고가를 기준값에서 제외한 "종가 확정 돌파" 판정.

    reference_high = max(highs[-(lookback+1):-1])   (오늘 고가 제외, 직전 lookback개)
    matched = closes[-1] > reference_high            (동일가는 strict breakout 제외)

    lookback+1개의 봉을 확보하지 못하면 None(판정 불가).
    """
    if lookback < 1 or len(highs) < lookback + 1 or len(closes) < 1:
        return None
    reference_high = max(highs[-(lookback + 1):-1])
    return closes[-1] > reference_high


def calc_gap_pct(today_open: float, prev_close: float) -> Optional[float]:
    """가이드 7.3: 갭 비율 = (오늘 시가 / 전일 종가 - 1) * 100.

    전일 종가가 0/None이면 계산 불가(None)."""
    if not prev_close or today_open is None:
        return None
    return (today_open / prev_close - 1) * 100


def calc_oversold_streak(closes: list, days: int) -> Optional[bool]:
    """가이드 7.4: `days`일 연속 하락(보합 포함 시 하락으로 보지 않음) 판정.

    declining = all(closes[-i] < closes[-i-1] for i in range(1, days+1))
    days<1이면 입력 오류로 None. days+1개 종가를 확보 못하면 None(판정 불가).
    """
    if days < 1:
        return None
    if len(closes) < days + 1:
        return None
    return all(closes[-i] < closes[-i - 1] for i in range(1, days + 1))


def calc_return_nd(closes: list, days: int) -> Optional[float]:
    """가이드 7.4: n일 수익률 = (closes[-1]/closes[-days-1] - 1) * 100."""
    if days < 1 or len(closes) < days + 1:
        return None
    base = closes[-days - 1]
    if not base:
        return None
    return (closes[-1] / base - 1) * 100


def calc_atr14(highs: list, lows: list, closes: list, period: int = 14) -> Optional[float]:
    """Wilder ATR (가이드 8.2 Risk 배점용).

    TR[t] = max(H[t]-L[t], |H[t]-Cprev[t]|, |L[t]-Cprev[t]|)
    ATR은 RSI와 동일한 Wilder 재귀 평활(최초 period개 TR 단순평균, 이후
    (이전ATR*(period-1)+TR)/period)로 계산한다. 첫 TR은 전일 종가가
    필요하므로 입력은 최소 period+1개의 봉이 있어야 한다.
    """
    n = min(len(highs), len(lows), len(closes))
    if period < 1 or n < period + 1:
        return None

    trs = []
    for i in range(1, n):
        h, l, c_prev = highs[i], lows[i], closes[i - 1]
        trs.append(max(h - l, abs(h - c_prev), abs(l - c_prev)))

    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    return atr


def _sma(values: list, period: int) -> list:
    """단순이동평균 시계열. 처음 (period-1)개는 None(워밍업 부족)."""
    out = []
    for i in range(len(values)):
        if i < period - 1:
            out.append(None)
        else:
            window = values[i - period + 1:i + 1]
            out.append(sum(window) / period)
    return out


def calc_stochastic(highs, lows, closes, k_period=14, smooth_k=3, d_period=3) -> dict:
    """Slow Stochastic(기본 14,3,3) 계산.

    기존 구현은 Raw %K를 한 번만 평활한 값을 "%D"라고 표시해 14,3,3 중
    실제로는 두 단계(FastK→SlowK)만 계산하고 있었다(가이드 6.4). 여기서는
    FastK → SlowK(=SMA(FastK, smooth_k)) → SlowD(=SMA(SlowK, d_period))
    세 단계를 모두 계산한다. 반환하는 "%K"/"%D"는 SlowK/SlowD다.
    고저 차이가 0이면 FastK=50으로 둔다(중간 계산은 반올림하지 않음).
    골든/데드크로스는 전일 SlowK/SlowD와 당일 값을 비교해서만 판정한다.
    """
    if len(closes) < k_period:
        return {"%K": None, "%D": None, "cross": None}

    fast_k = []
    for i in range(k_period - 1, len(closes)):
        period_highs = highs[i - k_period + 1:i + 1]
        period_lows  = lows[i - k_period + 1:i + 1]
        highest_high = max(period_highs)
        lowest_low   = min(period_lows)
        if highest_high == lowest_low:
            fast_k.append(50.0)
        else:
            fast_k.append((closes[i] - lowest_low) / (highest_high - lowest_low) * 100)

    slow_k = _sma(fast_k, smooth_k)
    slow_d = _sma([v for v in slow_k if v is not None], d_period)
    # slow_d는 slow_k의 None(워밍업) 구간을 제외하고 계산했으므로,
    # slow_k와 같은 길이로 맞추기 위해 앞쪽을 None으로 채운다.
    pad = len(slow_k) - len(slow_d)
    slow_d = [None] * pad + slow_d

    cur_k, cur_d = slow_k[-1], slow_d[-1]
    prev_k = slow_k[-2] if len(slow_k) >= 2 else None
    prev_d = slow_d[-2] if len(slow_d) >= 2 else None

    cross = None
    if None not in (cur_k, cur_d, prev_k, prev_d):
        if prev_k <= prev_d and cur_k > cur_d:
            cross = "golden"
        elif prev_k >= prev_d and cur_k < cur_d:
            cross = "dead"

    return {
        "%K":   round(cur_k, 2) if cur_k is not None else None,
        "%D":   round(cur_d, 2) if cur_d is not None else None,
        "cross": cross,
    }


# ─────────────────────────────────────────────────────────────
# 구조화 신호 코드 (가이드 9단계 — "중립/상승/하락·결측을 구조화 필드로
# 관리하고 문자열은 출력에만 사용한다"). 계산 함수는 아래 코드값만
# 반환하고, 사람이 읽는 한국어 문구는 cmd_*() 출력 시점에만 만든다 —
# 이전에는 "강세"/"약세" 같은 단어가 박힌 완성 문장을 반환해서 호출부가
# `"강세" in text` 식으로 문자열을 다시 파싱해야 했다(취약한 패턴).
# ─────────────────────────────────────────────────────────────
TREND_UP, TREND_DOWN, TREND_FLAT, TREND_UNKNOWN = "up", "down", "flat", "unknown"
DIVERGENCE_BULLISH, DIVERGENCE_BEARISH = "bullish", "bearish"
PATTERN_BULLISH, PATTERN_BEARISH = "bullish", "bearish"
PATTERN_NEUTRAL, PATTERN_CAUTION = "neutral", "caution"


def calc_obv(closes: list, volumes: list) -> dict:
    """OBV(On-Balance Volume) 계산.

    종가가 전일보다 오르면 거래량 +, 내리면 -, 같으면 0 누적.
    가격-OBV 다이버전스로 추세 반전 신호 포착.
    """
    if len(closes) < 2 or len(volumes) < 2:
        return {"obv": None, "obv_ma20": None, "trend": TREND_UNKNOWN,
                "vs_ma20": None, "divergence": None, "series": None,
                "data_status": "INSUFFICIENT_DATA"}

    obv_series = [0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i-1]:
            obv_series.append(obv_series[-1] + volumes[i])   # 상승 → 거래량 더함
        elif closes[i] < closes[i-1]:
            obv_series.append(obv_series[-1] - volumes[i])   # 하락 → 거래량 뺌
        else:
            obv_series.append(obv_series[-1])                 # 보합 → 변화 없음

    current_obv = obv_series[-1]

    # OBV 20일 이동평균
    obv_ma20 = sum(obv_series[-20:]) / 20 if len(obv_series) >= 20 else None

    # 추세 판단 (최근 5일 OBV 기울기)
    if len(obv_series) >= 5:
        obv_slope = obv_series[-1] - obv_series[-5]
        trend = TREND_UP if obv_slope > 0 else (TREND_DOWN if obv_slope < 0 else TREND_FLAT)
    else:
        trend = TREND_UNKNOWN

    # 다이버전스 탐지 (최근 20일 기준)
    divergence = None
    if len(closes) >= 20 and len(obv_series) >= 20:
        price_chg = closes[-1] - closes[-20]
        obv_chg   = obv_series[-1] - obv_series[-20]
        if price_chg > 0 and obv_chg < 0:
            divergence = DIVERGENCE_BEARISH  # 가격 상승 + OBV 하락 → 상승 추세 약화
        elif price_chg < 0 and obv_chg > 0:
            divergence = DIVERGENCE_BULLISH  # 가격 하락 + OBV 상승 → 하락 추세 약화

    vs_ma20 = None
    if obv_ma20 is not None:
        vs_ma20 = "above" if current_obv > obv_ma20 else "below"

    return {
        "obv":        round(current_obv),
        "obv_ma20":   round(obv_ma20) if obv_ma20 is not None else None,
        "trend":      trend,
        "vs_ma20":    vs_ma20,
        "divergence": divergence,
        "series":     obv_series,   # 다이버전스 추가 계산용
        "data_status": "SUCCESS",
    }


def calc_volume_pressure(opens: list, highs: list, lows: list,
                          closes: list, volumes: list) -> dict:
    """일봉 기반 추정 매수/매도 압력 (가이드 6.2 — 이전의 "CVD 근사"를 개명).

    실제 체결 데이터 없이는 진짜 CVD를 복원할 수 없다(가이드 3번 보완사항).
    이 함수는 명시적으로 "추정치"이며 실제 체결 분류 기반 CVD가 아니다.

    range[t] = high[t] - low[t]
    pressure[t] = 0                             if range==0
    pressure[t] = (close[t]-open[t]) / range[t]  otherwise   (정상 OHLC면 -1~1)
    estimated_delta[t] = volume[t] * pressure[t]
    cumulative_pressure[t] = sum(estimated_delta[0:t+1])

    누적 초기값은 루프 밖 0에서 시작하고, 입력 N개와 결과 N개 길이를
    맞춘다(과거 버전은 초기 0을 배열에 함께 넣어 N+1 길이가 됐었음).
    OHLC 정합성이 깨진 봉(low<=min(o,c)<=max(o,c)<=high 위반)은
    pressure를 0으로 두고 data_issues에 기록한다(값을 조작하지 않음).
    """
    n = min(len(opens), len(highs), len(lows), len(closes), len(volumes))
    if n < 2:
        return {
            "pressure": None, "pressure_ma20": None, "trend": TREND_UNKNOWN,
            "vs_ma20": None, "divergence": None, "data_issues": 0,
            "data_status": "INSUFFICIENT_DATA",
            "note": "⚠️ 체결 데이터가 아닌 일봉 캔들 기반 추정 매수/매도 압력 — 실제 CVD 아님",
        }

    cumulative = []
    running = 0.0
    data_issues = 0
    for i in range(n):
        o, h, l, c, v = opens[i], highs[i], lows[i], closes[i], volumes[i]
        rng = h - l
        valid_ohlc = rng > 0 and l <= min(o, c) <= max(o, c) <= h
        if rng > 0 and valid_ohlc:
            pressure = (c - o) / rng
        else:
            pressure = 0.0
            if rng < 0 or (rng > 0 and not valid_ohlc):
                data_issues += 1
        running += v * pressure
        cumulative.append(running)

    current = cumulative[-1]
    ma20 = sum(cumulative[-20:]) / 20 if len(cumulative) >= 20 else None

    # 5구간(5일) 변화는 x[-1]-x[-6] (x[-1]-x[-5]는 4구간 차이 — 가이드 보완 5)
    if len(cumulative) >= 6:
        slope = cumulative[-1] - cumulative[-6]
        trend = TREND_UP if slope > 0 else (TREND_DOWN if slope < 0 else TREND_FLAT)
    else:
        slope = None
        trend = TREND_UNKNOWN

    divergence = None
    if len(closes) >= 20 and len(cumulative) >= 20:
        price_chg = closes[-1] - closes[-20]
        pres_chg  = cumulative[-1] - cumulative[-20]
        if price_chg > 0 and pres_chg < 0:
            divergence = DIVERGENCE_BEARISH  # 가격 상승 + 추정압력 하락 → 매수세 이탈
        elif price_chg < 0 and pres_chg > 0:
            divergence = DIVERGENCE_BULLISH  # 가격 하락 + 추정압력 상승 → 저점 매집

    vs_ma20 = None
    if ma20 is not None:
        vs_ma20 = "above" if current > ma20 else "below"

    return {
        "pressure":      round(current),
        "pressure_ma20": round(ma20) if ma20 is not None else None,
        "trend":         trend,
        "vs_ma20":       vs_ma20,
        "divergence":    divergence,
        "data_issues":   data_issues,
        "data_status":   "SUCCESS",
        "note":          "⚠️ 체결 데이터가 아닌 일봉 캔들 기반 추정 매수/매도 압력 — 실제 CVD 아님",
    }


def detect_candle_patterns(opens, highs, lows, closes) -> list:
    """주요 캔들 패턴 인식."""
    signals = []
    if len(closes) < 3:
        return signals

    o1, h1, l1, c1 = opens[-3], highs[-3], lows[-3], closes[-3]
    o2, h2, l2, c2 = opens[-2], highs[-2], lows[-2], closes[-2]
    o,  h,  l,  c  = opens[-1], highs[-1], lows[-1], closes[-1]

    body      = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    prev_body  = abs(c2 - o2)

    # 망치형 (Hammer) — 하락 후 반전 신호
    if lower_wick >= body * 2 and upper_wick <= body * 0.3 and c > o:
        signals.append(("🔨 망치형", PATTERN_BULLISH, "하락 후 반전 가능성"))

    # 상승 장악형 (Bullish Engulfing)
    if c2 < o2 and c > o and o < c2 and c > o2:
        signals.append(("📈 상승 장악형", PATTERN_BULLISH, "전일 음봉 완전 포함"))

    # 도지 (Doji) — 방향 불확실
    if body <= (h - l) * 0.1:
        signals.append(("⚖️ 도지", PATTERN_NEUTRAL, "매수·매도 균형, 방향 전환 가능"))

    # 교수형 (Hanging Man) — 고점 주의
    if lower_wick >= body * 2 and c < h * 0.98:
        signals.append(("⚠️ 교수형", PATTERN_CAUTION, "고점에서의 반전 가능성"))

    # 샛별형 (Morning Star) — 3일 패턴 (가이드 6.3 최소 수정)
    # 기존 버그: 둘째 몸통을 자기 자신의 0.3배와 비교(prev_body <= prev_body*0.3은
    # prev_body==0이 아니면 항상 거짓)해 사실상 패턴이 감지되지 않았다.
    # 첫째 몸통(body1)과 비교하도록 수정한다.
    body1 = abs(c1 - o1)
    if c1 < o1 and body1 > 0 and prev_body <= body1 * 0.3 and c > o and c > (o1 + c1) / 2:
        signals.append(("🌟 샛별형", PATTERN_BULLISH, "3일 바닥 반전 패턴 (최소 조건 — 선행 추세·갭 조건 미반영)"))

    return signals


# ─────────────────────────────────────────────────────────────
# 100점 고정 배점 scoring — 가이드 8단계 (V1, 검증 전 초기안)
#
# 이 모델은 백테스트로 검증된 전략이 아니다. 가중치·임계값은 가이드 8.2의
# 초기 설계 가설을 그대로 코드화한 것이며, 매수 지시가 아니다.
# 각 영역은 반드시 최대치를 넘지 않고, 합계는 정확히 100이다. 필요한
# 원본 데이터가 없는 영역은 0점이 아니라 "결측"으로 별도 표시해 —
# 확인 불가와 조건 미충족(0점)을 구분한다(가이드 8.3).
# ─────────────────────────────────────────────────────────────

SCORE_VERSION = "v1-unvalidated-2026-09"


def score_trend(closes: list) -> dict:
    """Trend 20점: 정배열 10 + MA20 상승 기울기 5 + 종가>MA120 5."""
    ma = calc_ma(closes)
    alignment = ma_alignment_status(ma)
    points, missing = 0, []

    if alignment == "INSUFFICIENT_DATA":
        missing.append("ma_alignment(MA5/20/60/120)")
    elif alignment == "ALIGNED":
        points += 10

    ma20_now = ma.get("ma20")
    ma20_series = _sma(closes, 20)  # 길이 = len(closes), 워밍업 구간 None
    ma20_5ago = ma20_series[-6] if len(ma20_series) >= 6 else None
    if ma20_now is None or ma20_5ago is None:
        missing.append("ma20_slope(5봉 전 대비)")
    elif ma20_now > ma20_5ago:
        points += 5

    ma120 = ma.get("ma120")
    if ma120 is None:
        missing.append("ma120")
    elif closes[-1] > ma120:
        points += 5

    return {"area": "trend", "points": points, "max": 20, "missing": missing}


def score_momentum(closes: list, highs: list, lows: list) -> dict:
    """Momentum 20점: 50<=RSI<=70 8 + MACD hist>0 6 + (SlowK>SlowD, K<80) 6."""
    points, missing = 0, []

    rsi = calc_rsi(closes)
    if rsi is None:
        missing.append("rsi")
    elif 50 <= rsi <= 70:
        points += 8

    macd = calc_macd(closes)
    if macd.get("hist") is None:
        missing.append("macd_hist")
    elif macd["hist"] > 0:
        points += 6

    stoch = calc_stochastic(highs, lows, closes)
    if stoch.get("%K") is None or stoch.get("%D") is None:
        missing.append("stochastic")
    elif stoch["%K"] > stoch["%D"] and stoch["%K"] < 80:
        points += 6

    return {"area": "momentum", "points": points, "max": 20, "missing": missing}


def score_volume_liquidity(volumes: list, turnover_krw: Optional[float],
                            turnover_is_estimate: bool) -> dict:
    """Volume/Liquidity 20점: 20일평균 대비 거래량 2배 이상 10 + 거래대금 100억 이상 10."""
    points, missing = 0, []

    ratio = calc_volume_surge_ratio(volumes)
    if ratio is None:
        missing.append("volume_ratio_20d")
    elif ratio >= 2.0:
        points += 10

    if turnover_krw is None:
        missing.append("turnover")
    elif turnover_krw >= 10_000_000_000:  # 100억원
        points += 10

    return {"area": "volume_liquidity", "points": points, "max": 20,
            "missing": missing, "turnover_is_estimate": turnover_is_estimate}


def score_foreign_institution(foreign_5d: Optional[float], institution_5d: Optional[float],
                               foreign_today: Optional[float], institution_today: Optional[float]) -> dict:
    """Foreign/Institution 20점: 외국인5일합>0 5 + 기관5일합>0 5 + 당일 양쪽 순매수 10."""
    points, missing = 0, []

    if foreign_5d is None:
        missing.append("foreign_5d_sum")
    elif foreign_5d > 0:
        points += 5

    if institution_5d is None:
        missing.append("institution_5d_sum")
    elif institution_5d > 0:
        points += 5

    if foreign_today is None or institution_today is None:
        missing.append("today_foreign_institution")
    elif foreign_today > 0 and institution_today > 0:
        points += 10

    return {"area": "foreign_institution", "points": points, "max": 20, "missing": missing}


def score_pattern_breakout(closes: list, highs: list, opens: list, lows: list) -> dict:
    """Pattern/Breakout 10점: 이전 20봉 고가 종가 돌파 5 + 강세 패턴 1개 이상 5.

    강세 패턴 판정은 detect_candle_patterns의 "모양만" 감지한다(선행
    추세 조건은 가이드 6.3에서도 미반영으로 명시된 항목 — 여기서도
    동일하게 반영하지 않았다는 점을 결과에 남긴다).
    """
    points, missing = 0, []

    breakout = calc_new_high_breakout(closes, highs, lookback=20)
    if breakout is None:
        missing.append("breakout_20bar")
    elif breakout:
        points += 5

    if len(closes) < 3:
        missing.append("candle_patterns")
    else:
        patterns = detect_candle_patterns(opens, highs, lows, closes)
        bull_patterns = [p for p in patterns if p[1] == PATTERN_BULLISH]
        if bull_patterns:
            points += 5

    return {"area": "pattern_breakout", "points": points, "max": 10, "missing": missing,
            "note": "선행 추세 조건 미반영 — 패턴 모양만 감지(가이드 6.3과 동일한 한계)"}


def score_risk(closes: list, highs: list, lows: list) -> dict:
    """Risk 10점: ATR14/종가<=5% 5 + |종가-MA20|/MA20<=10% 5 (위험이 낮을 때 가점)."""
    points, missing = 0, []

    atr = calc_atr14(highs, lows, closes, period=14)
    if atr is None or not closes[-1]:
        missing.append("atr14")
    elif (atr / closes[-1]) <= 0.05:
        points += 5

    ma20 = calc_ma(closes, periods=[20]).get("ma20")
    if ma20 is None or ma20 == 0:
        missing.append("ma20_deviation")
    elif abs(closes[-1] - ma20) / ma20 <= 0.10:
        points += 5

    return {"area": "risk", "points": points, "max": 10, "missing": missing}


def combine_score(areas: list) -> dict:
    """영역별 결과를 합산해 최종 점수/결측 처리를 만든다 (가이드 8.3).

    필수 데이터가 모두 있으면 0~100 total_score를 반환한다. 결측 항목이
    있으면 status=INCOMPLETE로 표시하고 total_score=None을 반환한다 —
    확인된 항목만 100점으로 재환산하지 않는다(완전 데이터 종목과
    같은 기준으로 비교할 수 없다는 원칙).
    """
    total_max = sum(a["max"] for a in areas)
    assert total_max == 100, f"배점 합계가 100이 아님: {total_max}"

    missing_components = []
    for a in areas:
        for m in a["missing"]:
            missing_components.append(f"{a['area']}.{m}")

    known_points = sum(a["points"] for a in areas)

    if missing_components:
        # 결측 없는 영역의 배점 합 = "확인 가능했던" 최대 배점. 결측이 있는
        # 영역은 해당 영역 전체를 불확실로 간주해 available_weight에서 뺀다.
        available_weight = sum(a["max"] for a in areas if not a["missing"])
        return {
            "status": "INCOMPLETE",
            "total_score": None,
            "known_points": known_points,
            "available_weight": available_weight,
            "missing_components": missing_components,
            "areas": areas,
            "score_version": SCORE_VERSION,
        }

    band = None
    for threshold, label in [(85, "우선 검토"), (75, "후보"), (65, "관찰"), (50, "중립")]:
        if known_points >= threshold:
            band = label
            break
    if band is None:
        band = "우선순위 낮음"

    return {
        "status": "SUCCESS",
        "total_score": known_points,
        "band": band,
        "areas": areas,
        "score_version": SCORE_VERSION,
    }


# ─────────────────────────────────────────────────────────────
# 가격 데이터 수집
# ─────────────────────────────────────────────────────────────

_WATCHLIST_FILE     = os.path.join(os.path.dirname(__file__), "..", "data", "watchlist.json")


def _known_yahoo_suffix(ticker: str) -> Optional[str]:
    """종목 마스터(`data/index_stocks.json`, `cvd_obv.universe.current_universe`)에서
    시장을 확인할 수 있으면 해당 접미사를 반환한다. 마스터에 없으면 None —
    코드 패턴만으로 시장을 추측하지 않는다(가이드 5.3 원칙)."""
    try:
        from cvd_obv.universe import current_universe
        idx = current_universe().index_of(ticker)
    except Exception:
        return None
    return {"KOSPI200": ".KS", "KOSDAQ150": ".KQ"}.get(idx)


def _fetch_yahoo_chart(yf_ticker: str, days: int) -> Optional[dict]:
    """단일 Yahoo 티커에 대한 원시 차트 조회 (접미사 결정은 호출자 책임)."""
    end_ts   = int(datetime.now().timestamp())
    start_ts = int((datetime.now() - timedelta(days=days)).timestamp())
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_ticker}"
        f"?period1={start_ts}&period2={end_ts}&interval=1d"
    )
    try:
        data   = _curl_json(url)
        chart  = data.get("chart", {}).get("result", [{}])[0]
        ts_arr = chart.get("timestamp", [])
        q      = chart.get("indicators", {}).get("quote", [{}])[0]
        if not ts_arr:
            return None

        rows = []
        for i, ts in enumerate(ts_arr):
            o = q.get("open",  [None]*len(ts_arr))[i]
            h = q.get("high",  [None]*len(ts_arr))[i]
            l = q.get("low",   [None]*len(ts_arr))[i]
            c = q.get("close", [None]*len(ts_arr))[i]
            v = q.get("volume",[None]*len(ts_arr))[i]
            # None(값 없음)만 걸러낸다 — 0은 유효할 수 있는 값이라 truthy
            # 검사로 함께 버리지 않는다(가이드 결측 처리: None과 0 구분).
            if None in (o, h, l, c, v):
                continue
            if not (o > 0 and h > 0 and l > 0 and c > 0 and v >= 0
                    and l <= min(o, c) <= max(o, c) <= h):
                continue
            rows.append({
                "date": datetime.fromtimestamp(ts).strftime("%Y-%m-%d"),
                "open": o, "high": h, "low": l, "close": c, "volume": v
            })

        if len(rows) < 20:
            return None

        return {
            "name":    chart.get("meta", {}).get("shortName", yf_ticker),
            "rows":    rows,
            "opens":   [r["open"]   for r in rows],
            "highs":   [r["high"]   for r in rows],
            "lows":    [r["low"]    for r in rows],
            "closes":  [r["close"]  for r in rows],
            "volumes": [r["volume"] for r in rows],
        }
    except Exception:
        return None


def fetch_ohlcv_yahoo(ticker: str, days: int = 130) -> Optional[dict]:
    """Yahoo Finance에서 OHLCV 데이터 수집 (로컬·KIS 일봉이 없을 때 폴백).

    시장 판정 우선순위(가이드 5.3 — 코드 패턴만으로 추측하지 않음):
      1) data/index_stocks.json 종목 마스터에 있으면 그 시장의 접미사 사용
      2) 마스터에 없으면 .KS로 먼저 조회하고, 실패하면 .KQ로 재조회해
         "실제 응답이 있는 쪽"을 채택한다(추측이 아니라 실측 확인).
    이미 접미사가 붙은 티커(6자리 숫자가 아닌 경우)는 그대로 사용한다.
    """
    if not (ticker.isdigit() and len(ticker) == 6):
        result = _fetch_yahoo_chart(ticker, days)
        if result:
            result["ticker"] = ticker
        return result

    known = _known_yahoo_suffix(ticker)
    candidates = [known] if known else [".KS", ".KQ"]

    for suffix in candidates:
        result = _fetch_yahoo_chart(f"{ticker}{suffix}", days)
        if result:
            result["ticker"] = ticker
            return result
    return None


# ─────────────────────────────────────────────────────────────
# 키움증권 REST API — 2026-11-01부터 이 도구의 명령은 쓰지 않는다(KIS로 전환).
# closing_bet_scanner(--source kiwoom)·계약 테스트가 kiwoom_* 함수를 직접 부르므로 남긴다.
#
# 인증: POST /oauth2/token (api-id 없음), grant_type=client_credentials,
#       appkey/secretkey. 조회: 모든 TR이 POST + JSON body + api-id 헤더.
# 여기 구현된 TR(ka10001, ka10081)은 tools/kiwoom_cvd_obv_scanner.py와
# tools/update_index.py에서 이미 실제 키로 검증된 것만 사용한다.
# 확인되지 않은 TR(투자자 수급, 거래량 순위 등)은 추측해서 구현하지
# 않고 미지원으로 남겨둔다 — 잘못된 TR로 조용히 빈 결과를 반환하는
# 것보다 명시적으로 미지원임을 알리는 편이 안전하다.
# ─────────────────────────────────────────────────────────────

_KIWOOM_BASE        = "https://api.kiwoom.com"        # 실전투자
_KIWOOM_BASE_MOCK   = "https://mockapi.kiwoom.com"    # 모의투자
_KIWOOM_TOKEN_CACHE = os.path.join(os.path.dirname(__file__), "..", ".kiwoom_token.json")
_KIWOOM_MAX_RETRIES   = 3
_KIWOOM_RETRY_BACKOFF = [1, 2, 4]  # 초 (+ jitter)


def _kiwoom_base_url() -> str:
    return _KIWOOM_BASE_MOCK if os.environ.get("KIWOOM_ENV", "").lower() == "mock" else _KIWOOM_BASE


def _kiwoom_get_token() -> Optional[str]:
    """키움 REST API 토큰 발급 (캐시 활용, tools/kiwoom_cvd_obv_scanner.py와 공유)."""
    app_key    = os.environ.get("KIWOOM_APP_KEY")
    app_secret = os.environ.get("KIWOOM_APP_SECRET")
    if not app_key or not app_secret:
        return None

    if os.path.exists(_KIWOOM_TOKEN_CACHE):
        try:
            with open(_KIWOOM_TOKEN_CACHE) as f:
                cache = json.load(f)
            if cache.get("expires_at", 0) > time.time() + 60:
                return cache.get("token") or cache.get("access_token")
        except Exception:
            pass

    body = json.dumps({
        "grant_type": "client_credentials",
        "appkey":     app_key,
        "secretkey":  app_secret,
    })
    result = subprocess.run(
        ["curl", "-s", "-X", "POST", f"{_kiwoom_base_url()}/oauth2/token",
         "-H", "Content-Type: application/json;charset=UTF-8",
         "-H", "Accept: application/json",
         "-d", body],
        capture_output=True, timeout=_TIMEOUT
    )
    raw = result.stdout.strip()
    if not raw:
        _log_kiwoom_error("au10001", {}, "토큰 발급 빈 응답")
        return None
    try:
        data  = json.loads(raw)
        token = data.get("token") or data.get("access_token")
        if not token:
            _log_kiwoom_error("au10001", {}, f"토큰 필드 없음(return_msg={data.get('return_msg', '')})")
            return None
        # 응답 만료 필드명은 문서/실측 기준 token_expired(초); expires_in도 대비
        expires = int(data.get("token_expired", data.get("expires_in", 86400)))
        with open(_KIWOOM_TOKEN_CACHE, "w") as f:
            json.dump({"token": token, "expires_at": time.time() + expires}, f)
        return token
    except Exception as e:
        _log_kiwoom_error("au10001", {}, f"토큰 응답 파싱 실패({type(e).__name__})")
        return None


def _log_kiwoom_error(api_id: str, body: dict, reason: str, retry_count: int = 0) -> None:
    """키움 API 호출 실패를 stderr에 구조화 로그로 남긴다 (가이드 9단계:
    "오류에 종목·작업·API ID·시각·재시도 수를 남기며 인증정보는 제거한다").

    body에서 종목코드(stk_cd)만 추출해 남기고, appkey/secretkey/token 등
    인증정보는 절대 로그에 포함하지 않는다(이 함수는 body 전체를 찍지
    않고 stk_cd 한 필드만 뽑아 쓴다).
    """
    subject = body.get("stk_cd", "N/A") if isinstance(body, dict) else "N/A"
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    print(f"[kiwoom-error] ts={ts} api_id={api_id} subject={subject} "
          f"retry={retry_count} reason={reason}", file=sys.stderr)


def _kiwoom_request(path: str, body: dict, api_id: str, token: str,
                     extra_headers: list = None, _retry: int = 0) -> Optional[dict]:
    """키움 REST API POST 호출.

    연속조회(next-key/cont-yn)는 응답 "헤더"로 내려오므로 별도로 캡처해
    `_next_key`/`_cont_yn`으로 반환 dict에 실어준다. 일시적 오류(curl
    자체 실패·빈 응답)만 지수 backoff로 재시도하고, JSON 파싱 실패나
    API가 명시한 업무 오류(return_code != 0)는 재시도하지 않는다.
    재시도를 모두 소진하거나 재시도 불가능한 오류를 만나면
    `_log_kiwoom_error`로 stderr에 남긴다(인증정보 제외).
    """
    app_key = os.environ.get("KIWOOM_APP_KEY", "")
    url     = f"{_kiwoom_base_url()}{path}"
    cmd = ["curl", "-s", "--noproxy", "*", "-X", "POST", url,
           "-H", f"authorization: Bearer {token}",
           "-H", f"api-id: {api_id}",
           "-H", f"appkey: {app_key}",
           "-H", "Content-Type: application/json;charset=UTF-8"]
    if extra_headers:
        for h in extra_headers:
            cmd += ["-H", h]
    cmd += ["-d", json.dumps(body)]

    def _retry_or_none(reason: str):
        if _retry < _KIWOOM_MAX_RETRIES:
            time.sleep(_KIWOOM_RETRY_BACKOFF[_retry] + random.uniform(0, 0.3))
            return _kiwoom_request(path, body, api_id, token, extra_headers, _retry=_retry + 1)
        _log_kiwoom_error(api_id, body, reason, retry_count=_retry)
        return None

    header_path = None
    try:
        fd, header_path = tempfile.mkstemp(suffix=".hdr")
        os.close(fd)
        cmd += ["-D", header_path]
        result = subprocess.run(cmd, capture_output=True, timeout=_TIMEOUT)

        if result.returncode != 0:
            return _retry_or_none(f"curl 프로세스 실패(exit={result.returncode})")

        raw = result.stdout.strip()
        if not raw:
            return _retry_or_none("빈 응답")
        try:
            data = json.loads(raw)
        except Exception:
            _log_kiwoom_error(api_id, body, "JSON 파싱 실패", retry_count=_retry)
            return None

        rc = data.get("return_code", 0)
        if rc not in (0, "0", None, "00000"):
            _log_kiwoom_error(api_id, body,
                               f"업무 오류(return_code={rc}, return_msg={data.get('return_msg', '')})",
                               retry_count=_retry)
            return None

        try:
            with open(header_path, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if ":" not in line:
                        continue
                    k, _, v = line.partition(":")
                    k = k.strip().lower()
                    v = v.strip()
                    if k == "next-key":
                        data["_next_key"] = v
                    elif k == "cont-yn":
                        data["_cont_yn"] = v
        except OSError:
            pass

        return data
    except subprocess.TimeoutExpired:
        return _retry_or_none("요청 타임아웃")
    finally:
        if header_path and os.path.exists(header_path):
            try:
                os.remove(header_path)
            except OSError:
                pass


def _kiwoom_num(v) -> float:
    """키움 가격류 필드는 등락 방향 표시용 +/- 부호가 절대가격에도 붙어
    내려온다(예: 저가가 "-52700"). 절댓값으로 변환한다. 등락률·순매수처럼
    부호 자체가 의미 있는 필드에는 사용하지 않는다."""
    try:
        return abs(float(str(v).replace(",", "").replace("+", "")))
    except (ValueError, TypeError):
        return 0.0


def _kiwoom_signed(v) -> Optional[float]:
    """부호가 의미를 갖는 필드(등락률·전일대비 등)는 부호를 보존해 변환한다."""
    try:
        return float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return None


def kiwoom_quote(ticker: str) -> Optional[dict]:
    """키움 ka10001(주식기본정보요청)로 현재가·시가·고저·전일종가·거래량·시가총액 조회.

    필드 매핑은 공식 API 스펙(2026-09-14 첨부, kiwoom-rest-api-spec.json)의
    ka10001 responseIo 기준: mac(시가총액, 단위 억원), base_pric(기준가=전일종가,
    pred_pre=전일대비와의 차액으로 실측 대조 확인).
    """
    token = _kiwoom_get_token()
    if not token:
        return None
    data = _kiwoom_request("/api/dostk/stkinfo", {"stk_cd": ticker}, "ka10001", token)
    if not data:
        return None

    mac_eok = _kiwoom_signed(data.get("mac"))  # 억원 단위

    return {
        "symbol":      ticker,
        "name":        str(data.get("stk_nm", "")).strip(),
        "price":       _kiwoom_num(data.get("cur_prc")),
        "open":        _kiwoom_num(data.get("open_pric")),
        "high":        _kiwoom_num(data.get("high_pric")),
        "low":         _kiwoom_num(data.get("low_pric")),
        "prev_close":  _kiwoom_num(data.get("base_pric")),
        "volume":      int(_kiwoom_num(data.get("trde_qty"))),
        "change":      _kiwoom_signed(data.get("pred_pre")),
        "change_pct":  _kiwoom_signed(data.get("flu_rt")),
        "market_cap_eok": mac_eok,                                   # 억원 단위 원본값
        "market_cap_krw": mac_eok * 1e8 if mac_eok is not None else None,  # 원 단위 환산
        "as_of":       datetime.now().isoformat(timespec="seconds"),
        "source":      "kiwoom",
    }


def kiwoom_investor_flow(ticker: str, date: Optional[str] = None) -> Optional[dict]:
    """키움 ka10059(종목별투자자기관별요청)로 지정일 개인/외국인/기관 순매수(주) 조회.

    unit_tp=1(단주)·trde_tp=0(순매수)·amt_qty_tp=2(수량)로 고정 요청한다.
    date 미지정 시 오늘 날짜로 조회한다(휴장일이면 데이터가 없을 수 있음 —
    이 경우 None을 반환하며, 며칠 전 데이터를 임의로 대신 채우지 않는다).
    """
    token = _kiwoom_get_token()
    if not token:
        return None
    date = date or _today_str().replace("-", "")
    body = {
        "dt": date, "stk_cd": ticker,
        "amt_qty_tp": "2", "trde_tp": "0", "unit_tp": "1",
    }
    data = _kiwoom_request("/api/dostk/stkinfo", body, "ka10059", token)
    if not data:
        return None
    rows = data.get("stk_invsr_orgn", [])
    if not rows:
        return None
    row = rows[0]
    return {
        "date":          row.get("dt", date),
        "individual":    _kiwoom_signed(row.get("ind_invsr")),
        "foreign":       _kiwoom_signed(row.get("frgnr_invsr")),
        "institution":   _kiwoom_signed(row.get("orgn")),
        "unit":          "주",
        "source":        "kiwoom",
    }


_KIWOOM_MAX_DAILY_PAGES     = 20
_KIWOOM_MIN_PER_TRADING_DAY = 390


def _is_bar_date_complete(date_str: str, as_of: datetime) -> bool:
    """`date_str`(YYYY-MM-DD) 거래일 일봉을 `as_of`에 받았을 때 확정값인가.

    봉 기준 bars-1.0 `classify_fetched_daily`를 따른다. 당일 봉은 정규장 확정
    (15:35) 뒤에만 완료이고, 애프터마켓이 있는 날은 16:00~20:05(애프터마켓 진행 중)
    값을 다시 미완료로 본다 — 이전에는 15:30 뒤면 완료로 봐서 17:00에 받은 진행 중
    애프터마켓 가격이 확정 봉으로 쓰였다. 실제 휴장일력은 조회하지 않는다.
    """
    from cvd_obv.bar_standard import FETCH_INCOMPLETE, classify_fetched_daily
    day = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
    if day > as_of.date():
        return False
    return classify_fetched_daily(day, as_of) != FETCH_INCOMPLETE


def kiwoom_daily_bars(ticker: str, days: int = 460,
                       as_of: Optional[datetime] = None,
                       include_incomplete: bool = False) -> list:
    """키움 ka10081(주식일봉조회)로 최근 `days` 캘린더일의 일봉을 연속조회
    (next-key/cont-yn) 페이지네이션으로 수집한다. 오름차순 정렬, 중복 제거.

    데이터 검증(가이드 5.2): low<=min(open,close)<=max(open,close)<=high,
    가격 양수, 거래량 0 이상을 만족하지 않는 행은 값을 임의로 고치지 않고
    건너뛴다(제외 개수는 로그로만 남김 — 조용히 삼키지 않되, 추측으로 채우지도 않음).

    include_incomplete=False(기본)면 당일 장중 미완료 봉(정규장 마감
    15:30 이전)을 결과에서 제외한다 — 기본 종가 스캔은 완료 봉만 사용
    한다는 가이드 원칙에 따른 것이다. 실시간 참고용으로 당일 봉이
    필요하면 True로 호출한다.
    """
    token = _kiwoom_get_token()
    if not token:
        return []

    as_of = as_of or datetime.now()
    end   = as_of.strftime("%Y%m%d")
    start = (as_of - timedelta(days=days)).strftime("%Y%m%d")

    all_rows = []
    invalid_count = 0
    next_key = ""
    cont_yn  = ""
    seen_keys = set()

    for _ in range(_KIWOOM_MAX_DAILY_PAGES):
        body = {"stk_cd": ticker, "base_dt": end, "upd_stkpc_tp": "1"}
        headers_extra = []
        if next_key:
            headers_extra = [f"next-key: {next_key}", f"cont-yn: {cont_yn}"]

        data = _kiwoom_request("/api/dostk/chart", body, "ka10081", token,
                                extra_headers=headers_extra)
        if not data:
            break

        rows = (data.get("stk_dt_pole_chart_qry") or
                data.get("output") or
                data.get("stk_chart") or [])
        if not rows:
            break

        reached_start = False
        for item in rows:
            dt    = str(item.get("dt", ""))
            close = _kiwoom_num(item.get("cur_prc") or item.get("close_pric"))
            vol   = int(_kiwoom_num(item.get("trde_qty")))
            if not (close and vol >= 0):
                continue
            if len(dt) >= 8 and dt[:8] < start:
                reached_start = True
                break

            open_  = _kiwoom_num(item.get("open_pric"))
            high   = _kiwoom_num(item.get("high_pric"))
            low    = _kiwoom_num(item.get("low_pric"))
            valid  = (open_ > 0 and high > 0 and low > 0 and close > 0 and
                      low <= min(open_, close) <= max(open_, close) <= high)
            if not valid:
                invalid_count += 1
                continue

            # trde_prica는 백만원 단위(src/cvd_obv/ingestion/kiwoom_daily.py 실측 근거).
            # 없으면 None — 호출부가 추정치로 대체할지 정한다.
            raw_tv = item.get("trde_prica", item.get("trde_amt"))
            traded_value = (_kiwoom_num(raw_tv) * 1_000_000
                            if raw_tv not in (None, "") else None)
            all_rows.append({
                "date":   f"{dt[:4]}-{dt[4:6]}-{dt[6:8]}" if len(dt) >= 8 else dt,
                "close":  close,
                "volume": vol,
                "open":   open_,
                "high":   high,
                "low":    low,
                "traded_value": traded_value,
            })

        next_key = data.get("_next_key", "")
        cont_yn  = data.get("_cont_yn", "")
        if reached_start or not next_key or cont_yn != "Y" or next_key in seen_keys:
            break
        seen_keys.add(next_key)
        time.sleep(0.22)  # 초당 5회 제한 준수

    seen, dedup = set(), []
    for r in all_rows:
        if r["date"] in seen:
            continue
        seen.add(r["date"])
        dedup.append(r)
    dedup.sort(key=lambda r: r["date"])

    if not include_incomplete and dedup and not _is_bar_date_complete(dedup[-1]["date"], as_of):
        dedup = dedup[:-1]

    if invalid_count:
        print(f"  ⚠️  {ticker}: OHLC 정합성 오류로 {invalid_count}개 봉 제외", file=sys.stderr)

    return dedup


_STD_CACHE = None


def prime_standard_cache(codes, as_of: Optional[datetime] = None):
    """로컬 기준 일봉 캐시가 `codes`를 덮게 한다. 저장소를 못 읽으면 None.

    캐시 생성은 저장소 전체를 훑어 오래 걸린다(2026-11-01 실측: 351종목 63초,
    1종목 43초 — 종목 수와 거의 무관). 그래서 없는 종목이 나올 때마다 다시
    만들면 후보가 많은 명령(volume-surge 60종목)이 한 시간 가까이 걸린다.
    여러 종목을 다룰 명령은 시작할 때 전부 넘겨 한 번만 만든다.
    """
    global _STD_CACHE
    from cvd_obv.bar_standard import StandardDailyCache
    from cvd_obv.live_feed import index_codes

    as_of = as_of or datetime.now()
    want = {str(c) for c in codes}
    if (_STD_CACHE is not None and _STD_CACHE[1] == as_of.date()
            and want <= _STD_CACHE[0]):
        return _STD_CACHE[2]
    known = _STD_CACHE[0] if _STD_CACHE is not None and _STD_CACHE[1] == as_of.date() else set()
    all_codes = set(index_codes()) | known | want
    try:
        _STD_CACHE = (all_codes, as_of.date(), StandardDailyCache(sorted(all_codes), as_of))
    except Exception as e:      # pyarrow 미설치·저장소 없음 → API 폴백
        print(f"  ⚠️  로컬 일봉을 읽지 못함({type(e).__name__}) — API로 폴백",
              file=sys.stderr)
        return None
    return _STD_CACHE[2]


def _prev_weekday(d):
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def standard_daily_bars(ticker: str, days: int = 460,
                        as_of: Optional[datetime] = None,
                        include_incomplete: bool = False) -> tuple:
    """봉 기준 bars-1.0 일봉 — (rows, 상태). 상태: OK / STALE / EMPTY.

    과거 봉은 로컬 `silver.daily_bars`의 정규장·수정주가 봉이다(2026-09-14 이후는
    1분봉 정규장 복원본). 당일 봉은 API에서 붙인다:
      - 15:35~16:00: 정규장 확정값
      - `include_incomplete=True`이고 15:35 전: 장중 누적값(`intraday=True`)
      - 16:00~20:05: 애프터마켓이 섞여 붙이지 않는다
    로컬 최신일이 직전 평일보다 오래되면 STALE — 그래도 기준 일봉을 돌려준다(출처를
    섞지 않는다). 호출자는 경고만 낸다.
    """
    from cvd_obv.bar_standard import FETCH_INCOMPLETE, FETCH_REGULAR, classify_fetched_daily

    as_of = as_of or datetime.now()
    cache = prime_standard_cache([ticker], as_of)
    if cache is None:
        return [], "EMPTY"
    local = cache.bars(ticker, days=days)
    if not local:
        return [], "EMPTY"
    rows = [{"date": f"{b['dt'][:4]}-{b['dt'][4:6]}-{b['dt'][6:8]}",
             "open": b["open"], "high": b["high"], "low": b["low"],
             "close": b["close"], "volume": b["volume"],
             "traded_value": b.get("traded_value")} for b in local]
    today = as_of.date()
    scope = classify_fetched_daily(today, as_of)
    want_today = (scope == FETCH_REGULAR
                  or (include_incomplete and scope == FETCH_INCOMPLETE
                      and as_of.time() < datetime.strptime("15:35", "%H:%M").time()))
    if want_today and rows[-1]["date"] != today.isoformat() and today.weekday() < 5:
        for r in kis_daily_bars(ticker, days=10, as_of=as_of, include_incomplete=True):
            if r["date"] == today.isoformat():
                rows.append(dict(r, intraday=scope != FETCH_REGULAR))
                break
    # 로컬에는 직전 거래일(평일 근사 — 휴장일 직후면 거짓 경보일 수 있다)까지 있어야 한다
    local_last = datetime.strptime(local[-1]["dt"], "%Y%m%d").date()
    if local_last < _prev_weekday(today):
        return rows, "STALE"
    return rows, "OK"


def fetch_ohlcv(ticker: str, days: int = 460) -> Optional[dict]:
    """OHLCV 조회 — 봉 기준 bars-1.0.

    1순위 로컬 기준 일봉(정규장·수정주가) + 당일 정규장 봉(`standard_daily_bars`).
    오래됐으면 경고만 낸다. 로컬에 없으면 2순위 KIS API 일봉으로 폴백한다 — 2026-09-14
    이후 봉은 거래량이 애프터마켓 포함이고 최근 거래일 가격은 다음 영업일 아침 전까지
    애프터마켓 최종가라(docs/KIS-일봉-최근일-애프터마켓가격-20261101.md) **기준 밖**이며
    `source`에 표시된다. 3순위 Yahoo Finance(세션 정의 미확인, 기준 밖).

    기본 460 캘린더일은 MA120(완료 봉 120개) 워밍업 여유다.
    """
    def _pack(bars, source, name=None):
        return {
            "ticker":  ticker,
            "name":    name or ticker,
            "source":  source,
            "rows":    bars,
            "opens":   [r["open"]   for r in bars],
            "highs":   [r["high"]   for r in bars],
            "lows":    [r["low"]    for r in bars],
            "closes":  [r["close"]  for r in bars],
            "volumes": [r["volume"] for r in bars],
        }

    has_kis = _kis_enabled()
    bars, status = standard_daily_bars(ticker, days=days)
    if status == "STALE":
        print(f"  ⚠️  {ticker}: 로컬 일봉이 {bars[-1]['date']}까지 — "
              f"`python cvdobv.py data-load daily`로 갱신 필요(휴장일 직후면 정상)",
              file=sys.stderr)
    if len(bars) >= 20:
        return _pack(bars, "local_standard", kis_name(ticker) if has_kis else None)

    if has_kis:
        bars = kis_daily_bars(ticker, days=days)
        if len(bars) >= 20:
            return _pack(bars, "kis", kis_name(ticker))

    d = fetch_ohlcv_yahoo(ticker, days=max(days, 130))
    if d:
        d["source"] = "yahoo"
    return d


#: fetch_ohlcv 출처 → 화면 표시. 기준 밖 출처는 이유를 함께 보여 준다.
SOURCE_NOTE = {
    "local_standard": "로컬 정규장 일봉(봉 기준 bars-1.0)",
    "kis": ("KIS API 일봉 — 2026-09-14 이후 거래량은 애프터마켓 포함, 최근 거래일 가격은 "
            "다음 영업일 아침 전까지 애프터마켓 최종가, 기준 밖"),
    "kiwoom": "키움 API 일봉 — 2026-09-14 이후 애프터마켓 포함(EXTENDED), 기준 밖",
    "yahoo": "Yahoo Finance — 세션 정의 미확인, 기준 밖",
}


_KIWOOM_MRKT_TP = {"all": "000", "kospi": "001", "kosdaq": "101"}


def kiwoom_volume_surge(market: str = "all", stex_tp: str = "1") -> list:
    """키움 ka10023(거래량급증요청) — 공식 거래량 급증 랭킹.

    주의: 원본 KIS 경로가 쓰던 "당일 거래량 ÷ 20일 평균" 비율과 이 TR의
    sdnin_rt(급증률)는 정의가 다르다(tm_tp=2로 전일 거래량 대비 급증량/급증률을
    산출 — 20일 평균 대비 아님). 두 지표를 같은 것으로 표시하지 않는다.
    """
    token = _kiwoom_get_token()
    if not token:
        return []
    body = {
        "mrkt_tp": _KIWOOM_MRKT_TP.get(market, "000"),
        "sort_tp": "2",          # 2:급증률
        "tm_tp": "2",            # 2:전일 대비
        "trde_qty_tp": "5",      # 5천주 이상 (노이즈 제거)
        "tm": "",
        "stk_cnd": "20",         # ETF+ETN+스팩 제외 (개별 종목만)
        "pric_tp": "0",
        "stex_tp": stex_tp,
    }
    data = _kiwoom_request("/api/dostk/rkinfo", body, "ka10023", token)
    if not data:
        return []
    rows = data.get("trde_qty_sdnin", [])
    out = []
    for r in rows:
        out.append({
            "code":       r.get("stk_cd", ""),
            "name":       r.get("stk_nm", ""),
            "price":      _kiwoom_num(r.get("cur_prc")),
            "change_pct": _kiwoom_signed(r.get("flu_rt")),
            "prev_volume": int(_kiwoom_num(r.get("prev_trde_qty"))),
            "now_volume":  int(_kiwoom_num(r.get("now_trde_qty"))),
            "surge_rt":    _kiwoom_signed(r.get("sdnin_rt")),  # 급증률(%), 전일 대비 기준
        })
    return out


def kiwoom_new_high(market: str = "all", period_days: int = 60,
                     by_close: bool = True) -> list:
    """키움 ka10016(신고저가요청) — 공식 신고가/신저가 랭킹.

    period_days는 5/10/20/60/250만 지원된다(요청 스펙 dt 파라미터). 다른 값은
    호출자가 가장 가까운 지원값으로 스냅해야 한다(이 함수는 스냅하지 않고
    그대로 전달 — 잘못된 값이면 API가 오류를 반환한다).
    by_close=True면 high_low_close_tp=2(종가기준, "종가 확정 돌파"에 해당),
    False면 1(고저기준, 장중 터치 포함).
    """
    token = _kiwoom_get_token()
    if not token:
        return []
    body = {
        "mrkt_tp": _KIWOOM_MRKT_TP.get(market, "000"),
        "ntl_tp": "1",            # 1:신고가
        "high_low_close_tp": "2" if by_close else "1",
        "stk_cnd": "1",
        "trde_qty_tp": "00000",
        "crd_cnd": "0",
        "updown_incls": "0",
        "dt": str(period_days),
        "stex_tp": "1",
    }
    data = _kiwoom_request("/api/dostk/stkinfo", body, "ka10016", token)
    if not data:
        return []
    rows = data.get("ntl_pric", [])
    out = []
    for r in rows:
        out.append({
            "code":       r.get("stk_cd", ""),
            "name":       r.get("stk_nm", ""),
            "price":      _kiwoom_num(r.get("cur_prc")),
            "change_pct": _kiwoom_signed(r.get("flu_rt")),
            "volume":     int(_kiwoom_num(r.get("trde_qty"))),
            "vol_vs_prev_pct": _kiwoom_signed(r.get("pred_trde_qty_pre_rt")),
            "high":       _kiwoom_num(r.get("high_pric")),
            "low":        _kiwoom_num(r.get("low_pric")),
        })
    return out


# ─────────────────────────────────────────────────────────────
# KIS (한국투자증권) API — 이 도구의 데이터 원천(2026-11-01 키움에서 전환).
# 적재(data-load)와 같은 원천이라 로컬 일봉과 정의를 맞추기 쉽다. 토큰 캐시
# (`.kis_token.json`)와 `_kis_request`는 src/cvd_obv/ingestion(kis_client 등)도
# 같이 쓴다 — 같은 자격증명으로 토큰을 두 군데서 발급하면 서로 무효화된다.
# 호출 간격·재시도는 `KisClient`가 맡는다.
# ─────────────────────────────────────────────────────────────

_KIS_BASE = "https://openapi.koreainvestment.com:9443"
_KIS_TOKEN_CACHE = os.path.join(os.path.dirname(__file__), "..", ".kis_token.json")


def _kis_get_token(app_key: str, app_secret: str) -> Optional[str]:
    """KIS OAuth 토큰 발급 (캐시 활용)."""
    import time

    # 캐시 확인 (만료 전이면 재사용)
    if os.path.exists(_KIS_TOKEN_CACHE):
        with open(_KIS_TOKEN_CACHE) as f:
            cache = json.load(f)
        if cache.get("expires_at", 0) > time.time() + 60:
            return cache["access_token"]

    # 신규 발급
    url     = f"{_KIS_BASE}/oauth2/tokenP"
    payload = json.dumps({
        "grant_type":   "client_credentials",
        "appkey":       app_key,
        "appsecret":    app_secret,
    })
    result = subprocess.run(
        ["curl", "-s", "-X", "POST", url,
         "-H", "Content-Type: application/json",
         "-d", payload],
        capture_output=True, timeout=_TIMEOUT
    )
    try:
        data  = json.loads(result.stdout)
        token = data.get("access_token")
        if not token:
            return None
        expires = int(data.get("expires_in", 86400))
        import time
        cache = {"access_token": token, "expires_at": time.time() + expires}
        with open(_KIS_TOKEN_CACHE, "w") as f:
            json.dump(cache, f)
        return token
    except Exception:
        return None


def _kis_request(path: str, params: dict, tr_id: str) -> Optional[dict]:
    """KIS REST API 호출."""
    app_key    = os.environ.get("KIS_APP_KEY")
    app_secret = os.environ.get("KIS_APP_SECRET")
    if not app_key or not app_secret:
        return None

    token = _kis_get_token(app_key, app_secret)
    if not token:
        return None

    from urllib.parse import urlencode
    url     = f"{_KIS_BASE}{path}?{urlencode(params)}"
    headers = [
        f"authorization: Bearer {token}",
        f"appkey: {app_key}",
        f"appsecret: {app_secret}",
        f"tr_id: {tr_id}",
        "Content-Type: application/json; charset=utf-8",
    ]
    try:
        return _curl_json(url, headers)
    except Exception:
        return None


_KIS_QUOTATIONS = "/uapi/domestic-stock/v1/quotations"
_KIS_CLIENT = None


def _kis_enabled() -> bool:
    return bool(os.environ.get("KIS_APP_KEY") and os.environ.get("KIS_APP_SECRET"))


def _kis_client():
    global _KIS_CLIENT
    if _KIS_CLIENT is None:
        from cvd_obv.ingestion.kis_client import KisClient
        _KIS_CLIENT = KisClient(request=_kis_request)
    return _KIS_CLIENT


def _kis_get(path: str, params: dict, tr_id: str) -> Optional[dict]:
    """성공(`rt_cd == "0"`) 응답만 돌려준다. 실패하면 사유를 stderr에 남기고 None."""
    if not _kis_enabled():
        return None
    client = _kis_client()
    res = client.get(path, params, tr_id)
    if res is None:
        target = params.get("FID_INPUT_ISCD") or params.get("PDNO") or ""
        print(f"  ⚠️  KIS {tr_id} 실패({target}): {client.last_error}", file=sys.stderr)
    return res


def _kis_float(v) -> Optional[float]:
    """KIS 숫자 필드(부호 포함 문자열). 빈 값이면 None — 0으로 채우지 않는다."""
    s = str(v if v is not None else "").replace(",", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


_NAME_CACHE: dict = {}


def kis_name(ticker: str) -> Optional[str]:
    """종목명 — 지수 구성종목 명단에 있으면 그 이름, 없으면 KIS CTPF1002R(상품기본조회)
    `prdt_abrv_name`. KIS 현재가(FHKST01010100) 응답에는 종목명이 없다."""
    if ticker in _NAME_CACHE:
        return _NAME_CACHE[ticker]
    if not _NAME_CACHE:
        try:
            from cvd_obv.universe import current_universe
            for m in current_universe().members():
                _NAME_CACHE.setdefault(m.code, m.name)
        except Exception:
            pass
        if ticker in _NAME_CACHE:
            return _NAME_CACHE[ticker]
    res = _kis_get(f"{_KIS_QUOTATIONS}/search-stock-info",
                   {"PRDT_TYPE_CD": "300", "PDNO": ticker}, "CTPF1002R")
    name = str(((res or {}).get("output") or {}).get("prdt_abrv_name") or "").strip() or None
    _NAME_CACHE[ticker] = name
    return name


def kis_price_unsettled(now: Optional[datetime] = None) -> bool:
    """지금 KIS 현재가·고저가 애프터마켓 값일 수 있는가 — 16:00부터 다음 평일 09:00 전.

    실측(2026-11-01): 053800 10/30을 일요일에 조회하면 현재가 121,100·고가 127,400(애프터마켓
    최종가·최고가), 정규장 종가는 118,100. 정산 기준은 `adjustment_watch.settled`와 같다.
    """
    now = now or datetime.now()
    return now.weekday() >= 5 or now.hour >= 16 or now.hour < 9


def kis_quote(ticker: str, name: Optional[str] = None) -> Optional[dict]:
    """KIS FHKST01010100(주식현재가 시세) — 현재가·시가·고저·전일종가·거래량·시가총액.

    필드(2026-11-01 실측, 053800): stck_sdpr(기준가 = 전일 정규장 종가), hts_avls(시가총액,
    억원), acml_tr_pbmn(누적 거래대금, 원), w52_hgpr(52주 고가). 가격·등락은 부호가
    붙은 값 그대로 온다. 16:00부터 다음 평일 09:00 전까지는 현재가·고저·등락률이
    애프터마켓 값이다(`price_unsettled`). 시가·기준가는 정규장 값이다.
    """
    res = _kis_get(f"{_KIS_QUOTATIONS}/inquire-price",
                   {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker}, "FHKST01010100")
    out = (res or {}).get("output") or {}
    price = _kis_float(out.get("stck_prpr"))
    if not price:
        return None
    cap_eok = _kis_float(out.get("hts_avls"))
    return {
        "symbol":      ticker,
        "name":        name or kis_name(ticker) or ticker,
        "price":       price,
        "open":        _kis_float(out.get("stck_oprc")) or 0.0,
        "high":        _kis_float(out.get("stck_hgpr")) or 0.0,
        "low":         _kis_float(out.get("stck_lwpr")) or 0.0,
        "prev_close":  _kis_float(out.get("stck_sdpr")) or 0.0,
        "volume":      int(_kis_float(out.get("acml_vol")) or 0),
        "traded_value": _kis_float(out.get("acml_tr_pbmn")),               # 원 단위
        "change":      _kis_float(out.get("prdy_vrss")),
        "change_pct":  _kis_float(out.get("prdy_ctrt")),
        "market_cap_eok": cap_eok,                                          # 억원 단위 원본값
        "market_cap_krw": cap_eok * 1e8 if cap_eok is not None else None,   # 원 단위 환산
        "w52_high":    _kis_float(out.get("w52_hgpr")),
        "price_unsettled": kis_price_unsettled(),      # 현재가·고저가 애프터마켓 값일 수 있음
        "as_of":       datetime.now().isoformat(timespec="seconds"),
        "source":      "kis",
    }


_INVESTOR_CACHE: dict = {}


def kis_investor_history(ticker: str) -> list:
    """KIS FHKST01010900(주식현재가 투자자) — 최근 30거래일 개인/외국인/기관 순매수(주),
    최신일 먼저. 한 번 받은 종목은 다시 부르지 않는다."""
    if ticker in _INVESTOR_CACHE:
        return _INVESTOR_CACHE[ticker]
    res = _kis_get(f"{_KIS_QUOTATIONS}/inquire-investor",
                   {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker}, "FHKST01010900")
    rows = []
    for r in (res or {}).get("output") or []:
        dt = str(r.get("stck_bsop_date") or "")
        foreign = _kis_float(r.get("frgn_ntby_qty"))
        institution = _kis_float(r.get("orgn_ntby_qty"))
        # 당일 행은 장중에 수량이 비어 올 수 있다 — 빈 값은 0으로 채우지 않고 버린다
        if len(dt) != 8 or foreign is None or institution is None:
            continue
        rows.append({
            "date":        dt,
            "individual":  _kis_float(r.get("prsn_ntby_qty")),
            "foreign":     foreign,
            "institution": institution,
            "unit":        "주",
            "source":      "kis",
        })
    if res is not None:
        _INVESTOR_CACHE[ticker] = rows
    return rows


def kis_investor_flow(ticker: str, date: Optional[str] = None) -> Optional[dict]:
    """지정일(YYYYMMDD, 기본 오늘) 개인/외국인/기관 순매수(주). 그날 행이 없으면
    None — 휴장일에 며칠 전 값을 대신 채우지 않는다."""
    date = date or _today_str().replace("-", "")
    for r in kis_investor_history(ticker):
        if r["date"] == date:
            return r
    return None


def kis_daily_bars(ticker: str, days: int = 460,
                   as_of: Optional[datetime] = None,
                   include_incomplete: bool = False) -> list:
    """KIS FHKST03010100(기간별시세, 수정주가)로 최근 `days` 캘린더일 일봉, 오름차순.

    적재 어댑터 `KisDailySource`(100행씩 날짜를 뒤로 옮기며 받기, OHLC 검증)를 그대로
    쓴다. 2026-09-14 이후 봉은 거래량·거래대금이 애프터마켓 포함이고, 최근 거래일 가격은
    다음 영업일 아침 전까지 애프터마켓 최종가다(그 뒤 정규장 값으로 바뀐다). 그래서
    봉 기준(bars-1.0) 밖이다. include_incomplete=False면 당일 장중 미완료 봉을 뺀다.
    """
    if not _kis_enabled():
        return []
    from cvd_obv.ingestion.kis_daily import KisDailySource

    as_of = as_of or datetime.now()
    res = KisDailySource(client=_kis_client()).fetch_security(
        ticker, start=(as_of - timedelta(days=days)).date(), end=as_of.date())
    if res.error_message:
        print(f"  ⚠️  {ticker}: KIS 일봉 — {res.error_message}", file=sys.stderr)
    rows = [{
        "date":   r["trading_date"].isoformat(),
        "open":   float(r["open"]),
        "high":   float(r["high"]),
        "low":    float(r["low"]),
        "close":  float(r["close"]),
        "volume": int(r["volume"]),
        "traded_value": float(r["traded_value"]) if r["traded_value"] is not None else None,
    } for r in res.rows]
    if not include_incomplete and rows and not _is_bar_date_complete(rows[-1]["date"], as_of):
        rows = rows[:-1]
    return rows


#: --market → KIS 거래량순위 시장 코드(0001 거래소, 1001 코스닥). 전체(0000)는 한 번에
#: 30종목뿐이라 코스닥이 거의 다 채운다 — 시장마다 따로 받아 합친다.
_KIS_RANK_MARKETS = {"all": ("0001", "1001"), "kospi": ("0001",), "kosdaq": ("1001",)}


def kis_volume_surge_pool(market: str = "all") -> list:
    """KIS FHPST01710000(거래량순위) 거래증가율 순 — 시장마다 최대 30종목.

    FID_BLNG_CLS_CODE=1(거래증가율). `vol_inrt`는 오늘 누적 거래량 ÷ 전일 거래량 × 100
    (9999.99에서 잘린다) — **20일 평균 대비가 아니다**. FID_TRGT_EXLS_CLS_CODE의
    7·8·10번째 자리 = ETF·ETN·스팩 제외(2026-11-01 실측: 0000000000이면 ETF·스팩이
    상위를 채우고, 0000001101이면 사라진다).
    """
    out = []
    for iscd in _KIS_RANK_MARKETS.get(market, _KIS_RANK_MARKETS["all"]):
        res = _kis_get(f"{_KIS_QUOTATIONS}/volume-rank", {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE":  "20171",
            "FID_INPUT_ISCD":         iscd,
            "FID_DIV_CLS_CODE":       "1",            # 1: 보통주
            "FID_BLNG_CLS_CODE":      "1",            # 1: 거래증가율
            "FID_TRGT_CLS_CODE":      "111111111",
            "FID_TRGT_EXLS_CLS_CODE": "0000001101",   # ETF·ETN·스팩 제외
            "FID_INPUT_PRICE_1":      "",
            "FID_INPUT_PRICE_2":      "",
            "FID_VOL_CNT":            "",
            "FID_INPUT_DATE_1":       "",
        }, "FHPST01710000")
        for r in (res or {}).get("output") or []:
            code = str(r.get("mksc_shrn_iscd") or "").strip()
            if not code:
                continue
            out.append({
                "code":        code,
                "name":        str(r.get("hts_kor_isnm") or "").strip(),
                "price":       _kis_float(r.get("stck_prpr")) or 0.0,
                "change_pct":  _kis_float(r.get("prdy_ctrt")),
                "prev_volume": int(_kis_float(r.get("prdy_vol")) or 0),
                "now_volume":  int(_kis_float(r.get("acml_vol")) or 0),
                "surge_rt":    _kis_float(r.get("vol_inrt")),   # 전일 대비(%), 20일 평균 아님
            })
    out.sort(key=lambda x: -(x["surge_rt"] or 0))
    return out


# ─────────────────────────────────────────────────────────────
# 스캔 명령
# ─────────────────────────────────────────────────────────────

def cmd_check_market():
    """장 운영 여부 및 API 연결 상태 확인."""
    print("=" * 60)
    print("시장 및 API 상태 확인")
    print("=" * 60)

    now     = datetime.now()
    weekday = now.weekday()
    hour    = now.hour
    minute  = now.minute

    print(f"\n  📅 날짜:   {now.strftime('%Y-%m-%d')} ({['월','화','수','목','금','토','일'][weekday]})")
    print(f"  🕐 시각:   {now.strftime('%H:%M:%S')}")

    if weekday >= 5:
        print(f"  🏛️  장 상태: ❌ 휴장 (주말)")
    elif (9, 0) <= (hour, minute) < (15, 30):
        print(f"  🏛️  장 상태: ✅ 정규장 운영 중 (09:00~15:30)")
    elif (8, 30) <= (hour, minute) < (9, 0):
        print(f"  🏛️  장 상태: ⏰ 장 시작 전 (동시호가)")
    elif (15, 30) <= (hour, minute) < (18, 0):
        print(f"  🏛️  장 상태: ⏰ 장 마감 (시간외)")
    else:
        print(f"  🏛️  장 상태: ❌ 장 마감")

    print(f"\n  [증권사 API 연결 상태]")
    kis_key      = os.environ.get("KIS_APP_KEY")
    kis_secret   = os.environ.get("KIS_APP_SECRET")
    kiwoom_key   = os.environ.get("KIWOOM_APP_KEY")
    kiwoom_secret = os.environ.get("KIWOOM_APP_SECRET")
    dart_key     = os.environ.get("DART_API_KEY")

    print(f"  KIS API:     {'✅ 환경변수 설정됨' if kis_key and kis_secret else '❌ 미설정 — .env에 KIS_APP_KEY / KIS_APP_SECRET 설정'}")
    print(f"  키움 API:    {'설정됨' if kiwoom_key and kiwoom_secret else '미설정'} (이 도구의 명령은 쓰지 않음)")
    print(f"  DART API:    {'✅ 환경변수 설정됨' if dart_key else '❌ 미설정 — .env에 DART_API_KEY 설정'}")
    print(f"  Yahoo F.:    폴백 전용, 키 불필요 (실제 가용성은 확인 안 함 — check-broker는 이 값을 검사하지 않음)")

    print(f"\n  [데이터 우선순위]")
    if kis_key and kis_secret:
        print(f"  일봉: 로컬 정규장 일봉 → KIS 일봉(기준 밖) → Yahoo Finance(기준 밖)")
        print(f"  현재가·수급·거래량순위: KIS REST API")
    else:
        print(f"  일봉: 로컬 정규장 일봉 → Yahoo Finance(기준 밖) — 현재가·수급·순위는 KIS 설정 필요")


def cmd_check_broker():
    """증권사 API 연결 테스트."""
    print("=" * 60)
    print("증권사 API 연결 테스트")
    print("=" * 60)

    if _kis_enabled():
        print("\n  [KIS API 테스트] 인증 + 삼성전자(005930) 현재가 조회...")
        if not _kis_get_token(os.environ["KIS_APP_KEY"], os.environ["KIS_APP_SECRET"]):
            print(f"  ❌ KIS 인증 실패: appkey/appsecret 오류 가능성")
            print(f"     KIS 개발자 센터: https://apiportal.koreainvestment.com")
        else:
            q = kis_quote("005930")
            if q and q.get("price"):
                print(f"  ✅ KIS 연결 성공: 삼성전자 현재가 {q['price']:,.0f}원 (as_of {q['as_of']})")
            else:
                print(f"  ❌ KIS 인증은 성공했으나 시세 조회 실패 (FHKST01010100)")
    else:
        print("\n  [KIS API] 미설정")
        print("  KIS API 키 발급: https://apiportal.koreainvestment.com")
        print("  .env 파일에 아래 항목을 설정하세요 (.env.example 참고):")
        print("    KIS_APP_KEY=앱키")
        print("    KIS_APP_SECRET=앱시크릿")

    print()


# ─────────────────────────────────────────────────────────────
# 구조화 신호 코드 → 출력 문구 변환 (가이드 9단계). 계산 함수는 코드만
# 반환하므로, 사람이 읽는 한국어 문장은 여기(출력 계층)에서만 만든다.
# ─────────────────────────────────────────────────────────────

_TREND_LABEL = {TREND_UP: "상승", TREND_DOWN: "하락", TREND_FLAT: "보합", TREND_UNKNOWN: "불명"}


def _obv_signal_text(obv: dict) -> str:
    if obv.get("vs_ma20") == "above":
        return "🟢 OBV > MA20 (매수세 우위)"
    if obv.get("vs_ma20") == "below":
        return "🔴 OBV < MA20 (매도세 우위)"
    return _TREND_LABEL.get(obv.get("trend"), "불명")


def _obv_divergence_text(divergence: Optional[str]) -> Optional[str]:
    if divergence == DIVERGENCE_BEARISH:
        return "🔴 약세 다이버전스 (가격 상승 + OBV 하락 → 상승 추세 약화 신호)"
    if divergence == DIVERGENCE_BULLISH:
        return "🟢 강세 다이버전스 (가격 하락 + OBV 상승 → 하락 추세 약화 신호)"
    return None


def _pressure_signal_text(vp: dict) -> str:
    if vp.get("vs_ma20") == "above":
        if vp.get("trend") == TREND_UP:
            return "🟢 강한 매수 압력 (추정압력 > MA20, 상승 모멘텀)"
        return "🟡 매수 압력 우위 (추정압력 > MA20)"
    if vp.get("vs_ma20") == "below":
        if vp.get("trend") == TREND_DOWN:
            return "🔴 강한 매도 압력 (추정압력 < MA20, 하락 모멘텀)"
        return "🟡 매도 압력 우위 (추정압력 < MA20)"
    momentum_label = {TREND_UP: "매수세 강화", TREND_DOWN: "매도세 강화",
                       TREND_FLAT: "중립", TREND_UNKNOWN: "불명"}
    return momentum_label.get(vp.get("trend"), "불명")


def _pressure_divergence_text(divergence: Optional[str]) -> Optional[str]:
    if divergence == DIVERGENCE_BEARISH:
        return "🔴 약세 다이버전스 (가격 상승 + 추정압력 하락 → 매수세 이탈, 반전 주의)"
    if divergence == DIVERGENCE_BULLISH:
        return "🟢 강세 다이버전스 (가격 하락 + 추정압력 상승 → 저점 매집, 반등 기대)"
    return None


def cmd_indicators(ticker: str):
    """단일 종목 기술적 지표 전체 계산."""
    print("=" * 60)
    print(f"기술적 지표 분석: {ticker}")
    print("=" * 60)

    d = fetch_ohlcv(ticker)
    if not d:
        print(f"\n  ❌ 데이터 수집 실패 ({ticker})")
        print(f"  로컬 일봉 없음, KIS 미설정/실패, Yahoo Finance 한국주 코드({ticker}.KS)도 실패")
        return

    closes  = d["closes"]
    highs   = d["highs"]
    lows    = d["lows"]
    opens   = d["opens"]
    volumes = d["volumes"]
    last    = closes[-1]  # 지표 계산 기준 — 완료 봉의 마지막 종가

    # 화면에 보여줄 "현재가"는 가능하면 실시간 시세(KIS quote)를 쓴다.
    # closes[-1]은 완료 봉만 남긴 배열이라 장중에는 전일 종가가 되므로,
    # 두 값이 다를 수 있음을 명시적으로 구분해서 보여준다.
    live_price, live_unsettled = None, False
    print(f"  일봉 출처: {SOURCE_NOTE.get(d.get('source'), d.get('source'))}")
    if d.get("source") in ("kis", "local_standard") and _kis_enabled():
        q = kis_quote(ticker, name=d.get("name"))
        if q and q.get("price"):
            live_price, live_unsettled = q["price"], q.get("price_unsettled", False)

    print(f"\n  종목:    {d['name']} ({ticker})")
    if live_price is not None and abs(live_price - last) > 1e-9:
        if live_unsettled:
            print(f"  최근 체결가: {live_price:,.0f}원  (애프터마켓 포함 — 지표 기준 정규장 종가: {last:,.0f}원)")
        else:
            print(f"  실시간가: {live_price:,.0f}원  (지표 기준 종가: {last:,.0f}원 — 당일 미완료 봉 제외)")
    else:
        print(f"  현재가:  {last:,.0f}원")
    print(f"  데이터:  {d['rows'][0]['date']} ~ {d['rows'][-1]['date']} ({len(d['rows'])}일, 출처: {d.get('source', '?')})")

    # RSI
    rsi = calc_rsi(closes)
    if rsi is None:
        rsi_signal = "-"
    elif rsi > 70:
        rsi_signal = "🔴 과매수"
    elif rsi < 30:
        rsi_signal = "🟢 과매도"
    else:
        rsi_signal = "⬜ 중립"
    print(f"\n  [RSI(14)]")
    print(f"  값:      {rsi}")
    print(f"  판단:    {rsi_signal}")

    # MACD
    macd = calc_macd(closes)
    if macd["macd"] is not None and macd["signal"] is not None:
        macd_signal = "🟢 골든크로스" if macd["hist"] is not None and macd["hist"] > 0 else "🔴 데드크로스"
    else:
        macd_signal = "-"
    print(f"\n  [MACD(12,26,9)]")
    print(f"  MACD:    {macd['macd']}")
    print(f"  Signal:  {macd['signal']}")
    print(f"  히스토:  {macd['hist']}")
    print(f"  판단:    {macd_signal}")

    # 볼린저밴드
    bb = calc_bollinger(closes)
    if bb["pct_b"] is not None:
        if bb["pct_b"] < 0.2:
            bb_signal = "🟢 하단 근처 (과매도)"
        elif bb["pct_b"] > 0.8:
            bb_signal = "🔴 상단 근처 (과매수)"
        else:
            bb_signal = "⬜ 중간 구간"
    else:
        bb_signal = "-"
    print(f"\n  [볼린저밴드(20,2σ)]")
    print(f"  상단:    {bb['upper']:,.0f}원" if bb['upper'] is not None else "  상단:    -")
    print(f"  중심:    {bb['middle']:,.0f}원" if bb['middle'] is not None else "  중심:    -")
    print(f"  하단:    {bb['lower']:,.0f}원" if bb['lower'] is not None else "  하단:    -")
    print(f"  %B:      {bb['pct_b']}")
    print(f"  판단:    {bb_signal}")

    # 이동평균
    ma = calc_ma(closes)
    alignment = ma_alignment_status(ma)
    aligned = alignment == "ALIGNED"
    print(f"\n  [이동평균]")
    for p in [5, 20, 60, 120]:
        val = ma.get(f"ma{p}")
        diff = (last - val) / val * 100 if val is not None else None
        mark = f"({diff:+.1f}%)" if diff is not None else ""
        print(f"  MA{p:3d}:   {val:,.0f}원 {mark}" if val is not None else f"  MA{p:3d}:   -")
    alignment_label = {
        "ALIGNED": "✅ 정배열",
        "NOT_ALIGNED": "❌ 역배열 또는 혼합",
        "INSUFFICIENT_DATA": "⬜ 판정 불가 (데이터 부족)",
    }[alignment]
    print(f"  정배열:  {alignment_label}")

    # 스토캐스틱 (Slow 14,3,3)
    stoch = calc_stochastic(highs, lows, closes)
    if stoch["%K"] is not None and stoch["%D"] is not None:
        if stoch["%K"] < 20:
            st_signal = "🟢 과매도 구간"
        elif stoch["%K"] > 80:
            st_signal = "🔴 과매수 구간"
        elif stoch["%K"] > stoch["%D"]:
            st_signal = "🟢 %K > %D (상승 신호)"
        else:
            st_signal = "🔴 %K < %D (하락 신호)"
    else:
        st_signal = "-"
    print(f"\n  [Slow 스토캐스틱(14,3,3)]")
    print(f"  %K:      {stoch['%K']}")
    print(f"  %D:      {stoch['%D']}")
    print(f"  판단:    {st_signal}")
    if stoch.get("cross") == "golden":
        print(f"  📈 골든크로스 (전일 %K≤%D → 당일 %K>%D)")
    elif stoch.get("cross") == "dead":
        print(f"  📉 데드크로스 (전일 %K≥%D → 당일 %K<%D)")

    # 거래량
    avg_vol  = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else None
    last_vol = volumes[-1]
    vol_ratio = last_vol / avg_vol if avg_vol else None
    print(f"\n  [거래량]")
    print(f"  당일:    {_fmt(last_vol)}주")
    print(f"  20일평균: {_fmt(avg_vol)}주" if avg_vol is not None else "  20일평균: - (데이터 부족)")
    if vol_ratio is not None:
        print(f"  비율:    {vol_ratio:.1f}배 {'🔥 급증' if vol_ratio > 2 else ''}")
    else:
        print(f"  비율:    -")

    # OBV
    obv = calc_obv(closes, volumes)
    obv_div_text = _obv_divergence_text(obv['divergence'])
    print(f"\n  [OBV (On-Balance Volume)]")
    print(f"  현재 OBV:  {obv['obv']:,}" if obv['obv'] is not None else "  현재 OBV:  -")
    print(f"  OBV MA20:  {obv['obv_ma20']:,}" if obv['obv_ma20'] is not None else "  OBV MA20:  -")
    print(f"  추세:      {_TREND_LABEL.get(obv['trend'], '불명')}")
    print(f"  판단:      {_obv_signal_text(obv)}")
    if obv_div_text:
        print(f"  ⚡ 다이버전스: {obv_div_text}")

    # 추정 매수/매도 압력 (일봉 근사 — 실제 CVD 아님)
    vp = calc_volume_pressure(opens, highs, lows, closes, volumes)
    vp_div_text = _pressure_divergence_text(vp['divergence'])
    print(f"\n  [추정 매수/매도 압력 — 일봉 근사, 실제 CVD 아님]")
    print(f"  현재값:    {vp['pressure']:,}" if vp['pressure'] is not None else "  현재값:    -")
    print(f"  MA20:      {vp['pressure_ma20']:,}" if vp['pressure_ma20'] is not None else "  MA20:      -")
    print(f"  추세:      {_TREND_LABEL.get(vp['trend'], '불명')}")
    print(f"  판단:      {_pressure_signal_text(vp)}")
    if vp_div_text:
        print(f"  ⚡ 다이버전스: {vp_div_text}")
    if vp.get('data_issues'):
        print(f"  ⚠️  OHLC 정합성 오류 봉 {vp['data_issues']}개 발견 — 해당 봉은 압력 0으로 처리")
    print(f"  {vp['note']}")

    # 캔들 패턴
    patterns = detect_candle_patterns(opens, highs, lows, closes)
    pattern_strength_label = {PATTERN_BULLISH: "강세", PATTERN_BEARISH: "약세",
                               PATTERN_NEUTRAL: "중립", PATTERN_CAUTION: "주의"}
    print(f"\n  [캔들 패턴]")
    if patterns:
        for name, strength, desc in patterns:
            print(f"  {name} ({pattern_strength_label.get(strength, strength)}): {desc}")
    else:
        print(f"  특이 패턴 없음")

    # 종합 신호 (OBV, 추정압력 포함)
    signals = 0
    if rsi is not None and rsi < 40:                                     signals += 1
    if macd.get("hist") is not None and macd["hist"] > 0:                signals += 1
    if bb.get("pct_b") is not None and bb["pct_b"] < 0.3:                signals += 1
    if aligned:                                                          signals += 1
    if stoch.get("%K") is not None and stoch["%K"] < 40:                 signals += 1
    if vol_ratio is not None and vol_ratio > 1.5:                        signals += 1
    bull_patterns = [p for p in patterns if p[1] == PATTERN_BULLISH]
    signals += len(bull_patterns)
    # OBV 강세 신호
    if obv.get("vs_ma20") == "above":
        signals += 1
    if obv.get("divergence") == DIVERGENCE_BULLISH:
        signals += 1
    # 추정압력 강세 신호
    if vp.get("vs_ma20") == "above":
        signals += 1
    if vp.get("divergence") == DIVERGENCE_BULLISH:
        signals += 1

    max_signals = 9 + len(bull_patterns)

    print(f"\n  {'='*40}")
    print(f"  종합 신호 강도: {signals}/{max_signals}")
    print(f"  (RSI·MACD·BB·정배열·스토캐스틱·거래량·캔들·OBV·추정압력 반영)")
    if signals >= 7:
        print(f"  판정: 🟢 강한 매수 신호")
    elif signals >= 5:
        print(f"  판정: 🟢 매수 신호")
    elif signals >= 3:
        print(f"  판정: 🟡 중립 (추가 확인 필요)")
    else:
        print(f"  판정: 🔴 매수 신호 부족")

    # 다이버전스 경고 (종합 판정과 상충 시 강조)
    if obv.get("divergence") == DIVERGENCE_BEARISH:
        print(f"  ⚠️  OBV 약세 다이버전스 — 상승 추세 신뢰도 저하")
    if vp.get("divergence") == DIVERGENCE_BEARISH:
        print(f"  ⚠️  추정압력 약세 다이버전스 — 매수세 이탈 확인 필요")
    print(f"  {'='*40}")


def cmd_volume_surge(ratio: float = 3.0, min_cap: float = 500, market: str = "all"):
    """거래량 급증 종목 스캔 (가이드 7.1: 직전 20일 평균 대비 배수).

    KIS 거래량순위(FHPST01710000, 거래증가율 = 전일 대비)를 "후보 풀"로 먼저 받아온 뒤
    (시장마다 30종목 — 20일평균 기준 급증 종목을 놓칠 가능성은 있음, coverage=rank_subset),
    후보별로 일봉을 다시 조회해 가이드 7.1의 정확한 공식(직전 20완료봉 평균 대비 오늘
    거래량)으로 재계산한다.
    """
    print("=" * 60)
    print(f"거래량 급증 종목 스캔 (직전 20일 평균 대비, 시장: {market})")
    print("=" * 60)

    if not _kis_enabled():
        print("\n  ⚠️  KIS 미연결")
        print("  .env에 KIS_APP_KEY / KIS_APP_SECRET 설정 (.env.example 참고)")
        print()
        print("  대안 — WebSearch로 전일 거래량 급증 종목 확인:")
        print(f'  검색어: "오늘 거래량 급증 종목 {_today_str()}"')
        print(f'  검색어: "전일 특이 거래량 코스피 코스닥 {_today_str()}"')
        return

    pool = kis_volume_surge_pool(market=market)
    print(f"\n  ✅ KIS 거래량순위 후보 풀 {len(pool)}개(전일 대비 거래증가율 상위, coverage=rank_subset)")
    print(f"     → 종목별 일봉 재조회로 20일평균 대비 배수를 정확히 계산합니다...")

    as_of = datetime.now()
    # 후보 대부분이 지수 밖 종목이다 — 캐시를 한 번에 만든다(prime_standard_cache 설명)
    prime_standard_cache([it["code"] for it in pool], as_of)
    candidates, intraday_count, insufficient_count, api_count = [], 0, 0, 0
    for it in pool:
        # 봉 기준 bars-1.0: 20일 평균은 로컬 정규장 일봉, 당일은 장중 누적(정규장).
        # API 일봉만 쓰면 평균에 애프터마켓 거래량이 섞여 배수가 작게 나온다.
        bars, status = standard_daily_bars(it["code"], days=40, as_of=as_of,
                                           include_incomplete=True)
        if status != "OK":
            # 로컬에 없거나(지수 밖) 끊긴 종목(편출 등) — 끊긴 로컬 봉으로 배수를 내면
            # 몇 달 전 거래량과 비교하게 된다. KIS 일봉(기준 밖)으로 대신한다.
            bars = kis_daily_bars(it["code"], days=40, as_of=as_of, include_incomplete=True)
            api_count += 1
        if len(bars) < 21:
            insufficient_count += 1
            continue
        volumes = [b["volume"] for b in bars]
        r = calc_volume_surge_ratio(volumes)
        if r is None:
            insufficient_count += 1
            continue
        is_intraday = not _is_bar_date_complete(bars[-1]["date"], as_of)
        if is_intraday:
            intraday_count += 1
        if r >= ratio:
            candidates.append({**it, "ratio": r, "intraday": is_intraday,
                               "bar_date": bars[-1]["date"]})

    print(f"  (데이터 부족으로 판정 불가 {insufficient_count}개, 장중 당일 거래량 기준 {intraday_count}개, "
          f"KIS 일봉(기준 밖)으로 계산 {api_count}개)")

    if min_cap and min_cap > 0:
        capped = []
        for it in candidates:
            q = kis_quote(it["code"], name=it["name"])
            cap_eok = q.get("market_cap_eok") if q else None
            if cap_eok is not None and cap_eok >= min_cap:
                it["market_cap_eok"] = cap_eok
                capped.append(it)
        candidates = capped

    candidates.sort(key=lambda x: -x["ratio"])

    if not candidates:
        print("\n  조건을 충족하는 종목이 없습니다.")
        return

    print(f"\n  {'종목명':<15} {'코드':<8} {'현재가':>8} {'등락률':>7} {'20일평균배수':>10} {'시총(억)':>9} {'기준일':>10}")
    print(f"  {'─'*15} {'─'*8} {'─'*8} {'─'*7} {'─'*10} {'─'*9} {'─'*10}")
    for it in candidates[:20]:
        cap_str = f"{it['market_cap_eok']:,.0f}" if it.get("market_cap_eok") is not None else "-"
        chg = it["change_pct"]
        chg_str = f"{chg:+.1f}%" if chg is not None else "-"
        flag = "🕐" if it.get("intraday") else "  "
        print(f"  {it['name'][:15]:<15} {it['code']:<8} {it['price']:>8,.0f}원 {chg_str:>7} "
              f"{it['ratio']:>9.1f}배{flag} {cap_str:>9} {it['bar_date']:>10}")
    print(f"\n  🕐 = 장중 당일 누적 거래량 기준(완료 봉 아님, 최종치와 다를 수 있음)")


_NEW_HIGH_PERIOD_MAP = {  # --period → 직전 거래일 수
    "5d": 5, "10d": 10, "20d": 20, "60d": 60, "120d": 120, "250d": 250, "52w": 250,
}


def cmd_new_high(period: str = "52w", market: str = "all"):
    """신고가 돌파 종목 스캔 (가이드 7.2: 종가 > 직전 N거래일 최고가, 오늘 고가 제외).

    KIS에는 종가 기준 N일 신고가 순위 TR이 없다(FHPST01870000 '신고/신저근접'은 근접률
    순위이고, 2026-11-01 실측에서 거래량 0인 종목이 상위를 채웠다). 그래서 로컬 기준 일봉
    (bars-1.0)으로 코스피200+코스닥150 유니버스를 직접 판정한다 —
    coverage=index_kospi200_kosdaq150(전체 시장 아님). 완료 봉만 쓴다.
    """
    print("=" * 60)
    print(f"신고가 돌파 종목 스캔 ({period}, 시장: {market})")
    print("=" * 60)

    lookback = _NEW_HIGH_PERIOD_MAP.get(period)
    if lookback is None:
        print(f"\n  ⚠️  지원하지 않는 --period '{period}' — {'/'.join(_NEW_HIGH_PERIOD_MAP)} 중 선택")
        return
    universe = _load_universe(market)
    if not universe:
        print("\n  ❌ 유니버스 명단을 만들지 못했습니다 — python3 tools/update_index.py 확인")
        return

    as_of = datetime.now()
    print(f"\n  ✅ 로컬 기준 일봉 — 종가 기준 {lookback}거래일 신고가, "
          f"coverage=index_kospi200_kosdaq150 ({len(universe)}종목, 전체 시장 아님)")
    if prime_standard_cache([s["code"] for s in universe], as_of) is None:
        print("\n  ❌ 로컬 일봉을 읽지 못했습니다")
        return

    # 캐시 창이 400 캘린더일이라 250거래일 + 1봉까지 덮는다
    window_days = 400
    matched, undecided, last_dates = [], 0, {}
    for s in universe:
        bars, _status = standard_daily_bars(s["code"], days=window_days, as_of=as_of)
        closes = [b["close"] for b in bars]
        highs = [b["high"] for b in bars]
        hit = calc_new_high_breakout(closes, highs, lookback)
        if hit is None:
            undecided += 1
            continue
        last_dates[bars[-1]["date"]] = last_dates.get(bars[-1]["date"], 0) + 1
        if hit:
            vols = [b["volume"] for b in bars]
            matched.append({
                "code": s["code"], "name": s["name"], "close": closes[-1],
                "change_pct": (closes[-1] / closes[-2] - 1) * 100 if len(closes) >= 2 else None,
                "prior_high": max(highs[-(lookback + 1):-1]),
                "vol_ratio": calc_volume_surge_ratio(vols),
                "date": bars[-1]["date"],
            })

    basis = ", ".join(f"{d} {n}종목" for d, n in sorted(last_dates.items(), reverse=True)[:3])
    print(f"  판정 기준일: {basis or '-'} / 판정 불가(봉 부족) {undecided}종목")
    if len(last_dates) > 1:
        print(f"  ⚠️  종목마다 마지막 봉 날짜가 다릅니다 — 적재가 밀린 종목이 있을 수 있음")
    if not matched:
        print("\n  조건을 충족하는 종목이 없습니다.")
        return

    matched.sort(key=lambda x: -(x["change_pct"] or 0))
    print(f"\n  {'종목명':<15} {'코드':<8} {'종가':>9} {'등락률':>7} {'직전최고가':>10} {'거래량배수':>8}")
    print(f"  {'─'*15} {'─'*8} {'─'*9} {'─'*7} {'─'*10} {'─'*8}")
    for it in matched[:40]:
        chg = it["change_pct"]
        chg_str = f"{chg:+.1f}%" if chg is not None else "-"
        vr = f"{it['vol_ratio']:.1f}배" if it["vol_ratio"] is not None else "-"
        print(f"  {it['name'][:15]:<15} {it['code']:<8} {it['close']:>8,.0f}원 {chg_str:>7} "
              f"{it['prior_high']:>9,.0f}원 {vr:>8}")
    if len(matched) > 40:
        print(f"  … 외 {len(matched) - 40}종목")
    print(f"\n  거래량배수 = 직전 20봉 평균 대비(참고용 — 거래량 동반은 판정 조건이 아님)")


def _load_universe(market: str = "all") -> list:
    """오늘의 코스피200/코스닥150 명단(유니버스 기준 universe-1.0,
    `cvd_obv.universe.universe_on`).

    시장 전체가 아니라 이 인덱스에 포함된 종목만 커버한다 — gap-up처럼
    시장 전체 랭킹 TR이 없는 스캐너의 "제한된 유니버스" 근거로 쓴다.
    반환: [{"code":..., "name":..., "market": "kospi200"|"kosdaq150"}, ...]
    """
    try:
        from cvd_obv.universe import FILE_KEYS, parse_indexes, universe_on
        u = universe_on(None, parse_indexes(market if market != "all" else None))
    except Exception as e:
        print(f"  ⚠️  유니버스를 만들지 못함({type(e).__name__}: {e})", file=sys.stderr)
        return []
    for w in u.warnings:
        print(f"  ⚠️  [유니버스] {w}", file=sys.stderr)
    return [{"code": m.code, "name": m.name, "market": FILE_KEYS[m.index_id]}
            for m in u.members()]


def cmd_gap_up(min_gap: float = 3.0, market: str = "all"):
    """갭 상승 종목 스캔 (가이드 7.3: gap_pct = 시가/전일종가 - 1).

    시장 전체를 "시가-전일종가 갭" 기준으로 랭킹하는 KIS TR은 없다. 대신
    코스피200+코스닥150 유니버스를 종목별로 KIS 현재가(FHKST01010100) 조회해
    직접 계산한다 — coverage=index_kospi200_kosdaq150(전체 시장 아님).
    """
    print("=" * 60)
    print(f"갭 상승 종목 스캔 ({min_gap}% 이상, 시장: {market})")
    print("=" * 60)

    universe = _load_universe(market)
    if not universe:
        print("\n  ❌ data/index_stocks.json이 없습니다.")
        print("  먼저 실행: python3 tools/update_index.py")
        return
    if not _kis_enabled():
        print("\n  ⚠️  KIS 미설정 — 종목별 시가/전일종가 조회에 KIS 현재가가 필요합니다.")
        print("  .env에 KIS_APP_KEY / KIS_APP_SECRET 설정 (.env.example 참고)")
        print()
        print(f'  대안 — WebSearch: "오늘 갭상승 종목 {_today_str()} 코스피 코스닥"')
        return

    print(f"\n  ✅ coverage=index_kospi200_kosdaq150 ({len(universe)}종목) — 전체 시장 아님")
    print(f"  종목별 KIS 현재가(FHKST01010100) 순차 조회 중... (약 {len(universe)*0.15:.0f}초 예상)")

    matched, failed = [], 0
    for i, stock in enumerate(universe):
        q = kis_quote(stock["code"], name=stock["name"])   # 호출 간격은 KisClient가 지킨다
        if not q or not q.get("open") or not q.get("prev_close"):
            failed += 1
            continue
        gap = calc_gap_pct(q["open"], q["prev_close"])
        if gap is not None and gap >= min_gap:
            matched.append({
                "code": stock["code"], "name": stock["name"],
                "price": q["price"], "open": q["open"], "prev_close": q["prev_close"],
                "gap_pct": gap, "change_pct": q.get("change_pct"),
            })

    matched.sort(key=lambda x: -x["gap_pct"])

    print(f"\n  대상 {len(universe)}종목 — 조회 성공 {len(universe)-failed}, 실패 {failed}, 조건충족 {len(matched)}")
    if not matched:
        print("  조건을 충족하는 종목이 없습니다.")
        return

    print(f"\n  {'종목명':<15} {'코드':<8} {'현재가':>8} {'갭':>7} {'시가':>8} {'전일종가':>8}")
    print(f"  {'─'*15} {'─'*8} {'─'*8} {'─'*7} {'─'*8} {'─'*8}")
    for it in matched[:20]:
        print(f"  {it['name'][:15]:<15} {it['code']:<8} {it['price']:>8,.0f}원 "
              f"{it['gap_pct']:>+6.1f}% {it['open']:>8,.0f} {it['prev_close']:>8,.0f}")


def cmd_oversold(rsi_max: float = 30, days: int = 5):
    """과매도 반등 후보 — watchlist 전체 종목 기반 (가이드 7.4).

    matched = rsi is not None and rsi <= rsi_max and declining(days 연속 하락, 보합 포함 시 제외)
    30개 제한 없이 전체 watchlist를 처리하고, 실패 종목은 개수로 보고한다.
    """
    print("=" * 60)
    print(f"과매도 반등 후보 스캔 (RSI ≤ {rsi_max}, {days}일 연속 하락)")
    print("=" * 60)

    if days < 1:
        print("\n  ❌ --days는 1 이상이어야 합니다.")
        return
    if not (0 <= rsi_max <= 100):
        print("\n  ❌ --rsi-max는 0~100 범위여야 합니다.")
        return

    if not os.path.exists(_WATCHLIST_FILE):
        print("\n  data/watchlist.json 없음. stock_screener.py 실행 시 자동 생성됩니다.")
        print("  또는 WebSearch로 확인:")
        print(f'  검색어: "RSI 과매도 반등 후보 코스피 코스닥 {_today_str()}"')
        return

    with open(_WATCHLIST_FILE, encoding="utf-8") as f:
        wl = json.load(f)
    raw_tickers = [sym for group in wl.values() for sym in group
                   if sym.isdigit() and len(sym) == 6]
    tickers = sorted(set(raw_tickers))  # 중복 제거
    print(f"\n  Watchlist 국내(6자리) 종목: {len(tickers)}개 (중복 제거 전 {len(raw_tickers)}개)")
    if not tickers:
        print("  국내(6자리 코드) 종목이 없습니다 — watchlist가 해외 종목 위주일 수 있습니다.")
        return

    prime_standard_cache(tickers)
    oversold, failed = [], []
    for ticker in tickers:  # 전체 처리 — 30개 제한 없음
        d = fetch_ohlcv(ticker)
        if not d or len(d["closes"]) < days + 1:
            failed.append(ticker)
            continue
        closes = d["closes"]
        rsi = calc_rsi(closes)
        declining = calc_oversold_streak(closes, days)
        if rsi is not None and declining is True and rsi <= rsi_max:
            obv = calc_obv(closes, d["volumes"])
            vp  = calc_volume_pressure(d["opens"], d["highs"], d["lows"], closes, d["volumes"])
            obv_bull = obv.get("divergence") == DIVERGENCE_BULLISH
            vp_bull  = vp.get("divergence") == DIVERGENCE_BULLISH
            return_nd = calc_return_nd(closes, days)

            oversold.append({
                "ticker":    ticker,
                "name":      d["name"],
                "rsi":       rsi,
                "close":     closes[-1],
                "return_nd": return_nd,
                "obv_sig":   "✅" if obv_bull else "  ",
                "vp_sig":    "✅" if vp_bull else "  ",
                "priority":  (2 if obv_bull else 0) + (2 if vp_bull else 0),
            })

    print(f"  조회 실패/데이터 부족: {len(failed)}개" + (f" ({', '.join(failed[:10])}{'...' if len(failed) > 10 else ''})" if failed else ""))

    if oversold:
        # OBV/추정압력 다이버전스 우선 정렬, 동점이면 RSI 낮은 순
        oversold.sort(key=lambda x: (-x["priority"], x["rsi"]))
        print(f"\n  {'종목명':<15} {'코드':<8} {'현재가':>8} {'RSI':>6} {f'{days}일수익률':>10} {'OBV':>5} {'압력':>5}")
        print(f"  {'─'*15} {'─'*8} {'─'*8} {'─'*6} {'─'*10} {'─'*5} {'─'*5}")
        for s in oversold:
            ret_str = f"{s['return_nd']:>+9.1f}%" if s['return_nd'] is not None else "      -   "
            print(f"  {s['name']:<15} {s['ticker']:<8} "
                  f"{s['close']:>8,.0f}원 {s['rsi']:>6.1f} {ret_str}"
                  f" {s['obv_sig']:>5} {s['vp_sig']:>5}")
        print(f"\n  ✅ = 강세 다이버전스 확인됨 (OBV/추정압력 상승 + 가격 하락 → 반등 기대)")
    else:
        print(f"\n  RSI {rsi_max} 이하 + {days}일 연속 하락 조건을 모두 충족하는 종목 없음")


def score_stock(ticker: str) -> dict:
    """단일 종목에 100점 모델(가이드 8단계)을 적용한다 — 실제 API 조회 포함.

    1차 필터(가이드 8.1)는 확인 가능한 것만 적용한다: 가격·시가총액·추정
    거래대금. 관리종목/ETF 여부를 확인할 공식 필드는 아직 찾지 못해
    필터링하지 않고 "미검증"으로만 표시한다(추측 금지 원칙).
    """
    if not _kis_enabled():
        return {"status": "ERROR", "reason": "KIS API 미설정"}

    quote = kis_quote(ticker)
    if not quote or not quote.get("price"):
        return {"status": "ERROR", "reason": "시세 조회 실패(FHKST01010100)"}

    d = fetch_ohlcv(ticker)
    if not d or len(d["closes"]) < 21:
        return {"status": "ERROR", "reason": "일봉 데이터 부족(21봉 미만)"}

    closes, highs, lows, opens, volumes = d["closes"], d["highs"], d["lows"], d["opens"], d["volumes"]

    # 1차 필터 — 확인 가능한 항목만 (가이드 8.1 예시 임계값)
    prefilter_reasons = []
    if quote["price"] < 1000:
        prefilter_reasons.append(f"가격 {quote['price']:,.0f}원 < 1,000원")
    if quote.get("market_cap_krw") is None:
        prefilter_reasons.append("시가총액 미확인")
    elif quote["market_cap_krw"] < 100_000_000_000:
        prefilter_reasons.append(f"시가총액 {quote['market_cap_krw']/1e8:,.0f}억원 < 1,000억원")
    # KIS 현재가의 누적 거래대금(acml_tr_pbmn)이 있으면 그 값, 없으면 가격×거래량 추정
    turnover = quote.get("traded_value")
    turnover_is_estimate = turnover is None
    if turnover_is_estimate:
        turnover = quote["price"] * quote["volume"]
    if turnover < 5_000_000_000:
        label = "추정 거래대금" if turnover_is_estimate else "거래대금"
        prefilter_reasons.append(f"{label} {turnover/1e8:,.0f}억원 < 50억원")

    # 외국인/기관 5일 합 + 당일 — 최근 4완료거래일 + 오늘(장중이면 당일 집계치).
    # 주말에는 "오늘"이 거래일이 아니므로 최근 5완료거래일을 쓴다.
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    if now.weekday() >= 5:
        dates_for_sum = [r["date"] for r in d["rows"][-5:]]
    else:
        trading_dates = [r["date"] for r in d["rows"][-4:]]
        dates_for_sum = trading_dates + [today_str] if (not trading_dates or trading_dates[-1] != today_str) else trading_dates

    foreign_sum = institution_sum = 0.0
    foreign_today = institution_today = None
    all_days_ok = True
    for dt_str in dates_for_sum:
        inv = kis_investor_flow(ticker, date=dt_str.replace("-", ""))
        if not inv:
            all_days_ok = False
        else:
            foreign_sum += inv["foreign"] or 0.0
            institution_sum += inv["institution"] or 0.0
            if dt_str == dates_for_sum[-1]:     # 평일 = 오늘, 주말 = 마지막 거래일
                foreign_today = inv["foreign"]
                institution_today = inv["institution"]

    foreign_5d = foreign_sum if all_days_ok else None
    institution_5d = institution_sum if all_days_ok else None

    areas = [
        score_trend(closes),
        score_momentum(closes, highs, lows),
        score_volume_liquidity(volumes, turnover, turnover_is_estimate=turnover_is_estimate),
        score_foreign_institution(foreign_5d, institution_5d, foreign_today, institution_today),
        score_pattern_breakout(closes, highs, opens, lows),
        score_risk(closes, highs, lows),
    ]
    result = combine_score(areas)
    result["ticker"] = ticker
    result["name"] = quote["name"]
    result["as_of"] = quote["as_of"]
    result["prefilter_passed"] = not prefilter_reasons
    result["prefilter_reasons"] = prefilter_reasons
    return result


def cmd_score(ticker: str) -> bool:
    """100점 고정 배점 scoring 실행 (가이드 8단계, V1 검증 전 초기안).

    종료 코드(가이드 9단계 — "전체 성공과 부분 실패의 반환 상태/종료
    코드를 문서화"): status=ERROR(조회 자체 실패)일 때만 False를
    반환해 main()이 exit code 1로 종료한다. SUCCESS/INCOMPLETE는 둘 다
    "명령이 정상적으로 결과를 냈다"는 의미이므로 True다 — 데이터가
    부족해 INCOMPLETE가 나온 것은 도구 실패가 아니라 정직한 결과다.
    """
    print("=" * 60)
    print(f"100점 스코어링: {ticker}  [{SCORE_VERSION}]")
    print("=" * 60)
    print("  ⚠️  검증 전 초기 가설 모델입니다 — 매수 지시가 아닙니다 (가이드 8단계).")

    result = score_stock(ticker)
    if result.get("status") == "ERROR":
        print(f"\n  ❌ {result['reason']}")
        return False

    print(f"\n  종목: {result['name']} ({ticker})  as_of {result['as_of']}")

    if result["prefilter_passed"]:
        print(f"  1차 필터: ✅ 통과 (가격/시총/거래대금 확인됨 — 관리종목·ETF 여부는 미검증)")
    else:
        print(f"  1차 필터: ❌ 미통과 — {'; '.join(result['prefilter_reasons'])}")
        print(f"            (아래 점수는 참고용 — 필터 통과 종목과 동일 기준으로 비교하지 말 것)")

    area_labels = {
        "trend": "Trend", "momentum": "Momentum", "volume_liquidity": "Volume/Liquidity",
        "foreign_institution": "Foreign/Institution", "pattern_breakout": "Pattern/Breakout",
        "risk": "Risk",
    }
    print(f"\n  {'영역':<22} {'점수':>8} {'결측'}")
    print(f"  {'─'*22} {'─'*8} {'─'*20}")
    for a in result["areas"]:
        label = area_labels.get(a["area"], a["area"])
        missing_str = ", ".join(a["missing"]) if a["missing"] else "-"
        print(f"  {label:<22} {a['points']:>3}/{a['max']:<3} {missing_str}")

    print()
    if result["status"] == "SUCCESS":
        print(f"  총점: {result['total_score']}/100  →  {result['band']}")
        print(f"  (85+ 우선검토 / 75+ 후보 / 65+ 관찰 / 50+ 중립 / 50미만 우선순위낮음)")
    else:
        print(f"  총점: 산출 불가 (INCOMPLETE)")
        print(f"  확인된 점수: {result['known_points']}점 / 확인 가능 배점: {result['available_weight']}점")
        print(f"  결측 항목: {', '.join(result['missing_components'])}")
        print(f"  ⚠️  결측 종목을 100점 환산하거나 완전 데이터 종목과 같은 순위로 비교하지 마십시오.")

    return True


def cmd_quote(*tickers) -> bool:
    """KIS API로 현재가·수급 조회.

    종료 코드: KIS 미설정이거나 요청한 종목 중 하나라도 시세 조회에
    실패하면 False(exit code 1) — 여러 종목을 한 번에 조회할 때 일부만
    실패해도 전체 호출이 조용히 "성공"으로 보이지 않게 한다.
    """
    if not _kis_enabled():
        print("❌ KIS API 미연결")
        print("   .env 파일에 설정 (.env.example 참고):")
        print("     KIS_APP_KEY=앱키")
        print("     KIS_APP_SECRET=앱시크릿")
        print("   발급: https://apiportal.koreainvestment.com")
        return False

    all_ok = True
    for ticker in tickers:
        print(f"\n  [{ticker}] 현재가 (KIS FHKST01010100)")
        q = kis_quote(ticker)
        if q:
            chg_str = f"{q['change_pct']:+.2f}%" if q['change_pct'] is not None else "-"
            print(f"  종목명:  {q['name']}")
            print(f"  현재가:  {q['price']:,.0f}원 ({chg_str})")
            print(f"  시가/고가/저가: {q['open']:,.0f} / {q['high']:,.0f} / {q['low']:,.0f}원")
            if q.get("price_unsettled"):
                print(f"  ⚠️  16:00~다음 평일 09:00 — 현재가·고가·저가·등락률은 애프터마켓 값일 수 있음"
                      f"(정규장 종가는 indicators의 지표 기준 종가)")
            print(f"  전일종가: {q['prev_close']:,.0f}원")
            print(f"  거래량:  {_fmt(q['volume'])}주")
            if q.get("traded_value") is not None:
                print(f"  거래대금: {q['traded_value']/1e8:,.0f}억원")
            if q.get("market_cap_eok") is not None:
                print(f"  시가총액: {q['market_cap_eok']:,.0f}억원")
        else:
            print(f"  ❌ 조회 실패")
            all_ok = False

        # 오늘 행이 없으면(휴장일·장중 집계 전) 가장 최근 거래일 행을 날짜와 함께 보여 준다
        inv = kis_investor_flow(ticker)
        if not inv:
            history = kis_investor_history(ticker)
            inv = history[0] if history else None
        if inv:
            print(f"  [수급 — {inv['date']}, 단위: {inv['unit']}]")
            print(f"  개인:    {_fmt(inv['individual'])}")
            print(f"  외국인:  {_fmt(inv['foreign'])}")
            print(f"  기관:    {_fmt(inv['institution'])}")
        else:
            # 수급 데이터 결측은 종료 코드에는 반영하지 않는다(시세 조회 실패와는 구분).
            print(f"  [수급] 데이터 없음")

    return all_ok


def cmd_kis_realtime(*tickers) -> bool:
    """quote의 옛 이름 — 이제 quote도 KIS를 쓴다."""
    print("⚠️  kis-realtime은 quote의 옛 이름입니다 — quote로 실행합니다.")
    return cmd_quote(*tickers)


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def main():
    # Windows 환경 한글 깨짐 방지 — CLI 진입점에서만 적용(가이드 9단계 항목:
    # import 시 stdout/stderr를 바꾸면 pytest 등 다른 도구의 캡처와 충돌한다).
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="당일 투자 후보 스캐너 — 기술적 분석 + 증권사 API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""사용 예시:
  python3 daily_scanner.py check-market
  python3 daily_scanner.py check-broker
  python3 daily_scanner.py quote 005930 000660
  python3 daily_scanner.py score 005930                # 100점 scoring (V1 검증 전)
  python3 daily_scanner.py indicators 009150
  python3 daily_scanner.py indicators 005930 000660 009150
  python3 daily_scanner.py volume-surge
  python3 daily_scanner.py volume-surge --ratio 5.0 --market kospi
  python3 daily_scanner.py new-high
  python3 daily_scanner.py new-high --period 20d --market kosdaq
  python3 daily_scanner.py gap-up --min-gap 5.0
  python3 daily_scanner.py oversold --rsi-max 25
  python3 daily_scanner.py kis-realtime 005930 000660   # quote의 옛 이름

종료 코드(quote/score만 해당 — 그 외 명령은 항상 0):
  0: 정상 (score의 INCOMPLETE 포함 — 데이터 부족은 도구 실패가 아님)
  1: 요청한 종목 중 하나라도 조회 자체가 실패했거나(quote), KIS 미설정
     /시세·일봉 조회 실패로 점수를 낼 수 없었을 때(score)
"""
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("check-market",  help="장 운영 여부 및 API 상태 확인")
    sub.add_parser("check-broker",  help="증권사 API 연결 테스트")

    p_q = sub.add_parser("quote", help="KIS 현재가·수급 조회 (여러 종목 동시 가능)")
    p_q.add_argument("tickers", nargs="+", help="종목 코드 1개 이상")

    p_score = sub.add_parser("score", help="100점 고정 배점 scoring (V1 검증 전 초기안)")
    p_score.add_argument("ticker", help="종목 코드 1개")

    p_ind = sub.add_parser("indicators", help="기술적 지표 전체 계산 (여러 종목 동시 가능)")
    p_ind.add_argument("tickers", nargs="+", help="종목 코드 1개 이상 (예: 005930 000660 009150)")

    p_vol = sub.add_parser("volume-surge", help="거래량 급증 종목")
    p_vol.add_argument("--ratio",  type=float, default=3.0, help="직전 20일 평균 대비 배수 최소값 (기본 3.0)")
    p_vol.add_argument("--min-cap",type=float, default=500, help="최소 시총 억원 (기본 500)")
    p_vol.add_argument("--market", choices=["all", "kospi", "kosdaq"], default="all", help="시장 구분 (기본 all)")

    p_nh  = sub.add_parser("new-high",   help="신고가 돌파 종목")
    p_nh.add_argument("--period", default="52w", choices=list(_NEW_HIGH_PERIOD_MAP.keys()),
                       help="직전 N거래일 (기본 52w = 250거래일)")
    p_nh.add_argument("--market", choices=["all", "kospi", "kosdaq"], default="all", help="시장 구분 (기본 all)")

    p_gap = sub.add_parser("gap-up",     help="갭 상승 종목")
    p_gap.add_argument("--min-gap", type=float, default=3.0, help="최소 갭 %% (기본 3.0)")
    p_gap.add_argument("--market", choices=["all", "kospi", "kosdaq"], default="all", help="시장 구분 (기본 all)")

    p_os  = sub.add_parser("oversold",   help="과매도 반등 후보")
    p_os.add_argument("--rsi-max", type=float, default=30,  help="RSI 최대값 (기본 30)")
    p_os.add_argument("--days",    type=int,   default=5,   help="연속 하락일 (기본 5)")

    p_kis = sub.add_parser("kis-realtime", help="quote의 옛 이름")
    p_kis.add_argument("tickers", nargs="+",   help="종목 코드 목록")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "check-market":
        cmd_check_market()
    elif args.command == "check-broker":
        cmd_check_broker()
    elif args.command == "quote":
        if not cmd_quote(*args.tickers):
            sys.exit(1)
    elif args.command == "score":
        if not cmd_score(args.ticker):
            sys.exit(1)
    elif args.command == "indicators":
        prime_standard_cache(args.tickers)
        for ticker in args.tickers:
            cmd_indicators(ticker)
            if len(args.tickers) > 1:
                print()  # 종목 간 빈 줄 구분
    elif args.command == "volume-surge":
        cmd_volume_surge(args.ratio, args.min_cap, args.market)
    elif args.command == "new-high":
        cmd_new_high(args.period, args.market)
    elif args.command == "gap-up":
        cmd_gap_up(args.min_gap, args.market)
    elif args.command == "oversold":
        cmd_oversold(args.rsi_max, args.days)
    elif args.command == "kis-realtime":
        cmd_kis_realtime(*args.tickers)


if __name__ == "__main__":
    main()

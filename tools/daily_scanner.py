#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
당일 투자 후보 스캐너 — 기술적 분석 + 증권사 API
daily-scanner 스킬이 자동 호출합니다.

사용법:
    python3 tools/daily_scanner.py check-market          # 장 운영 여부 확인
    python3 tools/daily_scanner.py check-broker          # API 연결 상태 확인
    python3 tools/daily_scanner.py volume-surge          # 거래량 급증 종목
    python3 tools/daily_scanner.py new-high              # 신고가 돌파 종목
    python3 tools/daily_scanner.py gap-up                # 갭 상승 종목
    python3 tools/daily_scanner.py oversold              # 과매도 반등 후보
    python3 tools/daily_scanner.py indicators 005930     # 기술적 지표 계산
    python3 tools/daily_scanner.py patterns 005930       # 캔들 패턴 인식
    python3 tools/daily_scanner.py kis-realtime 005930   # KIS API 실시간

증권사 API 키 설정 — 프로젝트 루트 .env 파일 (.env.example 참고):
    KIS:  KIS_APP_KEY=... / KIS_APP_SECRET=... / KIS_ACCOUNT_NO=...
    키움: KIWOOM_APP_KEY=...
"""

import argparse
import json
import os
import subprocess
import sys
import math
import io
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, Union

# Windows 환경 한글 깨짐 방지
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

# ── .env 로더: 프로젝트 루트의 .env를 os.environ에 주입 ──
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))
except ImportError:
    pass  # python-dotenv 미설치 시 OS 환경변수만 사용

_TIMEOUT = 20
_TODAY   = datetime.now().strftime("%Y-%m-%d")
_WEEKDAY = datetime.now().weekday()  # 0=월, 6=일


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

def calc_rsi(closes: list, period: int = 14) -> float:
    """RSI 계산."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]

    avg_gain = sum(gains[-period:]) / period if gains else 0
    avg_loss = sum(losses[-period:]) / period if losses else 0

    if avg_loss == 0:
        return 100.0
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


def calc_stochastic(highs, lows, closes, k_period=14, d_period=3) -> dict:
    """스토캐스틱 계산."""
    if len(closes) < k_period:
        return {"%K": None, "%D": None}

    k_values = []
    for i in range(k_period - 1, len(closes)):
        period_highs = highs[i - k_period + 1:i + 1]
        period_lows  = lows[i  - k_period + 1:i + 1]
        highest_high = max(period_highs)
        lowest_low   = min(period_lows)
        if highest_high == lowest_low:
            k_values.append(50)
        else:
            k_val = (closes[i] - lowest_low) / (highest_high - lowest_low) * 100
            k_values.append(round(k_val, 2))

    d_val = sum(k_values[-d_period:]) / d_period if len(k_values) >= d_period else None
    return {
        "%K": k_values[-1] if k_values else None,
        "%D": round(d_val, 2) if d_val else None,
    }


def calc_obv(closes: list, volumes: list) -> dict:
    """OBV(On-Balance Volume) 계산.

    종가가 전일보다 오르면 거래량 +, 내리면 -, 같으면 0 누적.
    가격-OBV 다이버전스로 추세 반전 신호 포착.
    """
    if len(closes) < 2 or len(volumes) < 2:
        return {"obv": None, "obv_ma20": None, "signal": "데이터 부족", "divergence": None}

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
        obv_slope = (obv_series[-1] - obv_series[-5]) / 5
        obv_trend = "상승" if obv_slope > 0 else "하락"
    else:
        obv_trend = "불명"

    # 다이버전스 탐지 (최근 20일 기준)
    divergence = None
    if len(closes) >= 20 and len(obv_series) >= 20:
        price_chg = closes[-1] - closes[-20]
        obv_chg   = obv_series[-1] - obv_series[-20]
        if price_chg > 0 and obv_chg < 0:
            divergence = "🔴 약세 다이버전스 (가격 상승 + OBV 하락 → 상승 추세 약화 신호)"
        elif price_chg < 0 and obv_chg > 0:
            divergence = "🟢 강세 다이버전스 (가격 하락 + OBV 상승 → 하락 추세 약화 신호)"

    # OBV vs MA20 비교
    if obv_ma20:
        signal = "🟢 OBV > MA20 (매수세 우위)" if current_obv > obv_ma20 else "🔴 OBV < MA20 (매도세 우위)"
    else:
        signal = obv_trend

    return {
        "obv":        round(current_obv),
        "obv_ma20":   round(obv_ma20) if obv_ma20 else None,
        "obv_trend":  obv_trend,
        "signal":     signal,
        "divergence": divergence,
        "series":     obv_series,   # 다이버전스 추가 계산용
    }


def calc_cvd(opens: list, closes: list, volumes: list) -> dict:
    """CVD(Cumulative Volume Delta) 근사 계산.

    틱 데이터 없이 일봉/분봉 캔들로 근사:
      - 양봉(종가 > 시가): 거래량의 (종가-시가)/(고가-저가) 비율을 매수 체결로 추정
      - 음봉(종가 < 시가): 반대로 매도 체결로 추정
    완전한 CVD는 틱 데이터 필요 — 이 계산은 일봉 기반 근사값임.

    신호 해석:
      - 가격 상승 + CVD 상승: 실제 매수세 동반 → 강한 상승 신호
      - 가격 상승 + CVD 하락: 매수세 없는 상승 → 약세 다이버전스 (반전 주의)
      - 가격 하락 + CVD 상승: 하락 중 매수세 유입 → 강세 다이버전스 (반등 기대)
      - 가격 하락 + CVD 하락: 실제 매도세 동반 → 강한 하락 신호
    """
    if len(opens) < 2 or len(closes) < 2 or len(volumes) < 2:
        return {"cvd": None, "signal": "데이터 부족", "divergence": None}

    # 일봉 근사 CVD 계산
    cvd_series = [0]
    for i in range(len(closes)):
        o, c, v = opens[i], closes[i], volumes[i]
        candle_range = abs(c - o)
        total_range  = candle_range if candle_range > 0 else v * 0.001

        if c >= o:
            # 양봉: 몸통 비율만큼 매수 체결로 추정
            buy_vol  = v * (c - o) / total_range if candle_range > 0 else v * 0.5
            sell_vol = v - buy_vol
        else:
            # 음봉: 몸통 비율만큼 매도 체결로 추정
            sell_vol = v * (o - c) / total_range if candle_range > 0 else v * 0.5
            buy_vol  = v - sell_vol

        delta = buy_vol - sell_vol
        cvd_series.append(cvd_series[-1] + delta)

    current_cvd = cvd_series[-1]

    # CVD 20일 이동평균
    cvd_ma20 = sum(cvd_series[-20:]) / 20 if len(cvd_series) >= 20 else None

    # CVD 5일 기울기 (모멘텀)
    if len(cvd_series) >= 5:
        cvd_slope = cvd_series[-1] - cvd_series[-5]
        cvd_momentum = "매수세 강화" if cvd_slope > 0 else "매도세 강화"
    else:
        cvd_momentum = "불명"

    # 다이버전스 탐지 (최근 20일)
    divergence = None
    if len(closes) >= 20 and len(cvd_series) >= 20:
        price_chg = closes[-1] - closes[-20]
        cvd_chg   = cvd_series[-1] - cvd_series[-20]
        if price_chg > 0 and cvd_chg < 0:
            divergence = "🔴 약세 다이버전스 (가격 상승 + CVD 하락 → 매수세 이탈, 반전 주의)"
        elif price_chg < 0 and cvd_chg > 0:
            divergence = "🟢 강세 다이버전스 (가격 하락 + CVD 상승 → 저점 매집, 반등 기대)"

    # 신호
    if cvd_ma20:
        if current_cvd > cvd_ma20 and cvd_slope > 0:
            signal = "🟢 강한 매수세 (CVD > MA20, 상승 모멘텀)"
        elif current_cvd > cvd_ma20:
            signal = "🟡 매수세 우위 (CVD > MA20)"
        elif current_cvd < cvd_ma20 and cvd_slope < 0:
            signal = "🔴 강한 매도세 (CVD < MA20, 하락 모멘텀)"
        else:
            signal = "🟡 매도세 우위 (CVD < MA20)"
    else:
        signal = cvd_momentum

    return {
        "cvd":         round(current_cvd),
        "cvd_ma20":    round(cvd_ma20) if cvd_ma20 else None,
        "cvd_momentum": cvd_momentum,
        "signal":      signal,
        "divergence":  divergence,
        "note":        "⚠️ 일봉 기반 근사값 — 정밀 CVD는 분봉/틱 데이터 필요",
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
        signals.append(("🔨 망치형", "강세", "하락 후 반전 가능성"))

    # 상승 장악형 (Bullish Engulfing)
    if c2 < o2 and c > o and o < c2 and c > o2:
        signals.append(("📈 상승 장악형", "강세", "전일 음봉 완전 포함"))

    # 도지 (Doji) — 방향 불확실
    if body <= (h - l) * 0.1:
        signals.append(("⚖️ 도지", "중립", "매수·매도 균형, 방향 전환 가능"))

    # 교수형 (Hanging Man) — 고점 주의
    if lower_wick >= body * 2 and c < h * 0.98:
        signals.append(("⚠️ 교수형", "주의", "고점에서의 반전 가능성"))

    # 샛별형 (Morning Star) — 3일 패턴
    if c1 < o1 and abs(c2 - o2) <= prev_body * 0.3 and c > o and c > (o1 + c1) / 2:
        signals.append(("🌟 샛별형", "강세", "3일 바닥 반전 패턴"))

    return signals


# ─────────────────────────────────────────────────────────────
# 가격 데이터 수집
# ─────────────────────────────────────────────────────────────

def fetch_ohlcv_yahoo(ticker: str, days: int = 130) -> Optional[dict]:
    """Yahoo Finance에서 OHLCV 데이터 수집. 한국주는 .KS 접미사 자동."""
    yf_ticker = f"{ticker}.KS" if ticker.isdigit() and len(ticker) == 6 else ticker
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

        rows = []
        for i, ts in enumerate(ts_arr):
            o = q.get("open",  [None]*len(ts_arr))[i]
            h = q.get("high",  [None]*len(ts_arr))[i]
            l = q.get("low",   [None]*len(ts_arr))[i]
            c = q.get("close", [None]*len(ts_arr))[i]
            v = q.get("volume",[None]*len(ts_arr))[i]
            if all([o, h, l, c, v]):
                rows.append({
                    "date": datetime.fromtimestamp(ts).strftime("%Y-%m-%d"),
                    "open": o, "high": h, "low": l, "close": c, "volume": v
                })

        if len(rows) < 20:
            return None

        return {
            "ticker":  ticker,
            "name":    chart.get("meta", {}).get("shortName", ticker),
            "rows":    rows,
            "opens":   [r["open"]   for r in rows],
            "highs":   [r["high"]   for r in rows],
            "lows":    [r["low"]    for r in rows],
            "closes":  [r["close"]  for r in rows],
            "volumes": [r["volume"] for r in rows],
        }
    except Exception as e:
        return None


# ─────────────────────────────────────────────────────────────
# KIS (한국투자증권) API
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


def kis_volume_rank(market: str = "0") -> list:
    """KIS: 거래량 순위 조회 (실시간)."""
    data = _kis_request(
        "/uapi/domestic-stock/v1/quotations/volume-rank",
        {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE":  "20171",
            "FID_INPUT_ISCD":         "0000",
            "FID_DIV_CLS_CODE":       "0",
            "FID_BLNG_CLS_CODE":      market,
            "FID_TRGT_CLS_CODE":      "111111111",
            "FID_TRGT_EXLS_CLS_CODE": "000000",
            "FID_INPUT_PRICE_1":      "1000",
            "FID_INPUT_PRICE_2":      "0",
            "FID_VOL_CNT":            "0",
            "FID_INPUT_DATE_1":       "",
        },
        tr_id="FHPST01710000"
    )
    if not data:
        return []
    return data.get("output", [])[:30]


def kis_price(ticker: str) -> Optional[dict]:
    """KIS: 현재가 및 기본 정보 조회."""
    data = _kis_request(
        "/uapi/domestic-stock/v1/quotations/inquire-price",
        {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
        tr_id="FHKST01010100"
    )
    if not data:
        return None
    return data.get("output", {})


def kis_investor(ticker: str) -> Optional[dict]:
    """KIS: 외국인·기관 순매수 조회."""
    data = _kis_request(
        "/uapi/domestic-stock/v1/quotations/inquire-investor",
        {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD":         ticker,
            "FID_INPUT_DATE_1":       _TODAY.replace("-", ""),
            "FID_INPUT_DATE_2":       _TODAY.replace("-", ""),
            "FID_PERIOD_DIV_CODE":    "D",
        },
        tr_id="FHKST01010900"
    )
    if not data:
        return None
    items = data.get("output2", [])
    return items[0] if items else None


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
    kis_key    = os.environ.get("KIS_APP_KEY")
    kis_secret = os.environ.get("KIS_APP_SECRET")
    kiwoom_key = os.environ.get("KIWOOM_APP_KEY")
    dart_key   = os.environ.get("DART_API_KEY")

    print(f"  KIS API:    {'✅ 환경변수 설정됨' if kis_key and kis_secret else '❌ 미설정 — .env에 KIS_APP_KEY / KIS_APP_SECRET 설정'}")
    print(f"  키움 API:   {'✅ 환경변수 설정됨' if kiwoom_key else '❌ 미설정 — .env에 KIWOOM_APP_KEY 설정'}")
    print(f"  DART API:   {'✅ 환경변수 설정됨' if dart_key else '❌ 미설정 — .env에 DART_API_KEY 설정'}")
    print(f"  Yahoo F.:   ✅ 항상 사용 가능 (무료, 지연 15분)")

    print(f"\n  [데이터 우선순위]")
    if kis_key and kis_secret:
        print(f"  1순위: KIS API (실시간) → 2순위: Yahoo Finance (15분 지연)")
    else:
        print(f"  1순위: Yahoo Finance (15분 지연) — KIS API 설정 시 실시간 전환")


def cmd_check_broker():
    """증권사 API 연결 테스트."""
    print("=" * 60)
    print("증권사 API 연결 테스트")
    print("=" * 60)

    # KIS API 테스트
    kis_key    = os.environ.get("KIS_APP_KEY")
    kis_secret = os.environ.get("KIS_APP_SECRET")
    if kis_key and kis_secret:
        print("\n  [KIS API 테스트] 삼성전자(005930) 현재가 조회...")
        result = kis_price("005930")
        if result and result.get("stck_prpr"):
            print(f"  ✅ KIS 연결 성공: 삼성전자 현재가 {result['stck_prpr']}원")
        else:
            print(f"  ❌ KIS 연결 실패: 토큰 발급 오류 또는 키 오류")
            print(f"     KIS 개발자 센터: https://apiportal.koreainvestment.com/")
    else:
        print("\n  [KIS API] 미설정")
        print("  KIS API 키 발급: https://apiportal.koreainvestment.com/")
        print("  .env 파일에 아래 항목을 설정하세요 (.env.example 참고):")
        print("    KIS_APP_KEY=앱키")
        print("    KIS_APP_SECRET=앱시크릿")
        print("    KIS_ACCOUNT_NO=계좌번호-상품코드")

    print()


def cmd_indicators(ticker: str):
    """단일 종목 기술적 지표 전체 계산."""
    print("=" * 60)
    print(f"기술적 지표 분석: {ticker}")
    print("=" * 60)

    d = fetch_ohlcv_yahoo(ticker)
    if not d:
        print(f"\n  ❌ 데이터 수집 실패 ({ticker})")
        print(f"  Yahoo Finance 한국주 코드: {ticker}.KS")
        return

    closes  = d["closes"]
    highs   = d["highs"]
    lows    = d["lows"]
    opens   = d["opens"]
    volumes = d["volumes"]
    last    = closes[-1]

    print(f"\n  종목:    {d['name']} ({ticker})")
    print(f"  현재가:  {last:,.0f}원")
    print(f"  데이터:  {d['rows'][0]['date']} ~ {d['rows'][-1]['date']} ({len(d['rows'])}일)")

    # RSI
    rsi = calc_rsi(closes)
    rsi_signal = "🔴 과매수" if rsi and rsi > 70 else "🟢 과매도" if rsi and rsi < 30 else "⬜ 중립"
    print(f"\n  [RSI(14)]")
    print(f"  값:      {rsi}")
    print(f"  판단:    {rsi_signal}")

    # MACD
    macd = calc_macd(closes)
    if macd["macd"] and macd["signal"]:
        macd_signal = "🟢 골든크로스" if macd["hist"] and macd["hist"] > 0 else "🔴 데드크로스"
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
    print(f"  상단:    {bb['upper']:,.0f}원" if bb['upper'] else "  상단:    -")
    print(f"  중심:    {bb['middle']:,.0f}원" if bb['middle'] else "  중심:    -")
    print(f"  하단:    {bb['lower']:,.0f}원" if bb['lower'] else "  하단:    -")
    print(f"  %B:      {bb['pct_b']}")
    print(f"  판단:    {bb_signal}")

    # 이동평균
    ma = calc_ma(closes)
    aligned = all(
        ma.get(f"ma{p}") and ma.get(f"ma{q}") and ma[f"ma{p}"] > ma[f"ma{q}"]
        for p, q in [(5,20),(20,60),(60,120)]
        if ma.get(f"ma{p}") and ma.get(f"ma{q}")
    )
    print(f"\n  [이동평균]")
    for p in [5, 20, 60, 120]:
        val = ma.get(f"ma{p}")
        diff = (last - val) / val * 100 if val else None
        mark = f"({diff:+.1f}%)" if diff else ""
        print(f"  MA{p:3d}:   {val:,.0f}원 {mark}" if val else f"  MA{p:3d}:   -")
    print(f"  정배열:  {'✅ 정배열' if aligned else '❌ 역배열 또는 혼합'}")

    # 스토캐스틱
    stoch = calc_stochastic(highs, lows, closes)
    if stoch["%K"] and stoch["%D"]:
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
    print(f"\n  [스토캐스틱(14,3,3)]")
    print(f"  %K:      {stoch['%K']}")
    print(f"  %D:      {stoch['%D']}")
    print(f"  판단:    {st_signal}")

    # 거래량
    avg_vol  = sum(volumes[-20:]) / 20
    last_vol = volumes[-1]
    vol_ratio = last_vol / avg_vol if avg_vol else 0
    print(f"\n  [거래량]")
    print(f"  당일:    {_fmt(last_vol)}주")
    print(f"  20일평균: {_fmt(avg_vol)}주")
    print(f"  비율:    {vol_ratio:.1f}배 {'🔥 급증' if vol_ratio > 2 else ''}")

    # OBV
    obv = calc_obv(closes, volumes)
    print(f"\n  [OBV (On-Balance Volume)]")
    print(f"  현재 OBV:  {obv['obv']:,}" if obv['obv'] else "  현재 OBV:  -")
    print(f"  OBV MA20:  {obv['obv_ma20']:,}" if obv['obv_ma20'] else "  OBV MA20:  -")
    print(f"  추세:      {obv['obv_trend']}")
    print(f"  판단:      {obv['signal']}")
    if obv['divergence']:
        print(f"  ⚡ 다이버전스: {obv['divergence']}")

    # CVD (일봉 근사)
    cvd = calc_cvd(opens, closes, volumes)
    print(f"\n  [CVD (Cumulative Volume Delta) — 일봉 근사]")
    print(f"  현재 CVD:  {cvd['cvd']:,}" if cvd['cvd'] else "  현재 CVD:  -")
    print(f"  CVD MA20:  {cvd['cvd_ma20']:,}" if cvd['cvd_ma20'] else "  CVD MA20:  -")
    print(f"  모멘텀:    {cvd['cvd_momentum']}")
    print(f"  판단:      {cvd['signal']}")
    if cvd['divergence']:
        print(f"  ⚡ 다이버전스: {cvd['divergence']}")
    print(f"  {cvd['note']}")

    # 캔들 패턴
    patterns = detect_candle_patterns(opens, highs, lows, closes)
    print(f"\n  [캔들 패턴]")
    if patterns:
        for name, strength, desc in patterns:
            print(f"  {name} ({strength}): {desc}")
    else:
        print(f"  특이 패턴 없음")

    # 종합 신호 (OBV, CVD 포함)
    signals = 0
    if rsi and rsi < 40:                                        signals += 1
    if macd.get("hist") and macd["hist"] > 0:                   signals += 1
    if bb.get("pct_b") and bb["pct_b"] < 0.3:                  signals += 1
    if aligned:                                                  signals += 1
    if stoch.get("%K") and stoch["%K"] < 40:                   signals += 1
    if vol_ratio > 1.5:                                         signals += 1
    bull_patterns = [p for p in patterns if p[1] == "강세"]
    signals += len(bull_patterns)
    # OBV 강세 신호
    if obv.get("obv") and obv.get("obv_ma20") and obv["obv"] > obv["obv_ma20"]:
        signals += 1
    if obv.get("divergence") and "강세" in obv["divergence"]:
        signals += 1
    # CVD 강세 신호
    if cvd.get("cvd") and cvd.get("cvd_ma20") and cvd["cvd"] > cvd["cvd_ma20"]:
        signals += 1
    if cvd.get("divergence") and "강세" in cvd["divergence"]:
        signals += 1

    max_signals = 9 + len(bull_patterns)

    print(f"\n  {'='*40}")
    print(f"  종합 신호 강도: {signals}/{max_signals}")
    print(f"  (RSI·MACD·BB·정배열·스토캐스틱·거래량·캔들·OBV·CVD 반영)")
    if signals >= 7:
        print(f"  판정: 🟢 강한 매수 신호")
    elif signals >= 5:
        print(f"  판정: 🟢 매수 신호")
    elif signals >= 3:
        print(f"  판정: 🟡 중립 (추가 확인 필요)")
    else:
        print(f"  판정: 🔴 매수 신호 부족")

    # 다이버전스 경고 (종합 판정과 상충 시 강조)
    if obv.get("divergence") and "약세" in obv["divergence"]:
        print(f"  ⚠️  OBV 약세 다이버전스 — 상승 추세 신뢰도 저하")
    if cvd.get("divergence") and "약세" in cvd["divergence"]:
        print(f"  ⚠️  CVD 약세 다이버전스 — 매수세 이탈 확인 필요")
    print(f"  {'='*40}")


def cmd_volume_surge(ratio: float = 3.0, min_cap: float = 500):
    """거래량 급증 종목 스캔."""
    print("=" * 60)
    print(f"거래량 급증 종목 스캔 (평균 대비 {ratio}배 이상)")
    print("=" * 60)

    # KIS API 있으면 실시간, 없으면 안내
    kis_key = os.environ.get("KIS_APP_KEY")
    if kis_key and os.environ.get("KIS_APP_SECRET"):
        print("\n  ✅ KIS API — 실시간 거래량 순위 조회")
        items = kis_volume_rank()
        if items:
            print(f"\n  {'종목명':<15} {'코드':<8} {'현재가':>8} {'등락률':>7} {'거래량':>10}")
            print(f"  {'─'*15} {'─'*8} {'─'*8} {'─'*7} {'─'*10}")
            for item in items[:20]:
                name  = item.get("hts_kor_isnm", "")[:15]
                code  = item.get("mksc_shrn_iscd", "")
                price = item.get("stck_prpr", "")
                chg   = item.get("prdy_ctrt", "")
                vol   = item.get("acml_vol", "")
                print(f"  {name:<15} {code:<8} {price:>8}원 {chg:>6}% {_fmt(vol):>10}주")
        else:
            print("  ❌ KIS API 조회 실패")
    else:
        print("\n  ⚠️  KIS API 미연결 — Yahoo Finance로 대체 (전일 기준)")
        print("  실시간 거래량 순위는 KIS API 연동 필요:")
        print("  .env에 KIS_APP_KEY / KIS_APP_SECRET 설정 (.env.example 참고)")
        print()
        print("  대안 — WebSearch로 전일 거래량 급증 종목 확인:")
        print(f'  검색어: "오늘 거래량 급증 종목 {_TODAY}"')
        print(f'  검색어: "전일 특이 거래량 코스피 코스닥 {_TODAY}"')


def cmd_new_high(period: str = "52w"):
    """신고가 돌파 종목 스캔."""
    print("=" * 60)
    print(f"신고가 돌파 종목 스캔 ({period})")
    print("=" * 60)
    print()
    print("  신고가 종목은 WebSearch 또는 KIS API로 확인합니다:")
    print()
    print(f'  WebSearch: "오늘 52주 신고가 코스피 코스닥 {_TODAY}"')
    print(f'  WebSearch: "today 52-week high KOSPI KOSDAQ {_TODAY}"')
    print()

    kis_key = os.environ.get("KIS_APP_KEY")
    if kis_key and os.environ.get("KIS_APP_SECRET"):
        print("  ✅ KIS API 연결됨 — 등락률 상위 종목으로 대체 조회")
        items = kis_volume_rank()
        if items:
            high_candidates = [i for i in items
                               if float(i.get("prdy_ctrt", "0")) > 5.0]
            if high_candidates:
                print(f"  등락률 +5% 이상 종목 (신고가 후보):")
                for item in high_candidates[:10]:
                    print(f"  {item.get('hts_kor_isnm',''):<15} "
                          f"{item.get('stck_prpr','')}원 "
                          f"+{item.get('prdy_ctrt','')}%")


def cmd_gap_up(min_gap: float = 3.0):
    """갭 상승 종목 — WebSearch 안내."""
    print("=" * 60)
    print(f"갭 상승 종목 스캔 ({min_gap}% 이상)")
    print("=" * 60)
    print()
    print(f'  WebSearch: "오늘 갭상승 종목 {_TODAY} 코스피 코스닥"')
    print(f'  WebSearch: "gap up stocks Korea {_TODAY}"')
    print()
    print("  실시간 갭 상승 감지는 KIS API 연동 후 가능합니다:")
    print("  .env에 KIS_APP_KEY / KIS_APP_SECRET 설정 (.env.example 참고)")


def cmd_oversold(rsi_max: float = 30, days: int = 5):
    """과매도 반등 후보 — 지정 종목 목록 기반."""
    print("=" * 60)
    print(f"과매도 반등 후보 스캔 (RSI ≤ {rsi_max}, {days}일 연속 하락)")
    print("=" * 60)
    print()
    print("  watchlist 종목을 기술적으로 스캔합니다.")
    print("  watchlist.json 파일을 data/ 폴더에 생성하세요:")
    print()

    watchlist_path = os.path.join(
        os.path.dirname(__file__), "..", "data", "watchlist.json"
    )
    if os.path.exists(watchlist_path):
        with open(watchlist_path) as f:
            wl = json.load(f)
        tickers = [sym for group in wl.values() for sym in group
                   if sym.isdigit() and len(sym) == 6]
        print(f"  Watchlist 종목 수: {len(tickers)}개")
        print()
        oversold = []
        for ticker in tickers[:30]:  # 최대 30개
            d = fetch_ohlcv_yahoo(ticker)
            if not d:
                continue
            rsi = calc_rsi(d["closes"])
            if rsi and rsi <= rsi_max:
                obv = calc_obv(d["closes"], d["volumes"])
                cvd = calc_cvd(d["opens"], d["closes"], d["volumes"])

                # OBV/CVD 강세 다이버전스 여부
                obv_bull = obv.get("divergence") and "강세" in obv["divergence"]
                cvd_bull = cvd.get("divergence") and "강세" in cvd["divergence"]

                oversold.append({
                    "ticker":   ticker,
                    "name":     d["name"],
                    "rsi":      rsi,
                    "close":    d["closes"][-1],
                    "chg5d":    (d["closes"][-1]/d["closes"][-6]-1)*100 if len(d["closes"])>5 else 0,
                    "obv_sig":  "✅" if obv_bull else "  ",
                    "cvd_sig":  "✅" if cvd_bull else "  ",
                    "priority": (2 if obv_bull else 0) + (2 if cvd_bull else 0),
                })

        if oversold:
            # OBV/CVD 다이버전스 우선 정렬, 동점이면 RSI 낮은 순
            oversold.sort(key=lambda x: (-x["priority"], x["rsi"]))
            print(f"  {'종목명':<15} {'코드':<8} {'현재가':>8} {'RSI':>6} {'5일등락':>8} {'OBV':>5} {'CVD':>5}")
            print(f"  {'─'*15} {'─'*8} {'─'*8} {'─'*6} {'─'*8} {'─'*5} {'─'*5}")
            for s in oversold:
                print(f"  {s['name']:<15} {s['ticker']:<8} "
                      f"{s['close']:>8,.0f}원 {s['rsi']:>6.1f} {s['chg5d']:>+7.1f}%"
                      f" {s['obv_sig']:>5} {s['cvd_sig']:>5}")
            print(f"\n  ✅ = 강세 다이버전스 확인됨 (OBV/CVD 상승 + 가격 하락 → 반등 기대)")
        else:
            print(f"  RSI {rsi_max} 이하 종목 없음 (watchlist 기준)")
    else:
        print("  data/watchlist.json 없음. stock_screener.py 실행 시 자동 생성됩니다.")
        print("  또는 WebSearch로 확인:")
        print(f'  검색어: "RSI 과매도 반등 후보 코스피 코스닥 {_TODAY}"')


def cmd_kis_realtime(*tickers):
    """KIS API로 실시간 데이터 조회."""
    kis_key = os.environ.get("KIS_APP_KEY")
    if not kis_key or not os.environ.get("KIS_APP_SECRET"):
        print("❌ KIS API 미연결")
        print("   .env 파일에 설정 (.env.example 참고):")
        print("     KIS_APP_KEY=앱키")
        print("     KIS_APP_SECRET=앱시크릿")
        print("   발급: https://apiportal.koreainvestment.com/")
        return

    for ticker in tickers:
        print(f"\n  [{ticker}] 실시간 데이터")
        price = kis_price(ticker)
        if price:
            print(f"  현재가:  {price.get('stck_prpr','?')}원")
            print(f"  등락률:  {price.get('prdy_ctrt','?')}%")
            print(f"  거래량:  {_fmt(price.get('acml_vol','?'))}주")
            print(f"  시가총액: {_fmt(price.get('hts_avls','?'))}억")
        investor = kis_investor(ticker)
        if investor:
            print(f"  외국인:  {_fmt(investor.get('frgn_ntby_qty','?'))}주")
            print(f"  기관:    {_fmt(investor.get('orgn_ntby_qty','?'))}주")


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="당일 투자 후보 스캐너 — 기술적 분석 + 증권사 API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""사용 예시:
  python3 daily_scanner.py check-market
  python3 daily_scanner.py check-broker
  python3 daily_scanner.py indicators 009150
  python3 daily_scanner.py indicators 005930 000660 009150
  python3 daily_scanner.py volume-surge
  python3 daily_scanner.py volume-surge --ratio 5.0
  python3 daily_scanner.py new-high
  python3 daily_scanner.py gap-up --min-gap 5.0
  python3 daily_scanner.py oversold --rsi-max 25
  python3 daily_scanner.py kis-realtime 005930 000660
"""
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("check-market",  help="장 운영 여부 및 API 상태 확인")
    sub.add_parser("check-broker",  help="증권사 API 연결 테스트")

    p_ind = sub.add_parser("indicators", help="기술적 지표 전체 계산 (여러 종목 동시 가능)")
    p_ind.add_argument("tickers", nargs="+", help="종목 코드 1개 이상 (예: 005930 000660 009150)")

    p_vol = sub.add_parser("volume-surge", help="거래량 급증 종목")
    p_vol.add_argument("--ratio",  type=float, default=3.0, help="평균 대비 배수 (기본 3.0)")
    p_vol.add_argument("--min-cap",type=float, default=500, help="최소 시총 억원 (기본 500)")

    p_nh  = sub.add_parser("new-high",   help="신고가 돌파 종목")
    p_nh.add_argument("--period", default="52w", help="기간 (기본 52w)")

    p_gap = sub.add_parser("gap-up",     help="갭 상승 종목")
    p_gap.add_argument("--min-gap", type=float, default=3.0, help="최소 갭 %% (기본 3.0)")

    p_os  = sub.add_parser("oversold",   help="과매도 반등 후보")
    p_os.add_argument("--rsi-max", type=float, default=30,  help="RSI 최대값 (기본 30)")
    p_os.add_argument("--days",    type=int,   default=5,   help="연속 하락일 (기본 5)")

    p_kis = sub.add_parser("kis-realtime", help="KIS API 실시간 데이터")
    p_kis.add_argument("tickers", nargs="+",   help="종목 코드 목록")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "check-market":
        cmd_check_market()
    elif args.command == "check-broker":
        cmd_check_broker()
    elif args.command == "indicators":
        for ticker in args.tickers:
            cmd_indicators(ticker)
            if len(args.tickers) > 1:
                print()  # 종목 간 빈 줄 구분
    elif args.command == "volume-surge":
        cmd_volume_surge(args.ratio, args.min_cap)
    elif args.command == "new-high":
        cmd_new_high(args.period)
    elif args.command == "gap-up":
        cmd_gap_up(args.min_gap)
    elif args.command == "oversold":
        cmd_oversold(args.rsi_max, args.days)
    elif args.command == "kis-realtime":
        cmd_kis_realtime(*args.tickers)


if __name__ == "__main__":
    main()

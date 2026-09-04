#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KIS API 기반 CVD / OBV 전용 스캐너

KIS API로 분봉 데이터를 실시간으로 가져와서
정밀한 CVD(Cumulative Volume Delta)와 OBV(On-Balance Volume)를 계산합니다.

일봉 기반 daily_scanner.py의 근사값과 달리,
분봉 체결 데이터를 사용하므로 정밀도가 훨씬 높습니다.

사용법:
    python3 tools/kis_cvd_obv_scanner.py scan 005930 000660 009150
    python3 tools/kis_cvd_obv_scanner.py scan-all
    python3 tools/kis_cvd_obv_scanner.py detail 009150
    python3 tools/kis_cvd_obv_scanner.py rank --top 20

환경변수 (필수):
    export KIS_APP_KEY=발급받은앱키
    export KIS_APP_SECRET=발급받은시크릿
    export KIS_ACCOUNT_NO=계좌번호-상품코드
"""

import argparse
import io
import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlencode

# Windows 한글 깨짐 방지
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

# ─────────────────────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────────────────────

_KIS_BASE        = "https://openapi.koreainvestment.com:9443"
_TOKEN_CACHE     = os.path.join(os.path.dirname(__file__), "..", ".kis_token.json")
_WATCHLIST_PATH  = os.path.join(os.path.dirname(__file__), "..", "data", "watchlist.json")
_TIMEOUT         = 20
_TODAY           = datetime.now().strftime("%Y%m%d")

# CVD/OBV 신호 임계값
THRESHOLDS = {
    "cvd_surge_ratio":   2.0,   # CVD가 20일 평균의 N배 이상 → 급등 신호
    "obv_surge_ratio":   1.5,   # OBV 기울기가 평균의 N배 이상 → 매집 신호
    "divergence_days":   20,    # 다이버전스 판단 기간 (일)
    "min_volume":        10000, # 최소 거래량 필터 (소형주 노이즈 제거)
    "strong_signal_min": 4,     # 종합 신호 강도 기준 (강한 매수)
}


# ─────────────────────────────────────────────────────────────
# ETF / 파생상품 필터
# ─────────────────────────────────────────────────────────────

# ETF/ETN/ELW 코드 패턴
# - 코드가 숫자 6자리지만 1로 시작: ETF (069500=KODEX200 등)
# - Q로 시작: 코넥스
# - 0으로 시작: ETN/ELW 등 파생
# KIS 거래량 순위에서 shrs_sale_rcnt 기준 ETF 구분 가능
_ETF_CODE_PREFIXES = ("0",)   # 0으로 시작하는 코드 = ETF/ETN 계열

# 잘 알려진 ETF 코드 앞 2자리 패턴
_KNOWN_ETF_FIRST2 = {
    "06", "36", "37", "38", "43", "45", "46",  # KODEX, TIGER, KBSTAR 계열
    "27", "26", "25", "28", "29",               # ARIRANG, HANARO 계열
    "10", "11", "12", "13", "14",               # 채권 ETF 계열
    "23", "24",                                  # KOSEF 계열
}

def _is_etf(ticker: str, name: str = "") -> bool:
    """ETF/ETN/ELW 여부 판별."""
    if not ticker:
        return False
    # 알파벳 포함 코드 (Q530107, 0193T0 등) → ETN/ELW 제외
    if not ticker.isdigit():
        return True
    # 숫자 6자리가 아닌 경우
    if len(ticker) != 6:
        return True
    # 잘 알려진 ETF 앞 2자리
    if ticker[:2] in _KNOWN_ETF_FIRST2:
        return True
    # 종목명에 ETF/ETN/리버스/레버리지/인버스 포함
    etf_keywords = ["ETF", "ETN", "레버리지", "인버스", "리버스",
                    "KODEX", "TIGER", "KBSTAR", "ARIRANG", "HANARO",
                    "KOSEF", "FOCUS", "PLUS", "ACE"]
    name_upper = name.upper()
    if any(kw.upper() in name_upper for kw in etf_keywords):
        return True
    return False


# ─────────────────────────────────────────────────────────────
# 공통 유틸
# ─────────────────────────────────────────────────────────────

def _curl(url: str, headers: list = None) -> str:
    cmd = [
        "curl", "-s", "--noproxy", "*",
        "-H", "User-Agent: Mozilla/5.0",
        "-H", "Accept: application/json",
    ]
    if headers:
        for h in headers:
            cmd += ["-H", h]
    cmd.append(url)
    result = subprocess.run(cmd, capture_output=True, timeout=_TIMEOUT)
    if result.returncode != 0:
        raise ConnectionError(f"요청 실패: {url[:80]}")
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return result.stdout.decode("euc-kr", errors="replace")


def _fmt(v, suffix="") -> str:
    if v is None:
        return "-"
    try:
        v = float(str(v).replace(",", ""))
        if abs(v) >= 1e12:
            return f"{v/1e12:.2f}조{suffix}"
        if abs(v) >= 1e8:
            return f"{v/1e8:.1f}억{suffix}"
        if abs(v) >= 1e4:
            return f"{v/1e4:.0f}만{suffix}"
        return f"{v:,.0f}{suffix}"
    except (ValueError, TypeError):
        return str(v)


def _separator(char="─", width=62):
    print(f"  {char * width}")


def _name6(name: str) -> str:
    """종목명을 한글 6자(표시폭 12) 고정으로 맞춤.

    한글 1자 = 표시폭 2, 영문/숫자 1자 = 표시폭 1.
    6자 초과 시 잘라내고 뒤에 표시폭 맞춰 스페이스 패딩.
    """
    def display_width(s: str) -> int:
        w = 0
        for ch in s:
            cp = ord(ch)
            # 한글, 한자, 전각문자 등 → 폭 2
            if (0xAC00 <= cp <= 0xD7A3 or  # 한글 완성형
                0x1100 <= cp <= 0x11FF or   # 한글 자모
                0x3130 <= cp <= 0x318F or   # 한글 호환 자모
                0x4E00 <= cp <= 0x9FFF or   # CJK 한자
                0xFF00 <= cp <= 0xFFEF):    # 전각 영문/기호
                w += 2
            else:
                w += 1
        return w

    TARGET = 12  # 한글 6자 = 표시폭 12

    # 6자(표시폭 12) 초과 시 앞에서 잘라냄
    truncated = ""
    w = 0
    for ch in name:
        cw = 2 if ord(ch) >= 0x1100 else 1
        if w + cw > TARGET:
            break
        truncated += ch
        w += cw

    # 부족한 폭만큼 스페이스 패딩
    return truncated + " " * (TARGET - display_width(truncated))


# ─────────────────────────────────────────────────────────────
# KIS API 인증
# ─────────────────────────────────────────────────────────────

def _get_token() -> Optional[str]:
    """KIS 토큰 발급 (24시간 캐시)."""
    app_key    = os.environ.get("KIS_APP_KEY")
    app_secret = os.environ.get("KIS_APP_SECRET")
    if not app_key or not app_secret:
        return None

    # 캐시 확인
    if os.path.exists(_TOKEN_CACHE):
        try:
            with open(_TOKEN_CACHE) as f:
                cache = json.load(f)
            if cache.get("expires_at", 0) > time.time() + 60:
                return cache["access_token"]
        except Exception:
            pass

    # 신규 발급
    url     = f"{_KIS_BASE}/oauth2/tokenP"
    payload = json.dumps({
        "grant_type": "client_credentials",
        "appkey":     app_key,
        "appsecret":  app_secret,
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
            print(f"  ❌ 토큰 발급 실패: {data.get('msg1','')}")
            return None
        cache = {
            "access_token": token,
            "expires_at":   time.time() + int(data.get("expires_in", 86400))
        }
        with open(_TOKEN_CACHE, "w") as f:
            json.dump(cache, f)
        return token
    except Exception as e:
        print(f"  ❌ 토큰 파싱 오류: {e}")
        return None


def _kis(path: str, params: dict, tr_id: str) -> Optional[dict]:
    """KIS API 호출."""
    app_key    = os.environ.get("KIS_APP_KEY")
    app_secret = os.environ.get("KIS_APP_SECRET")
    token      = _get_token()
    if not token or not app_key:
        return None
    url = f"{_KIS_BASE}{path}?{urlencode(params)}"
    headers = [
        f"authorization: Bearer {token}",
        f"appkey: {app_key}",
        f"appsecret: {app_secret}",
        f"tr_id: {tr_id}",
        "Content-Type: application/json; charset=utf-8",
    ]
    try:
        raw  = _curl(url, headers)
        data = json.loads(raw)
        if data.get("rt_cd") != "0":
            return None
        return data
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# KIS API 데이터 수집
# ─────────────────────────────────────────────────────────────

def kis_minute_chart(ticker: str, period: str = "1") -> list:
    """
    KIS: 분봉 차트 데이터 조회 (TR: FHKST03010200)
    period: "1"=1분봉, "3"=3분봉, "5"=5분봉, "10"=10분봉, "30"=30분봉, "60"=60분봉

    반환: [{"time", "open", "high", "low", "close", "volume"}, ...]
    """
    data = _kis(
        "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
        {
            "FID_ETC_CLS_CODE":   "",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD":     ticker,
            "FID_INPUT_HOUR_1":   datetime.now().strftime("%H%M%S"),
            "FID_PW_DATA_INCU_YN": "Y",   # 과거 데이터 포함
        },
        tr_id="FHKST03010200"
    )
    if not data:
        return []

    rows = []
    for item in data.get("output2", []):
        try:
            rows.append({
                "time":   item.get("stck_bsop_date", "") + item.get("stck_cntg_hour", ""),
                "open":   float(item.get("stck_oprc", 0)),
                "high":   float(item.get("stck_hgpr", 0)),
                "low":    float(item.get("stck_lwpr", 0)),
                "close":  float(item.get("stck_prpr", 0)),
                "volume": int(item.get("cntg_vol", 0)),
            })
        except (ValueError, TypeError):
            continue
    return list(reversed(rows))  # 시간 오름차순


def kis_daily_chart(ticker: str, days: int = 60) -> list:
    """
    KIS: 일봉 차트 데이터 조회 (TR: FHKST03010100)
    OBV 계산용 — 최근 N일 일봉 데이터
    """
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    data = _kis(
        "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
        {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD":         ticker,
            "FID_INPUT_DATE_1":       start_date,
            "FID_INPUT_DATE_2":       _TODAY,
            "FID_PERIOD_DIV_CODE":    "D",   # D=일봉
            "FID_ORG_ADJ_PRC":        "0",   # 수정주가 적용
        },
        tr_id="FHKST03010100"
    )
    if not data:
        return []

    rows = []
    for item in data.get("output2", []):
        try:
            rows.append({
                "date":   item.get("stck_bsop_date", ""),
                "open":   float(item.get("stck_oprc", 0)),
                "high":   float(item.get("stck_hgpr", 0)),
                "low":    float(item.get("stck_lwpr", 0)),
                "close":  float(item.get("stck_prpr", 0)),
                "volume": int(item.get("acml_vol", 0)),
            })
        except (ValueError, TypeError):
            continue
    return list(reversed(rows))  # 날짜 오름차순


def kis_current_price(ticker: str) -> dict:
    """KIS: 현재가 및 기본 정보."""
    data = _kis(
        "/uapi/domestic-stock/v1/quotations/inquire-price",
        {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
        tr_id="FHKST01010100"
    )
    if not data:
        return {}
    return data.get("output", {})


def kis_investor_trend(ticker: str) -> dict:
    """KIS: 외국인·기관 순매수 (당일)."""
    data = _kis(
        "/uapi/domestic-stock/v1/quotations/inquire-investor",
        {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD":         ticker,
            "FID_INPUT_DATE_1":       _TODAY,
            "FID_INPUT_DATE_2":       _TODAY,
            "FID_PERIOD_DIV_CODE":    "D",
        },
        tr_id="FHKST01010900"
    )
    if not data:
        return {}
    items = data.get("output2", [])
    return items[0] if items else {}


# ─────────────────────────────────────────────────────────────
# CVD 계산 (분봉 기반 — 정밀)
# ─────────────────────────────────────────────────────────────

def calc_cvd_from_minutes(minute_bars: list) -> dict:
    """
    분봉 데이터 기반 정밀 CVD 계산.

    캔들 몸통 비율로 매수/매도 체결량 추정:
      양봉: 매수 체결 = volume × (종가-시가) / (고가-저가)
      음봉: 매도 체결 = volume × (시가-종가) / (고가-저가)
      도지: 50% 분할

    분봉을 쓰면 일봉 근사보다 훨씬 정교하게
    당일 장중 매수/매도 강도를 파악할 수 있습니다.
    """
    if len(minute_bars) < 5:
        return {
            "cvd": None, "cvd_series": [], "buy_vol": 0, "sell_vol": 0,
            "signal": "데이터 부족 (분봉 5개 미만)",
            "divergence": None, "momentum": None,
        }

    cvd_series = [0]
    total_buy  = 0
    total_sell = 0

    for bar in minute_bars:
        o, h, l, c, v = bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]
        if v == 0:
            cvd_series.append(cvd_series[-1])
            continue

        hl_range = h - l
        oc_range = abs(c - o)

        if hl_range > 0:
            if c >= o:  # 양봉 또는 도지
                ratio   = oc_range / hl_range
                buy_v   = v * (0.5 + ratio * 0.5)
                sell_v  = v - buy_v
            else:       # 음봉
                ratio   = oc_range / hl_range
                sell_v  = v * (0.5 + ratio * 0.5)
                buy_v   = v - sell_v
        else:
            buy_v = sell_v = v * 0.5

        total_buy  += buy_v
        total_sell += sell_v
        delta = buy_v - sell_v
        cvd_series.append(cvd_series[-1] + delta)

    current_cvd = cvd_series[-1]
    total_vol   = total_buy + total_sell

    # CVD 이동평균 (20분봉)
    n = min(20, len(cvd_series))
    cvd_ma = sum(cvd_series[-n:]) / n if n > 0 else 0

    # CVD 모멘텀 (최근 5분봉 기울기)
    if len(cvd_series) >= 6:
        momentum = cvd_series[-1] - cvd_series[-6]
        momentum_label = "🟢 매수 강화" if momentum > 0 else "🔴 매도 강화"
    else:
        momentum       = 0
        momentum_label = "불명"

    # 매수 비율
    buy_ratio = total_buy / total_vol * 100 if total_vol > 0 else 50

    # 신호 판단
    if current_cvd > cvd_ma and momentum > 0:
        signal = "🟢 강한 매수세 (CVD 상승 + 모멘텀 양전)"
    elif current_cvd > cvd_ma:
        signal = "🟡 매수 우위 (CVD > MA)"
    elif current_cvd < cvd_ma and momentum < 0:
        signal = "🔴 강한 매도세 (CVD 하락 + 모멘텀 음전)"
    elif current_cvd < cvd_ma:
        signal = "🟡 매도 우위 (CVD < MA)"
    else:
        signal = "⬜ 중립"

    # 급등 신호: CVD가 평균의 N배 이상
    surge = abs(current_cvd) > abs(cvd_ma) * THRESHOLDS["cvd_surge_ratio"] if cvd_ma != 0 else False

    return {
        "cvd":       round(current_cvd),
        "cvd_ma":    round(cvd_ma),
        "cvd_series": cvd_series,
        "buy_vol":   round(total_buy),
        "sell_vol":  round(total_sell),
        "buy_ratio": round(buy_ratio, 1),
        "signal":    signal,
        "momentum":  momentum_label,
        "surge":     surge,
        "bars":      len(minute_bars),
        "note":      "분봉 기반 (일봉 근사보다 정밀)",
    }


# ─────────────────────────────────────────────────────────────
# OBV 계산 (일봉 기반)
# ─────────────────────────────────────────────────────────────

def calc_obv_from_daily(daily_bars: list) -> dict:
    """
    일봉 데이터 기반 OBV 계산.

    종가 상승 → 거래량 더함
    종가 하락 → 거래량 뺌
    보합       → 변화 없음
    """
    if len(daily_bars) < 5:
        return {
            "obv": None, "obv_ma20": None, "obv_series": [],
            "signal": "데이터 부족", "divergence": None, "trend": None,
        }

    closes  = [b["close"]  for b in daily_bars]
    volumes = [b["volume"] for b in daily_bars]

    obv_series = [0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i-1]:
            obv_series.append(obv_series[-1] + volumes[i])
        elif closes[i] < closes[i-1]:
            obv_series.append(obv_series[-1] - volumes[i])
        else:
            obv_series.append(obv_series[-1])

    current_obv = obv_series[-1]

    # OBV 20일 이동평균
    n      = min(20, len(obv_series))
    obv_ma = sum(obv_series[-n:]) / n if n > 0 else 0

    # OBV 5일 기울기
    if len(obv_series) >= 6:
        slope = obv_series[-1] - obv_series[-6]
        trend = "🟢 상승 (매수 매집)" if slope > 0 else "🔴 하락 (매도 이탈)"
    else:
        slope = 0
        trend = "불명"

    # 다이버전스 판단 (최근 20일)
    divergence = None
    d = THRESHOLDS["divergence_days"]
    if len(closes) >= d and len(obv_series) >= d:
        price_chg = closes[-1] - closes[-d]
        obv_chg   = obv_series[-1] - obv_series[-d]
        if price_chg > 0 and obv_chg < 0:
            divergence = {
                "type":  "약세",
                "label": "🔴 약세 다이버전스",
                "desc":  f"가격 {price_chg:+,.0f}원 상승, OBV {_fmt(obv_chg)} 하락 → 상승 추세 신뢰도 저하",
            }
        elif price_chg < 0 and obv_chg > 0:
            divergence = {
                "type":  "강세",
                "label": "🟢 강세 다이버전스",
                "desc":  f"가격 {price_chg:+,.0f}원 하락, OBV {_fmt(obv_chg)} 상승 → 하락 중 매집, 반등 기대",
            }

    # 신호
    if current_obv > obv_ma and slope > 0:
        signal = "🟢 강한 매수 (OBV > MA20, 상승 기울기)"
    elif current_obv > obv_ma:
        signal = "🟡 매수 우위 (OBV > MA20)"
    elif current_obv < obv_ma and slope < 0:
        signal = "🔴 강한 매도 (OBV < MA20, 하락 기울기)"
    else:
        signal = "🟡 매도 우위 (OBV < MA20)"

    return {
        "obv":        round(current_obv),
        "obv_ma20":   round(obv_ma),
        "obv_series": obv_series,
        "trend":      trend,
        "slope":      slope,
        "signal":     signal,
        "divergence": divergence,
    }


# ─────────────────────────────────────────────────────────────
# 종합 신호 채점
# ─────────────────────────────────────────────────────────────

def score_signals(cvd: dict, obv: dict, price_info: dict, investor: dict) -> dict:
    """CVD + OBV + 외국인·기관 종합 신호 채점."""
    score  = 0
    labels = []

    # CVD 신호 (최대 4점)
    if cvd.get("cvd") and cvd.get("cvd_ma"):
        if cvd["cvd"] > cvd["cvd_ma"]:
            score += 1
            labels.append("CVD > MA")
        if cvd.get("surge"):
            score += 1
            labels.append("CVD 급등")
    if cvd.get("momentum") and "강화" in cvd.get("momentum", ""):
        if "매수" in cvd["momentum"]:
            score += 1
            labels.append("CVD 매수 모멘텀")
    if cvd.get("buy_ratio", 50) > 55:
        score += 1
        labels.append(f"매수 비율 {cvd['buy_ratio']}%")

    # OBV 신호 (최대 4점)
    if obv.get("obv") and obv.get("obv_ma20"):
        if obv["obv"] > obv["obv_ma20"]:
            score += 1
            labels.append("OBV > MA20")
        if obv.get("slope", 0) > 0:
            score += 1
            labels.append("OBV 상승 기울기")
    div = obv.get("divergence")
    if div:
        if div["type"] == "강세":
            score += 2
            labels.append("OBV 강세 다이버전스 ★")
        elif div["type"] == "약세":
            score -= 2
            labels.append("OBV 약세 다이버전스 ⚠️")

    # 외국인·기관 순매수 (최대 2점)
    try:
        frgn = int(str(investor.get("frgn_ntby_qty", "0")).replace(",", ""))
        orgn = int(str(investor.get("orgn_ntby_qty", "0")).replace(",", ""))
        if frgn > 0:
            score += 1
            labels.append(f"외국인 순매수 {_fmt(frgn)}주")
        if orgn > 0:
            score += 1
            labels.append(f"기관 순매수 {_fmt(orgn)}주")
    except (ValueError, TypeError):
        pass

    # 등락률
    try:
        chg = float(str(price_info.get("prdy_ctrt", "0")).replace(",", ""))
        if chg > 3:
            score += 1
            labels.append(f"등락률 +{chg}%")
        elif chg < -3:
            score -= 1
            labels.append(f"등락률 {chg}%")
    except (ValueError, TypeError):
        pass

    max_score = 10
    if score >= THRESHOLDS["strong_signal_min"]:
        verdict = "🟢 강한 매수 신호"
    elif score >= 3:
        verdict = "🟡 중립 (추가 확인)"
    elif score >= 0:
        verdict = "⬜ 신호 약함"
    else:
        verdict = "🔴 매도/회피 신호"

    return {
        "score":     score,
        "max":       max_score,
        "labels":    labels,
        "verdict":   verdict,
    }


# ─────────────────────────────────────────────────────────────
# 단일 종목 전체 분석
# ─────────────────────────────────────────────────────────────

def analyze_one(ticker: str, minute_period: str = "5", verbose: bool = True) -> dict:
    """단일 종목 CVD/OBV 전체 분석."""
    if verbose:
        print(f"\n  분석 중: {ticker} ({minute_period}분봉 CVD + 일봉 OBV)")

    # 데이터 수집
    price_info  = kis_current_price(ticker)
    minute_bars = kis_minute_chart(ticker, period=minute_period)
    daily_bars  = kis_daily_chart(ticker, days=60)
    investor    = kis_investor_trend(ticker)

    # 지표 계산
    cvd = calc_cvd_from_minutes(minute_bars)
    obv = calc_obv_from_daily(daily_bars)
    sig = score_signals(cvd, obv, price_info, investor)

    result = {
        "ticker":     ticker,
        "name":       price_info.get("hts_kor_isnm", ticker),
        "price":      price_info.get("stck_prpr", "-"),
        "change_pct": price_info.get("prdy_ctrt", "-"),
        "cvd":        cvd,
        "obv":        obv,
        "signal":     sig,
        "investor":   investor,
        "minute_bars": len(minute_bars),
        "daily_bars":  len(daily_bars),
    }

    if verbose:
        _print_detail(result, minute_period)

    return result


def _print_detail(r: dict, minute_period: str):
    """단일 종목 상세 출력."""
    cvd = r["cvd"]
    obv = r["obv"]
    sig = r["signal"]
    inv = r["investor"]

    print()
    print(f"{'='*64}")
    print(f"  CVD / OBV 분석: {r['name']} ({r['ticker']})")
    print(f"{'='*64}")

    # 현재가
    print(f"\n  현재가:   {r['price']}원  ({r['change_pct']}%)")

    # CVD
    _separator()
    print(f"  [CVD — {minute_period}분봉 기반 (정밀 매수/매도 강도)]")
    _separator()
    print(f"  현재 CVD:     {_fmt(cvd.get('cvd'))}")
    print(f"  CVD MA({cvd.get('bars','?')}):  {_fmt(cvd.get('cvd_ma'))}")
    print(f"  당일 매수량:  {_fmt(cvd.get('buy_vol'))}주")
    print(f"  당일 매도량:  {_fmt(cvd.get('sell_vol'))}주")
    print(f"  매수 비율:    {cvd.get('buy_ratio', '-')}%")
    print(f"  모멘텀:       {cvd.get('momentum', '-')}")
    print(f"  판단:         {cvd.get('signal', '-')}")
    if cvd.get("surge"):
        print(f"  ⚡ CVD 급등 감지 — 평균 대비 {THRESHOLDS['cvd_surge_ratio']}배 이상")
    print(f"  {cvd.get('note', '')}")

    # OBV
    _separator()
    print(f"  [OBV — 일봉 기반 (누적 매수/매도 추세)]")
    _separator()
    print(f"  현재 OBV:     {_fmt(obv.get('obv'))}")
    print(f"  OBV MA20:     {_fmt(obv.get('obv_ma20'))}")
    print(f"  추세:         {obv.get('trend', '-')}")
    print(f"  판단:         {obv.get('signal', '-')}")
    div = obv.get("divergence")
    if div:
        print(f"\n  ⚡ {div['label']}")
        print(f"     {div['desc']}")

    # 외국인·기관
    _separator()
    print(f"  [외국인·기관 순매수 (당일)]")
    _separator()
    try:
        frgn = int(str(inv.get("frgn_ntby_qty", "0")).replace(",", ""))
        orgn = int(str(inv.get("orgn_ntby_qty", "0")).replace(",", ""))
        priv = int(str(inv.get("prvt_ntby_qty", "0")).replace(",", ""))
        print(f"  외국인:  {_fmt(frgn):>10}주  {'🟢 매수' if frgn > 0 else '🔴 매도'}")
        print(f"  기관:    {_fmt(orgn):>10}주  {'🟢 매수' if orgn > 0 else '🔴 매도'}")
        print(f"  개인:    {_fmt(priv):>10}주  {'🟢 매수' if priv > 0 else '🔴 매도'}")
    except (ValueError, TypeError):
        print(f"  데이터 없음")

    # 종합 신호
    _separator("═")
    print(f"  종합 신호 강도: {sig['score']} / {sig['max']}점")
    print(f"  판정:           {sig['verdict']}")
    if sig["labels"]:
        print(f"  신호 근거:")
        for lb in sig["labels"]:
            print(f"    • {lb}")
    _separator("═")


# ─────────────────────────────────────────────────────────────
# 스캔 명령
# ─────────────────────────────────────────────────────────────

def cmd_scan(tickers: list, minute_period: str = "5", name_cache: dict = None):
    """여러 종목 CVD/OBV 스캔 후 신호 강도 순 정렬."""
    print(f"\n{'='*64}")
    print(f"  KIS CVD / OBV 스캐너")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  종목 수: {len(tickers)}개  |  분봉: {minute_period}분")
    print(f"{'='*64}")

    # API 연결 확인
    token = _get_token()
    if not token:
        print("\n  ❌ KIS API 미연결")
        print("  export KIS_APP_KEY=앱키")
        print("  export KIS_APP_SECRET=시크릿")
        return

    results = []
    name_cache = name_cache or {}
    for ticker in tickers:
        cached_name = name_cache.get(ticker, "")
        print(f"  [{ticker}] {cached_name} 수집 중...", end="\r")
        r = analyze_one(ticker, minute_period, verbose=False)
        # 캐시된 종목명이 있으면 우선 사용 (price_info에서 못 가져온 경우 대비)
        if cached_name and r.get("name") == ticker:
            r["name"] = cached_name
        results.append(r)
        time.sleep(0.3)   # API 호출 간격 (초당 20회 제한)

    # 신호 강도 순 정렬
    results.sort(key=lambda x: x["signal"]["score"], reverse=True)

    # ── 범례 출력 ──────────────────────────────────────────────
    print()
    print("  [범례]")
    print("  CVD↑/CVD↓ : CVD > 이동평균(매수세 우위) / CVD < 이동평균(매도세 우위)")
    print("  OBV↑/OBV↓ : OBV > MA20(매수 축적)       / OBV < MA20(매도 이탈)")
    print("  외국인/기관: 순매수(+) 초록 / 순매도(-) 빨강  ·  0 = 데이터 없음")
    print()

    # ── 헤더 ────────────────────────────────────────────────────
    H1 = f"  {'종목명(6자)':<12} {'코드':<8} {'현재가':>9} {'등락':>7}  {'CVD':^7} {'OBV':^7}  {'외국인순매수':>12} {'기관순매수':>11}  {'점수':>4}  판정"
    H2 = f"  {'─'*12} {'─'*8} {'─'*9} {'─'*7}  {'─'*7} {'─'*7}  {'─'*12} {'─'*11}  {'─'*4}  {'─'*14}"
    print(H1)
    print(H2)

    for r in results:
        cvd  = r["cvd"]
        obv  = r["obv"]
        sig  = r["signal"]
        inv  = r["investor"]
        name = _name6(r["name"])

        # ── CVD 방향 표시 ────────────────────────────────────────
        cvd_val = cvd.get("cvd", 0) or 0
        cvd_ma  = cvd.get("cvd_ma", 0) or 0
        if cvd_val > cvd_ma:
            cvd_sym = "🟢CVD↑"
        else:
            cvd_sym = "🔴CVD↓"

        # ── OBV 방향 표시 ────────────────────────────────────────
        obv_val  = obv.get("obv", 0) or 0
        obv_ma20 = obv.get("obv_ma20", 0) or 0
        if obv_val > obv_ma20:
            obv_sym = "🟢OBV↑"
        else:
            obv_sym = "🔴OBV↓"

        # ── 외국인·기관 순매수 수치 표시 ────────────────────────
        try:
            frgn = int(str(inv.get("frgn_ntby_qty", "0")).replace(",", ""))
            orgn = int(str(inv.get("orgn_ntby_qty", "0")).replace(",", ""))
            frgn_icon = "🟢" if frgn > 0 else ("🔴" if frgn < 0 else "⬜")
            orgn_icon = "🟢" if orgn > 0 else ("🔴" if orgn < 0 else "⬜")
            # 수치를 +/- 기호와 함께 표시
            frgn_s = f"{frgn_icon}{'+' if frgn>0 else ''}{_fmt(frgn)}주"
            orgn_s = f"{orgn_icon}{'+' if orgn>0 else ''}{_fmt(orgn)}주"
        except (ValueError, TypeError):
            frgn_s = "⬜ -"
            orgn_s = "⬜ -"

        # ── 등락률 색상 ──────────────────────────────────────────
        try:
            chg = float(str(r["change_pct"]).replace(",", ""))
            chg_s = f"{chg:+.2f}%"
        except (ValueError, TypeError):
            chg_s = f"{r['change_pct']}%"

        print(f"  {name} {r['ticker']:<8} {r['price']:>8}원 {chg_s:>7}"
              f"  {cvd_sym}  {obv_sym}"
              f"  {frgn_s:>12} {orgn_s:>11}"
              f"  {sig['score']:>3}점  {sig['verdict']}")

        # ── 다이버전스 강조 표시 ─────────────────────────────────
        div = obv.get("divergence")
        if div:
            print(f"    ⚡ {div['label']}: {div['desc'][:65]}")

    # 강한 신호 종목 별도 강조
    strong = [r for r in results if r["signal"]["score"] >= THRESHOLDS["strong_signal_min"]]
    if strong:
        print(f"\n  🎯 강한 매수 신호 종목 ({THRESHOLDS['strong_signal_min']}점 이상):")
        for r in strong:
            print(f"     {r['name']} ({r['ticker']}) — {r['signal']['score']}점")
            print(f"     신호: {', '.join(r['signal']['labels'][:4])}")


def cmd_scan_all(minute_period: str = "5"):
    """watchlist.json 전체 종목 스캔."""
    if not os.path.exists(_WATCHLIST_PATH):
        print(f"\n  ❌ watchlist.json 없음: {_WATCHLIST_PATH}")
        print("  python3 tools/stock_screener.py 실행 시 자동 생성됩니다.")
        return

    with open(_WATCHLIST_PATH) as f:
        wl = json.load(f)

    # 한국주만 추출
    tickers = [
        sym for group in wl.values() for sym in group
        if sym.isdigit() and len(sym) == 6
    ]

    if not tickers:
        print("  ❌ watchlist에 한국주 종목이 없습니다.")
        return

    print(f"  watchlist 전체 스캔: {len(tickers)}개 종목")
    cmd_scan(tickers, minute_period)


def cmd_rank(top: int = 20, minute_period: str = "5"):
    """
    KIS 거래량 상위 종목 자동 수집 후 CVD/OBV 스캔.
    별도로 watchlist 없이도 당일 시장 전체에서 신호 포착.
    """
    print(f"\n  거래량 상위 {top}개 종목 자동 스캔 중...")

    token = _get_token()
    if not token:
        print("  ❌ KIS API 미연결")
        return

    # 거래량 순위에서 상위 종목 수집
    data = _kis(
        "/uapi/domestic-stock/v1/quotations/volume-rank",
        {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE":  "20171",
            "FID_INPUT_ISCD":         "0000",
            "FID_DIV_CLS_CODE":       "0",
            "FID_BLNG_CLS_CODE":      "0",
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
        print("  ❌ 거래량 순위 조회 실패")
        return

    all_items = data.get("output", [])

    # ETF/ETN/ELW 제외 후 상위 top개 추출
    # KIS 거래량 순위 응답에 hts_kor_isnm(종목명) 포함
    filtered = []
    etf_count = 0
    for item in all_items:
        ticker = item.get("mksc_shrn_iscd", "")
        name   = item.get("hts_kor_isnm", "")
        if not ticker:
            continue
        if _is_etf(ticker, name):
            etf_count += 1
            continue
        filtered.append({"ticker": ticker, "name": name})
        if len(filtered) >= top:
            break

    if not filtered:
        print("  ❌ 종목 코드 추출 실패 (ETF 제외 후 결과 없음)")
        return

    print(f"  ETF/ETN 제외: {etf_count}개  |  일반 종목: {len(filtered)}개")

    # 종목명 캐시로 전달 (analyze_one에서 재사용)
    tickers   = [f["ticker"] for f in filtered]
    name_cache = {f["ticker"]: f["name"] for f in filtered}

    cmd_scan(tickers, minute_period, name_cache=name_cache)


# ─────────────────────────────────────────────────────────────
# CLI 진입점
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="KIS API 기반 CVD / OBV 전용 스캐너",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""사용 예시:
  python3 tools/kis_cvd_obv_scanner.py scan 005930 000660 009150
  python3 tools/kis_cvd_obv_scanner.py scan --period 3 005930
  python3 tools/kis_cvd_obv_scanner.py detail 009150
  python3 tools/kis_cvd_obv_scanner.py detail --period 1 009150
  python3 tools/kis_cvd_obv_scanner.py scan-all
  python3 tools/kis_cvd_obv_scanner.py rank --top 30

환경변수:
  export KIS_APP_KEY=앱키
  export KIS_APP_SECRET=시크릿
  export KIS_ACCOUNT_NO=계좌번호-상품코드"""
    )

    sub = parser.add_subparsers(dest="command")

    # scan: 지정 종목 스캔
    p_scan = sub.add_parser("scan", help="지정 종목 CVD/OBV 스캔 (요약)")
    p_scan.add_argument("tickers", nargs="+",        help="종목 코드 1개 이상")
    p_scan.add_argument("--period", default="5",     help="분봉 단위 1/3/5/10/30/60 (기본: 5)")

    # detail: 단일 종목 상세 분석
    p_det = sub.add_parser("detail", help="단일 종목 CVD/OBV 상세 분석")
    p_det.add_argument("ticker",                     help="종목 코드")
    p_det.add_argument("--period", default="5",      help="분봉 단위 (기본: 5)")

    # scan-all: watchlist 전체 스캔
    p_all = sub.add_parser("scan-all", help="watchlist 전체 종목 스캔")
    p_all.add_argument("--period", default="5",      help="분봉 단위 (기본: 5)")

    # rank: 거래량 상위 자동 스캔
    p_rank = sub.add_parser("rank", help="거래량 상위 종목 자동 수집 후 스캔")
    p_rank.add_argument("--top",    type=int, default=20, help="상위 N개 (기본: 20)")
    p_rank.add_argument("--period", default="5",          help="분봉 단위 (기본: 5)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    if args.command == "scan":
        cmd_scan(args.tickers, args.period)
    elif args.command == "detail":
        token = _get_token()
        if not token:
            print("❌ KIS API 미연결")
            sys.exit(1)
        analyze_one(args.ticker, args.period, verbose=True)
    elif args.command == "scan-all":
        cmd_scan_all(args.period)
    elif args.command == "rank":
        cmd_rank(args.top, args.period)


if __name__ == "__main__":
    main()

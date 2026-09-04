#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
키움증권 REST API 기반 CVD / OBV 스캐너
코스피200 / 코스닥150 종목의 최근 5영업일 분봉을 한 번에 수집해서
CVD·OBV를 계산하고 스코어 상위 종목을 출력합니다.

일별 누적 불필요 — 오늘 한 번 실행으로 5일치 분석 완료.
키움 분봉 API(ka10080)는 최대 160일치 과거 분봉을 한 번에 제공합니다.

사전 준비:
  1. 키움 REST API 신청: https://openapi.kiwoom.com
  2. App Key / App Secret 발급
  3. 환경변수 등록:
     Windows: [System.Environment]::SetEnvironmentVariable("KIWOOM_APP_KEY","키","User")
              [System.Environment]::SetEnvironmentVariable("KIWOOM_APP_SECRET","시크릿","User")
     macOS:   export KIWOOM_APP_KEY=키 && export KIWOOM_APP_SECRET=시크릿

사용법:
  # 종목 목록 먼저 생성 (최초 1회 및 분기별 변경 시)
  python3 tools/update_index.py

  # 스캔 실행
  python3 tools/kiwoom_cvd_obv_scanner.py scan --index kospi200 --top 10
  python3 tools/kiwoom_cvd_obv_scanner.py scan --index kosdaq150 --top 10
  python3 tools/kiwoom_cvd_obv_scanner.py scan --index both --top 10

  # CVD 산출 기간 변경 (기본 5일, 10일·20일도 가능)
  python3 tools/kiwoom_cvd_obv_scanner.py scan --index kospi200 --days 10
  python3 tools/kiwoom_cvd_obv_scanner.py scan --index kospi200 --days 20
  python3 tools/kiwoom_cvd_obv_scanner.py detail --code 005930 --days 10

  # 단일 종목 상세 분석
  python3 tools/kiwoom_cvd_obv_scanner.py detail --code 009150
"""

import argparse
import io
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from typing import Optional

# Windows 한글 깨짐 방지
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

# ─────────────────────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────────────────────

_BASE          = os.path.join(os.path.dirname(__file__), "..")
_DATA_DIR      = os.path.join(_BASE, "data")
_KIWOOM_TOKEN  = os.path.join(_BASE, ".kiwoom_token.json")
_TIMEOUT       = 30
_RATE_DELAY    = 0.22   # 초당 5회 제한 준수 (0.2초 + 여유)

_KIWOOM_BASE      = "https://api.kiwoom.com"       # 실전투자
_KIWOOM_BASE_MOCK = "https://mockapi.kiwoom.com"   # 모의투자

# 5영업일 = 약 7 캘린더 일 (주말 포함)
_BDAY_5        = 7

# update_index.py가 생성하는 단일 파일에서 종목 목록 로드
_INDEX_FILE = os.path.join(_DATA_DIR, "index_stocks.json")

os.makedirs(_DATA_DIR, exist_ok=True)


# ─────────────────────────────────────────────────────────────
# 코스피200 / 코스닥150 기본 종목 목록
# ─────────────────────────────────────────────────────────────
# 2025년 기준 대표 종목 — update-index 명령으로 최신화 가능


def load_index(name: str) -> list:
    """
    update_index.py가 생성한 index_stocks.json에서 종목 목록 로드.
    반환: [{"code": "005930", "name": "삼성전자"}, ...]
    """
    if not os.path.exists(_INDEX_FILE):
        print(f"  ❌ 종목 목록 파일 없음: {_INDEX_FILE}")
        print(f"  먼저 실행: python3 tools/update_index.py")
        return []

    with open(_INDEX_FILE, encoding="utf-8") as f:
        data = json.load(f)

    idx_data = data.get(name, {})
    stocks   = idx_data.get("stocks", [])
    updated  = idx_data.get("updated", "?")

    if not stocks:
        print(f"  ❌ {name} 종목 데이터 없음")
        print(f"  실행: python3 tools/update_index.py --index {name}")
        return []

    print(f"  📋 {name}: {len(stocks)}개 종목 로드 (갱신: {updated})")
    return stocks


# ─────────────────────────────────────────────────────────────
# 키움 REST API 인증
# ─────────────────────────────────────────────────────────────

def _get_token() -> Optional[str]:
    """키움 REST API 토큰 발급 (24시간 캐시)."""
    app_key    = os.environ.get("KIWOOM_APP_KEY")
    app_secret = os.environ.get("KIWOOM_APP_SECRET")
    if not app_key or not app_secret:
        return None

    # 캐시 확인
    if os.path.exists(_KIWOOM_TOKEN):
        try:
            with open(_KIWOOM_TOKEN) as f:
                cache = json.load(f)
            if cache.get("expires_at", 0) > time.time() + 60:
                return cache["token"]
        except Exception:
            pass

    # 신규 발급 — 두 가지 필드명 시도 (키움 API 버전별 차이)
    for secret_field in ["secretkey", "appsecret", "appSecret"]:
        body = json.dumps({
            "grant_type": "client_credentials",
            "appkey":     app_key,
            secret_field: app_secret,
        })
        result = subprocess.run(
            ["curl", "-s", "-X", "POST",
             f"{_KIWOOM_BASE}/oauth2/token",
             "-H", "Content-Type: application/json;charset=UTF-8",
             "-H", "Accept: application/json",
             "-d", body],
            capture_output=True, timeout=_TIMEOUT
        )
        raw = result.stdout.strip()
        print(f"  [디버그] 토큰 응답 ({secret_field}): {raw[:200]}")

        if not raw:
            continue
        # HTML 응답 감지
        if raw.startswith(b"<") if isinstance(raw, bytes) else raw.startswith("<"):
            print(f"  ⚠️  HTML 응답 — URL 또는 인증 방식 확인 필요")
            continue
        try:
            data  = json.loads(raw)
            token = (data.get("token") or data.get("access_token") or
                     data.get("tokenValue") or data.get("accessToken"))
            if token:
                expires = int(data.get("token_expired",
                              data.get("expires_in", 86400)))
                with open(_KIWOOM_TOKEN, "w") as f:
                    json.dump({"token": token,
                               "expires_at": time.time() + expires}, f)
                print(f"  ✅ 키움 토큰 발급 완료 (필드: {secret_field})")
                return token
            else:
                print(f"  ❌ 토큰 필드 없음: {data}")
        except Exception as e:
            print(f"  ❌ JSON 파싱 오류: {e}  |  응답: {raw[:100]}")

    return None


def _kiwoom(path: str, body: dict, api_id: str, token: str) -> Optional[dict]:
    """
    키움 REST API POST 호출.
    키움 REST API는 모든 조회를 POST + Body + api-id 헤더로 처리합니다.
    """
    app_key = os.environ.get("KIWOOM_APP_KEY", "")
    url     = f"{_KIWOOM_BASE}{path}"
    body_str = json.dumps(body)
    result  = subprocess.run(
        ["curl", "-s", "--noproxy", "*",
         "-X", "POST", url,
         "-H", f"authorization: Bearer {token}",
         "-H", f"api-id: {api_id}",
         "-H", "Content-Type: application/json;charset=UTF-8",
         "-d", body_str],
        capture_output=True, timeout=_TIMEOUT
    )
    raw = result.stdout.strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
        rc   = data.get("return_code", 0)
        if rc not in (0, "0", None, "00000"):
            return None
        return data
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# 데이터 수집
# ─────────────────────────────────────────────────────────────

def fetch_stock_name(code: str, token: str) -> str:
    """
    키움 ka10001 (주식기본정보) 으로 종목명 조회.
    실패 시 빈 문자열 반환.
    """
    data = _kiwoom("/api/dostk/stkinfo",
                   {"stk_cd": code}, "ka10001", token)
    if not data:
        return ""
    # 응답 구조: stk_nm이 최상위 필드로 바로 존재
    # {"stk_cd":"005930", "stk_nm":"삼성전자", "cur_prc":"+257000", ...}
    name = str(data.get("stk_nm", "")).strip()
    if name:
        return name
    # 혹시 output 안에 있을 경우 대비
    info = data.get("output") or {}
    if isinstance(info, list) and info:
        info = info[0]
    return str(info.get("stk_nm", "")).strip()


def fetch_minute_bars(code: str, token: str,
                      period: int = 5, bdays: int = 5) -> list:
    """
    키움 ka10080 (주식분봉조회)로 최근 N영업일 분봉 한 번에 수집.

    키움 분봉 API는 최대 160일치 과거 데이터를 페이지네이션으로 제공.
    bdays=5 → 약 7 캘린더 일 → 1회~수 회 요청으로 충분히 커버.
    1회 응답 최대 900건이므로 5분봉 5일 ≈ 780봉 → 보통 1~2회 요청.
    """
    all_bars = []
    next_key = ""
    # 5영업일 = 7 캘린더일 기준 cutoff
    cutoff   = (datetime.now() - timedelta(days=bdays + 3)).strftime("%Y%m%d")

    for page in range(10):  # 안전 상한 (무한루프 방지)
        body = {
            "stk_cd":       code,
            "tic_scope":    str(period),
            "upd_stkpc_tp": "1",   # 수정주가 적용
        }
        if next_key:
            body["next_key"] = next_key

        # ka10080: 주식분봉조회
        data = _kiwoom("/api/dostk/chart", body, "ka10080", token)

        if not data:
            break

        # 응답 구조: {"stk_cd":"005930", "stk_min_pole_chart_qry":[...]}
        rows = (data.get("stk_min_pole_chart_qry") or
                data.get("output") or
                data.get("stk_chart") or [])
        if not rows:
            break

        reached_cutoff = False
        for item in rows:
            # 필드명: cntr_tm(체결시간), open_pric, high_pric, low_pric
            #         cur_prc(종가), trde_qty(체결량)
            dt    = str(item.get("cntr_tm", item.get("dt", "")))
            open_ = _f(item.get("open_pric", 0))
            high  = _f(item.get("high_pric",  0))
            low   = _f(item.get("low_pric",   0))
            close = _f(item.get("cur_prc",    0) or
                       item.get("cur_pric",   0) or
                       item.get("close_pric", 0))
            vol   = _i(item.get("trde_qty",   0))

            if not (close and vol):
                continue

            bar_date = dt[:8] if len(dt) >= 8 else ""
            if bar_date and bar_date < cutoff:
                reached_cutoff = True
                break

            all_bars.append({
                "dt": dt, "open": open_, "high": high,
                "low": low, "close": close, "volume": vol,
            })

        next_key = data.get("next_key", "")
        if reached_cutoff or not next_key:
            break

        time.sleep(_RATE_DELAY)

    # 시간 오름차순 정렬
    return list(reversed(all_bars))


def fetch_daily_bars(code: str, token: str, days: int = 60) -> list:
    """키움 일봉 조회 (OBV 계산용)."""
    end   = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")

    body = {
        "stk_cd":       code,
        "base_dt":      end,
        "upd_stkpc_tp": "1",   # 수정주가
    }
    # ka10081: 주식일봉조회
    data = _kiwoom("/api/dostk/chart", body, "ka10081", token)
    if not data:
        return []

    # 일봉 응답 구조: {"stk_dt_pole_chart_qry": [...]}
    # 필드: dt(날짜), cur_prc(종가), trde_qty(거래량),
    #       open_pric, high_pric, low_pric
    rows = (data.get("stk_dt_pole_chart_qry") or
            data.get("output") or
            data.get("stk_chart") or [])
    result = []
    for item in rows:
        dt    = str(item.get("dt", item.get("cntr_tm", "")))
        close = _f(item.get("cur_prc",    0) or
                   item.get("cur_pric",   0) or
                   item.get("close_pric", 0))
        vol   = _i(item.get("trde_qty",   0))
        if not (close and vol):
            continue
        if len(dt) >= 8 and dt[:8] < start:
            break
        result.append({
            "dt": dt, "close": close, "volume": vol,
            "open": _f(item.get("open_pric", 0)),
            "high": _f(item.get("high_pric", 0)),
            "low":  _f(item.get("low_pric",  0)),
        })
    return list(reversed(result))


# ─────────────────────────────────────────────────────────────
# 수치 변환
# ─────────────────────────────────────────────────────────────

def _f(v) -> float:
    try:
        return float(str(v).replace(",", "").replace("+", ""))
    except (ValueError, TypeError):
        return 0.0


def _i(v) -> int:
    try:
        return int(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return 0


def _fmt(v) -> str:
    if v is None:
        return "-"
    try:
        v = float(str(v).replace(",", ""))
        if abs(v) >= 1e12: return f"{v/1e12:.2f}조"
        if abs(v) >= 1e8:  return f"{v/1e8:.1f}억"
        if abs(v) >= 1e4:  return f"{v/1e4:.0f}만"
        return f"{v:,.0f}"
    except (ValueError, TypeError):
        return str(v)


def _name6(name: str) -> str:
    """종목명 한글 6자(표시폭 12) 고정."""
    def dw(s):
        w = 0
        for ch in s:
            cp = ord(ch)
            if (0xAC00 <= cp <= 0xD7A3 or 0x1100 <= cp <= 0x11FF or
                0x3130 <= cp <= 0x318F or 0x4E00 <= cp <= 0x9FFF or
                0xFF00 <= cp <= 0xFFEF):
                w += 2
            else:
                w += 1
        return w
    TARGET = 12
    t, w = "", 0
    for ch in name:
        cw = 2 if ord(ch) >= 0x1100 else 1
        if w + cw > TARGET:
            break
        t += ch
        w += cw
    return t + " " * (TARGET - dw(t))


# ─────────────────────────────────────────────────────────────
# CVD 계산 (분봉 기반 — N영업일 전체)
# ─────────────────────────────────────────────────────────────

def calc_cvd(bars: list) -> dict:
    """
    최근 N영업일 분봉 전체로 CVD 계산.

    양봉: 매수 = volume × (0.5 + |종가-시가|/|고가-저가| × 0.5)
    음봉: 매도 = 반대
    도지/HL=0: 50/50 분할
    """
    if len(bars) < 5:
        return {"cvd": None, "cvd_ma": None, "buy_ratio": 50.0,
                "signal": "데이터 부족", "momentum": 0, "score": 0}

    cvd_s = [0.0]
    total_buy = total_sell = 0.0

    for bar in bars:
        o, h, l, c, v = (bar["open"], bar["high"], bar["low"],
                          bar["close"], bar["volume"])
        if v == 0:
            cvd_s.append(cvd_s[-1])
            continue
        hl = h - l
        oc = abs(c - o)
        if hl > 0:
            ratio = oc / hl
            if c >= o:
                buy_v  = v * (0.5 + ratio * 0.5)
                sell_v = v - buy_v
            else:
                sell_v = v * (0.5 + ratio * 0.5)
                buy_v  = v - sell_v
        else:
            buy_v = sell_v = v * 0.5

        total_buy  += buy_v
        total_sell += sell_v
        cvd_s.append(cvd_s[-1] + buy_v - sell_v)

    cur  = cvd_s[-1]
    n    = min(20, len(cvd_s))
    ma   = sum(cvd_s[-n:]) / n if n else 0
    tot  = total_buy + total_sell
    bratio = total_buy / tot * 100 if tot > 0 else 50.0

    # 최근 5봉 기울기 (모멘텀)
    mom = cvd_s[-1] - cvd_s[-6] if len(cvd_s) >= 6 else 0

    # 일별 CVD 구간 분리 (영업일별 평균 CVD 변화 확인)
    # bars는 시간 오름차순 — 날짜 변경점 탐지
    day_deltas = []
    prev_date  = ""
    day_start  = 0.0
    for i, bar in enumerate(bars):
        d = bar["dt"][:8]
        if d != prev_date:
            if prev_date:
                day_deltas.append(cvd_s[i] - day_start)
            day_start = cvd_s[i]
            prev_date = d
    if prev_date:
        day_deltas.append(cvd_s[-1] - day_start)

    # 5일 중 매수 우위 일수
    bull_days = sum(1 for d in day_deltas if d > 0)

    # 스코어
    score = 0
    if cur > ma:                            score += 2
    if mom > 0:                             score += 1
    if bratio > 55:                         score += 1
    if bull_days >= 3:                      score += 1  # 5일 중 3일 이상 매수 우위
    if abs(cur) > abs(ma) * 2 and cur > 0: score += 1  # CVD 급등

    sig = ("🟢강한매수" if cur > ma and mom > 0 and bratio > 55 else
           "🟢매수우위" if cur > ma else
           "🔴강한매도" if cur < ma and mom < 0 else
           "🔴매도우위" if cur < ma else "⬜중립")

    return {
        "cvd":       round(cur),
        "cvd_ma":    round(ma),
        "buy_vol":   round(total_buy),
        "sell_vol":  round(total_sell),
        "buy_ratio": round(bratio, 1),
        "momentum":  round(mom),
        "bull_days": bull_days,
        "total_days": len(day_deltas),
        "signal":    sig,
        "score":     score,
    }


# ─────────────────────────────────────────────────────────────
# OBV 계산 (일봉 기반)
# ─────────────────────────────────────────────────────────────

def calc_obv(daily_bars: list) -> dict:
    """일봉 기반 OBV 계산 + 5영업일 기울기."""
    if len(daily_bars) < 5:
        return {"obv": None, "obv_ma20": None,
                "signal": "데이터 부족", "divergence": None, "score": 0}

    closes  = [b["close"]  for b in daily_bars]
    volumes = [b["volume"] for b in daily_bars]

    obv_s = [0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i-1]:
            obv_s.append(obv_s[-1] + volumes[i])
        elif closes[i] < closes[i-1]:
            obv_s.append(obv_s[-1] - volumes[i])
        else:
            obv_s.append(obv_s[-1])

    cur  = obv_s[-1]
    n    = min(20, len(obv_s))
    ma20 = sum(obv_s[-n:]) / n if n else 0

    # 5영업일 기울기 (최근 6봉 차이)
    slope5 = obv_s[-1] - obv_s[-6] if len(obv_s) >= 6 else 0

    # 5영업일 다이버전스
    div = None
    if len(closes) >= 6 and len(obv_s) >= 6:
        pc = closes[-1] - closes[-6]   # 5일 가격 변화
        oc = obv_s[-1]  - obv_s[-6]   # 5일 OBV 변화
        if pc < 0 and oc > 0:
            div = "강세"   # 가격 하락 + OBV 상승 → 저점 매집
        elif pc > 0 and oc < 0:
            div = "약세"   # 가격 상승 + OBV 하락 → 상승 신뢰도 저하

    score = 0
    if cur > ma20:    score += 2
    if slope5 > 0:    score += 1
    if div == "강세": score += 2
    if div == "약세": score -= 2

    sig = ("🟢매수축적" if cur > ma20 and slope5 > 0 else
           "🔴매도이탈" if cur < ma20 and slope5 < 0 else "⬜중립")

    return {
        "obv":        round(cur),
        "obv_ma20":   round(ma20),
        "slope5":     round(slope5),
        "signal":     sig,
        "divergence": div,
        "score":      score,
    }


# ─────────────────────────────────────────────────────────────
# 단일 종목 분석
# ─────────────────────────────────────────────────────────────

def analyze(code: str, token: str,
            period: int = 5, bdays: int = 5,
            name: str = "") -> Optional[dict]:
    """단일 종목 CVD + OBV 분석."""
    # 종목명 조회 (캐시된 이름 없으면 API 호출)
    stock_name = name or fetch_stock_name(code, token) or code

    # 분봉: 최근 5영업일치 한 번에 수집
    min_bars   = fetch_minute_bars(code, token, period=period, bdays=bdays)
    # 일봉: OBV용 60일치
    daily_bars = fetch_daily_bars(code, token, days=60)

    if not min_bars and not daily_bars:
        return None

    cvd = calc_cvd(min_bars)
    obv = calc_obv(daily_bars)

    return {
        "code":     code,
        "name":     stock_name,
        "cvd":      cvd,
        "obv":      obv,
        "total":    cvd["score"] + obv["score"],
        "min_bars": len(min_bars),
        "days":     cvd.get("total_days", 0),
    }


# ─────────────────────────────────────────────────────────────
# 스캔 출력
# ─────────────────────────────────────────────────────────────

def _print_results(results: list, top: int, label: str, period: int, bdays: int):
    """스캔 결과 출력."""
    results.sort(key=lambda x: x["total"], reverse=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    print(f"\n{'='*70}")
    print(f"  {label} CVD/OBV 스캐너  |  {now}")
    print(f"  분봉: {period}분  |  기간: 최근 {bdays}영업일 한 번에 수집")
    print(f"  분석 종목: {len(results)}개  |  상위 {top}개 출력")
    print(f"{'='*70}")

    print(f"\n  [범례]")
    print(f"  CVD: {bdays}영업일 분봉 누적 매수/매도 강도")
    print(f"       🟢강한매수/🟢매수우위 = CVD>MA + 모멘텀 양전")
    print(f"       bull_days = {bdays}일 중 매수 우위 일수 (5일·10일·20일 선택 가능)")
    print(f"  OBV: 일봉 누적 매수/매도 추세")
    print(f"       다이버전스 강세 = 가격 하락 + OBV 상승 (저점 매집)")
    print()

    H = (f"  {'순':>3}  {'종목명':12}  {'코드':6}  "
         f"{'CVD신호':^8}  {'OBV신호':^8}  "
         f"{'매수%':>5}  {'bull':>4}  {'총점':>4}")
    print(H)
    print(f"  {'─'*3}  {'─'*12}  {'─'*6}  {'─'*8}  {'─'*8}  "
          f"{'─'*5}  {'─'*4}  {'─'*4}")

    for rank, r in enumerate(results[:top], 1):
        cvd  = r["cvd"]
        obv  = r["obv"]
        name = _name6(r["name"])
        div  = obv.get("divergence")

        # CVD 방향
        if cvd.get("cvd") and cvd.get("cvd_ma"):
            cvd_sym = "🟢CVD↑" if cvd["cvd"] > cvd["cvd_ma"] else "🔴CVD↓"
        else:
            cvd_sym = "⬜ -  "

        # OBV 방향
        if obv.get("obv") and obv.get("obv_ma20"):
            obv_sym = "🟢OBV↑" if obv["obv"] > obv["obv_ma20"] else "🔴OBV↓"
        else:
            obv_sym = "⬜ -  "

        bratio   = cvd.get("buy_ratio", 50)
        bdays_ok = f"{cvd.get('bull_days',0)}/{cvd.get('total_days',0)}일"

        print(f"  {rank:>3}위  {name}  {r['code']:6}  "
              f"{cvd_sym:^8}  {obv_sym:^8}  "
              f"{bratio:>4.1f}%  {bdays_ok:>5}  {r['total']:>3}점")

        # 다이버전스 강조
        if div:
            icon = "🟢" if div == "강세" else "🔴"
            print(f"        ⚡ OBV {div} 다이버전스 {icon}  "
                  f"({bdays}일: 가격{'↓' if div=='강세' else '↑'} + OBV{'↑' if div=='강세' else '↓'})")

    print(f"\n  {'─'*60}")
    if len(results) > top:
        print(f"  전체 {len(results)}개 분석 완료  |  하위 {len(results)-top}개 생략")


# ─────────────────────────────────────────────────────────────
# 명령 구현
# ─────────────────────────────────────────────────────────────

def cmd_scan(index_name: str, top: int = 10,
             period: int = 5, bdays: int = 5):
    """코스피200 또는 코스닥150 전종목 스캔."""
    token = _get_token()
    if not token:
        print("\n  ❌ 키움 API 미연결")
        print("  export KIWOOM_APP_KEY=앱키")
        print("  export KIWOOM_APP_SECRET=앱시크릿")
        print("  키움 REST API 신청: https://openapi.kiwoom.com")
        return

    indices = (["kospi200", "kosdaq150"] if index_name == "both"
               else [index_name])

    for idx in indices:
        stocks = load_index(idx)          # [{"code":..., "name":...}, ...]
        label  = "코스피200" if idx == "kospi200" else "코스닥150"
        total  = len(stocks)

        if not stocks:
            continue

        # 종목명 캐시 — analyze() 내부에서 ka10001 중복 호출 방지
        name_cache = {s["code"]: s["name"] for s in stocks}

        print(f"\n  {label} 스캔 시작: {total}개 종목")
        print(f"  분봉({period}분) 최근 {bdays}영업일 한 번에 수집 중...")

        results = []
        for i, s in enumerate(stocks, 1):
            code = s["code"]
            name = s["name"]
            print(f"  [{i:3}/{total}] {code} {name[:8]} ...", end="\r")
            r = analyze(code, token, period=period, bdays=bdays, name=name)
            if r:
                results.append(r)
            time.sleep(_RATE_DELAY)

        _print_results(results, top, label, period, bdays)


def cmd_detail(code: str, token: str, period: int = 5, bdays: int = 5):
    """단일 종목 상세 분석."""
    print(f"\n  [{code}] 분봉({period}분) 최근 {bdays}영업일 데이터 수집 중...")
    r = analyze(code, token, period=period, bdays=bdays)
    if not r:
        print(f"  ❌ 데이터 수집 실패")
        return

    cvd = r["cvd"]
    obv = r["obv"]
    div = obv.get("divergence")

    print(f"\n{'='*62}")
    print(f"  CVD/OBV 상세 분석: {r['name']} ({code})")
    print(f"  분봉 {period}분  |  최근 {bdays}영업일  |  {r['min_bars']}봉 수집")
    print(f"{'='*62}")

    print(f"\n  [CVD — {period}분봉 최근 {bdays}영업일 한 번에 계산]")
    print(f"  수집 봉 수:    {r['min_bars']}봉  ({r['days']}일 포함)")
    print(f"  현재 CVD:      {_fmt(cvd.get('cvd'))}")
    print(f"  CVD MA({min(20, r['min_bars'])}):    {_fmt(cvd.get('cvd_ma'))}")
    print(f"  총 매수 체결:  {_fmt(cvd.get('buy_vol'))}주")
    print(f"  총 매도 체결:  {_fmt(cvd.get('sell_vol'))}주")
    print(f"  매수 비율:     {cvd.get('buy_ratio',50):.1f}%")
    print(f"  5일 매수우위:  {cvd.get('bull_days',0)}/{cvd.get('total_days',0)}일")
    print(f"  모멘텀(5봉):   {_fmt(cvd.get('momentum'))}")
    print(f"  신호:          {cvd.get('signal','-')}")
    print(f"  CVD 점수:      {cvd['score']}점")

    print(f"\n  [OBV — 일봉 60일 기반, 5영업일 기울기]")
    print(f"  현재 OBV:      {_fmt(obv.get('obv'))}")
    print(f"  OBV MA20:      {_fmt(obv.get('obv_ma20'))}")
    print(f"  5일 기울기:    {_fmt(obv.get('slope5'))}")
    print(f"  신호:          {obv.get('signal','-')}")
    if div:
        icon = "🟢" if div == "강세" else "🔴"
        print(f"  ⚡ OBV {div} 다이버전스 {icon}")
        desc = ("가격 하락 + OBV 상승 → 저점 매집, 반등 기대" if div == "강세"
                else "가격 상승 + OBV 하락 → 매수세 이탈, 추세 약화")
        print(f"     {desc}")
    print(f"  OBV 점수:      {obv['score']}점")

    print(f"\n  {'═'*42}")
    print(f"  총 점수: {r['total']}점")
    verdict = ("🟢 강한 매수 신호" if r["total"] >= 8 else
               "🟢 매수 신호"     if r["total"] >= 5 else
               "🟡 중립"          if r["total"] >= 2 else
               "🔴 매도/회피")
    print(f"  판정:    {verdict}")
    print(f"  {'═'*42}")


def cmd_update_index(token: str):
    """종목 목록 최신화 (API 조회 실패 시 기본값 저장)."""
    print("  종목 목록 최신화 중...")
    for name, default in [("kospi200", _KOSPI200_DEFAULT),
                           ("kosdaq150", _KOSDAQ150_DEFAULT)]:
        # 키움 업종별 종목 조회 시도
        tickers = []
        code    = "028" if name == "kospi200" else "004"
        # ka10099: 업종별종목조회
        data    = _kiwoom("/api/dostk/mrkcap",
                          {"upjong_code": code}, "ka10099", token)
        if data and data.get("output"):
            for item in data["output"]:
                c = str(item.get("stk_cd", "")).strip()
                if c and c.isdigit() and len(c) == 6:
                    tickers.append(c)

        if len(tickers) >= 50:
            save_index(name, tickers)
        else:
            print(f"  ⚠️  {name} API 조회 실패 → 기본값 {len(default)}개 저장")
            save_index(name, default)
            print(f"     정확한 최신 목록: https://data.krx.co.kr")


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="키움 REST API CVD/OBV 스캐너 — 최근 5영업일 분봉 한 번에 수집",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""사용 예시:
  # 종목 목록 먼저 생성 (최초 1회, 분기별 변경 시 재실행)
  python3 tools/update_index.py

  # 5영업일 스캔 (기본)
  python3 tools/kiwoom_cvd_obv_scanner.py scan --index kospi200 --top 10
  python3 tools/kiwoom_cvd_obv_scanner.py scan --index kosdaq150 --top 10
  python3 tools/kiwoom_cvd_obv_scanner.py scan --index both --top 10

  # CVD 산출 기간 변경
  python3 tools/kiwoom_cvd_obv_scanner.py scan --index kospi200 --days 10
  python3 tools/kiwoom_cvd_obv_scanner.py scan --index kospi200 --days 20

  # 단일 종목 상세
  python3 tools/kiwoom_cvd_obv_scanner.py detail --code 005930 --days 10

환경변수:
  export KIWOOM_APP_KEY=앱키
  export KIWOOM_APP_SECRET=앱시크릿"""
    )

    sub = parser.add_subparsers(dest="cmd")

    # scan
    ps = sub.add_parser("scan", help="코스피200/코스닥150 스캔")
    ps.add_argument("--index",  default="kospi200",
                    choices=["kospi200", "kosdaq150", "both"])
    ps.add_argument("--top",    type=int, default=10)
    ps.add_argument("--period", type=int, default=5,
                    choices=[1, 3, 5, 10, 15, 30, 60])
    ps.add_argument("--days",   type=int, default=5,
                    help="최근 영업일 수 (기본: 5)")

    # detail
    pd = sub.add_parser("detail", help="단일 종목 상세 분석")
    pd.add_argument("--code",   required=True)
    pd.add_argument("--period", type=int, default=5)
    pd.add_argument("--days",   type=int, default=5)

    # update-index
    sub.add_parser("update-index", help="종목 목록 최신화")

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        sys.exit(0)

    if args.cmd == "scan":
        cmd_scan(args.index, top=args.top,
                 period=args.period, bdays=args.days)

    elif args.cmd == "detail":
        token = _get_token()
        if not token:
            print("❌ 키움 API 미연결")
            sys.exit(1)
        cmd_detail(args.code, token, period=args.period, bdays=args.days)

    elif args.cmd == "update-index":
        cmd_update_index()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
stock_screener.py — 모멘텀 발견 + 가치 검증 종목 스크리너

사용법:
  python3 stock_screener.py                      # watchlist 전체 스캔
  python3 stock_screener.py 005930 000660 035720 # 지정 종목 스캔
  python3 stock_screener.py --update 005930      # 005930 기본면 데이터 업데이트

프레임워크:
  1단계 (모멘텀 발견): 60일 신고가 + 거래량 급증 → 후보군 진입
  2단계 (가치 검증):  6개 기준 점수 ≥ 3/6 → 매수 신호
  신호 등급: 3/6 = 탐색 포지션 3% | 4/6 = 표준 포지션 5% | 5~6/6 = 확신 포지션 8%

개선 사항 (NVDA/AMD/MU 백테스트 반영):
  1. 매출총이익률 2분기 연속 개선 → 독립 매수 조건 추가
  2. EPS 예상 초과 > 30% → 경기순환주 독립 조건 추가
  3. 이진 판단 대신 신호 등급제 도입
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from collections import OrderedDict

# ============================================================
# 설정
# ============================================================

DATA_DIR       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
FUND_FILE      = os.path.join(DATA_DIR, "fundamentals.json")
WATCHLIST_FILE = os.path.join(DATA_DIR, "watchlist.json")

# 기본 watchlist — 한국주 + 미국주 혼합
DEFAULT_WATCHLIST = {
    "kr_semiconductor": ["005930", "000660", "042700"],        # 삼성전자, SK하이닉스, 한미반도체
    "kr_tech":          ["035720", "035420", "259960"],        # 카카오, NAVER, 크래프톤
    "kr_battery":       ["373220", "247540", "006400"],        # LG에너지솔루션, 에코프로비엠, 삼성SDI
    "kr_auto":          ["005380", "000270", "012330"],        # 현대차, 기아, 현대모비스
    "kr_bio":           ["207940", "068270", "326030"],        # 삼성바이오로직스, 셀트리온, SK바이오팜
    "us_ai_chip":       ["NVDA", "AMD", "MU", "AVGO"],
    "us_ai_app":        ["GOOG", "META", "MSFT", "AMZN"],
    "hk_internet":      ["0700.HK", "9888.HK"],
}

# ============================================================
# 가격 데이터 수집
# ============================================================

def _is_korean(ticker: str) -> bool:
    """한국 종목 코드 여부 판별 (6자리 숫자)."""
    return ticker.replace(".", "").isdigit() and len(ticker) == 6


def fetch_prices_curl(ticker: str, days: int = 120):
    """Yahoo Finance 일봉 데이터 수신 (한국주: .KS 접미사 자동 추가)."""
    yf_ticker = f"{ticker}.KS" if _is_korean(ticker) else ticker
    end_ts   = int(datetime.now().timestamp())
    start_ts = int((datetime.now() - timedelta(days=days)).timestamp())
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_ticker}"
        f"?period1={start_ts}&period2={end_ts}&interval=1d"
    )
    try:
        result = subprocess.run(
            ["curl", "-s", "-H", "User-Agent: Mozilla/5.0", url],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return None
        data  = json.loads(result.stdout)
        chart = data.get("chart", {}).get("result", [{}])[0]
        timestamps = chart.get("timestamp", [])
        quote      = chart.get("indicators", {}).get("quote", [{}])[0]
        rows = []
        for i, ts in enumerate(timestamps):
            c = quote.get("close",  [None] * len(timestamps))[i]
            v = quote.get("volume", [None] * len(timestamps))[i]
            h = quote.get("high",   [None] * len(timestamps))[i]
            if c and v and h:
                dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                rows.append({"date": dt, "close": c, "high": h, "volume": v})
        return rows if len(rows) > 60 else None
    except Exception:
        return None


# ============================================================
# 기본면 데이터 관리
# ============================================================

def load_fundamentals() -> dict:
    if os.path.exists(FUND_FILE):
        with open(FUND_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_fundamentals(data: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(FUND_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def update_fundamental_interactive(ticker: str):
    """대화식 기본면 데이터 업데이트."""
    funds = load_fundamentals()
    if ticker not in funds:
        funds[ticker] = {"quarters": {}}

    name = funds[ticker].get("name", ticker)
    print(f"\n  {ticker} ({name}) 기본면 데이터 업데이트")
    print(f"  기존 분기: {', '.join(funds[ticker]['quarters'].keys()) or '없음'}")
    print(f"  (데이터 출처: DART dart.fss.or.kr 또는 네이버금융)")

    date      = input("  실적 발표일 (YYYY-MM-DD): ").strip()
    label     = input("  레이블 (예: 2025Q1): ").strip()
    rev_yoy   = float(input("  매출액 전년 대비 증가율 (%): "))
    gm        = float(input("  매출총이익률 (%): "))
    eps_beat  = float(input("  EPS 컨센서스 초과율 (%): "))

    funds[ticker]["quarters"][date] = {
        "label": label, "rev_yoy": rev_yoy, "gm": gm, "eps_beat": eps_beat
    }
    save_fundamentals(funds)
    print(f"  ✅ {ticker} {label} 저장 완료")


# ============================================================
# 1단계: 모멘텀 발견
# ============================================================

def check_momentum(prices: list) -> dict:
    """60일 신고가 + 거래량 급증 모멘텀 신호 확인."""
    if len(prices) < 61:
        return None

    latest = prices[-1]
    close  = latest["close"]

    # 60일 신고가
    past_60_highs = [p["high"] for p in prices[-61:-1]]
    is_60d_high   = close > max(past_60_highs)

    # 거래량: 최근 5일 평균 > 20일 평균 × 1.5배
    vol_5    = sum(p["volume"] for p in prices[-5:]) / 5
    vol_20   = sum(p["volume"] for p in prices[-20:]) / 20
    vol_ratio = vol_5 / vol_20 if vol_20 > 0 else 0
    is_volume = vol_ratio > 1.5

    # 30일 등락률
    close_30d = prices[-31]["close"] if len(prices) > 30 else prices[0]["close"]
    pct_30d   = (close - close_30d) / close_30d * 100

    # 최근 5일 내 돌파일 존재 여부
    recent_breakout = False
    for i in range(-5, 0):
        if prices[i]["close"] > max(p["high"] for p in prices[i-60:i]):
            recent_breakout = True
            break

    triggered = (is_60d_high or recent_breakout) and is_volume

    return {
        "triggered":   triggered,
        "close":       round(close, 2),
        "date":        latest["date"],
        "is_60d_high": is_60d_high,
        "vol_ratio":   round(vol_ratio, 2),
        "pct_30d":     round(pct_30d, 1),
    }


# ============================================================
# 2단계: 가치 검증 (6개 기준)
# ============================================================

def check_value(ticker: str, signal_date: str = None) -> dict:
    """6개 기준 가치 검증."""
    funds = load_fundamentals()
    if ticker not in funds or not funds[ticker].get("quarters"):
        return None

    quarters  = funds[ticker]["quarters"]
    sorted_q  = sorted(quarters.items(), key=lambda x: x[0])

    if signal_date:
        valid = [(d, q) for d, q in sorted_q if d <= signal_date]
    else:
        valid = sorted_q

    if not valid:
        return None

    latest = valid[-1]
    prev   = valid[-2] if len(valid) >= 2 else None
    prev2  = valid[-3] if len(valid) >= 3 else None

    d   = latest[1]
    pd  = prev[1]  if prev  else None
    pd2 = prev2[1] if prev2 else None

    checks = {}

    # 1. 매출 성장 가속 (전분기 대비 증가율 개선)
    if pd:
        checks["매출성장가속"] = d["rev_yoy"] > pd["rev_yoy"]
    else:
        checks["매출성장가속"] = d["rev_yoy"] > 20

    # 2. 매출총이익률 방향
    if pd:
        checks["이익률확장"] = d["gm"] > pd["gm"] or d["gm"] > 55
    else:
        checks["이익률확장"] = d["gm"] > 45

    # 3. EPS 컨센서스 초과 > 10%
    checks["실적서프라이즈"] = d["eps_beat"] > 10

    # 4. 고성장 (매출 증가율 > 15%)
    checks["고성장"] = d["rev_yoy"] > 15

    # 5. 건전한 이익률 (매출총이익률 > 40%)
    checks["건전이익률"] = d["gm"] > 40

    # 6. 이익률 2분기 연속 개선 (NVDA 2023-01 누락 방지)
    if pd and pd2:
        checks["이익률연속개선"] = d["gm"] > pd["gm"] > pd2["gm"]
    elif pd:
        checks["이익률연속개선"] = d["gm"] > pd["gm"]
    else:
        checks["이익률연속개선"] = False

    score = sum(1 for v in checks.values() if v)

    # 독립 통과 조건
    independent_pass   = False
    independent_reason = ""

    # 조건 A: 이익률 2분기 연속 개선 + 이익률 > 45% (NVDA 2023-01 케이스)
    if checks.get("이익률연속개선") and d["gm"] > 45:
        independent_pass   = True
        independent_reason = "이익률 연속 개선 + 45% 초과"

    # 조건 B: EPS 초과 > 30% (경기순환주 바닥 신호)
    if d["eps_beat"] > 30:
        independent_pass   = True
        independent_reason = "EPS 컨센서스 30% 초과 (경기순환주 신호)"

    return {
        "score":              score,
        "max":                6,
        "checks":             checks,
        "fund":               d,
        "fund_date":          latest[0],
        "fund_label":         d.get("label", ""),
        "independent_pass":   independent_pass,
        "independent_reason": independent_reason,
    }


# ============================================================
# 신호 등급 판정
# ============================================================

def grade_signal(momentum: dict, value: dict) -> tuple:
    """모멘텀 + 가치 종합 등급 반환."""
    if not momentum or not momentum["triggered"]:
        return "SKIP", "모멘텀 신호 없음", ""

    if not value:
        return "WATCH", "모멘텀 발생 — 기본면 데이터 없음", "기본면 보완 필요"

    score = value["score"]
    ind   = value["independent_pass"]

    if score >= 5 or (score >= 4 and ind):
        return "BUY_8%", f"확신 포지션 ({score}/6)", "8% 포지션 권장"
    elif score >= 4 or (score >= 3 and ind):
        return "BUY_5%", f"표준 포지션 ({score}/6)", "5% 포지션 권장"
    elif score >= 3:
        return "BUY_3%", f"탐색 포지션 ({score}/6)", "3% 포지션 권장"
    elif ind:
        return "BUY_3%", f"독립 조건 통과: {value['independent_reason']}", "3% 포지션 권장"
    else:
        return "PASS", f"모멘텀은 있으나 기본면 미달 ({score}/6)", "계속 관찰"


# ============================================================
# 단일 종목 스캔
# ============================================================

def scan_ticker(ticker: str, verbose: bool = True) -> dict:
    """단일 종목 스캔."""
    prices = fetch_prices_curl(ticker)
    if not prices:
        if verbose:
            yf_suffix = ".KS" if _is_korean(ticker) else ""
            print(f"  {ticker:<10} ⚠️  가격 데이터 수신 실패 (Yahoo: {ticker}{yf_suffix})")
        return None

    momentum = check_momentum(prices)
    value    = check_value(ticker)
    grade, reason, advice = grade_signal(momentum, value)

    result = {
        "ticker":   ticker,
        "grade":    grade,
        "reason":   reason,
        "advice":   advice,
        "momentum": momentum,
        "value":    value,
    }

    if verbose:
        m = momentum
        symbol = {
            "BUY_8%": "🔴", "BUY_5%": "🟡", "BUY_3%": "🟢",
            "WATCH":  "👀", "PASS":   "⬜", "SKIP":   "  "
        }
        s = symbol.get(grade, "  ")

        # 한국주는 원화 단위, 미국주는 달러 단위
        price_str = f"{m['close']:,.0f}원" if _is_korean(ticker) else f"${m['close']}"

        if grade.startswith("BUY"):
            print(f"  {s} {ticker:<10} {price_str:<12} 30일 {m['pct_30d']:+.1f}% 거래량{m['vol_ratio']}x  → {grade} {reason}")
            if value:
                v = value
                checks_str = " ".join(f"{'✅' if val else '❌'}{k}" for k, val in v["checks"].items())
                print(f"     기본면({v['fund_label']}): 매출{v['fund']['rev_yoy']}% 이익률{v['fund']['gm']}% EPS초과{v['fund']['eps_beat']}%")
                print(f"     {checks_str}")
                if v["independent_pass"]:
                    print(f"     ★ 독립 조건 통과: {v['independent_reason']}")
        elif grade == "WATCH":
            print(f"  {s} {ticker:<10} {price_str:<12} 30일 {m['pct_30d']:+.1f}%  → 모멘텀 발생! 기본면 데이터 보완 필요")
        elif grade == "PASS":
            print(f"  {s} {ticker:<10} {price_str:<12}  → {reason}")

    return result


# ============================================================
# 메인
# ============================================================

def main():
    args = sys.argv[1:]

    # 업데이트 모드
    if args and args[0] == "--update":
        ticker = args[1] if len(args) > 1 else input("  종목 코드: ").strip().upper()
        update_fundamental_interactive(ticker)
        return

    # 기본 watchlist 초기화
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(WATCHLIST_FILE):
        with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_WATCHLIST, f, indent=2, ensure_ascii=False)
        print(f"  기본 watchlist 생성: {WATCHLIST_FILE}")

    # 스캔 대상 결정
    if args:
        tickers = [t.upper() for t in args]
    else:
        with open(WATCHLIST_FILE, encoding="utf-8") as f:
            wl = json.load(f)
        tickers = [sym for group in wl.values() for sym in group]

    # 스캔 실행
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"\n{'='*70}")
    print(f"  모멘텀 발견 + 가치 검증 종목 스크리너  {today}")
    print(f"  스캔 대상: {len(tickers)}개 종목")
    print(f"{'='*70}\n")

    buy_signals   = []
    watch_signals = []

    for ticker in tickers:
        result = scan_ticker(ticker)
        if result:
            if result["grade"].startswith("BUY"):
                buy_signals.append(result)
            elif result["grade"] == "WATCH":
                watch_signals.append(result)

    # 결과 요약
    print(f"\n{'='*70}")
    print(f"  📋 스캔 결과 요약")
    print(f"{'='*70}")

    if buy_signals:
        print(f"\n  🎯 매수 신호: {len(buy_signals)}개")
        for s in sorted(buy_signals, key=lambda x: x["grade"], reverse=True):
            m = s["momentum"]
            price_str = f"{m['close']:,.0f}원" if _is_korean(s["ticker"]) else f"${m['close']}"
            print(f"     {s['grade']:<8} {s['ticker']:<10} {price_str:<12} {s['reason']}")
    else:
        print(f"\n  매수 신호 없음")

    if watch_signals:
        print(f"\n  👀 관찰 대상 (기본면 보완 필요): {len(watch_signals)}개")
        for s in watch_signals:
            m = s["momentum"]
            price_str = f"{m['close']:,.0f}원" if _is_korean(s["ticker"]) else f"${m['close']}"
            print(f"     {s['ticker']:<10} {price_str:<12} 30일 {m['pct_30d']:+.1f}% — --update {s['ticker']} 로 기본면 입력")

    print(f"\n  기본면 데이터 파일: {FUND_FILE}")
    print(f"  Watchlist 파일:     {WATCHLIST_FILE}")
    print(f"  사용법: --update 종목코드  (기본면 입력/수정)")
    print(f"  데이터 출처: DART(dart.fss.or.kr), 네이버금융, Yahoo Finance\n")


if __name__ == "__main__":
    main()

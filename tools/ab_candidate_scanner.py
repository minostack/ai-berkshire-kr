#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A·B 후보 종목 선정기 (초안, 2026-09-27)

근거 문서: `reports/종목검색-지표-리서치-20260927.md` §5 "다음에 시험할 후보"

    A. 실적 서프라이즈 후 표류(PEAD)          보유 20~60거래일
    B. 거래량 급증 + 52주 신고가 근접          보유 20거래일

둘 다 **학술 근거가 있는 지표군**이지만, 우리 유니버스·우리 비용으로는
**아직 검증하지 않았다**. 이 도구는 "사전등록 전 초안" 정의로 오늘의 후보를
뽑을 뿐이다. 매매 판단의 단독 근거로 쓰지 말 것.

B — 거래량 급증 + 52주 신고가 근접
==================================
입력은 로컬 일봉(`silver.daily_bars`, 수정주가)뿐이다 — 추가 수집이 없다.

    RVOL      = mean(거래량, D-4 … D) / mean(거래량, D-54 … D-5)
                (최근 5거래일 vs 그 전 50거래일 — Gervais·Kaniel·Mingelgrin
                 2001의 "1주 vs 이전 49~50일" 구성을 따랐다)
    52주 근접 = 종가_D / max(고가, 최근 252거래일)
                (George·Hwang 2004의 정의 — 1.0이면 신고가)

게이트
  1. 최근 252거래일 중 240일 이상 일봉 존재      (52주 고가를 정의할 수 있어야)
  2. RVOL ≥ 1.5
  3. 52주 근접 ≥ 0.90                            (고가 대비 -10% 이내)
  4. 20일 평균 거래대금 ≥ 10억 원                (체결 가능성)

점수(100) = RVOL 백분위 × 50 + 52주 근접 백분위 × 50  (그날 유니버스 내)

A — 실적 서프라이즈 후 표류(PEAD)
=================================
입력은 DART `fnlttMultiAcnt`(다중회사 주요계정)의 **영업이익**이다.
컨센서스가 없으므로 **과거추세 기준 SUE**(표준화 예상외 이익)를 쓴다.

    분기 단독 영업이익: 1Q·2Q·3Q 보고서는 thstrm_amount(3개월)
                        4Q = 사업보고서 연간 − 3Q 보고서 누적(thstrm_add_amount)
    ΔQ_t   = Q_t − Q_{t-4}      전년 동기 값은 **같은 보고서의 frmtrm**을 쓴다
                                (재작성·정정이 반영된 비교 기준을 쓰기 위함)
    SUE_t  = ΔQ_t / 표준편차(ΔQ_{t-1} … ΔQ_{t-8})   최소 4개 필요

    이벤트일 = 잠정실적 공시일 (설계 0.2 — `docs/PREREGISTRATION_A2.md`)
               분기 말일 다음날 ~ 정기보고서 접수일 사이 가장 이른 공정공시
               `영업(잠정)실적`. 없거나 월별 공시자(한 해 8건 초과)면 정기보고서
               접수일(rcept_no 앞 8자리)
    진입     = 이벤트일 **다음** 거래일 (장 마감 후 공시 가능성)

    ⚠ 실거래 제약: SUE는 정기보고서의 **확정** 영업이익으로 계산한다. 잠정치를
    구조적으로 받을 API가 없어서다. 그래서 후보는 정기보고서가 접수된 **뒤에야**
    목록에 나온다. 경과일은 잠정 공시일부터 세므로 백테스트 A2와 같은 보유 창을
    쓰지만, 화면의 '지연'(잠정 공시 → 보고서 접수 거래일 수)만큼 A2 백테스트의
    진입보다 늦다. A2 백테스트 수익률을 이 목록의 기대수익으로 읽으면 안 된다.
    (설계 0.1은 이벤트일 = 정기보고서 접수일 — A v1 백테스트와 같은 정의)

게이트
  1. SUE 계산 가능(과거 ΔQ 4개 이상, 표준편차 > 0)
  2. SUE ≥ 1.0
  3. 진입일 이후 경과 거래일 ≤ 60                (표류 보유 구간 안)
  4. 20일 평균 거래대금 ≥ 10억 원

정렬 = SUE 내림차순. 52주 근접도는 참고로 함께 낸다 — 한국 표본에서
"52주 고가에 가까울수록 PEAD가 크다"는 보고가 있다(Pacific-Basin Finance
Journal 44, 2017). 이 조합은 게이트에 넣지 않았다(검증 전).

사용법
======
    python tools/ab_candidate_scanner.py                  # A와 B 모두
    python tools/ab_candidate_scanner.py --mode B         # 로컬 일봉만, 네트워크 없음
    python tools/ab_candidate_scanner.py --date 2026-09-22 --top 30
    python tools/ab_candidate_scanner.py --mode A --refresh-dart

결과 JSON은 `reports/daily-scan/ab-candidates-*.json`에 남는다.
"""

import argparse
import io
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import date, datetime, timedelta
from typing import Optional

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_BASE, ".env"))
except ImportError:
    pass  # python-dotenv 미설치 시 OS 환경변수만 사용

#: 0.1 → 0.2 (2026-09-27): A의 이벤트일을 정기보고서 접수일 → 잠정실적 공시일로
#: 바꿨다(`docs/PREREGISTRATION_A2.md`). B는 0.1과 같다.
#: 0.2 → 0.3 (2026-09-27): B 통과 종목에 "A 조건 충족" 표시를 붙였다
#: (`docs/PREREGISTRATION_AB.md` §4). **B 선정 기준은 바뀌지 않았다** — 표시만 늘었다.
DESIGN_VERSION = "ab-draft-0.3"

# ── B 정의 — 바꾸면 다른 설계다. 사전등록 시 이 값을 그대로 고정할 것 ──
RVOL_RECENT, RVOL_BASE = 5, 50
RVOL_MIN = 1.5
HIGH_WINDOW = 252
MIN_HISTORY = 240
PROX_MIN = 0.90
LIQ_DAYS = 20
LIQ_MIN_KRW = 1_000_000_000
HOLD_B = 20

# ── A 정의 ──
ACCOUNT_PREFIX = "영업이익"
SUE_HIST, SUE_MIN_HIST = 8, 4
SUE_MIN = 1.0
EVENT_WINDOW = 60
HOLD_A = (20, 60)

# ── A 이벤트일(A2) — `docs/PREREGISTRATION_A2.md` §1 ──
PRELIM_KEY = "영업(잠정)실적"
MONTHLY_MAX = 8          # 한 해 잠정실적 공시가 이보다 많으면 월별 공시자로 본다
PRELIM_LIVE_MAX_AGE = 12 * 3600

# ── A∩B 표시 — `docs/PREREGISTRATION_AB.md` §1 ──
AB_SUE_MIN = 1.0         # 신호일에 이미 접수된 가장 최근 분기 SUE ≥ 이 값
AB_RECENCY_TD = 60       # 그 정기보고서 접수 후 경과 거래일 ≤ 이 값

#: 잠정 일봉(16:10, 1분봉 합 — 거래량 근사)일 때 RVOL을 믿을 수 있는 폭.
#: 실측: 2026-10-14 HMM 잠정 1.76 → 확정 2.11(+20%), 10/7 씨젠 잠정 탈락 → 확정 1.51.
#: 게이트 ±이 폭 안의 종목은 확정 뒤 판정이 바뀔 수 있다고 표시한다(선정은 안 바꾼다).
PROVISIONAL_RVOL_BAND = 0.20
#: 일봉 거래량에 NXT 메인마켓을 더한 봉의 표시(`cvd_obv.ingestion.nxt_volume`)
NXT_FLAG = "NXT_MAIN_VOLUME_INCLUDED"

INDEXES = ("KOSPI200", "KOSDAQ150")
SNAPSHOT_DIR = os.path.join(_BASE, "reports", "daily-scan")
DART_CACHE_DIR = os.path.join(_BASE, "data", "bronze", "dart_fnltt_multi")
DART_CORP_CACHE = os.path.join(_BASE, "data", "bronze", "dart_corp_codes.json")
DART_LIST_LIVE_DIR = os.path.join(_BASE, "data", "bronze", "dart_list_I002_live")
DART_URL = "https://opendart.fss.or.kr/api"
DART_BATCH = 100                 # fnlttMultiAcnt가 한 번에 받는 회사 수 상한
DART_EMPTY_RETRY_SEC = 12 * 3600  # 미제출이던 회사는 12시간 뒤 다시 묻는다

#: 보고서 코드 → 분기. 4분기는 사업보고서(연간)에서 3분기 누적을 빼서 만든다.
REPRT_Q = {"11013": 1, "11012": 2, "11014": 3, "11011": 4}

WARNING = (
    "⚠ A·B는 학술 근거가 있는 지표군이지만 우리 유니버스·비용(왕복 0.409%)으로는 "
    "미검증이다.\n  사전등록 전 초안 정의로 뽑은 후보일 뿐 — 매매 판단의 단독 "
    "근거로 쓰지 말 것.")


# ─────────────────────────────────────────────────────────────
# 공통 계산
# ─────────────────────────────────────────────────────────────

def percentile_ranks(values: list) -> list:
    """평균순위 백분위(0~1). 동점은 같은 값, 표본 1개면 1.0."""
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [1.0]
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2
        for k in range(i, j + 1):
            ranks[order[k]] = avg / (n - 1)
        i = j + 1
    return ranks


def avg_traded_value(bars: list, n: int = LIQ_DAYS) -> Optional[float]:
    """최근 n거래일 평균 거래대금. n일 미만이면 None — 짧은 평균을 n일로 속이지 않는다."""
    if len(bars) < n:
        return None
    return sum(b["value"] for b in bars[-n:]) / n


# ─────────────────────────────────────────────────────────────
# B — 거래량 급증 + 52주 신고가 근접
# ─────────────────────────────────────────────────────────────

def b_metrics(bars: list) -> Optional[dict]:
    """bars: 날짜 오름차순 [{date, high, close, volume, value}], 마지막이 기준일 D."""
    need = RVOL_RECENT + RVOL_BASE
    if len(bars) < need:
        return None
    vols = [b["volume"] for b in bars]
    recent = sum(vols[-RVOL_RECENT:]) / RVOL_RECENT
    base = sum(vols[-need:-RVOL_RECENT]) / RVOL_BASE
    window = bars[-HIGH_WINDOW:]
    high_52w = max(b["high"] for b in window)
    close = bars[-1]["close"]
    ret20 = (close / bars[-21]["close"] - 1) if len(bars) > 20 else None
    return {
        "as_of": bars[-1]["date"].isoformat(),
        "close": close,
        "rvol": recent / base if base > 0 else None,
        "vol_ratio_1d": vols[-1] / base if base > 0 else None,
        "high_52w": high_52w,
        "prox_52w": close / high_52w if high_52w > 0 else None,
        "history_days": len(window),
        "avg_value_20d": avg_traded_value(bars),
        "ret_20d": ret20,
        # 애프터마켓 포함 봉 수 — 최근 창에만 있으면 RVOL이 부풀려질 수 있다
        "ext_days_recent": sum(1 for b in bars[-RVOL_RECENT:] if b.get("extended")),
        "ext_days_base": sum(1 for b in bars[-need:-RVOL_RECENT] if b.get("extended")),
        # NXT 메인마켓 포함 봉 수 — 창 안에서 섞이면 RVOL이 거래량 정의 경계를 걸친다
        "nxt_days_recent": sum(1 for b in bars[-RVOL_RECENT:] if b.get("nxt")),
        "nxt_days_base": sum(1 for b in bars[-need:-RVOL_RECENT] if b.get("nxt")),
    }


def b_gate(m: dict) -> list:
    """통과하지 못한 조건 목록(빈 목록 = 통과)."""
    fails = []
    if m["history_days"] < MIN_HISTORY:
        fails.append("HISTORY")
    if m["rvol"] is None or m["rvol"] < RVOL_MIN:
        fails.append("RVOL")
    if m["prox_52w"] is None or m["prox_52w"] < PROX_MIN:
        fails.append("PROX_52W")
    if m["avg_value_20d"] is None or m["avg_value_20d"] < LIQ_MIN_KRW:
        fails.append("LIQUIDITY")
    return fails


def score_b(rows: list) -> list:
    """rows: b_metrics 결과에 code가 붙은 dict. 점수와 통과 여부를 채워 정렬해 돌려준다.

    백분위 모집단은 RVOL·근접도가 모두 계산된 **유니버스 전체**다 — 통과
    종목끼리만 백분위를 매기면 점수가 날마다 모집단 크기에 따라 흔들린다.
    """
    valid = [r for r in rows if r["rvol"] is not None and r["prox_52w"] is not None]
    pr_v = percentile_ranks([r["rvol"] for r in valid])
    pr_p = percentile_ranks([r["prox_52w"] for r in valid])
    for r, a, b in zip(valid, pr_v, pr_p):
        r["score"] = round(a * 50 + b * 50, 2)
    for r in rows:
        r.setdefault("score", None)
        r["fails"] = b_gate(r)
        r["passed"] = not r["fails"]
    rows.sort(key=lambda r: (-(r["score"] or -1), -(r["avg_value_20d"] or 0),
                             r["code"]))
    return rows


# ─────────────────────────────────────────────────────────────
# A — PEAD (과거추세 SUE)
# ─────────────────────────────────────────────────────────────

def parse_amount(s) -> Optional[float]:
    if s is None:
        return None
    s = str(s).replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def pick_account(rows: list, fs_div: str) -> Optional[dict]:
    """주요계정 행 중 손익계산서의 영업이익 한 줄."""
    cand = [r for r in rows
            if r.get("fs_div") == fs_div and r.get("sj_div") == "IS"
            and str(r.get("account_nm", "")).replace(" ", "").startswith(ACCOUNT_PREFIX)]
    if not cand:
        return None
    cand.sort(key=lambda r: int(r.get("ord") or 0))
    return cand[0]


def quarter_events(reports: dict) -> list:
    """한 회사의 보고서 묶음 → 분기별 ΔQ 이벤트(시간순).

    reports: {(bsns_year, reprt_code): [주요계정 행 …]}

    연결(CFS)을 우선하되 **한 회사 안에서는 한 가지만** 쓴다 — 분기마다
    연결·별도가 섞이면 ΔQ의 표준편차가 회계 기준 차이까지 먹는다.
    """
    if not reports:
        return []
    # 보고서 코드는 분기 순서와 다르다(11013=1Q > 11012=2Q) — 분기 번호로 비교한다
    latest = max(reports, key=lambda k: (k[0], REPRT_Q.get(k[1], 0)))
    fs_div = "CFS" if pick_account(reports[latest], "CFS") else "OFS"

    out = []
    for (year, rc), rows in sorted(reports.items()):
        q = REPRT_Q.get(rc)
        acc = pick_account(rows, fs_div)
        if q is None or acc is None:
            continue
        cur, prev = parse_amount(acc.get("thstrm_amount")), parse_amount(acc.get("frmtrm_amount"))
        if q == 4:
            q3 = pick_account(reports.get((year, "11014"), []), fs_div)
            if q3 is None:
                continue
            q3_cur = parse_amount(q3.get("thstrm_add_amount"))
            q3_prev = parse_amount(q3.get("frmtrm_add_amount"))
            if None in (cur, prev, q3_cur, q3_prev):
                continue
            cur, prev = cur - q3_cur, prev - q3_prev
        if cur is None or prev is None:
            continue
        rcept = str(acc.get("rcept_no") or "")
        if len(rcept) < 8:
            continue
        out.append({
            "period": f"{year}Q{q}",
            "value": cur,
            "prior_value": prev,
            "diff": cur - prev,
            "rcept_no": rcept,
            "rcept_date": datetime.strptime(rcept[:8], "%Y%m%d").date(),
            "fs_div": fs_div,
        })
    out.sort(key=lambda e: e["period"])
    return out


def sue_latest(events: list, as_of: date) -> Optional[dict]:
    """as_of까지 접수된 이벤트 중 가장 최근 분기의 SUE."""
    known = [e for e in events if e["rcept_date"] <= as_of]
    if not known:
        return None
    last = known[-1]
    hist = [e["diff"] for e in known[:-1]][-SUE_HIST:]
    ev = dict(last, hist_n=len(hist), sue=None)
    if len(hist) < SUE_MIN_HIST:
        return ev
    mean = sum(hist) / len(hist)
    sd = math.sqrt(sum((x - mean) ** 2 for x in hist) / (len(hist) - 1))
    if sd > 0:
        ev["sue"] = last["diff"] / sd
    return ev


def elapsed_trading_days(event_date: date, calendar: list) -> Optional[int]:
    """이벤트일 **다음** 거래일을 진입일(=1일째)로 센 경과 거래일. 진입일이 아직이면 0."""
    return sum(1 for d in calendar if d > event_date)


def a_gate(ev: dict) -> list:
    fails = []
    if ev.get("sue") is None:
        fails.append("SUE_NA")
    elif ev["sue"] < SUE_MIN:
        fails.append("SUE")
    el = ev.get("elapsed_td")
    if el is None or el < 1 or el > EVENT_WINDOW:
        fails.append("WINDOW")
    if ev.get("avg_value_20d") is None or ev["avg_value_20d"] < LIQ_MIN_KRW:
        fails.append("LIQUIDITY")
    return fails


# ── A 이벤트일: 잠정실적 공시일 (설계 0.2) ──

def is_prelim(report_nm: str) -> bool:
    return PRELIM_KEY in str(report_nm).replace(" ", "")


def quarter_end(period: str) -> date:
    import calendar as _cal
    y, q = int(period[:4]), int(period[-1])
    m = 3 * q
    return date(y, m, _cal.monthrange(y, m)[1])


def monthly_years(prelim_dates) -> set:
    """잠정실적 공시가 MONTHLY_MAX건을 넘는 연도(월별 실적 공시자로 본다)."""
    cnt = {}
    for d in prelim_dates:
        cnt[d.year] = cnt.get(d.year, 0) + 1
    return {y for y, n in cnt.items() if n > MONTHLY_MAX}


def prelim_event_date(period: str, rcept: date, prelim_dates, flagged_years) -> tuple:
    """(이벤트일, 사유). 분기 말일 다음날 ~ 정기보고서 접수일 사이 가장 이른
    잠정실적 공시일. 사유: PRELIM / NO_PRELIM / MONTHLY_FILER(→ 접수일 유지)."""
    qe = quarter_end(period)
    cand = sorted(d for d in prelim_dates if qe < d <= rcept)
    if not cand:
        return rcept, "NO_PRELIM"
    if cand[0].year in flagged_years or rcept.year in flagged_years:
        return rcept, "MONTHLY_FILER"
    return cand[0], "PRELIM"


def apply_prelim_event(ev: dict, prelim_dates, calendar: list) -> dict:
    """SUE 이벤트에 잠정실적 이벤트일과 경과일을 붙인다.

    경과일(`elapsed_td`)은 **잠정 공시일** 기준 — 백테스트 A2와 같은 보유 창이다.
    그러나 SUE는 정기보고서가 있어야 계산되므로, 실거래에서 이 후보를 처음
    알 수 있는 날은 **정기보고서 접수일**이다. 그 차이를 `report_lag_td`로 남긴다.
    """
    d, why = prelim_event_date(ev["period"], ev["rcept_date"], prelim_dates,
                               monthly_years(prelim_dates))
    ev["event_date"] = d
    ev["event_source"] = why
    ev["elapsed_td"] = elapsed_trading_days(d, calendar)
    ev["report_lag_td"] = ev["elapsed_td"] - elapsed_trading_days(ev["rcept_date"], calendar)
    return ev


# ── A∩B 표시 (설계 0.3) ──

def ab_condition(events: list, as_of: date, calendar: list) -> dict:
    """B 통과 종목의 A 조건 — 사전등록 AB §1.

    SUE는 **as_of에 이미 접수된 정기보고서**로만 계산한다(잠정 공시일을 쓰지 않는다 —
    확정치를 미리 보는 문제가 없도록). 백테스트(`ab_backtest_ab`)의
    `negfilter.classify` + `a_flag`와 같은 판정이다(테스트로 고정).

    status: A_POS / NEUTRAL / NEG / STALE / SUE_NA / NO_EVENT
    """
    ev = sue_latest(events, as_of)
    if ev is None:
        return {"a_flag": False, "status": "NO_EVENT", "sue": None, "age_td": None,
                "period": None}
    age = elapsed_trading_days(ev["rcept_date"], calendar)
    sue = ev["sue"]
    if sue is None:
        st = "SUE_NA"
    elif age > AB_RECENCY_TD:
        st = "STALE"
    elif sue >= AB_SUE_MIN:
        st = "A_POS"
    elif sue <= -AB_SUE_MIN:
        st = "NEG"
    else:
        st = "NEUTRAL"
    return {"a_flag": st == "A_POS", "status": st, "sue": sue, "age_td": age,
            "period": ev["period"], "rcept_date": ev["rcept_date"]}


# ─────────────────────────────────────────────────────────────
# 데이터 — 로컬 일봉·유니버스·종목명
# ─────────────────────────────────────────────────────────────

def load_universe(as_of: date, indexes) -> tuple:
    """{security_id: [지수…]}, 경고 목록 — 기준일 명단(유니버스 기준 universe-1.0,
    `cvd_obv.universe.universe_on`). 이력 밖 최근 날짜는 index_stocks.json과 이력
    마지막 구성 중 더 최근 것을 쓰고 경고한다."""
    from cvd_obv.universe import universe_on
    u = universe_on(as_of, indexes)
    members = {}
    for m in u.members():
        members.setdefault(m.security_id, []).append(m.index_id)
    return members, list(u.warnings)


def load_bars(securities, end: date, lookback_days: int = 420,
              allow_extended: bool = False) -> dict:
    """{security_id: [bar…]} — 기준 일봉(봉 기준 bars-1.0), 품질 FAILED 제외.

    정규장·수정주가 일봉만 쓴다. 2026-09-14 이후는 `cvdobv.py daily-cache
    regular-from-minute`가 1분봉으로 만든 정규장 봉(`DERIVED_MINUTE_1M`)이나
    15:35~16:00에 받은 당일 봉이다. 애프터마켓 포함 `EXTENDED` 봉은 거래량이
    정규장보다 커서 RVOL을 부풀리므로 기본으로 쓰지 않는다.
    `allow_extended=True`면 정규장 봉이 없는 날만 EXTENDED로 채우고 봉에
    `extended=True`를 남긴다.

    복원 봉의 빈 거래대금은 `bar_standard`의 단일 규칙(같은 날 EXTENDED 공식
    거래대금 × 거래량 비율, 없으면 종가×거래량)으로 추정한다.
    """
    import pyarrow.dataset as ds
    from cvd_obv.bar_standard import is_provisional, read_standard_daily

    start = end - timedelta(days=lookback_days)
    flt = (ds.field("trading_date") >= start) & (ds.field("trading_date") <= end)
    if securities is not None:
        flt = flt & ds.field("security_id").isin(sorted(securities))
    table = read_standard_daily(
        columns=["security_id", "trading_date", "high", "close", "volume",
                 "traded_value", "price_scope", "source", "quality_flags"],
        filters=flt, allow_extended=allow_extended, drop_failed=True,
        fill_traded_value=True)

    out = {}
    for sid, d, h, c, v, tv, scope, src, fl in zip(
            *(table.column(c).to_pylist()
              for c in ("security_id", "trading_date", "high", "close",
                        "volume", "traded_value", "price_scope", "source",
                        "quality_flags"))):
        out.setdefault(sid, []).append({
            "date": d, "high": float(h), "close": float(c), "volume": int(v),
            "value": float(tv) if tv is not None else None,
            "extended": scope == "EXTENDED",
            # 16:10 잠정 봉 — 1분봉 합이라 거래량이 근사다(익일 08:30 확정이 덮는다)
            "provisional": is_provisional(src),
            # 거래량에 NXT 메인마켓이 들어 있다(2026-08-03 이후 NXT 거래 종목)
            "nxt": NXT_FLAG in (fl or []),
        })
    for bars in out.values():
        bars.sort(key=lambda b: b["date"])
    return out


def load_names() -> dict:
    try:
        from cvd_obv.storage.parquet_store import ParquetStore
        t = ParquetStore().read_arrow("silver", "security_master",
                                      columns=["security_id", "name"])
        return dict(zip(t.column("security_id").to_pylist(),
                        t.column("name").to_pylist()))
    except Exception:
        return {}


# ─────────────────────────────────────────────────────────────
# 데이터 — DART
# ─────────────────────────────────────────────────────────────

#: 네트워크 오류(연결·SSL 핸드셰이크 시간 초과 등) 재시도 — 2026-10-15 실측에서
#: 한 번의 SSL 시간 초과로 A 전체가 비었다. 응답을 받은 뒤의 오류(HTTP 상태,
#: JSON이 아닌 본문)는 재시도하지 않는다 — 호출부가 따로 다룬다.
DART_RETRIES = 3
DART_RETRY_WAIT_SEC = 2.0


def _dart_get(path: str, params: dict, key: str, raw: bool = False):
    import socket
    import urllib.error

    q = urllib.parse.urlencode(dict(params, crtfc_key=key))
    for attempt in range(1, DART_RETRIES + 1):
        try:
            with urllib.request.urlopen(f"{DART_URL}/{path}?{q}", timeout=60) as r:
                body = r.read()
            break
        except urllib.error.HTTPError:
            raise                          # 서버가 응답했다 — 다시 물어도 같다
        except (urllib.error.URLError, socket.timeout, TimeoutError,
                ConnectionError) as e:
            if attempt == DART_RETRIES:
                raise
            time.sleep(DART_RETRY_WAIT_SEC * attempt)
    return body if raw else json.loads(body.decode("utf-8"))


def dart_corp_codes(key: str, max_age_sec: int = 7 * 86400) -> dict:
    """{종목코드6자리: corp_code} — 7일 캐시."""
    if os.path.exists(DART_CORP_CACHE) and \
            time.time() - os.path.getmtime(DART_CORP_CACHE) < max_age_sec:
        with open(DART_CORP_CACHE, encoding="utf-8") as f:
            return json.load(f)
    import xml.etree.ElementTree as ET
    raw = _dart_get("corpCode.xml", {}, key, raw=True)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        root = ET.fromstring(z.read(z.namelist()[0]).decode("utf-8"))
    out = {}
    for item in root.findall("list"):
        sc = (item.findtext("stock_code") or "").strip()
        cc = (item.findtext("corp_code") or "").strip()
        if sc and cc:
            out[sc] = cc
    os.makedirs(os.path.dirname(DART_CORP_CACHE), exist_ok=True)
    with open(DART_CORP_CACHE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    return out


_KEEP = ("rcept_no", "fs_div", "sj_div", "account_nm", "ord", "thstrm_amount",
         "thstrm_add_amount", "frmtrm_amount", "frmtrm_add_amount")


def dart_fetch_reports(corp_codes: list, years, key: str, refresh: bool = False,
                       log=print) -> dict:
    """{corp_code: {(year, reprt_code): [행…]}}

    캐시: `data/bronze/dart_fnltt_multi/{year}_{reprt}.json`
          {"rows": {corp_code: [영업이익 행…]}, "asked": {corp_code: 조회시각}}
    이미 행이 있는 회사는 다시 묻지 않는다(정정은 `--refresh-dart`).
    행이 없던 회사(미제출)는 12시간이 지나면 다시 묻는다.
    """
    os.makedirs(DART_CACHE_DIR, exist_ok=True)
    now = time.time()
    result = {cc: {} for cc in corp_codes}
    calls = 0
    for year in years:
        for rc in REPRT_Q:
            path = os.path.join(DART_CACHE_DIR, f"{year}_{rc}.json")
            cache = {"rows": {}, "asked": {}}
            if os.path.exists(path) and not refresh:
                with open(path, encoding="utf-8") as f:
                    cache = json.load(f)
            todo = [cc for cc in corp_codes
                    if refresh or (cc not in cache["rows"]
                                   and now - cache["asked"].get(cc, 0) > DART_EMPTY_RETRY_SEC)]
            pending = [todo[i:i + DART_BATCH] for i in range(0, len(todo), DART_BATCH)]
            while pending:
                batch = pending.pop(0)
                try:
                    data = _dart_get("fnlttMultiAcnt.json",
                                     {"corp_code": ",".join(batch),
                                      "bsns_year": str(year), "reprt_code": rc}, key)
                except ValueError:
                    # 2026-10-14 실측: 아직 없는 보고서(2026/11014)를 100개 묶음으로
                    # 물으면 JSON 대신 HTML 오류 페이지가 온다. 50개씩이면 정상(013).
                    # 묶음을 반으로 나눠 다시 묻는다 — 한 종목에서도 실패하면 진짜 오류.
                    calls += 1
                    if len(batch) == 1:
                        raise RuntimeError(f"DART {year}/{rc}: JSON이 아닌 응답 "
                                           f"(corp_code {batch[0]})")
                    half = len(batch) // 2
                    pending[:0] = [batch[:half], batch[half:]]
                    time.sleep(0.2)
                    continue
                calls += 1
                status = data.get("status")
                if status not in ("000", "013"):   # 013 = 조회 데이터 없음
                    raise RuntimeError(f"DART {year}/{rc}: {status} {data.get('message')}")
                for cc in batch:
                    cache["rows"].pop(cc, None)
                    cache["asked"][cc] = now
                for r in data.get("list", []) or []:
                    if r.get("sj_div") != "IS" or not str(r.get("account_nm", "")) \
                            .replace(" ", "").startswith(ACCOUNT_PREFIX):
                        continue
                    cache["rows"].setdefault(r["corp_code"], []).append(
                        {k: r.get(k) for k in _KEEP})
                time.sleep(0.2)
            if todo:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(cache, f, ensure_ascii=False)
            for cc in corp_codes:
                if cc in cache["rows"]:
                    result[cc][(year, rc)] = cache["rows"][cc]
    if calls:
        log(f"  DART 조회 {calls}회 (나머지는 캐시)")
    return result


def dart_prelim_dates(corp_codes, key, bgn: str, end: str, cache_dir: str,
                      max_age_sec: Optional[int] = None, log=print) -> dict:
    """{corp_code: [잠정실적 공시 접수일…]} — DART 공시검색(공정공시 I002), 회사별 캐시.

    `max_age_sec=None`이면 캐시가 있는 한 다시 묻지 않는다(백테스트).
    실거래 스캔은 12시간 캐시를 쓴다 — 새 잠정 공시를 놓치지 않기 위해.
    """
    os.makedirs(cache_dir, exist_ok=True)
    out, calls, now = {}, 0, time.time()
    for n, cc in enumerate(corp_codes, 1):
        path = os.path.join(cache_dir, f"{cc}.json")
        fresh = os.path.exists(path) and (
            max_age_sec is None or now - os.path.getmtime(path) < max_age_sec)
        if fresh:
            with open(path, encoding="utf-8") as f:
                rows = json.load(f)["rows"]
        else:
            rows, page = [], 1
            while True:
                d = _dart_get("list.json", {
                    "corp_code": cc, "bgn_de": bgn, "end_de": end,
                    "pblntf_detail_ty": "I002", "page_count": "100",
                    "page_no": str(page)}, key)
                calls += 1
                st = d.get("status")
                if st == "013":
                    break
                if st != "000":
                    raise RuntimeError(f"DART list {cc}: {st} {d.get('message')}")
                rows += [{"rcept_dt": r["rcept_dt"], "report_nm": r["report_nm"].strip(),
                          "rcept_no": r["rcept_no"]}
                         for r in d.get("list", []) if is_prelim(r.get("report_nm"))]
                if page >= int(d.get("total_page") or 1):
                    break
                page += 1
                time.sleep(0.1)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"fetched_at": datetime.now().isoformat(timespec="seconds"),
                           "range": [bgn, end], "rows": rows}, f, ensure_ascii=False)
            time.sleep(0.1)
        out[cc] = sorted({datetime.strptime(r["rcept_dt"], "%Y%m%d").date() for r in rows})
        if n % 100 == 0:
            log(f"  공시검색 {n}/{len(corp_codes)} (API {calls}회)")
    if calls:
        log(f"  DART 공시검색 {calls}회 (나머지는 캐시)")
    return out


# ─────────────────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────────────────

def run_b(bars_by_sid: dict, members: dict, names: dict, as_of: date) -> list:
    rows = []
    for sid, bars in bars_by_sid.items():
        if not bars or bars[-1]["date"] != as_of:
            continue     # 기준일 봉이 없는 종목(거래정지 등)은 순위에 넣지 않는다
        m = b_metrics(bars)
        if m is None:
            continue
        m.update(code=sid.split(":")[-1], security_id=sid,
                 name=names.get(sid, ""), index=",".join(members.get(sid, [])),
                 provisional=bool(bars[-1].get("provisional")))
        rows.append(m)
    return score_b(rows)


def nxt_boundary_rows(rows: list) -> list:
    """RVOL 창 안에 NXT 포함 봉과 KRX만인 봉이 섞인 종목 — 최근 창 비중이 큰 순.

    NXT에서 거래되지 않는 종목(창 전체 0)과 창 전체가 NXT 포함인 종목은 정의가 한결같다.
    """
    need = RVOL_RECENT + RVOL_BASE
    out = [r for r in rows
           if 0 < r.get("nxt_days_recent", 0) + r.get("nxt_days_base", 0) < need]
    return sorted(out, key=lambda r: (r["nxt_days_base"] / RVOL_BASE
                                      - r["nxt_days_recent"] / RVOL_RECENT))


def volume_definition(rows: list, mixed: list) -> dict:
    """스냅샷에 남기는 거래량 정의 — 나중에 결과를 비교할 때 기준이 같은지 본다."""
    need = RVOL_RECENT + RVOL_BASE
    return {"daily_volume": "KRX 정규장 + 시간외 종가 + NXT 메인마켓(09:00~15:20, "
                            "표시된 봉만)",
            "nxt_flag": NXT_FLAG,
            "nxt_included_from": "2026-08-03",
            "securities_all_nxt": sum(1 for r in rows
                                      if r.get("nxt_days_recent", 0)
                                      + r.get("nxt_days_base", 0) == need),
            "securities_no_nxt": sum(1 for r in rows
                                     if r.get("nxt_days_recent", 0)
                                     + r.get("nxt_days_base", 0) == 0),
            "securities_mixed": [{"code": r["code"], "recent": r["nxt_days_recent"],
                                  "base": r["nxt_days_base"]} for r in mixed]}


def provisional_watch(rows: list) -> dict:
    """잠정 봉에서 판정이 확정 뒤 바뀔 수 있는 B 종목(선정은 바꾸지 않는다).

    거래량만 근사이므로 RVOL 게이트만 본다 — 52주 근접·이력은 잠정 봉에서도
    정확하다(종가·고가는 정규장 1분봉 값). 거래대금은 근사지만 20일 평균이라
    하루 오차의 영향이 작다.
      `fragile`  통과했지만 RVOL < 게이트×(1+폭) — 확정 뒤 빠질 수 있다
      `possible` RVOL만 미달이고 RVOL ≥ 게이트÷(1+폭) — 확정 뒤 들어올 수 있다
    """
    hi = RVOL_MIN * (1 + PROVISIONAL_RVOL_BAND)
    lo = RVOL_MIN / (1 + PROVISIONAL_RVOL_BAND)
    fragile, possible = [], []
    for r in rows:
        if not r.get("provisional") or r.get("rvol") is None:
            continue
        if r["passed"] and r["rvol"] < hi:
            fragile.append(r)
        elif not r["passed"] and r.get("fails") == ["RVOL"] and r["rvol"] >= lo:
            possible.append(r)
    return {"fragile": fragile, "possible": possible, "band": PROVISIONAL_RVOL_BAND,
            "rvol_range": [round(lo, 3), round(hi, 3)]}


def annotate_b_with_a(b_rows: list, as_of: date, calendar: list, refresh: bool,
                      log=print) -> list:
    """B 통과 종목에 A 조건(`ab_condition`)을 붙인다. 선정은 바꾸지 않는다."""
    passed = [r for r in b_rows if r["passed"]]
    if not passed:
        return []
    key = os.environ.get("DART_API_KEY")
    if not key:
        for r in passed:
            r["a_cond"] = None
        return ["DART_API_KEY 미설정 — B의 A 조건 표시를 생략했다"]
    codes = dart_corp_codes(key)
    cc_of = {r["code"]: codes.get(r["code"]) for r in passed}
    ccs = sorted({c for c in cc_of.values() if c})
    reports = dart_fetch_reports(ccs, range(as_of.year - 3, as_of.year + 1), key,
                                 refresh, log) if ccs else {}
    for r in passed:
        cc = cc_of.get(r["code"])
        r["a_cond"] = ab_condition(quarter_events(reports.get(cc, {})) if cc else [],
                                   as_of, calendar)
    return []


def run_a(bars_by_sid: dict, members: dict, names: dict, as_of: date,
          calendar: list, refresh: bool, log=print) -> tuple:
    key = os.environ.get("DART_API_KEY")
    if not key:
        return [], ["DART_API_KEY 미설정 — .env에 설정하면 A를 돌릴 수 있다"]
    codes = dart_corp_codes(key)
    sid_by_cc, missing = {}, []
    for sid in members:
        cc = codes.get(sid.split(":")[-1])
        if cc:
            sid_by_cc[cc] = sid
        else:
            missing.append(sid)
    years = range(as_of.year - 3, as_of.year + 1)
    reports = dart_fetch_reports(sorted(sid_by_cc), years, key, refresh, log)
    # 월별 공시자 판정에 한 해 전체가 필요하다 — 전년 1월 1일부터 조회
    prelim = dart_prelim_dates(sorted(sid_by_cc), key, f"{as_of.year - 1}0101",
                               as_of.strftime("%Y%m%d"), DART_LIST_LIVE_DIR,
                               max_age_sec=0 if refresh else PRELIM_LIVE_MAX_AGE, log=log)

    rows = []
    for cc, reps in reports.items():
        sid = sid_by_cc[cc]
        ev = sue_latest(quarter_events(reps), as_of)
        if ev is None:
            continue
        bars = bars_by_sid.get(sid, [])
        bm = b_metrics(bars) if bars and bars[-1]["date"] == as_of else None
        ev.update(
            code=sid.split(":")[-1], security_id=sid, name=names.get(sid, ""),
            index=",".join(members.get(sid, [])),
            avg_value_20d=avg_traded_value(bars) if bars else None,
            prox_52w=bm["prox_52w"] if bm else None,
            close=bm["close"] if bm else None,
        )
        apply_prelim_event(ev, prelim.get(cc, []), calendar)
        ev["fails"] = a_gate(ev)
        ev["passed"] = not ev["fails"]
        rows.append(ev)
    rows.sort(key=lambda r: (-(r["sue"] if r["sue"] is not None else -1e9), r["code"]))
    warns = [f"DART corp_code 없음 {len(missing)}종목(리츠·신규상장 등): "
             + ", ".join(s.split(':')[-1] for s in missing[:10])] if missing else []
    return rows, warns


def _fmt_krw(v):
    return "-" if v is None else f"{v / 1e8:,.0f}억"


def print_b(rows: list, top: int):
    passed = [r for r in rows if r["passed"]]
    print(f"\n━━ B. 거래량 급증 + 52주 신고가 근접  (통과 {len(passed)} / 평가 {len(rows)})")
    print(f"   게이트: RVOL≥{RVOL_MIN}  52주근접≥{PROX_MIN:.2f}  20일평균거래대금≥{_fmt_krw(LIQ_MIN_KRW)}"
          f"  | 보유 {HOLD_B}거래일")
    if not passed:
        print("   (통과 종목 없음)")
        return
    print(f"   A조건 = 이미 접수된 최근 정기보고서 SUE≥{AB_SUE_MIN} (접수 후 ≤{AB_RECENCY_TD}거래일)"
          " — 사전등록 AB, 탐색 결과·전향 확인 전")
    print(f"   {'#':>2} {'코드':<7} {'종목명':<14} {'점수':>6} {'RVOL':>5} {'1일':>5} "
          f"{'52주근접':>7} {'20일수익':>7} {'거래대금':>8}  {'A조건':<18} 지수")
    for i, r in enumerate(passed[:top], 1):
        print(f"   {i:>2} {r['code']:<7} {r['name'][:12]:<14} {r['score']:>6.1f} "
              f"{r['rvol']:>5.2f} {r['vol_ratio_1d']:>5.2f} {r['prox_52w']:>7.3f} "
              f"{(r['ret_20d'] or 0) * 100:>6.1f}% {_fmt_krw(r['avg_value_20d']):>8}  "
              f"{_fmt_a_cond(r.get('a_cond')):<18} {r['index']}")


def _fmt_a_cond(c) -> str:
    if c is None:
        return "-"
    if c["sue"] is None:
        return {"NO_EVENT": "보고서 없음", "SUE_NA": "SUE 불가"}.get(c["status"], c["status"])
    mark = "✓" if c["a_flag"] else "·"
    tail = f" {c['age_td']}일전" if c["status"] != "STALE" else f" {c['age_td']}일(오래됨)"
    return f"{mark} SUE {c['sue']:+.1f}{tail}"


def print_a(rows: list, top: int):
    passed = [r for r in rows if r["passed"]]
    print(f"\n━━ A. 실적 서프라이즈 후 표류(PEAD)  (통과 {len(passed)} / SUE 산출 "
          f"{sum(1 for r in rows if r['sue'] is not None)})")
    print(f"   게이트: SUE≥{SUE_MIN}  진입 후 경과 1~{EVENT_WINDOW}거래일  "
          f"20일평균거래대금≥{_fmt_krw(LIQ_MIN_KRW)}  | 보유 {HOLD_A[0]}~{HOLD_A[1]}거래일")
    if not passed:
        print("   (통과 종목 없음)")
        return
    print("   이벤트일 = 잠정실적 공시일(없거나 월별 공시자면 정기보고서일, 표시 *). "
          "경과는 이벤트일 기준.")
    print("   ⚠ SUE는 정기보고서 확정치로 계산 — 이 목록은 보고서 접수 뒤에야 나온다. "
          "'지연'만큼 백테스트(A2) 진입보다 늦다.")
    print(f"   {'#':>2} {'코드':<7} {'종목명':<14} {'SUE':>5} {'분기':<7} {'영업이익':>9} "
          f"{'전년동기':>9} {'이벤트일':<11} {'보고서일':<10} {'경과':>4} {'지연':>4} {'52주근접':>7}")
    for i, r in enumerate(passed[:top], 1):
        prox = f"{r['prox_52w']:.3f}" if r["prox_52w"] is not None else "-"
        mark = " " if r["event_source"] == "PRELIM" else "*"
        print(f"   {i:>2} {r['code']:<7} {r['name'][:12]:<14} {r['sue']:>5.2f} {r['period']:<7} "
              f"{_fmt_krw(r['value']):>9} {_fmt_krw(r['prior_value']):>9} "
              f"{r['event_date'].isoformat()}{mark} {r['rcept_date'].isoformat():<10} "
              f"{r['elapsed_td']:>4} {r['report_lag_td']:>4} {prox:>7}")


def save_snapshot(payload: dict, as_of: date, run_id: str) -> str:
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    path = os.path.join(SNAPSHOT_DIR, f"ab-candidates-{as_of:%Y%m%d}-{run_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    return path


def main() -> int:
    ap = argparse.ArgumentParser(
        description="A(PEAD)·B(거래량 급증+52주 신고가) 후보 선정 — 미검증 초안",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=("A", "B", "AB"), default="AB")
    ap.add_argument("--date", help="기준일 YYYY-MM-DD (기본: 로컬 일봉의 최신 거래일)")
    ap.add_argument("--index", default=",".join(INDEXES),
                    help="쉼표 구분 (기본 KOSPI200,KOSDAQ150)")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--refresh-dart", action="store_true",
                    help="DART 캐시를 무시하고 다시 조회(정정보고서 반영)")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--allow-extended", action="store_true",
                    help="정규장 봉이 없는 날을 애프터마켓 포함 EXTENDED 일봉으로 채운다"
                         "(봉 기준 bars-1.0 밖 — 결과에 경고로 표시)")
    args = ap.parse_args()

    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    print(WARNING)
    today = date.today()
    end = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else today
    # 일봉 Parquet는 파일이 많아 읽기가 느리다 — 전 종목을 한 번만 읽고 기준일을 정한다
    all_bars = load_bars(None, end, allow_extended=args.allow_extended)
    calendar = sorted({b["date"] for bs in all_bars.values() for b in bs})
    if not calendar:
        print("  ❌ 로컬 일봉이 없다 — `python cvdobv.py daily-cache build` 먼저")
        return 2
    as_of = calendar[-1]
    if args.date and as_of != end:
        print(f"  ❌ {end} 일봉이 없다(휴장일이거나 미수집) — 직전 거래일 {as_of}")
        return 2
    warns = []
    if not args.date and (today - as_of).days > 3:
        warns.append(f"로컬 일봉 최신일이 {as_of} — {(today - as_of).days}일 전이다. "
                     f"주말·휴장일 뒤라면 정상이고, 아니면 `python cvdobv.py daily-cache build`로 갱신")

    indexes = [s.strip().upper() for s in args.index.split(",") if s.strip()]
    members, w = load_universe(as_of, indexes)
    warns += w
    if not args.allow_extended:
        from cvd_obv.bar_standard import unrestored_days
        pending = unrestored_days(start=as_of, end=end, securities=members)
        if pending:
            warns.append("정규장 복원 전인 날이 있다(기준 일봉에서 빠짐): "
                         + ", ".join(f"{d} {n}종목" for d, n in pending.items())
                         + " — 20:00 이후 `python cvdobv.py data-load daily`")
    bars = {sid: bs for sid, bs in all_bars.items() if sid in members}
    # 기준일 봉이 16:10 잠정 봉인가 — 거래량이 1분봉 합(근사)이라 RVOL이 확정과
    # 다를 수 있다(실측 최대 +20%). 결과를 쓰되 기록에 남겨 확정 결과와 구별한다.
    n_prov = sum(1 for bs in bars.values()
                 if bs and bs[-1]["date"] == as_of and bs[-1].get("provisional"))
    if n_prov:
        warns.append(f"기준일 {as_of} 일봉이 잠정이다({n_prov}종목, 16:10 1분봉 합 — 거래량 근사). "
                     f"RVOL이 확정과 다를 수 있다. 확정은 다음 거래일 08:30 "
                     f"`data-load daily` — 전향 기록은 확정 뒤 다시 돌린 결과로 남길 것")
    names = load_names()
    print(f"\n기준일 {as_of}  유니버스 {len(members)}종목({'+'.join(indexes)})  "
          f"일봉 보유 {len(bars)}종목  설계 {DESIGN_VERSION}")

    run_id = uuid.uuid4().hex[:8]
    from cvd_obv.bar_standard import BAR_STANDARD_VERSION
    payload = {"design_version": DESIGN_VERSION, "as_of": as_of, "run_id": run_id,
               "generated_at": datetime.now().isoformat(timespec="seconds"),
               "bar_standard": BAR_STANDARD_VERSION,
               # 기준일 봉이 잠정이면 True — 확정 결과와 섞지 않는다
               "provisional": bool(n_prov), "provisional_securities": n_prov,
               "universe": indexes, "warnings": warns,
               "design": {"B": {"rvol": f"{RVOL_RECENT}d/{RVOL_BASE}d", "rvol_min": RVOL_MIN,
                                "high_window": HIGH_WINDOW, "prox_min": PROX_MIN,
                                "liq_min_krw": LIQ_MIN_KRW, "hold_td": HOLD_B},
                          "A_and_B": {"definition": "B 통과 + 이미 접수된 최근 정기보고서 "
                                                    "SUE ≥ AB_SUE_MIN, 접수 후 ≤ AB_RECENCY_TD거래일",
                                      "sue_min": AB_SUE_MIN, "recency_td": AB_RECENCY_TD,
                                      "prereg": "docs/PREREGISTRATION_AB.md"},
                          "A": {"account": ACCOUNT_PREFIX, "sue_hist": SUE_HIST,
                                "sue_min_hist": SUE_MIN_HIST, "sue_min": SUE_MIN,
                                "event_window_td": EVENT_WINDOW,
                                "event_date": "잠정실적 공시일(분기말~정기보고서일 사이 최초, "
                                              "없거나 월별 공시자면 정기보고서 접수일)",
                                "entry": "이벤트일 다음 거래일(백테스트 기준) — 실거래 목록은 "
                                         "정기보고서 접수 후에 나온다",
                                "prereg": "docs/PREREGISTRATION_A2.md",
                                "liq_min_krw": LIQ_MIN_KRW, "hold_td": list(HOLD_A)}}}

    b_rows = a_rows = []
    if "B" in args.mode:
        b_rows = run_b(bars, members, names, as_of)
        mixed = sum(1 for r in b_rows if r["ext_days_recent"] > r["ext_days_base"]
                    * RVOL_RECENT / RVOL_BASE)
        if mixed:
            warns.append(f"B: {mixed}종목의 RVOL 최근 창에 애프터마켓 포함 일봉"
                         f"(EXTENDED)이 기준 창보다 많이 섞여 있다 — 거래량이 "
                         f"정규장만일 때보다 커서 RVOL이 부풀 수 있다. "
                         f"--allow-extended를 빼거나 `python cvdobv.py data-load "
                         f"daily`로 정규장 봉을 만들면 사라진다")
        nxt_mixed = nxt_boundary_rows(b_rows)
        if nxt_mixed:
            warns.append(f"B: {len(nxt_mixed)}종목의 RVOL 창({RVOL_RECENT}+{RVOL_BASE}일)에 "
                         f"NXT 메인마켓 포함 봉과 KRX만인 봉이 섞여 있다 — 거래량 정의 경계"
                         f"(NXT 포함은 2026-08-03부터)를 걸쳐 RVOL이 부풀거나 줄 수 있다: "
                         + ", ".join(f"{r['code']}({r['nxt_days_recent']}/{RVOL_RECENT}·"
                                     f"{r['nxt_days_base']}/{RVOL_BASE})"
                                     for r in nxt_mixed[:10])
                         + (" …" if len(nxt_mixed) > 10 else ""))
        payload["volume_definition"] = volume_definition(b_rows, nxt_mixed)
        try:
            warns += annotate_b_with_a(b_rows, as_of, calendar, args.refresh_dart)
        except Exception as e:  # DART 오류가 B 결과까지 날리지 않게
            warns.append(f"B의 A 조건 표시 실패: {e}")
        print_b(b_rows, args.top)
        watch = provisional_watch(b_rows)
        if watch["fragile"] or watch["possible"]:
            lo, hi = watch["rvol_range"]
            print(f"   ⚠ 잠정 봉 — RVOL {lo}~{hi} 구간은 확정 뒤 판정이 바뀔 수 있다"
                  f"(거래량 오차 ±{PROVISIONAL_RVOL_BAND:.0%})")
            for tag, rs in (("확정 뒤 빠질 수 있음", watch["fragile"]),
                            ("확정 뒤 들어올 수 있음", watch["possible"])):
                if rs:
                    print(f"     {tag}: " + ", ".join(
                        f"{r['code']} {r['name']}(RVOL {r['rvol']:.2f})" for r in rs))
        payload["B_provisional_watch"] = {
            "rvol_range": watch["rvol_range"],
            "fragile": [r["code"] for r in watch["fragile"]],
            "possible": [{"code": r["code"], "name": r["name"], "rvol": r["rvol"],
                          "prox_52w": r["prox_52w"]} for r in watch["possible"]]}
        payload["B"] = [r for r in b_rows if r["passed"]]
        payload["A_and_B"] = [r["code"] for r in payload["B"]
                              if (r.get("a_cond") or {}).get("a_flag")]
        payload["B_evaluated"] = len(b_rows)
    if "A" in args.mode:
        try:
            a_rows, w = run_a(bars, members, names, as_of, calendar, args.refresh_dart)
            warns += w
        except Exception as e:  # 네트워크·DART 오류가 B 결과까지 날리지 않게
            warns.append(f"A 실패: {e}")
            a_rows = []
        print_a(a_rows, args.top)
        payload["A"] = [r for r in a_rows if r["passed"]]
        payload["A_evaluated"] = sum(1 for r in a_rows if r["sue"] is not None)

    if "B" in args.mode:
        both = [r for r in b_rows if r["passed"] and (r.get("a_cond") or {}).get("a_flag")]
        print(f"\n━━ A∩B (B 통과 + A조건)  {len(both)}종목"
              + (": " + ", ".join(f"{r['code']} {r['name']}" for r in both) if both else ""))
        print("   백테스트(2017~2026, 사전등록 AB): 유니버스 대비 초과가 나머지 B보다 "
              "20일 +1.36%p·60일 +2.34%p(q=0.03)")
        print("   ⚠ 탐색 결과다 — 같은 데이터로 사전등록 6개를 돌린 중 하나. 지수 대비는 판정 불가. "
              "전향 기록으로 확인 전")

    for w in warns:
        print(f"  ⚠ {w}")
    if not args.no_save:
        print(f"\n  저장: {os.path.relpath(save_snapshot(payload, as_of, run_id), _BASE)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

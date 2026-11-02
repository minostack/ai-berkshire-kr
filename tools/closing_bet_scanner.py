#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
종가매수 후보 스캐너 (설계 closing-0.2, 2026-09-30)

대상: 코스피200 + 코스닥150 — 기준일 명단(`cvd_obv.universe.universe_on`, 유니버스 기준
      universe-1.0). 이력이 덮지 않는 최근 날짜는 `data/index_stocks.json`(/update-index)
입력: 최근 약 1개월 일봉 흐름 — 이평·신고가 계산용으로 최근 EVAL_BARS(61)봉을 쓴다.

유형 정의 — 아래 상수가 곧 정의다. 바꾸면 다른 설계이므로 DESIGN_VERSION을 올릴 것.

  공통 "장대 양봉"(기준봉)
      몸통 = 종가 − 시가 ≥ 시가 × LONG_BODY_PCT
      몸통 ≥ (고가 − 저가) × LONG_BODY_RATIO     (윗꼬리가 긴 봉 제외)
      거래량 ≥ 직전 20일 평균 × LONG_VOL_MULT
    "상한가" = 전일 종가 대비 +LIMIT_UP_PCT 이상 (C형에서 장대 양봉과 동급으로 인정)

  A형 — 장대 양봉 후 짧은 도지 2~3일
      기준봉 다음날부터 오늘까지 2~3개 봉, 각 봉은
        · |종가 − 시가| ≤ 기준봉 몸통 × A_DOJI_PCT          ("길이의 5% 이내")
        · 종가 ≥ 당일 MA5                                     (5일선 미이탈)
  B형 — 장대 양봉 후 4연속 음봉, 5일째 20일선 지지 양봉
      기준봉 +1~+4일: 모두 음봉(종가 < 시가)
      +4일 종가 ≤ MA5 × (1 + B_MA5_NEAR)                      (5일선 이탈·근접)
      +5일(=오늘): 양봉, 저가 ≤ MA20 × (1 + B_MA20_TOUCH), 종가 ≥ MA20
  C형 — 상한가/장대 양봉 후 8~12봉 기간 조정, 거래량 늘며 5일선 돌파 양봉
      기준봉과 오늘 사이 조정 봉 C_MIN~C_MAX개
      조정 구간 최저가 ≥ 기준봉 시가                          (기준봉을 다 지우지 않은 횡보)
      오늘: 양봉, 종가 > MA5, (전일 종가 ≤ 전일 MA5 또는 시가 ≤ MA5)  (5일선 돌파)
            거래량 ≥ 조정 구간 평균 × C_REVIVE_MULT
  (4) A·B·C 공통 — 조정 구간 평균 거래량 ≤ 기준봉 거래량 × SHRINK_MAX
                    이고 조정 구간의 어떤 봉도 기준봉 거래량을 넘지 않는다
                    조정 구간에 거래량 0인 봉(거래정지)이 있으면 불충족   ← 0.2
  D형 — 거래대금이 실린 신고가
      오늘 고가 ≥ 직전 N−1봉 최고가, 종가 ≥ 직전 N−1봉 최고 종가  (N = 5, 20, 60)
      오늘 거래대금 ≥ 직전 20일 평균 × D_VALUE_MULT, 그리고 ≥ D_VALUE_MIN
      통과 기준 = --d-period(기본 60, 60일 신고가면 5·20일도 자동 충족)

  공통 유동성: 직전 20일 평균 거래대금 ≥ LIQ_MIN_KRW

설계 이력
  0.1 → 0.2 (2026-09-30, 검토 보고서 `tools/closing_bet_scanner_review.md` 반영)
    · 2026-09-14 이후 키움 일봉(EXTENDED)의 종가는 애프터마켓 최종가(20:00 확정)다.
      15:40 종가매수 판단 시점에는 알 수 없는 값이므로, 1분봉 정규장(09:00~15:30)으로
      정규장 OHLCV를 복원해 쓴다. 복원할 수 없으면 그 종목은 판정 불가(기본).
    · 거래정지 봉(거래량 0)이 A형 "도지 + 거래량 감소"를 자동 통과하던 것을 막았다.
    · 같은 봉의 여러 리비전 중 최신 것을 고른다(`read_arrow(latest_by=…)`).
    · 거래대금 추정치(종가×거래량 등)를 실측과 구분해 표시한다.
    · 중간 거래일 누락·봉 부족·판정 오류를 "후보 없음"과 구분해 집계한다.
    패턴 수치 상수는 0.1과 같다.
  0.2 → 0.3 (2026-10-03, 봉 기준 `bars-1.0` — `src/cvd_obv/bar_standard.py`)
    · 정규장 복원을 저장소의 단일 규칙(`daily-cache regular-from-minute`)으로 통일했다.
      이전의 즉석 1분봉 합산 복원은 거래량이 공식값보다 작게 나왔다(9/1~9/11 대조
      하위 1% 77%). 늦게 찍힌 종가 체결(15:32 OTHER) 처리는 저장 규칙으로 옮겼다.
    · 거래대금이 없는 복원 봉은 같은 날 EXTENDED 공식 거래대금 × 거래량 비율로
      추정한다(이전 종가×거래량) — A·B 선정기와 같은 규칙.
    · 당일 봉 성격은 받은 시각으로 정한다: 15:35~16:00 정규장(이전 15:30부터).
    · 사전 점검이 정규장 복원 명령까지 낸다.
    패턴 수치 상수는 0.1과 같다.
  0.3 → 0.4 (2026-10-05)
    · 스캐너의 데이터 적재(--check/--ensure-data, 적재 명령 생성·실행)를 없앴다.
      적재는 `data-load daily` 하나가 맡는다. 스캐너는 [데이터 기준] 알림만 낸다.
    판정 규칙·패턴 상수는 0.3과 같다.

⚠ 위 수치는 차트 매매 관행을 옮긴 "초안"이며 수익성이 검증되지 않았다
  (1년 탐색 백테스트: `reports/종가매수-ABC-백테스트-*.md`). 매매 판단의 단독
  근거로 쓰지 말 것.

데이터 소스
  --source local  (기본) 로컬 `silver.daily_bars`의 기준 일봉(정규장·수정주가).
                  16:10 `data-load provisional`(잠정) · 익일 08:30 `data-load daily`(확정)로 갱신.
  --source kis    지난 날 = 로컬 기준 일봉, 로컬에 없는 날(보통 오늘)만 KIS FHKST03010100
                  조회(.env KIS_APP_KEY/SECRET, 종목당 1회). 당일 봉은 15:35~16:00에 받은 것만
                  정규장으로 본다 — 이 시간대 실행이 종가매수 판단에 가장 정확하다.
                  당일 거래량은 KRX만(로컬 9/14 이후 봉은 NXT 메인마켓 포함)이라 작게 잡힌다.
                  직전 거래일 종가가 로컬과 0.5% 넘게 다르면 수정주가 불일치로 경고한다.
  --source kiwoom 같은 방식으로 키움 ka10081 조회(대체 원천, KIWOOM 키 필요)
                  --live: 장중 미완료 당일 봉 포함(15:20 전후 참고용, 거래량은 마감 전 값)
  --allow-extended 정규장 복원이 안 된 EXTENDED 봉도 판정에 쓴다(애프터마켓 가격 섞임 —
                  결과에 표시된다)

사용법
    python tools/closing_bet_scanner.py
    python tools/closing_bet_scanner.py --source kis --live
    python tools/closing_bet_scanner.py --date 2026-09-22 --types A,B,C
    python tools/closing_bet_scanner.py --d-period 20 --no-save

결과: 콘솔 표 + `reports/종가매수-scan-{YYYYMMDD}-{HHMMSS}.md` (실행마다 새 파일)
종료 코드: 0 정상, 2 데이터 없음/설정 오류, 3 판정 가능한 종목이 하나도 없음

데이터 적재(0.4~)
  이 스캐너는 적재하지 않는다. 일봉·1분봉·정규장 복원은 적재 프로세스
  16:10 `data-load provisional`(잠정 일봉) · 익일 08:30 `data-load daily`(정규장 확정)가 맡는다.
  스캔할 때는 [데이터 기준] 알림만 낸다 — 봉 기준(bars-1.0), 스캔 기준일, 시장 최신
  완료 거래일(KIS 005930 1회 조회 — 실패 시 키움, --no-probe면 평일 근사), 기준일 봉 없는 종목,
  정규장 복원 전 봉. 로컬이 뒤처졌으면 적재 명령을 안내한다.
"""

import argparse
import json
import os
import sys
import tempfile
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))
sys.path.insert(0, os.path.join(_BASE, "tools"))

DESIGN_VERSION = "closing-0.4"

from cvd_obv.bar_standard import (  # noqa: E402
    AFTERMARKET_START, FETCH_EXTENDED, FETCH_REGULAR, classify_fetched_daily, is_provisional,
    read_standard_daily,
)

# ── 공통 ──
LIQ_DAYS = 20
LIQ_MIN_KRW = 1_000_000_000
LONG_BODY_PCT = 0.05        # 몸통 ≥ 시가의 5%
LONG_BODY_RATIO = 0.6       # 몸통 ≥ 전체 길이의 60%
LONG_VOL_MULT = 1.5         # 거래량 ≥ 직전 20일 평균의 1.5배
LIMIT_UP_PCT = 0.29         # 상한가(30%) — 호가 단위 반올림 여유
SHRINK_MAX = 0.6            # 조정 구간 평균 거래량 ≤ 기준봉의 60%

# ── A ──
A_DAYS = (2, 3)
A_DOJI_PCT = 0.05

# ── B ──
B_BEAR_DAYS = 4
B_MA5_NEAR = 0.01           # +4일 종가가 MA5의 +1% 이내(이탈 포함)
B_MA20_TOUCH = 0.02         # 오늘 저가가 MA20의 +2% 이내까지 내려와야 "지지 확인"

# ── C ──
C_MIN, C_MAX = 8, 12
C_REVIVE_MULT = 1.5

# ── D ──
D_PERIODS = (5, 20, 60)
D_VALUE_MULT = 1.5
D_VALUE_MIN = 10_000_000_000   # 100억 원

TYPES = ("A", "B", "C", "D")

#: 판정에 쓰는 최근 봉 수. 가장 긴 조건(D 60일 신고가 = 오늘 + 직전 59봉,
#: 20일 평균 거래대금)을 덮는다. 누락·EXTENDED 검사도 이 창에만 한다.
EVAL_BARS = 61


def min_bars(t: str, d_period: int = 60) -> int:
    """유형별로 정의를 온전히 적용하는 데 필요한 최소 봉 수."""
    if t == "A":
        return LIQ_DAYS + max(A_DAYS) + 1          # 기준봉 앞 20봉 + 기준봉 + 조정 3
    if t == "B":
        return LIQ_DAYS + B_BEAR_DAYS + 2          # 기준봉 + 음봉 4 + 오늘
    if t == "C":
        return LIQ_DAYS + C_MAX + 2                # 기준봉 + 조정 12 + 오늘
    return max(d_period, LIQ_DAYS + 1)


KST = timezone(timedelta(hours=9))
REGULAR_OPEN_KST = time(9, 0)
REGULAR_FINAL_KST = time(15, 35)       # 정규장 확정(15:30 종가 + 여유 5분, 봉 기준 bars-1.0)

#: bar["scope"] 값
SCOPE_REGULAR = "REGULAR"              # 정규장 봉(원천·1분봉 복원·15:35~16:00 수집)
SCOPE_LIVE = "LIVE"                    # 장중 미완료 당일 봉
SCOPE_EXTENDED = "EXTENDED"            # 애프터마켓 포함, 복원 못 함

LOAD_CAL_DAYS = 150         # EVAL_BARS를 넉넉히 덮는 캘린더일
REPORT_DIR = os.path.join(_BASE, "reports")

WARNING = ("⚠ 차트 관행을 옮긴 초안 정의(수익성 미검증). 후보 목록일 뿐 매매 판단의 "
           "단독 근거로 쓰지 말 것.")

# 판정 제외 사유
EX_NO_TODAY = "기준일 봉 없음"
EX_SHORT = "봉 부족"
EX_GAP = "중간 거래일 누락"
EX_EXTENDED = "애프터마켓 봉(정규장 복원 불가)"
EX_ERROR = "판정 오류"


# ─────────────────────────────────────────────────────────────
# 지표
# ─────────────────────────────────────────────────────────────

def sma_at(values: list, i: int, n: int) -> Optional[float]:
    """values[i]를 포함한 n개 단순평균. 봉이 모자라면 None."""
    if i + 1 < n:
        return None
    return sum(values[i + 1 - n:i + 1]) / n


def avg_before(values: list, i: int, n: int) -> Optional[float]:
    """values[i] 직전 n개 평균(i 제외)."""
    if i < n:
        return None
    return sum(values[i - n:i]) / n


def is_long_bull(bars: list, i: int, volumes: list) -> bool:
    b = bars[i]
    body = b["close"] - b["open"]
    rng = b["high"] - b["low"]
    if body <= 0 or rng <= 0:
        return False
    if body < b["open"] * LONG_BODY_PCT or body < rng * LONG_BODY_RATIO:
        return False
    vavg = avg_before(volumes, i, LIQ_DAYS)
    return vavg is not None and vavg > 0 and b["volume"] >= vavg * LONG_VOL_MULT


def is_limit_up(bars: list, i: int) -> bool:
    return i > 0 and bars[i]["close"] >= bars[i - 1]["close"] * (1 + LIMIT_UP_PCT)


def volume_shrunk(bars: list, base: int, seg: range) -> tuple:
    """(통과여부, 조정평균/기준봉 비율) — 공통 조건 (4).

    조정 구간에 거래량 0인 봉(거래정지)이 있으면 불충족이다. 정지일 봉은
    시가=종가·거래량 0이라 "짧은 도지 + 거래량 감소"를 자동으로 만족해 버린다.
    """
    vb = bars[base]["volume"]
    vols = [bars[k]["volume"] for k in seg]
    if not vols or vb <= 0 or min(vols) <= 0:
        return False, None
    ratio = sum(vols) / len(vols) / vb
    return ratio <= SHRINK_MAX and max(vols) <= vb, ratio


# ─────────────────────────────────────────────────────────────
# 유형 판정 — bars: 날짜 오름차순, 마지막 봉이 기준일(오늘)
# ─────────────────────────────────────────────────────────────

def check_a(bars, closes, volumes) -> Optional[dict]:
    t = len(bars) - 1
    for n in A_DAYS:
        base = t - n
        if base < LIQ_DAYS or not is_long_bull(bars, base, volumes):
            continue
        body = bars[base]["close"] - bars[base]["open"]
        seg = range(base + 1, t + 1)
        ok = True
        for k in seg:
            ma5 = sma_at(closes, k, 5)
            if abs(bars[k]["close"] - bars[k]["open"]) > body * A_DOJI_PCT or \
                    ma5 is None or bars[k]["close"] < ma5:
                ok = False
                break
        if not ok:
            continue
        vok, vr = volume_shrunk(bars, base, seg)
        if vok:
            return {"base": bars[base]["date"], "adj_days": n, "vol_ratio": vr,
                    "base_body_pct": body / bars[base]["open"]}
    return None


def check_b(bars, closes, volumes) -> Optional[dict]:
    t = len(bars) - 1
    base = t - (B_BEAR_DAYS + 1)
    if base < LIQ_DAYS or not is_long_bull(bars, base, volumes):
        return None
    seg = range(base + 1, base + 1 + B_BEAR_DAYS)
    if any(bars[k]["close"] >= bars[k]["open"] for k in seg):
        return None
    last_bear = seg[-1]
    ma5 = sma_at(closes, last_bear, 5)
    if ma5 is None or bars[last_bear]["close"] > ma5 * (1 + B_MA5_NEAR):
        return None
    today = bars[t]
    ma20 = sma_at(closes, t, 20)
    if ma20 is None or today["close"] <= today["open"]:
        return None
    if not (today["low"] <= ma20 * (1 + B_MA20_TOUCH) and today["close"] >= ma20):
        return None
    vok, vr = volume_shrunk(bars, base, seg)
    if not vok:
        return None
    return {"base": bars[base]["date"], "vol_ratio": vr,
            "low_vs_ma20": today["low"] / ma20 - 1}


def check_c(bars, closes, volumes) -> Optional[dict]:
    t = len(bars) - 1
    today = bars[t]
    ma5 = sma_at(closes, t, 5)
    ma5_prev = sma_at(closes, t - 1, 5)
    if ma5 is None or ma5_prev is None or today["close"] <= today["open"]:
        return None
    if today["close"] <= ma5:
        return None
    if not (closes[t - 1] <= ma5_prev or today["open"] <= ma5):
        return None
    for n in range(C_MIN, C_MAX + 1):          # 가장 가까운 기준봉 우선
        base = t - n - 1
        if base < LIQ_DAYS:
            break
        limit_up = is_limit_up(bars, base)
        if not (limit_up or is_long_bull(bars, base, volumes)):
            continue
        seg = range(base + 1, t)
        if min(bars[k]["low"] for k in seg) < bars[base]["open"]:
            continue
        vok, vr = volume_shrunk(bars, base, seg)
        if not vok:
            continue
        seg_avg = sum(volumes[k] for k in seg) / len(seg)
        if seg_avg <= 0:          # volume_shrunk가 막지만 나눗셈 전에 한 번 더
            continue
        if today["volume"] < seg_avg * C_REVIVE_MULT:
            continue
        return {"base": bars[base]["date"], "limit_up": limit_up, "adj_days": n,
                "vol_ratio": vr, "revive": today["volume"] / seg_avg}
    return None


def check_d(bars, d_period: int) -> Optional[dict]:
    t = len(bars) - 1
    values = [b["value"] for b in bars]
    vavg = avg_before(values, t, LIQ_DAYS)
    if vavg is None or vavg <= 0:
        return None
    hits = []
    for n in D_PERIODS:
        if t < n - 1:
            continue
        prior = bars[t - (n - 1):t]
        if bars[t]["high"] >= max(b["high"] for b in prior) and \
                bars[t]["close"] >= max(b["close"] for b in prior):
            hits.append(n)
    if d_period not in hits:
        return None
    mult = values[t] / vavg
    if mult < D_VALUE_MULT or values[t] < D_VALUE_MIN:
        return None
    return {"new_high": hits, "value": values[t], "value_mult": mult,
            "value_est": bool(bars[t].get("value_est"))}


def evaluate(bars: list, d_period: int) -> dict:
    """{liq, A, B, C, D, na} — na는 봉이 모자라 정의를 온전히 적용할 수 없는 유형."""
    bars = bars[-EVAL_BARS:]
    closes = [b["close"] for b in bars]
    volumes = [b["volume"] for b in bars]
    t = len(bars) - 1
    liq = avg_before([b["value"] for b in bars], t, LIQ_DAYS)
    res = {"liq": liq, "na": [x for x in TYPES if len(bars) < min_bars(x, d_period)]}
    if liq is None or liq < LIQ_MIN_KRW:
        return res
    res["A"] = check_a(bars, closes, volumes)
    res["B"] = check_b(bars, closes, volumes)
    res["C"] = check_c(bars, closes, volumes)
    res["D"] = check_d(bars, d_period)
    return res


def eligibility(bars: list, as_of: date, cal_index: dict,
                allow_extended: bool) -> Optional[str]:
    """판정 불가 사유(없으면 None). 스캐너와 백테스트가 같은 규칙을 쓴다.

    cal_index: {거래일: 순번} — 시장 거래일 달력. 창 안의 봉 수가 달력상 거래일
    수와 다르면 중간이 빠진 것이다(달력 날짜 +1 검사는 휴장일을 못 거른다).
    """
    if not bars or bars[-1]["date"] != as_of:
        return EX_NO_TODAY
    if len(bars) < LIQ_DAYS + 2:
        return EX_SHORT
    window = bars[-EVAL_BARS:]
    i0, i1 = cal_index.get(window[0]["date"]), cal_index.get(window[-1]["date"])
    if i0 is None or i1 is None or i1 - i0 + 1 != len(window):
        return EX_GAP
    if not allow_extended and any(b["scope"] == SCOPE_EXTENDED for b in window):
        return EX_EXTENDED
    return None


def judge(bars: list, as_of: date, cal_index: dict, d_period: int,
          allow_extended: bool) -> dict:
    """{"status": None|사유, "res": evaluate 결과 또는 None, "error": 메시지}"""
    why = eligibility(bars, as_of, cal_index, allow_extended)
    if why:
        return {"status": why, "res": None}
    try:
        return {"status": None, "res": evaluate(bars, d_period)}
    except Exception as e:          # 한 종목 오류가 전체 스캔을 멈추지 않게
        return {"status": EX_ERROR, "res": None, "error": f"{type(e).__name__}: {e}"}


# ─────────────────────────────────────────────────────────────
# 데이터
# ─────────────────────────────────────────────────────────────

def load_universe(day: Optional[date] = None, warns: Optional[list] = None) -> dict:
    """{code: (name, 지수)} — 기준일 명단(유니버스 기준 universe-1.0,
    `cvd_obv.universe.universe_on`). `--date`로 과거를 보면 그날의 구성종목이다
    (이전에는 오늘 명단을 과거에 그대로 써서 생존편향이 있었다). 이력 밖 경고는
    `warns`에 덧붙인다."""
    from cvd_obv.universe import universe_on
    u = universe_on(day)
    if warns is not None:
        warns.extend(u.warnings)
    return {m.code: (m.name, m.index_id) for m in u.members()}


def pick_daily(rows: list) -> dict:
    """기준 일봉 행 → {(sid, 날짜): 봉}. 같은 날 REGULAR가 있으면 그것을 쓴다.
    rows는 이미 논리 키별 최신 리비전 하나씩이어야 한다(순서 무관).
    `traded_value_est`가 없으면 거래대금이 비었을 때 종가×거래량으로 추정한다."""
    picked = {}
    for r in rows:
        key = (r["security_id"], r["trading_date"])
        scope = r["price_scope"]
        prev = picked.get(key)
        if prev is not None and prev["scope"] == SCOPE_REGULAR:
            continue
        if prev is not None and scope != "REGULAR":
            continue            # EXTENDED끼리는 먼저 온 것을 유지(리비전은 이미 정리됨)
        c, v, tv = float(r["close"]), int(r["volume"]), r["traded_value"]
        est = r.get("traded_value_est", tv is None)
        picked[key] = {"date": r["trading_date"], "open": float(r["open"]),
                       "high": float(r["high"]), "low": float(r["low"]), "close": c,
                       "volume": v,
                       "value": float(tv) if tv is not None else c * v,
                       "value_est": bool(est),
                       "scope": SCOPE_REGULAR if scope == "REGULAR" else SCOPE_EXTENDED,
                       # 16:10 잠정 일봉(1분봉 합, 거래량 근사) — 익일 확정 복원이 덮는다
                       "provisional": is_provisional(r.get("source"))}
    return picked


_DAILY_COLS = ["security_id", "trading_date", "open", "high", "low", "close",
               "volume", "traded_value", "price_scope", "adjustment_type",
               "quality_status", "source"]


def _read_standard(sids, start: date, end: date) -> list:
    """로컬 기준 일봉 행(봉 기준 bars-1.0). EXTENDED는 정규장 봉이 없는 날만 —
    판정에서 빼되 사전 점검이 "복원 불가"로 셀 수 있게 남긴다."""
    import pyarrow.dataset as ds

    flt = ((ds.field("trading_date") >= start) & (ds.field("trading_date") <= end)
           & ds.field("security_id").isin(sorted(sids)))
    return read_standard_daily(columns=_DAILY_COLS, filters=flt, allow_extended=True,
                               drop_failed=True, fill_traded_value=True).to_pylist()


def restore_regular(picked: dict) -> dict:
    """EXTENDED 봉을 로컬 기준 일봉(정규장 복원본)으로 바꾼다. 없으면 그대로 둔다.

    복원 규칙은 `cvdobv.py daily-cache regular-from-minute` 하나뿐이다 — 여기서
    다시 계산하지 않는다."""
    ext = [k for k, b in picked.items() if b["scope"] == SCOPE_EXTENDED]
    if ext:
        days = [d for _, d in ext]
        stored = pick_daily(_read_standard({s for s, _ in ext}, min(days), max(days)))
        for k in ext:
            if k in stored and stored[k]["scope"] == SCOPE_REGULAR:
                picked[k] = stored[k]
    return picked


def _group(picked: dict) -> dict:
    out = {}
    for (sid, _), bar in sorted(picked.items()):
        out.setdefault(sid.split(":", 1)[1], []).append(bar)
    return out


def load_local(codes, end: date, start: Optional[date] = None) -> dict:
    """{code: [bar…]} — 로컬 기준 일봉(정규장·수정주가, 최신 리비전, 품질 FAILED 제외).
    정규장 봉이 없는 날은 EXTENDED로 남아 판정에서 빠진다."""
    start = start or end - timedelta(days=LOAD_CAL_DAYS)
    return _group(pick_daily(_read_standard({f"KRX:{c}" for c in codes}, start, end)))


def fetched_scope(bar_date: date, fetched_at: datetime, live: bool) -> str:
    """API로 받은 일봉 한 개의 성격. 순수 함수(테스트 대상).

    봉 기준 bars-1.0 `classify_fetched_daily`를 따른다: 확정 뒤에 받은 과거 봉은
    그날의 scope, 당일 봉은 15:35~16:00에 받았으면 정규장 값이다. 확정 전 값은
    `--live`면 LIVE(장중 참고), 아니면 판정에서 빼는 EXTENDED로 둔다.
    """
    got = classify_fetched_daily(bar_date, fetched_at)
    if got == FETCH_REGULAR:
        return SCOPE_REGULAR
    if got == FETCH_EXTENDED:
        return SCOPE_EXTENDED
    if live and bar_date == fetched_at.date():
        return SCOPE_LIVE
    return SCOPE_EXTENDED


kiwoom_scope = fetched_scope        # 0.4 이름(호환)

#: API 조회 원천 — 이름, 필요한 키
API_SOURCES = {"kis": ("KIS", ("KIS_APP_KEY", "KIS_APP_SECRET")),
               "kiwoom": ("키움", ("KIWOOM_APP_KEY", "KIWOOM_APP_SECRET"))}
#: 당일 봉을 받을 때 함께 받는 캘린더일(직전 거래일 종가 대조용)
API_RECENT_DAYS = 10
#: 직전 거래일 종가가 로컬과 이만큼 넘게 다르면 수정주가 불일치로 알린다
ADJ_MISMATCH = 0.005


def _fetch_kis(code: str, end: date, src=[]) -> list:
    from cvd_obv.ingestion.kis_daily import FLAG_KIS_UNSETTLED, KisDailySource
    if not src:
        src.append(KisDailySource(max_pages=1))
    res = src[0].fetch_security(code, start=end - timedelta(days=API_RECENT_DAYS), end=end)
    # unsettled: 다음 거래일 09:00 전에 받은 최근 거래일 — 가격이 애프터마켓 값일 수 있다
    return [{"date": r["trading_date"], "open": float(r["open"]), "high": float(r["high"]),
             "low": float(r["low"]), "close": float(r["close"]), "volume": int(r["volume"]),
             "traded_value": None if r["traded_value"] is None else float(r["traded_value"]),
             "unsettled": FLAG_KIS_UNSETTLED in r["quality_flags"]}
            for r in res.rows]


def _fetch_kiwoom(code: str, end: date) -> list:
    import daily_scanner as ds_
    as_of = (datetime.now() if end == date.today()
             else datetime.combine(end, datetime.max.time()))
    rows = ds_.kiwoom_daily_bars(code, days=API_RECENT_DAYS, as_of=as_of,
                                 include_incomplete=True)
    return [dict(r, date=date.fromisoformat(r["date"])) for r in rows]


def merge_api(local: dict, api_rows: dict, now: datetime, live: bool) -> tuple:
    """로컬 기준 일봉 위에 API 봉을 얹는다. 순수 함수(테스트 대상).

    - 로컬에 정규장(REGULAR) 봉이 있는 날은 로컬을 쓴다 — 지난 날의 기준은 로컬이다
      (9/14 이후 API 일봉은 거래량에 애프터마켓이 섞여 EXTENDED)
    - 로컬에 없는 날(보통 오늘)만 API 봉을 쓴다. 성격은 `fetched_scope`
    - `--live`가 아니면 15:35 전 당일 봉(장중 미완료)은 버린다
    - 직전 거래일 종가가 로컬과 `ADJ_MISMATCH` 넘게 다르면 코드를 돌려준다(수정주가 의심).
      API 봉이 미정산(`unsettled` — KIS 최근 거래일을 다음 거래일 09:00 전에 받음)이면
      종가가 애프터마켓 값이라 대조하지 않는다(주말 실행 시 전 종목 오경보 방지)

    반환: (picked {(sid, date): bar}, mismatch [code…])
    """
    picked, mismatch = dict(local), []
    for code, rows in api_rows.items():
        sid = f"KRX:{code}"
        prev = max((r["date"] for r in rows if r["date"] < now.date()), default=None)
        for r in rows:
            d = r["date"]
            key = (sid, d)
            mine = local.get(key)
            if mine is not None and mine["scope"] == SCOPE_REGULAR:
                if d == prev and mine["close"] and not r.get("unsettled") and                         abs(r["close"] / mine["close"] - 1) > ADJ_MISMATCH:
                    mismatch.append(code)
                continue
            if not live and d == now.date() and now.time() < REGULAR_FINAL_KST:
                continue
            tv = r.get("traded_value")
            picked[key] = {"date": d, "open": r["open"], "high": r["high"], "low": r["low"],
                           "close": r["close"], "volume": r["volume"],
                           "value": tv if tv is not None else r["close"] * r["volume"],
                           "value_est": tv is None, "scope": fetched_scope(d, now, live),
                           "api": True}
    return picked, sorted(set(mismatch))


def load_api(codes, end: date, live: bool, source: str) -> tuple:
    """지난 날 = 로컬 기준 일봉, 오늘 = API(KIS 기본 · 키움 대체) 일봉.
    반환: ({code: [bar…]}, 수정주가 불일치 의심 코드 목록)"""
    fetch = _fetch_kis if source == "kis" else _fetch_kiwoom
    label = API_SOURCES[source][0]
    now = datetime.now()
    start = end - timedelta(days=LOAD_CAL_DAYS)
    local = pick_daily(_read_standard({f"KRX:{c}" for c in codes}, start, end))
    api_rows, failed = {}, 0
    for i, code in enumerate(codes, 1):
        try:
            api_rows[code] = fetch(code, end)
        except Exception as e:      # 한 종목 실패는 로컬만으로 둔다(기준일 봉 없음으로 집계)
            failed += 1
            if failed <= 3:
                print(f"  ⚠ {label} 일봉 {code} 조회 실패({type(e).__name__}: {e})",
                      file=sys.stderr)
        if i % 50 == 0:
            print(f"  … {label} 일봉 {i}/{len(codes)}", file=sys.stderr)
    if failed:
        print(f"  ⚠ {label} 일봉 조회 실패 {failed}종목", file=sys.stderr)
    picked, mismatch = merge_api(local, api_rows, now, live)
    return _group(restore_regular(picked)), mismatch


def market_calendar(bars: dict) -> list:
    """로드한 종목들의 봉 날짜 합집합 — 수백 종목이면 시장 거래일과 같다."""
    return sorted({b["date"] for bs in bars.values() for b in bs})


# 데이터 기준 알림 — 적재는 하지 않는다(적재는 `cvdobv.py data-load daily`의 일)
# ─────────────────────────────────────────────────────────────

PROBE_CODE = "005930"
LOAD_COMMAND = "python cvdobv.py data-load daily"


def _prev_weekday(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _probe_kis(now: datetime) -> list:
    from cvd_obv.ingestion.kis_daily import KisDailySource
    res = KisDailySource(max_pages=1).fetch_security(
        PROBE_CODE, start=now.date() - timedelta(days=30), end=now.date())
    if not res.rows:
        raise RuntimeError(res.message or "KIS 일봉 없음")
    return [r["trading_date"] for r in res.rows]


def _probe_kiwoom(now: datetime) -> list:
    import daily_scanner as ds_
    rows = ds_.kiwoom_daily_bars(PROBE_CODE, days=20, as_of=now, include_incomplete=True)
    return [date.fromisoformat(r["date"]) for r in rows]


#: 거래일 조회 원천 — (이름, 필요한 키, 조회 함수). 적재 원천(market_source)과 같은 순서
_PROBES = {"kis": ("KIS", ("KIS_APP_KEY", "KIS_APP_SECRET"), _probe_kis),
           "kiwoom": ("키움", ("KIWOOM_APP_KEY", "KIWOOM_APP_SECRET"), _probe_kiwoom)}


def _probe_order() -> list:
    """적재 원천 선택(`CVD_MARKET_SOURCE`, 기본 kis)을 먼저, 나머지를 대체로 쓴다."""
    first = (os.environ.get("CVD_MARKET_SOURCE") or "kis").strip().lower()
    return [first] + [k for k in _PROBES if k != first] if first in _PROBES else list(_PROBES)


def reference_days(now: datetime) -> tuple:
    """(완료된 거래일 오름차순 목록, 출처). KIS(기본) 또는 키움 기준 종목 일봉 1회 조회로
    실제 거래일을 얻는다(휴장일 반영). 키가 없거나 모두 실패하면 평일 근사(휴장일 미반영)."""
    for key in _probe_order():
        label, env_keys, probe = _PROBES[key]
        if not all(os.environ.get(k) for k in env_keys):
            continue
        try:
            # 거래일 여부만 본다 — 장중 당일 봉은 아직 거래일이 끝나지 않았으므로 빼고,
            # 정규장(15:35)이 끝났으면 그날은 이미 정규장 거래일이다
            days = sorted(set(probe(now)))
            if days and days[-1] == now.date() and now.time() < REGULAR_FINAL_KST:
                days = days[:-1]
            if days:
                return days, f"{label} {PROBE_CODE} 일봉"
        except Exception as e:      # 조회 실패 시 다음 원천, 끝내 실패하면 근사
            print(f"  ⚠ 기준 거래일 {label} 조회 실패({type(e).__name__}: {e})",
                  file=sys.stderr)
    return reference_days_approx(now)


def reference_days_approx(now: datetime) -> tuple:
    """평일 근사 거래일 목록(휴장일 미반영)."""
    d = now.date()
    if d.weekday() >= 5 or now.time() < REGULAR_FINAL_KST:
        d = _prev_weekday(d)
    days = [d]
    for _ in range(9):
        days.insert(0, _prev_weekday(days[0]))
    return days, "평일 근사(휴장일 미반영 — 추정)"


def data_basis(codes, bars: dict, as_of: date, reference: Optional[date] = None,
               ref_src: str = "") -> dict:
    """스캔에 쓴 데이터의 기준을 요약한다. 순수 함수(테스트 대상) — 적재 판단·명령은 없다.

    - 기준일(as_of): 이번 스캔이 판정한 날짜(로드한 일봉의 마지막 거래일)
    - 시장 최신 완료 거래일(reference): 키움 조회 또는 평일 근사
    - 기준일 봉이 없는 종목, 판정 창 안의 정규장 복원 전(EXTENDED) 봉 날짜별 종목 수
    """
    missing = [c for c in codes if not bars.get(c) or bars[c][-1]["date"] != as_of]
    unrestored = {}
    for c in codes:
        for b in (bars.get(c) or [])[-EVAL_BARS:]:
            if b["scope"] == SCOPE_EXTENDED:
                unrestored[b["date"]] = unrestored.get(b["date"], 0) + 1
    behind = reference is not None and as_of < reference
    scopes = Counter(b["scope"] for c in codes for b in (bars.get(c) or [])[-1:])
    provisional = {}
    for c in codes:
        for b in (bars.get(c) or [])[-EVAL_BARS:]:
            if b.get("provisional"):
                provisional[b["date"]] = provisional.get(b["date"], 0) + 1
    return {"as_of": as_of, "reference": reference, "reference_src": ref_src,
            "behind": behind, "missing_today": missing,
            "unrestored": dict(sorted(unrestored.items())),
            "today_scope": dict(scopes), "universe": len(codes),
            "provisional": dict(sorted(provisional.items())),
            "needs_load": behind or bool(unrestored) or bool(missing)}


def basis_lines(b: dict, source: str) -> list:
    """데이터 기준 알림 문구(콘솔·리포트 공용)."""
    L = ["봉 기준 bars-1.0 — 일봉 = 정규장(09:00~15:30)·수정주가. 2026-09-14 이후는 "
         "1분봉 정규장 복원본(`daily-cache regular-from-minute`), 애프터마켓 포함 봉은 판정에서 제외"]
    ref = (f"{b['reference']} ({b['reference_src']})" if b["reference"]
           else "확인 안 함(--date 지정)")
    L.append(f"스캔 기준일 {b['as_of']} · 시장 최신 완료 거래일 {ref}")
    if source.split()[0] in API_SOURCES:
        label = API_SOURCES[source.split()[0]][0]
        L.append(f"당일 봉: {label} API 조회값(지난 날은 로컬 기준 일봉) — 15:35~16:00 조회분만 "
                 "정규장 확정값, 그 밖의 시각은 미확정(--live는 장중 참고)")
        L.append("⚠ API 당일 봉 거래량·거래대금은 KRX만이다 — 로컬 9/14 이후 봉은 NXT 메인마켓을 "
                 "더한 값이라 당일 거래량이 상대적으로 작게 잡힌다(C형 거래량 증가·D형 거래대금이 "
                 "덜 걸리는 쪽)")
    if b["behind"]:
        L.append(f"⚠ 로컬 일봉이 {b['reference']}보다 뒤처졌다 — {b['as_of']} 기준으로 스캔했다")
    n = b["universe"]
    if b["missing_today"]:
        L.append(f"⚠ 기준일 봉 없는 종목 {len(b['missing_today'])}/{n} — 판정 불가로 집계")
    if b["unrestored"]:
        per = ", ".join(f"{d} {k}종목" for d, k in b["unrestored"].items())
        L.append(f"⚠ 정규장 복원 전 봉(EXTENDED) — {per}")
    if b.get("provisional"):
        per = ", ".join(f"{d} {k}종목" for d, k in b["provisional"].items())
        L.append(f"⚠ 잠정 일봉(16:10 1분봉 합, 거래량 근사) — {per}. 가격은 정규장 값이지만 거래량이 "
                 "공식값보다 작을 수 있어(대조 하위 1% 0.77, 거래량 많은 날 더 빠짐) 장대 양봉·조정 "
                 "거래량·C형 거래량 증가·D형 거래대금 판정이 확정 봉과 다를 수 있다. 확정은 익일 08:30 "
                 "`data-load daily`")
    if b["needs_load"]:
        L.append(f"→ 데이터 적재는 적재 프로세스가 맡는다: `{LOAD_COMMAND}` "
                 "(스케줄 익일 08:30 `scripts/daily-load.bat`, 애프터마켓 확정 20:05 이후). "
                 "이 스캐너는 적재하지 않는다")
    else:
        L.append("데이터 상태: 유니버스 전 종목이 기준일 정규장 일봉을 가졌다")
    return L


# ─────────────────────────────────────────────────────────────
# 출력
# ─────────────────────────────────────────────────────────────

def _krw(v) -> str:
    return "-" if v is None else f"{v / 1e8:,.0f}억"


def _pct(v) -> str:
    return "-" if v is None else f"{v * 100:+.1f}%"


TYPE_TITLE = {
    "A": "A형 — 장대 양봉 후 5일선 위 도지 2~3일",
    "B": "B형 — 장대 양봉 후 4연속 음봉 → 20일선 지지 양봉",
    "C": "C형 — 상한가/장대 양봉 후 8~12봉 기간 조정 → 거래량 동반 5일선 돌파",
    "D": "D형 — 거래대금 동반 신고가",
}


def describe(t: str, r: dict) -> str:
    if t == "A":
        return (f"기준봉 {r['base']}(몸통 {_pct(r['base_body_pct'])}), 조정 {r['adj_days']}일, "
                f"조정 거래량 {r['vol_ratio']:.0%}")
    if t == "B":
        return (f"기준봉 {r['base']}, 음봉 거래량 {r['vol_ratio']:.0%}, "
                f"저가/MA20 {_pct(r['low_vs_ma20'])}")
    if t == "C":
        kind = "상한가" if r["limit_up"] else "장대양봉"
        return (f"{kind} {r['base']}, 조정 {r['adj_days']}봉(거래량 {r['vol_ratio']:.0%}), "
                f"당일 거래량 조정평균×{r['revive']:.1f}")
    est = "(추정)" if r.get("value_est") else ""
    return (f"신고가 {'/'.join(str(n) for n in r['new_high'])}일, 거래대금 "
            f"{_krw(r['value'])}{est}(20일평균×{r['value_mult']:.1f})")


def parse_types(s: str) -> list:
    toks = [x.strip().upper() for x in s.split(",")]
    toks = [x for x in toks if x]
    bad = [x for x in toks if x not in TYPES]
    if not toks or bad:
        raise argparse.ArgumentTypeError(
            f"유형은 A,B,C,D 중 쉼표로 구분 — 잘못된 값: {', '.join(bad) or '(빈 값)'}")
    return [t for t in TYPES if t in toks]


def parse_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"날짜 형식은 YYYY-MM-DD — 입력값: {s}")


def build_rows(judged: dict, universe: dict, bars: dict, types) -> dict:
    by_type = {t: [] for t in types}
    for code, j in judged.items():
        res = j["res"]
        if res is None:
            continue
        name, idx = universe[code]
        last = bars[code][-1]
        chg = last["close"] / bars[code][-2]["close"] - 1
        hit = [t for t in TYPES if res.get(t)]
        for t in types:
            if res.get(t):
                by_type[t].append({"code": code, "name": name, "index": idx,
                                   "close": last["close"], "chg": chg, "liq": res["liq"],
                                   "detail": describe(t, res[t]), "also": hit,
                                   "scope": last["scope"]})
    for rows in by_type.values():
        rows.sort(key=lambda r: -r["liq"])
    return by_type


def summarize(judged: dict, bars: dict, types, d_period: int) -> dict:
    """제외 사유·유형별 판정 가능 수·데이터 성격 집계."""
    excluded = Counter(j["status"] for j in judged.values() if j["status"])
    ok = {c: j for c, j in judged.items() if j["res"] is not None}
    liquid = sum(1 for j in ok.values() if j["res"]["liq"] and j["res"]["liq"] >= LIQ_MIN_KRW)
    evaluable = {t: sum(1 for j in ok.values() if t not in j["res"]["na"]) for t in types}
    scopes = Counter(b["scope"] for c in ok for b in bars[c][-EVAL_BARS:])
    est = sum(1 for c in ok for b in bars[c][-EVAL_BARS:] if b.get("value_est"))
    errors = {c: j["error"] for c, j in judged.items() if j.get("error")}
    return {"excluded": dict(excluded), "judged": len(ok), "liquid": liquid,
            "evaluable": evaluable, "scopes": dict(scopes), "value_est_bars": est,
            "errors": errors}


def print_console(by_type: dict):
    for t, rows in by_type.items():
        print(f"\n━━ {TYPE_TITLE[t]}  {len(rows)}종목")
        for r in rows:
            also = ",".join(r["also"])
            print(f"  {r['code']} {r['name'][:10]:<10} {r['index']:<9} "
                  f"{r['close']:>10,.0f} {_pct(r['chg']):>7}  유동성 {_krw(r['liq']):>7}  "
                  f"[{also}] {r['detail']}")


def _atomic_write(path: str, text: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def report_path(as_of: date, generated: datetime) -> str:
    base = os.path.join(REPORT_DIR, f"종가매수-scan-{as_of:%Y%m%d}-{generated:%H%M%S}")
    path, n = base + ".md", 1
    while os.path.exists(path):          # 같은 초에 두 번 실행해도 덮어쓰지 않는다
        n += 1
        path = f"{base}-{n}.md"
    return path


def render_report(by_type: dict, meta: dict) -> str:
    s = meta["summary"]
    L = [f"# 종가매수 후보 스캔 — {meta['as_of']}", "",
         f"- 설계 {DESIGN_VERSION} · 데이터 {meta['source']} · 유형 {','.join(meta['types'])} · "
         f"D 기준 {meta['d_period']}일 · EXTENDED 허용 {'예' if meta['allow_extended'] else '아니오'}",
         f"- 유니버스 KOSPI200+KOSDAQ150 {meta['universe']}종목 → 판정 {s['judged']} "
         f"(유동성 통과 {s['liquid']})",
         f"- 제외: " + (", ".join(f"{k} {v}" for k, v in s["excluded"].items()) or "없음"),
         f"- 유형별 판정 가능: " + ", ".join(f"{t} {n}" for t, n in s["evaluable"].items()),
         f"- 판정 창 봉 성격: " + ", ".join(f"{k} {v:,}" for k, v in s["scopes"].items())
         + f" · 거래대금 추정 봉 {s['value_est_bars']:,}",
         f"- 생성 {meta['generated']}", "", f"> {WARNING}", "",
         "## 데이터 기준", ""] + [f"- {x}" for x in basis_lines(meta["basis"], meta["source"])] + [""]
    for w in meta["warnings"]:
        L.append(f"> ⚠ {w}")
    if meta["warnings"]:
        L.append("")
    L += ["## 요약", "", "| 유형 | 종목 수 |", "|---|---|"]
    L += [f"| {TYPE_TITLE[t]} | {len(rows)} |" for t, rows in by_type.items()]
    for t, rows in by_type.items():
        L += ["", f"## {TYPE_TITLE[t]}", ""]
        if not rows:
            L.append("해당 종목 없음")
            continue
        L += ["| 코드 | 종목 | 지수 | 종가 | 등락 | 20일 평균 거래대금 | 중복 유형 | 봉 성격 | 근거 |",
              "|---|---|---|---:|---:|---:|---|---|---|"]
        for r in rows:
            L.append(f"| {r['code']} | {r['name']} | {r['index']} | {r['close']:,.0f} | "
                     f"{_pct(r['chg'])} | {_krw(r['liq'])} | {','.join(r['also'])} | "
                     f"{r['scope']} | {r['detail']} |")
    if s["errors"]:
        L += ["", "## 판정 오류", ""] + [f"- {c}: {e}" for c, e in s["errors"].items()]
    L += ["", "## 판정 기준(요약)", "",
          f"- 장대 양봉: 몸통 ≥ 시가×{LONG_BODY_PCT:.0%}, 몸통 ≥ 고저폭×{LONG_BODY_RATIO:.0%}, "
          f"거래량 ≥ 직전20일평균×{LONG_VOL_MULT}",
          f"- 조정 거래량(A·B·C): 평균 ≤ 기준봉×{SHRINK_MAX:.0%}, 어느 봉도 기준봉 초과 금지, "
          "거래량 0(거래정지) 봉 있으면 불충족",
          f"- A: 조정 {A_DAYS[0]}~{A_DAYS[1]}일, 각 봉 몸통 ≤ 기준봉 몸통×{A_DOJI_PCT:.0%}, 종가 ≥ MA5",
          f"- B: 4연속 음봉, 4일째 종가 ≤ MA5×(1+{B_MA5_NEAR:.0%}), 5일째 양봉·저가 ≤ "
          f"MA20×(1+{B_MA20_TOUCH:.0%})·종가 ≥ MA20",
          f"- C: 조정 {C_MIN}~{C_MAX}봉, 조정 최저가 ≥ 기준봉 시가, 당일 양봉·MA5 돌파·"
          f"거래량 ≥ 조정평균×{C_REVIVE_MULT}",
          f"- D: {meta['d_period']}일 신고가(고가·종가), 거래대금 ≥ 20일평균×{D_VALUE_MULT} "
          f"및 ≥ {_krw(D_VALUE_MIN)}",
          f"- 공통 유동성: 직전 20일 평균 거래대금 ≥ {_krw(LIQ_MIN_KRW)}",
          "- 봉 기준 bars-1.0: 정규장·수정주가 일봉(`src/cvd_obv/bar_standard.py`)",
          "- 봉 성격: REGULAR 정규장(원천·1분봉 복원·15:35~16:00 수집) / "
          "LIVE 장중 미완료 / EXTENDED 애프터마켓 포함(기본 판정 제외)"]
    return "\n".join(L) + "\n"


# ─────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="종가매수 후보(A·B·C·D형) 스캐너 — 미검증 초안",
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=("local", "kis", "kiwoom"), default="local",
                    help="local=로컬 기준 일봉, kis=오늘 봉만 KIS 조회(기본 API), kiwoom=키움 대체")
    ap.add_argument("--live", action="store_true",
                    help="(kis·kiwoom 전용) 장중 미완료 당일 봉 포함 — 종가 직전 참고용")
    ap.add_argument("--date", type=parse_date, help="기준일 YYYY-MM-DD (기본: 가장 최근 일봉)")
    ap.add_argument("--types", type=parse_types, default=list(TYPES), help="예: A,B,C")
    ap.add_argument("--d-period", type=int, choices=D_PERIODS, default=60)
    ap.add_argument("--allow-extended", action="store_true",
                    help="정규장 복원이 안 된 애프터마켓 봉도 판정에 사용")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--no-probe", action="store_true",
                    help="시장 최신 거래일 확인용 KIS·키움 조회(1회)를 하지 않는다 — 평일 근사로 알림")
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.live and args.source not in API_SOURCES:
        ap.error("--live는 --source kis·kiwoom에서만 쓸 수 있다(로컬 일봉에는 장중 봉이 없다)")

    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(_BASE, ".env"))
    except ImportError:
        pass

    print(WARNING)
    types = args.types
    today = date.today()
    end = args.date or today
    universe_warns = []
    universe = load_universe(end, universe_warns)
    codes = sorted(universe)

    adj_mismatch = []
    if args.source == "local":
        bars = load_local(codes, end)
    else:
        env_keys = API_SOURCES[args.source][1]
        if not all(os.environ.get(k) for k in env_keys):
            print(f"  ❌ {' / '.join(env_keys)} 미설정")
            return 2
        bars, adj_mismatch = load_api(codes, end, args.live, args.source)

    calendar = market_calendar(bars)
    if not calendar:
        print(f"  ❌ 일봉이 없다 — 적재 프로세스(`{LOAD_COMMAND}`)를 먼저 돌릴 것")
        return 2
    as_of = calendar[-1]
    if args.date and as_of != end:
        print(f"  ❌ {end} 일봉이 없다(휴장일이거나 미수집) — 직전 거래일 {as_of}")
        return 2
    cal_index = {d: i for i, d in enumerate(calendar)}

    if args.date:
        reference, ref_src = None, ""
    else:
        ref, ref_src = (reference_days(datetime.now()) if not args.no_probe else
                        reference_days_approx(datetime.now()))
        reference = ref[-1]
    basis = data_basis(codes, bars, as_of, reference, ref_src)
    print("\n[데이터 기준]")
    for line in basis_lines(basis, args.source):
        print(f"  {line}")

    judged = {c: judge(bars.get(c, []), as_of, cal_index, args.d_period, args.allow_extended)
              for c in codes}
    summary = summarize(judged, bars, types, args.d_period)

    warns = list(universe_warns)
    ex = summary["excluded"]
    if ex.get(EX_EXTENDED):
        warns.append(f"{ex[EX_EXTENDED]}종목: 판정 창에 애프터마켓 포함 봉(EXTENDED)이 있고 "
                     f"정규장 복원 전이라 제외 — 적재 프로세스(`{LOAD_COMMAND}`)가 복원하면 "
                     f"판정된다(또는 --allow-extended: 애프터마켓 가격 섞임)")
    if args.allow_extended and summary["scopes"].get(SCOPE_EXTENDED):
        warns.append(f"EXTENDED 봉 {summary['scopes'][SCOPE_EXTENDED]:,}개가 판정에 쓰였다 — "
                     f"종가가 15:30 공식 종가가 아니고(20:00 확정) 거래량에 애프터마켓이 섞임")
    if summary["value_est_bars"]:
        warns.append(f"거래대금 추정 봉 {summary['value_est_bars']:,}개(1분봉 복원·원천 누락) — "
                     f"유동성·D형 거래대금 판정에 실측과 섞여 쓰였다")
    if args.live:
        warns.append("장중 미완료 봉 포함 — 당일 거래량·거래대금은 마감 전 값이다")
    if adj_mismatch:
        warns.append(f"직전 거래일 종가가 로컬과 {ADJ_MISMATCH:.1%} 넘게 다른 종목 {len(adj_mismatch)}개"
                     f"({', '.join(adj_mismatch[:10])}{' …' if len(adj_mismatch) > 10 else ''}) — "
                     "권리락·액면분할 등 수정주가 미반영 의심, 당일 봉과 지난 봉의 가격 기준이 "
                     "다를 수 있다")
    if summary["errors"]:
        warns.append(f"판정 오류 {len(summary['errors'])}종목 — 보고서 '판정 오류' 절 참조")

    src = args.source + (" (live)" if args.live else "")
    print(f"\n기준일 {as_of}  데이터 {src}  유니버스 {len(codes)}  판정 {summary['judged']}  "
          f"유동성 통과 {summary['liquid']}  설계 {DESIGN_VERSION}")
    if ex:
        print("  제외: " + ", ".join(f"{k} {v}" for k, v in ex.items()))
    print("  유형별 판정 가능: " + ", ".join(f"{t} {n}" for t, n in summary["evaluable"].items()))

    if summary["judged"] == 0:
        for w in warns:
            print(f"  ⚠ {w}")
        print("  ❌ 판정 가능한 종목이 없다 — 결과를 '후보 없음'으로 읽으면 안 된다. "
              "위 [데이터 기준] 참조")
        return 3

    by_type = build_rows(judged, universe, bars, types)
    print_console(by_type)
    for w in warns:
        print(f"  ⚠ {w}")

    if not args.no_save:
        gen = datetime.now()
        meta = {"as_of": as_of, "source": src, "universe": len(codes), "types": types,
                "d_period": args.d_period, "allow_extended": args.allow_extended,
                "summary": summary, "warnings": warns, "basis": basis,
                "generated": gen.isoformat(timespec="seconds")}
        path = report_path(as_of, gen)
        _atomic_write(path, render_report(by_type, meta))
        print(f"\n  저장: {os.path.relpath(path, _BASE)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

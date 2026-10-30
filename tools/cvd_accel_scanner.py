#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CVD **가속** 스캐너 (설계 A1, 2026-09-27) — 1분봉 기준

무엇이 다른가
=============
기존 V1.5 스캐너(`tools/kiwoom_cvd_obv_scanner.py`)는 **수준(level)** 을 본다:
`종가 > MA20`, `OBV > OBV MA20`, `매수우위일 ≥ 50%`. 이 조건들은 모두
"이미 올랐다"를 확인한다. 실측 결과 선정 종목의 **97.8%가 신호 전 10거래일
동안 이미 상승**해 있었고(평균 +20.15%), MA20 조건을 빼도 95.1%였다.

이 도구는 같은 CVD 원자료를 **변화율(가속)** 로 읽는다.

    일별 정규화 흐름   f_d = Σ(거래량 × direction) / Σ거래량      (-1 ~ 1)
    최근  = mean(f, D-2 … D)      3거래일
    이전  = mean(f, D-9 … D-3)    7거래일
    가속 A = 최근 - 이전

`direction`과 `f_d`는 V1.5의 `calc_cvd()`를 **그대로 호출**해서 얻는다
(하루치 봉만 넘기면 `normalized_delta`가 곧 그날의 f_d다). 계산식을 다시
쓰지 않는 이유는 하나다 — 두 도구가 서로 다른 숫자를 내면 어느 쪽이 맞는지
알 방법이 없다.

게이트 (5개 — 백테스트에 쓴 정의와 동일)
========================================
  1. 최근 10거래일 f가 모두 존재(완료된 정규장 기준)
  2. **A > 0**                  흐름이 빨라지고 있다
  3. **최근 3일 흐름 > 0**       실제로 매수 우위여야 한다
  4. 유동성(20일 평균 거래대금) 데이터 존재
  5. **과열 제외: 종가가 MA20 대비 +10% 이내**

의도적으로 **넣지 않은** 조건: `종가 > MA20`, `OBV > OBV MA20`,
`매수우위일 ≥ 50%`, 30분 모멘텀. 전부 "이미 올랐다"를 확인하는 조건이다.
조건 5는 MA20을 반대 방향으로 쓴다 — 상회를 요구하지 않고 과열만 막는다.

**백테스트와 다른 한 가지**: 실거래 스캔은 여기에 V1.5의 **분봉 데이터 품질
판정(`data_status == SUCCESS`)** 을 추가로 요구한다(미달이면 후보가 아니라
`DATA_INSUFFICIENT`). 백테스트는 이미 수집·검증된 저장 봉을 읽었으므로 이
검사가 필요하지 않았다. 선정 기준을 조인 것이 아니라 **API 수집 결함을 신호로
착각하지 않기 위한 가드**다.

점수 (100점, 그날 스캔 유니버스 내 백분위)
==========================================
    가속 A 백분위        × 60
    최근 3일 흐름 백분위  × 20
    유동성 백분위        × 20
정렬은 `(-점수, -20일 평균 거래대금, 종목코드)`.

검증 상태 — 반드시 같이 읽을 것
===============================
| 주장 | 상태 | 근거 |
|---|---|---|
| 신호가 움직임 **앞**에 온다 | **확립** | 신호 전 상승폭 +20.15% → **+0.25%**, 상승 종목 비율 97.8% → **55.2%** (1분봉 250일). 일봉 대리로 2010~2026 20,470건에서도 +0.93% / 58.0%로 재현 |
| 그 선행성이 **수익**이 된다 | **미확립** | 1분봉 250일 +45.1%이지만 거래당 95% 구간 [-0.207%, +1.683%]로 0 포함. 설계 인자 민감도 -2.1%~+64.7%. 일봉 대리 15.7년 D→D+2 **-0.162%**(구간 0 불포함) |

즉 **이 도구는 "덜 늦은 리스트"를 만드는 것까지만 검증됐다.** 수익은
검증되지 않았고, 장기 대리 검증은 오히려 음수 쪽이다. 매매 판단의 단독
근거로 쓰지 말 것.
자세한 근거: `reports/CVD-가속설계-검증-20260927.md`,
`reports/CVD-가속-일봉대리-장기검증-2010-2026-20260927.md`

왜 별도 파일인가
================
`tools/kiwoom_cvd_obv_scanner.py`는 **해시로 동결된 기준 구현**이다
(`data/manifests/baseline_v1_5.json`, 골든 회귀 `tests/regression/`).
그 파일을 고치면 지금까지의 모든 백테스트가 어느 코드로 나온 건지 흔들린다.
그래서 V1.5는 건드리지 않고, 그 계산 함수를 **불러 쓰는** 새 도구로 만든다.
"""

import argparse
import json
import math
import os
import sys
import uuid
from datetime import datetime
from typing import Optional

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))

from cvd_obv.bar_standard import describe as bar_standard  # noqa: E402
from cvd_obv.baseline.scanner_v15 import load_scanner_v15  # noqa: E402
from cvd_obv.bar_standard import resolve_scan_day
from cvd_obv.live_feed import STANDARD_DAILY_SCOPE, StandardLiveFeed  # noqa: E402

DESIGN_VERSION = "accel-1.0"
#: 가속 창 — 백테스트에 쓴 주 설정. 바꾸면 검증된 설계가 아니다.
RECENT_DAYS, PRIOR_DAYS = 3, 7
OVERHEAT_MAX = 10.0          # close_vs_ma20_pct 상한(%)
DEFAULT_PERIOD = 1           # 1분봉 — 검증에 쓴 해상도
#: 하루 f_d를 쓸 때 요구하는 최소 봉 완성도(표시 경고용, 게이트 아님)
DAY_COMPLETENESS_WARN = 0.80
SNAPSHOT_DIR = os.path.join(_BASE, "reports", "daily-scan")


# ─────────────────────────────────────────────────────────────
# 일별 흐름 f_d — V1.5 calc_cvd()를 하루치씩 호출해서 얻는다
# ─────────────────────────────────────────────────────────────

def daily_flows(sc, reg_bars: list, period: int, as_of: datetime) -> dict:
    """완료된 정규장 거래일별 f_d.

    반환: {"YYYYMMDD": {"f": float, "bars": int, "complete": bool}}

    f_d는 `calc_cvd(하루치 봉)["normalized_delta"]`다 — 정의상
    `Σ(거래량×direction) / Σ거래량`이고, V1.5와 **같은 함수**를 쓰므로
    direction 계산식이 갈릴 여지가 없다.
    """
    by_day = {}
    for b in reg_bars:
        by_day.setdefault(b["dt"][:8], []).append(b)

    expected = sc.expected_bars_per_day(sc._SESSION_REGULAR, period)
    out = {}
    for day, bars in by_day.items():
        if not sc.is_session_complete(day, sc._SESSION_REGULAR, as_of):
            continue
        got = sc.calc_cvd(bars, requested_days=None, period=period,
                          as_of=as_of, session=sc._SESSION_REGULAR,
                          min_bars=1, absent_is_error=False)
        f = got.get("normalized_delta")
        if f is None:
            continue
        out[day] = {"f": f, "bars": len(bars),
                    "complete": len(bars) >= expected * DAY_COMPLETENESS_WARN}
    return out


def accel_from_flows(flows: dict, recent: int, prior: int) -> Optional[dict]:
    """최근 `recent+prior` 거래일이 **연속으로** 있어야 가속을 낸다."""
    days = sorted(flows)
    need = recent + prior
    if len(days) < need:
        return None
    win = days[-need:]
    vals = [flows[d]["f"] for d in win]
    r = sum(vals[-recent:]) / recent
    p = sum(vals[:prior]) / prior
    return {
        "accel": r - p,
        "flow_recent": r,
        "flow_prior": p,
        "window_days": win,
        "flow_as_of": win[-1],
        "min_bars": min(flows[d]["bars"] for d in win),
        "incomplete_days": [d for d in win if not flows[d]["complete"]],
        "flow_series": [{"date": d, "f": flows[d]["f"], "bars": flows[d]["bars"]}
                        for d in win],
    }


# ─────────────────────────────────────────────────────────────
# 종목 하나
# ─────────────────────────────────────────────────────────────

def analyze_accel(sc, code: str, token: str, name: str = "",
                  period: int = DEFAULT_PERIOD,
                  recent: int = RECENT_DAYS, prior: int = PRIOR_DAYS,
                  as_of: Optional[datetime] = None,
                  daily_scope: str = STANDARD_DAILY_SCOPE) -> Optional[dict]:
    """가속 판정에 필요한 값만 모은다.

    OBV·가격추세·유동성·과열은 V1.5 함수를 그대로 쓴다(게이트에는 과열과
    유동성만 들어가지만, OBV·가격은 참고 표시용으로 함께 낸다 — 가속 설계가
    무엇을 **버렸는지** 화면에서 보이게 하려는 의도다).
    """
    as_of = as_of or datetime.now()
    window = recent + prior
    stock_name = name or sc.fetch_stock_name(code, token) or code

    # 완료 거래일 window개를 확보하려면 진행 중인 당일을 버릴 여유가 필요하다
    fetch_days = window + 2
    bars = sc.fetch_minute_bars(code, token, period=period, bdays=fetch_days,
                                as_of=as_of)
    bars = sc._trim_to_last_n_trading_days(bars, fetch_days)
    by_session = sc.split_bars_by_session(bars)
    reg_bars = by_session[sc._SESSION_REGULAR]

    daily_fetch_days = max(220, math.ceil(window * 7 / 5) + 15)
    daily_bars = sc.fetch_daily_bars(code, token, days=daily_fetch_days,
                                     as_of=as_of)
    daily_done = [b for b in daily_bars
                  if sc._is_bar_day_complete(b["dt"][:8], as_of, daily_scope)]
    if not reg_bars and not daily_done:
        return None

    flows = daily_flows(sc, reg_bars, period, as_of)
    acc = accel_from_flows(flows, recent, prior)

    # 창 전체의 데이터 품질은 V1.5 판정을 그대로 쓴다
    quality = sc.calc_cvd(reg_bars, requested_days=window, period=period,
                          as_of=as_of, session=sc._SESSION_REGULAR,
                          trading_days=sorted({b["dt"][:8] for b in daily_bars
                                               if len(b["dt"]) >= 8}))
    price = sc.calc_price_trend(daily_done)
    liquidity = sc.calc_liquidity(daily_done)
    obv = sc.calc_obv(daily_done, divergence_lookback=window)

    return {
        "code": code,
        "name": stock_name,
        "period": period,
        "recent_days": recent,
        "prior_days": prior,
        "accel": acc,
        "cvd_quality": {"data_status": quality.get("data_status"),
                        "status_reasons": quality.get("status_reasons"),
                        "completeness": quality.get("completeness"),
                        "bars_used": quality.get("bars_used")},
        "price": price,
        "liquidity": liquidity,
        "obv": obv,
        "as_of": as_of.strftime("%Y-%m-%d %H:%M:%S"),
        "daily_scope": daily_scope,
    }


# ─────────────────────────────────────────────────────────────
# 횡단면 — 게이트와 점수
# ─────────────────────────────────────────────────────────────

_STATUS_LABEL = {"BUY_CANDIDATE": "🟩후보", "WATCH": "🟨관찰",
                 "DATA_INSUFFICIENT": "⬜부족"}
_STATUS_ORDER = {"BUY_CANDIDATE": 0, "WATCH": 1, "DATA_INSUFFICIENT": 2}


def _percentiles(values: dict) -> dict:
    """결측을 뺀 오름차순 백분위(0~1). 단일 종목이면 0.5(비교 불가)."""
    good = {k: v for k, v in values.items() if v is not None}
    if len(good) < 2:
        return {k: (0.5 if k in good else None) for k in values}
    order = sorted(good.items(), key=lambda kv: kv[1])
    out = {k: None for k in values}
    for i, (k, _) in enumerate(order):
        out[k] = i / (len(order) - 1)
    return out


def compute_gate(r: dict, overheat_max: Optional[float] = OVERHEAT_MAX) -> dict:
    """가속 게이트 — 백테스트와 같은 5개 조건."""
    acc = r.get("accel")
    liq = (r.get("liquidity") or {}).get("value")
    cvm = (r.get("price") or {}).get("close_vs_ma20_pct")
    quality = (r.get("cvd_quality") or {}).get("data_status")

    reasons = []
    if acc is None:
        reasons.append(f"일별 흐름 {r['recent_days'] + r['prior_days']}거래일 미확보")
    if quality != "SUCCESS":
        reasons.append(f"분봉 데이터 품질 미충족({quality})")
    if acc is not None and acc["accel"] <= 0:
        reasons.append("가속 A ≤ 0(흐름이 빨라지지 않음)")
    if acc is not None and acc["flow_recent"] <= 0:
        reasons.append(f"최근 {r['recent_days']}일 흐름 ≤ 0(매수 우위 아님)")
    if liq is None:
        reasons.append("유동성 데이터 없음")
    if overheat_max is not None:
        if cvm is None:
            reasons.append("MA20 이격 계산 불가(과열 판정 불가)")
        elif cvm > overheat_max:
            reasons.append(f"과열(MA20 대비 +{cvm:.1f}% > +{overheat_max:.0f}%)")

    data_ok = acc is not None and quality == "SUCCESS"
    if not data_ok:
        status = "DATA_INSUFFICIENT"
    elif not reasons:
        status = "BUY_CANDIDATE"
    else:
        status = "WATCH"
    return {"status": status, "gate_passed": status == "BUY_CANDIDATE",
            "gate_reasons": reasons}


def finalize(results: list, overheat_max: Optional[float] = OVERHEAT_MAX) -> list:
    """백분위 점수(60/20/20)를 채우고 순위대로 돌려준다.

    백분위 모집단은 **그날 스캔한 유니버스 전체**다(백테스트와 동일).
    """
    a_map = {r["code"]: (r["accel"]["accel"] if r.get("accel") else None)
             for r in results}
    f_map = {r["code"]: (r["accel"]["flow_recent"] if r.get("accel") else None)
             for r in results}
    l_map = {r["code"]: (r.get("liquidity") or {}).get("value") for r in results}
    a_pct, f_pct, l_pct = (_percentiles(a_map), _percentiles(f_map),
                           _percentiles(l_map))

    for r in results:
        c = r["code"]
        score = ((a_pct[c] or 0) * 60 + (f_pct[c] or 0) * 20
                 + (l_pct[c] or 0) * 20)
        r["accel_percentile"] = a_pct[c]
        r["flow_percentile"] = f_pct[c]
        r["liquidity_percentile"] = l_pct[c]
        r["total"] = round(score, 1)
        r.update(compute_gate(r, overheat_max))

    results.sort(key=lambda r: (_STATUS_ORDER.get(r["status"], 9),
                                -r["total"],
                                -((r.get("liquidity") or {}).get("value") or 0),
                                r["code"]))
    for i, r in enumerate(results, 1):
        r["rank"] = i
    return results


# ─────────────────────────────────────────────────────────────
# 출력
# ─────────────────────────────────────────────────────────────

_BANNER = (
    "  ⚠ 이 설계는 **선행성만** 검증됐다 — 신호 전 상승폭 +20.15%→+0.25%,\n"
    "    상승 종목 비율 97.8%→55.2%(1분봉 250일), 일봉 대리 2010~2026 재현.\n"
    "    **수익은 검증되지 않았다** — 거래당 95% 구간이 0을 포함하고,\n"
    "    일봉 대리 15.7년 D→D+2는 -0.162%(구간 0 불포함)로 음수다.\n"
    "    단독 매매 근거로 쓰지 말 것."
)


def _fmt(v, digits=4, suffix=""):
    return "—" if v is None else f"{v:+.{digits}f}{suffix}"


def print_results(results: list, top: int, label: str, as_of: datetime,
                  period: int, recent: int, prior: int,
                  overheat_max: Optional[float]) -> None:
    print()
    print("=" * 108)
    print(f"  CVD 가속 스캔 — {label}   기준시각 {as_of:%Y-%m-%d %H:%M}")
    print(f"  {period}분봉 · 가속창 최근{recent}일-이전{prior}일 · "
          f"과열상한 {'없음' if overheat_max is None else f'+{overheat_max:.0f}%'} · "
          f"설계 {DESIGN_VERSION}")
    print("=" * 108)
    print(_BANNER)
    print("=" * 108)

    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("  판정: " + "  ".join(
        f"{_STATUS_LABEL.get(k, k)} {v}" for k, v in sorted(
            counts.items(), key=lambda kv: _STATUS_ORDER.get(kv[0], 9))))

    flow_days = {r["accel"]["flow_as_of"] for r in results if r.get("accel")}
    if flow_days:
        newest = max(flow_days)
        print(f"  흐름 기준일(마지막 완료 정규장): {newest}"
              + (f"   ⚠ 종목별로 다름: {len(flow_days)}종류" if len(flow_days) > 1 else ""))
        if newest != as_of.strftime("%Y%m%d"):
            print("  ⚠ 오늘 정규장이 아직 완료되지 않았다 — 위 신호는 "
                  "**직전 거래일 종가까지**의 흐름이다.")

    print()
    print(f"  {'순위':>4} {'종목':<14} {'점수':>6} {'가속A':>10} "
          f"{'최근':>9} {'이전':>9} {'MA20이격':>9} {'거래대금':>10} "
          f"{'OBV':>6} {'판정':>8}")
    print("  " + "-" * 104)
    for r in results[:top]:
        acc = r.get("accel") or {}
        price = r.get("price") or {}
        liq = (r.get("liquidity") or {}).get("value")
        obv = r.get("obv") or {}
        obv_mark = ("▲" if (obv.get("obv_ma20") is not None
                            and obv.get("obv") is not None
                            and obv["obv"] > obv["obv_ma20"]) else "▽")
        cvm = price.get("close_vs_ma20_pct")
        cvm_cell = "—" if cvm is None else f"{cvm:+.1f}%"
        liq_cell = "—" if not liq else f"{liq / 1e8:,.0f}억"
        print(f"  {r['rank']:>4} {sc_name(r):<14} {r['total']:>6.1f} "
              f"{_fmt(acc.get('accel')):>10} {_fmt(acc.get('flow_recent')):>9} "
              f"{_fmt(acc.get('flow_prior')):>9} {cvm_cell:>9} {liq_cell:>10} "
              f"{obv_mark:>6} {_STATUS_LABEL.get(r['status'], r['status']):>8}")

    shown = [r for r in results[:top] if r["status"] != "BUY_CANDIDATE"]
    if shown:
        print()
        print("  [미통과 사유]")
        for r in shown:
            print(f"   {r['code']} {r['name'][:10]}: "
                  + "; ".join(r["gate_reasons"][:3]))

    warn = [r for r in results[:top]
            if r.get("accel") and r["accel"]["incomplete_days"]]
    if warn:
        print()
        print(f"  ⚠ 봉 완성도 80% 미만인 날이 섞인 종목 {len(warn)}개 "
              "(게이트에는 반영하지 않음 — 백테스트 정의를 그대로 두기 위해):")
        for r in warn[:5]:
            print(f"   {r['code']} {r['name'][:10]}: "
                  f"{', '.join(r['accel']['incomplete_days'])}")

    print()
    print("  용어: 가속A = 최근 흐름 - 이전 흐름 (>0이면 매수 흐름이 빨라짐)")
    print("        최근/이전 = 일별 정규화 CVD 흐름 평균 (-1~1)")
    print("        OBV ▲/▽ = OBV가 MA20 위/아래 — **게이트에 쓰지 않는다**"
          "(이미 오른 종목을 고르게 되므로 의도적으로 뺐다)")
    print()


def sc_name(r: dict) -> str:
    n = r["name"] if len(r["name"]) <= 8 else r["name"][:8]
    return f"{r['code']} {n}"


def save_snapshot(results: list, index_key: str, label: str, as_of: datetime,
                  period: int, recent: int, prior: int,
                  overheat_max: Optional[float], run_id: str,
                  universe_standard: Optional[dict] = None) -> str:
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    fname = (f"cvd-accel-{index_key}-{as_of:%Y%m%d_%H%M%S}-{run_id}.json")
    path = os.path.join(SNAPSHOT_DIR, fname)
    payload = {
        "design_version": DESIGN_VERSION,
        "design": {
            "flow": "f_d = sum(volume*direction)/sum(volume), V1.5 calc_cvd 호출",
            "accel": f"mean(f, 최근 {recent}일) - mean(f, 이전 {prior}일)",
            "gate": ["연속 거래일 확보", "A > 0", f"최근 {recent}일 흐름 > 0",
                     "유동성 존재",
                     ("과열 없음" if overheat_max is None
                      else f"MA20 이격 <= +{overheat_max:.0f}%")],
            "score": "가속 백분위 60 + 최근흐름 백분위 20 + 유동성 백분위 20",
            "excluded_on_purpose": ["종가 > MA20", "OBV > OBV MA20",
                                    "매수우위일 >= 50%", "30분 모멘텀"],
        },
        "validation_status": {
            "leadingness": "확립 (신호 전 상승폭 +20.15%->+0.25%, 97.8%->55.2%)",
            "profitability": ("미확립 — 거래당 95% 구간 0 포함, "
                              "일봉 대리 15.7년 D→D+2 -0.162%"),
            "reports": ["reports/CVD-가속설계-검증-20260927.md",
                        "reports/CVD-가속-일봉대리-장기검증-2010-2026-20260927.md"],
        },
        "baseline": {"module": "tools/kiwoom_cvd_obv_scanner.py",
                     "scanner_version": load_scanner_v15()._SCANNER_VERSION},
        "run_id": run_id,
        "index": index_key,
        "label": label,
        "as_of": as_of.strftime("%Y-%m-%d %H:%M:%S"),
        "period_min": period,
        "bar_standard": bar_standard(),
        "universe_standard": universe_standard,
        "recent_days": recent,
        "prior_days": prior,
        "overheat_max_pct": overheat_max,
        "universe_size": len(results),
        "results": results,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1, default=str)
    print(f"  💾 저장: {os.path.relpath(path, _BASE)}")
    return path


# ─────────────────────────────────────────────────────────────
# 명령
# ─────────────────────────────────────────────────────────────

def cmd_scan(args) -> int:
    sc = load_scanner_v15()
    token = sc._get_token()
    if not token:
        print("\n  ❌ 키움 API 미연결 — .env의 KIWOOM_APP_KEY/SECRET 확인")
        return 2

    # 기준 거래일은 적재 상태가 정한다(봉 기준 bars-1.0, 2026-10-26) —
    # 16:10 이전은 전 거래일 확정 봉, 이후는 오늘 잠정 봉. --as-of는 그대로 존중.
    as_of = args.as_of or resolve_scan_day().end_of_day
    run_id = uuid.uuid4().hex[:8]
    overheat = None if args.no_overheat else args.overheat
    indices = (["kospi200", "kosdaq150"] if args.index == "both"
               else [args.index])
    labels = {"kospi200": "코스피200", "kosdaq150": "코스닥150"}

    # 유니버스 기준 universe-1.0 — 스캔 기준일 명단(`cvd_obv.universe.universe_on`)
    feed = StandardLiveFeed(sc, as_of=as_of)
    pool = []
    for idx in indices:
        for s in feed.load_index(idx):
            pool.append({**s, "index": idx})
    if args.limit:
        pool = pool[:args.limit]
        print(f"  ⚠ --limit {args.limit}: 유니버스를 잘라서 돌린다 — "
              "백분위 점수는 이 부분집합 안에서의 상대값이다")
    if not pool:
        return 2

    window = args.recent + args.prior
    est_min = len(pool) * (window + 2) * sc.expected_bars_per_day(
        sc._SESSION_REGULAR, args.period) / 900 * 0.22 / 60
    print(f"\n  스캔 {len(pool)}종목 · {args.period}분봉 {window + 2}거래일 수집 "
          f"(분봉 호출만 약 {max(est_min, 0.1):.1f}분, 일봉·종목명 호출은 별도 — "
          "초당 5회 제한)")

    results = []
    # 봉 기준 bars-1.0: 일봉은 로컬 정규장 봉, 분봉은 1분봉 합성(`live_feed`)
    feed.codes |= {s["code"] for s in pool}
    with feed.installed():
        for i, s in enumerate(pool, 1):
            print(f"  [{i:3}/{len(pool)}] {s['code']} {s['name'][:8]} ...", end="\r")
            try:
                r = analyze_accel(sc, s["code"], token, name=s["name"],
                                  period=args.period, recent=args.recent,
                                  prior=args.prior, as_of=as_of,
                                  daily_scope=args.daily_scope)
            except Exception as exc:                      # noqa: BLE001
                print(f"\n  ⚠ {s['code']} {s['name'][:8]} 실패: {exc}")
                continue
            if r:
                r["index"] = s["index"]
                results.append(r)
    for line in feed.summary_lines():
        print(line)

    if not results:
        print("\n  ❌ 분석된 종목이 없다")
        return 3

    # 백분위 모집단은 스캔한 전체다(백테스트와 동일 — 지수별로 쪼개지 않는다)
    finalize(results, overheat)
    label = ("코스피200+코스닥150(통합)" if args.index == "both"
             else labels.get(args.index, args.index))
    print_results(results, args.top, label, as_of, args.period,
                  args.recent, args.prior, overheat)
    if args.save_json:
        save_snapshot(results, args.index, label, as_of, args.period,
                      args.recent, args.prior, overheat, run_id,
                      universe_standard=feed.universe().describe())
    return 0


def cmd_detail(args) -> int:
    sc = load_scanner_v15()
    token = sc._get_token()
    if not token:
        print("\n  ❌ 키움 API 미연결 — .env의 KIWOOM_APP_KEY/SECRET 확인")
        return 2
    # 기준 거래일은 적재 상태가 정한다(봉 기준 bars-1.0, 2026-10-26) —
    # 16:10 이전은 전 거래일 확정 봉, 이후는 오늘 잠정 봉. --as-of는 그대로 존중.
    as_of = args.as_of or resolve_scan_day().end_of_day
    feed = StandardLiveFeed(sc, as_of=as_of, codes=[args.code])
    with feed.installed():
        r = analyze_accel(sc, args.code, token, period=args.period,
                          recent=args.recent, prior=args.prior, as_of=as_of,
                          daily_scope=args.daily_scope)
    for line in feed.summary_lines():
        print(line)
    if not r:
        print("  ❌ 데이터 없음")
        return 3
    overheat = None if args.no_overheat else args.overheat
    r.update(compute_gate(r, overheat))
    acc = r.get("accel")

    print()
    print("=" * 78)
    print(f"  {r['code']} {r['name']} — CVD 가속 상세  "
          f"(기준 {as_of:%Y-%m-%d %H:%M}, {args.period}분봉)")
    print("=" * 78)
    print(_BANNER)
    print("=" * 78)
    print("  ※ 단일 종목에는 백분위 점수가 없다 — 비교 모집단이 없기 때문이다."
          "\n     절대 기준(게이트)만 판정한다.")
    if acc:
        print(f"\n  가속 A        {acc['accel']:+.4f}")
        print(f"  최근 {r['recent_days']}일 흐름 {acc['flow_recent']:+.4f}")
        print(f"  이전 {r['prior_days']}일 흐름 {acc['flow_prior']:+.4f}")
        print(f"  흐름 기준일    {acc['flow_as_of']}  "
              f"(창 {acc['window_days'][0]}~{acc['window_days'][-1]})")
        print(f"\n  일별 흐름 f_d")
        for row in acc["flow_series"]:
            bar = "█" * min(40, int(abs(row["f"]) * 200))
            print(f"   {row['date']}  {row['f']:+.4f}  {bar}"
                  f"   ({row['bars']}봉)")
    else:
        print("\n  가속 계산 불가 — 연속 거래일 미확보")
    price, liq = r.get("price") or {}, (r.get("liquidity") or {})
    cvm = price.get("close_vs_ma20_pct")
    cvm_cell = "—" if cvm is None else f"{cvm:+.2f}%"
    limit_cell = "없음" if overheat is None else f"+{overheat:.0f}%"
    liq_value = liq.get("value")
    liq_cell = "—" if not liq_value else f"{liq_value / 1e8:,.0f}억"
    print(f"\n  MA20 이격     {cvm_cell}   (과열 상한 {limit_cell})")
    print(f"  20일 거래대금  {liq_cell}")
    print(f"  분봉 품질      {r['cvd_quality']['data_status']}"
          + (f" — {'; '.join(r['cvd_quality']['status_reasons'])}"
             if r["cvd_quality"].get("status_reasons") else ""))
    print(f"\n  판정: {_STATUS_LABEL.get(r['status'], r['status'])}")
    for reason in r["gate_reasons"]:
        print(f"    - {reason}")
    print()
    return 0


def _as_of(value):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"기준시각 형식이 아니다: {value} (예: '2026-09-27 15:40')")


def _add_common(p):
    p.add_argument("--period", type=int, default=DEFAULT_PERIOD,
                   choices=[1, 3, 5, 10, 15, 30, 60],
                   help="분봉 주기(기본 1 — 검증에 쓴 해상도)")
    p.add_argument("--recent", type=int, default=RECENT_DAYS,
                   help=f"최근 구간 거래일(기본 {RECENT_DAYS})")
    p.add_argument("--prior", type=int, default=PRIOR_DAYS,
                   help=f"이전 구간 거래일(기본 {PRIOR_DAYS})")
    p.add_argument("--overheat", type=float, default=OVERHEAT_MAX,
                   help=f"MA20 이격 상한 %% (기본 {OVERHEAT_MAX:.0f})")
    p.add_argument("--no-overheat", action="store_true",
                   help="과열 조건 제거(민감도 확인용 — 검증된 설계가 아니다)")
    p.add_argument("--daily-scope", default=STANDARD_DAILY_SCOPE,
                   choices=["auto", "regular", "extended"],
                   help="일봉 완료 판정 기준(기본 regular — 봉 기준 bars-1.0 정규장 일봉)")
    p.add_argument("--as-of", type=_as_of, default=None,
                   help="기준시각 고정(회귀 검증용)")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="CVD 가속 스캐너 — 선행성만 검증된 설계(수익 미확립)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="지수 전종목 스캔")
    scan.add_argument("--index", default="both",
                      choices=["kospi200", "kosdaq150", "both"])
    scan.add_argument("--top", type=int, default=15)
    scan.add_argument("--save-json", action="store_true")
    scan.add_argument("--limit", type=int, default=None,
                      help="유니버스 앞 N종목만(연결 확인용)")
    _add_common(scan)
    scan.set_defaults(func=cmd_scan)

    detail = sub.add_parser("detail", help="단일 종목 상세")
    detail.add_argument("--code", required=True)
    _add_common(detail)
    detail.set_defaults(func=cmd_detail)

    args = ap.parse_args()
    if args.recent < 1 or args.prior < 1:
        ap.error("--recent/--prior는 1 이상이어야 한다")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

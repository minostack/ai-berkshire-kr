#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CVD/OBV 스캔 — 체결량 **통합 기준(UN)** · 창 **15거래일** (설계 `sides-1.0`)

두 설계를 한 번에 돌린다.

    가속(accel)   흐름이 빨라지는 종목.  A = 최근5일 평균 − 이전10일 평균
    일반(cvdobv)  흐름·추세가 함께 선 종목. V1.5 정규장 절대조건의 일별 재현판

무엇이 달라졌나 (앞 설계 대비)
------------------------------
| | accel-1.0 / accel-truecvd-0.1 | **sides-1.0** |
|---|---|---|
| 흐름 원천 | 1분봉 추정 / KIS `J`(KRX 단독) | KIS **`UN`(KRX+NXT)** |
| 창 | 최근3 + 이전7 = 10거래일 | **최근5 + 이전10 = 15거래일** |
| 일반 설계 | 없음(V1.5는 장중 분봉 필요) | **있음**(일별 재현판, 창 15일) |

왜 통합 기준인가: 2026-10-20 A안으로 **정규장 일봉 거래량에 NXT 메인마켓이
들어왔다**. OBV·유동성·MA20이 그 거래량에서 나오므로 흐름만 KRX 단독으로 두면
같은 봉에서 분자와 분모의 시장이 어긋난다. 자세한 것은
`src/cvd_obv/sides_flow.py`.

검증 상태 (사전등록 WINDOW15, 대장 10번 · 2026-10-21)
  · **창 15 > 창 10 — 유니버스 안에서는 근거가 있다.** 가속 설계 창차
    +0.0269%p [+0.0091, +0.0438] q=0.0070(16.4년 4,015거래일). 창 15의
    가속은 유니버스도 이긴다(+0.0290%p q=0.0338). 창 10은 못 이겼다
  · ⚠ **지수는 여전히 못 이긴다** — −0.4035%p(유의 음수), 지수를 이긴 날 32.8%.
    "유니버스 안에서 나은 선택"일 뿐이다
  · ⚠ 평균 순수익은 **−0.42%**(2일 보유, 비용 후). 위 수치는 전부 상대 비교다
  · ⚠ 효과가 **2010-2017에 몰려 있다**(2018-2026 구간은 0을 포함)
  · ⚠ 선행성이 약해진다 — 신호 전 상승 +1.40% → **+2.42%**
  · ⚠ **통합 기준(UN)에서는 재현하지 못했다**(362거래일, MDE 0.138%p로 부족).
    이 스캐너가 UN을 쓰므로, 위 근거는 "기준은 창 효과에 영향 없다"는
    **검증되지 않은 가정** 위에 있다
  · 일반(OBV 결합) 설계의 창차는 `UNDERPOWERED`다 — 창 15를 쓸 근거가 없다
  · MA 길이 20은 **10·60과 비교해 바꿀 근거가 없었다**(사전등록 MALEN,
    대장 11번). 유일하게 0을 벗어난 짝차는 가속 MA10−MA20 −0.0094%p로
    **MA20이 낫다**는 방향이다. MA 길이는 창보다 훨씬 작게 작동한다
    (가장 큰 짝차 0.0125%p vs 창 효과 0.0269%p)
  근거: `reports/WINDOW15-창10-창15-비교-20261021.md`,
        `reports/MALEN-이동평균길이-10-20-60-비교-20261021.md`
  단독 매매 근거로 쓰지 말 것.

재사용(정의를 두 벌 만들지 않는다):
  · `cvd_obv.sides_flow.read_sides_flow`      통합 기준 흐름
  · `accel_vidya_backtest.accel_from_flow`    가속(창을 인자로 받는다)
  · `truecvd_backtest.obv_series`·`window_flow`  OBV(V1.5 calc_obv 벡터화판)·창 흐름
  · `cvd_accel_scanner._percentiles`          백분위 배점
  · `cvd_obv.bar_standard.read_standard_daily`, `cvd_obv.universe`

    python tools/cvd_obv_sides_scan.py --top 15 --save-json
    python tools/cvd_obv_sides_scan.py --design accel --no-live-today
"""

import argparse
import json
import os
import sys
import time
import uuid
from datetime import date, datetime, timedelta

import numpy as np

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import accel_vidya_backtest as av               # noqa: E402
import cvd_accel_scanner as ac                  # noqa: E402
import truecvd_backtest as tc                   # noqa: E402

from cvd_obv.bar_standard import read_standard_daily  # noqa: E402
from cvd_obv.ingestion.kis_trade_sides import TradeSidesLoader  # noqa: E402
from cvd_obv.sides_flow import BASIS_VERSION, read_sides_flow  # noqa: E402
from cvd_obv.universe import current_universe  # noqa: E402

DESIGN_VERSION = "sides-1.0"
OUT_DIR = os.path.join(_BASE, "reports", "daily-scan")

# ── 설계 상수 (사용자 지정: 15영업일) ──
RECENT_DAYS = 5
PRIOR_DAYS = 10
WINDOW = RECENT_DAYS + PRIOR_DAYS        # 15거래일 — 두 설계가 같은 길이를 본다
BULL_MIN = 0.50                          # 일반 설계: 매수우위일 비율 하한
OVERHEAT_MAX = ac.OVERHEAT_MAX           # 가속 설계: MA20 이격 상한 %(10.0)
MA_DAYS = 20
LIQ_DAYS = 20
LOOKBACK_DAYS = 150                      # 일봉 워밍업(달력일) — MA20·OBV MA20
DESIGNS = ("accel", "cvdobv")


# ─────────────────────────────────────────────────────────────
# 입력
# ─────────────────────────────────────────────────────────────

def latest_trading_day() -> date:
    """기준 거래일 = **기준 일봉의 마지막 날**.

    `silver.market_calendar`로 정하지 않는다 — 달력은 `calendar build`를 따로
    돌려야 늘어나서 밀리기 쉽다(2026-10-19 실측: 달력 10-12, 일봉 10-19).
    """
    import pyarrow.dataset as ds
    t = read_standard_daily(
        columns=["security_id", "trading_date"],
        filters=(ds.field("trading_date") > date.today() - timedelta(days=30))
        & (ds.field("trading_date") <= date.today()),
        allow_extended=False, drop_failed=False)
    days = sorted(set(t.column("trading_date").to_pylist()))
    if not days:
        raise SystemExit("최근 30일 기준 일봉이 없다 — 적재 상태를 확인하라")
    return days[-1]


def fetch_today_un(codes, day: date, log=print) -> dict:
    """오늘치 `UN` 체결량을 API에서 바로 받는다(저장 테이블은 건드리지 않는다).

    16:10 적재가 돌기 전에 스캔할 때만 쓴다. 장중 누적값이 올 수 있으므로
    정규장이 끝난 뒤에 부르는 것을 전제한다.
    """
    ld = TradeSidesLoader(market_div="UN")
    out, fails = {}, 0
    t0 = time.monotonic()
    for i, c in enumerate(codes, 1):
        rows = ld.fetch_security(c, day, day)
        hit = next((r for r in rows if r["trading_date"] == day
                    and (r["buy_qty"] + r["sell_qty"]) > 0), None)
        if hit:
            out[c] = (hit["buy_qty"], hit["sell_qty"])
        else:
            fails += 1
        if i % 100 == 0:
            log(f"    {i}/{len(codes)} ({time.monotonic() - t0:.0f}초)")
    log(f"  오늘({day}) 체결량[UN] {len(out)}종목 수신 · 없음 {fails}종목 "
        f"({time.monotonic() - t0:.0f}초)")
    return out


def security_names(u) -> dict:
    """{코드: 종목명}. `Universe.stocks()`는 지수별로 받는다(인자 필수)."""
    names = {}
    for idx in u.describe()["counts"]:
        for st in u.stocks(idx):
            names[st["code"]] = st.get("name", "")
    if not names:
        raise SystemExit("종목명을 하나도 못 읽었다 — universe 구조를 확인하라")
    return names


# ─────────────────────────────────────────────────────────────
# 지표
# ─────────────────────────────────────────────────────────────

def build_rows(as_of: date, live_today: bool = True, log=print,
               ma_days: int = MA_DAYS) -> dict:
    import pyarrow.dataset as ds

    u = current_universe()
    sids = sorted(u.security_ids())
    codes = [s.split(":")[1] for s in sids]
    log(f"  유니버스 {len(codes)}종목 — {u.describe()['sources']['KOSPI200']}")
    for w in (u.warnings or []):
        log(f"  ⚠ [유니버스] {w}")

    start = as_of - timedelta(days=LOOKBACK_DAYS)

    # ── 흐름 (통합 기준) ──
    flows = read_sides_flow(sids, start, as_of)
    stored_last = max((d for per in flows.values() for d in per), default=None)
    log(f"  저장 체결량[{BASIS_VERSION}] 마지막 거래일 {stored_last}")
    live = {}
    if live_today and stored_last and stored_last < as_of:
        log(f"  오늘({as_of}) 체결량은 저장분에 없다 — API에서 받는다")
        live = fetch_today_un(codes, as_of, log=log)
        from cvd_obv.sides_flow import SOURCE_UN, SidesDay
        for c, (b, s) in live.items():
            flows.setdefault(c, {})[as_of] = SidesDay(b, s, SOURCE_UN, True)
    prov = sum(1 for per in flows.values() for sd in per.values()
               if sd.provisional)
    if prov:
        log(f"  ⚠ 잠정 체결량 {prov}행 — 익일 08:30 확정으로 덮인다")

    # ── 일봉 ──
    day = read_standard_daily(
        columns=["security_id", "trading_date", "close", "volume",
                 "traded_value", "quality_flags"],
        filters=(ds.field("trading_date") >= start)
        & (ds.field("trading_date") <= as_of)
        & ds.field("security_id").isin(sids),
        allow_extended=False, drop_failed=True, fill_traded_value=True)
    bars, nxt_vol_days = {}, 0
    for sid, d, c, v, tv, fl in zip(
            day.column("security_id").to_pylist(),
            day.column("trading_date").to_pylist(),
            day.column("close").to_pylist(),
            day.column("volume").to_pylist(),
            day.column("traded_value").to_pylist(),
            day.column("quality_flags").to_pylist()):
        bars.setdefault(sid.split(":")[1], []).append(
            (d, float(c), int(v), float(tv) if tv is not None else float(c) * v))
        if "NXT_MAIN_VOLUME_INCLUDED" in (fl or []):
            nxt_vol_days += 1
    for c in bars:
        bars[c].sort()
    log(f"  일봉 {sum(len(v) for v in bars.values()):,}행 · "
        f"NXT 거래량 포함 {nxt_vol_days:,}행")

    cal = sorted({d for rows in bars.values() for d, *_ in rows})
    pos = {d: i for i, d in enumerate(cal)}
    T = len(cal)
    names = security_names(u)

    rows = []
    for c in codes:
        br = bars.get(c, [])
        if len(br) < ma_days:
            continue
        k = np.array([pos[d] for d, *_ in br])
        close = np.array([r[1] for r in br], float)
        volume = np.array([r[2] for r in br], float)
        value = np.array([r[3] for r in br], float)

        c_cal = np.full(T, np.nan); c_cal[k] = close
        v_cal = np.full(T, np.nan); v_cal[k] = volume
        ma_cal = np.full(T, np.nan); ma_cal[k] = av.rolling_mean(close, ma_days)
        liq_cal = np.full(T, np.nan); liq_cal[k] = av.rolling_mean(value, LIQ_DAYS)

        # 흐름을 캘린더 격자에
        f_cal = np.full(T, np.nan)
        w_cal = np.full(T, np.nan)
        prov_cal = np.zeros(T, bool)
        for d, sd in flows.get(c, {}).items():
            i = pos.get(d)
            fv = sd.f
            if i is None or fv is None:
                continue
            f_cal[i] = fv
            w_cal[i] = sd.total
            prov_cal[i] = sd.provisional

        a = av.accel_from_flow(f_cal, RECENT_DAYS, PRIOR_DAYS)
        wf = tc.window_flow(f_cal, WINDOW, w_cal)
        # OBV MA도 같은 길이로 간다 — 한 설계 안에서 MA 자리를 섞지 않는다
        ob = tc.obv_series(close, volume, ma_days=ma_days)
        obv_cal = np.full(T, np.nan); obv_cal[k] = ob["obv"]
        obvma_cal = np.full(T, np.nan); obvma_cal[k] = ob["obv_ma20"]
        nsl_cal = np.full(T, np.nan); nsl_cal[k] = ob["normalized_slope"]

        j = pos.get(as_of)
        if j is None:
            continue

        cvm = (float(c_cal[j] / ma_cal[j] - 1) * 100
               if np.isfinite(ma_cal[j]) and np.isfinite(c_cal[j]) else None)
        win = [str(cal[x]) for x in range(max(0, j - WINDOW + 1), j + 1)]
        row = {
            "code": c, "name": names.get(c, ""),
            "window_days": win,
            "flow_as_of": str(as_of),
            "flow_provisional": bool(prov_cal[j]),
            "flow_source": (flows.get(c, {}).get(as_of).source
                            if flows.get(c, {}).get(as_of) else None),
            "price": {
                "close": (float(c_cal[j]) if np.isfinite(c_cal[j]) else None),
                # 키 이름은 하위호환으로 두되 실제 길이는 ma_days에 적는다
                "ma_days": ma_days,
                "ma20": (float(ma_cal[j]) if np.isfinite(ma_cal[j]) else None),
                "close_vs_ma20_pct": (round(cvm, 1) if cvm is not None else None),
                "above_ma20": (bool(cvm > 0) if cvm is not None else None)},
            "liquidity": {"value": (float(liq_cal[j])
                                    if np.isfinite(liq_cal[j]) else None),
                          "days_used": LIQ_DAYS},
            "volume": (float(v_cal[j]) if np.isfinite(v_cal[j]) else None),
        }
        row["accel"] = ({
            "accel": float(a["accel"][j]), "flow_recent": float(a["recent"][j]),
            "flow_prior": float(a["prior"][j]),
            "recent_days": RECENT_DAYS, "prior_days": PRIOR_DAYS}
            if a["ok"][j] and np.isfinite(a["accel"][j]) else None)
        row["cvdobv"] = ({
            "normalized_delta": float(wf["nd"][j]),
            "bull_ratio": float(wf["bull"][j]),
            "days_used": int(wf["days"][j]),
            "obv": (float(obv_cal[j]) if np.isfinite(obv_cal[j]) else None),
            "obv_ma20": (float(obvma_cal[j]) if np.isfinite(obvma_cal[j]) else None),
            "obv_normalized_slope": (float(nsl_cal[j])
                                     if np.isfinite(nsl_cal[j]) else None)}
            if (np.isfinite(wf["nd"][j]) and np.isfinite(wf["bull"][j])
                and wf["days"][j] >= WINDOW) else None)
        rows.append(row)

    return {"rows": rows, "universe": u, "as_of": as_of, "cal": cal,
            "stored_last": stored_last, "live_count": len(live),
            "provisional_rows": prov, "nxt_volume_rows": nxt_vol_days,
            "ma_days": ma_days}


# ─────────────────────────────────────────────────────────────
# 게이트·배점
# ─────────────────────────────────────────────────────────────

def gate_accel(r: dict) -> tuple:
    """accel-1.0의 5개 조건을 창만 15일로 바꿔 적용한다."""
    a = r.get("accel")
    if a is None:
        return "DATA_INSUFFICIENT", [f"흐름 {WINDOW}거래일 연속 아님"]
    why = []
    if a["accel"] <= 0:
        why.append("가속 A ≤ 0(흐름이 빨라지지 않음)")
    if a["flow_recent"] <= 0:
        why.append(f"최근 {RECENT_DAYS}일 흐름 ≤ 0(매수 우위 아님)")
    if (r["liquidity"] or {}).get("value") is None:
        why.append("유동성 데이터 없음")
    cvm = r["price"]["close_vs_ma20_pct"]
    man = r["price"].get("ma_days", MA_DAYS)
    if cvm is None:
        why.append(f"MA{man} 없음")
    elif cvm > OVERHEAT_MAX:
        why.append(f"과열(MA{man} +{cvm:.1f}% > {OVERHEAT_MAX:.0f}%)")
    return ("BUY_CANDIDATE" if not why else "WATCH"), why


def gate_cvdobv(r: dict) -> tuple:
    """V1.5 정규장 절대조건 중 **일별로 재현 가능한 5개**. 창 15일.

    V1.5의 30분 모멘텀(`normalized_momentum`)은 장중 데이터가 필요해 **뺐다**.
    그래서 이것은 V1.5가 아니다 — 더 느슨한 게이트다.
    """
    c = r.get("cvdobv")
    if c is None:
        return "DATA_INSUFFICIENT", [f"흐름 {WINDOW}거래일 미만"]
    why = []
    if c["normalized_delta"] <= 0:
        why.append(f"{WINDOW}일 흐름 ≤ 0(순매수 아님)")
    if c["bull_ratio"] < BULL_MIN:
        why.append(f"매수우위일 비율 < {BULL_MIN*100:.0f}%")
    if not r["price"].get("above_ma20"):
        why.append(f"종가 ≤ MA{r['price'].get('ma_days', MA_DAYS)}")
    sl, ob, om = (c["obv_normalized_slope"], c["obv"], c["obv_ma20"])
    man = r["price"].get("ma_days", MA_DAYS)
    if sl is None or sl <= 0:
        why.append("OBV 기울기 ≤ 0")
    elif ob is None or om is None or ob <= om:
        why.append(f"OBV ≤ OBV MA{man}")
    if (r["liquidity"] or {}).get("value") is None:
        why.append("유동성 데이터 없음")
    return ("BUY_CANDIDATE" if not why else "WATCH"), why


#: 배점은 accel-1.0의 60/20/20을 그대로 옮겼다 — **검증 전 설계값**이다.
SCORE_WEIGHTS = (60, 20, 20)


def finalize(rows: list, design: str) -> list:
    """게이트·점수. 두 설계가 같은 60/20/20 배점을 쓰되 주지표만 다르다."""
    gate = gate_accel if design == "accel" else gate_cvdobv
    out = []
    for r in rows:
        x = dict(r)
        x["status"], x["gate_reasons"] = gate(r)
        out.append(x)

    def main_metric(x):
        if design == "accel":
            return x["accel"]["accel"] if x.get("accel") else None
        return x["cvdobv"]["normalized_delta"] if x.get("cvdobv") else None

    def second_metric(x):
        if design == "accel":
            return x["accel"]["flow_recent"] if x.get("accel") else None
        return x["cvdobv"]["bull_ratio"] if x.get("cvdobv") else None

    mp = ac._percentiles({x["code"]: main_metric(x) for x in out})
    sp = ac._percentiles({x["code"]: second_metric(x) for x in out})
    lp = ac._percentiles({x["code"]: (x.get("liquidity") or {}).get("value")
                          for x in out})
    w1, w2, w3 = SCORE_WEIGHTS
    for x in out:
        c = x["code"]
        x["main_percentile"] = mp[c]
        x["second_percentile"] = sp[c]
        x["liquidity_percentile"] = lp[c]
        x["total"] = round((mp[c] or 0) * w1 + (sp[c] or 0) * w2
                           + (lp[c] or 0) * w3, 1)
    order = {"BUY_CANDIDATE": 0, "WATCH": 1, "DATA_INSUFFICIENT": 2}
    out.sort(key=lambda x: (order.get(x["status"], 9), -x["total"],
                            -((x.get("liquidity") or {}).get("value") or 0),
                            x["code"]))
    for i, x in enumerate(out, 1):
        x["rank"] = i
    return out


# ─────────────────────────────────────────────────────────────
# 출력
# ─────────────────────────────────────────────────────────────

def print_design(rows: list, design: str, top: int, ma_days: int = MA_DAYS):
    cand = [r for r in rows if r["status"] == "BUY_CANDIDATE"]
    watch = [r for r in rows if r["status"] == "WATCH"]
    insuf = [r for r in rows if r["status"] == "DATA_INSUFFICIENT"]
    title = ("가속 — 흐름이 빨라지는 종목" if design == "accel"
             else "일반 — 흐름·추세가 함께 선 종목(V1.5 일별 재현판)")
    print()
    print(f"  ── {design}  {title}")
    print(f"     판정: 🟩후보 {len(cand)}  🟨관찰 {len(watch)}  ⬜부족 {len(insuf)}")
    if not cand:
        print("     후보 없음")
    else:
        if design == "accel":
            hdr = (f"  {'순위':>4} {'종목':<22}{'점수':>7}{'가속A':>10}"
                   f"{f'최근{RECENT_DAYS}':>10}{f'이전{PRIOR_DAYS}':>10}"
                   f"{f'MA{ma_days}이격':>10}{'거래대금':>12}")
        else:
            hdr = (f"  {'순위':>4} {'종목':<22}{'점수':>7}{f'{WINDOW}일흐름':>10}"
                   f"{'매수우위':>10}{'OBV기울기':>11}"
                   f"{f'MA{ma_days}이격':>10}{'거래대금':>12}")
        print(hdr)
        print("  " + "-" * (len(hdr) + 6))
        for r in cand[:top]:
            cvm = r["price"]["close_vs_ma20_pct"]
            liq = (r.get("liquidity") or {}).get("value") or 0
            cvs = f"{cvm:+.1f}%" if cvm is not None else "—"
            if design == "accel":
                a = r["accel"]
                print(f"  {r['rank']:>4} {r['code']} {r['name'][:14]:<15}"
                      f"{r['total']:>7.1f}{a['accel']:>+10.4f}"
                      f"{a['flow_recent']:>+10.4f}{a['flow_prior']:>+10.4f}"
                      f"{cvs:>10}{liq/1e8:>11,.0f}억")
            else:
                c = r["cvdobv"]
                print(f"  {r['rank']:>4} {r['code']} {r['name'][:14]:<15}"
                      f"{r['total']:>7.1f}{c['normalized_delta']:>+10.4f}"
                      f"{c['bull_ratio']*100:>9.0f}%"
                      f"{c['obv_normalized_slope']:>+11.4f}"
                      f"{cvs:>10}{liq/1e8:>11,.0f}억")
    import collections
    why = collections.Counter()
    for r in rows:
        if r["status"] != "BUY_CANDIDATE":
            for w in r["gate_reasons"]:
                why["과열" if w.startswith("과열") else w] += 1
    if why:
        print("     비후보 사유:")
        for k, v in why.most_common(6):
            print(f"       {v:>4}건  {k}")
    return cand, watch, insuf


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--as-of", default=None, help="YYYY-MM-DD (기본 최근 거래일)")
    ap.add_argument("--design", default="both", choices=("both",) + DESIGNS)
    ap.add_argument("--ma-days", type=int, default=MA_DAYS,
                    help=f"이동평균 길이(기본 {MA_DAYS}). 바꾸면 검증된 기본값이 "
                         f"아니다 — 사전등록 MALEN(대장 11번)에서 10·60은 "
                         f"20을 이기지 못했다")
    ap.add_argument("--no-live-today", action="store_true",
                    help="오늘치 API 호출 없이 저장분만 쓴다")
    ap.add_argument("--save-json", action="store_true")
    a = ap.parse_args()

    print()
    print("=" * 108)
    print(f"  CVD/OBV 스캔 — 체결량 통합 기준(UN) · 창 {WINDOW}거래일 "
          f"(설계 {DESIGN_VERSION})")
    print("=" * 108)
    print("  검증 상태 — 사전등록 WINDOW15(대장 10번, 16.4년 4,015거래일)")
    print(f"    · 창 {WINDOW} > 창 10: 가속 창차 +0.0269%p [+0.0091, +0.0438] "
          "q=0.0070")
    print("    · 창 15의 가속은 유니버스를 이긴다 +0.0290%p q=0.0338 "
          "(창 10은 못 이겼다)")
    print("    ⚠ 그러나 지수는 못 이긴다 −0.4035%p(유의 음수), 이긴 날 32.8%")
    print("    ⚠ 평균 순수익 −0.42%(비용 후) — 위는 전부 상대 비교다")
    print("    ⚠ 효과가 2010-2017에 몰려 있고, 선행성은 약해진다(+1.40%→+2.42%)")
    print("    ⚠ 통합 기준(UN)에서는 재현 못 했다 — 이 스캐너가 쓰는 기준이다")
    print("    · 일반(OBV) 설계의 창차는 UNDERPOWERED — 창 15 근거 없음")
    print(f"    · MA{MA_DAYS}는 10·60과 비교해 바꿀 근거 없음(사전등록 MALEN, "
          "대장 11번)")
    print("    단독 매매 근거로 쓰지 말 것.")
    print("=" * 108)

    if a.ma_days != MA_DAYS:
        print()
        print(f"  ⚠ MA 길이를 {MA_DAYS} → {a.ma_days}로 바꿔 돌린다 — "
              f"**검증된 기본값이 아니다.**")
        print(f"    사전등록 MALEN(대장 11번, 16.4년 4,015거래일)에서 "
              f"MA10·MA60은 MA20을 이기지 못했다.")
        print(f"    유일하게 0을 벗어난 짝차는 **가속 MA10 − MA20 = "
              f"−0.0094%p [−0.0167, −0.0020]** 로 MA20이 나은 쪽이다.")
        print(f"    근거: reports/MALEN-이동평균길이-10-20-60-비교-20261021.md")

    as_of = date.fromisoformat(a.as_of) if a.as_of else latest_trading_day()
    print()
    print(f"  기준 거래일 {as_of} · MA{a.ma_days} · 흐름 창 {WINDOW}거래일")
    built = build_rows(as_of, not a.no_live_today, ma_days=a.ma_days)

    designs = DESIGNS if a.design == "both" else (a.design,)
    results, counts = {}, {}
    for d in designs:
        rows = finalize(built["rows"], d)
        cand, watch, insuf = print_design(rows, d, a.top, a.ma_days)
        results[d] = rows
        counts[d] = {"scanned": len(rows), "candidates": len(cand),
                     "watch": len(watch), "insufficient": len(insuf)}

    if len(designs) == 2:
        ca = {r["code"] for r in results["accel"]
              if r["status"] == "BUY_CANDIDATE"}
        cb = {r["code"] for r in results["cvdobv"]
              if r["status"] == "BUY_CANDIDATE"}
        both = ca & cb
        print()
        print(f"  ── 두 설계 교집합 {len(both)}종목 "
              f"(가속만 {len(ca - cb)} · 일반만 {len(cb - ca)})")
        if both:
            by_rank = sorted(both, key=lambda c: next(
                r["rank"] for r in results["accel"] if r["code"] == c))
            name = {r["code"]: r["name"] for r in results["accel"]}
            print("     " + ", ".join(f"{c} {name.get(c, '')}"
                                      for c in by_rank[:12]))
        counts["both"] = len(both)

    if a.save_json:
        os.makedirs(OUT_DIR, exist_ok=True)
        run = f"{datetime.now():%Y%m%d_%H%M%S}-{uuid.uuid4().hex[:8]}"
        suffix = "" if a.ma_days == MA_DAYS else f"-ma{a.ma_days}"
        p = os.path.join(OUT_DIR,
                         f"cvd-obv-sides-{as_of:%Y%m%d}{suffix}-{run}.json")
        payload = {
            "design_version": DESIGN_VERSION,
            "basis": BASIS_VERSION,
            "basis_note": ("체결량 = KIS FHKST03010800 통합 기준(UN = KRX+NXT). "
                           "일봉 거래량도 2026-09-14 이후 NXT 메인마켓을 "
                           "포함하므로 분자·분모의 시장이 같다"),
            "window": {"total": WINDOW, "recent": RECENT_DAYS,
                       "prior": PRIOR_DAYS, "bull_min": BULL_MIN,
                       "overheat_max_pct": OVERHEAT_MAX,
                       "ma_days": built["ma_days"],
                       "ma_is_default": built["ma_days"] == MA_DAYS,
                       "liq_days": LIQ_DAYS},
            "score_weights": {"main": SCORE_WEIGHTS[0],
                              "second": SCORE_WEIGHTS[1],
                              "liquidity": SCORE_WEIGHTS[2],
                              "note": "accel-1.0의 60/20/20 — 검증 전 설계값"},
            "validation": {
                "status": "PARTIAL",
                "prereg": "docs/PREREGISTRATION_WINDOW15.md (대장 10번)",
                "window_15_vs_10": ("accel 창차 +0.0269%p [+0.0091,+0.0438] "
                                    "q=0.0070 SIGNIFICANT_EDGE (J, 16.4년, "
                                    "4,015거래일). v15daily 창차는 UNDERPOWERED"),
                "vs_universe": "accel(창15) +0.0290%p q=0.0338 SIGNIFICANT_EDGE",
                "vs_index": ("accel(창15) -0.4035%p SIGNIFICANT_NEGATIVE — "
                             "지수는 못 이긴다"),
                "mean_net_return": "-0.4217% (h=2, 비용 후) — 절대 수익은 음수",
                "ma_length": ("MA20 유지 — 10·60 대비 짝차 전부 불가, "
                              "가속 MA10−MA20만 −0.0094%p로 MA20 우위 "
                              "(사전등록 MALEN, 대장 11번)"),
                "caveats": [
                    "효과가 2010-2017에 몰려 있다(2018-2026은 0을 포함)",
                    "선행성 약화: 신호 전 상승 +1.40% → +2.42%",
                    "통합 기준(UN) 362거래일에서는 재현 못 함(MDE 0.138%p)",
                    "검정은 J 패널 — '기준은 창 효과에 영향 없다'는 미검증 가정"],
                "basis_change": ("NXTBASIS(대장 9번) H1 +0.0363%p "
                                 "[-0.0090,+0.0797] UNDERPOWERED"),
                "reports": ["reports/WINDOW15-창10-창15-비교-20261021.md",
                            "reports/MALEN-이동평균길이-10-20-60-비교-20261021.md",
                            "reports/NXTBASIS-KRX단독-통합기준-비교-20261019.md",
                            "reports/TRUECVD-체결량-스캐너-시뮬레이션-20261019.md"]},
            "as_of": str(as_of),
            "stored_last_trade_date": str(built["stored_last"]),
            "today_from_api": built["live_count"],
            "provisional_flow_rows": built["provisional_rows"],
            "nxt_volume_bar_rows": built["nxt_volume_rows"],
            "universe_standard": built["universe"].describe(),
            "counts": counts,
            "results": {d: results[d] for d in designs},
        }
        with open(p, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1, default=str)
        print(f"\n  💾 저장: {os.path.relpath(p, _BASE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

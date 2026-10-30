#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CVD 가속 스캔 — 흐름을 **공급자 체결량**으로 바꾼 판 (설계 `accel-truecvd-0.1`)

accel-1.0과 **게이트·배점은 같고 흐름 원천만 다르다.**
  accel-1.0      f = 1분봉 캔들에서 추정 (0.6·CLV + 0.4·Body)
  이 도구         f = (매수체결량 − 매도체결량) / (매수체결량 + 매도체결량)
                     KIS FHKST03010800, `silver.daily_trade_sides`

⚠ **이것은 accel-1.0이 아니다.** 두 흐름의 상관은 +0.63(R² 0.40)이고 게이트
판정 일치율은 74.4%다 — 다른 리스트가 나온다. 그리고 사전등록 백테스트
(D4, 16.4년, 4,011거래일)에서 이 설계의 유니버스 대비 초과수익은
**+0.0015%p [−0.022, +0.024]** 로 **0과 구별되지 않았다**. 지수 대비는
**−0.436%p**로 유의하게 음수였다.
근거: `reports/TRUECVD-체결량-스캐너-시뮬레이션-20261019.md`

재사용(정의를 두 벌 만들지 않는다):
  · `cvd_accel_scanner.compute_gate`·`_percentiles`·`RECENT_DAYS`·`OVERHEAT_MAX`
  · `accel_vidya_backtest.accel_from_flow`·`rolling_mean`
  · `cvd_obv.ingestion.kis_trade_sides.read_trade_sides`
  · `cvd_obv.bar_standard.read_standard_daily`, `cvd_obv.universe`

    python tools/cvd_accel_truecvd_scan.py --top 15 --save-json
    python tools/cvd_accel_truecvd_scan.py --no-live-today     # 저장분만
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

from cvd_obv.bar_standard import read_standard_daily  # noqa: E402
from cvd_obv.contracts.market_schema import TRADE_SIDE_ZERO_FLAG  # noqa: E402
from cvd_obv.ingestion.kis_trade_sides import read_trade_sides  # noqa: E402
from cvd_obv.universe import current_universe  # noqa: E402

DESIGN_VERSION = "accel-truecvd-0.1"
#: 시장 기준 — 기본은 `J`(KRX 단독). `UN`은 `J + NX`를 합산한 참고용이다.
#: `PREREGISTRATION_NXTBASIS.md` §4: 기본 기준은 바꾸지 않는다.
BASES = ("J", "UN")
OUT_DIR = os.path.join(_BASE, "reports", "daily-scan")
MA_DAYS = 20
LIQ_DAYS = 20
NEED_DAYS = ac.RECENT_DAYS + ac.PRIOR_DAYS       # 10거래일 연속
LOOKBACK_DAYS = 120                              # 일봉 워밍업(MA20·거래대금)
_KIS_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-trade-volume"
_KIS_TR = "FHKST03010800"


def fetch_today(codes, day: date, log=print, market_div: str = "J") -> dict:
    """오늘치 체결량을 API에서 바로 받는다(저장 테이블에는 쓰지 않는다).

    저장 규칙이 "당일 행은 쓰지 않는다"이므로(장중 누적값이 온다) 스캔에서만
    쓰고 테이블은 건드리지 않는다. 정규장이 끝난 뒤에 부르는 것을 전제한다 —
    16:25 실측에서 3회 호출 합계 변동이 +88주(0.0006%)였다.
    """
    sys.path.insert(0, os.path.join(_BASE, "tools"))
    import daily_scanner as dsc
    for line in open(os.path.join(_BASE, ".env"), encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    s = day.strftime("%Y%m%d")
    out, fails = {}, 0
    t0 = time.monotonic()
    for i, c in enumerate(codes, 1):
        r = dsc._kis_request(_KIS_PATH, {
            "FID_COND_MRKT_DIV_CODE": market_div, "FID_INPUT_ISCD": c,
            "FID_PERIOD_DIV_CODE": "D",
            "FID_INPUT_DATE_1": s, "FID_INPUT_DATE_2": s}, _KIS_TR)
        hit = None
        for x in (r or {}).get("output2", []):
            if str(x.get("stck_bsop_date")) == s:
                b, sl = int(x["total_shnu_qty"] or 0), int(x["total_seln_qty"] or 0)
                if b + sl > 0:
                    hit = (b, sl)
                break
        if hit:
            out[c] = hit
        else:
            fails += 1
        if i % 100 == 0:
            log(f"    {i}/{len(codes)} ({time.monotonic()-t0:.0f}초)")
    log(f"  오늘({day}) 체결량[{market_div}] {len(out)}종목 수신 · 없음 "
        f"{fails}종목 ({time.monotonic()-t0:.0f}초)")
    return out


def read_stored_flows(sids, start: date, as_of: date, market_div: str) -> dict:
    """{코드: {날짜: (매수, 매도)}} — 한 시장 구분만. 흐름 없는 행은 뺀다."""
    import pyarrow.dataset as ds
    t = read_trade_sides(
        columns=["security_id", "trading_date", "buy_qty", "sell_qty",
                 "quality_flags", "market_div"],
        filters=(ds.field("trading_date") >= start)
        & (ds.field("trading_date") <= as_of)
        & (ds.field("market_div") == market_div)
        & ds.field("security_id").isin(sids))
    out = {}
    for sid, d, b, sl, fl in zip(
            t.column("security_id").to_pylist(),
            t.column("trading_date").to_pylist(),
            t.column("buy_qty").to_pylist(),
            t.column("sell_qty").to_pylist(),
            t.column("quality_flags").to_pylist()):
        if TRADE_SIDE_ZERO_FLAG in (fl or []) or (b + sl) <= 0:
            continue
        out.setdefault(sid.split(":")[1], {})[d] = (int(b), int(sl))
    return out


def add_nx(flows: dict, nx: dict) -> dict:
    """UN = J + NX. **J 행이 있는 (종목, 날)만** 남긴다.

    `PREREGISTRATION_NXTBASIS.md` §1.1과 같은 규칙이다 — NX가 없으면 그날
    NXT 체결이 없는 것으로 보아 더하지 않는다(그 날은 f_UN = f_J).
    `nxtbasis_backtest.make_un_sides`와 **같은 처리여야 한다.**
    """
    hit = 0
    for c, per in flows.items():
        nd = nx.get(c, {})
        for d, (b, sl) in list(per.items()):
            nb, ns = nd.get(d, (0, 0))
            if (nb + ns) > 0:
                per[d] = (b + nb, sl + ns)
                hit += 1
    return {"days_with_nx": hit}


def build_rows(as_of: date, live_today: bool, log=print,
               basis: str = "J") -> dict:
    import pyarrow.dataset as ds

    u = current_universe()
    sids = sorted(u.security_ids())
    codes = [s.split(":")[1] for s in sids]
    log(f"  유니버스 {len(codes)}종목 — {u.describe()['sources']['KOSPI200']}")
    for w in (u.warnings or []):
        log(f"  ⚠ [유니버스] {w}")

    start = as_of - timedelta(days=LOOKBACK_DAYS)

    # ── 체결량(저장분) ──
    flows = read_stored_flows(sids, start, as_of, "J")
    stored_last = max((d for per in flows.values() for d in per), default=None)
    log(f"  저장 체결량[J] 마지막 거래일 {stored_last}")

    nxstat = None
    if basis == "UN":
        nx = read_stored_flows(sids, start, as_of, "NX")
        nx_last = max((d for per in nx.values() for d in per), default=None)
        log(f"  저장 체결량[NX] 마지막 거래일 {nx_last} · {len(nx)}종목")
        if nx_last is None:
            raise SystemExit("NX 체결량이 없다 — tools/collect_trade_sides.py "
                             "--market-div NX 로 먼저 수집하라")
        nxstat = add_nx(flows, nx)
        log(f"  UN 합산: NX가 있는 (종목,날) {nxstat['days_with_nx']:,}쌍")

    live = {}
    if live_today and stored_last and stored_last < as_of:
        log(f"  오늘({as_of}) 체결량은 저장분에 없다 — API에서 받는다")
        live = fetch_today(codes, as_of, log=log)
        for c, v in live.items():
            flows.setdefault(c, {})[as_of] = v
        if basis == "UN":
            live_nx = fetch_today(codes, as_of, log=log, market_div="NX")
            for c, (nb, ns) in live_nx.items():
                if c in live:            # J가 없는 종목은 UN에도 넣지 않는다
                    b, sl = flows[c][as_of]
                    flows[c][as_of] = (b + nb, sl + ns)
            nxstat["live_days_with_nx"] = len(
                [c for c in live_nx if c in live])
            log(f"  오늘치 UN 합산 {nxstat['live_days_with_nx']}종목")

    # ── 일봉 ──
    day = read_standard_daily(
        columns=["security_id", "trading_date", "close", "volume",
                 "traded_value", "source"],
        filters=(ds.field("trading_date") >= start)
        & (ds.field("trading_date") <= as_of)
        & ds.field("security_id").isin(sids),
        allow_extended=False, drop_failed=True, fill_traded_value=True)
    bars = {}
    for sid, d, c, v, tv in zip(
            day.column("security_id").to_pylist(),
            day.column("trading_date").to_pylist(),
            day.column("close").to_pylist(),
            day.column("volume").to_pylist(),
            day.column("traded_value").to_pylist()):
        bars.setdefault(sid.split(":")[1], []).append(
            (d, float(c), int(v), float(tv) if tv is not None else float(c) * v))
    for c in bars:
        bars[c].sort()

    # 거래일 축 = 일봉에서 관측된 날짜 합집합
    cal = sorted({d for rows in bars.values() for d, *_ in rows})
    pos = {d: i for i, d in enumerate(cal)}
    T = len(cal)

    # 종목명 — `Universe.stocks(index_id)`는 지수별로 받는다(인자 필수).
    # 예전 구현은 try/except로 감싸 조용히 비었다(2026-10-19 실측).
    names = {}
    for idx in u.describe()["counts"]:
        for st in u.stocks(idx):
            names[st["code"]] = st.get("name", "")
    if not names:
        raise SystemExit("종목명을 하나도 못 읽었다 — universe 구조를 확인하라")

    rows = []
    for c in codes:
        br = bars.get(c, [])
        if len(br) < MA_DAYS:
            continue
        k = np.array([pos[d] for d, *_ in br])
        close = np.array([r[1] for r in br], float)
        value = np.array([r[3] for r in br], float)
        ma = av.rolling_mean(close, MA_DAYS)
        liq = av.rolling_mean(value, LIQ_DAYS)

        c_cal = np.full(T, np.nan); c_cal[k] = close
        ma_cal = np.full(T, np.nan); ma_cal[k] = ma
        liq_cal = np.full(T, np.nan); liq_cal[k] = liq

        f_cal = np.full(T, np.nan)
        per = flows.get(c, {})
        for d, (b, sl) in per.items():
            i = pos.get(d)
            if i is not None:
                f_cal[i] = (b - sl) / (b + sl)
        a = av.accel_from_flow(f_cal)

        j = pos.get(as_of)
        if j is None:
            continue
        acc = None
        if a["ok"][j] and np.isfinite(a["accel"][j]):
            win = [cal[x] for x in range(j - NEED_DAYS + 1, j + 1)]
            acc = {"accel": float(a["accel"][j]),
                   "flow_recent": float(a["recent"][j]),
                   "flow_prior": float(a["prior"][j]),
                   "window_days": [str(d) for d in win],
                   "flow_as_of": str(as_of),
                   "flow_series": [{"date": str(cal[x]),
                                    "f": float(f_cal[x])}
                                   for x in range(j - NEED_DAYS + 1, j + 1)]}
        cvm = (float(c_cal[j] / ma_cal[j] - 1) * 100
               if np.isfinite(ma_cal[j]) and np.isfinite(c_cal[j]) else None)
        rows.append({
            "code": c, "name": names.get(c, ""),
            # compute_gate가 사유 문구에 쓰는 키 — 스캐너 상수를 그대로 넘긴다
            "recent_days": ac.RECENT_DAYS, "prior_days": ac.PRIOR_DAYS,
            "period": None, "accel": acc,
            # 일별 체결량은 품질 판정 대상이 아니다 — 흐름이 10일 연속 있으면 SUCCESS
            "cvd_quality": {"data_status": "SUCCESS" if acc else "NO_FLOW"},
            "price": {"close": (float(c_cal[j]) if np.isfinite(c_cal[j]) else None),
                      "close_vs_ma20_pct": (round(cvm, 1) if cvm is not None else None),
                      "above_ma20": (bool(cvm > 0) if cvm is not None else None)},
            "liquidity": {"value": (float(liq_cal[j])
                                    if np.isfinite(liq_cal[j]) else None),
                          "days_used": LIQ_DAYS},
            "flow_today_from_api": c in live,
        })
    return {"rows": rows, "universe": u, "cal": cal, "as_of": as_of,
            "stored_last": stored_last, "live_count": len(live),
            "basis": basis, "nx": nxstat}


def finalize(rows: list) -> list:
    """게이트·점수 — 스캐너 함수를 그대로 쓴다(배점 60/20/20)."""
    for r in rows:
        r.update(ac.compute_gate(r))
    a_map = {r["code"]: (r["accel"]["accel"] if r.get("accel") else None)
             for r in rows}
    f_map = {r["code"]: (r["accel"]["flow_recent"] if r.get("accel") else None)
             for r in rows}
    l_map = {r["code"]: (r.get("liquidity") or {}).get("value") for r in rows}
    ap, fp, lp = (ac._percentiles(a_map), ac._percentiles(f_map),
                  ac._percentiles(l_map))
    for r in rows:
        c = r["code"]
        r["accel_percentile"] = ap[c]
        r["flow_percentile"] = fp[c]
        r["liquidity_percentile"] = lp[c]
        r["total"] = round((ap[c] or 0) * 60 + (fp[c] or 0) * 20
                           + (lp[c] or 0) * 20, 1)
    order = {"BUY_CANDIDATE": 0, "WATCH": 1, "DATA_INSUFFICIENT": 2}
    rows.sort(key=lambda r: (order.get(r["status"], 9), -r["total"],
                             -((r.get("liquidity") or {}).get("value") or 0),
                             r["code"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def main() -> int:
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--top", type=int, default=15)
    ap_.add_argument("--as-of", default=None, help="YYYY-MM-DD (기본 최근 거래일)")
    ap_.add_argument("--no-live-today", action="store_true",
                     help="오늘치 API 호출 없이 저장분만 쓴다")
    ap_.add_argument("--basis", default="J", choices=BASES,
                     help="체결량 시장 기준 — J(KRX 단독, 기본) / UN(J+NX 합산, 참고)")
    ap_.add_argument("--save-json", action="store_true")
    a = ap_.parse_args()

    print()
    print("=" * 104)
    print(f"  CVD 가속 스캔 — **체결량 기준** (설계 {DESIGN_VERSION})")
    print("=" * 104)
    print("  ⚠ 이것은 accel-1.0이 아니다 — 흐름 원천만 다른 별도 설계다.")
    print("    사전등록 백테스트(D4, 16.4년 4,011거래일): 유니버스 대비")
    print("    **+0.0015%p [−0.022, +0.024] — 0과 구별되지 않는다.**")
    print("    지수 대비 **−0.436%p**(유의 음수), 지수를 이긴 날 31.6%.")
    print("    선행성은 재현된다(신호 전 상승폭 +1.41%, 상승 비율 61.4%).")
    print("    단독 매매 근거로 쓰지 말 것.")
    print("=" * 104)

    as_of = (date.fromisoformat(a.as_of) if a.as_of else None)
    if as_of is None:
        # 달력(`silver.market_calendar`)은 `calendar build`를 따로 돌려야 늘어난다
        # — 적재 파이프라인에 없어서 밀리기 쉽다(2026-10-19 실측: 달력 10-12,
        # 일봉 10-19). 그래서 **기준 일봉에서** 최근 거래일을 찾는다.
        import pyarrow.dataset as ds
        t = read_standard_daily(
            columns=["security_id", "trading_date"],
            filters=(ds.field("trading_date") > date.today() - timedelta(days=30))
            & (ds.field("trading_date") <= date.today()),
            allow_extended=False, drop_failed=False)
        days = sorted(set(t.column("trading_date").to_pylist()))
        if not days:
            raise SystemExit("최근 30일 기준 일봉이 없다 — 적재 상태를 확인하라")
        as_of = days[-1]
    print(f"  기준 거래일 {as_of}")
    if a.basis == "UN":
        print("  시장 기준 **UN (KRX + NXT)** — 참고용. 사전등록 기본은 J다.")
        print("    일봉(거래량·MA20·거래대금)은 KRX 단독 기준 그대로다 —")
        print("    흐름만 통합 기준으로 바꾼 것이다.")
    else:
        print("  시장 기준 J (KRX 단독)")

    built = build_rows(as_of, not a.no_live_today, basis=a.basis)
    rows = finalize(built["rows"])
    cand = [r for r in rows if r["status"] == "BUY_CANDIDATE"]
    watch = [r for r in rows if r["status"] == "WATCH"]
    insuf = [r for r in rows if r["status"] == "DATA_INSUFFICIENT"]

    print()
    print(f"  판정: 🟩후보 {len(cand)}  🟨관찰 {len(watch)}  ⬜부족 {len(insuf)}")
    print(f"  흐름 기준일 {as_of}"
          + (f" (오늘치 {built['live_count']}종목은 API 직접 수신 — 저장 테이블"
             f"에는 쓰지 않았다)" if built["live_count"] else
             f" (저장분만, 마지막 저장일 {built['stored_last']})"))
    print()
    hdr = (f"  {'순위':>4} {'종목':<22}{'점수':>7}{'가속A':>10}{'최근':>10}"
           f"{'이전':>10}{'MA20이격':>10}{'거래대금':>12}")
    print(hdr)
    print("  " + "-" * 92)
    for r in cand[:a.top]:
        acc = r["accel"]
        cvm = r["price"]["close_vs_ma20_pct"]
        liq = (r.get("liquidity") or {}).get("value") or 0
        print(f"  {r['rank']:>4} {r['code']} {r['name'][:14]:<15}"
              f"{r['total']:>7.1f}{acc['accel']:>+10.4f}"
              f"{acc['flow_recent']:>+10.4f}{acc['flow_prior']:>+10.4f}"
              f"{(f'{cvm:+.1f}%' if cvm is not None else '—'):>10}"
              f"{liq/1e8:>11,.0f}억")

    import collections
    why = collections.Counter()
    for r in rows:
        if r["status"] != "BUY_CANDIDATE":
            for w in r["gate_reasons"]:
                why["과열" if w.startswith("과열") else w] += 1
    print()
    print("  비후보 사유:")
    for k, v in why.most_common(6):
        print(f"    {v:>4}건  {k}")
    below = [r for r in cand if (r["price"]["close_vs_ma20_pct"] or 0) < 0]
    if cand:
        print(f"  후보 {len(cand)} 중 MA20 아래 {len(below)} "
              f"({len(below)/len(cand)*100:.1f}%)")

    if a.save_json:
        os.makedirs(OUT_DIR, exist_ok=True)
        run = f"{datetime.now():%Y%m%d_%H%M%S}-{uuid.uuid4().hex[:8]}"
        p = os.path.join(OUT_DIR, f"cvd-accel-truecvd-{as_of:%Y%m%d}-{run}.json")
        payload = {
            "design_version": DESIGN_VERSION,
            # 기준을 적지 않으면 어느 시장으로 돌린 리스트인지 알 수 없다
            # (`PREREGISTRATION_NXTBASIS.md` §4)
            "basis": built["basis"],
            "basis_note": ("J = KRX 단독(기본) / UN = J + NX 합산(참고). "
                           "UN이어도 일봉·거래대금·MA20은 KRX 단독 기준이다"),
            "nx_merge": built["nx"],
            "flow_source": "KIS FHKST03010800 (silver.daily_trade_sides)",
            "not_accel_1_0": ("게이트·배점은 같고 흐름 원천이 다르다. 두 흐름 "
                              "상관 +0.63, 게이트 일치 74.4%"),
            "validation": {
                "prereg": "docs/PREREGISTRATION_TRUECVD.md",
                "report": "reports/TRUECVD-체결량-스캐너-시뮬레이션-20261019.md",
                "H1_vs_universe_h2": "+0.0015%p [-0.0219, +0.0241] UNDERPOWERED",
                "H2_vs_index_h2": "-0.4360%p SIGNIFICANT_NEGATIVE"},
            "as_of": str(as_of),
            "stored_last_trade_date": str(built["stored_last"]),
            "today_from_api": built["live_count"],
            "universe_standard": built["universe"].describe(),
            "counts": {"scanned": len(rows), "candidates": len(cand),
                       "watch": len(watch), "insufficient": len(insuf)},
            "results": rows,
        }
        with open(p, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1, default=str)
        print(f"\n  💾 저장: {os.path.relpath(p, _BASE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

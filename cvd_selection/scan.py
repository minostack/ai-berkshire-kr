#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CVD 종가매수 종목 선정 스캔 (설계 cvdsel-2.0) — 실행 입구.

    python -m cvd_selection.scan                    # 최근 일봉 거래일
    python -m cvd_selection.scan --as-of 2026-10-22 --save
    python -m cvd_selection.scan --show-rejects 20  # 탈락 사유 상세

출력 머리에 항상 '수급 확정 기준일 D-1 / 당일 가격 잠정 기준'을 표시한다(명세 12장).
결과 파일은 실행마다 새 이름으로 쓰고 이전 스냅샷을 덮지 않는다(명세 3.2-5).
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import os
import sys
import uuid
from datetime import date, datetime, timedelta

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "cvd_selection"

from cvd_selection import DESIGN_VERSION  # noqa: E402
from cvd_selection.config import changed_fields, thresholds_from  # noqa: E402
from cvd_selection import data as D  # noqa: E402
from cvd_selection.prereg import PREREG_SHA8, SETS, as_text  # noqa: E402
from cvd_selection.selection import DR20_DAYS, Inputs, evaluate, rank  # noqa: E402

from cvd_obv.bar_standard import BAR_STANDARD_VERSION  # noqa: E402
from cvd_obv.domain.ids import KST  # noqa: E402
from cvd_obv.sides_flow import BASIS_VERSION  # noqa: E402
from cvd_obv.universe import universe_on  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

#: 데이터 범위 — 명세와 다른 점을 결과에 그대로 남긴다
DATA_SCOPE = {
    "session_scope": ("가격 = KRX 정규장(bars-1.0). 거래량 = KRX 정규장 + 장후 시간외 "
                      "종가(평균 0.08%) + 2026-08-03 이후 NXT 메인마켓"),
    "flow_scope": ("KIS FHKST03010800 일별 매수·매도 체결량, 통합 기준 UN(KRX+NXT, "
                   "sides-1.0). 세션 분리 불가 — 프리·애프터 체결 포함"),
    "classification_method": ("공급자 분류(매수 주도/매도 주도 체결 추정). 미분류량 U "
                              "미제공 → Vflow = B + S, 미분류율 5% 기준 미적용"),
    "vwap_method": ("Σ거래대금 / Σ거래량 — KRX 09:00~15:30 + NXT 메인 1분봉. 봉 거래대금 = "
                    "KIS 누적거래대금(acml_tr_pbmn) 직전 봉 차이. 대금 없는 봉은 분자·분모에서 제외, 그 거래량이 0.1% 넘으면 계산하지 않음. "
                    "단 누적거래대금이 없는 2025-10-15 전 날의 사후 실행은 대표가격 (H+L+C)/3 "
                    "가중 추정(vwap_method=ESTIMATED_TYPICAL_PRICE)"),
    "volume_basis": ("RVOL·AVG_A20 = 1분봉 범위(KRX 애프터마켓 제외 + 2026-08-03부터 NXT 메인) "
                     "거래량·거래대금, 당일·과거 20일 같은 정의. 일봉에만 있는 물량(블록딜 추정)은 "
                     "제외(명세 3.1). 거래량 = min(일봉, 1분봉 합). 거래대금 = 1분봉 누적거래대금 "
                     "차이 합, 장후 시간외 종가 봉은 거래량 × 종가. 대금 없는 봉은 (H+L+C)/3 추정 — "
                     "2025-10-15 전 날은 전부 추정(avg_a20_est)"),
    "not_implemented": ["관리종목·투자경고 상태 확인(데이터 없음)",
                        "업종 한도(업종 데이터 없음)",
                        "보유 중복 제외·총 5종목 한도(매매 단계)",
                        "종가매매 주문 가능 여부(매매 단계)",
                        "체결량 KRX/NXT 시장별 분리(UN 통합만 매일 수집)"],
}


def run(as_of: date, thresholds, log=print) -> dict:
    t = thresholds
    u = universe_on(as_of)
    sids = sorted(u.security_ids())
    codes = [s.split(":")[1] for s in sids]
    for w in (u.warnings or []):
        log(f"  ⚠ [유니버스] {w}")
    membership = {}
    names = {}
    for m in u.members():
        membership.setdefault(m.code, m.index_id)
        names[m.code] = m.name

    start = as_of - timedelta(days=D.LOOKBACK_DAYS)
    daily = D.load_daily(sids, start, as_of)
    cal = D.trading_calendar(daily, as_of)
    if not cal or cal[-1] != as_of:
        raise SystemExit(f"{as_of} 일봉이 없다 — 휴장일이거나 16:10 잠정 적재 전이다")
    di = len(cal) - 1
    fdays = cal[max(0, di - t.flow_days):di]
    # 체결량은 DR5 창(5일)과 보조 DR20 창(20일, 사전등록 CVDSEL 전향 기록)까지 읽는다
    flows = D.load_flows(sids, cal[max(0, di - DR20_DAYS)], as_of)
    avail = D.load_flow_available_at(sids, fdays)
    minutes = D.load_minutes(codes, as_of)
    vdays = cal[max(0, di - t.vol_days):]
    totals = D.load_minute_day_totals(codes, vdays[0], as_of)
    coverage = D.load_minute_coverage(codes, as_of)

    now = D.now_kst()
    live = as_of == now.date()
    decision_at = now if live else datetime.combine(as_of, datetime.min.time(),
                                                    KST).replace(hour=15, minute=35)
    x = Inputs(day=as_of, calendar=cal, daily=daily, flows=flows,
               flow_available=avail, minutes=minutes, coverage=coverage,
               names=names, membership=membership, decision_at=decision_at,
               live=live, thresholds=t, minute_totals=totals)
    rows = [evaluate(c, x) for c in codes]
    passed = rank(rows, t.daily_pick)

    d_bars = [daily[c][-1] for c in codes if daily.get(c) and daily[c][-1]["date"] == as_of]
    return {
        "rows": rows, "passed": passed, "universe": u, "calendar": cal,
        "flow_days": fdays, "decision_at": decision_at, "live": live,
        "d_bar_count": len(d_bars),
        "d_bar_provisional": sum(1 for b in d_bars if b["provisional"]),
        "minute_codes": len({c for (v, c) in minutes if v == "KRX"}),
        "nxt_minute_codes": len({c for (v, c) in minutes if v == "NXT"}),
    }


# ─────────────────────────────────────────────────────────────
# 출력
# ─────────────────────────────────────────────────────────────

def _pct(v, nd=2):
    return "—" if v is None else f"{v*100:+.{nd}f}%"


def print_report(res: dict, as_of: date, t, top: int, show_rejects: int):
    rows, passed = res["rows"], res["passed"]
    fd = res["flow_days"]
    print()
    print("=" * 112)
    print(f"  CVD 종가매수 종목 선정 — 설계 {DESIGN_VERSION} · 기준 거래일 D = {as_of}")
    print(f"  수급 확정 기준일 D-1 = {fd[-1]}  (창 {fd[0]}~{fd[-1]}, {len(fd)}거래일)"
          f"  /  당일 가격 기준: {as_of} 일봉 "
          f"{'잠정(16:10 1분봉 합)' if res['d_bar_provisional'] else '확정'} "
          f"{res['d_bar_provisional']}/{res['d_bar_count']}종목 잠정")
    print(f"  선정 시각 {res['decision_at']:%Y-%m-%d %H:%M} KST "
          f"({'실시간' if res['live'] else '사후 재현 — 15:35 가정'})"
          f" · 1분봉 KRX {res['minute_codes']} / NXT {res['nxt_minute_codes']}종목")
    print("  ⚠ 임계값·배점은 명세의 검증용 시작값이다. 총점은 성공 확률이 아니다.")
    print("  ⚠ 체결량은 UN 통합(세션 분리 불가)·미분류량 미제공. VWAP은 1분봉 누적거래대금으로 계산.")
    if not res["live"]:
        print("  ⚠ 사후 실행 — 체결량 수신 시각이 선정 시각 뒤인 행은 표시만 하고 탈락시키지 않는다.")
    n_est = sum(1 for r in rows if r.get("vwap_method") == "ESTIMATED_TYPICAL_PRICE")
    if n_est:
        print(f"  ⚠ VWAP 추정 {n_est}종목 — 1분봉 누적거래대금이 없는 날(2025-10-15 전)이라 "
              "대표가격 (H+L+C)/3 가중으로 계산했다(정확 VWAP 아님)")
    print("=" * 112)

    q = [r for r in rows if r["quality_pass"]]
    print(f"  유니버스 {len(rows)} → 품질 통과 {len(q)} → 필수 조건 통과 {len(passed)}"
          f" → 선정 상위 {min(len(passed), t.daily_pick)}")
    pc = {k: [r for r in q if (r.get("prereg_cvdsel") or {}).get(k)] for k in SETS}
    print("  [사전등록 CVDSEL 전향 기록 — 판정 아님] "
          + " · ".join(f"{k} {len(v)}" for k, v in pc.items())
          + "  (S0 명세 일봉 / S1 추세 완화 / S3 ATRP 해제 / S2 DR5−DR20≥10)")
    for k in ("S0", "S1", "S3"):
        if pc[k]:
            print(f"     {k}: " + ", ".join(f"{r['symbol']} {r['name']}" for r in pc[k][:10]))
    print()
    if not passed:
        print("  필수 조건을 모두 통과한 종목이 없다 — 이날은 신규 매수 없음.")
    else:
        hdr = (f"  {'순위':>4} {'종목':<20}{'점수':>5}{'DR5':>8}{'DR2':>8}{'POS5':>5}"
               f"{'RVOL':>6}{'CLV':>6}{'VWAP이격':>9}{'Rlate':>8}{'Hold':>8}"
               f"{'ATRP':>6}{'돌파':>5}{'거래대금20':>11}")
        print(hdr)
        print("  " + "-" * (len(hdr) + 4))
        for r in passed[:top]:
            mark = "★" if r["picked"] else " "
            bo = "Y" if r["score"]["parts"]["breakout"] == 15 else "-"
            print(f" {mark}{r['rank']:>4} {r['symbol']} {r['name'][:12]:<13}"
                  f"{r['score']['total']:>5}{r['dr5']:>+7.1f}%{r['dr2']:>+7.1f}%"
                  f"{r['pos5']:>5}{r['rvol']:>6.2f}{r['clv']:>6.2f}"
                  f"{_pct(r['gap_vwap']):>9}{_pct(r['rlate']):>8}"
                  f"{_pct(r['hold_close']):>8}{r['atrp14']:>5.1f}%{bo:>5}"
                  f"{r['avg_a20']/1e8:>9,.0f}억")
        print("  ★ = 일별 신규 최대 3종목. 보유 중복·총 5종목·업종 한도는 매매 단계에서 적용(미구현).")

    # 탈락 사유 — 단계별 첫 탈락 + 조건별 전체 건수
    first = collections.Counter()
    every = collections.Counter()
    for r in rows:
        if r["reject_reason"]:
            first[f"[{r['reject_reason'][0]['stage']}] {r['reject_reason'][0]['code']}"] += 1
        for z in r["reject_reason"]:
            every[f"[{z['stage']}] {z['code']}"] += 1
    if every:
        print()
        print("  탈락 사유 (종목 수 — 해당 조건 미충족 전체 / 첫 사유)")
        for k, v in every.most_common():
            print(f"    {v:>4} / {first.get(k, 0):>4}  {k}")
    if show_rejects:
        near = sorted((r for r in rows if r["quality_pass"] and not r["filter_pass"]),
                      key=lambda r: (len(r["reject_reason"]), r["symbol"]))
        print()
        print(f"  근접 탈락 (품질 통과, 미충족 조건 적은 순 {show_rejects}개)")
        for r in near[:show_rejects]:
            why = "; ".join(z["text"] for z in r["reject_reason"])
            print(f"    {r['symbol']} {r['name'][:12]:<13} {why}")


CSV_FIELDS = [
    "trade_date", "symbol", "name", "market_membership", "decision_at",
    "available_at", "data_version", "provisional_flag", "missing_reason",
    "last_confirmed_flow_date", "dr5", "dr2", "pos5", "delta_prev",
    "krx_close", "volume_total", "turnover_total", "volume_basis", "turnover_basis",
    "volume_basis_excluded", "avg_a20", "avg_a20_est", "rvol", "clv", "dr20",
    "prereg_cvdsel",
    "ma20", "ma60", "hh20_prev", "vwap", "vwap_method", "vwap_value", "vwap_volume", "gap_vwap", "atrp14",
    "rlate", "hold_close", "quality_pass", "filter_pass", "reject_reason",
    "score", "rank", "picked", "flags",
]


def save(res: dict, as_of: date, t, out_dir: str) -> tuple:
    os.makedirs(out_dir, exist_ok=True)
    run_id = f"{datetime.now():%Y%m%d_%H%M%S}-{uuid.uuid4().hex[:8]}"
    stem = os.path.join(out_dir, f"cvdsel-{as_of:%Y%m%d}-{run_id}")
    version = f"{DESIGN_VERSION}/{BAR_STANDARD_VERSION}/{BASIS_VERSION}"
    for r in res["rows"]:
        r["decision_at"] = res["decision_at"].isoformat()
        r["universe_asof"] = str(as_of)
        r["data_version"] = version
        r["event_cutoff"] = {"flow_confirmed_through": str(res["flow_days"][-1]),
                             "price_as_of": f"{as_of} 15:30"}
    payload = {
        "design_version": DESIGN_VERSION, "data_version": version, "run_id": run_id,
        "trade_date": str(as_of), "decision_at": res["decision_at"].isoformat(),
        "live": res["live"],
        "label": f"수급 확정 기준일 D-1 = {res['flow_days'][-1]} / 당일 가격 잠정 기준 15:30",
        "flow_days": [str(d) for d in res["flow_days"]],
        "d_bar": {"count": res["d_bar_count"], "provisional": res["d_bar_provisional"]},
        "thresholds": t.to_dict(), "thresholds_changed": changed_fields(t),
        "data_scope": DATA_SCOPE,
        "universe_standard": res["universe"].describe(),
        "counts": {"universe": len(res["rows"]),
                   "quality_pass": sum(r["quality_pass"] for r in res["rows"]),
                   "filter_pass": len(res["passed"]),
                   "picked": sum(r["picked"] for r in res["rows"])},
        "prereg_forward": {
            "prereg": "docs/PREREGISTRATION_CVDSEL.md", "sha8": PREREG_SHA8,
            "note": "전향 기록 — 판정에 쓰지 않는다. 일봉 재현 조건(1분봉 조건 제외), RVOL·AVG_A20은 1분봉 범위",
            "counts": {k: sum(1 for r in res["rows"] if (r.get("prereg_cvdsel") or {}).get(k))
                       for k in SETS},
            "symbols": {k: sorted(r["symbol"] for r in res["rows"]
                                  if (r.get("prereg_cvdsel") or {}).get(k)) for k in SETS},
        },
        "rows": res["rows"],
    }
    with open(stem + ".json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1, default=str)
    with open(stem + ".csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in sorted(res["rows"], key=lambda r: (r["rank"] is None, r["rank"] or 0,
                                                    r["symbol"])):
            out = dict(r)
            out["reject_reason"] = "; ".join(f"{z['stage']}:{z['code']}"
                                             for z in r["reject_reason"])
            out["score"] = r["score"]["total"] if r["score"] else None
            out["flags"] = ";".join(r["flags"])
            out["prereg_cvdsel"] = as_text(r["prereg_cvdsel"]) if r.get("prereg_cvdsel") else None
            w.writerow(out)
    return stem + ".json", stem + ".csv"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="CVD 종가매수 종목 선정 (cvdsel-2.0)")
    ap.add_argument("--as-of", help="YYYY-MM-DD (기본: 최근 일봉 거래일)")
    ap.add_argument("--top", type=int, default=20, help="출력할 통과 종목 수")
    ap.add_argument("--show-rejects", type=int, default=0,
                    help="품질 통과·조건 탈락 종목 N개 사유 출력")
    ap.add_argument("--save", action="store_true",
                    help=f"JSON·CSV 저장({os.path.relpath(OUT_DIR)})")
    ap.add_argument("--out-dir", default=OUT_DIR)
    g = ap.add_argument_group("임계값 덮어쓰기 — 바꾸면 명세 시작값이 아니다")
    g.add_argument("--min-dr5", type=float)
    g.add_argument("--min-rvol", type=float)
    g.add_argument("--min-clv", type=float)
    g.add_argument("--min-avg-value", type=float, help="원 단위(기본 1e10 = 100억)")
    g.add_argument("--max-atrp", type=float)
    a = ap.parse_args(argv)
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")

    t = thresholds_from({"min_dr5": a.min_dr5, "min_rvol": a.min_rvol,
                         "min_clv": a.min_clv, "min_avg_value": a.min_avg_value,
                         "max_atrp": a.max_atrp})
    if changed_fields(t):
        print(f"  ⚠ 임계값 변경: {changed_fields(t)} — 명세 시작값이 아니다")
    as_of = date.fromisoformat(a.as_of) if a.as_of else D.latest_daily_date()
    res = run(as_of, t)
    print_report(res, as_of, t, a.top, a.show_rejects)
    if a.save:
        pj, pc = save(res, as_of, t, a.out_dir)
        print(f"\n  💾 {os.path.relpath(pj)}\n  💾 {os.path.relpath(pc)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

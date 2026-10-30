#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
후보 A2(PEAD, 잠정실적 공시일 기준) 사전등록 백테스트

약속: `docs/PREREGISTRATION_A2.md`. A v1(`ab_backtest_a.py`)과 **이벤트일만**
다르다 — 나머지는 v1의 `run()`을 그대로 쓰고, 이벤트일 훅과 A2 전용 보조
분석만 끼운다.

    python tools/ab_backtest_a2.py
"""

import argparse
import json
import os
import sys
import uuid

import numpy as np

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ab_backtest_a as ba  # noqa: E402
import ab_candidate_scanner as ab  # noqa: E402

PREREG_PATH = os.path.join(_BASE, "docs", "PREREGISTRATION_A2.md")
OUT_DIR = os.path.join(_BASE, "reports", "backtest")
LIST_CACHE_DIR = os.path.join(_BASE, "data", "bronze", "dart_list_I002")

# ── 사전등록 §1 — 규칙 자체는 스캐너(설계 0.2)에 있다. 백테스트와 실거래가
#    같은 함수를 쓰게 하려는 것이다 ──
LIST_BGN, LIST_END = "20160101", "20260922"
PRELIM_KEY = ab.PRELIM_KEY
MONTHLY_MAX = ab.MONTHLY_MAX
is_prelim = ab.is_prelim
quarter_end = ab.quarter_end
monthly_years = ab.monthly_years
prelim_event_date = ab.prelim_event_date


def fetch_prelim_dates(corp_codes, key, refresh=False, log=print) -> dict:
    """백테스트용 — 2016-01-01 ~ 2026-09-22 전체 구간, 캐시가 있으면 다시 묻지 않는다."""
    return ab.dart_prelim_dates(corp_codes, key, LIST_BGN, LIST_END, LIST_CACHE_DIR,
                                max_age_sec=0 if refresh else None, log=log)


# ─────────────────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────────────────

def run(resamples: int, log=print) -> dict:
    key = os.environ.get("DART_API_KEY")
    if not key:
        raise RuntimeError("DART_API_KEY 미설정")
    codes = ab.dart_corp_codes(key)
    from ab_backtest_b import load_universe_intervals
    sids = sorted({s for iv in load_universe_intervals().values() for s in iv})
    ccs = sorted({codes[s.split(":")[-1]] for s in sids if codes.get(s.split(":")[-1])})
    log(f"잠정실적 공시 수집 — {len(ccs)}개 회사")
    prelim = fetch_prelim_dates(ccs, key, log=log)
    flagged = {cc: monthly_years(ds) for cc, ds in prelim.items()}
    reasons = {}

    def event_date(cc, e):
        d, why = prelim_event_date(e["period"], e["rcept_date"], prelim.get(cc, []),
                                   flagged.get(cc, set()))
        reasons[(cc, e["period"])] = why
        return d

    def extra(ctx):
        test, mask_of, events, cal_arr = (ctx["test"], ctx["mask_of"], ctx["events"],
                                          ctx["cal_arr"])
        moved = {(ev[0], ev[1]) for ev in events if ev[5] != ev[4]}
        shifts = [int(np.searchsorted(cal_arr, np.datetime64(ev[4]), side="right")
                      - np.searchsorted(cal_arr, np.datetime64(ev[5]), side="right"))
                  for ev in events if ev[5] != ev[4]]
        sig_moved = [ev for ev in events if ev[2] >= ab.SUE_MIN]
        why_all = list(reasons.values())
        s0 = {
            "events_final": len(events),
            "events_final_moved": len(moved),
            "moved_share": len(moved) / len(events) if events else None,
            "A_signals_moved": sum(1 for ev in sig_moved if ev[5] != ev[4]),
            "A_signals": len(sig_moved),
            "shift_td_quantiles(10,25,50,75,90)":
                [float(x) for x in np.percentile(shifts, [10, 25, 50, 75, 90])] if shifts else None,
            "reasons_all_events": {r: why_all.count(r) for r in set(why_all)},
            "monthly_filer_company_years": sum(len(v) for v in flagged.values()),
        }
        prelim_only = mask_of(lambda s, si, k: s >= ab.SUE_MIN and (si, k) in moved)
        return {"S0_coverage": s0,
                "S3_prelim_only": test(prelim_only, ctx["PRIMARY_H"], "universe", 60)[0]}

    out = ba.run(resamples, log, prereg_path=PREREG_PATH, event_date=event_date,
                 extra=extra)
    # v1 전용 보조(SUE 5분위)는 A2 사전등록에 없다 — 결과에서 뺀다
    out["results"].pop("S3_quintiles", None)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="후보 A2(PEAD·잠정실적일) 사전등록 백테스트")
    ap.add_argument("--resamples", type=int, default=ba.RESAMPLES)
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    out = run(args.resamples)
    if not args.no_save:
        os.makedirs(OUT_DIR, exist_ok=True)
        path = os.path.join(OUT_DIR, f"prereg-a2-{uuid.uuid4().hex[:8]}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2, default=str)
        print(f"저장: {os.path.relpath(path, _BASE)}")
    for k in ("H1", "H2"):
        v = out["results"][k]
        if v.get("ci95") is None:
            print(k, v)
            continue
        print(f"{k}: mean={v['mean']*100:+.3f}%  CI=[{v['ci95'][0]*100:+.3f}, "
              f"{v['ci95'][1]*100:+.3f}]  q={v['q']:.4f}  MDE={v['mde']*100:.3f}%  "
              f"→ {v['verdict']}")
    print(json.dumps(out["results"]["S0_coverage"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

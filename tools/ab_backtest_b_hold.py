#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
후보 B 보유기간 연장(40·60거래일) 사전등록 백테스트

약속: `docs/PREREGISTRATION_B_HOLD.md`. 신호는 `ab_backtest_b.build_panel()` —
B와 같은 코드에서 h·기간(·S2의 억제 길이)만 바꿔 받는다.

    python tools/ab_backtest_b_hold.py
"""

import argparse
import hashlib
import json
import os
import sys
import uuid
from datetime import date, datetime

import numpy as np

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ab_backtest_b as bt  # noqa: E402
import ab_candidate_scanner as ab  # noqa: E402

PREREG_PATH = os.path.join(_BASE, "docs", "PREREGISTRATION_B_HOLD.md")
OUT_DIR = os.path.join(_BASE, "reports", "backtest")

# ── 사전등록 §1·§2 ──
SIGNAL_END = date(2026, 6, 12)
HOLDS = (40, 60)
REF_H = 20
HORIZONS = (REF_H,) + HOLDS
RESAMPLES = 4000
SEED = 20260927
SUBPERIODS = (("2010-2017", date(2010, 1, 1), date(2017, 12, 31)),
              ("2018-2026", date(2018, 1, 1), date(2026, 12, 31)))


def make_test(P, resamples):
    """패널 P 위에서 신호일 계열 검정 — B의 day_series/test와 같은 계산."""
    net, member, idx_ret, in_period = P["net"], P["member"], P["idx_ret"], P["in_period"]
    cm, tax, sl = (P["cost_cfg"][k] for k in ("commission_rate", "tax_rate_sell",
                                              "slippage_rate"))

    def gross(h):
        return (net[h] + 1) * (1 + sl) * (1 + cm) / ((1 - sl) * (1 - cm - tax)) - 1

    def test(mask, h, vs, seed_off, row_filter=None, date_filter=None, use_gross=False):
        r = gross(h) if use_gross else net[h]
        sm = mask & ~np.isnan(r)
        if row_filter is not None:
            sm &= row_filter
        um = member & ~np.isnan(r)
        days = np.flatnonzero(sm.any(0) & in_period)
        if date_filter is not None:
            days = days[date_filter(days)]
        s = np.array([r[sm[:, t], t].mean() for t in days])
        b = (np.array([r[um[:, t], t].mean() for t in days]) if vs == "universe"
             else idx_ret[h][days])
        ok = ~np.isnan(b)
        days, s, b = days[ok], s[ok], b[ok]
        out = bt.summarize(s - b, h, resamples, SEED + seed_off)
        out.update(signal_mean=float(s.mean()) if len(s) else None,
                   base_mean=float(b.mean()) if len(b) else None,
                   n_signals=int(sum(sm[:, t].sum() for t in days)))
        return out, days, s - b

    return test


def run(resamples: int, log=print) -> dict:
    with open(PREREG_PATH, "rb") as f:
        prereg_sha = hashlib.sha256(f.read()).hexdigest()

    log("패널(억제 20) — 주가설")
    P = bt.build_panel(log, horizons=HORIZONS, signal_end=SIGNAL_END)
    test = make_test(P, resamples)
    B = P["sig"]["B"]
    cal, cal_arr = P["cal"], P["cal_arr"]

    res = {}
    fam = {}
    for i, h in enumerate(HOLDS):
        fam[f"H1-{h}"] = test(B, h, "universe", 1 + i)[0]
        fam[f"H2-{h}"] = test(B, h, "index", 3 + i)[0]
    keys = list(fam)
    qs = bt.bh_fdr([fam[k].get("p", 1.0) for k in keys])
    for k, q in zip(keys, qs):
        fam[k]["q"] = q
        fam[k]["verdict"] = bt.verdict(fam[k], q)
    res.update(fam)

    res["S1_ref_h20"] = {"H1": test(B, REF_H, "universe", 10)[0],
                         "H2": test(B, REF_H, "index", 11)[0]}
    res["S3_gross_vs_index"] = {f"h{h}": test(B, h, "index", 20 + h, use_gross=True)[0]
                                for h in HORIZONS}
    s4 = {}
    for i, (name, a, b) in enumerate(SUBPERIODS):
        a64, b64 = np.datetime64(a), np.datetime64(b)
        s4[name] = test(B, 60, "universe", 40 + i,
                        date_filter=lambda d, a64=a64, b64=b64:
                        (cal_arr[d] >= a64) & (cal_arr[d] <= b64))[0]
    for i, idx_id in enumerate(ab.INDEXES):
        s4[idx_id] = test(B, 60, "universe", 50 + i,
                          row_filter=(P["member_of"] == idx_id))[0]
    res["S4_h60"] = s4
    _, d60, x60 = test(B, 60, "universe", 1)
    years = {}
    for t, x in zip(d60, x60):
        years.setdefault(cal[t].year, []).append(x)
    res["S5_years_h60"] = {str(y): {"days": len(v), "mean_excess": float(np.mean(v))}
                           for y, v in sorted(years.items())}

    s2 = {}
    for h in HOLDS:
        log(f"패널(억제 {h}) — S2")
        Ph = bt.build_panel(log, horizons=(h,), signal_end=SIGNAL_END, suppress_td=h)
        th = make_test(Ph, resamples)
        s2[f"suppress{h}_h{h}"] = {"H1": th(Ph["sig"]["B"], h, "universe", 60 + h)[0],
                                   "H2": th(Ph["sig"]["B"], h, "index", 61 + h)[0],
                                   "B_signals": int(Ph["sig"]["B"].sum())}
    res["S2_suppress_eq_h"] = s2

    return {
        "design_version": ab.DESIGN_VERSION,
        "prereg": {"path": "docs/PREREGISTRATION_B_HOLD.md", "sha256": prereg_sha},
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "period": [bt.SIGNAL_START.isoformat(), SIGNAL_END.isoformat()],
        "resamples": resamples, "seed": SEED,
        "counts": {"B_signals": int(B.sum()),
                   **{f"no_label_h{h}": int((B & np.isnan(P["net"][h])).sum())
                      for h in HORIZONS}},
        "results": res,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="B 보유기간 연장 사전등록 백테스트")
    ap.add_argument("--resamples", type=int, default=RESAMPLES)
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    out = run(args.resamples)
    if not args.no_save:
        os.makedirs(OUT_DIR, exist_ok=True)
        path = os.path.join(OUT_DIR, f"prereg-b-hold-{uuid.uuid4().hex[:8]}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2, default=str)
        print(f"저장: {os.path.relpath(path, _BASE)}")
    for k in ("H1-40", "H1-60", "H2-40", "H2-60"):
        v = out["results"][k]
        print(f"{k}: mean={v['mean']*100:+.3f}%  CI=[{v['ci95'][0]*100:+.3f}, "
              f"{v['ci95'][1]*100:+.3f}]  q={v['q']:.4f}  MDE={v['mde']*100:.3f}%  "
              f"→ {v['verdict']}")
    print(json.dumps(out["counts"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

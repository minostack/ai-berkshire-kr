#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A∩B 결합(B 신호 중 최근 양의 서프라이즈) 사전등록 백테스트

약속: `docs/PREREGISTRATION_AB.md`.
- B 신호: `ab_backtest_b.build_panel()` (B와 같은 코드)
- A 조건: `ab_backtest_negfilter.classify()` — 신호일에 이미 접수된 정기보고서 SUE
- H1 통계: `ab_backtest_negfilter.summarize_diff()` (두 집단 차이, 분기 군집)

    python tools/ab_backtest_ab.py
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
import ab_backtest_negfilter as nf  # noqa: E402
import ab_candidate_scanner as ab  # noqa: E402

PREREG_PATH = os.path.join(_BASE, "docs", "PREREGISTRATION_AB.md")
OUT_DIR = os.path.join(_BASE, "reports", "backtest")

# ── 사전등록 §1·§2·§3 ──
TEST_START = date(2017, 1, 1)
SIGNAL_END = date(2026, 6, 12)
POS_SUE = 1.0
RECENCY_TD = 60
HOLDS = (20, 60)
DART_YEARS = range(2016, 2027)
RESAMPLES = 4000
SEED = 20260927
SUBPERIODS = (("2017-2021", date(2017, 1, 1), date(2021, 12, 31)),
              ("2022-2026", date(2022, 1, 1), date(2026, 12, 31)))


def a_flag(c: dict, any_age: bool = False) -> bool:
    """classify() 결과 → A 조건(최근 SUE ≥ 1.0)."""
    sue, age = c.get("sue"), c.get("age_td")
    if sue is None or sue < POS_SUE:
        return False
    return any_age or (age is not None and age <= RECENCY_TD)


def verdict_combo(s: dict, q: float) -> str:
    if s.get("verdict") == "INSUFFICIENT":
        return "INSUFFICIENT"
    lo, hi = s["ci95"]
    if lo > 0 and q <= 0.05:
        return "COMBO_HELPS"
    if hi < 0:
        return "COMBO_HURTS"
    if abs(s["diff"]) < s["mde"]:
        return "UNDERPOWERED"
    return "NO_SIGNIFICANT_EDGE"


def run(resamples: int, log=print) -> dict:
    with open(PREREG_PATH, "rb") as f:
        prereg_sha = hashlib.sha256(f.read()).hexdigest()
    key = os.environ.get("DART_API_KEY")
    if not key:
        raise RuntimeError("DART_API_KEY 미설정")

    P = bt.build_panel(log, horizons=HOLDS, signal_end=SIGNAL_END)
    cal, cal_arr, net, member = P["cal"], P["cal_arr"], P["net"], P["member"]
    idx_ret, sids, in_period = P["idx_ret"], P["sids"], P["in_period"]
    in_test = (cal_arr >= np.datetime64(TEST_START)) & in_period
    b_sig = P["sig"]["B"] & in_test[None, :]

    log("DART 보고서(캐시)")
    codes = ab.dart_corp_codes(key)
    cc_of = {sid: codes.get(sid.split(":")[-1]) for sid in sids}
    reports = ab.dart_fetch_reports(sorted({c for c in cc_of.values() if c}), DART_YEARS,
                                    key, log=log)
    events_of = {cc: ab.quarter_events(r) for cc, r in reports.items()}

    log("A 조건 판정")
    a_mask = np.zeros_like(b_sig)
    a_any_mask = np.zeros_like(b_sig)
    status = {}
    for si, t in zip(*np.nonzero(b_sig)):
        cc = cc_of.get(sids[si])
        c = (nf.classify(events_of.get(cc, []), cal[t], cal_arr) if cc
             else {"status": "NO_EVENT", "sue": None, "age_td": None})
        a_mask[si, t] = a_flag(c)
        a_any_mask[si, t] = a_flag(c, any_age=True)
        key_ = "A_POS" if a_mask[si, t] else c["status"]
        status[key_] = status.get(key_, 0) + 1

    uni = {}
    for h in HOLDS:
        r = net[h]
        um = member & ~np.isnan(r)
        with np.errstate(invalid="ignore"):
            uni[h] = np.where(um.any(0), np.where(um, r, 0).sum(0) / um.sum(0), np.nan)
    quarter_of = np.array([f"{d.year}Q{(d.month - 1) // 3 + 1}" for d in cal])

    def h1_test(h, grp, seed_off, date_filter=None):
        si, t = np.nonzero(b_sig)
        e = net[h][si, t] - uni[h][t]
        ok = ~np.isnan(e)
        if date_filter is not None:
            ok &= date_filter(t)
        return nf.summarize_diff(e[ok], grp[si, t][ok], quarter_of[t][ok], resamples,
                                 SEED + seed_off)

    def day_series(mask, h, vs, seed_off):
        r = net[h]
        sm = mask & ~np.isnan(r)
        days = np.flatnonzero(sm.any(0))
        s = np.array([r[sm[:, t], t].mean() for t in days])
        b = uni[h][days] if vs == "universe" else idx_ret[h][days]
        ok = ~np.isnan(b)
        out = bt.summarize(s[ok] - b[ok], h, resamples, SEED + seed_off)
        out.update(signal_mean=float(s[ok].mean()), base_mean=float(b[ok].mean()),
                   n_signals=int(sum(sm[:, t].sum() for t in days[ok])))
        return out

    log("검정")
    fam = {}
    for i, h in enumerate(HOLDS):
        fam[f"H1-{h}"] = h1_test(h, a_mask, 1 + i)
        fam[f"H2-{h}"] = day_series(b_sig & a_mask, h, "index", 3 + i)
    keys = list(fam)
    qs = bt.bh_fdr([fam[k].get("p", 1.0) for k in keys])
    for k, q in zip(keys, qs):
        fam[k]["q"] = q
        fam[k]["verdict"] = (verdict_combo(fam[k], q) if k.startswith("H1")
                             else bt.verdict(fam[k], q))
    res = dict(fam)
    res["S0_counts"] = {"B_signals": int(b_sig.sum()), "A_and_B": int((b_sig & a_mask).sum()),
                        "status": status}
    res["S1_vs_universe"] = {
        f"h{h}": {"A_and_B": day_series(b_sig & a_mask, h, "universe", 10 + h),
                  "B_only": day_series(b_sig & ~a_mask, h, "universe", 20 + h)}
        for h in HOLDS}
    res["S2_any_age"] = {f"h{h}": h1_test(h, a_any_mask, 30 + h) for h in HOLDS}
    s3 = {}
    for i, (name, a, b) in enumerate(SUBPERIODS):
        a64, b64 = np.datetime64(a), np.datetime64(b)
        s3[name] = h1_test(60, a_mask, 40 + i,
                           date_filter=lambda t, a64=a64, b64=b64:
                           (cal_arr[t] >= a64) & (cal_arr[t] <= b64))
    res["S3_h60"] = s3

    return {
        "design_version": ab.DESIGN_VERSION,
        "prereg": {"path": "docs/PREREGISTRATION_AB.md", "sha256": prereg_sha},
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "period": [TEST_START.isoformat(), SIGNAL_END.isoformat()],
        "resamples": resamples, "seed": SEED, "results": res,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="A∩B 결합 사전등록 백테스트")
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
        path = os.path.join(OUT_DIR, f"prereg-ab-{uuid.uuid4().hex[:8]}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2, default=str)
        print(f"저장: {os.path.relpath(path, _BASE)}")
    r = out["results"]
    for k in ("H1-20", "H1-60"):
        v = r[k]
        print(f"{k}: A∩B {v['n_excluded']} / B-only {v['n_kept']}  diff={v['diff']*100:+.3f}%p  "
              f"CI=[{v['ci95'][0]*100:+.3f}, {v['ci95'][1]*100:+.3f}]  q={v['q']:.4f}  "
              f"MDE={v['mde']*100:.3f}  → {v['verdict']}")
    for k in ("H2-20", "H2-60"):
        v = r[k]
        print(f"{k}: mean={v['mean']*100:+.3f}%  CI=[{v['ci95'][0]*100:+.3f}, "
              f"{v['ci95'][1]*100:+.3f}]  q={v['q']:.4f}  → {v['verdict']}")
    print(json.dumps(r["S0_counts"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

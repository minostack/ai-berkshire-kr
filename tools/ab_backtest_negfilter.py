#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
음의 서프라이즈 제외 필터 — B에 부착한 사전등록 백테스트

약속: `docs/PREREGISTRATION_NEGFILTER.md`.

B 신호는 `ab_backtest_b.build_panel()`로 **B 백테스트와 같은 코드**에서 얻고,
SUE는 스캐너의 `quarter_events`·`sue_latest`를 신호일 기준으로 호출한다
(정기보고서 접수일 기준 — 실거래에서 그대로 재현 가능한 정의).

    python tools/ab_backtest_negfilter.py
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

PREREG_PATH = os.path.join(_BASE, "docs", "PREREGISTRATION_NEGFILTER.md")
OUT_DIR = os.path.join(_BASE, "reports", "backtest")

# ── 사전등록 §1·§2·§3 ──
TEST_START = date(2017, 1, 1)
TEST_END = date(2026, 8, 13)
NEG_SUE = -1.0
RECENCY_TD = 60
PRIMARY_H = 20
SECONDARY_H = (5, 10)
DART_YEARS = range(2016, 2027)
RESAMPLES = 4000
SEED = 20260927
SUBPERIODS = (("2017-2021", date(2017, 1, 1), date(2021, 12, 31)),
              ("2022-2026", date(2022, 1, 1), date(2026, 12, 31)))


# ─────────────────────────────────────────────────────────────
# 필터
# ─────────────────────────────────────────────────────────────

def classify(events: list, d: date, cal_arr: np.ndarray, recency_td: int = RECENCY_TD,
             neg: float = NEG_SUE) -> dict:
    """신호일 d의 필터 판정.

    status: EXCLUDE / KEEP_POS / NO_EVENT / SUE_NA / STALE
    `exclude_any_age`: 최근성 없이 SUE ≤ neg인가(보조 S3용)
    """
    ev = ab.sue_latest(events, d)
    if ev is None:
        return {"status": "NO_EVENT", "sue": None, "age_td": None, "exclude_any_age": False}
    sue = ev["sue"]
    age = int(np.searchsorted(cal_arr, np.datetime64(d), side="right")
              - np.searchsorted(cal_arr, np.datetime64(ev["rcept_date"]), side="right"))
    any_age = sue is not None and sue <= neg
    if sue is None:
        st = "SUE_NA"
    elif age > recency_td:
        st = "STALE"
    elif sue <= neg:
        st = "EXCLUDE"
    else:
        st = "KEEP_POS"
    return {"status": st, "sue": sue, "age_td": age, "exclude_any_age": any_age,
            "period": ev["period"]}


# ─────────────────────────────────────────────────────────────
# 통계
# ─────────────────────────────────────────────────────────────

def two_group_cluster_bootstrap(e, is_ex, clusters, reps, seed):
    """mean(e|제외) − mean(e|유지)의 분기 군집 부트스트랩 분포."""
    ids, inv = np.unique(clusters, return_inverse=True)
    C = len(ids)
    s_ex = np.bincount(inv, weights=np.where(is_ex, e, 0.0), minlength=C)
    n_ex = np.bincount(inv, weights=is_ex.astype(float), minlength=C)
    s_k = np.bincount(inv, weights=np.where(~is_ex, e, 0.0), minlength=C)
    n_k = np.bincount(inv, weights=(~is_ex).astype(float), minlength=C)
    rng = np.random.default_rng(seed)
    pick = rng.integers(0, C, size=(reps, C))
    with np.errstate(invalid="ignore", divide="ignore"):
        out = s_ex[pick].sum(1) / n_ex[pick].sum(1) - s_k[pick].sum(1) / n_k[pick].sum(1)
    return out[~np.isnan(out)], C


def summarize_diff(e, is_ex, clusters, reps, seed) -> dict:
    e = np.asarray(e, float)
    is_ex = np.asarray(is_ex, bool)
    n_ex, n_k = int(is_ex.sum()), int((~is_ex).sum())
    if n_ex < 10 or n_k < 10:
        return {"n_excluded": n_ex, "n_kept": n_k, "verdict": "INSUFFICIENT"}
    obs = float(e[is_ex].mean() - e[~is_ex].mean())
    boot, C = two_group_cluster_bootstrap(e, is_ex, clusters, reps, seed)
    lo, hi = np.percentile(boot, [2.5, 97.5])
    se = float(boot.std(ddof=1))
    p = float(min(1.0, 2 * min((boot <= 0).mean(), (boot >= 0).mean())))
    return {"n_excluded": n_ex, "n_kept": n_k, "n_clusters": int(C),
            "valid_resamples": int(len(boot)),
            "excluded_mean": float(e[is_ex].mean()), "kept_mean": float(e[~is_ex].mean()),
            "diff": obs, "ci95": [float(lo), float(hi)], "se": se,
            "mde": bt.Z_MDE * se, "p": p}


def verdict_filter(s: dict, q: float) -> str:
    if s.get("verdict") == "INSUFFICIENT":
        return "INSUFFICIENT"
    lo, hi = s["ci95"]
    if hi < 0 and q <= 0.05:
        return "FILTER_HELPS"
    if lo > 0:
        return "FILTER_HURTS"
    if abs(s["diff"]) < s["mde"]:
        return "UNDERPOWERED"
    return "NO_SIGNIFICANT_EDGE"


# ─────────────────────────────────────────────────────────────
# 본체
# ─────────────────────────────────────────────────────────────

def run(resamples: int, log=print) -> dict:
    with open(PREREG_PATH, "rb") as f:
        prereg_sha = hashlib.sha256(f.read()).hexdigest()
    key = os.environ.get("DART_API_KEY")
    if not key:
        raise RuntimeError("DART_API_KEY 미설정")

    P = bt.build_panel(log)
    cal, cal_arr, net, member, sig = P["cal"], P["cal_arr"], P["net"], P["member"], P["sig"]
    idx_ret, sids = P["idx_ret"], P["sids"]
    in_test = ((cal_arr >= np.datetime64(TEST_START)) & (cal_arr <= np.datetime64(TEST_END)))
    b_sig = sig["B"] & in_test[None, :]

    log("DART 보고서(캐시)")
    codes = ab.dart_corp_codes(key)
    cc_of = {sid: codes.get(sid.split(":")[-1]) for sid in sids}
    reports = ab.dart_fetch_reports(sorted({c for c in cc_of.values() if c}), DART_YEARS,
                                    key, log=log)
    events_of = {cc: ab.quarter_events(r) for cc, r in reports.items()}

    log("필터 판정")
    rows = []    # (si, t, status, exclude_any_age)
    for si, t in zip(*np.nonzero(b_sig)):
        cc = cc_of.get(sids[si])
        c = (classify(events_of.get(cc, []), cal[t], cal_arr) if cc
             else {"status": "NO_EVENT", "exclude_any_age": False})
        rows.append((int(si), int(t), c["status"], c["exclude_any_age"]))
    status = {}
    for r in rows:
        status[r[2]] = status.get(r[2], 0) + 1
    ex_mask = np.zeros_like(b_sig)
    ex_any_mask = np.zeros_like(b_sig)
    for si, t, st, any_age in rows:
        ex_mask[si, t] = st == "EXCLUDE"
        ex_any_mask[si, t] = any_age
    kept_mask = b_sig & ~ex_mask

    # 같은 날 유니버스 동일가중 순수익
    uni = {}
    for h in P["horizons"]:
        r = net[h]
        um = member & ~np.isnan(r)
        with np.errstate(invalid="ignore"):
            uni[h] = np.where(um.any(0), np.where(um, r, 0).sum(0) / um.sum(0), np.nan)
    quarter_of = np.array([f"{d.year}Q{(d.month - 1) // 3 + 1}" for d in cal])

    def h1_test(h, excl, seed_off, date_filter=None):
        r = net[h]
        si, t = np.nonzero(b_sig)
        e = r[si, t] - uni[h][t]
        ok = ~np.isnan(e)
        if date_filter is not None:
            ok &= date_filter(t)
        return summarize_diff(e[ok], excl[si, t][ok], quarter_of[t][ok], resamples,
                              SEED + seed_off)

    def day_series(mask, h, vs):
        r = net[h]
        sm = mask & ~np.isnan(r)
        days = np.flatnonzero(sm.any(0))
        s = np.array([r[sm[:, t], t].mean() for t in days])
        b = uni[h][days] if vs == "universe" else idx_ret[h][days]
        ok = ~np.isnan(b)
        out = bt.summarize(s[ok] - b[ok], h, resamples, SEED + 90)
        out.update(signal_mean=float(s[ok].mean()), base_mean=float(b[ok].mean()),
                   n_signals=int(sum(sm[:, t].sum() for t in days[ok])))
        return out

    log("검정")
    res = {}
    h1 = h1_test(PRIMARY_H, ex_mask, 1)
    h2 = day_series(kept_mask, PRIMARY_H, "index")
    q = bt.bh_fdr([h1.get("p", 1.0), h2.get("p", 1.0)])
    h1["q"], h2["q"] = q
    h1["verdict"] = verdict_filter(h1, q[0])
    h2["verdict"] = bt.verdict(h2, q[1])
    res["H1"], res["H2"] = h1, h2

    res["S0_counts"] = {"B_signals_test_period": int(b_sig.sum()), "status": status,
                        "excluded_share": status.get("EXCLUDE", 0) / max(1, len(rows))}
    s1 = {h: h1_test(h, ex_mask, 10 + h) for h in SECONDARY_H}
    qs = bt.bh_fdr([s1[h].get("p", 1.0) for h in SECONDARY_H])
    for h, qq in zip(SECONDARY_H, qs):
        s1[h]["q"] = qq
        s1[h]["verdict"] = verdict_filter(s1[h], qq)
    res["S1"] = {f"h{h}": v for h, v in s1.items()}
    res["S2_before_after_vs_universe"] = {
        "B_unfiltered": day_series(b_sig, PRIMARY_H, "universe"),
        "B_filtered": day_series(kept_mask, PRIMARY_H, "universe"),
        "B_unfiltered_vs_index": day_series(b_sig, PRIMARY_H, "index"),
    }
    res["S3_any_age"] = h1_test(PRIMARY_H, ex_any_mask, 30)
    s4 = {}
    for i, (name, a, b) in enumerate(SUBPERIODS):
        a64, b64 = np.datetime64(a), np.datetime64(b)
        s4[name] = h1_test(PRIMARY_H, ex_mask, 40 + i,
                           date_filter=lambda t, a64=a64, b64=b64:
                           (cal_arr[t] >= a64) & (cal_arr[t] <= b64))
    res["S4"] = s4

    return {
        "design_version": ab.DESIGN_VERSION,
        "prereg": {"path": "docs/PREREGISTRATION_NEGFILTER.md", "sha256": prereg_sha},
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "period": [TEST_START.isoformat(), TEST_END.isoformat()],
        "resamples": resamples, "seed": SEED,
        "results": res,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="음의 서프라이즈 제외 필터(B) 사전등록 백테스트")
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
        path = os.path.join(OUT_DIR, f"prereg-negfilter-{uuid.uuid4().hex[:8]}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2, default=str)
        print(f"저장: {os.path.relpath(path, _BASE)}")
    r = out["results"]
    h1 = r["H1"]
    if h1.get("ci95"):
        print(f"H1: 제외 {h1['n_excluded']} / 유지 {h1['n_kept']}  diff={h1['diff']*100:+.3f}%p  "
              f"CI=[{h1['ci95'][0]*100:+.3f}, {h1['ci95'][1]*100:+.3f}]  q={h1['q']:.4f}  "
              f"MDE={h1['mde']*100:.3f}  → {h1['verdict']}")
    else:
        print("H1:", h1)
    h2 = r["H2"]
    print(f"H2: mean={h2['mean']*100:+.3f}%  CI=[{h2['ci95'][0]*100:+.3f}, "
          f"{h2['ci95'][1]*100:+.3f}]  q={h2['q']:.4f}  → {h2['verdict']}")
    print(json.dumps(r["S0_counts"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

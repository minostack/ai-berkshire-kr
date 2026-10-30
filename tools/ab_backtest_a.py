#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
후보 A(실적 서프라이즈 후 표류, PEAD) 사전등록 백테스트

약속: `docs/PREREGISTRATION_A.md` — 그 문서에 적힌 것만 계산한다.

SUE는 스캐너의 `quarter_events`·`sue_latest`를 **이벤트마다 그대로 호출**해
얻는다(각 이벤트 시점에 이미 접수된 과거 분기만 보게 된다). 가격·수익률·
유니버스·비용 처리는 B 백테스트(`ab_backtest_b.py`)의 함수를 재사용한다.

    python tools/ab_backtest_a.py
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

PREREG_PATH = os.path.join(_BASE, "docs", "PREREGISTRATION_A.md")
OUT_DIR = os.path.join(_BASE, "reports", "backtest")

# ── 사전등록 §2·§3 ──
SIGNAL_START = date(2017, 1, 1)
SIGNAL_END = date(2026, 6, 12)
DART_YEARS = range(2016, 2027)
PRIMARY_H = 60
SECONDARY_H = (20, 40)
NEG_SUE = -1.0
RESAMPLES = 4000
SEED = 20260927
SUBPERIODS = (("2017-2021", date(2017, 1, 1), date(2021, 12, 31)),
              ("2022-2026", date(2022, 1, 1), date(2026, 12, 31)))


# ─────────────────────────────────────────────────────────────
# 이벤트
# ─────────────────────────────────────────────────────────────

def events_with_sue(reports: dict) -> list:
    """한 회사의 모든 분기 이벤트에 **그 시점 기준** SUE를 붙인다.

    이벤트 e의 SUE = `ab.sue_latest(e까지의 분기, as_of=e의 접수일)`.
    e보다 뒤 분기이거나 e 이후에 접수된 분기는 과거 창에 들어가지 않는다.
    """
    evs = ab.quarter_events(reports)
    out = []
    for i, e in enumerate(evs):
        s = ab.sue_latest(evs[:i + 1], e["rcept_date"])
        if s is None or s["period"] != e["period"]:
            continue     # 더 늦은 분기가 먼저 접수된 이상 사례 — 세지 않는다
        out.append(s)
    return out


def signal_day_index(cal_arr: np.ndarray, rcept: date) -> int:
    """이벤트일 당일 또는 직전의 마지막 거래일 인덱스(없으면 -1)."""
    return int(np.searchsorted(cal_arr, np.datetime64(rcept), side="right")) - 1


# ─────────────────────────────────────────────────────────────
# 통계 — 달력 분기 군집 부트스트랩
# ─────────────────────────────────────────────────────────────

def cluster_bootstrap(x: np.ndarray, clusters: np.ndarray, reps: int, seed: int):
    ids, inv = np.unique(clusters, return_inverse=True)
    sums = np.bincount(inv, weights=x)
    cnts = np.bincount(inv).astype(float)
    rng = np.random.default_rng(seed)
    pick = rng.integers(0, len(ids), size=(reps, len(ids)))
    return sums[pick].sum(axis=1) / cnts[pick].sum(axis=1), len(ids)


def summarize(x, clusters, reps, seed) -> dict:
    x = np.asarray(x, float)
    if len(x) < 10 or len(np.unique(clusters)) < 5:
        return {"n_days": int(len(x)), "mean": float(x.mean()) if len(x) else None,
                "verdict": "INSUFFICIENT"}
    boot, nc = cluster_bootstrap(x, clusters, reps, seed)
    lo, hi = np.percentile(boot, [2.5, 97.5])
    se = float(boot.std(ddof=1))
    p = float(min(1.0, 2 * min((boot <= 0).mean(), (boot >= 0).mean())))
    return {"n_days": int(len(x)), "n_clusters": int(nc), "mean": float(x.mean()),
            "ci95": [float(lo), float(hi)], "se": se, "mde": bt.Z_MDE * se, "p": p,
            "win_days": float((x > 0).mean())}


# ─────────────────────────────────────────────────────────────
# 본체
# ─────────────────────────────────────────────────────────────

def run(resamples: int, log=print, prereg_path: str = PREREG_PATH,
        event_date=None, extra=None) -> dict:
    """A 백테스트 본체.

    `event_date(corp_code, 이벤트) -> date`를 주면 신호일을 그 날짜로 잡는다
    (A2 — 잠정실적 공시일). 없으면 정기보고서 접수일(A v1).
    `extra(ctx) -> dict`는 사전등록마다 다른 보조 분석을 붙이는 자리다.
    """
    with open(prereg_path, "rb") as f:
        prereg_sha = hashlib.sha256(f.read()).hexdigest()
    cost_cfg = json.load(open(os.path.join(_BASE, "configs", "costs", "krx_v1.json"),
                              encoding="utf-8"))
    cm, tax, sl = (cost_cfg["commission_rate"], cost_cfg["tax_rate_sell"],
                   cost_cfg["slippage_rate"])
    key = os.environ.get("DART_API_KEY")
    if not key:
        raise RuntimeError("DART_API_KEY 미설정")

    log("캘린더·지수·유니버스 로드")
    cal, idx_open, idx_close = bt.load_calendar_and_index()
    cal_arr = np.array(cal, dtype="datetime64[D]")
    pos = {d: k for k, d in enumerate(cal)}
    T = len(cal)
    intervals = bt.load_universe_intervals()
    sids = sorted({s for iv in intervals.values() for s in iv})
    sidx = {s: i for i, s in enumerate(sids)}
    S = len(sids)
    in_period = ((cal_arr >= np.datetime64(SIGNAL_START))
                 & (cal_arr <= np.datetime64(SIGNAL_END)))

    log(f"일봉 로드 — {S}종목")
    bars = bt.load_all_bars(set(sids))
    horizons = (PRIMARY_H,) + SECONDARY_H
    net = {h: np.full((S, T), np.nan) for h in horizons}
    avgval = np.full((S, T), np.nan)
    prox = np.full((S, T), np.nan)
    member = np.zeros((S, T), bool)
    member_of = np.full((S, T), "", dtype=object)
    for si, sid in enumerate(sids):
        for idx_id, iv in intervals.items():
            for start, end in iv.get(sid, []):
                a = np.searchsorted(cal_arr, np.datetime64(start))
                b = T if end is None else np.searchsorted(cal_arr, np.datetime64(end))
                member[si, a:b] = True
                member_of[si, a:b] = idx_id
        rows = [r for r in bars.get(sid, []) if r[0] in pos]
        if not rows:
            continue
        k = np.array([pos[r[0]] for r in rows])
        o = np.full(T, np.nan)
        c = np.full(T, np.nan)
        o[k] = [r[1] for r in rows]
        c[k] = [r[3] for r in rows]
        for h in horizons:
            ent = np.full(T, np.nan)
            ex = np.full(T, np.nan)
            ent[:T - 1] = o[1:]
            ex[:T - h] = c[h:]
            net[h][si] = bt.net_return(ent, ex, cm, tax, sl)
        m = bt.compute_metrics([r[2] for r in rows], [r[3] for r in rows],
                               [r[4] for r in rows], [r[5] for r in rows])
        avgval[si, k] = m["avgval"]
        prox[si, k] = m["prox"]

    log("DART corp_code 매핑·보고서 조회")
    codes = ab.dart_corp_codes(key)
    cc_of = {sid: codes.get(sid.split(":")[-1]) for sid in sids}
    mapped = {sid: cc for sid, cc in cc_of.items() if cc}
    obs = member & in_period[None, :]
    mapped_rows = np.array([sid in mapped for sid in sids])
    coverage = {
        "securities": S, "securities_mapped": len(mapped),
        "member_obs": int(obs.sum()),
        "member_obs_mapped": int(obs[mapped_rows].sum()),
    }
    reports = ab.dart_fetch_reports(sorted(set(mapped.values())), DART_YEARS, key,
                                    log=log)
    sid_of_cc = {}
    for sid, cc in mapped.items():
        sid_of_cc.setdefault(cc, []).append(sid)

    log("이벤트·SUE")
    events = []     # (si, k, sue, period)
    n_events = n_sue = 0
    for cc, reps in reports.items():
        evs = events_with_sue(reps)
        n_events += len(evs)
        for e in evs:
            if e["sue"] is None:
                continue
            n_sue += 1
            ev_date = event_date(cc, e) if event_date else e["rcept_date"]
            k = signal_day_index(cal_arr, ev_date)
            if k < 0 or not in_period[k]:
                continue
            for sid in sid_of_cc[cc]:
                si = sidx[sid]
                if not member[si, k]:
                    continue
                if not (np.nan_to_num(avgval[si, k], nan=-1) >= ab.LIQ_MIN_KRW):
                    continue
                events.append((si, k, e["sue"], e["period"], e["rcept_date"], ev_date))
    # 같은 종목·같은 신호일에 이벤트가 둘이면 늦은 분기만
    dedup = {}
    for ev in events:
        key_ = (ev[0], ev[1])
        if key_ not in dedup or ev[3] > dedup[key_][3]:
            dedup[key_] = ev
    events = list(dedup.values())

    def mask_of(pred):
        mk = np.zeros((S, T), bool)
        for si, k, sue, *_ in events:
            if pred(sue, si, k):
                mk[si, k] = True
        return mk

    sig_a = mask_of(lambda s, si, k: s >= ab.SUE_MIN)
    sig_neg = mask_of(lambda s, si, k: s <= NEG_SUE)

    idx_ret = {h: np.full(T, np.nan) for h in horizons}
    for h in horizons:
        idx_ret[h][:T - h] = idx_close[h:] / idx_open[1:T - h + 1] - 1
    quarter_of = np.array([f"{d.year}Q{(d.month - 1) // 3 + 1}" for d in cal])

    def test(sig_mask, h, vs, seed_off=0, row_filter=None, date_filter=None):
        r = net[h]
        sm = sig_mask & ~np.isnan(r)
        if row_filter is not None:
            sm &= row_filter
        um = member & ~np.isnan(r)
        days = np.flatnonzero(sm.any(axis=0) & in_period)
        if date_filter is not None:
            days = days[date_filter(days)]
        s = np.array([r[sm[:, t], t].mean() for t in days])
        n_sig = int(sum(sm[:, t].sum() for t in days))
        b = (np.array([r[um[:, t], t].mean() for t in days]) if vs == "universe"
             else idx_ret[h][days])
        ok = ~np.isnan(b)
        days, s, b = days[ok], s[ok], b[ok]
        out = summarize(s - b, quarter_of[days], resamples, SEED + seed_off)
        out.update(signal_mean=float(s.mean()) if len(s) else None,
                   base_mean=float(b.mean()) if len(b) else None, n_signals=n_sig)
        return out, days, s - b

    log("검정")
    res = {}
    h1, h1_days, h1_d = test(sig_a, PRIMARY_H, "universe", 1)
    h2, _, _ = test(sig_a, PRIMARY_H, "index", 2)
    q = bt.bh_fdr([h1.get("p", 1.0), h2.get("p", 1.0)])
    h1["q"], h2["q"] = q
    h1["verdict"], h2["verdict"] = bt.verdict(h1, q[0]), bt.verdict(h2, q[1])
    res["H1"], res["H2"] = h1, h2

    s1 = {h: test(sig_a, h, "universe", 10 + h)[0] for h in SECONDARY_H}
    qs = bt.bh_fdr([s1[h].get("p", 1.0) for h in SECONDARY_H])
    for h, qq in zip(SECONDARY_H, qs):
        s1[h]["q"] = qq
        s1[h]["verdict"] = bt.verdict(s1[h], qq)
    res["S1"] = {f"h{h}": v for h, v in s1.items()}
    res["S2_negative_surprise"] = test(sig_neg, PRIMARY_H, "universe", 20)[0]

    # S3 — 전체 이벤트 SUE 5분위(표본 전체 경계, 서술용)
    sues = np.array([ev[2] for ev in events])
    edges = np.quantile(sues, [0.2, 0.4, 0.6, 0.8]) if len(sues) else []
    s3 = {"edges": [float(e) for e in edges]}
    for qi in range(5):
        lo = -np.inf if qi == 0 else edges[qi - 1]
        hi = np.inf if qi == 4 else edges[qi]
        mk = mask_of(lambda s, si, k, lo=lo, hi=hi: lo <= s < hi if qi < 4 else lo <= s)
        s3[f"Q{qi + 1}"] = test(mk, PRIMARY_H, "universe", 30 + qi)[0]
    res["S3_quintiles"] = s3

    prox_ok = np.nan_to_num(prox, nan=-1) >= ab.PROX_MIN
    res["S4_prox52w"] = {
        "near_high(>=0.90)": test(sig_a, PRIMARY_H, "universe", 40, row_filter=prox_ok)[0],
        "far(<0.90)": test(sig_a, PRIMARY_H, "universe", 41, row_filter=~prox_ok)[0],
    }
    s5 = {}
    for i, (name, a, b) in enumerate(SUBPERIODS):
        a64, b64 = np.datetime64(a), np.datetime64(b)
        s5[name] = test(sig_a, PRIMARY_H, "universe", 50 + i,
                        date_filter=lambda d, a64=a64, b64=b64:
                        (cal_arr[d] >= a64) & (cal_arr[d] <= b64))[0]
    for i, idx_id in enumerate(ab.INDEXES):
        s5[idx_id] = test(sig_a, PRIMARY_H, "universe", 55 + i,
                          row_filter=(member_of == idx_id))[0]
    res["S5"] = s5
    years = {}
    for t, d in zip(h1_days, h1_d):
        years.setdefault(cal[t].year, []).append((t, d))
    res["S6_years"] = {
        str(y): {"days": len(v), "mean_excess": float(np.mean([d for _, d in v])),
                 "signals": int(sum((sig_a[:, t] & ~np.isnan(net[PRIMARY_H][:, t])).sum()
                                    for t, _ in v))}
        for y, v in sorted(years.items())}

    if extra is not None:
        res.update(extra({"test": test, "mask_of": mask_of, "events": events,
                          "sig_a": sig_a, "cal": cal, "cal_arr": cal_arr,
                          "reports": reports, "PRIMARY_H": PRIMARY_H}))

    return {
        "design_version": ab.DESIGN_VERSION,
        "prereg": {"path": os.path.relpath(prereg_path, _BASE).replace(os.sep, "/"),
                   "sha256": prereg_sha},
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "period": [SIGNAL_START.isoformat(), SIGNAL_END.isoformat()],
        "cost_model": cost_cfg["cost_model_version"], "resamples": resamples, "seed": SEED,
        "coverage": coverage,
        "counts": {"events_all": n_events, "events_with_sue": n_sue,
                   "events_in_universe_liquid": len(events),
                   "A_signals": int(sig_a.sum()),
                   "A_signals_no_label": int((sig_a & np.isnan(net[PRIMARY_H])).sum()),
                   "NEG_signals": int(sig_neg.sum())},
        "results": res,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="후보 A(PEAD) 사전등록 백테스트")
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
        path = os.path.join(OUT_DIR, f"prereg-a-{uuid.uuid4().hex[:8]}.json")
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
    print(json.dumps(out["coverage"]), json.dumps(out["counts"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

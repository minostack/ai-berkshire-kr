#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
후보 B(거래량 급증 + 52주 신고가 근접) 사전등록 백테스트

약속: `docs/PREREGISTRATION_B.md` — 이 파일은 그 문서에 적힌 것**만** 계산한다.
결과 JSON에 문서의 sha256을 박아, 어느 버전의 약속으로 돌렸는지 남긴다.

신호 정의는 `tools/ab_candidate_scanner.py`의 상수와 `b_gate`를 import해서
쓴다. 지표는 16년 × 800종목을 돌리기 위해 numpy로 벡터화했고, 스캐너의
`b_metrics`와 같은 값이 나오는지 `tests/unit/test_ab_backtest_b.py`가 고정한다.

    python tools/ab_backtest_b.py
    python tools/ab_backtest_b.py --resamples 1000     # 빠른 확인용(판정에는 4000)
"""

import argparse
import hashlib
import json
import math
import os
import sys
import uuid
from datetime import date, datetime

import numpy as np

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ab_candidate_scanner as ab  # noqa: E402

PREREG_PATH = os.path.join(_BASE, "docs", "PREREGISTRATION_B.md")
OUT_DIR = os.path.join(_BASE, "reports", "backtest")

# ── 사전등록 §2·§3 — 바꾸면 약속 위반 ──
SIGNAL_START = date(2010, 1, 4)
SIGNAL_END = date(2026, 8, 13)
PRIMARY_H = 20
SECONDARY_H = (5, 10)
SUPPRESS_TD = 20
RESAMPLES = 4000
SEED = 20260927
Z_MDE = 1.96 + 0.84
SUBPERIODS = (("2010-2017", date(2010, 1, 1), date(2017, 12, 31)),
              ("2018-2026", date(2018, 1, 1), date(2026, 12, 31)))
LOAD_FROM = date(2008, 6, 1)     # 252봉 워밍업


# ─────────────────────────────────────────────────────────────
# 지표 — 스캐너 b_metrics의 벡터화판
# ─────────────────────────────────────────────────────────────

def compute_metrics(high, close, volume, value) -> dict:
    """봉 배열(날짜 오름차순) → 각 봉을 기준일로 본 B 지표 배열.

    i번째 값 = `ab.b_metrics(bars[:i+1])`와 같다. 정의가 없으면 NaN.
    """
    high = np.asarray(high, float)
    close = np.asarray(close, float)
    volume = np.asarray(volume, float)
    value = np.asarray(value, float)
    n = len(close)
    R, B = ab.RVOL_RECENT, ab.RVOL_BASE
    need = R + B

    rvol = np.full(n, np.nan)
    cs = np.concatenate([[0.0], np.cumsum(volume)])
    if n >= need:
        i = np.arange(need - 1, n)
        recent = (cs[i + 1] - cs[i + 1 - R]) / R
        base = (cs[i + 1 - R] - cs[i + 1 - need]) / B
        with np.errstate(divide="ignore", invalid="ignore"):
            rvol[i] = np.where(base > 0, recent / base, np.nan)

    W = ab.HIGH_WINDOW
    high52 = np.maximum.accumulate(high) if n else high.copy()
    if n > W:
        from numpy.lib.stride_tricks import sliding_window_view
        high52[W - 1:] = sliding_window_view(high, W).max(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        prox = np.where(high52 > 0, close / high52, np.nan)
    history = np.minimum(np.arange(1, n + 1), W)

    L = ab.LIQ_DAYS
    avgval = np.full(n, np.nan)
    cv = np.concatenate([[0.0], np.cumsum(value)])
    if n >= L:
        i = np.arange(L - 1, n)
        avgval[i] = (cv[i + 1] - cv[i + 1 - L]) / L

    # b_metrics는 55봉 미만이면 지표 전체를 내지 않는다 — 같게 맞춘다
    short = np.arange(n) < need - 1
    prox[short] = np.nan
    avgval[short] = np.nan
    return {"rvol": rvol, "prox": prox, "history": history, "avgval": avgval}


def gate_masks(m: dict) -> dict:
    """게이트 마스크. 조건값은 스캐너 상수를 그대로 쓴다."""
    hist_ok = m["history"] >= ab.MIN_HISTORY
    liq_ok = np.nan_to_num(m["avgval"], nan=-1) >= ab.LIQ_MIN_KRW
    rvol_ok = np.nan_to_num(m["rvol"], nan=-1) >= ab.RVOL_MIN
    prox_ok = np.nan_to_num(m["prox"], nan=-1) >= ab.PROX_MIN
    return {"B": hist_ok & liq_ok & rvol_ok & prox_ok,
            "RVOL_ONLY": hist_ok & liq_ok & rvol_ok,
            "PROX_ONLY": hist_ok & liq_ok & prox_ok}


def suppress(idx: np.ndarray, gap: int) -> np.ndarray:
    """정렬된 캘린더 인덱스에서 직전 채택 후 gap거래일 안의 재신호를 버린다."""
    kept, last = [], -10 ** 9
    for k in idx:
        if k - last >= gap:
            kept.append(k)
            last = k
    return np.asarray(kept, dtype=int)


def net_return(entry, exit_, commission, tax, slippage):
    """`CostModel.net_return`과 같은 식(배열판)."""
    buy = entry * (1 + slippage)
    sell = exit_ * (1 - slippage)
    return sell * (1 - commission - tax) / (buy * (1 + commission)) - 1


# ─────────────────────────────────────────────────────────────
# 통계
# ─────────────────────────────────────────────────────────────

def block_bootstrap(x: np.ndarray, block: int, reps: int, seed: int) -> np.ndarray:
    """이동 블록 부트스트랩 평균 분포."""
    n = len(x)
    rng = np.random.default_rng(seed)
    L = max(1, min(block, n))
    nb = math.ceil(n / L)
    starts = rng.integers(0, n - L + 1, size=(reps, nb))
    idx = (starts[:, :, None] + np.arange(L)[None, None, :]).reshape(reps, -1)[:, :n]
    return x[idx].mean(axis=1)


def summarize(x: np.ndarray, block: int, reps: int, seed: int) -> dict:
    x = np.asarray(x, float)
    if len(x) < 10:
        return {"n_days": int(len(x)), "mean": float(x.mean()) if len(x) else None,
                "verdict": "INSUFFICIENT"}
    boot = block_bootstrap(x, block, reps, seed)
    lo, hi = np.percentile(boot, [2.5, 97.5])
    se = float(boot.std(ddof=1))
    p = float(min(1.0, 2 * min((boot <= 0).mean(), (boot >= 0).mean())))
    return {"n_days": int(len(x)), "mean": float(x.mean()), "ci95": [float(lo), float(hi)],
            "se": se, "mde": Z_MDE * se, "p": p, "win_days": float((x > 0).mean())}


def bh_fdr(pvals: list) -> list:
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    q = [0.0] * m
    prev = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        prev = min(prev, pvals[i] * m / rank)
        q[i] = prev
    return q


def verdict(s: dict, q: float) -> str:
    if s.get("verdict") == "INSUFFICIENT":
        return "INSUFFICIENT"
    lo, hi = s["ci95"]
    if lo > 0 and q <= 0.05:
        return "SIGNIFICANT_EDGE"
    if hi < 0:
        return "SIGNIFICANT_NEGATIVE"
    if abs(s["mean"]) < s["mde"]:
        return "UNDERPOWERED"
    return "NO_SIGNIFICANT_EDGE"


# ─────────────────────────────────────────────────────────────
# 데이터
# ─────────────────────────────────────────────────────────────

def load_calendar_and_index():
    import pyarrow.compute as pc
    from cvd_obv.storage.parquet_store import ParquetStore
    t = ParquetStore().read_arrow("silver", "benchmark_bars",
                                  columns=["benchmark_id", "trading_date", "open", "close",
                                           "price_scope", "run_id", "data_revision"])
    t = t.filter(pc.and_(pc.equal(t.column("benchmark_id"), "KRX:KOSPI200"),
                         pc.equal(t.column("price_scope"), "REGULAR")))
    rows = {}
    for d, o, c in zip(t.column("trading_date").to_pylist(), t.column("open").to_pylist(),
                       t.column("close").to_pylist()):
        rows[d] = (float(o) if o else np.nan, float(c) if c else np.nan)
    cal = sorted(d for d in rows if d >= LOAD_FROM)
    if not cal:
        raise RuntimeError("KOSPI200 지수 일봉이 없다 — 캘린더를 만들 수 없다")
    idx_open = np.array([rows[d][0] for d in cal])
    idx_close = np.array([rows[d][1] for d in cal])
    return cal, idx_open, idx_close


def load_universe_intervals():
    from cvd_obv.universe import membership_intervals
    return membership_intervals(ab.INDEXES)


def load_all_bars(securities, store=None, load_from=None):
    """기준 일봉(봉 기준 bars-1.0 — 정규장·수정주가, 품질 FAILED 제외, 최신 리비전).

    빈 거래대금(1분봉 복원 봉)은 `bar_standard`의 단일 규칙으로 추정한다 — 같은 날
    EXTENDED 공식 거래대금 × 거래량 비율, 없으면 종가×거래량. 2026-10-03 이전에는
    종가×거래량만 썼다(2026-09-14 이후 복원 봉에만 해당).

    `load_from`은 로드 시작일이다. 비우면 `LOAD_FROM`(B 사전등록 그대로) —
    기본값으로 부르면 B 결과는 바뀌지 않는다(`test_ab_backtest_b` 고정).
    짧은 창만 필요한 다른 사전등록(예: VIDYA, 2025-09~)이 16년을 다 읽지 않도록
    두었다.
    """
    import pyarrow.dataset as ds
    from cvd_obv.bar_standard import read_standard_daily
    t = read_standard_daily(
        store,
        columns=["security_id", "trading_date", "open", "high", "close", "volume",
                 "traded_value"],
        filters=(ds.field("trading_date") >= (load_from or LOAD_FROM))
        & ds.field("security_id").isin(sorted(securities)),
        drop_failed=True, fill_traded_value=True)
    t = t.sort_by([("security_id", "ascending"), ("trading_date", "ascending")])
    cols = {c: t.column(c).to_pylist() for c in
            ("security_id", "trading_date", "open", "high", "close", "volume", "traded_value")}
    out = {}
    for sid, d, o, h, c, v, tv in zip(*cols.values()):
        c = float(c)
        v = int(v)
        out.setdefault(sid, []).append(
            (d, float(o), float(h), c, v, float(tv) if tv is not None else c * v))
    return out


# ─────────────────────────────────────────────────────────────
# 본체
# ─────────────────────────────────────────────────────────────

def build_panel(log=print, horizons=None, signal_end=None, suppress_td=None) -> dict:
    """사전등록 B의 신호·수익률 패널. 다른 사전등록(필터 등)이 **같은 B 신호**를
    쓰도록 run()에서 떼어 냈다 — 여기를 고치면 B 결과가 달라진다.

    인자를 비우면 B 사전등록 그대로다. `horizons`·`signal_end`·`suppress_td`는
    보유기간 연장 사전등록(`PREREGISTRATION_B_HOLD.md`)처럼 **다른 약속**이
    명시적으로 바꿀 때만 준다.
    """
    horizons = tuple(horizons) if horizons else (PRIMARY_H,) + SECONDARY_H
    signal_end = signal_end or SIGNAL_END
    suppress_td = suppress_td or SUPPRESS_TD
    cost_cfg = json.load(open(os.path.join(_BASE, "configs", "costs", "krx_v1.json"),
                              encoding="utf-8"))
    cm, tax, sl = (cost_cfg["commission_rate"], cost_cfg["tax_rate_sell"],
                   cost_cfg["slippage_rate"])

    log("캘린더·지수 로드")
    cal, idx_open, idx_close = load_calendar_and_index()
    pos = {d: k for k, d in enumerate(cal)}
    T = len(cal)
    log("유니버스 이력 로드")
    intervals = load_universe_intervals()
    sids = sorted({s for iv in intervals.values() for s in iv})
    log(f"일봉 로드 — {len(sids)}종목 (느림)")
    bars = load_all_bars(set(sids))

    S = len(sids)
    net = {h: np.full((S, T), np.nan) for h in horizons}
    member = np.zeros((S, T), bool)
    member_of = np.full((S, T), "", dtype=object)
    sig = {k: np.zeros((S, T), bool) for k in ("B", "RVOL_ONLY", "PROX_ONLY")}
    raw_b = 0
    cal_arr = np.array(cal, dtype="datetime64[D]")
    in_period = (cal_arr >= np.datetime64(SIGNAL_START)) & (cal_arr <= np.datetime64(signal_end))

    log("지표·수익률 계산")
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
            net[h][si] = net_return(ent, ex, cm, tax, sl)
        m = compute_metrics([r[2] for r in rows], [r[3] for r in rows],
                            [r[4] for r in rows], [r[5] for r in rows])
        masks = gate_masks(m)
        # 스캐너 b_gate로 교차 확인 — 벡터화가 게이트를 다르게 읽으면 여기서 멈춘다
        for j in np.flatnonzero(masks["B"]):
            d = {"history_days": int(m["history"][j]), "rvol": m["rvol"][j],
                 "prox_52w": m["prox"][j], "avg_value_20d": m["avgval"][j]}
            assert ab.b_gate(d) == [], (sid, rows[j][0], d)
        for key, mk in masks.items():
            full = np.zeros(T, bool)
            full[k[mk]] = True
            full &= member[si] & in_period
            if key == "B":
                raw_b += int(full.sum())
            kept = suppress(np.flatnonzero(full), suppress_td)
            sig[key][si, kept] = True

    idx_ret = {h: np.full(T, np.nan) for h in horizons}
    for h in horizons:
        idx_ret[h][:T - h] = idx_close[h:] / idx_open[1:T - h + 1] - 1

    return {"cal": cal, "cal_arr": cal_arr, "T": T, "S": S, "sids": sids,
            "net": net, "member": member, "member_of": member_of, "sig": sig,
            "raw_b": raw_b, "in_period": in_period, "idx_ret": idx_ret,
            "horizons": horizons, "cost_cfg": cost_cfg}


def run(resamples: int, log=print) -> dict:
    with open(PREREG_PATH, "rb") as f:
        prereg_sha = hashlib.sha256(f.read()).hexdigest()

    P = build_panel(log)
    (cal, cal_arr, T, S, net, member, member_of, sig, raw_b, in_period,
     idx_ret, cost_cfg) = (P[k] for k in (
        "cal", "cal_arr", "T", "S", "net", "member", "member_of", "sig", "raw_b",
        "in_period", "idx_ret", "cost_cfg"))
    cm, tax, sl = (cost_cfg["commission_rate"], cost_cfg["tax_rate_sell"],
                   cost_cfg["slippage_rate"])

    def day_series(sig_mask, h, vs, row_filter=None, date_filter=None, rets=None):
        r = net[h] if rets is None else rets
        sm = sig_mask & ~np.isnan(r)
        if row_filter is not None:
            sm &= row_filter
        um = member & ~np.isnan(r)
        days = np.flatnonzero(sm.any(axis=0) & in_period)
        if date_filter is not None:
            days = days[date_filter(days)]
        sig_mean = np.array([r[sm[:, t], t].mean() for t in days])
        n_sig = np.array([sm[:, t].sum() for t in days])
        if vs == "universe":
            base = np.array([r[um[:, t], t].mean() for t in days])
        else:
            base = idx_ret[h][days]
        ok = ~np.isnan(base)
        return days[ok], sig_mean[ok], base[ok], int(n_sig[ok].sum())

    def test(sig_mask, h, vs, seed_off=0, **kw):
        days, s, b, n_sig = day_series(sig_mask, h, vs, **kw)
        out = summarize(s - b, h, resamples, SEED + seed_off)
        out.update(signal_mean=float(s.mean()) if len(s) else None,
                   base_mean=float(b.mean()) if len(b) else None,
                   n_signals=n_sig)
        return out, days, s - b

    log("검정")
    res = {}
    h1, h1_days, h1_d = test(sig["B"], PRIMARY_H, "universe", 1)
    h2, _, _ = test(sig["B"], PRIMARY_H, "index", 2)
    q = bh_fdr([h1.get("p", 1.0), h2.get("p", 1.0)])
    h1["q"], h2["q"] = q
    h1["verdict"], h2["verdict"] = verdict(h1, q[0]), verdict(h2, q[1])
    res["H1"], res["H2"] = h1, h2

    s1 = {h: test(sig["B"], h, "universe", 10 + h)[0] for h in SECONDARY_H}
    qs = bh_fdr([s1[h].get("p", 1.0) for h in SECONDARY_H])
    for h, qq in zip(SECONDARY_H, qs):
        s1[h]["q"] = qq
        s1[h]["verdict"] = verdict(s1[h], qq)
    res["S1"] = {f"h{h}": v for h, v in s1.items()}
    res["S2"] = {k: test(sig[k], PRIMARY_H, "universe", 30 + i)[0]
                 for i, k in enumerate(("RVOL_ONLY", "PROX_ONLY"))}
    s3 = {}
    for i, (name, a, b) in enumerate(SUBPERIODS):
        a64, b64 = np.datetime64(a), np.datetime64(b)
        s3[name] = test(sig["B"], PRIMARY_H, "universe", 40 + i,
                        date_filter=lambda d, a64=a64, b64=b64:
                        (cal_arr[d] >= a64) & (cal_arr[d] <= b64))[0]
    for i, idx_id in enumerate(ab.INDEXES):
        s3[idx_id] = test(sig["B"], PRIMARY_H, "universe", 50 + i,
                          row_filter=(member_of == idx_id))[0]
    res["S3"] = s3
    years = {}
    for t, d in zip(h1_days, h1_d):
        y = str(cal[t].year)
        years.setdefault(y, []).append(d)
    res["S4"] = {y: {"days": len(v), "mean_excess": float(np.mean(v)),
                     "signals": int(sum((sig["B"][:, t] & ~np.isnan(net[PRIMARY_H][:, t])).sum()
                                        for t in h1_days if cal[t].year == int(y)))}
                 for y, v in sorted(years.items())}

    # ── 사후(POST-HOC) — 사전등록에 없던 서술. 판정에 쓰지 않는다 ──
    # H1·H2가 갈린 이유(동일가중 유니버스 vs 시총가중 지수, 비용)를 보이기 위한 것.
    # net_return의 역산 — 종가/시가 비율(비용 전)
    gross_h = ((net[PRIMARY_H] + 1) * (1 + sl) * (1 + cm)
               / ((1 - sl) * (1 - cm - tax)) - 1)
    res["POST_HOC"] = {
        "_note": "사전등록에 없던 서술 분석. 판정에 쓰지 않는다",
        "B_gross_vs_index_h20": test(sig["B"], PRIMARY_H, "index", 60, rets=gross_h)[0],
        "universe_gross_vs_index_h20": test(member, PRIMARY_H, "index", 61,
                                            rets=gross_h)[0],
    }

    # 제외 건수 — 신호는 있었지만 진입·청산 봉이 없어 수익률을 못 낸 것
    b_all = sig["B"]
    excluded = int((b_all & np.isnan(net[PRIMARY_H])).sum())
    universe_n = int((member & in_period[None, :]).sum())
    universe_na = int((member & in_period[None, :] & np.isnan(net[PRIMARY_H])).sum())

    return {
        "design_version": ab.DESIGN_VERSION,
        "prereg": {"path": "docs/PREREGISTRATION_B.md", "sha256": prereg_sha},
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "period": [SIGNAL_START.isoformat(), SIGNAL_END.isoformat()],
        "cost_model": cost_cfg["cost_model_version"],
        "resamples": resamples, "seed": SEED,
        "counts": {"securities": S, "calendar_days": int(in_period.sum()),
                   "raw_B_pass": raw_b, "B_signals_after_suppress": int(b_all.sum()),
                   "B_signals_no_label": excluded,
                   "universe_obs": universe_n, "universe_obs_no_label": universe_na},
        "results": res,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="후보 B 사전등록 백테스트")
    ap.add_argument("--resamples", type=int, default=RESAMPLES)
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    out = run(args.resamples)
    txt = json.dumps(out, ensure_ascii=False, indent=2, default=str)
    if not args.no_save:
        os.makedirs(OUT_DIR, exist_ok=True)
        path = os.path.join(OUT_DIR, f"prereg-b-{uuid.uuid4().hex[:8]}.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(txt)
        print(f"저장: {os.path.relpath(path, _BASE)}")
    r = out["results"]
    for k in ("H1", "H2"):
        v = r[k]
        print(f"{k}: mean={v['mean']*100:+.3f}%  CI=[{v['ci95'][0]*100:+.3f}, "
              f"{v['ci95'][1]*100:+.3f}]  q={v['q']:.4f}  MDE={v['mde']*100:.3f}%  "
              f"→ {v['verdict']}")
    print(json.dumps(out["counts"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

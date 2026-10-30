#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
사전등록 VIDYA — CVD 가속 게이트에 VIDYA 조건을 더하면 나아지는가

약속: `docs/PREREGISTRATION_VIDYA.md` — 이 파일은 그 문서에 적힌 것**만**
계산한다. 결과 JSON에 문서의 sha256을 박는다.

재사용(새로 짜지 않는다 — 같은 정의가 두 벌 생기면 어느 쪽이 맞는지 모른다):
  · `ab_backtest_b.load_calendar_and_index` 캘린더·지수
  · `ab_backtest_b.load_universe_intervals` 유니버스 이력
  · `ab_backtest_b.load_all_bars`           기준 일봉(bars-1.0)
  · `ab_backtest_b.net_return`              비용 식
  · `ab_backtest_b.summarize/bh_fdr/verdict` 이동 블록 부트스트랩·판정
  · `cvd_obv.indicators.vidya`              VIDYA·CMO
  · `data/gold/cvd_daily_flow.json`         일별 CVD 흐름(가속 검증과 같은 파일)

    python tools/accel_vidya_backtest.py
    python tools/accel_vidya_backtest.py --resamples 1000 --no-save   # 빠른 확인
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

import ab_backtest_b as bb  # noqa: E402

from cvd_obv.indicators import CMO_LENGTH, VIDYA_LENGTH, vidya  # noqa: E402

PREREG_PATH = os.path.join(_BASE, "docs", "PREREGISTRATION_VIDYA.md")
FLOW_PATH = os.path.join(_BASE, "data", "gold", "cvd_daily_flow.json")
OUT_DIR = os.path.join(_BASE, "reports", "backtest")

# ── 사전등록 §1·§2·§3 — 바꾸면 약속 위반 ──
RECENT_DAYS = 3
PRIOR_DAYS = 7
NEED_DAYS = RECENT_DAYS + PRIOR_DAYS          # 10거래일 연속
OVERHEAT_MAX = 1.10                           # 종가 ≤ MA20 × 1.10
MA_DAYS = 20
LIQ_DAYS = 20
PRE_DAYS = 10                                 # 선행성 지표 D−10→D

SIGNAL_START = date(2025, 9, 12)
SIGNAL_END = date(2026, 9, 22)
LOAD_FROM = date(2024, 6, 1)                  # VIDYA·MA20 워밍업
PRIMARY_H = 2
SECONDARY_H = 5
RESAMPLES = 4000
SEED = 20261012

#: 민감도 격자 (보조 — 주 결과로 올리지 않는다, 사전등록 §3)
SENS_GRID = [(c, v) for c in (9, 14) for v in (14, 20, 30)]


# ─────────────────────────────────────────────────────────────
# 지표
# ─────────────────────────────────────────────────────────────

def rolling_mean(x: np.ndarray, n: int) -> np.ndarray:
    """길이 n 이동평균. 앞 n−1개는 NaN. **그 창에 NaN이 있을 때만** NaN.

    cumsum만 쓰면 NaN 하나가 뒤의 모든 합을 오염시켜 그 종목의 MA20이 영구히
    NaN이 된다(일봉 한 칸 결측으로 게이트가 영구 탈락). NaN을 0으로 바꿔
    합하고, 창 안 NaN 개수를 따로 세어 그 창만 무효로 만든다.
    """
    x = np.asarray(x, float)
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    bad = ~np.isfinite(x)
    c = np.concatenate(([0.0], np.cumsum(np.where(bad, 0.0, x))))
    nb = np.concatenate(([0], np.cumsum(bad.astype(int))))
    s = c[n:] - c[:-n]
    holes = nb[n:] - nb[:-n]
    out[n - 1:] = np.where(holes == 0, s / n, np.nan)
    return out


def accel_from_flow(f: np.ndarray, recent_days: int = None,
                    prior_days: int = None) -> dict:
    """캘린더 위치별 가속 지표. f는 캘린더 길이, 흐름 없는 날은 NaN.

    `있음`은 D−(n−1)…D의 **n거래일 연속** 흐름 존재다(n = 최근 + 이전).
    인자를 비우면 accel-1.0 그대로 **최근 3 + 이전 7 = 10거래일**이고,
    VIDYA·TRUECVD 사전등록이 쓴 정의가 이것이다 — **기본값은 바꾸지 않는다.**
    인자는 창을 바꾼 설계(`sides-1.0`: 최근 5 + 이전 10)가 같은 식을 쓰기
    위한 것이다.
    """
    r_n = RECENT_DAYS if recent_days is None else int(recent_days)
    p_n = PRIOR_DAYS if prior_days is None else int(prior_days)
    need = r_n + p_n
    if r_n < 1 or p_n < 1:
        raise ValueError(f"최근·이전은 1 이상이어야 한다 — {r_n}, {p_n}")

    T = len(f)
    have = np.isfinite(f)
    run = np.zeros(T, int)                      # 연속 존재 길이
    for k in range(T):
        run[k] = run[k - 1] + 1 if have[k] and k > 0 else int(have[k])
    ok = run >= need

    recent = np.full(T, np.nan)
    prior = np.full(T, np.nan)
    z = np.where(have, f, 0.0)
    c = np.concatenate(([0.0], np.cumsum(z)))
    # 최근 = mean(f, D−(r−1)…D), 이전 = mean(f, D−(need−1)…D−r)
    idx = np.arange(T)
    m = idx >= need - 1
    r_hi = idx[m] + 1
    recent[m] = (c[r_hi] - c[r_hi - r_n]) / r_n
    p_hi = r_hi - r_n
    prior[m] = (c[p_hi] - c[p_hi - p_n]) / p_n
    recent[~ok] = np.nan
    prior[~ok] = np.nan
    return {"recent": recent, "prior": prior, "accel": recent - prior,
            "ok": ok, "recent_days": r_n, "prior_days": p_n, "need_days": need}


# ─────────────────────────────────────────────────────────────
# 데이터
# ─────────────────────────────────────────────────────────────

def load_flow(path=FLOW_PATH) -> dict:
    """{security_id: {date: f}} — 정규장 1분봉 일별 정규화 CVD 흐름."""
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    out = {}
    for sid, days in raw.items():
        out[sid] = {date.fromisoformat(d): float(v["f"]) for d, v in days.items()}
    return out


def build_panel(log=print, cmo_len=CMO_LENGTH, vidya_len=VIDYA_LENGTH) -> dict:
    """신호·수익률·집단 패널. 민감도 격자는 (cmo_len, vidya_len)만 바꿔 다시 부른다."""
    cost = json.load(open(os.path.join(_BASE, "configs", "costs", "krx_v1.json"),
                          encoding="utf-8"))
    cm, tax, sl = cost["commission_rate"], cost["tax_rate_sell"], cost["slippage_rate"]

    cal_all, _, _ = bb.load_calendar_and_index()
    cal = [d for d in cal_all if d >= LOAD_FROM]
    pos = {d: k for k, d in enumerate(cal)}
    T = len(cal)
    cal_arr = np.array(cal, dtype="datetime64[D]")
    in_period = ((cal_arr >= np.datetime64(SIGNAL_START))
                 & (cal_arr <= np.datetime64(SIGNAL_END)))

    flow = load_flow()
    intervals = bb.load_universe_intervals()
    univ_sids = {s for iv in intervals.values() for s in iv}
    sids = sorted(univ_sids & set(flow))
    log(f"흐름 {len(flow)}종목 · 유니버스 이력 {len(univ_sids)}종목 → 교집합 {len(sids)}종목")
    log(f"일봉 로드 ({LOAD_FROM}~) — {len(sids)}종목")
    bars = bb.load_all_bars(set(sids), load_from=LOAD_FROM)

    S = len(sids)
    horizons = (PRIMARY_H, SECONDARY_H)
    net = {h: np.full((S, T), np.nan) for h in horizons}
    gross = {h: np.full((S, T), np.nan) for h in horizons}
    member = np.zeros((S, T), bool)
    accel_ok = np.zeros((S, T), bool)           # 가속 입력이 갖춰진 (종목, 날)
    gate = np.zeros((S, T), bool)               # ACCEL 5조건
    v1 = np.zeros((S, T), bool)                 # 종가 > VIDYA
    v2 = np.zeros((S, T), bool)                 # VIDYA 상승
    pre10 = np.full((S, T), np.nan)
    score_in = {k: np.full((S, T), np.nan) for k in ("accel", "recent", "liq")}

    for si, sid in enumerate(sids):
        for iv in intervals.values():
            for start, end in iv.get(sid, []):
                a = np.searchsorted(cal_arr, np.datetime64(start))
                b = T if end is None else np.searchsorted(cal_arr, np.datetime64(end))
                member[si, a:b] = True

        rows = [r for r in bars.get(sid, []) if r[0] in pos]
        if len(rows) < MA_DAYS:
            continue
        k = np.array([pos[r[0]] for r in rows])
        close = np.array([r[3] for r in rows], float)
        value = np.array([r[5] for r in rows], float)

        ma = rolling_mean(close, MA_DAYS)
        liq = rolling_mean(value, LIQ_DAYS)
        vd = vidya(close, vidya_len, cmo_len)
        pre = np.full(len(close), np.nan)
        pre[PRE_DAYS:] = close[PRE_DAYS:] / close[:-PRE_DAYS] - 1

        # 캘린더 격자로 흩뿌린다 — 수익률·흐름과 같은 좌표계
        o_cal = np.full(T, np.nan)
        c_cal = np.full(T, np.nan)
        o_cal[k] = [r[1] for r in rows]
        c_cal[k] = close
        for h in horizons:
            ent = np.full(T, np.nan)
            ex = np.full(T, np.nan)
            ent[:T - 1] = o_cal[1:]
            ex[:T - h] = c_cal[h:]
            net[h][si] = bb.net_return(ent, ex, cm, tax, sl)
            gross[h][si] = ex / ent - 1

        ma_cal = np.full(T, np.nan)
        liq_cal = np.full(T, np.nan)
        vd_cal = np.full(T, np.nan)
        vd_prev = np.full(T, np.nan)
        ma_cal[k] = ma
        liq_cal[k] = liq
        vd_cal[k] = vd
        vd_prev[k[1:]] = vd[:-1]                # 직전 **봉**의 VIDYA
        pre10[si, k] = pre

        f_cal = np.full(T, np.nan)
        fd = flow.get(sid, {})
        for d, val in fd.items():
            kk = pos.get(d)
            if kk is not None:
                f_cal[kk] = val
        ac = accel_from_flow(f_cal)

        have_bar = np.isfinite(c_cal)
        cond_flow = ac["ok"] & np.isfinite(ac["accel"])
        cond_a = np.nan_to_num(ac["accel"], nan=-1.0) > 0
        cond_recent = np.nan_to_num(ac["recent"], nan=-1.0) > 0
        cond_liq = np.isfinite(liq_cal)
        cond_heat = np.isfinite(ma_cal) & (c_cal <= ma_cal * OVERHEAT_MAX)

        accel_ok[si] = cond_flow & have_bar & cond_liq & np.isfinite(ma_cal)
        gate[si] = (accel_ok[si] & cond_a & cond_recent & cond_heat
                    & member[si] & in_period)
        v1[si] = np.isfinite(vd_cal) & (c_cal > vd_cal)
        v2[si] = np.isfinite(vd_cal) & np.isfinite(vd_prev) & (vd_cal > vd_prev)
        score_in["accel"][si] = ac["accel"]
        score_in["recent"][si] = ac["recent"]
        score_in["liq"][si] = liq_cal

    return {"cal": cal, "cal_arr": cal_arr, "T": T, "S": S, "sids": sids,
            "net": net, "gross": gross, "member": member, "gate": gate,
            "accel_ok": accel_ok, "v1": v1, "v2": v2, "pre10": pre10,
            "score_in": score_in, "in_period": in_period, "horizons": horizons,
            "cost": cost, "params": {"cmo": cmo_len, "vidya": vidya_len}}


# ─────────────────────────────────────────────────────────────
# 날짜별 짝지은 계열
# ─────────────────────────────────────────────────────────────

def paired_split(P, h, cond) -> dict:
    """날짜별 `mean(가속∧조건) − mean(가속∧¬조건)`. 한쪽이 빈 날짜는 제외."""
    g, r = P["gate"], P["net"][h]
    a_mask = g & cond & np.isfinite(r)
    b_mask = g & ~cond & np.isfinite(r)
    out, dates, drop = [], [], 0
    for k in np.flatnonzero(P["in_period"]):
        ia, ib = a_mask[:, k], b_mask[:, k]
        na, nb = int(ia.sum()), int(ib.sum())
        if na == 0 or nb == 0:
            drop += 1 if (na + nb) else 0
            continue
        out.append(r[ia, k].mean() - r[ib, k].mean())
        dates.append(P["cal"][k])
    return {"x": np.asarray(out, float), "dates": dates, "dropped_days": drop}


def paired_universe(P, h, cond=None) -> dict:
    """날짜별 `mean(가속[∧조건]) − mean(그날 유니버스 전체)`."""
    g, r, mem = P["gate"], P["net"][h], P["member"]
    sel = g if cond is None else (g & cond)
    sel = sel & np.isfinite(r)
    ref = mem & np.isfinite(r)
    out, dates = [], []
    for k in np.flatnonzero(P["in_period"]):
        s, u = sel[:, k], ref[:, k]
        if not s.any() or not u.any():
            continue
        out.append(r[s, k].mean() - r[u, k].mean())
        dates.append(P["cal"][k])
    return {"x": np.asarray(out, float), "dates": dates}


def group_stats(P, h, cond=None) -> dict:
    """집단 요약 — 신호 수, 평균 순수익, 선행성 지표(D−10→D)."""
    g, r = P["gate"], P["net"][h]
    sel = g if cond is None else (g & cond)
    m = sel & np.isfinite(r)
    pre = P["pre10"][sel & np.isfinite(P["pre10"])]
    return {"n_signals": int(sel.sum()),
            "n_with_return": int(m.sum()),
            "mean_net": float(r[m].mean()) if m.any() else None,
            "median_net": float(np.median(r[m])) if m.any() else None,
            "pre10_mean": float(pre.mean()) if pre.size else None,
            "pre10_up_ratio": float((pre > 0).mean()) if pre.size else None}


def top5_rotation(P, h, cond=None) -> dict:
    """보조 — 날짜별 점수 상위 5종목 동일가중 h일 순수익 평균.

    원래 가속 검증의 ARM/STOP/TRAIL 청산 엔진이 **아니다**. 점수는 스캐너와
    같은 배점(가속 60 · 최근 흐름 20 · 유동성 20)이나 백분위 모집단은 그날
    가속 입력이 갖춰진 종목(`accel_ok`)이다.
    """
    g, r = P["gate"], P["net"][h]
    sel = g if cond is None else (g & cond)
    si = P["score_in"]
    per_day, trades = [], 0
    for k in np.flatnonzero(P["in_period"]):
        pool = P["accel_ok"][:, k]
        cand = sel[:, k] & np.isfinite(r[:, k])
        if not cand.any() or pool.sum() < 10:
            continue
        sc = np.zeros(P["S"])
        for key, w in (("accel", 60.0), ("recent", 20.0), ("liq", 20.0)):
            v = si[key][:, k]
            ok = pool & np.isfinite(v)
            if ok.sum() < 2:
                continue
            vals = v[ok]
            rank = vals.argsort().argsort() / (len(vals) - 1)
            tmp = np.zeros(P["S"])
            tmp[ok] = rank
            sc += w * tmp
        pick = np.flatnonzero(cand)
        pick = pick[np.argsort(-sc[pick])][:5]
        per_day.append(float(r[pick, k].mean()))
        trades += len(pick)
    x = np.asarray(per_day, float)
    return {"n_days": int(x.size), "trades": trades,
            "mean_net": float(x.mean()) if x.size else None,
            "win_days": float((x > 0).mean()) if x.size else None, "x": x}


# ─────────────────────────────────────────────────────────────
# 본체
# ─────────────────────────────────────────────────────────────

def run(resamples: int, log=print) -> dict:
    with open(PREREG_PATH, "rb") as fh:
        sha = hashlib.sha256(fh.read()).hexdigest()
    log(f"사전등록 sha256 {sha[:8]}")

    P = build_panel(log)
    log(f"캘린더 {P['T']}거래일 · 신호 구간 {int(P['in_period'].sum())}거래일 · "
        f"가속 통과 {int(P['gate'].sum()):,}건")

    cond = {"V1": P["v1"], "V2": P["v2"]}
    block = PRIMARY_H

    # ── 주가설 4개 ──
    primary = {}
    primary["H1"] = {"name": "V1이 가속 집합을 가른다 (ACCEL_V1 − ACCEL_NOT_V1)",
                     **paired_split(P, PRIMARY_H, cond["V1"])}
    primary["H2"] = {"name": "V2가 가속 집합을 가른다 (ACCEL_V2 − ACCEL_NOT_V2)",
                     **paired_split(P, PRIMARY_H, cond["V2"])}
    primary["H3"] = {"name": "V1 추가가 유니버스를 이긴다 (ACCEL_V1 − UNIV)",
                     **paired_universe(P, PRIMARY_H, cond["V1"])}
    primary["H4"] = {"name": "V2 추가가 유니버스를 이긴다 (ACCEL_V2 − UNIV)",
                     **paired_universe(P, PRIMARY_H, cond["V2"])}

    order = ["H1", "H2", "H3", "H4"]
    for key in order:
        d = primary[key]
        d["stats"] = bb.summarize(d["x"], block, resamples, SEED)
        d.pop("x")
        d.pop("dates", None)
    pvals = [primary[k]["stats"].get("p", 1.0) for k in order]
    qs = bb.bh_fdr(pvals)
    for key, q in zip(order, qs):
        primary[key]["q"] = q
        primary[key]["verdict"] = bb.verdict(primary[key]["stats"], q)
        log(f"  {key} {primary[key]['verdict']:<22} "
            f"평균 {primary[key]['stats'].get('mean', float('nan')):+.5f} "
            f"q={q:.4f}")

    # ── 보조 (서술용) ──
    aux = {"baseline_accel_vs_univ": {}, "groups": {}, "secondary_h": {},
           "coverage": {}, "top5": {}, "sensitivity": {}}

    for h in P["horizons"]:
        s = paired_universe(P, h)
        aux["baseline_accel_vs_univ"][h] = {
            **bb.summarize(s["x"], h, resamples, SEED),
            "name": "ACCEL − UNIV (기준선)"}

    for h in P["horizons"]:
        aux["groups"][h] = {
            "ACCEL": group_stats(P, h),
            "ACCEL_V1": group_stats(P, h, cond["V1"]),
            "ACCEL_NOT_V1": group_stats(P, h, ~cond["V1"] & P["gate"]),
            "ACCEL_V2": group_stats(P, h, cond["V2"]),
            "ACCEL_NOT_V2": group_stats(P, h, ~cond["V2"] & P["gate"])}

    h2 = SECONDARY_H
    aux["secondary_h"][h2] = {
        "H1": bb.summarize(paired_split(P, h2, cond["V1"])["x"], h2, resamples, SEED),
        "H2": bb.summarize(paired_split(P, h2, cond["V2"])["x"], h2, resamples, SEED),
        "H3": bb.summarize(paired_universe(P, h2, cond["V1"])["x"], h2, resamples, SEED),
        "H4": bb.summarize(paired_universe(P, h2, cond["V2"])["x"], h2, resamples, SEED)}

    n_gate = int(P["gate"].sum())
    days = np.flatnonzero(P["in_period"])
    aux["coverage"] = {
        "accel_signals": n_gate,
        "v1_pass": int((P["gate"] & cond["V1"]).sum()),
        "v2_pass": int((P["gate"] & cond["V2"]).sum()),
        "v1_ratio": float((P["gate"] & cond["V1"]).sum() / n_gate) if n_gate else None,
        "v2_ratio": float((P["gate"] & cond["V2"]).sum() / n_gate) if n_gate else None,
        "both_pass": int((P["gate"] & cond["V1"] & cond["V2"]).sum()),
        "signal_days": int(((P["gate"][:, days]).sum(axis=0) > 0).sum()),
        "per_day_median": float(np.median(P["gate"][:, days].sum(axis=0))),
        "per_day_median_v1": float(np.median((P["gate"] & cond["V1"])[:, days].sum(axis=0))),
        "per_day_median_v2": float(np.median((P["gate"] & cond["V2"])[:, days].sum(axis=0)))}

    for label, c in (("ACCEL", None), ("ACCEL_V1", cond["V1"]), ("ACCEL_V2", cond["V2"])):
        t = top5_rotation(P, PRIMARY_H, c)
        t.pop("x")
        aux["top5"][label] = t

    log("민감도 격자 (보조)")
    for cl, vl in SENS_GRID:
        key = f"cmo{cl}_vidya{vl}"
        if (cl, vl) == (CMO_LENGTH, VIDYA_LENGTH):
            aux["sensitivity"][key] = {
                "H1_mean": primary["H1"]["stats"].get("mean"),
                "H2_mean": primary["H2"]["stats"].get("mean"),
                "note": "주 설정"}
            continue
        Q = build_panel(log=lambda *a: None, cmo_len=cl, vidya_len=vl)
        h1 = paired_split(Q, PRIMARY_H, Q["v1"])["x"]
        h2x = paired_split(Q, PRIMARY_H, Q["v2"])["x"]
        aux["sensitivity"][key] = {
            "H1_mean": float(h1.mean()) if h1.size else None,
            "H2_mean": float(h2x.mean()) if h2x.size else None,
            "v1_ratio": float((Q["gate"] & Q["v1"]).sum() / max(1, Q["gate"].sum())),
            "v2_ratio": float((Q["gate"] & Q["v2"]).sum() / max(1, Q["gate"].sum()))}
        log(f"  {key}: H1 {aux['sensitivity'][key]['H1_mean']:+.5f} "
            f"H2 {aux['sensitivity'][key]['H2_mean']:+.5f}")

    return {
        "prereg": {"path": os.path.relpath(PREREG_PATH, _BASE), "sha256": sha},
        "run_id": f"vidya_{datetime.utcnow():%Y%m%d%H%M%S}_{uuid.uuid4().hex[:6]}",
        "design": {"name": "accel-1.0 + VIDYA", "recent": RECENT_DAYS,
                   "prior": PRIOR_DAYS, "overheat_max_pct": 10.0,
                   "cmo_length": CMO_LENGTH, "vidya_length": VIDYA_LENGTH,
                   "cmo_scale": "-1..+1", "price": "기준 일봉 REGULAR 종가"},
        "period": [str(SIGNAL_START), str(SIGNAL_END)],
        "trading_days": int(P["in_period"].sum()),
        "securities": len(P["sids"]),
        "horizons": {"primary": PRIMARY_H, "secondary": SECONDARY_H},
        "stats_config": {"bootstrap": "moving_block", "block": block,
                         "resamples": resamples, "seed": SEED,
                         "z_mde": bb.Z_MDE, "fdr": "BH over 4 primary"},
        "cost": P["cost"],
        "primary": primary,
        "aux": aux,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--resamples", type=int, default=RESAMPLES)
    ap.add_argument("--no-save", action="store_true")
    a = ap.parse_args()
    res = run(a.resamples)
    if not a.no_save:
        os.makedirs(OUT_DIR, exist_ok=True)
        path = os.path.join(OUT_DIR, f"prereg-vidya-{res['prereg']['sha256'][:8]}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=1, default=str)
        print(f"저장: {os.path.relpath(path, _BASE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

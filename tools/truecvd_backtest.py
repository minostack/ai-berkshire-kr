#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
사전등록 TRUECVD — 공급자 체결량으로 만든 일별 CVD로 두 설계를 시뮬레이션한다.

약속: `docs/PREREGISTRATION_TRUECVD.md` — 이 파일은 그 문서에 적힌 것**만**
계산한다. 결과 JSON에 문서의 sha256을 박는다.

설계 A `accel(f_true)`   — accel-1.0 게이트 5개, f만 체결량으로 교체
설계 B `v15daily(f_true)` — V1.5 정규장 절대조건 중 일별 재현 가능한 5개
                            (30분 모멘텀은 장중 데이터가 없어 **뺀다**)

재사용(새로 짜지 않는다):
  · `ab_backtest_b` 캘린더·지수·유니버스·일봉·비용·부트스트랩·BH·판정
  · `accel_vidya_backtest.accel_from_flow` 가속 지표(연속 10거래일 규칙)
  · `accel_vidya_backtest.rolling_mean`    NaN을 창 단위로만 무효화하는 이동평균
  · `cvd_obv.ingestion.kis_trade_sides.read_trade_sides` 체결량 reader
  · OBV는 V1.5 `calc_obv`의 벡터화판 — `tests/unit/test_truecvd_backtest.py`가
    같은 값임을 고정한다

    python tools/truecvd_backtest.py
    python tools/truecvd_backtest.py --resamples 500 --no-save   # 연결 확인
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

import ab_backtest_b as bb                      # noqa: E402
import accel_vidya_backtest as av               # noqa: E402

from cvd_obv.ingestion.kis_trade_sides import read_trade_sides  # noqa: E402

PREREG_PATH = os.path.join(_BASE, "docs", "PREREGISTRATION_TRUECVD.md")
OUT_DIR = os.path.join(_BASE, "reports", "backtest")

# ── 사전등록 §1·§2·§3 — 바꾸면 약속 위반 ──
SIGNAL_START = date(2010, 6, 8)
SIGNAL_END = date(2026, 10, 9)
LOAD_FROM = date(2009, 6, 1)
PRIMARY_H = 2
SECONDARY_H = 5
RESAMPLES = 4000
SEED = 20261019
MA_DAYS = 20
LIQ_DAYS = 20
PRE_DAYS = 10
V15_WINDOW = 10            # V1.5 기본 창(최근 10거래일)
V15_BULL_MIN = 0.50
OBV_LOOKBACK = 5           # calc_obv 기본값
SUBPERIODS = (("2010-2018", date(2010, 1, 1), date(2017, 12, 31)),
              ("2018-2026", date(2018, 1, 1), date(2026, 12, 31)))


# ─────────────────────────────────────────────────────────────
# OBV — V1.5 calc_obv의 벡터화판
# ─────────────────────────────────────────────────────────────

def obv_series(close, volume, lookback: int = OBV_LOOKBACK,
               ma_days: int = None) -> dict:
    """각 봉을 기준일로 본 OBV 지표. V1.5 `calc_obv`와 같은 값이어야 한다.

    정의(V1.5 그대로):
        obv[0] = 0, obv[i] = obv[i−1] ± volume[i] (종가 상승/하락), 같으면 유지
        obv_ma20 = 최근 `ma_days`개 obv의 평균 (모자라면 없음)
                   `ma_days`를 비우면 20 — 8·10번 사전등록 값이다
        slope    = obv[i] − obv[i−k],  k = min(lookback, i)
        창 거래량 = volume[i−k+1 … i]          ← **비대칭이다**(V1.5 정의)
        normalized_slope = slope / 창 거래량
    """
    c = np.asarray(close, float)
    v = np.asarray(volume, float)
    n = len(c)
    step = np.zeros(n)
    if n > 1:
        d = np.sign(np.diff(c))
        step[1:] = d * v[1:]
    obv = np.cumsum(step)

    man = MA_DAYS if ma_days is None else int(ma_days)
    ma20 = av.rolling_mean(obv, man)

    slope = np.full(n, np.nan)
    win_vol = np.full(n, np.nan)
    k = lookback
    if n > k:
        slope[k:] = obv[k:] - obv[:-k]
        cv = np.concatenate(([0.0], np.cumsum(v)))
        # volume[i−k+1 … i] 의 합
        win_vol[k - 1:] = cv[k:] - cv[:-k]
    with np.errstate(invalid="ignore", divide="ignore"):
        nslope = np.where(win_vol > 0, slope / win_vol, np.nan)

    # V1.5는 봉이 max(6, lookback+1) 미만이면 전부 None
    need = max(6, lookback + 1)
    if n < need:
        return {"obv": np.full(n, np.nan), "obv_ma20": np.full(n, np.nan),
                "normalized_slope": np.full(n, np.nan)}
    bad = np.arange(n) < need - 1
    obv_out = obv.astype(float).copy()
    obv_out[bad] = np.nan
    ma20 = ma20.copy()
    ma20[bad] = np.nan
    nslope = nslope.copy()
    nslope[bad] = np.nan
    return {"obv": obv_out, "obv_ma20": ma20, "normalized_slope": nslope}


def window_flow(f, window: int = V15_WINDOW, weight=None) -> dict:
    """최근 `window` 거래일의 창 흐름과 매수우위일 비율.

    `weight`가 있으면 가중 평균(= Σ(buy−sell)/Σ(buy+sell))이 되고, 없으면
    단순 평균이다. V1.5의 `normalized_delta`가 거래량 가중이므로 체결량
    합계를 가중치로 넣는다.
    """
    f = np.asarray(f, float)
    n = len(f)
    have = np.isfinite(f)
    w = np.ones(n) if weight is None else np.asarray(weight, float)
    w = np.where(have & np.isfinite(w), w, 0.0)
    num = np.where(have, f, 0.0) * w
    pos = (have & (f > 0)).astype(float)

    cn = np.concatenate(([0.0], np.cumsum(num)))
    cw = np.concatenate(([0.0], np.cumsum(w)))
    cp = np.concatenate(([0.0], np.cumsum(pos)))
    ch = np.concatenate(([0], np.cumsum(have.astype(int))))

    nd = np.full(n, np.nan)
    bull = np.full(n, np.nan)
    days = np.zeros(n, int)
    if n >= window:
        sl = slice(window - 1, n)
        ws = cw[window:] - cw[:-window]
        ns = cn[window:] - cn[:-window]
        ps = cp[window:] - cp[:-window]
        hs = ch[window:] - ch[:-window]
        nd[sl] = np.where(ws > 0, ns / ws, np.nan)
        bull[sl] = np.where(hs > 0, ps / hs, np.nan)
        days[sl] = hs
    return {"nd": nd, "bull": bull, "days": days}


# ─────────────────────────────────────────────────────────────
# 패널
# ─────────────────────────────────────────────────────────────

def load_sides(securities, start: date, end: date,
               market_div: str = "J") -> dict:
    """{security_id: {date: (buy, sell)}} — 두 수가 0인 행은 흐름 없음으로 뺀다.

    `market_div`는 **섞지 않는다.** 기본 `"J"`(KRX 단독)이 TRUECVD 사전등록
    §1.1의 약속이다. 다른 값은 `PREREGISTRATION_NXTBASIS.md`가 쓴다.
    """
    import pyarrow.dataset as ds
    from cvd_obv.contracts.market_schema import TRADE_SIDE_ZERO_FLAG
    t = read_trade_sides(
        columns=["security_id", "trading_date", "buy_qty", "sell_qty",
                 "market_div", "quality_flags"],
        filters=(ds.field("trading_date") >= start)
        & (ds.field("trading_date") <= end)
        & (ds.field("market_div") == market_div)
        & ds.field("security_id").isin(sorted(securities)))
    out = {}
    zero = 0
    for sid, d, b, s, flags in zip(
            t.column("security_id").to_pylist(),
            t.column("trading_date").to_pylist(),
            t.column("buy_qty").to_pylist(),
            t.column("sell_qty").to_pylist(),
            t.column("quality_flags").to_pylist()):
        if TRADE_SIDE_ZERO_FLAG in (flags or []) or (b + s) <= 0:
            zero += 1
            continue
        out.setdefault(sid, {})[d] = (int(b), int(s))
    return out, zero


def build_panel(log=print, *, signal_start: date = None, signal_end: date = None,
                load_from: date = None, market_div: str = "J",
                bars: dict = None, sides: dict = None,
                zero_rows: int = None, recent_days: int = None,
                prior_days: int = None, v15_window: int = None,
                ma_days: int = None) -> dict:
    """신호·게이트·순수익 패널.

    **인자를 비우면 TRUECVD 사전등록(§1·§2) 그대로**다 — 그 결과를 재현할 때는
    `build_panel(log)`로 부른다. 인자는 `PREREGISTRATION_NXTBASIS.md`가
    같은 게이트 정의를 다른 기준·기간에 쓰기 위한 것이고, 기본값은 바꾸지 않는다.

    `bars`·`sides`를 넘기면 다시 읽지 않는다(두 기준을 돌릴 때 일봉을 한 번만
    읽기 위함). `sides`를 넘길 때는 `zero_rows`도 함께 넘긴다.

    `recent_days`·`prior_days`·`v15_window`는 창을 바꾼 설계
    (`PREREGISTRATION_WINDOW15.md`)가 같은 게이트를 다른 길이로 쓰기 위한 것이다.
    비우면 TRUECVD 사전등록 그대로 **3 + 7 = 10** / **10**이다.
    `ma_days`는 MA 길이(종가 대비 게이트와 OBV MA에 함께 먹는다) —
    비우면 **20**이다. 유동성 창(`LIQ_DAYS`)은 따라 움직이지 않는다.
    """
    signal_start = signal_start or SIGNAL_START
    signal_end = signal_end or SIGNAL_END
    load_from = load_from or LOAD_FROM
    r_n = av.RECENT_DAYS if recent_days is None else int(recent_days)
    p_n = av.PRIOR_DAYS if prior_days is None else int(prior_days)
    v15 = V15_WINDOW if v15_window is None else int(v15_window)
    ma_n = MA_DAYS if ma_days is None else int(ma_days)

    cost = json.load(open(os.path.join(_BASE, "configs", "costs", "krx_v1.json"),
                          encoding="utf-8"))
    cm, tax, sl = cost["commission_rate"], cost["tax_rate_sell"], cost["slippage_rate"]

    log("캘린더·지수 로드")
    cal_all, idx_open, idx_close = bb.load_calendar_and_index()
    keep = [i for i, d in enumerate(cal_all) if d >= load_from]
    cal = [cal_all[i] for i in keep]
    idx_open = np.asarray(idx_open)[keep]
    idx_close = np.asarray(idx_close)[keep]
    pos = {d: k for k, d in enumerate(cal)}
    T = len(cal)
    cal_arr = np.array(cal, dtype="datetime64[D]")
    in_period = ((cal_arr >= np.datetime64(signal_start))
                 & (cal_arr <= np.datetime64(signal_end)))

    log("유니버스 이력 로드")
    intervals = bb.load_universe_intervals()
    sids = sorted({s for iv in intervals.values() for s in iv})
    if bars is None:
        log(f"일봉 로드 ({load_from}~) — {len(sids)}종목 (느림)")
        bars = bb.load_all_bars(set(sids), load_from=load_from)
    if sides is None:
        log(f"체결량 로드 (market_div={market_div})")
        sides, zero_rows = load_sides(sids, load_from, signal_end, market_div)
        log(f"  체결량 {sum(len(v) for v in sides.values()):,}행 "
            f"({len(sides)}종목) · 흐름 없음(0/0) {zero_rows:,}행")

    S = len(sids)
    horizons = (PRIMARY_H, SECONDARY_H)
    net = {h: np.full((S, T), np.nan, dtype=np.float32) for h in horizons}
    member = np.zeros((S, T), bool)
    gate_a = np.zeros((S, T), bool)
    gate_b = np.zeros((S, T), bool)
    ok_a = np.zeros((S, T), bool)      # 설계 A 입력이 갖춰진 (종목,날)
    ok_b = np.zeros((S, T), bool)
    pre10 = np.full((S, T), np.nan, dtype=np.float32)
    score_in = {k: np.full((S, T), np.nan, dtype=np.float32)
                for k in ("accel", "recent", "liq")}

    log("지표·수익률 계산")
    for si, sid in enumerate(sids):
        for iv in intervals.values():
            for a, b in iv.get(sid, []):
                i0 = np.searchsorted(cal_arr, np.datetime64(a))
                i1 = T if b is None else np.searchsorted(cal_arr, np.datetime64(b))
                member[si, i0:i1] = True

        rows = [r for r in bars.get(sid, []) if r[0] in pos]
        if len(rows) < ma_n:
            continue
        k = np.array([pos[r[0]] for r in rows])
        close = np.array([r[3] for r in rows], float)
        volume = np.array([r[4] for r in rows], float)
        value = np.array([r[5] for r in rows], float)

        ma = av.rolling_mean(close, ma_n)
        # 유동성은 MA 길이와 무관하다 — 데이터 가용성 판정용이라 20일 고정
        liq = av.rolling_mean(value, LIQ_DAYS)
        ob = obv_series(close, volume, ma_days=ma_n)
        pre = np.full(len(close), np.nan)
        pre[PRE_DAYS:] = close[PRE_DAYS:] / close[:-PRE_DAYS] - 1

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

        ma_cal = np.full(T, np.nan); ma_cal[k] = ma
        liq_cal = np.full(T, np.nan); liq_cal[k] = liq
        obv_cal = np.full(T, np.nan); obv_cal[k] = ob["obv"]
        obvma_cal = np.full(T, np.nan); obvma_cal[k] = ob["obv_ma20"]
        nsl_cal = np.full(T, np.nan); nsl_cal[k] = ob["normalized_slope"]
        pre10[si, k] = pre

        # f_true를 캘린더 격자에
        f_cal = np.full(T, np.nan)
        w_cal = np.full(T, np.nan)
        for d, (bq, sq) in sides.get(sid, {}).items():
            kk = pos.get(d)
            if kk is not None:
                f_cal[kk] = (bq - sq) / (bq + sq)
                w_cal[kk] = bq + sq
        ac = av.accel_from_flow(f_cal, r_n, p_n)
        wf = window_flow(f_cal, v15, w_cal)

        have_bar = np.isfinite(c_cal)
        liq_ok = np.isfinite(liq_cal)
        ma_ok = np.isfinite(ma_cal)

        # ── 설계 A: accel-1.0 게이트 5개 ──
        ok_a[si] = (ac["ok"] & np.isfinite(ac["accel"]) & have_bar
                    & liq_ok & ma_ok & member[si] & in_period)
        gate_a[si] = (ok_a[si]
                      & (np.nan_to_num(ac["accel"], nan=-1.0) > 0)
                      & (np.nan_to_num(ac["recent"], nan=-1.0) > 0)
                      & (c_cal <= ma_cal * 1.10))

        # ── 설계 B: v15daily 게이트 5개 (모멘텀 제외) ──
        obv_ok = (np.isfinite(nsl_cal) & (nsl_cal > 0)
                  & np.isfinite(obv_cal) & np.isfinite(obvma_cal)
                  & (obv_cal > obvma_cal))
        ok_b[si] = (np.isfinite(wf["nd"]) & np.isfinite(wf["bull"])
                    & (wf["days"] >= v15) & have_bar & liq_ok & ma_ok
                    & np.isfinite(nsl_cal) & np.isfinite(obvma_cal)
                    & member[si] & in_period)
        gate_b[si] = (ok_b[si]
                      & (np.nan_to_num(wf["nd"], nan=-1.0) > 0)
                      & (np.nan_to_num(wf["bull"], nan=-1.0) >= V15_BULL_MIN)
                      & (c_cal > ma_cal)
                      & obv_ok)

        score_in["accel"][si] = ac["accel"]
        score_in["recent"][si] = ac["recent"]
        score_in["liq"][si] = liq_cal

    idx_ret = {h: np.full(T, np.nan) for h in horizons}
    for h in horizons:
        idx_ret[h][:T - h] = idx_close[h:] / idx_open[1:T - h + 1] - 1

    return {"cal": cal, "cal_arr": cal_arr, "T": T, "S": S, "sids": sids,
            "net": net, "member": member, "in_period": in_period,
            "gate": {"accel": gate_a, "v15daily": gate_b},
            "ok": {"accel": ok_a, "v15daily": ok_b},
            "pre10": pre10, "score_in": score_in, "idx_ret": idx_ret,
            "horizons": horizons, "cost": cost, "zero_rows": zero_rows,
            "market_div": market_div, "bars": bars,
            "period": (signal_start, signal_end),
            "window": {"recent": r_n, "prior": p_n, "v15": v15,
                       "ma": ma_n, "liq": LIQ_DAYS}}


# ─────────────────────────────────────────────────────────────
# 계열
# ─────────────────────────────────────────────────────────────

def paired_vs_universe(P, design, h, mask=None):
    g, r, mem = P["gate"][design], P["net"][h], P["member"]
    sel = g & np.isfinite(r)
    ref = mem & np.isfinite(r)
    days = np.flatnonzero(P["in_period"] if mask is None else (P["in_period"] & mask))
    out, dates = [], []
    for k in days:
        s, u = sel[:, k], ref[:, k]
        if not s.any() or not u.any():
            continue
        out.append(float(r[s, k].mean()) - float(r[u, k].mean()))
        dates.append(P["cal"][k])
    return {"x": np.asarray(out, float), "dates": dates}


def paired_vs_index(P, design, h, mask=None):
    g, r = P["gate"][design], P["net"][h]
    sel = g & np.isfinite(r)
    ir = P["idx_ret"][h]
    days = np.flatnonzero(P["in_period"] if mask is None else (P["in_period"] & mask))
    out, dates = [], []
    for k in days:
        s = sel[:, k]
        if not s.any() or not np.isfinite(ir[k]):
            continue
        out.append(float(r[s, k].mean()) - float(ir[k]))
        dates.append(P["cal"][k])
    return {"x": np.asarray(out, float), "dates": dates}


def group_stats(P, design, h):
    g, r = P["gate"][design], P["net"][h]
    m = g & np.isfinite(r)
    pre = P["pre10"][g & np.isfinite(P["pre10"])]
    days = np.flatnonzero(P["in_period"])
    per_day = g[:, days].sum(axis=0)
    return {"signals": int(g.sum()), "with_return": int(m.sum()),
            "mean_net": float(r[m].mean()) if m.any() else None,
            "median_net": float(np.median(r[m])) if m.any() else None,
            "pass_rate": (float(g.sum() / P["ok"][design].sum())
                          if P["ok"][design].sum() else None),
            "signal_days": int((per_day > 0).sum()),
            "per_day_median": float(np.median(per_day)),
            "pre10_mean": float(pre.mean()) if pre.size else None,
            "pre10_up_ratio": float((pre > 0).mean()) if pre.size else None}


# ─────────────────────────────────────────────────────────────
# 본체
# ─────────────────────────────────────────────────────────────

def run(resamples: int, log=print) -> dict:
    with open(PREREG_PATH, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    log(f"사전등록 sha256 {sha[:8]}")

    P = build_panel(log)
    log(f"캘린더 {P['T']}거래일 · 신호 구간 {int(P['in_period'].sum())}거래일")
    for d in ("accel", "v15daily"):
        log(f"  {d}: 입력 갖춤 {int(P['ok'][d].sum()):,} · "
            f"통과 {int(P['gate'][d].sum()):,}")

    block = PRIMARY_H
    order = ["H1", "H2", "H3", "H4"]
    spec = {
        "H1": ("accel(f_true) − 유니버스", "accel", paired_vs_universe),
        "H2": ("accel(f_true) − 지수", "accel", paired_vs_index),
        "H3": ("v15daily(f_true) − 유니버스", "v15daily", paired_vs_universe),
        "H4": ("v15daily(f_true) − 지수", "v15daily", paired_vs_index),
    }
    primary = {}
    for key in order:
        name, design, fn = spec[key]
        s = fn(P, design, PRIMARY_H)
        primary[key] = {"name": name, "design": design,
                        "stats": bb.summarize(s["x"], block, resamples, SEED)}
    qs = bb.bh_fdr([primary[k]["stats"].get("p", 1.0) for k in order])
    for key, q in zip(order, qs):
        primary[key]["q"] = q
        primary[key]["verdict"] = bb.verdict(primary[key]["stats"], q)
        st = primary[key]["stats"]
        log(f"  {key} {primary[key]['verdict']:<22} "
            f"평균 {st.get('mean', float('nan')):+.5f} "
            f"n={st.get('n_days')} q={q:.4f}")

    aux = {"groups": {}, "secondary_h": {}, "subperiods": {}}
    for h in P["horizons"]:
        aux["groups"][h] = {d: group_stats(P, d, h)
                            for d in ("accel", "v15daily")}
    h2 = SECONDARY_H
    aux["secondary_h"][h2] = {}
    for key in order:
        name, design, fn = spec[key]
        aux["secondary_h"][h2][key] = bb.summarize(
            fn(P, design, h2)["x"], h2, resamples, SEED)
    for label, a, b in SUBPERIODS:
        m = ((P["cal_arr"] >= np.datetime64(a)) & (P["cal_arr"] <= np.datetime64(b)))
        aux["subperiods"][label] = {}
        for key in ("H1", "H3"):
            name, design, fn = spec[key]
            aux["subperiods"][label][key] = bb.summarize(
                fn(P, design, PRIMARY_H, mask=m)["x"], block, resamples, SEED)

    return {
        "prereg": {"path": os.path.relpath(PREREG_PATH, _BASE), "sha256": sha},
        "run_id": f"truecvd_{datetime.utcnow():%Y%m%d%H%M%S}_{uuid.uuid4().hex[:6]}",
        "designs": {
            "accel": "accel-1.0 게이트 5개, f = KIS 체결량",
            "v15daily": ("V1.5 정규장 절대조건 중 일별 재현 가능한 5개 "
                         "(30분 모멘텀 제외), f = KIS 체결량")},
        "period": [str(SIGNAL_START), str(SIGNAL_END)],
        "trading_days": int(P["in_period"].sum()),
        "securities": len(P["sids"]),
        "zero_flow_rows": P["zero_rows"],
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
        p = os.path.join(OUT_DIR, f"prereg-truecvd-{res['prereg']['sha256'][:8]}.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1, default=str)
        print(f"저장: {os.path.relpath(p, _BASE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

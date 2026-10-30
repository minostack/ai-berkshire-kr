# -*- coding: utf-8 -*-
"""사전등록 CVDSEL — CVD 종가매수 선정 조건 세 가지 완화 후보 (`docs/PREREGISTRATION_CVDSEL.md`).

    python tools/cvdsel_backtest.py [--no-save] [--reps 4000]

문서 §1의 네 집합(S0 명세 · S1 추세 완화 · S3 ATRP 완화 · S2 흐름 개선 단독)을
2010-07-01 ~ 2021-11-30 신호일에 만들고 §3의 주가설 4개를 계산한다.
결과 JSON에 문서의 sha256을 박는다.

재사용(정의가 두 벌 생기지 않게):
- 패널 `cvd_selection.research.features.build_panel`(10/25 탐색과 같은 함수)
- 통계 `ab_backtest_b.summarize`·`bh_fdr`·`verdict`·`net_return`·`load_calendar_and_index`,
  `ab_backtest_negfilter.summarize_diff`
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from datetime import date, datetime

import numpy as np
import pandas as pd

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.join(_BASE, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ab_backtest_b as bt  # noqa: E402
import ab_backtest_negfilter as nf  # noqa: E402
from cvd_selection.research.features import build_panel  # noqa: E402

PREREG_PATH = os.path.join(_BASE, "docs", "PREREGISTRATION_CVDSEL.md")

# §2
SIGNAL_START = date(2010, 7, 1)
SIGNAL_END = date(2021, 11, 30)
LOAD_FROM = date(2009, 6, 1)
#: 보조 h=60 청산까지 가격을 읽는다(신호는 SIGNAL_END까지만)
PRICE_END = date(2022, 3, 31)
H_MAIN = 20
H_AUX = (5, 60)
SEED = 20261025
BLOCK = 20

# §1.2 공통 조건
MIN_AVG_A20 = 100e8
MIN_DR5, MIN_POS5, MIN_RVOL, MIN_CLV = 5.0, 3, 1.3, 0.7
MAX_ATRP = 4.0
MIN_DDR = 10.0

SUBPERIODS = ((date(2010, 7, 1), date(2015, 12, 31)), (date(2016, 1, 1), date(2021, 11, 30)))


# ─────────────────────────────────────────────────────────────
# 집합 정의 (§1.2)
# ─────────────────────────────────────────────────────────────

def _b(s) -> np.ndarray:
    return np.asarray(pd.Series(s).fillna(False).astype(bool))


def sets(p: pd.DataFrame) -> dict:
    """{S0, S1, S3, S2, S13}: 불리언 배열. NaN 지표는 불통과."""
    liq = _b(p.avg_a20 >= MIN_AVG_A20)
    flow = _b((p.dr5 >= MIN_DR5) & (p.pos5 >= MIN_POS5) & (p.dr2 > 0)
              & (p.delta_prev_pos == True))  # noqa: E712 — object 열(True/False/None)
    common = liq & flow & _b(p.rvol >= MIN_RVOL) & _b(p.clv >= MIN_CLV)
    trend_spec = _b(p.trend) & _b(p.ma20_up)
    trend_relax = _b(p.above_ma60) & _b(p.ma20_up)
    atrp_ok = _b(p.atrp14 <= MAX_ATRP)
    ddr = p.dr5 - p.dr20
    return {
        "S0": common & trend_spec & atrp_ok,
        "S1": common & trend_relax & atrp_ok,
        "S3": common & trend_spec,
        "S2": liq & _b(ddr >= MIN_DDR),
        "S13": common & trend_relax,            # 보조 — 추세·ATRP 함께 완화
    }


# ─────────────────────────────────────────────────────────────
# 수익
# ─────────────────────────────────────────────────────────────

def add_returns(p: pd.DataFrame, cal, idx_close, cost) -> pd.DataFrame:
    """net{h}, ex{h}(그날 편입 종목 평균 순수익 대비), idx{h}(KOSPI200 종가 → 종가)."""
    cm, tax, sl = cost
    pos = {d: i for i, d in enumerate(cal)}
    di = p.date.dt.date.map(pos).to_numpy(dtype=float)
    for h in (H_MAIN,) + H_AUX:
        exit_ = p.close * (1 + p[f"fwd{h}"])
        p[f"net{h}"] = bt.net_return(p.close.to_numpy(), exit_.to_numpy(), cm, tax, sl)
        p[f"ex{h}"] = p[f"net{h}"] - p.groupby("date")[f"net{h}"].transform("mean")
        j = di + h
        ok = np.isfinite(di) & (j < len(cal))
        r = np.full(len(p), np.nan)
        ii = di[ok].astype(int)
        r[ok] = idx_close[ii + h] / idx_close[ii] - 1
        p[f"idx{h}"] = r
    return p


def load(log=print) -> pd.DataFrame:
    p = build_panel(SIGNAL_START, PRICE_END, LOAD_FROM)
    p = p[p.member].reset_index(drop=True)
    cal, _, idx_close = bt.load_calendar_and_index()
    cost = json.load(open(os.path.join(_BASE, "configs", "costs", "krx_v1.json"), encoding="utf-8"))
    p = add_returns(p, cal, idx_close,
                    (cost["commission_rate"], cost["tax_rate_sell"], cost["slippage_rate"]))
    # 수익률 패널은 PRICE_END까지 읽었다. 신호는 §2 기간만 — 평균(ex)은 그날 편입 전체라 영향 없다
    p = p[(p.date.dt.date >= SIGNAL_START) & (p.date.dt.date <= SIGNAL_END)].reset_index(drop=True)
    log(f"패널 {len(p):,} 종목·거래일 · {p.date.nunique()}거래일 · {p.code.nunique()}종목")
    return p


# ─────────────────────────────────────────────────────────────
# 통계 (§3)
# ─────────────────────────────────────────────────────────────

def quarter(dates: pd.Series) -> np.ndarray:
    return dates.dt.to_period("Q").astype(str).to_numpy()


def diff_test(p, sup: np.ndarray, base: np.ndarray, h: int, reps: int) -> dict:
    """신호 단위 두 집단 차이: mean(sup\\base의 ex) − mean(base의 ex), 분기 군집."""
    m = sup & np.isfinite(p[f"ex{h}"].to_numpy())
    added = (sup & ~base)[m]
    s = nf.summarize_diff(p[f"ex{h}"].to_numpy()[m], added, quarter(p.date[m]), reps, SEED)
    if s.get("verdict") == "INSUFFICIENT":
        return s
    s["mean"] = s["diff"]                      # bt.verdict는 "mean"을 읽는다
    return s


def day_series(p, sig: np.ndarray, h: int, vs: str) -> np.ndarray:
    """날짜별 mean(신호 순수익) − (그날 유니버스 평균 순수익 | KOSPI200 수익)."""
    net = p[f"net{h}"]
    ok = np.isfinite(net.to_numpy())
    uni = net[ok].groupby(p.date[ok]).mean()
    s = net[sig & ok].groupby(p.date[sig & ok]).mean()
    if vs == "universe":
        ref = uni.reindex(s.index)
    else:
        ref = p.loc[sig & ok].groupby("date")[f"idx{h}"].first().reindex(s.index)
    x = (s - ref).dropna()
    return x.to_numpy()


def main_tests(p, S, reps, h=H_MAIN) -> dict:
    t = {
        "H1": diff_test(p, S["S1"], S["S0"], h, reps),
        "H2": bt.summarize(day_series(p, S["S2"], h, "universe"), BLOCK, reps, SEED),
        "H3": diff_test(p, S["S3"], S["S0"], h, reps),
        "H4": bt.summarize(day_series(p, S["S2"], h, "index"), BLOCK, reps, SEED),
    }
    keys = [k for k in t if t[k].get("verdict") != "INSUFFICIENT"]
    q = bt.bh_fdr([t[k]["p"] for k in keys])
    for k in t:
        t[k]["q"] = q[keys.index(k)] if k in keys else None
        t[k]["verdict"] = bt.verdict(t[k], t[k]["q"] if t[k]["q"] is not None else 1.0)
    return t


# ─────────────────────────────────────────────────────────────
# 보조 (서술용)
# ─────────────────────────────────────────────────────────────

def set_summary(p, m: np.ndarray, h: int) -> dict:
    ok = m & np.isfinite(p[f"ex{h}"].to_numpy())
    d = p.loc[ok]
    if not len(d):
        return {"n": 0}
    return {"n": int(len(d)), "days": int(d.date.nunique()), "stocks": int(d.code.nunique()),
            "ex_mean": float(d[f"ex{h}"].mean()), "net_mean": float(d[f"net{h}"].mean()),
            "win": float((d[f"net{h}"] > 0).mean()),
            "vs_index_mean": float((d[f"net{h}"] - d[f"idx{h}"]).mean())}


def first_touch(p, m: np.ndarray, full: pd.DataFrame, down=-0.05, up=0.08, h=20) -> dict:
    """신호 종가 기준 h거래일 안에 −5%·+8% 중 무엇에 먼저 닿는가(같은 날 둘 다면 '동시')."""
    out = {"down_first": 0, "up_first": 0, "both_same_day": 0, "neither": 0}
    by = {c: g for c, g in full.groupby("code")}
    for code, d, c0 in zip(p.code[m], p.date[m], p.close[m]):
        g = by[code]
        i = g.index.get_loc(g.index[g.date == d][0])
        w = g.iloc[i + 1:i + 1 + h]
        dn = (w.low <= c0 * (1 + down)).to_numpy()
        upp = (w.high >= c0 * (1 + up)).to_numpy()
        a = np.argmax(dn) if dn.any() else 10 ** 6
        b = np.argmax(upp) if upp.any() else 10 ** 6
        k = ("neither" if a == b == 10 ** 6 else "both_same_day" if a == b
             else "down_first" if a < b else "up_first")
        out[k] += 1
    return out


def _c_over_ma20(p: pd.DataFrame) -> pd.Series:
    import pyarrow.dataset as ds
    from cvd_obv.bar_standard import read_standard_daily
    codes = sorted(p.code.unique())
    t = read_standard_daily(
        columns=["security_id", "trading_date", "close"],
        filters=(ds.field("trading_date") >= LOAD_FROM) & (ds.field("trading_date") <= SIGNAL_END)
        & ds.field("security_id").isin([f"KRX:{c}" for c in codes]),
        drop_failed=True).to_pandas()
    t["code"] = t.security_id.str.split(":").str[1]
    t["date"] = pd.to_datetime(t.trading_date)
    t["close"] = t.close.astype(float)
    t = t.sort_values(["code", "date"])
    t["ma20"] = t.groupby("code").close.transform(lambda s: s.rolling(20, min_periods=20).mean())
    m = p[["code", "date", "close"]].merge(t[["code", "date", "ma20"]], on=["code", "date"], how="left")
    return pd.Series((m.close / m.ma20).to_numpy(), index=p.index)


def aux(p, S, reps) -> dict:
    out = {"sets": {}, "subperiods": {}, "h_aux": {}}
    for h in (H_MAIN,) + H_AUX:
        out["sets"][f"h{h}"] = {k: set_summary(p, v, h) for k, v in S.items()}
    for h in H_AUX:
        out["h_aux"][f"h{h}"] = main_tests(p, S, reps, h)
    for a, b in SUBPERIODS:
        sel = ((p.date.dt.date >= a) & (p.date.dt.date <= b)).to_numpy()
        q = p.loc[sel].reset_index(drop=True)
        Sq = {k: v[sel] for k, v in S.items()}
        out["subperiods"][f"{a}~{b}"] = main_tests(q, Sq, reps)
    # S2 오분위(그날 유동성 통과 종목 안의 DR5 − DR20 백분위)
    liq = _b(p.avg_a20 >= MIN_AVG_A20) & np.isfinite((p.dr5 - p.dr20).to_numpy())
    q = p.loc[liq].copy()
    q["ddr_q"] = (q.assign(ddr=q.dr5 - q.dr20).groupby("date").ddr.rank(pct=True)
                  .mul(5).clip(upper=4.999).astype(int))
    out["ddr_quintile_ex20"] = q.groupby("ddr_q").ex20.mean().to_dict()
    # 늘어난 신호의 성격
    add1, add3 = S["S1"] & ~S["S0"], S["S3"] & ~S["S0"]
    full = build_panel(SIGNAL_START, PRICE_END, LOAD_FROM)[["code", "date", "high", "low"]]
    full = full.sort_values(["code", "date"]).reset_index(drop=True)
    # C/MA20 — 패널 열에 MA20 값이 없어 여기서 같은 정의(종가 20일 단순평균)로 낸다.
    # 패널은 신호 시작 전 워밍업을 잘라 냈으므로 LOAD_FROM부터 다시 읽은 일봉으로 계산한다
    c_ma20 = _c_over_ma20(p)
    out["added_S1"] = {
        "n": int(add1.sum()),
        "c_over_ma20_quantiles": (c_ma20[add1].quantile([.1, .25, .5, .75, .9]).to_dict()
                                  if add1.any() else {}),
        "share_c_le_ma20": float((c_ma20[add1] <= 1).mean()) if add1.any() else None,
        "first_touch": first_touch(p, add1, full),
    }
    out["added_S3"] = {
        "n": int(add3.sum()),
        "atrp14_quantiles": (p.atrp14[add3].quantile([.25, .5, .75]).to_dict() if add3.any() else {}),
        "first_touch": first_touch(p, add3, full),
    }
    out["S0_first_touch"] = first_touch(p, S["S0"], full)
    out["signals_by_year"] = {k: p.date[v].dt.year.value_counts().sort_index().to_dict()
                              for k, v in S.items()}
    return out


# ─────────────────────────────────────────────────────────────

def run(reps: int, log=print) -> dict:
    with open(PREREG_PATH, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    log(f"사전등록 sha256 {sha[:8]}")
    p = load(log)
    S = sets(p)
    for k, v in S.items():
        log(f"  {k}: 신호 {int(v.sum()):,}")
    main = main_tests(p, S, reps)
    return {
        "prereg": {"path": os.path.relpath(PREREG_PATH, _BASE), "sha256": sha},
        "run_id": f"{datetime.now():%Y%m%d_%H%M%S}-{uuid.uuid4().hex[:8]}",
        "period": {"signal_start": str(SIGNAL_START), "signal_end": str(SIGNAL_END),
                   "h": H_MAIN, "seed": SEED, "reps": reps, "block": BLOCK},
        "panel": {"rows": int(len(p)), "days": int(p.date.nunique()),
                  "stocks": int(p.code.nunique())},
        "main": main,
        "aux": aux(p, S, reps),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--reps", type=int, default=4000)
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    res = run(a.reps)
    for k, s in res["main"].items():
        mean = s.get("mean")
        ci = s.get("ci95")
        print(f"{k}: 관측 {mean if mean is None else f'{mean*100:+.3f}%p'} "
              f"구간 {ci and [round(c*100, 3) for c in ci]} MDE "
              f"{s.get('mde') and round(s['mde']*100, 3)} q {s.get('q') and round(s['q'], 4)} "
              f"→ {s['verdict']}")
    if not a.no_save:
        out_dir = os.path.join(_BASE, "reports", "backtest")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"prereg-cvdsel-{res['prereg']['sha256'][:8]}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1, default=str)
        print("저장", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

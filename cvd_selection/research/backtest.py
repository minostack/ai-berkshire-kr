# -*- coding: utf-8 -*-
"""2부 — CVD 선정 조건 격자 시뮬레이션 (학습 2022~2024 → 검증 2025-01~2026-09).

    python -m cvd_selection.research.backtest

- 모집단: 그날 코스피200·코스닥150 편입 종목(이력) 전체. 사후 선택 없음
- 신호일 D 종가 매수 → h거래일 뒤 종가(fwd5·fwd20·fwd60). 매매 규칙(-5%/+8%)은 사용자 별도
  진행이라 넣지 않았다
- 초과수익 = 신호 종목 fwd − 같은 날 편입 종목 전체 평균 fwd
- 표준오차 = 월 단위 군집(같은 종목이 며칠 연속 신호를 내고 20일 보유가 겹치므로)
- 비용 왕복 0.409%(`configs/costs/krx_v1.json`: 수수료 0.015%×2 + 매도세 0.18% + 슬리피지 0.1%×2)
- 공통 고정: AVG_A20 ≥ 100억(명세 유동성 조건 — 바꾸지 않는다)
- 1분봉 조건(VWAP·Rlate·HoldClose)은 2025-10 전 데이터가 없어 넣지 못했다

학습 구간에서 군집 t가 가장 큰 변형을 **하나** 고르고, 검증 구간은 그 변형·명세 기준선·학습 상위
10개만 한 번 본다. 격자 크기가 크므로 학습 최고값은 위로 치우친다(검증에서 줄어드는 폭을 같이 적는다).
"""

from __future__ import annotations

import itertools
import json
import os
import sys

import numpy as np
import pandas as pd

from .features import CACHE, add_xs
from .onset import windows, winners

COST = 0.00409
TRAIN = (pd.Timestamp("2022-01-03"), pd.Timestamp("2024-12-30"))
TEST = (pd.Timestamp("2025-01-02"), pd.Timestamp("2026-09-24"))   # fwd20이 10/23까지 닿는 마지막 날
MIN_N, MIN_MONTHS = 200, 12

FLOW = {
    "명세(dr5≥5·pos5≥3·dr2>0·Δ>0)": lambda d: (d.dr5 >= 5) & (d.pos5 >= 3) & (d.dr2 > 0) & (d.delta_prev_pos == 1),
    "ΔDR≥5": lambda d: d.ddr >= 5,
    "ΔDR≥10": lambda d: d.ddr >= 10,
    "ΔDR≥5·dr2>0": lambda d: (d.ddr >= 5) & (d.dr2 > 0),
    "dr5_xs≥0.6": lambda d: d.dr5_xs >= 0.6,
    "dr5_xs≥0.8": lambda d: d.dr5_xs >= 0.8,
    "흐름 없음": lambda d: np.ones(len(d), bool),
}
TREND = {
    "명세(C>MA20>MA60·MA20↑)": lambda d: d.trend & d.ma20_up,
    "C>MA60·MA20↑": lambda d: d.above_ma60 & d.ma20_up,
    "20일 돌파": lambda d: d.breakout20,
    "추세 없음": lambda d: np.ones(len(d), bool),
}
RVOL = {"rvol≥1.3": 1.3, "rvol≥2": 2.0, "rvol 없음": None}
CLV = {"clv≥0.7": 0.7, "clv≥0.5": 0.5, "clv 없음": None}
ATRP = {"atrp≤4": 4.0, "atrp≤6": 6.0, "atrp 없음": None}
SPEC = ("명세(dr5≥5·pos5≥3·dr2>0·Δ>0)", "명세(C>MA20>MA60·MA20↑)", "rvol≥1.3", "clv≥0.7", "atrp≤4")


def prepare(p: pd.DataFrame) -> pd.DataFrame:
    p = p[p.member].copy()
    p["ddr"] = p.dr5 - p.dr20
    for h in (5, 20, 60):
        p[f"ex{h}"] = p[f"fwd{h}"] - p.groupby("date")[f"fwd{h}"].transform("mean")
    p["month"] = p.date.dt.to_period("M")
    p["liq"] = p.avg_a20 >= 1e10
    return p.reset_index(drop=True)


def masks(p: pd.DataFrame):
    cache = {}
    for k, f in FLOW.items():
        cache[("f", k)] = np.asarray(f(p), bool)
    for k, f in TREND.items():
        cache[("t", k)] = np.asarray(f(p), bool)
    for k, v in RVOL.items():
        cache[("r", k)] = np.ones(len(p), bool) if v is None else (p.rvol >= v).to_numpy()
    for k, v in CLV.items():
        cache[("c", k)] = np.ones(len(p), bool) if v is None else (p.clv >= v).to_numpy()
    for k, v in ATRP.items():
        cache[("a", k)] = np.ones(len(p), bool) if v is None else (p.atrp14 <= v).to_numpy()
    return cache


def stats(p: pd.DataFrame, m: np.ndarray, h: int = 20) -> dict:
    s = p.loc[m & p[f"ex{h}"].notna().to_numpy(), [f"ex{h}", f"fwd{h}", "month", "date", "code"]]
    n = len(s)
    if n == 0:
        return {"n": 0}
    x = s[f"ex{h}"].to_numpy()
    mu = x.mean()
    g = pd.Series(x - mu).groupby(s["month"].to_numpy()).sum()
    se = np.sqrt((g ** 2).sum()) / n
    return {"n": n, "days": int(s.date.nunique()), "stocks": int(s.code.nunique()),
            "months": int(s.month.nunique()), "ex": mu, "se": se,
            "t": mu / se if se > 0 else np.nan,
            "raw": float(s[f"fwd{h}"].mean()), "net": float(s[f"fwd{h}"].mean() - COST),
            "hit": float((s[f"fwd{h}"] > COST).mean())}


def run():
    sys.stdout.reconfigure(encoding="utf-8")
    raw = add_xs(pd.read_parquet(os.path.join(CACHE, "panel-20220103-20261023.parquet")))
    p = prepare(raw)
    mk = masks(p)
    liq = p.liq.to_numpy()
    tr = ((p.date >= TRAIN[0]) & (p.date <= TRAIN[1])).to_numpy()
    te = ((p.date >= TEST[0]) & (p.date <= TEST[1])).to_numpy()

    rows = []
    for f, t, r, c, a in itertools.product(FLOW, TREND, RVOL, CLV, ATRP):
        m = liq & mk[("f", f)] & mk[("t", t)] & mk[("r", r)] & mk[("c", c)] & mk[("a", a)]
        st = stats(p, m & tr)
        rows.append({"flow": f, "trend": t, "rvol": r, "clv": c, "atrp": a, **st,
                     "_mask": m})
    g = pd.DataFrame(rows)
    ok = g[(g.n >= MIN_N) & (g.months >= MIN_MONTHS)].sort_values("t", ascending=False)
    spec = g[(g.flow == SPEC[0]) & (g.trend == SPEC[1]) & (g.rvol == SPEC[2])
             & (g.clv == SPEC[3]) & (g.atrp == SPEC[4])].iloc[0]

    def test_row(r, label):
        out = {"label": label, "flow": r.flow, "trend": r.trend, "rvol": r.rvol, "clv": r.clv,
               "atrp": r.atrp}
        for h in (5, 20, 60):
            for name, sel in (("train", tr), ("test", te)):
                st = stats(p, r._mask & sel, h)
                for k in ("n", "ex", "t", "net", "hit", "stocks", "days"):
                    out[f"{name}_h{h}_{k}"] = st.get(k)
        return out

    picks = [test_row(spec, "명세 기준선(일봉 조건)")]
    picks += [test_row(r, f"학습 {i + 1}위") for i, (_, r) in enumerate(ok.head(10).iterrows())]
    # 단일 조건만 바꾼 변형 — 어느 조건이 성적을 움직이는가(학습·검증 모두)
    one = []
    for key, opts in (("flow", FLOW), ("trend", TREND), ("rvol", RVOL), ("clv", CLV), ("atrp", ATRP)):
        for o in opts:
            if o == spec[key]:
                continue
            r = g[(g.flow == (o if key == "flow" else spec.flow)) & (g.trend == (o if key == "trend" else spec.trend))
                  & (g.rvol == (o if key == "rvol" else spec.rvol)) & (g.clv == (o if key == "clv" else spec.clv))
                  & (g.atrp == (o if key == "atrp" else spec.atrp))].iloc[0]
            one.append(test_row(r, f"명세에서 {key}만 → {o}"))

    # 우상향 종목 주 다리의 상승 초기를 잡았는가(사후 기술 통계)
    win = winners(raw)
    _, early, lg = windows(raw, win)
    em = early[early.main][["code", "date", "leg"]]
    key = p.set_index(["code", "date"]).index
    pos = pd.Series(np.arange(len(p)), index=key)

    def recall(mask):
        idx = pos.reindex(pd.MultiIndex.from_frame(em[["code", "date"]])).to_numpy()
        hit = np.zeros(len(em), bool)
        okk = ~np.isnan(idx)
        hit[okk] = mask[idx[okk].astype(int)]
        per = pd.Series(hit).groupby(em.leg.to_numpy()).any()
        return float(per.mean()), int(per.sum()), int(len(per))

    for row, src in zip(picks, [spec] + [r for _, r in ok.head(10).iterrows()]):
        row["recall_main_legs"] = recall(src._mask)
    for row in one:
        row["recall_main_legs"] = None

    n_valid = len(ok)
    out = {"grid_size": len(g), "valid": n_valid, "train": [str(x.date()) for x in TRAIN],
           "test": [str(x.date()) for x in TEST], "cost": COST,
           "picks": picks, "one_change": one,
           "train_top30": ok.drop(columns="_mask").head(30).to_dict("records"),
           "train_t_quantiles": ok.t.quantile([.5, .9, .99]).to_dict()}
    path = os.path.join(CACHE, "backtest.json")
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)

    pd.set_option("display.width", 250)
    print(f"격자 {len(g)}개 · 학습 표본 조건 충족 {n_valid}개 · 학습 t 분포 {out['train_t_quantiles']}")
    cols = ["label", "flow", "trend", "rvol", "clv", "atrp",
            "train_h20_n", "train_h20_ex", "train_h20_t", "test_h20_n", "test_h20_ex", "test_h20_t",
            "test_h20_net", "test_h20_hit", "test_h5_ex", "test_h60_ex", "test_h60_t", "recall_main_legs"]
    print(pd.DataFrame(picks)[cols].to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    print()
    print(pd.DataFrame(one)[cols[:-1]].to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    print("저장", path)


if __name__ == "__main__":
    run()

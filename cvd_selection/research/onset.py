# -*- coding: utf-8 -*-
"""1부 — 우상향 대표 종목의 '상승 초기' 공통 지표 (사후 선택, 기술 통계).

    python -m cvd_selection.research.onset

선정 (사후 — 결과를 알고 고른다. 수익 근거로 쓰지 않는다)
----------------------------------------------------------
- 창 2022-10-04 ~ 2026-10-23, 창 안에서 한 번이라도 코스피200·코스닥150 편입
- 우상향 = 창 수익 배수 ≥ 3 그리고 로그가격 선형회귀 R² ≥ 0.7, 창 거래일 ≥ 900

상승 구간(다리)
--------------
종가 지그재그(되돌림 25%)로 저점 T → 고점 P를 나누고, 상승폭 P/T ≥ 1.8인 다리만 쓴다.
- 바닥 구간  = T-10 .. T
- 상승 초기  = T+1 .. 종가가 처음 1.3·T 이상이 된 날(최대 60거래일)

비교 기준선 = 같은 창의 편입 종목·거래일 전체.
"""

from __future__ import annotations

import json
import os
import sys

import numpy as np
import pandas as pd

from .features import CACHE, XS_COLS, add_xs

WIN_START = pd.Timestamp("2022-10-04")
WIN_END = pd.Timestamp("2026-10-23")
MIN_MULT, MIN_R2, MIN_DAYS = 3.0, 0.7, 900
ZIGZAG, MIN_LEG, EARLY_UP, EARLY_MAX = 0.25, 1.8, 1.3, 60

BOOL = ["trend", "ma20_up", "above_ma60", "breakout20", "delta_prev_pos"]
CONT = ["dr5", "dr2", "dr20", "pos5", "rvol", "avg_a20", "clv", "atrp14", "near52",
        "ret20", "ret60"] + [f"{c}_xs" for c in XS_COLS]
#: 조건 형태의 비율(명세 시작값과 그 주변)
RULES = {
    "dr5≥5": lambda d: d.dr5 >= 5, "dr5>0": lambda d: d.dr5 > 0,
    "dr20>0": lambda d: d.dr20 > 0, "dr20≥3": lambda d: d.dr20 >= 3,
    "pos5≥3": lambda d: d.pos5 >= 3, "dr2>0": lambda d: d.dr2 > 0,
    "rvol≥1.3": lambda d: d.rvol >= 1.3, "rvol≥2": lambda d: d.rvol >= 2,
    "avg_a20≥100억": lambda d: d.avg_a20 >= 1e10,
    "clv≥0.7": lambda d: d.clv >= 0.7, "atrp≤4": lambda d: d.atrp14 <= 4,
    "atrp≤6": lambda d: d.atrp14 <= 6, "near52≥0.9": lambda d: d.near52 >= 0.9,
    "dr20>0&ret20≤0": lambda d: (d.dr20 > 0) & (d.ret20 <= 0),
    "dr5_xs≥0.8": lambda d: d.dr5_xs >= 0.8, "dr20_xs≥0.8": lambda d: d.dr20_xs >= 0.8,
    "ret60_xs≥0.8": lambda d: d.ret60_xs >= 0.8, "near52_xs≥0.8": lambda d: d.near52_xs >= 0.8,
    "spec_daily": lambda d: ((d.dr5 >= 5) & (d.pos5 >= 3) & (d.dr2 > 0) & (d.delta_prev_pos == True)
                             & (d.rvol >= 1.3) & (d.avg_a20 >= 1e10) & d.trend & d.ma20_up
                             & (d.clv >= 0.7) & (d.atrp14 <= 4)),
}


def names() -> dict:
    out = {}
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, os.path.join(root, "src"))
    from cvd_obv.universe import read_index_file
    data = read_index_file()

    def walk(o):
        if isinstance(o, dict):
            if "code" in o and "name" in o:
                out[o["code"]] = o["name"]
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(data)
    return out


def winners(p: pd.DataFrame) -> pd.DataFrame:
    w = p[(p.date >= WIN_START) & (p.date <= WIN_END)]
    ever = set(w[w.member].code)
    rows = []
    for code, g in w.groupby("code"):
        if code not in ever or len(g) < MIN_DAYS:
            continue
        c = g.close.to_numpy()
        y = np.log(c)
        x = np.arange(len(y))
        b, a = np.polyfit(x, y, 1)
        r2 = 1 - ((y - (a + b * x)) ** 2).sum() / ((y - y.mean()) ** 2).sum()
        rows.append({"code": code, "mult": c[-1] / c[0], "r2": r2,
                     "start_member": bool(g.member.iloc[0])})
    df = pd.DataFrame(rows)
    return df[(df.mult >= MIN_MULT) & (df.r2 >= MIN_R2)].sort_values("mult", ascending=False)


def legs(c: np.ndarray) -> list:
    """지그재그 저점·고점 인덱스 쌍 [(T, P)] — 되돌림 ZIGZAG 기준."""
    out, lo_i, hi_i, mode = [], 0, 0, "up"
    for i in range(1, len(c)):
        if mode == "up":
            if c[i] > c[hi_i]:
                hi_i = i
            elif c[i] <= c[hi_i] * (1 - ZIGZAG):
                out.append((lo_i, hi_i))
                lo_i, mode = i, "down"
        else:
            if c[i] < c[lo_i]:
                lo_i = i
            elif c[i] >= c[lo_i] * (1 + ZIGZAG):
                hi_i, mode = i, "up"
    if mode == "up":
        out.append((lo_i, hi_i))
    return [(t, q) for t, q in out if q > t and c[q] / c[t] >= MIN_LEG]


def windows(p: pd.DataFrame, win: pd.DataFrame):
    pre, early, leg_rows = [], [], []
    w = p[(p.date >= WIN_START) & (p.date <= WIN_END)]
    for code in win.code:
        g = w[w.code == code].sort_values("date").reset_index()
        c = g.close.to_numpy()
        lg_all = [(t, q) for t, q in legs(c) if t > 0]
        main = max(lg_all, key=lambda x: c[x[1]] / c[x[0]]) if lg_all else None
        for t, q in lg_all:
            hit = np.nonzero(c[t + 1:q + 1] >= EARLY_UP * c[t])[0]
            e_end = t + 1 + (hit[0] if len(hit) else 0)
            e_end = min(e_end, t + EARLY_MAX)
            leg_id = f"{code}-{g.date[t]:%Y%m%d}"
            pre.append(g.iloc[max(0, t - 10):t + 1].assign(leg=leg_id, main=(t, q) == main))
            early.append(g.iloc[t + 1:e_end + 1].assign(leg=leg_id, main=(t, q) == main))
            leg_rows.append({"code": code, "main": (t, q) == main, "trough": g.date[t].date(), "peak": g.date[q].date(),
                             "mult": c[q] / c[t], "early_days": e_end - t,
                             "early_end": g.date[e_end].date()})
    return pd.concat(pre), pd.concat(early), pd.DataFrame(leg_rows)


def _stat(s: pd.DataFrame, col: pd.Series, how: str) -> float:
    """기준선은 종목·거래일 그대로, 다리 표본은 **다리별 평균을 먼저 내고** 다리 사이 중앙/평균."""
    if "leg" in s:
        per = col.astype(float).groupby(s["leg"]).mean()
        return float(per.median() if how == "median" else per.mean())
    return float(np.nanmedian(col) if how == "median" else np.nanmean(col.astype(float)))


def profile(sets: dict) -> pd.DataFrame:
    rows = []
    for k in CONT:
        rows.append({"지표": k, **{n: _stat(s, s[k], "median") for n, s in sets.items()}})
    for k in BOOL:
        rows.append({"지표": k, **{n: _stat(s, s[k], "mean") for n, s in sets.items()}})
    for k, f in RULES.items():
        rows.append({"지표": k, **{n: _stat(s, f(s), "mean") for n, s in sets.items()}})
    return pd.DataFrame(rows)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = add_xs(pd.read_parquet(os.path.join(CACHE, "panel-20220103-20261023.parquet")))
    nm = names()
    win = winners(p)
    win["name"] = win.code.map(nm).fillna(win.code)
    pre, early, lg = windows(p, win)
    lg["name"] = lg.code.map(nm).fillna(lg.code)
    base = p[p.member & (p.date >= WIN_START) & (p.date <= WIN_END)]
    prof = profile({"기준선(전체)": base, "바닥(T-10..T)": pre, "상승초기": early,
                    "주다리 바닥": pre[pre.main], "주다리 초기": early[early.main]})
    prof["초기/기준선"] = prof["상승초기"] / prof["기준선(전체)"]

    pd.set_option("display.width", 200)
    print(f"우상향 종목 {len(win)} (창 시작 편입 {int(win.start_member.sum())})")
    print(win[["code", "name", "mult", "r2", "start_member"]].to_string(index=False))
    print(f"\n상승 다리 {len(lg)}개 · 상승초기 표본 {len(early)} 종목·거래일 · 기준선 {len(base):,}")
    print(lg[lg.main].to_string(index=False))
    print()
    print(prof.to_string(index=False, float_format=lambda v: f"{v:,.3f}"))

    out = os.path.join(CACHE, "onset.json")
    json.dump({"winners": win.to_dict("records"), "legs": lg.astype(str).to_dict("records"),
               "profile": prof.to_dict("records")},
              open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    print("\n저장", out)


if __name__ == "__main__":
    main()

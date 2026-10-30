# -*- coding: utf-8 -*-
"""연구용 종목·거래일 패널 — 일봉(bars-1.0) + 확정 체결량(sides-1.0) + 지수 편입 이력.

스캐너(`cvd_selection.selection`)와 같은 정의를 **일봉만으로** 벡터화한다. 1분봉이 필요한
VWAP·Rlate·HoldClose는 2025-10 전에 없으므로 연구 패널에 넣지 않는다.

    표기: D = 신호일(종가 매수 가정). 체결량 지표는 D-1까지(스캐너와 같다).

| 열 | 정의 |
|---|---|
| dr5, dr2, dr20 | 100 × Σ(B−S) / Σ(B+S), D-5..D-1 / D-2..D-1 / D-20..D-1 |
| pos5 | D-5..D-1 중 B−S > 0인 날 수 |
| delta_prev_pos | D-1의 B−S > 0 |
| rvol | V_D / 평균(V_{D-20..D-1}) — 일봉 거래량(블록딜 포함, 연구 기간에 1분봉 없음) |
| avg_a20 | 평균(거래대금_{D-20..D-1}) |
| trend | C > MA20 > MA60 |
| ma20_up | MA20 > MA20(D-5) |
| clv | (C−L)/(H−L) — 명세 4.3·`indicators.clv`와 같다 |
| atrp14 | 100 × Wilder ATR14(D-1까지) / C_{D-1} |
| breakout20 | C > max(H_{D-20..D-1}) |
| near52 | C / max(H_{D-250..D}) |
| ret5, ret20, ret60 | 과거 수익률 |
| fwd5, fwd20, fwd60 | C_{D+h} / C_D − 1 (종가 매수 → h거래일 뒤 종가) |
| member | D에 코스피200·코스닥150 편입(이력, `flat_intervals`) |
"""

from __future__ import annotations

import os
import sys
from datetime import date

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "src"))

from cvd_obv.bar_standard import read_standard_daily  # noqa: E402
from cvd_obv.sides_flow import read_sides_flow  # noqa: E402
from cvd_obv.universe import flat_intervals  # noqa: E402

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")


def _wilder(tr: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(tr), np.nan)
    if len(tr) <= n:
        return out
    a = np.nanmean(tr[1:n + 1])
    out[n] = a
    for i in range(n + 1, len(tr)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def stock_features(g: pd.DataFrame) -> pd.DataFrame:
    """한 종목(날짜 오름차순)의 지표 — 열 정의는 모듈 문서 표.

    입력 열: close, high, low, volume, value, buy, sell(체결량 없는 날 NaN).
    """
    g = g.copy()
    c, h, l, v = g["close"], g["high"], g["low"], g["volume"].astype(float)
    delta = g["buy"] - g["sell"]
    vflow = g["buy"] + g["sell"]
    dp, vp = delta.shift(1), vflow.shift(1)        # D-1까지
    for n in (2, 5, 20):
        # 결측이 하나라도 있으면 NaN(스캐너의 FLOW_MISSING과 같다)
        g[f"dr{n}"] = 100 * dp.rolling(n, min_periods=n).sum() / vp.rolling(n, min_periods=n).sum()
    g["pos5"] = (dp > 0).astype(float).where(dp.notna()).rolling(5, min_periods=5).sum()
    g["delta_prev_pos"] = (dp > 0).where(dp.notna())
    vm = v.shift(1).rolling(20, min_periods=20).mean()
    g["rvol"] = v / vm
    g["avg_a20"] = g["value"].shift(1).rolling(20, min_periods=20).mean()
    ma20 = c.rolling(20, min_periods=20).mean()
    ma60 = c.rolling(60, min_periods=60).mean()
    g["trend"] = (c > ma20) & (ma20 > ma60)
    g["ma20_up"] = ma20 > ma20.shift(5)
    g["above_ma60"] = c > ma60
    rng = h - l
    # 명세·스캐너(`indicators.clv`)와 같은 (C−L)/(H−L). 2026-10-25 전에는 (2C−H−L)/(H−L)로
    # 잘못 계산했다 — 사전등록 CVDSEL 단위 테스트에서 발견, 실행 전 수정
    g["clv"] = ((c - l) / rng).where(rng > 0)
    tr = np.maximum(h - l, np.maximum((h - c.shift(1)).abs(), (l - c.shift(1)).abs()))
    tr.iloc[0] = np.nan
    atr = pd.Series(_wilder(tr.to_numpy(), 14), index=g.index)
    g["atrp14"] = 100 * atr.shift(1) / c.shift(1)
    g["breakout20"] = c > h.shift(1).rolling(20, min_periods=20).max()
    g["near52"] = c / h.rolling(250, min_periods=120).max()
    for n in (5, 20, 60):
        g[f"ret{n}"] = c / c.shift(n) - 1
    for n in (5, 20, 60):
        g[f"fwd{n}"] = c.shift(-n) / c - 1
    return g


def build_panel(start: date, end: date, warmup_start: date, use_cache=True) -> pd.DataFrame:
    """편입 이력이 있는 전 종목의 패널. `member`로 그날 편입 여부를 거른다."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"panel-{start:%Y%m%d}-{end:%Y%m%d}.parquet")
    if use_cache and os.path.exists(path):
        return pd.read_parquet(path)

    iv = flat_intervals()
    sids = sorted(s for s, ivs in iv.items()
                  if any((e is None or e >= start) and s0 <= end for _, s0, e in ivs))
    import pyarrow.dataset as ds
    t = read_standard_daily(
        columns=["security_id", "trading_date", "high", "low", "close", "volume",
                 "traded_value"],
        filters=(ds.field("trading_date") >= warmup_start) & (ds.field("trading_date") <= end)
        & ds.field("security_id").isin(sids),
        drop_failed=True, fill_traded_value=True)
    df = t.to_pandas()
    for c in ("high", "low", "close", "traded_value"):
        df[c] = df[c].astype(float)
    df["code"] = df["security_id"].str.split(":").str[1]
    df = df.rename(columns={"trading_date": "date", "traded_value": "value"})
    df["date"] = pd.to_datetime(df["date"])

    flows = read_sides_flow(sids, warmup_start, end)
    fl = pd.DataFrame([(c, d, sd.buy, sd.sell, sd.provisional)
                       for c, per in flows.items() for d, sd in per.items()],
                      columns=["code", "date", "buy", "sell", "fprov"])
    fl["date"] = pd.to_datetime(fl["date"])
    fl = fl[~fl["fprov"]].drop(columns="fprov")
    df = df.merge(fl, on=["code", "date"], how="left")
    df = df.sort_values(["code", "date"]).reset_index(drop=True)

    parts = [stock_features(g) for _, g in df.groupby("code", sort=False)]
    p = pd.concat(parts, ignore_index=True)
    p = p[(p["date"] >= pd.Timestamp(start)) & (p["date"] <= pd.Timestamp(end))].copy()

    # 그날 편입 여부(이력)
    mem = np.zeros(len(p), bool)
    idx_of = np.empty(len(p), object)
    dates = p["date"].dt.date.to_numpy()
    for i, (sid, d) in enumerate(zip(p["security_id"].to_numpy(), dates)):
        for ix, s0, e in iv.get(sid, ()):
            if s0 <= d and (e is None or d < e):
                mem[i] = True
                idx_of[i] = ix
                break
    p["member"] = mem
    p["index_id"] = idx_of
    p = p.drop(columns=["security_id"])
    p.to_parquet(path)
    return p


XS_COLS = ("dr5", "dr20", "rvol", "ret20", "ret60", "near52")


def add_xs(p: pd.DataFrame) -> pd.DataFrame:
    """그날 편입 종목 안의 백분위(0~1) — `<열>_xs`. 편입 아닌 행은 NaN.

    체결량 DR은 시장 전체가 음(중앙 −6%)으로 치우쳐 있어(공급자 분류 특성, 추정) 절댓값보다
    같은 날 다른 종목과의 상대 위치가 해석하기 쉽다.
    """
    m = p["member"]
    for c in XS_COLS:
        p[f"{c}_xs"] = np.nan
        p.loc[m, f"{c}_xs"] = p.loc[m].groupby("date")[c].rank(pct=True)
    return p


__all__ = ["build_panel", "add_xs", "CACHE", "XS_COLS"]

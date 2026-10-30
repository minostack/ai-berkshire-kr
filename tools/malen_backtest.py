#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
사전등록 MALEN — 이동평균 길이를 10·20·60 중 무엇으로 두어야 하는가.

약속: `docs/PREREGISTRATION_MALEN.md` — 이 파일은 그 문서에 적힌 것**만**
계산한다. 결과 JSON에 문서의 sha256을 박는다.

MA 길이는 **설계마다 역할이 다르다**(사전등록 §1.1).
    가속 `accel`     종가 ≤ MA_n × 1.10 (과열 차단만)
    일반 `v15daily`  종가 > MA_n  그리고  OBV > OBV MA_n
한 설계 안에서는 모든 MA 자리를 같은 길이로 바꾼다. 유동성 창(20일)은
MA가 아니므로 따라 움직이지 않는다.

재사용(새로 짜지 않는다):
  · `truecvd_backtest.build_panel(ma_days=…)`  게이트·OBV·수익 패널
  · `truecvd_backtest.paired_vs_universe`·`paired_vs_index`·`group_stats`
  · `nxtbasis_backtest.basis_diff`·`gate_agreement`
  · `ab_backtest_b.summarize`·`bh_fdr`·`verdict`

    python tools/malen_backtest.py
    python tools/malen_backtest.py --resamples 500 --no-save
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
import nxtbasis_backtest as nx                  # noqa: E402
import truecvd_backtest as tc                   # noqa: E402

PREREG_PATH = os.path.join(_BASE, "docs", "PREREGISTRATION_MALEN.md")
OUT_DIR = os.path.join(_BASE, "reports", "backtest")

# ── 사전등록 §1·§2·§3 — 바꾸면 약속 위반 ──
MA_LENGTHS = (10, 20, 60)
BASELINE_MA = 20                      # 기준선(현행)
SIGNAL_START = date(2010, 6, 15)      # 10번과 동일
SIGNAL_END = date(2026, 10, 9)
LOAD_FROM = date(2009, 6, 1)
#: 흐름 창은 10번에서 확정한 현행값으로 **고정**한다
RECENT_DAYS, PRIOR_DAYS, V15_WINDOW = 5, 10, 15
PRIMARY_H = 2
SECONDARY_H = 5
RESAMPLES = 4000
SEED = 20261021
DESIGNS = ("accel", "v15daily")
SUBPERIODS = (("2010-2017", date(2010, 1, 1), date(2017, 12, 31)),
              ("2018-2026", date(2018, 1, 1), date(2026, 12, 31)))

ORDER = ["H1", "H2", "H3", "H4"]
SPEC = {
    "H1": ("가속 MA10 − MA20", "accel", 10),
    "H2": ("가속 MA60 − MA20", "accel", 60),
    "H3": ("일반 MA10 − MA20", "v15daily", 10),
    "H4": ("일반 MA60 − MA20", "v15daily", 60),
}


def gate_reasons(P, design: str, ma_n: int) -> dict:
    """MA 때문에 떨어진 건수 — 설계별로 조건이 다르다(보조)."""
    m = P["in_period"]
    ok = P["ok"][design][:, m]
    g = P["gate"][design][:, m]
    return {"ma_days": ma_n, "input_ready": int(ok.sum()),
            "passed": int(g.sum()),
            "blocked": int((ok & ~g).sum())}


def run(resamples: int, log=print) -> dict:
    with open(PREREG_PATH, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    log(f"사전등록 sha256 {sha[:8]}")

    log("유니버스 이력 로드")
    intervals = bb.load_universe_intervals()
    sids = sorted({s for iv in intervals.values() for s in iv})
    log(f"체결량 로드 J ({LOAD_FROM}~{SIGNAL_END})")
    sides, zero = tc.load_sides(sids, LOAD_FROM, SIGNAL_END, "J")
    log(f"  J {sum(len(v) for v in sides.values()):,}행 ({len(sides)}종목)")
    log(f"일봉 로드 ({LOAD_FROM}~) — {len(sids)}종목 (느림)")
    bars = bb.load_all_bars(set(sids), load_from=LOAD_FROM)

    panels = {}
    for ma_n in MA_LENGTHS:
        log(f"패널 MA{ma_n}")
        panels[ma_n] = tc.build_panel(
            log=lambda *a, **k: None,
            signal_start=SIGNAL_START, signal_end=SIGNAL_END,
            load_from=LOAD_FROM, bars=bars, sides=sides, zero_rows=zero,
            recent_days=RECENT_DAYS, prior_days=PRIOR_DAYS,
            v15_window=V15_WINDOW, ma_days=ma_n)
        P = panels[ma_n]
        for d in DESIGNS:
            log(f"    {d}: 입력 갖춤 {int(P['ok'][d].sum()):,} · "
                f"통과 {int(P['gate'][d].sum()):,}")
    base = panels[BASELINE_MA]
    log(f"신호 구간 {int(base['in_period'].sum())}거래일")

    primary = {}
    for key in ORDER:
        name, design, ma_n = SPEC[key]
        s = nx.basis_diff(panels[ma_n], base, design, PRIMARY_H)
        primary[key] = {"name": name, "design": design, "ma_days": ma_n,
                        "baseline_ma": BASELINE_MA, "two_sided_prediction": True,
                        "skipped_days": s["skipped_days"],
                        "stats": bb.summarize(s["x"], PRIMARY_H, resamples, SEED)}
    qs = bb.bh_fdr([primary[k]["stats"].get("p", 1.0) for k in ORDER])
    for key, q in zip(ORDER, qs):
        primary[key]["q"] = q
        primary[key]["verdict"] = bb.verdict(primary[key]["stats"], q)
        st = primary[key]["stats"]
        log(f"  {key} {primary[key]['verdict']:<22} "
            f"평균 {st.get('mean', float('nan')):+.5f} "
            f"n={st.get('n_days')} q={q:.4f}")

    # ── 보조 ──
    aux = {"per_length": {}, "secondary_h": {}, "ma10_minus_ma60": {},
           "gate_agreement": {}, "subperiods": {}, "gate_reasons": {}}
    for ma_n in MA_LENGTHS:
        P = panels[ma_n]
        aux["per_length"][ma_n] = {}
        for d in DESIGNS:
            aux["per_length"][ma_n][d] = {
                "vs_universe": bb.summarize(
                    tc.paired_vs_universe(P, d, PRIMARY_H)["x"],
                    PRIMARY_H, resamples, SEED),
                "vs_index": bb.summarize(
                    tc.paired_vs_index(P, d, PRIMARY_H)["x"],
                    PRIMARY_H, resamples, SEED),
                "group": tc.group_stats(P, d, PRIMARY_H)}
        aux["gate_reasons"][ma_n] = {d: gate_reasons(P, d, ma_n)
                                     for d in DESIGNS}
        if ma_n != BASELINE_MA:
            aux["gate_agreement"][ma_n] = {
                d: nx.gate_agreement(P, base, d) for d in DESIGNS}
    aux["secondary_h"][SECONDARY_H] = {
        key: bb.summarize(
            nx.basis_diff(panels[SPEC[key][2]], base, SPEC[key][1],
                          SECONDARY_H)["x"],
            SECONDARY_H, resamples, SEED) for key in ORDER}
    # MA10 − MA60 (따라 나오는 차 — 서술만)
    for d in DESIGNS:
        s = nx.basis_diff(panels[10], panels[60], d, PRIMARY_H)
        aux["ma10_minus_ma60"][d] = bb.summarize(
            s["x"], PRIMARY_H, resamples, SEED)
    for label, a, b in SUBPERIODS:
        aux["subperiods"][label] = {}
        for key in ORDER:
            name, design, ma_n = SPEC[key]
            s = nx.basis_diff(panels[ma_n], base, design, PRIMARY_H)
            keep = [i for i, d_ in enumerate(s["dates"]) if a <= d_ <= b]
            aux["subperiods"][label][key] = bb.summarize(
                s["x"][keep], PRIMARY_H, resamples, SEED)

    return {
        "prereg": {"path": os.path.relpath(PREREG_PATH, _BASE), "sha256": sha},
        "run_id": f"malen_{datetime.utcnow():%Y%m%d%H%M%S}_{uuid.uuid4().hex[:6]}",
        "ma_lengths": list(MA_LENGTHS),
        "baseline_ma": BASELINE_MA,
        "fixed": {"recent": RECENT_DAYS, "prior": PRIOR_DAYS,
                  "v15_window": V15_WINDOW, "liq_days": tc.LIQ_DAYS,
                  "overheat_max_pct": 10.0, "bull_min": tc.V15_BULL_MIN,
                  "flow": "J (KRX 단독)"},
        "roles": {"accel": "종가 ≤ MA_n × 1.10 (과열 차단만)",
                  "v15daily": "종가 > MA_n 그리고 OBV > OBV MA_n"},
        "period": [str(SIGNAL_START), str(SIGNAL_END)],
        "trading_days": int(base["in_period"].sum()),
        "securities": len(base["sids"]),
        "horizons": {"primary": PRIMARY_H, "secondary": SECONDARY_H},
        "stats_config": {"bootstrap": "moving_block", "block": PRIMARY_H,
                         "resamples": resamples, "seed": SEED,
                         "z_mde": bb.Z_MDE, "fdr": "BH over 4 primary"},
        "cost": base["cost"],
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
        p = os.path.join(OUT_DIR,
                         f"prereg-malen-{res['prereg']['sha256'][:8]}.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1, default=str)
        print(f"저장: {os.path.relpath(p, _BASE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

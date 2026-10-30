#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
사전등록 WINDOW15 — 흐름 창을 10거래일에서 15거래일로 늘리면 결과가 달라지는가.

약속: `docs/PREREGISTRATION_WINDOW15.md` — 이 파일은 그 문서에 적힌 것**만**
계산한다. 결과 JSON에 문서의 sha256을 박는다.

주 패널  `J`(KRX 단독) 2010-06-15 ~ 2026-10-09 — 검정력이 가장 높은 자리
보조 패널 `sides-1.0`(통합 기준) 2025-04-12 ~ 2026-10-09 — 가정 점검용(탐색)

재사용(새로 짜지 않는다):
  · `truecvd_backtest.build_panel`   게이트·OBV·창 흐름·수익 패널(창을 인자로)
  · `truecvd_backtest.paired_vs_universe`·`paired_vs_index`·`group_stats`
  · `nxtbasis_backtest.basis_diff`·`gate_agreement`  두 패널 짝 비교
  · `ab_backtest_b.summarize`·`bh_fdr`·`verdict`     통계·판정(p는 양측)

    python tools/window15_backtest.py
    python tools/window15_backtest.py --resamples 500 --no-save   # 연결 확인
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

PREREG_PATH = os.path.join(_BASE, "docs", "PREREGISTRATION_WINDOW15.md")
OUT_DIR = os.path.join(_BASE, "reports", "backtest")

# ── 사전등록 §1·§2·§3 — 바꾸면 약속 위반 ──
SIGNAL_START = date(2010, 6, 15)      # 이력 시작 + 15거래일 워밍업(긴 창에 맞춘다)
SIGNAL_END = date(2026, 10, 9)        # 8번과 동일
LOAD_FROM = date(2009, 6, 1)          # 8번과 동일
UN_START = date(2025, 4, 12)          # 9번 시작 + 5거래일(창이 길어진 만큼)
UN_LOAD_FROM = date(2024, 1, 1)
PRIMARY_H = 2
SECONDARY_H = 5
RESAMPLES = 4000
SEED = 20261021
#: (최근, 이전, 일반설계 창) — 왼쪽이 기준선, 오른쪽이 시험 대상
W10 = (3, 7, 10)
W15 = (5, 10, 15)
DESIGNS = ("accel", "v15daily")
SUBPERIODS = (("2010-2017", date(2010, 1, 1), date(2017, 12, 31)),
              ("2018-2026", date(2018, 1, 1), date(2026, 12, 31)))

ORDER = ["H1", "H2", "H3", "H4", "H5", "H6"]
SPEC = {
    "H1": ("accel 창차 15−10", "accel", "diff"),
    "H2": ("v15daily 창차 15−10", "v15daily", "diff"),
    "H3": ("accel(창15) − 유니버스", "accel", "uni"),
    "H4": ("accel(창15) − 지수", "accel", "idx"),
    "H5": ("v15daily(창15) − 유니버스", "v15daily", "uni"),
    "H6": ("v15daily(창15) − 지수", "v15daily", "idx"),
}


def _series(P15, P10, kind, design, h):
    if kind == "diff":
        return nx.basis_diff(P15, P10, design, h)
    if kind == "uni":
        return tc.paired_vs_universe(P15, design, h)
    return tc.paired_vs_index(P15, design, h)


def run_pair(label, sides, zero, bars, signal_start, load_from, resamples,
             log=print, market_div: str = "J") -> dict:
    """한 흐름 원천에서 창 10·15 패널을 만들고 여섯 계열을 낸다."""
    panels = {}
    for tag, (r_n, p_n, v15) in (("w10", W10), ("w15", W15)):
        log(f"  [{label}] 패널 {tag} (최근{r_n}+이전{p_n}, 일반창{v15})")
        panels[tag] = tc.build_panel(
            log=lambda *a, **k: None,
            signal_start=signal_start, signal_end=SIGNAL_END,
            load_from=load_from, market_div=market_div, bars=bars,
            sides=sides, zero_rows=zero,
            recent_days=r_n, prior_days=p_n, v15_window=v15)
        P = panels[tag]
        for d in DESIGNS:
            log(f"      {d}: 입력 갖춤 {int(P['ok'][d].sum()):,} · "
                f"통과 {int(P['gate'][d].sum()):,}")
    P15, P10 = panels["w15"], panels["w10"]
    log(f"  [{label}] 신호 구간 {int(P15['in_period'].sum())}거래일")

    out = {}
    for key in ORDER:
        name, design, kind = SPEC[key]
        s = _series(P15, P10, kind, design, PRIMARY_H)
        out[key] = {"name": name, "design": design, "kind": kind,
                    "two_sided_prediction": kind == "diff",
                    "stats": bb.summarize(s["x"], PRIMARY_H, resamples, SEED)}
        if kind == "diff":
            out[key]["skipped_days"] = s["skipped_days"]
    qs = bb.bh_fdr([out[k]["stats"].get("p", 1.0) for k in ORDER])
    for key, q in zip(ORDER, qs):
        out[key]["q"] = q
        out[key]["verdict"] = bb.verdict(out[key]["stats"], q)
        st = out[key]["stats"]
        log(f"      {key} {out[key]['verdict']:<22} "
            f"평균 {st.get('mean', float('nan')):+.5f} "
            f"n={st.get('n_days')} q={q:.4f}")
    return {"primary": out, "panels": panels,
            "trading_days": int(P15["in_period"].sum()),
            "securities": len(P15["sids"])}


def run(resamples: int, log=print) -> dict:
    with open(PREREG_PATH, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    log(f"사전등록 sha256 {sha[:8]}")

    log("유니버스 이력 로드")
    intervals = bb.load_universe_intervals()
    sids = sorted({s for iv in intervals.values() for s in iv})

    # ── 주 패널: J, 16.4년 ──
    log(f"체결량 로드 J ({LOAD_FROM}~{SIGNAL_END})")
    j_sides, j_zero = tc.load_sides(sids, LOAD_FROM, SIGNAL_END, "J")
    log(f"  J {sum(len(v) for v in j_sides.values()):,}행 "
        f"({len(j_sides)}종목) · 흐름없음 {j_zero:,}")
    log(f"일봉 로드 ({LOAD_FROM}~) — {len(sids)}종목 (느림)")
    bars = bb.load_all_bars(set(sids), load_from=LOAD_FROM)
    main = run_pair("주/J", j_sides, j_zero, bars, SIGNAL_START, LOAD_FROM,
                    resamples, log)

    # ── 보조 패널: 통합 기준, 짧다 (탐색) ──
    log(f"보조 체결량 로드 sides-1.0 ({UN_LOAD_FROM}~{SIGNAL_END})")
    from cvd_obv.sides_flow import read_sides_flow
    flow = read_sides_flow(sids, UN_LOAD_FROM, SIGNAL_END)
    un_sides = {f"KRX:{c}": {d: (sd.buy, sd.sell) for d, sd in per.items()}
                for c, per in flow.items()}
    log(f"  통합 {sum(len(v) for v in un_sides.values()):,}행 "
        f"({len(un_sides)}종목)")
    un_bars = bb.load_all_bars(set(sids), load_from=UN_LOAD_FROM)
    aux_un = run_pair("보조/UN", un_sides, 0, un_bars, UN_START, UN_LOAD_FROM,
                      resamples, log, market_div="UN")

    # ── 보조 통계 ──
    P15, P10 = main["panels"]["w15"], main["panels"]["w10"]
    aux = {"secondary_h": {}, "w10_only": {}, "gate_agreement": {},
           "groups": {}, "subperiods": {},
           "un_panel": {k: v for k, v in aux_un["primary"].items()},
           "un_trading_days": aux_un["trading_days"]}
    aux["secondary_h"][SECONDARY_H] = {
        key: bb.summarize(_series(P15, P10, SPEC[key][2], SPEC[key][1],
                                  SECONDARY_H)["x"],
                          SECONDARY_H, resamples, SEED) for key in ORDER}
    for d in DESIGNS:
        aux["w10_only"][d] = {
            "vs_universe": bb.summarize(
                tc.paired_vs_universe(P10, d, PRIMARY_H)["x"],
                PRIMARY_H, resamples, SEED),
            "vs_index": bb.summarize(
                tc.paired_vs_index(P10, d, PRIMARY_H)["x"],
                PRIMARY_H, resamples, SEED)}
        aux["gate_agreement"][d] = nx.gate_agreement(P15, P10, d)
        aux["groups"][d] = {"w15": tc.group_stats(P15, d, PRIMARY_H),
                            "w10": tc.group_stats(P10, d, PRIMARY_H)}
    for label, a, b in SUBPERIODS:
        m = ((P15["cal_arr"] >= np.datetime64(a))
             & (P15["cal_arr"] <= np.datetime64(b)))
        aux["subperiods"][label] = {}
        for key in ("H1", "H3"):
            name, design, kind = SPEC[key]
            s = (nx.basis_diff(P15, P10, design, PRIMARY_H)
                 if kind == "diff" else
                 tc.paired_vs_universe(P15, design, PRIMARY_H, mask=m))
            if kind == "diff":
                keep = [i for i, d_ in enumerate(s["dates"]) if a <= d_ <= b]
                x = s["x"][keep]
            else:
                x = s["x"]
            aux["subperiods"][label][key] = bb.summarize(
                x, PRIMARY_H, resamples, SEED)

    return {
        "prereg": {"path": os.path.relpath(PREREG_PATH, _BASE), "sha256": sha},
        "run_id": f"window15_{datetime.utcnow():%Y%m%d%H%M%S}_{uuid.uuid4().hex[:6]}",
        "windows": {"baseline": {"recent": W10[0], "prior": W10[1],
                                 "v15_window": W10[2], "total": 10},
                    "tested": {"recent": W15[0], "prior": W15[1],
                               "v15_window": W15[2], "total": 15}},
        "designs": {"accel": "accel-1.0 게이트 5개",
                    "v15daily": "V1.5 정규장 절대조건 5개(30분 모멘텀 제외)"},
        "panel": {"primary": {"flow": "J (KRX 단독)",
                              "period": [str(SIGNAL_START), str(SIGNAL_END)],
                              "trading_days": main["trading_days"],
                              "securities": main["securities"]},
                  "auxiliary": {"flow": "sides-1.0 (통합 기준)",
                                "period": [str(UN_START), str(SIGNAL_END)],
                                "trading_days": aux_un["trading_days"],
                                "note": "탐색 — 판정에 쓰지 않는다"}},
        "horizons": {"primary": PRIMARY_H, "secondary": SECONDARY_H},
        "stats_config": {"bootstrap": "moving_block", "block": PRIMARY_H,
                         "resamples": resamples, "seed": SEED,
                         "z_mde": bb.Z_MDE, "fdr": "BH over 6 primary"},
        "cost": P15["cost"],
        "primary": main["primary"],
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
                         f"prereg-window15-{res['prereg']['sha256'][:8]}.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1, default=str)
        print(f"저장: {os.path.relpath(p, _BASE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
사전등록 NXTBASIS — 체결량 기준을 KRX 단독(J)에서 KRX+NXT 통합(UN)으로
바꾸면 스캐너 결과가 달라지는가.

약속: `docs/PREREGISTRATION_NXTBASIS.md` — 이 파일은 그 문서에 적힌 것**만**
계산한다. 결과 JSON에 문서의 sha256을 박는다.

재사용(새로 짜지 않는다):
  · `truecvd_backtest.build_panel`  게이트·OBV·창 흐름·수익 패널
    (`market_div`·기간·`bars`·`sides`를 인자로 받게 확장했다. 인자를 비우면
     TRUECVD 사전등록 그대로이고, 그 결과가 변하지 않음을 재실행으로 확인한다)
  · `truecvd_backtest.paired_vs_universe` / `paired_vs_index` / `group_stats`
  · `ab_backtest_b.summarize` / `bh_fdr` / `verdict`  (p는 이미 양측이다)

    python tools/nxtbasis_backtest.py
    python tools/nxtbasis_backtest.py --resamples 500 --no-save   # 연결 확인
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
import truecvd_backtest as tc                   # noqa: E402

PREREG_PATH = os.path.join(_BASE, "docs", "PREREGISTRATION_NXTBASIS.md")
OUT_DIR = os.path.join(_BASE, "reports", "backtest")

# ── 사전등록 §1·§2·§3 — 바꾸면 약속 위반 ──
#: NX 커버리지 문턱 — 이 비율을 처음 넘는 거래일이 기준점(§2)
COVERAGE_MIN = 0.90
#: 그 거래일에서 신호 시작까지의 워밍업(가속 게이트가 10거래일 연속 흐름을 요구)
WARMUP_DAYS = 10
SIGNAL_END = date(2026, 10, 9)        # TRUECVD와 동일
LOAD_FROM = date(2024, 1, 1)          # MA20·OBV MA20·유동성 워밍업
PRIMARY_H = 2
SECONDARY_H = 5
RESAMPLES = 4000
SEED = 20261019                       # TRUECVD와 동일 시드
NXT_START = date(2025, 3, 4)

DESIGNS = ("accel", "v15daily")


# ─────────────────────────────────────────────────────────────
# 기준 만들기
# ─────────────────────────────────────────────────────────────

def make_un_sides(j_sides: dict, nx_sides: dict) -> tuple:
    """UN = J + NX. **J 행이 있는 (종목, 날)만** 남긴다(§1.1).

    NX 행이 없으면 그날 NXT 체결이 없는 것으로 보아 (0, 0)을 더한다 —
    결과적으로 `f_UN = f_J`가 된다. NX만 있는 날은 쓰지 않는다(기준 비대칭).
    """
    out, hit, miss = {}, 0, 0
    for sid, days in j_sides.items():
        nd = nx_sides.get(sid, {})
        acc = {}
        for d, (b, s) in days.items():
            nb, ns = nd.get(d, (0, 0))
            if (nb + ns) > 0:
                hit += 1
            else:
                miss += 1
            acc[d] = (b + nb, s + ns)
        out[sid] = acc
    return out, {"pairs": hit + miss, "with_nx": hit, "without_nx": miss}


def member_filter(intervals: dict):
    """(종목, 날) → 그날 코스피200·코스닥150 구성종목인가."""
    segs = {}
    for iv in intervals.values():
        for sid, rng in iv.items():
            for a, b in rng:
                segs.setdefault(sid, []).append((a, b or date(9999, 1, 1)))

    def is_member(sid, d):
        return any(a <= d < b for a, b in segs.get(sid, []))
    return is_member


def nx_coverage(j_sides: dict, nx_sides: dict, cal, is_member=None) -> dict:
    """거래일별 NX 커버리지 = (NX 수량>0 종목) / (그날 J 행이 있는 종목).

    §2의 신호 시작 규칙은 이 계열만 보고 정한다 — **수익률과 무관하다.**

    `is_member`를 주면 분모·분자를 **그날의 구성종목으로 제한**한다. 사전등록
    §2의 "전체"가 모호했는데(전체 수집 종목인가, 그날 구성종목인가),
    게이트가 구성종목에만 적용되므로 **구성종목 분모**로 확정했다. 두 분모의
    측정값을 리포트에 모두 적는다 — 수익률을 보기 **전에** 정한 것이다.
    """
    denom, numer = {}, {}
    for sid, days in j_sides.items():
        nd = nx_sides.get(sid, {})
        for d in days:
            if is_member is not None and not is_member(sid, d):
                continue
            denom[d] = denom.get(d, 0) + 1
            nb, ns = nd.get(d, (0, 0))
            if (nb + ns) > 0:
                numer[d] = numer.get(d, 0) + 1
    share = {}
    for d, n in denom.items():
        share[d] = (numer.get(d, 0) / n) if n else 0.0
    cross = [d for d in cal if d in share and share[d] >= COVERAGE_MIN]
    return {"share": share, "denom": denom, "numer": numer,
            "first_cross": cross[0] if cross else None}


def signal_start_from(cov: dict, cal) -> date:
    """§2 규칙 — 커버리지 90%를 처음 넘는 거래일 + 10거래일."""
    first = cov["first_cross"]
    if first is None:
        raise SystemExit(f"NX 커버리지가 {COVERAGE_MIN:.0%}를 넘는 거래일이 없다")
    k = cal.index(first)
    if k + WARMUP_DAYS >= len(cal):
        raise SystemExit("워밍업 10거래일을 붙일 여유가 없다")
    return cal[k + WARMUP_DAYS]


# ─────────────────────────────────────────────────────────────
# 계열
# ─────────────────────────────────────────────────────────────

def basis_diff(P_un, P_j, design: str, h: int) -> dict:
    """날짜별 `mean(UN 통과) − mean(J 통과)`.

    §3에 적은 대로 유니버스·지수는 같은 날의 상수라 상쇄되므로 이 계열 하나다.
    **두 기준 모두 통과 종목이 있는 날만** 쓴다(한쪽이 비면 차이를 정의할 수 없다).
    """
    r = P_un["net"][h]                       # 일봉이 같으므로 수익 행렬도 같다
    gu = P_un["gate"][design] & np.isfinite(r)
    gj = P_j["gate"][design] & np.isfinite(r)
    days = np.flatnonzero(P_un["in_period"])
    out, dates, skipped = [], [], 0
    for k in days:
        su, sj = gu[:, k], gj[:, k]
        if not su.any() or not sj.any():
            skipped += 1
            continue
        out.append(float(r[su, k].mean()) - float(r[sj, k].mean()))
        dates.append(P_un["cal"][k])
    return {"x": np.asarray(out, float), "dates": dates, "skipped_days": skipped}


def gate_agreement(P_un, P_j, design: str) -> dict:
    """두 기준의 게이트 판정 일치도 — 보조(서술용)."""
    m = P_un["in_period"]
    gu = P_un["gate"][design][:, m]
    gj = P_j["gate"][design][:, m]
    both = int((gu & gj).sum())
    only_un = int((gu & ~gj).sum())
    only_j = int((~gu & gj).sum())
    union = both + only_un + only_j
    ok_un = int(P_un["ok"][design][:, m].sum())
    ok_j = int(P_j["ok"][design][:, m].sum())
    return {"both": both, "only_un": only_un, "only_j": only_j,
            "jaccard": (both / union) if union else None,
            "pass_un": int(gu.sum()), "pass_j": int(gj.sum()),
            "ok_un": ok_un, "ok_j": ok_j,
            "pass_rate_un": (int(gu.sum()) / ok_un) if ok_un else None,
            "pass_rate_j": (int(gj.sum()) / ok_j) if ok_j else None}


def flow_agreement(j_sides: dict, un_sides: dict, start: date, end: date) -> dict:
    """흐름 수준 재측정 — §0의 900쌍 표본을 모집단 전체로 확장(보조)."""
    fj, fu, nxw = [], [], []
    for sid, days in j_sides.items():
        ud = un_sides.get(sid, {})
        for d, (b, s) in days.items():
            if not (start <= d <= end):
                continue
            ub, us = ud.get(d, (b, s))
            if (b + s) <= 0 or (ub + us) <= 0:
                continue
            fj.append((b - s) / (b + s))
            fu.append((ub - us) / (ub + us))
            nxw.append(1.0 - (b + s) / (ub + us))
    fj = np.asarray(fj); fu = np.asarray(fu); nxw = np.asarray(nxw)
    n = len(fj)
    if n < 2:
        return {"pairs": n}
    dif = fu - fj
    # 한쪽이 상수면 상관이 정의되지 않는다 — NaN을 숫자처럼 적지 않는다
    corr = (float(np.corrcoef(fj, fu)[0, 1])
            if fj.std() > 0 and fu.std() > 0 else None)
    return {"pairs": int(n),
            "corr": corr,
            "f_j_mean": float(fj.mean()), "f_un_mean": float(fu.mean()),
            "f_j_std": float(fj.std()), "f_un_std": float(fu.std()),
            "diff_mean": float(dif.mean()), "diff_median": float(np.median(dif)),
            "abs_diff_mean": float(np.abs(dif).mean()),
            "abs_diff_p95": float(np.percentile(np.abs(dif), 95)),
            "sign_mismatch": float(((fj > 0) != (fu > 0)).mean()),
            "nxt_share_mean": float(nxw.mean()),
            "nxt_share_median": float(np.median(nxw)),
            "nxt_share_max": float(nxw.max())}


def top_overlap(P_un, P_j, design: str, top: int = 15) -> dict:
    """**가속 A** 상위 N 겹침 — 가속 설계만 해당(보조).

    실거래 스캐너의 점수는 가속·흐름·유동성 백분위를 60/20/20으로 섞은 값이다.
    여기서는 유동성·흐름이 두 기준에서 거의 같으므로 **가속 A만으로 줄 세운다** —
    스캐너 점수 겹침과 같은 수가 아니다. 리포트에 그대로 적는다.
    """
    if design != "accel":
        return {}
    m = np.flatnonzero(P_un["in_period"])
    sc_u = P_un["score_in"]["accel"]
    sc_j = P_j["score_in"]["accel"]
    rates, days = [], 0
    for k in m:
        iu = np.flatnonzero(P_un["gate"][design][:, k])
        ij = np.flatnonzero(P_j["gate"][design][:, k])
        if len(iu) < top or len(ij) < top:
            continue
        tu = set(iu[np.argsort(-np.nan_to_num(sc_u[iu, k], nan=-9e9))][:top])
        tj = set(ij[np.argsort(-np.nan_to_num(sc_j[ij, k], nan=-9e9))][:top])
        rates.append(len(tu & tj) / top)
        days += 1
    return {"top": top, "days": days,
            "overlap_mean": float(np.mean(rates)) if rates else None,
            "overlap_median": float(np.median(rates)) if rates else None}


# ─────────────────────────────────────────────────────────────
# 본체
# ─────────────────────────────────────────────────────────────

def run(resamples: int, log=print) -> dict:
    with open(PREREG_PATH, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    log(f"사전등록 sha256 {sha[:8]}")

    cal_all, _, _ = bb.load_calendar_and_index()
    cal = [d for d in cal_all if d >= LOAD_FROM]

    log("유니버스 이력 로드")
    intervals = bb.load_universe_intervals()
    sids = sorted({s for iv in intervals.values() for s in iv})

    log(f"체결량 로드 J / NX ({LOAD_FROM}~{SIGNAL_END})")
    j_sides, j_zero = tc.load_sides(sids, LOAD_FROM, SIGNAL_END, "J")
    nx_sides, nx_zero = tc.load_sides(sids, LOAD_FROM, SIGNAL_END, "NX")
    log(f"  J  {sum(len(v) for v in j_sides.values()):,}행 ({len(j_sides)}종목)"
        f" · 흐름없음 {j_zero:,}")
    log(f"  NX {sum(len(v) for v in nx_sides.values()):,}행 ({len(nx_sides)}종목)"
        f" · 흐름없음 {nx_zero:,}")

    un_sides, nxstat = make_un_sides(j_sides, nx_sides)
    log(f"  UN 쌍 {nxstat['pairs']:,} · NX 있음 {nxstat['with_nx']:,} "
        f"({nxstat['with_nx'] / max(1, nxstat['pairs']):.1%}) · "
        f"없음 {nxstat['without_nx']:,}")

    is_member = member_filter(intervals)
    cov = nx_coverage(j_sides, nx_sides, cal, is_member)
    cov_all = nx_coverage(j_sides, nx_sides, cal)      # 분모=수집 종목 전체(기록용)
    signal_start = signal_start_from(cov, cal)
    log(f"  NX 커버리지[구성종목] {COVERAGE_MIN:.0%} 최초 도달 "
        f"{cov['first_cross']} → 신호 시작 {signal_start} "
        f"(워밍업 {WARMUP_DAYS}거래일)")
    log(f"  NX 커버리지[수집 전체] 최초 도달 {cov_all['first_cross']} "
        f"(참고 — 분모 해석이 모호했다, 리포트에 둘 다 적는다)")

    log(f"일봉 로드 ({LOAD_FROM}~) — {len(sids)}종목")
    bars = bb.load_all_bars(set(sids), load_from=LOAD_FROM)

    panels = {}
    for basis, sides, zero in (("J", j_sides, j_zero), ("UN", un_sides, j_zero)):
        log(f"패널 {basis}")
        panels[basis] = tc.build_panel(
            log=lambda *a, **k: None,
            signal_start=signal_start, signal_end=SIGNAL_END,
            load_from=LOAD_FROM, market_div=basis, bars=bars,
            sides=sides, zero_rows=zero)
        P = panels[basis]
        for d in DESIGNS:
            log(f"  {basis} {d}: 입력 갖춤 {int(P['ok'][d].sum()):,} · "
                f"통과 {int(P['gate'][d].sum()):,}")

    Pj, Pu = panels["J"], panels["UN"]
    log(f"신호 구간 {int(Pu['in_period'].sum())}거래일 "
        f"({signal_start} ~ {SIGNAL_END})")

    block = PRIMARY_H
    order = ["H1", "H2", "H3", "H4", "H5", "H6"]
    spec = {
        "H1": ("accel 기준차 UN−J", "accel", "diff"),
        "H2": ("v15daily 기준차 UN−J", "v15daily", "diff"),
        "H3": ("accel(f_UN) − 유니버스", "accel", "uni"),
        "H4": ("accel(f_UN) − 지수", "accel", "idx"),
        "H5": ("v15daily(f_UN) − 유니버스", "v15daily", "uni"),
        "H6": ("v15daily(f_UN) − 지수", "v15daily", "idx"),
    }

    def series(kind, design, h):
        if kind == "diff":
            return basis_diff(Pu, Pj, design, h)
        if kind == "uni":
            return tc.paired_vs_universe(Pu, design, h)
        return tc.paired_vs_index(Pu, design, h)

    primary = {}
    for key in order:
        name, design, kind = spec[key]
        s = series(kind, design, PRIMARY_H)
        primary[key] = {"name": name, "design": design, "kind": kind,
                        "two_sided_prediction": kind == "diff",
                        "stats": bb.summarize(s["x"], block, resamples, SEED)}
        if kind == "diff":
            primary[key]["skipped_days"] = s["skipped_days"]
    qs = bb.bh_fdr([primary[k]["stats"].get("p", 1.0) for k in order])
    for key, q in zip(order, qs):
        primary[key]["q"] = q
        primary[key]["verdict"] = bb.verdict(primary[key]["stats"], q)
        st = primary[key]["stats"]
        log(f"  {key} {primary[key]['verdict']:<22} "
            f"평균 {st.get('mean', float('nan')):+.5f} "
            f"n={st.get('n_days')} q={q:.4f}")

    # ── 보조 (판정에 쓰지 않는다) ──
    aux = {"secondary_h": {}, "j_only_window": {}, "gate_agreement": {},
           "groups": {}, "flow": {}, "top_overlap": {}, "coverage": {}}
    aux["secondary_h"][SECONDARY_H] = {
        key: bb.summarize(series(spec[key][2], spec[key][1], SECONDARY_H)["x"],
                          SECONDARY_H, resamples, SEED) for key in order}
    for design in DESIGNS:
        aux["j_only_window"][design] = {
            "vs_universe": bb.summarize(
                tc.paired_vs_universe(Pj, design, PRIMARY_H)["x"],
                block, resamples, SEED),
            "vs_index": bb.summarize(
                tc.paired_vs_index(Pj, design, PRIMARY_H)["x"],
                block, resamples, SEED)}
        aux["gate_agreement"][design] = gate_agreement(Pu, Pj, design)
        aux["groups"][design] = {
            "J": tc.group_stats(Pj, design, PRIMARY_H),
            "UN": tc.group_stats(Pu, design, PRIMARY_H)}
        aux["top_overlap"][design] = top_overlap(Pu, Pj, design)
    aux["flow"] = flow_agreement(j_sides, un_sides, signal_start, SIGNAL_END)
    def _cov_summary(c):
        sh = [c["share"][d] for d in cal if d in c["share"] and d >= NXT_START]
        inp = [c["share"][d] for d in cal
               if d in c["share"] and signal_start <= d <= SIGNAL_END]
        return {"first_cross": str(c["first_cross"]),
                "days_after_nxt_start": len(sh),
                "share_mean": float(np.mean(sh)) if sh else None,
                "share_max": float(max(sh)) if sh else None,
                "share_last": sh[-1] if sh else None,
                "in_period_share_mean": float(np.mean(inp)) if inp else None,
                "in_period_share_min": float(min(inp)) if inp else None}
    aux["coverage"] = {
        "denominator_used": "그날의 구성종목(코스피200∪코스닥150)",
        "members": _cov_summary(cov),
        "all_collected": _cov_summary(cov_all),
        "nx_pairs_with_qty": nxstat["with_nx"],
        "nx_pairs_without_qty": nxstat["without_nx"],
        "nx_securities": len(nx_sides),
        "j_securities": len(j_sides)}

    return {
        "prereg": {"path": os.path.relpath(PREREG_PATH, _BASE), "sha256": sha},
        "run_id": f"nxtbasis_{datetime.utcnow():%Y%m%d%H%M%S}_{uuid.uuid4().hex[:6]}",
        "bases": {"J": "KRX 단독", "UN": "KRX + NXT (J + NX 합산)"},
        "designs": {"accel": "accel-1.0 게이트 5개",
                    "v15daily": "V1.5 정규장 절대조건 5개(30분 모멘텀 제외)"},
        "period": [str(signal_start), str(SIGNAL_END)],
        "signal_start_rule": (f"NX 커버리지 ≥ {COVERAGE_MIN:.0%} 최초 거래일"
                              f"({cov['first_cross']}) + {WARMUP_DAYS}거래일"),
        "trading_days": int(Pu["in_period"].sum()),
        "securities": len(Pu["sids"]),
        "horizons": {"primary": PRIMARY_H, "secondary": SECONDARY_H},
        "stats_config": {"bootstrap": "moving_block", "block": block,
                         "resamples": resamples, "seed": SEED,
                         "z_mde": bb.Z_MDE, "fdr": "BH over 6 primary"},
        "cost": Pu["cost"],
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
                         f"prereg-nxtbasis-{res['prereg']['sha256'][:8]}.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1, default=str)
        print(f"저장: {os.path.relpath(p, _BASE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

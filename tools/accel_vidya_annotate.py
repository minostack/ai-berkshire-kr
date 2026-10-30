#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""가속 스캔 결과에 VIDYA를 **참고 열로** 붙인다 — 게이트는 바꾸지 않는다.

사전등록 `docs/PREREGISTRATION_VIDYA.md` §4: 네 주가설 전부 `UNDERPOWERED`
였으므로 **스캐너 게이트를 바꾸지 않는다**(`tools/cvd_accel_scanner.py`의
`DESIGN_VERSION`·게이트·점수 그대로). 이 도구는 이미 저장된 스캔 스냅샷을
읽어 VIDYA 값과 통과 여부를 **표시만** 한다.

VIDYA는 기준 일봉(bars-1.0, 정규장·수정주가) 종가로 계산한다. 스냅샷의
MA20·유동성은 스캔 당시 봉으로 계산된 값이므로, 그 사이 잠정→확정 전환이
있었으면 다를 수 있다 — 그래서 MA20 이격을 확정 봉으로 다시 계산해 대조한다.

    python tools/accel_vidya_annotate.py --snapshot <경로>
    python tools/accel_vidya_annotate.py --snapshot <경로> --top 20 --save-json
"""

import argparse
import glob
import json
import os
import sys
from datetime import date, datetime

import numpy as np

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))

from cvd_obv.bar_standard import (  # noqa: E402
    is_provisional, read_standard_daily,
)
from cvd_obv.indicators import CMO_LENGTH, VIDYA_LENGTH, vidya  # noqa: E402

OUT_DIR = os.path.join(_BASE, "reports", "daily-scan")
MA_DAYS = 20
WARMUP_DAYS = 400          # VIDYA 시드 영향 제거 (사전등록 §1.2)


def latest_snapshot(pattern="cvd-accel-both-*.json") -> str:
    paths = sorted(glob.glob(os.path.join(OUT_DIR, pattern)))
    if not paths:
        raise SystemExit(f"스냅샷이 없다: {pattern}")
    return paths[-1]


def daily_series(codes, as_of: date) -> dict:
    """{code: {close, vidya, vidya_prev, ma20, bars, last_day, provisional}}."""
    import pyarrow.dataset as ds

    sids = sorted(f"KRX:{c}" for c in codes)
    t = read_standard_daily(
        columns=["security_id", "trading_date", "close", "source"],
        filters=(ds.field("trading_date") <= as_of)
        & ds.field("security_id").isin(sids),
        drop_failed=True, allow_extended=False)
    t = t.sort_by([("security_id", "ascending"), ("trading_date", "ascending")])
    by = {}
    for sid, d, c, src in zip(t.column("security_id").to_pylist(),
                              t.column("trading_date").to_pylist(),
                              t.column("close").to_pylist(),
                              t.column("source").to_pylist()):
        by.setdefault(sid, []).append((d, float(c), src))

    out = {}
    for sid, rows in by.items():
        rows = rows[-WARMUP_DAYS:]
        close = np.array([r[1] for r in rows], float)
        vd = vidya(close, VIDYA_LENGTH, CMO_LENGTH)
        ma = (float(close[-MA_DAYS:].mean()) if len(close) >= MA_DAYS else None)
        out[sid.split(":")[1]] = {
            "close": float(close[-1]),
            "vidya": float(vd[-1]) if np.isfinite(vd[-1]) else None,
            "vidya_prev": float(vd[-2]) if len(vd) > 1 and np.isfinite(vd[-2]) else None,
            "ma20": ma,
            "bars": len(close),
            "last_day": rows[-1][0].isoformat(),
            "provisional": bool(is_provisional(rows[-1][2])),
        }
    return out


def annotate(snapshot_path: str, as_of: date = None) -> dict:
    snap = json.load(open(snapshot_path, encoding="utf-8"))
    results = snap["results"]
    flow_as_of = next((r["accel"]["flow_as_of"] for r in results
                       if r.get("accel")), None)
    if as_of is None:
        as_of = datetime.strptime(flow_as_of, "%Y%m%d").date()

    series = daily_series([r["code"] for r in results], as_of)

    rows, missing = [], []
    for r in results:
        s = series.get(r["code"])
        if not s or s["vidya"] is None or s["last_day"] != as_of.isoformat():
            missing.append(r["code"])
            v1 = v2 = None
            gap = None
        else:
            v1 = s["close"] > s["vidya"]
            v2 = (s["vidya_prev"] is not None and s["vidya"] > s["vidya_prev"])
            gap = (s["close"] / s["vidya"] - 1) * 100
        ma_now = s["ma20"] if s else None
        rows.append({
            "code": r["code"], "name": r["name"], "rank": r.get("rank"),
            "status": r["status"], "score": r.get("total"),
            "accel": (r["accel"] or {}).get("accel"),
            "flow_recent": (r["accel"] or {}).get("flow_recent"),
            "close_vs_ma20_pct_snapshot": (r.get("price") or {}).get("close_vs_ma20_pct"),
            "close_vs_ma20_pct_confirmed": (
                round((s["close"] / ma_now - 1) * 100, 2)
                if (s and ma_now) else None),
            "liquidity": (r.get("liquidity") or {}).get("value"),
            "close": s["close"] if s else None,
            "vidya": round(s["vidya"], 2) if (s and s["vidya"]) else None,
            "vidya_gap_pct": round(gap, 2) if gap is not None else None,
            "V1_above_vidya": v1, "V2_vidya_rising": v2,
            "daily_provisional": s["provisional"] if s else None,
        })

    cand = [x for x in rows if x["status"] == "BUY_CANDIDATE"]
    v1_pass = [x for x in cand if x["V1_above_vidya"]]
    v2_pass = [x for x in cand if x["V2_vidya_rising"]]

    # 무결성 — 확정 봉으로 다시 계산한 과열 판정이 뒤집히는 종목
    flips = []
    for x in cand:
        a, b = x["close_vs_ma20_pct_snapshot"], x["close_vs_ma20_pct_confirmed"]
        if a is None or b is None:
            continue
        if (a <= 10.0) != (b <= 10.0):
            flips.append({"code": x["code"], "name": x["name"],
                          "snapshot": a, "confirmed": b})

    return {
        "tool": "accel_vidya_annotate",
        "note": ("VIDYA는 참고 열이다 — 사전등록 VIDYA §4(전부 UNDERPOWERED)에 따라 "
                 "가속 게이트·점수·DESIGN_VERSION을 바꾸지 않았다"),
        "prereg": {"path": "docs/PREREGISTRATION_VIDYA.md",
                   "verdict": "4/4 UNDERPOWERED",
                   "report": "reports/VIDYA-가속결합-백테스트-20261012.md"},
        "snapshot": os.path.relpath(snapshot_path, _BASE),
        "snapshot_as_of": snap.get("as_of"),
        "flow_as_of": flow_as_of,
        "daily_as_of": as_of.isoformat(),
        "vidya_params": {"cmo_length": CMO_LENGTH, "vidya_length": VIDYA_LENGTH,
                         "price": "기준 일봉 REGULAR 종가", "cmo_scale": "-1..+1"},
        "counts": {
            "scanned": len(rows), "candidates": len(cand),
            "v1_pass": len(v1_pass), "v2_pass": len(v2_pass),
            "v1_ratio": (len(v1_pass) / len(cand)) if cand else None,
            "v2_ratio": (len(v2_pass) / len(cand)) if cand else None,
            "vidya_unavailable": len(missing),
        },
        "ma20_overheat_flips": flips,
        "rows": rows,
    }


def _mark(flag) -> str:
    """통과 O · 미통과 · · 계산 불가 ?"""
    if flag is None:
        return "?"
    return "O" if flag else "·"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default=None)
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--save-json", action="store_true")
    a = ap.parse_args()

    path = a.snapshot or latest_snapshot()
    res = annotate(path)
    c = res["counts"]

    print()
    print("=" * 104)
    print(f"  CVD 가속 + VIDYA(참고)  —  흐름 기준일 {res['flow_as_of']} · "
          f"일봉 기준일 {res['daily_as_of']}")
    print(f"  VIDYA: CMO {CMO_LENGTH} · 길이 {VIDYA_LENGTH} · 기준 일봉 정규장 종가")
    print("=" * 104)
    print("  ⚠ VIDYA는 **게이트가 아니다**. 사전등록 백테스트에서 네 주가설 전부")
    print("    UNDERPOWERED(최대 +0.102%p, MDE 0.409%)였으므로 가속 게이트를 바꾸지")
    print("    않았다. 아래 V1/V2 열은 표시용이다.")
    print("=" * 104)
    print(f"  스캔 {c['scanned']} · 후보 {c['candidates']} · "
          f"V1(종가>VIDYA) {c['v1_pass']}"
          f"({(c['v1_ratio'] or 0) * 100:.1f}%) · V2(VIDYA 상승) {c['v2_pass']}"
          f"({(c['v2_ratio'] or 0) * 100:.1f}%)")
    if c["vidya_unavailable"]:
        print(f"  VIDYA 계산 불가 {c['vidya_unavailable']}종목")
    print()

    cand = [x for x in res["rows"] if x["status"] == "BUY_CANDIDATE"]
    hdr = (f"  {'순위':>4} {'종목':<22}{'점수':>7}{'가속A':>10}"
           f"{'MA20이격':>10}{'VIDYA':>12}{'VIDYA이격':>10}{'V1':>5}{'V2':>5}")
    for title, sel in (("전체 후보 상위", cand),
                       ("V1(종가>VIDYA) 통과분 상위",
                        [x for x in cand if x["V1_above_vidya"]])):
        print(f"  ── {title} {min(a.top, len(sel))}/{len(sel)} ──")
        print(hdr)
        print("  " + "-" * 100)
        for x in sel[:a.top]:
            v1 = _mark(x["V1_above_vidya"])
            v2 = _mark(x["V2_vidya_rising"])
            ma = x["close_vs_ma20_pct_confirmed"]
            ma_cell = f"{ma:+.1f}%" if ma is not None else "—"
            vd_cell = f"{x['vidya']:,.0f}" if x["vidya"] else "—"
            gap_cell = (f"{x['vidya_gap_pct']:+.1f}%"
                        if x["vidya_gap_pct"] is not None else "—")
            print(f"  {x['rank']:>4} {x['code']} {x['name'][:14]:<15}"
                  f"{x['score']:>7.1f}{x['accel']:>+10.4f}"
                  f"{ma_cell:>10}{vd_cell:>12}{gap_cell:>10}"
                  f"{v1:>5}{v2:>5}")
        print()

    if res["ma20_overheat_flips"]:
        print(f"  ⚠ 확정 봉으로 다시 계산하면 과열 판정이 뒤집히는 후보 "
              f"{len(res['ma20_overheat_flips'])}종목:")
        for f in res["ma20_overheat_flips"]:
            print(f"     {f['code']} {f['name']}: 스냅샷 {f['snapshot']:+.1f}% → "
                  f"확정 {f['confirmed']:+.1f}%")
    else:
        print("  확정 봉으로 다시 계산해도 과열 판정이 뒤집히는 후보는 없다")

    if a.save_json:
        os.makedirs(OUT_DIR, exist_ok=True)
        name = (f"cvd-accel-vidya-{res['flow_as_of']}-"
                f"{os.path.basename(path).split('-')[-1]}")
        out = os.path.join(OUT_DIR, name)
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=1)
        print(f"\n  💾 저장: {os.path.relpath(out, _BASE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

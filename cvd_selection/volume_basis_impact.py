# -*- coding: utf-8 -*-
"""거래량 기준 영향 측정 — 블록딜 포함(일봉) vs 제외(1분봉 범위).

    python cvd_selection/volume_basis_impact.py [--start 2025-11-13] [--end 2026-10-22]

정의 (종목·거래일마다)
---------------------
- 포함 V_in  = 기준 확정 일봉 거래량(bars-1.0, 지금 스캐너가 쓰는 값)
- 1분봉 범위 M = KRX 1분봉 합(애프터마켓 제외 — 정규장·늦은 종가 체결·시간외 종가)
                 + NXT 메인 1분봉 합(그날 일봉에 NXT가 포함된 경우만 — 범위를 일봉과 맞춘다)
- 제외 V_ex  = min(V_in, M). 일봉에만 있는 물량(블록딜 추정, `분석-일봉거래량-…md`)을 뺀다
- 거래대금: 포함 = 일봉 거래대금(1분봉 복원 봉은 bars-1.0 추정), 제외 = 같은 범위 1분봉
  누적거래대금 차이 합(정확). 1분봉 거래대금이 없는 봉은 0으로 센다(키움 전용 15:35 봉뿐)

측정
----
RVOL = V_D / 직전 20거래일 평균, AVG_A20 = 직전 20거래일 거래대금 평균(D 제외).
- 포함/포함(지금 확정 재현) vs 제외/제외 — 기준선(RVOL 1.3·1.5·2.0, AVG_A20 100억) 판정이 바뀐 비율
- 실시간 혼합: 당일 D는 16:10 잠정 봉(= 1분봉 합, 블록딜 없음)인데 과거 20일은 확정 봉(블록딜 포함)이다.
  제외/포함 조합이 실제 당일 실행과 같은 정의다
1분봉 누적거래대금은 2025-10-15부터 있으므로 기본 시작일은 그 뒤 20거래일이다.
"""

import argparse
import collections
import glob
import os
import statistics
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cvd_selection import data as D  # noqa: E402
from cvd_obv.storage.parquet_store import ParquetStore  # noqa: E402
from cvd_obv.universe import current_universe  # noqa: E402

_COLS = ["security_id", "trading_date", "session", "bar_start_at_utc", "volume",
         "traded_value", "run_id", "data_revision", "quality_status"]


def _month_tables(store, start, end, ids):
    """월 partition마다 1분봉을 읽어 (종목, 날짜) 합계를 돌려준다 — 중복 제거는 Arrow로."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.dataset as ds

    base = store.table_dir("silver", "minute_bars")
    out = {}
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        files = glob.glob(os.path.join(base, "period_min=1", f"year={y}", f"month={m:02d}",
                                       "**", "*.parquet"), recursive=True)
        parts = []
        flt = ((ds.field("trading_date") >= start) & (ds.field("trading_date") <= end)
               & ds.field("security_id").isin(ids))
        for p in files:
            dset = ds.dataset(p, format="parquet", partitioning=None)
            have = set(dset.schema.names)
            t = dset.to_table(columns=[c for c in _COLS if c in have], filter=flt)
            if not t.num_rows:
                continue
            for c in _COLS:
                if c not in have:
                    t = t.append_column(c, pa.nulls(t.num_rows))
            parts.append(t.select(_COLS))
        if parts:
            t = pa.concat_tables(parts, promote_options="permissive")
            t = t.filter(pc.not_equal(t.column("quality_status"), "FAILED"))
            # decimal은 first/last 집계를 받지 않는다 — 원 단위 정수라 float64로 바꿔도 같다
            t = t.set_column(t.schema.get_field_index("traded_value"), "traded_value",
                             pc.cast(t.column("traded_value"), pa.float64()))
            # 같은 봉 키의 최신 리비전·나중 run_id 한 행 — 저장소 규칙과 같다
            t = t.sort_by([("security_id", "ascending"), ("bar_start_at_utc", "ascending"),
                           ("session", "ascending"), ("data_revision", "ascending"),
                           ("run_id", "ascending")])
            t = t.group_by(["security_id", "bar_start_at_utc", "session"], use_threads=False
                           ).aggregate([("trading_date", "last"), ("volume", "last"),
                                        ("traded_value", "last")])
            t = t.rename_columns(["security_id", "bar_start_at_utc", "session",
                                  "trading_date", "volume", "traded_value"])
            hm = pc.add(pc.multiply(pc.hour(t.column("bar_start_at_utc")), 60),
                        pc.minute(t.column("bar_start_at_utc")))   # UTC 분, KST − 9h
            is_nxt = pc.starts_with(t.column("security_id"), "NXT:")
            krx_keep = pc.and_(pc.invert(is_nxt), pc.not_equal(t.column("session"), "AFTER"))
            nxt_keep = pc.and_(is_nxt, pc.less(hm, 6 * 60 + 30))   # KST 09:00~15:30
            nxt_keep = pc.and_(nxt_keep, pc.greater_equal(hm, 0))
            t = t.filter(pc.or_(krx_keep, nxt_keep))
            tv = pc.fill_null(pc.cast(t.column("traded_value"), pa.float64()), 0.0)
            t = t.set_column(t.schema.get_field_index("traded_value"), "traded_value", tv)
            g = t.group_by(["security_id", "trading_date"]).aggregate(
                [("volume", "sum"), ("traded_value", "sum")])
            for sid, d, v, a in zip(g.column("security_id").to_pylist(),
                                    g.column("trading_date").to_pylist(),
                                    g.column("volume_sum").to_pylist(),
                                    g.column("traded_value_sum").to_pylist()):
                venue, code = sid.split(":")
                out[(venue, code, d)] = (int(v), float(a))
        print(f"  1분봉 {y}-{m:02d} 집계 완료", flush=True)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-11-13")
    ap.add_argument("--end", default="2026-10-22")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    mstart = date(2025, 10, 15)

    u = current_universe()
    codes = sorted(u.codes())
    store = ParquetStore()
    ids = [f"KRX:{c}" for c in codes] + [f"NXT:{c}" for c in codes]
    mins = _month_tables(store, mstart, end, ids)
    daily = D.load_daily([f"KRX:{c}" for c in codes], mstart, end)

    stats = collections.Counter()
    ratio_rvol, ratio_live, ratio_a20, block_share = [], [], [], []
    month_flip = collections.defaultdict(collections.Counter)
    for c in codes:
        rows = []
        for b in daily.get(c, []):
            k = mins.get(("KRX", c, b["date"]))
            if k is None or b["provisional"]:
                rows.append(None)
                continue
            m_vol, m_val = k
            if "NXT_MAIN_VOLUME_INCLUDED" in b["flags"]:
                n = mins.get(("NXT", c, b["date"]), (0, 0.0))
                m_vol += n[0]
                m_val += n[1]
            v_in = b["volume"]
            v_ex = min(v_in, m_vol)
            rows.append({"date": b["date"], "v_in": v_in, "v_ex": v_ex,
                         "a_in": b["value"], "a_ex": m_val})
        for i in range(20, len(rows)):
            r, prev = rows[i], rows[i - 20:i]
            if r is None or any(p is None for p in prev) or not (start <= r["date"] <= end):
                continue
            mi = statistics.fmean(p["v_in"] for p in prev)
            me = statistics.fmean(p["v_ex"] for p in prev)
            ai = statistics.fmean(p["a_in"] for p in prev)
            ae = statistics.fmean(p["a_ex"] for p in prev)
            if not (mi and me and ai):
                continue
            rv_in, rv_ex = r["v_in"] / mi, r["v_ex"] / me
            rv_live = r["v_ex"] / mi                   # 당일 잠정(블록딜 없음) ÷ 과거 확정(포함)
            stats["n"] += 1
            block_share.append(1 - r["v_ex"] / r["v_in"])
            ratio_rvol.append(rv_ex / rv_in)
            ratio_live.append(rv_live / rv_in)
            ratio_a20.append(ae / ai)
            ym = r["date"].strftime("%Y-%m")
            month_flip[ym]["n"] += 1
            for th in (1.3, 1.5, 2.0):
                if (rv_in >= th) != (rv_ex >= th):
                    stats[f"rvol{th}_flip"] += 1
                    stats[f"rvol{th}_{'in>ex' if rv_in >= th else 'ex>in'}"] += 1
                    if th == 1.3:
                        month_flip[ym]["rvol"] += 1
                if (rv_in >= th) != (rv_live >= th):
                    stats[f"live{th}_flip"] += 1
                    stats[f"live{th}_{'lost' if rv_in >= th else 'gained'}"] += 1
                stats[f"rvol{th}_in"] += rv_in >= th
                stats[f"rvol{th}_ex"] += rv_ex >= th
                stats[f"rvol{th}_live"] += rv_live >= th
            if (ai >= 1e10) != (ae >= 1e10):
                stats["a20_flip"] += 1
                month_flip[ym]["a20"] += 1
            stats["a20_in"] += ai >= 1e10
            stats["a20_ex"] += ae >= 1e10
            if r["v_ex"] < r["v_in"] * 0.98:
                stats["d_block2"] += 1

    n = stats["n"]

    def pct(x):
        return f"{100 * x / n:.2f}%"

    def q(xs, p):
        xs = sorted(xs)
        return xs[min(len(xs) - 1, int(p * len(xs)))]

    print(f"\n표본 {n:,} 종목·거래일 ({start} ~ {end}, 현재 구성종목 {len(codes)})")
    print(f"당일 블록딜(제외 물량 > 2%) 있는 날: {stats['d_block2']:,} ({pct(stats['d_block2'])})")
    print(f"제외 물량 비중: 평균 {100*statistics.fmean(block_share):.2f}% · 95% {100*q(block_share,.95):.2f}% · "
          f"99% {100*q(block_share,.99):.2f}% · 최대 {100*max(block_share):.1f}%")
    print("\n[1] 확정 기준 비교 — RVOL 포함(V_in/평균 V_in) vs 제외(V_ex/평균 V_ex)")
    print(f"  RVOL 제외/포함 비: 중앙 {statistics.median(ratio_rvol):.4f} · 1% {q(ratio_rvol,.01):.3f} · "
          f"99% {q(ratio_rvol,.99):.3f}")
    for th in (1.3, 1.5, 2.0):
        print(f"  RVOL ≥ {th}: 포함 {stats[f'rvol{th}_in']:,} / 제외 {stats[f'rvol{th}_ex']:,} · "
              f"판정 바뀜 {stats[f'rvol{th}_flip']:,} ({pct(stats[f'rvol{th}_flip'])}) — "
              f"포함만 통과 {stats[f'rvol{th}_in>ex']:,}, 제외만 통과 {stats[f'rvol{th}_ex>in']:,}")
    print(f"  AVG_A20 제외/포함 비: 중앙 {statistics.median(ratio_a20):.4f} · 1% {q(ratio_a20,.01):.3f}")
    print(f"  AVG_A20 ≥ 100억: 포함 {stats['a20_in']:,} / 제외 {stats['a20_ex']:,} · "
          f"판정 바뀜 {stats['a20_flip']:,} ({pct(stats['a20_flip'])})")
    print("\n[2] 당일 실행 혼합 — 당일 잠정 봉(블록딜 없음) ÷ 과거 확정(블록딜 포함) vs 확정 포함")
    print(f"  RVOL 혼합/포함 비: 중앙 {statistics.median(ratio_live):.4f} · 1% {q(ratio_live,.01):.3f}")
    for th in (1.3, 1.5, 2.0):
        print(f"  RVOL ≥ {th}: 혼합 {stats[f'rvol{th}_live']:,} · 바뀜 {stats[f'live{th}_flip']:,} "
              f"({pct(stats[f'live{th}_flip'])}) — 혼합에서 탈락 {stats[f'live{th}_lost']:,}, "
              f"혼합에서만 통과 {stats[f'live{th}_gained']:,}")
    print("\n[3] 월별 판정 바뀜 (RVOL 1.3 / AVG_A20 100억)")
    for ym in sorted(month_flip):
        f = month_flip[ym]
        print(f"  {ym}  표본 {f['n']:>6,}  RVOL {f['rvol']:>4} ({100*f['rvol']/f['n']:.2f}%)  "
              f"A20 {f['a20']:>3} ({100*f['a20']/f['n']:.2f}%)")


if __name__ == "__main__":
    main()

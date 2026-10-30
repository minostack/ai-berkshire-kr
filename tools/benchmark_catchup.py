#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""지수 일봉 일일 보충 — 마지막 적재일 **이후만** 받는다.

왜 이 껍데기가 필요한가
----------------------
`cvdobv.py benchmark build`는 구간을 주지 않으면 **전 구간을 다시 받아 같은 키를
또 쓴다.** `silver.benchmark_bars`의 PK는
`(benchmark_id, trading_date, price_scope, data_revision)`이고 적재기는
`data_revision=1`을 고정으로 쓰므로, 매일 전 구간을 돌리면 중복이 하루 13,000행씩
쌓인다. 실제로 과거 두 run(`bm_20260918060000_krx`, `bench_20260923`)이 그렇게
겹쳤고, 그중 **3행은 값이 다르다**(2026-09-18 장중 스냅샷 대 최종값).
`ab_backtest_b.load_calendar_and_index`는 `data_revision`을 보지 않고 날짜 키로
덮어쓰므로, 중복이 있으면 **어느 값이 쓰일지가 파일 순서에 달린다.**
근거: `reports/NXTBASIS-KRX단독-통합기준-비교-20261019.md` §7-7

그래서 `data-load`와 **같은 사고**로 동작한다 — 마지막 적재일을 스스로 찾고
그 다음 거래일부터만 받는다(bars-1.0의 2단계 적재와 같은 원칙).

받을 구간이 없으면 아무것도 쓰지 않고 0으로 끝난다(주말·공휴일 안전).

    python tools/benchmark_catchup.py
    python tools/benchmark_catchup.py --dry-run
    python tools/benchmark_catchup.py --benchmarks KRX:KOSPI200
"""

import argparse
import os
import sys
from datetime import date, datetime, timedelta

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))

from cvd_obv.domain.status import ExitCode, QualityStatus  # noqa: E402
from cvd_obv.ingestion.benchmark_history import (  # noqa: E402
    BenchmarkHistoryRepository,
)
from cvd_obv.ingestion.kiwoom_benchmark import BENCHMARKS  # noqa: E402
from cvd_obv.ingestion.market_source import benchmark_source  # noqa: E402
from cvd_obv.storage.parquet_store import ParquetStore  # noqa: E402

DEFAULT_BENCHMARKS = ("KRX:KOSPI200", "KRX:KOSPI", "KRX:KOSDAQ")


def last_stored(store: ParquetStore) -> dict:
    """{benchmark_id: 마지막 거래일}. 테이블이 없으면 빈 dict."""
    try:
        t = store.read_arrow("silver", "benchmark_bars",
                             columns=["benchmark_id", "trading_date"])
    except Exception:
        return {}
    out = {}
    for b, d in zip(t.column("benchmark_id").to_pylist(),
                    t.column("trading_date").to_pylist()):
        if out.get(b) is None or d > out[b]:
            out[b] = d
    return out


def plan(last: dict, ids, end: date) -> tuple:
    """무엇을 어느 구간으로 받을지 정한다 — 부수효과 없는 순수 함수.

    돌려주는 것: ({시작일 또는 None: [지수]}, [(지수, 마지막적재일) 건너뛴 것])
    시작일 `None`은 '적재된 것이 없어 전 구간'을 뜻한다.
    """
    groups, skipped = {}, []
    for bid in ids:
        lo = last.get(bid)
        start = (lo + timedelta(days=1)) if lo else None
        if start is not None and start > end:
            skipped.append((bid, lo))
            continue
        groups.setdefault(start, []).append(bid)
    return groups, skipped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--benchmarks", default=",".join(DEFAULT_BENCHMARKS))
    ap.add_argument("--end", default=None,
                    help="YYYY-MM-DD (기본: 어제 — 당일 지수는 장중에 확정되지 않는다)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--source", default=None, choices=("kis", "kiwoom"),
                    help="원천(기본 환경변수 CVD_MARKET_SOURCE, 없으면 kis — 2026-10-23부터)")
    a = ap.parse_args()

    ids = [s.strip() for s in a.benchmarks.split(",") if s.strip()]
    unknown = [i for i in ids if i not in BENCHMARKS]
    if unknown:
        print(f"모르는 지수: {unknown} (가능: {sorted(BENCHMARKS)})")
        return int(ExitCode.CLI_CONFIG_ERROR)

    end = (date.fromisoformat(a.end) if a.end
           else date.today() - timedelta(days=1))

    store = ParquetStore()
    last = last_stored(store)
    # 같은 시작일끼리 묶어 한 번에 받는다(보통 한 덩어리다)
    groups, skipped = plan(last, ids, end)
    for bid, lo in skipped:
        print(f"  {bid}: 마지막 적재 {lo} — 받을 구간 없음 (끝 {end}), 건너뛴다")
    for start, bids in sorted(groups.items(),
                              key=lambda kv: (kv[0] is not None, kv[0])):
        for bid in bids:
            print(f"  {bid}: 마지막 적재 {last.get(bid)} → "
                  f"시작 {start or '전 구간'}")

    if not groups:
        print(f"전부 최신이다 (끝 {end}) — 아무것도 쓰지 않았다")
        return int(ExitCode.OK)

    repo = BenchmarkHistoryRepository(source=benchmark_source(a.source))
    print(f"  원천: {repo.source.SOURCE if hasattr(repo.source, 'SOURCE') else 'KIWOOM_KA20006'}")
    rows = 0
    status_partial = False
    for start, bids in sorted(groups.items(), key=lambda kv: (kv[0] is not None, kv[0])):
        run_id = f"bench_catchup_{datetime.now():%Y%m%d%H%M%S}"
        print(f"  적재 {bids} {start or '전 구간'} ~ {end}"
              + (" [dry-run]" if a.dry_run else ""))
        s = repo.build(bids, run_id=run_id, start=start, end=end,
                       dry_run=a.dry_run,
                       on_progress=lambda i, n, r: print(
                           f"    [{i}/{n}] {r['benchmark_id']} {r['rows']}행 "
                           f"{r['first_date']}~{r['last_date']} {r['status']}"
                           + (" ⚠ " + ",".join(r["quality_flags"])
                              if r["quality_flags"] else "")))
        rows += s["rows"]
        if s["status"] != QualityStatus.SUCCESS:
            status_partial = True
        for p in s["artifacts"]:
            print(f"    manifest: {p}")

    print(f"완료: {rows}행 (가격지수다 — 배당 재투자 미반영)")
    return int(ExitCode.PARTIAL_SUCCESS if status_partial else ExitCode.OK)


if __name__ == "__main__":
    raise SystemExit(main())

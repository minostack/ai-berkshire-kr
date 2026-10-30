#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KIS 일별 매수/매도 체결량(FHKST03010800) 수집기 — 시장 구분별.

`src/cvd_obv/ingestion/kis_trade_sides.TradeSidesLoader`를 그대로 쓴다.
이 파일은 **대상 종목·기간을 정하고 로그를 남기는 껍데기**다.

시장 구분 (`reports/KIS-체결량-일별CVD-대비검토-20261019.md` §8 실측)
    J  = KRX 단독      NX = NXT 단독      UN = J + NX (잔차 0으로 확인)
섞어 쓰지 않는다. `market_div`가 PK에 들어 있어 같은 테이블에 공존한다.

    python tools/collect_trade_sides.py --market-div NX \
        --start 2025-03-04 --end 2026-10-18 --codes-from table
    python tools/collect_trade_sides.py --market-div NX --dry-run
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))

from cvd_obv.contracts.market_schema import TRADE_SIDE_MARKET_DIVS  # noqa: E402
from cvd_obv.ingestion.kis_trade_sides import (  # noqa: E402
    HISTORY_START, NXT_START, TradeSidesLoader, read_trade_sides,
)
LOG_DIR = os.path.join(_BASE, "logs", "trade-sides")


def codes_from_table(market_div: str, since: date) -> list:
    """이미 적재된 다른 구분의 종목 목록 — 짝지어 비교하려면 모집단이 같아야 한다."""
    import pyarrow.dataset as ds
    t = read_trade_sides(
        columns=["security_id", "trading_date", "market_div"],
        filters=(ds.field("market_div") == market_div)
        & (ds.field("trading_date") >= since))
    return sorted({s.split(":")[-1] for s in t.column("security_id").to_pylist()})


def codes_from_universe(as_of: date) -> list:
    from cvd_obv.universe import universe_on
    u = universe_on(as_of)
    out = []
    for idx in u.describe()["counts"]:
        out.extend(st["code"] for st in u.stocks(idx))
    if not out:
        raise SystemExit("유니버스에서 종목을 하나도 못 읽었다")
    return sorted(set(out))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market-div", required=True, choices=TRADE_SIDE_MARKET_DIVS)
    ap.add_argument("--start", default=None, help="YYYY-MM-DD (기본: NX는 2025-03-04, 그 외 공급자 이력 시작)")
    ap.add_argument("--end", default=None, help="YYYY-MM-DD (기본: 어제 — 당일은 장중 누적값이다)")
    ap.add_argument("--codes-from", default="table", choices=("table", "universe"),
                    help="table = 이미 적재된 J 구분과 같은 종목(기본), universe = 현재 구성종목")
    ap.add_argument("--pair-with", default="J", help="--codes-from table 일 때 짝 구분")
    ap.add_argument("--sleep", type=float, default=0.05)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    md = a.market_div
    start = (date.fromisoformat(a.start) if a.start
             else (NXT_START if md == "NX" else HISTORY_START))
    end = (date.fromisoformat(a.end) if a.end else date.today() - timedelta(days=1))
    if md == "NX" and start < NXT_START:
        raise SystemExit(f"NX는 {NXT_START} 전에 존재하지 않는다 — start를 올려라")

    if a.codes_from == "table":
        codes = codes_from_table(a.pair_with, start)
        src = f"적재된 {a.pair_with} 구분({start} 이후 행이 있는 종목)"
    else:
        codes = codes_from_universe(end)
        src = f"universe_on({end})"

    print(f"시장 구분 {md} · 기간 {start} ~ {end} · 종목 {len(codes)}개")
    print(f"대상 출처: {src}")
    days = (end - start).days
    pages = max(1, -(-int(days * 0.685) // 100) + 1)   # 거래일 ≈ 달력일 × 0.685
    print(f"예상 호출 약 {len(codes) * pages:,}회 "
          f"(종목당 {pages}페이지 추정) → 약 {len(codes) * pages / 4.1 / 60:.0f}분(추정)")
    if a.dry_run:
        print("dry-run — 호출하지 않았다")
        return 0

    os.makedirs(LOG_DIR, exist_ok=True)
    ld = TradeSidesLoader(market_div=md, sleep=a.sleep)
    res = ld.run(codes, start=start, end=end)
    res["codes_source"] = src

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    p = os.path.join(LOG_DIR, f"collect-{md}-{stamp}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=str)

    print()
    print(f"행 {res['rows']:,} · 호출 {res['calls']:,} · 실패 {res['failed_calls']} "
          f"· 패딩 {res['padding_rows']:,} · {res['elapsed_sec'] / 60:.1f}분")
    print(f"응답 없음 {len(res['empty_securities'])}종목 · "
          f"전부 0 {len(res['zero_only_securities'])}종목")
    print(f"요약 저장: {os.path.relpath(p, _BASE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

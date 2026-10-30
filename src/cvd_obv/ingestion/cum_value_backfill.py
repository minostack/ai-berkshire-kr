# -*- coding: utf-8 -*-
"""과거 1분봉에 **누적거래대금**(`cum_traded_value`)을 채운다 — KIS로 다시 받아 덮는다.

2026-10-23 전 1분봉은 키움에서 받아 누적거래대금이 없다(열 자체가 그날 추가됐다).
열만 따로 채울 수는 없으므로(Parquet 행은 바꾸지 않는다) 같은 날을 KIS 1분봉으로 다시
받아 **같은 봉 키로 새 행**을 쓴다. reader는 같은 리비전이면 나중 run_id를 고르므로
(`topup_load_<지금>`) 새 행이 이긴다. 옛 행은 지우지 않는다(이력 보존).

무엇이 바뀌나
- `cum_traded_value`, `traded_value`(누적 차이)가 생긴다. `source`가 KIS가 된다
- 시가·고가·저가·종가·거래량은 같다(005930 2026-10-22 KRX 621/621·NXT 690/690 일치).
  단 KIS 가격은 **지금 기준 수정주가**라, 그 뒤 수정 이벤트가 있었던 종목은 가격이
  바뀐다(207940 2026-10-02 이벤트 전 날 ×0.9923). 거래량 정의는 같다
- 키움에만 있던 15:35 시간외 종가 봉(OTHER)은 KIS에 없어 옛 행이 그대로 남는다

대상과 재개
-----------
대상 = 저장소에 1분봉이 있는 (종목, 날짜) 중 아직 누적거래대금이 없는 쌍(정규장 봉 기준).
받은 날은 대상에서 빠지므로 **끊겨도 다시 돌리면 남은 것만** 받는다. 종목 N개마다
나눠 쓴다. 커버리지(`bar_coverage`)는 쓰지 않는다 — 일일 적재의 재수집 판단을 바꾸지
않기 위해서다.

KIS 1분봉은 **약 1년**만 거슬러 준다(2026-10-23 기준 2025-10-15부터). 그보다 오래된
날은 채울 수 없고, 이 지평은 하루씩 앞으로 움직인다 — 오래된 날을 채우려면 빨리 돌려야
한다.
"""

from __future__ import annotations

import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime

from ..contracts.market_schema import MINUTE_BAR_COLUMNS, MINUTE_BAR_PK, validate_quarantined_ohlc
from ..domain.ids import UTC
from ..storage.parquet_store import ParquetStore
from .minute_history import MinuteHistoryCache, month_dataset

__all__ = ["cum_value_targets", "CumValueBackfill", "PAGES_PER_DAY"]

#: 하루 KIS 호출 수 근사 — KRX 애프터마켓 전(9/11까지) 약 4, 뒤 약 6, NXT 약 6
PAGES_PER_DAY = {"KRX": 5.5, "NXT": 6.5}


def cum_value_targets(store: ParquetStore, start: date, end: date,
                      venues=("KRX", "NXT"), securities=None) -> dict:
    """{저장 종목 ID: [누적거래대금이 없는 날…]} — 정규장 1분봉이 있는 날만."""
    import pyarrow.compute as pc
    import pyarrow.dataset as ds

    data = month_dataset(store, start, end)
    if data is None:
        return {}
    t = data.to_table(
        columns=["security_id", "trading_date", "cum_traded_value"],
        filter=((ds.field("trading_date") >= start) & (ds.field("trading_date") <= end)
                & (ds.field("period_min") == 1) & (ds.field("session") == "REGULAR")
                & (ds.field("quality_status") != "FAILED")))
    have, done = set(), set()
    filled = pc.is_valid(t.column("cum_traded_value")).to_pylist()
    for sid, d, ok in zip(t.column("security_id").to_pylist(),
                          t.column("trading_date").to_pylist(), filled):
        if sid.split(":")[0] not in venues:
            continue
        if securities is not None and sid not in securities:
            continue
        have.add((sid, d))
        if ok:
            done.add((sid, d))
    out = {}
    for sid, d in sorted(have - done):
        out.setdefault(sid, []).append(d)
    return out


class CumValueBackfill:
    """KIS로 다시 받아 누적거래대금이 있는 1분봉을 쓴다(여러 종목 동시)."""

    def __init__(self, store: ParquetStore = None, workers: int = 4,
                 source_factory=None, flush_every: int = 20, log=print):
        self.store = store or ParquetStore()
        self.workers = max(1, int(workers))
        self.flush_every = int(flush_every)
        self.log = log or (lambda msg: None)
        if source_factory is None:
            from .kis_minute import KisMinuteSource
            source_factory = KisMinuteSource
        self.source_factory = source_factory
        self._lock = threading.Lock()

    def run(self, targets: dict, run_id: str, dry_run: bool = False):
        started = _time.monotonic()
        codes = sorted(targets)
        summary = {"run_id": run_id, "securities": len(codes),
                   "pairs": sum(len(v) for v in targets.values()),
                   "written_rows": 0, "filled_pairs": 0, "failed": [],
                   "missing_pairs": 0, "api_calls": 0, "artifacts": []}
        if dry_run or not codes:
            return summary
        local = threading.local()
        buffer, chunk = [], [0]

        def _source():
            if not hasattr(local, "src"):
                local.src = self.source_factory()
                local.cache = MinuteHistoryCache(store=self.store, source=local.src)
            return local.src

        def _flush(force=False):
            with self._lock:
                if not buffer or (not force and len(buffer) < self.flush_every):
                    return
                rows = [r for part in buffer for r in part]
                buffer.clear()
                chunk[0] += 1
                part = f"cum{chunk[0]:04d}"
            if not rows:
                return
            rows.sort(key=lambda r: (r["security_id"], r["bar_start_at_utc"]))
            w = self.store.write(
                "silver", "minute_bars", rows, MINUTE_BAR_COLUMNS, MINUTE_BAR_PK,
                run_id, partition_fn=MinuteHistoryCache._partition,
                validators=[validate_quarantined_ohlc], part_key=part,
                extra_manifest={"source": "KIS_FHKST03010230",
                                "purpose": "cum_traded_value backfill",
                                "securities": sorted({r["security_id"] for r in rows})})
            with self._lock:
                summary["artifacts"].append(w.manifest_path)

        def _one(sid):
            src = _source()
            days = set(targets[sid])
            before = src.client.calls
            res = src.fetch_security(sid, period=1,
                                     min_date=min(days).strftime("%Y%m%d"))
            calls = src.client.calls - before
            if res.status == "FAILED":
                return sid, None, calls, len(days)
            rows = [r for r in local.cache.to_silver_rows(res, run_id, datetime.now(tz=UTC))
                    if r["trading_date"] in days]
            got = {r["trading_date"] for r in rows if r["session"] == "REGULAR"}
            return sid, rows, calls, len(days - got)

        done = 0
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = [pool.submit(_one, sid) for sid in codes]
            for fut in as_completed(futures):
                sid, rows, calls, missing = fut.result()
                done += 1
                with self._lock:
                    summary["api_calls"] += calls
                    if rows is None:
                        summary["failed"].append(sid)
                    else:
                        buffer.append(rows)
                        summary["written_rows"] += len(rows)
                        summary["filled_pairs"] += len(targets[sid]) - missing
                        summary["missing_pairs"] += missing
                _flush()
                if done % 25 == 0 or done == len(codes):
                    el = _time.monotonic() - started
                    self.log(f"  [{done}/{len(codes)}] 호출 {summary['api_calls']:,} · "
                             f"채운 쌍 {summary['filled_pairs']:,} · 실패 "
                             f"{len(summary['failed'])} · {el / 60:.1f}분 경과, 남은 예상 "
                             f"{el / done * (len(codes) - done) / 60:.0f}분")
        _flush(force=True)
        summary["elapsed_sec"] = round(_time.monotonic() - started, 1)
        return summary

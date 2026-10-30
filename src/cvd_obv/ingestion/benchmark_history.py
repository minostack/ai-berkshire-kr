# -*- coding: utf-8 -*-
"""벤치마크 지수 일봉 저장소 — `silver.benchmark_bars`(스키마 §5)."""

from __future__ import annotations

import time as _time
from datetime import date, datetime

from .. import SCHEMA_VERSION
from ..contracts.market_schema import (
    BENCHMARK_BAR_COLUMNS, BENCHMARK_BAR_PK, validate_quarantined_ohlc,
    validate_series_order,
)
from ..domain.errors import NoInputDataError
from ..domain.ids import UTC, to_utc
from ..domain.market_rules import default_registry
from ..domain.status import QualityStatus
from ..storage.parquet_store import ParquetStore
from .kiwoom_benchmark import (
    BENCHMARKS, KIWOOM_BENCHMARK_SOURCE, KiwoomBenchmarkSource,
)

__all__ = ["BenchmarkHistoryRepository"]


def _source_id(source) -> str:
    """행의 `source` 값 — 어댑터가 밝히지 않으면 키움(예전 기본)."""
    return getattr(source, "SOURCE", None) or KIWOOM_BENCHMARK_SOURCE


def _code_key(source) -> str:
    """BENCHMARKS에서 이 원천의 업종코드 키(키움 `inds_cd`, KIS `kis_cd`)."""
    return getattr(source, "CODE_KEY", None) or "inds_cd"


class BenchmarkHistoryRepository:
    LAYER = "silver"
    TABLE = "benchmark_bars"

    def __init__(self, store: ParquetStore = None,
                 source: KiwoomBenchmarkSource = None, registry=None,
                 market: str = "KRX", data_revision: int = 1):
        self.store = store or ParquetStore()
        self.source = source
        self.registry = registry or default_registry()
        self.market = market
        self.data_revision = int(data_revision)

    def to_silver_rows(self, benchmark_id, fetch_result, run_id):
        status = (QualityStatus.SUCCESS
                  if fetch_result.status == QualityStatus.SUCCESS
                  else QualityStatus.PARTIAL)
        rows = []
        for bar in fetch_result.rows:
            day = bar["trading_date"]
            rule = self.registry.resolve(self.market, day)
            rows.append({
                "benchmark_id": benchmark_id,
                "trading_date": day,
                "open": bar["open"], "high": bar["high"],
                "low": bar["low"], "close": bar["close"],
                "volume": bar["volume"],
                # 가격지수다. 배당 재투자 지수는 이 API가 주지 않는다.
                "total_return_close": None,
                "price_scope": "REGULAR",
                "available_at_utc": to_utc(rule.daily_confirmed_at(day)),
                "source": _source_id(self.source),
                "quality_status": status,
                "run_id": run_id,
                "market_rule_version": rule.version,
                "schema_version": SCHEMA_VERSION,
                "data_revision": self.data_revision,
            })
        return rows

    def build(self, benchmark_ids, run_id: str, start: date = None,
              end: date = None, dry_run: bool = False, on_progress=None):
        if self.source is None:
            from .market_source import benchmark_source
            self.source = benchmark_source()
        token = self.source.token()
        started = _time.monotonic()

        all_rows, per_benchmark = [], []
        for i, benchmark_id in enumerate(benchmark_ids, start=1):
            t0 = _time.monotonic()
            result = self.source.fetch_benchmark(
                benchmark_id, token=token, start=start, end=end)
            rows = (self.to_silver_rows(benchmark_id, result, run_id)
                    if result.status != QualityStatus.FAILED else [])
            all_rows.extend(rows)
            record = result.to_dict()
            record["benchmark_id"] = benchmark_id
            record["inds_cd"] = BENCHMARKS[benchmark_id][_code_key(self.source)]
            record["rows_written"] = len(rows)
            record["seconds"] = round(_time.monotonic() - t0, 2)
            per_benchmark.append(record)
            if on_progress:
                on_progress(i, len(benchmark_ids), record)

        write = None
        if all_rows and not dry_run:
            all_rows.sort(key=lambda r: (r["benchmark_id"], r["trading_date"]))
            write = self.store.write(
                self.LAYER, self.TABLE, all_rows, BENCHMARK_BAR_COLUMNS,
                BENCHMARK_BAR_PK, run_id,
                partition_fn=lambda r: [("benchmark_id",
                                         r["benchmark_id"].replace(":", "_")),
                                        ("year", r["trading_date"].year)],
                validators=[validate_quarantined_ohlc,
                            lambda rs: validate_series_order(
                                rs, key_field="benchmark_id",
                                table="silver.benchmark_bars")],
                extra_manifest={
                    "source": _source_id(self.source),
                    "index_codes": {b: BENCHMARKS[b][_code_key(self.source)]
                                    for b in benchmark_ids},
                    # 키움은 ×100 정수로 준다(되돌려 저장), KIS는 실제 값 그대로
                    "index_scale": 1 if _code_key(self.source) == "kis_cd" else 100,
                    "code_identification": (
                        "업종코드는 응답 값과 알려진 기준일 종가를 대조해 "
                        "실측으로 특정했다(모듈 docstring 참고)"),
                    "price_index_only": True,
                })

        return {
            "run_id": run_id,
            "benchmarks": per_benchmark,
            "rows": len(all_rows),
            "elapsed_sec": round(_time.monotonic() - started, 1),
            "artifacts": [write.manifest_path] if write else [],
            "status": (QualityStatus.SUCCESS
                       if all(b["status"] == QualityStatus.SUCCESS
                              for b in per_benchmark)
                       else QualityStatus.PARTIAL),
        }

    def load(self, benchmark_id: str = None):
        def _where(row):
            return benchmark_id is None or row["benchmark_id"] == benchmark_id

        rows = self.store.read(self.LAYER, self.TABLE, pk=BENCHMARK_BAR_PK,
                               where=_where)
        rows.sort(key=lambda r: (r["benchmark_id"], r["trading_date"]))
        return rows

    def series(self, benchmark_id: str):
        """`ForwardReturnBuilder`에 넘길 `BarSeries`."""
        from ..labels.forward_returns import BarSeries

        rows = self.load(benchmark_id)
        if not rows:
            raise NoInputDataError(f"{benchmark_id} 지수 일봉이 없다")
        return BarSeries(benchmark_id, rows)

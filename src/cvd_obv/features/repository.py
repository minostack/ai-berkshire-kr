# -*- coding: utf-8 -*-
"""일봉 feature 저장소 — `gold.daily_features`."""

from __future__ import annotations

import time as _time
from datetime import date

from ..contracts.feature_schema import (
    DAILY_FEATURE_COLUMNS, DAILY_FEATURE_PK, feature_version,
)
from ..bar_standard import read_standard_daily
from ..domain.errors import NoInputDataError
from ..domain.status import QualityStatus
from ..ingestion.daily_history import DailyHistoryRepository
from ..storage.parquet_store import ParquetStore
from .daily_features import DailyFeatureBuilder

__all__ = ["DailyFeatureRepository"]


class DailyFeatureRepository:
    LAYER = "gold"
    TABLE = "daily_features"

    def __init__(self, store: ParquetStore = None,
                 daily_repo: DailyHistoryRepository = None,
                 obv_lookback: int = 20, data_revision: int = 1):
        self.store = store or ParquetStore()
        self.daily_repo = daily_repo or DailyHistoryRepository(store=self.store)
        self.obv_lookback = int(obv_lookback)
        self.data_revision = int(data_revision)
        self.feature_version = feature_version(self.obv_lookback)

    @staticmethod
    def _partition(row):
        return [("feature_version", row["feature_version"]),
                ("year", row["trading_date"].year)]

    def _load_series(self, securities=None):
        """일봉을 Arrow로 읽어 종목별로 자른다.

        품질 FAILED 행은 제외한다. 그러면 그 날짜의 feature가 아예 만들어지지
        않는데, 잘못된 가격으로 MA와 OBV를 오염시키는 것보다 낫다.
        빠진 날짜가 있으면 이후 rolling 창이 한 칸씩 당겨지므로, 제외한 행 수를
        요약에 남겨 영향 범위를 볼 수 있게 한다.
        """
        import pyarrow as pa
        import pyarrow.compute as pc

        # 봉 기준 bars-1.0: 정규장·수정주가 일봉, 하루 한 행(같은 날 EXTENDED
        # 원본과 정규장 복원본이 함께 있어도 복원본만). 리비전은 최신 하나.
        table = read_standard_daily(
            self.store,
            columns=["security_id", "trading_date", "close", "volume",
                     "quality_status"])
        if table.num_rows == 0:
            raise NoInputDataError("일봉이 없다 — `daily-cache build` 먼저")

        excluded = pc.sum(pc.cast(
            pc.equal(table.column("quality_status"), "FAILED"), "int64")
        ).as_py() or 0
        table = table.filter(
            pc.not_equal(table.column("quality_status"), "FAILED"))
        if securities:
            table = table.filter(
                pc.is_in(table.column("security_id"),
                         value_set=pa.array(list(securities))))
            if table.num_rows == 0:
                raise NoInputDataError(f"요청한 종목이 일봉에 없다: {securities}")

        table = table.sort_by([("security_id", "ascending"),
                               ("trading_date", "ascending")])
        sids = table.column("security_id").to_pylist()
        days = table.column("trading_date").to_pylist()
        closes = table.column("close").to_pylist()
        volumes = table.column("volume").to_pylist()

        grouped, start = {}, 0
        for i in range(1, len(sids) + 1):
            if i < len(sids) and sids[i] == sids[start]:
                continue
            grouped[sids[start]] = [
                {"trading_date": days[j], "close": float(closes[j]),
                 "volume": int(volumes[j])}
                for j in range(start, i)]
            start = i
        return grouped, excluded

    def build(self, run_id: str, start: date = None, end: date = None,
              securities=None, flush_every: int = 20, dry_run: bool = False,
              on_progress=None):
        grouped, excluded = self._load_series(securities)
        builder = DailyFeatureBuilder(obv_lookback=self.obv_lookback,
                                      data_revision=self.data_revision)

        buffer, writes, status_counts = [], [], {}
        gate_counts = {"cond_price": 0, "cond_obv": 0, "cond_liquidity": 0,
                       "gate_all": 0, "evaluable": 0}
        total_rows = 0
        chunk = 0
        items = sorted(grouped.items())

        def _flush():
            nonlocal buffer, chunk
            if dry_run or not buffer:
                buffer = []
                return
            chunk += 1
            buffer.sort(key=lambda r: (r["security_id"], r["trading_date"]))
            writes.append(self.store.write(
                self.LAYER, self.TABLE, buffer, DAILY_FEATURE_COLUMNS,
                DAILY_FEATURE_PK, run_id, partition_fn=self._partition,
                part_key=f"c{chunk:03d}",
                extra_manifest={
                    "feature_version": self.feature_version,
                    "obv_lookback": self.obv_lookback,
                    "source_table": "silver.daily_bars",
                    "excluded_failed_bars": excluded,
                    "equivalence": (
                        "V1.5 calc_obv/calc_price_trend/calc_liquidity와 동일. "
                        "tests/regression/test_daily_feature_equivalence.py"),
                    "securities": sorted({r["security_id"] for r in buffer}),
                }))
            buffer = []

        for i, (sid, bars) in enumerate(items, start=1):
            rows = builder.build_security(sid, bars, run_id, start=start,
                                          end=end)
            for row in rows:
                status_counts[row["feature_status"]] = \
                    status_counts.get(row["feature_status"], 0) + 1
                if row["gate_all"] is None:
                    continue
                gate_counts["evaluable"] += 1
                for key in ("cond_price", "cond_obv", "cond_liquidity",
                            "gate_all"):
                    if row[key]:
                        gate_counts[key] += 1
            buffer.extend(rows)
            total_rows += len(rows)
            if on_progress:
                on_progress(i, len(items), sid, len(rows))
            if flush_every and i % flush_every == 0:
                _flush()
        _flush()

        return {
            "run_id": run_id,
            "feature_version": self.feature_version,
            "obv_lookback": self.obv_lookback,
            "rows": total_rows,
            "securities": len(items),
            "excluded_failed_bars": excluded,
            "status_counts": status_counts,
            "gate_counts": gate_counts,
            "gate_rates": {
                key: (round(gate_counts[key] / gate_counts["evaluable"], 4)
                      if gate_counts["evaluable"] else None)
                for key in ("cond_price", "cond_obv", "cond_liquidity",
                            "gate_all")
            },
            "artifacts": [w.manifest_path for w in writes],
            "status": (QualityStatus.SUCCESS if total_rows else
                       QualityStatus.FAILED),
        }

    def load(self, security_id: str = None, feature_version_: str = None):
        target = feature_version_ or self.feature_version

        def _where(row):
            if row["feature_version"] != target:
                return False
            return security_id is None or row["security_id"] == security_id

        return self.store.read(self.LAYER, self.TABLE, pk=DAILY_FEATURE_PK,
                               where=_where)

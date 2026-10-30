# -*- coding: utf-8 -*-
"""Forward return 패널 저장소.

일봉을 종목 단위로 읽어 label을 만들고 청크로 내려쓴다. 199종목 27년 × 5개
horizon이면 약 680만 행이라, 전부 파이썬 dict로 들고 있으면 메모리가 감당되지
않는다. 수집 계층과 같은 방식으로 나눠 쓴다.
"""

from __future__ import annotations

from datetime import date

from ..bar_standard import read_standard_daily
from ..contracts.label_schema import (
    DAILY_FORWARD_RETURN_COLUMNS, DAILY_FORWARD_RETURN_PK, DEFAULT_HORIZONS,
    LABEL_VERSION,
)
from ..domain.calendar import TradingCalendar
from ..domain.errors import NoInputDataError
from ..domain.status import LabelStatus
from ..ingestion.daily_history import DailyHistoryRepository
from ..storage.parquet_store import ParquetStore
from .costs import CostModel
from .forward_returns import BarSeries, ForwardReturnBuilder

__all__ = ["DailyForwardReturnRepository", "LabelBuildSummary"]


class LabelBuildSummary:
    """label 생성 결과 요약."""

    __slots__ = ("run_id", "rows", "securities", "writes", "status_counts",
                 "horizons", "label_version", "cost_model_version",
                 "calendar_days", "start", "end", "quarantined_bars")

    def __init__(self, run_id, rows, securities, writes, status_counts,
                 horizons, label_version, cost_model_version, calendar_days,
                 start, end, quarantined_bars=0):
        self.run_id = run_id
        self.rows = rows
        self.securities = securities
        self.writes = writes
        self.status_counts = status_counts
        self.horizons = horizons
        self.label_version = label_version
        self.cost_model_version = cost_model_version
        self.calendar_days = calendar_days
        self.start = start
        self.end = end
        self.quarantined_bars = int(quarantined_bars)

    def to_dict(self):
        return {
            "run_id": self.run_id,
            "rows": self.rows,
            "securities": self.securities,
            "horizons": list(self.horizons),
            "label_version": self.label_version,
            "cost_model_version": self.cost_model_version,
            "calendar_days": self.calendar_days,
            "quarantined_bars_excluded": self.quarantined_bars,
            "status_counts": dict(self.status_counts),
            "artifacts": [w.manifest_path for w in self.writes],
        }


class DailyForwardReturnRepository:
    """`gold.daily_forward_returns` 생성·저장·조회."""

    LAYER = "gold"
    TABLE = "daily_forward_returns"

    def __init__(self, store: ParquetStore = None,
                 daily_repo: DailyHistoryRepository = None,
                 cost_model: CostModel = None,
                 horizons=DEFAULT_HORIZONS,
                 label_version: str = LABEL_VERSION,
                 data_revision: int = 1, entry_rule: str = "NEXT_OPEN",
                 ):
        from ..contracts.label_schema import label_version_for

        self.entry_rule = entry_rule
        # 버전 문자열이 진입 규칙을 담고 있다. 규칙만 바꾸고 버전을 그대로
        # 두면 이름과 내용이 어긋난 행이 남는다.
        if label_version == LABEL_VERSION:
            label_version = label_version_for(entry_rule)
        self.store = store or ParquetStore()
        self.daily_repo = daily_repo or DailyHistoryRepository(store=self.store)
        self.cost_model = cost_model or CostModel.from_config()
        self.horizons = tuple(horizons)
        self.label_version = label_version
        self.data_revision = int(data_revision)

    @staticmethod
    def _partition(row):
        d = row["signal_date"]
        return [("horizon_td", row["horizon_td"]), ("signal_year", d.year)]

    def _load_series(self, securities=None):
        """일봉을 Arrow로 읽어 종목별 `BarSeries`로 나눈다.

        `DailyHistoryRepository.load()`는 행마다 파이썬 dict를 만들어서 199종목
        27년이면 수 GB가 된다. 필요한 6개 열만 Arrow로 받고 종목 단위로 자른다.

        품질이 FAILED인 행(예: 공급자 OHLC 오류로 격리된 행)은 제외한다.
        제외하면 그 구간 label이 ENTRY/EXIT_UNAVAILABLE이 되는데, 그게
        "값을 모른다"는 사실을 그대로 전달하는 방식이다.
        """
        import pyarrow as pa
        import pyarrow.compute as pc

        # 봉 기준 bars-1.0: 정규장·수정주가 일봉, 하루 한 행. 진입·청산 가격이
        # 애프터마켓 최종가가 되지 않게 한다. 리비전은 최신 하나.
        table = read_standard_daily(
            self.store,
            columns=["security_id", "trading_date", "open", "high", "low",
                     "close", "quality_status"])
        if table.num_rows == 0:
            raise NoInputDataError("일봉이 없다 — `daily-cache build` 먼저")

        quarantined = pc.sum(
            pc.cast(pc.equal(table.column("quality_status"), "FAILED"),
                    "int64")).as_py() or 0
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
        all_days = table.column("trading_date").to_pylist()
        calendar = TradingCalendar("KRX", set(all_days))

        series_map, start_idx = {}, 0
        for i in range(1, len(sids) + 1):
            if i < len(sids) and sids[i] == sids[start_idx]:
                continue
            block = table.slice(start_idx, i - start_idx)
            series_map[sids[start_idx]] = BarSeries(
                sids[start_idx], block.select(
                    ["trading_date", "open", "high", "low", "close"]
                ).to_pylist())
            start_idx = i
        calendar.derived_from_securities = len(series_map)
        return series_map, calendar, quarantined

    def build(self, run_id: str, start: date = None, end: date = None,
              securities=None, flush_every: int = 10, dry_run: bool = False,
              on_progress=None, benchmark_series=None, benchmark_id=None):
        """저장된 일봉 전체에서 forward return 패널을 만든다."""
        series_map, calendar, quarantined = self._load_series(securities)

        builder = ForwardReturnBuilder(
            calendar, cost_model=self.cost_model, horizons=self.horizons,
            label_version=self.label_version, data_revision=self.data_revision,
            entry_rule=self.entry_rule)

        buffer, writes, status_counts = [], [], {}
        total_rows = 0
        chunk = 0
        items = sorted(series_map.items())

        def _flush():
            nonlocal buffer, chunk
            if dry_run or not buffer:
                buffer = []
                return
            chunk += 1
            buffer.sort(key=lambda r: (r["horizon_td"], r["security_id"],
                                       r["signal_date"]))
            writes.append(self.store.write(
                self.LAYER, self.TABLE, buffer, DAILY_FORWARD_RETURN_COLUMNS,
                DAILY_FORWARD_RETURN_PK, run_id,
                partition_fn=self._partition, part_key=f"c{chunk:03d}",
                extra_manifest={
                    "label_version": self.label_version,
                    "cost_model_version": self.cost_model.version,
                    "cost_model": self.cost_model.to_dict(),
                    "horizons": list(self.horizons),
                    "entry_rule": "NEXT_OPEN",
                    "calendar_source": calendar.source,
                    "calendar_days": len(calendar),
                    "securities": sorted({r["security_id"] for r in buffer}),
                }))
            buffer = []

        for i, (sid, series) in enumerate(items, start=1):
            rows = builder.build_security(
                series, run_id, benchmark=benchmark_series,
                benchmark_id=benchmark_id, start=start, end=end)
            for row in rows:
                status_counts[row["label_status"]] = \
                    status_counts.get(row["label_status"], 0) + 1
            buffer.extend(rows)
            total_rows += len(rows)
            if on_progress:
                on_progress(i, len(items), sid, len(rows))
            if flush_every and i % flush_every == 0:
                _flush()
        _flush()

        return LabelBuildSummary(
            run_id, total_rows, len(items), writes, status_counts,
            self.horizons, self.label_version, self.cost_model.version,
            len(calendar), start, end, quarantined)

    def load(self, horizon: int = None, security_id: str = None):
        def _where(row):
            if horizon is not None and row["horizon_td"] != horizon:
                return False
            if security_id and row["security_id"] != security_id:
                return False
            return True

        return self.store.read(self.LAYER, self.TABLE,
                               pk=DAILY_FORWARD_RETURN_PK, where=_where)

    def coverage(self):
        """label 상태 분포 — 성숙하지 않은 구간이 얼마나 되는지 본다."""
        table = self.store.read_arrow(
            self.LAYER, self.TABLE,
            columns=["horizon_td", "label_status", "signal_date"])
        statuses = table.column("label_status").to_pylist()
        horizons = table.column("horizon_td").to_pylist()
        out = {}
        for h, s in zip(horizons, statuses):
            out.setdefault(h, {}).setdefault(s, 0)
            out[h][s] += 1
        dates = table.column("signal_date").to_pylist()
        return {
            "rows": table.num_rows,
            "by_horizon": out,
            "date_range": [min(dates).isoformat(), max(dates).isoformat()],
            "complete_ratio": round(
                sum(1 for s in statuses if s == LabelStatus.COMPLETE)
                / max(1, len(statuses)), 4),
        }

# -*- coding: utf-8 -*-
"""`silver.market_calendar` 생성·저장 — 스키마 §5.

거래 캘린더는 수집한 일봉의 거래일 합집합에서 만든다. KRX 공식 휴장일 자료를
쓰지 않으므로 그 한계를 행마다 `source`로 남긴다.

캘린더를 파일로 굳혀 두는 이유는 두 가지다. 첫째, horizon 계산이 매번 전체
일봉을 읽지 않아도 된다. 둘째, 나중에 일봉이 늘어나면 캘린더도 달라지는데,
어떤 캘린더로 계산한 결과인지 남아 있어야 재현할 수 있다.
"""

from __future__ import annotations

from ..contracts.market_schema import (
    DAILY_BAR_PK, MARKET_CALENDAR_COLUMNS, MARKET_CALENDAR_PK,
)
from ..domain.calendar import TradingCalendar
from ..domain.errors import NoInputDataError
from ..storage.parquet_store import ParquetStore
from .daily_history import DailyHistoryRepository

__all__ = ["MarketCalendarRepository"]


class MarketCalendarRepository:
    LAYER = "silver"
    TABLE = "market_calendar"

    def __init__(self, store: ParquetStore = None,
                 daily_repo: DailyHistoryRepository = None,
                 market: str = "KRX"):
        self.store = store or ParquetStore()
        self.daily_repo = daily_repo or DailyHistoryRepository(store=self.store)
        self.market = market

    def derive(self) -> TradingCalendar:
        """저장된 일봉에서 캘린더를 만든다(파일로 쓰지는 않는다)."""
        table = self.store.read_arrow(
            self.daily_repo.LAYER, self.daily_repo.TABLE,
            # (날짜, 종목) 집합만 본다 — 리비전과 무관하므로 저장 PK로 충분하다.
            columns=["trading_date", "security_id"], dedupe_pk=DAILY_BAR_PK)
        if table.num_rows == 0:
            raise NoInputDataError("일봉이 없다 — `daily-cache build` 먼저")
        rows = [{"trading_date": d, "security_id": s}
                for d, s in zip(table.column("trading_date").to_pylist(),
                                table.column("security_id").to_pylist())]
        return TradingCalendar.from_daily_bars(rows, market=self.market)

    def build(self, run_id: str, calendar: TradingCalendar = None,
              dry_run: bool = False):
        calendar = calendar or self.derive()
        rows = calendar.to_rows()
        summary = {
            "market": self.market,
            "trading_days": len(rows),
            "date_range": [calendar.start.isoformat(), calendar.end.isoformat()],
            "source": calendar.source,
            "derived_from_securities": calendar.derived_from_securities,
            # 근거가 얇은 날은 "휴장"과 "그 종목들이 안 움직였다"를 구별할 수
            # 없다. 지우지 않고 세어 보고한다.
            "thin_days": [
                {"date": d.isoformat(), "securities": calendar.securities_on(d)}
                for d in calendar.thin_days(30)],
            "market_rule_versions": sorted({r["market_rule_version"]
                                            for r in rows}),
            "price_scopes": sorted({r["daily_price_scope"] for r in rows}),
            "large_gaps": [
                {"from": g["from"].isoformat(), "to": g["to"].isoformat(),
                 "calendar_days": g["calendar_days"]}
                for g in calendar.gaps(max_gap_days=7)
            ],
        }
        if dry_run:
            summary["artifacts"] = []
            return summary

        write = self.store.write(
            self.LAYER, self.TABLE, rows, MARKET_CALENDAR_COLUMNS,
            MARKET_CALENDAR_PK, run_id,
            partition_fn=lambda r: [("market", r["market"]),
                                    ("year", r["trading_date"].year)],
            extra_manifest={
                "source": calendar.source,
                "derived_from_securities": calendar.derived_from_securities,
                "market_rule_versions": summary["market_rule_versions"],
            })
        summary["artifacts"] = [write.manifest_path]
        return summary

    def load(self):
        return self.store.read(self.LAYER, self.TABLE,
                               pk=MARKET_CALENDAR_PK)

    def load_calendar(self) -> TradingCalendar:
        """저장된 캘린더를 다시 `TradingCalendar`로."""
        rows = self.load()
        if not rows:
            raise NoInputDataError("저장된 캘린더가 없다")
        first = rows[0]
        # 하루별 근거 종목 수를 되살린다. 첫 행 값 하나로 덮으면 1996년의
        # 12종목짜리 날과 2026년의 546종목짜리 날이 같아 보인다.
        per_day = {r["trading_date"]: r["derived_from_securities"]
                   for r in rows if r.get("derived_from_securities")}
        return TradingCalendar(
            first["market"], [r["trading_date"] for r in rows],
            source=first["source"],
            derived_from_securities=max(per_day.values(), default=0),
            per_day_securities=per_day)

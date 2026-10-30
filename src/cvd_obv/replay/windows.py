# -*- coding: utf-8 -*-
"""replay용 봉 공급기 — 한 번 읽고, 필요한 구간만 잘라 준다.

`MinuteHistoryCache.slice_as_of()`는 호출할 때마다 테이블 전체를 읽는다. 한
종목·한 날짜를 볼 때는 괜찮지만, 257거래일 × 200종목이면 5만 번이 되어 끝나지
않는다. 그래서 replay 전용으로 **한 번 읽어 정렬해 두고 구간만 잘라 주는**
공급기를 따로 둔다.

잘라 주는 규칙은 `slice_as_of`와 같아야 한다. 특히 **그날 봉은 `bar_end <=
as_of`인 것만** 넘긴다. 이 규칙이 replay의 전부라고 해도 된다 — 여기가 뚫리면
15:35 신호가 15:40 봉을 보게 된다.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import date, datetime, timedelta

from ..contracts.market_schema import MINUTE_BAR_LOGICAL_KEY
from ..bar_standard import read_standard_daily
from ..domain.errors import NoInputDataError
from ..domain.ids import KST, normalize_security_id, to_utc
from ..domain.status import QualityStatus

__all__ = ["MinuteWindow", "DailyWindow"]

#: 분봉 공급기가 들고 있는 열 — 원본과 합성본이 같은 모양이 되게 맞춘다
_MINUTE_COLUMNS = ["security_id", "trading_date", "session", "bar_start_at_utc",
                   "bar_end_at_utc", "open", "high", "low", "close", "volume"]


class MinuteWindow:
    """거래일별 분봉. 최근 N거래일만 들고 있는다.

    봉 기준 bars-1.0: 분봉 원천은 1분봉 하나다. `period`가 1이 아니면 1분봉을
    합성한다(`bar_standard.resample_minute_table`, 원본 5분봉 대비 99.99% 일치).
    원본 5분봉은 2026-09-22에 수집을 멈췄으므로, 그 뒤 날짜를 replay하려면
    합성 말고는 방법이 없다.

    `minute_source="native"`는 저장된 원본 N분봉을 그대로 읽는다 — 2026-10-03
    이전에 만든 replay 결과를 바이트 단위로 재현할 때만 쓴다.
    """

    SOURCES = ("standard", "native")

    def __init__(self, store, securities, start: date, end: date,
                 period: int = 5, keep_days: int = 8,
                 minute_source: str = "standard"):
        import pyarrow as pa
        import pyarrow.compute as pc
        import pyarrow.dataset as ds

        from ..bar_standard import STORED_MINUTE_PERIOD, resample_minute_table
        from ..domain.errors import ConfigError

        if minute_source not in self.SOURCES:
            raise ConfigError(f"minute_source는 {self.SOURCES} 중 하나: "
                              f"{minute_source}")
        self.period = int(period)
        self.keep_days = int(keep_days)
        self.minute_source = minute_source
        self._cache = {}                 # trading_date -> {code: [bar…]}

        wanted = {normalize_security_id(s) for s in securities}
        read_period = (self.period if minute_source == "native"
                       else STORED_MINUTE_PERIOD)
        # 앞쪽 lookback(최근 N거래일)을 덮는 캘린더일 여유. 이전에는 날짜를
        # 거르지 않고 전 구간을 읽었다 — 1분봉이면 수천만 행이 된다.
        lower = start - timedelta(days=int(self.keep_days * 7 / 5) + 15)
        # 주기·종목·날짜를 **읽는 시점에** 걸러내고, 1분봉은 한 달씩 읽어 바로
        # 합성한다. 전 구간 1분봉을 한 번에 올리면 메모리가 감당하지 못한다.
        chunks = []
        cursor = lower.replace(day=1)
        while cursor <= end:
            nxt = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
            lo, hi = max(cursor, lower), min(nxt - timedelta(days=1), end)
            try:
                part = store.read_arrow(
                    "silver", "minute_bars",
                    columns=["security_id", "period_min", "trading_date",
                             "session", "bar_start_at_utc", "bar_end_at_utc",
                             "open", "high", "low", "close", "volume",
                             "quality_status"],
                    filters=(ds.field("period_min") == read_period)
                    & ds.field("security_id").isin(sorted(wanted))
                    & (ds.field("trading_date") >= lo)
                    & (ds.field("trading_date") <= hi),
                    latest_by=MINUTE_BAR_LOGICAL_KEY)  # 같은 봉은 최신 리비전만
            except NoInputDataError:
                part = None
            cursor = nxt
            if part is None or part.num_rows == 0:
                continue
            mask = pc.and_(
                pc.equal(part.column("period_min"), read_period),
                pc.is_in(part.column("security_id"),
                         value_set=_arrow_set(wanted)))
            mask = pc.and_(mask, pc.not_equal(part.column("quality_status"),
                                              QualityStatus.FAILED))
            # 저장소 필터를 믿지 않고 한 번 더 자른다 — 청크가 겹치면 같은 봉이
            # 두 번 들어간다.
            days = part.column("trading_date")
            mask = pc.and_(mask, pc.and_(
                pc.greater_equal(days, _arrow_date(lo)),
                pc.less_equal(days, _arrow_date(hi))))
            part = part.filter(mask)
            if read_period != self.period:
                part = resample_minute_table(part, self.period)
            chunks.append(part.select(_MINUTE_COLUMNS))
        if not chunks:
            raise NoInputDataError(
                f"{lower}~{end} 분봉이 없다(period={read_period}, "
                f"source={minute_source})")
        table = pa.concat_tables(
            [c.cast(chunks[0].schema) for c in chunks])
        table = table.sort_by([("trading_date", "ascending"),
                               ("security_id", "ascending"),
                               ("bar_start_at_utc", "ascending")])
        self._table = table
        self._days, self._bounds = _day_bounds(
            table.column("trading_date").to_pylist())
        self._days_in_range = [d for d in self._days if start <= d <= end]

    @property
    def trading_days(self):
        return list(self._days_in_range)

    def _materialize(self, day):
        """그날 분봉을 종목별 V1.5 형태로 만든다(하루에 한 번만)."""
        if day in self._cache:
            return self._cache[day]
        index = bisect_right(self._days, day) - 1
        if index < 0 or self._days[index] != day:
            self._cache[day] = {}
            return self._cache[day]
        offset, length = self._bounds[index]
        rows = self._table.slice(offset, length).to_pylist()
        by_code = {}
        for row in rows:
            start_kst = row["bar_start_at_utc"].astimezone(KST)
            end_kst = row["bar_end_at_utc"].astimezone(KST)
            by_code.setdefault(row["security_id"].split(":")[-1], []).append({
                "dt": start_kst.strftime("%Y%m%d%H%M%S"),
                "open": float(row["open"]), "high": float(row["high"]),
                "low": float(row["low"]), "close": float(row["close"]),
                "volume": int(row["volume"]),
                "bar_start": start_kst.replace(tzinfo=None),
                "bar_end": end_kst.replace(tzinfo=None),
                "session": row["session"],
                "_end_utc": row["bar_end_at_utc"],
            })
        self._cache[day] = by_code
        if len(self._cache) > self.keep_days:
            for old in sorted(self._cache)[:-self.keep_days]:
                del self._cache[old]
        return by_code

    def slice_as_of(self, code, as_of: datetime, lookback_trading_days: int = 5):
        """`as_of`까지 알 수 있었던 분봉. 최근 N거래일치만."""
        cutoff = to_utc(as_of if as_of.tzinfo else as_of.replace(tzinfo=KST))
        today = cutoff.astimezone(KST).date()
        index = bisect_right(self._days, today) - 1
        if index < 0:
            return []
        first = max(0, index - int(lookback_trading_days) + 1)
        out = []
        for day in self._days[first:index + 1]:
            bars = self._materialize(day).get(str(code), ())
            if day == today:
                # 그날 봉은 **끝난 것만** 본다. 이 한 줄이 불변식 2다.
                bars = [b for b in bars if b["_end_utc"] <= cutoff]
            out.extend(bars)
        return [{k: v for k, v in bar.items() if k != "_end_utc"}
                for bar in out]


class DailyWindow:
    """종목별 일봉. 워밍업 구간까지 한 번에 읽어 둔다."""

    def __init__(self, store, securities, start: date, end: date,
                 warmup_days: int = 260):
        import pyarrow.compute as pc

        wanted = {normalize_security_id(s) for s in securities}
        # 봉 기준 bars-1.0: 정규장·수정주가 일봉, 하루 한 행. 리비전은 최신 하나.
        table = read_standard_daily(
            store,
            columns=["security_id", "trading_date", "open", "high", "low",
                     "close", "volume", "quality_status"])
        lower = start - timedelta(days=int(warmup_days))
        mask = pc.and_(
            pc.is_in(table.column("security_id"), value_set=_arrow_set(wanted)),
            pc.not_equal(table.column("quality_status"), QualityStatus.FAILED))
        mask = pc.and_(mask, pc.greater_equal(
            table.column("trading_date"), _arrow_date(lower)))
        mask = pc.and_(mask, pc.less_equal(
            table.column("trading_date"), _arrow_date(end)))
        table = table.filter(mask).sort_by([("security_id", "ascending"),
                                            ("trading_date", "ascending")])

        self._bars, self._dates = {}, {}
        for row in table.to_pylist():
            code = row["security_id"].split(":")[-1]
            self._bars.setdefault(code, []).append({
                "dt": row["trading_date"].strftime("%Y%m%d"),
                "close": float(row["close"]), "volume": int(row["volume"]),
                "open": float(row["open"]), "high": float(row["high"]),
                "low": float(row["low"]),
            })
            self._dates.setdefault(code, []).append(row["trading_date"])

    def slice_as_of(self, code, as_of: datetime, days: int = 220):
        """`as_of` 당일까지의 일봉. 미완성 일봉 판정은 V1.5가 한다."""
        code = str(code)
        dates = self._dates.get(code)
        if not dates:
            return []
        local = as_of.astimezone(KST) if as_of.tzinfo else as_of
        today = local.date()
        end = bisect_right(dates, today)
        lower = today - timedelta(days=int(days))
        first = bisect_right(dates, lower)
        return list(self._bars[code][first:end])


def _arrow_set(values):
    import pyarrow as pa

    return pa.array(sorted(values), type=pa.string())


def _arrow_date(value: date):
    import pyarrow as pa

    return pa.scalar(value, type=pa.date32())


def _day_bounds(days):
    """정렬된 날짜 열 → (고유 날짜, (offset, length)) 목록."""
    unique, bounds = [], []
    start = 0
    for i in range(1, len(days) + 1):
        if i == len(days) or days[i] != days[start]:
            unique.append(days[start])
            bounds.append((start, i - start))
            start = i
    return unique, bounds

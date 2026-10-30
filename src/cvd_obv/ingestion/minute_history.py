# -*- coding: utf-8 -*-
"""분봉 cache — 아키텍처 §6 `minute_history_cache`, 로드맵 Stage 4.

`ka10080`에는 날짜 파라미터가 없다. 그래서 "주는 데까지 한 번에 받아 두고,
과거 시점은 offline에서 잘라 쓴다". 그 잘라 쓰기가 `slice_as_of()`이고,
Stage 5 replay가 API를 다시 부르지 않는 근거다.

`slice_as_of`가 지키는 것
-------------------------
**`bar_end_at <= as_of`만 돌려준다.** 15:35 신호를 만들 때 15:40에 끝나는 봉이
섞이면 그 결과는 미래를 본 것이다. 이 한 줄이 아키텍처 §10 불변식 2다.

실측(2026-09-18, 삼성전자 5분봉)
--------------------------------
20,175봉 / 257거래일 / 23페이지 / 6.2초. 구간은 2025-09-01 ~ 2026-09-18.
하루 봉 수는 중앙값 78(정규장 77 + 시간외종가 1)이고, 애프터마켓이 시행된
2026-09-14부터는 126(+48)이다. 로드맵이 전제한 "약 255거래일"과 일치한다.
"""

from __future__ import annotations

import time as _time
from datetime import date, datetime

from .. import SCHEMA_VERSION
from ..contracts.market_schema import (
    BAR_COVERAGE_COLUMNS, BAR_COVERAGE_PK, MINUTE_BAR_COLUMNS,
    MINUTE_BAR_LOGICAL_KEY, MINUTE_BAR_PK,
    OHLC_VIOLATION_FLAG, ohlc_violations_of, validate_quarantined_ohlc,
)
from ..domain.errors import ConfigError, NoInputDataError
from ..domain.ids import KST, UTC, normalize_security_id, to_utc
from ..domain.market_rules import default_registry
from ..domain.status import QualityStatus, Session
from ..storage.parquet_store import ParquetStore
from .kiwoom_minute import (
    KIWOOM_MINUTE_SOURCE, TIMESTAMP_SEMANTICS, KiwoomMinuteSource,
)

__all__ = ["MinuteHistoryCache", "month_dataset"]


def month_dataset(store, start, end, period: int = 1):
    """`[start, end]` 달의 `silver.minute_bars` 파티션만 하나의 pyarrow dataset으로 연다.

    파일이 수만 개라(2026-10: 약 1.9만) `read_arrow`처럼 전부 열면 약 2분이 걸린다.
    그 달 파티션만, 계약 스키마로 묶어 열면 몇 초다. 나중에 추가된 열
    (`cum_traded_value`)이 없는 옛 파일은 그 열이 null로 읽힌다. 파일이 없으면 None.
    """
    import glob
    import os
    import pyarrow.dataset as ds
    from ..storage.parquet_store import arrow_schema

    base = store.table_dir("silver", "minute_bars")
    files = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        files += glob.glob(os.path.join(base, f"period_min={int(period)}", f"year={y}",
                                        f"month={m:02d}", "**", "*.parquet"),
                           recursive=True)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    if not files:
        return None
    return ds.dataset(files, format="parquet", partitioning=None,
                      schema=arrow_schema(MINUTE_BAR_COLUMNS))


def _source_id(source) -> str:
    """행의 `source` 값 — 어댑터가 밝히지 않으면 키움(예전 기본)."""
    return getattr(source, "SOURCE", None) or KIWOOM_MINUTE_SOURCE


def _semantics(source) -> str:
    return getattr(source, "TIMESTAMP_SEMANTICS", None) or TIMESTAMP_SEMANTICS


def _bucket_of(security_id: str, buckets: int = 32) -> int:
    """종목 해시 mod 32 — 스키마 §5가 지정한 partition 키."""
    total = 0
    for ch in security_id.encode("utf-8"):
        total = (total * 131 + ch) & 0xFFFFFFFF
    return total % buckets


class MinuteHistoryCache:
    """`silver.minute_bars` 수집·저장·조회."""

    LAYER = "silver"
    TABLE = "minute_bars"
    COVERAGE_TABLE = "bar_coverage"

    def __init__(self, store: ParquetStore = None,
                 source: KiwoomMinuteSource = None, registry=None,
                 market: str = "KRX", data_revision: int = 1):
        self.store = store or ParquetStore()
        self.source = source
        self.registry = registry or default_registry()
        self.market = market
        self.data_revision = int(data_revision)

    @staticmethod
    def _partition(row):
        d = row["trading_date"]
        return [("period_min", row["period_min"]), ("year", d.year),
                ("month", f"{d.month:02d}"),
                ("security_bucket", f"{_bucket_of(row['security_id']):02d}")]

    # ── 정규화 ─────────────────────────────────────────────

    def to_silver_rows(self, fetch_result, run_id: str, fetched_at: datetime):
        security_id = normalize_security_id(fetch_result.security_code)
        fetched_utc = to_utc(fetched_at)
        base_status = (QualityStatus.SUCCESS
                       if fetch_result.status == QualityStatus.SUCCESS
                       else QualityStatus.PARTIAL)
        fetch_flags = set(fetch_result.quality_flags)
        rows = []
        for bar in fetch_result.bars:
            day = bar["trading_date"]
            rule = self.registry.resolve(self.market, day)
            flags = set(fetch_flags) | set(bar.get("quality_flags") or ())
            status = base_status
            if ohlc_violations_of(bar):
                flags.add(OHLC_VIOLATION_FLAG)
                status = QualityStatus.FAILED
            rows.append({
                "security_id": security_id,
                "period_min": fetch_result.period_min,
                # 봉 시각은 naive KST로 들어온다(V1.5 규약). 저장은 UTC.
                "bar_start_at_utc": to_utc(bar["bar_start"], assume_tz=KST),
                "bar_end_at_utc": to_utc(bar["bar_end"], assume_tz=KST),
                "trading_date": day,
                "session": bar["session"],
                "open": bar["open"], "high": bar["high"],
                "low": bar["low"], "close": bar["close"],
                "volume": bar["volume"],
                "traded_value": bar["traded_value"],
                # KIS만 준다(그날 누적 거래대금, 원). 키움 봉은 비어 있다
                "cum_traded_value": bar.get("cum_traded_value"),
                "source_timestamp_raw": bar["source_timestamp_raw"],
                "timestamp_semantics": _semantics(self.source),
                # 봉은 끝나야 알 수 있다 — 가용 시각은 봉 종료 시각이다.
                "available_at_utc": to_utc(bar["bar_end"], assume_tz=KST),
                "source": _source_id(self.source),
                "quality_status": status,
                "quality_flags": sorted(flags),
                "run_id": run_id,
                "market_rule_version": rule.version,
                "schema_version": SCHEMA_VERSION,
                "data_revision": self.data_revision,
            })
        return rows

    def to_coverage_rows(self, fetch_result, run_id: str):
        """거래일·세션별 기대 봉 수 대비 실제 봉 수(스키마 §5 `bar_coverage`)."""
        if self.source is None:
            from .market_source import minute_source
            self.source = minute_source()
        sc = self.source.scanner
        security_id = normalize_security_id(fetch_result.security_code)
        period = fetch_result.period_min

        grouped = {}
        for bar in fetch_result.bars:
            grouped.setdefault((bar["trading_date"], bar["session"]),
                               []).append(bar)

        rows = []
        for (day, session), bars in sorted(grouped.items()):
            rule = self.registry.resolve(self.market, day)
            if session == Session.OTHER:
                expected = 0          # 시간외종가 등 — 기대 수를 정의하지 않는다
            elif rule.session_exists(day, session):
                expected = sc.expected_bars_per_day(session, period)
            else:
                expected = 0
            actual = len(bars)
            completeness = (actual / expected) if expected else 1.0
            reasons = []
            if expected and actual < expected:
                reasons.append("MISSING_BARS")
            if expected and actual > expected:
                reasons.append("OVER_COUNT")
            if not fetch_result.pagination_complete:
                reasons.append("PAGINATION_INCOMPLETE")

            status = QualityStatus.SUCCESS
            if reasons:
                status = (QualityStatus.PARTIAL if actual else
                          QualityStatus.FAILED)
            rows.append({
                "dataset": "minute",
                "security_id": security_id,
                "period_min": period,
                "trading_date": day,
                "session": session,
                "expected_bars": int(expected),
                "actual_bars": int(actual),
                "completeness": round(float(completeness), 6),
                "first_bar_at_utc": to_utc(bars[0]["bar_start"], assume_tz=KST),
                "last_bar_at_utc": to_utc(bars[-1]["bar_end"], assume_tz=KST),
                "pagination_complete": bool(fetch_result.pagination_complete),
                "status": status,
                "reason_codes": reasons,
                "run_id": run_id,
                "market_rule_version": rule.version,
                "schema_version": SCHEMA_VERSION,
                "data_revision": self.data_revision,
            })
        return rows

    # ── 수집 ───────────────────────────────────────────────

    def build(self, codes, run_id: str, period: int = 5, retries: int = 2,
              flush_every: int = 10, dry_run: bool = False, on_progress=None,
              max_pages: int = None, end=None, min_date: str = None,
              keep_bar=None, keep_coverage=None):
        """종목 목록의 분봉을 API가 주는 끝까지 받아 저장한다.

        `end`를 주면 그 **거래일까지만** 저장한다. `ka10080`에는 날짜
        파라미터가 없어 호출 시점까지 전부 주므로, 장중에 돌리면 오늘의
        반쪽 세션이 정상 거래일처럼 섞인다. 요청을 줄일 수는 없으니 받은
        뒤에 자른다.

        `min_date`(YYYYMMDD)를 주면 그보다 과거에 닿는 순간 **페이지 요청을
        멈춘다**. 일일 누적(`minute-cache catchup`)에서 이미 가진 구간을 다시
        받지 않기 위한 것이다 — `end`가 저장 범위를 자르는 것과 달리 이쪽은
        호출 수를 줄인다.

        `keep_bar(security_id, trading_date, session)`·`keep_coverage(…)`를 주면
        False인 봉·커버리지 행을 **저장하지 않는다**(받기는 한다). 일일 누적이 같은
        봉을 두 번 저장하지 않으려고 쓴다(`minute_catchup.SessionStorePolicy`).
        """
        if self.source is None:
            from .market_source import minute_source
            self.source = minute_source()
        token = self.source.token()
        started = _time.monotonic()
        fetched_at = datetime.now(tz=UTC)

        buffer, coverage_buffer = [], []
        writes, cov_writes, per_security = [], [], []
        total_bars = 0          # 저장한 봉
        total_fetched = 0       # 받은 봉(저장 필터 전)
        chunk = 0

        def _flush():
            nonlocal buffer, coverage_buffer, chunk
            if dry_run or not (buffer or coverage_buffer):
                buffer, coverage_buffer = [], []
                return
            chunk += 1
            # 저장 필터로 봉이 하나도 남지 않아도 커버리지(받은 시각의 근거)는 쓴다
            if buffer:
                buffer.sort(key=lambda r: (r["security_id"], r["bar_start_at_utc"]))
                writes.append(self.store.write(
                    self.LAYER, self.TABLE, buffer, MINUTE_BAR_COLUMNS,
                    MINUTE_BAR_PK, run_id, partition_fn=self._partition,
                    validators=[validate_quarantined_ohlc],
                    part_key=f"c{chunk:03d}",
                    extra_manifest={
                        "source": _source_id(self.source),
                        "period_min": period,
                        "min_date": min_date,
                        "timestamp_semantics": _semantics(self.source),
                        "securities": sorted({r["security_id"] for r in buffer}),
                        "note": ("최신부터 주는 데까지(또는 min_date까지) 받은 "
                                 "결과이며, 과거 시점은 slice_as_of()로 "
                                 "offline에서 자른다"),
                    }))
            if coverage_buffer:
                cov_writes.append(self.store.write(
                    self.LAYER, self.COVERAGE_TABLE, coverage_buffer,
                    BAR_COVERAGE_COLUMNS, BAR_COVERAGE_PK, run_id,
                    partition_fn=lambda r: [("dataset", r["dataset"]),
                                            ("year", r["trading_date"].year)],
                    part_key=f"c{chunk:03d}"))
            buffer, coverage_buffer = [], []

        for i, code in enumerate(codes, start=1):
            t0 = _time.monotonic()
            attempts = 0
            for attempts in range(1, retries + 2):
                result = self.source.fetch_security(
                    code, token=token, period=period, max_pages=max_pages,
                    min_date=min_date)
                if result.status != QualityStatus.FAILED:
                    break

            if end is not None:
                # 상한 넘는 봉을 **버린다.** 수집 메타(pages,
                # pagination_complete)는 건드리지 않는다 — 수집은 끝까지
                # 됐고, 자른 것은 저장 범위다.
                result.bars = [b for b in result.bars
                               if b["trading_date"] <= end]

            rows = self.to_silver_rows(result, run_id, fetched_at)
            if result.status != QualityStatus.FAILED:
                total_fetched += len(rows)
            if result.status == QualityStatus.FAILED:
                # 일봉과 같은 규칙: 잘린 시계열을 저장하지 않는다.
                rows = []
            else:
                cov = self.to_coverage_rows(result, run_id)
                if keep_coverage is not None:
                    cov = [r for r in cov if keep_coverage(
                        r["security_id"], r["trading_date"], r["session"])]
                coverage_buffer.extend(cov)
                if keep_bar is not None:
                    rows = [r for r in rows if keep_bar(
                        r["security_id"], r["trading_date"], r["session"])]
            buffer.extend(rows)
            total_bars += len(rows)

            record = result.to_dict()
            record["security_id"] = normalize_security_id(code)
            record["seconds"] = round(_time.monotonic() - t0, 2)
            record["attempts"] = attempts
            record["rows_written"] = len(rows)
            per_security.append(record)
            if on_progress:
                on_progress(i, len(codes), record)
            if flush_every and i % flush_every == 0:
                _flush()
        _flush()

        succeeded = sum(1 for r in per_security
                        if r["status"] == QualityStatus.SUCCESS)
        failed = sum(1 for r in per_security
                     if r["status"] == QualityStatus.FAILED)
        dates = [r["first_date"] for r in per_security if r["first_date"]]
        last_dates = [r["last_date"] for r in per_security if r["last_date"]]
        return {
            "run_id": run_id,
            "period_min": period,
            "attempted": len(per_security),
            "succeeded": succeeded,
            "partial": len(per_security) - succeeded - failed,
            "failed": failed,
            "bars": total_bars,
            "fetched_bars": total_fetched,
            "earliest_date": min(dates) if dates else None,
            "latest_date": max(last_dates) if last_dates else None,
            "end_cap": end.isoformat() if end is not None else None,
            "min_date": min_date,
            "elapsed_sec": round(_time.monotonic() - started, 1),
            "sec_per_security": (round((_time.monotonic() - started)
                                       / len(per_security), 2)
                                 if per_security else None),
            "artifacts": [w.manifest_path for w in writes + cov_writes],
            "status": (QualityStatus.SUCCESS if failed == 0 and total_fetched
                       else QualityStatus.PARTIAL if total_fetched
                       else QualityStatus.FAILED),
        }

    # ── 조회 ───────────────────────────────────────────────

    def existing_securities(self, period: int = 5):
        try:
            table = self.store.read_arrow(
                self.LAYER, self.TABLE,
                # 종목 집합만 본다 — 리비전과 무관하므로 저장 PK로 충분하다.
                columns=["security_id", "period_min"], dedupe_pk=MINUTE_BAR_PK)
        except NoInputDataError:
            return set()
        import pyarrow.compute as pc
        table = table.filter(pc.equal(table.column("period_min"), period))
        return set(table.column("security_id").to_pylist())

    def load_bars(self, security_id: str, start: datetime = None,
                  end: datetime = None, period: int = 5,
                  session: str = None, columns=None):
        """저장된 분봉을 읽는다. 시각 경계는 `bar_end_at_utc` 기준이다."""
        import pyarrow as pa
        import pyarrow.compute as pc

        needed = columns or ["security_id", "period_min", "bar_start_at_utc",
                             "bar_end_at_utc", "trading_date", "session",
                             "open", "high", "low", "close", "volume",
                             "quality_status"]
        table = self.store.read_arrow(
            self.LAYER, self.TABLE, columns=needed,
            latest_by=MINUTE_BAR_LOGICAL_KEY)     # 같은 봉은 최신 리비전만
        mask = pc.and_(
            pc.equal(table.column("security_id"),
                     normalize_security_id(security_id)),
            pc.equal(table.column("period_min"), period))
        if session:
            Session.validate(session)
            mask = pc.and_(mask, pc.equal(table.column("session"), session))
        if start is not None:
            mask = pc.and_(mask, pc.greater_equal(
                table.column("bar_end_at_utc"), pa.scalar(to_utc(start))))
        if end is not None:
            mask = pc.and_(mask, pc.less_equal(
                table.column("bar_end_at_utc"), pa.scalar(to_utc(end))))
        return table.filter(mask).sort_by([("bar_start_at_utc", "ascending")])

    def slice_as_of(self, security_id: str, as_of: datetime,
                    lookback_trading_days: int = 5, period: int = 5,
                    calendar=None):
        """`as_of` 시점에 **알 수 있었던** 분봉만 돌려준다.

        `bar_end_at_utc <= as_of`를 강제한다(아키텍처 §10 불변식 2). 이 조건이
        없으면 15:35 신호에 15:40에 끝난 봉이 섞여 미래를 보게 된다.

        결과는 V1.5 `fetch_minute_bars()`가 돌려주는 형태(naive KST datetime의
        `bar_start`/`bar_end`, `session`)로 맞춘다. 그래야 `analyze()`에 그대로
        넣어 live와 같은 코드로 replay할 수 있다.
        """
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=KST)
        cutoff = to_utc(as_of)

        table = self.load_bars(security_id, end=cutoff, period=period)
        if table.num_rows == 0:
            return []

        days = sorted({d for d in table.column("trading_date").to_pylist()})
        if lookback_trading_days:
            keep_days = set(days[-int(lookback_trading_days):])
        else:
            keep_days = set(days)

        bars = []
        for row in table.to_pylist():
            if row["trading_date"] not in keep_days:
                continue
            if row["quality_status"] == QualityStatus.FAILED:
                continue
            start_kst = row["bar_start_at_utc"].astimezone(KST)
            end_kst = row["bar_end_at_utc"].astimezone(KST)
            bars.append({
                "dt": start_kst.strftime("%Y%m%d%H%M%S"),
                "open": float(row["open"]), "high": float(row["high"]),
                "low": float(row["low"]), "close": float(row["close"]),
                "volume": int(row["volume"]),
                "bar_start": start_kst.replace(tzinfo=None),
                "bar_end": end_kst.replace(tzinfo=None),
                "session": row["session"],
            })
        return bars

    # ── 검증 ───────────────────────────────────────────────

    def validate(self, period: int = 5):
        """cache 신뢰성 요약 — 커버리지 테이블 기준."""
        try:
            table = self.store.read_arrow(
                self.LAYER, self.COVERAGE_TABLE,
                columns=["security_id", "period_min", "trading_date", "session",
                         "expected_bars", "actual_bars", "completeness",
                         "status", "pagination_complete"],
                dedupe_pk=BAR_COVERAGE_PK)
        except NoInputDataError:
            raise NoInputDataError(
                "분봉 커버리지가 없다 — `minute-cache build` 먼저")
        import pyarrow.compute as pc
        table = table.filter(pc.equal(table.column("period_min"), period))
        rows = table.to_pylist()
        if not rows:
            raise NoInputDataError(f"period={period} 커버리지가 없다")

        by_session = {}
        for row in rows:
            entry = by_session.setdefault(
                row["session"], {"days": 0, "expected": 0, "actual": 0,
                                 "incomplete": 0})
            entry["days"] += 1
            entry["expected"] += row["expected_bars"]
            entry["actual"] += row["actual_bars"]
            if row["status"] != QualityStatus.SUCCESS:
                entry["incomplete"] += 1

        dates = [r["trading_date"] for r in rows]
        securities = {r["security_id"] for r in rows}
        short, late_listed = {}, {}
        per_sec_days = {}
        for row in rows:
            if row["session"] != Session.REGULAR:
                continue
            per_sec_days[row["security_id"]] = \
                per_sec_days.get(row["security_id"], 0) + 1
        # 거래일이 짧은 이유는 두 가지다. 상장이 늦었으면 정상이고, 자기
        # 구간 안이 비었으면 수집 결함이다. 일봉 검증기와 같은 규칙을 쓴다.
        first_seen, last_seen = {}, {}
        for row in rows:
            if row["session"] != Session.REGULAR:
                continue
            sid, day = row["security_id"], row["trading_date"]
            if sid not in first_seen or day < first_seen[sid]:
                first_seen[sid] = day
            if sid not in last_seen or day > last_seen[sid]:
                last_seen[sid] = day
        panel_days = sorted({r["trading_date"] for r in rows
                             if r["session"] == Session.REGULAR})
        index_of = {d: i for i, d in enumerate(panel_days)}
        late_listed = {}
        for sid, days_count in per_sec_days.items():
            span = index_of[last_seen[sid]] - index_of[first_seen[sid]] + 1
            if days_count < span * 0.98:
                short[sid] = {"days": days_count, "expected_span": span}
            elif first_seen[sid] > panel_days[0]:
                late_listed[sid] = first_seen[sid].isoformat()

        incomplete_pagination = sum(
            1 for r in rows if not r["pagination_complete"])
        report = {
            "table": f"{self.LAYER}.{self.COVERAGE_TABLE}",
            "period_min": period,
            "securities": len(securities),
            "trading_dates": len(set(dates)),
            "date_range": [min(dates).isoformat(), max(dates).isoformat()],
            "by_session": by_session,
            "short_securities": short,
            "late_listed": late_listed,
            "pagination_incomplete_rows": incomplete_pagination,
            "regular_days_median": (sorted(per_sec_days.values())
                                    [len(per_sec_days) // 2]
                                    if per_sec_days else 0),
        }
        report["status"] = (
            QualityStatus.PARTIAL
            if (short or incomplete_pagination) else QualityStatus.SUCCESS)
        return report

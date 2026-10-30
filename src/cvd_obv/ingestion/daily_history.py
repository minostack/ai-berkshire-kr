# -*- coding: utf-8 -*-
"""일봉 이력 저장소 — 아키텍처 §6 `daily_history_cache`, 로드맵 Stage 1.

수집 결과를 `silver.daily_bars`로 정규화해 Parquet에 쓰고, 종목별 수집 기록을
`bronze.ingest_runs`에 남긴다.

정직하게 남겨야 할 두 가지
--------------------------
1. **수정주가는 소급 재작성된다.** `upd_stkpc_tp=1`로 받은 값은 오늘 시점의
   조정계수가 반영된 시계열이다. 과거의 그날 화면에 뜬 가격이 아니다.
   `adjustment_as_of_utc`에 수집 시각을 남겨, 아키텍처 §10 불변식 8이 요구하는
   "연구용 시계열임을 분리"할 근거를 만든다.
2. **일봉의 종가 의미가 2026-09-14를 경계로 바뀐다.** 그 이후 키움 일봉 종가는
   애프터마켓 최종 체결가다. `price_scope`로 구분해 저장한다.
3. **당일 봉은 받은 시각이 성격을 정한다**(봉 기준 `bars-1.0`,
   `bar_standard.classify_fetched_daily`). 15:35 전·16:00~20:05에 받은 당일 봉은
   확정되지 않은 값이라 저장하지 않는다. 15:35~16:00에 받은 당일 봉은 애프터마켓
   체결이 없는 정규장 값이므로 `price_scope=REGULAR`로 저장하고
   `FETCHED_IN_REGULAR_WINDOW` 플래그를 단다. 이전에는 시각과 무관하게
   `EXTENDED`로 저장해, 17:00에 받은 진행 중 애프터마켓 가격이 확정 봉처럼 남았다.
"""

from __future__ import annotations

import json
import time as _time
from datetime import date, datetime

from .. import SCHEMA_VERSION
from ..contracts.market_schema import (
    DAILY_BAR_COLUMNS, DAILY_BAR_LOGICAL_KEY, DAILY_BAR_PK,
    INGEST_RUN_COLUMNS, INGEST_RUN_PK,
    OHLC_VIOLATION_FLAG, ohlc_violations_of, validate_ohlc,
    validate_quarantined_ohlc, validate_series_order,
)
from ..domain.errors import NoInputDataError, PartialSuccess
from ..domain.ids import UTC, normalize_security_id, to_utc
from ..domain.market_rules import default_registry
from ..domain.status import QualityStatus
from ..storage.parquet_store import ParquetStore
from ..bar_standard import FETCH_INCOMPLETE, classify_fetched_daily
from .kiwoom_daily import KIWOOM_DAILY_SOURCE, KiwoomDailySource


def _source_id(source) -> str:
    """행의 `source` 값 — 어댑터가 밝히지 않으면 키움(예전 기본)."""
    return getattr(source, "SOURCE", None) or KIWOOM_DAILY_SOURCE

__all__ = ["DailyHistoryRepository", "IngestSummary", "load_index_codes",
           "INDEX_FILE_KEYS"]

INDEX_FILE_KEYS = {"KOSPI200": "kospi200", "KOSDAQ150": "kosdaq150"}

#: 15:35~16:00에 받은 당일 봉 — 정규장 값으로 저장했다는 표시
FETCHED_IN_REGULAR_WINDOW = "FETCHED_IN_REGULAR_WINDOW"


def load_index_codes(path, index_id: str = "KOSPI200"):
    """`data/index_stocks.json`에서 종목코드 목록을 읽는다 → (코드 목록, 갱신 시각).

    **주의**: 이것은 오늘의 구성종목 스냅샷이다. 과거 시점의 구성종목이 아니다.
    수집 대상 선정에만 쓰고, 과거 replay 유니버스로 쓰지 않는다(생존편향).
    파일 해석은 유니버스 기준 `universe-1.0`(`cvd_obv.universe.read_index_file`)
    하나다.
    """
    import os
    from ..universe import read_index_file

    index_id = index_id.upper()
    if index_id not in INDEX_FILE_KEYS:
        raise NoInputDataError(f"알 수 없는 index_id: {index_id}")
    if path and not os.path.exists(path):
        raise NoInputDataError(f"구성종목 파일이 없다: {path}")
    block = read_index_file(path).get(index_id)
    codes = [s["code"] for s in (block or {}).get("stocks", [])]
    if not codes:
        raise NoInputDataError(f"{index_id} 구성종목이 비어 있다: {path}")
    return codes, block.get("updated")


class IngestSummary:
    """수집 실행 하나의 결과."""

    __slots__ = ("run_id", "index_id", "attempted", "succeeded", "partial",
                 "failed", "rows", "per_security", "write_result",
                 "ingest_write_result", "elapsed_sec", "start", "end")

    def __init__(self, run_id, index_id, per_security, rows, elapsed_sec,
                 start, end, write_result=None, ingest_write_result=None):
        self.run_id = run_id
        self.index_id = index_id
        self.per_security = per_security
        self.rows = rows
        self.elapsed_sec = elapsed_sec
        self.start = start
        self.end = end
        self.write_result = write_result
        self.ingest_write_result = ingest_write_result
        self.attempted = len(per_security)
        self.succeeded = sum(1 for r in per_security
                             if r["status"] == QualityStatus.SUCCESS)
        self.partial = sum(1 for r in per_security
                           if r["status"] == QualityStatus.PARTIAL)
        self.failed = sum(1 for r in per_security
                          if r["status"] == QualityStatus.FAILED)

    @property
    def status(self):
        if self.failed == 0 and self.partial == 0:
            return QualityStatus.SUCCESS
        if self.succeeded == 0:
            return QualityStatus.FAILED
        return QualityStatus.PARTIAL

    @property
    def manifest_paths(self):
        """이번 실행이 남긴 manifest 경로. 청크로 나눠 썼으면 여러 개다."""
        out = []
        for w in (self.write_result, self.ingest_write_result):
            if w is None:
                continue
            out.extend(x.manifest_path for x in
                       (w if isinstance(w, list) else [w]))
        return out

    def to_dict(self):
        dates = [r["first_date"] for r in self.per_security if r["first_date"]]
        return {
            "run_id": self.run_id,
            "index_id": self.index_id,
            "attempted": self.attempted,
            "succeeded": self.succeeded,
            "partial": self.partial,
            "failed": self.failed,
            "rows": self.rows,
            "status": self.status,
            "elapsed_sec": round(self.elapsed_sec, 1),
            "sec_per_security": (round(self.elapsed_sec / self.attempted, 2)
                                 if self.attempted else None),
            "earliest_date": min(dates) if dates else None,
            "artifacts": self.manifest_paths,
        }


class DailyHistoryRepository:
    """`silver.daily_bars` 수집·저장·조회."""

    LAYER = "silver"
    TABLE = "daily_bars"

    def __init__(self, store: ParquetStore = None,
                 source: KiwoomDailySource = None, registry=None,
                 market: str = "KRX", data_revision: int = 1, clock=None):
        self.store = store or ParquetStore()
        #: 종목별 수집 시각. 테스트에서 고정하려고 주입할 수 있게 둔다.
        self.clock = clock or (lambda: datetime.now(tz=UTC))
        #: 마지막 `to_silver_rows`가 버린 미확정 당일 봉 수
        self.last_incomplete_skipped = 0
        self.source = source
        self.registry = registry or default_registry()
        self.market = market
        self.data_revision = int(data_revision)

    # ── 정규화 ─────────────────────────────────────────────

    def to_silver_rows(self, fetch_result, run_id: str, fetched_at: datetime):
        """fetch 결과 → `silver.daily_bars` row 목록."""
        security_id = normalize_security_id(fetch_result.security_code)
        fetched_utc = to_utc(fetched_at)
        status = (QualityStatus.SUCCESS
                  if fetch_result.status == QualityStatus.SUCCESS
                  else QualityStatus.PARTIAL)
        flags = list(fetch_result.quality_flags)
        adjustment_type = (self.source.adjustment_type if self.source
                           else "SPLIT_ADJUSTED")

        rows = []
        skipped = 0
        for bar in fetch_result.rows:
            day = bar["trading_date"]
            rule = self.registry.resolve(self.market, day)
            confirmed = rule.daily_confirmed_at(day)
            row_flags = set(flags) | set(bar.get("quality_flags") or ())
            row_status = status
            scope = classify_fetched_daily(day, fetched_at, self.registry,
                                           self.market)
            if scope == FETCH_INCOMPLETE:
                skipped += 1        # 확정 전 값 — 다음 적재에서 다시 받는다
                continue
            if scope != rule.daily_scope_of(day):
                # 애프터마켓 시행일의 15:35~16:00 수집 → 정규장 확정값
                row_flags.add(FETCHED_IN_REGULAR_WINDOW)
                confirmed = rule.session_complete_at(day, scope)
            # 공급자가 보낸 OHLC가 앞뒤가 안 맞는 경우가 실제로 있다(실측:
            # 018880 2001-12-18 저가 41,750 > 고가 36,700). 행을 버리지 않고
            # FAILED로 표시해 저장한다 — 버리면 표본이 조용히 바뀐다.
            ohlc_reasons = ohlc_violations_of(bar)
            if ohlc_reasons:
                row_flags.add(OHLC_VIOLATION_FLAG)
                row_status = QualityStatus.FAILED
            rows.append({
                "security_id": security_id,
                "trading_date": day,
                "open": bar["open"], "high": bar["high"],
                "low": bar["low"], "close": bar["close"],
                "volume": bar["volume"],
                "traded_value": bar["traded_value"],
                "price_scope": scope,
                "adjustment_type": adjustment_type,
                "adjustment_factor": None,
                "adjustment_as_of_utc": fetched_utc,
                "available_at_utc": to_utc(confirmed),
                "source": _source_id(self.source),
                "quality_status": row_status,
                # 종목 단위 수집 플래그 + 이 행 고유의 문제를 합친다.
                "quality_flags": sorted(row_flags),
                "run_id": run_id,
                "market_rule_version": rule.version,
                "schema_version": SCHEMA_VERSION,
                "data_revision": self.data_revision,
            })
        self.last_incomplete_skipped = skipped
        return rows

    @staticmethod
    def _partition(row):
        d = row["trading_date"]
        return [("market", "KRX"), ("year", d.year), ("month", f"{d.month:02d}")]

    # ── 재개 ───────────────────────────────────────────────

    def failed_securities(self):
        """`bronze.ingest_runs`가 불완전하다고 기록한 종목.

        수집이 페이지 도중 끊기면 시계열이 임의의 날짜에서 잘린다. 그대로 두면
        "그 시점에 상장폐지된 종목"처럼 보이므로, 다시 받을 대상을 여기서 뽑는다.
        판정은 로그가 아니라 저장된 수집 기록을 근거로 한다.
        """
        try:
            table = self.store.read_arrow(
                "bronze", "ingest_runs",
                columns=["security_id", "status", "pagination_complete",
                         "run_id"])
        except NoInputDataError:
            return {}
        rows = table.to_pylist()
        # 같은 종목을 여러 run에서 받았으면 가장 나중 run의 결과만 본다.
        latest = {}
        for row in rows:
            sid = row["security_id"]
            if sid not in latest or row["run_id"] > latest[sid]["run_id"]:
                latest[sid] = row
        return {sid: row for sid, row in latest.items()
                if row["status"] != QualityStatus.SUCCESS
                or not row["pagination_complete"]}

    def suspect_truncated_securities(self, page_size: int = 600):
        """페이지 경계에서 잘린 것으로 **의심되는** 종목.

        ka10081은 최근 날짜부터 과거로 페이지를 넘긴다. 도중에 응답이 끊기면
        시계열의 **오래된 쪽**이 통째로 사라지고, 남은 행 수가 페이지 크기의
        정확한 배수가 된다. 실측에서 잘린 종목들의 행 수는 3,600 / 6,000 /
        5,400 / 4,800 / 1,800처럼 모두 600의 배수였다.

        정상 종목의 행 수가 우연히 600의 배수일 확률은 낮지만 0은 아니므로,
        이것은 **판정이 아니라 재수집 후보 선별**이다. 후보를 다시 받아 보는
        비용은 종목당 몇 초라 과선별이 과소선별보다 안전하다.

        마지막 거래일이 패널의 마지막 날이 아닌 종목(상장폐지·거래정지)은
        제외한다 — 그쪽은 짧은 이유가 따로 있다.
        """
        try:
            table = self.store.read_arrow(
                self.LAYER, self.TABLE,
                columns=["security_id", "trading_date"],
                # 행 수를 센다 — 리비전이 여럿인 봉을 한 번만 세야 한다.
                latest_by=DAILY_BAR_LOGICAL_KEY)
        except NoInputDataError:
            return {}
        sids = table.column("security_id").to_pylist()
        days = table.column("trading_date").to_pylist()
        if not sids:
            return {}
        panel_last = max(days)

        counts, last_seen = {}, {}
        for sid, day in zip(sids, days):
            counts[sid] = counts.get(sid, 0) + 1
            if sid not in last_seen or day > last_seen[sid]:
                last_seen[sid] = day

        out = {}
        for sid, count in counts.items():
            if count % page_size != 0:
                continue
            if last_seen[sid] != panel_last:
                continue
            out[sid] = {"rows": count, "pages": count // page_size,
                        "reason": f"행 수가 페이지 크기({page_size})의 정확한 배수"}
        return out

    def existing_securities(self):
        """이미 저장된 종목 집합.

        긴 수집이 중간에 끊겼을 때 처음부터 다시 받지 않기 위한 것이다.
        판정 기준은 "행이 있는가"이지 "완전한가"가 아니므로, 부분 수집된 종목을
        건너뛸 위험이 있다. 그래서 기본값이 아니라 명시적 옵션으로만 쓴다.
        """
        try:
            table = self.store.read_arrow(
                self.LAYER, self.TABLE, columns=["security_id"],
                # 종목 집합만 본다 — 리비전과 무관하므로 저장 PK로 충분하다.
                dedupe_pk=DAILY_BAR_PK)
        except NoInputDataError:
            return set()
        return set(table.column("security_id").to_pylist())

    # ── 수집 ───────────────────────────────────────────────

    def build(self, codes, run_id: str, start: date = None, end: date = None,
              on_progress=None, dry_run: bool = False, max_pages: int = None,
              flush_every: int = 20, index_id: str = None, retries: int = 2):
        """종목 목록의 일봉을 수집해 저장한다.

        `flush_every`종목마다 Parquet으로 내려쓴다. 199종목 27년이면 약 130만
        행이라 전부 메모리에 들고 있으면 수 GB가 된다. 나눠 쓰면 메모리가
        잡히고, 중간에 끊겨도 그때까지 받은 데이터가 파일로 남는다.
        """
        if self.source is None:
            from .market_source import daily_source
            self.source = daily_source()
        token = self.source.token()
        started = _time.monotonic()
        fetched_at = datetime.now(tz=UTC)

        buffer, per_security, ingest_rows = [], [], []
        writes, total_rows = [], 0
        chunk = 0

        def _flush():
            nonlocal buffer, chunk
            if dry_run or not buffer:
                buffer = []
                return
            chunk += 1
            buffer.sort(key=lambda r: (r["security_id"], r["trading_date"]))
            writes.append(self.store.write(
                self.LAYER, self.TABLE, buffer, DAILY_BAR_COLUMNS,
                DAILY_BAR_PK, run_id, partition_fn=self._partition,
                validators=[validate_quarantined_ohlc, validate_series_order],
                part_key=f"c{chunk:03d}",
                extra_manifest={
                    "source": _source_id(self.source),
                    "adjustment_type": self.source.adjustment_type,
                    "index_id": index_id,
                    "requested_start": start.isoformat() if start else None,
                    "requested_end": end.isoformat() if end else None,
                    "market_rule_versions": sorted(
                        {r["market_rule_version"] for r in buffer}),
                    "price_scopes": sorted({r["price_scope"] for r in buffer}),
                    "securities": sorted({r["security_id"] for r in buffer}),
                }))
            buffer = []

        for i, code in enumerate(codes, start=1):
            t0 = _time.monotonic()
            attempts = 0
            for attempts in range(1, retries + 2):
                result = self.source.fetch_security(
                    code, token=token, start=start, end=end,
                    max_pages=max_pages)
                if result.status != QualityStatus.FAILED:
                    break

            # 받은 시각은 종목마다 잰다 — 15:58에 시작한 수집이 16:00을 넘기면
            # 뒤쪽 종목의 당일 봉은 이미 애프터마켓 값이다.
            rows = self.to_silver_rows(result, run_id, self.clock())
            if result.status == QualityStatus.FAILED:
                # 페이지 도중 끊긴 시계열을 저장하면 "그 시점에 상장폐지된
                # 종목"처럼 보인다. 재시도해도 실패하면 아예 쓰지 않고
                # 기록만 남긴다 — 다음 실행에서 다시 받게 된다.
                rows = []
            buffer.extend(rows)
            total_rows += len(rows)

            record = result.to_dict()
            record["security_id"] = normalize_security_id(code)
            record["seconds"] = round(_time.monotonic() - t0, 2)
            record["attempts"] = attempts
            record["rows_written"] = len(rows)
            record["incomplete_today_skipped"] = self.last_incomplete_skipped
            per_security.append(record)
            ingest_rows.append(self._ingest_row(result, run_id, fetched_at,
                                                len(rows), attempts))
            if on_progress:
                on_progress(i, len(codes), record)
            if flush_every and i % flush_every == 0:
                _flush()

        _flush()

        elapsed = _time.monotonic() - started
        ingest_write = None
        if not dry_run and ingest_rows:
            ingest_write = self.store.write(
                "bronze", "ingest_runs", ingest_rows, INGEST_RUN_COLUMNS,
                INGEST_RUN_PK, run_id,
                partition_fn=lambda r: [("dataset", r["dataset"]),
                                        ("started_year",
                                         r["started_at_utc"].year)])

        return IngestSummary(run_id, index_id, per_security, total_rows,
                             elapsed, start, end, writes or None, ingest_write)

    def _ingest_row(self, result, run_id, started_at, rows_written,
                    attempts: int = 1):
        return {
            "run_id": run_id,
            "source": _source_id(self.source),
            "dataset": "daily",
            "security_id": normalize_security_id(result.security_code),
            "started_at_utc": to_utc(started_at),
            "finished_at_utc": datetime.now(tz=UTC),
            # 요청 파라미터에는 토큰·appkey가 들어가지 않는다(스키마 §4).
            "request_params_json": json.dumps(result.requested, sort_keys=True,
                                              ensure_ascii=False),
            "status": result.status,
            "rows_received": result.rows_received,
            "rows_written": rows_written,
            "page_count": result.pages,
            "retry_count": max(0, int(attempts) - 1),
            "pagination_complete": result.pagination_complete,
            "first_trading_date": result.first_date,
            "last_trading_date": result.last_date,
            "error_code": result.error_code,
            "error_message": result.error_message,
            "payload_hash": None,
            "schema_version": SCHEMA_VERSION,
        }

    # ── 조회·검증 ──────────────────────────────────────────

    def load(self, security_id: str = None, start: date = None,
             end: date = None, price_scope: str = None,
             adjustment_type: str = None):
        def _where(row):
            if security_id and row["security_id"] != normalize_security_id(security_id):
                return False
            if start and row["trading_date"] < start:
                return False
            if end and row["trading_date"] > end:
                return False
            if price_scope and row["price_scope"] != price_scope:
                return False
            if adjustment_type and row["adjustment_type"] != adjustment_type:
                return False
            return True

        # 논리 키로 중복 제거 — `read()`는 리비전 큰 행을 남기므로 봉마다
        # 최신 리비전 하나만 돌려준다(저장 PK로 하면 리비전이 모두 남는다).
        rows = self.store.read(self.LAYER, self.TABLE,
                               pk=DAILY_BAR_LOGICAL_KEY, where=_where)
        rows.sort(key=lambda r: (r["security_id"], r["trading_date"]))
        return rows

    def validate(self, rows=None):
        """저장된 일봉의 불변식과 커버리지를 점검한다(Stage 1 DoD)."""
        rows = rows if rows is not None else self.load()
        if not rows:
            raise NoInputDataError(f"{self.LAYER}.{self.TABLE}: 데이터가 없다")

        validate_series_order(rows)
        # 격리 표시가 된 위반과 표시가 없는 위반을 구분한다. 앞은 공급자 데이터
        # 오류를 제대로 처리한 결과이고, 뒤는 우리 코드의 결함이다.
        ohlc_violations = validate_ohlc(rows, strict=False)
        unquarantined = [
            row for row in rows
            if ohlc_violations_of(row)
            and (row["quality_status"] != QualityStatus.FAILED
                 or OHLC_VIOLATION_FLAG not in (row["quality_flags"] or ()))
        ]

        by_security = {}
        for row in rows:
            entry = by_security.setdefault(
                row["security_id"],
                {"rows": 0, "first": row["trading_date"],
                 "last": row["trading_date"], "scopes": set(),
                 "flags": set(), "partial": 0, "failed": 0})
            entry["rows"] += 1
            entry["first"] = min(entry["first"], row["trading_date"])
            entry["last"] = max(entry["last"], row["trading_date"])
            entry["scopes"].add(row["price_scope"])
            entry["flags"].update(row["quality_flags"])
            if row["quality_status"] == QualityStatus.FAILED:
                entry["failed"] += 1
            elif row["quality_status"] != QualityStatus.SUCCESS:
                entry["partial"] += 1

        # 시계열이 짧은 이유는 두 가지이고 성격이 전혀 다르다.
        #   (1) 상장이 늦었다 / 상장폐지됐다 — 정상이다.
        #   (2) 보유 구간 안에 거래일이 빠졌다 — 수집 결함이다.
        # 길이를 중앙값과 비교하면 둘이 구분되지 않으므로, 각 종목의
        # 자기 구간(first~last) 안에서 거래일을 몇 % 채웠는지로 본다.
        panel_days = sorted({r["trading_date"] for r in rows})
        day_index = {d: i for i, d in enumerate(panel_days)}
        panel_first, panel_last = panel_days[0], panel_days[-1]

        gaps, late_listed, delisted_or_halted = {}, {}, {}
        for sid, entry in by_security.items():
            expected = (day_index[entry["last"]] - day_index[entry["first"]] + 1)
            entry["expected"] = expected
            entry["coverage"] = entry["rows"] / expected if expected else 0.0
            if entry["coverage"] < 0.98:
                gaps[sid] = {"rows": entry["rows"], "expected": expected,
                             "coverage": round(entry["coverage"], 4)}
            if entry["first"] > panel_first:
                late_listed[sid] = entry["first"].isoformat()
            if entry["last"] < panel_last:
                delisted_or_halted[sid] = entry["last"].isoformat()

        lengths = sorted(e["rows"] for e in by_security.values())
        median = lengths[len(lengths) // 2] if lengths else 0

        report = {
            "table": f"{self.LAYER}.{self.TABLE}",
            "rows": len(rows),
            "securities": len(by_security),
            "date_range": [panel_first.isoformat(), panel_last.isoformat()],
            "panel_trading_days": len(panel_days),
            "rows_per_security": {
                "min": lengths[0], "median": median, "max": lengths[-1]},
            # 자기 구간을 다 못 채운 종목 — 진짜 결측 후보
            "coverage_gaps": gaps,
            # 구간이 짧은 정상 사유
            "late_listed": late_listed,
            "ends_before_panel": delisted_or_halted,
            "ohlc_violations": len(ohlc_violations),
            "ohlc_violations_quarantined": len(ohlc_violations) - len(unquarantined),
            "ohlc_violations_unquarantined": len(unquarantined),
            "quarantined_rows": sum(e["failed"] for e in by_security.values()),
            "partial_rows": sum(e["partial"] for e in by_security.values()),
            "price_scopes": sorted({s for e in by_security.values()
                                    for s in e["scopes"]}),
            "quality_flags": sorted({f for e in by_security.values()
                                     for f in e["flags"]}),
        }
        # 격리되지 않은 위반만 FAILED다. 격리된 행은 "처리된 문제"이므로
        # 경고 수준(PARTIAL)으로 남긴다 — 스키마 §12의 quarantine 규정대로다.
        report["status"] = (
            QualityStatus.FAILED if unquarantined else
            QualityStatus.PARTIAL
            if (gaps or report["partial_rows"] or report["quarantined_rows"])
            else QualityStatus.SUCCESS)
        return report

    def assert_complete(self, report):
        """부분 성공을 성공으로 보고하지 않는다."""
        if report["status"] == QualityStatus.PARTIAL:
            raise PartialSuccess(
                f"{report['table']}: 커버리지가 모자란 종목 "
                f"{len(report['coverage_gaps'])}개, 부분 품질 "
                f"{report['partial_rows']}행",
                context={"report": report})
        return report

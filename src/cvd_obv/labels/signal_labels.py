# -*- coding: utf-8 -*-
"""신호별 forward return 수집 — 로드맵 Stage 6.

`gold.signals`의 신호 하나하나에 "그래서 그 뒤 어떻게 됐나"를 붙인다.

수익률을 **여기서 다시 계산하지 않는다**
----------------------------------------
Stage 1이 이미 무조건부 패널(`gold.daily_forward_returns`)을 만들어 뒀다. 같은
진입·청산 규칙을 두 번 구현하면 두 표의 숫자가 갈릴 수 있고, 갈렸을 때 어느
쪽이 맞는지 알 방법이 없다. 그래서 이 모듈은 **조인만 한다**. 신호가 붙은
(종목, 날짜)를 패널에서 찾아 그 행을 그대로 옮긴다. 옮긴 사실은
`label_source` 열에 남는다.

덕분에 기준선 비교가 성립한다. Stage 1의 base rate와 Stage 6의 신호 수익률이
같은 계산에서 나오므로, 둘의 차이는 **신호의 효과**이지 계산 방식의 차이가
아니다.

지켜야 할 것
------------
- **`signal_at < entry_at`.** 신호를 낸 시각보다 앞선 진입은 있을 수 없다.
  어기면 `LookAheadError`를 던진다. 조용히 버리지 않는다.
- **거래 캘린더로 확인한다.** 진입일이 신호일의 다음 거래일인지, 청산일이
  진입일로부터 h-1 거래일 뒤인지 캘린더로 대조한다(Stage 6 DoD).
- **없는 값을 만들지 않는다.** 거래정지·상장폐지·결측으로 가격이 없으면
  패널의 `ENTRY_UNAVAILABLE`/`EXIT_UNAVAILABLE`이 그대로 넘어온다. 0이나
  직전가로 메우면 정지 종목의 수익률이 0으로 보인다.
- **성숙하지 않은 구간은 `PENDING_MATURITY`다.** h=20이면 마지막 20거래일
  신호는 아직 답이 나오지 않았다. 그 행도 쓰되 통계에서는 뺀다.
- **같은 (signal_id, horizon, label_version)은 하나뿐이다.** PK 검증이 강제한다.
"""

from __future__ import annotations

from datetime import date

from .. import MARKET_RULE_VERSION, SCHEMA_VERSION
from ..contracts.label_schema import (
    DAILY_FORWARD_RETURN_PK, DEFAULT_HORIZONS, LABEL_VERSION,
    SIGNAL_FORWARD_RETURN_COLUMNS, SIGNAL_FORWARD_RETURN_PK,
    SIGNAL_LABEL_SOURCE,
)
from ..contracts.schema import SIGNAL_PK, assert_single_signal_source
from ..domain.errors import (
    ConfigError, DataQualityError, LookAheadError, NoInputDataError,
)
from ..domain.status import LabelStatus
from ..ingestion.calendar_store import MarketCalendarRepository
from ..storage.parquet_store import ParquetStore

__all__ = ["SignalLabelBuilder", "SignalLabelSummary", "NO_PANEL_ROW"]

#: 신호는 있는데 패널에 그 (종목, 날짜)가 없을 때. 진입가 자체를 모르는
#: 상황이므로 `ENTRY_UNAVAILABLE`로 둔다.
NO_PANEL_ROW = "NO_PANEL_ROW"

#: 패널에서 그대로 옮기는 열. 이름이 같으므로 매핑이 필요 없다.
_CARRIED = (
    "entry_date", "exit_date", "entry_at_utc", "exit_at_utc",
    "entry_price", "exit_price",
    "stock_return_gross", "stock_return_net",
    "benchmark_id", "benchmark_return",
    "excess_return_gross", "excess_return_net",
    "mfe", "mae",
    "up", "up_gross", "outperform",
    "hit_3pct", "hit_5pct", "hit_minus_3pct", "hit_minus_5pct",
    "commission", "tax", "slippage",
    "label_status", "status_reason", "cost_model_version",
)

_PANEL_COLUMNS = ("security_id", "signal_date", "horizon_td",
                  "entry_rule", "label_version") + _CARRIED


class SignalLabelSummary:
    """수집 결과 요약."""

    __slots__ = ("run_id", "signals", "rows", "horizons", "label_version",
                 "status_counts", "by_signal_status", "calendar_days",
                 "calendar_checked", "missing_panel", "start", "end",
                 "writes")

    def __init__(self, run_id, signals, rows, horizons, label_version,
                 status_counts, by_signal_status, calendar_days,
                 calendar_checked, missing_panel, start, end, writes):
        self.run_id = run_id
        self.signals = int(signals)
        self.rows = int(rows)
        self.horizons = tuple(horizons)
        self.label_version = label_version
        self.status_counts = dict(status_counts)
        self.by_signal_status = by_signal_status
        self.calendar_days = int(calendar_days)
        self.calendar_checked = int(calendar_checked)
        self.missing_panel = int(missing_panel)
        self.start = start
        self.end = end
        self.writes = list(writes)

    def to_dict(self):
        return {
            "run_id": self.run_id,
            "signals": self.signals,
            "rows": self.rows,
            "horizons": list(self.horizons),
            "label_version": self.label_version,
            "status_counts": self.status_counts,
            "by_signal_status": self.by_signal_status,
            "calendar_days": self.calendar_days,
            # 날짜가 실제로 대조된 행 수. 성숙 전·가격 결측 행은 날짜가
            # null이라 대조 대상이 아니므로 이 수가 rows보다 작다.
            "calendar_checked_rows": self.calendar_checked,
            "missing_panel_rows": self.missing_panel,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "artifacts": [w.manifest_path for w in self.writes],
        }


class SignalLabelBuilder:
    """`gold.signals` × `gold.daily_forward_returns` → `gold.forward_returns`."""

    LAYER = "gold"
    TABLE = "forward_returns"

    def __init__(self, store: ParquetStore = None, horizons=DEFAULT_HORIZONS,
                 label_version: str = LABEL_VERSION, data_revision: int = 1,
                 calendar=None, entry_rule: str = "NEXT_OPEN",
                 confirmed_close_entry: bool = False):
        from ..contracts.label_schema import ENTRY_RULES

        from ..contracts.label_schema import label_version_for

        if entry_rule not in ENTRY_RULES:
            raise ConfigError(f"알 수 없는 entry_rule: {entry_rule}")
        self.entry_rule = entry_rule
        # 신호(15:35)보다 5분 앞선 같은 날 **확정 종가**(15:30)에 샀다고
        # 가정한다. 동시호가는 그 종가를 만드는 절차이므로 실행 불가능하다.
        # 기본은 꺼 두고 SAME_CLOSE에서만 열며, 열더라도 허용 범위는
        # "신호 당일" 하나뿐이다 — 신호일보다 앞선 날짜의 진입은 이
        # 가정으로도 설명되지 않으므로 그대로 막는다.
        self.confirmed_close_entry = bool(confirmed_close_entry)
        if self.confirmed_close_entry and entry_rule != "SAME_CLOSE":
            raise ConfigError(
                "confirmed_close_entry는 entry_rule=SAME_CLOSE에서만 쓴다 "
                f"(받은 값: {entry_rule})")
        # `gold.forward_returns`의 PK에 entry_rule이 없다. 규칙별로
        # label_version을 갈라 두지 않으면 두 벌이 서로 덮어쓴다.
        # 읽을 패널의 버전. 확정 종가 가정은 일봉 패널을 바꾸지 않으므로
        # **정규 SAME_CLOSE 패널**을 그대로 읽고, 결과만 확정 버전으로 쓴다.
        panel_version = label_version
        if label_version == LABEL_VERSION:
            panel_version = label_version_for(entry_rule)
            label_version = label_version_for(
                entry_rule, confirmed_close=bool(confirmed_close_entry))
        self.panel_version = panel_version
        self.store = store or ParquetStore()
        self.horizons = tuple(int(h) for h in horizons)
        self.label_version = label_version
        self.data_revision = int(data_revision)
        self._calendar = calendar

    # ── 입력 ──────────────────────────────────────────────────

    def calendar(self):
        if self._calendar is None:
            self._calendar = MarketCalendarRepository(
                self.store).load_calendar()
        return self._calendar

    def load_signals(self, index_id=None, start=None, end=None,
                     signal_source=None):
        """신호를 (signal_id, 종목, 거래일, 시각, 상태)로 읽는다."""
        table = self.store.read_arrow(
            self.LAYER, "signals",
            columns=["signal_id", "security_id", "trading_date",
                     "signal_at_utc", "status", "index_id", "signal_source"],
            dedupe_pk=SIGNAL_PK)
        if table.num_rows == 0:
            raise NoInputDataError("신호가 없다 — `replay run` 먼저")

        listed = table.to_pylist()
        assert_single_signal_source(listed, signal_source,
                                    "signal-label build")

        out = []
        for row in listed:
            if index_id and row["index_id"] != index_id:
                continue
            if signal_source and row["signal_source"] != signal_source:
                continue
            day = row["trading_date"]
            if start is not None and day < start:
                continue
            if end is not None and day > end:
                continue
            out.append(row)
        if not out:
            raise NoInputDataError(
                "조건에 맞는 신호가 없다 — index/기간을 확인하라")
        out.sort(key=lambda r: (r["trading_date"], r["security_id"]))
        return out

    def load_panel(self, start, end):
        """무조건부 패널을 (종목, 날짜, horizon)으로 찾을 수 있게 올린다."""
        import pyarrow.dataset as ds

        expr = ((ds.field("entry_rule") == self.entry_rule)
                & (ds.field("label_version") == self.panel_version))
        if start is not None:
            expr = expr & (ds.field("signal_date") >= start)
        if end is not None:
            expr = expr & (ds.field("signal_date") <= end)

        table = self.store.read_arrow(
            self.LAYER, "daily_forward_returns",
            columns=list(_PANEL_COLUMNS), filters=expr,
            dedupe_pk=DAILY_FORWARD_RETURN_PK)
        if table.num_rows == 0:
            raise NoInputDataError(
                "무조건부 패널이 없다 — `forward build` 먼저")

        wanted = set(self.horizons)
        panel = {}
        for row in table.to_pylist():
            if row["horizon_td"] not in wanted:
                continue
            panel[(row["security_id"], row["signal_date"],
                   row["horizon_td"])] = row
        if not panel:
            raise NoInputDataError(
                f"패널에 horizon {sorted(wanted)} 행이 없다")
        return panel

    # ── 생성 ──────────────────────────────────────────────────

    def make_rows(self, signals, panel, run_id):
        """신호 × horizon 행을 만든다. 캘린더 대조와 불변식 검사를 포함한다."""
        calendar = self.calendar()
        rows, mismatches, missing = [], [], 0
        lookahead = []

        for signal in signals:
            sec = signal["security_id"]
            day = signal["trading_date"]
            signal_at = signal["signal_at_utc"]
            # SAME_CLOSE는 신호일 종가에 사므로 진입일이 신호일 자신이다.
            if self.entry_rule == "SAME_CLOSE":
                expected_entry = day
            else:
                expected_entry = (calendar.next_trading_date(day)
                                  if calendar.is_trading_day(day)
                                  or day <= calendar.end else None)

            for horizon in self.horizons:
                found = panel.get((sec, day, horizon))
                row = {
                    "signal_id": signal["signal_id"],
                    "horizon_td": int(horizon),
                    "entry_rule": self.entry_rule,
                    "security_id": sec,
                    "signal_date": day,
                    "signal_at_utc": signal_at,
                    "label_source": SIGNAL_LABEL_SOURCE,
                    "label_version": self.label_version,
                    "market_rule_version": MARKET_RULE_VERSION,
                    "run_id": run_id,
                    "schema_version": SCHEMA_VERSION,
                    "data_revision": self.data_revision,
                }
                if found is None:
                    # 신호는 냈는데 그 종목·날짜의 일봉이 패널에 없다. 진입가를
                    # 모르는 것이므로 만들어내지 않는다.
                    missing += 1
                    row.update({name: None for name in _CARRIED})
                    row["label_status"] = LabelStatus.ENTRY_UNAVAILABLE
                    row["status_reason"] = NO_PANEL_ROW
                    row["cost_model_version"] = "UNKNOWN"
                    row["calendar_checked"] = False
                    rows.append(row)
                    continue

                for name in _CARRIED:
                    row[name] = found[name]

                problem = _calendar_problem(calendar, day, horizon, found,
                                            self.entry_rule,
                                            expected_entry)
                # 날짜가 null인 행(성숙 전·가격 결측)은 대조할 것이 없다.
                row["calendar_checked"] = (problem is None
                                           and found["entry_date"] is not None)
                if problem is not None:
                    mismatches.append({
                        "signal_id": signal["signal_id"],
                        "security_id": sec, "signal_date": day.isoformat(),
                        "horizon_td": int(horizon), "problem": problem})

                entry_at = found["entry_at_utc"]
                same_day_ok = (self.confirmed_close_entry
                               and found["entry_date"] == day)
                if entry_at is not None and signal_at is not None \
                        and entry_at <= signal_at and not same_day_ok:
                    lookahead.append({
                        "signal_id": signal["signal_id"],
                        "horizon_td": int(horizon),
                        "signal_at_utc": signal_at.isoformat(),
                        "entry_at_utc": entry_at.isoformat()})
                rows.append(row)

        if lookahead:
            raise LookAheadError(
                f"진입 시각이 신호 시각보다 앞서거나 같은 행이 "
                f"{len(lookahead)}건 있다",
                context={"violations": lookahead[:20]})
        if mismatches:
            raise DataQualityError(
                f"거래 캘린더와 맞지 않는 진입·청산일이 {len(mismatches)}건 "
                f"있다 — 캘린더나 패널 중 하나가 틀렸다",
                context={"mismatches": mismatches[:20]})
        return rows, missing

    def build(self, run_id: str, index_id=None, start: date = None,
              end: date = None, signal_source=None, dry_run: bool = False):
        signals = self.load_signals(index_id, start, end, signal_source)
        days = [s["trading_date"] for s in signals]
        panel = self.load_panel(min(days), max(days))
        rows, missing = self.make_rows(signals, panel, run_id)
        checked = sum(1 for r in rows if r["calendar_checked"])

        status_counts, by_signal = {}, {}
        signal_status = {s["signal_id"]: s["status"] for s in signals}
        for row in rows:
            status = row["label_status"]
            status_counts[status] = status_counts.get(status, 0) + 1
            bucket = by_signal.setdefault(
                signal_status[row["signal_id"]], {})
            bucket[status] = bucket.get(status, 0) + 1

        writes = []
        if not dry_run:
            writes.append(self.store.write(
                self.LAYER, self.TABLE, rows, SIGNAL_FORWARD_RETURN_COLUMNS,
                SIGNAL_FORWARD_RETURN_PK, run_id,
                partition_fn=_partition,
                extra_manifest={
                    "label_version": self.label_version,
                    "label_source": SIGNAL_LABEL_SOURCE,
                    "entry_rule": "NEXT_OPEN",
                    "horizons": list(self.horizons),
                    "signals": len(signals),
                    "index_id": index_id,
                    "calendar_days": len(self.calendar()),
                }))

        return SignalLabelSummary(
            run_id, len(signals), len(rows), self.horizons,
            self.label_version, status_counts, by_signal,
            len(self.calendar()), checked, missing,
            min(days), max(days), writes)


def _calendar_problem(calendar, signal_date, horizon, panel_row,
                      entry_rule, expected_entry):
    """진입·청산일이 거래 캘린더와 맞는가. 맞으면 None, 아니면 사유 문자열.

    성숙 전이거나 가격이 없는 행은 날짜가 null이다. 그건 오류가 아니라 **모른다는
    표시**이므로 대조 대상이 아니다.
    """
    entry = panel_row["entry_date"]
    exit_date = panel_row["exit_date"]
    if entry is None:
        return None
    if expected_entry is not None and entry != expected_entry:
        where = ("신호일 자신" if entry_rule == "SAME_CLOSE"
                 else "다음 거래일")
        return (f"진입일이 {where}이 아니다: {entry} "
                f"(기대 {expected_entry})")
    if not calendar.is_trading_day(entry):
        return f"진입일이 거래일이 아니다: {entry}"
    if exit_date is None:
        return None
    if not calendar.is_trading_day(exit_date):
        return f"청산일이 거래일이 아니다: {exit_date}"
    # SAME_CLOSE는 진입일 종가에 사므로 그날 움직임에는 노출되지 않는다.
    # 노출을 h거래일로 맞추려면 h-1이 아니라 h를 민다.
    step = (horizon if entry_rule in ("SAME_CLOSE", "NEXT_CLOSE")
            else horizon - 1)
    expected_exit = calendar.shift(entry, step)
    if expected_exit is not None and exit_date != expected_exit:
        return (f"청산일이 진입일+{step}거래일이 아니다: {exit_date} "
                f"(기대 {expected_exit})")
    return None


def _partition(row):
    # 스키마 §7 — horizon_td / entry_year / entry_month.
    # 성숙 전이라 entry_date가 없는 행은 신호일로 나눈다. 파티션이 없는
    # 바구니에 몰아넣으면 나중에 그 행만 읽을 수 없다.
    day = row["entry_date"] or row["signal_date"]
    return [("horizon_td", row["horizon_td"]),
            ("entry_year", day.year),
            ("entry_month", f"{day.month:02d}")]

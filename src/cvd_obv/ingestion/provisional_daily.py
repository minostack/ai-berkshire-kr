# -*- coding: utf-8 -*-
"""당일 **잠정** 정규장 일봉 — 16:10에 1분봉만으로 만든다.

왜 필요한가
-----------
확정 정규장 일봉(`regular_from_minute`)은 애프터마켓 확정(20:05) 뒤에야 만들 수
있다. 거래량을 "EXTENDED − 애프터마켓 분봉"으로 빼기 때문이다. 그래서 확정은 다음
날 08:30에 돈다. 그 사이(당일 16:10 ~ 익일 08:30)에도 오늘 봉이 필요한 스캐너가
있어, 1분봉 합으로 잠정 봉을 먼저 만든다.

규칙 (`split_minutes`를 확정 복원과 같이 쓴다)
----------------------------------------------
- 시가   = 첫 REGULAR 1분봉 시가(09:00 시가 단일가)
- 고가·저가 = 정규장 1분봉(늦은 종가 체결 포함) 최고·최저
- 종가   = 종가 단일가 체결 봉(15:30 REGULAR 또는 15:30~15:35 OTHER) — 없으면 만들지 않는다
- 거래량 = 정규장 + 15:35~16:00 장후 시간외 종가 1분봉 합. 확정 봉(공식 거래량)과
           정의를 맞추려고 시간외 종가를 넣는다(2026-10-06 실측: 키움 일봉 거래량은
           정규장 + 시간외 종가). **정확하지 않다** — 1분봉이 빠뜨린 체결만큼 작다
           (9/1~10/2 대조 하위 1% 0.77배, 74%만 정확히 일치)
- 가격은 원주가 그대로다. 당일 봉에는 수정주가 조정이 없다(조정은 지난 봉에 걸린다)

저장
----
`price_scope=REGULAR`, `source=DERIVED_MINUTE_1M_PROVISIONAL`, **`data_revision=0`**,
`quality_status=PARTIAL`, 플래그 `PROVISIONAL_MINUTE_SUM_VOLUME`. 확정 복원 봉은 같은
논리 키에 리비전 1로 들어가므로 reader(`read_arrow(latest_by=…)`)는 run_id 순서와
무관하게 확정 봉을 고른다. 확정 봉이 이미 있는 종목은 잠정 봉을 쓰지 않는다.
"""

from __future__ import annotations

from datetime import date, datetime

from .. import SCHEMA_VERSION
from ..bar_standard import (
    DAILY_ADJUSTMENT, DAILY_PRICE_SCOPE, PROVISIONAL_REVISION,
    PROVISIONAL_SOURCE, is_provisional,
)
from ..contracts.market_schema import (
    DAILY_BAR_COLUMNS, DAILY_BAR_LOGICAL_KEY, DAILY_BAR_PK,
    ohlc_violations_of, validate_quarantined_ohlc, validate_series_order,
)
from ..domain.ids import UTC, to_utc
from ..domain.market_rules import default_registry
from ..domain.status import QualityStatus, Session
from ..storage.parquet_store import ParquetStore
from .regular_from_minute import (
    AFTERMARKET_START, CLOSE_PRINT_END_KST, FLAG_LATE_CLOSE_PRINT,
    FLAG_NO_VALUE, _dec, _partition, read_minutes, split_minutes,
)

__all__ = ["FLAG_PROVISIONAL", "derive_provisional_bar",
           "provisional_ready_at", "ProvisionalDailyBuilder"]

FLAG_PROVISIONAL = "PROVISIONAL_MINUTE_SUM_VOLUME"


def derive_provisional_bar(minutes: list):
    """(봉 | None, 사유, 플래그) — 한 종목·하루 1분봉으로 잠정 정규장 일봉."""
    reg, other, _after, reason = split_minutes(minutes)
    if reason != "OK":
        return None, reason, []
    # 15:35~16:00 장후 시간외 종가. 16:00 뒤 OTHER는 없지만(애프터마켓은 AFTER)
    # 시각으로 한 번 더 막는다.
    otc = [m for m in other if CLOSE_PRINT_END_KST <= m["kst"]
           and m["kst"].hour < 16]
    flags = [FLAG_PROVISIONAL, FLAG_NO_VALUE]
    if reg[-1]["session"] != "REGULAR":
        flags.append(FLAG_LATE_CLOSE_PRINT)
    bar = {
        "open": float(reg[0]["open"]),
        "high": max(float(m["high"]) for m in reg),
        "low": min(float(m["low"]) for m in reg),
        "close": float(reg[-1]["close"]),
        "volume": int(sum(int(m["volume"]) for m in reg + otc)),
    }
    bar["high"] = max(bar["high"], bar["open"], bar["close"])
    bar["low"] = min(bar["low"], bar["open"], bar["close"])
    return bar, "OK", sorted(flags)


def provisional_ready_at(day: date, registry=None, market: str = "KRX"):
    """잠정 봉을 만들 수 있는 시각(KST) — 애프터마켓 시작(16:00).

    장후 시간외 종가(~16:00)가 끝나야 거래량이 확정 봉 정의에 가깝다. 애프터마켓이
    없는 날(시행 전)은 None — 그날 일봉은 15:35에 확정되므로 잠정 봉이 필요 없다.
    """
    rule = (registry or default_registry()).resolve(market, day)
    if day < AFTERMARKET_START or rule.daily_only:
        return None
    bounds = rule.session_bounds(day, Session.AFTER)
    return bounds[0] if bounds else None


class ProvisionalDailyBuilder:
    """하루치 잠정 정규장 일봉을 `silver.daily_bars`에 쓴다."""

    def __init__(self, store: ParquetStore = None, registry=None,
                 market: str = "KRX"):
        self.store = store or ParquetStore()
        self.registry = registry or default_registry()
        self.market = market

    def _existing(self, day, securities):
        """그날 (확정 정규장 봉이 있는 종목, 잠정 봉만 있는 종목)."""
        import pyarrow as pa
        import pyarrow.compute as pc
        import pyarrow.dataset as ds
        from ..domain.errors import NoInputDataError

        try:
            table = self.store.read_arrow(
                "silver", "daily_bars",
                columns=["security_id", "trading_date", "price_scope",
                         "adjustment_type", "source", "run_id",
                         "data_revision"],
                filters=((ds.field("trading_date") == day)
                         & (ds.field("price_scope") == DAILY_PRICE_SCOPE)),
                latest_by=DAILY_BAR_LOGICAL_KEY)
        except NoInputDataError:
            return set(), set()
        table = table.filter(pc.is_in(table.column("security_id"),
                                      value_set=pa.array(sorted(securities))))
        final, prov = set(), set()
        for sid, src, adj in zip(table.column("security_id").to_pylist(),
                                 table.column("source").to_pylist(),
                                 table.column("adjustment_type").to_pylist()):
            if adj != DAILY_ADJUSTMENT:
                continue
            (prov if is_provisional(src) else final).add(sid)
        return final, prov - final

    def build_range(self, run_id: str, start: date, end: date, securities,
                    now: datetime = None, missing_only: bool = True,
                    dry_run: bool = False):
        """밀린 거래일들의 잠정 봉 — 확정 봉도 잠정 봉도 없는 종목만(기본).

        작업을 못 돌린 날을 메울 때 쓴다. 확정 복원이 곧 덮어쓰지만, 확정이
        실패하는 종목(애프터마켓 분봉·확정 일봉 누락 등)도 그날 봉을 갖게 된다.
        거래일은 저장소가 아는 날(기준 종목의 1분봉·일봉)이다 — 1분봉 catchup
        뒤에 돌려야 한다.
        """
        from .load_state import trading_days

        out = {"start": start.isoformat(), "end": end.isoformat(), "days": {},
               "written": 0, "targets": 0, "artifacts": []}
        if start > end:
            return out
        for day in trading_days(self.store, start, end):
            got = self.build(run_id, day, securities, now=now,
                             missing_only=missing_only, dry_run=dry_run)
            out["days"][day.isoformat()] = {
                k: got[k] for k in ("status", "targets", "written",
                                    "already_final", "already_provisional",
                                    "skipped")}
            out["written"] += got["written"]
            out["targets"] += got["targets"]
            out["artifacts"] += got["artifacts"]
        return out

    def build(self, run_id: str, day: date, securities, now: datetime = None,
              missing_only: bool = False, dry_run: bool = False):
        now = to_utc(now or datetime.now(tz=UTC))
        securities = set(securities)
        summary = {"date": day.isoformat(), "targets": 0, "written": 0,
                   "already_final": 0, "already_provisional": 0,
                   "skipped": {}, "skipped_samples": [],
                   "flags": {}, "artifacts": [], "status": "OK"}
        ready = provisional_ready_at(day, self.registry, self.market)
        if ready is None:
            summary["status"] = "NOT_NEEDED"
            return summary
        if now < to_utc(ready):
            summary["status"] = "TOO_EARLY"
            summary["ready_at"] = ready.isoformat()
            return summary

        final, prov = self._existing(day, securities)
        summary["already_final"] = len(final)
        skip = set(final)
        if missing_only:
            # 밀린 날 메우기: 이미 잠정 봉이 있으면 다시 만들지 않는다
            summary["already_provisional"] = len(prov)
            skip |= prov
        targets = sorted(securities - skip)
        summary["targets"] = len(targets)
        if not targets:
            return summary
        minutes = read_minutes(self.store, day, day, set(targets))
        if not minutes:
            # 휴장일이거나 그날 1분봉을 아직 받지 않았다 — 종목마다 사유를 쌓지 않는다
            summary["status"] = "NO_MINUTES"
            return summary

        rule = self.registry.resolve(self.market, day)
        # NXT 메인마켓 거래량(A안) — 16:10이면 NXT 메인마켓(~15:20)은 끝났다
        from .nxt_volume import apply_nxt, nxt_main_volume, nxt_traded
        nxt_vol = nxt_main_volume(self.store, day, day, targets)
        traded = nxt_traded(self.store, targets, day)
        rows = []
        for sid in targets:
            bar, reason, flags = derive_provisional_bar(
                minutes.get((sid, day), []))
            if bar is not None:
                bar["volume"], flags = apply_nxt(bar["volume"], flags, (sid, day),
                                                 nxt_vol, traded)
            if bar is None:
                summary["skipped"][reason] = summary["skipped"].get(reason, 0) + 1
                if len(summary["skipped_samples"]) < 20:
                    summary["skipped_samples"].append(
                        {"security_id": sid, "reason": reason})
                continue
            for flag in flags:
                summary["flags"][flag] = summary["flags"].get(flag, 0) + 1
            row = {
                "security_id": sid, "trading_date": day,
                "open": _dec(bar["open"]), "high": _dec(bar["high"]),
                "low": _dec(bar["low"]), "close": _dec(bar["close"]),
                "volume": bar["volume"], "traded_value": None,
                "price_scope": DAILY_PRICE_SCOPE,
                "adjustment_type": DAILY_ADJUSTMENT,
                "adjustment_factor": 1.0,
                "adjustment_as_of_utc": now,
                "available_at_utc": to_utc(ready),
                "source": PROVISIONAL_SOURCE,
                # 거래량이 근사값이라 SUCCESS로 두지 않는다
                "quality_status": QualityStatus.PARTIAL,
                "quality_flags": flags,
                "run_id": run_id,
                "market_rule_version": rule.version,
                "schema_version": SCHEMA_VERSION,
                "data_revision": PROVISIONAL_REVISION,
            }
            if ohlc_violations_of(row):
                summary["skipped"]["OHLC_VIOLATION"] = \
                    summary["skipped"].get("OHLC_VIOLATION", 0) + 1
                continue
            rows.append(row)

        summary["written"] = len(rows)
        if rows and not dry_run:
            written = self.store.write(
                "silver", "daily_bars", rows, DAILY_BAR_COLUMNS, DAILY_BAR_PK,
                run_id, partition_fn=_partition,
                validators=[validate_quarantined_ohlc, validate_series_order],
                extra_manifest={"source": PROVISIONAL_SOURCE,
                                "derived_from": ["silver.minute_bars:1m"],
                                "requested_date": day.isoformat()})
            summary["artifacts"] = [written.manifest_path]
        return summary

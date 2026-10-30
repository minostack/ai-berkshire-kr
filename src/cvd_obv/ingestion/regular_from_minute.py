# -*- coding: utf-8 -*-
"""정규장 일봉을 1분봉으로 복원한다 — 2026-09-14 애프터마켓 도입 이후.

키움 일봉(ka10081)은 2026-09-14부터 애프터마켓까지 반영한 `EXTENDED`로만
온다(종가 = 애프터마켓 최종 체결가, 거래량에 애프터마켓 포함). 그 이전 일봉은
정규장 기준(`REGULAR`)이라 같은 열에 두 정의가 섞인다. RVOL·52주 고가처럼
일봉 시계열을 쓰는 지표는 이 경계를 넘으면 정의가 바뀐다.

2026-10-23부터 적재 원천은 KIS다. KIS 일봉은 9/14 이후 가격이 정규장이고 거래량만
애프터마켓 포함이라 `EXTENDED`로 저장된다(`kis_daily` 머리말) — 아래 규칙은 그대로
맞는다(고가·저가가 이미 정규장 값이면 분봉 쪽을 고르는 분기로 같은 값이 나온다).

이 모듈은 같은 날의 EXTENDED 일봉과 1분봉으로 `REGULAR` 일봉을 만들어
`silver.daily_bars`에 `source=DERIVED_MINUTE_1M`으로 넣는다. 원본 EXTENDED 행은
지우지 않는다.

복원 규칙 (2026-09 실측 근거, `docs/STAGE_STATUS_V2.md` 참고)
--------------------------------------------------------------
- 시가   = EXTENDED 시가. 09:00 시가는 애프터마켓과 무관하다.
- 종가   = 종가 단일가 체결 봉의 종가 — 15:30 REGULAR 봉, 체결이 늦으면 15:30~15:35
           OTHER 봉(2026-10-03 `tools/closing_bet_scanner.py`의 즉석 복원에서 옮겨 와
           복원 규칙을 하나로 합쳤다). 9/1~9/11 REGULAR 일봉과 대조해 15:30 봉이
           있으면 3,511/3,511 일치, 없으면(분봉이 15:19에서 끊긴 55건) 전부
           불일치 → **종가 체결 봉이 없으면 만들지 않는다**.
- 고가   = EXTENDED 고가가 애프터마켓 분봉 최고가보다 높으면 그 값(정규장에서
           나온 고가), 아니면 분봉 REGULAR 최고가. 저가도 대칭.
- 거래량 = EXTENDED 거래량 − 애프터마켓 분봉 거래량. 9/14~9/30 실측에서 이 값은
           항상 분봉 REGULAR+OTHER 합 이상이었다(분봉이 빠뜨리는 체결이 있어도
           뺄셈은 영향받지 않는다).
- 거래대금 = null. 분봉이 거래대금을 주지 않아(0) 정규장 몫을 알 수 없다.
           스키마가 "계산으로 채우지 않는다"고 정한 열이다.

분봉을 애프터마켓 확정(20:00 + 여유) **전에** 받았으면 애프터마켓 봉이 덜 들어
있어 뺄셈 거래량이 부푼다. 분봉 행에는 수집 시각 열이 없으므로 `run_id`에 박힌
UTC 시각(`<kind>_<yyyymmddHHMMSS>_<nonce>`, 스키마 §2)으로 판정한다. 16:10에 도는
일일 분봉 누적(`minute-cache catchup`)이 받은 날은 다음 수집 때까지 만들지 않는다
(catchup이 그런 날을 다시 받는다). EXTENDED 일봉도 같다 — 20:00 전에 받은 일봉은
애프터마켓 거래량이 덜 들어 있으므로 수집 시각(`adjustment_as_of_utc`)으로 거른다.

분봉은 수정주가가 아닐 수 있다. 시가 비율 `f = EXTENDED 시가 / 분봉 첫 시가`를
조정계수로 보고 분봉 가격에 곱한다(실측: 207940 2026-09-28~30 f=1.00776).
"""

from __future__ import annotations

from datetime import date, datetime, time

from .. import SCHEMA_VERSION
from ..contracts.market_schema import (
    DAILY_BAR_COLUMNS, DAILY_BAR_LOGICAL_KEY, DAILY_BAR_PK,
    ohlc_violations_of, validate_quarantined_ohlc, validate_series_order,
)
from ..bar_standard import is_provisional
from .nxt_volume import (
    FLAG_NXT_INCLUDED, FLAG_NXT_RESCALED, apply_nxt, nxt_main_volume, nxt_traded,
)
from ..domain.ids import KST as _KST, UTC, run_started_at, to_utc
from ..domain.market_rules import default_registry
from ..domain.status import QualityStatus
from ..storage.parquet_store import ParquetStore

__all__ = ["DERIVED_SOURCE", "AFTERMARKET_START", "derive_regular_bar",
           "split_minutes", "read_minutes", "RegularFromMinuteBuilder"]

DERIVED_SOURCE = "DERIVED_MINUTE_1M"
#: 확정 봉 리비전 — 1: KRX 거래량만(2026-10-20 전), 2: NXT 메인마켓 거래량 규칙 이후
FINAL_REVISION = 2
AFTERMARKET_START = date(2026, 9, 14)
#: 종가 단일가 봉(KST 시작 시각). 이 봉이 없으면 정규장 종가를 알 수 없다.
CLOSE_AUCTION_KST = time(15, 30)
#: 이 시각부터는 장후 시간외 종가매매 — 종가 단일가 체결로 보지 않는다.
CLOSE_PRINT_END_KST = time(15, 35)
#: 분봉 정규장 합이 뺄셈 거래량보다 이만큼 넘게 작으면 PARTIAL로 표시한다.
VOLUME_GAP_TOL = 0.02

FLAG_ADJUSTED = "MINUTE_PRICE_RESCALED"
FLAG_VOLUME_GAP = "MINUTE_REGULAR_VOLUME_GAP"
FLAG_NO_VALUE = "TRADED_VALUE_UNAVAILABLE"
FLAG_FETCH_UNKNOWN = "MINUTE_FETCH_TIME_UNKNOWN"
FLAG_LATE_CLOSE_PRINT = "LATE_CLOSE_PRINT"

#: run_id의 실행 시각 — 분봉 행에는 수집 시각 열이 없다.
fetched_at_of = run_started_at


def split_minutes(minutes: list):
    """한 종목·하루 1분봉 → (정규장, 그 밖 OTHER, 애프터마켓, 사유).

    정규장 = REGULAR 봉 + 늦게 찍힌 종가 단일가 체결(15:30~15:35 OTHER). 종가
    단일가 체결은 보통 15:30 REGULAR 봉이지만, 체결이 늦어지면 15:30~15:35 사이
    시각으로 찍혀 OTHER로 분류된다(실측 2026-09-17 삼성전기 15:32 OTHER = 공식
    종가). 15:35부터는 시간외 종가매매라 정규장이 아니다 — `other`에 남는다.
    사유가 "OK"가 아니면 정규장 봉을 만들 수 없다(종가를 모른다).
    """
    late = [m for m in minutes if m["session"] == "OTHER"
            and CLOSE_AUCTION_KST <= m["kst"] < CLOSE_PRINT_END_KST]
    reg = sorted([m for m in minutes if m["session"] == "REGULAR"] + late,
                 key=lambda m: m["kst"])
    after = [m for m in minutes if m["session"] == "AFTER"]
    other = [m for m in minutes if m["session"] == "OTHER"
             and not any(m is x for x in late)]
    if not [m for m in reg if m["session"] == "REGULAR"]:
        return reg, other, after, "NO_REGULAR_MINUTES"
    if not (CLOSE_AUCTION_KST <= reg[-1]["kst"] < CLOSE_PRINT_END_KST):
        return reg, other, after, "NO_CLOSE_AUCTION_BAR"
    return reg, other, after, "OK"


def read_minutes(store, start, end, securities):
    """{(sid, date): [분봉…]} — 1분봉, 최신 리비전만, FAILED 제외."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.dataset as ds
    from ..contracts.market_schema import MINUTE_BAR_LOGICAL_KEY

    flt = ((ds.field("period_min") == 1)
           & (ds.field("trading_date") >= start)
           & (ds.field("trading_date") <= end))
    table = store.read_arrow(
        "silver", "minute_bars",
        columns=["security_id", "period_min", "trading_date", "session",
                 "bar_start_at_utc", "open", "high", "low", "close",
                 "volume", "quality_status", "run_id", "data_revision"],
        filters=flt, latest_by=MINUTE_BAR_LOGICAL_KEY)
    table = table.filter(pc.is_in(table.column("security_id"),
                                  value_set=pa.array(sorted(securities))))
    out = {}
    for r in table.to_pylist():
        if r["quality_status"] == QualityStatus.FAILED:
            continue
        kst = r["bar_start_at_utc"].astimezone(_KST).time()
        out.setdefault((r["security_id"], r["trading_date"]), []).append(
            {"kst": kst, "session": r["session"], "open": r["open"],
             "high": r["high"], "low": r["low"], "close": r["close"],
             "volume": r["volume"],
             "fetched_at": fetched_at_of(r["run_id"])})
    return out


def derive_regular_bar(ext: dict, minutes: list, after_final_at=None,
                       day_fetched_at=None):
    """EXTENDED 일봉 하나 + 그날 1분봉 → (정규장 봉 dict | None, 사유, 플래그).

    `ext`: open/high/low/close/volume (숫자).
    `minutes`: [{"kst": time, "session": str, "open", "high", "low", "close",
                 "volume", "fetched_at": datetime|None}] — 한 종목·하루,
                 리비전 중복 제거된 것.
    `after_final_at`: 애프터마켓 확정 시각(UTC). 주면 그 전에 받은 분봉으로는
                 만들지 않는다.
    `day_fetched_at`: 그날을 **마지막으로 받은** 시각(UTC, 정규장 커버리지 —
                 `MinuteCatchup.last_fetched_since`). 2026-10-16부터 일일 누적은
                 정규장 봉을 16:10에, 애프터마켓 봉을 다음 날 08:30에 나눠 저장하므로
                 정규장 봉의 수집 시각은 확정 전이 정상이다. 이 값이 확정 뒤면
                 애프터마켓 봉이 다 있다. 없으면(커버리지 없는 옛 데이터) 예전처럼
                 분봉 행 중 가장 이른 수집 시각으로 판정한다.
    """
    reg, other, after, reason = split_minutes(minutes)
    if reason != "OK":
        return None, reason, []

    flags = [FLAG_NO_VALUE]
    if reg[-1]["session"] != "REGULAR":
        flags.append(FLAG_LATE_CLOSE_PRINT)
    if after_final_at is not None and day_fetched_at is not None:
        if day_fetched_at < after_final_at:
            return None, "AFTER_MARKET_NOT_FINAL", []
    elif after_final_at is not None:
        fetched = [m.get("fetched_at") for m in minutes]
        if any(f is None for f in fetched):
            flags.append(FLAG_FETCH_UNKNOWN)
        known = [f for f in fetched if f is not None]
        if known and min(known) < after_final_at:
            return None, "AFTER_MARKET_NOT_FINAL", []
    first_open = float(reg[0]["open"])
    if first_open <= 0:
        return None, "BAD_MINUTE_OPEN", []
    f = float(ext["open"]) / first_open
    if abs(f - 1.0) > 1e-4:
        flags.append(FLAG_ADJUSTED)

    reg_high = max(float(m["high"]) for m in reg) * f
    reg_low = min(float(m["low"]) for m in reg) * f
    after_high = max((float(m["high"]) for m in after), default=None)
    after_low = min((float(m["low"]) for m in after), default=None)
    ext_high, ext_low = float(ext["high"]), float(ext["low"])
    # EXTENDED 고가가 애프터마켓에서 나오지 않았다면 정규장(또는 장후
    # 단일가)에서 나온 값이다 — 분봉 누락에 영향받지 않는 쪽을 쓴다.
    high = (ext_high if after_high is None or ext_high > after_high * f
            else reg_high)
    low = (ext_low if after_low is None or ext_low < after_low * f
           else reg_low)
    close = float(reg[-1]["close"]) * f
    open_ = float(ext["open"])
    high = max(high, open_, close)
    low = min(low, open_, close)

    after_vol = sum(int(m["volume"]) for m in after)
    volume = int(ext["volume"]) - int(round(after_vol / f))
    if volume < 0:
        return None, "NEGATIVE_VOLUME", []
    minute_vol = sum(int(m["volume"]) for m in reg + other) / f
    if volume and (volume - minute_vol) / volume > VOLUME_GAP_TOL:
        flags.append(FLAG_VOLUME_GAP)

    bar = {"open": open_, "high": high, "low": low, "close": close,
           "volume": volume, "adjustment_factor": f}
    return bar, "OK", sorted(flags)


class RegularFromMinuteBuilder:
    """EXTENDED만 있는 날짜에 정규장 일봉을 만들어 `silver.daily_bars`에 쓴다."""

    def __init__(self, store: ParquetStore = None, registry=None,
                 market: str = "KRX"):
        self.store = store or ParquetStore()
        self.registry = registry or default_registry()
        self.market = market

    def _targets(self, start, end, securities):
        """(대상, NXT 미포함 확정) — 둘 다 (종목, 날짜) → EXTENDED 행.

        대상은 확정 REGULAR가 없는 날, 그리고 복원 봉 시가가 지금 원본 시가와 다른 날
        (원본이 수정주가 소급으로 다시 받아졌다)이다. NXT 미포함 확정은 확정 봉은 있지만
        `NXT_MAIN_VOLUME_INCLUDED`가 없는 날 — 그날 NXT 1분봉이 있으면 다시 만든다.
        """
        import pyarrow.compute as pc
        import pyarrow.dataset as ds

        flt = ((ds.field("trading_date") >= start)
               & (ds.field("trading_date") <= end)
               & (ds.field("adjustment_type") == "SPLIT_ADJUSTED"))
        table = self.store.read_arrow(
            "silver", "daily_bars",
            columns=["security_id", "trading_date", "open", "high", "low",
                     "close", "volume", "price_scope", "quality_status",
                     "quality_flags", "adjustment_as_of_utc", "source",
                     "run_id", "data_revision"],
            filters=flt, latest_by=DAILY_BAR_LOGICAL_KEY)
        if securities is not None:
            import pyarrow as pa
            table = table.filter(pc.is_in(table.column("security_id"),
                                          value_set=pa.array(sorted(securities))))
        rows = table.to_pylist()
        # 잠정 봉(16:10, 1분봉 합)은 확정이 아니다 — 그날도 복원 대상이다.
        # 복원 봉은 리비전 1·2, 잠정 봉은 0이라 쓰고 나면 reader가 복원 봉을 고른다.
        regular = {(r["security_id"], r["trading_date"]): r for r in rows
                   if r["price_scope"] == "REGULAR"
                   and not is_provisional(r["source"])}
        ext = {(r["security_id"], r["trading_date"]): r for r in rows
               if r["price_scope"] == "EXTENDED"
               and r["quality_status"] != QualityStatus.FAILED}
        targets = {k: r for k, r in ext.items() if k not in regular}
        # 원본이 수정주가 소급으로 다시 받아진 날 — 복원 봉 시가는 원본 시가 그대로이므로
        # 둘이 다르면 옛 원본으로 만든 봉이다(2026-10-21 실측: 207940 10/02 이벤트)
        for k, r in regular.items():
            if (k in ext and r["source"] == DERIVED_SOURCE
                    and abs(float(r["open"]) - float(ext[k]["open"])) > 1e-6):
                targets[k] = ext[k]
        # 수정주가 계수가 걸렸는데 NXT 몫을 계수 없이 더한 옛 봉(2026-10-26 이전 규칙)
        for k, r in regular.items():
            fl = r["quality_flags"] or []
            if (k in ext and r["source"] == DERIVED_SOURCE and FLAG_ADJUSTED in fl
                    and FLAG_NXT_INCLUDED in fl and FLAG_NXT_RESCALED not in fl):
                targets[k] = ext[k]
        # NXT 거래량이 아직 안 들어간 확정 봉 — NXT 1분봉이 생기면 다시 만든다
        without_nxt = {k: ext[k] for k, r in regular.items()
                       if k in ext and FLAG_NXT_INCLUDED not in (r["quality_flags"] or [])}
        return targets, without_nxt

    def _minutes(self, start, end, securities):
        """{(sid, date): [분봉…]} — 1분봉, 최신 리비전만."""
        return read_minutes(self.store, start, end, securities)

    def build(self, run_id: str, start: date = None, end: date = None,
              securities=None, dry_run: bool = False):
        start = max(start or AFTERMARKET_START, AFTERMARKET_START)
        end = end or date.today()
        targets, without_nxt = self._targets(start, end, securities)
        # NXT 메인마켓 거래량(A안, `nxt_volume`) — 받은 쌍만 값이 있다
        sids = {sid for sid, _ in list(targets) + list(without_nxt)}
        nxt_vol = nxt_main_volume(self.store, start, end, sids) if sids else {}
        traded = nxt_traded(self.store, sids, end) if sids else set()
        upgrades = {k: r for k, r in without_nxt.items() if k in nxt_vol}
        targets = {**upgrades, **targets}
        summary = {"start": start.isoformat(), "end": end.isoformat(),
                   "targets": len(targets), "nxt_upgrades": len(upgrades),
                   "written": 0, "skipped": {},
                   "flags": {}, "skipped_samples": [], "artifacts": []}
        if not targets:
            return summary
        minutes = self._minutes(start, end, {sid for sid, _ in targets})
        from .minute_catchup import MinuteCatchup
        last_fetched = MinuteCatchup(store=self.store).last_fetched_since(
            1, start, end)

        now = datetime.now(tz=UTC)
        rows = []
        for (sid, day), ext in sorted(targets.items()):
            rule = self.registry.resolve(self.market, day)
            after_final = to_utc(rule.daily_confirmed_at(day))
            got_at = ext.get("adjustment_as_of_utc")
            if got_at is not None and got_at < after_final:
                bar, reason, flags = None, "EXTENDED_NOT_FINAL", []
            else:
                bar, reason, flags = derive_regular_bar(
                    ext, minutes.get((sid, day), []),
                    after_final_at=after_final,
                    day_fetched_at=last_fetched.get((sid, day)))
            if bar is None:
                summary["skipped"][reason] = summary["skipped"].get(reason, 0) + 1
                if len(summary["skipped_samples"]) < 20:
                    summary["skipped_samples"].append(
                        {"security_id": sid, "date": day.isoformat(),
                         "reason": reason})
                continue
            bar["volume"], flags = apply_nxt(bar["volume"], flags, (sid, day),
                                             nxt_vol, traded,
                                             factor=bar["adjustment_factor"])
            for flag in flags:
                summary["flags"][flag] = summary["flags"].get(flag, 0) + 1
            status = (QualityStatus.PARTIAL if FLAG_VOLUME_GAP in flags
                      else QualityStatus.SUCCESS)
            row = {
                "security_id": sid, "trading_date": day,
                "open": _dec(bar["open"]), "high": _dec(bar["high"]),
                "low": _dec(bar["low"]), "close": _dec(bar["close"]),
                "volume": bar["volume"], "traded_value": None,
                "price_scope": "REGULAR",
                "adjustment_type": "SPLIT_ADJUSTED",
                "adjustment_factor": bar["adjustment_factor"],
                "adjustment_as_of_utc": now,
                # 애프터마켓 분봉까지 써서 만들므로 애프터마켓 확정 뒤에야 안다
                "available_at_utc": to_utc(rule.daily_confirmed_at(day)),
                "source": DERIVED_SOURCE,
                "quality_status": status,
                "quality_flags": flags,
                "run_id": run_id,
                "market_rule_version": rule.version,
                "schema_version": SCHEMA_VERSION,
                # 2 = NXT 메인마켓 거래량 규칙(A안) 이후의 확정 봉. 예전 KRX만인 확정
                # 봉(1)과 같은 날이면 이것이 이긴다
                "data_revision": FINAL_REVISION,
            }
            if ohlc_violations_of(row):
                summary["skipped"]["OHLC_VIOLATION"] = \
                    summary["skipped"].get("OHLC_VIOLATION", 0) + 1
                continue
            rows.append(row)

        summary["written"] = len(rows)
        if rows and not dry_run:
            rows.sort(key=lambda r: (r["security_id"], r["trading_date"]))
            written = self.store.write(
                "silver", "daily_bars", rows, DAILY_BAR_COLUMNS, DAILY_BAR_PK,
                run_id, partition_fn=_partition,
                validators=[validate_quarantined_ohlc, validate_series_order],
                extra_manifest={"source": DERIVED_SOURCE,
                                "derived_from": ["silver.daily_bars:EXTENDED",
                                                 "silver.minute_bars:1m"],
                                "requested_start": start.isoformat(),
                                "requested_end": end.isoformat()})
            summary["artifacts"] = [written.manifest_path]
        return summary


def _partition(row):
    d = row["trading_date"]
    return [("market", "KRX"), ("year", d.year), ("month", f"{d.month:02d}")]


def _dec(x: float):
    from decimal import Decimal
    return Decimal(str(round(x, 4)))


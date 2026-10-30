# -*- coding: utf-8 -*-
"""입력 — 기적재 데이터만 읽는다. API를 부르지 않고 저장소에 쓰지 않는다.

| 자료 | 원천 | D 선정에 쓰는 범위 |
|---|---|---|
| 일봉 | `read_standard_daily`(정규장·수정주가) | D-1까지 확정 + D 봉(16:10 잠정 또는 확정) |
| 체결량 | `read_sides_flow`(sides-1.0) | D-1까지, **확정 행만** |
| 1분봉 | `silver.minute_bars` period_min=1 | D 하루, KRX·NXT |
| 명단 | `universe_on(D)` | D 당시 구성 |
"""

from __future__ import annotations

import glob
import os
from datetime import date, datetime, timedelta

from . import _BASE  # noqa: F401 — src 경로 등록

from cvd_obv.bar_standard import (  # noqa: E402
    PROVISIONAL_SOURCE, read_standard_daily,
)
from cvd_obv.contracts.market_schema import (  # noqa: E402
    BAR_COVERAGE_PK, MINUTE_BAR_LOGICAL_KEY,
)
from cvd_obv.domain.ids import KST  # noqa: E402
from cvd_obv.storage.parquet_store import ParquetStore  # noqa: E402

#: 일봉 워밍업(달력일) — MA60 + MA20(D-5) + ATR14 여유
LOOKBACK_DAYS = 140

#: 일봉 품질 플래그(bars-1.0)
FLAG_NXT_INCLUDED = "NXT_MAIN_VOLUME_INCLUDED"
FLAG_NXT_MISSING = "NXT_VOLUME_MISSING"


def latest_daily_date(store=None, today: date = None) -> date:
    """기준 일봉의 마지막 거래일(오늘 이전·당일 포함)."""
    import pyarrow.dataset as ds
    today = today or date.today()
    t = read_standard_daily(
        store=store, columns=["security_id", "trading_date"],
        filters=(ds.field("trading_date") > today - timedelta(days=30))
        & (ds.field("trading_date") <= today) & (ds.field("security_id") == "KRX:005930"))
    days = sorted(set(t.column("trading_date").to_pylist()))
    if not days:
        raise SystemExit("최근 30일 기준 일봉이 없다 — 적재 상태를 확인하라")
    return days[-1]


# ─────────────────────────────────────────────────────────────
# 일봉
# ─────────────────────────────────────────────────────────────

def load_daily(sids, start: date, end: date, store=None) -> dict:
    """{코드: [봉 dict …]} 오래된 순. 품질 FAILED는 뺀다."""
    import pyarrow.dataset as ds
    t = read_standard_daily(
        store=store,
        columns=["security_id", "trading_date", "open", "high", "low", "close",
                 "volume", "traded_value", "source", "quality_flags",
                 "quality_status", "data_revision"],
        filters=(ds.field("trading_date") >= start)
        & (ds.field("trading_date") <= end)
        & ds.field("security_id").isin(sorted(sids)),
        drop_failed=True, fill_traded_value=True)
    out = {}
    for r in t.to_pylist():
        out.setdefault(r["security_id"].split(":")[1], []).append({
            "date": r["trading_date"],
            "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]),
            "volume": int(r["volume"]),
            "value": float(r["traded_value"]) if r["traded_value"] is not None else None,
            "value_est": bool(r.get("traded_value_est")),
            "source": r["source"],
            "provisional": r["source"] == PROVISIONAL_SOURCE,
            "flags": list(r["quality_flags"] or []),
            "revision": r["data_revision"],
        })
    for rows in out.values():
        rows.sort(key=lambda x: x["date"])
    return out


def trading_calendar(daily: dict, end: date) -> list:
    """저장 일봉에 나타난 거래일(종목 합집합). 휴장일은 봉이 없다."""
    return sorted({b["date"] for rows in daily.values() for b in rows
                   if b["date"] <= end})


# ─────────────────────────────────────────────────────────────
# 체결량 — 확정 행 + 수신 시각
# ─────────────────────────────────────────────────────────────

def load_flows(sids, start: date, end: date, store=None) -> dict:
    """{코드: {거래일: SidesDay}} — sides-1.0 통합 기준."""
    from cvd_obv.sides_flow import read_sides_flow
    return read_sides_flow(sids, start, end, store=store)


def load_flow_available_at(sids, days, store=None) -> dict:
    """{(코드, 거래일): 최초 이용 가능 시각(UTC)} — 그날 쓰인 행의 `available_at_utc`.

    같은 날 여러 구분(UN/J/NX)·리비전이 있으면 **가장 늦은** 시각을 쓴다(보수적).
    """
    import pyarrow.dataset as ds
    from cvd_obv.ingestion.kis_trade_sides import read_trade_sides
    if not days:
        return {}
    t = read_trade_sides(
        store=store,
        columns=["security_id", "trading_date", "market_div", "available_at_utc"],
        filters=ds.field("trading_date").isin(sorted(days))
        & ds.field("security_id").isin(sorted(sids)))
    out = {}
    for sid, d, at in zip(t.column("security_id").to_pylist(),
                          t.column("trading_date").to_pylist(),
                          t.column("available_at_utc").to_pylist()):
        k = (sid.split(":")[1], d)
        if at is not None and (k not in out or at > out[k]):
            out[k] = at
    return out


# ─────────────────────────────────────────────────────────────
# 1분봉 — D 하루만, 해당 월 partition만 연다
# ─────────────────────────────────────────────────────────────

def _minute_files(store: ParquetStore, day: date) -> list:
    base = store.table_dir("silver", "minute_bars")
    pat = os.path.join(base, "period_min=1", f"year={day.year}",
                       f"month={day.month:02d}", "**", "*.parquet")
    return sorted(glob.glob(pat, recursive=True))


def load_minutes(codes, day: date, store=None) -> dict:
    """{(\"KRX\"|\"NXT\", 코드): [1분봉 …]} — 최신 리비전만, FAILED 제외.

    `ParquetStore.read_arrow`는 테이블 전체 파일을 연다(수 GB). 여기서는 D가 든
    월 partition만 열고, 중복 제거는 저장소와 **같은 규칙**(`_dedupe_arrow`)을 쓴다.
    """
    import pyarrow as pa
    import pyarrow.dataset as ds

    store = store or ParquetStore()
    ids = sorted({f"KRX:{c}" for c in codes} | {f"NXT:{c}" for c in codes})
    cols = ["security_id", "period_min", "trading_date", "session",
            "bar_start_at_utc", "open", "high", "low", "close", "volume",
            "traded_value", "cum_traded_value", "source", "quality_status",
            "run_id", "data_revision"]
    flt = (ds.field("trading_date") == day) & ds.field("security_id").isin(ids)
    tables = []
    for p in _minute_files(store, day):
        dset = ds.dataset(p, format="parquet", partitioning=None)
        have = set(dset.schema.names)
        got = dset.to_table(columns=[c for c in cols if c in have], filter=flt)
        if not got.num_rows:
            continue
        # 2026-10-23 전 파일(키움)에는 cum_traded_value 열이 없다 — null로 채운다
        for c in cols:
            if c not in have:
                got = got.append_column(c, pa.nulls(got.num_rows))
        tables.append(got.select(cols))
    if not tables:
        return {}
    t = pa.concat_tables(tables, promote_options="permissive")
    t = store._dedupe_arrow(t, tuple(MINUTE_BAR_LOGICAL_KEY) + ("data_revision",))
    t = store._dedupe_arrow(t, tuple(MINUTE_BAR_LOGICAL_KEY))
    out = {}
    for r in t.to_pylist():
        if r["quality_status"] == "FAILED":
            continue
        venue, code = r["security_id"].split(":")
        out.setdefault((venue, code), []).append({
            "kst": r["bar_start_at_utc"].astimezone(KST).time(),
            "session": r["session"],
            "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]),
            "volume": int(r["volume"]),
            "traded_value": (float(r["traded_value"])
                             if r["traded_value"] is not None else None),
            "cum_traded_value": (float(r["cum_traded_value"])
                                 if r["cum_traded_value"] is not None else None),
            "source": r["source"],
        })
    for rows in out.values():
        rows.sort(key=lambda b: b["kst"])
        # KIS 수집 지평 첫날(2025-10-15)은 적재기가 첫 봉 거래대금을 비워 둔다(그 앞 봉을
        # 받았는지 몰라서, `KIS_FIRST_BAR_VALUE_UNKNOWN`). 다른 날 첫 봉 거래대금도 그날 첫
        # 누적값이므로 같은 정의로 채운다. KRX는 09:00 전 봉이 없어 첫 봉이 그날 첫 봉이다.
        first = rows[0]
        if first["traded_value"] is None and first["cum_traded_value"] is not None:
            first["traded_value"] = first["cum_traded_value"]
            first["value_from_first_cum"] = True
    return out


_TOTAL_COLS = ["security_id", "trading_date", "session", "bar_start_at_utc", "high",
               "low", "close", "volume", "traded_value", "cum_traded_value", "run_id",
               "data_revision", "quality_status"]


def load_minute_day_totals(codes, start: date, end: date, store=None) -> dict:
    """{(\"KRX\"|\"NXT\", 코드, 거래일): 합계 dict} — 1분봉 범위 거래량·거래대금.

    범위는 확정 일봉(bars-1.0)과 같다. KRX = 애프터마켓(AFTER)을 뺀 봉(정규장·늦은 종가
    체결·장후 시간외 종가), NXT = 메인마켓 09:00~15:30. 일봉에만 있는 물량(블록딜 추정)은
    1분봉에 없으므로 여기 합계에도 없다.

    | 키 | 뜻 |
    |---|---|
    | volume | 거래량 합 |
    | value | 거래대금 있는 봉의 거래대금 합(시간외 종가 봉은 거래량 × 종가) |
    | unvalued_volume | 거래대금 없는 봉의 거래량 합 |
    | typ_unvalued | 거래대금 없는 봉의 Σ (H+L+C)/3 × 거래량 — 누적거래대금 없는 날 추정용 |
    | last_close | KRX 마지막 봉 종가(원주가) — 일봉 수정 계수 확인용 |

    1분봉은 수정주가가 아니다(원주가·원거래량). 일봉과 맞추는 일은 쓰는 쪽이 한다.

    중복 제거는 저장소 규칙(최신 리비전 → 나중 run_id)과 같다. D마다 Python 행을
    만들지 않고 월 partition별 Arrow 집계로 처리한다.
    """
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.dataset as ds

    store = store or ParquetStore()
    ids = sorted({f"KRX:{c}" for c in codes} | {f"NXT:{c}" for c in codes})
    base = store.table_dir("silver", "minute_bars")
    keep_nulls = pc.ScalarAggregateOptions(skip_nulls=False)
    flt = ((ds.field("trading_date") >= start) & (ds.field("trading_date") <= end)
           & ds.field("security_id").isin(ids))
    out = {}
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        parts = []
        for p in sorted(glob.glob(os.path.join(
                base, "period_min=1", f"year={y}", f"month={m:02d}", "**", "*.parquet"),
                recursive=True)):
            dset = ds.dataset(p, format="parquet", partitioning=None)
            have = set(dset.schema.names)
            t = dset.to_table(columns=[c for c in _TOTAL_COLS if c in have], filter=flt)
            if not t.num_rows:
                continue
            for c in _TOTAL_COLS:
                if c not in have:
                    t = t.append_column(c, pa.nulls(t.num_rows))
            # decimal은 first/last 집계를 받지 않는다 — 원 단위라 float64로 바꿔도 같다
            for c in ("high", "low", "close", "traded_value", "cum_traded_value"):
                t = t.set_column(t.schema.get_field_index(c), c,
                                 pc.cast(t.column(c), pa.float64()))
            parts.append(t.select(_TOTAL_COLS))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        if not parts:
            continue
        t = pa.concat_tables(parts, promote_options="permissive")
        t = t.filter(pc.not_equal(t.column("quality_status"), "FAILED"))
        # 같은 봉 키(MINUTE_BAR_LOGICAL_KEY)에서 최신 리비전·나중 run_id 한 행
        t = t.sort_by([("security_id", "ascending"), ("bar_start_at_utc", "ascending"),
                       ("session", "ascending"), ("data_revision", "ascending"),
                       ("run_id", "ascending")])
        last = [(c, "last", keep_nulls) for c in
                ("trading_date", "high", "low", "close", "volume", "traded_value",
                 "cum_traded_value")]
        t = t.group_by(["security_id", "bar_start_at_utc", "session"],
                       use_threads=False).aggregate(last)
        t = t.rename_columns([n.removesuffix("_last") for n in t.column_names])
        utc_min = pc.add(pc.multiply(pc.hour(t.column("bar_start_at_utc")), 60),
                         pc.minute(t.column("bar_start_at_utc")))   # KST − 9h
        is_nxt = pc.starts_with(t.column("security_id"), "NXT:")
        krx_keep = pc.and_(pc.invert(is_nxt), pc.not_equal(t.column("session"), "AFTER"))
        nxt_keep = pc.and_(is_nxt, pc.less(utc_min, 6 * 60 + 30))  # KST 09:00~15:30
        t = t.filter(pc.or_(krx_keep, nxt_keep))
        t = t.sort_by([("security_id", "ascending"), ("bar_start_at_utc", "ascending")])
        vol = pc.cast(t.column("volume"), pa.float64())
        tv = t.column("traded_value")
        # 장후 시간외 종가 봉(KRX OTHER, 15:35~ — 키움만 준다)은 거래대금이 없다. 그 시간
        # 체결은 전부 당일 종가라 거래대금 = 거래량 × 봉 종가로 정확하다
        utc_min = pc.add(pc.multiply(pc.hour(t.column("bar_start_at_utc")), 60),
                         pc.minute(t.column("bar_start_at_utc")))
        close_auction = pc.and_(pc.and_(pc.invert(pc.starts_with(t.column("security_id"), "NXT:")),
                                        pc.equal(t.column("session"), "OTHER")),
                                pc.greater_equal(utc_min, 6 * 60 + 35))
        tv = pc.if_else(pc.and_(close_auction, pc.is_null(tv)),
                        pc.multiply(vol, t.column("close")), tv)
        typ = pc.divide(pc.add(pc.add(t.column("high"), t.column("low")), t.column("close")), 3.0)
        t = t.set_column(t.schema.get_field_index("traded_value"), "traded_value", tv)
        t = t.append_column("unvalued_volume",
                            pc.if_else(pc.is_null(tv), t.column("volume"), 0))
        t = t.append_column("typ_unvalued",
                            pc.if_else(pc.is_null(tv), pc.multiply(typ, vol), 0.0))
        g = t.group_by(["security_id", "trading_date"], use_threads=False).aggregate([
            ("volume", "sum"), ("traded_value", "sum"), ("unvalued_volume", "sum"),
            ("typ_unvalued", "sum"), ("traded_value", "first", keep_nulls),
            ("cum_traded_value", "first", keep_nulls), ("volume", "first", keep_nulls),
            ("typ_unvalued", "first", keep_nulls), ("close", "last", keep_nulls)])
        for r in g.to_pylist():
            venue, code = r["security_id"].split(":")
            value = r["traded_value_sum"] or 0.0
            unvalued = int(r["unvalued_volume_sum"])
            typ_unvalued = float(r["typ_unvalued_sum"])
            # KIS 수집 지평 첫날은 KRX 첫 봉 거래대금이 비어 있다 — 그날 첫 누적값으로
            # 채운다(`load_minutes`와 같은 규칙). NXT 첫 메인 봉의 누적값에는 프리마켓이
            # 섞이므로 채우지 않는다
            if (venue == "KRX" and r["traded_value_first"] is None
                    and r["cum_traded_value_first"] is not None):
                value += r["cum_traded_value_first"]
                unvalued -= int(r["volume_first"])
                typ_unvalued -= float(r["typ_unvalued_first"])
            out[(venue, code, r["trading_date"])] = {
                "volume": int(r["volume_sum"]), "value": value,
                "unvalued_volume": unvalued, "typ_unvalued": typ_unvalued,
                "last_close": r["close_last"],
            }
    return out


def load_minute_coverage(codes, day: date, store=None) -> dict:
    """{(\"KRX\"|\"NXT\", 코드): 커버리지 dict} — D 정규장 1분봉 수집 기록.

    커버리지가 있으면 그날 수집이 끝난 것이다. 그러면 창 안의 빠진 분은
    무거래로 보고, 커버리지도 봉도 없으면 수신 누락(결측)이다.
    """
    import pyarrow.dataset as ds
    from cvd_obv.domain.errors import NoInputDataError

    store = store or ParquetStore()
    ids = sorted({f"KRX:{c}" for c in codes} | {f"NXT:{c}" for c in codes})
    try:
        t = store.read_arrow(
            "silver", "bar_coverage",
            columns=["dataset", "security_id", "period_min", "trading_date",
                     "session", "expected_bars", "actual_bars", "completeness",
                     "pagination_complete", "status", "run_id", "data_revision"],
            filters=(ds.field("dataset") == "minute") & (ds.field("period_min") == 1)
            & (ds.field("trading_date") == day) & (ds.field("session") == "REGULAR")
            & ds.field("security_id").isin(ids),
            latest_by=BAR_COVERAGE_PK[:-1])
    except NoInputDataError:
        return {}
    out = {}
    for r in t.to_pylist():
        venue, code = r["security_id"].split(":")
        out[(venue, code)] = {k: r[k] for k in (
            "expected_bars", "actual_bars", "completeness",
            "pagination_complete", "status")}
    return out


def now_kst() -> datetime:
    return datetime.now(tz=KST)


__all__ = ["LOOKBACK_DAYS", "latest_daily_date", "load_daily",
           "trading_calendar", "load_flows", "load_flow_available_at",
           "load_minutes", "load_minute_day_totals", "load_minute_coverage", "now_kst",
           "FLAG_NXT_INCLUDED", "FLAG_NXT_MISSING"]

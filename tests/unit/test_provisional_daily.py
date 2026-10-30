# -*- coding: utf-8 -*-
"""2단계 일일 적재 — 16:10 잠정 일봉, 익일 08:30 정규장 확정.

지키는 것
1. 잠정 일봉은 1분봉 합이다: 종가 = 종가 단일가 체결 봉, 거래량 = 정규장 + 15:35~16:00
   시간외 종가(애프터마켓은 빼고).
2. 16:00 전에는 만들지 않는다. 확정 봉이 있는 종목은 건드리지 않는다.
3. 확정 봉(리비전 1)은 run_id 순서와 무관하게 잠정 봉(리비전 0)을 이긴다.
4. 잠정 봉만 있는 날은 "정규장 복원 전"이다 — 확정 복원의 대상이고 경고에 잡힌다.
5. 적재는 장부의 마지막 확정일부터 다시 본다. 밀린 날을 먼저 확정한 뒤 잠정 봉을 만든다.
"""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from cvd_obv.bar_standard import (
    PROVISIONAL_SOURCE, provisional_days, read_standard_daily, unrestored_days,
)
from cvd_obv.contracts.market_schema import (
    DAILY_BAR_COLUMNS, DAILY_BAR_PK, MINUTE_BAR_COLUMNS, MINUTE_BAR_PK,
)
from cvd_obv.domain.ids import KST
from cvd_obv.ingestion import load_state
from cvd_obv.ingestion.provisional_daily import (
    FLAG_PROVISIONAL, ProvisionalDailyBuilder, derive_provisional_bar,
)
from cvd_obv.ingestion.regular_from_minute import (
    FLAG_LATE_CLOSE_PRINT, RegularFromMinuteBuilder,
)
from cvd_obv.storage.parquet_store import ParquetStore

pytest.importorskip("pyarrow")

SID, SID2 = "KRX:000001", "KRX:000002"
DAY = date(2026, 9, 22)
UTC = timezone.utc


def _m(hh, mm, session, o, h, l, c, v):
    return {"kst": time(hh, mm), "session": session, "open": o, "high": h,
            "low": l, "close": c, "volume": v}


def _day():
    return [
        _m(9, 0, "REGULAR", 100, 103, 99, 102, 1000),
        _m(12, 0, "REGULAR", 102, 104, 101, 103, 500),
        _m(15, 30, "REGULAR", 103, 103, 103, 103, 300),   # 종가 단일가
        _m(15, 40, "OTHER", 103, 103, 103, 103, 20),      # 장후 시간외 종가
        _m(16, 0, "AFTER", 103, 108, 103, 107, 150),
    ]


# ── 1. 1분봉 합 ─────────────────────────────────────────────

def test_잠정_일봉은_정규장과_시간외_종가_합이고_애프터마켓은_뺀다():
    bar, reason, flags = derive_provisional_bar(_day())
    assert reason == "OK"
    assert (bar["open"], bar["high"], bar["low"], bar["close"]) == (100, 104, 99, 103)
    assert bar["volume"] == 1000 + 500 + 300 + 20
    assert FLAG_PROVISIONAL in flags


def test_늦게_찍힌_종가_체결도_종가로_쓴다():
    minutes = [_m(9, 0, "REGULAR", 100, 101, 99, 100, 10),
               _m(15, 19, "REGULAR", 100, 100, 98, 99, 5),
               _m(15, 32, "OTHER", 97, 97, 97, 97, 40)]
    bar, reason, flags = derive_provisional_bar(minutes)
    assert reason == "OK" and bar["close"] == 97 and bar["low"] == 97
    assert bar["volume"] == 55 and FLAG_LATE_CLOSE_PRINT in flags


def test_종가_체결_봉이_없으면_만들지_않는다():
    minutes = [m for m in _day() if m["kst"] != time(15, 30)]
    assert derive_provisional_bar(minutes)[:2] == (None, "NO_CLOSE_AUCTION_BAR")


# ── 저장소 픽스처 ──────────────────────────────────────────

def _minute_rows(sid, run_id="m1"):
    out = []
    for m in _day():
        start = datetime.combine(DAY, m["kst"], tzinfo=KST).astimezone(UTC)
        end = start + timedelta(minutes=1)
        out.append({"security_id": sid, "trading_date": DAY, "period_min": 1,
                    "session": m["session"], "bar_start_at_utc": start,
                    "bar_end_at_utc": end,
                    "open": Decimal(str(m["open"])), "high": Decimal(str(m["high"])),
                    "low": Decimal(str(m["low"])), "close": Decimal(str(m["close"])),
                    "volume": m["volume"], "traded_value": None,
                    "source_timestamp_raw": start.astimezone(KST).strftime("%Y%m%d%H%M%S"),
                    "timestamp_semantics": "BAR_START", "available_at_utc": end,
                    "quality_status": "SUCCESS", "quality_flags": [], "source": "TEST",
                    "market_rule_version": "TEST", "run_id": run_id,
                    "schema_version": "2.0.0", "data_revision": 1})
    return out


def _daily(sid, close, scope, volume, source="TEST", revision=1, run_id="r1"):
    c = Decimal(str(close))
    return {"security_id": sid, "trading_date": DAY, "open": c, "high": c + 1,
            "low": c - 1, "close": c, "volume": volume, "traded_value": None,
            "price_scope": scope, "adjustment_type": "SPLIT_ADJUSTED",
            "adjustment_factor": None, "adjustment_as_of_utc": None,
            "available_at_utc": datetime(2026, 9, 22, 11, tzinfo=UTC),
            "quality_status": "SUCCESS", "quality_flags": [], "source": source,
            "market_rule_version": "TEST", "run_id": run_id,
            "schema_version": "2.0.0", "data_revision": revision}


def _write_daily(st, rows, run_id):
    st.write("silver", "daily_bars", rows, DAILY_BAR_COLUMNS, DAILY_BAR_PK, run_id,
             partition_fn=lambda r: [("year", r["trading_date"].year)])


@pytest.fixture
def store(tmp_path):
    st = ParquetStore(root=str(tmp_path))
    rows = _minute_rows(SID) + _minute_rows(SID2)
    st.write("silver", "minute_bars", rows, MINUTE_BAR_COLUMNS, MINUTE_BAR_PK, "m1",
             partition_fn=lambda r: [("year", r["trading_date"].year)])
    return st


def _closes(st):
    t = read_standard_daily(st, columns=["security_id", "trading_date", "close",
                                         "volume", "source"])
    return {r["security_id"]: (float(r["close"]), r["volume"], r["source"])
            for r in t.to_pylist()}


# ── 2. 만드는 시각·대상 ─────────────────────────────────────

def test_16시_전에는_만들지_않는다(store):
    out = ProvisionalDailyBuilder(store).build(
        "p1", DAY, {SID}, now=datetime(2026, 9, 22, 15, 50, tzinfo=KST))
    assert out["status"] == "TOO_EARLY" and out["written"] == 0


def test_애프터마켓_시행_전_날은_잠정_봉이_필요없다(store):
    out = ProvisionalDailyBuilder(store).build(
        "p1", date(2026, 9, 11), {SID}, now=datetime(2026, 9, 11, 16, 10, tzinfo=KST))
    assert out["status"] == "NOT_NEEDED"


def test_16시_뒤에는_잠정_봉을_쓰고_기준_reader가_읽는다(store):
    out = ProvisionalDailyBuilder(store).build(
        "p1", DAY, {SID}, now=datetime(2026, 9, 22, 16, 10, tzinfo=KST))
    assert out["written"] == 1
    assert _closes(store)[SID] == (103.0, 1820, PROVISIONAL_SOURCE)


def test_확정_봉이_있는_종목은_잠정_봉을_쓰지_않는다(store):
    _write_daily(store, [_daily(SID, 103, "REGULAR", 1900,
                                source="DERIVED_MINUTE_1M")], "f1")
    out = ProvisionalDailyBuilder(store).build(
        "p1", DAY, {SID, SID2}, now=datetime(2026, 9, 22, 16, 10, tzinfo=KST))
    assert out["already_final"] == 1 and out["written"] == 1
    assert _closes(store)[SID][2] == "DERIVED_MINUTE_1M"


def test_1분봉이_없는_날은_종목별_사유를_쌓지_않는다(store):
    out = ProvisionalDailyBuilder(store).build(
        "p1", date(2026, 9, 23), {SID, SID2},
        now=datetime(2026, 9, 23, 16, 10, tzinfo=KST))
    assert out["status"] == "NO_MINUTES" and out["skipped"] == {}


# ── 3·4. 확정 봉이 잠정 봉을 덮는다 ────────────────────────

def test_확정_봉은_run_id와_무관하게_잠정_봉을_이긴다(store):
    # 잠정 봉 run_id가 사전순으로 더 나중이어도(수동 재실행 등) 확정 봉이 선택된다
    _write_daily(store, [_daily(SID, 103, "REGULAR", 1820, source=PROVISIONAL_SOURCE,
                                revision=0, run_id="zzz")], "zzz")
    _write_daily(store, [_daily(SID, 103, "REGULAR", 1900,
                                source="DERIVED_MINUTE_1M", run_id="aaa")], "aaa")
    assert _closes(store)[SID] == (103.0, 1900, "DERIVED_MINUTE_1M")


def test_잠정_봉만_있는_날은_복원_전으로_세고_확정_복원_대상이다(store):
    _write_daily(store, [_daily(SID, 107, "EXTENDED", 1970),
                         _daily(SID, 103, "REGULAR", 1820, source=PROVISIONAL_SOURCE,
                                revision=0)], "p1")
    assert unrestored_days(store, start=DAY) == {DAY: 1}
    assert provisional_days(store, start=DAY) == {DAY: 1}
    targets, _ = RegularFromMinuteBuilder(store)._targets(DAY, DAY, {SID})
    assert set(targets) == {(SID, DAY)}


def test_확정_복원이_잠정_봉을_대체한다(store):
    """16:10 잠정 → (애프터마켓 확정 뒤 받은 분봉·일봉으로) 확정 복원."""
    ProvisionalDailyBuilder(store).build(
        "p1", DAY, {SID}, now=datetime(2026, 9, 22, 16, 10, tzinfo=KST))
    late = "topup_load_20260922231000_abcd1234"         # 익일 08:10 KST 수집
    store.write("silver", "minute_bars", _minute_rows(SID, run_id=late),
                MINUTE_BAR_COLUMNS, MINUTE_BAR_PK, late,
                partition_fn=lambda r: [("year", r["trading_date"].year)])
    ext = _daily(SID, 107, "EXTENDED", 2000, run_id=late)
    ext.update(open=Decimal("100"), high=Decimal("108"), low=Decimal("99"),
               adjustment_as_of_utc=datetime(2026, 9, 22, 23, 10, tzinfo=UTC))
    _write_daily(store, [ext], late)
    out = RegularFromMinuteBuilder(store).build("f1", DAY, DAY, {SID})
    assert out["written"] == 1
    assert _closes(store)[SID] == (103.0, 2000 - 150, "DERIVED_MINUTE_1M")
    assert provisional_days(store, start=DAY) == {}
    assert unrestored_days(store, start=DAY) == {}


# ── 5. 밀린 날부터 ─────────────────────────────────────────

def test_확정_시점은_애프터마켓_확정_전이면_전날이다():
    assert load_state.confirmed_horizon(datetime(2026, 10, 7, 8, 30, tzinfo=KST)) == \
        date(2026, 10, 6)
    assert load_state.confirmed_horizon(datetime(2026, 10, 6, 16, 10, tzinfo=KST)) == \
        date(2026, 10, 5)
    assert load_state.confirmed_horizon(datetime(2026, 10, 6, 20, 10, tzinfo=KST)) == \
        date(2026, 10, 6)


def test_확인_구간은_장부의_마지막_확정일부터():
    today = date(2026, 10, 12)
    state = {"confirmed_through": "2026-10-01"}
    assert load_state.backlog_since(today, state)[:2] == (date(2026, 10, 1), None)
    assert load_state.backlog_since(today, state, days=3)[:2] == (None, 3)
    assert load_state.backlog_since(today, state, since=date(2026, 9, 1))[:2] == (
        date(2026, 9, 1), None)
    assert load_state.backlog_since(today, {})[:2] == (None, load_state.DEFAULT_DAYS)
    assert load_state.backlog_since(today, {"confirmed_through": "깨짐"})[:2] == (
        None, load_state.DEFAULT_DAYS)


def test_장부는_다른_키를_남기고_덮어_쓴다(tmp_path):
    path = str(tmp_path / "state.json")
    load_state.save_state({"confirmed_through": "2026-10-01", "x": 1}, path)
    load_state.save_state({"confirmed_through": "2026-10-02"}, path)
    got = load_state.load_state(path)
    assert got["confirmed_through"] == "2026-10-02" and got["x"] == 1


def test_밀린_날이_길어도_catchup이_그날까지_거슬러_간다():
    from cvd_obv.cli.main import daily_load_steps

    steps = daily_load_steps(date(2026, 10, 30), since=date(2026, 10, 1))
    catchup = steps[0]
    assert catchup[catchup.index("--since") + 1] == "2026-10-01"
    assert int(catchup[catchup.index("--days") + 1]) >= 30   # 기본 10이면 잘린다
    nxt = steps[1]                                             # NXT 1분봉도 같은 구간
    assert nxt[nxt.index("--venue") + 1] == "NXT"
    assert nxt[nxt.index("--since") + 1] == "2026-10-01"
    assert steps[2][steps[2].index("--start") + 1] == "2026-10-01"


def test_잠정_적재는_확정_단계를_모두_마친_뒤_잠정_봉을_만든다():
    from cvd_obv.cli.main import daily_load_steps

    final = daily_load_steps(date(2026, 10, 6), since=date(2026, 10, 2))
    prov = daily_load_steps(date(2026, 10, 6), since=date(2026, 10, 2), provisional=True)
    # 확정 단계 → 오늘 잠정 봉 → 체결량 → 구성종목 이력(선택 단계 둘, 맨 끝)
    assert prov[:-3] == final[:-1] and prov[-2] == final[-1]
    assert prov[-3][:4] == ["daily-cache", "provisional", "--date", "2026-10-06"]
    assert prov[-2][:2] == ["trade-sides", "daily"]
    assert prov[-1][:2] == ["universe", "refresh"]
    assert not any(s[:2] == ["universe", "refresh"] for s in final)     # 08:30에는 없다


# ── 장부는 확정 안 된 날 앞까지만 ───────────────────────────

def test_장부는_확정_안_된_가장_이른_날_앞까지만_옮긴다():
    h = date(2026, 10, 13)
    assert load_state.confirmed_through(h, None) == h
    assert load_state.confirmed_through(h, date(2026, 10, 13)) == date(2026, 10, 12)
    assert load_state.confirmed_through(h, date(2026, 10, 14)) == h


def test_확정_봉이_모자란_거래일을_찾는다(tmp_path):
    st = ParquetStore(root=str(tmp_path))
    d1, d2 = DAY, DAY + timedelta(days=1)
    probe = "KRX:005930"
    rows = [dict(_daily(probe, 10, "EXTENDED", 5), trading_date=d)
            for d in (d1, d2)]
    # d1: 두 종목 모두 확정 / d2: 확정 하나 + 잠정 하나 → 50% < 90%
    for sid in (SID, SID2):
        rows.append(dict(_daily(sid, 10, "REGULAR", 5,
                                source="DERIVED_MINUTE_1M"), trading_date=d1))
    rows.append(dict(_daily(SID, 10, "REGULAR", 5, source="DERIVED_MINUTE_1M"),
                     trading_date=d2))
    rows.append(dict(_daily(SID2, 10, "REGULAR", 5, source=PROVISIONAL_SOURCE,
                            revision=0), trading_date=d2))
    _write_daily(st, rows, "r1")
    assert load_state.trading_days(st, d1, d2) == [d1, d2]
    assert load_state.first_unconfirmed_day(st, d1, d2, {SID, SID2}) == d2
    assert load_state.first_unconfirmed_day(st, d1, d1, {SID, SID2}) is None
    # 비율은 넘겨준 종목 기준이다 — SID만 보면 d2도 확정
    assert load_state.first_unconfirmed_day(st, d1, d2, {SID}) is None


# ── 놓친 작업 메우기 — 밀린 날: 1분봉 → 잠정 → 확정 → 그다음 오늘 ──

def test_밀린_날은_1분봉_잠정_확정_순서이고_오늘_잠정은_맨_뒤다():
    from cvd_obv.cli.main import daily_load_steps

    steps = daily_load_steps(date(2026, 10, 14), since=date(2026, 10, 12),
                             provisional=True)
    names = [" ".join(s[:2]) for s in steps]
    assert names == ["minute-cache catchup", "minute-cache catchup",
                     "daily-cache provisional",
                     "daily-cache build", "daily-cache build",
                     "daily-cache readjust",
                     "daily-cache regular-from-minute", "daily-cache provisional",
                     "trade-sides daily", "universe refresh"]
    assert "--venue" not in steps[0] and "NXT" in steps[1]     # KRX 다음 NXT 1분봉
    backlog, today = steps[2], steps[-3]
    assert backlog[backlog.index("--start") + 1] == "2026-10-12"
    assert backlog[backlog.index("--end") + 1] == "2026-10-13"      # 어제까지
    assert "--missing-only" in backlog
    assert today[today.index("--date") + 1] == "2026-10-14"


def test_밀린_날이_없으면_잠정_메우기_단계도_없다():
    from cvd_obv.cli.main import daily_load_steps

    steps = daily_load_steps(date(2026, 10, 14), since=date(2026, 10, 14))
    assert [" ".join(s[:2]) for s in steps].count("daily-cache provisional") == 0


def test_빠진_잠정_봉만_만든다(store):
    # SID2는 이미 잠정 봉이 있다 — missing_only면 다시 만들지 않는다
    _write_daily(store, [_daily(SID2, 103, "REGULAR", 1, source=PROVISIONAL_SOURCE,
                                revision=0, run_id="p0")], "p0")
    _write_daily(store, [dict(_daily("KRX:005930", 1, "EXTENDED", 1))], "x")
    out = ProvisionalDailyBuilder(store).build_range(
        "p1", DAY, DAY, {SID, SID2}, now=datetime(2026, 9, 23, 8, 30, tzinfo=KST))
    got = out["days"][DAY.isoformat()]
    assert got["written"] == 1 and got["already_provisional"] == 1
    assert _closes(store)[SID2][1] == 1                # 기존 잠정 봉 그대로


def test_선행_점검은_날짜별_상태를_보여준다(store):
    _write_daily(store, [_daily("KRX:005930", 1, "EXTENDED", 1),
                         _daily(SID, 103, "REGULAR", 1, source="DERIVED_MINUTE_1M"),
                         _daily(SID2, 103, "REGULAR", 1, source=PROVISIONAL_SOURCE,
                                revision=0)], "x")
    rows = {r["date"]: r for r in load_state.day_status(
        store, DAY, DAY + timedelta(days=1), {SID, SID2})}
    assert rows[DAY]["final"] == 1 and rows[DAY]["provisional"] == 1
    assert rows[DAY]["status"] == load_state.STATUS_PROVISIONAL
    assert rows[DAY + timedelta(days=1)]["status"] == load_state.STATUS_EMPTY


# ── 구성종목 이력 정기 갱신(16:10 마지막 단계) ──

def test_구성종목_이력_갱신은_선택_단계다():
    from cvd_obv.cli.main import _is_optional_step

    assert _is_optional_step(["universe", "refresh", "--index", "KOSPI200,KOSDAQ150"])


def test_이력_끝은_KRX_자료가_있는_마지막_거래일이다():
    from cvd_obv.cli.main import refresh_end

    days = [date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23), date(2026, 10, 26)]
    have = {date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23)}
    # 16:10 — 오늘(10-26) 자료는 아직 없다 → 직전 거래일
    assert refresh_end(date(2026, 10, 26), days, have.__contains__) == date(2026, 10, 23)
    # 오늘 뒤 날짜는 보지 않는다
    assert refresh_end(date(2026, 10, 22), days, have.__contains__) == date(2026, 10, 22)
    assert refresh_end(date(2026, 10, 26), days, lambda d: False) is None

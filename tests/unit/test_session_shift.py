# -*- coding: utf-8 -*-
"""장 시간이 통째로 늦춰진 날(수능 등) — 세션 경계 이동.

지키는 것
1. 설정한 날만 이동한다. 평일은 그대로다.
2. 이동한 날은 봉 시각을 평일 시계로 당겨 세션을 판정한다(16:30 = 종가 단일가, 17:00 = 애프터마켓).
3. 시장 규칙의 세션 경계·애프터마켓 확정 시각도 같이 늦춰진다.
4. 고정 시각 라벨로 먼저 저장된 봉과 다시 받은 봉이 함께 있어도 한 번만 센다.
5. 잠정 일봉은 실제 종가 단일가(16:30)를 종가로 쓰고, 16:00~16:30 봉을 정규장으로 더한다.
"""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from cvd_obv.baseline.scanner_v15 import load_scanner_v15
from cvd_obv.bar_standard import PROVISIONAL_SOURCE, read_standard_daily
from cvd_obv.contracts.market_schema import MINUTE_BAR_COLUMNS, MINUTE_BAR_PK
from cvd_obv.domain import session_shift
from cvd_obv.domain.ids import KST
from cvd_obv.domain.market_rules import default_registry
from cvd_obv.domain.status import Session
from cvd_obv.ingestion.provisional_daily import ProvisionalDailyBuilder
from cvd_obv.storage.parquet_store import ParquetStore

pytest.importorskip("pyarrow")

SUNEUNG = date(2026, 11, 19)
NORMAL = date(2026, 11, 18)
UTC = timezone.utc
SID = "KRX:000001"


# ── 1. 설정 ────────────────────────────────────────────────

def test_수능일은_60분_늦춰지고_평일은_그대로다():
    assert session_shift.shift_of(SUNEUNG) == timedelta(minutes=60)
    assert session_shift.shift_of("20261119") == timedelta(minutes=60)
    assert session_shift.shift_of(NORMAL) == timedelta(0)


def test_봉_시각은_평일_시계로_당긴다():
    assert session_shift.clock_dt_str("20261119163000") == "20261119153000"
    assert session_shift.clock_dt_str("20261118163000") == "20261118163000"
    at = datetime(2026, 11, 19, 16, 30, tzinfo=KST)
    assert session_shift.clock_time(at) == time(15, 30)


# ── 2. 세션 판정 ───────────────────────────────────────────

@pytest.mark.parametrize("hhmm, expected", [
    ("1000", "REGULAR"), ("1545", "REGULAR"), ("1619", "REGULAR"),
    ("1630", "REGULAR"), ("1700", "AFTER"), ("2059", "AFTER"),
])
def test_수능일_분봉_세션은_한_시간_늦춘_경계로_판정한다(hhmm, expected):
    sc = load_scanner_v15()
    assert sc.session_of(session_shift.clock_dt_str(f"20261119{hhmm}00")) == expected


def test_평일_분봉_세션은_그대로다():
    sc = load_scanner_v15()
    assert sc.session_of(session_shift.clock_dt_str("20261118160000")) == "AFTER"
    assert sc.session_of(session_shift.clock_dt_str("20261118153000")) == "REGULAR"


# ── 3. 시장 규칙 ───────────────────────────────────────────

def test_시장_규칙의_세션_경계와_확정_시각도_늦춰진다():
    rule = default_registry().resolve("KRX", SUNEUNG)
    start, end = rule.session_bounds(SUNEUNG, Session.REGULAR)
    assert (start.time(), end.time()) == (time(10, 0), time(16, 30))
    assert rule.daily_confirmed_at(SUNEUNG).time() == time(21, 5)
    assert rule.daily_confirmed_at(NORMAL).time() == time(20, 5)


# ── 4. 중복 봉 ─────────────────────────────────────────────

def _row(hh, mm, session, close, volume, run_id, day=SUNEUNG, sid=SID):
    start = datetime.combine(day, time(hh, mm), tzinfo=KST).astimezone(UTC)
    end = start + timedelta(minutes=1)
    return {"security_id": sid, "trading_date": day, "period_min": 1,
            "session": session, "bar_start_at_utc": start, "bar_end_at_utc": end,
            "open": Decimal(str(close)), "high": Decimal(str(close)),
            "low": Decimal(str(close)), "close": Decimal(str(close)),
            "volume": volume, "traded_value": None,
            "source_timestamp_raw": start.astimezone(KST).strftime("%Y%m%d%H%M%S"),
            "timestamp_semantics": "BAR_START", "available_at_utc": end,
            "quality_status": "SUCCESS", "quality_flags": [], "source": "TEST",
            "market_rule_version": "TEST", "run_id": run_id,
            "schema_version": "2.0.0", "data_revision": 1}


OLD_RUN = "topup_load_20261119071407_aaaaaaaa"   # 16:14 KST, 고정 시각 라벨
NEW_RUN = "topup_load_20261119090000_bbbbbbbb"   # 18:00 KST, 평일 시계 라벨


def test_수능일은_같은_봉_시각에_나중_행만_남긴다():
    rows = [_row(15, 45, "OTHER", 100, 10, OLD_RUN),
            _row(15, 45, "REGULAR", 100, 10, NEW_RUN)]
    kept = session_shift.dedupe_shifted_bars(rows)
    assert [r["run_id"] for r in kept] == [NEW_RUN]


def test_평일_행은_중복_제거하지_않는다():
    rows = [_row(15, 45, "OTHER", 100, 10, OLD_RUN, day=NORMAL),
            _row(15, 45, "REGULAR", 100, 10, NEW_RUN, day=NORMAL)]
    assert len(session_shift.dedupe_shifted_bars(rows)) == 2


# ── 5. 잠정 일봉 ───────────────────────────────────────────

@pytest.fixture
def store(tmp_path):
    st = ParquetStore(root=str(tmp_path))
    # 16:10 첫 수집 — 고정 시각 라벨(15:31~ OTHER), 16:00 뒤 봉은 아직 없음
    old = [_row(10, 0, "REGULAR", 100, 1000, OLD_RUN),
           _row(15, 30, "REGULAR", 101, 300, OLD_RUN),
           _row(15, 45, "OTHER", 102, 200, OLD_RUN)]
    # 18:00 다시 받기 — 평일 시계 라벨
    new = [_row(10, 0, "REGULAR", 100, 1000, NEW_RUN),
           _row(15, 30, "REGULAR", 101, 300, NEW_RUN),
           _row(15, 45, "REGULAR", 102, 200, NEW_RUN),
           _row(16, 10, "REGULAR", 104, 100, NEW_RUN),
           _row(16, 30, "REGULAR", 105, 500, NEW_RUN)]   # 종가 단일가
    for rows, run in ((old, OLD_RUN), (new, NEW_RUN)):
        st.write("silver", "minute_bars", rows, MINUTE_BAR_COLUMNS, MINUTE_BAR_PK,
                 run, partition_fn=lambda r: [("year", r["trading_date"].year)])
    return st


def test_수능일_잠정_일봉은_16시30분_종가와_늦춘_정규장_거래량이다(store):
    out = ProvisionalDailyBuilder(store).build(
        "p1", SUNEUNG, {SID}, now=datetime(2026, 11, 19, 18, 0, tzinfo=KST))
    assert out["written"] == 1
    t = read_standard_daily(store, columns=["security_id", "close", "high",
                                            "volume", "source"])
    row = t.to_pylist()[0]
    assert float(row["close"]) == 105.0
    assert float(row["high"]) == 105.0
    assert row["volume"] == 1000 + 300 + 200 + 100 + 500   # 15:45 봉은 한 번만
    assert row["source"] == PROVISIONAL_SOURCE


def test_수능일_잠정_일봉은_17시_전에는_만들지_않는다(store):
    out = ProvisionalDailyBuilder(store).build(
        "p1", SUNEUNG, {SID}, now=datetime(2026, 11, 19, 16, 40, tzinfo=KST))
    assert out["status"] == "TOO_EARLY"

# -*- coding: utf-8 -*-
"""1분봉 중복 저장 줄이기(2026-10-16) — 16:10은 정규장만, 다음 날 08:30은 애프터마켓만.

지키는 것
1. 애프터마켓 확정 전 수집은 애프터마켓 봉·커버리지를 저장하지 않는다.
2. 정규장이 이미 완전한 날의 확정 뒤 다시 받기는 정규장 봉을 저장하지 않지만,
   정규장 커버리지는 새로 쓴다(그날을 확정 뒤에 받았다는 근거).
3. 계획에 없는 (종목, 날짜)는 저장하지 않는다.
4. 그렇게 나눠 저장해도 정규장 확정 복원이 된다 — 거래량 = EXTENDED − 애프터마켓.
"""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from cvd_obv.baseline.scanner_v15 import load_scanner_v15
from cvd_obv.bar_standard import read_standard_daily
from cvd_obv.contracts.market_schema import DAILY_BAR_COLUMNS, DAILY_BAR_PK
from cvd_obv.domain.ids import KST
from cvd_obv.ingestion.kiwoom_minute import MinuteFetchResult
from cvd_obv.ingestion.minute_catchup import MinuteCatchup, SessionStorePolicy
from cvd_obv.ingestion.minute_history import MinuteHistoryCache
from cvd_obv.ingestion.regular_from_minute import RegularFromMinuteBuilder
from cvd_obv.storage.parquet_store import ParquetStore

pytest.importorskip("pyarrow")

SID, CODE = "KRX:000001", "000001"
DAY = date(2026, 9, 22)
UTC = timezone.utc
AT_1610 = datetime(2026, 9, 22, 16, 10, tzinfo=KST)
AT_0830 = datetime(2026, 9, 23, 8, 30, tzinfo=KST)
RUN_1610 = "topup_load_20260922071000_aaaa0001"     # 16:10 KST
RUN_0830 = "topup_load_20260922233000_bbbb0002"     # 다음 날 08:30 KST

# (시각, 세션, 가격, 거래량)
SPEC = [((9, 0), "REGULAR", 100, 1000), ((12, 0), "REGULAR", 102, 500),
        ((15, 30), "REGULAR", 103, 300), ((15, 40), "OTHER", 103, 20),
        ((16, 0), "AFTER", 104, 150), ((19, 59), "AFTER", 98, 30)]


def _bars(upto):
    out = []
    for (hh, mm), session, px, vol in SPEC:
        start = datetime.combine(DAY, time(hh, mm))
        if start >= upto.replace(tzinfo=None):
            continue
        out.append({"trading_date": DAY, "bar_start": start,
                    "bar_end": start + timedelta(minutes=1), "session": session,
                    "open": Decimal(px), "high": Decimal(px), "low": Decimal(px),
                    "close": Decimal(px), "volume": vol, "traded_value": None,
                    "source_timestamp_raw": start.strftime("%Y%m%d%H%M%S")})
    return out


class _Source:
    def __init__(self, upto):
        self.scanner = load_scanner_v15()
        self.upto = upto

    def token(self):
        return "T"

    def fetch_security(self, code, token=None, period=1, max_pages=None,
                       min_date=None):
        bars = _bars(self.upto)
        return MinuteFetchResult(code, period, bars, 1, len(bars), True, "SUCCESS")


def _fetch(store, upto, run_id, policy):
    cache = MinuteHistoryCache(store=store)
    cache.source = _Source(upto)
    return cache.build([CODE], run_id=run_id, period=1,
                       keep_bar=policy.keep_bar, keep_coverage=policy.keep_coverage)


def _stored_sessions(store):
    t = store.read_arrow("silver", "minute_bars",
                         columns=["session", "run_id", "data_revision",
                                  "security_id", "period_min", "bar_start_at_utc"])
    out = {}
    for s, r in zip(t.column("session").to_pylist(), t.column("run_id").to_pylist()):
        out.setdefault(r, []).append(s)
    return {r: sorted(v) for r, v in out.items()}


def test_확정_전에는_애프터마켓을_저장하지_않는다():
    p = SessionStorePolicy(AT_1610)
    assert p.keep_bar(SID, DAY, "REGULAR") and p.keep_bar(SID, DAY, "OTHER")
    assert not p.keep_bar(SID, DAY, "AFTER") and not p.keep_coverage(SID, DAY, "AFTER")


def test_정규장이_완전한_날의_확정_뒤_다시_받기는_정규장_봉만_건너뛴다():
    p = SessionStorePolicy(AT_0830, keep_regular_skip={(SID, DAY)})
    assert not p.keep_bar(SID, DAY, "REGULAR")
    assert p.keep_coverage(SID, DAY, "REGULAR")            # 받은 시각의 근거
    assert p.keep_bar(SID, DAY, "AFTER") and p.keep_bar(SID, DAY, "OTHER")


def test_계획에_없는_날은_저장하지_않는다():
    p = SessionStorePolicy(AT_0830, targets={(SID, DAY)})
    assert not p.keep_bar(SID, DAY - timedelta(days=1), "REGULAR")
    assert not p.keep_coverage("KRX:000002", DAY, "REGULAR")


def test_나눠_저장해도_정규장_확정_복원이_된다(tmp_path):
    st = ParquetStore(root=str(tmp_path))
    # 16:10 — 정규장(+시간외 종가)만. 16:00 애프터마켓 봉은 받지만 저장하지 않는다
    _fetch(st, datetime(2026, 9, 22, 16, 10), RUN_1610,
           SessionStorePolicy(AT_1610, targets={(SID, DAY)}))
    # 다음 날 08:30 — 애프터마켓만(정규장 커버리지는 새로 쓴다)
    out = _fetch(st, datetime(2026, 9, 23, 8, 30), RUN_0830,
                 SessionStorePolicy(AT_0830, keep_regular_skip={(SID, DAY)},
                                    targets={(SID, DAY)}))
    got = _stored_sessions(st)
    assert got[RUN_1610] == ["OTHER", "REGULAR", "REGULAR", "REGULAR"]
    assert got[RUN_0830] == ["AFTER", "AFTER", "OTHER"]
    assert out["fetched_bars"] == 6 and out["bars"] == 3

    catchup = MinuteCatchup(store=st)
    assert catchup.last_fetched_since(1, DAY)[(SID, DAY)] == \
        datetime(2026, 9, 22, 23, 30, tzinfo=UTC)
    assert catchup.after_pending_since(1, DAY, AT_0830) == set()   # 다시 받지 않는다

    ext = {"security_id": SID, "trading_date": DAY, "open": Decimal("100"),
           "high": Decimal("104"), "low": Decimal("98"), "close": Decimal("98"),
           "volume": 2000, "traded_value": None, "price_scope": "EXTENDED",
           "adjustment_type": "SPLIT_ADJUSTED", "adjustment_factor": None,
           "adjustment_as_of_utc": datetime(2026, 9, 22, 23, 31, tzinfo=UTC),
           "available_at_utc": datetime(2026, 9, 22, 11, 5, tzinfo=UTC),
           "quality_status": "SUCCESS", "quality_flags": [], "source": "TEST",
           "market_rule_version": "TEST", "run_id": "x", "schema_version": "2.0.0",
           "data_revision": 1}
    st.write("silver", "daily_bars", [ext], DAILY_BAR_COLUMNS, DAILY_BAR_PK, "x",
             partition_fn=lambda r: [("year", r["trading_date"].year)])
    res = RegularFromMinuteBuilder(st).build("f1", DAY, DAY, {SID})
    assert res["written"] == 1, res["skipped"]
    row = read_standard_daily(st, columns=["security_id", "close", "volume"]).to_pylist()[0]
    assert float(row["close"]) == 103 and row["volume"] == 2000 - 180


def test_확정_전에만_받은_날은_복원하지_않는다(tmp_path):
    st = ParquetStore(root=str(tmp_path))
    _fetch(st, datetime(2026, 9, 22, 16, 10), RUN_1610,
           SessionStorePolicy(AT_1610, targets={(SID, DAY)}))
    assert MinuteCatchup(store=st).after_pending_since(1, DAY, AT_0830) == {(SID, DAY)}
    ext = {"security_id": SID, "trading_date": DAY, "open": Decimal("100"),
           "high": Decimal("104"), "low": Decimal("98"), "close": Decimal("98"),
           "volume": 2000, "traded_value": None, "price_scope": "EXTENDED",
           "adjustment_type": "SPLIT_ADJUSTED", "adjustment_factor": None,
           "adjustment_as_of_utc": datetime(2026, 9, 22, 23, 31, tzinfo=UTC),
           "available_at_utc": datetime(2026, 9, 22, 11, 5, tzinfo=UTC),
           "quality_status": "SUCCESS", "quality_flags": [], "source": "TEST",
           "market_rule_version": "TEST", "run_id": "x", "schema_version": "2.0.0",
           "data_revision": 1}
    st.write("silver", "daily_bars", [ext], DAILY_BAR_COLUMNS, DAILY_BAR_PK, "x",
             partition_fn=lambda r: [("year", r["trading_date"].year)])
    res = RegularFromMinuteBuilder(st).build("f1", DAY, DAY, {SID})
    assert res["written"] == 0 and res["skipped"] == {"AFTER_MARKET_NOT_FINAL": 1}

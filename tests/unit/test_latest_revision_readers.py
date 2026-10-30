# -*- coding: utf-8 -*-
"""같은 봉의 리비전이 여럿일 때 조회 경로가 **최신 리비전 하나만** 쓰는가.

저장 PK(`DAILY_BAR_PK`, `MINUTE_BAR_PK`)에는 `data_revision`이 들어 있어
`dedupe_pk=`만으로 읽으면 리비전 1과 2가 둘 다 남는다. "현재 값"이 필요한
reader는 `latest_by=`(논리 키)로 읽어야 한다.

모든 테스트는 rev1을 run_id "run_z", rev2를 "run_a"로 쓴다. run_id 사전순이
리비전 순서와 반대라서, 리비전이 아니라 run_id(또는 파일 순서)로 고르는
구현이면 옛 값이 나와 드러난다.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from cvd_obv.contracts.market_schema import (
    DAILY_BAR_COLUMNS, DAILY_BAR_PK, MINUTE_BAR_COLUMNS, MINUTE_BAR_PK,
)
from cvd_obv.domain.ids import KST
from cvd_obv.storage.parquet_store import ParquetStore

SID = "KRX:000001"
CODE = "000001"
D1, D2 = date(2026, 9, 1), date(2026, 9, 2)
OLD, NEW = ("run_z", 1), ("run_a", 2)       # (run_id, data_revision)


def _daily(day, close, run_id, rev, status="SUCCESS"):
    c = Decimal(str(close))
    return {"security_id": SID, "trading_date": day,
            "open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 100,
            "traded_value": Decimal(str(close * 100)), "price_scope": "REGULAR",
            "adjustment_type": "SPLIT_ADJUSTED", "adjustment_factor": 1.0,
            "adjustment_as_of_utc": None,
            "available_at_utc": datetime(day.year, day.month, day.day, 7,
                                         tzinfo=timezone.utc),
            "quality_status": status, "quality_flags": [], "source": "TEST",
            "market_rule_version": "TEST", "run_id": run_id,
            "schema_version": "2.0.0", "data_revision": rev}


def _write_daily(st, rows, run_id):
    st.write("silver", "daily_bars", rows, DAILY_BAR_COLUMNS, DAILY_BAR_PK,
             run_id, partition_fn=lambda r: [("year", r["trading_date"].year)])


@pytest.fixture
def daily_store(tmp_path):
    """D1은 리비전 1(close 100)·2(close 110), D2는 리비전 1(close 120)만."""
    st = ParquetStore(root=str(tmp_path))
    _write_daily(st, [_daily(D1, 100, *OLD), _daily(D2, 120, *OLD)], OLD[0])
    _write_daily(st, [_daily(D1, 110, *NEW)], NEW[0])
    return st


def test_전제_저장_PK만으로는_두_리비전이_남는다(daily_store):
    t = daily_store.read_arrow("silver", "daily_bars",
                               columns=["trading_date"], dedupe_pk=DAILY_BAR_PK)
    assert t.num_rows == 3


# ─────────────────────────────────────────────────────────────
# 일봉 reader
# ─────────────────────────────────────────────────────────────

def test_백테스트_평가가격은_최신_리비전(daily_store):
    from cvd_obv.backtest.engine import BacktestEngine

    marks = BacktestEngine(daily_store)._load_marks({SID}, D1, D2)
    assert marks == {(SID, D1): 110.0, (SID, D2): 120.0}


def test_feature_시계열은_봉당_한_행_최신_리비전(daily_store):
    from cvd_obv.features.repository import DailyFeatureRepository

    grouped, excluded = DailyFeatureRepository(store=daily_store)._load_series()
    assert excluded == 0
    assert [(b["trading_date"], b["close"]) for b in grouped[SID]] == [
        (D1, 110.0), (D2, 120.0)]


def test_label_시계열은_최신_리비전(daily_store):
    """옛 경로는 같은 날짜가 두 번 들어와 `BarSeries`가 ConfigError를 냈다."""
    from cvd_obv.labels.repository import DailyForwardReturnRepository

    series_map, calendar, quarantined = DailyForwardReturnRepository(
        store=daily_store)._load_series()
    assert quarantined == 0
    series = series_map[SID]
    assert series.dates == [D1, D2]
    assert series.by_date[D1]["close"] == Decimal("110")


def test_최신_리비전이_FAILED면_옛_리비전으로_되돌아가지_않는다(tmp_path):
    """현재 값이 격리 상태면 그 봉은 없는 것이다 — rev1로 대체하면 안 된다."""
    from cvd_obv.labels.repository import DailyForwardReturnRepository

    st = ParquetStore(root=str(tmp_path))
    _write_daily(st, [_daily(D1, 100, *OLD), _daily(D2, 120, *OLD)], OLD[0])
    _write_daily(st, [_daily(D1, 110, *NEW, status="FAILED")], NEW[0])

    series_map, _cal, quarantined = DailyForwardReturnRepository(
        store=st)._load_series()
    assert quarantined == 1
    assert series_map[SID].dates == [D2]


def test_replay_일봉_창은_최신_리비전(daily_store):
    from cvd_obv.replay.windows import DailyWindow

    window = DailyWindow(daily_store, [SID], D1, D2, warmup_days=5)
    bars = window.slice_as_of(CODE, datetime(2026, 9, 2, 16, tzinfo=KST))
    assert [(b["dt"], b["close"]) for b in bars] == [
        ("20260901", 110.0), ("20260902", 120.0)]


def test_잘림_의심은_리비전이_아니라_봉을_센다(daily_store):
    """논리 봉 2개 = page_size 2의 배수. 리비전까지 세면 3이라 놓친다."""
    from cvd_obv.ingestion.daily_history import DailyHistoryRepository

    out = DailyHistoryRepository(store=daily_store) \
        .suspect_truncated_securities(page_size=2)
    assert out[SID]["rows"] == 2


def test_일봉_load는_봉당_한_행_최신_리비전(daily_store):
    """`read()` 경로도 같다 — 저장 PK로 읽으면 D1이 두 번 나왔다."""
    from cvd_obv.ingestion.daily_history import DailyHistoryRepository

    rows = DailyHistoryRepository(store=daily_store).load(security_id=SID)
    assert [(r["trading_date"], r["close"], r["data_revision"]) for r in rows]         == [(D1, Decimal("110"), 2), (D2, Decimal("120"), 1)]


def test_B_백테스트_일봉_로드는_최신_리비전(daily_store):
    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..",
                                    "tools"))
    import ab_backtest_b as bt

    bars = bt.load_all_bars({SID}, store=daily_store)
    assert [(b[0], b[3]) for b in bars[SID]] == [(D1, 110.0), (D2, 120.0)]


# ─────────────────────────────────────────────────────────────
# 분봉 reader
# ─────────────────────────────────────────────────────────────

BAR_START = datetime(2026, 9, 1, 9, 0, tzinfo=KST)


def _minute(close, run_id, rev, start=BAR_START):
    c = Decimal(str(close))
    start_utc = start.astimezone(timezone.utc)
    end_utc = start_utc + timedelta(minutes=1)
    return {"security_id": SID, "period_min": 1,
            "bar_start_at_utc": start_utc, "bar_end_at_utc": end_utc,
            "trading_date": start.date(), "session": "REGULAR",
            "open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 10,
            "traded_value": None,
            "source_timestamp_raw": start.strftime("%Y%m%d%H%M%S"),
            "timestamp_semantics": "BAR_START", "available_at_utc": end_utc,
            "source": "TEST", "quality_status": "SUCCESS", "quality_flags": [],
            "run_id": run_id, "market_rule_version": "TEST",
            "schema_version": "2.0.0", "data_revision": rev}


@pytest.fixture
def minute_store(tmp_path):
    """09:00 봉은 리비전 1(close 100)·2(close 110), 09:01 봉은 리비전 1만."""
    st = ParquetStore(root=str(tmp_path))
    part = lambda r: [("year", r["trading_date"].year)]
    st.write("silver", "minute_bars",
             [_minute(100, *OLD),
              _minute(105, *OLD, start=BAR_START + timedelta(minutes=1))],
             MINUTE_BAR_COLUMNS, MINUTE_BAR_PK, OLD[0], partition_fn=part)
    st.write("silver", "minute_bars", [_minute(110, *NEW)],
             MINUTE_BAR_COLUMNS, MINUTE_BAR_PK, NEW[0], partition_fn=part)
    return st


def test_분봉_load_bars는_최신_리비전(minute_store):
    from cvd_obv.ingestion.minute_history import MinuteHistoryCache

    t = MinuteHistoryCache(store=minute_store).load_bars(SID, period=1)
    assert [float(c) for c in t.column("close").to_pylist()] == [110.0, 105.0]


def test_replay_분봉_창은_최신_리비전(minute_store):
    from cvd_obv.replay.windows import MinuteWindow

    window = MinuteWindow(minute_store, [SID], D1, D1, period=1)
    bars = window.slice_as_of(CODE, datetime(2026, 9, 1, 15, 30, tzinfo=KST))
    assert [(b["dt"], b["close"]) for b in bars] == [
        ("20260901090000", 110.0), ("20260901090100", 105.0)]

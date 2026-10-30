# -*- coding: utf-8 -*-
"""수정주가 소급 재조정(2026-10-21)과 9/14 전 일봉의 NXT 메인마켓 소급.

1. 같은 날을 다시 받았는데 종가가 바뀌면 소급으로 감지한다(같으면 감지하지 않는다).
2. 복원 봉 시가가 원본 시가와 다르면 그날을 다시 복원한다.
3. 9/14 전 일봉: 기준 행 + NXT 30분봉 메인마켓(09:00~15:29) 합. 다시 돌려도 같고,
   기준 행이 다시 받아지면(수정주가) 따라서 다시 만든다.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from cvd_obv.bar_standard import read_standard_daily
from cvd_obv.contracts.market_schema import (
    DAILY_BAR_COLUMNS, DAILY_BAR_PK, MINUTE_BAR_COLUMNS, MINUTE_BAR_PK,
)
from cvd_obv.domain.ids import KST
from cvd_obv.ingestion.adjustment_watch import adjustment_events
from cvd_obv.ingestion.nxt_history import NxtHistoryBuilder
from cvd_obv.ingestion.nxt_volume import FLAG_NXT_INCLUDED
from cvd_obv.storage.parquet_store import ParquetStore

pytest.importorskip("pyarrow")

SID = "KRX:000001"
UTC = timezone.utc
D = date(2026, 3, 3)


def _daily(d, open_, close, scope="REGULAR", volume=1000, run_id="r1", at=None,
           source="KIWOOM_KA10081", rev=1, flags=()):
    c = Decimal(str(close)); o = Decimal(str(open_))
    return {"security_id": SID, "trading_date": d, "open": o, "high": max(o, c) + 1,
            "low": min(o, c) - 1, "close": c, "volume": volume, "traded_value": None,
            "price_scope": scope, "adjustment_type": "SPLIT_ADJUSTED",
            "adjustment_factor": None,
            "adjustment_as_of_utc": at or datetime(2026, 3, 4, tzinfo=UTC),
            "available_at_utc": datetime(d.year, d.month, d.day, 7, tzinfo=UTC),
            "quality_status": "SUCCESS", "quality_flags": list(flags), "source": source,
            "market_rule_version": "T", "run_id": run_id, "schema_version": "2.0.0",
            "data_revision": rev}


def _write(st, rows, run_id):
    st.write("silver", "daily_bars", rows, DAILY_BAR_COLUMNS, DAILY_BAR_PK, run_id,
             partition_fn=lambda r: [("year", r["trading_date"].year)], part_key=run_id)


def _nxt30(st, d, spec, run_id="n1"):
    rows = []
    for (hh, mm), vol in spec:
        start = datetime(d.year, d.month, d.day, hh, mm, tzinfo=KST).astimezone(UTC)
        rows.append({"security_id": "NXT:000001", "period_min": 30, "trading_date": d,
                     "session": "REGULAR", "bar_start_at_utc": start,
                     "bar_end_at_utc": start + timedelta(minutes=30),
                     "open": Decimal(1), "high": Decimal(1), "low": Decimal(1),
                     "close": Decimal(1), "volume": vol, "traded_value": None,
                     "source_timestamp_raw": "x", "timestamp_semantics": "BAR_START",
                     "available_at_utc": start, "quality_status": "SUCCESS",
                     "quality_flags": [], "source": "T", "market_rule_version": "T",
                     "run_id": run_id, "schema_version": "2.0.0", "data_revision": 1})
    st.write("silver", "minute_bars", rows, MINUTE_BAR_COLUMNS, MINUTE_BAR_PK, run_id,
             partition_fn=lambda r: [("year", r["trading_date"].year)])


def test_다시_받은_종가가_바뀌면_소급으로_감지한다(tmp_path):
    st = ParquetStore(root=str(tmp_path))
    _write(st, [_daily(D, 100, 100, run_id="a", at=datetime(2026, 3, 4, tzinfo=UTC))], "a")
    _write(st, [_daily(D, 100, 100, run_id="b", at=datetime(2026, 3, 5, tzinfo=UTC))], "b")
    assert adjustment_events(st, D, D) == {}
    _write(st, [_daily(D, 99.23, 99.23, run_id="c", at=datetime(2026, 4, 1, tzinfo=UTC))], "c")
    ev = adjustment_events(st, D, D)
    assert SID in ev and abs(ev[SID]["ratio"] - 0.9923) < 1e-6


def test_다음_거래일_전에_받은_최근_종가는_시간외_폭_안의_변화를_넘어간다(tmp_path):
    """2026-10-26 실측 — 10-23(금) 종가를 토요일 아침에 받으면 시간외 가격이었다."""
    st = ParquetStore(root=str(tmp_path))
    fri = date(2026, 10, 23)
    sat_0844 = datetime(2026, 10, 24, 8, 44, tzinfo=KST).astimezone(UTC)
    mon_1610 = datetime(2026, 10, 26, 16, 10, tzinfo=KST).astimezone(UTC)
    tue_0830 = datetime(2026, 10, 27, 8, 30, tzinfo=KST).astimezone(UTC)
    kis = "KIS_FHKST03010100"
    _write(st, [_daily(fri, 1, 213500, scope="EXTENDED", run_id="a", at=sat_0844,
                       source=kis)], "a")
    _write(st, [_daily(fri, 1, 215500, scope="EXTENDED", run_id="b", at=mon_1610,
                       source=kis)], "b")
    assert adjustment_events(st, fri, fri) == {}           # ×1.0094 — 시간외 폭 안
    # 확정 뒤(다음 거래일 09:00 이후) 두 번 받은 값끼리는 작은 소급도 잡는다
    _write(st, [_daily(fri, 1, 213800, scope="EXTENDED", run_id="c", at=tue_0830,
                       source=kis)], "c")
    assert adjustment_events(st, fri, fri)[SID]["ratio"] < 0.995


def test_확정_전_수집이어도_분할처럼_큰_변화는_바로_잡는다(tmp_path):
    """290650 2026-10-23 60,200 → 30,150(1:2) — 토요일 수집과의 비교라도 소급이다."""
    st = ParquetStore(root=str(tmp_path))
    fri = date(2026, 10, 23)
    kis = "KIS_FHKST03010100"
    _write(st, [_daily(fri, 1, 60200, scope="EXTENDED", run_id="a", source=kis,
                       at=datetime(2026, 10, 24, 8, 44, tzinfo=KST).astimezone(UTC))], "a")
    _write(st, [_daily(fri, 1, 30150, scope="EXTENDED", run_id="b", source=kis,
                       at=datetime(2026, 10, 26, 8, 34, tzinfo=KST).astimezone(UTC))], "b")
    assert abs(adjustment_events(st, fri, fri)[SID]["ratio"] - 0.50083) < 1e-4


def test_일일_적재는_일봉을_확인_구간보다_앞에서부터_받는다():
    from cvd_obv.cli.main import READJUST_LOOKBACK_DAYS, daily_load_steps

    steps = daily_load_steps(date(2026, 10, 26), since=date(2026, 10, 23))
    build = next(s for s in steps if s[:2] == ["daily-cache", "build"])
    readjust = next(s for s in steps if s[:2] == ["daily-cache", "readjust"])
    want = f"{date(2026, 10, 23) - timedelta(days=READJUST_LOOKBACK_DAYS)}"
    assert build[build.index("--start") + 1] == want
    assert readjust[readjust.index("--since") + 1] == want


def test_9월14일_전_일봉에_NXT_메인마켓을_더하고_다시_돌려도_같다(tmp_path):
    st = ParquetStore(root=str(tmp_path))
    _write(st, [_daily(D, 100, 101, volume=1000)], "base")
    # 프리 08:30(500) · 메인 09:00(70)·15:00(30) · 애프터 15:30(9)
    _nxt30(st, D, [((8, 30), 500), ((9, 0), 70), ((15, 0), 30), ((15, 30), 9)])
    b = NxtHistoryBuilder(st)
    out = b.build("h1", date(2025, 10, 1), date(2026, 9, 14), {SID})
    assert out["written"] == 1
    row = read_standard_daily(st, columns=["security_id", "volume", "quality_flags",
                                           "data_revision"]).to_pylist()[0]
    assert row["volume"] == 1100 and FLAG_NXT_INCLUDED in row["quality_flags"]
    assert row["data_revision"] == 2
    again = b.build("h2", date(2025, 10, 1), date(2026, 9, 14), {SID})
    assert again["written"] == 0 and again["up_to_date"] == 1


def test_기준_일봉이_다시_받아지면_NXT_포함_봉도_다시_만든다(tmp_path):
    st = ParquetStore(root=str(tmp_path))
    _write(st, [_daily(D, 100, 101, volume=1000)], "base")
    _nxt30(st, D, [((9, 0), 100)])
    b = NxtHistoryBuilder(st)
    b.build("h1", date(2025, 10, 1), date(2026, 9, 14), {SID})
    _write(st, [_daily(D, 99.23, 100.22, volume=1000, run_id="zz_refetch")], "zz_refetch")
    out = b.build("h2", date(2025, 10, 1), date(2026, 9, 14), {SID})
    assert out["rebuilt"] == 1
    row = read_standard_daily(st, columns=["security_id", "open", "volume"]).to_pylist()[0]
    assert float(row["open"]) == 99.23 and row["volume"] == 1100


def test_복원_봉_시가가_원본과_다르면_다시_복원_대상이다(tmp_path):
    from cvd_obv.ingestion.regular_from_minute import DERIVED_SOURCE, RegularFromMinuteBuilder
    st = ParquetStore(root=str(tmp_path))
    d = date(2026, 9, 22)
    _write(st, [_daily(d, 99.23, 98, scope="EXTENDED"),
                _daily(d, 100, 101, source=DERIVED_SOURCE, rev=2)], "x")
    targets, _ = RegularFromMinuteBuilder(st)._targets(d, d, {SID})
    assert (SID, d) in targets


def test_API에서_못_받은_종목은_대조_못_함으로_센다(tmp_path):
    """2026-10-21 실측: 무응답을 '어긋남 없음'으로 넘겨 351종목 대조가 사실상 0건이었다."""
    from cvd_obv.ingestion.adjustment_watch import audit_against_api

    class _R:
        def __init__(self, rows, status):
            self.rows, self.status = rows, status

    class _Src:
        def token(self):
            return "T"

        def fetch_security(self, code, token=None, start=None):
            if code == "000002":
                return _R([], "FAILED")
            return _R([{"trading_date": D, "close": Decimal("99.23")}], "SUCCESS")

    st = ParquetStore(root=str(tmp_path))
    _write(st, [_daily(D, 100, 100)], "a")
    events, failed = audit_against_api(["000001", "000002"], store=st, source=_Src(),
                                       start=date(2026, 1, 1))
    assert SID in events and abs(events[SID]["ratio"] - 0.9923) < 1e-6
    assert failed == ["000002"]


def _nxt1(st, d, spec, run_id="topup_load_20261028132000_aaaa0001"):
    """NXT 1분봉 저장(커버리지 포함) — spec: [((시, 분), 종가, 거래량)]."""
    from cvd_obv.baseline.scanner_v15 import load_scanner_v15
    from cvd_obv.ingestion.kiwoom_minute import MinuteFetchResult
    from cvd_obv.ingestion.minute_history import MinuteHistoryCache

    v15 = load_scanner_v15()

    class _Src:
        scanner = v15

        def token(self):
            return "T"

        def fetch_security(self, code, token=None, period=1, max_pages=None, min_date=None):
            bars = []
            for (hh, mm), px, vol in spec:
                start = datetime(d.year, d.month, d.day, hh, mm)
                bars.append({"trading_date": d, "bar_start": start,
                             "bar_end": start + timedelta(minutes=1),
                             "session": v15.session_of(start.strftime("%Y%m%d%H%M%S")),
                             "open": Decimal(px), "high": Decimal(px), "low": Decimal(px),
                             "close": Decimal(px), "volume": vol, "traded_value": None,
                             "source_timestamp_raw": start.strftime("%Y%m%d%H%M%S")})
            return MinuteFetchResult(code, period, bars, 1, len(bars), True, "SUCCESS")

    cache = MinuteHistoryCache(store=st)
    cache.source = _Src()
    cache.build(["NXT:000001"], run_id=run_id, period=1)


def test_저장된_NXT_1분봉이_있으면_30분봉보다_먼저_쓴다(tmp_path):
    st = ParquetStore(root=str(tmp_path))
    _write(st, [_daily(D, 100, 101, volume=1000)], "base")
    _nxt30(st, D, [((9, 0), 999)])
    # 프리 08:10(400) · 메인 09:00(70)·15:19(30) · 애프터 15:45(5)
    _nxt1(st, D, [((8, 10), 100, 400), ((9, 0), 100, 70), ((15, 19), 101, 30),
                  ((15, 45), 101, 5)])
    out = NxtHistoryBuilder(st).build("h1", date(2025, 10, 1), date(2026, 9, 14), {SID})
    assert out["from_1m"] == 1 and out["from_30m"] == 0 and out["rescaled"] == 0
    row = read_standard_daily(st, columns=["security_id", "volume",
                                           "quality_flags"]).to_pylist()[0]
    assert row["volume"] == 1100 and FLAG_NXT_INCLUDED in row["quality_flags"]


def test_NXT_1분봉과_기준_일봉의_수정주가_기준이_다르면_계수로_나눈다(tmp_path):
    from cvd_obv.ingestion.nxt_volume import FLAG_NXT_RESCALED

    st = ParquetStore(root=str(tmp_path))
    # 일봉은 1:2 분할 뒤 기준(종가 50.5), NXT 1분봉은 분할 전 기준(종가 101)
    _write(st, [_daily(D, 50, 50.5, volume=2000)], "base")
    _nxt1(st, D, [((9, 0), 100, 70), ((15, 19), 101, 30)])
    out = NxtHistoryBuilder(st).build("h1", date(2025, 10, 1), date(2026, 9, 14), {SID})
    assert out["rescaled"] == 1
    row = read_standard_daily(st, columns=["security_id", "volume",
                                           "quality_flags"]).to_pylist()[0]
    assert row["volume"] == 2200 and FLAG_NXT_RESCALED in row["quality_flags"]

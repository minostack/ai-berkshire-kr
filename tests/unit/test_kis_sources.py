# -*- coding: utf-8 -*-
"""KIS 적재 원천(2026-10-23) — 1분봉·일봉 어댑터, 누적거래대금, 열 추가 호환.

1. 1분봉: 날짜를 넘는 커서 페이지, `min_date`에서 멈춤, NXT는 시장 구분 NX.
2. 누적거래대금 → 봉 거래대금(같은 날 직전 봉과 차이). 첫 봉을 받았는지 모르는 날은 비운다.
3. 일봉: 100행 페이지를 뒤로 넘기고, 9/14 이후 정산된 행에 `KIS_PRICE_REGULAR_SESSION`,
   다음 거래일 09:00 전에 받은 행에 `KIS_LAST_DAY_UNSETTLED`.
4. 옛 Parquet 파일에 없는 열(`cum_traded_value`)을 읽어도 null로 채운다.
5. 수정주가 소급 감지는 원천이 다른 수집끼리 비교하지 않는다.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from cvd_obv.contracts.market_schema import (
    DAILY_BAR_COLUMNS, DAILY_BAR_PK, MINUTE_BAR_COLUMNS, MINUTE_BAR_PK,
)
from cvd_obv.domain.errors import ConfigError
from cvd_obv.domain.ids import KST
from cvd_obv.ingestion.adjustment_watch import adjustment_events
from cvd_obv.ingestion.kis_daily import FLAG_KIS_REGULAR_PRICE, KisDailySource
from cvd_obv.ingestion.kis_minute import (
    FLAG_NO_FIRST_VALUE, KIS_MINUTE_SOURCE, KisMinuteSource, kis_market_code,
)
from cvd_obv.ingestion.minute_history import MinuteHistoryCache
from cvd_obv.storage.parquet_store import ParquetStore

pytest.importorskip("pyarrow")
UTC = timezone.utc


class FakeClient:
    """요청 파라미터 → 응답. 호출 기록을 남긴다."""

    def __init__(self, respond):
        self.respond = respond
        self.calls = []
        self.last_error = None

    def get(self, path, params, tr_id):
        self.calls.append(dict(params))
        return self.respond(params)

    def token(self):
        return "T"


def _minute(day, hhmm, vol, cum, price=100):
    return {"stck_bsop_date": day, "stck_cntg_hour": f"{hhmm}00",
            "stck_prpr": str(price), "stck_oprc": str(price),
            "stck_hgpr": str(price + 1), "stck_lwpr": str(price - 1),
            "cntg_vol": str(vol), "acml_tr_pbmn": str(cum)}


# 두 거래일: 10/21 15:29·15:30, 10/22 09:00·09:01·15:30 (최신이 앞)
_BARS = [
    _minute("20261022", "1530", 30, 6000),
    _minute("20261022", "0901", 20, 3000),
    _minute("20261022", "0900", 10, 1000),
    _minute("20261021", "1530", 5, 900),
    _minute("20261021", "1529", 4, 400),
]


def _paged(bars, size=2):
    def respond(params):
        cur = params["FID_INPUT_DATE_1"] + params["FID_INPUT_HOUR_1"]
        rows = [b for b in bars
                if b["stck_bsop_date"] + b["stck_cntg_hour"] <= cur][:size]
        return {"rt_cd": "0", "output2": rows}
    return respond


def _source(client):
    return KisMinuteSource(client=client,
                           clock=lambda: datetime(2026, 10, 23, 8, 30, tzinfo=KST))


def test_market_code():
    assert kis_market_code("NXT:005930") == ("NX", "005930")
    assert kis_market_code("KRX:005930") == ("J", "005930")
    assert kis_market_code("005930") == ("J", "005930")


def test_minute_paging_crosses_days_and_stops_at_min_date():
    client = FakeClient(_paged(_BARS))
    res = _source(client).fetch_security("005930", min_date="20261022")
    assert res.status == "SUCCESS" and res.stop_reason == "REACHED_MIN_DATE"
    assert [b["dt"][8:12] for b in res.bars] == ["0900", "0901", "1530"]
    # 시작 커서는 오늘 235959, 다음 커서는 가장 오래된 봉 1분 전
    assert client.calls[0]["FID_INPUT_DATE_1"] == "20261023"
    assert client.calls[0]["FID_INPUT_HOUR_1"] == "235959"
    assert client.calls[1]["FID_INPUT_HOUR_1"] == "090000"
    assert all(c["FID_PW_DATA_INCU_YN"] == "Y" for c in client.calls)


def test_minute_traded_value_from_cumulative():
    res = _source(FakeClient(_paged(_BARS))).fetch_security("005930",
                                                           min_date="20261022")
    by = {b["dt"][8:12]: b for b in res.bars}
    assert by["0900"]["traded_value"] == Decimal(1000)      # 그날 첫 봉 = 누적
    assert by["0901"]["traded_value"] == Decimal(2000)
    assert by["1530"]["traded_value"] == Decimal(3000)
    assert by["1530"]["cum_traded_value"] == Decimal(6000)


def test_minute_first_bar_value_unknown_at_horizon():
    res = _source(FakeClient(_paged(_BARS))).fetch_security("005930")
    assert res.stop_reason == "EMPTY_PAGE"
    oldest = res.bars[0]
    assert oldest["dt"].startswith("202610211529")
    assert oldest["traded_value"] is None
    assert FLAG_NO_FIRST_VALUE in oldest["quality_flags"]
    assert res.bars[1]["traded_value"] == Decimal(500)


def test_minute_nxt_and_failure():
    client = FakeClient(lambda p: None)
    res = _source(client).fetch_security("NXT:005930", min_date="20261022")
    assert client.calls[0]["FID_COND_MRKT_DIV_CODE"] == "NX"
    assert res.status == "FAILED" and res.error_code == "API_NO_RESPONSE"
    with pytest.raises(ConfigError):
        _source(client).fetch_security("005930", period=5)


def test_minute_rows_keep_cum_value_and_source(tmp_path):
    src = _source(FakeClient(_paged(_BARS)))
    res = src.fetch_security("005930", min_date="20261022")
    cache = MinuteHistoryCache(store=ParquetStore(str(tmp_path)), source=src)
    rows = cache.to_silver_rows(res, "r1", datetime(2026, 10, 23, tzinfo=UTC))
    assert {r["source"] for r in rows} == {KIS_MINUTE_SOURCE}
    assert rows[-1]["cum_traded_value"] == Decimal(6000)
    assert rows[-1]["traded_value"] == Decimal(3000)


def _daily_row(d, close, vol=100):
    return {"stck_bsop_date": d, "stck_oprc": str(close), "stck_hgpr": str(close + 5),
            "stck_lwpr": str(close - 5), "stck_clpr": str(close),
            "acml_vol": str(vol), "acml_tr_pbmn": str(vol * close)}


def test_daily_paging_and_flags():
    days = [date(2026, 5, 1).fromordinal(date(2026, 5, 1).toordinal() + i)
            for i in range(160)]
    rows = [_daily_row(d.strftime("%Y%m%d"), 1000 + i) for i, d in enumerate(days)]

    def respond(params):
        lo, hi = params["FID_INPUT_DATE_1"], params["FID_INPUT_DATE_2"]
        got = [r for r in rows if lo <= r["stck_bsop_date"] <= hi]
        return {"rt_cd": "0", "output2": list(reversed(got))[:100]}

    client = FakeClient(respond)
    res = KisDailySource(client=client).fetch_security(
        "005930", start=days[0], end=days[-1])
    assert res.status == "SUCCESS" and len(res.rows) == 160 and res.pages == 2
    assert client.calls[0]["FID_ORG_ADJ_PRC"] == "0"
    by = {r["trading_date"]: r for r in res.rows}
    assert FLAG_KIS_REGULAR_PRICE in by[date(2026, 9, 14)]["quality_flags"]
    assert FLAG_KIS_REGULAR_PRICE not in by[date(2026, 9, 11)]["quality_flags"]
    assert by[days[0]]["traded_value"] == Decimal(100 * 1000)    # 원 단위


def test_daily_last_day_unsettled_before_next_weekday_9am():
    """금요일 행을 토요일·월요일 08:59에 받으면 미정산, 월요일 09:00부터 정산(실측 10/23·10/30)."""
    from cvd_obv.ingestion.kis_daily import FLAG_KIS_UNSETTLED

    fri, thu = date(2026, 10, 30), date(2026, 10, 29)
    rows = [_daily_row("20261030", 1000), _daily_row("20261029", 990)]

    def fetch(at):
        src = KisDailySource(client=FakeClient(lambda p: {"rt_cd": "0", "output2": rows}),
                             clock=lambda: at)
        return {r["trading_date"]: r["quality_flags"]
                for r in src.fetch_security("005930", start=thu, end=fri).rows}

    for at in (datetime(2026, 10, 31, 8, 47, tzinfo=KST), datetime(2026, 11, 2, 8, 59, tzinfo=KST)):
        by = fetch(at)
        assert FLAG_KIS_UNSETTLED in by[fri] and FLAG_KIS_REGULAR_PRICE not in by[fri]
        assert FLAG_KIS_REGULAR_PRICE in by[thu]          # 금요일 09:00이 지났다
    by = fetch(datetime(2026, 11, 2, 9, 0, tzinfo=KST))
    assert FLAG_KIS_REGULAR_PRICE in by[fri] and FLAG_KIS_UNSETTLED not in by[fri]


def _minute_row(sid, at, run_id, with_cum):
    row = {"security_id": sid, "period_min": 1, "bar_start_at_utc": at,
           "bar_end_at_utc": at, "trading_date": at.date(), "session": "REGULAR",
           "open": Decimal(1), "high": Decimal(1), "low": Decimal(1),
           "close": Decimal(1), "volume": 1, "traded_value": None,
           "source_timestamp_raw": "x", "timestamp_semantics": "BAR_START",
           "available_at_utc": at, "source": "S", "quality_status": "SUCCESS",
           "quality_flags": [], "run_id": run_id, "market_rule_version": "v",
           "schema_version": "2.0.0", "data_revision": 1}
    if with_cum:
        row["cum_traded_value"] = Decimal(77)
    return row


def test_old_files_without_new_column(tmp_path):
    store = ParquetStore(str(tmp_path))
    old_cols = [c for c in MINUTE_BAR_COLUMNS if c.name != "cum_traded_value"]
    t1 = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 10, 2, 0, 0, tzinfo=UTC)
    store.write("silver", "minute_bars", [_minute_row("KRX:1", t1, "r1", False)],
                old_cols, MINUTE_BAR_PK, "r1")
    store.write("silver", "minute_bars", [_minute_row("KRX:1", t2, "r2", True)],
                MINUTE_BAR_COLUMNS, MINUTE_BAR_PK, "r2")
    t = store.read_arrow("silver", "minute_bars",
                         columns=["security_id", "bar_start_at_utc", "cum_traded_value"])
    got = sorted(zip(t.column("bar_start_at_utc").to_pylist(),
                     t.column("cum_traded_value").to_pylist()))
    assert [v for _, v in got] == [None, Decimal(77)]
    assert store.read_arrow("silver", "minute_bars").num_rows == 2
    assert len(store.read("silver", "minute_bars",
                          columns=["security_id", "cum_traded_value"])) == 2


def _drow(d, close, source, run_id, at):
    c = Decimal(str(close))
    return {"security_id": "KRX:1", "trading_date": d, "open": c, "high": c,
            "low": c, "close": c, "volume": 1, "traded_value": None,
            "price_scope": "EXTENDED", "adjustment_type": "SPLIT_ADJUSTED",
            "adjustment_factor": None, "adjustment_as_of_utc": at,
            "available_at_utc": at, "source": source, "quality_status": "SUCCESS",
            "quality_flags": [], "run_id": run_id, "market_rule_version": "v",
            "schema_version": "2.0.0", "data_revision": 1}


def test_adjustment_not_compared_across_sources(tmp_path):
    store = ParquetStore(str(tmp_path))
    d = date(2026, 10, 20)
    at1 = datetime(2026, 10, 21, tzinfo=UTC)
    at2 = datetime(2026, 10, 23, tzinfo=UTC)
    # 키움 EXTENDED 종가(애프터마켓) 209,000 → KIS 종가(정규장) 211,500: 소급이 아니다
    store.write("silver", "daily_bars", [_drow(d, 209000, "KIWOOM_KA10081", "r1", at1)],
                DAILY_BAR_COLUMNS, DAILY_BAR_PK, "r1")
    store.write("silver", "daily_bars", [_drow(d, 211500, "KIS_FHKST03010100", "r2", at2)],
                DAILY_BAR_COLUMNS, DAILY_BAR_PK, "r2")
    assert adjustment_events(store, d, d) == {}
    # 같은 원천에서 바뀌면 감지한다
    store.write("silver", "daily_bars",
                [_drow(d, 210000, "KIS_FHKST03010100", "r3",
                       datetime(2026, 10, 24, tzinfo=UTC))],
                DAILY_BAR_COLUMNS, DAILY_BAR_PK, "r3")
    assert "KRX:1" in adjustment_events(store, d, d)


def test_benchmark_paging_and_anchor():
    from cvd_obv.ingestion.kis_benchmark import KisBenchmarkSource

    days = [date(2024, 1, 2).fromordinal(date(2024, 1, 2).toordinal() + i)
            for i in range(120)]
    rows = [{"stck_bsop_date": d.strftime("%Y%m%d"), "bstp_nmix_prpr": "376.34"
             if d == date(2024, 4, 2) else "300.00", "bstp_nmix_oprc": "300.00",
             "bstp_nmix_hgpr": "400.00", "bstp_nmix_lwpr": "290.00",
             "acml_vol": "1000"} for d in days]

    def respond(params):
        lo, hi = params["FID_INPUT_DATE_1"], params["FID_INPUT_DATE_2"]
        got = [r for r in rows if lo <= r["stck_bsop_date"] <= hi]
        return {"rt_cd": "0", "output2": list(reversed(got))[:50]}

    client = FakeClient(respond)
    res = KisBenchmarkSource(client=client).fetch_benchmark(
        "KRX:KOSPI200", start=days[0], end=days[-1])
    assert res.status == "SUCCESS" and len(res.rows) == 120 and res.pages == 3
    assert client.calls[0]["FID_INPUT_ISCD"] == "2001"
    assert client.calls[0]["FID_COND_MRKT_DIV_CODE"] == "U"
    assert "ANCHOR_MISMATCH" not in res.quality_flags
    assert res.rows[0]["close"] == Decimal("300.00")


def test_live_minutes_local_past_and_kis_today(tmp_path):
    """지난 날은 로컬, 오늘은 KIS. 로컬에 빠진 날이 있으면 그날까지 KIS로 받는다."""
    from datetime import timedelta
    from types import SimpleNamespace

    from cvd_obv.live_minutes import KisLiveMinutes

    store = ParquetStore(str(tmp_path))
    d1, d2, today = date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23)

    def mrow(sid, d, hhmm):
        start = datetime(d.year, d.month, d.day, int(hhmm[:2]), int(hhmm[2:]),
                         tzinfo=KST).astimezone(UTC)
        r = _minute_row(sid, start, "r1", False)
        r.update(bar_end_at_utc=start + timedelta(minutes=1), trading_date=d,
                 volume=7)
        return r

    rows = [mrow("KRX:000001", d1, "0900"), mrow("KRX:000001", d2, "0900"),
            mrow("KRX:000002", d2, "0900")]          # 000002는 d1이 없다
    store.write("silver", "minute_bars", rows, MINUTE_BAR_COLUMNS, MINUTE_BAR_PK, "r1",
                partition_fn=MinuteHistoryCache._partition)

    class Daily:
        def fetch_security(self, code, start=None, end=None):
            return SimpleNamespace(rows=[{"trading_date": d1}, {"trading_date": d2}])

    asked = []

    class Src:
        client = SimpleNamespace(calls=0)
        clock = None

        def fetch_security(self, code, period=1, min_date=None):
            asked.append((code, min_date))
            self.client.calls += 1
            s = datetime(2026, 10, 23, 9, 0)
            return SimpleNamespace(status="SUCCESS", bars=[{
                "dt": "20261023090000", "open": Decimal(1), "high": Decimal(1),
                "low": Decimal(1), "close": Decimal(1), "volume": 3,
                "bar_start": s, "bar_end": s + timedelta(minutes=1),
                "session": "REGULAR"}])

    from cvd_obv.baseline.scanner_v15 import load_scanner_v15
    live = KisLiveMinutes(load_scanner_v15(), store=store, source=Src(),
                          daily_source=Daily())
    as_of = datetime(2026, 10, 23, 15, 40)
    got = live.bars("000001", bdays=2, as_of=as_of)
    assert [b["dt"][:8] for b in got] == ["20261021", "20261022", "20261023"]
    assert asked[-1] == ("KRX:000001", "20261023")           # 오늘만 KIS
    live.bars("000002", bdays=2, as_of=as_of)
    assert asked[-1] == ("KRX:000002", "20261021")           # 빠진 날까지 KIS
    assert live.fallback == {"000002": [d1]}
    assert live.local_hits == 1


def test_universe_snapshot_picks_labelled_members():
    from types import SimpleNamespace
    from cvd_obv.ingestion.kis_universe import KisUniverseSource

    labels = {"A": "KOSPI200", "B": "KOSPI", "C": "KOSPI200", "D": "KOSPI200"}

    class Krx:
        def stock_caps(self, bas_dd, market):
            return {"A": 30, "B": 20, "C": 10} if bas_dd == "20261022" else {}

        def checksum(self, members, bas_dd, market):
            return {"bas_dd": bas_dd, "diff": 0, "target": 1, "ours": 1}

    client = FakeClient(lambda p: {"rt_cd": "0", "output": {
        "rprs_mrkt_kor_name": labels[p["FID_INPUT_ISCD"]]}})
    src = KisUniverseSource(client=client, krx=Krx(), top=2,
                            previous={"KOSPI200": ["D"]})
    res = src.fetch("KOSPI200", as_of=date(2026, 10, 23))
    # 시총 상위 2(A, B) + 직전 명단(D) — C는 후보 밖
    assert res["codes"] == ["A", "D"] and res["candidates"] == 3
    assert res["checksum"]["diff"] == 0 and res["source"] == "KIS_FHKST01010100"


def test_cum_value_backfill_fills_and_resumes(tmp_path):
    """누적거래대금이 없는 (종목, 날)만 대상 → KIS 행을 새 run으로 쓴다 → 다시 보면 대상 없음."""
    from datetime import timedelta
    from types import SimpleNamespace

    from cvd_obv.ingestion.cum_value_backfill import CumValueBackfill, cum_value_targets

    store = ParquetStore(str(tmp_path))
    d = date(2026, 9, 15)
    start = datetime(2026, 9, 15, 9, 0, tzinfo=KST).astimezone(UTC)
    old = _minute_row("KRX:000001", start, "topup_load_20260916000000_aaaaaaaa", False)
    old.update(trading_date=d, bar_end_at_utc=start + timedelta(minutes=1))
    store.write("silver", "minute_bars", [old], MINUTE_BAR_COLUMNS, MINUTE_BAR_PK,
                old["run_id"], partition_fn=MinuteHistoryCache._partition)
    targets = cum_value_targets(store, date(2026, 9, 1), date(2026, 9, 30))
    assert targets == {"KRX:000001": [d]}

    class Src:
        SOURCE, TIMESTAMP_SEMANTICS = KIS_MINUTE_SOURCE, "BAR_START"

        def __init__(self):
            self.client = SimpleNamespace(calls=0)

        def fetch_security(self, code, period=1, min_date=None):
            self.client.calls += 2
            s = datetime(2026, 9, 15, 9, 0)
            bar = {"dt": "20260915090000", "bar_start": s,
                   "bar_end": s + timedelta(minutes=1), "trading_date": d,
                   "session": "REGULAR", "open": Decimal(1), "high": Decimal(1),
                   "low": Decimal(1), "close": Decimal(1), "volume": 1,
                   "traded_value": Decimal(5), "cum_traded_value": Decimal(5),
                   "source_timestamp_raw": "20260915090000"}
            return SimpleNamespace(status="SUCCESS", bars=[bar], quality_flags=[],
                                   security_code=code, period_min=1)

    summary = CumValueBackfill(store, workers=2, source_factory=Src, log=None).run(
        targets, "topup_load_20261023000000_bbbbbbbb")
    assert summary["filled_pairs"] == 1 and summary["api_calls"] == 2
    assert cum_value_targets(store, date(2026, 9, 1), date(2026, 9, 30)) == {}

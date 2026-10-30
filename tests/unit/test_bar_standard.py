# -*- coding: utf-8 -*-
"""봉 기준 `bars-1.0` — `src/cvd_obv/bar_standard.py`와 그것을 쓰는 경로.

지키는 것
1. API 일봉은 받은 시각이 성격을 정한다(15:35 전·16:00~20:05 미확정, 15:35~16:00 정규장).
2. 적재 단계가 미확정 당일 봉을 버리고, 15:35~16:00 수집분은 REGULAR로 저장한다.
3. 같은 날 EXTENDED 원본과 정규장 복원본이 있어도 reader는 하루 한 행(정규장)을 쓴다.
   feature·label·replay·백테스트 평가가격이 모두 같은 입구를 쓴다.
4. 빈 거래대금 추정 규칙은 하나다(EXTENDED 공식 거래대금 × 거래량 비율 → 종가×거래량).
5. N분봉은 1분봉을 합성한다. Arrow 경로와 파이썬 경로가 같은 값을 낸다.
6. replay 분봉 공급기는 기본으로 1분봉을 합성한다.
7. 일일 적재 순서는 1분봉 → 일봉 → 정규장 복원이다.
"""

import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from cvd_obv.bar_standard import (
    FETCH_EXTENDED, FETCH_INCOMPLETE, FETCH_REGULAR, StandardDailyCache,
    classify_fetched_daily, read_standard_daily, resample_bars,
    resample_minute_table, select_standard_daily, unrestored_days,
)
from cvd_obv.contracts.market_schema import (
    DAILY_BAR_COLUMNS, DAILY_BAR_PK, MINUTE_BAR_COLUMNS, MINUTE_BAR_PK,
)
from cvd_obv.domain.errors import ConfigError
from cvd_obv.domain.ids import KST
from cvd_obv.storage.parquet_store import ParquetStore

pa = pytest.importorskip("pyarrow")

SID = "KRX:000001"
PRE = date(2026, 9, 11)      # 애프터마켓 시행 전
POST = date(2026, 9, 22)     # 시행 후


def _at(day, hh, mm):
    return datetime(day.year, day.month, day.day, hh, mm)


# ─────────────────────────────────────────────────────────────
# 1. 받은 시각으로 성격 판정
# ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("day,hh,mm,expected", [
    (POST, 15, 20, FETCH_INCOMPLETE),     # 장중
    (POST, 15, 34, FETCH_INCOMPLETE),     # 종가 확정 여유 전
    (POST, 15, 35, FETCH_REGULAR),        # 정규장 확정 ~ 애프터마켓 전
    (POST, 15, 59, FETCH_REGULAR),
    (POST, 16, 0, FETCH_INCOMPLETE),      # 애프터마켓 진행 중
    (POST, 20, 4, FETCH_INCOMPLETE),
    (POST, 20, 5, FETCH_EXTENDED),        # 애프터마켓 확정
    (PRE, 15, 34, FETCH_INCOMPLETE),
    (PRE, 15, 35, FETCH_REGULAR),
    (PRE, 18, 0, FETCH_REGULAR),          # 시행 전에는 16:00 뒤에도 정규장 확정값
])
def test_당일_일봉_성격은_받은_시각으로_정한다(day, hh, mm, expected):
    assert classify_fetched_daily(day, _at(day, hh, mm)) == expected


def test_과거_봉은_그날의_scope다():
    assert classify_fetched_daily(POST, _at(POST + timedelta(days=1), 9, 0)) == FETCH_EXTENDED
    assert classify_fetched_daily(PRE, _at(POST, 9, 0)) == FETCH_REGULAR


def test_tz_aware_시각도_KST로_해석한다():
    utc = datetime(2026, 9, 22, 6, 40, tzinfo=timezone.utc)     # 15:40 KST
    assert classify_fetched_daily(POST, utc) == FETCH_REGULAR


# ─────────────────────────────────────────────────────────────
# 2. 적재 단계
# ─────────────────────────────────────────────────────────────

class _Fetch:
    def __init__(self, days):
        self.security_code = "000001"
        self.status = "SUCCESS"
        self.quality_flags = []
        self.rows = [{"trading_date": d, "open": Decimal("100"), "high": Decimal("110"),
                      "low": Decimal("90"), "close": Decimal("105"), "volume": 1000,
                      "traded_value": Decimal("105000")} for d in days]


@pytest.mark.parametrize("hh,mm,scope,flagged", [
    (15, 20, None, False),
    (15, 40, "REGULAR", True),
    (17, 0, None, False),
    (21, 0, "EXTENDED", False),
])
def test_적재는_미확정_당일_봉을_버리고_정규장_창은_REGULAR로_저장(tmp_path, hh, mm, scope, flagged):
    from cvd_obv.ingestion.daily_history import (
        FETCHED_IN_REGULAR_WINDOW, DailyHistoryRepository,
    )
    repo = DailyHistoryRepository(store=ParquetStore(root=str(tmp_path)))
    fetched = datetime(2026, 9, 22, hh, mm, tzinfo=KST)
    rows = repo.to_silver_rows(_Fetch([date(2026, 9, 21), POST]), "r", fetched)
    by_day = {r["trading_date"]: r for r in rows}
    assert by_day[date(2026, 9, 21)]["price_scope"] == "EXTENDED"   # 확정된 과거 봉
    if scope is None:
        assert POST not in by_day
        assert repo.last_incomplete_skipped == 1
    else:
        assert by_day[POST]["price_scope"] == scope
        assert (FETCHED_IN_REGULAR_WINDOW in by_day[POST]["quality_flags"]) is flagged


# ─────────────────────────────────────────────────────────────
# 3·4. 하루 한 행, 거래대금 추정
# ─────────────────────────────────────────────────────────────

def _daily(day, close, scope, volume=100, traded_value="auto", sid=SID,
           adjustment="SPLIT_ADJUSTED", status="SUCCESS", run_id="r1"):
    c = Decimal(str(close))
    if traded_value == "auto":
        traded_value = Decimal(str(close * volume))
    return {"security_id": sid, "trading_date": day,
            "open": c, "high": c + 1, "low": c - 1, "close": c, "volume": volume,
            "traded_value": traded_value, "price_scope": scope,
            "adjustment_type": adjustment, "adjustment_factor": None,
            "adjustment_as_of_utc": None,
            "available_at_utc": datetime(day.year, day.month, day.day, 11,
                                         tzinfo=timezone.utc),
            "quality_status": status, "quality_flags": [], "source": "TEST",
            "market_rule_version": "TEST", "run_id": run_id,
            "schema_version": "2.0.0", "data_revision": 1}


@pytest.fixture
def store(tmp_path):
    """D1 정규장 원천, D2 EXTENDED + 정규장 복원본(거래대금 없음), D3 EXTENDED만."""
    st = ParquetStore(root=str(tmp_path))
    d1, d2, d3 = date(2026, 9, 11), date(2026, 9, 14), date(2026, 9, 15)
    rows = [_daily(d1, 100, "REGULAR"),
            _daily(d2, 120, "EXTENDED", volume=110, traded_value=Decimal("13860")),
            _daily(d2, 110, "REGULAR", volume=100, traded_value=None),
            _daily(d3, 130, "EXTENDED", volume=100),
            _daily(d1, 999, "REGULAR", adjustment="RAW")]
    st.write("silver", "daily_bars", rows, DAILY_BAR_COLUMNS, DAILY_BAR_PK, "r1",
             partition_fn=lambda r: [("year", r["trading_date"].year)])
    return st


def test_기준_일봉은_하루_한_행_정규장_수정주가만(store):
    t = read_standard_daily(store, columns=["security_id", "trading_date", "close"])
    got = sorted(zip(t.column("trading_date").to_pylist(),
                     [float(c) for c in t.column("close").to_pylist()]))
    assert got == [(date(2026, 9, 11), 100.0), (date(2026, 9, 14), 110.0)]


def test_allow_extended면_정규장_봉이_없는_날만_EXTENDED로_채운다(store):
    t = read_standard_daily(store, columns=["trading_date", "close"], allow_extended=True)
    got = sorted(zip(t.column("trading_date").to_pylist(),
                     t.column("price_scope").to_pylist()))
    assert got == [(date(2026, 9, 11), "REGULAR"), (date(2026, 9, 14), "REGULAR"),
                   (date(2026, 9, 15), "EXTENDED")]


def test_빈_거래대금은_EXTENDED_공식값_비율로_추정(store):
    t = read_standard_daily(store, columns=["trading_date", "traded_value"],
                            fill_traded_value=True)
    rows = {r["trading_date"]: r for r in t.to_pylist()}
    d2 = rows[date(2026, 9, 14)]
    assert d2["traded_value_est"] is True
    assert d2["traded_value"] == pytest.approx(13860 * 100 / 110)
    assert rows[date(2026, 9, 11)]["traded_value_est"] is False


def test_EXTENDED가_없으면_종가x거래량으로_추정():
    t = pa.Table.from_pylist([_daily(POST, 50, "REGULAR", volume=10, traded_value=None)])
    out = select_standard_daily(t, fill_traded_value=True).to_pylist()[0]
    assert out["traded_value"] == 500 and out["traded_value_est"] is True


def test_정규장_복원_전인_날을_센다(store):
    assert unrestored_days(store) == {date(2026, 9, 15): 1}


def test_src_reader들이_같은_날_두_행을_읽지_않는다(store):
    """feature·label·replay·백테스트 평가가격 — 수정 전에는 9/14가 두 번 나왔다."""
    from cvd_obv.backtest.engine import BacktestEngine
    from cvd_obv.features.repository import DailyFeatureRepository
    from cvd_obv.labels.repository import DailyForwardReturnRepository
    from cvd_obv.replay.windows import DailyWindow

    grouped, _ = DailyFeatureRepository(store=store)._load_series()
    assert [b["close"] for b in grouped[SID]] == [100.0, 110.0]

    series, _, _ = DailyForwardReturnRepository(store=store)._load_series()
    assert len(series) == 1                     # 중복 거래일이면 ConfigError

    window = DailyWindow(store, ["000001"], date(2026, 9, 11), date(2026, 9, 15))
    assert [b["dt"] for b in window._bars["000001"]] == ["20260911", "20260914"]

    marks = BacktestEngine(store)._load_marks({SID}, date(2026, 9, 1), date(2026, 9, 30))
    assert marks[(SID, date(2026, 9, 14))] == 110.0


def test_실시간_일봉_공급기는_로컬_기준_봉에_당일_정규장_봉만_붙인다(store):
    live = [{"dt": "20260916", "open": 1, "high": 2, "low": 1, "close": 2, "volume": 5}]
    at_1540 = StandardDailyCache(["000001"], _at(date(2026, 9, 16), 15, 40), store=store)
    bars = at_1540.bars("000001", live_fetch=lambda c: live)
    assert [b["dt"] for b in bars] == ["20260911", "20260914", "20260916"]
    at_1700 = StandardDailyCache(["000001"], _at(date(2026, 9, 16), 17, 0), store=store)
    assert [b["dt"] for b in at_1700.bars("000001", live_fetch=lambda c: live)] == [
        "20260911", "20260914"]


# ─────────────────────────────────────────────────────────────
# 5. N분봉 합성
# ─────────────────────────────────────────────────────────────

def _minute_rows():
    """정규장 09:00~09:06, 15:19, 15:30(종가), 15:32 OTHER(늦은 종가 체결), 16:00 AFTER."""
    spec = [((9, 0), "REGULAR", 100, 10), ((9, 1), "REGULAR", 101, 20),
            ((9, 4), "REGULAR", 99, 5), ((9, 5), "REGULAR", 102, 7),
            ((9, 6), "REGULAR", 103, 1), ((15, 19), "REGULAR", 104, 3),
            ((15, 30), "REGULAR", 105, 50), ((15, 32), "OTHER", 105, 4),
            ((16, 0), "AFTER", 106, 8)]
    out = []
    for (hh, mm), session, px, vol in spec:
        start = datetime(2026, 9, 22, hh, mm, tzinfo=KST).astimezone(timezone.utc)
        out.append({"security_id": SID, "trading_date": POST, "session": session,
                    "bar_start_at_utc": start, "open": Decimal(str(px)),
                    "high": Decimal(str(px + 1)), "low": Decimal(str(px - 1)),
                    "close": Decimal(str(px)), "volume": vol})
    return out


def test_1분봉을_5분봉으로_합성한다():
    t = resample_minute_table(pa.Table.from_pylist(_minute_rows()), 5).to_pylist()
    got = [(r["bar_start_at_utc"].astimezone(KST).strftime("%H:%M"), r["session"],
            r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in t]
    assert got == [
        ("09:00", "REGULAR", 100, 102, 98, 99, 35),
        ("09:05", "REGULAR", 102, 104, 101, 103, 8),
        ("15:15", "REGULAR", 104, 105, 103, 104, 3),
        ("15:30", "REGULAR", 105, 106, 104, 105, 54),    # 15:32 OTHER가 합쳐진다
        ("16:00", "AFTER", 106, 107, 105, 106, 8),
    ]
    assert all(r["bar_end_at_utc"] - r["bar_start_at_utc"] == timedelta(minutes=5) for r in t)


def test_60분봉에서는_15시30분_봉이_15시_봉에_들어간다():
    t = resample_minute_table(pa.Table.from_pylist(_minute_rows()), 60).to_pylist()
    starts = [r["bar_start_at_utc"].astimezone(KST).strftime("%H:%M") for r in t]
    assert starts == ["09:00", "15:00", "16:00"]


def test_파이썬_합성과_Arrow_합성이_같다():
    rows = _minute_rows()
    arrow = resample_minute_table(pa.Table.from_pylist(rows), 5).to_pylist()
    py = resample_bars([{"dt": r["bar_start_at_utc"].astimezone(KST).strftime("%Y%m%d%H%M%S"),
                         "open": float(r["open"]), "high": float(r["high"]),
                         "low": float(r["low"]), "close": float(r["close"]),
                         "volume": r["volume"], "session": r["session"]} for r in rows], 5)
    assert [(b["open"], b["high"], b["low"], b["close"], b["volume"]) for b in py] == \
        [(a["open"], a["high"], a["low"], a["close"], a["volume"]) for a in arrow]


def test_끝나지_않은_묶음은_버린다():
    bars = [{"dt": "20260922153000", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
            {"dt": "20260922153100", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]
    assert resample_bars(bars, 5, as_of=datetime(2026, 9, 22, 15, 33)) == []
    assert len(resample_bars(bars, 5, as_of=datetime(2026, 9, 22, 15, 35))) == 1


def test_시계_정각에_맞물리지_않는_주기는_합성하지_않는다():
    with pytest.raises(ConfigError):
        resample_bars([], 45)


# ─────────────────────────────────────────────────────────────
# 6. replay 분봉 공급기
# ─────────────────────────────────────────────────────────────

def test_replay_분봉은_기본으로_1분봉을_합성한다(tmp_path):
    from cvd_obv.replay.windows import MinuteWindow

    st = ParquetStore(root=str(tmp_path))
    rows = []
    for r in _minute_rows():
        end = r["bar_start_at_utc"] + timedelta(minutes=1)
        rows.append(dict(r, period_min=1, bar_end_at_utc=end, traded_value=None,
                         source_timestamp_raw=r["bar_start_at_utc"].astimezone(KST)
                         .strftime("%Y%m%d%H%M%S"),
                         timestamp_semantics="BAR_START", available_at_utc=end,
                         quality_status="SUCCESS", quality_flags=[], source="TEST",
                         market_rule_version="TEST", run_id="m1",
                         schema_version="2.0.0", data_revision=1))
    st.write("silver", "minute_bars", rows, MINUTE_BAR_COLUMNS, MINUTE_BAR_PK, "m1",
             partition_fn=lambda r: [("year", r["trading_date"].year)])
    window = MinuteWindow(st, ["000001"], POST, POST, period=5)
    bars = window.slice_as_of("000001", datetime(2026, 9, 22, 15, 35, tzinfo=KST))
    assert [b["dt"][8:12] for b in bars] == ["0900", "0905", "1515", "1530"]
    assert bars[-1]["volume"] == 54


# ─────────────────────────────────────────────────────────────
# 7. 일일 적재 순서·실행 입구
# ─────────────────────────────────────────────────────────────

def test_일일_적재는_1분봉_잠정_일봉_정규장복원_순서():
    from cvd_obv.cli.main import daily_load_steps

    steps = daily_load_steps(date(2026, 10, 2), days=10)
    assert [s[:2] for s in steps] == [["minute-cache", "catchup"],
                                     ["minute-cache", "catchup"],      # NXT
                                     ["daily-cache", "provisional"],
                                     ["daily-cache", "build"], ["daily-cache", "build"],
                                     ["daily-cache", "readjust"],
                                     ["daily-cache", "regular-from-minute"],
                                     ["trade-sides", "daily"]]
    assert "--missing-only" in steps[2]
    assert steps[0][steps[0].index("--period") + 1] == "1"
    assert steps[1][steps[1].index("--venue") + 1] == "NXT"


def test_CVD_OBV_입구는_일봉_완료를_정규장으로_채운다():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))
    import cvd_obv_scan

    assert cvd_obv_scan.standard_argv(["scan", "--index", "kospi200"])[-2:] == [
        "--daily-scope", "regular"]
    assert cvd_obv_scan.standard_argv(["scan", "--daily-scope", "auto"]).count(
        "--daily-scope") == 1


def test_실시간_분봉_공급기는_적재_1분봉을_받아_합성한다():
    """2026-10-26: 분봉은 **적재분만** 읽는다 — V1.5 fetch 함수를 부르지 않는다."""
    from datetime import timedelta
    from cvd_obv.live_feed import StandardLiveFeed

    v15_calls = []

    class FakeV15:
        @staticmethod
        def fetch_minute_bars(code, token, period=5, bdays=5, as_of=None):
            v15_calls.append(period)          # 불리면 안 된다
            return []

        fetch_daily_bars = staticmethod(lambda *a, **k: [])

        @staticmethod
        def session_of(dt):
            return "REGULAR"

    base = datetime(2026, 9, 22, 9, 0)
    stored = [{"dt": (base + timedelta(minutes=m)).strftime("%Y%m%d%H%M%S"),
               "open": 1.0, "high": 2.0, "low": 1.0, "close": 2.0, "volume": 1,
               "bar_start": base + timedelta(minutes=m),
               "bar_end": base + timedelta(minutes=m + 1),
               "session": "REGULAR"} for m in range(10)]

    feed = StandardLiveFeed(FakeV15(), as_of=datetime(2026, 9, 22, 15, 40))
    feed._minutes.bars = lambda code, bdays, as_of: list(stored)
    bars = feed.fetch_minute_bars("000001", "T", period=5, bdays=5)
    assert v15_calls == []                    # API 경로가 사라졌다
    assert [(b["dt"], b["volume"]) for b in bars] == [("20260922090000", 5),
                                                      ("20260922090500", 5)]


def test_as_of를_주지_않으면_적재_상태가_기준일을_정한다(monkeypatch):
    """장중에 돌려도 오늘 봉을 끌어오지 않는다(규칙은 test_scan_day.py)."""
    from cvd_obv import bar_standard as bs
    from cvd_obv.live_feed import StandardLiveFeed

    class FakeV15:
        fetch_minute_bars = staticmethod(lambda *a, **k: [])
        fetch_daily_bars = staticmethod(lambda *a, **k: [])
        session_of = staticmethod(lambda dt: "REGULAR")

    want = bs.ScanDay(date(2026, 10, 23), False, False, "테스트")
    monkeypatch.setattr("cvd_obv.live_feed.resolve_scan_day",
                        lambda **k: want)
    feed = StandardLiveFeed(FakeV15())
    assert feed.scan_day is want
    assert feed.as_of == want.end_of_day
    assert any("2026-10-23" in l for l in feed.banner_lines())


def test_적재_run_id는_기존_수동_id보다_나중으로_정렬되고_시각을_담는다():
    """reader는 같은 리비전이면 사전순으로 나중 run_id를 쓴다. 자동 id `cli_…`는
    `topup-…`·`min1-…`보다 앞이라 새 확정값이 옛 값에 가려졌다."""
    from cvd_obv.cli.main import data_load_run_id
    from cvd_obv.domain.ids import run_started_at

    at = datetime(2026, 10, 3, 11, 30, tzinfo=timezone.utc)
    rid = data_load_run_id(at)
    for existing in ("topup-20261002153750-kospi200", "min1-20260926",
                     "cli_daily_cache_20260923180609_34e64384",
                     "cli_minute_cache_20261002064054_097d3eed"):
        assert rid > existing
    assert run_started_at(rid) == at
    assert data_load_run_id(at + timedelta(days=1)) > rid

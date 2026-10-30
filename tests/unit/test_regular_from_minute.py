# -*- coding: utf-8 -*-
"""1분봉으로 정규장 일봉 복원 — `ingestion/regular_from_minute.py`."""

from datetime import datetime, time, timezone

from cvd_obv.ingestion.regular_from_minute import (
    FLAG_ADJUSTED, FLAG_FETCH_UNKNOWN, FLAG_LATE_CLOSE_PRINT, FLAG_NO_VALUE,
    FLAG_VOLUME_GAP, derive_regular_bar, fetched_at_of,
)

UTC = timezone.utc
# 2026-09-29 애프터마켓 확정 20:00 KST + 여유 → 11:xx UTC
AFTER_FINAL = datetime(2026, 9, 29, 11, 10, tzinfo=UTC)


def _m(hh, mm, session, o, h, l, c, v):
    return {"kst": time(hh, mm), "session": session, "open": o, "high": h,
            "low": l, "close": c, "volume": v}


def _day():
    """정규장 09:00~15:30, 장후 단일가(OTHER), 애프터마켓(AFTER)."""
    return [
        _m(9, 0, "REGULAR", 100, 103, 99, 102, 1000),
        _m(12, 0, "REGULAR", 102, 104, 101, 103, 500),
        _m(15, 30, "REGULAR", 103, 103, 103, 103, 300),   # 종가 단일가
        _m(15, 35, "OTHER", 103, 103, 103, 103, 20),
        _m(16, 0, "AFTER", 103, 108, 103, 107, 150),
        _m(19, 59, "AFTER", 107, 107, 97, 98, 30),
    ]


def _ext(**kw):
    # 키움 EXTENDED 일봉: 종가 = 애프터마켓 최종가, 거래량에 애프터마켓 포함
    bar = {"open": 100, "high": 108, "low": 97, "close": 98, "volume": 2000}
    bar.update(kw)
    return bar


def test_종가는_15시30분_봉이고_거래량은_애프터마켓을_뺀다():
    bar, reason, flags = derive_regular_bar(_ext(), _day())
    assert reason == "OK"
    assert bar["close"] == 103          # 애프터마켓 98이 아니다
    assert bar["open"] == 100
    assert bar["volume"] == 2000 - 180  # EXTENDED − AFTER(150+30)
    assert FLAG_NO_VALUE in flags


def test_애프터마켓에서_나온_고가_저가는_정규장_분봉으로_바꾼다():
    bar, _, _ = derive_regular_bar(_ext(), _day())
    assert bar["high"] == 104 and bar["low"] == 99


def test_EXTENDED_고가가_애프터마켓보다_높으면_정규장_고가다():
    # 분봉이 정규장 최고가 봉을 빠뜨려도 EXTENDED 고가가 맞다
    bar, _, _ = derive_regular_bar(_ext(high=110), _day())
    assert bar["high"] == 110


def test_15시30분_봉이_없으면_만들지_않는다():
    minutes = [m for m in _day() if m["kst"] != time(15, 30)]
    bar, reason, _ = derive_regular_bar(_ext(), minutes)
    assert bar is None and reason == "NO_CLOSE_AUCTION_BAR"


def test_정규장_분봉이_없으면_만들지_않는다():
    bar, reason, _ = derive_regular_bar(_ext(), [])
    assert bar is None and reason == "NO_REGULAR_MINUTES"


def test_분봉_가격이_조정되지_않았으면_시가_비율로_맞춘다():
    # 일봉은 수정주가, 분봉은 원주가 — 시가 비율 2배
    bar, _, flags = derive_regular_bar(_ext(open=200, high=216, low=194,
                                            volume=1000), _day())
    assert FLAG_ADJUSTED in flags
    assert bar["close"] == 206
    assert bar["volume"] == 1000 - 90     # 애프터마켓 180주 → 수정 90주


def test_분봉이_정규장_체결을_많이_빠뜨리면_표시한다():
    bar, _, flags = derive_regular_bar(_ext(volume=5000), _day())
    assert bar["volume"] == 4820
    assert FLAG_VOLUME_GAP in flags


def _fetched(minutes, when):
    return [dict(m, fetched_at=when) for m in minutes]


def test_애프터마켓_확정_전에_받은_분봉으로는_만들지_않는다():
    # 16:10 KST 일일 누적이 받은 날 — 애프터마켓 봉이 덜 들어 있다
    early = datetime(2026, 9, 29, 7, 10, tzinfo=UTC)
    bar, reason, _ = derive_regular_bar(_ext(), _fetched(_day(), early),
                                        after_final_at=AFTER_FINAL)
    assert bar is None and reason == "AFTER_MARKET_NOT_FINAL"


def test_애프터마켓_확정_뒤에_받았으면_만든다():
    late = datetime(2026, 9, 30, 1, 0, tzinfo=UTC)
    bar, reason, flags = derive_regular_bar(_ext(), _fetched(_day(), late),
                                            after_final_at=AFTER_FINAL)
    assert reason == "OK" and FLAG_FETCH_UNKNOWN not in flags


def test_수집_시각을_모르면_만들되_표시한다():
    bar, reason, flags = derive_regular_bar(_ext(), _fetched(_day(), None),
                                            after_final_at=AFTER_FINAL)
    assert reason == "OK" and FLAG_FETCH_UNKNOWN in flags


def test_run_id에서_수집_시각을_읽는다():
    assert (fetched_at_of("cli_minute_cache_20261002011936_3f5ca096")
            == datetime(2026, 10, 2, 1, 19, 36, tzinfo=UTC))
    # 날짜만 있으면 그날 00:00 KST = 전날 15:00 UTC (보수적 하한)
    assert (fetched_at_of("min1-20260926")
            == datetime(2026, 9, 25, 15, 0, tzinfo=UTC))
    assert fetched_at_of("manual") is None


# ── 늦게 찍힌 종가 단일가 체결 (closing_bet_scanner 즉석 복원에서 옮겨 온 규칙) ──

def test_종가_체결이_15시32분_OTHER로_찍혀도_공식_종가로_쓴다():
    """2026-09-17 삼성전기 실측: 정규장 마지막 15:19, 종가 체결 15:32(OTHER),
    15:35 시간외 종가는 같은 가격."""
    minutes = [
        _m(9, 0, "REGULAR", 100, 103, 99, 102, 1000),
        _m(15, 19, "REGULAR", 102, 102, 101, 101, 300),
        _m(15, 32, "OTHER", 99, 99, 99, 99, 400),          # 종가 단일가
        _m(15, 35, "OTHER", 99, 99, 99, 99, 20),           # 시간외 종가 — 정규장 아님
        _m(16, 0, "AFTER", 99, 100, 98, 100, 150),
    ]
    bar, reason, flags = derive_regular_bar(_ext(volume=1870), minutes)
    assert reason == "OK"
    assert bar["close"] == 99
    assert bar["volume"] == 1870 - 150
    assert FLAG_LATE_CLOSE_PRINT in flags


def test_15시35분_시간외_종가만_있으면_만들지_않는다():
    minutes = [_m(9, 0, "REGULAR", 100, 101, 99, 100, 10),
               _m(15, 19, "REGULAR", 100, 101, 99, 100, 10),
               _m(15, 35, "OTHER", 100, 100, 100, 100, 5)]
    bar, reason, _ = derive_regular_bar(_ext(), minutes)
    assert bar is None and reason == "NO_CLOSE_AUCTION_BAR"


def test_OTHER만_있고_정규장_분봉이_없으면_만들지_않는다():
    bar, reason, _ = derive_regular_bar(
        _ext(), [_m(15, 32, "OTHER", 99, 99, 99, 99, 400)])
    assert bar is None and reason == "NO_REGULAR_MINUTES"

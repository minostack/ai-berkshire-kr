#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/kiwoom_cvd_obv_scanner.py V1.5 세션 모델 단위 테스트.

CVD_OBV_Scanner_V1_4_Review_2026-09-14.md 5절 "필수 회귀 사례"를 그대로
검증한다: 9/11 이전과 9/14 이후 혼합, 15:29/15:30/16:00/16:05/19:59/20:00
경계, 애프터마켓 비대상·무체결 종목, 하루 전체 누락, 동일 시각 재실행,
60분봉의 30분 Momentum 요청, 토큰 만료 필드.

기대값은 구현을 복사하지 않고 제도(정규장 390분·애프터마켓 240분,
2026-09-14 시행)에서 직접 계산한 값을 쓴다.
"""

import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

import kiwoom_cvd_obv_scanner as sc


# ─────────────────────────────────────────────────────────────
# 헬퍼 — 합성 분봉
# ─────────────────────────────────────────────────────────────

def make_bar(dt: str, close: float = 100.0, vol: int = 1000,
             period: int = 5) -> dict:
    """양봉 1개(매수 우위)를 만든다. 세션·봉 경계는 구현 함수로 채운다."""
    o = close - 1.0
    h = close + 0.5
    l = o - 0.5
    start, end = sc.bar_bounds(dt, period)
    return {"dt": dt, "open": o, "high": h, "low": l, "close": close,
            "volume": vol, "bar_start": start, "bar_end": end,
            "session": sc.session_of(dt)}


def session_bars(date: str, start_hhmm: int, count: int, period: int = 5,
                 close: float = 100.0) -> list:
    """date의 start_hhmm부터 period분 간격으로 count개 봉을 만든다."""
    bars = []
    t = datetime.strptime(f"{date}{start_hhmm:04d}", "%Y%m%d%H%M")
    for i in range(count):
        bars.append(make_bar(t.strftime("%Y%m%d%H%M%S"), close=close + i * 0.01,
                             period=period))
        t = t + __import__("datetime").timedelta(minutes=period)
    return bars


def regular_day(date: str, period: int = 5) -> list:
    """
    정규장 하루치 — 2026-09-16 ka10080 실측 형태를 그대로 재현한다.
    접속매매 09:00~15:20 구간 봉 + 15:30 종가 단일가 봉(봉 경계일 때만).
    5분봉이면 09:00~15:15의 76봉 + 15:30봉 = 77봉.
    """
    import math
    bars = session_bars(date, 900, math.ceil(380 / period), period=period)
    if 390 % period == 0:
        bars.append(make_bar(f"{date}153000", close=101.0, period=period))
    return bars


def off_session_bar(date: str, period: int = 5) -> list:
    """15:35 시간외종가 봉 1개 — 실측상 정규장 뒤에 소량으로 1봉 존재."""
    return [make_bar(f"{date}153500", close=101.0, vol=50, period=period)]


def after_day(date: str, period: int = 5) -> list:
    """애프터마켓 하루치(16:00~19:55 시작 기준, 5분봉 48개)."""
    return session_bars(date, 1600, sc.expected_bars_per_day(sc._SESSION_AFTER, period),
                        period=period)


def full_day(date: str, period: int = 5) -> list:
    """실측 하루치: 정규장 77 + 시간외종가 1 + 애프터마켓 48 = 126봉(5분봉)."""
    return regular_day(date, period) + off_session_bar(date, period) + after_day(date, period)


# ─────────────────────────────────────────────────────────────
# 1. 세션 분류 — 15:30~16:00은 어느 세션도 아니다
# ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("dt,expected", [
    ("20260914090000", sc._SESSION_REGULAR),
    ("20260914152900", sc._SESSION_REGULAR),
    ("20260914153000", sc._SESSION_REGULAR),   # 종가 단일가까지 정규장
    ("20260914154000", sc._SESSION_OTHER),     # 시간외종가 구간 — 정규장으로 채우지 않는다
    ("20260914160000", sc._SESSION_AFTER),
    ("20260914195500", sc._SESSION_AFTER),
    ("20260914200000", sc._SESSION_AFTER),
    ("20260914203000", sc._SESSION_OTHER),
    # 시행일 이전 16~18시는 폐지된 시간외단일가 — 애프터마켓이 아니다
    ("20260911163000", sc._SESSION_OTHER),
    ("20260911140000", sc._SESSION_REGULAR),
])
def test_session_of(dt, expected):
    assert sc.session_of(dt) == expected


def test_after_market_effective_date():
    assert sc.after_market_active("20260914") is True
    assert sc.after_market_active("20260913") is False


# ─────────────────────────────────────────────────────────────
# 2. 세션 완료 판정 — 경계 시각(리뷰 5절 필수 회귀 사례)
# ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("as_of,session,expected", [
    ("2026-09-14 15:29", sc._SESSION_REGULAR, False),
    ("2026-09-14 15:30", sc._SESSION_REGULAR, False),   # 수신지연 여유 5분
    ("2026-09-14 15:34", sc._SESSION_REGULAR, False),
    ("2026-09-14 15:35", sc._SESSION_REGULAR, True),
    ("2026-09-14 16:05", sc._SESSION_AFTER,   False),
    ("2026-09-14 19:59", sc._SESSION_AFTER,   False),
    ("2026-09-14 20:00", sc._SESSION_AFTER,   False),
    ("2026-09-14 20:05", sc._SESSION_AFTER,   True),
])
def test_is_session_complete_boundaries(as_of, session, expected):
    as_of_dt = datetime.strptime(as_of, "%Y-%m-%d %H:%M")
    assert sc.is_session_complete("20260914", session, as_of_dt) is expected


def test_after_session_absent_before_effective_date():
    """시행일 이전 날짜에는 애프터마켓 세션 자체가 없다 — 항상 미완료."""
    as_of = datetime(2026, 9, 12, 23, 0)
    assert sc.session_end_dt("20260911", sc._SESSION_AFTER) is None
    assert sc.is_session_complete("20260911", sc._SESSION_AFTER, as_of) is False


def test_daily_bar_completion_scope():
    """
    리뷰 P0-1의 핵심 회귀: 16:05에 당일 일봉을 완료로 처리하던 문제.
    auto(기본)는 애프터마켓 종료 후에만 확정하고, regular를 명시하면
    15:30+여유 기준으로 확정한다.
    """
    at_1605 = datetime(2026, 9, 14, 16, 5)
    at_2005 = datetime(2026, 9, 14, 20, 5)

    assert sc._is_bar_day_complete("20260914", at_1605, "auto") is False
    assert sc._is_bar_day_complete("20260914", at_1605, "regular") is True
    assert sc._is_bar_day_complete("20260914", at_2005, "auto") is True
    # 시행일 이전 날짜는 scope와 무관하게 정규장 기준
    assert sc._is_bar_day_complete("20260911", datetime(2026, 9, 11, 16, 5), "auto") is True


# ─────────────────────────────────────────────────────────────
# 3. 기대 봉 수 · 완성도 (리뷰 P1-1: 126봉 → 161% SUCCESS 문제)
# ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("period,regular,after", [
    # 2026-09-16 삼성전자·SK하이닉스 ka10080 실측값
    (1,  381, 240),
    (5,   77,  48),
    (30,  14,   8),
    (60,   7,   4),
])
def test_expected_bars_matches_measured_api_response(period, regular, after):
    assert sc.expected_bars_per_day(sc._SESSION_REGULAR, period) == regular
    assert sc.expected_bars_per_day(sc._SESSION_AFTER, period) == after


def test_regular_expectation_excludes_single_price_gap():
    """
    15:20~15:30 단일가 구간에는 접속 체결이 없어 분봉이 생성되지 않는다.
    5분봉 기준 09:00~15:15 76봉 + 15:30 단일가 1봉 = 77봉(390/5=78이 아니다).
    """
    bars = regular_day("20260914")
    times = sorted(sc._hhmm(b["dt"]) for b in bars)
    assert len(bars) == 77
    assert times[-2:] == [1515, 1530]
    assert 1520 not in times and 1525 not in times


def test_mixed_session_day_is_split_not_inflated():
    """
    실측 하루치(정규장 77 + 시간외종가 1 + 애프터마켓 48 = 126봉)를 넣어도
    정규장 완성도는 100%다 (V1.4에서는 126/78 = 161.5%가 나오면서 SUCCESS로
    통과했다).
    """
    as_of = datetime(2026, 9, 15, 21, 0)
    bars = full_day("20260914")
    assert len(bars) == 126      # 실측 일치: 77 + 1(시간외종가) + 48

    split = sc.split_bars_by_session(bars)
    assert len(split[sc._SESSION_REGULAR]) == 77
    assert len(split[sc._SESSION_OTHER]) == 1
    assert len(split[sc._SESSION_AFTER]) == 48

    reg = sc.calc_cvd(split[sc._SESSION_REGULAR], requested_days=1, period=5,
                      as_of=as_of, session=sc._SESSION_REGULAR)
    aft = sc.calc_cvd(split[sc._SESSION_AFTER], requested_days=None, period=5,
                      as_of=as_of, session=sc._SESSION_AFTER,
                      min_bars=sc._AFTER_MIN_BARS, absent_is_error=False)

    assert reg["completeness"] == pytest.approx(1.0)
    assert reg["data_status"] == "SUCCESS"
    assert aft["completeness"] == pytest.approx(1.0)
    assert aft["data_status"] == "SUCCESS"


def test_over_count_day_is_flagged_not_hidden():
    """
    기대치를 초과하는 봉이 들어오면 완성도를 100%로 클램프하고 PARTIAL로
    표시한다 — 초과분이 다른 날 누락을 상쇄하지 못하게 한다.
    """
    as_of = datetime(2026, 9, 15, 21, 0)
    # 1분 간격 390봉을 5분봉으로 해석 → 기대 78봉을 크게 초과
    bars = session_bars("20260914", 900, 390, period=1)
    bars = [b for b in bars if b["session"] == sc._SESSION_REGULAR]
    res = sc.calc_cvd(bars, requested_days=1, period=5, as_of=as_of,
                      session=sc._SESSION_REGULAR)
    assert res["over_count_days"] == ["20260914"]
    assert res["completeness"] == pytest.approx(1.0)
    assert res["data_status"] == "PARTIAL"


def test_boundary_bar_at_1520_is_not_over_count():
    """
    접속매매 종료 시각(15:20) 정각 체결이 있으면 정규장 봉이 78개가 된다
    (실측: LG디스플레이 2026-09-15). 이는 정상이므로 세션 혼입으로 보지
    않는다 — 상한은 expected+1이다.
    """
    as_of = datetime(2026, 9, 15, 21, 0)
    bars = regular_day("20260914")
    bars.append(make_bar("20260914152000", close=101.0))   # 15:20 정각 체결 봉
    bars.sort(key=lambda b: b["dt"])
    assert len(bars) == 78

    res = sc.calc_cvd(bars, requested_days=1, period=5, as_of=as_of,
                      session=sc._SESSION_REGULAR)
    assert res["over_count_days"] == []
    assert res["data_status"] == "SUCCESS"
    assert sc.max_bars_per_day(sc._SESSION_REGULAR, 5) == 78
    assert sc.max_bars_per_day(sc._SESSION_REGULAR, 60) == 7   # 15:20이 봉 경계가 아님
    assert sc.max_bars_per_day(sc._SESSION_AFTER, 5) == 48


def test_absent_trading_day_detected():
    """일봉에는 있는데 분봉이 통째로 없는 거래일을 잡는다(리뷰 P1-5)."""
    as_of = datetime(2026, 9, 16, 21, 0)
    bars = regular_day("20260914") + regular_day("20260916")
    res = sc.calc_cvd(bars, requested_days=3, period=5, as_of=as_of,
                      session=sc._SESSION_REGULAR,
                      trading_days=["20260914", "20260915", "20260916"])
    assert res["absent_days"] == ["20260915"]
    assert res["data_status"] == "PARTIAL"


# ─────────────────────────────────────────────────────────────
# 4. 모멘텀 — 실제 시간 구간 (리뷰 P1-4)
# ─────────────────────────────────────────────────────────────

def test_momentum_60min_bar_cannot_promise_30min():
    """60분봉으로 30분 모멘텀을 요청하면 RESOLUTION_LIMITED로 표시한다."""
    as_of = datetime(2026, 9, 14, 20, 30)
    bars = session_bars("20260914", 900, 6, period=60)
    bars = [b for b in bars if b["session"] == sc._SESSION_REGULAR]
    res = sc.calc_cvd(bars, period=60, as_of=as_of, session=sc._SESSION_REGULAR,
                      min_bars=1)
    assert res["momentum_status"] == "RESOLUTION_LIMITED"
    assert res["momentum_minutes_actual"] == 60
    assert res["momentum_bars"] == 1


def test_momentum_warming_up_and_success():
    as_of = datetime(2026, 9, 14, 20, 30)

    few = session_bars("20260914", 900, 2, period=5)
    res_few = sc.calc_cvd(few, period=5, as_of=as_of,
                          session=sc._SESSION_REGULAR, min_bars=1)
    assert res_few["momentum_status"] == "WARMING_UP"
    assert res_few["momentum_minutes_actual"] == 10

    enough = session_bars("20260914", 900, 12, period=5)
    res_ok = sc.calc_cvd(enough, period=5, as_of=as_of,
                         session=sc._SESSION_REGULAR, min_bars=1)
    assert res_ok["momentum_status"] == "SUCCESS"
    assert res_ok["momentum_minutes_actual"] == 30
    assert res_ok["momentum_bars"] == 6


def test_momentum_spans_single_price_gap():
    """
    정규장 마지막 30분(15:05~15:35)에는 15:20~15:30 단일가 구간 때문에 봉이
    4개뿐이지만, 실제 시간 구간은 30분이므로 SUCCESS여야 한다(봉 수 환산이면
    20분으로 잘못 계산된다). 봉 누락 자체는 completeness가 별도로 본다.
    """
    as_of = datetime(2026, 9, 14, 20, 30)
    bars = regular_day("20260914")
    res = sc.calc_cvd(bars, requested_days=1, period=5, as_of=as_of,
                      session=sc._SESSION_REGULAR)
    assert res["momentum_status"] == "SUCCESS"
    assert res["momentum_minutes_actual"] == 30
    assert res["momentum_bars"] == 4      # 15:05·15:10·15:15·15:30


def test_momentum_does_not_cross_sessions():
    """
    16:05 시점에 정규장 마지막 봉이 저녁 모멘텀에 섞이면 안 된다(리뷰 P1-4).
    애프터마켓 첫 봉 1개만 있을 때 모멘텀 구간은 그 봉까지만 본다.
    """
    as_of = datetime(2026, 9, 14, 16, 6)
    bars = regular_day("20260914") + session_bars("20260914", 1600, 1, period=5)
    aft = sc.split_bars_by_session(bars)[sc._SESSION_AFTER]
    res = sc.calc_cvd(aft, period=5, as_of=as_of, session=sc._SESSION_AFTER,
                      min_bars=1, absent_is_error=False)
    assert res["momentum_bars"] == 1
    assert res["momentum_minutes_actual"] == 5
    assert res["momentum_status"] == "WARMING_UP"


def test_session_open_staleness_has_warmup_grace():
    """
    장 시작 직후(09:04)에는 당일 첫 봉이 아직 완성되지 않은 게 정상이므로
    최신성 문제로 보지 않는다. 같은 조건이라도 09:30이면 진행 중 세션에서
    30분째 봉이 없다는 뜻이므로 최신성 문제로 표시한다.
    """
    bars = regular_day("20260914") + regular_day("20260915")
    days = ["20260914", "20260915", "20260916"]

    early = sc.calc_cvd(bars, requested_days=2, period=5,
                        as_of=datetime(2026, 9, 16, 9, 4),
                        session=sc._SESSION_REGULAR, trading_days=days)
    assert early["stale"] in (False, None)
    assert early["data_status"] == "SUCCESS", early["status_reasons"]

    late = sc.calc_cvd(bars, requested_days=2, period=5,
                       as_of=datetime(2026, 9, 16, 9, 30),
                       session=sc._SESSION_REGULAR, trading_days=days)
    assert late["stale"] is True
    assert late["data_status"] == "PARTIAL"
    assert any("최신성" in r for r in late["status_reasons"])


# ─────────────────────────────────────────────────────────────
# 5. 완료 거래일 기준 bull_ratio (리뷰 P2-1)
# ─────────────────────────────────────────────────────────────

def test_bull_ratio_for_rank_requires_completed_days():
    # 완료 거래일 1일뿐 → 점수·Gate용 값 없음(당일 장중 값으로 폴백 금지)
    as_of = datetime(2026, 9, 15, 11, 0)
    bars = regular_day("20260914") + session_bars("20260915", 900, 20, period=5)
    reg = sc.split_bars_by_session(bars)[sc._SESSION_REGULAR]
    res = sc.calc_cvd(reg, requested_days=2, period=5, as_of=as_of,
                      session=sc._SESSION_REGULAR)
    assert res["completed_total_days"] == 1
    assert res["completed_bull_ratio"] is not None
    assert res["bull_ratio_for_rank"] is None
    assert res["intraday_delta"] is not None

    # 완료 거래일 2일 → 사용 가능
    as_of2 = datetime(2026, 9, 16, 11, 0)
    bars2 = regular_day("20260914") + regular_day("20260915")
    res2 = sc.calc_cvd(bars2, requested_days=2, period=5, as_of=as_of2,
                       session=sc._SESSION_REGULAR,
                       trading_days=["20260914", "20260915"])
    assert res2["completed_total_days"] == 2
    assert res2["bull_ratio_for_rank"] == res2["completed_bull_ratio"]


# ─────────────────────────────────────────────────────────────
# 6. 애프터마켓 비대상 · 무체결 구분 (리뷰 P1-1)
# ─────────────────────────────────────────────────────────────

def test_after_not_applicable_before_effective_date():
    as_of = datetime(2026, 9, 11, 19, 0)
    res = sc.calc_cvd([], period=5, as_of=as_of, session=sc._SESSION_AFTER,
                      trading_days=["20260910", "20260911"],
                      min_bars=sc._AFTER_MIN_BARS, absent_is_error=False)
    assert res["data_status"] == "NOT_APPLICABLE"


def test_after_no_data_after_effective_date():
    as_of = datetime(2026, 9, 15, 21, 0)
    res = sc.calc_cvd([], period=5, as_of=as_of, session=sc._SESSION_AFTER,
                      trading_days=["20260914", "20260915"],
                      min_bars=sc._AFTER_MIN_BARS, absent_is_error=False)
    # 대상 제외인지 무체결인지 API 미제공인지는 구분할 수 없다 — 상태만 구분
    assert res["data_status"] == "NO_DATA"


def test_after_absent_day_is_not_an_error():
    """애프터마켓은 종목별 대상 여부를 알 수 없어 누락일을 결함으로 보지 않는다."""
    as_of = datetime(2026, 9, 16, 21, 0)
    bars = after_day("20260914") + after_day("20260916")
    res = sc.calc_cvd(bars, period=5, as_of=as_of, session=sc._SESSION_AFTER,
                      trading_days=["20260914", "20260915", "20260916"],
                      min_bars=sc._AFTER_MIN_BARS, absent_is_error=False)
    assert res["absent_days"] == ["20260915"]
    assert res["data_status"] == "SUCCESS"


# ─────────────────────────────────────────────────────────────
# 7. 토큰 만료 파싱 (리뷰 P1-6)
# ─────────────────────────────────────────────────────────────

def test_token_expiry_uses_expires_dt():
    at, source = sc._parse_token_expiry({"token": "x", "expires_dt": "20260916235959"})
    assert source == "expires_dt"
    assert at == datetime(2026, 9, 16, 23, 59, 59).timestamp()


def test_token_expiry_falls_back_conservatively():
    import time as _t
    at, source = sc._parse_token_expiry({"token": "x"})
    assert source == "fallback"
    # 임의 24시간 가정 금지 — 1시간 캐시
    assert 0 < at - _t.time() <= sc._TOKEN_FALLBACK_TTL + 1


# ─────────────────────────────────────────────────────────────
# 8. 스냅샷 덮어쓰기 방지 (리뷰 P2-3)
# ─────────────────────────────────────────────────────────────

def test_snapshot_does_not_overwrite(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "_SNAPSHOT_DIR", str(tmp_path))
    as_of = datetime(2026, 9, 14, 16, 5, 30)
    results = []
    p1 = sc._save_scan_snapshot(results, "kospi200", "코스피200", 5, 5, as_of,
                                run_id="abc12345")
    p2 = sc._save_scan_snapshot(results, "kospi200", "코스피200", 5, 5, as_of,
                                run_id="abc12345")
    assert p1 != p2
    assert os.path.exists(p1) and os.path.exists(p2)

    import json
    payload = json.load(open(p1, encoding="utf-8"))
    assert payload["scanner_version"] == sc._SCANNER_VERSION
    assert payload["market_rule_version"] == sc._MARKET_RULE_VERSION
    assert payload["config"]["session_config"]["after_market_start_date"] == "20260914"
    assert len(payload["config_hash"]) == 12

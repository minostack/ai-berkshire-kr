#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/kiwoom_cvd_obv_scanner.py V1.5 점수·Gate 경로 테스트.

API 호출 없이 합성 일봉·분봉으로 analyze() 이후 단계(_finalize_scores,
_compute_gate, _print_results)를 그대로 태운다. 세션 리팩터링 뒤에도
정규장 기준 점수 경로가 살아 있는지, 애프터마켓 기준(--session after)에서
정규장 추세 조건 + 애프터마켓 조건이 함께 적용되는지 확인한다.
"""

import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

import kiwoom_cvd_obv_scanner as sc
from test_cvd_obv_sessions import after_day, regular_day

AS_OF = datetime(2026, 9, 16, 10, 0)          # 정규장 진행 중(애프터마켓 닫힘)
MINUTE_DATES = ["20260909", "20260910", "20260911", "20260914", "20260915"]


def trading_dates(n: int = 150, end: str = "20260915") -> list:
    """end에서 거슬러 올라가며 주말을 제외한 n개 날짜(휴장일 미반영 근사)."""
    out = []
    d = datetime.strptime(end, "%Y%m%d")
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    return sorted(out)


def daily_bars(dates: list) -> list:
    """완만한 우상향 일봉 — MA20/60 상회·정배열, 과열(+15%) 아님."""
    bars = []
    for i, d in enumerate(dates):
        close = 100.0 + i * 0.2
        bars.append({"dt": d, "close": close, "volume": 1_000_000 + i * 100,
                     "open": close - 0.3, "high": close + 0.5, "low": close - 0.6})
    return bars


def build_result(rank_session=sc._SESSION_REGULAR, with_after=True) -> dict:
    dates = trading_dates()
    dbars = daily_bars(dates)

    min_bars = []
    for d in MINUTE_DATES:
        min_bars += regular_day(d)
    if with_after:
        min_bars += after_day("20260914") + after_day("20260915")

    split = sc.split_bars_by_session(min_bars)
    cvd_regular = sc.calc_cvd(split[sc._SESSION_REGULAR], requested_days=5,
                              period=5, as_of=AS_OF, session=sc._SESSION_REGULAR,
                              trading_days=dates)
    cvd_after = sc.calc_cvd(split[sc._SESSION_AFTER], requested_days=None,
                            period=5, as_of=AS_OF, session=sc._SESSION_AFTER,
                            trading_days=dates, min_bars=sc._AFTER_MIN_BARS,
                            absent_is_error=False)
    obv = sc.calc_obv(dbars, divergence_lookback=5)
    price = sc.calc_price_trend(dbars)
    liquidity = sc.calc_liquidity(dbars)
    risk = sc.calc_risk(dbars, close_vs_ma20_pct=price.get("close_vs_ma20_pct"))

    cvd = cvd_after if rank_session == sc._SESSION_AFTER else cvd_regular
    return {
        "code": "005930", "name": "테스트종목",
        "cvd": cvd, "cvd_regular": cvd_regular, "cvd_after": cvd_after,
        "rank_session": rank_session,
        "obv": obv, "price": price, "liquidity": liquidity, "risk": risk,
        "raw_score": cvd["score"] + obv["score"] + price["score"],
        "min_bars": len(min_bars),
        "bars_by_session": {k: len(v) for k, v in split.items()},
        "days": cvd.get("total_days", 0),
        "daily_bars_used": len(dbars),
        "daily_scope": "auto",
        "as_of": AS_OF.strftime("%Y-%m-%d %H:%M:%S"),
    }


def test_regular_session_path_produces_buy_candidate():
    r = build_result()
    assert r["cvd_regular"]["data_status"] == "SUCCESS"
    assert r["obv"]["data_status"] == "SUCCESS"

    results = [r]
    sc._finalize_scores(results)

    assert r["status"] == "BUY_CANDIDATE", r["gate_reasons"]
    assert r["gate_reasons"] == []
    assert 0 <= r["total"] <= 100
    # 단일 종목이면 백분위는 0.5(중립) — 절대 배점만 남는다
    assert r["cvd_points"] == pytest.approx(20.0)      # (20+10+10)×0.5
    assert r["obv_points"] == pytest.approx(25.0)      # MA20 상회 15 + 0.5×20
    assert r["price_points"] == pytest.approx(15.0)    # MA20/MA60/정배열 모두 충족


def test_after_session_path_uses_after_metrics_and_regular_trend():
    r = build_result(rank_session=sc._SESSION_AFTER)
    assert r["cvd_after"]["data_status"] == "SUCCESS"
    assert r["cvd_after"]["completed_total_days"] == 2

    sc._finalize_scores([r])
    assert r["status"] == "BUY_CANDIDATE", r["gate_reasons"]
    # 애프터마켓 기준이어도 지속성(bull%)은 정규장 완료 거래일 값을 쓴다
    assert r["cvd_regular"]["bull_ratio_for_rank"] is not None


def test_after_session_without_after_data_is_data_insufficient():
    r = build_result(rank_session=sc._SESSION_AFTER, with_after=False)
    assert r["cvd_after"]["data_status"] == "NO_DATA"

    sc._finalize_scores([r])
    assert r["status"] == "DATA_INSUFFICIENT"


def test_percentile_population_excludes_partial_stocks():
    good = build_result()
    bad = build_result()
    bad["code"] = "000660"
    bad["cvd_regular"]["data_status"] = "PARTIAL"
    bad["cvd"] = bad["cvd_regular"]

    results = [good, bad]
    sc._finalize_scores(results)

    # PARTIAL 종목은 모집단에서 빠져 유효값이 1개 → 백분위 0.5(중립)
    assert good["cvd_points"] == pytest.approx(20.0)
    assert bad["status"] == "DATA_INSUFFICIENT"


def test_print_results_runs(capsys):
    results = [build_result()]
    sc._finalize_scores(results)
    sc._print_results(results, 10, "테스트", 5, 5, as_of=AS_OF,
                      rank_session=sc._SESSION_REGULAR, daily_scope="auto")
    out = capsys.readouterr().out
    assert "애프터마켓" in out
    assert "테스트종목" in out
    assert "🟩후보" in out

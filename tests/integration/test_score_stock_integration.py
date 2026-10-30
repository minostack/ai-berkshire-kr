#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
score_stock() 통합 테스트 — 가이드 10단계("통합" 테스트 계층: fake provider
+ 고정 데이터 → 옵션 → 필터 → 점수 → CLI/JSON).

실제 네트워크를 타는 kiwoom_quote/fetch_ohlcv/kiwoom_investor_flow를
고정 데이터를 반환하는 가짜 함수로 교체해, score_stock()의 오케스트레이션
로직(1차 필터 → 6개 영역 점수 계산 → combine_score) 전체를 오프라인으로
검증한다. 개별 score_* 순수 함수는 tests/unit/test_scoring.py에서 이미
검증했으므로, 여기서는 "실제 데이터 조회 계층과 올바르게 엮이는가"에
집중한다.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

import daily_scanner as ds


def _make_fake_bars(n=130, start_price=100.0):
    """MA120까지 확보되는 완만한 우상향 합성 일봉(오늘 봉 포함, 완료봉 가정)."""
    rows, opens, highs, lows, closes, volumes = [], [], [], [], [], []
    price = start_price
    for i in range(n):
        o = price
        c = price + 0.4
        h = max(o, c) + 1.0
        l = min(o, c) - 1.0
        v = 100_000 + (i % 5) * 1000
        rows.append({"date": f"2026-01-{(i % 28) + 1:02d}", "open": o, "high": h,
                      "low": l, "close": c, "volume": v})
        opens.append(o); highs.append(h); lows.append(l); closes.append(c); volumes.append(v)
        price = c
    return {
        "ticker": "005930", "name": "가짜종목", "source": "kiwoom",
        "rows": rows, "opens": opens, "highs": highs, "lows": lows,
        "closes": closes, "volumes": volumes,
    }


def _fake_quote(price=50000.0, market_cap_krw=500_000_000_000, volume=10_000_000):
    return {
        "symbol": "005930", "name": "가짜종목", "price": price,
        "open": price - 100, "high": price + 500, "low": price - 500,
        "prev_close": price - 200, "volume": volume,
        "change": 200.0, "change_pct": 0.4,
        "market_cap_eok": market_cap_krw / 1e8, "market_cap_krw": market_cap_krw,
        "as_of": "2026-09-15T10:00:00", "source": "kiwoom",
    }


def _fake_investor_flow_factory(foreign=100.0, institution=50.0):
    def _fn(ticker, date=None):
        return {"date": date or "20260915", "individual": -150.0,
                "foreign": foreign, "institution": institution,
                "unit": "주", "source": "kiwoom"}
    return _fn


@pytest.fixture(autouse=True)
def _kiwoom_configured(monkeypatch):
    monkeypatch.setenv("KIWOOM_APP_KEY", "fake-key")
    monkeypatch.setenv("KIWOOM_APP_SECRET", "fake-secret")


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(ds.time, "sleep", lambda *_a, **_k: None)


def test_score_stock_success_end_to_end(monkeypatch):
    bars = _make_fake_bars(n=130)
    monkeypatch.setattr(ds, "kiwoom_quote", lambda ticker: _fake_quote())
    monkeypatch.setattr(ds, "fetch_ohlcv", lambda ticker, days=460: bars)
    monkeypatch.setattr(ds, "kiwoom_investor_flow", _fake_investor_flow_factory())

    result = ds.score_stock("005930")

    assert result["status"] == "SUCCESS"
    assert result["prefilter_passed"] is True
    assert isinstance(result["total_score"], (int, float))
    assert 0 <= result["total_score"] <= 100
    assert len(result["areas"]) == 6
    assert sum(a["max"] for a in result["areas"]) == 100


def test_score_stock_incomplete_when_investor_flow_partially_missing(monkeypatch):
    bars = _make_fake_bars(n=130)
    monkeypatch.setattr(ds, "kiwoom_quote", lambda ticker: _fake_quote())
    monkeypatch.setattr(ds, "fetch_ohlcv", lambda ticker, days=460: bars)

    call_count = {"n": 0}

    def flaky_investor_flow(ticker, date=None):
        call_count["n"] += 1
        if call_count["n"] == 2:  # 5번 중 1번만 실패해도 5일 합 전체를 결측 처리해야 함
            return None
        return {"date": date, "individual": 0.0, "foreign": 10.0,
                "institution": 5.0, "unit": "주", "source": "kiwoom"}

    monkeypatch.setattr(ds, "kiwoom_investor_flow", flaky_investor_flow)

    result = ds.score_stock("005930")

    assert result["status"] == "INCOMPLETE"
    assert result["total_score"] is None
    assert any("foreign_institution" in m for m in result["missing_components"])
    # 결측 영역(20점)을 제외한 나머지 80점만 확인 가능한 가중치여야 함
    assert result["available_weight"] == 80


def test_score_stock_prefilter_fails_but_score_still_computed(monkeypatch):
    bars = _make_fake_bars(n=130)
    # 가격 500원 < 1,000원 -> 1차 필터 미통과
    monkeypatch.setattr(ds, "kiwoom_quote", lambda ticker: _fake_quote(price=500.0, volume=1000))
    monkeypatch.setattr(ds, "fetch_ohlcv", lambda ticker, days=460: bars)
    monkeypatch.setattr(ds, "kiwoom_investor_flow", _fake_investor_flow_factory())

    result = ds.score_stock("005930")

    assert result["prefilter_passed"] is False
    assert any("1,000원" in r for r in result["prefilter_reasons"])
    # 필터 미통과여도 참고용 점수 자체는 계산되어야 함(cmd_score가 경고와 함께 표시)
    assert result["status"] in ("SUCCESS", "INCOMPLETE")


def test_score_stock_error_when_kiwoom_not_configured(monkeypatch):
    monkeypatch.delenv("KIWOOM_APP_KEY", raising=False)
    monkeypatch.delenv("KIWOOM_APP_SECRET", raising=False)

    result = ds.score_stock("005930")
    assert result["status"] == "ERROR"


def test_score_stock_error_when_quote_fails(monkeypatch):
    monkeypatch.setattr(ds, "kiwoom_quote", lambda ticker: None)

    result = ds.score_stock("005930")
    assert result["status"] == "ERROR"
    assert "시세" in result["reason"]


def test_score_stock_error_when_insufficient_bars(monkeypatch):
    monkeypatch.setattr(ds, "kiwoom_quote", lambda ticker: _fake_quote())
    monkeypatch.setattr(ds, "fetch_ohlcv", lambda ticker, days=460: _make_fake_bars(n=5))

    result = ds.score_stock("005930")
    assert result["status"] == "ERROR"
    assert "일봉" in result["reason"]


# ─────────────────────────────────────────────────────────────
# cmd_score()/cmd_quote() 반환값 — 가이드 9단계("전체 성공과 부분 실패의
# 반환 상태/종료 코드를 문서화"). main()은 이 반환값이 False일 때만
# sys.exit(1)을 호출한다.
# ─────────────────────────────────────────────────────────────

def test_cmd_score_returns_false_on_error(monkeypatch, capsys):
    monkeypatch.delenv("KIWOOM_APP_KEY", raising=False)
    assert ds.cmd_score("005930") is False


def test_cmd_score_returns_true_on_incomplete_not_just_success(monkeypatch, capsys):
    # INCOMPLETE는 도구 실패가 아니라 정직한 결과 — exit code 0(True)이어야 함
    bars = _make_fake_bars(n=130)
    monkeypatch.setattr(ds, "kiwoom_quote", lambda ticker: _fake_quote())
    monkeypatch.setattr(ds, "fetch_ohlcv", lambda ticker, days=460: bars)
    monkeypatch.setattr(ds, "kiwoom_investor_flow", lambda ticker, date=None: None)

    assert ds.cmd_score("005930") is True


def test_cmd_quote_returns_false_when_any_ticker_fails(monkeypatch, capsys):
    monkeypatch.setenv("KIWOOM_APP_KEY", "k")
    monkeypatch.setenv("KIWOOM_APP_SECRET", "s")
    responses = {"005930": _fake_quote(), "000660": None}
    monkeypatch.setattr(ds, "kiwoom_quote", lambda ticker: responses[ticker])
    monkeypatch.setattr(ds, "kiwoom_investor_flow", lambda ticker, date=None: None)

    assert ds.cmd_quote("005930", "000660") is False


def test_cmd_quote_returns_true_when_all_succeed(monkeypatch, capsys):
    monkeypatch.setenv("KIWOOM_APP_KEY", "k")
    monkeypatch.setenv("KIWOOM_APP_SECRET", "s")
    monkeypatch.setattr(ds, "kiwoom_quote", lambda ticker: _fake_quote())
    # 수급 데이터 결측은 정상 상황일 수 있으므로 종료 코드에 영향을 주면 안 됨
    monkeypatch.setattr(ds, "kiwoom_investor_flow", lambda ticker, date=None: None)

    assert ds.cmd_quote("005930", "000660") is True

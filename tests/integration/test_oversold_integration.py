#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cmd_oversold() 통합 테스트 — 가이드 10단계(fake provider + 고정 데이터로
옵션→필터→CLI 출력까지 오프라인 검증). data/watchlist.json과
fetch_ohlcv를 고정 데이터로 교체해 실제 네트워크 없이 전체 흐름을 돈다.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

import daily_scanner as ds


def _bars_with_decline(days=8, closes=None):
    """closes 리스트를 그대로 종가로 쓰는 합성 일봉(최소 필드만)."""
    if closes is None:
        closes = [100 - i for i in range(days)]  # 연속 하락
    n = len(closes)
    rows = [{"date": f"2026-09-{i+1:02d}"} for i in range(n)]
    return {
        "ticker": "000001", "name": "테스트종목", "source": "kiwoom", "rows": rows,
        "opens": closes[:], "highs": [c + 1 for c in closes], "lows": [c - 1 for c in closes],
        "closes": closes, "volumes": [10000] * n,
    }


@pytest.fixture()
def fake_watchlist(tmp_path, monkeypatch):
    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps({
        "group_a": ["005930", "000660", "005930"],  # 중복 포함
        "group_b": ["AAPL", "MSFT"],                 # 6자리 아닌 해외 티커는 제외돼야 함
    }), encoding="utf-8")
    monkeypatch.setattr(ds, "_WATCHLIST_FILE", str(path))
    return path


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(ds.time, "sleep", lambda *_a, **_k: None)


def test_oversold_deduplicates_and_filters_non_kr_tickers(fake_watchlist, monkeypatch, capsys):
    monkeypatch.setattr(ds, "fetch_ohlcv", lambda ticker: _bars_with_decline())
    monkeypatch.setattr(ds, "calc_rsi", lambda closes: 25.0)  # 항상 과매도로 고정

    ds.cmd_oversold(rsi_max=30, days=3)

    out = capsys.readouterr().out
    assert "중복 제거 전 3개" in out    # group_a에 005930이 두 번
    assert "2개" in out                 # 중복 제거 후 국내 종목 2개(005930, 000660)


def test_oversold_processes_full_list_without_30_cap(fake_watchlist, monkeypatch, capsys):
    # 35개 이상의 서로 다른 6자리 코드를 만들어 과거의 tickers[:30] 제한이
    # 사라졌는지 확인한다.
    many_tickers = [f"{i:06d}" for i in range(35)]
    fake_watchlist.write_text(json.dumps({"g": many_tickers}), encoding="utf-8")

    called_tickers = []

    def fake_fetch(ticker):
        called_tickers.append(ticker)
        return _bars_with_decline()

    monkeypatch.setattr(ds, "fetch_ohlcv", fake_fetch)
    monkeypatch.setattr(ds, "calc_rsi", lambda closes: 50.0)  # rsi_max 미달 -> 매칭 없음

    ds.cmd_oversold(rsi_max=30, days=3)

    assert len(called_tickers) == 35  # 30개 제한 없이 전체 처리


def test_oversold_declining_streak_filters_flat_days(fake_watchlist, monkeypatch, capsys):
    fake_watchlist.write_text(json.dumps({"g": ["005930"]}), encoding="utf-8")
    # days=3이므로 검사 구간은 마지막 4개(closes[-4..-1]). 그 안에 보합을
    # 넣어 3일 연속 하락이 성립하지 않도록 구성한다.
    flat_break = [100, 90, 85, 85, 80]  # closes[-1]=80 < closes[-2]=85 이지만
                                          # closes[-2]=85 < closes[-3]=85 은 거짓
    monkeypatch.setattr(ds, "fetch_ohlcv", lambda ticker: _bars_with_decline(closes=flat_break))
    monkeypatch.setattr(ds, "calc_rsi", lambda closes: 20.0)

    ds.cmd_oversold(rsi_max=30, days=3)

    out = capsys.readouterr().out
    assert "조건을" in out and "없음" in out  # 보합으로 연속 하락이 깨져 후보 없음


def test_oversold_return_nd_uses_days_parameter_not_hardcoded_5(fake_watchlist, monkeypatch, capsys):
    fake_watchlist.write_text(json.dumps({"g": ["005930"]}), encoding="utf-8")
    # 3일 연속 하락, 3일 수익률 = 70/100-1 = -30%
    closes = [100, 90, 80, 70]
    monkeypatch.setattr(ds, "fetch_ohlcv", lambda ticker: _bars_with_decline(closes=closes))
    monkeypatch.setattr(ds, "calc_rsi", lambda closes: 20.0)

    ds.cmd_oversold(rsi_max=30, days=3)

    out = capsys.readouterr().out
    assert "-30.0%" in out


def test_oversold_invalid_days_rejected_before_any_fetch(fake_watchlist, monkeypatch, capsys):
    called = {"n": 0}
    monkeypatch.setattr(ds, "fetch_ohlcv", lambda ticker: called.__setitem__("n", called["n"] + 1))

    ds.cmd_oversold(rsi_max=30, days=0)

    assert called["n"] == 0
    assert "1 이상" in capsys.readouterr().out

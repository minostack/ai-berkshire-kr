# -*- coding: utf-8 -*-
"""스캔 기준 거래일 규칙(2026-10-26) — 실행 시각이 결과를 바꾸면 안 된다.

예전에는 장중에 실행하면 **그 순간까지의 분봉**을 API로 받아 섞었다. 그러면
10:30에 돌린 CVD와 14:00에 돌린 CVD가 다른데 둘 다 "오늘"이라 불렸다.
이 테스트는 그 일이 다시 일어나지 않게 막는다.
"""

import os
import sys
from datetime import date, datetime, time

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (os.path.join(_ROOT, "tools"), os.path.join(_ROOT, "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from cvd_obv import bar_standard as bs          # noqa: E402

CONFIRMED = "DERIVED_MINUTE_1M_REGULAR"


class _Col:
    def __init__(self, v):
        self._v = v

    def to_pylist(self):
        return self._v


class _Table:
    def __init__(self, rows):
        self._rows = rows

    def column(self, name):
        i = {"trading_date": 0, "source": 1}[name]
        return _Col([r[i] for r in self._rows])


def _patch(monkeypatch, rows):
    monkeypatch.setattr(bs, "read_standard_daily",
                        lambda *a, **k: _Table(rows))


def _days(*specs):
    """(날짜, 원천) 목록 — 종목 하나씩만 둔다(규칙은 날짜 단위다)."""
    return [(d, s) for d, s in specs]


D22 = date(2026, 10, 22)
D23 = date(2026, 10, 23)
D26 = date(2026, 10, 26)


# ── 16:10 경계 ──────────────────────────────────────────────

def test_16시10분_이전에는_오늘을_뺀다(monkeypatch):
    _patch(monkeypatch, _days((D22, CONFIRMED), (D23, CONFIRMED),
                              (D26, bs.PROVISIONAL_SOURCE)))
    r = bs.resolve_scan_day(datetime(2026, 10, 26, 10, 32))
    assert r.day == D23
    assert r.provisional is False
    assert r.cutoff_passed is False
    assert "16:10 이전" in r.note


def test_16시10분_정각부터_오늘을_쓴다(monkeypatch):
    rows = _days((D23, CONFIRMED), (D26, bs.PROVISIONAL_SOURCE))
    _patch(monkeypatch, rows)
    assert bs.resolve_scan_day(datetime(2026, 10, 26, 16, 9, 59)).day == D23
    r = bs.resolve_scan_day(datetime(2026, 10, 26, 16, 10, 0))
    assert r.day == D26 and r.cutoff_passed is True


def test_16시10분_이후_오늘_봉은_잠정으로_표시된다(monkeypatch):
    _patch(monkeypatch, _days((D23, CONFIRMED), (D26, bs.PROVISIONAL_SOURCE)))
    r = bs.resolve_scan_day(datetime(2026, 10, 26, 17, 0))
    assert r.day == D26
    assert r.provisional is True and r.label == "잠정"
    assert r.note == ""          # 정상 경로 — 덧붙일 말이 없다


def test_16시10분_이후인데_오늘_봉이_없으면_전_거래일로_물러난다(monkeypatch):
    _patch(monkeypatch, _days((D22, CONFIRMED), (D23, CONFIRMED)))
    r = bs.resolve_scan_day(datetime(2026, 10, 26, 17, 0))
    assert r.day == D23 and r.cutoff_passed is True
    assert "아직 없다" in r.note and "16:10 잠정 적재를 확인하라" in r.note


def test_확정으로_덮인_오늘_봉은_확정으로_읽는다(monkeypatch):
    """익일 08:30이 잠정을 덮으면 같은 날이 확정이 된다."""
    _patch(monkeypatch, _days((D23, CONFIRMED), (D26, CONFIRMED)))
    r = bs.resolve_scan_day(datetime(2026, 10, 26, 20, 30))
    assert r.day == D26 and r.provisional is False and r.label == "확정"


# ── 실행 시각 비의존 ────────────────────────────────────────

def test_같은_날_몇_시에_돌려도_기준일이_같다(monkeypatch):
    _patch(monkeypatch, _days((D23, CONFIRMED)))
    got = {bs.resolve_scan_day(datetime(2026, 10, 26, h, m)).day
           for h, m in ((9, 1), (10, 32), (13, 0), (15, 59), (16, 9))}
    assert got == {D23}


def test_분봉_자르는_기준은_거래일_끝이다(monkeypatch):
    """실행 시각이 아니라 **그 거래일의 끝**으로 자른다."""
    _patch(monkeypatch, _days((D23, CONFIRMED)))
    a = bs.resolve_scan_day(datetime(2026, 10, 26, 9, 1)).end_of_day
    b = bs.resolve_scan_day(datetime(2026, 10, 26, 15, 59)).end_of_day
    assert a == b == datetime.combine(D23, time(23, 59, 59))


# ── 비어 있을 때 ────────────────────────────────────────────

def test_적재분이_없으면_멈춘다(monkeypatch):
    _patch(monkeypatch, [])
    with pytest.raises(bs.NoScanDataError):
        bs.resolve_scan_day(datetime(2026, 10, 26, 10, 0))


def test_오늘_것밖에_없는데_16시10분_전이면_멈춘다(monkeypatch):
    """조용히 오늘 봉을 쓰지 않는다 — 규칙을 어기느니 멈춘다."""
    _patch(monkeypatch, _days((D26, bs.PROVISIONAL_SOURCE)))
    with pytest.raises(bs.NoScanDataError):
        bs.resolve_scan_day(datetime(2026, 10, 26, 11, 0))


# ── 상수 ────────────────────────────────────────────────────

def test_경계는_16시10분_적재_시각이다():
    assert bs.PROVISIONAL_LOAD_KST == time(16, 10)

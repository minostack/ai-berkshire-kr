#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`tools/ab_backtest_a2.py` — 잠정실적 공시일 매핑 규칙(사전등록 A2 §1)."""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

import ab_backtest_a2 as a2  # noqa: E402


def test_보고서명_필터():
    assert a2.is_prelim("연결재무제표기준영업(잠정)실적(공정공시)")
    assert a2.is_prelim("[기재정정]영업(잠정)실적(공정공시)")
    assert a2.is_prelim("영업 (잠정) 실적(공정공시)")
    assert not a2.is_prelim("장래사업ㆍ경영계획(공정공시)")


def test_분기말일():
    assert a2.quarter_end("2026Q2") == date(2026, 6, 30)
    assert a2.quarter_end("2025Q4") == date(2025, 12, 31)
    assert a2.quarter_end("2024Q1") == date(2024, 3, 31)


def test_창_안의_가장_이른_잠정공시를_쓴다():
    prelim = [date(2026, 4, 7), date(2026, 7, 7), date(2026, 7, 30)]
    d, why = a2.prelim_event_date("2026Q2", date(2026, 8, 14), prelim, set())
    assert (d, why) == (date(2026, 7, 7), "PRELIM")


def test_분기말_전_공시와_정기보고서_뒤_공시는_매칭하지_않는다():
    prelim = [date(2026, 6, 30), date(2026, 8, 15)]
    d, why = a2.prelim_event_date("2026Q2", date(2026, 8, 14), prelim, set())
    assert (d, why) == (date(2026, 8, 14), "NO_PRELIM")


def test_정기보고서와_같은날_잠정공시는_매칭():
    d, why = a2.prelim_event_date("2026Q2", date(2026, 8, 14), [date(2026, 8, 14)], set())
    assert why == "PRELIM" and d == date(2026, 8, 14)


def test_월별_공시자는_정기보고서일로_되돌린다():
    monthly = [date(2025, m, 10) for m in range(1, 13)]
    flagged = a2.monthly_years(monthly)
    assert flagged == {2025}
    d, why = a2.prelim_event_date("2025Q2", date(2025, 8, 14), monthly, flagged)
    assert (d, why) == (date(2025, 8, 14), "MONTHLY_FILER")


def test_월별_기준은_8건_초과():
    assert a2.monthly_years([date(2025, m, 1) for m in range(1, 9)]) == set()
    assert a2.monthly_years([date(2025, m, 1) for m in range(1, 10)]) == {2025}

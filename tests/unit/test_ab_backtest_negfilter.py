#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`tools/ab_backtest_negfilter.py` — 필터 판정이 사전등록 §1 그대로인가."""

import os
import sys
from datetime import date, timedelta

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

import ab_backtest_negfilter as nf  # noqa: E402

CAL = np.array([np.datetime64(date(2026, 1, 1) + timedelta(days=i)) for i in range(300)])


def _events(diffs, last_rcept):
    out = []
    for i, d in enumerate(diffs):
        rd = last_rcept if i == len(diffs) - 1 else date(2020, 1, 1) + timedelta(days=90 * i)
        out.append({"period": f"P{i:02d}", "diff": d, "value": d, "prior_value": 0,
                    "rcept_no": rd.strftime("%Y%m%d") + "000000", "rcept_date": rd,
                    "fs_div": "CFS"})
    return out


NEG = _events([1, -1, 1, -1, -50], date(2026, 3, 1))    # 마지막 SUE 크게 음수
POS = _events([1, -1, 1, -1, 50], date(2026, 3, 1))


def test_최근_음의_서프라이즈는_제외():
    c = nf.classify(NEG, date(2026, 3, 20), CAL)
    assert c["status"] == "EXCLUDE" and c["age_td"] == 19


def test_60거래일이_지나면_STALE():
    c = nf.classify(NEG, date(2026, 3, 1) + timedelta(days=61), CAL)
    assert c["status"] == "STALE" and c["exclude_any_age"] is True
    assert nf.classify(NEG, date(2026, 3, 1) + timedelta(days=60), CAL)["status"] == "EXCLUDE"


def test_신호일_이후_접수된_보고서는_보지_않는다():
    c = nf.classify(NEG, date(2026, 2, 28), CAL)
    assert c["status"] != "EXCLUDE", "3/1 접수분이 2/28 신호에 보이면 미래 정보다"


def test_양의_서프라이즈와_SUE없음은_유지():
    assert nf.classify(POS, date(2026, 3, 20), CAL)["status"] == "KEEP_POS"
    few = _events([1, -50], date(2026, 3, 1))
    assert nf.classify(few, date(2026, 3, 20), CAL)["status"] == "SUE_NA"
    assert nf.classify([], date(2026, 3, 20), CAL)["status"] == "NO_EVENT"


def test_두집단_차이_부트스트랩():
    e = np.array([-1.0] * 20 + [1.0] * 20)
    ex = np.array([True] * 20 + [False] * 20)
    cl = np.array([f"Q{i % 10}" for i in range(40)])
    s = nf.summarize_diff(e, ex, cl, 500, 1)
    assert s["diff"] == -2.0 and s["ci95"][1] < 0
    assert nf.verdict_filter(s, 0.01) == "FILTER_HELPS"


def test_판정():
    assert nf.verdict_filter({"ci95": [0.001, 0.02], "diff": 0.01, "mde": 0.005}, 0.01) \
        == "FILTER_HURTS"
    assert nf.verdict_filter({"ci95": [-0.02, 0.02], "diff": -0.001, "mde": 0.01}, 0.9) \
        == "UNDERPOWERED"
    assert nf.verdict_filter({"ci95": [-0.03, -0.001], "diff": -0.01, "mde": 0.01}, 0.2) \
        == "NO_SIGNIFICANT_EDGE"

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`tools/ab_backtest_ab.py` — A 조건 판정과 결합 판정 규칙."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

import ab_backtest_ab as abx  # noqa: E402


def test_A조건은_최근_SUE_1이상():
    assert abx.a_flag({"sue": 1.0, "age_td": 60})
    assert not abx.a_flag({"sue": 0.99, "age_td": 10})
    assert not abx.a_flag({"sue": 2.0, "age_td": 61})
    assert abx.a_flag({"sue": 2.0, "age_td": 61}, any_age=True)
    assert not abx.a_flag({"sue": None, "age_td": 5})
    assert not abx.a_flag({"status": "NO_EVENT", "sue": None, "age_td": None})


def test_결합_판정():
    assert abx.verdict_combo({"ci95": [0.001, 0.02], "diff": 0.01, "mde": 0.005}, 0.01) \
        == "COMBO_HELPS"
    assert abx.verdict_combo({"ci95": [0.001, 0.02], "diff": 0.01, "mde": 0.005}, 0.2) \
        == "NO_SIGNIFICANT_EDGE"
    assert abx.verdict_combo({"ci95": [-0.02, -0.001], "diff": -0.01, "mde": 0.005}, 0.01) \
        == "COMBO_HURTS"
    assert abx.verdict_combo({"ci95": [-0.02, 0.02], "diff": 0.001, "mde": 0.01}, 0.9) \
        == "UNDERPOWERED"

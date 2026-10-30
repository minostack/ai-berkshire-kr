#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`tools/ab_backtest_a.py` — 이벤트 시점 SUE와 신호일 매핑.

사전등록(`docs/PREREGISTRATION_A.md` §1)의 핵심 약속은 **각 이벤트의 SUE가
그 시점에 이미 접수된 과거 분기만으로 계산된다**는 것이다.
"""

import os
import sys
from datetime import date

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

import ab_backtest_a as ba  # noqa: E402


def _acc(th, frm, rcept, th_add=None, frm_add=None):
    return {"fs_div": "CFS", "sj_div": "IS", "account_nm": "영업이익", "ord": "1",
            "rcept_no": rcept + "000001", "thstrm_amount": str(th),
            "frmtrm_amount": str(frm), "thstrm_add_amount": th_add,
            "frmtrm_add_amount": frm_add}


def _reports():
    """2016Q1~2017Q2 — 1~3Q만(4Q 계산 경로는 스캐너 테스트가 고정)."""
    rc_q = {1: ("11013", "0515"), 2: ("11012", "0814"), 3: ("11014", "1114")}
    diffs = {(2016, 1): 1, (2016, 2): -1, (2016, 3): 2, (2017, 1): -2,
             (2017, 2): 30}
    out = {}
    for (y, q), d in diffs.items():
        rc, md = rc_q[q]
        out[(y, rc)] = [_acc(100 + d, 100, f"{y}{md}")]
    return out


def test_이벤트마다_그_시점의_과거만_쓴다():
    evs = {e["period"]: e for e in ba.events_with_sue(_reports())}
    assert evs["2017Q1"]["hist_n"] == 3 and evs["2017Q1"]["sue"] is None
    last = evs["2017Q2"]
    assert last["hist_n"] == 4
    import statistics
    assert last["sue"] == pytest.approx(30 / statistics.stdev([1, -1, 2, -2]))


def test_늦게_접수된_과거분기는_과거창에_없다():
    reps = _reports()
    # 2017Q1이 정정돼 2017Q2보다 늦게 접수됐다면 2017Q2의 과거창에 들어가면 안 된다
    reps[(2017, "11013")] = [_acc(98, 100, "20170901")]
    evs = {e["period"]: e for e in ba.events_with_sue(reps)}
    assert evs["2017Q2"]["hist_n"] == 3


def test_신호일은_접수일_당일_또는_직전_거래일():
    cal = np.array(["2026-08-13", "2026-08-14", "2026-08-18"], dtype="datetime64[D]")
    assert ba.signal_day_index(cal, date(2026, 8, 14)) == 1
    assert ba.signal_day_index(cal, date(2026, 8, 16)) == 1   # 일요일 접수 → 금요일
    assert ba.signal_day_index(cal, date(2026, 8, 1)) == -1


def test_군집_부트스트랩은_군집_단위로_뽑는다():
    x = np.array([1.0, 1.0, 1.0, -3.0])
    cl = np.array(["A", "A", "A", "B"])
    boot, nc = ba.cluster_bootstrap(x, cl, 2000, 1)
    assert nc == 2
    # 가능한 값: A만(1.0), B만(-3.0), A+B(0.0) — 군집 내부가 쪼개지지 않는다
    assert set(np.round(boot, 6)) <= {1.0, -3.0, 0.0}

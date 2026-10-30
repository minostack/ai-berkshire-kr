#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`tools/ab_candidate_scanner.py` 단위 테스트 — A·B 초안 정의를 고정한다.

지키는 것
1. B의 RVOL·52주 근접 정의(5일/50일, 252거래일 고가)와 게이트 4조건.
2. A의 분기 단독 영업이익 — 4분기는 연간에서 3분기 누적을 뺀다.
3. A는 기준일 이후 접수된 보고서를 쓰지 않는다(미래 정보 차단).
4. 진입은 이벤트일 **다음** 거래일이다.
"""

import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

import ab_candidate_scanner as ab  # noqa: E402


def _bars(n, close=100.0, high=None, volume=1000, value=2e9, last=None):
    start = date(2025, 1, 1)
    out = []
    for i in range(n):
        out.append({"date": start + timedelta(days=i), "high": high or close,
                    "close": close, "volume": volume, "value": value})
    if last:
        out[-1].update(last)
    return out


# ─────────────────────────────────────────────────────────────
# 공통
# ─────────────────────────────────────────────────────────────

def test_백분위는_평균순위이고_동점은_같다():
    assert ab.percentile_ranks([3, 1, 2]) == [1.0, 0.0, 0.5]
    assert ab.percentile_ranks([5, 5, 1]) == [0.75, 0.75, 0.0]
    assert ab.percentile_ranks([7]) == [1.0]
    assert ab.percentile_ranks([]) == []


def test_평균거래대금은_20일_미만이면_None():
    assert ab.avg_traded_value(_bars(19)) is None
    assert ab.avg_traded_value(_bars(20, value=3e9)) == pytest.approx(3e9)


# ─────────────────────────────────────────────────────────────
# B
# ─────────────────────────────────────────────────────────────

def test_RVOL은_최근5일_대비_이전50일():
    bars = _bars(300, volume=1000)
    for b in bars[-5:]:
        b["volume"] = 3000
    m = ab.b_metrics(bars)
    assert m["rvol"] == pytest.approx(3.0)
    assert m["vol_ratio_1d"] == pytest.approx(3.0)


def test_RVOL_기준창에_최근5일이_섞이지_않는다():
    bars = _bars(300, volume=1000)
    bars[-6]["volume"] = 51000       # 기준 창의 마지막 날
    m = ab.b_metrics(bars)
    assert m["rvol"] == pytest.approx(1000 / 2000)


def test_52주_근접은_252거래일_고가_기준():
    bars = _bars(300, close=100.0)
    bars[-253]["high"] = 1000.0      # 창 밖 — 무시돼야 한다
    bars[-252]["high"] = 125.0       # 창 안 첫날
    m = ab.b_metrics(bars)
    assert m["high_52w"] == 125.0
    assert m["prox_52w"] == pytest.approx(0.8)


def test_일봉이_55일_미만이면_B지표를_내지_않는다():
    assert ab.b_metrics(_bars(54)) is None


def test_B_게이트_4조건():
    base = {"history_days": 252, "rvol": 2.0, "prox_52w": 0.95,
            "avg_value_20d": 2e9}
    assert ab.b_gate(base) == []
    assert ab.b_gate(dict(base, history_days=239)) == ["HISTORY"]
    assert ab.b_gate(dict(base, rvol=1.49)) == ["RVOL"]
    assert ab.b_gate(dict(base, prox_52w=0.899)) == ["PROX_52W"]
    assert ab.b_gate(dict(base, avg_value_20d=9.9e8)) == ["LIQUIDITY"]
    assert ab.b_gate(dict(base, rvol=1.5, prox_52w=0.90,
                          avg_value_20d=1e9)) == [], "경계값은 통과"


def test_B_점수는_유니버스_전체_백분위로_매긴다():
    rows = [
        {"code": "A", "rvol": 3.0, "prox_52w": 1.0, "history_days": 252, "avg_value_20d": 2e9},
        {"code": "B", "rvol": 1.0, "prox_52w": 0.5, "history_days": 252, "avg_value_20d": 2e9},
        {"code": "C", "rvol": 2.0, "prox_52w": 0.95, "history_days": 252, "avg_value_20d": 2e9},
    ]
    out = ab.score_b(rows)
    assert [r["code"] for r in out] == ["A", "C", "B"]
    assert out[0]["score"] == 100.0 and out[-1]["score"] == 0.0
    assert out[-1]["passed"] is False      # B는 게이트 탈락이지만 백분위 모집단에는 있다


# ─────────────────────────────────────────────────────────────
# A
# ─────────────────────────────────────────────────────────────

def _acc(fs, th, frm, th_add=None, frm_add=None, rcept="20250515000001"):
    return {"fs_div": fs, "sj_div": "IS", "account_nm": "영업이익", "ord": "1",
            "rcept_no": rcept, "thstrm_amount": th, "frmtrm_amount": frm,
            "thstrm_add_amount": th_add, "frmtrm_add_amount": frm_add}


def test_금액_파싱():
    assert ab.parse_amount("1,234") == 1234.0
    assert ab.parse_amount("-1,234") == -1234.0
    assert ab.parse_amount("-") is None
    assert ab.parse_amount(None) is None


def test_4분기는_연간에서_3분기_누적을_뺀다():
    reports = {
        (2025, "11014"): [_acc("CFS", "30", "20", th_add="90", frm_add="60",
                               rcept="20251114000001")],
        (2025, "11011"): [_acc("CFS", "150", "100", rcept="20260310000001")],
    }
    ev = {e["period"]: e for e in ab.quarter_events(reports)}
    assert ev["2025Q3"]["diff"] == 10.0
    q4 = ev["2025Q4"]
    assert q4["value"] == 60.0 and q4["prior_value"] == 40.0
    assert q4["diff"] == 20.0
    assert q4["rcept_date"] == date(2026, 3, 10)


def test_4분기는_3분기_보고서가_없으면_만들지_않는다():
    reports = {(2025, "11011"): [_acc("CFS", "150", "100", rcept="20260310000001")]}
    assert ab.quarter_events(reports) == []


def test_연결_별도는_한_회사_안에서_섞지_않는다():
    """최신 보고서에 연결이 있으면 연결만 — 연결이 없는 과거 분기는 버린다."""
    reports = {
        (2025, "11013"): [_acc("OFS", "10", "5", rcept="20250515000001")],
        (2025, "11012"): [_acc("CFS", "30", "20", rcept="20250814000001"),
                          _acc("OFS", "12", "6", rcept="20250814000001")],
    }
    ev = ab.quarter_events(reports)
    assert [e["period"] for e in ev] == ["2025Q2"]
    assert ev[0]["fs_div"] == "CFS" and ev[0]["diff"] == 10.0


def _events(diffs, last_rcept):
    """분기별 ΔQ 이벤트 — 마지막만 접수일을 지정한다."""
    out = []
    for i, d in enumerate(diffs):
        rd = last_rcept if i == len(diffs) - 1 else date(2020, 1, 1) + timedelta(days=90 * i)
        out.append({"period": f"P{i:02d}", "diff": d, "value": d, "prior_value": 0,
                    "rcept_no": rd.strftime("%Y%m%d") + "000000", "rcept_date": rd,
                    "fs_div": "CFS"})
    return out


def test_SUE는_직전_8개_ΔQ의_표준편차로_나눈다():
    hist = [1, -1, 1, -1, 1, -1, 1, -1, 100]   # 첫 값은 창 밖(9개 중 앞 1개)
    hist = [1000] + hist[:-1]
    events = _events(hist + [4], date(2026, 8, 14))
    ev = ab.sue_latest(events, date(2026, 9, 22))
    assert ev["hist_n"] == 8
    import statistics
    assert ev["sue"] == pytest.approx(4 / statistics.stdev(hist[-8:]))


def test_SUE는_과거_ΔQ가_4개_미만이면_None():
    ev = ab.sue_latest(_events([1, 2, 3, 9], date(2026, 8, 14)), date(2026, 9, 22))
    assert ev["hist_n"] == 3 and ev["sue"] is None


def test_기준일_이후_접수된_보고서는_쓰지_않는다():
    events = _events([1, -1, 1, -1, 1, 50], date(2026, 8, 14))
    ev = ab.sue_latest(events, date(2026, 8, 13))
    assert ev["period"] == "P04", "8/14 접수분이 8/13 기준일에 보이면 미래 정보다"
    assert ab.sue_latest(events, date(2026, 8, 14))["period"] == "P05"


def test_경과일은_이벤트_다음_거래일을_1일로_센다():
    cal = [date(2026, 8, 13), date(2026, 8, 14), date(2026, 8, 18), date(2026, 8, 19)]
    assert ab.elapsed_trading_days(date(2026, 8, 14), cal) == 2
    assert ab.elapsed_trading_days(date(2026, 8, 19), cal) == 0


def test_A_게이트():
    base = {"sue": 1.0, "elapsed_td": 1, "avg_value_20d": 1e9}
    assert ab.a_gate(base) == []
    assert ab.a_gate(dict(base, sue=None)) == ["SUE_NA"]
    assert ab.a_gate(dict(base, sue=0.99)) == ["SUE"]
    assert ab.a_gate(dict(base, elapsed_td=0)) == ["WINDOW"], "진입일 전은 후보가 아니다"
    assert ab.a_gate(dict(base, elapsed_td=61)) == ["WINDOW"]
    assert ab.a_gate(dict(base, avg_value_20d=None)) == ["LIQUIDITY"]


# ─────────────────────────────────────────────────────────────
# A 설계 0.2 — 이벤트일 = 잠정실적 공시일
# ─────────────────────────────────────────────────────────────

def _cal_days(*ds):
    return [date(2026, m, d) for m, d in ds]


def test_A_이벤트일은_잠정공시일이고_경과는_그날부터_센다():
    cal = _cal_days((7, 7), (7, 8), (7, 9), (8, 13), (8, 14), (8, 17), (8, 18))
    ev = {"period": "2026Q2", "rcept_date": date(2026, 8, 14)}
    ab.apply_prelim_event(ev, [date(2026, 7, 7), date(2026, 7, 30)], cal)
    assert ev["event_date"] == date(2026, 7, 7) and ev["event_source"] == "PRELIM"
    assert ev["elapsed_td"] == 6          # 7/8부터 8/18까지
    assert ev["report_lag_td"] == 4       # 잠정(7/7) → 보고서(8/14) 사이 거래일


def test_A_잠정공시가_없으면_정기보고서일_지연0():
    cal = _cal_days((8, 14), (8, 17))
    ev = {"period": "2026Q2", "rcept_date": date(2026, 8, 14)}
    ab.apply_prelim_event(ev, [], cal)
    assert ev["event_date"] == date(2026, 8, 14) and ev["event_source"] == "NO_PRELIM"
    assert ev["elapsed_td"] == 1 and ev["report_lag_td"] == 0


def test_A_잠정공시_기준_경과가_60을_넘으면_창_밖():
    ev = {"sue": 2.0, "elapsed_td": 61, "avg_value_20d": 2e9}
    assert ab.a_gate(ev) == ["WINDOW"]


def test_설계버전은_0_3():
    assert ab.DESIGN_VERSION == "ab-draft-0.3"


# ─────────────────────────────────────────────────────────────
# A∩B 표시 (설계 0.3) — 백테스트 판정과 같아야 한다
# ─────────────────────────────────────────────────────────────

def test_A조건_판정은_백테스트_AB와_같다():
    """스캐너 `ab_condition`과 백테스트(`negfilter.classify` + `ab.a_flag`)가
    같은 날·같은 이벤트에서 같은 결론을 내야 한다 — 다르면 전향 기록이
    백테스트한 규칙을 재는 것이 아니다."""
    import numpy as np
    import ab_backtest_ab as abx
    import ab_backtest_negfilter as nf

    cal = [date(2026, 1, 1) + timedelta(days=i) for i in range(200)]
    cal_arr = np.array([np.datetime64(d) for d in cal])
    cases = {
        "pos": [1, -1, 1, -1, 50],
        "neg": [1, -1, 1, -1, -50],
        "flat": [1, -1, 1, -1, 0.5],
        "short": [1, 50],
    }
    for name, diffs in cases.items():
        evs = _events(diffs, date(2026, 3, 1))
        for as_of in (date(2026, 2, 28), date(2026, 3, 1), date(2026, 4, 1),
                      date(2026, 4, 30), date(2026, 5, 1), date(2026, 6, 1)):
            cal_upto = [d for d in cal if d <= as_of]
            got = ab.ab_condition(evs, as_of, cal_upto)
            ref = abx.a_flag(nf.classify(evs, as_of, cal_arr))
            assert got["a_flag"] == ref, (name, as_of, got)


def test_A조건_표시_형식():
    assert ab._fmt_a_cond(None) == "-"
    assert ab._fmt_a_cond({"a_flag": True, "status": "A_POS", "sue": 2.34,
                           "age_td": 12}).startswith("✓ SUE +2.3")
    assert "오래됨" in ab._fmt_a_cond({"a_flag": False, "status": "STALE", "sue": 3.0,
                                       "age_td": 70})
    assert ab._fmt_a_cond({"a_flag": False, "status": "NO_EVENT", "sue": None,
                           "age_td": None}) == "보고서 없음"


def test_DART가_JSON_대신_HTML을_주면_묶음을_나눠_다시_묻는다(tmp_path, monkeypatch):
    """2026-10-14 실측: 100개 묶음은 HTML 오류 페이지, 50개 묶음은 정상."""
    monkeypatch.setattr(ab, "DART_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(ab.time, "sleep", lambda s: None)
    asked = []

    def fake_get(path, params, key, raw=False):
        batch = params["corp_code"].split(",")
        asked.append(len(batch))
        if len(batch) > 50:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return {"status": "013", "message": "조회된 데이타가 없습니다."}

    monkeypatch.setattr(ab, "_dart_get", fake_get)
    codes = [f"{i:08d}" for i in range(100)]
    ab.dart_fetch_reports(codes, [2026], "KEY", log=lambda *a: None)
    # 연도 하나 × 보고서 4종 — 매번 100개가 실패하고 50개씩 두 번 성공
    assert asked == [100, 50, 50] * len(ab.REPRT_Q)


def _brow(code, rvol, passed, fails=None, provisional=True):
    return {"code": code, "name": code, "rvol": rvol, "prox_52w": 0.95,
            "passed": passed, "fails": fails if fails is not None else [],
            "provisional": provisional}


def test_잠정_봉의_RVOL_게이트_근처_종목을_표시한다():
    """폭 20%: 통과 중 RVOL<1.8은 빠질 수 있고, RVOL만 미달인 1.25 이상은 들어올 수 있다."""
    rows = [_brow("A", 2.10, True),                      # 여유 — 표시 안 함
            _brow("B", 1.76, True),                      # 10/14 HMM 잠정값 — fragile
            _brow("C", 1.30, False, ["RVOL"]),           # 10/7 씨젠류 — possible
            _brow("D", 1.30, False, ["RVOL", "PROX_52W"]),  # 근접도도 미달 — 제외
            _brow("E", 1.20, False, ["RVOL"]),           # 폭 밖 — 제외
            _brow("F", 1.60, True, provisional=False)]   # 확정 봉 — 제외
    w = ab.provisional_watch(rows)
    assert [r["code"] for r in w["fragile"]] == ["B"]
    assert [r["code"] for r in w["possible"]] == ["C"]
    assert w["rvol_range"] == [1.25, 1.8]


def test_DART_네트워크_오류는_재시도하고_HTTP_오류는_재시도하지_않는다(monkeypatch):
    import urllib.error

    monkeypatch.setattr(ab.time, "sleep", lambda s: None)
    calls = {"n": 0}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"status": "000"}'

    def flaky(url, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError("_ssl.c:1063: The handshake operation timed out")
        return _Resp()

    monkeypatch.setattr(ab.urllib.request, "urlopen", flaky)
    assert ab._dart_get("list.json", {}, "KEY") == {"status": "000"}
    assert calls["n"] == 3

    def http_err(url, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError(url, 500, "err", {}, None)

    calls["n"] = 0
    monkeypatch.setattr(ab.urllib.request, "urlopen", http_err)
    with pytest.raises(urllib.error.HTTPError):
        ab._dart_get("list.json", {}, "KEY")
    assert calls["n"] == 1

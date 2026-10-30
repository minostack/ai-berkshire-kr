# -*- coding: utf-8 -*-
"""지표·필터·점수 단위 테스트 — 저장소 없이 합성 입력만 쓴다.

    python -m pytest cvd_selection/tests -q
"""

import os
import sys
from datetime import date, datetime, time, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from cvd_selection.config import Thresholds  # noqa: E402
from cvd_selection.indicators import (  # noqa: E402
    band_score, clv, flow_metrics, late_window, minute_vwap, sma_at, wilder_atr,
)
from cvd_selection.selection import Inputs, evaluate, rank  # noqa: E402
from cvd_obv.domain.ids import KST  # noqa: E402


# ── 지표 ──

def test_flow_metrics_spec_formulas():
    days = [(60, 40, None), (55, 45, None), (40, 60, None), (70, 30, None), (52, 48, None)]
    m = flow_metrics(days)
    # Delta = 20, 10, -20, 40, 4 → 합 54, Vflow 합 500
    assert m["dr5"] == pytest.approx(100 * 54 / 500)
    assert m["dr2"] == pytest.approx(100 * 44 / 200)
    assert m["pos5"] == 4
    assert m["delta_prev"] == 4
    assert m["u_known"] is False


def test_flow_metrics_uses_unclassified_in_denominator():
    m = flow_metrics([(60, 40, 100)] * 5)
    assert m["dr5"] == pytest.approx(10.0)


def test_flow_metrics_needs_five_days():
    assert flow_metrics([(1, 0, None)] * 4) is None


def test_clv_and_undefined():
    assert clv(90, 100, 80) == pytest.approx(0.5)
    assert clv(100, 100, 100) is None


def test_sma_at():
    assert sma_at([1, 2, 3, 4], 2, 3) == 3.5
    assert sma_at([1, 2, 3, 4], 5, 3) is None


def test_wilder_atr_constant_range():
    n = 30
    highs, lows, closes = [11.0] * n, [9.0] * n, [10.0] * n
    assert wilder_atr(highs, lows, closes, 14) == pytest.approx(2.0)


def test_wilder_atr_gap_counts():
    closes = [10.0] * 15 + [12.0]
    highs = [10.5] * 15 + [12.5]
    lows = [9.5] * 15 + [11.5]
    # 마지막 TR = max(1, |12.5−10|, |11.5−10|) = 2.5 → (1×13 + 2.5)/14
    assert wilder_atr(highs, lows, closes, 14) == pytest.approx((13 + 2.5) / 14)


def test_vwap_from_traded_value():
    bars = [{"volume": 10, "traded_value": 100},
            {"volume": 30, "traded_value": 360}]
    v = minute_vwap(bars)
    assert v["vwap"] == pytest.approx(460 / 40) and v["missing_bars"] == 0


def test_vwap_not_estimated_when_value_missing():
    bars = [{"volume": 10, "traded_value": 100, "high": 11, "low": 9, "close": 10},
            {"volume": 10, "traded_value": None, "high": 13, "low": 11, "close": 12}]
    v = minute_vwap(bars, 0.001)
    assert v["vwap"] is None and v["missing_bars"] == 1 and v["missing_volume"] == 10


def test_vwap_tiny_unvalued_bar_is_excluded_not_estimated():
    bars = [{"volume": 9999, "traded_value": 9999 * 10.0},
            {"volume": 1, "traded_value": None, "high": 99, "low": 99, "close": 99}]
    v = minute_vwap(bars, 0.001)
    assert v["vwap"] == pytest.approx(10.0) and v["volume"] == 9999


def test_late_window_half_open():
    mk = lambda h, m, o, c: {"kst": time(h, m), "open": o, "close": c}  # noqa: E731
    bars = [mk(14, 49, 1, 1), mk(14, 50, 100, 101), mk(15, 19, 104, 105), mk(15, 20, 999, 999)]
    w = late_window(bars, time(14, 50), time(15, 20))
    assert w["bars"] == 2 and w["expected"] == 30
    assert (w["p_start"], w["p_end"]) == (100.0, 105.0)


def test_band_score_edges():
    tbl = [(5.0, 15), (10.0, 20), (15.0, 25)]
    assert band_score(4.99, tbl) == 0
    assert band_score(5.0, tbl) == 15
    assert band_score(10.0, tbl) == 20
    assert band_score(30.0, tbl) == 25


# ── 종목 평가 (합성 시나리오) ──

class _SD:
    def __init__(self, buy, sell, prov=False):
        self.buy, self.sell, self.provisional, self.source = buy, sell, prov, "UN"


def _scenario(**kw):
    """통과하도록 맞춘 기본 시나리오 — kw로 한 가지씩 깨뜨린다."""
    n = 80
    cal = [kw.get("start", date(2026, 6, 1)) + timedelta(days=i) for i in range(n)]
    D = cal[-1]
    bars = []
    for i, d in enumerate(cal):
        c = 10000 + 10 * i                       # 완만한 상승 → C > MA20 > MA60
        bars.append({"date": d, "open": c - 20, "high": c + 50, "low": c - 50,
                     "close": c, "volume": 1_000_000, "value": 2e10,
                     "value_est": False, "source": "X", "provisional": False,
                     "flags": [], "revision": 1})
    last = bars[-1]
    last.update(volume=kw.get("d_volume", 1_600_000), high=last["close"] + 10,
                low=last["close"] - 90, provisional=True)
    # 1분봉 범위 합계 — 기본은 일봉과 같다(블록딜 없음)
    totals = {("KRX", "000001", b["date"]): {
        "volume": b["volume"], "value": 2e10 * b["volume"] / 1_000_000,
        "unvalued_volume": 0, "typ_unvalued": 0.0, "last_close": b["close"]} for b in bars}
    flows = {d: _SD(*kw.get("flow", (60, 40))) for d in cal[-6:-1]}
    if "flow_override" in kw:
        flows.update(kw["flow_override"](cal))
    C = last["close"]
    minutes = [{"kst": time(9, 0), "session": "REGULAR", "open": C - 80, "high": C - 40,
                "low": C - 90, "close": C - 60, "volume": 1000, "traded_value": (C - 60) * 1000}]
    for m in range(30):
        t0 = (datetime(2026, 1, 1, 14, 50) + timedelta(minutes=m)).time()
        p = C - 30 + m
        minutes.append({"kst": t0, "session": "REGULAR", "open": p, "high": p + 1,
                        "low": p - 1, "close": p + 1, "volume": 100, "traded_value": p * 100})
    x = Inputs(day=D, calendar=cal, daily={"000001": bars},
               flows={"000001": flows}, flow_available={},
               minutes={("KRX", "000001"): minutes},
               coverage={("KRX", "000001"): {"status": "SUCCESS"}},
               names={"000001": "테스트"}, membership={"000001": "KOSPI200"},
               decision_at=datetime.combine(D, time(15, 35), KST), live=False,
               thresholds=Thresholds(), minute_totals=totals)
    return x


def test_scenario_passes_and_scores():
    r = evaluate("000001", _scenario())
    assert r["reject_reason"] == [], r["reject_reason"]
    assert r["filter_pass"] and r["dr5"] == pytest.approx(20.0)
    assert r["pos5"] == 5 and r["rvol"] == pytest.approx(1.6)
    assert r["vwap"] is not None and r["gap_vwap"] > 0
    assert r["score"]["parts"]["dr5"] == 25 and r["score"]["parts"]["pos5"] == 20


def _codes(r):
    return {z["code"] for z in r["reject_reason"]}


def test_provisional_flow_is_not_used():
    x = _scenario(flow_override=lambda cal: {cal[-2]: _SD(60, 40, prov=True)})
    r = evaluate("000001", x)
    assert "FLOW_NOT_CONFIRMED" in _codes(r) and r["dr5"] is None


def test_missing_prev_day_flow_skips():
    x = _scenario()
    del x.flows["000001"][x.calendar[-2]]
    assert "FLOW_MISSING" in _codes(evaluate("000001", x))


def test_today_flow_is_never_read():
    """D 당일 체결량이 있어도(강한 매도) 결과가 바뀌지 않는다 — D-1까지만 쓴다."""
    x = _scenario()
    base = evaluate("000001", x)
    x.flows["000001"][x.day] = _SD(0, 100)
    assert evaluate("000001", x)["dr5"] == base["dr5"]


def test_missing_minute_value_rejects_without_estimate():
    x = _scenario()
    x.minutes[("KRX", "000001")][0]["traded_value"] = None   # 1000주 / 4000주
    r = evaluate("000001", x)
    assert "VWAP_VALUE_MISSING" in _codes(r) and r["vwap"] is None


def _no_value(x):
    for b in x.minutes[("KRX", "000001")]:
        b["traded_value"] = None
    return x


def test_vwap_estimated_only_before_cum_value_start():
    import cvd_selection.selection as S
    x = _no_value(_scenario())
    old = S.CUM_VALUE_START
    try:
        S.CUM_VALUE_START = x.day                     # D == 시작일 → 추정 안 함
        assert "VWAP_VALUE_MISSING" in _codes(evaluate("000001", x))
        S.CUM_VALUE_START = x.day + timedelta(days=1)  # D가 시작일 전 → 추정
        r = evaluate("000001", x)
        assert r["vwap_method"] == "ESTIMATED_TYPICAL_PRICE"
        assert "VWAP_ESTIMATED" in r["flags"] and r["vwap"] is not None
        assert "VWAP_VALUE_MISSING" not in _codes(r)
        x.live = True                                  # 당일 실행은 추정하지 않는다
        assert "VWAP_VALUE_MISSING" in _codes(evaluate("000001", x))
    finally:
        S.CUM_VALUE_START = old


def _tot(x, d):
    return x.minute_totals[("KRX", "000001", d)]


def test_block_volume_on_d_is_excluded_from_rvol():
    """D 일봉에만 있는 물량(블록딜)은 RVOL에 들어가지 않는다."""
    x = _scenario()
    x.daily["000001"][-1]["volume"] = 2_400_000          # 1분봉 합은 1,600,000
    r = evaluate("000001", x)
    assert r["rvol"] == pytest.approx(1.6)
    assert r["volume_basis"] == 1_600_000 and r["volume_basis_excluded"] == 800_000


def test_past_block_volume_does_not_inflate_denominator():
    x = _scenario()
    for b in x.daily["000001"][:-1]:
        b["volume"] = 1_500_000                          # 1분봉 합은 1,000,000
    r = evaluate("000001", x)
    assert r["rvol"] == pytest.approx(1.6) and r["filter_pass"]
    assert r["avg_a20"] == pytest.approx(2e10)


def test_past_minute_day_missing_rejects():
    x = _scenario()
    del x.minute_totals[("KRX", "000001", x.calendar[-3])]
    r = evaluate("000001", x)
    assert "MINUTE_VOLUME_MISSING" in _codes(r) and r["rvol"] is None


def test_unvalued_minutes_make_avg_a20_estimate():
    x = _scenario()
    k = _tot(x, x.calendar[-2])
    k.update(value=k["value"] - 1e9, unvalued_volume=50_000, typ_unvalued=1e9)
    r = evaluate("000001", x)
    assert r["avg_a20_est"] and r["avg_a20"] == pytest.approx(2e10)


def test_minute_over_daily_is_capped():
    x = _scenario()
    _tot(x, x.calendar[-2])["volume"] = 1_300_000
    r = evaluate("000001", x)
    assert f"MINUTE_OVER_DAILY={x.calendar[-2]}" in r["flags"]
    assert r["rvol"] == pytest.approx(1.6)


def test_split_adjusts_raw_minute_volume():
    """1분봉은 원주가 — 일봉(수정)과 종가가 2배 다르면 거래량을 2배로 맞춘다."""
    x = _scenario()
    d = x.calendar[-2]
    k = _tot(x, d)
    k.update(volume=500_000, last_close=x.daily["000001"][-2]["close"] * 2)
    r = evaluate("000001", x)
    assert f"MINUTE_VOLUME_ADJUSTED={d}" in r["flags"]
    assert r["rvol"] == pytest.approx(1.6)


def test_nxt_main_added_from_scope_change_date():
    x = _scenario(start=date(2026, 8, 3))
    for b in x.daily["000001"]:
        b["volume"] += 100_000                           # 일봉 = KRX + NXT
        x.minute_totals[("NXT", "000001", b["date"])] = {
            "volume": 100_000, "value": 2e9, "unvalued_volume": 0,
            "typ_unvalued": 0.0, "last_close": b["close"]}
    r = evaluate("000001", x)
    assert r["volume_basis"] == 1_700_000 and r["rvol"] == pytest.approx(1.7 / 1.1)


def test_rvol_filter():
    assert "RVOL" in _codes(evaluate("000001", _scenario(d_volume=1_200_000)))


def test_selling_flow_fails():
    r = evaluate("000001", _scenario(flow=(40, 60)))
    assert {"DR5", "POS5", "DR2", "DELTA_PREV"} <= _codes(r)


def test_late_window_without_coverage_is_unverified():
    x = _scenario()
    x.coverage = {}
    m = x.minutes[("KRX", "000001")]
    del m[5]
    assert "LATE_WINDOW_UNVERIFIED" in _codes(evaluate("000001", x))


def test_flow_after_decision_rejects_only_live():
    x = _scenario()
    late = x.decision_at + timedelta(hours=1)
    x.flow_available = {("000001", x.calendar[-2]): late}
    assert "FLOW_AVAILABLE_AFTER_DECISION" in evaluate("000001", x)["flags"]
    x.live = True
    assert "FLOW_AFTER_DECISION" in _codes(evaluate("000001", x))


def test_rank_tiebreak_and_pick():
    rows = []
    for code, dr5, a in (("000003", 20, 1), ("000002", 20, 2), ("000001", 30, 1), ("000004", 10, 9)):
        rows.append({"symbol": code, "filter_pass": True, "dr5": dr5, "avg_a20": a,
                     "score": {"total": 80}})
    passed = rank(rows, 3)
    assert [r["symbol"] for r in passed] == ["000001", "000002", "000003", "000004"]
    assert [r["picked"] for r in passed] == [True, True, True, False]


# ── 사전등록 CVDSEL 전향 기록 ──

def test_prereg_sets_recorded_without_changing_selection():
    x = _scenario()
    r = evaluate("000001", x)
    assert r["filter_pass"]
    assert r["prereg_cvdsel"] == {"S0": True, "S1": True, "S3": True, "S2": False}
    assert r["dr20"] is None                      # 체결량이 5일뿐 — DR20 없음 → S2 불통과


def test_dr20_uses_confirmed_d20_to_d1_only():
    x = _scenario()
    for d in x.calendar[-21:-1]:
        x.flows["000001"][d] = _SD(70, 30)
    x.flows["000001"][x.day] = _SD(0, 100)        # 당일 흐름은 쓰지 않는다
    r = evaluate("000001", x)
    assert r["dr20"] == pytest.approx(40.0)
    x.flows["000001"][x.calendar[-15]] = _SD(70, 30, prov=True)
    assert evaluate("000001", x)["dr20"] is None


def test_prereg_trend_relaxed_only_in_s1():
    """MA20 ≤ MA60이지만 C > MA60·MA20 상승 — 명세 탈락, S1만 통과."""
    from cvd_selection.prereg import cvdsel_sets
    row = {"avg_a20": 2e10, "dr5": 10, "pos5": 4, "dr2": 3, "delta_prev": 5, "rvol": 1.5,
           "clv": 0.8, "krx_close": 110, "ma20": 100, "ma60": 101, "ma20_lag": 99,
           "atrp14": 3.0, "dr20": -5}
    assert cvdsel_sets(row) == {"S0": False, "S1": True, "S3": False, "S2": True}
    row["atrp14"] = 5.0
    assert cvdsel_sets(row) == {"S0": False, "S1": False, "S3": False, "S2": True}


def test_adjusted_daily_close_rescales_minute_prices():
    """D 일봉이 뒤에 생긴 권리락으로 0.5배 수정됐을 때 — 1분봉(원주가)을 같은 계수로 맞춘다."""
    x = _scenario()
    base = evaluate("000001", x)
    for b in x.daily["000001"]:
        for k in ("open", "high", "low", "close"):
            b[k] *= 0.5
    r = evaluate("000001", x)
    assert any(f.startswith("MINUTE_PRICE_ADJUSTED=") for f in r["flags"])
    assert r["gap_vwap"] == pytest.approx(base["gap_vwap"])
    assert r["hold_close"] == pytest.approx(base["hold_close"])
    assert r["rlate"] == pytest.approx(base["rlate"])

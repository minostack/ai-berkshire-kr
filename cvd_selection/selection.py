# -*- coding: utf-8 -*-
"""종목 평가 — 품질(3.2) → 필수 조건(5장) → 점수(6장) → 순위.

한 종목을 평가하는 `evaluate()`는 저장소를 모른다. 입력 묶음(`Inputs`)만 받는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time

from .config import BREAKOUT_SCORE, SCORE_TABLE, VWAP_SCORE, Thresholds
from .prereg import cvdsel_sets
from .indicators import (
    band_score, clv, flow_metrics, late_window, mean, minute_vwap, sma_at,
    typical_vwap, wilder_atr,
)

#: 2026-08-03부터 일봉 거래량에 NXT 메인마켓이 들어간다(bars-1.0 A안). 9/14 이후는 확정 복원,
#: 8/3~9/11은 NXT 1분봉 소급(`nxt-history`, 2026-10-29) — 경계가 9/14에서 8/3으로 옮겨졌다
VOLUME_SCOPE_CHANGE = date(2026, 8, 3)
NXT_MAIN_START = time(9, 0)
NXT_MAIN_END = time(15, 30)
#: KRX 정규장 1분봉 범위 — 09:00 ~ 15:30 종가 단일가 봉, 늦게 찍힌 종가 체결(~15:35)
KRX_REG_START = time(9, 0)
KRX_REG_END = time(15, 35)
#: 1분봉 누적거래대금(KIS)이 있는 첫날. 그 전은 KIS 수집 지평 밖이라 채울 수 없다.
#: 이 날 전으로 사후 실행할 때만 VWAP을 대표가격으로 추정한다.
CUM_VALUE_START = date(2025, 10, 15)

#: 일봉 종가와 1분봉 종가가 이만큼 넘게 다르면 수정 계수로 본다(`_minute_basis`와 같은 5%)
MINUTE_ADJ_TOL = 0.05
#: 보조 DR20 창(사전등록 CVDSEL S2 = DR5 − DR20 ≥ 10)
DR20_DAYS = 20

STAGE_QUALITY = "품질"
STAGE_FILTER = "조건"


@dataclass
class Inputs:
    day: date
    calendar: list                    # 거래일 오름차순, 마지막이 D
    daily: dict                       # {코드: [봉…]}
    flows: dict                       # {코드: {날짜: SidesDay}}
    flow_available: dict              # {(코드, 날짜): datetime UTC}
    minutes: dict                     # {(venue, 코드): [1분봉…]}
    coverage: dict                    # {(venue, 코드): dict}
    names: dict                       # {코드: 이름}
    membership: dict                  # {코드: 지수}
    decision_at: datetime             # 선정 시각(KST aware)
    live: bool                        # D가 오늘이고 지금 돌리는가
    thresholds: Thresholds = field(default_factory=Thresholds)
    #: {(venue, 코드, 날짜): 합계} — `data.load_minute_day_totals`, D와 직전 20거래일
    minute_totals: dict = field(default_factory=dict)


def _reason(stage, code, text):
    return {"stage": stage, "code": code, "text": text}


def evaluate(code: str, x: Inputs) -> dict:
    t = x.thresholds
    D = x.day
    row = {
        "trade_date": str(D), "symbol": code, "name": x.names.get(code, ""),
        "market_membership": x.membership.get(code),
        "provisional_flag": None, "missing_reason": None,
        "flags": [], "reject_reason": [],
    }
    rej = row["reject_reason"]
    flags = row["flags"]

    # ── 일봉 ──
    bars = [b for b in x.daily.get(code, []) if b["date"] <= D]
    if not bars or bars[-1]["date"] != D:
        rej.append(_reason(STAGE_QUALITY, "NO_D_BAR", f"{D} 일봉 없음(미적재·거래정지)"))
        row["missing_reason"] = "NO_D_BAR"
        return _finish(row)
    today = bars[-1]
    row["provisional_flag"] = today["provisional"]
    row["d_bar_source"] = today["source"]
    if today["volume"] <= 0:
        rej.append(_reason(STAGE_QUALITY, "NO_TRADE", "당일 거래량 0(거래정지 추정)"))
    if len(bars) < t.min_daily_bars:
        rej.append(_reason(STAGE_QUALITY, "SHORT_HISTORY",
                           f"유효 일봉 {len(bars)} < {t.min_daily_bars}"))
    j = len(bars) - 1
    prev = bars[:-1]

    # 직전 vol_days 거래일이 달력과 빈틈 없이 이어지는가(정지·결측 구간 제외)
    cal = x.calendar
    di = cal.index(D)
    want = cal[max(0, di - t.vol_days):di]
    have = {b["date"] for b in prev[-t.vol_days:]}
    gap = [d for d in want if d not in have]
    if len(want) < t.vol_days or gap:
        rej.append(_reason(STAGE_QUALITY, "DAILY_GAP",
                           f"직전 {t.vol_days}거래일 일봉 결측 {len(gap)}일"))
    if _nxt_missing(today):
        rej.append(_reason(STAGE_QUALITY, "NXT_VOLUME_MISSING",
                           "NXT 거래 종목인데 당일 NXT 거래량 미수신 — 과거와 범위 불일치"))
    if want and want[0] < VOLUME_SCOPE_CHANGE <= D:
        flags.append("VOLUME_SCOPE_CHANGE_IN_WINDOW")

    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    C = today["close"]
    row["krx_close"] = C
    row["volume_total"] = today["volume"]
    row["turnover_total"] = today["value"]
    row["turnover_est"] = today["value_est"]

    # 4.2 거래 참여 — 거래량·거래대금은 일봉이 아니라 1분봉 범위(블록딜 제외, 명세 3.1).
    # 당일(잠정 봉 = 1분봉 합)과 과거 20일이 같은 정의가 된다
    pv = prev[-t.vol_days:]
    basis = {b["date"]: _minute_basis(code, b, x, t) for b in pv + [today]}
    d_basis = basis[D]
    p_basis = [basis[b["date"]] for b in pv]
    lacking = [b["date"] for b in pv if basis[b["date"]] is None]
    if d_basis is None or lacking:
        rej.append(_reason(STAGE_QUALITY, "MINUTE_VOLUME_MISSING",
                           "1분봉 범위 거래량 없음 — "
                           + (f"당일" if d_basis is None else "")
                           + (f" 과거 {len(lacking)}일(최근 {lacking[-1]})" if lacking else "")))
        row["missing_reason"] = row["missing_reason"] or "MINUTE_VOLUME_MISSING"
    full = len(pv) == t.vol_days and not lacking
    vol_mean = mean(p["volume"] for p in p_basis) if full else None
    values = [p["value"] for p in p_basis] if full else []
    avg_a20 = mean(values) if full and all(v is not None for v in values) else None
    row["volume_basis"] = d_basis["volume"] if d_basis else None
    row["turnover_basis"] = d_basis["value"] if d_basis else None
    row["volume_basis_excluded"] = (today["volume"] - d_basis["volume"]) if d_basis else None
    row["rvol"] = (d_basis["volume"] / vol_mean) if vol_mean and d_basis else None
    row["avg_a20"] = avg_a20
    row["avg_a20_est"] = full and any(p["value_est"] for p in p_basis)
    if full and avg_a20 is None:
        rej.append(_reason(STAGE_QUALITY, "MINUTE_VALUE_MISSING",
                           "과거 20일 1분봉 거래대금 없음 — AVG_A20 계산 불가"))
        row["missing_reason"] = row["missing_reason"] or "MINUTE_VALUE_MISSING"
    for p in [d_basis] + p_basis:
        for f in (p or {}).get("flags", ()):
            if f not in flags:
                flags.append(f)

    # 4.3 가격
    row["ma20"] = sma_at(closes, t.ma_short, j)
    row["ma60"] = sma_at(closes, t.ma_long, j)
    row["ma20_lag"] = sma_at(closes, t.ma_short, j - t.ma_slope_lag)
    row["clv"] = clv(C, today["high"], today["low"])
    row["hh20_prev"] = (max(b["high"] for b in prev[-t.hh_days:])
                        if len(prev) >= t.hh_days else None)
    atr = wilder_atr(highs[:-1], lows[:-1], closes[:-1], t.atr_days)
    row["atrp14"] = (100.0 * atr / closes[-2]) if atr is not None and j >= 1 else None

    # ── 1분봉: VWAP·마감 30분 ──
    krx = x.minutes.get(("KRX", code), [])
    nxt = x.minutes.get(("NXT", code), [])
    cov = x.coverage.get(("KRX", code))
    reg = [b for b in krx if KRX_REG_START <= b["kst"] < KRX_REG_END
           and b["session"] in ("REGULAR", "OTHER")]
    nxt_main = [b for b in nxt if NXT_MAIN_START <= b["kst"] < NXT_MAIN_END]
    row["minute_bars_krx"] = len(reg)
    row["minute_bars_nxt"] = len(nxt_main)
    if _nxt_included(today) and not nxt_main:
        flags.append("VWAP_KRX_ONLY")
    # VWAP 범위: KRX 정규장(09:00 ~ 15:30 종가 단일가 봉, 15:31~15:34에 늦게 찍힌 종가
    # 체결 포함) + NXT 메인. 15:35 이후 장후 시간외 종가 봉은 넣지 않는다
    scope = reg + nxt_main
    # 1분봉은 원주가다. D 일봉이 뒤에 생긴 권리락·분할로 수정되면(사후 실행) 일봉 종가와 KRX
    # 마지막 1분봉 종가가 어긋난다. 종가 단일가 체결가 = 일봉 종가이므로 5% 넘는 차이는 수정
    # 계수로 보고 1분봉 가격(VWAP·마감 30분)을 일봉 기준으로 바꾼다. 비율(Rlate)은 그대로다
    pf = 1.0
    if reg and reg[-1]["close"] and abs(C / reg[-1]["close"] - 1) > MINUTE_ADJ_TOL:
        pf = C / reg[-1]["close"]
        flags.append(f"MINUTE_PRICE_ADJUSTED={pf:.4f}")
    row["minute_price_factor"] = pf
    vw = minute_vwap(scope, t.vwap_max_unvalued_share)
    if vw["vwap"] is not None:
        vw["vwap"] *= pf
    row["vwap"] = vw["vwap"]
    row["vwap_value"] = vw["value"]
    row["vwap_volume"] = vw["volume"]
    row["vwap_method"] = "EXACT_CUM_VALUE" if vw["vwap"] is not None else None
    if reg and vw["vwap"] is None and D < CUM_VALUE_START and not x.live:
        # 누적거래대금이 없는 날의 사후 실행 — 대표가격 추정으로 대신한다
        row["vwap"] = typical_vwap(scope) * pf
        row["vwap_volume"] = sum(b["volume"] for b in scope)
        row["vwap_method"] = "ESTIMATED_TYPICAL_PRICE"
        flags.append("VWAP_ESTIMATED")
    row["gap_vwap"] = (C / row["vwap"] - 1) if row["vwap"] else None
    if reg and row["vwap"] is None:
        rej.append(_reason(STAGE_QUALITY, "VWAP_VALUE_MISSING",
                           f"거래대금 없는 1분봉 {vw['missing_bars']}개"
                           f"(거래량 {vw['missing_volume']:,}) — VWAP 계산 불가"))
        row["missing_reason"] = row["missing_reason"] or "VWAP_VALUE_MISSING"
    elif vw["missing_bars"]:
        flags.append(f"VWAP_UNVALUED_EXCLUDED={vw['missing_volume']}")

    lw = late_window(reg, t.late_start, t.late_end)
    row["late_window"] = lw
    if not reg:
        rej.append(_reason(STAGE_QUALITY, "NO_MINUTES", "당일 KRX 1분봉 없음(수신 누락)"))
        row["missing_reason"] = row["missing_reason"] or "NO_MINUTES"
    elif lw["bars"] == 0:
        rej.append(_reason(STAGE_QUALITY, "LATE_WINDOW_EMPTY",
                           f"마감 30분({lw['expected']}분) 1분봉 0개"))
    elif lw["bars"] < lw["expected"] and cov is None:
        rej.append(_reason(STAGE_QUALITY, "LATE_WINDOW_UNVERIFIED",
                           f"마감 30분 봉 {lw['bars']}/{lw['expected']} — "
                           "수집 기록 없어 무거래·누락 구분 불가"))
    elif lw["bars"] < lw["expected"]:
        flags.append(f"LATE_NO_TRADE_MIN={lw['expected'] - lw['bars']}")
    if lw["p_start"] and lw["p_end"]:
        row["rlate"] = lw["p_end"] / lw["p_start"] - 1
        row["hold_close"] = C / (lw["p_end"] * pf) - 1
    else:
        row["rlate"] = row["hold_close"] = None

    # ── 4.1 확정 체결량 D-5..D-1 ──
    fdays = cal[max(0, di - t.flow_days):di]
    per = x.flows.get(code, {})
    got, miss, prov = [], [], []
    for d in fdays:
        sd = per.get(d)
        if sd is None:
            miss.append(d)
        elif sd.provisional:
            prov.append(d)
        else:
            got.append(sd)
    row["flow_days"] = [str(d) for d in fdays]
    row["flow_source"] = sorted({sd.source for sd in got})
    confirmed = [d for d in sorted(per) if d < D and not per[d].provisional]
    row["last_confirmed_flow_date"] = str(confirmed[-1]) if confirmed else None
    avail = [x.flow_available.get((code, d)) for d in fdays]
    avail = [a for a in avail if a is not None]
    row["available_at"] = max(avail).isoformat() if avail else None
    fm = None
    if len(fdays) < t.flow_days or miss:
        rej.append(_reason(STAGE_QUALITY, "FLOW_MISSING",
                           f"확정 체결량 결측 {len(miss) or t.flow_days}일"
                           + (f"(최근 {miss[-1]})" if miss else "")))
        row["missing_reason"] = row["missing_reason"] or "FLOW_MISSING"
    elif prov:
        rej.append(_reason(STAGE_QUALITY, "FLOW_NOT_CONFIRMED",
                           f"잠정 체결량만 있음 {prov[-1]} — 확정 전 신규 선정 생략"))
        row["missing_reason"] = row["missing_reason"] or "FLOW_NOT_CONFIRMED"
    else:
        fm = flow_metrics([(sd.buy, sd.sell, None) for sd in got])
        if avail and max(avail) > x.decision_at:
            if x.live:
                rej.append(_reason(STAGE_QUALITY, "FLOW_AFTER_DECISION",
                                   "체결량 수신이 선정 시각 뒤"))
            else:
                flags.append("FLOW_AVAILABLE_AFTER_DECISION")
    row["buy_total"] = [sd.buy for sd in got] if fm else None
    row["sell_total"] = [sd.sell for sd in got] if fm else None
    row["unclassified"] = None
    for k in ("dr5", "dr2", "pos5", "delta_prev"):
        row[k] = fm[k] if fm else None

    # 보조 — DR20(D-20..D-1 확정 체결량, 결측·잠정이 하나라도 있으면 없음). 판정에 쓰지 않는다
    d20 = cal[max(0, di - DR20_DAYS):di]
    s20 = [per.get(d) for d in d20]
    if len(d20) == DR20_DAYS and all(sd is not None and not sd.provisional for sd in s20):
        vf = sum(sd.buy + sd.sell for sd in s20)
        row["dr20"] = 100.0 * sum(sd.buy - sd.sell for sd in s20) / vf if vf else None
    else:
        row["dr20"] = None

    if row["clv"] is None and today["volume"] > 0:
        rej.append(_reason(STAGE_QUALITY, "CLV_UNDEFINED", "고가 = 저가"))

    # ── 5장 필수 조건 — 품질 통과 여부와 무관하게 전부 매겨 진단에 남긴다 ──
    _filters(row, t)
    # 사전등록 CVDSEL 전향 기록 — 판정에 쓰지 않는다
    row["prereg_cvdsel"] = cvdsel_sets(row)
    return _finish(row)


def _minute_basis(code: str, bar: dict, x: Inputs, t: Thresholds):
    """하루의 1분봉 범위 거래량·거래대금 — 일봉에만 있는 물량(블록딜 추정)을 뺀 값.

    - 범위는 bars-1.0 일봉과 같다: KRX(애프터마켓 제외) + 2026-08-03부터 NXT 메인.
      일봉 표시(NXT_MAIN_VOLUME_INCLUDED)가 아니라 날짜로 정한다 — 16:10 잠정 봉과 확정
      봉에서 같은 범위가 된다
    - 거래량 = min(일봉, 1분봉 합). 1분봉이 일봉보다 큰 날(키움 1분봉 일부)은 일봉으로 자른다
    - 1분봉은 원주가다. 일봉 종가와 1분봉 마지막 종가가 5% 넘게 다르면 수정 계수로 보고
      거래량을 그 계수로 바꾼다(액면분할 등이 20일 창에 걸친 경우)
    - 거래대금 = 1분봉 누적거래대금 차이 합. 대금 없는 봉은 (H+L+C)/3 × 거래량으로 채우고,
      그 거래량이 0.1%를 넘으면 추정(value_est)으로 표시한다. 2025-10-15 전 날은 전부 추정
    """
    d = bar["date"]
    k = x.minute_totals.get(("KRX", code, d))
    if not k or k["volume"] <= 0:
        return None
    vol, value = k["volume"], k["value"] + k["typ_unvalued"]
    unvalued = k["unvalued_volume"]
    if d >= VOLUME_SCOPE_CHANGE:
        n = x.minute_totals.get(("NXT", code, d))
        if n:
            vol += n["volume"]
            value += n["value"] + n["typ_unvalued"]
            unvalued += n["unvalued_volume"]
    flags = []
    if k["last_close"]:
        f = bar["close"] / k["last_close"]
        if abs(f - 1) > MINUTE_ADJ_TOL:
            vol = round(vol / f)
            unvalued = round(unvalued / f)
            flags.append(f"MINUTE_VOLUME_ADJUSTED={d}")
    if vol > bar["volume"]:
        value *= bar["volume"] / vol
        unvalued = round(unvalued * bar["volume"] / vol)
        vol = bar["volume"]
        flags.append(f"MINUTE_OVER_DAILY={d}")
    est = unvalued > t.vwap_max_unvalued_share * vol
    return {"volume": vol, "value": value, "value_est": est, "flags": flags}


def _nxt_missing(bar):
    return "NXT_VOLUME_MISSING" in bar["flags"]


def _nxt_included(bar):
    return "NXT_MAIN_VOLUME_INCLUDED" in bar["flags"]


def _need(row, rej, key, label):
    if row.get(key) is None:
        rej.append(_reason(STAGE_FILTER, f"{key.upper()}_NA", f"{label} 계산 불가"))
        return False
    return True


def _filters(row, t: Thresholds):
    rej = row["reject_reason"]

    def fail(code, text):
        rej.append(_reason(STAGE_FILTER, code, text))

    if _need(row, rej, "avg_a20", "AVG_A20") and row["avg_a20"] < t.min_avg_value:
        fail("LIQUIDITY", f"AVG_A20 {row['avg_a20']/1e8:,.0f}억 < {t.min_avg_value/1e8:,.0f}억")
    C = row.get("krx_close")
    if all(_need(row, rej, k, k.upper()) for k in ("ma20", "ma60", "ma20_lag")):
        if not (C > row["ma20"] > row["ma60"]):
            fail("TREND", "C > MA20 > MA60 아님")
        if not row["ma20"] > row["ma20_lag"]:
            fail("MA20_SLOPE", f"MA20 ≤ MA20(D-{t.ma_slope_lag})")
    if row.get("dr5") is not None:
        if row["dr5"] < t.min_dr5:
            fail("DR5", f"DR5 {row['dr5']:+.2f}% < {t.min_dr5:+.0f}%")
        if row["pos5"] < t.min_pos5:
            fail("POS5", f"POS5 {row['pos5']} < {t.min_pos5}")
        if not row["dr2"] > 0:
            fail("DR2", f"DR2 {row['dr2']:+.2f}% ≤ 0")
        if not row["delta_prev"] > 0:
            fail("DELTA_PREV", "Delta(D-1) ≤ 0")
    if _need(row, rej, "rvol", "RVOL") and row["rvol"] < t.min_rvol:
        fail("RVOL", f"RVOL {row['rvol']:.2f} < {t.min_rvol}")
    if row.get("clv") is not None and row["clv"] < t.min_clv:
        fail("CLV", f"CLV {row['clv']:.2f} < {t.min_clv}")
    if _need(row, rej, "gap_vwap", "GAPVWAP"):
        g = row["gap_vwap"]
        if not (0 < g <= t.max_gap_vwap):
            fail("GAP_VWAP", f"GAPVWAP {g*100:+.2f}% ∉ (0, {t.max_gap_vwap*100:.0f}%]")
    if row.get("rlate") is not None:
        if row["rlate"] < t.min_rlate:
            fail("RLATE", f"Rlate {row['rlate']*100:+.2f}% < 0")
        if row["hold_close"] < t.min_hold_close:
            fail("HOLD_CLOSE", f"HoldClose {row['hold_close']*100:+.2f}% < "
                               f"{t.min_hold_close*100:+.1f}%")
    if _need(row, rej, "atrp14", "ATRP14") and row["atrp14"] > t.max_atrp:
        fail("ATRP", f"ATRP14 {row['atrp14']:.2f}% > {t.max_atrp:.0f}%")


def _finish(row):
    row["filter_pass"] = not row["reject_reason"]
    row["quality_pass"] = not any(r["stage"] == STAGE_QUALITY
                                  for r in row["reject_reason"])
    row["score"] = score(row) if row["filter_pass"] else None
    return row


def score(row) -> dict:
    parts = {
        "dr5": band_score(row["dr5"], SCORE_TABLE["dr5"]),
        "pos5": band_score(row["pos5"], SCORE_TABLE["pos5"]),
        "rvol": band_score(row["rvol"], SCORE_TABLE["rvol"]),
        "clv": band_score(row["clv"], SCORE_TABLE["clv"]),
        "breakout": (BREAKOUT_SCORE[0]
                     if row["hh20_prev"] is not None and row["krx_close"] > row["hh20_prev"]
                     else BREAKOUT_SCORE[1]),
        "vwap": next((pts for hi, pts in VWAP_SCORE if 0 < row["gap_vwap"] <= hi), 0),
    }
    return {"total": sum(parts.values()), "parts": parts}


def rank(rows: list, pick: int) -> list:
    """통과 종목 순위: 총점 → DR5 → AVG_A20 → 종목코드. 상위 `pick`개에 `picked`."""
    passed = sorted((r for r in rows if r["filter_pass"]),
                    key=lambda r: (-r["score"]["total"], -r["dr5"],
                                   -r["avg_a20"], r["symbol"]))
    for i, r in enumerate(passed, 1):
        r["rank"] = i
        r["picked"] = i <= pick
    for r in rows:
        r.setdefault("rank", None)
        r.setdefault("picked", False)
    return passed


__all__ = ["Inputs", "evaluate", "score", "rank", "STAGE_QUALITY", "STAGE_FILTER"]

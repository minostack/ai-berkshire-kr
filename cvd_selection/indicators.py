# -*- coding: utf-8 -*-
"""지표 계산 — 명세 4장. 입력은 파이썬 리스트, 저장소를 모른다(테스트 가능하게).

모든 함수는 계산할 수 없으면 None을 돌려준다. 0으로 메우지 않는다.
"""

from __future__ import annotations

from datetime import time


# ─────────────────────────────────────────────────────────────
# 4.1 확정 CVD — 체결량 흐름
# ─────────────────────────────────────────────────────────────

def flow_metrics(days: list) -> dict | None:
    """D-5..D-1 확정 체결량 → DR5·DR2·POS5·Delta_(D-1).

    `days`: 오래된 순 [(buy, sell, unclassified|None), …] 길이 5.
    Vflow = B + S + U. 미분류량 U를 공급자가 주지 않으면 0으로 두지 않고
    `u_known=False`로 표시한다(분모가 명세보다 작다).
    """
    if len(days) < 5:
        return None
    deltas = [b - s for b, s, _u in days]
    vflow = [b + s + (u or 0) for b, s, u in days]
    v5, v2 = sum(vflow), sum(vflow[-2:])
    if v5 <= 0 or v2 <= 0:
        return None
    return {
        "dr5": 100.0 * sum(deltas) / v5,
        "dr2": 100.0 * sum(deltas[-2:]) / v2,
        "pos5": sum(1 for d in deltas if d > 0),
        "delta_prev": deltas[-1],
        "deltas": deltas,
        "u_known": all(u is not None for _b, _s, u in days),
    }


# ─────────────────────────────────────────────────────────────
# 4.2·4.3 일봉 지표 — `bars`는 오래된 순, 마지막이 D
# ─────────────────────────────────────────────────────────────

def mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def sma_at(closes: list, n: int, end: int):
    """closes[end-n+1 .. end] 평균. 모자라면 None."""
    if end < n - 1 or end >= len(closes):
        return None
    return mean(closes[end - n + 1:end + 1])


def wilder_atr(highs, lows, closes, n: int = 14):
    """마지막 봉까지의 Wilder ATR(n). 첫 TR은 고가−저가, 이후 전일 종가로 갭 반영.

    시드 = 처음 n개 TR 평균, 이후 ATR = (ATR×(n−1) + TR) / n.
    """
    m = len(closes)
    if m < n + 1:
        return None
    tr = [highs[0] - lows[0]]
    for i in range(1, m):
        pc = closes[i - 1]
        tr.append(max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc)))
    atr = mean(tr[:n])
    for x in tr[n:]:
        atr = (atr * (n - 1) + x) / n
    return atr


def clv(close, high, low):
    """(C − L) / (H − L). 고가 = 저가면 None(명세: 결측 → 신규 선정 제외)."""
    if high is None or low is None or high <= low:
        return None
    return (close - low) / (high - low)


# ─────────────────────────────────────────────────────────────
# 4.3 VWAP·4.4 마감 30분 — 1분봉
# ─────────────────────────────────────────────────────────────

def minute_vwap(bars: list, max_unvalued_share: float = 0.0) -> dict:
    """1분봉 → VWAP = Σ거래대금 / Σ거래량 (명세 4.3 `VWAP_D = A_D / V_D`).

    봉 거래대금은 KIS 누적거래대금(`acml_tr_pbmn`)의 같은 날 직전 봉 차이다. 대표가격으로
    **추정하지 않는다.** 거래대금 없는 봉은 분자·분모에서 함께 빼고, 그 거래량이 전체의
    `max_unvalued_share`를 넘으면 계산하지 않는다(`vwap=None`).
    """
    total = sum(b["volume"] for b in bars)
    valued = [b for b in bars if b.get("traded_value") is not None]
    vol = sum(b["volume"] for b in valued)
    value = sum(float(b["traded_value"]) for b in valued)
    missing_vol = total - vol
    ok = vol > 0 and missing_vol <= max_unvalued_share * total
    return {"vwap": value / vol if ok else None, "value": value if ok else None,
            "volume": vol, "missing_bars": len(bars) - len(valued),
            "missing_volume": missing_vol}


def typical_vwap(bars: list):
    """**추정 VWAP** — 대표가격 (H+L+C)/3를 거래량으로 가중 평균한다.

    1분봉 누적거래대금이 없는 날(2025-10-15 전) 사후 실행에만 쓴다. 정확 VWAP 대비
    오차 실측(2026-10-22·23, 351종목): 중앙 1.4~1.8bp, 95% 5.7bp 이하, 최대 39bp.
    """
    vol = sum(b["volume"] for b in bars)
    if vol <= 0:
        return None
    return sum((b["high"] + b["low"] + b["close"]) / 3.0 * b["volume"]
               for b in bars) / vol


def late_window(bars: list, start: time, end: time) -> dict:
    """KRX 1분봉(시작 시각 기준) 중 [start, end) 창 → P_start·P_end·봉 수.

    무거래 분은 키움이 봉을 주지 않는다. 그래서 창 안의 빠진 분은 그날 수집이
    끝난 경우(커버리지 있음) **무거래**로 세고, 수집 자체가 없으면 호출 쪽이
    결측으로 처리한다.
    """
    w = sorted((b for b in bars if start <= b["kst"] < end),
               key=lambda b: b["kst"])
    expected = (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
    if not w:
        return {"bars": 0, "expected": expected, "p_start": None, "p_end": None,
                "first": None, "last": None}
    return {"bars": len(w), "expected": expected,
            "p_start": float(w[0]["open"]), "p_end": float(w[-1]["close"]),
            "first": w[0]["kst"].strftime("%H:%M"),
            "last": w[-1]["kst"].strftime("%H:%M")}


# ─────────────────────────────────────────────────────────────
# 6장 점수
# ─────────────────────────────────────────────────────────────

def band_score(value, table) -> int:
    got = 0
    for lo, pts in table:
        if value is not None and value >= lo:
            got = pts
    return got


__all__ = ["flow_metrics", "mean", "sma_at", "wilder_atr", "clv",
           "minute_vwap", "late_window", "band_score"]

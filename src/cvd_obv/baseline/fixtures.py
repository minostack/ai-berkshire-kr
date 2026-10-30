# -*- coding: utf-8 -*-
"""골든 샘플용 결정적 합성 입력.

API를 부르지 않는다. 골든 샘플이 네트워크·시장 상태에 의존하면 회귀 테스트가
아니라 시황 테스트가 된다. 대신 시드가 고정된 선형합동생성기로 봉을 만들어
같은 시드·같은 코드면 언제나 같은 바이트가 나오게 한다.

한계(명시)
----------
- 휴장일을 반영하지 않는다(주말만 제외). 골든 샘플의 목적은 계산식 고정이지
  거래 캘린더 재현이 아니다. 실제 캘린더는 Stage 1 `silver.market_calendar`.
- 가격·거래량은 실측 분포가 아니라 합성값이다. 절대 수준에 의미를 두지 않는다.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

__all__ = [
    "FIXTURE_VERSION", "SCENARIOS", "Lcg",
    "trading_days_back", "build_daily_bars", "build_minute_bars",
    "build_case", "stub_fetchers", "restore_fetchers",
]

#: fixture 생성 규칙 버전. 이 값이 바뀌면 골든 샘플을 다시 만들어야 한다.
FIXTURE_VERSION = "1.0.0"

_AFTER_START = "20260914"


class Lcg:
    """Numerical Recipes 계수의 선형합동생성기 — 파이썬 버전과 무관하게 동일."""

    __slots__ = ("state",)

    def __init__(self, seed: int):
        self.state = seed & 0xFFFFFFFF

    def next_u32(self) -> int:
        self.state = (1664525 * self.state + 1013904223) & 0xFFFFFFFF
        return self.state

    def unit(self) -> float:
        """[0, 1) 균등."""
        return self.next_u32() / 4294967296.0

    def signed(self) -> float:
        """[-1, 1) 균등."""
        return self.unit() * 2.0 - 1.0


def _seed_of(text: str) -> int:
    """문자열 → 32bit 시드(FNV-1a). `hash()`는 실행마다 달라서 쓰지 않는다."""
    h = 2166136261
    for ch in text.encode("utf-8"):
        h = ((h ^ ch) * 16777619) & 0xFFFFFFFF
    return h


def trading_days_back(end_date: str, count: int):
    """`end_date`(YYYYMMDD)에서 과거로 주말을 제외한 `count`일. 오름차순."""
    day = datetime.strptime(end_date, "%Y%m%d")
    out = []
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day.strftime("%Y%m%d"))
        day -= timedelta(days=1)
    return list(reversed(out))


def build_daily_bars(code: str, days: int, end_date: str,
                     drift: float = 0.0006, shock_scale: float = 0.012,
                     base_price: float = 50000.0,
                     base_volume: int = 1_200_000):
    """일봉 리스트 — V1.5 `fetch_daily_bars()`가 돌려주는 형태.

    `drift`는 일간 로그수익률의 추세항, `shock_scale`은 잡음 폭이다. 잡음이
    추세보다 크면 MA20 상회·OBV 상승 같은 조건이 시드에 따라 흔들린다. 골든
    샘플은 각 Gate 경로(통과/미통과/데이터부족)를 하나씩 고정해야 하므로
    시나리오별로 둘을 함께 정한다.
    """
    rng = Lcg(_seed_of(f"daily|{code}|{end_date}|{days}"))
    dates = trading_days_back(end_date, days)
    price = base_price
    bars = []
    for i, d in enumerate(dates):
        shock = rng.signed() * shock_scale
        price = price * math.exp(drift + shock)
        close = round(price, 0)
        spread = max(close * 0.008, 10.0)
        open_ = round(close - shock * spread * 8, 0)
        high = round(max(open_, close) + rng.unit() * spread, 0)
        low = round(min(open_, close) - rng.unit() * spread, 0)
        vol = int(base_volume * (0.6 + rng.unit() * 0.8))
        bars.append({"dt": d, "close": float(close), "volume": vol,
                     "open": float(open_), "high": float(high),
                     "low": float(low)})
    return bars


def build_minute_bars(scanner, code: str, days: int, end_date: str,
                      period: int = 5, buy_bias: float = 0.35,
                      include_after: bool = True,
                      base_price: float = 50000.0,
                      base_volume: int = 9000):
    """분봉 리스트 — V1.5 `fetch_minute_bars()` 형태(세션·봉 경계 포함).

    `buy_bias`는 종가가 봉 고가 쪽에 붙는 정도다. 양수면 CVD가 매수 우위로,
    음수면 매도 우위로 나온다. 세션 분류와 봉 경계는 V1.5 함수를 그대로 써서
    fixture가 구현과 어긋나지 않게 한다.
    """
    rng = Lcg(_seed_of(f"minute|{code}|{end_date}|{days}|{period}"))
    dates = trading_days_back(end_date, days)
    price = base_price
    bars = []

    def _emit(dt_str, vol_scale):
        nonlocal price
        shock = rng.signed() * 0.0016
        price = price * math.exp(shock)
        rng_span = max(price * 0.0022, 5.0)
        low = price - rng_span * (0.5 + rng.unit() * 0.5)
        high = price + rng_span * (0.5 + rng.unit() * 0.5)
        pos = 0.5 + buy_bias * 0.5 + rng.signed() * 0.18
        pos = min(0.97, max(0.03, pos))
        close = low + (high - low) * pos
        open_ = low + (high - low) * (1.0 - pos) * 0.8 + (high - low) * 0.1
        vol = int(base_volume * vol_scale * (0.5 + rng.unit()))
        if vol <= 0:
            return
        start, end = scanner.bar_bounds(dt_str, period)
        if end is None:
            return
        bars.append({
            "dt": dt_str, "open": round(open_, 0), "high": round(high, 0),
            "low": round(low, 0), "close": round(close, 0), "volume": vol,
            "bar_start": start, "bar_end": end,
            "session": scanner.session_of(dt_str),
        })

    for d in dates:
        # 정규장 접속매매 09:00~15:20 + 15:30 종가 단일가 봉
        t = datetime.strptime(f"{d}0900", "%Y%m%d%H%M")
        for _ in range(math.ceil(380 / period)):
            _emit(t.strftime("%Y%m%d%H%M%S"), 1.0)
            t += timedelta(minutes=period)
        if 390 % period == 0:
            _emit(f"{d}153000", 2.5)
        if include_after and d >= _AFTER_START:
            t = datetime.strptime(f"{d}1600", "%Y%m%d%H%M")
            for _ in range(240 // period):
                _emit(t.strftime("%Y%m%d%H%M%S"), 0.12)
                t += timedelta(minutes=period)
    return bars


#: 골든 샘플 시나리오.
#: (이름, 종목코드, 종목명, drift, shock_scale, buy_bias, daily_days)
#: 각 Gate 결과 경로를 하나씩 덮는다 — 통과/미통과/데이터부족.
SCENARIOS = (
    ("bullish", "900001", "합성강세", 0.0032, 0.006, 0.45, 150),
    ("bearish", "900002", "합성약세", -0.0032, 0.006, -0.40, 150),
    ("warmup", "900003", "합성워밍업", 0.0010, 0.012, 0.20, 18),
)


def build_case(scanner, name: str, end_date: str, period: int = 5,
               bdays: int = 5):
    """시나리오 하나의 입력 묶음."""
    for key, code, label, drift, shock, bias, daily_days in SCENARIOS:
        if key == name:
            break
    else:
        raise KeyError(f"알 수 없는 시나리오: {name}")
    return {
        "scenario": name,
        "code": code,
        "name": label,
        "period": period,
        "bdays": bdays,
        "end_date": end_date,
        "daily_days": daily_days,
        "daily_bars": build_daily_bars(code, daily_days, end_date, drift=drift,
                                       shock_scale=shock),
        "minute_bars": build_minute_bars(scanner, code, bdays, end_date,
                                         period=period, buy_bias=bias),
    }


def stub_fetchers(scanner, cases):
    """`analyze()`가 API 대신 fixture를 읽도록 fetch 계열을 바꿔치기한다.

    되돌리기용 원본 함수를 함께 돌려준다 — 전역 상태를 건드리는 이상 복구까지
    책임진다.
    """
    by_code = {c["code"]: c for c in cases}
    original = {
        "fetch_minute_bars": scanner.fetch_minute_bars,
        "fetch_daily_bars": scanner.fetch_daily_bars,
        "fetch_stock_name": scanner.fetch_stock_name,
    }

    def fake_minute(code, token, period=5, bdays=5, as_of=None, **kwargs):
        case = by_code[code]
        bars = [dict(b) for b in case["minute_bars"]]
        if as_of is not None:
            bars = [b for b in bars if b["bar_end"] <= as_of]
        return bars

    def fake_daily(code, token, days=60, as_of=None, **kwargs):
        case = by_code[code]
        return [dict(b) for b in case["daily_bars"]]

    def fake_name(code, token):
        return by_code[code]["name"]

    scanner.fetch_minute_bars = fake_minute
    scanner.fetch_daily_bars = fake_daily
    scanner.fetch_stock_name = fake_name
    return original


def restore_fetchers(scanner, original):
    for key, fn in original.items():
        setattr(scanner, key, fn)

# -*- coding: utf-8 -*-
"""실시간 스캐너용 기준 봉 공급기 — 봉 기준 `bars-1.0`.

V1.5(`tools/kiwoom_cvd_obv_scanner.py`)는 해시로 동결돼 있어 고칠 수 없다. 그래서
replay가 하는 것과 같은 방식으로, V1.5 모듈의 `fetch_daily_bars`·
`fetch_minute_bars` 자리에 기준 봉을 주는 함수를 끼운다(`installed()`). 계산
함수(`analyze`, `calc_*`, 점수·Gate)는 그대로 돈다.

| | V1.5 원래 동작 | 기준 공급기 |
|---|---|---|
| 일봉 | 키움 ka10081(2026-09-14 이후 EXTENDED, 종가 = 20:00 애프터마켓 최종가) | **적재된** 기준 일봉(정규장·수정주가)만 |
| 분봉 | 키움 ka10080 `--period`분봉 | **적재된 1분봉만** 읽어 `--period`분봉으로 합성(`stored_minutes`). **API를 부르지 않는다**(2026-10-26) |
| 일봉 완료 | `daily_scope=auto`(20:05) | `daily_scope=regular`(15:35) — 정규장 봉이므로 |

기준 거래일 (2026-10-26 확정)
---------------------------
실행 시각으로 분봉을 자르지 않는다. **적재 단계가 기준일을 정한다** —

    16:10 이전 → 전 거래일 **확정** 봉으로 스캔
    16:10 이후 → 오늘 **잠정** 봉으로 스캔

장중에 "지금까지의 분봉"을 쓰면 10:30에 돌린 값과 14:00에 돌린 값이 달라지고
둘 다 "오늘"이라 불린다 — 재현도 대조도 안 된다. 규칙은
`bar_standard.resolve_scan_day()`에 있다.

CVD 가속 스캐너(`tools/cvd_accel_scanner.py`)도 같은 V1.5 함수를 불러 쓰므로
같은 공급기를 쓴다.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import datetime

from .bar_standard import (
    BAR_STANDARD_VERSION, STORED_MINUTE_PERIOD, StandardDailyCache,
    resample_bars, resolve_scan_day,
)

__all__ = ["StandardLiveFeed", "index_codes", "STANDARD_DAILY_SCOPE"]

#: 기준 일봉은 정규장 봉이므로 V1.5에 "정규장 마감에 확정"으로 알려 준다.
STANDARD_DAILY_SCOPE = "regular"


def index_codes(path: str = None) -> list:
    """`data/index_stocks.json`의 코스피200·코스닥150 종목코드(오늘 명단).

    일봉 캐시를 미리 채울 범위를 정하는 데 쓴다. 판정 명단은 `universe_on()`.
    """
    from .universe import current_universe
    return current_universe(path=path).codes()


class StandardLiveFeed:
    """V1.5 fetch 함수와 같은 서명으로 기준 봉을 준다."""

    def __init__(self, scanner, as_of: datetime = None, codes=None,
                 store=None, log=None):
        self.sc = scanner
        self.as_of = as_of
        self.codes = set(codes or ())
        self.store = store
        self.log = log or (lambda msg: print(msg, file=sys.stderr))
        # V1.5 원래 fetch 함수는 더 쓰지 않는다 — 적재분만 읽는다(2026-10-26).
        # 원복은 `installed()`가 따로 들고 있다.
        self._cache = None
        self.missing_local = []          # 로컬 기준 일봉이 없던 종목
        self._universe = None
        self.minute_calls = 0
        # 기준 거래일 — 실행 시각이 아니라 적재 상태가 정한다(2026-10-26).
        # as_of를 직접 준 호출(replay·회귀 테스트)은 그대로 존중한다.
        self.scan_day = None
        if self.as_of is None:
            self.scan_day = resolve_scan_day(store=store)
            self.as_of = self.scan_day.end_of_day
        # 분봉은 **적재분만** 읽는다 — API를 부르지 않는다
        from .stored_minutes import StoredMinutes
        self._minutes = StoredMinutes(store=store, log=self.log)

    # ── 일봉 ──────────────────────────────────────────────────────
    def _daily_cache(self, code, as_of):
        if self._cache is None or (code not in self.codes):
            self.codes |= set(index_codes()) | {str(code)}
            self._cache = StandardDailyCache(sorted(self.codes),
                                             as_of or self.as_of,
                                             store=self.store)
        return self._cache

    def fetch_daily_bars(self, code, token, days=60, as_of=None):
        """적재된 기준 일봉만. `token`은 받지만 쓰지 않는다(V1.5 서명 유지)."""
        as_of = as_of or self.as_of
        cache = self._daily_cache(str(code), as_of)
        # live_fetch를 주지 않는다 — 적재분만 쓴다(2026-10-26)
        bars = cache.bars(str(code), days=days)
        if not bars:
            self.missing_local.append(str(code))
        return [{k: b[k] for k in ("dt", "open", "high", "low", "close", "volume")}
                for b in bars]

    # ── 분봉 ──────────────────────────────────────────────────────
    def fetch_minute_bars(self, code, token, period=5, bdays=5, as_of=None):
        """적재된 1분봉만 읽어 `period`분봉으로 합성한다. API를 부르지 않는다."""
        as_of = as_of or self.as_of
        self.minute_calls += 1
        one = self._minutes.bars(code, bdays=bdays, as_of=as_of)
        if int(period) == STORED_MINUTE_PERIOD:
            return one
        return resample_bars(one, period, as_of=as_of,
                             session_of=self.sc.session_of)

    # ── 설치 ──────────────────────────────────────────────────────
    @contextmanager
    def installed(self):
        """V1.5 모듈의 fetch 함수와 명단 로더를 기준 공급기로 바꾼다(끝나면 되돌린다)."""
        saved = (self.sc.fetch_daily_bars, self.sc.fetch_minute_bars,
                 self.sc.load_index)
        self.sc.fetch_daily_bars = self.fetch_daily_bars
        self.sc.fetch_minute_bars = self.fetch_minute_bars
        self.sc.load_index = self.load_index
        try:
            yield self
        finally:
            (self.sc.fetch_daily_bars, self.sc.fetch_minute_bars,
             self.sc.load_index) = saved

    # ── 명단 ──────────────────────────────────────────────────────
    def universe(self):
        """스캔 기준일의 명단(유니버스 기준 universe-1.0, `universe_on`)."""
        if self._universe is None:
            from .universe import universe_on
            day = (self.as_of or datetime.now()).date()
            self._universe = universe_on(day)
            for w in self._universe.warnings:
                self.log(f"  ⚠ [유니버스] {w}")
        return self._universe

    def load_index(self, name: str) -> list:
        """V1.5 `load_index(name)`와 같은 형태 — `[{"code", "name"}, …]`."""
        from .universe import parse_indexes
        idx = parse_indexes(name)[0]
        stocks = self.universe().stocks(idx)
        self.log(f"  📋 {name}: {len(stocks)}개 종목 ({self.universe().sources.get(idx)})")
        return stocks

    def banner_lines(self):
        """스캔 전에 찍는 기준일 안내 — 무엇으로 돌렸는지 결과보다 먼저 보여 준다."""
        d = self.scan_day
        if d is None:
            return [f"  [봉 기준 {BAR_STANDARD_VERSION}] 기준시각 지정 실행 "
                    f"(as_of={self.as_of})"]
        out = [f"  [봉 기준 {BAR_STANDARD_VERSION}] 기준 거래일 "
               f"{d.day} · {d.label} 봉 · 적재분만 사용(API 호출 없음)"]
        if d.note:
            out.append(f"  · {d.note}")
        return out

    def summary_lines(self):
        d = self.scan_day
        head = (f"  [봉 기준 {BAR_STANDARD_VERSION}] 기준 거래일 {d.day} "
                f"· {d.label} 봉") if d else \
               f"  [봉 기준 {BAR_STANDARD_VERSION}] as_of={self.as_of}"
        out = [head + " · 일봉·분봉 모두 적재분(API 호출 없음)"]
        if self.minute_calls:
            out += self._minutes.summary_lines()
        if self._cache is not None:
            from datetime import timedelta
            from .bar_standard import unrestored_days
            end = self._cache.as_of
            end = end.date() if isinstance(end, datetime) else end
            pending = unrestored_days(store=self.store,
                                      start=end - timedelta(days=14), end=end,
                                      securities={f"KRX:{c}" for c in self.codes})
            if pending:
                out.append("  ⚠ 정규장 복원 전인 날은 일봉 계산(OBV·MA·유동성)에서 빠진다: "
                           + ", ".join(f"{d} {n}종목" for d, n in pending.items())
                           + " — 20:05 이후 `python cvdobv.py data-load daily`")
        if self.missing_local:
            uniq = sorted(set(self.missing_local))
            out.append(f"  ⚠ 로컬 기준 일봉이 없는 종목 {len(uniq)}개"
                       f"({', '.join(uniq[:8])}{' …' if len(uniq) > 8 else ''}) — "
                       f"OBV·MA·유동성이 결측으로 처리된다. "
                       f"`python cvdobv.py data-load daily`로 적재")
        return out

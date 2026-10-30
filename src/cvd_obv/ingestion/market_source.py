# -*- coding: utf-8 -*-
"""일봉·1분봉 **적재 원천** 선택 — 기본 KIS(2026-10-23부터), 키움은 대체 원천.

왜 바꿨나: 키움 REST가 2026-10-21부터 "8050 IP 미등록"으로 거절해 적재가 멈췄고,
KIS 1분봉은 키움 1분봉과 봉 단위로 같으며(`kis_minute` 머리말) 날짜를 지정해 받을 수
있고 누적거래대금을 준다.

고르는 순서: 명령 인자 `--source` > 환경변수 `CVD_MARKET_SOURCE`(`kis`|`kiwoom`) > `kis`.
같은 선택을 쓰는 곳: `silver.daily_bars`·`silver.minute_bars`·`silver.benchmark_bars`
적재, 스캐너 실행 중 1분봉(`live_feed` → `live_minutes`), 구성종목 스냅샷
(`universe snapshot`). 스캐너의 일봉 당일분·종목명·토큰은 여전히 V1.5(키움)다.
"""

from __future__ import annotations

import os

from ..domain.errors import ConfigError

__all__ = ["SOURCES", "DEFAULT_SOURCE", "DAILY_SOURCE_IDS", "resolve_source",
           "daily_source", "minute_source", "benchmark_source", "universe_source",
           "diagnose"]

SOURCES = ("kis", "kiwoom")
DEFAULT_SOURCE = "kis"
#: `silver.daily_bars.source` 중 공급자 원본 일봉(파생 봉 제외)
DAILY_SOURCE_IDS = ("KIWOOM_KA10081", "KIS_FHKST03010100")


def resolve_source(name: str = None) -> str:
    name = (name or os.environ.get("CVD_MARKET_SOURCE") or DEFAULT_SOURCE).strip().lower()
    if name not in SOURCES:
        raise ConfigError(f"적재 원천은 {SOURCES} 중 하나: {name!r}")
    return name


def daily_source(name: str = None, adjusted: bool = True, max_pages: int = None):
    if resolve_source(name) == "kiwoom":
        from .kiwoom_daily import KiwoomDailySource
        kw = {"max_pages": max_pages} if max_pages else {}
        return KiwoomDailySource(adjusted=adjusted, **kw)
    from .kis_daily import KisDailySource
    return KisDailySource(adjusted=adjusted, max_pages=max_pages)


def minute_source(name: str = None):
    if resolve_source(name) == "kiwoom":
        from .kiwoom_minute import KiwoomMinuteSource
        return KiwoomMinuteSource()
    from .kis_minute import KisMinuteSource
    return KisMinuteSource()


def benchmark_source(name: str = None):
    if resolve_source(name) == "kiwoom":
        from .kiwoom_benchmark import KiwoomBenchmarkSource
        return KiwoomBenchmarkSource()
    from .kis_benchmark import KisBenchmarkSource
    return KisBenchmarkSource()


def universe_source(name: str = None, previous=None, log=None):
    if resolve_source(name) == "kiwoom":
        from .universe_snapshots import KiwoomUniverseSource
        return KiwoomUniverseSource()
    from .kis_universe import KisUniverseSource
    return KisUniverseSource(previous=previous, log=log)


def diagnose(name: str = None) -> str:
    """적재 원천 API가 실패했을 때 실제 거절 사유 한 줄(`원천: 사유`)."""
    src = resolve_source(name)
    if src == "kiwoom":
        from .kiwoom_auth import diagnose_kiwoom
        return f"키움: {diagnose_kiwoom()}"
    from .kis_client import diagnose_kis
    return f"KIS: {diagnose_kis()}"

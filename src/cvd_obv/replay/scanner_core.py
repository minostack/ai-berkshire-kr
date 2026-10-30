# -*- coding: utf-8 -*-
"""V1.5 계산 코어에 cache를 주입한다 — 로드맵 Stage 5.

왜 코드를 다시 쓰지 않는가
--------------------------
replay가 live와 다른 코드로 계산하면, 나중에 결과가 갈릴 때 **어느 쪽이 맞는지
알 방법이 없다.** 그래서 V1.5 `analyze()`를 그대로 부르고 입력만 바꾼다.
Stage 0에서 동결한 골든 회귀가 그 계산식을 지키고 있으므로, 여기서 하는 일은
"API 자리에 cache를 끼우는 것"뿐이다.

주입 방식
---------
`analyze()`는 내부에서 `fetch_minute_bars`·`fetch_daily_bars`·`fetch_stock_name`을
직접 부른다. 이 세 함수를 **호출하는 동안만** 바꿔치기하고, 그 사이 네트워크
함수(`_kiwoom`)가 불리면 예외를 던진다. 조용히 API를 타면 그건 더 이상 replay가
아니기 때문이다.

시각 규약
---------
V1.5는 naive KST datetime으로 계산한다(`datetime.now()`). 그래서 `as_of`도 naive
KST로 받는다. tz-aware를 받으면 KST로 바꾼 뒤 tzinfo를 떼어 낸다 — 두 규약이
섞이면 세션 판정이 조용히 어긋난다.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime

from ..baseline.scanner_v15 import load_scanner_v15
from ..domain.errors import ConfigError
from ..domain.ids import KST

__all__ = ["CacheBackedScanner", "ReplayNetworkError"]


class ReplayNetworkError(RuntimeError):
    """replay 중 네트워크 호출이 일어났다 — 있어서는 안 되는 일이다."""


def _naive_kst(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(KST).replace(tzinfo=None)


class CacheBackedScanner:
    """cache에서 읽은 봉으로 V1.5 `analyze()`를 돌린다."""

    def __init__(self, scanner=None):
        self.scanner = scanner or load_scanner_v15()
        self.calls = 0

    # ── 주입 ──────────────────────────────────────────────────────
    @contextmanager
    def _injected(self, code, minute_bars, daily_bars, name):
        sc = self.scanner
        saved = {attr: getattr(sc, attr)
                 for attr in ("fetch_minute_bars", "fetch_daily_bars",
                              "fetch_stock_name", "_kiwoom", "_get_token")}

        def _minute(req_code, token, period=5, bdays=5, as_of=None):
            if str(req_code) != str(code):
                raise ConfigError(
                    f"replay가 예상 밖 종목을 요청했다: {req_code} != {code}")
            return list(minute_bars)

        def _daily(req_code, token, days=60, as_of=None):
            if str(req_code) != str(code):
                raise ConfigError(
                    f"replay가 예상 밖 종목을 요청했다: {req_code} != {code}")
            return list(daily_bars)

        def _name(req_code, token):
            return name or str(req_code)

        def _blocked(*args, **kwargs):
            raise ReplayNetworkError(
                "replay 중에는 API를 부르지 않는다 — cache가 비어 있는지 확인하라")

        sc.fetch_minute_bars = _minute
        sc.fetch_daily_bars = _daily
        sc.fetch_stock_name = _name
        sc._kiwoom = _blocked
        sc._get_token = _blocked
        try:
            yield
        finally:
            for attr, value in saved.items():
                setattr(sc, attr, value)

    # ── 실행 ──────────────────────────────────────────────────────
    def analyze(self, code, as_of, minute_bars, daily_bars, name=None,
                period: int = 5, bdays: int = 5, daily_scope: str = "auto",
                rank_session: str = None):
        """한 종목의 분석 결과(V1.5 dict) 또는 None."""
        sc = self.scanner
        session = rank_session or sc._SESSION_REGULAR
        with self._injected(code, minute_bars, daily_bars, name):
            result = sc.analyze(code, token="", period=period, bdays=bdays,
                                name=name or str(code),
                                as_of=_naive_kst(as_of),
                                daily_scope=daily_scope,
                                rank_session=session)
        self.calls += 1
        return result

    def finalize(self, results):
        """횡단면 백분위 점수. 모집단은 **넘겨받은 결과 전체**다.

        분석에 실패한 종목(`analyze`가 None)은 애초에 이 목록에 들어오지
        않는다. 실패 종목을 0점으로 끼워 넣으면 백분위가 통째로 밀린다.
        """
        self.scanner._finalize_scores(results)
        return results

    def snapshot_payload(self, results, as_of, index_key, run_id,
                         period: int = 5, bdays: int = 5,
                         daily_scope: str = "auto", rank_session: str = None,
                         label: str = ""):
        """live가 저장하는 것과 같은 형태의 스냅샷 payload를 만든다.

        같은 형태여야 `contracts.v15_signals.convert_v15_snapshot()`을 그대로
        태울 수 있고, 그래야 live와 replay가 **같은 SignalRecord**가 된다.
        """
        import hashlib
        import json

        sc = self.scanner
        session = rank_session or sc._SESSION_REGULAR
        config = {
            "period": period, "bdays": bdays,
            "rank_session": session,
            "daily_scope": daily_scope,
            "index": index_key,
            "session_config": sc._session_config_snapshot(),
        }
        config_hash = hashlib.sha256(
            json.dumps(config, sort_keys=True, ensure_ascii=False,
                       default=sc._json_default).encode("utf-8")
        ).hexdigest()[:12]
        local = _naive_kst(as_of)
        return {
            "scanner_version": sc._SCANNER_VERSION,
            "market_rule_version": sc._MARKET_RULE_VERSION,
            "run_id": run_id,
            "as_of": local.isoformat(),
            "saved_at": datetime.now(tz=KST).replace(tzinfo=None).isoformat(),
            "last_input_bar_at": sc._last_input_bar_at(results),
            "label": label or f"replay {local:%Y-%m-%d}",
            "config": config,
            "config_hash": config_hash,
            "count": len(results),
            "results": results,
            "note": ("과거 replay. 분봉·일봉은 cache에서 읽었고 API를 부르지 "
                     "않았다. 유니버스는 그 시점 구성종목 이력에서 가져왔다."),
        }

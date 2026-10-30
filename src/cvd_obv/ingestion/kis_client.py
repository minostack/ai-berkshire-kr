# -*- coding: utf-8 -*-
"""KIS(한국투자증권) REST 호출 — 일봉·1분봉 적재 원천(2026-10-23부터).

HTTP·토큰은 `tools/daily_scanner.py`의 `_kis_request()`를 그대로 쓴다
(`kis_trade_sides._kis`). 같은 자격증명으로 토큰을 두 군데서 발급하면 서로를
무효화할 수 있어 토큰 캐시(`.kis_token.json`)는 하나여야 한다.

여기서 더하는 것
- **호출 간격**: 실전 계좌 한도(초당 20건)보다 넉넉히 쉰다(`MIN_INTERVAL`).
- **재시도**: 응답 없음·`rt_cd != 0`이면 몇 번 더 부른다. 초당 건수 초과
  (`EGW00201`)는 조금 더 오래 쉰다.
- **진단**: 실패하면 실제 거절 사유(`msg_cd`·`msg1`)를 한 줄로 돌려준다.
"""

from __future__ import annotations

import time

from ..domain.errors import ExternalApiError
from ..domain.status import DetailCode

__all__ = ["KisClient", "diagnose_kis", "MIN_INTERVAL"]

#: 호출 사이 최소 간격(초) — 초당 약 12건
MIN_INTERVAL = 0.08
#: 초당 거래건수 초과 응답 코드
_RATE_LIMIT_CODES = ("EGW00201",)


class KisClient:
    """`_kis_request` 위에 호출 간격·재시도를 얹은 얇은 래퍼."""

    def __init__(self, request=None, retries: int = 3, sleep=time.sleep,
                 min_interval: float = MIN_INTERVAL):
        self._request = request
        self.retries = int(retries)
        self.sleep = sleep
        self.min_interval = float(min_interval)
        self._last = 0.0
        self.calls = 0
        self.last_error = None

    def _req(self):
        if self._request is None:
            from .kis_trade_sides import _kis
            self._request = _kis()._kis_request
        return self._request

    def token(self, waits=None) -> str:
        """토큰이 발급되는지 확인한다(캐시가 있으면 호출하지 않는다).

        부팅 직후 네트워크가 덜 붙은 일시 실패에 대비해 키움과 같은 간격(30·60·120초,
        `KIWOOM_TOKEN_RETRY_WAITS`)으로 다시 시도한다.
        """
        import os
        from .kis_trade_sides import _kis
        from .kiwoom_auth import _waits_from_env
        dsc = _kis()
        key, secret = os.environ["KIS_APP_KEY"], os.environ["KIS_APP_SECRET"]
        tok = dsc._kis_get_token(key, secret)
        for wait in (_waits_from_env() if waits is None else waits):
            if tok:
                break
            print(f"  ⚠ KIS 토큰 발급 실패 — {wait:g}초 뒤 다시 시도", flush=True)
            self.sleep(wait)
            tok = dsc._kis_get_token(key, secret)
        if not tok:
            raise ExternalApiError("KIS 토큰 발급 실패 — KIS_APP_KEY/KIS_APP_SECRET 확인",
                                   detail_code=DetailCode.AUTH_FAILED)
        return tok

    def get(self, path: str, params: dict, tr_id: str):
        """성공(`rt_cd == "0"`) 응답 dict, 끝내 실패하면 None(사유는 `last_error`)."""
        req = self._req()
        for attempt in range(self.retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                self.sleep(wait)
            res = req(path, params, tr_id)
            self._last = time.monotonic()
            self.calls += 1
            if res and res.get("rt_cd") == "0":
                self.last_error = None
                return res
            code = (res or {}).get("msg_cd")
            self.last_error = (f"{code} {(res or {}).get('msg1', '')}".strip()
                               if res else "응답 없음")
            backoff = 1.0 if code in _RATE_LIMIT_CODES else 0.4
            self.sleep(backoff * (attempt + 1))
        return None


def diagnose_kis() -> str:
    """KIS 데이터 요청이 실패했을 때 실제 거절 사유를 한 줄로. 예외를 내지 않는다."""
    try:
        client = KisClient(retries=0)
        client.token()
        res = client.get(
            "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": "005930",
             "FID_INPUT_DATE_1": "20260101", "FID_INPUT_DATE_2": "20260110",
             "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"},
            "FHKST03010100")
        if res is not None:
            return "지금은 정상 응답(일시 장애였을 수 있다)"
        return client.last_error or "응답 없음"
    except Exception as exc:                    # noqa: BLE001
        return f"진단 실패: {exc}"

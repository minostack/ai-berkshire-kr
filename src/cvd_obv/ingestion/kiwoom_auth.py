# -*- coding: utf-8 -*-
"""키움 토큰 발급 — 실패하면 잠시 기다렸다가 다시 시도한다.

2026-10-14 08:09 실측: 부팅 10분 뒤 자동 실행된 적재가 토큰 발급에서 "IP가 등록되지
않았습니다"로 실패했고, 같은 PC·같은 유선 네트워크에서 21분 뒤 작업은 정상이었다.
네트워크가 붙은 지 2분째였다. 이런 일시 실패로 하루 작업을 버리지 않도록 몇 번 더
시도한다. 계속 실패하면(키·IP 등록 문제) 기존처럼 `ExternalApiError`로 끝난다.

토큰 발급 자체는 V1.5 `_get_token()`을 그대로 쓴다(해시 동결 파일 — 고치지 않는다).
"""

from __future__ import annotations

import os
import time

__all__ = ["TOKEN_RETRY_WAITS", "token_with_retry"]

#: 실패 뒤 기다리는 초 — 합계 3분 30초. 환경변수 `KIWOOM_TOKEN_RETRY_WAITS`
#: ("30,60,120" 형식, 빈 값이면 재시도 없음)로 바꿀 수 있다.
TOKEN_RETRY_WAITS = (30, 60, 120)


def _waits_from_env():
    raw = os.environ.get("KIWOOM_TOKEN_RETRY_WAITS")
    if raw is None:
        return TOKEN_RETRY_WAITS
    try:
        return tuple(float(x) for x in raw.split(",") if x.strip())
    except ValueError:
        return TOKEN_RETRY_WAITS


def diagnose_kiwoom(scanner=None) -> str:
    """키움 데이터 요청이 실패했을 때 **실제 거절 사유**를 한 줄로 돌려준다.

    V1.5 `_kiwoom()`은 인증 오류 응답을 재시도 뒤 None으로 바꿔 돌려주므로, 호출하는
    쪽에서는 "응답 없음"으로만 보인다. 2026-10-21 실측: 실제로는 `return_code=3`,
    "8050: IP가 등록되지 않았습니다"였다(토큰은 캐시로 통과). 같은 요청을 한 번 직접
    보내 본문을 읽는다. 진단이 실패해도 예외를 내지 않는다.
    """
    import json
    import subprocess

    try:
        if scanner is None:
            from ..baseline.scanner_v15 import load_scanner_v15
            scanner = load_scanner_v15()
        tok = scanner._get_token()
        if not tok:
            return "토큰 발급 실패"
        body = json.dumps({"stk_cd": "005930", "tic_scope": "1", "upd_stkpc_tp": "1"})
        r = subprocess.run(
            ["curl", "-s", "--noproxy", "*", "-X", "POST",
             scanner._KIWOOM_BASE + "/api/dostk/chart",
             "-H", "Content-Type: application/json;charset=UTF-8",
             "-H", f"authorization: Bearer {tok}", "-H", "api-id: ka10080", "-d", body],
            capture_output=True, timeout=30)
        data = json.loads(r.stdout.decode("utf-8", "replace") or "{}")
        code = data.get("return_code")
        if code in (0, "0"):
            return "지금은 정상 응답(일시 장애였을 수 있다)"
        return f"return_code={code} {data.get('return_msg', '')}".strip()
    except Exception as exc:                    # noqa: BLE001
        return f"진단 실패: {exc}"


def _print(message):
    print(message, flush=True)


def token_with_retry(scanner, waits=None, sleep=time.sleep, log=_print):
    """토큰 문자열 또는 None(모든 시도 실패)."""
    waits = _waits_from_env() if waits is None else tuple(waits)
    tok = scanner._get_token()
    for i, wait in enumerate(waits, 1):
        if tok:
            return tok
        if log:
            log(f"  ⚠ 키움 토큰 발급 실패 — {wait:g}초 뒤 다시 시도({i}/{len(waits)}). "
                "부팅 직후라면 네트워크가 아직 덜 붙었을 수 있다")
        sleep(wait)
        tok = scanner._get_token()
    return tok

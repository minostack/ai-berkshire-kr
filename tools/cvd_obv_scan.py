#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CVD/OBV 스캐너 — 봉 기준 `bars-1.0` 실행 입구 (계산은 V1.5 그대로)

`tools/kiwoom_cvd_obv_scanner.py`(V1.5)는 해시로 동결된 기준 구현이다
(`data/manifests/baseline_v1_5.json`, `tests/regression/test_v15_golden.py`).
그 파일을 고치지 않고, **봉만 기준에 맞춰 넣어 준다** — replay와 같은 방식이다
(`src/cvd_obv/live_feed.py`).

| | V1.5 직접 실행 | 이 입구 |
|---|---|---|
| 일봉 | 키움 API(2026-09-14 이후 EXTENDED — 종가가 20:00 애프터마켓 최종가) | 로컬 정규장·수정주가 일봉 + 15:35~16:00에 받은 당일 봉 |
| 분봉 | 키움 API `--period`분봉 | 키움 API 1분봉 → `--period`분봉 합성(원본 대비 99.99% 일치) |
| 일봉 완료 | `--daily-scope auto`(20:05) | `--daily-scope regular`(15:35) |

기준 거래일 (2026-10-26 확정) — 실행 시각으로 자르지 않는다
    16:10 이전 → 전 거래일 **확정** 봉으로 스캔
    16:10 이후 → 오늘 **잠정** 봉으로 스캔
적재된 봉만 읽고 **API를 부르지 않는다**. 그래서 같은 날 몇 번을 돌려도
같은 값이 나오고, 키움 토큰도 필요 없다.

인자는 V1.5와 같다(`scan`, `detail`, `--period`, `--days`, `--save-json` …).
`--daily-scope`를 주지 않으면 `regular`로 채운다.

사전 조건: 로컬 일봉이 최신이어야 한다 — 16:10 `data-load provisional`(잠정),
익일 08:30 `data-load daily`(분봉 → 일봉 → 정규장 확정 복원). 로컬 일봉이 없는 종목은 OBV·MA·유동성이
결측으로 처리되고 실행 끝에 목록이 나온다.

사용법
    python tools/cvd_obv_scan.py scan --index kospi200 --top 15
    python tools/cvd_obv_scan.py scan --index both --rank-mode combined --save-json
    python tools/cvd_obv_scan.py detail --code 005930 --days 10
"""

import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))

from cvd_obv.baseline.scanner_v15 import load_scanner_v15  # noqa: E402
from cvd_obv.live_feed import STANDARD_DAILY_SCOPE, StandardLiveFeed  # noqa: E402


#: V1.5 서명을 맞추려고 넘기는 자리표시자. 적재분만 읽으므로 어디에도 쓰이지 않는다 —
#: 혹시 API 호출 경로가 남아 있으면 이 값이 그대로 전달돼 **드러나게** 실패한다.
NO_TOKEN = "NO_TOKEN__STORED_BARS_ONLY"


def standard_argv(argv: list) -> list:
    """V1.5 인자에 기준 일봉 완료 범위를 채운다. 순수 함수(테스트 대상)."""
    argv = list(argv)
    if argv and argv[0] in ("scan", "detail") and not any(
            a == "--daily-scope" or a.startswith("--daily-scope=") for a in argv):
        argv += ["--daily-scope", STANDARD_DAILY_SCOPE]
    return argv


def main(argv=None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    argv = standard_argv(sys.argv[1:] if argv is None else argv)
    if "--session" in argv and argv[argv.index("--session") + 1:][:1] == ["after"]:
        print("  ⚠ --session after는 봉 기준 bars-1.0 밖이다(신호 세션 = 정규장). "
              "참고용으로만 본다", file=sys.stderr)
    sc = load_scanner_v15()
    feed = StandardLiveFeed(sc)
    for line in feed.banner_lines():
        print(line)
    old_argv = sys.argv
    saved_token = sc._get_token
    sys.argv = [os.path.join(_BASE, "tools", "kiwoom_cvd_obv_scanner.py")] + argv
    try:
        # 적재분만 읽으므로 **키움 토큰이 필요 없다**(2026-10-26). V1.5는 시작할 때
        # 토큰을 받고 실패하면 중단하는데, 그 관문만 걷어낸다 — 해시 동결된 V1.5
        # 파일은 건드리지 않고 fetch 함수와 같은 방식으로 갈아끼운다.
        sc._get_token = lambda *a, **k: NO_TOKEN
        with feed.installed():
            rc = sc.main()
    finally:
        sys.argv = old_argv
        sc._get_token = saved_token
    if argv and argv[0] in ("scan", "detail"):
        for line in feed.summary_lines():
            print(line)
    return int(rc or 0)


if __name__ == "__main__":
    sys.exit(main())

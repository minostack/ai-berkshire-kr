# -*- coding: utf-8 -*-
"""CVD 기반 종가매수 종목 선정 스캐너 (설계 `cvdsel-2.0`).

전략 명세: `CVD_stock_selection_strategy.md` v2.0(2026-10-23) — **선정(1~6장·9장 진단)만**
구현한다. 매수 주문·청산(7·8·10장)은 별도로 진행하므로 이 패키지에 없다.

기존 스캐너(`tools/`)와 섞지 않으려고 별도 디렉토리에 둔다. 데이터 정의는 기존
기준을 그대로 쓴다(두 벌 만들지 않는다).

    일봉    `cvd_obv.bar_standard.read_standard_daily`   (bars-1.0)
    체결량  `cvd_obv.sides_flow.read_sides_flow`          (sides-1.0, UN 통합)
    명단    `cvd_obv.universe.universe_on`                (universe-1.0)
    1분봉   `silver.minute_bars` period_min=1 — KRX:<코드>, NXT:<코드>
"""

import os
import sys

DESIGN_VERSION = "cvdsel-2.0"

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_BASE, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

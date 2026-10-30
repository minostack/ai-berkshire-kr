# -*- coding: utf-8 -*-
"""CVD/OBV 검증 시스템 V2.

`docs/SYSTEM_ARCHITECTURE_V2.md`, `docs/DEVELOPMENT_ROADMAP_V2.md`,
`docs/DATA_SCHEMA_V2.md`의 계약을 코드로 고정한 패키지.

Stage 0(기준선 동결)에서는 계산 로직을 옮기지 않는다. 기준 구현은 여전히
`tools/kiwoom_cvd_obv_scanner.py` V1.5이고, 이 패키지는 그 결과를 V2 스키마로
번역·검증·동결하는 계층만 제공한다.
"""

__all__ = ["SCHEMA_VERSION", "SCANNER_VERSION", "MARKET_RULE_VERSION"]

#: `docs/DATA_SCHEMA_V2.md` SemVer
SCHEMA_VERSION = "2.0.0"

#: 기준 구현 버전 — `tools/kiwoom_cvd_obv_scanner.py` `_SCANNER_VERSION`
SCANNER_VERSION = "1.5"

#: 기준 시장제도 버전 — `_MARKET_RULE_VERSION`
MARKET_RULE_VERSION = "KRX-2026-09-14-aftermarket"

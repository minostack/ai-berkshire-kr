# -*- coding: utf-8 -*-
"""Stage 0 — 기준선 동결.

`tools/kiwoom_cvd_obv_scanner.py` V1.5를 고쳐 쓰지 않고, 그 입출력을 해시와
골든 샘플로 고정한다. 이후 어떤 단계도 이 기준선을 말없이 바꿀 수 없다.
"""

from .golden import (
    GOLDEN_AS_OF, GOLDEN_PATH, GOLDEN_VERSION, build_golden, load_golden,
    verify_golden, write_golden,
)
from .manifest import (
    MANIFEST_PATH, build_manifest, load_manifest, verify_manifest,
    write_manifest,
)
from .scanner_v15 import V15_PATH, load_scanner_v15, project_root

__all__ = [
    "V15_PATH", "load_scanner_v15", "project_root",
    "GOLDEN_VERSION", "GOLDEN_PATH", "GOLDEN_AS_OF",
    "build_golden", "write_golden", "load_golden", "verify_golden",
    "MANIFEST_PATH", "build_manifest", "write_manifest", "load_manifest",
    "verify_manifest",
]

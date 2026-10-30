# -*- coding: utf-8 -*-
"""테스트 공통 설정 — `src/`를 import 경로에 올린다.

패키지 설치(`pip install -e .`) 없이도 리포지토리에서 바로 돌게 한다.
기존 `tools/` 기반 테스트는 각자 경로를 넣으므로 건드리지 않는다.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_ROOT, "src")

for path in (_SRC,):
    if path not in sys.path:
        sys.path.insert(0, path)

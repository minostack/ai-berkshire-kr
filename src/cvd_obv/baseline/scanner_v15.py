# -*- coding: utf-8 -*-
"""기준 구현(`tools/kiwoom_cvd_obv_scanner.py` V1.5) 로더.

V1.5는 패키지가 아니라 단일 스크립트다. `sys.path`를 영구히 오염시키지 않고
경로로 직접 로드해, 어떤 파일을 기준선으로 삼았는지 항상 명시적으로 만든다.

이 모듈은 V1.5를 **수정하지 않는다.** Stage 0의 목적은 동결이지 개선이 아니다.
"""

from __future__ import annotations

import importlib.util
import os
import sys

from ..domain.errors import ConfigError

__all__ = ["V15_PATH", "project_root", "load_scanner_v15"]

_MODULE_NAME = "_cvd_obv_baseline_v15"
_cached = None


def project_root() -> str:
    """리포지토리 루트 — `src/cvd_obv/baseline/`에서 세 단계 위."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", "..", ".."))


V15_PATH = os.path.join(project_root(), "tools", "kiwoom_cvd_obv_scanner.py")


def load_scanner_v15(path: str = None):
    """V1.5 모듈 객체를 돌려준다(한 번만 로드).

    부작용: import 시 `.env` 로드와 `data/` 디렉터리 생성이 일어난다. 네트워크
    호출은 함수 호출 시점에만 발생하므로, 골든 샘플은 fetch 계열을 주입해
    대체한다(`fixtures.stub_fetchers`).
    """
    global _cached
    target = os.path.abspath(path or V15_PATH)
    if _cached is not None and getattr(_cached, "__file__", None) == target:
        return _cached
    if not os.path.exists(target):
        raise ConfigError(f"기준 구현을 찾을 수 없다: {target}")
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, target)
    if spec is None or spec.loader is None:
        raise ConfigError(f"모듈 로드 실패: {target}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    _cached = module
    return module

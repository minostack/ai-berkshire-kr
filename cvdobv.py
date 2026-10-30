#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`cvdobv` 실행 스크립트 — 설치 없이 리포지토리에서 바로 쓴다."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from cvd_obv.cli.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

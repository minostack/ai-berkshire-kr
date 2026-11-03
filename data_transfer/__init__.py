# -*- coding: utf-8 -*-
"""PC 간 수집 데이터 이관 — 운영 PC(PC1)의 잠정·확정 봉을 임시 PC(PC2)로 옮긴다.

운영 PC만 정기 적재(16:10 잠정 / 08:30 확정)를 돌리고, 다른 PC는 그 결과를 받아
스캐너만 돌린다. 두 PC가 함께 적재하면 KIS 토큰이 충돌하므로(docs/MIGRATION.md
3절 ⑦) 데이터를 옮기는 길을 따로 둔다.

    python -m data_transfer.transfer status                       # 수집 현황
    python -m data_transfer.transfer export --date 2026-11-02     # 하루치 내보내기
    python -m data_transfer.transfer export --start 2026-10-26 --end 2026-11-02
    python -m data_transfer.transfer import --latest              # DOWNLOAD의 최신 묶음

데이터 정의는 기존 기준을 그대로 쓴다 — 행을 **바꾸지 않고** 옮긴다(run_id·
data_revision 그대로). 그래서 받는 PC의 reader도 같은 규칙(리비전 큰 것, 같으면
나중 run_id)으로 최신 봉을 고른다.
"""

import os
import sys

#: 묶음(zip) 형식 버전 — manifest.json의 `format_version`
FORMAT_VERSION = "transfer-1.0"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BASE = os.path.dirname(_HERE)
_SRC = os.path.join(_BASE, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

#: 내보내기·가져오기 묶음을 두는 폴더(git 비추적)
DOWNLOAD_DIR = os.path.join(_HERE, "DOWNLOAD")

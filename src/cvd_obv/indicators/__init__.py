# -*- coding: utf-8 -*-
"""가격 지표 — 스캐너와 백테스트가 **같은 함수**를 쓰도록 모아 둔 곳.

같은 지표를 두 벌 구현하면 어느 쪽이 맞는지 알 수 없다. 백테스트가 벡터화해야
하면 여기 함수의 배열판을 두고, 스칼라판과 같은 값이 나오는지 단위 테스트로
고정한다.
"""

from .vidya import CMO_LENGTH, VIDYA_LENGTH, cmo, cmo_series, vidya

__all__ = ["CMO_LENGTH", "VIDYA_LENGTH", "cmo", "cmo_series", "vidya"]

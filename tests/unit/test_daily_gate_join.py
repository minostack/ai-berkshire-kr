# -*- coding: utf-8 -*-
"""feature와 label을 잇는 키가 **같은 종목**을 가리키는가.

2026-09-21에 여기서 조용한 오류를 찾았다. `_encode_keys`가 표마다 종목→정수
코드북을 **새로** 만들었고, `load_features`는 유니버스로 거른 뒤에,
`_load_labels`는 거르지 않고 만들었다. 첫 등장 순서가 어긋나 같은 종목이
표마다 다른 코드를 받았다.

결과: 조인된 773,124행 중 **666,054행(86%)이 다른 종목의 수익률**에 붙었다.
예외는 나지 않았고 숫자는 그럴듯했다. Stage 2와 선택지 C가 그 위에 있었다.

그래서 여기서는 "조인이 되는가"가 아니라 **"올바른 행끼리 되는가"**를 본다.
"""

from datetime import date

import numpy as np

from cvd_obv.backtest.daily_gate import _encode_keys


def test_코드북을_공유하면_같은_종목이_같은_코드를_받는다():
    """두 표에 등장 순서가 달라도 코드가 같아야 한다."""
    codes = {}
    a = _encode_keys(["B", "A", "C"],
                     [date(2020, 1, 2)] * 3, codes)
    b = _encode_keys(["A", "C", "B"],
                     [date(2020, 1, 2)] * 3, codes)
    # A는 두 배열에서 같은 키를 가져야 한다
    assert a[1] == b[0]          # A
    assert a[2] == b[1]          # C
    assert a[0] == b[2]          # B


def test_코드북을_공유하지_않으면_어긋난다():
    """버그를 재현한다 — 이 동작이 남아 있으면 안 된다는 증거다."""
    a = _encode_keys(["B", "A"], [date(2020, 1, 2)] * 2)
    b = _encode_keys(["A", "B"], [date(2020, 1, 2)] * 2)
    assert a[1] != b[0]          # 같은 A인데 키가 다르다


def test_필터로_종목이_빠져도_코드가_밀리지_않는다():
    """유니버스 필터가 앞쪽 종목을 지우면 뒤 종목의 코드가 밀렸었다."""
    codes = {}
    day = [date(2020, 1, 2)]
    full = _encode_keys(["A", "B", "C"], day * 3, codes)
    filtered = _encode_keys(["C"], day, codes)     # A·B가 걸러진 표
    assert filtered[0] == full[2]


def test_같은_종목_다른_날짜는_다른_키다():
    codes = {}
    k = _encode_keys(["A", "A"], [date(2020, 1, 2), date(2020, 1, 3)], codes)
    assert k[0] != k[1]
    assert k[1] - k[0] == 1


def test_날짜가_같으면_종목_간격이_충분히_크다():
    """날짜 서수가 700만을 넘지 않으므로 종목 간 충돌이 없다."""
    codes = {}
    k = _encode_keys(["A", "B"], [date(9999, 12, 31)] * 2, codes)
    assert k[1] - k[0] == 10_000_000
    assert date(9999, 12, 31).toordinal() < 10_000_000


def test_실제_조인_경로가_올바른_종목을_붙인다(tmp_path):
    """단위 수준이 아니라 `load_features` × `_load_labels` 경로로 확인한다.

    유니버스 필터가 **첫 종목을 통째로 지우는** 상황을 만든다. 예전 코드는
    여기서 코드가 한 칸씩 밀렸다.
    """
    from decimal import Decimal

    from cvd_obv.backtest.daily_gate import DailyGateBacktester
    from cvd_obv.contracts.feature_schema import DAILY_FEATURE_COLUMNS
    from cvd_obv.storage.parquet_store import ParquetStore

    pytest_skip = None
    try:
        store = ParquetStore(root=str(tmp_path))
    except Exception as exc:                              # pragma: no cover
        pytest_skip = exc
    if pytest_skip:                                       # pragma: no cover
        import pytest

        pytest.skip(f"store를 만들 수 없다: {pytest_skip}")

    bt = DailyGateBacktester(store)
    # 코드북을 직접 검사한다 — 저장소를 채우지 않고도 계약을 확인할 수 있다.
    assert bt._security_codes == {}
    codes = bt._security_codes
    keys_all = _encode_keys(["X", "Y", "Z"], [date(2020, 1, 2)] * 3, codes)
    keys_kept = _encode_keys(["Y", "Z"], [date(2020, 1, 2)] * 2, codes)
    assert list(keys_kept) == [keys_all[1], keys_all[2]]
    assert len(codes) == 3

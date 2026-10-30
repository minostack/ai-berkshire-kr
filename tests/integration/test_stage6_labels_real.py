# -*- coding: utf-8 -*-
"""저장된 `gold.forward_returns`가 불변식을 지키는지 — Stage 6 DoD.

빌더가 쓰는 순간에 검사하는 것과, **이미 저장된 파일**이 검사를 통과하는 것은
다른 문제다. 나중에 다른 경로로 한 줄이라도 더 써 넣으면 빌더의 검사는
지나간다. 그래서 산출물 자체를 다시 본다.

테이블이 없으면 건너뛴다. 네트워크는 타지 않는다.
"""

import os

import pytest

from cvd_obv.contracts.label_schema import (
    SIGNAL_FORWARD_RETURN_PK, is_confirmed_close,
)
from cvd_obv.domain.status import LabelStatus
from cvd_obv.storage.parquet_store import ParquetStore

pytest.importorskip("pyarrow")

COLUMNS = ["signal_id", "horizon_td", "entry_rule", "security_id",
           "signal_date", "signal_at_utc", "entry_date", "exit_date",
           "entry_at_utc", "stock_return_net", "label_status",
           "label_version", "label_source", "calendar_checked"]


@pytest.fixture(scope="module")
def rows():
    store = ParquetStore()
    directory = store.table_dir("gold", "forward_returns")
    if not os.path.isdir(directory):
        pytest.skip("신호 label이 없다 — `signal-label build` 먼저")
    table = store.read_arrow("gold", "forward_returns", columns=COLUMNS,
                             dedupe_pk=SIGNAL_FORWARD_RETURN_PK)
    if table.num_rows == 0:
        pytest.skip("신호 label이 비어 있다")
    return table.to_pylist()


def test_진입은_언제나_신호_뒤다(rows):
    """확정 종가 라벨만 예외이고, 그 예외는 **버전 문자열에 적혀 있다**.

    `label_version`에 `confirmed` 표식이 붙은 행은 신호(15:35)보다 5분 앞선
    같은 날 종가(15:30)에 샀다고 가정한 것이다. 실행 불가능하지만 의도한
    것이고, 행 하나만 보고 구별할 수 있다. 표식이 없는 행이 이 불변식을
    어기면 그건 버그다.
    """
    bad = [r for r in rows
           if r["entry_at_utc"] is not None
           and r["entry_at_utc"] <= r["signal_at_utc"]
           and not is_confirmed_close(r["label_version"])]
    assert not bad, f"신호 시각 이전 진입 {len(bad)}건"


def test_확정_종가_라벨은_신호_당일에만_앞선다(rows):
    """예외를 열어 둔 만큼 그 범위도 검사한다.

    허용하는 것은 "신호 당일 종가" 하나뿐이다. 신호일보다 **앞선 날짜**의
    진입은 확정 종가 가정으로도 설명되지 않는다.
    """
    marked = [r for r in rows if is_confirmed_close(r["label_version"])]
    if not marked:
        pytest.skip("확정 종가 라벨이 없다")
    bad = [r for r in marked
           if r["entry_date"] is not None
           and r["entry_date"] != r["signal_date"]]
    assert not bad, f"신호일과 진입일이 다른 확정 종가 라벨 {len(bad)}건"
    assert all(r["entry_rule"] == "SAME_CLOSE" for r in marked)


def test_PK가_유일하다(rows):
    keys = [tuple(r[k] for k in SIGNAL_FORWARD_RETURN_PK) for r in rows]
    assert len(keys) == len(set(keys))


def test_상태가_COMPLETE인_행에만_수익률이_있다(rows):
    """없는 값을 0으로 메우지 않았는지 양방향으로 확인한다."""
    for row in rows:
        if row["label_status"] == LabelStatus.COMPLETE:
            assert row["stock_return_net"] is not None, row["signal_id"]
        else:
            assert row["stock_return_net"] is None, row["signal_id"]


def test_모든_행이_무조건부_패널에서_왔다(rows):
    sources = {r["label_source"] for r in rows}
    assert sources == {"gold.daily_forward_returns"}


def test_날짜가_있는_행은_캘린더로_대조했다(rows):
    for row in rows:
        if row["entry_date"] is not None:
            assert row["calendar_checked"] is True, row["signal_id"]


def test_성숙_전_행에는_청산일이_없다(rows):
    pending = [r for r in rows
               if r["label_status"] == LabelStatus.PENDING_MATURITY]
    if not pending:
        pytest.skip("성숙 전 행이 없다")
    assert all(r["exit_date"] is None for r in pending)

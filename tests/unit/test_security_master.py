# -*- coding: utf-8 -*-
"""종목 명부 — 사라진 종목을 사라진 날짜와 함께 남긴다.

키움 종목 목록에는 상장폐지 종목이 없다. 그 목록으로 과거를 보면 망한 회사는
존재한 적이 없는 것이 되고, 그게 생존편향이 수집 단계에서 들어오는 경로다.

여기서 지키는 것은 **모르는 것을 null로 두는 것**이다.
- 패널 첫날부터 보이면 그 이전 상장일은 알 수 없다 → `listed_from`은 null.
- 패널 마지막 날까지 보이면 폐지되지 않았다 → `delisted_on`은 null.
"""

from datetime import date

import pytest

from cvd_obv.contracts.schema import validate_rows
from cvd_obv.contracts.universe_schema import (
    SECURITY_MASTER_COLUMNS, SECURITY_MASTER_PK,
)
from cvd_obv.domain.errors import NoInputDataError
from cvd_obv.ingestion.security_master import SecurityMasterBuilder

DAYS = [date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4), date(2026, 3, 5)]
RUN_ID = "test_20260918000000_aaaaaaaa"


class FakeApi:
    def __init__(self, by_day):
        self._by_day = by_day

    def fetch(self, service, bas_dd):
        return self._by_day.get(bas_dd, [])


def _row(code, name):
    return {"ISU_CD": code, "ISU_NM": name, "MKT_NM": "KOSPI"}


def _api(spec):
    return FakeApi({d.strftime("%Y%m%d"): rows for d, rows in spec.items()})


def _build(spec):
    builder = SecurityMasterBuilder(_api(spec))
    return builder.build(DAYS, run_id=RUN_ID, dry_run=True), builder


def test_계속_보이는_종목은_상장일도_폐지일도_모른다():
    spec = {d: [_row("005930", "삼성전자")] for d in DAYS}
    builder = SecurityMasterBuilder(_api(spec))
    runs, seen = builder.scan(DAYS)
    assert seen == DAYS
    summary = builder.build(DAYS, run_id=RUN_ID, dry_run=True)
    assert summary["securities"] == 1
    assert summary["delisted"] == 0
    assert summary["listed_during_panel"] == 0


def test_중간에_사라지면_다음_거래일을_폐지일로_본다():
    spec = {DAYS[0]: [_row("005930", "삼성전자"), _row("117930", "한진해운")],
            DAYS[1]: [_row("005930", "삼성전자"), _row("117930", "한진해운")],
            DAYS[2]: [_row("005930", "삼성전자")],
            DAYS[3]: [_row("005930", "삼성전자")]}
    builder = SecurityMasterBuilder(_api(spec))
    summary = builder.build(DAYS, run_id=RUN_ID, dry_run=True)
    assert summary["delisted"] == 1
    runs, seen = builder.scan(DAYS)
    assert runs["117930"][-1]["last"] == DAYS[1]


def test_중간에_나타나면_그날이_상장일이다():
    spec = {DAYS[0]: [_row("005930", "삼성전자")],
            DAYS[1]: [_row("005930", "삼성전자")],
            DAYS[2]: [_row("005930", "삼성전자"), _row("0126Z0", "삼성에피스홀딩스")],
            DAYS[3]: [_row("005930", "삼성전자"), _row("0126Z0", "삼성에피스홀딩스")]}
    builder = SecurityMasterBuilder(_api(spec))
    summary = builder.build(DAYS, run_id=RUN_ID, dry_run=True)
    assert summary["listed_during_panel"] == 1
    assert summary["delisted"] == 0


def test_사명이_바뀌면_행이_나뉜다():
    spec = {DAYS[0]: [_row("000660", "하이닉스")],
            DAYS[1]: [_row("000660", "하이닉스")],
            DAYS[2]: [_row("000660", "SK하이닉스")],
            DAYS[3]: [_row("000660", "SK하이닉스")]}
    builder = SecurityMasterBuilder(_api(spec))
    summary = builder.build(DAYS, run_id=RUN_ID, dry_run=True)
    assert summary["securities"] == 1
    assert summary["rows"] == 2
    assert summary["renamed"] == 1


def test_스키마_계약을_지킨다():
    spec = {d: [_row("005930", "삼성전자"), _row("000660", "SK하이닉스")]
            for d in DAYS}
    builder = SecurityMasterBuilder(_api(spec))
    rows, summary = builder.make_rows(DAYS, run_id=RUN_ID)
    validate_rows(rows, SECURITY_MASTER_COLUMNS, SECURITY_MASTER_PK,
                  "silver.security_master")
    assert summary["panel_start"] == DAYS[0].isoformat()
    assert summary["panel_end"] == DAYS[-1].isoformat()
    assert {r["security_id"] for r in rows} == {"KRX:005930", "KRX:000660"}
    assert all(r["listed_from"] is None and r["delisted_on"] is None
               for r in rows)


def test_응답이_하나도_없으면_오류다():
    builder = SecurityMasterBuilder(FakeApi({}))
    with pytest.raises(NoInputDataError):
        builder.build(DAYS, run_id=RUN_ID, dry_run=True)

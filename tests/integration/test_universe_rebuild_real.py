# -*- coding: utf-8 -*-
"""실제 KRX 캐시로 복원 엔진을 돌린다 — 2026년 6월 정기변경 구간.

합성 데이터로는 "차액이 정확히 한 종목 시총과 같다"는 전제가 늘 성립하므로
아무것도 증명하지 못한다. 여기서는 실제 KRX Open API 응답(bronze 캐시)으로
두 가지를 확인한다.

1. 변경이 없는 날은 **오차 0원**이다 — 지수 MKTCAP이 구성종목 시총 합이라는
   전제 자체의 검증이다.
2. 2026-06-12 정기변경(공시: 4종목 선정 / 4종목 제외)을 공시 명단대로
   되돌리면 그 이전 거래일이 다시 0원으로 맞는다.

캐시가 없으면 건너뛴다. 네트워크를 타지 않는다.
"""

import json
import os
from datetime import date

import pytest

from cvd_obv.ingestion.krx_openapi import KrxOpenApi

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SNAPSHOT = os.path.join(ROOT, "data", "universe_snapshots", "KOSPI200_20260918.json")

#: 2026-05-22 공지 첨부 `붙임2606KOSPI200KOSDAQ150KRX300.xlsx`
JUNE_IN = ["000990", "267270", "456040", "483650"]
JUNE_OUT = ["004490", "005250", "006360", "114090"]
#: 2026-08-25 인적분할 신설법인 편입(역산으로 특정)
SPINOFF = "0220W0"

DAYS_AFTER = ("20260917", "20260916", "20260915")
DAYS_BEFORE = ("20260611", "20260610", "20260609", "20260605")


def _api():
    api = KrxOpenApi(key="CACHE-ONLY")
    for bas in DAYS_AFTER + DAYS_BEFORE:
        if not (api.cached("idx/kospi_dd_trd", bas)
                and api.cached("sto/stk_bydd_trd", bas)):
            pytest.skip(f"bronze 캐시에 {bas}가 없다 — `universe krx-cache` 먼저")
    return api


@pytest.fixture()
def members():
    if not os.path.exists(SNAPSHOT):
        pytest.skip("구성종목 스냅샷이 없다")
    with open(SNAPSHOT, encoding="utf-8") as fp:
        return [m.split(":", 1)[1] for m in json.load(fp)["members"]]


def test_오늘_명단은_최근_거래일들과_0원으로_맞는다(members):
    api = _api()
    for bas in DAYS_AFTER:
        got = api.checksum(members, bas)
        assert got["diff"] == 0, f"{bas} 차액 {got['diff']:,}"


def test_6월_정기변경을_되돌리면_그_이전도_0원이다(members):
    api = _api()
    before = (set(members) - {SPINOFF} - set(JUNE_IN)) | set(JUNE_OUT)
    assert len(before) == 200
    for bas in DAYS_BEFORE:
        got = api.checksum(sorted(before), bas)
        assert got["diff"] == 0, f"{bas} 차액 {got['diff']:,}"


def test_섹터지수_합은_지수_전체와_같다():
    """코스피200 섹터지수는 구성종목을 빈틈없이 나눈다 — 다건 변경을 좁힐 단서."""
    api = _api()
    for bas in DAYS_AFTER[:1] + DAYS_BEFORE[:1]:
        caps = api.index_caps(bas)
        sectors = api.sector_caps(bas)
        assert sectors, "섹터지수가 하나도 없다"
        assert sum(sectors.values()) == caps["코스피 200"]

# -*- coding: utf-8 -*-
"""코스닥은 코스피와 다르다 — 구독이 열렸다고 같은 방법을 쓸 수 없다.

2026-09-21에 `idx/kosdaq_dd_trd` 구독이 열렸다. 실측해 보니 세 가지가 달랐다.

1. 전체 가산 항등식은 성립한다(명단 검산 경로는 그대로 쓸 수 있다)
2. **섹터 분해는 성립하지 않는다**(코스피200은 0원, 코스닥150은 1.2% 부족)
3. 이력이 2015-07-13부터다(코스피200은 2010-01-04)

여기서 지키려는 것은 2번이다. 가산성이 없는 지수에서 섹터 검산을 하면
**조용히 틀린 값**이 나온다. 그래서 거부한다.
"""

import gzip
import json
from datetime import date

import pytest

from cvd_obv.domain.errors import ConfigError
from cvd_obv.ingestion.krx_openapi import (
    KOSDAQ150_START, MARKETS, KrxOpenApi, market_for,
)


def _cache(tmp_path, service, bas_dd, rows):
    path = tmp_path / service.replace("/", "_") / bas_dd[:4] / f"{bas_dd}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)


@pytest.fixture()
def api(tmp_path):
    """실측값의 구조를 그대로 옮긴 캐시 — 코스피는 맞고 코스닥은 어긋난다."""
    _cache(tmp_path, "idx/kospi_dd_trd", "20260918",
           [{"IDX_NM": "코스피 200", "MKTCAP": "1000"},
            {"IDX_NM": "코스피 200 금융", "MKTCAP": "400"},
            {"IDX_NM": "코스피 200 건설", "MKTCAP": "600"}])
    _cache(tmp_path, "sto/stk_bydd_trd", "20260918",
           [{"ISU_CD": "005930", "ISU_NM": "가", "MKTCAP": "700"},
            {"ISU_CD": "000660", "ISU_NM": "나", "MKTCAP": "300"}])
    _cache(tmp_path, "idx/kosdaq_dd_trd", "20260918",
           [{"IDX_NM": "코스닥 150", "MKTCAP": "1000"},
            {"IDX_NM": "코스닥 150 정보기술", "MKTCAP": "500"},
            {"IDX_NM": "코스닥 150 헬스케어", "MKTCAP": "380"}])
    _cache(tmp_path, "sto/ksq_bydd_trd", "20260918",
           [{"ISU_CD": "196170", "ISU_NM": "다", "MKTCAP": "600"},
            {"ISU_CD": "086520", "ISU_NM": "라", "MKTCAP": "400"}])
    return KrxOpenApi(cache_dir=str(tmp_path), key="TEST-KEY")


def test_두_지수를_모두_안다():
    assert sorted(MARKETS) == ["KOSDAQ150", "KOSPI200"]
    assert market_for("kosdaq150").index_service == "idx/kosdaq_dd_trd"
    assert market_for("KOSPI200").stock_service == "sto/stk_bydd_trd"


def test_Market을_그대로_넘겨도_된다():
    m = market_for("KOSPI200")
    assert market_for(m) is m


def test_모르는_지수는_거부한다():
    with pytest.raises(ConfigError):
        market_for("KOSPI500")


def test_코스닥150_시작일이_2015년이다():
    """코스피200 패널은 2010년부터지만 코스닥150은 2015-07-13부터다.

    재현 표본을 16년으로 계산하면 틀린다. 약 11.2년이다.
    """
    assert KOSDAQ150_START == date(2015, 7, 13)
    assert market_for("KOSDAQ150").start > market_for("KOSPI200").start


def test_시장별로_다른_서비스를_부른다(api):
    assert api.stock_caps("20260918", "KOSPI200") == {"005930": 700,
                                                      "000660": 300}
    assert api.stock_caps("20260918", "KOSDAQ150") == {"196170": 600,
                                                       "086520": 400}


def test_기본값은_코스피200이라_기존_호출이_그대로_돈다(api):
    """market 인자를 붙이기 전 코드가 깨지지 않아야 한다."""
    assert api.stock_caps("20260918") == api.stock_caps("20260918", "KOSPI200")
    assert api.index_caps("20260918")["코스피 200"] == 1000


def test_코스닥_명단도_같은_방법으로_검산한다(api):
    r = api.checksum(["196170", "086520"], "20260918", market="KOSDAQ150")
    assert r["market"] == "KOSDAQ150"
    assert r["index_name"] == "코스닥 150"
    assert r["diff"] == 0


def test_코스닥_섹터_검산은_거부한다(api):
    """가산성이 없는데 합을 내면 조용히 틀린 값이 나온다 — 멈추는 편이 낫다."""
    assert api.sector_caps("20260918", "KOSPI200") == {"코스피 200 금융": 400,
                                                       "코스피 200 건설": 600}
    with pytest.raises(ConfigError):
        api.sector_caps("20260918", "KOSDAQ150")


def test_가산성은_주장하지_않고_잰다(api):
    """`sector_gap`은 거부하지 않는다 — 정말 깨졌는지 확인하는 경로다."""
    assert api.sector_gap("20260918", "KOSPI200")["diff"] == 0
    gap = api.sector_gap("20260918", "KOSDAQ150")
    assert gap["diff"] == 120                 # 1000 - (500 + 380)
    assert gap["declared_additive"] is False


def test_오늘자_빈_응답은_캐시하지_않는다(tmp_path, monkeypatch):
    """빈 배열은 "비거래일"과 "아직 안 올라옴"을 구별하지 못한다.

    오늘 것을 캐시하면 "오늘은 비거래일"이 영구히 굳는다. 실제로
    2026-09-21(월) 두 서비스에 빈 캐시가 박혔다.
    """
    from datetime import date

    import cvd_obv.ingestion.krx_openapi as mod

    today = date.today().strftime("%Y%m%d")
    api = KrxOpenApi(cache_dir=str(tmp_path), key="TEST-KEY")
    monkeypatch.setattr(api, "_call", lambda service, bas_dd: [])

    assert api.fetch("idx/kospi_dd_trd", today) == []
    assert not api.cached("idx/kospi_dd_trd", today)   # 굳지 않았다

    # 과거의 빈 응답은 확정이므로 캐시한다 — 휴장일을 매번 다시 묻지 않는다
    assert api.fetch("idx/kospi_dd_trd", "20200101") == []
    assert api.cached("idx/kospi_dd_trd", "20200101")
    assert mod.date is date

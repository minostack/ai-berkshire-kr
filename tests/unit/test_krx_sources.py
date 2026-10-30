# -*- coding: utf-8 -*-
"""KRX 두 출처의 계약 — 캐시는 늙지 않고, 첨부는 조용히 비어 있지 않는다.

`KrxOpenApi`는 시세 수집기가 아니라 검산기다. 그래서 여기서 확인하는 것은
"응답을 잘 받느냐"가 아니라 **차액을 정확히 계산하느냐**, 그리고 **캐시가 있으면
네트워크를 건드리지 않느냐**다(과거 데이터는 확정값이므로).
"""

import gzip
import json
import os

import pytest

from cvd_obv.ingestion.krx_index_files import parse_attachment
from cvd_obv.ingestion.krx_openapi import KrxOpenApi

openpyxl = pytest.importorskip("openpyxl")


def _cache(tmp_path, service, bas_dd, rows):
    path = tmp_path / service.replace("/", "_") / bas_dd[:4] / f"{bas_dd}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)


@pytest.fixture()
def api(tmp_path):
    _cache(tmp_path, "idx/kospi_dd_trd", "20260917",
           [{"IDX_NM": "코스피 200", "MKTCAP": "3000"},
            {"IDX_NM": "코스피 200 TR", "MKTCAP": ""}])
    _cache(tmp_path, "sto/stk_bydd_trd", "20260917",
           [{"ISU_CD": "000001", "ISU_NM": "가", "MKTCAP": "1000"},
            {"ISU_CD": "000002", "ISU_NM": "나", "MKTCAP": "2000"},
            {"ISU_CD": "000003", "ISU_NM": "다", "MKTCAP": "5000"}])
    return KrxOpenApi(cache_dir=str(tmp_path), key="TEST-KEY")


def test_명단이_맞으면_차액이_0이다(api):
    got = api.checksum(["000001", "000002"], "20260917")
    assert got["diff"] == 0
    assert got["listed"] == 2
    assert api.calls == 0            # 캐시만 읽었다


def test_명단이_틀리면_차액이_그만큼_남는다(api):
    got = api.checksum(["000001"], "20260917")
    assert got["diff"] == 2000       # 000002가 빠져 있다
    assert got["not_listed"] == []


def test_상장되지_않은_종목은_따로_보고한다(api):
    got = api.checksum(["000001", "000002", "999999"], "20260917")
    assert got["not_listed"] == ["999999"]
    assert got["diff"] == 0


def test_값이_빈_지수는_버린다(api):
    assert "코스피 200 TR" not in api.index_caps("20260917")


def _write_xlsx(path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "KOSPI 200 편출입"
    ws.append(["KOSPI 200 지수 편입/편출 종목"])
    ws.append([])
    ws.append(["편입", "", "편출", ""])
    ws.append(["종목코드", "종목명", "종목코드", "종목명"])
    ws.append(["KR7000990002", "DB하이텍", "KR7004490009", "세방전지"])
    full = wb.create_sheet("KOSPI 200 구성종목")
    full.append(["KOSPI 200 지수 구성종목"])
    full.append([])
    full.append(["종목코드", "종목명"])
    for code, name in (("KR7005930003", "삼성전자"), ("KR7000660001", "SK하이닉스")):
        full.append([code, name])
    wb.save(path)


def test_첨부에서_편출입과_전체명단을_모두_읽는다(tmp_path):
    path = tmp_path / "정기변경.xlsx"
    _write_xlsx(path)
    got = parse_attachment(str(path))
    assert got["delta"]["KOSPI200"]["in"] == ["000990"]
    assert got["delta"]["KOSPI200"]["out"] == ["004490"]
    assert got["full"]["KOSPI200"] == ["005930", "000660"]


def test_읽지_못한_형식은_예외로_드러낸다(tmp_path):
    path = tmp_path / "공지.hwp"
    path.write_bytes(b"not a spreadsheet")
    with pytest.raises(ValueError):
        parse_attachment(str(path))

# -*- coding: utf-8 -*-
"""벤치마크 지수 수집 — 스케일과 업종코드 검산.

실측(2026-09-18)으로 확인한 사실을 코드가 지키는지 본다.
- `ka20006` 응답 값은 ×100 스케일이다(KOSPI 2024-04-02 = `275316` → 2,753.16).
- 업종코드는 문서 추측이 아니라 응답 값으로 특정했다(201 = 코스피200).
  공급자가 코드 체계를 바꾸면 조용히 **다른 지수**를 벤치마크로 쓰게 되므로,
  수집할 때마다 알려진 기준일 값과 대조한다.
"""

from datetime import date
from decimal import Decimal

import pytest

from cvd_obv.contracts.market_schema import (
    BENCHMARK_BAR_COLUMNS, BENCHMARK_BAR_PK,
)
from cvd_obv.contracts.schema import validate_rows
from cvd_obv.domain.errors import ExternalApiError
from cvd_obv.domain.status import QualityStatus
from cvd_obv.ingestion.benchmark_history import BenchmarkHistoryRepository
from cvd_obv.ingestion.kiwoom_benchmark import (
    BENCHMARKS, INDEX_SCALE, KiwoomBenchmarkSource,
)
from cvd_obv.storage.parquet_store import ParquetStore

RUN_ID = "test_20260918000000_aaaaaaaa"


def _index_row(dt, close, open_=None, high=None, low=None, volume=1000):
    """×100 스케일 원본 응답 형태."""
    close_raw = int(close * 100)
    return {"dt": dt,
            "cur_prc": str(close_raw),
            "open_pric": str(int((open_ or close) * 100)),
            "high_pric": str(int((high or close + 1) * 100)),
            "low_pric": str(int((low or close - 1) * 100)),
            "trde_qty": str(volume), "trde_prica": "123"}


class FakeScanner:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def _kiwoom(self, path, body, api_id, token, extra_headers=None):
        self.calls.append({"body": dict(body), "api_id": api_id})
        idx = len(self.calls) - 1
        if idx >= len(self.pages):
            return {"inds_dt_pole_qry": [], "_next_key": "", "_cont_yn": ""}
        return self.pages[idx]

    def _get_token(self):
        return "FAKE"


def _page(rows, next_key="", cont="N"):
    return {"inds_dt_pole_qry": rows, "_next_key": next_key, "_cont_yn": cont}


# ─────────────────────────────────────────────────────────────
# 스케일
# ─────────────────────────────────────────────────────────────

def test_지수값은_100으로_나눈다():
    """스케일을 놓치면 초과수익이 100배로 계산된다."""
    assert INDEX_SCALE == Decimal(100)
    flags = set()
    row = KiwoomBenchmarkSource._normalize(
        {"dt": "20240402", "cur_prc": "275316", "open_pric": "274415",
         "high_pric": "276033", "low_pric": "273719", "trde_qty": "1"}, flags)
    assert row["close"] == Decimal("2753.16")
    assert row["open"] == Decimal("2744.15")


def test_부호가_붙어도_절대값으로_읽는다():
    flags = set()
    row = KiwoomBenchmarkSource._normalize(
        {"dt": "20240402", "cur_prc": "275316", "open_pric": "274415",
         "high_pric": "276033", "low_pric": "-273719", "trde_qty": "1"}, flags)
    assert row["low"] == Decimal("2737.19")


def test_값이_비면_행을_버리고_표시한다():
    flags = set()
    assert KiwoomBenchmarkSource._normalize(
        {"dt": "20240402", "cur_prc": "", "open_pric": "1",
         "high_pric": "1", "low_pric": "1"}, flags) is None
    assert "INCOMPLETE_ROW" in flags


# ─────────────────────────────────────────────────────────────
# 업종코드 검산
# ─────────────────────────────────────────────────────────────

def test_기준일_값이_맞으면_경고가_없다():
    anchor_dt, anchor_value = BENCHMARKS["KRX:KOSPI200"]["anchor"]
    scanner = FakeScanner([_page([
        _index_row("20260918", 1090.02),
        _index_row(anchor_dt, float(anchor_value)),
    ], "", "N")])
    result = KiwoomBenchmarkSource(scanner=scanner).fetch_benchmark(
        "KRX:KOSPI200", token="T", end=date(2026, 9, 18))
    assert result.status == QualityStatus.SUCCESS
    assert "ANCHOR_MISMATCH" not in result.quality_flags


def test_업종코드가_다른_지수를_가리키면_경고한다():
    """공급자가 코드 체계를 바꿔도 조용히 다른 지수를 쓰지 않는다."""
    anchor_dt, _ = BENCHMARKS["KRX:KOSPI200"]["anchor"]
    scanner = FakeScanner([_page([
        _index_row(anchor_dt, 2753.16),      # 코스피200이 아니라 종합지수 값
    ], "", "N")])
    result = KiwoomBenchmarkSource(scanner=scanner).fetch_benchmark(
        "KRX:KOSPI200", token="T", end=date(2026, 9, 18))
    assert "ANCHOR_MISMATCH" in result.quality_flags


def test_기준일이_범위에_없으면_검산하지_못했다고_남긴다():
    scanner = FakeScanner([_page([_index_row("20260918", 1090.02)], "", "N")])
    result = KiwoomBenchmarkSource(scanner=scanner).fetch_benchmark(
        "KRX:KOSPI200", token="T", end=date(2026, 9, 18))
    assert "ANCHOR_DATE_ABSENT" in result.quality_flags


def test_요청에_실측한_업종코드를_쓴다():
    scanner = FakeScanner([_page([_index_row("20260918", 1090.02)], "", "N")])
    KiwoomBenchmarkSource(scanner=scanner).fetch_benchmark(
        "KRX:KOSPI200", token="T", end=date(2026, 9, 18))
    assert scanner.calls[0]["body"]["inds_cd"] == "201"
    assert scanner.calls[0]["api_id"] == "ka20006"


def test_모르는_벤치마크는_거부한다():
    with pytest.raises(ExternalApiError):
        KiwoomBenchmarkSource(scanner=FakeScanner([])).fetch_benchmark(
            "KRX:NASDAQ", token="T")


# ─────────────────────────────────────────────────────────────
# 저장
# ─────────────────────────────────────────────────────────────

def test_저장_행이_스키마를_통과한다(tmp_path):
    anchor_dt, anchor_value = BENCHMARKS["KRX:KOSPI200"]["anchor"]
    scanner = FakeScanner([_page([
        _index_row("20260916", 1080.0),
        _index_row(anchor_dt, float(anchor_value)),
    ], "", "N")])
    repo = BenchmarkHistoryRepository(
        store=ParquetStore(root=str(tmp_path)),
        source=KiwoomBenchmarkSource(scanner=scanner))
    summary = repo.build(["KRX:KOSPI200"], run_id=RUN_ID)

    assert summary["rows"] == 2
    rows = repo.load()
    validate_rows(rows, BENCHMARK_BAR_COLUMNS, BENCHMARK_BAR_PK,
                  "silver.benchmark_bars")
    assert rows[0]["benchmark_id"] == "KRX:KOSPI200"
    # 가격지수이므로 배당 재투자 종가는 없다 — 0으로 채우지 않는다.
    assert all(r["total_return_close"] is None for r in rows)


def test_실패한_지수는_저장하지_않는다(tmp_path):
    class Dead(FakeScanner):
        def _kiwoom(self, *a, **k):
            return None

    repo = BenchmarkHistoryRepository(
        store=ParquetStore(root=str(tmp_path)),
        source=KiwoomBenchmarkSource(scanner=Dead([])))
    summary = repo.build(["KRX:KOSPI200"], run_id=RUN_ID)
    assert summary["rows"] == 0
    assert summary["status"] == QualityStatus.PARTIAL
    assert repo.store.files_of("silver", "benchmark_bars") == []


def test_BarSeries로_넘길_수_있다(tmp_path):
    anchor_dt, anchor_value = BENCHMARKS["KRX:KOSPI200"]["anchor"]
    scanner = FakeScanner([_page([
        _index_row("20260916", 1080.0),
        _index_row(anchor_dt, float(anchor_value)),
    ], "", "N")])
    repo = BenchmarkHistoryRepository(
        store=ParquetStore(root=str(tmp_path)),
        source=KiwoomBenchmarkSource(scanner=scanner))
    repo.build(["KRX:KOSPI200"], run_id=RUN_ID)

    series = repo.series("KRX:KOSPI200")
    assert len(series) == 2
    assert series.get(date(2026, 9, 16))["close"] == Decimal("1080.00")

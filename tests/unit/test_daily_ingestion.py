# -*- coding: utf-8 -*-
"""일봉 수집 — 응답 파싱·단위·페이지네이션·저장.

네트워크를 쓰지 않는다. 키움 응답 형태를 고정한 가짜 API로 돌려서, 실측에서
확인된 사실들이 코드에 그대로 박혀 있는지 검증한다.

실측 근거(2026-09-16, 삼성전자·SK하이닉스·NAVER 24,244행):
- 응답 깊이는 1985-01-04까지다(로드맵이 전제한 약 11년이 아니다).
- 가격 필드에 등락 부호가 붙는다(저가 `-247500`).
- `trde_prica`는 백만원 단위이며, 1999년 이전 구간은 그 규칙이 깨진다.
- `upd_stkpc_tp=1`은 가격과 거래량을 함께 조정한다.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from cvd_obv.contracts.market_schema import (
    DAILY_BAR_COLUMNS, DAILY_BAR_PK, validate_ohlc, validate_series_order,
)
from cvd_obv.contracts.schema import validate_rows
from cvd_obv.domain.errors import DataQualityError
from cvd_obv.domain.status import DetailCode, QualityStatus
from cvd_obv.ingestion.daily_history import DailyHistoryRepository
from cvd_obv.ingestion.kiwoom_daily import (
    TRADED_VALUE_UNIT, KiwoomDailySource, parse_int, parse_price,
)
from cvd_obv.storage.parquet_store import ParquetStore


# ─────────────────────────────────────────────────────────────
# 가짜 키움
# ─────────────────────────────────────────────────────────────

def _api_row(dt, close=50000, volume=1000, traded_value=50, sign=False):
    low = f"-{close - 500}" if sign else str(close - 500)
    return {"dt": dt, "cur_prc": str(close), "open_pric": str(close - 200),
            "high_pric": str(close + 300), "low_pric": low,
            "trde_qty": str(volume), "trde_prica": str(traded_value)}


class FakeScanner:
    """`_kiwoom`만 흉내 내는 최소 객체."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def _kiwoom(self, path, body, api_id, token, extra_headers=None):
        self.calls.append({"body": dict(body), "headers": list(extra_headers or [])})
        idx = len(self.calls) - 1
        if idx >= len(self.pages):
            return {"stk_dt_pole_chart_qry": [], "_next_key": "", "_cont_yn": ""}
        return self.pages[idx]

    def _get_token(self):
        return "FAKE"


def _page(rows, next_key="", cont="N"):
    return {"stk_dt_pole_chart_qry": rows, "_next_key": next_key,
            "_cont_yn": cont}


# ─────────────────────────────────────────────────────────────
# 값 파싱
# ─────────────────────────────────────────────────────────────

def test_등락부호가_붙은_가격은_절대값으로_읽는다():
    """실측: 키움은 저가에도 `-`를 붙여 내려보낸다. 음수 가격이 아니다."""
    value, signed = parse_price("-247500")
    assert value == Decimal("247500")
    assert signed is True
    value, signed = parse_price("248,000")
    assert value == Decimal("248000")
    assert signed is False


def test_빈_값과_이상한_값은_None():
    assert parse_price("")[0] is None
    assert parse_price(None)[0] is None
    assert parse_price("abc")[0] is None
    assert parse_int("") is None
    assert parse_int("1,234") == 1234


def test_거래대금은_백만원_단위로_환산된다():
    flags = set()
    row = KiwoomDailySource._normalize(
        _api_row("20260916", close=253000, volume=11262711,
                 traded_value=2833172), flags)
    assert TRADED_VALUE_UNIT == Decimal(10) ** 6
    assert row["traded_value"] == Decimal(2833172) * TRADED_VALUE_UNIT
    # 거래량 × 가격과 자릿수가 맞으므로 플래그가 붙지 않는다.
    assert row["quality_flags"] == []


def test_거래대금이_어긋나면_고치지_않고_표시한다():
    """1999년 이전 구간처럼 규칙이 깨지는 값은 버리지도 고치지도 않는다."""
    flags = set()
    row = KiwoomDailySource._normalize(
        _api_row("19950916", close=100000, volume=1000, traded_value=1), flags)
    assert row is not None
    assert row["traded_value"] == Decimal(10) ** 6
    assert "TRADED_VALUE_INCONSISTENT" in row["quality_flags"]


def test_부호가_있으면_종목_단위_플래그가_남는다():
    flags = set()
    KiwoomDailySource._normalize(_api_row("20260916", sign=True), flags)
    assert "SIGNED_PRICE_FIELD" in flags


def test_거래정지일_거래량_0도_버리지_않는다():
    """삼성전자 2018-05-03(액면분할 매매정지)처럼 거래량 0인 실제 거래일이 있다."""
    flags = set()
    row = KiwoomDailySource._normalize(
        _api_row("20180503", close=2650000, volume=0, traded_value=0), flags)
    assert row is not None
    assert row["volume"] == 0


def test_가격이_0이하면_행을_버리고_표시한다():
    flags = set()
    assert KiwoomDailySource._normalize(
        {"dt": "20260916", "cur_prc": "0", "open_pric": "0",
         "high_pric": "0", "low_pric": "0", "trde_qty": "10"}, flags) is None
    assert "NON_POSITIVE_PRICE" in flags


# ─────────────────────────────────────────────────────────────
# 페이지네이션
# ─────────────────────────────────────────────────────────────

def test_cont_yn이_이어지는_동안_페이지를_넘긴다():
    scanner = FakeScanner([
        _page([_api_row("20260916"), _api_row("20260915")], "k1", "Y"),
        _page([_api_row("20260914"), _api_row("20260911")], "k2", "Y"),
        _page([_api_row("20260910")], "", "N"),
    ])
    result = KiwoomDailySource(scanner=scanner).fetch_security(
        "005930", token="T", end=date(2026, 9, 16))
    assert result.pages == 3
    assert len(result.rows) == 5
    assert result.status == QualityStatus.SUCCESS
    assert result.pagination_complete is True
    assert result.stop_reason == "NO_MORE_PAGES"
    # 결과는 날짜 오름차순이다.
    assert [r["dt"] for r in result.rows] == [
        "20260910", "20260911", "20260914", "20260915", "20260916"]


def test_다음_페이지_요청에_커서를_되돌려준다():
    scanner = FakeScanner([
        _page([_api_row("20260916")], "cursor-1", "Y"),
        _page([_api_row("20260915")], "", "N"),
    ])
    KiwoomDailySource(scanner=scanner).fetch_security(
        "005930", token="T", end=date(2026, 9, 16))
    assert scanner.calls[0]["headers"] == []
    assert "next-key: cursor-1" in scanner.calls[1]["headers"]
    assert "cont-yn: Y" in scanner.calls[1]["headers"]


def test_페이지_상한에_걸리면_성공으로_보고하지_않는다():
    """중단을 성공으로 처리하면 짧은 시계열을 완전한 것으로 믿게 된다."""
    scanner = FakeScanner([_page([_api_row("2026091%d" % i)], f"k{i}", "Y")
                           for i in range(9)])
    result = KiwoomDailySource(scanner=scanner).fetch_security(
        "005930", token="T", end=date(2026, 9, 16), max_pages=2)
    assert result.pages == 2
    assert result.status == QualityStatus.PARTIAL
    assert result.error_code == DetailCode.API_PARTIAL_PAGE
    assert result.pagination_complete is False


def test_같은_커서가_반복되면_멈춘다():
    scanner = FakeScanner([
        _page([_api_row("20260916")], "same", "Y"),
        _page([_api_row("20260915")], "same", "Y"),
    ])
    result = KiwoomDailySource(scanner=scanner).fetch_security(
        "005930", token="T", end=date(2026, 9, 16), max_pages=10)
    assert result.stop_reason == "CURSOR_REPEAT"
    assert result.status == QualityStatus.PARTIAL
    assert "PAGINATION_CURSOR_REPEAT" in result.quality_flags


def test_start에_도달하면_멈춘다():
    scanner = FakeScanner([
        _page([_api_row("20260916"), _api_row("20260915")], "k1", "Y"),
        _page([_api_row("20260914"), _api_row("20260901")], "k2", "Y"),
    ])
    result = KiwoomDailySource(scanner=scanner).fetch_security(
        "005930", token="T", start=date(2026, 9, 14), end=date(2026, 9, 16))
    assert result.stop_reason == "REACHED_START"
    assert [r["dt"] for r in result.rows] == [
        "20260914", "20260915", "20260916"]


def test_응답이_없으면_실패로_보고한다():
    class Dead(FakeScanner):
        def _kiwoom(self, *a, **k):
            return None

    result = KiwoomDailySource(scanner=Dead([])).fetch_security(
        "005930", token="T", end=date(2026, 9, 16))
    assert result.status == QualityStatus.FAILED
    assert result.pagination_complete is False


def test_중복_날짜는_제거된다():
    scanner = FakeScanner([
        _page([_api_row("20260916"), _api_row("20260916")], "", "N"),
    ])
    result = KiwoomDailySource(scanner=scanner).fetch_security(
        "005930", token="T", end=date(2026, 9, 16))
    assert len(result.rows) == 1
    assert result.rows_received == 2


# ─────────────────────────────────────────────────────────────
# 저장 계층
# ─────────────────────────────────────────────────────────────

@pytest.fixture
def repo(tmp_path):
    scanner = FakeScanner([
        _page([_api_row("20260916"), _api_row("20260915"),
               _api_row("20260911")], "", "N"),
    ])
    source = KiwoomDailySource(scanner=scanner)
    return DailyHistoryRepository(store=ParquetStore(root=str(tmp_path)),
                                  source=source)


def test_저장_행이_스키마를_통과한다(repo):
    summary = repo.build(["005930"], run_id="test_20260916000000_aaaaaaaa")
    rows = repo.load()
    assert len(rows) == 3
    validate_rows(rows, DAILY_BAR_COLUMNS, DAILY_BAR_PK, "silver.daily_bars")
    validate_ohlc(rows)
    validate_series_order(rows)
    assert summary.status == QualityStatus.SUCCESS


def test_price_scope가_애프터마켓_시행일을_경계로_갈린다(repo):
    repo.build(["005930"], run_id="test_20260916000000_aaaaaaaa")
    scopes = {r["trading_date"]: r["price_scope"] for r in repo.load()}
    assert scopes[date(2026, 9, 11)] == "REGULAR"
    assert scopes[date(2026, 9, 14 + 2)] == "EXTENDED"


def test_available_at이_일봉_확정시각이다(repo):
    """15:35 신호가 그날 일봉을 쓸 수 있는지는 이 값이 결정한다."""
    repo.build(["005930"], run_id="test_20260916000000_aaaaaaaa")
    by_date = {r["trading_date"]: r for r in repo.load()}
    # 애프터마켓 시행 전: 15:30 마감 + 여유 5분 = 06:35 UTC
    assert by_date[date(2026, 9, 11)]["available_at_utc"] == \
        datetime(2026, 9, 11, 6, 35, tzinfo=timezone.utc)
    # 시행 후: 20:00 마감 + 여유 5분 = 11:05 UTC
    assert by_date[date(2026, 9, 16)]["available_at_utc"] == \
        datetime(2026, 9, 16, 11, 5, tzinfo=timezone.utc)


def test_수정주가_여부가_행에_기록된다(tmp_path):
    scanner = FakeScanner([_page([_api_row("20260916")], "", "N")])
    repo = DailyHistoryRepository(
        store=ParquetStore(root=str(tmp_path)),
        source=KiwoomDailySource(scanner=scanner, adjusted=False))
    repo.build(["005930"], run_id="test_20260916000000_bbbbbbbb")
    row = repo.load()[0]
    assert row["adjustment_type"] == "RAW"
    assert row["adjustment_as_of_utc"] is not None
    assert scanner.calls[0]["body"]["upd_stkpc_tp"] == "0"


def test_dry_run은_파일을_쓰지_않는다(repo):
    summary = repo.build(["005930"], run_id="test_20260916000000_cccccccc",
                         dry_run=True)
    assert summary.rows == 3
    assert summary.write_result is None
    assert repo.store.files_of("silver", "daily_bars") == []


def test_수집_기록이_남는다(repo):
    repo.build(["005930"], run_id="test_20260916000000_dddddddd")
    runs = repo.store.read("bronze", "ingest_runs",
                           pk=("run_id", "dataset", "security_id"))
    assert len(runs) == 1
    assert runs[0]["security_id"] == "KRX:005930"
    assert runs[0]["pagination_complete"] is True
    assert runs[0]["rows_written"] == 3
    # 요청 기록에 토큰·appkey가 들어가면 안 된다.
    assert "token" not in runs[0]["request_params_json"].lower()


def test_OHLC_위반_행은_버리지_않고_격리한다(tmp_path):
    """실측: 018880 2001-12-18은 저가가 고가보다 높다(공급자 데이터 오류).

    스키마 §12는 이를 hard fail이 아니라 row quarantine으로 규정한다. 한 행
    때문에 같은 실행의 나머지 종목까지 버리면 안 되고, 조용히 지워도 안 된다.
    """
    good = _api_row("20260915")
    bad = {"dt": "20260916", "cur_prc": "100", "open_pric": "100",
           "high_pric": "50", "low_pric": "90", "trde_qty": "10",
           "trde_prica": "1"}
    scanner = FakeScanner([_page([good, bad], "", "N")])
    repo = DailyHistoryRepository(store=ParquetStore(root=str(tmp_path)),
                                  source=KiwoomDailySource(scanner=scanner))
    repo.build(["005930"], run_id="test_20260916000000_eeeeeeee")

    rows = {r["trading_date"]: r for r in repo.load()}
    assert len(rows) == 2, "정상 행까지 사라지면 안 된다"
    flagged = rows[date(2026, 9, 16)]
    assert flagged["quality_status"] == QualityStatus.FAILED
    assert "OHLC_INVARIANT_VIOLATION" in flagged["quality_flags"]
    # 같은 실행의 정상 행은 영향을 받지 않는다.
    assert rows[date(2026, 9, 15)]["quality_status"] == QualityStatus.SUCCESS


def test_격리_표시가_없는_위반은_여전히_막는다():
    """표시를 빠뜨린 코드 결함은 조용히 통과시키지 않는다."""
    from cvd_obv.contracts.market_schema import validate_quarantined_ohlc

    row = {"security_id": "KRX:005930", "trading_date": date(2026, 9, 16),
           "open": Decimal("100"), "high": Decimal("50"),
           "low": Decimal("90"), "close": Decimal("100"), "volume": 10,
           "quality_status": "SUCCESS", "quality_flags": []}
    with pytest.raises(DataQualityError):
        validate_quarantined_ohlc([row])

    row["quality_status"] = "FAILED"
    row["quality_flags"] = ["OHLC_INVARIANT_VIOLATION"]
    assert validate_quarantined_ohlc([row]) == 1


def test_뒤늦게_상장된_종목은_결함이_아니다(tmp_path):
    """상장이 늦어 시계열이 짧은 것과 구간 안이 빈 것은 전혀 다른 문제다."""
    full = [_api_row("202609%02d" % d) for d in (1, 2, 3, 4, 7, 8, 9, 10)]
    late = [_api_row("202609%02d" % d) for d in (7, 8, 9, 10)]
    scanner = FakeScanner([_page(full, "", "N"), _page(late, "", "N")])
    repo = DailyHistoryRepository(store=ParquetStore(root=str(tmp_path)),
                                  source=KiwoomDailySource(scanner=scanner))
    repo.build(["005930", "000660"], run_id="test_20260916000000_ffffffff")
    report = repo.validate()
    assert report["securities"] == 2
    assert "KRX:000660" in report["late_listed"]
    assert report["coverage_gaps"] == {}
    assert report["status"] == QualityStatus.SUCCESS


def test_구간_안의_결측은_집어낸다(tmp_path):
    full = [_api_row("202609%02d" % d) for d in (1, 2, 3, 4, 7, 8, 9, 10)]
    holed = [_api_row("202609%02d" % d) for d in (1, 2, 10)]   # 가운데가 빔
    scanner = FakeScanner([_page(full, "", "N"), _page(holed, "", "N")])
    repo = DailyHistoryRepository(store=ParquetStore(root=str(tmp_path)),
                                  source=KiwoomDailySource(scanner=scanner))
    repo.build(["005930", "000660"], run_id="test_20260916000000_11111111")
    report = repo.validate()
    assert "KRX:000660" in report["coverage_gaps"]
    assert report["coverage_gaps"]["KRX:000660"]["coverage"] < 0.98
    assert report["status"] == QualityStatus.PARTIAL


# ─────────────────────────────────────────────────────────────
# 재개·복구
# ─────────────────────────────────────────────────────────────

def test_저장된_종목을_알려준다(repo):
    assert repo.existing_securities() == set()
    repo.build(["005930"], run_id="test_20260916000000_11110000")
    assert repo.existing_securities() == {"KRX:005930"}


def test_불완전하게_수집된_종목을_찾아낸다(tmp_path):
    """잘린 시계열을 그대로 두면 '그때 상장폐지된 종목'처럼 보인다."""
    class Flaky(FakeScanner):
        """첫 종목은 정상, 둘째 종목은 2페이지째에서 응답이 끊긴다."""

        def _kiwoom(self, path, body, api_id, token, extra_headers=None):
            self.calls.append({"body": dict(body),
                               "headers": list(extra_headers or [])})
            if body["stk_cd"] == "005930":
                return _page([_api_row("20260916")], "", "N")
            if not extra_headers:
                return _page([_api_row("20260916")], "k1", "Y")
            return None

    repo = DailyHistoryRepository(
        store=ParquetStore(root=str(tmp_path)),
        source=KiwoomDailySource(scanner=Flaky([])))
    repo.build(["005930", "000660"], run_id="test_20260916000000_22220000",
               retries=0)

    failed = repo.failed_securities()
    assert set(failed) == {"KRX:000660"}
    # 실패한 종목의 잘린 행은 저장하지 않는다.
    assert repo.existing_securities() == {"KRX:005930"}


def test_재시도로_회복되면_실패로_남지_않는다(tmp_path):
    class FlakyOnce(FakeScanner):
        def __init__(self):
            super().__init__([])
            self.attempt = 0

        def _kiwoom(self, path, body, api_id, token, extra_headers=None):
            self.calls.append({"body": dict(body), "headers": []})
            self.attempt += 1
            if self.attempt == 1:
                return None          # 첫 시도만 실패
            return _page([_api_row("20260916")], "", "N")

    repo = DailyHistoryRepository(
        store=ParquetStore(root=str(tmp_path)),
        source=KiwoomDailySource(scanner=FlakyOnce()))
    summary = repo.build(["005930"], run_id="test_20260916000000_33330000",
                         retries=2)
    assert summary.status == QualityStatus.SUCCESS
    assert summary.per_security[0]["attempts"] == 2
    assert repo.failed_securities() == {}


def test_페이지_경계에서_잘린_종목을_의심_대상으로_뽑는다(tmp_path):
    """실측: 잘린 종목의 행 수는 3,600/6,000/5,400처럼 모두 600의 배수였다.

    로그가 아니라 저장된 데이터만 보고도 재수집 후보를 뽑을 수 있어야 한다.
    수집이 crash로 끝나면 수집 기록 자체가 남지 않기 때문이다.
    """
    dates = ["202601%02d" % d for d in range(1, 13)]
    scanner = FakeScanner([
        _page([_api_row(d) for d in dates], "", "N"),            # 12행
        _page([_api_row(d) for d in dates[-4:]], "", "N"),       # 4행 = 4의 배수
    ])
    repo = DailyHistoryRepository(store=ParquetStore(root=str(tmp_path)),
                                  source=KiwoomDailySource(scanner=scanner))
    repo.build(["005930", "000660"], run_id="test_20260916000000_44440000")

    suspects = repo.suspect_truncated_securities(page_size=4)
    assert "KRX:000660" in suspects
    assert suspects["KRX:000660"]["rows"] == 4
    # 12행도 4의 배수라 함께 잡힌다 — 과선별은 허용한다(재수집이 저렴하다).
    assert "KRX:005930" in suspects
    # 배수가 아니면 후보에서 빠진다.
    assert repo.suspect_truncated_securities(page_size=5) == {}


def test_마지막_거래일이_이른_종목은_의심하지_않는다(tmp_path):
    """상장폐지·거래정지로 짧은 것과 페이지 절단은 다르다."""
    scanner = FakeScanner([
        _page([_api_row("202601%02d" % d) for d in range(1, 13)], "", "N"),
        _page([_api_row("202601%02d" % d) for d in range(1, 5)], "", "N"),
    ])
    repo = DailyHistoryRepository(store=ParquetStore(root=str(tmp_path)),
                                  source=KiwoomDailySource(scanner=scanner))
    repo.build(["005930", "000660"], run_id="test_20260916000000_55550000")
    # 000660은 4행이지만 마지막 날짜가 패널보다 이르다 → 의심 대상 아님
    suspects = repo.suspect_truncated_securities(page_size=4)
    assert "KRX:000660" not in suspects


def test_재수집분이_이전_잘린_행을_덮는다(tmp_path):
    """같은 PK가 여러 run에 있으면 나중 run의 행만 읽혀야 한다.

    Arrow 읽기에 중복 제거가 없으면 잘린 행과 재수집분이 함께 집계된다.
    """
    store = ParquetStore(root=str(tmp_path))
    short = FakeScanner([_page([_api_row("20260916")], "", "N")])
    repo = DailyHistoryRepository(store=store,
                                  source=KiwoomDailySource(scanner=short))
    repo.build(["005930"], run_id="daily_20260101000000_aaaaaaaa")

    full = FakeScanner([_page([_api_row("20260915"), _api_row("20260916")],
                              "", "N")])
    repo.source = KiwoomDailySource(scanner=full)
    repo.build(["005930"], run_id="daily_20260202000000_bbbbbbbb")

    rows = repo.load()
    assert len(rows) == 2, "중복 제거가 되지 않으면 3행이 된다"
    assert {r["run_id"] for r in rows} == {"daily_20260202000000_bbbbbbbb"}

    # Arrow 경로도 같은 규칙을 따른다.
    table = store.read_arrow("silver", "daily_bars",
                             columns=["security_id", "trading_date"],
                             dedupe_pk=DAILY_BAR_PK)
    assert table.num_rows == 2


def test_한_번만_쓴_테이블은_중복_제거를_건너뛴다(tmp_path):
    """수백만 행을 헛되이 훑지 않는지 — 결과는 같아야 한다."""
    store = ParquetStore(root=str(tmp_path))
    scanner = FakeScanner([_page([_api_row("20260915"), _api_row("20260916")],
                                 "", "N")])
    repo = DailyHistoryRepository(store=store,
                                  source=KiwoomDailySource(scanner=scanner))
    repo.build(["005930"], run_id="daily_20260101000000_aaaaaaaa")
    table = store.read_arrow("silver", "daily_bars",
                             columns=["security_id", "trading_date"],
                             dedupe_pk=DAILY_BAR_PK)
    assert table.num_rows == 2

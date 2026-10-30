# -*- coding: utf-8 -*-
"""Silver 시장 데이터 열 계약 — 스키마 §4~§5.

`schema.py`의 `Column`/`validate_rows`를 그대로 쓰고, 여기서는 테이블 정의와
시장 데이터 고유의 불변식(OHLC 관계, 음수 거래량, 정렬·중복)을 다룬다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

from ..domain.errors import DataQualityError
from ..domain.status import DetailCode, QualityStatus
from .schema import Column

__all__ = [
    "DAILY_BAR_COLUMNS", "DAILY_BAR_PK", "DAILY_BAR_LOGICAL_KEY",
    "MINUTE_BAR_COLUMNS", "MINUTE_BAR_PK", "MINUTE_BAR_LOGICAL_KEY",
    "BAR_COVERAGE_COLUMNS", "BAR_COVERAGE_PK",
    "BENCHMARK_BAR_COLUMNS", "BENCHMARK_BAR_PK",
    "MARKET_CALENDAR_COLUMNS", "MARKET_CALENDAR_PK",
    "INGEST_RUN_COLUMNS", "INGEST_RUN_PK",
    "PRICE_SCOPES", "ADJUSTMENT_TYPES", "OHLC_VIOLATION_FLAG",
    "ohlc_violations_of", "validate_ohlc", "validate_quarantined_ohlc",
    "validate_series_order",
]

#: OHLC 불변식을 어긴 행에 붙이는 품질 플래그.
OHLC_VIOLATION_FLAG = "OHLC_INVARIANT_VIOLATION"

PRICE_SCOPES = ("REGULAR", "EXTENDED", "VENDOR_DAILY")
ADJUSTMENT_TYPES = ("RAW", "SPLIT_ADJUSTED", "TOTAL_RETURN")

_PRICE = "decimal"

# ─────────────────────────────────────────────────────────────
# silver.daily_bars (스키마 §5)
# ─────────────────────────────────────────────────────────────

DAILY_BAR_PK = ("security_id", "trading_date", "price_scope",
                "adjustment_type", "data_revision")
#: 조회용 논리 키 — 같은 봉의 리비전들 중 최신 하나를 고를 때 쓴다
#: (`ParquetStore.read_arrow(latest_by=...)`).
DAILY_BAR_LOGICAL_KEY = DAILY_BAR_PK[:-1]

DAILY_BAR_COLUMNS = [
    Column("security_id", "string", nullable=False),
    Column("trading_date", "date", nullable=False),
    Column("open", _PRICE, nullable=False),
    Column("high", _PRICE, nullable=False),
    Column("low", _PRICE, nullable=False),
    Column("close", _PRICE, nullable=False),
    Column("volume", "int", nullable=False),
    Column("traded_value", "decimal", precision=24, scale=0,
           note="공급자 미제공 시 null — 계산으로 채우지 않는다"),
    Column("price_scope", "string", nullable=False, enum=PRICE_SCOPES),
    Column("adjustment_type", "string", nullable=False, enum=ADJUSTMENT_TYPES),
    Column("adjustment_factor", "float"),
    Column("adjustment_as_of_utc", "timestamp",
           note="조정계수가 반영된 시점 = 수집 시각. 소급 재작성 위험의 근거."),
    Column("available_at_utc", "timestamp", nullable=False),
    Column("source", "string", nullable=False),
    Column("quality_status", "string", nullable=False,
           enum=("SUCCESS", "PARTIAL", "FAILED")),
    Column("quality_flags", "list<string>", nullable=False),
    Column("run_id", "string", nullable=False),
    Column("market_rule_version", "string", nullable=False),
    Column("schema_version", "string", nullable=False),
    Column("data_revision", "int", nullable=False),
]

# ─────────────────────────────────────────────────────────────
# silver.daily_trade_sides — 일별 매수/매도 체결량 (KIS FHKST03010800)
# ─────────────────────────────────────────────────────────────
#
# 분봉 캔들에서 **추정**한 방향(CVD)과 달리, 공급자가 매수·매도로 갈라 준
# 체결량이다. 그래서 컬럼 이름에 est/direction이 없다.
#
# 주의 — 합계는 그날 정규장 거래량보다 **작다**(실측 중앙 0.92~0.96).
# 동시호가 체결처럼 공격자 측이 없는 체결이 빠지는 것으로 보인다(추정,
# `reports/KIS-체결량-일별CVD-대비검토-20261019.md` §6). 그러므로
#   · `buy_qty + sell_qty`를 거래량으로 쓰지 않는다
#   · 흐름은 반드시 (buy − sell)/(buy + sell)로 정규화해 쓴다

DAILY_TRADE_SIDE_PK = ("security_id", "trading_date", "market_div",
                       "data_revision")
DAILY_TRADE_SIDE_LOGICAL_KEY = DAILY_TRADE_SIDE_PK[:-1]

#: 시장 구분 — KIS FID_COND_MRKT_DIV_CODE
TRADE_SIDE_MARKET_DIVS = ("J", "NX", "UN")

DAILY_TRADE_SIDE_COLUMNS = [
    Column("security_id", "string", nullable=False),
    Column("trading_date", "date", nullable=False),
    Column("buy_qty", "int", nullable=False,
           note="매수 체결량(total_shnu_qty). 공급자 제공값 — 계산하지 않는다"),
    Column("sell_qty", "int", nullable=False,
           note="매도 체결량(total_seln_qty)"),
    Column("market_div", "string", nullable=False,
           enum=TRADE_SIDE_MARKET_DIVS,
           note="J:KRX, NX:NXT, UN:통합. 섞어 읽지 않는다"),
    Column("available_at_utc", "timestamp", nullable=False),
    Column("source", "string", nullable=False),
    Column("quality_status", "string", nullable=False,
           enum=("SUCCESS", "PARTIAL", "FAILED")),
    Column("quality_flags", "list<string>", nullable=False),
    Column("run_id", "string", nullable=False),
    Column("schema_version", "string", nullable=False),
    Column("data_revision", "int", nullable=False),
]

#: 두 수가 모두 0인 날 — 공급자 이력 시작 전(005930은 2010-05-24부터 0이 아니다)
TRADE_SIDE_ZERO_FLAG = "TRADE_SIDE_BOTH_ZERO"
#: 그날 기준 일봉이 없어 거래량 대조를 못 한 행
TRADE_SIDE_NO_DAILY_FLAG = "TRADE_SIDE_NO_DAILY_BAR"


# ─────────────────────────────────────────────────────────────
# silver.minute_bars (스키마 §5)
# ─────────────────────────────────────────────────────────────

MINUTE_BAR_PK = ("security_id", "period_min", "bar_start_at_utc", "session",
                 "data_revision")
MINUTE_BAR_LOGICAL_KEY = MINUTE_BAR_PK[:-1]

MINUTE_BAR_COLUMNS = [
    Column("security_id", "string", nullable=False),
    Column("period_min", "int", nullable=False),
    Column("bar_start_at_utc", "timestamp", nullable=False),
    Column("bar_end_at_utc", "timestamp", nullable=False,
           note="반개구간 [start, end). 이 값이 as_of 이하일 때만 쓸 수 있다"),
    Column("trading_date", "date", nullable=False),
    Column("session", "string", nullable=False,
           enum=("REGULAR", "AFTER", "OTHER")),
    Column("open", _PRICE, nullable=False),
    Column("high", _PRICE, nullable=False),
    Column("low", _PRICE, nullable=False),
    Column("close", _PRICE, nullable=False),
    Column("volume", "int", nullable=False),
    Column("traded_value", "decimal", precision=24, scale=0),
    Column("cum_traded_value", "decimal", precision=24, scale=0,
           note="그날 그 시장의 누적 거래대금(원) — KIS `acml_tr_pbmn`. 키움 봉·"
                "2026-10-23 전 파일에는 없다(null)"),
    Column("source_timestamp_raw", "string", nullable=False,
           note="원본 시각 문자열(키움 cntr_tm, KIS 날짜+stck_cntg_hour)"),
    Column("timestamp_semantics", "string", nullable=False,
           enum=("BAR_START", "BAR_END"),
           note="2026-09-16 실측: 키움 cntr_tm은 봉의 시작 시각"),
    Column("available_at_utc", "timestamp", nullable=False),
    Column("source", "string", nullable=False),
    Column("quality_status", "string", nullable=False,
           enum=("SUCCESS", "PARTIAL", "FAILED")),
    Column("quality_flags", "list<string>", nullable=False),
    Column("run_id", "string", nullable=False),
    Column("market_rule_version", "string", nullable=False),
    Column("schema_version", "string", nullable=False),
    Column("data_revision", "int", nullable=False),
]


# ─────────────────────────────────────────────────────────────
# silver.bar_coverage (스키마 §5)
# ─────────────────────────────────────────────────────────────

BAR_COVERAGE_PK = ("dataset", "security_id", "period_min", "trading_date",
                   "session", "data_revision")

BAR_COVERAGE_COLUMNS = [
    Column("dataset", "string", nullable=False),
    Column("security_id", "string", nullable=False),
    Column("period_min", "int", nullable=False),
    Column("trading_date", "date", nullable=False),
    Column("session", "string", nullable=False,
           enum=("REGULAR", "AFTER", "OTHER")),
    Column("expected_bars", "int", nullable=False,
           note="세션 제도상 그날 나올 수 있는 봉 수"),
    Column("actual_bars", "int", nullable=False),
    Column("completeness", "float", nullable=False),
    Column("first_bar_at_utc", "timestamp"),
    Column("last_bar_at_utc", "timestamp"),
    Column("pagination_complete", "bool", nullable=False),
    Column("status", "string", nullable=False,
           enum=("SUCCESS", "PARTIAL", "FAILED")),
    Column("reason_codes", "list<string>", nullable=False),
    Column("run_id", "string", nullable=False),
    Column("market_rule_version", "string", nullable=False),
    Column("schema_version", "string", nullable=False),
    Column("data_revision", "int", nullable=False),
]


# ─────────────────────────────────────────────────────────────
# silver.benchmark_bars (스키마 §5)
# ─────────────────────────────────────────────────────────────

BENCHMARK_BAR_PK = ("benchmark_id", "trading_date", "price_scope",
                    "data_revision")

BENCHMARK_BAR_COLUMNS = [
    Column("benchmark_id", "string", nullable=False),
    Column("trading_date", "date", nullable=False),
    Column("open", _PRICE, nullable=False),
    Column("high", _PRICE, nullable=False),
    Column("low", _PRICE, nullable=False),
    Column("close", _PRICE, nullable=False),
    Column("volume", "int"),
    Column("total_return_close", _PRICE,
           note="배당 재투자 지수 — 제공되지 않으면 null"),
    Column("price_scope", "string", nullable=False, enum=PRICE_SCOPES),
    Column("available_at_utc", "timestamp", nullable=False),
    Column("source", "string", nullable=False),
    Column("quality_status", "string", nullable=False,
           enum=("SUCCESS", "PARTIAL", "FAILED")),
    Column("run_id", "string", nullable=False),
    Column("market_rule_version", "string", nullable=False),
    Column("schema_version", "string", nullable=False),
    Column("data_revision", "int", nullable=False),
]

# ─────────────────────────────────────────────────────────────
# silver.market_calendar (스키마 §5)
# ─────────────────────────────────────────────────────────────

MARKET_CALENDAR_PK = ("market", "trading_date", "market_rule_version")

MARKET_CALENDAR_COLUMNS = [
    Column("market", "string", nullable=False),
    Column("trading_date", "date", nullable=False),
    Column("market_rule_version", "string", nullable=False),
    Column("is_trading_day", "bool", nullable=False),
    Column("regular_open_utc", "timestamp"),
    Column("regular_close_utc", "timestamp"),
    Column("after_open_utc", "timestamp"),
    Column("after_close_utc", "timestamp"),
    Column("next_trading_date", "date"),
    Column("previous_trading_date", "date"),
    Column("special_session", "bool", nullable=False),
    Column("daily_price_scope", "string", nullable=False, enum=PRICE_SCOPES),
    Column("source", "string", nullable=False),
    Column("derived_from_securities", "int", nullable=False),
]

# ─────────────────────────────────────────────────────────────
# bronze.ingest_runs (스키마 §4)
# ─────────────────────────────────────────────────────────────

INGEST_RUN_PK = ("run_id", "dataset", "security_id")

INGEST_RUN_COLUMNS = [
    Column("run_id", "string", nullable=False),
    Column("source", "string", nullable=False),
    Column("dataset", "string", nullable=False),
    Column("security_id", "string", nullable=False,
           note="종목 단위 수집 기록. 전체 실행 단위 행은 security_id='*'"),
    Column("started_at_utc", "timestamp", nullable=False),
    Column("finished_at_utc", "timestamp"),
    Column("request_params_json", "string", nullable=False,
           note="비밀정보 제거 후 canonical JSON"),
    Column("status", "string", nullable=False,
           enum=QualityStatus.values()),
    Column("rows_received", "int", nullable=False),
    Column("rows_written", "int", nullable=False),
    Column("page_count", "int", nullable=False),
    Column("retry_count", "int", nullable=False),
    Column("pagination_complete", "bool", nullable=False),
    Column("first_trading_date", "date"),
    Column("last_trading_date", "date"),
    Column("error_code", "string"),
    Column("error_message", "string"),
    Column("payload_hash", "string"),
    Column("schema_version", "string", nullable=False),
]


# ─────────────────────────────────────────────────────────────
# 시장 데이터 불변식
# ─────────────────────────────────────────────────────────────

def _d(value):
    return value if isinstance(value, Decimal) else Decimal(str(value))


def ohlc_violations_of(row) -> list:
    """한 행의 OHLC 불변식 위반 사유 목록. 위반이 없으면 빈 목록."""
    o, h, l, c = (_d(row["open"]), _d(row["high"]),
                  _d(row["low"]), _d(row["close"]))
    reasons = []
    if min(o, h, l, c) <= 0:
        reasons.append("0 이하 가격")
    if h < l:
        reasons.append(f"high({h}) < low({l})")
    if l > min(o, c):
        reasons.append(f"low({l}) > min(open,close)")
    if h < max(o, c):
        reasons.append(f"high({h}) < max(open,close)")
    vol = row.get("volume")
    if vol is not None and vol < 0:
        reasons.append(f"음수 거래량({vol})")
    return reasons


def validate_ohlc(rows, table: str = "silver.daily_bars", strict: bool = True):
    """OHLC 관계와 음수 값을 검사한다(스키마 §12).

    `strict=True`면 위반 시 예외, `False`면 위반 목록을 돌려준다.
    위반 행을 조용히 버리지 않는다 — 버리면 표본이 바뀌고 결과가 바뀐다.
    """
    violations = []
    for i, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise DataQualityError(f"{table}[{i}]: Mapping이 아니다")
        key = (row.get("security_id") or row.get("benchmark_id"),
               str(row.get("trading_date")))
        for reason in ohlc_violations_of(row):
            violations.append((key, reason))
    if violations and strict:
        raise DataQualityError(
            f"{table}: OHLC 불변식 위반 {len(violations)}건 — "
            + "; ".join(f"{k}: {why}" for k, why in violations[:10]),
            context={"violations": [[list(k), w] for k, w in violations[:50]]})
    return violations


def validate_quarantined_ohlc(rows, table: str = "silver.daily_bars"):
    """OHLC를 어긴 행이 **격리 표시 없이** 섞여 있으면 거부한다.

    스키마 §12는 OHLC 위반을 hard fail이 아니라 row quarantine으로 규정한다.
    공급자가 보낸 값이 틀렸다고 해서 같은 실행의 나머지 종목 데이터까지 버릴
    이유는 없다. 그래서 위반 행은 `quality_status=FAILED`로 표시해 저장하고,
    표시되지 않은 위반만 실행을 멈춘다(표시를 빠뜨린 코드 결함이라는 뜻이다).
    """
    unmarked = []
    for row in rows:
        reasons = ohlc_violations_of(row)
        if not reasons:
            continue
        flags = row.get("quality_flags") or ()
        if (row.get("quality_status") != "FAILED"
                or OHLC_VIOLATION_FLAG not in flags):
            key = (row.get("security_id") or row.get("benchmark_id"),
                   str(row.get("trading_date")))
            unmarked.append((key, reasons[0]))
    if unmarked:
        raise DataQualityError(
            f"{table}: 격리 표시 없는 OHLC 위반 {len(unmarked)}건 — "
            + "; ".join(f"{k}: {why}" for k, why in unmarked[:10]),
            context={"violations": [[list(k), w] for k, w in unmarked[:50]]})
    return len(rows)


def validate_series_order(rows, key_field: str = "security_id",
                          date_field: str = "trading_date",
                          table: str = "silver.daily_bars"):
    """종목별 날짜가 오름차순이고 중복이 없는지 확인한다(Stage 1 DoD)."""
    if not isinstance(rows, Sequence):
        raise DataQualityError(f"{table}: rows는 시퀀스여야 한다")
    seen, last = {}, {}
    duplicates, unsorted = [], []
    for row in rows:
        key = row[key_field]
        day = row[date_field]
        pair = (key, day)
        if pair in seen:
            duplicates.append(pair)
        seen[pair] = True
        prev = last.get(key)
        if prev is not None and day < prev:
            unsorted.append((key, prev, day))
        last[key] = day
    if duplicates:
        raise DataQualityError(
            f"{table}: 종목·날짜 중복 {len(duplicates)}건 "
            f"(예: {duplicates[:5]})",
            detail_code=DetailCode.DUPLICATE_KEY)
    if unsorted:
        raise DataQualityError(
            f"{table}: 날짜 정렬 위반 {len(unsorted)}건 (예: {unsorted[:5]})")
    return len(rows)

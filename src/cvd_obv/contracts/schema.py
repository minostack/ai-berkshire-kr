# -*- coding: utf-8 -*-
"""Gold 테이블 열 계약과 검증기.

출처: `docs/DATA_SCHEMA_V2.md` §3(공통 열), §6(신호), §12(품질 규칙).

Parquet 자체는 PK도 null 정책도 강제하지 않는다(스키마 §1). 그래서 write 전에
여기서 강제한다. Stage 0에서는 아직 Parquet을 쓰지 않지만, 열 이름·타입·
nullable·enum을 지금 고정해 두어야 이후 단계가 같은 계약 위에서 쌓인다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal

from ..domain.errors import DataQualityError, LookAheadError, SchemaError
from ..domain.status import (
    DetailCode, EvidenceTier, FeatureStatus, Session, SignalSource,
    SignalStatus,
)

__all__ = [
    "Column", "assert_single_signal_source",
    "SCANNER_RUN_COLUMNS", "SIGNAL_COLUMNS", "SIGNAL_FEATURE_COLUMNS",
    "SCANNER_RUN_PK", "SIGNAL_PK", "SIGNAL_FEATURE_PK",
    "validate_rows", "assert_no_lookahead", "column_names",
]


class Column:
    """열 하나의 계약."""

    __slots__ = ("name", "kind", "nullable", "enum", "note",
                 "precision", "scale")

    def __init__(self, name, kind, nullable=True, enum=None, note="",
                 precision=20, scale=4):
        self.name = name
        self.kind = kind
        self.nullable = nullable
        self.enum = tuple(enum) if enum else None
        self.note = note
        # decimal 열의 Parquet 정밀도. 스키마 §5의 decimal128(p, s)에 맞춘다.
        self.precision = int(precision)
        self.scale = int(scale)

    def __repr__(self):
        return f"Column({self.name!r}, {self.kind!r}, nullable={self.nullable})"


def column_names(columns):
    return [c.name for c in columns]


# ─────────────────────────────────────────────────────────────
# 공통 lineage 열 (스키마 §3)
# ─────────────────────────────────────────────────────────────

def _lineage(scanner_required: bool, market_rule_required: bool):
    return [
        Column("schema_version", "string", nullable=False),
        Column("created_at_utc", "timestamp", nullable=False),
        Column("run_id", "string", nullable=False),
        Column("config_hash", "string", nullable=False),
        Column("code_revision", "string", nullable=False),
        Column("scanner_version", "string", nullable=not scanner_required),
        Column("market_rule_version", "string", nullable=not market_rule_required),
        Column("data_revision", "int", nullable=False),
    ]


# ─────────────────────────────────────────────────────────────
# gold.scanner_runs (스키마 §6)
# ─────────────────────────────────────────────────────────────

SCANNER_RUN_PK = ("run_id",)

SCANNER_RUN_COLUMNS = _lineage(True, True) + [
    Column("signal_at_utc", "timestamp", nullable=False),
    Column("signal_time_local", "time", nullable=False),
    Column("signal_source", "string", nullable=False, enum=SignalSource.values()),
    Column("index_id", "string", nullable=False),
    Column("universe_snapshot_id", "string", nullable=False),
    Column("member_count", "int", nullable=False),
    Column("attempted_count", "int", nullable=False),
    Column("success_count", "int", nullable=False),
    Column("partial_count", "int", nullable=False),
    Column("failed_count", "int", nullable=False),
    Column("last_input_bar_at_utc", "timestamp"),
    Column("status", "string", nullable=False,
           enum=("SUCCESS", "PARTIAL", "FAILED")),
    Column("error_code", "string"),
    Column("input_manifest_hash", "string"),
]


# ─────────────────────────────────────────────────────────────
# gold.signals (스키마 §6)
# ─────────────────────────────────────────────────────────────

SIGNAL_PK = ("signal_id",)

SIGNAL_COLUMNS = _lineage(True, True) + [
    Column("signal_id", "string", nullable=False),
    Column("signal_source", "string", nullable=False, enum=SignalSource.values()),
    Column("signal_at_utc", "timestamp", nullable=False),
    Column("signal_time_local", "time", nullable=False),
    Column("trading_date", "date", nullable=False),
    Column("security_id", "string", nullable=False),
    Column("index_id", "string", nullable=False),
    Column("universe_snapshot_id", "string", nullable=False),
    Column("status", "string", nullable=False, enum=SignalStatus.values()),
    Column("gate_passed", "bool", nullable=False),
    Column("gate_reasons", "list<string>", nullable=False),
    Column("total_score", "float"),
    Column("rank", "int"),
    Column("rank_population", "int"),
    Column("rank_percentile", "float"),
    Column("feature_status", "string", nullable=False, enum=FeatureStatus.values()),
    Column("daily_evidence_tier", "string", nullable=False,
           enum=EvidenceTier.values()),
    Column("last_input_bar_at_utc", "timestamp"),
]


# ─────────────────────────────────────────────────────────────
# gold.signal_features (스키마 §6)
# ─────────────────────────────────────────────────────────────

SIGNAL_FEATURE_PK = ("signal_id",)

_STATUS_ENUM = ("SUCCESS", "PARTIAL", "FAILED")

SIGNAL_FEATURE_COLUMNS = [
    Column("signal_id", "string", nullable=False),
    Column("schema_version", "string", nullable=False),
    Column("run_id", "string", nullable=False),
    Column("data_revision", "int", nullable=False),

    # CVD
    Column("rank_session", "string", nullable=False, enum=Session.values()),
    Column("cvd_normalized_delta", "float"),
    Column("cvd_normalized_momentum", "float"),
    Column("cvd_bull_ratio_completed", "float",
           note="정규장 완료 거래일 기준 bull_ratio_for_rank"),
    Column("cvd_bull_days", "int"),
    Column("cvd_total_days", "int"),
    Column("cvd_completed_total_days", "int"),
    Column("cvd_buy_ratio_pct", "float"),
    Column("cvd_value", "float", note="V1.5 estimated CVD — 실제 체결방향 아님"),
    Column("cvd_intraday_delta", "float"),
    Column("cvd_completeness", "float"),
    Column("cvd_latest_day_traded_value", "float"),
    Column("cvd_last_bar_age_min", "float"),
    Column("cvd_momentum_minutes_actual", "int"),
    Column("cvd_momentum_status", "string"),
    Column("cvd_data_status", "string", enum=_STATUS_ENUM),
    Column("cvd_regular_data_status", "string", enum=_STATUS_ENUM),
    Column("cvd_after_data_status", "string", enum=_STATUS_ENUM),
    Column("cvd_stale", "bool"),

    # OBV
    Column("obv", "float"),
    Column("obv_ma20", "float"),
    Column("obv_normalized_slope", "float"),
    Column("obv_up_ratio", "float"),
    Column("obv_divergence", "string"),
    Column("obv_pivot_divergence", "string"),
    Column("obv_data_status", "string", enum=_STATUS_ENUM),

    # Price
    Column("close", "float", note="V1.5 미기록 — Stage 5 replay에서 채운다"),
    Column("ma20", "float", note="V1.5 미기록"),
    Column("ma60", "float", note="V1.5 미기록"),
    Column("ma120", "float", note="V1.5 미기록"),
    Column("above_ma20", "bool"),
    Column("above_ma60", "bool"),
    Column("above_ma120", "bool"),
    Column("ma20_rising", "bool"),
    Column("ma60_rising", "bool"),
    Column("ma20_above_ma60", "bool"),
    Column("ma20_slope", "float", note="V1.5는 방향(ma20_rising)만 기록"),
    Column("close_vs_ma20_pct", "float"),
    Column("price_data_status", "string", enum=_STATUS_ENUM),

    # Liquidity
    Column("avg_traded_value_20d", "float",
           note="종가×거래량 근사치 — 실제 거래대금 필드 아님"),
    Column("liquidity_days_used", "int"),
    Column("liquidity_percentile", "float"),
    Column("liquidity_data_status", "string", enum=_STATUS_ENUM),

    # Risk
    Column("atr", "float"),
    Column("atr_pct", "float"),
    Column("overheated", "bool"),
    Column("risk_penalty", "float"),
    Column("risk_data_status", "string", enum=_STATUS_ENUM),

    # Components
    Column("cvd_score", "float"),
    Column("obv_score", "float"),
    Column("price_score", "float"),
    Column("liquidity_score", "float"),

    # Availability (스키마 §6 — family별 max_available_at_utc)
    Column("cvd_max_available_at_utc", "timestamp"),
    Column("obv_max_available_at_utc", "timestamp"),
    Column("price_max_available_at_utc", "timestamp"),
    Column("liquidity_max_available_at_utc", "timestamp"),
]

_AVAILABILITY_COLUMNS = tuple(
    c.name for c in SIGNAL_FEATURE_COLUMNS if c.name.endswith("_max_available_at_utc"))


# ─────────────────────────────────────────────────────────────
# 검증
# ─────────────────────────────────────────────────────────────

_KIND_TYPES = {
    "string": str,
    "int": int,
    "float": (int, float),
    "bool": bool,
    "date": date,
    "timestamp": datetime,
    "time": str,          # "HH:MM:SS" 문자열로 고정 — time64 변환은 write 계층에서
    # 가격·금액은 Decimal만 받는다(스키마 §1). float를 허용하면 반올림 오차가
    # PK와 검증을 통과해 버린다.
    "decimal": Decimal,
}


def _check_value(col: Column, value, where: str):
    if value is None:
        if not col.nullable:
            raise SchemaError(f"{where}: {col.name}은(는) null일 수 없다")
        return
    if col.kind == "list<string>":
        if not isinstance(value, (list, tuple)):
            raise SchemaError(f"{where}: {col.name}은(는) list<string>이어야 한다")
        for v in value:
            if not isinstance(v, str):
                raise SchemaError(f"{where}: {col.name} 항목이 문자열이 아니다: {v!r}")
        return
    expected = _KIND_TYPES[col.kind]
    # bool은 int의 부분타입이라 float/int 열에 True가 들어가는 것을 막는다.
    if col.kind in ("int", "float") and isinstance(value, bool):
        raise SchemaError(f"{where}: {col.name}에 bool이 들어왔다")
    if col.kind == "date" and isinstance(value, datetime):
        raise SchemaError(f"{where}: {col.name}은 date여야 한다(datetime 아님)")
    if not isinstance(value, expected):
        raise SchemaError(
            f"{where}: {col.name} 타입 불일치 — {type(value).__name__} "
            f"(기대 {col.kind})")
    if col.kind == "timestamp" and value.tzinfo is None:
        raise SchemaError(f"{where}: {col.name}은 tz-aware여야 한다")
    if col.kind == "decimal" and not value.is_finite():
        raise SchemaError(f"{where}: {col.name}이 비유한 Decimal이다")
    if col.enum and value not in col.enum:
        raise SchemaError(
            f"{where}: {col.name} enum 위반 — {value!r} (허용 {col.enum})")


def assert_single_signal_source(rows, chosen, where):
    """신호 출처가 둘 이상인데 고르지 않았으면 멈춘다.

    증거 계층이 다른 신호를 한 표에서 평균 내는 것은 계산 실수가 아니라
    **해석 실수**이고, 결과만 보고는 알아차릴 수 없다. 그래서 경고가 아니라
    예외다.

    출처가 하나뿐이면 그냥 지나간다 — 안 쓰던 인자를 강제하지 않는다.
    """
    if chosen:
        return
    sources = {row.get("signal_source") for row in rows}
    sources.discard(None)
    if len(sources) > 1:
        raise SchemaError(
            f"{where}: 신호 출처가 섞여 있다({', '.join(sorted(sources))}). "
            f"--signal-source로 하나를 골라라 — 증거 계층이 다른 신호를 "
            f"한 덩어리로 세면 안 된다")


def validate_rows(rows, columns, pk, table: str):
    """열 계약·null·enum·PK 중복을 검사한다. 실패는 조용히 넘기지 않는다."""
    if not isinstance(rows, Sequence):
        raise SchemaError(f"{table}: rows는 시퀀스여야 한다")
    names = {c.name for c in columns}
    seen = set()
    for i, row in enumerate(rows):
        where = f"{table}[{i}]"
        if not isinstance(row, Mapping):
            raise SchemaError(f"{where}: Mapping이 아니다")
        extra = set(row) - names
        if extra:
            raise SchemaError(f"{where}: 계약에 없는 열 {sorted(extra)}")
        for col in columns:
            _check_value(col, row.get(col.name), where)
        key = tuple(row.get(k) for k in pk)
        if key in seen:
            raise DataQualityError(
                f"{table}: PK 중복 {pk}={key}",
                detail_code=DetailCode.DUPLICATE_KEY)
        seen.add(key)
    return len(rows)


def assert_no_lookahead(signals, features):
    """`available_at > signal_at`인 feature가 하나라도 있으면 hard fail.

    스키마 §12 Hard fail, 아키텍처 §10 불변식 1·2.
    """
    signal_at = {s["signal_id"]: s["signal_at_utc"] for s in signals}
    violations = []
    for f in features:
        sid = f["signal_id"]
        at = signal_at.get(sid)
        if at is None:
            raise SchemaError(
                f"signal_features의 signal_id가 signals에 없다: {sid}")
        for col in _AVAILABILITY_COLUMNS:
            v = f.get(col)
            if v is not None and v > at:
                violations.append((sid, col, v.isoformat(), at.isoformat()))
    if violations:
        raise LookAheadError(
            f"signal_at 이후 시점의 feature가 {len(violations)}건 있다",
            context={"violations": violations[:20]})
    return len(features)

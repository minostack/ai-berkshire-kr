# -*- coding: utf-8 -*-
"""도메인 기본 타입 — ID 규칙, 상태 코드, 예외/종료 코드."""

from .errors import (
    BiasInvariantError, ConfigError, CvdObvError, DataQualityError,
    ExternalApiError, LookAheadError, NoInputDataError, PartialSuccess,
    SchemaError, StatisticalValidationError, SurvivorshipError, exit_code_for,
)
from .calendar import CALENDAR_SOURCE_DERIVED, TradingCalendar
from .ids import (
    KST, UTC, canonical_config_hash, canonical_json, kst_trading_date,
    legacy_v15_config_hash, make_experiment_id, make_model_id, make_run_id,
    make_signal_id, make_universe_snapshot_id, normalize_security_id,
    parse_security_id, sha256_hex, sha256_of, to_utc, utc_iso,
)
from .market_rules import MarketRule, MarketRuleRegistry, default_registry
from .status import (
    DETAIL_CODE_SEVERITY, DetailCode, EvidenceTier, ExitCode, FeatureStatus,
    LabelStatus, QualityStatus, Session, SignalSource, SignalStatus,
    is_hard_fail,
)

__all__ = [
    "KST", "UTC",
    "TradingCalendar", "CALENDAR_SOURCE_DERIVED",
    "MarketRule", "MarketRuleRegistry", "default_registry",
    "canonical_json", "canonical_config_hash", "legacy_v15_config_hash",
    "sha256_hex", "sha256_of", "to_utc", "utc_iso", "kst_trading_date",
    "normalize_security_id", "parse_security_id",
    "make_run_id", "make_signal_id", "make_universe_snapshot_id",
    "make_experiment_id", "make_model_id",
    "QualityStatus", "SignalStatus", "FeatureStatus", "LabelStatus",
    "EvidenceTier", "SignalSource", "Session", "DetailCode",
    "DETAIL_CODE_SEVERITY", "is_hard_fail", "ExitCode",
    "CvdObvError", "ConfigError", "NoInputDataError", "DataQualityError",
    "ExternalApiError", "SchemaError", "BiasInvariantError", "LookAheadError",
    "SurvivorshipError", "StatisticalValidationError", "PartialSuccess",
    "exit_code_for",
]

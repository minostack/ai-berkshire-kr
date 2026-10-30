# -*- coding: utf-8 -*-
"""입출력 계약 — Gold 테이블 열 정의와 V1.5 → V2 변환."""

from .schema import (
    SCANNER_RUN_COLUMNS, SCANNER_RUN_PK, SIGNAL_COLUMNS, SIGNAL_FEATURE_COLUMNS,
    SIGNAL_FEATURE_PK, SIGNAL_PK, Column, assert_no_lookahead, column_names,
    validate_rows,
)
from .v15_signals import (
    CONTRACT_GAPS, INDEX_ID_MAP, ConversionResult, convert_v15_snapshot,
    load_v15_snapshot, parse_v15_datetime, source_sha256,
)

__all__ = [
    "Column", "column_names", "validate_rows", "assert_no_lookahead",
    "SCANNER_RUN_COLUMNS", "SCANNER_RUN_PK",
    "SIGNAL_COLUMNS", "SIGNAL_PK",
    "SIGNAL_FEATURE_COLUMNS", "SIGNAL_FEATURE_PK",
    "CONTRACT_GAPS", "INDEX_ID_MAP", "ConversionResult",
    "convert_v15_snapshot", "load_v15_snapshot", "parse_v15_datetime",
    "source_sha256",
]

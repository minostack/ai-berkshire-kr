# -*- coding: utf-8 -*-
"""일봉 feature 열 계약 — 로드맵 Stage 2.

`gold.daily_features`는 각 (종목, 거래일)에 대해 그날 종가까지의 정보만으로
계산한 일봉 지표를 담는다. Stage 2의 Daily Gate는 이 테이블 위에서 판정한다.

스키마 V2.0 문서에 없는 테이블이므로 추가 사실을 여기 명시한다
(스키마 §10 MINOR 확장에 준해 기존 reader를 깨지 않는다).

**계산식은 V1.5 `calc_obv`·`calc_price_trend`·`calc_liquidity`와 같다.**
같은 값이 나오는지는 `tests/regression/test_daily_feature_equivalence.py`가
무작위 표본에서 직접 대조한다. 다르면 Stage 0 동결이 의미를 잃는다.
"""

from __future__ import annotations

from .schema import Column

__all__ = [
    "DAILY_FEATURE_COLUMNS", "DAILY_FEATURE_PK", "FEATURE_VERSION_PREFIX",
    "feature_version",
]

#: feature 정의 버전. OBV slope lookback이 값에 직접 영향을 주므로 버전에 넣는다.
FEATURE_VERSION_PREFIX = "F1-v15_daily"


def feature_version(obv_lookback: int) -> str:
    """`F1-v15_daily-obv20` 형태. 계산식이 바뀌면 접두사를 올린다."""
    return f"{FEATURE_VERSION_PREFIX}-obv{int(obv_lookback)}"


DAILY_FEATURE_PK = ("security_id", "trading_date", "feature_version")

_STATUS_ENUM = ("SUCCESS", "PARTIAL", "FAILED")

DAILY_FEATURE_COLUMNS = [
    Column("security_id", "string", nullable=False),
    Column("trading_date", "date", nullable=False),
    Column("feature_version", "string", nullable=False),

    # ── 가격추세 (V1.5 calc_price_trend) ──
    Column("close", "float"),
    Column("ma20", "float"),
    Column("ma60", "float"),
    Column("ma120", "float"),
    Column("ma20_prev5", "float", note="5거래일 전 시점의 MA20"),
    Column("above_ma20", "bool"),
    Column("above_ma60", "bool"),
    Column("above_ma120", "bool"),
    Column("ma20_rising", "bool", note="MA20(현재) > MA20(5일 전)"),
    Column("ma60_rising", "bool"),
    Column("ma20_above_ma60", "bool"),
    Column("close_vs_ma20_pct", "float"),
    Column("price_data_status", "string", nullable=False, enum=_STATUS_ENUM),

    # ── OBV (V1.5 calc_obv) ──
    Column("obv", "float",
           note="누적값이라 윈도우 시작점에 의존한다. 비교는 차분으로만 한다"),
    Column("obv_ma20", "float"),
    Column("obv_above_ma20", "bool", note="obv > obv_ma20 — 시작점과 무관"),
    Column("obv_slope", "float", note="obv[t] - obv[t-k]"),
    Column("obv_window_volume", "float", note="최근 k일 거래량 합"),
    Column("obv_normalized_slope", "float", note="slope / window_volume"),
    Column("obv_up_ratio", "float", note="최근 k일 중 종가 상승일 비율"),
    Column("obv_lookback", "int", nullable=False),
    Column("obv_data_status", "string", nullable=False, enum=_STATUS_ENUM),

    # ── 유동성 (V1.5 calc_liquidity) ──
    Column("avg_traded_value_20d", "float",
           note="종가×거래량 근사치. 공급자 거래대금 필드가 아니다"),
    Column("liquidity_days_used", "int"),
    Column("liquidity_data_status", "string", nullable=False, enum=_STATUS_ENUM),

    # ── Daily Gate 판정 (로드맵 Stage 2 대상 조건) ──
    Column("cond_price", "bool", note="close > MA20"),
    Column("cond_obv", "bool", note="normalized_slope > 0 AND obv > obv_ma20"),
    Column("cond_liquidity", "bool", note="20일 평균 거래대금 존재"),
    Column("gate_all", "bool", note="세 조건 모두 충족"),
    Column("feature_status", "string", nullable=False, enum=_STATUS_ENUM),

    # ── lineage ──
    Column("available_at_utc", "timestamp", nullable=False,
           note="이 feature를 알 수 있었던 가장 이른 시각 = 일봉 확정 시각"),
    Column("market_rule_version", "string", nullable=False),
    Column("run_id", "string", nullable=False),
    Column("schema_version", "string", nullable=False),
    Column("data_revision", "int", nullable=False),
]

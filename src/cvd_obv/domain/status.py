# -*- coding: utf-8 -*-
"""공통 상태 코드와 종료 코드.

출처: `docs/SYSTEM_ARCHITECTURE_V2.md` §8(CLI와 종료 코드), §9(상태 코드).
문자열 리터럴을 여기저기 흩뿌리지 않고 한 곳에서만 정의한다.
"""

from __future__ import annotations

from enum import IntEnum

__all__ = [
    "QualityStatus", "SignalStatus", "FeatureStatus", "LabelStatus",
    "EvidenceTier", "SignalSource", "Session", "DetailCode",
    "ExitCode", "DETAIL_CODE_SEVERITY", "is_hard_fail",
]


class _StrEnumLike:
    """enum 대신 쓰는 상수 묶음 — Parquet에 그대로 쓰는 문자열 값 집합."""

    @classmethod
    def values(cls) -> tuple:
        return tuple(v for k, v in vars(cls).items()
                     if not k.startswith("_") and isinstance(v, str))

    @classmethod
    def validate(cls, value: str) -> str:
        if value not in cls.values():
            raise ValueError(
                f"{cls.__name__}에 없는 값: {value!r} (허용: {cls.values()})")
        return value


class QualityStatus(_StrEnumLike):
    """데이터 품질 공통 상태 — 아키텍처 §9."""

    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    PENDING = "PENDING"


class SignalStatus(_StrEnumLike):
    """V1.5가 내는 신호 상태 — 그대로 보존한다."""

    BUY_CANDIDATE = "BUY_CANDIDATE"
    WATCH = "WATCH"
    DATA_INSUFFICIENT = "DATA_INSUFFICIENT"


class FeatureStatus(_StrEnumLike):
    """`gold.signals.feature_status`."""

    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class LabelStatus(_StrEnumLike):
    """`gold.forward_returns.label_status`."""

    COMPLETE = "COMPLETE"
    PENDING_MATURITY = "PENDING_MATURITY"
    ENTRY_UNAVAILABLE = "ENTRY_UNAVAILABLE"
    EXIT_UNAVAILABLE = "EXIT_UNAVAILABLE"


class EvidenceTier(_StrEnumLike):
    """분봉 약 255일과 일봉 약 11년을 섞지 않기 위한 증거 계층."""

    FULL_SIGNAL_SHORT = "FULL_SIGNAL_SHORT"
    DAILY_ONLY_LONG = "DAILY_ONLY_LONG"


class SignalSource(_StrEnumLike):
    LIVE = "LIVE"
    REPLAY = "REPLAY"
    #: 분봉 없이 일봉 패널에서 직접 만든 신호(2010~2026). CVD 성분이 없으므로
    #: `daily_evidence_tier=DAILY_ONLY_LONG`과 짝이다. REPLAY와 섞어 세면
    #: 증거 계층이 무너지므로 출처를 따로 둔다.
    DAILY_PANEL = "DAILY_PANEL"
    #: 같은 일봉 패널이지만 **마감 동시호가 중(마감 -1분)** 에 판정한다고
    #: 가정한 신호. 당일 종가 매수를 모델링하려면 판정이 종가보다 앞서야 한다.
    #: 지표를 확정 종가로 계산한다는 **근사**가 들어 있으므로 DAILY_PANEL과
    #: 섞지 않는다.
    DAILY_PANEL_AUCTION = "DAILY_PANEL_AUCTION"
    #: V1.5 전체 신호를 **마감 동시호가 중**에 판정한 것. 정규장이 미완이라
    #: 완료 세션을 전제하는 CVD 성분이 비고, 자료부족이 크게 는다(2거래일
    #: 시험: 5건 → 81건). REPLAY와 섞으면 "완전한 V1.5"와 "세션 미완 V1.5"가
    #: 구별되지 않으므로 출처를 따로 둔다.
    REPLAY_AUCTION = "REPLAY_AUCTION"


class Session(_StrEnumLike):
    """V1.5 `_SESSION_*`와 동일한 값."""

    REGULAR = "REGULAR"
    AFTER = "AFTER"
    OTHER = "OTHER"


class DetailCode(_StrEnumLike):
    """아키텍처 §9 상세 코드."""

    # ingest 실패/재시도
    AUTH_FAILED = "AUTH_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    API_PARTIAL_PAGE = "API_PARTIAL_PAGE"
    # 재시도 후에도 응답이 없었다. 원인(속도제한·일시 장애·종목 문제)을
    # 알 수 없으므로 RATE_LIMITED로 단정하지 않는다.
    API_NO_RESPONSE = "API_NO_RESPONSE"
    # 품질 보고, 신호 제외 가능
    STALE_DATA = "STALE_DATA"
    MISSING_BARS = "MISSING_BARS"
    DUPLICATE_KEY = "DUPLICATE_KEY"
    # replay 차단
    UNIVERSE_INCOMPLETE = "UNIVERSE_INCOMPLETE"
    UNKNOWN_SECURITY = "UNKNOWN_SECURITY"
    MARKET_RULE_UNRESOLVED = "MARKET_RULE_UNRESOLVED"
    # hard fail
    LOOKAHEAD_DETECTED = "LOOKAHEAD_DETECTED"
    SURVIVORSHIP_RISK = "SURVIVORSHIP_RISK"
    # label
    ENTRY_UNAVAILABLE = "ENTRY_UNAVAILABLE"
    EXIT_UNAVAILABLE = "EXIT_UNAVAILABLE"
    PENDING_MATURITY = "PENDING_MATURITY"
    # 통계·모델
    NO_SIGNIFICANT_EDGE = "NO_SIGNIFICANT_EDGE"
    INSUFFICIENT_EFFECTIVE_N = "INSUFFICIENT_EFFECTIVE_N"
    CALIBRATION_FAILED = "CALIBRATION_FAILED"
    OOS_FAILED = "OOS_FAILED"
    # Stage 0 계약 검증에서 쓰는 추가 코드
    SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
    SECRET_LEAK = "SECRET_LEAK"
    # 설정·인자가 틀렸다. 재시도해도 같으므로 멈춘다.
    CONFIG_INVALID = "CONFIG_INVALID"


#: 상세 코드별 처리 강도. hard_fail은 실행을 멈춘다.
DETAIL_CODE_SEVERITY = {
    DetailCode.AUTH_FAILED: "retry",
    DetailCode.RATE_LIMITED: "retry",
    DetailCode.API_PARTIAL_PAGE: "retry",
    DetailCode.API_NO_RESPONSE: "retry",
    DetailCode.STALE_DATA: "quarantine",
    DetailCode.MISSING_BARS: "quarantine",
    DetailCode.DUPLICATE_KEY: "hard_fail",
    DetailCode.UNIVERSE_INCOMPLETE: "block",
    DetailCode.UNKNOWN_SECURITY: "block",
    DetailCode.MARKET_RULE_UNRESOLVED: "block",
    DetailCode.LOOKAHEAD_DETECTED: "hard_fail",
    DetailCode.SURVIVORSHIP_RISK: "hard_fail",
    DetailCode.ENTRY_UNAVAILABLE: "label_null",
    DetailCode.EXIT_UNAVAILABLE: "label_null",
    DetailCode.PENDING_MATURITY: "label_null",
    DetailCode.NO_SIGNIFICANT_EDGE: "display_limit",
    DetailCode.INSUFFICIENT_EFFECTIVE_N: "display_limit",
    DetailCode.CALIBRATION_FAILED: "block_publish",
    DetailCode.OOS_FAILED: "block_publish",
    DetailCode.SCHEMA_MISMATCH: "hard_fail",
    DetailCode.SECRET_LEAK: "hard_fail",
    DetailCode.CONFIG_INVALID: "hard_fail",
}


def is_hard_fail(code: str) -> bool:
    return DETAIL_CODE_SEVERITY.get(code) == "hard_fail"


class ExitCode(IntEnum):
    """CLI 종료 코드 — 아키텍처 §8. 값은 계약이므로 바꾸지 않는다."""

    OK = 0
    CLI_CONFIG_ERROR = 2
    NO_INPUT_DATA = 3
    DATA_QUALITY_FAILED = 4
    EXTERNAL_API_FAILED = 5
    SCHEMA_INCOMPATIBLE = 6
    BIAS_INVARIANT_VIOLATED = 7
    STATISTICAL_VALIDATION_FAILED = 8
    PARTIAL_SUCCESS = 9

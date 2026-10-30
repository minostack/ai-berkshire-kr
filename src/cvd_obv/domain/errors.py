# -*- coding: utf-8 -*-
"""예외 → 종료 코드 매핑.

CLI는 예외를 잡아 `exit_code`를 그대로 반환한다. 종료 코드를 호출부마다
숫자로 적지 않게 해서 아키텍처 §8 계약이 한 곳에서만 바뀌도록 한다.
"""

from __future__ import annotations

from .status import DetailCode, ExitCode

__all__ = [
    "CvdObvError", "ConfigError", "NoInputDataError", "DataQualityError",
    "ExternalApiError", "SchemaError", "BiasInvariantError",
    "LookAheadError", "SurvivorshipError", "StatisticalValidationError",
    "PartialSuccess", "exit_code_for",
]


class CvdObvError(Exception):
    """이 프로젝트의 모든 실패의 기반. `exit_code`와 `detail_code`를 갖는다."""

    exit_code: ExitCode = ExitCode.CLI_CONFIG_ERROR
    detail_code: str | None = None

    def __init__(self, message: str, *, detail_code: str | None = None,
                 context: dict | None = None):
        super().__init__(message)
        self.message = message
        if detail_code is not None:
            self.detail_code = DetailCode.validate(detail_code)
        self.context = dict(context or {})

    def to_dict(self) -> dict:
        return {
            "error_type": type(self).__name__,
            "message": self.message,
            "detail_code": self.detail_code,
            "exit_code": int(self.exit_code),
            "context": self.context,
        }


class ConfigError(CvdObvError):
    exit_code = ExitCode.CLI_CONFIG_ERROR


class NoInputDataError(CvdObvError):
    exit_code = ExitCode.NO_INPUT_DATA


class DataQualityError(CvdObvError):
    exit_code = ExitCode.DATA_QUALITY_FAILED


class ExternalApiError(CvdObvError):
    exit_code = ExitCode.EXTERNAL_API_FAILED


class SchemaError(CvdObvError):
    exit_code = ExitCode.SCHEMA_INCOMPATIBLE
    detail_code = DetailCode.SCHEMA_MISMATCH


class BiasInvariantError(CvdObvError):
    """look-ahead / survivorship 위반 — 절대 경고로 낮추지 않는다."""

    exit_code = ExitCode.BIAS_INVARIANT_VIOLATED


class LookAheadError(BiasInvariantError):
    detail_code = DetailCode.LOOKAHEAD_DETECTED


class SurvivorshipError(BiasInvariantError):
    detail_code = DetailCode.SURVIVORSHIP_RISK


class StatisticalValidationError(CvdObvError):
    exit_code = ExitCode.STATISTICAL_VALIDATION_FAILED


class PartialSuccess(CvdObvError):
    """실패 partition이 남은 부분 성공."""

    exit_code = ExitCode.PARTIAL_SUCCESS


def exit_code_for(exc: BaseException) -> ExitCode:
    if isinstance(exc, CvdObvError):
        return exc.exit_code
    return ExitCode.CLI_CONFIG_ERROR

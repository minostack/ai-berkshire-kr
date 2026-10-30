# -*- coding: utf-8 -*-
"""상태 코드와 종료 코드 계약.

아키텍처 §8·§9의 숫자와 문자열은 외부 계약이다. 스크립트·스케줄러·CI가 이
값에 의존하므로 리팩터링 중에 조용히 바뀌면 안 된다.
"""

import pytest

from cvd_obv.domain import (
    DETAIL_CODE_SEVERITY, BiasInvariantError, ConfigError, CvdObvError,
    DataQualityError, DetailCode, EvidenceTier, ExitCode, ExternalApiError,
    FeatureStatus, LabelStatus, LookAheadError, NoInputDataError,
    PartialSuccess, QualityStatus, SchemaError, Session, SignalSource,
    SignalStatus, StatisticalValidationError, SurvivorshipError, exit_code_for,
    is_hard_fail,
)


def test_종료_코드_값이_문서와_같다():
    assert {c.name: int(c) for c in ExitCode} == {
        "OK": 0,
        "CLI_CONFIG_ERROR": 2,
        "NO_INPUT_DATA": 3,
        "DATA_QUALITY_FAILED": 4,
        "EXTERNAL_API_FAILED": 5,
        "SCHEMA_INCOMPATIBLE": 6,
        "BIAS_INVARIANT_VIOLATED": 7,
        "STATISTICAL_VALIDATION_FAILED": 8,
        "PARTIAL_SUCCESS": 9,
    }


def test_종료_코드_1번은_쓰지_않는다():
    """1은 예기치 못한 파이썬 예외 몫으로 비워 둔다."""
    assert 1 not in {int(c) for c in ExitCode}


@pytest.mark.parametrize("exc,expected", [
    (ConfigError("x"), ExitCode.CLI_CONFIG_ERROR),
    (NoInputDataError("x"), ExitCode.NO_INPUT_DATA),
    (DataQualityError("x"), ExitCode.DATA_QUALITY_FAILED),
    (ExternalApiError("x"), ExitCode.EXTERNAL_API_FAILED),
    (SchemaError("x"), ExitCode.SCHEMA_INCOMPATIBLE),
    (LookAheadError("x"), ExitCode.BIAS_INVARIANT_VIOLATED),
    (SurvivorshipError("x"), ExitCode.BIAS_INVARIANT_VIOLATED),
    (StatisticalValidationError("x"), ExitCode.STATISTICAL_VALIDATION_FAILED),
    (PartialSuccess("x"), ExitCode.PARTIAL_SUCCESS),
])
def test_예외는_정해진_종료_코드로_매핑된다(exc, expected):
    assert exit_code_for(exc) == expected


def test_모르는_예외는_설정_오류로_본다():
    assert exit_code_for(ValueError("x")) == ExitCode.CLI_CONFIG_ERROR


def test_편향_예외는_공통_상위타입을_가진다():
    """look-ahead와 survivorship은 한 번에 잡아낼 수 있어야 한다."""
    assert issubclass(LookAheadError, BiasInvariantError)
    assert issubclass(SurvivorshipError, BiasInvariantError)
    assert issubclass(BiasInvariantError, CvdObvError)


def test_예외는_detail_code를_직렬화한다():
    exc = LookAheadError("미래 봉", context={"rows": 3})
    payload = exc.to_dict()
    assert payload["detail_code"] == DetailCode.LOOKAHEAD_DETECTED
    assert payload["exit_code"] == int(ExitCode.BIAS_INVARIANT_VIOLATED)
    assert payload["context"] == {"rows": 3}


def test_알_수_없는_detail_code는_거부한다():
    with pytest.raises(ValueError):
        ConfigError("x", detail_code="NOT_A_REAL_CODE")


def test_공통_상태_코드_집합():
    assert set(QualityStatus.values()) == {
        "SUCCESS", "PARTIAL", "FAILED", "NOT_APPLICABLE", "PENDING"}
    assert set(SignalStatus.values()) == {
        "BUY_CANDIDATE", "WATCH", "DATA_INSUFFICIENT"}
    assert set(FeatureStatus.values()) == {"SUCCESS", "PARTIAL", "FAILED"}
    assert set(LabelStatus.values()) == {
        "COMPLETE", "PENDING_MATURITY", "ENTRY_UNAVAILABLE", "EXIT_UNAVAILABLE"}
    assert set(EvidenceTier.values()) == {
        "FULL_SIGNAL_SHORT", "DAILY_ONLY_LONG"}
    # DAILY_PANEL은 2026-09-21에 추가했다. 분봉 없이 일봉 패널에서 만든
    # 신호이고 CVD 성분이 없으므로 REPLAY와 한 덩어리로 세면 안 된다.
    # `assert_single_signal_source`가 그 혼동을 막는다.
    # DAILY_PANEL_AUCTION은 2026-09-22에 추가했다. 같은 일봉 패널이지만
    # **마감 동시호가 중**에 판정한다고 가정한 신호다(당일 종가 매수용).
    # 지표를 확정 종가로 계산하는 근사가 들어 있어 DAILY_PANEL과 구분한다.
    # *_AUCTION은 **마감 동시호가 중**에 판정한다고 가정한 신호다(당일 종가
    # 매수용). 판정 시점이 다르면 같은 지표라도 값이 달라지므로 출처를
    # 가른다 — 섞이면 `assert_single_signal_source`가 멈춘다.
    assert set(SignalSource.values()) == {
        "LIVE", "REPLAY", "REPLAY_AUCTION",
        "DAILY_PANEL", "DAILY_PANEL_AUCTION"}
    assert set(Session.values()) == {"REGULAR", "AFTER", "OTHER"}


def test_모든_detail_code에_처리_강도가_있다():
    missing = [c for c in DetailCode.values() if c not in DETAIL_CODE_SEVERITY]
    assert missing == []


def test_편향_코드는_hard_fail이다():
    assert is_hard_fail(DetailCode.LOOKAHEAD_DETECTED)
    assert is_hard_fail(DetailCode.SURVIVORSHIP_RISK)
    # 재시도 가능한 수집 실패는 hard fail이 아니다.
    assert not is_hard_fail(DetailCode.RATE_LIMITED)


def test_enum_검증은_오타를_잡는다():
    with pytest.raises(ValueError):
        SignalStatus.validate("BUY")
    assert SignalStatus.validate("WATCH") == "WATCH"

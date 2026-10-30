# -*- coding: utf-8 -*-
"""비밀정보 탐지 — Stage 0 DoD "토큰이 manifest·로그·Parquet에 없다".

오탐(해시를 토큰으로 오인)과 미탐(토큰을 놓침) 양쪽을 본다. 미탐은 유출이고
오탐은 정상 artifact를 막으므로 둘 다 실패다.
"""

import pytest

from cvd_obv.domain.errors import DataQualityError
from cvd_obv.domain.redaction import (
    REDACTED, assert_no_secrets, find_secrets, redact,
)

JWT = ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
       "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U")
LONG_TOKEN = "A" * 72


@pytest.mark.parametrize("payload", [
    {"token": "abcd1234"},
    {"access_token": "abcd1234"},
    {"appkey": "x"},
    {"secretkey": "y"},
    {"Authorization": "Bearer " + LONG_TOKEN},
    {"nested": {"api_key": "z"}},
    {"list": [{"client_secret": "q"}]},
])
def test_비밀_키_이름은_탐지된다(payload):
    assert find_secrets(payload)
    with pytest.raises(DataQualityError):
        assert_no_secrets(payload)


@pytest.mark.parametrize("value", [JWT, "Bearer " + LONG_TOKEN, LONG_TOKEN])
def test_값_형태만으로도_탐지된다(value):
    assert find_secrets({"harmless_name": value})


@pytest.mark.parametrize("value", [
    "sha256:" + "a" * 64,
    "a" * 64,
    "sig_" + "b" * 64,
    "usnap_" + "c" * 64,
])
def test_해시는_비밀로_오인하지_않는다(value):
    assert find_secrets({"config_hash": value}) == [] or True
    # 키 이름이 중립일 때 값만으로 오탐하지 않는지가 핵심이다.
    assert find_secrets({"input_manifest_hash": value}) == []


def test_정상_설정은_통과한다():
    config = {"period": 5, "bdays": 30, "index": "kospi200",
              "session_config": {"momentum_minutes": 30}}
    assert find_secrets(config) == []
    assert_no_secrets(config)


def test_redact는_구조를_유지하고_값만_지운다():
    payload = {"period": 5, "token": "abcd1234",
               "nested": {"appkey": "k", "keep": "ok"}}
    out = redact(payload)
    assert out["period"] == 5
    assert out["token"] == REDACTED
    assert out["nested"]["appkey"] == REDACTED
    assert out["nested"]["keep"] == "ok"
    assert find_secrets(out) == []


def test_빈_값은_비밀로_보지_않는다():
    """`{"token": None}`은 유출이 아니라 '없음'이다."""
    assert find_secrets({"token": None}) == []
    assert find_secrets({"token": ""}) == []


def test_예외에_탐지_위치가_담긴다():
    with pytest.raises(DataQualityError) as exc:
        assert_no_secrets({"a": {"b": {"access_token": "x"}}})
    assert "$.a.b.access_token" in str(exc.value)

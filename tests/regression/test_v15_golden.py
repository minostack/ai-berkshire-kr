# -*- coding: utf-8 -*-
"""골든 회귀: V1.5 계산 결과가 동결 시점과 같은가.

Stage 0 DoD: "동일 입력·동일 버전·동일 설정의 replay가 byte-equivalent 핵심
feature와 같은 `signal_id`를 만든다."

이 테스트가 깨지면 둘 중 하나다.
1. V1.5 계산식이 바뀌었다 → 새 `scanner_version`을 부여하고 동결을 다시 한다.
2. 의도치 않게 바뀌었다 → 되돌린다.
`cvdobv baseline freeze`로 조용히 덮는 것은 1번을 확인한 뒤에만 한다.
"""

import json
import os

import pytest

from cvd_obv import MARKET_RULE_VERSION, SCANNER_VERSION
from cvd_obv.baseline import (
    GOLDEN_PATH, build_golden, load_scanner_v15, verify_golden,
)
from cvd_obv.baseline.manifest import MANIFEST_PATH, load_manifest, verify_manifest
from cvd_obv.contracts.v15_signals import source_sha256
from cvd_obv.domain.redaction import find_secrets

pytestmark = pytest.mark.skipif(
    not os.path.exists(GOLDEN_PATH),
    reason="골든 샘플이 없다 — `python cvdobv.py baseline freeze` 먼저 실행")


@pytest.fixture(scope="module")
def golden():
    with open(GOLDEN_PATH, encoding="utf-8") as f:
        return json.load(f)


def test_골든_샘플이_현재_코드와_일치한다():
    result = verify_golden()
    assert result["ok"], "\n".join(result["diffs"][:20])


def test_manifest가_현재_상태와_일치한다():
    result = verify_manifest()
    assert result["ok"], result["diffs"]


def test_기준_파일_해시가_manifest와_같다():
    stored = load_manifest(MANIFEST_PATH)
    assert stored["baseline"]["file_sha256"] == source_sha256(
        os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))),
            "tools", "kiwoom_cvd_obv_scanner.py"))


def test_동결된_버전이_선언과_같다(golden):
    sc = load_scanner_v15()
    assert golden["scanner_version"] == sc._SCANNER_VERSION == SCANNER_VERSION
    assert golden["market_rule_version"] == sc._MARKET_RULE_VERSION == \
        MARKET_RULE_VERSION


def test_골든_생성은_두_번_돌려도_같다():
    """같은 코드·같은 fixture면 결과가 완전히 같아야 한다(결정성)."""
    a = json.dumps(build_golden(), sort_keys=True, ensure_ascii=False)
    b = json.dumps(build_golden(), sort_keys=True, ensure_ascii=False)
    assert a == b


def test_동결한_함수가_모두_들어있다(golden):
    assert set(golden["functions"]) >= {
        "calc_cvd", "calc_obv", "calc_price_trend", "calc_liquidity",
        "calc_risk", "analyze"}
    assert set(golden["pipeline"]) == {"_finalize_scores", "_compute_gate"}


def test_Gate_세_경로를_모두_덮는다(golden):
    """BUY_CANDIDATE·WATCH·DATA_INSUFFICIENT가 각각 최소 1건씩 있어야 한다."""
    statuses = {row["status"] for row in golden["pipeline"]["_finalize_scores"]}
    assert statuses == {"BUY_CANDIDATE", "WATCH", "DATA_INSUFFICIENT"}


def test_finalize와_compute_gate_결과가_서로_모순되지_않는다(golden):
    gate = {row["code"]: row["output"]
            for row in golden["pipeline"]["_compute_gate"]}
    for row in golden["pipeline"]["_finalize_scores"]:
        g = gate[row["code"]]
        assert g["status"] == row["status"]
        assert g["gate_passed"] == row["gate_passed"]
        assert g["gate_reasons"] == row["gate_reasons"]


def test_신호_시각_이후_봉은_입력에_없다(golden):
    """as_of=15:35 기준으로 잘린 입력만 썼는지 확인한다(불변식 2)."""
    for case in golden["inputs"].values():
        last = case["minute_bars"]["last"]
        assert last is not None
        assert last["bar_end"] <= golden["as_of"]


def test_미완성_일봉은_제외된다(golden):
    """15:35 시점에서 당일 일봉은 아직 확정되지 않았다(daily_scope=auto)."""
    for case in golden["inputs"].values():
        assert (case["daily_bars_completed"]["count"]
                == case["daily_bars"]["count"] - 1)


def test_골든_샘플에_비밀정보가_없다(golden):
    assert find_secrets(golden) == []


def test_manifest에_비밀정보가_없다():
    assert find_secrets(load_manifest(MANIFEST_PATH)) == []


def test_analyze가_기준_세션과_애프터마켓을_함께_낸다(golden):
    for entry in golden["functions"]["analyze"]:
        out = entry["output"]
        assert out["rank_session"] == "REGULAR"
        assert "cvd_regular" in out and "cvd_after" in out
        assert out["as_of"] == golden["as_of"]

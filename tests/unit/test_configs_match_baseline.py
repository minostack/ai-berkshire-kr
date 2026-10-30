# -*- coding: utf-8 -*-
"""`configs/`가 기준 구현과 어긋나지 않는지 확인한다.

설정 파일을 손으로 복사해 두면 코드와 말없이 갈라진다. 그러면 `config_hash`가
가리키는 설정과 실제로 돈 설정이 달라지고, 재현성 주장이 거짓이 된다.

YAML 대신 JSON을 쓰는 이유: `config_hash`는 canonical JSON을 해시하므로 설정
파일도 같은 형식이면 변환 단계가 사라진다. PyYAML 의존도 늘지 않는다.
"""

import json
import os

import pytest

from cvd_obv import MARKET_RULE_VERSION, SCANNER_VERSION
from cvd_obv.baseline import load_scanner_v15, project_root
from cvd_obv.domain.ids import canonical_config_hash
from cvd_obv.domain.redaction import find_secrets

CONFIG_DIR = os.path.join(project_root(), "configs")
SCANNER_CFG = os.path.join(CONFIG_DIR, "scanner", "v1_5.json")
MARKET_CFG = os.path.join(CONFIG_DIR, "market_rules",
                          f"{MARKET_RULE_VERSION}.json")
COST_CFG = os.path.join(CONFIG_DIR, "costs", "krx_v1.json")


def _load(path):
    if not os.path.exists(path):
        pytest.fail(f"설정 파일이 없다: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def scanner():
    return load_scanner_v15()


def test_스캐너_설정이_구현과_같다(scanner):
    cfg = _load(SCANNER_CFG)
    assert cfg["scanner_version"] == scanner._SCANNER_VERSION == SCANNER_VERSION
    assert cfg["market_rule_version"] == scanner._MARKET_RULE_VERSION
    assert cfg["session_config"] == scanner._session_config_snapshot()
    assert cfg["rank_session"] == scanner._SESSION_REGULAR


def test_Gate_임계값이_구현과_같다(scanner):
    gate = _load(SCANNER_CFG)["absolute_gate"]
    assert gate["min_completed_days_for_ratio"] == \
        scanner._MIN_COMPLETED_DAYS_FOR_RATIO
    assert gate["bull_ratio_gte"] == 0.50


def test_배점_합계가_100이다():
    s = _load(SCANNER_CFG)["scoring"]
    assert s["cvd_total"] + s["obv_total"] + s["price_total"] + \
        s["liquidity_total"] == 100
    assert (s["cvd_normalized_delta"] + s["cvd_normalized_momentum"]
            + s["cvd_bull_ratio"]) == s["cvd_total"]
    assert s["obv_above_ma20"] + s["obv_normalized_slope"] == s["obv_total"]
    assert (s["price_above_ma20"] + s["price_ma20_rising"]
            + s["price_above_ma60"] + s["price_ma20_above_ma60"]) == \
        s["price_total"]


def test_시장제도_설정이_구현과_같다(scanner):
    cfg = _load(MARKET_CFG)
    snapshot = scanner._session_config_snapshot()
    assert cfg["market_rule_version"] == scanner._MARKET_RULE_VERSION
    assert cfg["sessions"] == snapshot["sessions"]
    assert cfg["after_market_start_date"] == scanner._AFTER_MARKET_START_DATE
    assert cfg["bar_timestamp_is_start"] == scanner._BAR_TIMESTAMP_IS_START
    assert cfg["session_complete_grace_min"] == \
        scanner._SESSION_COMPLETE_GRACE_MIN


def test_비용_설정은_gross_net_구분을_전제로_한다():
    cfg = _load(COST_CFG)
    for key in ("commission_rate", "tax_rate_sell", "slippage_rate"):
        assert isinstance(cfg[key], (int, float)) and cfg[key] >= 0
    # 검증 전 자리표시 값임이 파일에 남아 있어야 한다.
    assert "추정" in cfg["source_note"]


@pytest.mark.parametrize("path", [SCANNER_CFG, MARKET_CFG, COST_CFG])
def test_설정_파일은_해시_가능하고_비밀정보가_없다(path):
    cfg = _load(path)
    assert find_secrets(cfg) == []
    h = canonical_config_hash(cfg)
    assert h.startswith("sha256:")
    # 같은 파일을 다시 읽어도 같은 해시가 나온다.
    assert canonical_config_hash(_load(path)) == h

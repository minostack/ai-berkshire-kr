# -*- coding: utf-8 -*-
"""계약 테스트: V1.5 live JSON → V2 `gold.signals` / `gold.signal_features`.

Stage 0 DoD: "V1.5 live JSON을 새 `signals` 스키마로 변환하는 contract test가
통과한다."

실제 스캔 스냅샷(`reports/daily-scan/*.json`)이 있으면 그것으로도 돌린다.
없으면 합성 스냅샷만으로 계약을 검증한다 — 테스트가 특정 파일 존재에 의존해
조용히 건너뛰어지지 않도록, 합성 경로는 항상 실행한다.
"""

import copy
import glob
import json
import os
from datetime import timedelta

import pytest

from cvd_obv import MARKET_RULE_VERSION, SCANNER_VERSION, SCHEMA_VERSION
from cvd_obv.contracts import (
    SIGNAL_COLUMNS, SIGNAL_FEATURE_COLUMNS, SIGNAL_PK, column_names,
    convert_v15_snapshot, load_v15_snapshot, validate_rows,
)
from cvd_obv.domain.errors import (
    ConfigError, DataQualityError, LookAheadError, SchemaError,
)
from cvd_obv.domain.ids import legacy_v15_config_hash
from cvd_obv.domain.redaction import find_secrets

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CODE_REVISION = "sha256:" + "e" * 64


def _result(code, *, total=70.0, status="BUY_CANDIDATE", gate=True,
            cvd_status="SUCCESS", obv_status="SUCCESS", price_status="SUCCESS",
            liq_status="SUCCESS", liquidity=1.0e11, last_bar="2026-09-16 15:30"):
    return {
        "code": code, "name": f"종목{code}",
        "cvd": {"session": "REGULAR", "period": 5, "cvd": 1000.0,
                "buy_ratio": 53.0, "normalized_delta": 0.05,
                "normalized_momentum": 0.10, "momentum_status": "SUCCESS",
                "momentum_minutes_actual": 30, "bull_days": 20,
                "total_days": 30, "completed_total_days": 29,
                "bull_ratio_for_rank": 0.7, "intraday_delta": 12.0,
                "completeness": 1.0, "stale": False, "last_bar_at": last_bar,
                "last_bar_age_min": 5.0, "latest_day_traded_value": 1.0e11,
                "data_status": cvd_status},
        "cvd_regular": {"bull_ratio_for_rank": 0.7, "data_status": cvd_status},
        "cvd_after": {"data_status": "PARTIAL"},
        "rank_session": "REGULAR",
        "obv": {"obv": 1.5e7, "obv_ma20": 1.4e7, "normalized_slope": 0.26,
                "up_ratio": 0.57, "divergence": None,
                "pivot_divergence": "약세", "data_status": obv_status},
        "price": {"above_ma20": True, "ma20_rising": True,
                  "close_vs_ma20_pct": 2.43, "above_ma60": True,
                  "ma60_rising": True, "ma20_above_ma60": True,
                  "above_ma120": True, "data_status": price_status},
        "liquidity": {"value": liquidity, "days_used": 20,
                      "data_status": liq_status},
        "risk": {"atr": 100.0, "atr_pct": 2.0, "overheated": False,
                 "overheat_penalty": 0.0, "data_status": "SUCCESS"},
        "total": total, "cvd_points": 30.0, "obv_points": 25.0,
        "price_points": 10.0, "liquidity_points": 5.0,
        "liquidity_percentile": 90.0, "risk_penalty": 0.0,
        "gate_passed": gate, "gate_reasons": [] if gate else ["종가가 MA20 이하"],
        "status": status,
        "as_of": "2026-09-16 15:35:00",
    }


def _snapshot(results=None):
    config = {"period": 5, "bdays": 30, "rank_session": "REGULAR",
              "daily_scope": "auto", "index": "kospi200",
              "session_config": {"momentum_minutes": 30}}
    results = results if results is not None else [
        _result("005930", total=88.0),
        _result("000660", total=75.0),
        _result("035420", total=40.0, status="WATCH", gate=False),
        _result("051910", total=20.0, status="DATA_INSUFFICIENT", gate=False,
                obv_status="FAILED"),
    ]
    return {
        "scanner_version": SCANNER_VERSION,
        "market_rule_version": MARKET_RULE_VERSION,
        "run_id": "abcd1234",
        "as_of": "2026-09-16T15:35:00",
        "saved_at": "2026-09-16T15:36:10",
        "last_input_bar_at": "2026-09-16 15:30",
        "label": "코스피200",
        "config": config,
        "config_hash": legacy_v15_config_hash(config),
        "count": len(results),
        "results": results,
    }


@pytest.fixture
def converted():
    return convert_v15_snapshot(_snapshot(), code_revision=CODE_REVISION)


# ─────────────────────────────────────────────────────────────
# 기본 계약
# ─────────────────────────────────────────────────────────────

def test_변환_결과가_스키마를_통과한다(converted):
    assert len(converted.signals) == 4
    assert len(converted.features) == 4
    validate_rows(converted.signals, SIGNAL_COLUMNS, SIGNAL_PK, "gold.signals")


def test_모든_계약_열이_존재하고_추가_열은_없다(converted):
    expected = set(column_names(SIGNAL_COLUMNS))
    for row in converted.signals:
        assert set(row) == expected
    expected_f = set(column_names(SIGNAL_FEATURE_COLUMNS))
    for row in converted.features:
        assert set(row) == expected_f


def test_lineage_열이_채워진다(converted):
    for row in converted.signals:
        assert row["schema_version"] == SCHEMA_VERSION
        assert row["scanner_version"] == SCANNER_VERSION
        assert row["market_rule_version"] == MARKET_RULE_VERSION
        assert row["code_revision"] == CODE_REVISION
        assert row["config_hash"].startswith("sha256:")
        assert row["data_revision"] == 1


def test_signal_id는_유일하고_결정적이다(converted):
    ids = [s["signal_id"] for s in converted.signals]
    assert len(set(ids)) == len(ids)
    again = convert_v15_snapshot(_snapshot(), code_revision=CODE_REVISION)
    assert [s["signal_id"] for s in again.signals] == ids


def test_run_id가_달라도_signal_id는_같다():
    """같은 신호를 다시 수집해도 논리 ID는 바뀌지 않는다(아키텍처 §6)."""
    a = convert_v15_snapshot(_snapshot(), code_revision=CODE_REVISION,
                             run_id="import_20260916000000_11111111")
    b = convert_v15_snapshot(_snapshot(), code_revision=CODE_REVISION,
                             run_id="import_20260916000000_22222222")
    assert a.run_row["run_id"] != b.run_row["run_id"]
    assert [s["signal_id"] for s in a.signals] == [s["signal_id"] for s in b.signals]


def test_시각은_UTC로_저장하고_거래일은_KST로_남긴다(converted):
    s = converted.signals[0]
    assert s["signal_at_utc"].isoformat() == "2026-09-16T06:35:00+00:00"
    assert s["trading_date"].isoformat() == "2026-09-16"
    assert s["signal_time_local"] == "15:35:00"


def test_1535_신호는_경고를_만들지_않는다(converted):
    assert not any("신호 시각" in w for w in converted.warnings)


def test_1535가_아니면_경고한다():
    payload = _snapshot([_result("005930", last_bar="2026-09-16 12:55")])
    payload["as_of"] = "2026-09-16T12:56:43.430923"
    payload["last_input_bar_at"] = "2026-09-16 12:55"
    out = convert_v15_snapshot(payload, code_revision=CODE_REVISION)
    assert any("신호 시각" in w for w in out.warnings)


def test_유니버스_이력_부재를_항상_경고한다(converted):
    assert any("구성종목 이력" in w for w in converted.warnings)


# ─────────────────────────────────────────────────────────────
# 순위 규칙
# ─────────────────────────────────────────────────────────────

def test_순위는_성공_상태_종목만_모집단으로_한다(converted):
    by_code = {s["security_id"]: s for s in converted.signals}
    ranked = {c: s for c, s in by_code.items() if s["rank"] is not None}
    assert set(ranked) == {"KRX:005930", "KRX:000660", "KRX:035420"}
    # OBV FAILED 종목은 순위 모집단에서 빠진다.
    assert by_code["KRX:051910"]["rank"] is None
    assert by_code["KRX:051910"]["rank_percentile"] is None
    assert by_code["KRX:051910"]["feature_status"] == "FAILED"


def test_순위는_총점_내림차순(converted):
    ranked = sorted((s for s in converted.signals if s["rank"]),
                    key=lambda s: s["rank"])
    assert [s["security_id"] for s in ranked] == [
        "KRX:005930", "KRX:000660", "KRX:035420"]
    assert ranked[0]["rank_percentile"] == 1.0
    assert ranked[-1]["rank_percentile"] == 0.0
    assert all(s["rank_population"] == 3 for s in ranked)


def test_동점은_유동성_다음_종목코드로_가른다():
    results = [_result("000660", total=80.0, liquidity=1.0e10),
               _result("005930", total=80.0, liquidity=9.0e10)]
    out = convert_v15_snapshot(_snapshot(results), code_revision=CODE_REVISION)
    ranked = sorted(out.signals, key=lambda s: s["rank"])
    # 총점이 같으면 유동성이 큰 쪽이 앞선다.
    assert ranked[0]["security_id"] == "KRX:005930"


def test_모집단이_하나면_백분위는_중립():
    out = convert_v15_snapshot(_snapshot([_result("005930")]),
                               code_revision=CODE_REVISION)
    assert out.signals[0]["rank_percentile"] == 0.5


# ─────────────────────────────────────────────────────────────
# feature 매핑
# ─────────────────────────────────────────────────────────────

def test_feature_값이_V15_값을_그대로_옮긴다(converted):
    f = {x["signal_id"]: x for x in converted.features}
    s = converted.signals[0]
    feat = f[s["signal_id"]]
    assert feat["cvd_normalized_delta"] == 0.05
    assert feat["cvd_bull_ratio_completed"] == 0.7
    assert feat["obv_normalized_slope"] == 0.26
    assert feat["close_vs_ma20_pct"] == 2.43
    assert feat["avg_traded_value_20d"] == 1.0e11
    assert feat["rank_session"] == "REGULAR"


def test_V15가_기록하지_않는_값은_null로_남는다(converted):
    feat = converted.features[0]
    for col in ("close", "ma20", "ma60", "ma120", "ma20_slope"):
        assert feat[col] is None, f"{col}은 추정으로 채우면 안 된다"


def test_결측은_0으로_채우지_않는다():
    results = [_result("005930", liquidity=None, liq_status="FAILED")]
    out = convert_v15_snapshot(_snapshot(results), code_revision=CODE_REVISION)
    assert out.features[0]["avg_traded_value_20d"] is None
    assert out.features[0]["liquidity_data_status"] == "FAILED"


def test_CVD_가용시각은_마지막_봉_종료시각이다(converted):
    feat = converted.features[0]
    assert feat["cvd_max_available_at_utc"].isoformat() == \
        "2026-09-16T06:30:00+00:00"


# ─────────────────────────────────────────────────────────────
# 하드 페일
# ─────────────────────────────────────────────────────────────

def test_신호_시각_이후_봉은_거부한다():
    """15:35 신호에 15:40 봉이 섞이면 변환 자체가 실패해야 한다."""
    results = [_result("005930", last_bar="2026-09-16 15:40")]
    with pytest.raises(SchemaError):
        convert_v15_snapshot(_snapshot(results), code_revision=CODE_REVISION)


def test_중복_종목은_거부한다():
    results = [_result("005930"), _result("005930")]
    with pytest.raises(SchemaError):
        convert_v15_snapshot(_snapshot(results), code_revision=CODE_REVISION)


def test_code_revision_없이는_변환하지_않는다():
    with pytest.raises(ConfigError):
        convert_v15_snapshot(_snapshot(), code_revision="")


def test_알_수_없는_status는_거부한다():
    results = [_result("005930", status="MAYBE")]
    with pytest.raises(ValueError):
        convert_v15_snapshot(_snapshot(results), code_revision=CODE_REVISION)


def test_config에_토큰이_있으면_거부한다():
    payload = _snapshot()
    payload["config"]["access_token"] = "leaked-token-value"
    with pytest.raises(DataQualityError):
        convert_v15_snapshot(payload, code_revision=CODE_REVISION)


def test_결과에_비밀정보가_섞이지_않는다(converted):
    assert find_secrets(converted.run_row) == []
    assert find_secrets(converted.signals) == []


def test_lookahead_검사가_실제로_동작한다(converted):
    """검사기를 우회해 미래 가용시각을 넣으면 반드시 걸려야 한다."""
    from cvd_obv.contracts.schema import assert_no_lookahead

    features = copy.deepcopy(converted.features)
    features[0]["obv_max_available_at_utc"] = (
        converted.signals[0]["signal_at_utc"] + timedelta(minutes=1))
    with pytest.raises(LookAheadError):
        assert_no_lookahead(converted.signals, features)


# ─────────────────────────────────────────────────────────────
# 실제 스냅샷
# ─────────────────────────────────────────────────────────────

def _real_snapshots():
    """V1.5 버전을 선언한 스냅샷만 고른다.

    그 이전 스냅샷은 계산식을 알 수 없어 변환 대상이 아니다
    (`test_버전_없는_스냅샷은_거부한다` 참고).
    """
    pattern = os.path.join(ROOT, "reports", "daily-scan", "cvd-obv-*.json")
    out = []
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path, encoding="utf-8") as f:
                if json.load(f).get("scanner_version"):
                    out.append(path)
        except (OSError, ValueError):
            continue
    return out[:3]


def test_버전_없는_스냅샷은_거부한다():
    """V1.5 이전 형식에 기본 버전을 씌워 'V1.5 결과'로 만들지 않는다."""
    payload = _snapshot()
    del payload["scanner_version"]
    with pytest.raises(SchemaError):
        convert_v15_snapshot(payload, code_revision=CODE_REVISION)


@pytest.mark.parametrize("path", _real_snapshots() or [None])
def test_실제_스냅샷도_계약을_통과한다(path):
    if path is None:
        pytest.skip("reports/daily-scan에 V1.5 스냅샷이 없다")
    payload = load_v15_snapshot(path)
    out = convert_v15_snapshot(payload, code_revision=CODE_REVISION,
                               source_path=path)
    assert len(out.signals) == payload["count"]
    validate_rows(out.signals, SIGNAL_COLUMNS, SIGNAL_PK, "gold.signals")
    assert find_secrets(out.signals) == []
    # 상태 분포가 V1.5 원본과 일치한다(변환이 상태를 바꾸지 않는다).
    original = {}
    for r in payload["results"]:
        original[r["status"]] = original.get(r["status"], 0) + 1
    converted_counts = {}
    for s in out.signals:
        converted_counts[s["status"]] = converted_counts.get(s["status"], 0) + 1
    assert converted_counts == original


def test_스냅샷_config_hash_재계산이_일치한다():
    paths = _real_snapshots()
    if not paths:
        pytest.skip("reports/daily-scan에 V1.5 스냅샷이 없다")
    for path in paths:
        payload = load_v15_snapshot(path)
        out = convert_v15_snapshot(payload, code_revision=CODE_REVISION)
        assert not any("config_hash" in w for w in out.warnings), path

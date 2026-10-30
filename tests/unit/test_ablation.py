# -*- coding: utf-8 -*-
"""Ablation과 검정력 — 로드맵 Stage 8.

여기서 확인하는 핵심은 **"효과가 없다"와 "못 봤다"를 나누는가**다.

- 신뢰구간이 0을 포함한다고 "효과 없음"이라고 쓰면 안 된다. 관측값이
  MDE보다 작으면 애초에 이 표본으로 판정할 수 없었던 크기다.
- ablation 비교는 같은 날짜·같은 유니버스·같은 비용에서만 성립한다.
- BASE는 자기 자신과의 차이라 delta가 없다. 0으로 적으면 "차이 없음"을
  발견한 것처럼 보인다.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from cvd_obv.contracts.ablation_schema import ABLATION_RESULT_PK, EDGE_VERDICTS
from cvd_obv.contracts.label_schema import (
    LABEL_VERSION, SIGNAL_FORWARD_RETURN_COLUMNS, SIGNAL_FORWARD_RETURN_PK,
)
from cvd_obv.contracts.schema import (
    SIGNAL_COLUMNS, SIGNAL_FEATURE_COLUMNS, SIGNAL_FEATURE_PK, SIGNAL_PK,
)
from cvd_obv.domain.errors import ConfigError
from cvd_obv.domain.status import LabelStatus
from cvd_obv.statistics.ablation import TOP_N, VARIANTS, AblationRunner
from cvd_obv.statistics.power import (
    ALPHA, POWER, minimum_detectable_effect, required_observations,
    verdict_for, z_quantile,
)
from cvd_obv.statistics.score_model import ScoreModel
from cvd_obv.storage.parquet_store import ParquetStore

RUN_ID = "test_20260919000000_dddddddd"
MARKET_RULE = "KRX-2026-09-14-aftermarket"
SECURITIES = [f"KRX:00000{i}" for i in range(1, 9)]


def _days(count, start=date(2026, 1, 5)):
    out, day = [], start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


DAYS = _days(60)


def _at(day, hour=6):
    return datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────
# 검정력
# ─────────────────────────────────────────────────────────────

def test_정규분위수가_교과서_값과_같다():
    assert z_quantile(0.975) == pytest.approx(1.959964, abs=1e-5)
    assert z_quantile(0.80) == pytest.approx(0.841621, abs=1e-5)
    assert z_quantile(0.5) == pytest.approx(0.0, abs=1e-9)


def test_MDE는_SE의_약_2_8배다():
    """(1.96 + 0.84) x SE."""
    assert minimum_detectable_effect(1.0) == pytest.approx(2.8016, abs=1e-3)
    assert minimum_detectable_effect(0.01) == pytest.approx(0.028016, abs=1e-5)


def test_SE를_모르면_MDE도_없다():
    """모르는 값을 만들어내지 않는다."""
    assert minimum_detectable_effect(None) is None
    assert minimum_detectable_effect(0.0) is None


def test_필요_관측수는_제곱으로_늘어난다():
    """SE ∝ 1/sqrt(n)이므로 MDE를 절반으로 줄이려면 관측이 4배 필요하다."""
    assert required_observations(0.005, 0.010, 252) == 252 * 4
    assert required_observations(0.001, 0.010, 252) == 252 * 100


def test_이미_잡을_수_있는_크기면_더_필요하지_않다():
    assert required_observations(0.05, 0.01, 252) == 252


def test_관측치나_MDE가_없으면_필요_관측수도_없다():
    assert required_observations(None, 0.01, 252) is None
    assert required_observations(0.01, None, 252) is None
    assert required_observations(0.0, 0.01, 252) is None


def test_구간이_0을_비키면_유의다():
    assert verdict_for(0.05, 0.01, 0.09, 0.028) == "SIGNIFICANT_EDGE"


def test_관측값이_MDE보다_작으면_UNDERPOWERED다():
    """'효과 없음'이라고 쓰면 하지 않은 말을 한 것이 된다."""
    assert verdict_for(0.005, -0.02, 0.03, 0.028) == "UNDERPOWERED"


def test_관측값이_MDE보다_큰데_유의하지_않으면_효과없음이다():
    assert verdict_for(0.05, -0.02, 0.12, 0.028) == "NO_SIGNIFICANT_EDGE"


def test_추정할_수_없으면_NOT_EVALUABLE이다():
    assert verdict_for(None, None, None, None) == "NOT_EVALUABLE"


def test_판정값이_계약_안에_있다():
    for value in ("SIGNIFICANT_EDGE", "NO_SIGNIFICANT_EDGE", "UNDERPOWERED",
                  "NOT_EVALUABLE"):
        assert value in EDGE_VERDICTS


def test_기본_유의수준과_검정력이_관례값이다():
    assert (ALPHA, POWER) == (0.05, 0.80)


# ─────────────────────────────────────────────────────────────
# 합성 저장소
# ─────────────────────────────────────────────────────────────

def _signal(sid, day, status, score):
    return {
        "signal_id": f"s_{sid[-6:]}_{day:%Y%m%d}",
        "signal_source": "REPLAY", "signal_at_utc": _at(day),
        "signal_time_local": "15:35:00", "trading_date": day,
        "security_id": sid, "index_id": "KOSPI200",
        "universe_snapshot_id": "snap", "status": status,
        "gate_passed": status == "BUY_CANDIDATE", "gate_reasons": [],
        "total_score": score, "rank": SECURITIES.index(sid) + 1,
        "rank_population": len(SECURITIES),
        "rank_percentile": (SECURITIES.index(sid) + 1) / len(SECURITIES),
        "feature_status": "SUCCESS",
        "daily_evidence_tier": "FULL_SIGNAL_SHORT",
        "schema_version": "2.0.0", "created_at_utc": _at(day),
        "run_id": RUN_ID, "config_hash": "h", "code_revision": "r",
        "scanner_version": "1.5.0", "market_rule_version": MARKET_RULE,
        "data_revision": 1,
    }


def _feature(sid, day, idx):
    """종목마다 다른 강도. 앞 종목일수록 CVD·OBV·가격이 좋다."""
    strong = 1.0 - idx / len(SECURITIES)
    row = {name.name: None for name in SIGNAL_FEATURE_COLUMNS}
    row.update({
        "signal_id": f"s_{sid[-6:]}_{day:%Y%m%d}",
        "schema_version": "2.0.0", "run_id": RUN_ID, "data_revision": 1,
        "rank_session": "REGULAR",
        "cvd_normalized_delta": strong, "cvd_normalized_momentum": strong,
        "cvd_bull_ratio_completed": 0.4 + strong * 0.4,
        "cvd_momentum_status": "OK", "cvd_data_status": "SUCCESS",
        "cvd_regular_data_status": "SUCCESS",
        "cvd_after_data_status": "SUCCESS", "cvd_stale": False,
        "obv": 100.0 + strong * 20, "obv_ma20": 100.0,
        "obv_normalized_slope": strong - 0.1, "obv_data_status": "SUCCESS",
        "above_ma20": idx < 6, "above_ma60": True, "ma20_rising": True,
        "ma20_above_ma60": True, "price_data_status": "SUCCESS",
        "avg_traded_value_20d": 1.0e9 * (idx + 1),
        "liquidity_data_status": "SUCCESS", "liquidity_days_used": 20,
        "risk_penalty": 0.0, "risk_data_status": "SUCCESS",
        "obv_divergence": "NONE", "obv_pivot_divergence": "NONE",
        "cvd_momentum_status": "OK",
    })
    return row


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    st = ParquetStore(root=str(tmp_path_factory.mktemp("abl")))
    signals, features, labels = [], [], []
    model = ScoreModel()

    for day in DAYS:
        rows = [_feature(sid, day, i) for i, sid in enumerate(SECURITIES)]
        scores = model.score_day(rows)
        for i, (sid, row) in enumerate(zip(SECURITIES, rows)):
            status, _ = model.gate_row(row)
            signals.append(_signal(sid, day, status, scores[i]))
            features.append(row)
            # 강한 종목일수록 수익이 높게 — ablation이 방향을 잡는지 본다.
            # 날짜마다 흔들리게 두지 않으면 분산이 0이라 신뢰구간도 0이 되고,
            # 검정력 계산이 아무것도 검사하지 못한다.
            wobble = ((DAYS.index(day) * 37 + i * 11) % 23 - 11) * 0.004
            net = 0.02 - i * 0.005 + wobble
            labels.append({
                "signal_id": row["signal_id"], "horizon_td": 20,
                "entry_rule": "NEXT_OPEN", "security_id": sid,
                "signal_date": day, "signal_at_utc": _at(day),
                "entry_date": day, "exit_date": day,
                "entry_at_utc": _at(day, 7), "exit_at_utc": _at(day, 8),
                "entry_price": Decimal("100"), "exit_price": Decimal("101"),
                "stock_return_gross": net + 0.004, "stock_return_net": net,
                "benchmark_id": "KRX:KOSPI200", "benchmark_return": 0.001,
                "excess_return_gross": net + 0.003,
                "excess_return_net": net - 0.001,
                "mfe": 0.03, "mae": -0.01,
                "up": net > 0, "up_gross": net > 0, "outperform": net > 0.001,
                "hit_3pct": False, "hit_5pct": False,
                "hit_minus_3pct": False, "hit_minus_5pct": False,
                "commission": 0.00015, "tax": 0.0018, "slippage": 0.001,
                "label_status": LabelStatus.COMPLETE, "status_reason": None,
                "calendar_checked": True,
                "label_source": "gold.daily_forward_returns",
                "label_version": LABEL_VERSION, "cost_model_version": "krx_v1",
                "market_rule_version": MARKET_RULE, "run_id": RUN_ID,
                "schema_version": "2.0.0", "data_revision": 1,
            })

    st.write("gold", "signals", signals, SIGNAL_COLUMNS, SIGNAL_PK, RUN_ID,
             partition_fn=lambda r: [("index_id", r["index_id"])])
    st.write("gold", "signal_features", features, SIGNAL_FEATURE_COLUMNS,
             SIGNAL_FEATURE_PK, RUN_ID)
    st.write("gold", "forward_returns", labels, SIGNAL_FORWARD_RETURN_COLUMNS,
             SIGNAL_FORWARD_RETURN_PK, RUN_ID,
             partition_fn=lambda r: [("horizon_td", r["horizon_td"])])
    return st


@pytest.fixture(scope="module")
def result(store):
    return AblationRunner(store, horizon_td=20, n_resamples=200).run(
        run_id=RUN_ID, dry_run=True)


def _find(rows, variant, metric):
    for row in rows:
        if row["variant_id"] == variant and row["metric_name"] == metric:
            return row
    raise AssertionError(f"결과 없음: {variant} / {metric}")


# ─────────────────────────────────────────────────────────────
# 되짚기 검증이 먼저다
# ─────────────────────────────────────────────────────────────

def test_실행_전에_되짚기를_검증한다(result):
    check = result["verification"]
    assert check["score"]["mismatches"] == 0
    assert check["gate"]["mismatches"] == 0
    assert check["score"]["rows"] > 0


# ─────────────────────────────────────────────────────────────
# 공정한 비교
# ─────────────────────────────────────────────────────────────

def test_모든_변형이_같은_날짜에서_비교된다(result):
    counts = {row["paired_dates"] for row in result["rows"]
              if row["status"] == "SUCCESS"}
    assert len(counts) == 1, f"변형마다 날짜 수가 다르다: {counts}"


def test_BASE는_자기_자신과의_차이라_delta가_없다(result):
    for row in result["rows"]:
        if row["variant_id"] == "BASE":
            assert row["delta"] is None
            assert row["status_reason"]


def test_변형마다_제거한_성분이_기록된다(result):
    for row in result["rows"]:
        assert row["removed_components"] == list(VARIANTS[row["variant_id"]])


def test_추론_설정이_행마다_남는다(result):
    """CI 방식·cluster key·block length·seed·반복 횟수 (Stage 8 DoD)."""
    for row in result["rows"]:
        if row["status"] != "SUCCESS":
            continue
        assert row["inference_method"]
        assert row["cluster_key"] == "signal_date"
        assert row["block_length"] >= 20
        assert row["seed"] == 20260919
        assert row["ci_level"] == 0.95


# ─────────────────────────────────────────────────────────────
# 판정
# ─────────────────────────────────────────────────────────────

def test_모든_행에_판정이_붙는다(result):
    for row in result["rows"]:
        assert row["edge_verdict"] in EDGE_VERDICTS


def test_MDE와_관측치의_비가_기록된다(result):
    row = _find(result["rows"], "BASE", "TOP20_mean_return_net")
    assert row["mde_80"] is not None
    assert row["observed_over_mde"] == pytest.approx(
        abs(row["variant_value"]) / row["mde_80"], rel=1e-9)


def test_얼마나_더_필요한지도_기록된다(result):
    """'효과가 없다'로 끝내지 않고 '얼마나 더 모으면 되나'까지 답한다."""
    for row in result["rows"]:
        if row["status"] != "SUCCESS" or not row["mde_80"]:
            continue
        need = row["required_dates_80"]
        assert need is None or need >= row["paired_dates"]


def test_가격을_빼면_선택이_나빠진다(result):
    """합성 데이터는 강한 종목일수록 수익이 높다 — 가격 조건을 빼면
    MA20 아래 종목이 후보에 들어와 평균이 떨어져야 한다."""
    base = _find(result["rows"], "BASE", "BUY_mean_return_net")
    without = _find(result["rows"], "NO_PRICE", "BUY_mean_return_net")
    assert without["variant_value"] < base["variant_value"]
    assert without["delta"] < 0


def test_Gate를_빼면_거의_전부를_고른다(result):
    base = _find(result["rows"], "BASE", "BUY_mean_return_net")
    without = _find(result["rows"], "NO_GATE", "BUY_mean_return_net")
    assert without["mean_selection_size"] > base["mean_selection_size"]


def test_TOP은_Gate_통과분_중에서_고른다(result):
    """Gate를 무시하면 모든 변형의 TOP이 같아져 Gate의 몫을 잴 수 없다."""
    base = _find(result["rows"], "BASE", "TOP20_mean_return_net")
    without = _find(result["rows"], "NO_GATE", "TOP20_mean_return_net")
    assert base["mean_selection_size"] <= TOP_N
    assert without["mean_selection_size"] != base["mean_selection_size"]


# ─────────────────────────────────────────────────────────────
# 저장
# ─────────────────────────────────────────────────────────────

def test_계약에_없는_열은_쓰지_않는다(store):
    runner = AblationRunner(store, horizon_td=20, n_resamples=100)
    got = runner.run(run_id=RUN_ID + "w", dry_run=False)
    rows = store.read("gold", "ablation_results", pk=ABLATION_RESULT_PK)
    assert rows
    keys = [tuple(r[k] for k in ABLATION_RESULT_PK) for r in rows]
    assert len(keys) == len(set(keys))
    assert got["artifacts"]


def test_알_수_없는_변형은_거부한다(store):
    with pytest.raises(ConfigError):
        AblationRunner(store, horizon_td=20).run(variants=["NOPE"],
                                                 dry_run=True)


def test_BASE는_지정하지_않아도_들어간다(store):
    got = AblationRunner(store, horizon_td=20, n_resamples=100).run(
        variants=["NO_CVD"], dry_run=True)
    assert got["variants"][0] == "BASE"

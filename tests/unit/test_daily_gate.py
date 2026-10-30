# -*- coding: utf-8 -*-
"""Daily Gate 백테스터 — 조건 결합과 짝지은 비교.

여기서 확인하는 핵심은 **비교가 공정한가**다.
- 조건을 만족한 날과 그렇지 않은 날은 시장 상황이 다르다. 날짜를 짝지어
  비교하지 않으면 조건의 효과와 그날의 시장이 섞인다.
- 판정 불가(None)인 행은 어느 쪽에도 넣지 않는다. 한쪽에만 넣으면 표본이
  달라져 차이가 조건 때문인지 결측 때문인지 알 수 없게 된다.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from cvd_obv.backtest.daily_gate import (
    PREREGISTERED, VARIANTS, DailyGateBacktester, DailyGateSpec,
)
from cvd_obv.contracts.feature_schema import (
    DAILY_FEATURE_COLUMNS, DAILY_FEATURE_PK, feature_version,
)
from cvd_obv.contracts.label_schema import (
    DAILY_FORWARD_RETURN_COLUMNS, DAILY_FORWARD_RETURN_PK, LABEL_VERSION,
)
from cvd_obv.domain.errors import NoInputDataError
from cvd_obv.domain.status import EvidenceTier, LabelStatus
from cvd_obv.storage.parquet_store import ParquetStore

RUN_ID = "test_20260918000000_aaaaaaaa"
FV = feature_version(20)
SECURITIES = [f"KRX:00000{i}" for i in range(1, 6)]


def _days(count, start=date(2020, 1, 6)):
    out, day = [], start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def _feature_row(sid, day, price, obv, liq):
    from datetime import datetime, timezone
    gate = (None if (price is None or obv is None)
            else bool(price and obv and liq))
    return {
        "security_id": sid, "trading_date": day, "feature_version": FV,
        "close": 100.0, "ma20": 99.0, "ma60": 98.0, "ma120": 97.0,
        "ma20_prev5": 98.5,
        "above_ma20": price, "above_ma60": True, "above_ma120": True,
        "ma20_rising": True, "ma60_rising": True, "ma20_above_ma60": True,
        "close_vs_ma20_pct": 1.0, "price_data_status": "SUCCESS",
        "obv": 1000.0, "obv_ma20": 900.0, "obv_above_ma20": True,
        "obv_slope": 10.0, "obv_window_volume": 100.0,
        "obv_normalized_slope": 0.1, "obv_up_ratio": 0.6, "obv_lookback": 20,
        "obv_data_status": "SUCCESS",
        "avg_traded_value_20d": (1.0e9 if liq else None),
        "liquidity_days_used": 20, "liquidity_data_status": "SUCCESS",
        "cond_price": price, "cond_obv": obv, "cond_liquidity": liq,
        "gate_all": gate, "feature_status": "SUCCESS",
        "available_at_utc": datetime(day.year, day.month, day.day, 6, 35,
                                     tzinfo=timezone.utc),
        "market_rule_version": "KRX-2016-08-01-regular1530",
        "run_id": RUN_ID, "schema_version": "2.0.0", "data_revision": 1,
    }


def _label_row(sid, day, horizon, up, net):
    from datetime import datetime, timezone
    return {
        "security_id": sid, "signal_date": day, "horizon_td": horizon,
        "entry_rule": "NEXT_OPEN",
        "entry_date": day, "exit_date": day,
        "entry_at_utc": datetime(day.year, day.month, day.day,
                                 tzinfo=timezone.utc),
        "exit_at_utc": datetime(day.year, day.month, day.day,
                                tzinfo=timezone.utc),
        "entry_price": Decimal("100"), "exit_price": Decimal("101"),
        "stock_return_gross": net + 0.004, "stock_return_net": net,
        "benchmark_id": "KRX:KOSPI200", "benchmark_return": 0.001,
        "excess_return_gross": net + 0.003, "excess_return_net": net - 0.001,
        "mfe": 0.02, "mae": -0.02,
        "up": up, "up_gross": up, "outperform": up,
        "hit_3pct": False, "hit_5pct": False,
        "hit_minus_3pct": False, "hit_minus_5pct": False,
        "commission": 0.00015, "tax": 0.0018, "slippage": 0.001,
        "label_status": LabelStatus.COMPLETE, "status_reason": None,
        "label_version": LABEL_VERSION, "cost_model_version": "krx_v1",
        "market_rule_version": "KRX-2016-08-01-regular1530",
        "run_id": RUN_ID, "schema_version": "2.0.0", "data_revision": 1,
    }


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    """조건이 켜진 날 상승확률이 확실히 높은 합성 패널."""
    root = tmp_path_factory.mktemp("gate")
    st = ParquetStore(root=str(root))
    days = _days(300)

    features, labels = [], []
    for d_idx, day in enumerate(days):
        for s_idx, sid in enumerate(SECURITIES):
            price = ((d_idx + s_idx) % 2 == 0)
            obv = ((d_idx + s_idx) % 3 != 0)
            liq = True
            features.append(_feature_row(sid, day, price, obv, liq))
            # 두 조건이 모두 켜지면 상승확률 70%, 아니면 40%
            favourable = price and obv
            up = ((d_idx * 7 + s_idx * 3) % 10) < (7 if favourable else 4)
            for horizon in (1, 5):
                labels.append(_label_row(sid, day, horizon, up,
                                         0.01 if up else -0.01))

    st.write("gold", "daily_features", features, DAILY_FEATURE_COLUMNS,
             DAILY_FEATURE_PK, RUN_ID,
             partition_fn=lambda r: [("feature_version", r["feature_version"]),
                                     ("year", r["trading_date"].year)])
    st.write("gold", "daily_forward_returns", labels,
             DAILY_FORWARD_RETURN_COLUMNS, DAILY_FORWARD_RETURN_PK, RUN_ID,
             partition_fn=lambda r: [("horizon_td", r["horizon_td"]),
                                     ("signal_year", r["signal_date"].year)])
    return st


@pytest.fixture(scope="module")
def results(store):
    spec = DailyGateSpec(horizons=(1, 5), outcomes=("up",), n_resamples=200,
                         feature_version=FV)
    return DailyGateBacktester(store).evaluate(spec, run_id=RUN_ID)


def _find(rows, variant, horizon, outcome="up"):
    for row in rows:
        if (row["variant"] == variant and row["horizon_td"] == horizon
                and row["outcome"] == outcome):
            return row
    raise AssertionError(f"결과 없음: {variant} h={horizon}")


# ─────────────────────────────────────────────────────────────
# 조건 결합
# ─────────────────────────────────────────────────────────────

def test_모든_조합에_결과가_나온다(results):
    got = {(r["variant"], r["horizon_td"]) for r in results}
    assert got == {(v, h) for v in VARIANTS for h in (1, 5)}


def test_전체_유니버스는_선택률_100퍼센트(results):
    assert _find(results, "ALL", 1)["coverage"] == pytest.approx(1.0)


def test_조건을_더하면_선택률이_줄어든다(results):
    all_cov = _find(results, "ALL", 1)["coverage"]
    price_cov = _find(results, "PRICE", 1)["coverage"]
    both_cov = _find(results, "PRICE+OBV", 1)["coverage"]
    gate_cov = _find(results, "GATE_ALL", 1)["coverage"]
    assert all_cov > price_cov > both_cov
    assert gate_cov == pytest.approx(both_cov)   # 유동성은 항상 참인 합성자료


def test_조건이_켜진_표본의_상승확률이_더_높다(results):
    """합성자료에서 의도한 신호를 실제로 잡아내는지."""
    base = _find(results, "ALL", 1)["probability"]
    gated = _find(results, "PRICE+OBV", 1)["probability"]
    assert gated > base + 0.05


# ─────────────────────────────────────────────────────────────
# 짝지은 비교
# ─────────────────────────────────────────────────────────────

def test_기준선에는_차이가_없다(results):
    row = _find(results, "ALL", 1)
    assert row.get("delta_vs_all") is None


def test_차이는_같은_날짜끼리_짝지어_계산한다(results):
    row = _find(results, "PRICE+OBV", 1)
    assert row["delta_vs_all"] is not None
    assert row["delta_dates_n"] > 200
    # 확률 차이와 부호·크기가 대체로 맞아야 한다.
    naive_gap = row["probability"] - _find(results, "ALL", 1)["probability"]
    assert row["delta_vs_all"] == pytest.approx(naive_gap, abs=0.05)
    assert row["delta_ci_low"] < row["delta_vs_all"] < row["delta_ci_high"]


def test_유의한_차이는_0을_포함하지_않는다(results):
    row = _find(results, "PRICE+OBV", 1)
    assert row["delta_ci_low"] > 0


# ─────────────────────────────────────────────────────────────
# 판정 불가 처리
# ─────────────────────────────────────────────────────────────

def test_판정_불가_행은_양쪽_모두에서_빠진다(tmp_path):
    """한쪽에만 남기면 표본이 달라져 비교가 무의미해진다."""
    st = ParquetStore(root=str(tmp_path))
    days = _days(60)
    features, labels = [], []
    for d_idx, day in enumerate(days):
        for s_idx, sid in enumerate(SECURITIES[:2]):
            # 앞 20일은 워밍업 구간처럼 판정 불가로 둔다.
            unknown = d_idx < 20
            price = None if unknown else True
            obv = None if unknown else (s_idx == 0)
            features.append(_feature_row(sid, day, price, obv, True))
            labels.append(_label_row(sid, day, 1, s_idx == 0, 0.01))

    st.write("gold", "daily_features", features, DAILY_FEATURE_COLUMNS,
             DAILY_FEATURE_PK, RUN_ID,
             partition_fn=lambda r: [("feature_version", r["feature_version"]),
                                     ("year", r["trading_date"].year)])
    st.write("gold", "daily_forward_returns", labels,
             DAILY_FORWARD_RETURN_COLUMNS, DAILY_FORWARD_RETURN_PK, RUN_ID,
             partition_fn=lambda r: [("horizon_td", r["horizon_td"]),
                                     ("signal_year", r["signal_date"].year)])

    spec = DailyGateSpec(variants=["ALL", "OBV"], horizons=(1,),
                         outcomes=("up",), n_resamples=100, feature_version=FV)
    rows = DailyGateBacktester(st).evaluate(spec, run_id=RUN_ID)
    all_row = _find(rows, "ALL", 1)
    # 워밍업 20일 × 2종목 = 40행이 기준선에서도 빠져야 한다.
    assert all_row["raw_n"] == (60 - 20) * 2
    assert all_row["coverage"] == pytest.approx(1.0)


# ─────────────────────────────────────────────────────────────
# 계약
# ─────────────────────────────────────────────────────────────

def test_증거_계층이_일봉_전용으로_표시된다(results):
    assert all(r["evidence_tier"] == EvidenceTier.DAILY_ONLY_LONG
               for r in results)


def test_사전등록_여부가_표시된다(results):
    for row in results:
        assert row["preregistered"] is (row["variant"] in PREREGISTERED)
    exploratory = {r["variant"] for r in results if not r["preregistered"]}
    assert exploratory == {"PRICE+OBV", "PRICE+LIQ", "OBV+LIQ"}


def test_신뢰구간과_산출_방법을_함께_남긴다(results):
    for row in results:
        assert row["ci_method"] in ("CLUSTER_ROBUST_CR1",
                                    "MOVING_BLOCK_BOOTSTRAP",
                                    "NAIVE_IID_FLOOR")
        assert row["ci_low"] < row["probability"] < row["ci_high"]
        assert row["effective_n"] <= row["raw_n"]
        assert row["cluster_key"] == "signal_date"
        assert row["block_length"] >= 2
        # 독립 가정 CI보다 좁게 보고하지 않는다. 군집 내 상관이 음수면 군집
        # CI가 더 좁아질 수 있는데, 그 이득은 취하지 않는다.
        naive_width = row["naive_ci_high"] - row["naive_ci_low"]
        assert (row["ci_high"] - row["ci_low"]) >= naive_width * 0.999


def test_같은_정의는_같은_결과_ID를_갖는다(store):
    spec = DailyGateSpec(variants=["GATE_ALL"], horizons=(1,),
                         outcomes=("up",), n_resamples=100, feature_version=FV)
    a = DailyGateBacktester(store).evaluate(spec, run_id="run_a")
    b = DailyGateBacktester(store).evaluate(spec, run_id="run_b")
    assert a[0]["result_id"] == b[0]["result_id"]
    assert a[0]["probability"] == b[0]["probability"]


def test_정의가_다르면_결과_ID도_다르다(store):
    backtester = DailyGateBacktester(store)
    one = backtester.evaluate(
        DailyGateSpec(variants=["GATE_ALL"], horizons=(1,), outcomes=("up",),
                      n_resamples=100, feature_version=FV))
    five = backtester.evaluate(
        DailyGateSpec(variants=["GATE_ALL"], horizons=(5,), outcomes=("up",),
                      n_resamples=100, feature_version=FV))
    assert one[0]["result_id"] != five[0]["result_id"]


def test_label이_없는_horizon은_거부한다(store):
    spec = DailyGateSpec(variants=["ALL"], horizons=(99,), outcomes=("up",),
                         n_resamples=50, feature_version=FV)
    with pytest.raises(NoInputDataError):
        DailyGateBacktester(store).evaluate(spec)


def test_짝지은_기준선이_표의_ALL과_같은_모집단이다(store):
    """조건 판정이 불가능한 행을 기준선에만 넣으면 차이가 오염된다.

    선택률 100%인 조합(모든 행을 고르는 조건)의 차이는 **정확히 0**이어야
    한다. 0이 아니면 두 쪽이 서로 다른 표본을 보고 있다는 뜻이다.
    """
    spec = DailyGateSpec(variants=["ALL", "LIQUIDITY"], horizons=(1,),
                         outcomes=("up",), n_resamples=100, feature_version=FV)
    rows = DailyGateBacktester(store).evaluate(spec, run_id=RUN_ID)
    liq = _find(rows, "LIQUIDITY", 1)
    all_row = _find(rows, "ALL", 1)

    assert liq["coverage"] == pytest.approx(1.0)
    assert liq["raw_n"] == all_row["raw_n"]
    assert liq["probability"] == pytest.approx(all_row["probability"])
    assert liq["delta_vs_all"] == pytest.approx(0.0, abs=1e-12)


def test_유니버스_필터는_그날_구성종목만_남긴다():
    """모집단이 '한 번이라도 지수에 있었던 종목'이면 그것도 생존편향이다.

    편출된 뒤의 수익률까지 통계에 들어가면, 그 종목이 왜 빠졌는지(부진해서)가
    결과에 섞인다. 그날 실제 구성종목만 봐야 한다.
    """
    from datetime import date

    from cvd_obv.backtest.daily_gate import _membership_mask

    intervals = {
        "KRX:000830": [(date(2010, 1, 4), date(2015, 9, 1))],   # 합병으로 편출
        "KRX:005930": [(date(2010, 1, 4), None)],               # 계속 구성종목
        "KRX:111111": [(date(2012, 1, 2), date(2013, 1, 2)),
                       (date(2014, 1, 2), None)],               # 편출 후 재편입
    }
    securities = ["KRX:000830", "KRX:000830", "KRX:005930",
                  "KRX:111111", "KRX:111111", "KRX:111111", "KRX:999999"]
    days = [date(2015, 8, 31), date(2015, 9, 1), date(2020, 5, 4),
            date(2012, 6, 1), date(2013, 6, 1), date(2014, 6, 1),
            date(2020, 5, 4)]
    mask = _membership_mask(securities, days, intervals)
    assert list(mask) == [True, False, True, True, False, True, False]


def test_회전율은_전날에_없던_비율의_평균이다():
    """선택률이 '얼마나 많이 고르나'라면 회전율은 '얼마나 자주 바꾸나'다."""
    from datetime import date

    from cvd_obv.backtest.daily_gate import _turnover

    days = [date(2026, 3, 2)] * 2 + [date(2026, 3, 3)] * 2
    securities = ["A", "B", "B", "C"]
    # 3/3 선택 {B, C} 중 전날에 없던 것은 C 하나 → 0.5
    assert _turnover(securities, days) == pytest.approx(0.5)
    # 하루뿐이면 비교 대상이 없다.
    assert _turnover(["A"], [date(2026, 3, 2)]) is None


def test_비용_배수는_순수익을_정확히_다시_만든다():
    """라벨의 순수익은 가격에 비용을 곱해 만든 값이라 역산이 정확하다."""
    from decimal import Decimal

    from cvd_obv.labels.costs import CostModel

    costs = CostModel("t", commission_rate=0.00015,
                      tax_rate_sell=0.0018, slippage_rate=0.001)
    entry, exit_ = Decimal("10000"), Decimal("11000")
    gross = float(exit_ / entry - 1)
    expected = costs.net_return(entry, exit_)

    comm = float(costs.commission_rate)
    tax = float(costs.tax_rate_sell)
    slip = float(costs.slippage_rate)
    factor = ((1 - slip) * (1 - comm - tax)) / ((1 + slip) * (1 + comm))
    assert (1 + gross) * factor - 1 == pytest.approx(expected, abs=1e-12)


def test_FDR_보정은_q값을_단조롭게_만든다():
    """p가 작을수록 q도 작아야 한다 — 그렇지 않으면 순위가 뒤집힌다."""
    from cvd_obv.backtest.daily_gate import _apply_fdr

    def row(value, half_width):
        return {"delta_return_net": value,
                "delta_return_ci_low": value - half_width,
                "delta_return_ci_high": value + half_width}

    rows = [row(0.001, 0.0002), row(0.0001, 0.0009), row(0.0005, 0.0003)]
    _apply_fdr(rows)
    ordered = sorted(rows, key=lambda r: r["delta_return_net_p_value"])
    qs = [r["delta_return_net_q_value"] for r in ordered]
    assert qs == sorted(qs)
    assert all(r["p_value_method"] == "NORMAL_APPROX_FROM_CI" for r in rows)
    assert all(r["fdr_tests"] == 3 for r in rows)


# ─────────────────────────────────────────────────────────────
# 중첩 비교 (사전 등록 C)
# ─────────────────────────────────────────────────────────────

def test_기준_조합을_바꾸면_짝지음_대상이_바뀐다(store):
    """'OBV가 가격 위에 더하는 것이 있는가'는 ALL 대비로는 물을 수 없다."""
    common = dict(horizons=(5,), outcomes=("up",), n_resamples=200,
                  feature_version=FV, variants=["ALL", "PRICE", "PRICE+OBV"])
    vs_all = DailyGateBacktester(store).evaluate(
        DailyGateSpec(base_variant="ALL", **common), run_id=RUN_ID)
    vs_price = DailyGateBacktester(store).evaluate(
        DailyGateSpec(base_variant="PRICE", **common), run_id=RUN_ID)

    a = _find(vs_all, "PRICE+OBV", 5)
    b = _find(vs_price, "PRICE+OBV", 5)
    assert a["base_variant"] == "ALL" and b["base_variant"] == "PRICE"
    # 확률 자체는 같은 집합이므로 같고, 기준이 다르니 차이는 달라야 한다.
    assert a["probability"] == b["probability"]
    assert a["delta_vs_all"] != b["delta_vs_all"]


def test_기준_조합_자신은_차이가_없다(store):
    rows = DailyGateBacktester(store).evaluate(
        DailyGateSpec(horizons=(5,), outcomes=("up",), n_resamples=200,
                      feature_version=FV, variants=["ALL", "PRICE", "OBV"],
                      base_variant="PRICE"), run_id=RUN_ID)
    assert _find(rows, "PRICE", 5).get("delta_vs_all") is None
    assert _find(rows, "ALL", 5).get("delta_vs_all") is not None


def test_알_수_없는_기준_조합은_거부한다():
    from cvd_obv.domain.errors import ConfigError

    with pytest.raises(ConfigError):
        DailyGateSpec(base_variant="NOPE")


def test_기준_조합이_result_id에_들어간다(store):
    """기준이 다르면 다른 실험이다 — 같은 result_id가 나오면 안 된다."""
    common = dict(horizons=(5,), outcomes=("up",), n_resamples=100,
                  feature_version=FV, variants=["ALL", "PRICE", "PRICE+OBV"])
    vs_all = DailyGateBacktester(store).evaluate(
        DailyGateSpec(base_variant="ALL", **common), run_id=RUN_ID)
    vs_price = DailyGateBacktester(store).evaluate(
        DailyGateSpec(base_variant="PRICE", **common), run_id=RUN_ID)
    assert (_find(vs_all, "PRICE+OBV", 5)["result_id"]
            != _find(vs_price, "PRICE+OBV", 5)["result_id"])

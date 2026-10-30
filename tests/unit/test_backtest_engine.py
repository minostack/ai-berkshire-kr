# -*- coding: utf-8 -*-
"""포트폴리오 백테스트 엔진 — 로드맵 Stage 7.

여기서 확인하는 핵심은 **돈이 생기거나 사라지지 않는가**다.

- 거래 원장의 합이 자산 변화와 맞아야 한다. 안 맞으면 수익률 전부가 의심스럽다.
- 신호 시각 이전에 체결된 거래가 있으면 미래를 본 것이다.
- 살 수 없었던 신호는 조용히 사라지지 않고 사유와 함께 남는다.
- 진입일이 몰리면 분산된 포트폴리오가 아니라 h일마다 한 번 베팅하는
  단일 코호트다. 기본값이 그렇게 되지 않는지 본다.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from cvd_obv.backtest.engine import (
    SELECTION_POLICIES, BacktestEngine, BacktestSpec,
)
from cvd_obv.backtest.metrics import BacktestMetrics
from cvd_obv.contracts.backtest_schema import (
    assert_execution_after_signal, assert_ledger_reconciles,
)
from cvd_obv.contracts.feature_schema import (
    DAILY_FEATURE_COLUMNS, DAILY_FEATURE_PK, feature_version,
)
from cvd_obv.contracts.label_schema import (
    LABEL_VERSION, SIGNAL_FORWARD_RETURN_COLUMNS, SIGNAL_FORWARD_RETURN_PK,
)
from cvd_obv.contracts.market_schema import (
    BENCHMARK_BAR_COLUMNS, BENCHMARK_BAR_PK, DAILY_BAR_COLUMNS, DAILY_BAR_PK,
)
from cvd_obv.contracts.schema import SIGNAL_COLUMNS, SIGNAL_PK
from cvd_obv.domain.errors import (
    ConfigError, DataQualityError, LookAheadError,
)
from cvd_obv.domain.status import LabelStatus
from cvd_obv.labels.costs import CostModel
from cvd_obv.storage.parquet_store import ParquetStore

RUN_ID = "test_20260919000000_cccccccc"
FV = feature_version(20)
SECURITIES = [f"KRX:00000{i}" for i in range(1, 9)]
HORIZON = 5
MARKET_RULE = "KRX-2026-09-14-aftermarket"


def _days(count, start=date(2026, 1, 5)):
    out, day = [], start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


DAYS = _days(60)
# 마지막 h일 신호는 청산일이 구간 밖이라 PENDING_MATURITY가 된다.
SIGNAL_DAYS = DAYS[:58]
COSTS = CostModel.from_config()


def _at(day, hour):
    return datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)


def _price(sid, day):
    """종목·날짜마다 결정적인 가격. 종목별로 다른 추세를 준다."""
    idx = SECURITIES.index(sid)
    step = DAYS.index(day)
    return 10_000.0 + idx * 1_000 + step * (idx - 3) * 10.0


def _signal(sid, day, status, score):
    return {
        "signal_id": f"s_{sid[-6:]}_{day:%Y%m%d}",
        "signal_source": "REPLAY", "signal_at_utc": _at(day, 6),
        "signal_time_local": "15:35:00", "trading_date": day,
        "security_id": sid, "index_id": "KOSPI200",
        "universe_snapshot_id": "snap", "status": status,
        "gate_passed": status == "BUY_CANDIDATE", "gate_reasons": [],
        "total_score": score,
        "rank": SECURITIES.index(sid) + 1, "rank_population": len(SECURITIES),
        "rank_percentile": (SECURITIES.index(sid) + 1) / len(SECURITIES),
        "feature_status": "SUCCESS", "daily_evidence_tier": "FULL_SIGNAL_SHORT",
        "schema_version": "2.0.0", "created_at_utc": _at(day, 6),
        "run_id": RUN_ID, "config_hash": "h", "code_revision": "r",
        "scanner_version": "1.5.0", "market_rule_version": MARKET_RULE,
        "data_revision": 1,
    }


def _label(sid, day, status=LabelStatus.COMPLETE):
    pos = DAYS.index(day)
    complete = status == LabelStatus.COMPLETE
    entry = DAYS[pos + 1] if complete and pos + 1 < len(DAYS) else None
    exit_day = (DAYS[pos + HORIZON]
                if complete and pos + HORIZON < len(DAYS) else None)
    if complete and (entry is None or exit_day is None):
        complete, entry, exit_day = False, None, None
        status = LabelStatus.PENDING_MATURITY

    entry_price = _price(sid, entry) if entry else None
    exit_price = _price(sid, exit_day) if exit_day else None
    gross = (exit_price / entry_price - 1.0) if complete else None
    net = (COSTS.net_return(entry_price, exit_price) if complete else None)
    return {
        "signal_id": f"s_{sid[-6:]}_{day:%Y%m%d}",
        "horizon_td": HORIZON, "entry_rule": "NEXT_OPEN",
        "security_id": sid, "signal_date": day, "signal_at_utc": _at(day, 6),
        "entry_date": entry, "exit_date": exit_day,
        "entry_at_utc": _at(entry, 0) if entry else None,
        "exit_at_utc": _at(exit_day, 6) if exit_day else None,
        "entry_price": Decimal(str(entry_price)) if entry_price else None,
        "exit_price": Decimal(str(exit_price)) if exit_price else None,
        "stock_return_gross": gross, "stock_return_net": net,
        "benchmark_id": "KRX:KOSPI200",
        "benchmark_return": 0.001 if complete else None,
        "excess_return_gross": (gross - 0.001) if complete else None,
        "excess_return_net": (net - 0.001) if complete else None,
        "mfe": 0.02 if complete else None, "mae": -0.01 if complete else None,
        "up": (net > 0) if complete else None,
        "up_gross": (gross > 0) if complete else None,
        "outperform": (net > 0.001) if complete else None,
        "hit_3pct": False if complete else None,
        "hit_5pct": False if complete else None,
        "hit_minus_3pct": False if complete else None,
        "hit_minus_5pct": False if complete else None,
        "commission": float(COSTS.commission_rate),
        "tax": float(COSTS.tax_rate_sell),
        "slippage": float(COSTS.slippage_rate),
        "label_status": status,
        "status_reason": None if complete else "성숙 전",
        "calendar_checked": complete,
        "label_source": "gold.daily_forward_returns",
        "label_version": LABEL_VERSION, "cost_model_version": COSTS.version,
        "market_rule_version": MARKET_RULE, "run_id": RUN_ID,
        "schema_version": "2.0.0", "data_revision": 1,
    }


def _bar(sid, day):
    close = _price(sid, day)
    return {
        "security_id": sid, "trading_date": day,
        "open": Decimal(str(close)), "high": Decimal(str(close * 1.01)),
        "low": Decimal(str(close * 0.99)), "close": Decimal(str(close)),
        "volume": 1_000_000,
        "traded_value": Decimal(str(1_000_000 * close)),
        "price_scope": "REGULAR", "adjustment_type": "SPLIT_ADJUSTED",
        "adjustment_factor": 1.0, "adjustment_as_of_utc": None,
        "available_at_utc": _at(day, 9),
        "quality_status": "SUCCESS", "quality_flags": [],
        "source": "TEST",
        "market_rule_version": MARKET_RULE, "run_id": RUN_ID,
        "schema_version": "2.0.0", "data_revision": 1,
    }


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    st = ParquetStore(root=str(tmp_path_factory.mktemp("bt")))
    signals, labels, bars, features, bench = [], [], [], [], []

    for day in DAYS:
        for sid in SECURITIES:
            bars.append(_bar(sid, day))
            features.append(_feature(sid, day))
        bench.append({
            "benchmark_id": "KRX:KOSPI200", "trading_date": day,
            "open": Decimal("300"), "high": Decimal("301"),
            "low": Decimal("299"),
            "close": Decimal(str(300 + DAYS.index(day) * 0.5)),
            "volume": 0, "total_return_close": None,
            "price_scope": "REGULAR",
            "available_at_utc": _at(day, 9),
            "quality_status": "SUCCESS",
            "source": "TEST",
            "market_rule_version": MARKET_RULE, "run_id": RUN_ID,
            "schema_version": "2.0.0", "data_revision": 1,
        })

    for day in SIGNAL_DAYS:
        for i, sid in enumerate(SECURITIES):
            # 매일 앞의 4종목이 BUY_CANDIDATE다.
            status = "BUY_CANDIDATE" if i < 4 else "WATCH"
            signals.append(_signal(sid, day, status, 90.0 - i * 10))
            labels.append(_label(sid, day))

    st.write("gold", "signals", signals, SIGNAL_COLUMNS, SIGNAL_PK, RUN_ID,
             partition_fn=lambda r: [("index_id", r["index_id"])])
    st.write("gold", "forward_returns", labels, SIGNAL_FORWARD_RETURN_COLUMNS,
             SIGNAL_FORWARD_RETURN_PK, RUN_ID,
             partition_fn=lambda r: [("horizon_td", r["horizon_td"])])
    st.write("silver", "daily_bars", bars, DAILY_BAR_COLUMNS, DAILY_BAR_PK,
             RUN_ID, partition_fn=lambda r: [("year", r["trading_date"].year)])
    # benchmark_id에 콜론이 있어 파티션 디렉터리 이름으로 못 쓴다(Windows).
    st.write("silver", "benchmark_bars", bench, BENCHMARK_BAR_COLUMNS,
             BENCHMARK_BAR_PK, RUN_ID,
             partition_fn=lambda r: [("year", r["trading_date"].year)])
    st.write("gold", "daily_features", features, DAILY_FEATURE_COLUMNS,
             DAILY_FEATURE_PK, RUN_ID,
             partition_fn=lambda r: [("feature_version", r["feature_version"])])
    return st


def _feature(sid, day):
    return {
        "security_id": sid, "trading_date": day, "feature_version": FV,
        "close": 100.0, "ma20": 99.0, "ma60": 98.0, "ma120": 97.0,
        "ma20_prev5": 98.5, "above_ma20": True, "above_ma60": True,
        "above_ma120": True, "ma20_rising": True, "ma60_rising": True,
        "ma20_above_ma60": True, "close_vs_ma20_pct": 1.0,
        "price_data_status": "SUCCESS",
        "obv": 1000.0, "obv_ma20": 900.0, "obv_above_ma20": True,
        "obv_slope": 10.0, "obv_window_volume": 100.0,
        "obv_normalized_slope": 0.1, "obv_up_ratio": 0.6, "obv_lookback": 20,
        "obv_data_status": "SUCCESS",
        "avg_traded_value_20d": 1.0e12,
        "liquidity_days_used": 20, "liquidity_data_status": "SUCCESS",
        "cond_price": True, "cond_obv": True, "cond_liquidity": True,
        "gate_all": True, "feature_status": "SUCCESS",
        "available_at_utc": _at(day, 6),
        "market_rule_version": MARKET_RULE, "run_id": RUN_ID,
        "schema_version": "2.0.0", "data_revision": 1,
    }


def _run(store, **kwargs):
    kwargs.setdefault("horizon_td", HORIZON)
    kwargs.setdefault("max_positions", 8)
    spec = BacktestSpec(**kwargs)
    return BacktestEngine(store).run(spec, run_id=RUN_ID, dry_run=True)


@pytest.fixture(scope="module")
def result(store):
    return _run(store, selection_policy="BUY_ALL")


# ─────────────────────────────────────────────────────────────
# 돈이 맞는가
# ─────────────────────────────────────────────────────────────

def _relative_gap(got):
    return got.ledger_gap / got.spec.initial_capital


def test_원장_합계가_NAV_변화와_맞는다(result):
    """절대 원이 아니라 자금 대비로 본다 — 1억에 1e-8원은 부동소수 잡음이다."""
    assert _relative_gap(result) < 1e-12


def test_원장이_어긋나면_거부한다(result):
    """검증기가 실제로 잡는지 확인한다 — 안 잡으면 있으나 마나다."""
    nav = [dict(r) for r in result.nav]
    nav[-1]["net_nav"] += 1_000_000.0
    with pytest.raises(DataQualityError):
        assert_ledger_reconciles(result.trades, nav,
                                 result.spec.initial_capital)


def test_손익은_진입금액_x_순수익률이다(result):
    """거래마다 pnl/notional이 label의 net_return과 같아야 한다."""
    for trade in result.closed():
        assert (trade["pnl"] / trade["notional"]
                == pytest.approx(trade["net_return"], abs=1e-12))


def test_수익률을_다시_계산하지_않는다(store, result):
    """엔진은 label의 net_return을 그대로 쓴다 — 두 계층이 갈릴 수 없다."""
    labels = {r["signal_id"]: r["stock_return_net"]
              for r in store.read("gold", "forward_returns",
                                  pk=SIGNAL_FORWARD_RETURN_PK)}
    for trade in result.closed():
        assert trade["net_return"] == labels[trade["signal_id"]]


# ─────────────────────────────────────────────────────────────
# 체결과 신호의 분리
# ─────────────────────────────────────────────────────────────

def test_진입은_언제나_신호_뒤다(result):
    assert_execution_after_signal(
        [t for t in result.trades if t["trade_status"] != "REJECTED"])


def test_신호_이전_체결은_거부한다(result):
    trade = dict(result.closed()[0])
    trade["entry_at_utc"] = trade["signal_at_utc"]
    with pytest.raises(LookAheadError):
        assert_execution_after_signal([trade])


def test_진입일은_신호일의_다음_거래일이다(result):
    for trade in result.closed():
        assert DAYS.index(trade["entry_date"]) == \
            DAYS.index(trade["signal_date"]) + 1


# ─────────────────────────────────────────────────────────────
# 살 수 없었던 것을 조용히 버리지 않는다
# ─────────────────────────────────────────────────────────────

def test_체결되지_않은_신호는_사유와_함께_남는다(result):
    assert result.rejected
    assert all(t["reject_reason"] for t in result.rejected)
    assert all(t["trade_status"] == "REJECTED" for t in result.rejected)


def test_성숙하지_않은_label은_체결하지_않는다(result):
    reasons = {t["reject_reason"] for t in result.rejected}
    assert "LABEL_NOT_COMPLETE" in reasons
    for trade in result.rejected:
        if trade["reject_reason"] == "LABEL_NOT_COMPLETE":
            assert trade["entry_date"] is None
            assert trade["notional"] is None


def test_슬롯을_넘겨_사지_않는다(store):
    got = _run(store, selection_policy="BUY_ALL", max_positions=3)
    assert max(r["open_positions"] for r in got.nav) <= 3


def test_중복_보유를_막으면_같은_종목을_두_번_들지_않는다(store):
    got = _run(store, selection_policy="BUY_ALL", allow_overlap=False,
               max_entries_per_day=None)
    held = {}
    for trade in got.closed():
        for other in held.get(trade["security_id"], []):
            overlap = (trade["entry_date"] <= other["exit_date"]
                       and other["entry_date"] <= trade["exit_date"])
            assert not overlap, f"{trade['security_id']} 보유가 겹친다"
        held.setdefault(trade["security_id"], []).append(trade)


# ─────────────────────────────────────────────────────────────
# 진입일이 몰리지 않는가
# ─────────────────────────────────────────────────────────────

def test_기본값은_진입을_분산시킨다(store):
    """상한이 없으면 첫날 슬롯을 다 채우고 h일마다 통째로 교체한다."""
    staggered = _run(store, selection_policy="BUY_ALL", max_positions=8)
    cohort = _run(store, selection_policy="BUY_ALL", max_positions=8,
                  max_entries_per_day=None)

    def entry_days(got):
        return {t["entry_date"] for t in got.closed()}

    assert len(entry_days(staggered)) > len(entry_days(cohort))
    # 단일 코호트는 첫날 그날의 BUY_CANDIDATE를 전부 산다(하루 4종목).
    assert sum(1 for t in cohort.closed() if t["entry_date"] == DAYS[1]) == 4
    # 분산하면 슬롯수/horizon = 8/5 -> 2개까지만.
    assert sum(1 for t in staggered.closed()
               if t["entry_date"] == DAYS[1]) <= 2


def test_하루_진입_상한은_1_이상이어야_한다():
    with pytest.raises(ConfigError):
        BacktestSpec(max_entries_per_day=0)


# ─────────────────────────────────────────────────────────────
# 선택 정책
# ─────────────────────────────────────────────────────────────

def test_BUY_TOP3는_BUY_CANDIDATE_중_점수_상위_셋만_고른다(store):
    engine = BacktestEngine(store)
    spec = BacktestSpec(selection_policy="BUY_TOP3", horizon_td=HORIZON)
    pairs = engine.load_candidates(spec)[SIGNAL_DAYS[0]]
    picked = engine.select(spec, pairs)
    assert len(picked) == 3
    assert all(p[0]["status"] == "BUY_CANDIDATE" for p in picked)
    assert [p[0]["total_score"] for p in picked] == [90.0, 80.0, 70.0]


def test_WATCH만_있는_날에는_BUY_정책이_아무것도_고르지_않는다(store):
    engine = BacktestEngine(store)
    spec = BacktestSpec(selection_policy="BUY_ALL", horizon_td=HORIZON)
    pairs = engine.load_candidates(spec)[SIGNAL_DAYS[0]]
    watch_only = [p for p in pairs if p[0]["status"] == "WATCH"]
    assert engine.select(spec, watch_only) == []


def test_알_수_없는_정책은_거부한다():
    with pytest.raises(ConfigError):
        BacktestSpec(selection_policy="NOPE")


def test_모든_정책이_돌아간다(store):
    for policy in SELECTION_POLICIES:
        got = _run(store, selection_policy=policy)
        assert _relative_gap(got) < 1e-12, policy


# ─────────────────────────────────────────────────────────────
# 재현성
# ─────────────────────────────────────────────────────────────

def test_같은_spec은_같은_experiment_id를_준다():
    left = BacktestSpec(selection_policy="BUY_TOP5", horizon_td=5)
    right = BacktestSpec(selection_policy="BUY_TOP5", horizon_td=5)
    assert left.experiment_id() == right.experiment_id()


def test_정책이_다르면_experiment_id도_다르다():
    left = BacktestSpec(selection_policy="BUY_TOP5", horizon_td=5)
    right = BacktestSpec(selection_policy="BUY_TOP10", horizon_td=5)
    assert left.experiment_id() != right.experiment_id()


def test_하루_진입_상한도_experiment_id에_들어간다():
    left = BacktestSpec(max_entries_per_day=1)
    right = BacktestSpec(max_entries_per_day=None)
    assert left.experiment_id() != right.experiment_id()


# ─────────────────────────────────────────────────────────────
# 지표
# ─────────────────────────────────────────────────────────────

def test_기준선을_같은_구간에서_다시_잰다(store, result):
    metrics = BacktestMetrics(store, result, n_resamples=200)
    base = metrics.base_rate()
    days = {r["trading_date"] for r in result.nav}
    assert base["raw_n"] > 0
    assert set(base["dates"].tolist()) <= days


def test_기준선_대비_적중률에_신뢰구간이_붙는다(store, result):
    rows = BacktestMetrics(store, result, n_resamples=200).compute(RUN_ID)
    edge = next(r for r in rows
                if r["metric_name"] == "hit_rate_edge_vs_base_rate")
    assert edge["comparison"] == "base_rate"
    assert edge["ci_low"] is not None and edge["ci_high"] is not None
    assert edge["cluster_key"] == "signal_date"
    assert edge["block_length"] >= HORIZON


def test_walk_forward_구간을_나눠_따로_보고한다(store):
    got = _run(store, selection_policy="BUY_ALL",
               oos_start=SIGNAL_DAYS[20])
    rows = BacktestMetrics(store, got, n_resamples=200).compute(RUN_ID)
    scopes = {r["metric_scope"] for r in rows}
    assert scopes == {"FULL", "IN_SAMPLE", "OUT_OF_SAMPLE"}
    assert {t["sample_scope"] for t in got.closed()} <= {
        "IN_SAMPLE", "OUT_OF_SAMPLE"}


def test_oos_start가_start보다_앞서면_거부한다():
    with pytest.raises(ConfigError):
        BacktestSpec(start=DAYS[10], oos_start=DAYS[5])


def test_벤치마크가_없으면_초과수익을_0으로_두지_않는다(store, result):
    got = BacktestMetrics(store, result, n_resamples=200)
    nav = [dict(r, benchmark_nav=None) for r in result.nav]
    patched = type(result)(result.spec, result.experiment_id, result.run_row,
                           result.trades, nav, result.rejected,
                           result.ledger_gap)
    rows = BacktestMetrics(store, patched, n_resamples=200).compute(RUN_ID)
    bench = next(r for r in rows
                 if r["metric_name"] == "benchmark_total_return")
    assert bench["metric_value"] is None
    assert bench["status"] == "PARTIAL"
    assert not any(r["metric_name"] == "excess_total_return" for r in rows)


# ─────────────────────────────────────────────────────────────
# 저장
# ─────────────────────────────────────────────────────────────

def test_한_run_id로_여러_실험을_써도_서로_덮어쓰지_않는다(store, tmp_path):
    """`backtest_runs`는 파티션이 없다. part_key가 없으면 마지막만 남는다."""
    from cvd_obv.contracts.backtest_schema import BACKTEST_RUN_PK

    out = ParquetStore(root=str(tmp_path / "w"))
    engine = BacktestEngine(store)
    engine.store = store          # 읽기는 픽스처에서, 쓰기는 별도 저장소로
    ids = []
    for policy in ("BUY_TOP3", "BUY_TOP5", "SCORE_Q5"):
        spec = BacktestSpec(selection_policy=policy, horizon_td=HORIZON,
                            max_positions=8)
        result = engine.run(spec, run_id=RUN_ID, dry_run=True)
        result.writes = []
        engine.store = out
        BacktestEngine._write(engine, result, RUN_ID)
        engine.store = store
        ids.append(result.experiment_id)

    rows = out.read("gold", "backtest_runs", pk=BACKTEST_RUN_PK)
    assert {r["experiment_id"] for r in rows} == set(ids)
    assert len(rows) == 3


def test_동점은_거래대금_큰_쪽이_먼저다():
    """아키텍처 §6 — total_score desc, liquidity desc, security_id asc.

    점수만으로 정렬하면 동점의 순서가 읽은 파일 순서에 달린다. 하루에 몇
    종목만 사므로 그 순서가 결과를 바꾼다.
    """
    day = DAYS[0]
    pairs = [({"security_id": "KRX:000001", "trading_date": day,
               "total_score": 50.0, "status": "BUY_CANDIDATE",
               "rank": 1}, None),
             ({"security_id": "KRX:000002", "trading_date": day,
               "total_score": 50.0, "status": "BUY_CANDIDATE",
               "rank": 2}, None),
             ({"security_id": "KRX:000003", "trading_date": day,
               "total_score": 50.0, "status": "BUY_CANDIDATE",
               "rank": 3}, None)]
    liquidity = {("KRX:000002", day): 9.0e11, ("KRX:000003", day): 1.0e11}
    spec = BacktestSpec(selection_policy="BUY_ALL")

    got = [p[0]["security_id"]
           for p in BacktestEngine.select(spec, pairs, liquidity)]
    # 거래대금 큰 순 -> 000002, 000003. 거래대금을 모르는 000001은 맨 뒤.
    assert got == ["KRX:000002", "KRX:000003", "KRX:000001"]


def test_거래대금을_몰라도_순서가_결정적이다():
    day = DAYS[0]
    pairs = [({"security_id": f"KRX:00000{i}", "trading_date": day,
               "total_score": 50.0, "status": "BUY_CANDIDATE", "rank": i},
              None) for i in (3, 1, 2)]
    spec = BacktestSpec(selection_policy="BUY_ALL")
    got = [p[0]["security_id"] for p in BacktestEngine.select(spec, pairs)]
    assert got == ["KRX:000001", "KRX:000002", "KRX:000003"]


def test_현금이_거의_없으면_먼지_포지션을_만들지_않는다(store):
    """목표 비중의 10%도 못 채우면 슬롯만 차지하고 분산된 척하게 된다."""
    got = _run(store, selection_policy="BUY_ALL", max_positions=8,
               max_entries_per_day=None)
    target_floor = got.spec.min_weight_pct
    for trade in got.closed():
        assert trade["weight"] >= target_floor / got.spec.max_positions


# ─────────────────────────────────────────────────────────────
# RANDOM 대조군 — 지수와 비교하면 가중 방식을 비교하게 된다
# ─────────────────────────────────────────────────────────────

def test_RANDOM은_BUY가_아닌_종목도_고른다(store):
    """대조군의 요점은 **Gate를 보지 않는 것**이다.

    Gate 통과분 중에서 무작위로 고르면 그건 대조군이 아니라 또 하나의
    지표 전략이다.
    """
    from cvd_obv.backtest.engine import SELECTION_POLICIES

    assert SELECTION_POLICIES["RANDOM"] == ("random", None)
    got = _run(store, selection_policy="RANDOM")
    buy = _run(store, selection_policy="BUY_ALL")
    picked = {t["security_id"] for t in got.closed()}
    only_buy = {t["security_id"] for t in buy.closed()}
    # 유니버스 전체에서 뽑으므로 BUY 집합에 갇히지 않는다
    assert picked - only_buy or len(picked) >= len(only_buy)


def test_RANDOM은_seed가_같으면_같은_결과다(store):
    a = _run(store, selection_policy="RANDOM", random_seed=7)
    b = _run(store, selection_policy="RANDOM", random_seed=7)
    assert a.summary()["total_return"] == b.summary()["total_return"]


def test_RANDOM은_seed가_다르면_실험ID가_다르다(store):
    """seed별 결과가 같은 ID로 덮어쓰이면 분포를 볼 수 없다."""
    a = _run(store, selection_policy="RANDOM", random_seed=7)
    b = _run(store, selection_policy="RANDOM", random_seed=8)
    assert a.experiment_id != b.experiment_id


def test_seed는_RANDOM이_아닌_정책의_실험ID를_바꾸지_않는다(store):
    """기존 실험 ID가 바뀌면 저장된 결과와 끊긴다."""
    a = _run(store, selection_policy="BUY_ALL", random_seed=7)
    b = _run(store, selection_policy="BUY_ALL", random_seed=8)
    assert a.experiment_id == b.experiment_id


def test_출처가_섞였는데_고르지_않으면_멈춘다():
    """증거 계층이 다른 신호를 한 덩어리로 세는 것은 해석 실수다."""
    from cvd_obv.contracts.schema import assert_single_signal_source
    from cvd_obv.domain.errors import SchemaError

    mixed = [{"signal_source": "REPLAY"}, {"signal_source": "DAILY_PANEL"}]
    with pytest.raises(SchemaError):
        assert_single_signal_source(mixed, None, "테스트")
    assert_single_signal_source(mixed, "REPLAY", "고르면 통과")
    assert_single_signal_source([{"signal_source": "REPLAY"}], None, "단일")

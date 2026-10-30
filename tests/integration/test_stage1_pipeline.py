# -*- coding: utf-8 -*-
"""Stage 1 종단 흐름: 일봉 → 캘린더 → forward return → 기준선.

네트워크 없이 합성 일봉을 Parquet에 넣고 실제 계층을 그대로 태운다.
로드맵 §8 "통합: API fixture→cache→replay→label→backtest의 작은 종단 흐름"의
Stage 1 몫이다.

특히 **기준선이 재실행해도 같은 값인지**를 확인한다(Stage 1 DoD).
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from cvd_obv.contracts.market_schema import DAILY_BAR_COLUMNS, DAILY_BAR_PK
from cvd_obv.domain.errors import NoInputDataError
from cvd_obv.domain.status import LabelStatus, QualityStatus
from cvd_obv.ingestion.calendar_store import MarketCalendarRepository
from cvd_obv.ingestion.daily_history import DailyHistoryRepository
from cvd_obv.labels import CostModel, DailyForwardReturnRepository
from cvd_obv.statistics import BaseRateComputer
from cvd_obv.storage.parquet_store import ParquetStore

RUN_ID = "test_20260918000000_aaaaaaaa"
SECURITIES = ["KRX:005930", "KRX:000660", "KRX:035420"]


def _trading_days(start: date, count: int):
    days, day = [], start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def _daily_rows(days, securities=SECURITIES):
    """종목마다 다른 추세를 가진 합성 일봉."""
    from datetime import datetime, timezone

    rows = []
    for s_idx, sid in enumerate(securities):
        price = 10000.0
        for d_idx, day in enumerate(days):
            # 종목별로 다른 주기의 등락 — 날짜 공통 성분도 섞는다.
            common = 0.004 if (d_idx % 7) < 4 else -0.003
            idio = 0.002 if ((d_idx + s_idx) % 3) == 0 else -0.001
            price *= (1.0 + common + idio)
            close = round(price, 0)
            rows.append({
                "security_id": sid,
                "trading_date": day,
                "open": Decimal(str(close - 20)),
                "high": Decimal(str(close + 60)),
                "low": Decimal(str(close - 70)),
                "close": Decimal(str(close)),
                "volume": 1000 + d_idx,
                "traded_value": Decimal(str(int(close) * (1000 + d_idx))),
                "price_scope": "REGULAR",
                "adjustment_type": "SPLIT_ADJUSTED",
                "adjustment_factor": None,
                "adjustment_as_of_utc": datetime(2026, 9, 18,
                                                 tzinfo=timezone.utc),
                "available_at_utc": datetime(day.year, day.month, day.day,
                                             6, 35, tzinfo=timezone.utc),
                "source": "SYNTHETIC",
                "quality_status": "SUCCESS",
                "quality_flags": [],
                "run_id": RUN_ID,
                "market_rule_version": "KRX-2016-08-01-regular1530",
                "schema_version": "2.0.0",
                "data_revision": 1,
            })
    return rows


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    root = tmp_path_factory.mktemp("stage1")
    st = ParquetStore(root=str(root))
    days = _trading_days(date(2020, 1, 6), 300)
    st.write("silver", "daily_bars", _daily_rows(days), DAILY_BAR_COLUMNS,
             DAILY_BAR_PK, RUN_ID,
             partition_fn=lambda r: [("market", "KRX"),
                                     ("year", r["trading_date"].year)])
    return st


# ─────────────────────────────────────────────────────────────
# 캘린더
# ─────────────────────────────────────────────────────────────

def test_일봉에서_캘린더를_파생해_저장한다(store):
    repo = MarketCalendarRepository(store=store)
    summary = repo.build(run_id=RUN_ID)
    assert summary["trading_days"] == 300
    assert summary["derived_from_securities"] == 3
    assert summary["source"] == "DERIVED_FROM_DAILY_BARS"
    # 저장한 캘린더를 다시 읽어도 같은 거래일이 나온다.
    calendar = repo.load_calendar()
    assert len(calendar) == 300


# ─────────────────────────────────────────────────────────────
# forward return
# ─────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def labels(store):
    repo = DailyForwardReturnRepository(
        store=store, cost_model=CostModel.zero(), horizons=(1, 5))
    summary = repo.build(run_id=RUN_ID, flush_every=2)
    return repo, summary


def test_모든_종목_날짜_horizon에_행이_생긴다(labels):
    _repo, summary = labels
    assert summary.securities == 3
    assert summary.rows == 3 * 300 * 2


def test_성숙하지_않은_구간만_PENDING이다(labels):
    _repo, summary = labels
    counts = summary.status_counts
    assert counts[LabelStatus.COMPLETE] > 0
    # h=1은 마지막 1일, h=5는 마지막 5일이 성숙하지 않는다.
    assert counts[LabelStatus.PENDING_MATURITY] == 3 * (1 + 5)


def test_저장된_label을_다시_읽을_수_있다(labels):
    repo, _summary = labels
    rows = repo.load(horizon=5, security_id="KRX:005930")
    assert len(rows) == 300
    complete = [r for r in rows if r["label_status"] == LabelStatus.COMPLETE]
    assert len(complete) == 295
    assert all(r["entry_date"] > r["signal_date"] for r in complete)


def test_커버리지_요약이_상태_분포를_보여준다(labels):
    repo, _summary = labels
    coverage = repo.coverage()
    assert coverage["rows"] == 3 * 300 * 2
    assert set(coverage["by_horizon"]) == {1, 5}
    assert 0.9 < coverage["complete_ratio"] < 1.0


# ─────────────────────────────────────────────────────────────
# 기준선
# ─────────────────────────────────────────────────────────────

def test_기준선이_확률과_유효표본을_함께_낸다(store, labels):
    computer = BaseRateComputer(store, n_resamples=200, seed=11)
    row = computer.compute(5, "up", run_id=RUN_ID)
    assert 0.0 <= row["probability"] <= 1.0
    assert row["raw_n"] == 3 * 295
    assert row["trading_date_n"] == 295
    assert row["security_n"] == 3
    # 유효표본은 원표본보다 작아야 한다 — 날짜가 군집이기 때문이다.
    assert row["effective_n"] <= row["raw_n"]
    assert row["ci_low"] < row["probability"] < row["ci_high"]


def test_기준선은_재실행해도_같다(store, labels):
    """Stage 1 DoD: base rate가 재실행 시 동일하다."""
    a = BaseRateComputer(store, n_resamples=200, seed=11).compute(
        5, "up", run_id=RUN_ID)
    b = BaseRateComputer(store, n_resamples=200, seed=11).compute(
        5, "up", run_id="다른_run이어도_상관없다")
    for key in ("base_rate_id", "probability", "ci_low", "ci_high",
                "effective_n", "raw_n", "icc"):
        assert a[key] == b[key], key


def test_기준선_ID는_정의가_바뀌면_달라진다(store, labels):
    computer = BaseRateComputer(store, n_resamples=200, seed=11)
    h1 = computer.compute(1, "up", run_id=RUN_ID)
    h5 = computer.compute(5, "up", run_id=RUN_ID)
    gross = computer.compute(5, "up_gross", run_id=RUN_ID)
    assert len({h1["base_rate_id"], h5["base_rate_id"],
                gross["base_rate_id"]}) == 3


def test_독립_가정보다_넓은_구간을_보고한다(store, labels):
    computer = BaseRateComputer(store, n_resamples=200, seed=11)
    row = computer.compute(5, "up", run_id=RUN_ID)
    detail = row["_detail"]
    naive_width = detail["naive_ci"][1] - detail["naive_ci"][0]
    reported = row["ci_high"] - row["ci_low"]
    assert reported >= naive_width
    assert detail["block_length_rule"]


def test_확률로_볼_수_없는_outcome은_거부한다(store, labels):
    from cvd_obv.domain.errors import StatisticalValidationError

    computer = BaseRateComputer(store, n_resamples=50, seed=11)
    with pytest.raises(StatisticalValidationError):
        computer.compute(5, "stock_return_net", run_id=RUN_ID)


def test_표본이_없으면_조용히_0을_내지_않는다(store, labels):
    computer = BaseRateComputer(store, n_resamples=50, seed=11)
    with pytest.raises(NoInputDataError):
        computer.compute(99, "up", run_id=RUN_ID)


# ─────────────────────────────────────────────────────────────
# 일봉 검증
# ─────────────────────────────────────────────────────────────

def test_일봉_검증이_통과한다(store):
    report = DailyHistoryRepository(store=store).validate()
    assert report["securities"] == 3
    assert report["ohlc_violations"] == 0
    assert report["coverage_gaps"] == {}
    assert report["status"] == QualityStatus.SUCCESS

# -*- coding: utf-8 -*-
"""Forward return 계산 — 편향 방지와 결측 처리.

여기서 깨지면 이후 모든 성과 수치가 거짓이 된다. 특히 두 가지를 본다.
1. **진입이 신호 이후인가** — 당일 종가 체결은 미래를 보는 것이다.
2. **없는 가격을 지어내지 않는가** — 거래정지 종목의 수익률이 0으로 보이면
   그 표본이 통째로 오염된다.
"""

from datetime import date
from decimal import Decimal

import pytest

from cvd_obv.contracts.label_schema import (
    DAILY_FORWARD_RETURN_COLUMNS, DAILY_FORWARD_RETURN_PK,
)
from cvd_obv.contracts.schema import validate_rows
from cvd_obv.domain.calendar import TradingCalendar
from cvd_obv.domain.errors import ConfigError
from cvd_obv.domain.status import LabelStatus
from cvd_obv.labels import BarSeries, CostModel, ForwardReturnBuilder

# 2026-09-12(토)·13(일)은 휴장
DAYS = [date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10),
        date(2026, 9, 11), date(2026, 9, 14), date(2026, 9, 15),
        date(2026, 9, 16)]

RUN_ID = "test_20260918000000_aaaaaaaa"


def _bar(day, open_, high, low, close):
    return {"trading_date": day, "open": Decimal(str(open_)),
            "high": Decimal(str(high)), "low": Decimal(str(low)),
            "close": Decimal(str(close))}


def _series(skip=(), security_id="KRX:005930"):
    """가격이 하루 100씩 오르는 단순 시계열. 계산 검산이 쉽도록."""
    bars = []
    for i, day in enumerate(DAYS):
        if day in skip:
            continue
        base = 10000 + i * 100
        bars.append(_bar(day, base, base + 50, base - 50, base + 20))
    return BarSeries(security_id, bars)


@pytest.fixture
def calendar():
    return TradingCalendar("KRX", DAYS)


@pytest.fixture
def builder(calendar):
    return ForwardReturnBuilder(calendar, cost_model=CostModel.zero(),
                                horizons=(1, 3))


def _by(rows, day, horizon):
    for r in rows:
        if r["signal_date"] == day and r["horizon_td"] == horizon:
            return r
    raise AssertionError(f"행이 없다: {day} h={horizon}")


# ─────────────────────────────────────────────────────────────
# 편향 방지
# ─────────────────────────────────────────────────────────────

def test_진입은_항상_신호일_다음_거래일이다(builder):
    rows = builder.build_security(_series(), RUN_ID)
    for row in rows:
        if row["entry_date"] is not None:
            assert row["entry_date"] > row["signal_date"], row


def test_휴장일을_건너뛰고_진입한다(builder):
    row = _by(builder.build_security(_series(), RUN_ID), date(2026, 9, 11), 1)
    # 금요일 신호 → 토·일을 건너뛰고 월요일 시가 진입
    assert row["entry_date"] == date(2026, 9, 14)


def test_h1은_진입일_종가에_청산한다(builder):
    row = _by(builder.build_security(_series(), RUN_ID), date(2026, 9, 8), 1)
    assert row["entry_date"] == date(2026, 9, 9)
    assert row["exit_date"] == date(2026, 9, 9)


def test_h3은_진입일_포함_3거래일째_종가에_청산한다(builder):
    row = _by(builder.build_security(_series(), RUN_ID), date(2026, 9, 8), 3)
    assert row["entry_date"] == date(2026, 9, 9)
    assert row["exit_date"] == date(2026, 9, 11)


def test_마지막_구간은_성숙하지_않았다고_표시한다(builder):
    rows = builder.build_security(_series(), RUN_ID)
    last = _by(rows, date(2026, 9, 16), 1)
    assert last["label_status"] == LabelStatus.PENDING_MATURITY
    assert last["stock_return_net"] is None
    assert "캘린더" in last["status_reason"]


# ─────────────────────────────────────────────────────────────
# 수익률 계산
# ─────────────────────────────────────────────────────────────

def test_총수익률은_진입_시가_대비_청산_종가다(builder):
    row = _by(builder.build_security(_series(), RUN_ID), date(2026, 9, 8), 1)
    # 09-09 시가 10100, 종가 10120
    assert row["entry_price"] == Decimal("10100")
    assert row["exit_price"] == Decimal("10120")
    assert row["stock_return_gross"] == pytest.approx(10120 / 10100 - 1)


def test_비용이_0이면_gross와_net이_같다(builder):
    row = _by(builder.build_security(_series(), RUN_ID), date(2026, 9, 8), 1)
    assert row["stock_return_net"] == pytest.approx(row["stock_return_gross"])


def test_비용을_넣으면_net이_gross보다_낮다(calendar):
    costs = CostModel("t", commission_rate=0.00015, tax_rate_sell=0.0018,
                      slippage_rate=0.001)
    builder = ForwardReturnBuilder(calendar, cost_model=costs, horizons=(1,))
    row = _by(builder.build_security(_series(), RUN_ID), date(2026, 9, 8), 1)
    assert row["stock_return_net"] < row["stock_return_gross"]
    # 왕복 슬리피지 0.2% + 세금 0.18% + 수수료 0.03% ≈ 0.41%
    drag = row["stock_return_gross"] - row["stock_return_net"]
    assert 0.003 < drag < 0.006


def test_세금은_매도에만_붙는다():
    """매수에도 세금을 걸면 비용이 이중 계산된다."""
    taxed = CostModel("t", 0.0, 0.0018, 0.0)
    free = CostModel("t", 0.0, 0.0, 0.0)
    # 가격이 그대로면 손실은 세율만큼이다.
    assert taxed.net_return(100, 100) == pytest.approx(-0.0018)
    assert free.net_return(100, 100) == pytest.approx(0.0)


def test_진입가가_0이하면_거부한다():
    with pytest.raises(ConfigError):
        CostModel.zero().net_return(0, 100)


def test_up은_비용_후_기준이고_up_gross는_비용_전_기준(calendar):
    """비용을 넘지 못하는 소폭 상승은 up이 아니다."""
    costs = CostModel("t", 0.0, 0.005, 0.0)
    builder = ForwardReturnBuilder(calendar, cost_model=costs, horizons=(1,))
    series = BarSeries("KRX:005930", [
        _bar(DAYS[0], 100, 101, 99, 100),
        _bar(DAYS[1], 100, 101, 99, 100.2),   # +0.2% — 세금 0.5%에 못 미친다
        _bar(DAYS[2], 100, 101, 99, 100),
    ])
    row = _by(builder.build_security(series, RUN_ID), DAYS[0], 1)
    assert row["up_gross"] is True
    assert row["up"] is False


# ─────────────────────────────────────────────────────────────
# MFE / MAE / barrier
# ─────────────────────────────────────────────────────────────

def test_MFE_MAE는_장중_고가_저가_기준이다(calendar):
    builder = ForwardReturnBuilder(calendar, cost_model=CostModel.zero(),
                                   horizons=(3,))
    series = BarSeries("KRX:005930", [
        _bar(DAYS[0], 100, 100, 100, 100),
        _bar(DAYS[1], 100, 110, 95, 100),     # 진입일: 고가 +10%, 저가 -5%
        _bar(DAYS[2], 100, 104, 90, 100),     # 저가 -10%
        _bar(DAYS[3], 100, 100, 100, 100),
    ])
    row = _by(builder.build_security(series, RUN_ID), DAYS[0], 3)
    assert row["entry_price"] == Decimal("100")
    assert row["mfe"] == pytest.approx(0.10)
    assert row["mae"] == pytest.approx(-0.10)
    assert row["hit_5pct"] is True
    assert row["hit_3pct"] is True
    assert row["hit_minus_5pct"] is True


def test_종가만_보면_놓치는_장중_도달을_잡는다(calendar):
    """종가 기준이면 +10% 도달이 안 보인다 — barrier 정의가 label_version이다."""
    builder = ForwardReturnBuilder(calendar, cost_model=CostModel.zero(),
                                   horizons=(1,))
    series = BarSeries("KRX:005930", [
        _bar(DAYS[0], 100, 100, 100, 100),
        _bar(DAYS[1], 100, 112, 100, 100),    # 장중 +12%, 종가는 본전
        _bar(DAYS[2], 100, 100, 100, 100),
    ])
    row = _by(builder.build_security(series, RUN_ID), DAYS[0], 1)
    assert row["stock_return_gross"] == pytest.approx(0.0)
    assert row["hit_5pct"] is True


# ─────────────────────────────────────────────────────────────
# 결측 처리
# ─────────────────────────────────────────────────────────────

def test_진입일_봉이_없으면_수익률을_만들지_않는다(builder):
    """직전 가격으로 메우면 거래정지 종목의 수익률이 0으로 보인다."""
    rows = builder.build_security(_series(skip={date(2026, 9, 9)}), RUN_ID)
    row = _by(rows, date(2026, 9, 8), 1)
    assert row["label_status"] == LabelStatus.ENTRY_UNAVAILABLE
    assert row["stock_return_gross"] is None
    assert row["up"] is None


def test_청산일_봉이_없으면_수익률을_만들지_않는다(builder):
    rows = builder.build_security(_series(skip={date(2026, 9, 11)}), RUN_ID)
    row = _by(rows, date(2026, 9, 8), 3)
    assert row["label_status"] == LabelStatus.EXIT_UNAVAILABLE
    assert row["stock_return_net"] is None


def test_보유기간_중간_결측은_MFE에_반영_못함을_남긴다(builder):
    rows = builder.build_security(_series(skip={date(2026, 9, 10)}), RUN_ID)
    row = _by(rows, date(2026, 9, 8), 3)
    assert row["label_status"] == LabelStatus.COMPLETE
    assert "MFE/MAE" in (row["status_reason"] or "")


# ─────────────────────────────────────────────────────────────
# 벤치마크
# ─────────────────────────────────────────────────────────────

def test_벤치마크가_없으면_초과수익은_null이다(builder):
    """0으로 대체하면 '시장만큼은 했다'는 거짓 정보가 들어간다(Stage 1 DoD)."""
    row = _by(builder.build_security(_series(), RUN_ID), date(2026, 9, 8), 1)
    assert row["benchmark_return"] is None
    assert row["excess_return_net"] is None
    assert row["outperform"] is None


def test_벤치마크가_있으면_초과수익을_계산한다(builder):
    bench = BarSeries("KOSPI200", [
        _bar(day, 2000, 2010, 1990, 2000) for day in DAYS])
    rows = builder.build_security(_series(), RUN_ID, benchmark=bench,
                                  benchmark_id="KOSPI200")
    row = _by(rows, date(2026, 9, 8), 1)
    assert row["benchmark_id"] == "KOSPI200"
    assert row["benchmark_return"] == pytest.approx(0.0)
    assert row["excess_return_gross"] == pytest.approx(
        row["stock_return_gross"])
    assert row["outperform"] is True


# ─────────────────────────────────────────────────────────────
# 계약
# ─────────────────────────────────────────────────────────────

def test_행이_스키마_계약을_통과한다(builder):
    rows = builder.build_security(_series(), RUN_ID)
    validate_rows(rows, DAILY_FORWARD_RETURN_COLUMNS,
                  DAILY_FORWARD_RETURN_PK, "gold.daily_forward_returns")


def test_같은_키가_중복되지_않는다(builder):
    rows = builder.build_security(_series(), RUN_ID)
    keys = [(r["security_id"], r["signal_date"], r["horizon_td"],
             r["entry_rule"], r["label_version"]) for r in rows]
    assert len(set(keys)) == len(keys)


def test_캘린더에_없는_거래일은_오류다(calendar):
    """캘린더 파생이 잘못됐다는 신호이므로 조용히 넘기지 않는다."""
    builder = ForwardReturnBuilder(calendar, cost_model=CostModel.zero(),
                                   horizons=(1,))
    series = BarSeries("KRX:005930", [_bar(date(2026, 9, 12), 1, 1, 1, 1)])
    with pytest.raises(ConfigError):
        builder.build_security(series, RUN_ID)


def test_같은_거래일이_두_번_있으면_거부한다():
    with pytest.raises(ConfigError):
        BarSeries("KRX:005930", [_bar(DAYS[0], 1, 1, 1, 1),
                                 _bar(DAYS[0], 2, 2, 2, 2)])


def test_모르는_진입_규칙은_거부한다(calendar):
    """`SAME_CLOSE`는 2026-09-21에 구현했다 — 이제 거부 대상이 아니다.

    자세한 계약은 `test_same_close.py`에 있다.
    """
    with pytest.raises(ConfigError):
        ForwardReturnBuilder(calendar, entry_rule="LIMIT_ORDER")
    assert ForwardReturnBuilder(
        calendar, entry_rule="SAME_CLOSE").entry_rule == "SAME_CLOSE"

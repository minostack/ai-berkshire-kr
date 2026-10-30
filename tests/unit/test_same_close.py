# -*- coding: utf-8 -*-
"""SAME_CLOSE 진입 규칙 — 당일 종가 매수.

두 규칙을 비교하려면 **노출 구간이 같아야** 한다. 그래야 차이가
"진입 가격" 하나에서만 온다.

    NEXT_OPEN   신호 D → 진입 D+1 시가 → 청산 shift(D+1, h-1) 종가
    SAME_CLOSE  신호 D → 진입 D  종가  → 청산 shift(D,   h)   종가

`SAME_CLOSE`에서 `h-1`을 쓰면 노출이 하루 짧아져 비교가 성립하지 않는다.
그래서 `h`를 민다. 두 규칙은 **같은 날 청산**한다.

체결 가능성은 별개다 — 15:30에 확정된 종가를 15:35에 판정해서 사려면
시간외 종가매매(15:40~16:00)를 써야 한다. 계산이 되는 것과 실제로 살 수
있는 것은 다르다.
"""

from datetime import date
from decimal import Decimal

import pytest

from cvd_obv.domain.calendar import TradingCalendar
from cvd_obv.domain.errors import ConfigError
from cvd_obv.labels.forward_returns import BarSeries, ForwardReturnBuilder

DAYS = [date(2020, 1, d) for d in (2, 3, 6, 7, 8, 9, 10, 13, 14, 15)]


@pytest.fixture()
def calendar():
    return TradingCalendar("KRX", DAYS)


@pytest.fixture()
def series():
    bars = [{"security_id": "KRX:005930", "trading_date": d,
             "open": Decimal(str(100 + i)), "high": Decimal(str(110 + i)),
             "low": Decimal(str(90 + i)), "close": Decimal(str(105 + i)),
             "volume": 1000}
            for i, d in enumerate(DAYS)]
    return BarSeries("KRX:005930", bars)


def _row(calendar, series, rule, signal_date, horizon=3):
    builder = ForwardReturnBuilder(calendar, horizons=(horizon,),
                                   entry_rule=rule)
    rows = builder.build_security(series, run_id="t")
    return [r for r in rows if r["signal_date"] == signal_date][0]


def test_당일_종가에_산다(calendar, series):
    r = _row(calendar, series, "SAME_CLOSE", date(2020, 1, 3))
    assert r["entry_date"] == date(2020, 1, 3)       # 신호일 자신
    assert r["entry_price"] == Decimal("106")        # 그날 종가
    assert r["entry_rule"] == "SAME_CLOSE"


def test_익일_시가에_산다(calendar, series):
    r = _row(calendar, series, "NEXT_OPEN", date(2020, 1, 3))
    assert r["entry_date"] == date(2020, 1, 6)       # 다음 거래일
    assert r["entry_price"] == Decimal("102")        # 그날 시가


def test_두_규칙이_같은_날_청산한다(calendar, series):
    """노출 구간을 맞추는 것이 비교의 전제다."""
    a = _row(calendar, series, "NEXT_OPEN", date(2020, 1, 3))
    b = _row(calendar, series, "SAME_CLOSE", date(2020, 1, 3))
    assert a["exit_date"] == b["exit_date"] == date(2020, 1, 8)
    assert a["exit_price"] == b["exit_price"]


def test_차이는_진입가_하나에서만_온다(calendar, series):
    a = _row(calendar, series, "NEXT_OPEN", date(2020, 1, 3))
    b = _row(calendar, series, "SAME_CLOSE", date(2020, 1, 3))
    # 같은 청산가, 다른 진입가 → gross도 그만큼만 다르다
    expected = float(a["exit_price"] / b["entry_price"] - 1)
    assert b["stock_return_gross"] == pytest.approx(expected)


def test_진입일_장중_움직임은_세지_않는다(calendar):
    """종가에 샀으므로 그날의 고가·저가에는 노출되지 않는다.

    진입일을 창에 넣으면 **사기 전의** 움직임을 최대 유리/불리로 센다.
    그걸 잡으려고 진입일에만 큰 고가·낮은 저가를 둔 계열을 쓴다.
    """
    bars = []
    for i, d in enumerate(DAYS):
        spike = d == date(2020, 1, 3)
        bars.append({
            "security_id": "KRX:005930", "trading_date": d,
            "open": Decimal("100"),
            "high": Decimal("500") if spike else Decimal("101"),
            "low": Decimal("10") if spike else Decimal("99"),
            "close": Decimal("100"), "volume": 1000})
    spiky = BarSeries("KRX:005930", bars)

    same = _row(calendar, spiky, "SAME_CLOSE", date(2020, 1, 3))
    # 진입가 100, 이후 고가는 101뿐이므로 mfe는 1%여야 한다.
    assert same["mfe"] == pytest.approx(0.01)
    assert same["mae"] == pytest.approx(-0.01)

    # 대조: 같은 날을 창에 넣는 NEXT_OPEN은 그 다음 날부터라 역시 영향 없음.
    # 진입일 자체가 창에 들어가는 경우를 직접 확인한다.
    builder = ForwardReturnBuilder(calendar, horizons=(3,),
                                   entry_rule="SAME_CLOSE")
    high, low, _ = spiky.window_extremes(date(2020, 1, 3), date(2020, 1, 8))
    assert float(high) == 500.0 and float(low) == 10.0
    assert builder.entry_rule == "SAME_CLOSE"


def test_h가_커지면_청산이_그만큼_뒤로(calendar, series):
    one = _row(calendar, series, "SAME_CLOSE", date(2020, 1, 2), horizon=1)
    three = _row(calendar, series, "SAME_CLOSE", date(2020, 1, 2), horizon=3)
    assert one["exit_date"] == date(2020, 1, 3)      # shift(D, 1)
    assert three["exit_date"] == date(2020, 1, 7)    # shift(D, 3)


def test_청산이_진입보다_뒤가_아니면_거부한다(calendar, series):
    """h=0은 진입일 종가에 사서 진입일 종가에 파는 것이라 뜻이 없다."""
    with pytest.raises(ConfigError):
        ForwardReturnBuilder(calendar, horizons=(0,), entry_rule="SAME_CLOSE")


def test_모르는_규칙은_거부한다(calendar):
    with pytest.raises(ConfigError):
        ForwardReturnBuilder(calendar, entry_rule="LIMIT_ORDER")


def test_벤치마크도_같은_규칙을_쓴다(calendar, series):
    """전략은 종가에 사는데 벤치마크만 시가로 사면 초과수익이 틀어진다."""
    bench_bars = [{"security_id": "KRX:KOSPI200", "trading_date": d,
                   "open": Decimal(str(300 + i)), "high": Decimal(str(310 + i)),
                   "low": Decimal(str(290 + i)),
                   "close": Decimal(str(305 + i)), "volume": 1}
                  for i, d in enumerate(DAYS)]
    bench = BarSeries("KRX:KOSPI200", bench_bars)
    builder = ForwardReturnBuilder(calendar, horizons=(3,),
                                   entry_rule="SAME_CLOSE")
    rows = builder.build_security(series, run_id="t", benchmark=bench,
                                  benchmark_id="KRX:KOSPI200")
    r = [x for x in rows if x["signal_date"] == date(2020, 1, 3)][0]
    # 벤치마크 진입도 1/3 종가(306), 청산 1/8 종가(309)
    assert r["benchmark_return"] == pytest.approx(309 / 306 - 1)


# ─────────────────────────────────────────────────────────────
# NEXT_CLOSE — 마감 동시호가에 전량 매수 (실행 가능한 형태)
# ─────────────────────────────────────────────────────────────

def test_다음_거래일_종가에_산다(calendar, series):
    """전일 판정 → 다음 거래일 마감 동시호가 체결."""
    r = _row(calendar, series, "NEXT_CLOSE", date(2020, 1, 3))
    assert r["entry_date"] == date(2020, 1, 6)       # 다음 거래일
    assert r["entry_price"] == Decimal("107")        # 그날 종가
    assert r["entry_rule"] == "NEXT_CLOSE"


def test_동시호가_체결_시각은_정규장_마감이다(calendar, series):
    """동시호가는 종가를 만드는 절차다. 체결은 마감 시각에 일어난다.

    신호는 **전 거래일** 마감 뒤(15:35)이므로 주문 시각이 체결 시각보다
    앞선다 — SAME_CLOSE와 달리 순환이 없다.
    """
    r = _row(calendar, series, "NEXT_CLOSE", date(2020, 1, 3))
    # 2020년은 마감 15:30 → 06:30 UTC
    assert r["entry_at_utc"].hour == 6
    assert r["entry_at_utc"].minute == 30
    assert r["entry_at_utc"].date() == date(2020, 1, 6)


def test_NEXT_CLOSE도_노출이_h거래일이다(calendar, series):
    """진입일 종가부터라 h-1이 아니라 h를 민다."""
    r = _row(calendar, series, "NEXT_CLOSE", date(2020, 1, 3), horizon=3)
    assert r["entry_date"] == date(2020, 1, 6)
    assert r["exit_date"] == date(2020, 1, 9)        # shift(진입, 3)


def test_세_규칙의_노출이_모두_h다(calendar, series):
    """비교가 성립하려면 노출이 같아야 한다.

    NEXT_OPEN   open(D+1) ~ close(D+3)   3거래일
    NEXT_CLOSE  close(D+1) ~ close(D+4)  3거래일
    SAME_CLOSE  close(D)  ~ close(D+3)   3거래일
    """
    days = DAYS
    for rule, span in (("NEXT_OPEN", 3), ("NEXT_CLOSE", 3),
                       ("SAME_CLOSE", 3)):
        r = _row(calendar, series, rule, date(2020, 1, 3), horizon=3)
        i, j = days.index(r["entry_date"]), days.index(r["exit_date"])
        got = j - i + (1 if rule == "NEXT_OPEN" else 0)
        assert got == span, f"{rule}: 노출 {got}일"


def test_NEXT_CLOSE도_진입일_장중을_세지_않는다(calendar):
    bars = []
    for d in DAYS:
        spike = d == date(2020, 1, 6)                # 진입일
        bars.append({
            "security_id": "KRX:005930", "trading_date": d,
            "open": Decimal("100"),
            "high": Decimal("500") if spike else Decimal("101"),
            "low": Decimal("10") if spike else Decimal("99"),
            "close": Decimal("100"), "volume": 1000})
    spiky = BarSeries("KRX:005930", bars)
    r = _row(calendar, spiky, "NEXT_CLOSE", date(2020, 1, 3))
    assert r["mfe"] == pytest.approx(0.01)
    assert r["mae"] == pytest.approx(-0.01)


def test_규칙마다_label_version이_다르다():
    """PK에 entry_rule이 없으므로 버전으로 갈라야 서로 덮어쓰지 않는다."""
    from cvd_obv.contracts.label_schema import label_version_for

    versions = {label_version_for(r)
                for r in ("NEXT_OPEN", "NEXT_CLOSE", "SAME_CLOSE")}
    assert len(versions) == 3
    assert "next_close" in label_version_for("NEXT_CLOSE")

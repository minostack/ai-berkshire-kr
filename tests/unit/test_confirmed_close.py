# -*- coding: utf-8 -*-
"""확정 종가(15:30) 진입 가정 — 실행 불가능한 것을 실행 불가능한 채로 연다.

신호는 장 종료 후 15:35에 나오고, 체결은 같은 날 15:30 종가다. 체결이 신호보다
5분 앞선다. 동시호가는 그 종가를 **만드는** 절차이므로 확정 종가를 보고 낸
주문은 현실에 존재할 수 없다 — `assert_execution_after_signal`이 그래서 있다.

그래도 이 시나리오는 답이 있는 질문 하나에 대응한다: **"체결 문제가 전혀
없었다면 지표에 값어치가 있었는가"의 상한.** 그래서 가드를 없애는 대신
`confirmed_close_entry`라는 이름으로 **좁게** 연다.

여기 테스트들이 지키는 것
-------------------------
1. 기본값은 꺼져 있다 — 실수로 열리지 않는다.
2. SAME_CLOSE에서만 열린다 — 다른 규칙에서 켜면 거부한다.
3. 열어도 허용 범위는 "신호 당일" 하나다 — 신호일보다 **앞선 날짜**의 체결은
   이 가정으로도 설명되지 않으므로 그대로 막는다.
4. 켜면 `experiment_id`가 달라진다 — 정상 실험과 섞일 수 없다.
5. 끄면 기존 `experiment_id`가 그대로다 — 저장된 결과와 끊기지 않는다.
"""

from datetime import date

import pytest

from cvd_obv.backtest.engine import (
    SELECTION_POLICIES, BacktestEngine, BacktestSpec,
)
from cvd_obv.contracts.backtest_schema import (
    assert_execution_after_signal, assert_same_day_close_entry,
)
from cvd_obv.domain.errors import ConfigError, LookAheadError
from cvd_obv.labels.signal_labels import SignalLabelBuilder


def _trade(signal_day, entry_day, trade_id="t1"):
    return {"trade_id": trade_id,
            "signal_date": date(2026, 1, signal_day),
            "entry_date": None if entry_day is None else date(2026, 1, entry_day)}


# ── 가드 ──────────────────────────────────────────────────────

def test_신호_당일_체결은_통과한다():
    assert assert_same_day_close_entry([_trade(5, 5)]) == 1


def test_신호일보다_앞선_체결은_막는다():
    """확정 종가 가정으로도 설명되지 않는다. 그건 근사가 아니라 버그다."""
    with pytest.raises(LookAheadError) as err:
        assert_same_day_close_entry([_trade(5, 2)])
    assert "1건" in str(err.value)


def test_신호_다음날_체결도_통과한다():
    assert assert_same_day_close_entry([_trade(5, 6)]) == 1


def test_체결되지_않은_거래는_셀_것이_없다():
    assert assert_same_day_close_entry([_trade(5, None)]) == 1


def test_기존_가드는_같은_날_체결을_여전히_막는다():
    """`assert_same_day_close_entry`가 느슨한 것이지 원래 가드가 바뀐 게 아니다."""
    import datetime as dt

    signal_at = dt.datetime(2026, 1, 5, 6, 35, tzinfo=dt.timezone.utc)
    entry_at = dt.datetime(2026, 1, 5, 6, 30, tzinfo=dt.timezone.utc)
    with pytest.raises(LookAheadError):
        assert_execution_after_signal(
            [{"trade_id": "t1", "signal_at_utc": signal_at,
              "entry_at_utc": entry_at}])


# ── 설정 ──────────────────────────────────────────────────────

def test_기본값은_꺼져_있다():
    assert BacktestSpec().confirmed_close_entry is False
    assert SignalLabelBuilder().confirmed_close_entry is False


@pytest.mark.parametrize("rule", ["NEXT_OPEN", "NEXT_CLOSE"])
def test_SAME_CLOSE가_아니면_거부한다(rule):
    with pytest.raises(ConfigError):
        BacktestSpec(entry_rule=rule, confirmed_close_entry=True)
    with pytest.raises(ConfigError):
        SignalLabelBuilder(entry_rule=rule, confirmed_close_entry=True)


def test_SAME_CLOSE에서는_열린다():
    spec = BacktestSpec(entry_rule="SAME_CLOSE", confirmed_close_entry=True)
    assert spec.confirmed_close_entry is True
    assert spec.to_dict()["confirmed_close_entry"] is True


# ── 지문 ──────────────────────────────────────────────────────

def test_끄면_기존_실험_ID가_그대로다():
    """저장된 결과와 끊기지 않아야 한다. 이 값은 2026-09-21에 실측한 것이다."""
    spec = BacktestSpec(selection_policy="BUY_ALL", horizon_td=20,
                        max_positions=20, index_id="KOSPI200")
    assert spec.experiment_id() == "bt_126f2826cf7665276a33a1ff5059b9b4"
    assert "confirmed_close_entry" not in spec.to_dict()


def test_켜면_지문이_달라진다():
    off = BacktestSpec(entry_rule="SAME_CLOSE").experiment_id()
    on = BacktestSpec(entry_rule="SAME_CLOSE",
                      confirmed_close_entry=True).experiment_id()
    assert off != on


# ── Top 7 ─────────────────────────────────────────────────────

def test_BUY_TOP7이_있다():
    assert SELECTION_POLICIES["BUY_TOP7"] == ("buy_top", 7)


def test_BUY_TOP7은_상위_7종목만_고른다():
    from cvd_obv.domain.status import SignalStatus

    pairs = [({"security_id": f"KRX:{i:06d}", "trading_date": date(2026, 1, 5),
               "total_score": 100 - i, "status": SignalStatus.BUY_CANDIDATE,
               "rank": i}, {}) for i in range(1, 13)]
    spec = BacktestSpec(selection_policy="BUY_TOP7", max_positions=7)
    picked = BacktestEngine.select(spec, pairs)
    assert len(picked) == 7
    # 점수 내림차순이므로 i가 작은 쪽이 앞이다.
    assert [p[0]["security_id"] for p in picked] == [
        f"KRX:{i:06d}" for i in range(1, 8)]

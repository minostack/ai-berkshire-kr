# -*- coding: utf-8 -*-
"""시장제도 레지스트리 — 시행일 구간별 해석.

과거 날짜를 오늘의 제도로 다시 해석하면 백테스트 전체가 조용히 틀린다.
경계일 전후가 서로 다른 규칙으로 풀리는지를 본다.
"""

from datetime import date, datetime

import pytest

from cvd_obv.domain.errors import ConfigError
from cvd_obv.domain.ids import KST
from cvd_obv.domain.market_rules import (
    MarketRule, MarketRuleRegistry, default_registry,
)
from cvd_obv.domain.status import Session


@pytest.fixture(scope="module")
def registry():
    return default_registry()


def test_시행일_경계에서_규칙이_바뀐다(registry):
    assert registry.resolve("KRX", date(2026, 9, 13)).version == \
        "KRX-2016-08-01-regular1530"
    assert registry.resolve("KRX", date(2026, 9, 14)).version == \
        "KRX-2026-09-14-aftermarket"
    assert registry.resolve("KRX", date(2016, 7, 31)).version == \
        "KRX-2015-01-01-pre1530"
    assert registry.resolve("KRX", date(2016, 8, 1)).version == \
        "KRX-2016-08-01-regular1530"


def test_애프터마켓은_시행일_이전에_존재하지_않는다(registry):
    rule = registry.resolve("KRX", date(2026, 9, 16))
    assert rule.session_exists(date(2026, 9, 16), Session.AFTER)
    # 같은 규칙 객체라도 날짜가 시행일 이전이면 세션이 없다.
    assert not rule.session_exists(date(2026, 9, 13), Session.AFTER)
    assert rule.session_bounds(date(2026, 9, 13), Session.AFTER) is None


def test_정규장_종료시각이_구간마다_다르다(registry):
    def close(day):
        rule = registry.resolve("KRX", day)
        return rule.session_bounds(day, Session.REGULAR)[1].strftime("%H:%M")

    assert close(date(2016, 7, 29)) == "15:00"
    assert close(date(2016, 8, 1)) == "15:30"
    assert close(date(2026, 9, 16)) == "15:30"


def test_일봉_scope가_애프터마켓_시행으로_바뀐다(registry):
    """2026-09-14부터 키움 일봉 종가는 애프터마켓 최종 체결가다(실측)."""
    assert registry.resolve("KRX", date(2026, 9, 11)).daily_scope_of(
        date(2026, 9, 11)) == "REGULAR"
    assert registry.resolve("KRX", date(2026, 9, 14)).daily_scope_of(
        date(2026, 9, 14)) == "EXTENDED"


def test_일봉_확정시각(registry):
    def confirmed(day):
        return registry.resolve("KRX", day).daily_confirmed_at(day)

    assert confirmed(date(2026, 9, 11)).strftime("%H:%M") == "15:35"
    assert confirmed(date(2026, 9, 16)).strftime("%H:%M") == "20:05"


def test_세션_미정의_구간은_일봉_전용이다(registry):
    """1차 출처로 확인하지 않은 과거 구간은 세션을 만들어내지 않는다."""
    rule = registry.resolve("KRX", date(1990, 3, 2))
    assert rule.daily_only is True
    assert rule.sessions == {}
    assert rule.session_bounds(date(1990, 3, 2), Session.REGULAR) is None
    # 일봉 확정 시각은 보수적 가정값으로만 제공한다.
    assert rule.daily_confirmed_at(date(1990, 3, 2)).strftime("%H:%M") == "18:00"
    assert rule.verification_status != "VERIFIED"


def test_세션도_daily_only도_없는_규칙은_거부한다():
    with pytest.raises(ConfigError):
        MarketRule({"market_rule_version": "X", "market": "KRX",
                    "effective_from": "2020-01-01", "sessions": {}})


def test_적용할_규칙이_없으면_실행을_막는다():
    only_recent = MarketRuleRegistry([
        MarketRule({"market_rule_version": "KRX-2020",
                    "market": "KRX", "effective_from": "2020-01-01",
                    "sessions": {"REGULAR": {"start": 900, "end": 1530,
                                             "minutes": 390,
                                             "continuous_minutes": 380}}}),
    ])
    with pytest.raises(ConfigError) as exc:
        only_recent.resolve("KRX", date(2019, 12, 31))
    assert exc.value.detail_code == "MARKET_RULE_UNRESOLVED"


def test_버전_중복은_거부한다():
    payload = {"market_rule_version": "DUP", "market": "KRX",
               "effective_from": "2020-01-01",
               "sessions": {"REGULAR": {"start": 900, "end": 1530,
                                        "minutes": 390,
                                        "continuous_minutes": 380}}}
    with pytest.raises(ConfigError):
        MarketRuleRegistry([MarketRule(payload), MarketRule(dict(payload))])


def test_봉_완결_판정은_경계를_포함한다(registry):
    rule = registry.resolve("KRX", date(2026, 9, 16))
    as_of = datetime(2026, 9, 16, 15, 35, tzinfo=KST)
    assert rule.is_bar_complete(datetime(2026, 9, 16, 15, 35, tzinfo=KST), as_of)
    assert rule.is_bar_complete(datetime(2026, 9, 16, 15, 30, tzinfo=KST), as_of)
    assert not rule.is_bar_complete(
        datetime(2026, 9, 16, 15, 40, tzinfo=KST), as_of)


def test_알_수_없는_버전_조회는_거부한다(registry):
    with pytest.raises(ConfigError):
        registry.by_version("KRX-NOPE")

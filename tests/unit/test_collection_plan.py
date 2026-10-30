# -*- coding: utf-8 -*-
"""확증 표본 계산 — 데이터 수집 계획의 근거.

여기서 확인하는 핵심은 **발견과 확증의 계산이 다르다**는 것이다.

- 확증은 크기와 방향을 미리 지목한 단 하나의 검정이므로 단측을 쓸 수 있고,
  계수가 2.802에서 2.487로 내려간다. 필요 표본은 그 제곱만큼 준다.
- 필요량은 **추가분**이다. 이미 본 표본을 다시 세면 같은 우연을 두 번 센다.
"""

import math

import pytest

from cvd_obv.domain.errors import ConfigError
from cvd_obv.statistics.collection_plan import (
    TRADING_DAYS_PER_YEAR, confirmation_coefficient, plan_rows,
    required_new_observations,
)


def test_단측_계수가_양측보다_작다():
    """방향을 미리 걸면 문턱이 낮아진다 — 공짜가 아니라 약속의 대가다."""
    two = confirmation_coefficient(one_sided=False)
    one = confirmation_coefficient(one_sided=True)
    assert two == pytest.approx(2.8016, abs=1e-3)
    assert one == pytest.approx(2.4865, abs=1e-3)
    assert one < two


def test_단측이면_필요_표본이_약_79퍼센트다():
    """필요 표본은 계수의 제곱에 비례한다."""
    kwargs = dict(effect=0.001, se=0.0005, n=1000)
    two = required_new_observations(one_sided=False, **kwargs)
    one = required_new_observations(one_sided=True, **kwargs)
    assert one / two == pytest.approx((2.4865 / 2.8016) ** 2, rel=1e-2)


def test_효과가_절반이면_표본이_네_배다():
    """SE ∝ 1/sqrt(n)이므로 효과가 절반이면 네 배가 필요하다.

    올림(ceil) 때문에 몇 관측 어긋나는 것은 계산이 아니라 표기의 문제다.
    """
    big = required_new_observations(0.002, 0.0005, 1000)
    small = required_new_observations(0.001, 0.0005, 1000)
    assert small == pytest.approx(big * 4, abs=4)


def test_효과나_SE를_모르면_계산하지_않는다():
    """모르는 값을 만들어내지 않는다."""
    assert required_new_observations(None, 0.0005, 1000) is None
    assert required_new_observations(0.001, None, 1000) is None
    assert required_new_observations(0.001, 0.0005, 0) is None
    assert required_new_observations(0.0, 0.0005, 1000) is None


def test_부호가_반대여도_크기로_계산한다():
    """음의 효과도 같은 크기면 같은 표본이 필요하다."""
    assert (required_new_observations(-0.001, 0.0005, 1000)
            == required_new_observations(0.001, 0.0005, 1000))


def test_계획_행에_양측과_단측이_모두_들어간다():
    """단측만 실으면 '방향을 걸었다'는 조건이 표에서 사라진다."""
    rows = plan_rows([{"name": "후보", "effect": 0.0008, "se": 0.0003,
                       "n": 4094}])
    row = rows[0]
    assert row["required_new_two_sided"] > row["required_new_one_sided"]
    assert row["years_two_sided"] == pytest.approx(
        row["required_new_two_sided"] / TRADING_DAYS_PER_YEAR, abs=0.05)


def test_연_거래일이_0이면_거부한다():
    with pytest.raises(ConfigError):
        plan_rows([], days_per_year=0)


def test_실제_후보의_확증_규모가_십년_단위다():
    """선택지 C 후보(h=20): +0.0802%p, SE 0.0296%p, 4,094일에서 발견."""
    need = required_new_observations(0.000802, 0.000296, 4094,
                                     one_sided=True)
    assert 3000 < need < 4000
    assert 13 < need / TRADING_DAYS_PER_YEAR < 15

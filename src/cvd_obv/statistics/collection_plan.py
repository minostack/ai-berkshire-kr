# -*- coding: utf-8 -*-
"""확증에 필요한 표본 계산 — 데이터 수집 계획의 근거.

선택지 C에서 후보가 하나 나왔다(OBV가 가격 조건 위에 더한다, q=0.020~0.024).
사전 등록 규칙대로 **확증은 새 데이터로** 해야 한다. 그러면 질문은 하나다 —
**얼마나 필요한가?**

발견과 확증은 계산이 다르다
---------------------------
발견(discovery)은 "무엇이 있는지 모르고 찾는" 상황이라 양측 검정에 다중비교
보정까지 받는다. 확증(confirmation)은 **크기와 방향을 미리 지목한 단 하나의
검정**이다. 그래서

    발견: (z(1-α/2) + z(power)) x SE      ← 양측
    확증: (z(1-α)   + z(power)) x SE      ← 단측, 보정 없음

계수가 2.802에서 2.487로 내려가고, 필요 표본은 그 제곱만큼(약 79%) 줄어든다.
공짜로 얻는 것이 아니라 **방향을 미리 걸었기 때문에** 받는 이득이다. 결과가
반대로 나오면 그것으로 끝이다 — 사후에 양측으로 바꿔 읽으면 안 된다.

왜 "새" 표본이어야 하는가
-------------------------
이미 본 4,094일을 다시 세면 같은 우연을 두 번 세는 것이다. 확증 표본은
**발견에 쓰이지 않은 관측**이어야 하고, 그래서 필요량은 누적이 아니라
**추가분**으로 계산한다.
"""

from __future__ import annotations

import math

from ..domain.errors import ConfigError
from .power import POWER, z_quantile

__all__ = ["TRADING_DAYS_PER_YEAR", "confirmation_coefficient",
           "required_new_observations", "plan_rows"]

#: 연 거래일. 계획을 달력으로 옮길 때만 쓴다.
TRADING_DAYS_PER_YEAR = 245


def confirmation_coefficient(alpha: float = 0.05, power: float = POWER,
                             one_sided: bool = True) -> float:
    """검정 계수 — 단측이면 z(1-α), 양측이면 z(1-α/2)."""
    tail = alpha if one_sided else alpha / 2.0
    return z_quantile(1.0 - tail) + z_quantile(power)


def required_new_observations(effect, se, n, alpha: float = 0.05,
                              power: float = POWER, one_sided: bool = True):
    """관측된 크기의 효과를 확증하려면 **새로** 몇 관측이 필요한가.

    `se`는 `n`개 관측에서 잰 표준오차다. SE ∝ 1/sqrt(n)이므로

        필요 n' = n x (계수 x se / |효과|)^2

    효과나 SE를 모르면 None — 모르는 값을 만들어내지 않는다.
    """
    if not effect or not se or not n or n <= 0 or se <= 0:
        return None
    coefficient = confirmation_coefficient(alpha, power, one_sided)
    return int(math.ceil(n * (coefficient * se / abs(effect)) ** 2))


def plan_rows(candidates, alpha: float = 0.05, power: float = POWER,
              days_per_year: int = TRADING_DAYS_PER_YEAR):
    """후보마다 확증 표본을 계산해 계획 행으로.

    `candidates`는 `{"name", "effect", "se", "n"}`의 목록이다. 양측·단측을
    둘 다 낸다 — **단측을 쓰려면 방향을 미리 걸어야 한다**는 것을 표에서
    보이게 하려는 것이다.
    """
    if days_per_year <= 0:
        raise ConfigError("연 거래일은 0보다 커야 한다")

    rows = []
    for item in candidates:
        row = {
            "name": item["name"],
            "effect": item.get("effect"),
            "standard_error": item.get("se"),
            "discovery_n": item.get("n"),
            "note": item.get("note"),
        }
        for label, one_sided in (("two_sided", False), ("one_sided", True)):
            need = required_new_observations(
                item.get("effect"), item.get("se"), item.get("n"),
                alpha=alpha, power=power, one_sided=one_sided)
            row[f"required_new_{label}"] = need
            row[f"years_{label}"] = (round(need / days_per_year, 1)
                                     if need else None)
        rows.append(row)
    return rows

# -*- coding: utf-8 -*-
"""후보 판정 게이트 — 통계적 유의성만으로 통과시키지 않는다.

왜 필요한가
-----------
선택지 C에서 FDR을 통과한 후보가 나왔다. h=20에 **+0.0802%p**다. 통계적으로는
유의하지만, 20거래일 보유에 0.08%p면 겹치지 않게 굴려도 연 1%가 안 되고 Stage 2
비용 민감도에서 **비용을 2배로 하면 절대 수익이 사라졌다.**

즉 "유의하다"와 "거래할 가치가 있다"는 다른 질문이다. 지금까지는 두 번째
질문을 사람이 문장으로만 적었다. 그러면 다음 후보가 나왔을 때 기준이 또
흔들린다. 그래서 **다섯 축을 코드로 고정**한다.

다섯 축
-------
========== ===================================================================
통계       구간이 0을 넘지 않고, q값이 문턱 아래이며, 관측 효과가 MDE 이상인가
경제성     비용을 반영한 뒤에도 최소 초과수익을 넘는가 (연율로 환산해서 본다)
안정성     다른 유니버스나 새 기간에서 재현됐는가
실행가능성 그 종목을 그 크기로 실제로 살 수 있는가
확률품질   base rate만 찍는 예측기보다 Brier가 나은가
========== ===================================================================

판정 규칙
---------
- 축 하나라도 `FAIL`이면 전체 `FAIL`이다. **평균 내지 않는다** — 경제성이
  0이면 통계가 아무리 좋아도 거래할 이유가 없다.
- 판정할 자료가 없으면 `UNKNOWN`이고, `UNKNOWN`이 하나라도 있으면 전체는
  기껏해야 `INSUFFICIENT`다. **모르는 것을 통과로 세지 않는다.**
- 전부 `PASS`여야 `PASS`다.

기본 문턱은 어디서 왔나
-----------------------
`MIN_ANNUAL_EXCESS_BP = 100`(연 1%p)은 이 프로젝트에서 관측된 크기가 바로 이
언저리라서, "지금 후보가 간신히 걸리는" 위치에 일부러 뒀다. 임의의 값이라는
뜻이고, **사전 등록에서 바꿔 쓰라고 만든 기본값**이다. 결과를 보고 옮기면
그건 사후 선택이다.
"""

from __future__ import annotations

from ..domain.errors import ConfigError

__all__ = ["PASS", "FAIL", "UNKNOWN", "GateThresholds", "DEFAULT_THRESHOLDS",
           "TRADING_DAYS_PER_YEAR", "annualize", "evaluate_candidate"]

PASS = "PASS"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"

#: 연 거래일. `collection_plan`과 같은 값을 쓴다.
TRADING_DAYS_PER_YEAR = 245

_AXES = ("statistical", "economic", "stability", "executability",
         "probability")


class GateThresholds:
    """게이트 문턱. **실행 전에 고정하고 결과에 함께 적는다.**"""

    __slots__ = ("alpha_q", "min_annual_excess_bp", "min_observed_to_mde",
                 "min_daily_value_krw", "max_participation_rate",
                 "min_brier_improvement")

    def __init__(self, alpha_q: float = 0.05,
                 min_annual_excess_bp: float = 100.0,
                 min_observed_to_mde: float = 1.0,
                 min_daily_value_krw: float = 1e9,
                 max_participation_rate: float = 0.01,
                 min_brier_improvement: float = 0.0):
        if not 0 < alpha_q < 1:
            raise ConfigError("alpha_q는 0과 1 사이여야 한다")
        if min_annual_excess_bp < 0:
            raise ConfigError("최소 초과수익은 음수일 수 없다")
        self.alpha_q = alpha_q
        self.min_annual_excess_bp = min_annual_excess_bp
        self.min_observed_to_mde = min_observed_to_mde
        self.min_daily_value_krw = min_daily_value_krw
        self.max_participation_rate = max_participation_rate
        self.min_brier_improvement = min_brier_improvement

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


DEFAULT_THRESHOLDS = GateThresholds()


def annualize(effect_per_holding, horizon_td,
              days_per_year: int = TRADING_DAYS_PER_YEAR):
    """보유기간 한 번의 효과를 연율로. 겹치지 않게 굴린다고 가정한다.

    **복리로 부풀리지 않는다.** 관측값이 0.08%p 수준이라 단리와 복리의 차이는
    반올림보다 작고, 복리로 쓰면 없는 정밀도를 주장하게 된다.
    """
    if effect_per_holding is None or not horizon_td or horizon_td <= 0:
        return None
    return float(effect_per_holding) * (days_per_year / float(horizon_td))


def _statistical(c, t):
    """구간·q값·검정력을 함께 본다. 하나라도 어긋나면 FAIL."""
    ci_low, ci_high = c.get("ci_low"), c.get("ci_high")
    q, effect = c.get("q_value"), c.get("effect")
    mde = c.get("mde")
    if effect is None or ci_low is None or ci_high is None:
        return UNKNOWN, "구간이나 효과가 없다"
    if ci_low <= 0 <= ci_high:
        return FAIL, f"구간이 0을 포함한다 [{ci_low:+.4g}, {ci_high:+.4g}]"
    if q is None:
        return UNKNOWN, "q값이 없다"
    if q > t.alpha_q:
        return FAIL, f"q={q:.3f} > {t.alpha_q}"
    if mde is None:
        return UNKNOWN, "MDE가 없어 검정력을 알 수 없다"
    ratio = abs(effect) / mde if mde else None
    if ratio is None or ratio < t.min_observed_to_mde:
        return FAIL, (f"관측/MDE={ratio:.2f} < {t.min_observed_to_mde} "
                      f"— 검출 한계 아래다")
    return PASS, f"q={q:.3f}, 관측/MDE={ratio:.2f}"


def _economic(c, t):
    """비용을 뺀 뒤 연율로 환산해 본다."""
    effect = c.get("effect")
    horizon = c.get("horizon_td")
    if effect is None or not horizon:
        return UNKNOWN, "효과나 보유기간이 없다"
    cost = c.get("cost_per_round_trip")
    if cost is None:
        return UNKNOWN, "왕복 비용을 모른다 — 비용 전 수치로 통과시키지 않는다"
    net = float(effect) - float(cost)
    annual = annualize(net, horizon)
    annual_bp = annual * 10000.0 if annual is not None else None
    if annual_bp is None:
        return UNKNOWN, "연율 환산 실패"
    if annual_bp < t.min_annual_excess_bp:
        return FAIL, (f"비용 후 연 {annual_bp:.0f}bp "
                      f"< 문턱 {t.min_annual_excess_bp:.0f}bp")
    return PASS, f"비용 후 연 {annual_bp:.0f}bp"


def _stability(c, t):
    """다른 단면이나 새 기간에서 다시 나왔는가.

    `replication`은 `{"kind": "REPLICATION"|"CONFIRMATION", "same_sign": bool,
    "significant": bool, "correlation": ρ}` 꼴이다. ρ가 높으면 재현이라고
    부르기 어렵다 — 같은 것을 두 번 본 것이기 때문이다.
    """
    rep = c.get("replication")
    if not rep:
        return UNKNOWN, "재현 결과가 없다"
    if not rep.get("same_sign"):
        return FAIL, "부호가 재현되지 않았다"
    if not rep.get("significant"):
        return FAIL, "재현 표본에서 유의하지 않다"
    rho = rep.get("correlation")
    if rho is None:
        return UNKNOWN, "두 표본의 상관을 재지 않았다 — 독립이라 가정하지 않는다"
    if rho > 0.7:
        return FAIL, f"ρ={rho:.2f} — 같은 것을 두 번 본 것에 가깝다"
    kind = rep.get("kind", "REPLICATION")
    return PASS, f"{kind}, ρ={rho:.2f}"


def _executability(c, t):
    """살 수 있는 크기인가. 거래대금과 참여율을 함께 본다."""
    value = c.get("median_daily_value_krw")
    if value is None:
        return UNKNOWN, "일평균 거래대금이 없다"
    if value < t.min_daily_value_krw:
        return FAIL, (f"거래대금 {value/1e8:.1f}억 "
                      f"< 문턱 {t.min_daily_value_krw/1e8:.1f}억")
    part = c.get("participation_rate")
    if part is None:
        return UNKNOWN, "주문 참여율을 모른다"
    if part > t.max_participation_rate:
        return FAIL, (f"참여율 {part:.2%} > 문턱 "
                      f"{t.max_participation_rate:.2%}")
    return PASS, f"거래대금 {value/1e8:.1f}억, 참여율 {part:.2%}"


def _probability(c, t):
    """확률을 낼 것이라면, base rate보다 나아야 낸다."""
    brier = c.get("brier_score")
    base = c.get("brier_base_rate")
    if brier is None or base is None:
        return UNKNOWN, "Brier 비교가 없다"
    gain = float(base) - float(brier)
    if gain <= t.min_brier_improvement:
        return FAIL, f"Brier 개선 {gain:+.5f} — base rate를 이기지 못한다"
    return PASS, f"Brier 개선 {gain:+.5f}"


_CHECKS = {
    "statistical": _statistical,
    "economic": _economic,
    "stability": _stability,
    "executability": _executability,
    "probability": _probability,
}


def evaluate_candidate(candidate, thresholds: GateThresholds = None):
    """후보 하나를 다섯 축으로 판정한다.

    돌려주는 것은 판정 하나가 아니라 **축별 판정과 이유**다. 떨어졌을 때
    "무엇 때문에"가 남지 않으면 다음에 같은 논의를 다시 한다.
    """
    t = thresholds or DEFAULT_THRESHOLDS
    axes, reasons = {}, {}
    for axis in _AXES:
        verdict, why = _CHECKS[axis](candidate, t)
        axes[axis] = verdict
        reasons[axis] = why

    if FAIL in axes.values():
        overall = FAIL
        blocked = [a for a in _AXES if axes[a] == FAIL]
    elif UNKNOWN in axes.values():
        overall = "INSUFFICIENT"
        blocked = [a for a in _AXES if axes[a] == UNKNOWN]
    else:
        overall = PASS
        blocked = []

    return {
        "name": candidate.get("name"),
        "horizon_td": candidate.get("horizon_td"),
        "verdict": overall,
        "blocked_by": blocked,
        "axes": axes,
        "reasons": reasons,
        "thresholds": t.to_dict(),
    }

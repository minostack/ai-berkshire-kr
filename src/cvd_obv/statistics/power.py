# -*- coding: utf-8 -*-
"""검정력 — 로드맵 Stage 8.

"효과가 없다"와 "표본이 모자라 못 봤다"는 전혀 다른 말인데, 신뢰구간만
보고하면 둘이 섞인다. 구간이 [-1.0, +1.2]라면 그건 "효과가 0"이 아니라
**"-1.0과 +1.2 사이 어디든 될 수 있다"**는 뜻이다.

그래서 ablation 결과마다 **MDE**(minimum detectable effect)를 같이 낸다 —
유의수준 5%, 검정력 80%에서 이 표본으로 잡을 수 있었던 가장 작은 효과다.

    MDE = (z(1-α/2) + z(power)) x SE

관측 효과가 MDE보다 작으면, 그 효과가 진짜였더라도 **애초에 이 데이터로는
판정할 수 없었다.** 그때는 `UNDERPOWERED`로 적는다. "효과 없음"이라고 쓰면
하지 않은 말을 한 것이 된다.

그리고 "얼마나 더 있어야 하나"까지 답한다. 표준오차는 관측 수의 제곱근에
반비례하므로, 관측 효과를 잡으려면

    필요한 관측 수 = 현재 관측 수 x (MDE / 관측 효과)^2

만큼이 있어야 한다. 이게 "데이터를 더 모을 가치가 있는가"에 대한 답이다.

SE는 어디서 오는가
------------------
짝지음 차이 계열의 블록 부트스트랩 표준오차를 쓴다. 보유기간이 겹치므로
날짜를 독립으로 보면 SE가 작아지고, 그러면 MDE도 실제보다 작게 나와
"이만큼은 잡을 수 있었다"를 과장하게 된다.
"""

from __future__ import annotations

import math

__all__ = ["ALPHA", "POWER", "minimum_detectable_effect",
           "required_observations", "verdict_for", "z_quantile"]

ALPHA = 0.05
POWER = 0.80


def z_quantile(p: float) -> float:
    """표준정규 분위수. Acklam 근사(오차 ~1e-9)로 scipy 없이 쓴다."""
    if not 0.0 < p < 1.0:
        raise ValueError(f"0<p<1이어야 한다: {p}")
    a = (-3.969683028665376e+01, 2.209460984245205e+02,
         -2.759285104469687e+02, 1.383577518672690e+02,
         -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02,
         -1.556989798598866e+02, 6.680131188771972e+01,
         -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01,
         -2.400758277161838e+00, -2.549732539343734e+00,
         4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01,
         2.445134137142996e+00, 3.754408661907416e+00)
    low, high = 0.02425, 1.0 - 0.02425
    if p < low:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q
                + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > high:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q
                 + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r
            + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r
                            + b[4]) * r + 1)


def minimum_detectable_effect(standard_error, alpha: float = ALPHA,
                              power: float = POWER):
    """이 표본으로 잡을 수 있었던 최소 효과(양측 검정).

    `standard_error`가 없으면 None — 모르는 값을 만들어내지 않는다.
    """
    if standard_error is None or standard_error <= 0:
        return None
    return (z_quantile(1.0 - alpha / 2.0) + z_quantile(power)) * standard_error


def required_observations(value, mde, current_n):
    """관측 효과를 잡으려면 관측이 몇 개 필요한가.

    SE ∝ 1/sqrt(n)이므로 MDE도 같은 비율로 줄어든다. 관측 효과가 이미
    MDE보다 크면 현재 수로 충분하다.
    """
    if not value or not mde or not current_n or current_n <= 0:
        return None
    ratio = mde / abs(value)
    if ratio <= 1.0:
        return int(current_n)
    return int(math.ceil(current_n * ratio * ratio))


def verdict_for(value, ci_low, ci_high, mde):
    """기준선 대비 판정 — 로드맵 Stage 8 DoD.

    순서가 중요하다. **구간이 0을 비켜 갔으면 유의**이고, 0을 포함하면 그
    다음으로 "잡을 수 있었던 크기였나"를 본다. 관측값이 MDE보다 작으면
    `UNDERPOWERED`다 — 효과가 없다는 말이 아니라 **이 데이터로는 판정할 수
    없다**는 말이다.
    """
    if value is None or ci_low is None or ci_high is None:
        return "NOT_EVALUABLE"
    if ci_low * ci_high > 0:
        return "SIGNIFICANT_EDGE"
    if mde is None:
        return "NO_SIGNIFICANT_EDGE"
    if abs(value) < mde:
        return "UNDERPOWERED"
    return "NO_SIGNIFICANT_EDGE"

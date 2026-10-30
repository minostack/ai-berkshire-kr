# -*- coding: utf-8 -*-
"""두 유니버스의 의존성 — ρ를 "재고 나서" 쓴다.

왜 이 모듈이 필요한가
---------------------
선택지 C에서 나온 후보를 확증하려면 새 표본이 14~18년 필요하다. 기다릴 수
없으므로 **같은 기간의 다른 단면**(코스닥150)에서 재현을 시도한다. 그런데
같은 기간이면 두 유니버스는 같은 시장 요인을 공유한다. 코스피200에서 좋았던
날은 코스닥150에서도 좋았을 가능성이 높다.

그러면 "표본이 두 배"가 아니다. 얼마나 아닌지를 **재야** 한다.

수집 계획의 처음 규칙(ρ<0.3 쓰고 / 0.3~0.7 할인 / >0.7 버린다)은 읽기는
쉽지만 세 가지가 문제다.

1. **경계가 임의다.** 0.29와 0.31 사이에 통계적으로 아무 일도 일어나지 않는다.
2. **점 추정으로 판정한다.** 표본이 적으면 ρ 자체의 불확실성이 크다. ρ의
   신뢰구간이 [0.1, 0.8]인데 점 추정 0.35만 보고 "할인해서 쓴다"고 정할 수
   없다.
3. **버리거나 쓰거나뿐이다.** 실제로 필요한 것은 "이 의존성을 반영한 결합
   추정"이지 이분법이 아니다.

그래서 여기서는 **결합 추론**을 한다
-------------------------------------
두 계열을 **같은 날짜 블록으로 동시에** 재표집한다. 그러면 날짜가 공유하는
움직임이 재표집 안에 그대로 남고, 결합 추정의 신뢰구간이 의존성을 자동으로
반영한다. ρ를 문턱에 넣어 쓰는 대신 **분산에 들어가게** 하는 것이다.

    독립이라면   Var(결합평균) = σ²/2
    상관이 ρ면   Var(결합평균) = σ²(1+ρ)/2
    유효 독립 표본 수 = 2 / (1+ρ)

ρ=0이면 2개, ρ=1이면 1개다. 이 값을 함께 보고해서 "두 배"라는 말이 언제
성립하는지 눈에 보이게 한다.

**ρ에도 신뢰구간을 붙인다.** 같은 블록 부트스트랩에서 나오므로 추가 가정이
없다.

한계
----
- 두 계열의 **공통 거래일**만 쓴다. 코스닥150은 2015-07-13부터라 공통 구간이
  그만큼 짧아진다.
- 블록 길이는 보유기간 이상이어야 한다. 이 모듈은 검사만 하고 고르지는
  않는다 — 고르는 것은 사전 등록의 몫이다.
- 재현은 확증이 아니다. 같은 기간의 다른 단면에서 같은 부호가 나와도 **기간
  고유의 우연**은 배제되지 않는다.
"""

from __future__ import annotations

import math

import numpy as np

from ..domain.errors import StatisticalValidationError
from .inference import Estimate, normal_quantile

__all__ = ["align_by_date", "JointBootstrapResult", "joint_block_bootstrap",
           "effective_independent_studies"]


def effective_independent_studies(rho: float, k: int = 2) -> float:
    """상관 ρ로 묶인 `k`개 추정이 독립 몇 개 값어치인가.

    등분산·등상관을 가정한 고전적인 결과다.

        Var(평균) = σ²(1 + (k-1)ρ) / k  ⇒  유효 개수 = k / (1 + (k-1)ρ)

    ρ=0이면 k개, ρ=1이면 1개다. ρ가 음수면 1개보다 많아질 수 있는데, 그것은
    실제로 정보가 늘어난 것이 맞다(서로 반대로 움직이면 평균의 분산이 준다).
    """
    if k < 1:
        raise StatisticalValidationError("k는 1 이상이어야 한다")
    denominator = 1.0 + (k - 1) * float(rho)
    if denominator <= 0:
        return float(k)                    # 이론상 상한
    return float(k) / denominator


def align_by_date(series_a, series_b):
    """{날짜: 값} 두 개를 공통 날짜에서 맞춘다.

    `(dates, a, b)`를 돌려준다. 날짜는 정렬돼 있고, **정렬은 블록 부트스트랩의
    전제**다 — 블록이 "연속한 날"을 의미하려면 순서가 시간순이어야 한다.
    """
    common = sorted(set(series_a) & set(series_b))
    if not common:
        raise StatisticalValidationError("두 계열에 공통 날짜가 없다")
    a = np.array([float(series_a[d]) for d in common], dtype=np.float64)
    b = np.array([float(series_b[d]) for d in common], dtype=np.float64)
    return common, a, b


class JointBootstrapResult:
    """결합 부트스트랩 결과 하나.

    개별 추정·결합 추정·상관을 **같은 재표집**에서 낸다. 서로 다른 절차에서
    나온 숫자를 한 표에 섞지 않기 위해서다.
    """

    __slots__ = ("name_a", "name_b", "estimate_a", "estimate_b",
                 "pooled", "correlation", "correlation_ci_low",
                 "correlation_ci_high", "effective_studies",
                 "effective_studies_ci_low", "effective_studies_ci_high",
                 "n_dates", "block_length", "seed", "replicates",
                 "pooled_ci_width_ratio")

    def __init__(self, **kwargs):
        for slot in self.__slots__:
            setattr(self, slot, kwargs.get(slot))

    def to_row(self):
        """리포트 한 줄. 단위는 입력 단위 그대로다 — 여기서 바꾸지 않는다."""
        return {
            "name_a": self.name_a,
            "name_b": self.name_b,
            "n_dates": self.n_dates,
            "block_length": self.block_length,
            "seed": self.seed,
            "bootstrap_replicates": self.replicates,
            "mean_a": self.estimate_a.value,
            "mean_a_ci_low": self.estimate_a.ci_low,
            "mean_a_ci_high": self.estimate_a.ci_high,
            "mean_b": self.estimate_b.value,
            "mean_b_ci_low": self.estimate_b.ci_low,
            "mean_b_ci_high": self.estimate_b.ci_high,
            "pooled_mean": self.pooled.value,
            "pooled_ci_low": self.pooled.ci_low,
            "pooled_ci_high": self.pooled.ci_high,
            "correlation": self.correlation,
            "correlation_ci_low": self.correlation_ci_low,
            "correlation_ci_high": self.correlation_ci_high,
            "effective_studies": self.effective_studies,
            "effective_studies_ci_low": self.effective_studies_ci_low,
            "effective_studies_ci_high": self.effective_studies_ci_high,
            "pooled_ci_width_vs_independent": self.pooled_ci_width_ratio,
        }

    def verdict(self):
        """판정 — **점 추정이 아니라 구간으로** 읽는다.

        - `INDEPENDENT_ENOUGH`: ρ 상한이 0.3 미만. 두 단면을 사실상 별개로
          다뤄도 무리가 없다.
        - `REDUNDANT`: ρ 하한이 0.7 초과. 같은 것을 두 번 보는 것에 가깝다.
        - `DEPENDENT_USE_JOINT`: 그 사이. **버리지 않는다** — 결합 추정을
          쓰고 유효 표본을 그만큼 깎아서 보고한다.
        - `RHO_UNCERTAIN`: 구간이 0.3과 0.7을 모두 걸친다. 자료가 ρ조차
          제대로 잡지 못한다는 뜻이므로 결론을 미룬다.

        경계값 0.3·0.7은 **수집 계획에서 사전 등록한 값**을 그대로 쓴다.
        바꾸려면 계획을 고쳐야 한다 — 결과를 보고 옮기면 그게 사후 선택이다.
        """
        low, high = self.correlation_ci_low, self.correlation_ci_high
        if low is None or high is None:
            return "RHO_UNCERTAIN"
        if high < 0.3:
            return "INDEPENDENT_ENOUGH"
        if low > 0.7:
            return "REDUNDANT"
        if low < 0.3 and high > 0.7:
            return "RHO_UNCERTAIN"
        return "DEPENDENT_USE_JOINT"


def _moving_blocks(n_dates, block_length, rng):
    """이동블록 인덱스 한 벌. `inference.block_bootstrap_mean`과 같은 규칙."""
    n_blocks = int(math.ceil(n_dates / block_length))
    max_start = n_dates - block_length
    starts = rng.integers(0, max_start + 1, size=n_blocks)
    offsets = np.arange(block_length)
    return (starts[:, None] + offsets[None, :]).ravel()[:n_dates]


def joint_block_bootstrap(series_a, series_b, block_length: int,
                          name_a: str = "A", name_b: str = "B",
                          n_resamples: int = 2000, seed: int = 20260921,
                          level: float = 0.95) -> JointBootstrapResult:
    """두 유니버스를 **같은 날짜 블록으로 동시에** 재표집한다.

    핵심은 한 줄이다 — 매 반복에서 **한 벌의 날짜 인덱스**를 뽑아 두 계열에
    똑같이 적용한다. 따로 뽑으면 그 순간 두 계열을 독립으로 만들어 버리고,
    바로 그 가정이 우리가 검사하려는 것이다.

    `series_a`, `series_b`는 `{날짜: 그날의 값}`이다. 값은 보통 하루치 짝지음
    차이(delta)의 평균이고, 단위는 호출부가 정한다.
    """
    dates, a, b = align_by_date(series_a, series_b)
    n = len(dates)
    block_length = int(block_length)
    if block_length < 1:
        raise StatisticalValidationError("block_length는 1 이상이어야 한다")
    if n < block_length:
        raise StatisticalValidationError(
            f"공통 거래일({n})이 블록 길이({block_length})보다 적다")
    if n_resamples < 100:
        raise StatisticalValidationError("반복은 100회 이상이어야 한다")

    rng = np.random.default_rng(seed)
    means_a = np.empty(n_resamples)
    means_b = np.empty(n_resamples)
    pooled = np.empty(n_resamples)
    rhos = np.empty(n_resamples)

    for r in range(n_resamples):
        idx = _moving_blocks(n, block_length, rng)
        sa, sb = a[idx], b[idx]
        means_a[r] = sa.mean()
        means_b[r] = sb.mean()
        pooled[r] = 0.5 * (means_a[r] + means_b[r])
        # 분산이 0이면 상관은 정의되지 않는다 — 0으로 메우지 않는다.
        if sa.std() == 0 or sb.std() == 0:
            rhos[r] = np.nan
        else:
            rhos[r] = float(np.corrcoef(sa, sb)[0, 1])

    tail = (1.0 - level) / 2.0

    def _ci(sample):
        ok = sample[np.isfinite(sample)]
        if ok.size < n_resamples * 0.5:
            return None, None, None
        return (float(np.quantile(ok, tail)),
                float(np.quantile(ok, 1.0 - tail)),
                float(ok.std(ddof=1)))

    lo_a, hi_a, se_a = _ci(means_a)
    lo_b, hi_b, se_b = _ci(means_b)
    lo_p, hi_p, se_p = _ci(pooled)
    lo_r, hi_r, _ = _ci(rhos)

    rho_hat = (float(np.corrcoef(a, b)[0, 1])
               if a.std() > 0 and b.std() > 0 else None)

    detail = {"block_length": block_length, "seed": int(seed),
              "bootstrap_replicates": int(n_resamples), "n_dates": n,
              "first_date": str(dates[0]), "last_date": str(dates[-1])}

    est_a = Estimate(float(a.mean()), lo_a, hi_a, se_a,
                     method="JOINT_MOVING_BLOCK_BOOTSTRAP", level=level,
                     raw_n=n, effective_n=None, detail=dict(detail))
    est_b = Estimate(float(b.mean()), lo_b, hi_b, se_b,
                     method="JOINT_MOVING_BLOCK_BOOTSTRAP", level=level,
                     raw_n=n, effective_n=None, detail=dict(detail))
    est_p = Estimate(0.5 * (float(a.mean()) + float(b.mean())),
                     lo_p, hi_p, se_p,
                     method="JOINT_MOVING_BLOCK_BOOTSTRAP", level=level,
                     raw_n=2 * n, effective_n=None, detail=dict(detail))

    # 결합 구간이 "독립이었다면"보다 얼마나 넓은가 — 의존성의 대가를 폭으로.
    ratio = None
    if se_a and se_b and se_p:
        independent = 0.5 * math.sqrt(se_a ** 2 + se_b ** 2)
        if independent > 0:
            ratio = se_p / independent

    eff = (effective_independent_studies(rho_hat)
           if rho_hat is not None else None)
    # ρ가 클수록 유효 표본이 작아지므로 상·하한이 뒤집힌다.
    eff_low = (effective_independent_studies(hi_r)
               if hi_r is not None else None)
    eff_high = (effective_independent_studies(lo_r)
                if lo_r is not None else None)

    return JointBootstrapResult(
        name_a=name_a, name_b=name_b, estimate_a=est_a, estimate_b=est_b,
        pooled=est_p, correlation=rho_hat,
        correlation_ci_low=lo_r, correlation_ci_high=hi_r,
        effective_studies=eff, effective_studies_ci_low=eff_low,
        effective_studies_ci_high=eff_high, n_dates=n,
        block_length=block_length, seed=int(seed), replicates=n_resamples,
        pooled_ci_width_ratio=ratio)


# `normal_quantile`은 쓰지 않지만, 신뢰수준 검증을 한 곳에 모아 두기 위해
# 같은 표를 참조한다. 지원하지 않는 수준이면 여기서 먼저 막힌다.
def validate_level(level: float) -> float:
    normal_quantile(level)
    return level

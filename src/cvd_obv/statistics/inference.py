# -*- coding: utf-8 -*-
"""군집 구조를 반영한 추론 — 아키텍처 §6 `statistics`, 로드맵 Stage 8.

왜 필요한가
-----------
같은 날 200종목의 수익률은 독립이 아니다. 시장이 오르면 대부분 함께 오른다.
그 사실을 무시하고 "관측 130만 건"을 표본수로 쓰면 신뢰구간이 실제보다 10배쯤
좁아지고, 아무 의미 없는 차이가 "통계적으로 유의"해 보인다.

여기서 제공하는 것
------------------
- `icc_and_design_effect`: 날짜 내 상관(ICC)과 design effect, 유효표본수
- `cluster_robust_mean`: 날짜를 군집으로 한 robust 표준오차와 신뢰구간
- `block_bootstrap_mean`: 보유기간 중첩을 흡수하는 이동블록 부트스트랩

세 방법 모두 **어떻게 계산했는지**를 결과에 함께 담는다. 신뢰구간 숫자만 남고
방법이 사라지면 나중에 비교할 수 없다(로드맵 Stage 8 DoD).
"""

from __future__ import annotations

import math

import numpy as np

from ..domain.errors import StatisticalValidationError

__all__ = [
    "Estimate", "icc_and_design_effect", "cluster_robust_mean",
    "block_bootstrap_mean", "naive_mean", "normal_quantile",
]

#: 95% 양측 신뢰구간의 정규분포 임계값.
_Z = {0.90: 1.6448536269514722, 0.95: 1.959963984540054,
      0.99: 2.5758293035489004}


def normal_quantile(level: float) -> float:
    """양측 `level` 신뢰구간의 z값. 표에 없는 수준은 거부한다."""
    if level not in _Z:
        raise StatisticalValidationError(
            f"지원하지 않는 신뢰수준: {level} (가능: {sorted(_Z)})")
    return _Z[level]


class Estimate:
    """추정값 하나와 그 근거."""

    __slots__ = ("value", "ci_low", "ci_high", "se", "method", "level",
                 "raw_n", "effective_n", "detail")

    def __init__(self, value, ci_low=None, ci_high=None, se=None,
                 method="", level=0.95, raw_n=0, effective_n=None, detail=None):
        self.value = value
        self.ci_low = ci_low
        self.ci_high = ci_high
        self.se = se
        self.method = method
        self.level = level
        self.raw_n = int(raw_n)
        self.effective_n = effective_n
        self.detail = dict(detail or {})

    def to_dict(self):
        return {
            "value": self.value, "ci_low": self.ci_low, "ci_high": self.ci_high,
            "se": self.se, "ci_method": self.method, "ci_level": self.level,
            "raw_n": self.raw_n, "effective_n": self.effective_n,
            **self.detail,
        }

    def __repr__(self):
        return (f"Estimate({self.value:.4f}, CI=[{self.ci_low:.4f}, "
                f"{self.ci_high:.4f}], {self.method}, n={self.raw_n}, "
                f"n_eff={self.effective_n})")


def _as_arrays(values, clusters):
    y = np.asarray(values, dtype=np.float64)
    if y.size == 0:
        raise StatisticalValidationError("표본이 비어 있다")
    if not np.all(np.isfinite(y)):
        raise StatisticalValidationError("표본에 NaN/Inf가 있다 — 결측을 먼저 처리할 것")
    c = np.asarray(clusters)
    if c.shape[0] != y.shape[0]:
        raise StatisticalValidationError(
            f"표본과 군집 길이가 다르다: {y.shape[0]} vs {c.shape[0]}")
    # 문자열·날짜 무엇이든 정수 코드로 바꾼다.
    _, codes = np.unique(c, return_inverse=True)
    return y, codes


def naive_mean(values, level: float = 0.95) -> Estimate:
    """독립 가정 신뢰구간. **비교용으로만** 쓴다.

    날짜 군집을 무시하므로 실제보다 좁다. 군집 CI가 이보다 넓어지는지 확인하는
    기준선 역할이다.
    """
    y = np.asarray(values, dtype=np.float64)
    n = y.size
    if n < 2:
        raise StatisticalValidationError("표본이 2개 미만이다")
    mean = float(y.mean())
    se = float(y.std(ddof=1) / math.sqrt(n))
    z = normal_quantile(level)
    return Estimate(mean, mean - z * se, mean + z * se, se,
                    method="NAIVE_IID", level=level, raw_n=n, effective_n=n)


def icc_and_design_effect(values, clusters):
    """일원배치 분산분석 기반 ICC와 design effect.

      MSB = Σ_c n_c (ȳ_c − ȳ)² / (C − 1)
      MSW = Σ_c Σ_i (y_i − ȳ_c)² / (N − C)
      n₀  = (N − Σ n_c² / N) / (C − 1)
      ICC = (MSB − MSW) / (MSB + (n₀ − 1) MSW)

    음수 ICC는 0으로 자른다 — 군집이 오히려 분산을 줄인다는 추정은 표본 잡음일
    뿐이고, 유효표본을 실제보다 크게 잡는 쪽으로 쓰면 위험하다.
    `n₀`(평균이 아니라 조정 군집크기)를 쓰는 이유는 군집 크기가 다르기 때문이다.
    """
    y, codes = _as_arrays(values, clusters)
    n_total = y.size
    counts = np.bincount(codes).astype(np.float64)
    n_clusters = counts.size
    if n_clusters < 2:
        return {"icc": None, "design_effect": 1.0,
                "effective_n": float(n_total), "n_clusters": int(n_clusters),
                "mean_cluster_size": float(n_total), "adjusted_cluster_size": None}

    sums = np.bincount(codes, weights=y)
    cluster_means = sums / counts
    grand_mean = y.mean()

    ss_between = float(np.sum(counts * (cluster_means - grand_mean) ** 2))
    ss_within = float(np.sum((y - cluster_means[codes]) ** 2))
    df_between = n_clusters - 1
    df_within = n_total - n_clusters
    if df_within <= 0:
        return {"icc": None, "design_effect": 1.0,
                "effective_n": float(n_total), "n_clusters": int(n_clusters),
                "mean_cluster_size": float(n_total / n_clusters),
                "adjusted_cluster_size": None}

    ms_between = ss_between / df_between
    ms_within = ss_within / df_within
    n0 = (n_total - float(np.sum(counts ** 2)) / n_total) / df_between

    denom = ms_between + (n0 - 1.0) * ms_within
    icc = 0.0 if denom <= 0 else max(0.0, (ms_between - ms_within) / denom)
    deff = max(1.0, 1.0 + (n0 - 1.0) * icc)
    return {
        "icc": float(icc),
        "design_effect": float(deff),
        "effective_n": float(n_total / deff),
        "n_clusters": int(n_clusters),
        "mean_cluster_size": float(n_total / n_clusters),
        "adjusted_cluster_size": float(n0),
    }


def cluster_robust_mean(values, clusters, level: float = 0.95) -> Estimate:
    """날짜를 군집으로 한 robust 표준오차(CR1).

      SE² = C/(C−1) × Σ_c (Σ_{i∈c}(y_i − ȳ))² / N²

    유한표본 보정 `C/(C−1)`을 넣는다. 군집 수가 적을수록 보정이 커진다.
    """
    y, codes = _as_arrays(values, clusters)
    n_total = y.size
    n_clusters = int(np.bincount(codes).size)
    if n_clusters < 2:
        raise StatisticalValidationError(
            "군집이 2개 미만이면 군집 robust 추정을 할 수 없다")

    mean = float(y.mean())
    residual = y - mean
    cluster_sums = np.bincount(codes, weights=residual)
    correction = n_clusters / (n_clusters - 1.0)
    var = correction * float(np.sum(cluster_sums ** 2)) / (n_total ** 2)
    se = math.sqrt(max(var, 0.0))
    z = normal_quantile(level)

    stats = icc_and_design_effect(y, codes)
    return Estimate(mean, mean - z * se, mean + z * se, se,
                    method="CLUSTER_ROBUST_CR1", level=level, raw_n=n_total,
                    effective_n=stats["effective_n"],
                    detail={"n_clusters": n_clusters,
                            "icc": stats["icc"],
                            "design_effect": stats["design_effect"],
                            "finite_sample_correction": correction})


def block_bootstrap_mean(values, clusters, block_length: int,
                         n_resamples: int = 1000, seed: int = 20260101,
                         level: float = 0.95) -> Estimate:
    """군집(날짜) 축의 이동블록 부트스트랩.

    보유기간이 겹치면 이웃한 날짜의 수익률이 같은 가격 움직임을 공유한다.
    날짜를 하나씩 재표집하면 그 겹침이 사라져 신뢰구간이 다시 좁아지므로,
    연속한 `block_length`일을 통째로 뽑는다. 블록 길이는 최소 horizon 이상이어야
    한다(로드맵 Stage 8).

    재현을 위해 `seed`, 블록 길이, 반복 횟수를 결과에 남긴다.
    """
    y, codes = _as_arrays(values, clusters)
    order = np.unique(codes)
    n_clusters = order.size
    block_length = int(block_length)
    if block_length < 1:
        raise StatisticalValidationError("block_length는 1 이상이어야 한다")
    if n_clusters < block_length:
        raise StatisticalValidationError(
            f"군집 수({n_clusters})가 블록 길이({block_length})보다 작다")

    # 군집별 합계·개수만 있으면 평균을 다시 만들 수 있다 — 전체 행을 다시
    # 모으지 않아 메모리와 시간이 모두 줄어든다.
    counts = np.bincount(codes).astype(np.float64)
    sums = np.bincount(codes, weights=y)

    n_blocks = int(math.ceil(n_clusters / block_length))
    max_start = n_clusters - block_length
    rng = np.random.default_rng(seed)
    offsets = np.arange(block_length)

    means = np.empty(n_resamples, dtype=np.float64)
    for r in range(n_resamples):
        starts = rng.integers(0, max_start + 1, size=n_blocks)
        picked = (starts[:, None] + offsets[None, :]).ravel()
        total = float(sums[picked].sum())
        weight = float(counts[picked].sum())
        means[r] = total / weight if weight > 0 else np.nan

    means = means[np.isfinite(means)]
    if means.size < n_resamples * 0.5:
        raise StatisticalValidationError("부트스트랩 표본 중 절반 이상이 무효다")

    alpha = (1.0 - level) / 2.0
    low = float(np.quantile(means, alpha))
    high = float(np.quantile(means, 1.0 - alpha))
    stats = icc_and_design_effect(y, codes)
    return Estimate(float(y.mean()), low, high, float(means.std(ddof=1)),
                    method="MOVING_BLOCK_BOOTSTRAP", level=level,
                    raw_n=y.size, effective_n=stats["effective_n"],
                    detail={"block_length": block_length,
                            "bootstrap_replicates": int(means.size),
                            "seed": int(seed),
                            "n_clusters": int(n_clusters),
                            "icc": stats["icc"],
                            "design_effect": stats["design_effect"]})

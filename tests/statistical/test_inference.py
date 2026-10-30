# -*- coding: utf-8 -*-
"""군집 추론 — 합성 자료로 성질을 검증한다.

로드맵 §8: "합성 독립/군집 자료에서 CI coverage와 n_eff 방향성 검증".
아키텍처 §12: "synthetic clustered return에서 naive CI보다 cluster CI가 넓어지는지".

여기서 확인하는 것은 숫자가 예쁜지가 아니라 **방향이 맞는지**다.
군집 상관이 있으면 신뢰구간은 넓어져야 하고 유효표본수는 줄어야 한다.
그러지 않으면 이후 모든 "유의하다"는 판단이 거짓이 된다.
"""

import numpy as np
import pytest

from cvd_obv.domain.errors import StatisticalValidationError
from cvd_obv.statistics import (
    block_bootstrap_mean, cluster_robust_mean, icc_and_design_effect,
    naive_mean, normal_quantile,
)

SEED = 20260918


def _independent(n_dates=400, per_date=200, seed=SEED):
    """날짜 간·내 모두 독립인 자료."""
    rng = np.random.default_rng(seed)
    dates = np.repeat(np.arange(n_dates), per_date)
    values = rng.normal(0.0, 0.02, size=dates.size)
    return values, dates


def _clustered(n_dates=400, per_date=200, market_sd=0.015,
               idio_sd=0.02, seed=SEED):
    """날짜마다 공통 시장 충격이 있는 자료 — 실제 주식 패널의 구조."""
    rng = np.random.default_rng(seed)
    market = rng.normal(0.0, market_sd, size=n_dates)
    dates = np.repeat(np.arange(n_dates), per_date)
    values = market[dates] + rng.normal(0.0, idio_sd, size=dates.size)
    return values, dates


# ─────────────────────────────────────────────────────────────
# ICC와 유효표본수
# ─────────────────────────────────────────────────────────────

def test_독립_자료의_ICC는_0에_가깝다():
    values, dates = _independent()
    stats = icc_and_design_effect(values, dates)
    assert stats["icc"] < 0.01
    assert stats["design_effect"] < 1.5
    # 유효표본이 실제 표본과 크게 다르지 않아야 한다.
    assert stats["effective_n"] > 0.7 * values.size


def test_군집_자료는_유효표본이_크게_줄어든다():
    values, dates = _clustered()
    stats = icc_and_design_effect(values, dates)
    assert stats["icc"] > 0.2
    assert stats["design_effect"] > 10
    # 관측 8만 건이지만 유효표본은 그보다 훨씬 작다.
    assert stats["effective_n"] < 0.1 * values.size


def test_시장_충격이_클수록_유효표본이_줄어든다():
    weak = icc_and_design_effect(*_clustered(market_sd=0.005))
    strong = icc_and_design_effect(*_clustered(market_sd=0.03))
    assert strong["icc"] > weak["icc"]
    assert strong["effective_n"] < weak["effective_n"]


def test_ICC는_음수로_보고하지_않는다():
    """추정 잡음으로 음수가 나와도 유효표본을 부풀리지 않는다."""
    rng = np.random.default_rng(1)
    dates = np.repeat(np.arange(50), 4)
    values = rng.normal(size=dates.size)
    stats = icc_and_design_effect(values, dates)
    assert stats["icc"] >= 0.0
    assert stats["design_effect"] >= 1.0


def test_군집이_하나면_설계효과를_계산하지_않는다():
    stats = icc_and_design_effect([1.0, 2.0, 3.0], ["a", "a", "a"])
    assert stats["icc"] is None
    assert stats["design_effect"] == 1.0


# ─────────────────────────────────────────────────────────────
# 신뢰구간 폭
# ─────────────────────────────────────────────────────────────

def test_군집_자료에서_군집CI가_독립CI보다_넓다():
    """핵심 성질 — 이게 깨지면 모든 유의성 판단이 거짓이 된다."""
    values, dates = _clustered()
    naive = naive_mean(values)
    robust = cluster_robust_mean(values, dates)
    naive_width = naive.ci_high - naive.ci_low
    robust_width = robust.ci_high - robust.ci_low
    assert robust_width > 3 * naive_width


def test_독립_자료에서는_두_CI가_비슷하다():
    values, dates = _independent()
    naive = naive_mean(values)
    robust = cluster_robust_mean(values, dates)
    ratio = (robust.ci_high - robust.ci_low) / (naive.ci_high - naive.ci_low)
    assert 0.7 < ratio < 1.4


def test_블록_부트스트랩이_군집_구조를_반영한다():
    values, dates = _clustered()
    naive = naive_mean(values)
    boot = block_bootstrap_mean(values, dates, block_length=20,
                                n_resamples=300, seed=SEED)
    assert (boot.ci_high - boot.ci_low) > 3 * (naive.ci_high - naive.ci_low)
    assert boot.ci_low < boot.value < boot.ci_high


def test_같은_seed면_부트스트랩_결과가_같다():
    values, dates = _clustered(n_dates=120, per_date=30)
    a = block_bootstrap_mean(values, dates, 10, n_resamples=200, seed=7)
    b = block_bootstrap_mean(values, dates, 10, n_resamples=200, seed=7)
    assert (a.ci_low, a.ci_high) == (b.ci_low, b.ci_high)


def test_seed가_다르면_결과도_달라진다():
    values, dates = _clustered(n_dates=120, per_date=30)
    a = block_bootstrap_mean(values, dates, 10, n_resamples=200, seed=7)
    b = block_bootstrap_mean(values, dates, 10, n_resamples=200, seed=8)
    assert (a.ci_low, a.ci_high) != (b.ci_low, b.ci_high)


# ─────────────────────────────────────────────────────────────
# Coverage
# ─────────────────────────────────────────────────────────────

def test_군집_자료에서_군집CI의_coverage가_독립CI보다_낫다():
    """참값 0을 95% CI가 실제로 얼마나 덮는지 반복 실험으로 센다."""
    naive_hits = robust_hits = 0
    trials = 40
    for i in range(trials):
        values, dates = _clustered(n_dates=120, per_date=50, seed=1000 + i)
        if naive_mean(values).ci_low <= 0.0 <= naive_mean(values).ci_high:
            naive_hits += 1
        robust = cluster_robust_mean(values, dates)
        if robust.ci_low <= 0.0 <= robust.ci_high:
            robust_hits += 1
    # 독립 가정 CI는 명목 95%에 한참 못 미치고, 군집 CI는 그에 가깝다.
    assert naive_hits < robust_hits
    assert robust_hits >= 0.85 * trials
    assert naive_hits <= 0.6 * trials


# ─────────────────────────────────────────────────────────────
# 입력 검증
# ─────────────────────────────────────────────────────────────

def test_결측은_조용히_넘어가지_않는다():
    with pytest.raises(StatisticalValidationError):
        cluster_robust_mean([1.0, float("nan")], ["a", "b"])


def test_길이가_다르면_거부한다():
    with pytest.raises(StatisticalValidationError):
        cluster_robust_mean([1.0, 2.0], ["a"])


def test_빈_표본은_거부한다():
    with pytest.raises(StatisticalValidationError):
        cluster_robust_mean([], [])


def test_블록이_군집_수보다_길면_거부한다():
    values, dates = _clustered(n_dates=5, per_date=10)
    with pytest.raises(StatisticalValidationError):
        block_bootstrap_mean(values, dates, block_length=20)


def test_지원하지_않는_신뢰수준은_거부한다():
    with pytest.raises(StatisticalValidationError):
        normal_quantile(0.93)


def test_추정값이_방법과_근거를_함께_담는다():
    values, dates = _clustered(n_dates=100, per_date=20)
    est = cluster_robust_mean(values, dates)
    payload = est.to_dict()
    assert payload["ci_method"] == "CLUSTER_ROBUST_CR1"
    assert payload["ci_level"] == 0.95
    assert payload["n_clusters"] == 100
    assert payload["effective_n"] < payload["raw_n"]
    assert payload["icc"] > 0

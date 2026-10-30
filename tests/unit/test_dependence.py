# -*- coding: utf-8 -*-
"""두 유니버스 의존성 — ρ를 문턱이 아니라 분산에 넣는다.

여기서 지키려는 것은 하나다. **두 계열을 따로 재표집하면 안 된다.** 따로
뽑는 순간 독립이 가정으로 들어가고, 그 가정이 바로 검사 대상이다.
"""

import math

import numpy as np
import pytest

from cvd_obv.domain.errors import StatisticalValidationError
from cvd_obv.statistics.dependence import (
    align_by_date, effective_independent_studies, joint_block_bootstrap,
)


def _correlated(n, rho, seed=7, scale=0.01):
    """상관 ρ인 두 계열. 부트스트랩이 ρ를 되찾는지 확인하는 데 쓴다."""
    rng = np.random.default_rng(seed)
    z1 = rng.normal(size=n)
    z2 = rho * z1 + math.sqrt(max(0.0, 1 - rho ** 2)) * rng.normal(size=n)
    dates = [f"D{i:04d}" for i in range(n)]
    return (dict(zip(dates, z1 * scale)), dict(zip(dates, z2 * scale)))


def test_유효_독립_표본은_상관이_1이면_한_개다():
    """'표본이 두 배'라는 말이 언제 성립하는지가 이 식에 들어 있다."""
    assert effective_independent_studies(0.0) == pytest.approx(2.0)
    assert effective_independent_studies(1.0) == pytest.approx(1.0)
    assert effective_independent_studies(0.5) == pytest.approx(4.0 / 3.0)


def test_음의_상관이면_두_개보다_많아진다():
    """반대로 움직이면 평균의 분산이 줄어든다 — 정보가 정말 는 것이다."""
    assert effective_independent_studies(-0.5) > 2.0


def test_공통_날짜가_없으면_거부한다():
    with pytest.raises(StatisticalValidationError):
        align_by_date({"a": 1.0}, {"b": 2.0})


def test_공통_날짜만_정렬해서_쓴다():
    a = {"D2": 2.0, "D1": 1.0, "D3": 3.0}
    b = {"D3": 30.0, "D1": 10.0, "D9": 90.0}
    dates, xa, xb = align_by_date(a, b)
    assert dates == ["D1", "D3"]
    assert list(xa) == [1.0, 3.0]
    assert list(xb) == [10.0, 30.0]


def test_상관을_대략_되찾는다():
    """부트스트랩 ρ 구간이 참값을 덮어야 한다."""
    a, b = _correlated(600, 0.6)
    r = joint_block_bootstrap(a, b, block_length=20, n_resamples=300)
    assert r.correlation == pytest.approx(0.6, abs=0.12)
    assert r.correlation_ci_low < 0.6 < r.correlation_ci_high


def test_같은_블록을_두_계열에_함께_적용한다():
    """두 계열이 동일하면 결합 평균은 개별 평균과 **매 반복에서** 같다.

    따로 재표집했다면 두 평균이 어긋나 결합 구간이 더 좁아진다. 즉 이
    등식이 '한 벌의 인덱스를 공유한다'는 사실을 잡아낸다.
    """
    a, _ = _correlated(400, 0.0, seed=3)
    r = joint_block_bootstrap(a, dict(a), block_length=10, n_resamples=300)
    assert r.pooled.ci_low == pytest.approx(r.estimate_a.ci_low, rel=1e-12)
    assert r.pooled.ci_high == pytest.approx(r.estimate_a.ci_high, rel=1e-12)
    assert r.pooled_ci_width_ratio == pytest.approx(math.sqrt(2.0), rel=0.02)


def test_독립이면_결합_구간이_독립_가정과_비슷하다():
    """ρ가 0에 가까우면 의존성의 대가가 거의 없어야 한다."""
    a, b = _correlated(600, 0.0, seed=11)
    r = joint_block_bootstrap(a, b, block_length=20, n_resamples=400)
    assert r.pooled_ci_width_ratio == pytest.approx(1.0, abs=0.15)


def test_완전_상관이면_결합_구간이_독립_가정보다_넓다():
    """같은 것을 두 번 보면 구간이 줄지 않는다 — sqrt(2)배 쪽으로 간다."""
    a, b = _correlated(600, 0.98, seed=13)
    r = joint_block_bootstrap(a, b, block_length=20, n_resamples=400)
    assert r.pooled_ci_width_ratio > 1.3


def test_판정은_점_추정이_아니라_구간으로_한다():
    a, b = _correlated(800, 0.02, seed=17)
    assert joint_block_bootstrap(a, b, 20, n_resamples=400).verdict() \
        == "INDEPENDENT_ENOUGH"
    a, b = _correlated(800, 0.97, seed=19)
    assert joint_block_bootstrap(a, b, 20, n_resamples=400).verdict() \
        == "REDUNDANT"


def test_구간이_문턱을_모두_걸치면_결론을_미룬다():
    """표본이 적으면 ρ 자체가 불확실하다. 그때 '할인해서 쓴다'고 정할 수 없다."""
    a, b = _correlated(24, 0.5, seed=26)
    r = joint_block_bootstrap(a, b, block_length=5, n_resamples=300)
    assert r.correlation_ci_low < 0.3 and r.correlation_ci_high > 0.7
    assert r.verdict() == "RHO_UNCERTAIN"


def test_유효_표본_구간은_상관_구간과_반대로_뒤집힌다():
    """ρ가 클수록 유효 표본이 작으므로 상·하한이 서로 바뀐다."""
    a, b = _correlated(600, 0.5, seed=29)
    r = joint_block_bootstrap(a, b, 20, n_resamples=400)
    assert r.effective_studies_ci_low < r.effective_studies < \
        r.effective_studies_ci_high
    assert r.effective_studies_ci_low == pytest.approx(
        effective_independent_studies(r.correlation_ci_high))


def test_블록이_공통_거래일보다_길면_거부한다():
    a, b = _correlated(10, 0.3)
    with pytest.raises(StatisticalValidationError):
        joint_block_bootstrap(a, b, block_length=20, n_resamples=200)


def test_반복이_너무_적으면_거부한다():
    a, b = _correlated(200, 0.3)
    with pytest.raises(StatisticalValidationError):
        joint_block_bootstrap(a, b, block_length=5, n_resamples=50)


def test_같은_seed면_같은_결과다():
    a, b = _correlated(300, 0.4, seed=31)
    kw = dict(block_length=10, n_resamples=200, seed=555)
    assert (joint_block_bootstrap(a, b, **kw).to_row()
            == joint_block_bootstrap(a, b, **kw).to_row())


def test_분산이_0이면_상관을_0으로_메우지_않는다():
    """모르는 값을 만들어내지 않는다."""
    flat = {f"D{i:04d}": 1.0 for i in range(200)}
    other, _ = _correlated(200, 0.0, seed=37)
    other = {f"D{i:04d}": v for i, v in enumerate(other.values())}
    r = joint_block_bootstrap(flat, other, block_length=10, n_resamples=200)
    assert r.correlation is None
    assert r.verdict() == "RHO_UNCERTAIN"


def test_결과_행에_재현에_필요한_것이_다_있다():
    a, b = _correlated(300, 0.4, seed=41)
    row = joint_block_bootstrap(a, b, 10, name_a="KOSPI200",
                                name_b="KOSDAQ150", n_resamples=200).to_row()
    for key in ("name_a", "name_b", "n_dates", "block_length", "seed",
                "bootstrap_replicates", "pooled_mean", "correlation",
                "effective_studies"):
        assert key in row, key
    assert row["name_a"] == "KOSPI200" and row["name_b"] == "KOSDAQ150"

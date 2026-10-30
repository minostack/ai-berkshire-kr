# -*- coding: utf-8 -*-
"""VIDYA·CMO 단위 테스트 — 사전등록 `docs/PREREGISTRATION_VIDYA.md` §1.2 고정.

손으로 계산되는 성질만 본다. "그럴듯한 값이 나온다"는 테스트하지 않는다.
"""

import math

import numpy as np
import pytest

from cvd_obv.indicators import CMO_LENGTH, VIDYA_LENGTH, cmo, cmo_series, vidya


# ── CMO ──────────────────────────────────────────────────────

def test_계속_오르면_CMO는_1이다():
    p = list(range(1, 31))
    assert cmo(p, 9) == pytest.approx(1.0)


def test_계속_내리면_CMO는_마이너스1이다():
    p = list(range(30, 0, -1))
    assert cmo(p, 9) == pytest.approx(-1.0)


def test_움직임이_없으면_CMO는_0이다():
    """ΣUp + ΣDown = 0 — 0으로 나누지 않고 0을 준다."""
    assert cmo([100.0] * 30, 9) == pytest.approx(0.0)


def test_CMO는_손계산과_같다():
    # 변화량: +2, -1, +3, -4, +5  → ΣUp 10, ΣDown 5 → (10-5)/15
    p = [10, 12, 11, 14, 10, 15]
    assert cmo(p, 5) == pytest.approx(5.0 / 15.0)


def test_CMO_척도는_마이너스1에서_1이다():
    rng = np.random.default_rng(7)
    p = 1000 + np.cumsum(rng.normal(0, 10, 300))
    s = cmo_series(p, CMO_LENGTH)
    v = s[np.isfinite(s)]
    assert v.size > 250
    assert v.min() >= -1.0 and v.max() <= 1.0


def test_CMO는_앞_length개가_NaN이다():
    s = cmo_series(list(range(1, 21)), 9)
    assert np.all(np.isnan(s[:9]))
    assert np.all(np.isfinite(s[9:]))


def test_봉이_부족하면_CMO는_전부_NaN이다():
    assert np.all(np.isnan(cmo_series([1, 2, 3], 9)))
    assert math.isnan(cmo([1, 2, 3], 9))


def test_스칼라_CMO는_계열의_마지막과_같다():
    p = [10, 12, 11, 14, 10, 15, 17, 16, 20, 19, 22]
    assert cmo(p, 9) == pytest.approx(cmo_series(p, 9)[-1])


def test_CMO_길이가_0이면_오류다():
    with pytest.raises(ValueError):
        cmo_series([1, 2, 3], 0)


# ── VIDYA ────────────────────────────────────────────────────

def test_CMO가_1이면_VIDYA는_EMA와_같다():
    """단조 상승이면 |CMO| = 1이므로 α = k — 평범한 EMA가 된다."""
    p = np.arange(1.0, 61.0)
    n = 20
    got = vidya(p, n, 9)
    k = 2.0 / (n + 1.0)
    start = max(n - 1, 9)
    want = np.full(len(p), np.nan)
    want[start] = p[:n].mean()
    for i in range(start + 1, len(p)):
        want[i] = k * p[i] + (1 - k) * want[i - 1]
    np.testing.assert_allclose(got[start:], want[start:], rtol=1e-12)


def test_가격이_일정하면_VIDYA는_그_값에_머문다():
    out = vidya([100.0] * 60, 20, 9)
    assert out[-1] == pytest.approx(100.0)


def test_VIDYA는_가격_범위를_벗어나지_않는다():
    rng = np.random.default_rng(11)
    p = 1000 + np.cumsum(rng.normal(0, 15, 400))
    out = vidya(p, VIDYA_LENGTH, CMO_LENGTH)
    v = out[np.isfinite(out)]
    assert v.min() >= p.min() - 1e-9
    assert v.max() <= p.max() + 1e-9


def test_움직임이_없으면_VIDYA가_멈춘다():
    """α = k·|CMO| = 0 — 가격이 뛰어도 그 전 값을 유지한다."""
    p = [100.0] * 30 + [100.0] * 10
    out = vidya(p, 20, 9)
    assert out[-1] == pytest.approx(100.0)


def test_시드는_워밍업이_길면_영향이_없다():
    """사전등록 §1.2 — 신호 시작 250거래일 전부터 돌리는 근거.

    시드가 100봉 다른 두 계열을 300봉 뒤에서 비교한다. 완전히 같아지지는
    않는다 — α = k·|CMO|가 작은 구간에서는 옛 값이 천천히 빠진다. 상대오차
    1e-5(가격 1,000원에 0.01원)면 판정이 바뀌지 않는다.
    """
    rng = np.random.default_rng(3)
    p = 1000 + np.cumsum(rng.normal(0, 12, 400))
    full = vidya(p, VIDYA_LENGTH, CMO_LENGTH)
    cut = vidya(p[100:], VIDYA_LENGTH, CMO_LENGTH)
    assert full[-1] == pytest.approx(cut[-1], rel=1e-5)


def test_VIDYA는_시드_전이_NaN이다():
    out = vidya(np.arange(1.0, 41.0), 20, 9)
    start = max(20 - 1, 9)
    assert np.all(np.isnan(out[:start]))
    assert np.all(np.isfinite(out[start:]))


def test_봉이_부족하면_VIDYA는_전부_NaN이다():
    assert np.all(np.isnan(vidya([1, 2, 3], 20, 9)))


def test_기본값은_사전등록에_적힌_값이다():
    assert (CMO_LENGTH, VIDYA_LENGTH) == (9, 20)


def test_길이가_0이면_오류다():
    with pytest.raises(ValueError):
        vidya([1, 2, 3], 0, 9)

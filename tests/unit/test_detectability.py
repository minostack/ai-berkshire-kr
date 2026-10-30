# -*- coding: utf-8 -*-
"""검출 가능 구간 — 사전 등록 C의 H3.

이 표가 답하는 것은 "edge가 있는가"가 아니라 **"애초에 무엇을 잴 수
있는가"**다. 가설 검정이 아니므로 다중비교도 없다.

여기서 확인하는 성질:
- 선택 종목 수를 늘리면 하루 추정이 안정돼 MDE가 **작아진다**
- 표본(날짜)을 늘리면 MDE가 1/sqrt(n)로 준다
- 없는 값을 만들어내지 않는다
"""

import math

import pytest

from cvd_obv.domain.errors import ConfigError
from cvd_obv.statistics.detectability import (
    DEFAULT_HORIZONS, DEFAULT_SIZES, DetectabilityFrontier,
)

# 합성 저장소는 ablation 테스트의 것을 그대로 쓴다 — 같은 입력 계약이다.
from test_ablation import store  # noqa: F401
from cvd_obv.statistics.ablation import AblationRunner


@pytest.fixture(scope="module")
def frontier(store):  # noqa: F811
    return DetectabilityFrontier(
        AblationRunner(store, horizon_td=20), n_resamples=200)


def test_격자가_h1을_빼고_있다():
    """h=1은 2026-09-19에 접었다."""
    assert 1 not in DEFAULT_HORIZONS
    assert DEFAULT_SIZES[0] >= 1


def test_선택을_늘리면_MDE가_작아진다(frontier):
    """하루에 한 종목만 고르면 그날 추정이 그 한 종목에 휘둘린다."""
    narrow = frontier.cell(20, 1)
    wide = frontier.cell(20, 4)
    assert narrow["mde_80"] > wide["mde_80"]


def test_전부_고르면_잴_것이_없다(frontier):
    """모집단을 통째로 고르면 edge가 항상 0이라 분산도 MDE도 없다.

    그 자리에 0을 넣으면 '무한히 작은 효과도 잡을 수 있다'로 읽힌다.
    """
    row = frontier.cell(20, 8)          # 합성 저장소는 하루 8종목
    assert row["edge"] == pytest.approx(0.0, abs=1e-12)
    assert row["mde_80"] is None
    assert row["observed_over_mde"] is None


def test_표본을_늘리면_MDE가_제곱근으로_준다(frontier):
    """SE ∝ 1/sqrt(n)이므로 날짜가 4배면 MDE는 절반이다."""
    row = frontier.cell(20, 5, project_to=(1000, 4000))
    n = row["dates"]
    assert row["mde_at_1000"] == pytest.approx(
        row["mde_80"] * math.sqrt(n / 1000), rel=1e-9)
    # 4배 표본 -> 절반
    assert row["mde_at_4000"] / row["mde_at_1000"] == pytest.approx(
        math.sqrt(1000 / 4000), rel=1e-9)


def test_관측치와_MDE의_비가_기록된다(frontier):
    row = frontier.cell(20, 5)
    assert row["observed_over_mde"] == pytest.approx(
        abs(row["edge"]) / row["mde_80"], rel=1e-9)


def test_추론_설정이_칸마다_남는다(frontier):
    """어떤 방법으로 잰 구간인지 모르면 MDE도 해석할 수 없다."""
    row = frontier.cell(20, 5)
    assert row["inference_method"] in ("CLUSTER_ROBUST_CR1",
                                       "MOVING_BLOCK_BOOTSTRAP")
    assert row["block_length"] == 20
    assert row["cluster_key"] == "signal_date"
    assert row["seed"] == 20260919


def test_선택_크기가_모집단보다_크면_모집단만큼만_고른다(frontier):
    """합성 저장소는 하루 8종목이다."""
    row = frontier.cell(20, 50)
    assert row["mean_selection_size"] <= 8


def test_격자를_비우면_거부한다(frontier):
    with pytest.raises(ConfigError):
        frontier.run(horizons=(), sizes=(5,))
    with pytest.raises(ConfigError):
        frontier.run(horizons=(20,), sizes=())


def test_같은_h를_여러_크기로_봐도_한_번만_읽는다(frontier):
    """칸마다 테이블을 다시 읽으면 18칸에 18번 읽는다."""
    frontier._cache = {}
    frontier.cell(20, 3)
    frontier.cell(20, 5)
    assert list(frontier._cache) == [20]

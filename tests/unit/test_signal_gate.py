# -*- coding: utf-8 -*-
"""신호 성과 평가 — 로드맵 Stage 6.

여기서 확인하는 핵심은 **결론의 강도를 부풀리지 않는가**다.

- h일 보유면 이웃한 날짜의 신호가 같은 가격 구간을 공유한다. 날짜를 독립으로
  세면 신뢰구간이 겹침만큼 좁아진다 — 블록 부트스트랩과 비교해 넓은 쪽을 쓴다.
- 다중비교는 가족별로 보정한다. 확률 차이·수익 차이·순위상관은 다른 질문이다.
- 같은 수익률 차이가 결과별로 복제된 행을 두 번 세면 검정 수가 부풀려진다.
- 점수는 그날 안에서 줄 세운다. 절대 점수로 자르면 구간이 날마다 달라진다.
"""

from datetime import date, timedelta

import numpy as np
import pytest

from cvd_obv.backtest.common import apply_fdr
from cvd_obv.backtest.signal_gate import (
    SIGNAL_VARIANTS, SignalGateSpec, _daily_rank_ic, _delta_for, _rankdata,
    _variant_mask, _within_day_quintiles,
)
from cvd_obv.domain.errors import ConfigError


def _days(count, start=date(2026, 1, 5)):
    out, day = [], start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


# ─────────────────────────────────────────────────────────────
# 순위·분위
# ─────────────────────────────────────────────────────────────

def test_동점은_평균_순위를_받는다():
    """동점을 임의 순서로 두면 순위상관이 입력 순서에 따라 흔들린다."""
    assert list(_rankdata(np.array([1.0, 2.0, 2.0, 4.0]))) == [1.0, 2.5, 2.5, 4.0]
    left = _rankdata(np.array([5.0, 5.0, 1.0]))
    right = _rankdata(np.array([5.0, 1.0, 5.0]))
    assert sorted(left) == sorted(right)


def test_순위상관이_교과서_값과_같다():
    """d=[-1,1,-1,1,0] → 1 - 6*4/(5*24) = 0.8."""
    x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    y = np.array([2.0, 1.0, 4.0, 3.0, 5.0])
    ic, days = _daily_rank_ic(x, y, np.array(["d"] * 5), min_per_day=5)
    assert ic[0] == pytest.approx(0.8)
    assert days.size == 1


def test_완전히_뒤집힌_순서는_상관이_마이너스1이다():
    x = np.arange(10.0)
    ic, _ = _daily_rank_ic(x, -x, np.array(["d"] * 10))
    assert ic[0] == pytest.approx(-1.0)


def test_점수가_그날_안에서만_줄_세워진다():
    """절대 점수로 자르면 수준이 다른 날이 통째로 한 분위에 몰린다."""
    days = np.array(["a"] * 10 + ["b"] * 10)
    # b날은 점수 수준 자체가 100 높다.
    score = np.concatenate([np.arange(10.0), np.arange(10.0) + 100.0])
    q = _within_day_quintiles(score, days)
    assert set(q[:10]) == {1, 2, 3, 4, 5}
    assert list(q[:10]) == list(q[10:])


def test_표본이_얇은_날은_분위를_매기지_않는다():
    """5분위를 만들 수 없으면 0으로 두고 어느 분위에도 넣지 않는다."""
    days = np.array(["a"] * 3)
    q = _within_day_quintiles(np.array([1.0, 2.0, 3.0]), days)
    assert list(q) == [0, 0, 0]


def test_결측_점수는_분위에서_빠진다():
    days = np.array(["a"] * 6)
    score = np.array([1.0, 2.0, 3.0, 4.0, 5.0, np.nan])
    q = _within_day_quintiles(score, days)
    assert q[5] == 0
    assert set(q[:5]) == {1, 2, 3, 4, 5}


# ─────────────────────────────────────────────────────────────
# 선택 규칙
# ─────────────────────────────────────────────────────────────

def test_순위가_없는_신호는_상위_N에_들지_않는다():
    """rank가 null인 행을 0으로 채우면 전부 1등이 된다."""
    data = {"trading_date": np.array([date(2026, 1, 5)] * 3),
            "rank": np.array([1.0, np.nan, 25.0])}
    mask = _variant_mask("RANK_TOP20", data)
    assert list(mask) == [True, False, False]


def test_알_수_없는_조합은_거부한다():
    with pytest.raises(ConfigError):
        _variant_mask("NOPE", {"trading_date": np.array([])})


def test_사전_등록_목록이_전체_조합과_같다():
    """로드맵이 이름으로 지목한 것만 들어 있어야 한다."""
    from cvd_obv.backtest.signal_gate import PREREGISTERED
    assert PREREGISTERED == frozenset(SIGNAL_VARIANTS)


def test_연도_세그먼트는_거부한다():
    """replay 구간이 1년이라 연도 분해는 성립하지 않는다."""
    with pytest.raises(ConfigError):
        SignalGateSpec(segment_by="year")


# ─────────────────────────────────────────────────────────────
# 중첩을 반영한 신뢰구간
# ─────────────────────────────────────────────────────────────

def _overlapping_delta_inputs(n_days=120, horizon=20, seed=7):
    """이웃한 날짜가 같은 값을 공유하는(=중첩된) 합성 차이 계열.

    선택군에만 완만한 추세를 넣는다. 날짜를 독립으로 보면 이 추세가 '유의한
    차이'로 보이지만, 겹침을 반영하면 그렇지 않다.
    """
    rng = np.random.default_rng(seed)
    days = _days(n_days)
    shock = rng.normal(0, 0.01, size=n_days + horizon)
    dates, values, sel, base = [], [], [], []
    for i, day in enumerate(days):
        # 보유기간이 겹치므로 이웃 날짜가 같은 shock 구간을 공유한다.
        shared = float(shock[i:i + horizon].sum())
        for is_selected in (True, False):
            dates.append(day)
            values.append(shared + (0.0008 if is_selected else 0.0))
            sel.append(is_selected)
            base.append(True)
    return (np.asarray(values), np.asarray(dates), np.asarray(sel),
            np.asarray(base))


def test_보유기간이_겹치면_신뢰구간이_넓어진다():
    values, dates, sel, base = _overlapping_delta_inputs()
    spec = SignalGateSpec(n_resamples=400, seed=11)
    keys = ("v", "low", "high", "n", "method")

    wide = _delta_for(values, dates, sel, base, spec, 20, keys)
    narrow = _delta_for(values, dates, sel, base, spec, 1, keys)

    assert wide["method"] == "MOVING_BLOCK_BOOTSTRAP"
    assert (wide["high"] - wide["low"]) > (narrow["high"] - narrow["low"])
    assert wide["v"] == pytest.approx(narrow["v"])


def test_날짜가_블록보다_적으면_군집_구간만_쓴다():
    """블록을 만들 수 없을 때 조용히 넘어가지 않고 방법 이름으로 알린다."""
    values, dates, sel, base = _overlapping_delta_inputs(n_days=5, horizon=20)
    spec = SignalGateSpec(n_resamples=100)
    got = _delta_for(values, dates, sel, base, spec, 20,
                     ("v", "low", "high", "n", "method"))
    assert got["method"] == "CLUSTER_ROBUST_CR1"


# ─────────────────────────────────────────────────────────────
# 다중비교
# ─────────────────────────────────────────────────────────────

def _row(value, half, **extra):
    row = {"delta_return_net": value,
           "delta_return_ci_low": value - half,
           "delta_return_ci_high": value + half}
    row.update(extra)
    return row


def test_결과별로_복제된_행은_검정_하나로_센다():
    """수익률 차이는 up이든 up_gross든 같은 값이다."""
    rows = [_row(0.001, 0.0002, variant="A", horizon_td=1, segment=None,
                 outcome="up"),
            _row(0.001, 0.0002, variant="A", horizon_td=1, segment=None,
                 outcome="up_gross"),
            _row(0.0005, 0.0003, variant="B", horizon_td=1, segment=None,
                 outcome="up"),
            _row(0.0005, 0.0003, variant="B", horizon_td=1, segment=None,
                 outcome="up_gross")]
    apply_fdr(rows, dedupe_by=("variant", "horizon_td", "segment"))
    assert all(r["delta_return_net_fdr_tests"] == 2 for r in rows)
    # 묶인 행은 같은 q를 받는다.
    assert rows[0]["delta_return_net_q_value"] == \
        rows[1]["delta_return_net_q_value"]


def test_가족이_다르면_서로의_문턱에_영향을_주지_않는다():
    """조합이 많은 가족이 순위상관의 문턱을 좌우하면 안 된다."""
    rows = [_row(0.001, 0.0002) for _ in range(20)]
    rows.append({"rank_ic": -0.02, "rank_ic_ci_low": -0.039,
                 "rank_ic_ci_high": -0.001})
    apply_fdr(rows, value_key="delta_return_net")
    apply_fdr(rows, value_key="rank_ic", family="rank_ic")

    ic = rows[-1]
    assert ic["rank_ic_fdr_tests"] == 1
    assert "delta_return_net_q_value" not in ic
    assert all(r["delta_return_net_fdr_tests"] == 20 for r in rows[:-1])


def test_q값은_p값_순서를_지킨다():
    rows = [_row(0.001, 0.0002), _row(0.0001, 0.0009), _row(0.0005, 0.0003)]
    apply_fdr(rows)
    ordered = sorted(rows, key=lambda r: r["delta_return_net_p_value"])
    qs = [r["delta_return_net_q_value"] for r in ordered]
    assert qs == sorted(qs)
    assert all(r["p_value_method"] == "NORMAL_APPROX_FROM_CI" for r in rows)


def test_구간이_없는_행은_검정에서_빠진다():
    """추정할 수 없는 행을 0으로 넣으면 검정 수만 늘고 문턱이 헐거워진다."""
    rows = [_row(0.001, 0.0002),
            {"delta_return_net": None, "delta_return_ci_low": None,
             "delta_return_ci_high": None}]
    apply_fdr(rows)
    assert rows[0]["delta_return_net_fdr_tests"] == 1
    assert "delta_return_net_q_value" not in rows[1]

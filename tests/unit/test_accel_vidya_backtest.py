# -*- coding: utf-8 -*-
"""사전등록 VIDYA 백테스트의 벡터화가 스캐너 정의와 같은 값을 내는지 고정.

사전등록 `docs/PREREGISTRATION_VIDYA.md` §1.1 — 가속 지표는 스캐너
(`tools/cvd_accel_scanner.py`)의 정의를 그대로 쓴다. 16만 (종목,날)을 돌리려고
numpy로 다시 쓴 부분이 스캐너와 어긋나면 여기서 멈춘다.
"""

import os
import sys
from datetime import date, timedelta

import numpy as np
import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (os.path.join(_ROOT, "tools"), os.path.join(_ROOT, "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import ab_backtest_b as bb                      # noqa: E402
import accel_vidya_backtest as av               # noqa: E402
import cvd_accel_scanner as ac                  # noqa: E402


def _flows(vals):
    """스캐너 `daily_flows` 형식 — {date: {f, bars, complete}}."""
    base = date(2026, 1, 1)
    return {base + timedelta(days=i): {"f": v, "bars": 381, "complete": True}
            for i, v in enumerate(vals)}


# ── 가속 지표 ────────────────────────────────────────────────

def test_가속은_스캐너와_같은_값이다():
    vals = [0.10, -0.05, 0.22, 0.01, -0.11, 0.07, 0.30, -0.02, 0.15, 0.08]
    want = ac.accel_from_flows(_flows(vals), ac.RECENT_DAYS, ac.PRIOR_DAYS)

    f = np.full(40, np.nan)
    f[30:40] = vals                              # 마지막 10거래일에 흐름
    got = av.accel_from_flow(f)

    assert got["accel"][39] == pytest.approx(want["accel"])
    assert got["recent"][39] == pytest.approx(want["flow_recent"])
    assert got["prior"][39] == pytest.approx(want["flow_prior"])


def test_여러_기준일에서_스캐너와_같다():
    rng = np.random.default_rng(5)
    vals = rng.normal(0, 0.2, 60)
    f = np.asarray(vals, float)
    got = av.accel_from_flow(f)
    for end in (9, 20, 35, 59):
        want = ac.accel_from_flows(_flows(vals[:end + 1]),
                                   ac.RECENT_DAYS, ac.PRIOR_DAYS)
        assert got["accel"][end] == pytest.approx(want["accel"])
        assert got["recent"][end] == pytest.approx(want["flow_recent"])


def test_흐름이_10거래일_미만이면_가속이_없다():
    f = np.full(20, np.nan)
    f[12:20] = 0.1                               # 8거래일뿐
    got = av.accel_from_flow(f)
    assert not got["ok"][19]
    assert np.isnan(got["accel"][19])
    assert ac.accel_from_flows(_flows([0.1] * 8),
                              ac.RECENT_DAYS, ac.PRIOR_DAYS) is None


def test_중간에_빈_날이_있으면_백테스트는_신호를_내지_않는다():
    """사전등록 §1.1 조건 ① "10거래일 **연속**"의 결과.

    스캐너 코드는 `days[-10:]`으로 **있는 날 중 마지막 10개**를 쓴다(구멍이
    있어도 더 과거로 뻗는다). 백테스트는 사전등록대로 연속만 인정한다. 실제
    흐름 파일은 383/395종목이 259일 전부 있어 차이가 거의 없지만, 정의가
    다르다는 사실은 여기서 고정해 둔다.
    """
    f = np.full(30, np.nan)
    f[20:30] = 0.1
    f[24] = np.nan                               # 구멍 하나
    got = av.accel_from_flow(f)
    assert not got["ok"][29]

    holed = _flows([0.1] * 11)
    holed.pop(date(2026, 1, 5))                  # 11일 중 하나 제거 → 10일 남음
    assert ac.accel_from_flows(holed, ac.RECENT_DAYS, ac.PRIOR_DAYS) is not None


def test_가속_부호는_정의대로다():
    f = np.concatenate([np.full(7, -0.2), np.full(3, 0.3)])
    got = av.accel_from_flow(f)
    assert got["accel"][9] == pytest.approx(0.3 - (-0.2))
    assert got["recent"][9] == pytest.approx(0.3)
    assert got["prior"][9] == pytest.approx(-0.2)


# ── 이동평균·비용 ────────────────────────────────────────────

def test_이동평균은_앞이_NaN이고_값이_맞다():
    x = np.arange(1.0, 11.0)
    m = av.rolling_mean(x, 3)
    assert np.all(np.isnan(m[:2]))
    assert m[2] == pytest.approx(2.0)
    assert m[9] == pytest.approx(9.0)


def test_이동평균은_NaN을_전파한다():
    x = np.array([1.0, 2.0, np.nan, 4.0, 5.0])
    m = av.rolling_mean(x, 2)
    assert np.isnan(m[2]) and np.isnan(m[3])
    assert m[4] == pytest.approx(4.5)


def test_왕복비용은_0_409퍼센트다():
    c = av.json.load(open(os.path.join(_ROOT, "configs", "costs", "krx_v1.json"),
                          encoding="utf-8"))
    r = bb.net_return(100.0, 100.0, c["commission_rate"],
                      c["tax_rate_sell"], c["slippage_rate"])
    assert float(r) == pytest.approx(-0.00409, abs=1e-5)


# ── 사전등록 상수 ────────────────────────────────────────────

def test_사전등록_상수가_문서와_같다():
    assert (av.RECENT_DAYS, av.PRIOR_DAYS) == (3, 7)
    assert av.OVERHEAT_MAX == pytest.approx(1.10)
    assert (av.SIGNAL_START, av.SIGNAL_END) == (date(2025, 9, 12), date(2026, 9, 22))
    assert (av.PRIMARY_H, av.SECONDARY_H) == (2, 5)
    assert (av.CMO_LENGTH, av.VIDYA_LENGTH) == (9, 20)
    assert av.SEED == 20261012


def test_스캐너_상수와_같다():
    assert (av.RECENT_DAYS, av.PRIOR_DAYS) == (ac.RECENT_DAYS, ac.PRIOR_DAYS)
    assert av.OVERHEAT_MAX * 100 - 100 == pytest.approx(ac.OVERHEAT_MAX)


# ── 공유 로더에 더한 인자 ────────────────────────────────────

def test_로더의_load_from은_기본값이_B와_같다():
    """`load_all_bars(load_from=...)`를 더했다 — 기본값 호출은 B 그대로여야 한다."""
    import inspect
    sig = inspect.signature(bb.load_all_bars)
    assert sig.parameters["load_from"].default is None


@pytest.mark.skipif(not os.path.isdir(os.path.join(_ROOT, "data", "silver", "daily_bars")),
                    reason="로컬 일봉 저장소가 필요하다")
def test_좁은_창은_넓은_창의_꼬리다():
    sids = {"KRX:005930"}
    wide = bb.load_all_bars(sids)
    narrow = bb.load_all_bars(sids, load_from=date(2024, 6, 1))
    w = [r for r in wide["KRX:005930"] if r[0] >= date(2024, 6, 1)]
    assert w == narrow["KRX:005930"]

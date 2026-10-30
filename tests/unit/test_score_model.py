# -*- coding: utf-8 -*-
"""V1.5 점수·Gate 되짚기 — 로드맵 Stage 8의 토대.

Ablation의 전제는 **되짚은 점수가 V1.5와 같다**는 것이다. 다르면 "CVD를 뺀
점수"가 무엇을 뺀 것인지 알 수 없고, ablation 전부가 무의미해진다. 그래서
여기서 확인하는 것은 조립 규칙 자체다.

특히 백분위 모집단을 확인한다 — V1.5는 **그 항목의 품질이 SUCCESS인 종목**
으로만 백분위를 매긴다. 이걸 빠뜨리면 PARTIAL·FAILED 종목의 수치가 정상
종목의 상대 순위를 밀어 올린다(실제로 처음 구현에서 21%가 어긋났다).
"""

from datetime import date

import pytest

from cvd_obv.domain.errors import DataQualityError
from cvd_obv.domain.status import SignalStatus
from cvd_obv.statistics.score_model import (
    COMPONENTS, CVD_TERMS, ScoreModel, percentile_ranks,
)

DAY = date(2026, 3, 2)


def _row(sid, *, nd=0.5, nm=0.5, br=0.6, slope=0.5, obv=110.0, obv_ma20=100.0,
         liq=1.0e10, penalty=0.0, cvd_status="SUCCESS", obv_status="SUCCESS",
         price_status="SUCCESS", liq_status="SUCCESS",
         regular_status=None, momentum_status="OK",
         above_ma20=True, above_ma60=True, ma20_rising=True,
         ma20_above_ma60=True):
    return {
        "signal_id": sid, "security_id": sid,
        "cvd_normalized_delta": nd, "cvd_normalized_momentum": nm,
        "cvd_bull_ratio_completed": br, "cvd_momentum_status": momentum_status,
        "cvd_data_status": cvd_status,
        "cvd_regular_data_status": regular_status or cvd_status,
        "obv": obv, "obv_ma20": obv_ma20, "obv_normalized_slope": slope,
        "obv_data_status": obv_status,
        "above_ma20": above_ma20, "above_ma60": above_ma60,
        "ma20_rising": ma20_rising, "ma20_above_ma60": ma20_above_ma60,
        "price_data_status": price_status,
        "avg_traded_value_20d": liq, "liquidity_data_status": liq_status,
        "risk_penalty": penalty,
    }


# ─────────────────────────────────────────────────────────────
# 백분위
# ─────────────────────────────────────────────────────────────

def test_백분위는_동률에_평균_순위를_준다():
    """V1.5 `_percentile_ranks`와 같은 규칙이어야 한다."""
    assert percentile_ranks([1.0, 2.0, 3.0]) == [0.0, 0.5, 1.0]
    assert percentile_ranks([1.0, 2.0, 2.0, 3.0]) == [0.0, 0.5, 0.5, 1.0]


def test_유효값이_하나면_중립_0_5다():
    """상대비교가 불가능하다는 사실을 0이나 1로 바꾸면 안 된다."""
    assert percentile_ranks([None, 7.0, None]) == [None, 0.5, None]


def test_유효값이_없으면_전부_None이다():
    assert percentile_ranks([None, None]) == [None, None]


# ─────────────────────────────────────────────────────────────
# 점수 조립
# ─────────────────────────────────────────────────────────────

def test_만점_구성은_100점이다():
    """CVD 40 + OBV 35 + 가격 15 + 유동성 10 = 100."""
    rows = [_row("A", nd=1.0, nm=1.0, br=1.0, slope=1.0, liq=1.0e12),
            _row("B", nd=0.0, nm=0.0, br=0.0, slope=0.0, liq=1.0)]
    got = ScoreModel().score_day(rows)
    assert got[0] == 100.0
    # B는 백분위가 전부 0이지만 OBV>MA20(15)과 가격 15는 남는다.
    assert got[1] == 30.0


def test_백분위_모집단은_그_항목의_SUCCESS_종목만이다():
    """품질이 나쁜 종목의 수치가 정상 종목 순위를 밀어 올리면 안 된다."""
    good = [_row("A", nd=0.1), _row("B", nd=0.2)]
    # 품질 FAILED인데 값은 가장 큰 종목을 끼워 넣는다.
    polluted = good + [_row("C", nd=9.9, cvd_status="FAILED")]

    a_clean = ScoreModel().score_day(good)[0]
    a_dirty = ScoreModel().score_day(polluted)[0]
    assert a_clean == a_dirty, "FAILED 종목이 A의 백분위를 바꿨다"


def test_유동성_백분위는_유동성_품질로_거른다():
    rows = [_row("A", liq=1.0), _row("B", liq=2.0),
            _row("C", liq=9.9e15, liq_status="FAILED")]
    got = ScoreModel().score_day(rows)
    # C가 모집단에서 빠지므로 B가 최상위(10점)다.
    assert got[1] - got[0] == pytest.approx(10.0)


def test_risk_penalty는_그대로_더한다():
    plain = ScoreModel().score_day([_row("A")])[0]
    penalised = ScoreModel().score_day([_row("A", penalty=-5.0)])[0]
    assert penalised == pytest.approx(plain - 5.0)


def test_점수는_0과_100_사이로_자른다():
    assert ScoreModel().score_day([_row("A", penalty=-999.0)])[0] == 0.0


# ─────────────────────────────────────────────────────────────
# 성분 제거
# ─────────────────────────────────────────────────────────────

def test_CVD를_빼면_배점_40점이_사라진다():
    rows = [_row("A", nd=1.0, nm=1.0, br=1.0, slope=1.0, liq=1.0e12),
            _row("B", nd=0.0, nm=0.0, br=0.0, slope=0.0, liq=1.0)]
    full = ScoreModel().score_day(rows)[0]
    without = ScoreModel(["cvd"]).score_day(rows)[0]
    assert full - without == pytest.approx(40.0)


def test_CVD_항_하나만_뺄_수도_있다():
    rows = [_row("A", nd=1.0), _row("B", nd=0.0)]
    full = ScoreModel().score_day(rows)[0]
    without = ScoreModel(["cvd_delta"]).score_day(rows)[0]
    assert full - without == pytest.approx(20.0)


def test_CVD를_빼면_CVD_항도_같이_빠진다():
    """성분과 그 하위 항을 따로 세면 두 번 빼게 된다."""
    rows = [_row("A", nd=1.0, nm=1.0, br=1.0)]
    assert (ScoreModel(["cvd"]).score_day(rows)
            == ScoreModel(["cvd", "cvd_delta"]).score_day(rows))


def test_알_수_없는_이름은_거부한다():
    with pytest.raises(DataQualityError):
        ScoreModel(["nope"])


# ─────────────────────────────────────────────────────────────
# Gate
# ─────────────────────────────────────────────────────────────

def test_모든_절대조건을_채우면_BUY_CANDIDATE다():
    status, reasons = ScoreModel().gate_row(_row("A"))
    assert status == SignalStatus.BUY_CANDIDATE
    assert reasons == []


@pytest.mark.parametrize("kwargs,reason", [
    ({"nd": -0.1}, "cvd_delta"),
    ({"nm": -0.1}, "cvd_momentum"),
    ({"momentum_status": "WARMING_UP"}, "cvd_momentum_status"),
    ({"br": 0.49}, "cvd_bull_ratio"),
    ({"above_ma20": False}, "price_above_ma20"),
    ({"slope": -0.1}, "obv_trend"),
    ({"obv": 90.0}, "obv_trend"),
    ({"liq": None}, "liquidity_present"),
])
def test_조건_하나가_깨지면_WATCH가_되고_사유가_남는다(kwargs, reason):
    status, reasons = ScoreModel().gate_row(_row("A", **kwargs))
    assert status == SignalStatus.WATCH
    assert reason in reasons


def test_품질이_나쁘면_DATA_INSUFFICIENT다():
    status, _ = ScoreModel().gate_row(_row("A", obv_status="PARTIAL"))
    assert status == SignalStatus.DATA_INSUFFICIENT


def test_성분을_빼면_그_성분의_Gate_조건도_빠진다():
    """조건만 남기거나 배점만 남기면 차이가 어디서 왔는지 알 수 없다."""
    broken = _row("A", nd=-0.1)
    assert ScoreModel().gate_row(broken)[0] == SignalStatus.WATCH
    assert (ScoreModel(["cvd"]).gate_row(broken)[0]
            == SignalStatus.BUY_CANDIDATE)


def test_Gate를_통째로_빼도_데이터_품질은_남는다():
    """계산할 수 없는 종목이 후보에 들어오면 비교가 성립하지 않는다."""
    model = ScoreModel(["gate"])
    assert model.gate_row(_row("A", nd=-0.1))[0] == SignalStatus.BUY_CANDIDATE
    assert (model.gate_row(_row("A", obv_status="FAILED"))[0]
            == SignalStatus.DATA_INSUFFICIENT)


# ─────────────────────────────────────────────────────────────
# 검증기
# ─────────────────────────────────────────────────────────────

def test_되짚기가_다르면_검증이_실패한다():
    """검증기가 실제로 잡는지 본다 — 안 잡으면 있으나 마나다."""
    rows = [_row("A", nd=1.0), _row("B", nd=0.0)]
    by_day = {DAY: rows}
    with pytest.raises(DataQualityError) as exc:
        ScoreModel().verify(by_day, {"A": 0.0, "B": 0.0})
    assert exc.value.context["mismatch_n"] >= 1


def test_되짚기가_같으면_검증이_통과한다():
    rows = [_row("A", nd=1.0), _row("B", nd=0.0)]
    by_day = {DAY: rows}
    want = dict(zip(("A", "B"), ScoreModel().score_day(rows)))
    got = ScoreModel().verify(by_day, want)
    assert got["mismatches"] == 0 and got["rows"] == 2


def test_성분을_뺀_모델로는_검증할_수_없다():
    with pytest.raises(DataQualityError):
        ScoreModel(["cvd"]).verify({}, {})


def test_Gate_검증도_불일치를_잡는다():
    by_day = {DAY: [_row("A")]}
    with pytest.raises(DataQualityError):
        ScoreModel().verify_gate(by_day, {"A": SignalStatus.WATCH})


def test_제거_가능한_이름이_계약과_맞는다():
    assert set(COMPONENTS) == {"cvd", "obv", "price", "liquidity"}
    assert set(CVD_TERMS) == {"cvd_delta", "cvd_momentum", "cvd_bull_ratio"}

# -*- coding: utf-8 -*-
"""일봉 패널 신호 — 16년을 포트폴리오로 굴리기 위한 다리.

여기서 지키려는 것 세 가지.

1. **점수를 지어내지 않는다.** V1.5 총점은 CVD를 포함하므로 일봉만으로
   재현할 수 없다. 만들어 넣으면 "V1.5 검증"이 아니라 "새 지표 만들기"다.
2. **판정 불가와 거짓을 구별한다.** 데이터가 없어서 못 산 것과 조건에
   걸려서 안 산 것은 다른 사건이다.
3. **유니버스 이력을 반드시 통과한다.** 오늘의 구성종목을 과거에 적용하면
   생존편향이 결과의 부호까지 바꾼다(2026-09-18에 실제로 겪었다).
"""

from datetime import date, time

import pytest

from cvd_obv.backtest.daily_signals import (
    DAILY_SIGNAL_VERSION, GATE_VARIANTS, DailyPanelSignalEmitter,
)
from cvd_obv.domain.errors import ConfigError
from cvd_obv.domain.status import (
    EvidenceTier, FeatureStatus, SignalSource, SignalStatus,
)


class _FakeTable:
    def __init__(self, rows):
        self._rows = rows

    def to_pylist(self):
        return list(self._rows)


def _record(security="005930", day=date(2020, 3, 2), price=True, obv=True,
            liquidity=True, **status):
    row = {
        "security_id": security,
        "trading_date": day,
        "cond_price": price,
        "cond_obv": obv,
        "cond_liquidity": liquidity,
        "gate_all": bool(price and obv and liquidity),
        "avg_traded_value_20d": 5e9,
        "price_data_status": "SUCCESS",
        "obv_data_status": "SUCCESS",
        "liquidity_data_status": "SUCCESS",
        "feature_status": "SUCCESS",
        "feature_version": "F1-v15_daily-obv20",
    }
    row.update(status)
    return row


ALWAYS = {"005930": [(date(2000, 1, 1), None)],
          "000660": [(date(2000, 1, 1), None)]}


def _emit(records, variant="PRICE+OBV", intervals=None):
    emitter = DailyPanelSignalEmitter()
    return emitter.make_rows(_FakeTable(records), intervals or ALWAYS,
                             "KOSPI200", variant, "run-test")


# ── 점수를 만들지 않는다 ──────────────────────────────────────

def test_점수를_만들지_않는다():
    """CVD 없이 V1.5 총점을 재현할 수 없으므로 null로 둔다."""
    rows, _ = _emit([_record()])
    row = rows[0]
    assert row["total_score"] is None
    assert row["rank"] is None
    assert row["rank_population"] is None
    assert row["rank_percentile"] is None


def test_증거_계층을_섞지_않는다():
    rows, _ = _emit([_record()])
    assert rows[0]["signal_source"] == SignalSource.DAILY_PANEL
    assert rows[0]["daily_evidence_tier"] == EvidenceTier.DAILY_ONLY_LONG
    assert rows[0]["scanner_version"] == DAILY_SIGNAL_VERSION


# ── Gate 판정 ────────────────────────────────────────────────

def test_조건을_다_만족하면_BUY다():
    rows, _ = _emit([_record(price=True, obv=True)])
    assert rows[0]["status"] == SignalStatus.BUY_CANDIDATE
    assert rows[0]["gate_passed"] is True
    assert rows[0]["gate_reasons"] == []


def test_조건_하나라도_어긋나면_WATCH다():
    rows, _ = _emit([_record(price=True, obv=False)])
    assert rows[0]["status"] == SignalStatus.WATCH
    assert rows[0]["gate_passed"] is False
    assert "cond_obv:FALSE" in rows[0]["gate_reasons"]


def test_ALL은_아무_조건도_걸지_않는다():
    """대조군 모집단. 조건이 없으면 전부 BUY다."""
    rows, _ = _emit([_record(price=False, obv=False)], variant="ALL")
    assert rows[0]["status"] == SignalStatus.BUY_CANDIDATE


def test_유동성_조건은_PRICE_OBV에_없다():
    """선택지 C에서 유동성은 집합을 전혀 바꾸지 않는다고 확인됐다."""
    assert GATE_VARIANTS["PRICE+OBV"] == ("cond_price", "cond_obv")
    assert "cond_liquidity" in GATE_VARIANTS["GATE_ALL"]


def test_모르는_조합은_거부한다():
    with pytest.raises(ConfigError):
        _emit([_record()], variant="PRICE+CVD")


# ── 판정 불가 ≠ 거짓 ─────────────────────────────────────────

def test_상태가_SUCCESS가_아니면_거짓이_아니라_판정불가다():
    """데이터가 없어서 못 산 것과 조건에 걸려서 안 산 것은 다르다."""
    rows, _ = _emit([_record(obv_data_status="FAILED")])
    row = rows[0]
    assert row["status"] == SignalStatus.DATA_INSUFFICIENT
    assert row["feature_status"] == FeatureStatus.PARTIAL
    assert "cond_obv:UNDECIDABLE" in row["gate_reasons"]
    assert "cond_obv:FALSE" not in row["gate_reasons"]


def test_판정불가는_BUY로_세지_않는다():
    rows, _ = _emit([_record(price_data_status="PARTIAL")])
    assert rows[0]["gate_passed"] is False


def test_보지_않는_조건의_상태는_상관없다():
    """PRICE만 볼 때 OBV 상태가 나빠도 판정에 영향이 없어야 한다."""
    rows, _ = _emit([_record(obv_data_status="FAILED")], variant="PRICE")
    assert rows[0]["status"] == SignalStatus.BUY_CANDIDATE


# ── 유니버스 이력 ────────────────────────────────────────────

def test_구간_밖의_종목은_버린다():
    """생존편향 방어. 그날 구성종목이 아니었으면 신호가 아니다."""
    intervals = {"005930": [(date(2021, 1, 1), None)]}
    rows, skipped = _emit([_record(day=date(2020, 3, 2))],
                          intervals=intervals)
    assert rows == []
    assert skipped == 1


def test_구간_안이면_남긴다():
    intervals = {"005930": [(date(2019, 1, 1), date(2021, 1, 1))]}
    rows, skipped = _emit([_record(day=date(2020, 3, 2))],
                          intervals=intervals)
    assert len(rows) == 1 and skipped == 0


def test_편출_후_재편입_구간을_모두_본다():
    intervals = {"005930": [(date(2010, 1, 1), date(2015, 1, 1)),
                            (date(2020, 1, 1), None)]}
    inside, _ = _emit([_record(day=date(2012, 6, 1))], intervals=intervals)
    gap, skipped = _emit([_record(day=date(2017, 6, 1))],
                         intervals=intervals)
    assert len(inside) == 1
    assert gap == [] and skipped == 1


# ── look-ahead ───────────────────────────────────────────────

def test_신호_시각이_replay와_같다():
    """15:35 — Stage 5 replay와 같은 값이다.

    정규장은 15:30에 끝나고 종가도 그때 확정되지만 15:30 정각을 쓰면 판정
    여유가 0분이다. 두 경로가 다른 관행을 쓰면 나중에 섞어 읽게 된다.
    """
    rows, _ = _emit([_record(day=date(2020, 3, 2))])
    row = rows[0]
    assert row["signal_time_local"] == "15:35:00"
    assert row["trading_date"] == date(2020, 3, 2)
    # UTC로는 같은 날 06:35 (KST-9)
    assert row["signal_at_utc"].hour == 6
    assert row["signal_at_utc"].minute == 35


def test_시장_규칙_버전이_비어_있지_않다():
    """계약상 필수다. 일봉 feature와 같은 레지스트리를 써야 lineage가 맞는다."""
    rows, _ = _emit([_record(day=date(2020, 3, 2))])
    assert rows[0]["market_rule_version"]


# ── 결정성 ───────────────────────────────────────────────────

def test_같은_입력이면_signal_id가_같다():
    a, _ = _emit([_record()])
    b, _ = _emit([_record()])
    assert a[0]["signal_id"] == b[0]["signal_id"]


def test_조합이_다르면_signal_id가_다르다():
    """PRICE와 PRICE+OBV는 다른 실험이다 — 같은 ID를 쓰면 덮어쓴다."""
    a, _ = _emit([_record()], variant="PRICE")
    b, _ = _emit([_record()], variant="PRICE+OBV")
    assert a[0]["signal_id"] != b[0]["signal_id"]

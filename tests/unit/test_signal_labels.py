# -*- coding: utf-8 -*-
"""신호별 forward return 수집 — 로드맵 Stage 6.

여기서 확인하는 핵심은 **없는 값을 만들지 않는가**다.

- 거래정지·상장폐지·결측으로 가격이 없으면 수익률은 null이고 상태가 남는다.
  0으로 채우면 정지 종목이 "본전"으로 보이고, 그게 표본에 섞인다.
- 성숙하지 않은 구간은 `PENDING_MATURITY`다. 마지막 h거래일 신호를 0으로
  취급하면 최근 신호가 전부 무수익이 된다.
- 진입은 신호 시각보다 뒤다. 아니면 예외다.
- 진입·청산일은 거래 캘린더로 대조한다.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from cvd_obv.contracts.label_schema import (
    DAILY_FORWARD_RETURN_COLUMNS, DAILY_FORWARD_RETURN_PK, LABEL_VERSION,
    SIGNAL_FORWARD_RETURN_PK,
)
from cvd_obv.contracts.schema import SIGNAL_COLUMNS, SIGNAL_PK
from cvd_obv.domain.calendar import TradingCalendar
from cvd_obv.domain.errors import DataQualityError, LookAheadError
from cvd_obv.domain.status import LabelStatus
from cvd_obv.labels.signal_labels import NO_PANEL_ROW, SignalLabelBuilder
from cvd_obv.storage.parquet_store import ParquetStore

RUN_ID = "test_20260919000000_bbbbbbbb"
SECURITIES = ["KRX:000001", "KRX:000002", "KRX:000003"]


def _days(count, start=date(2026, 1, 5)):
    out, day = [], start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


DAYS = _days(40)
CALENDAR = TradingCalendar("KRX", DAYS)


def _signal_row(sid, day, status, score, rank):
    at = datetime(day.year, day.month, day.day, 6, 35, tzinfo=timezone.utc)
    return {
        "signal_id": f"sig_{sid[-6:]}_{day:%Y%m%d}",
        "signal_source": "REPLAY", "signal_at_utc": at,
        "signal_time_local": "15:35:00", "trading_date": day,
        "security_id": sid, "index_id": "KOSPI200",
        "universe_snapshot_id": "snap", "status": status,
        "gate_passed": status == "BUY_CANDIDATE", "gate_reasons": [],
        "total_score": score, "rank": rank, "rank_population": 3,
        "rank_percentile": None if rank is None else rank / 3.0,
        "feature_status": "SUCCESS", "daily_evidence_tier": "FULL_SIGNAL_SHORT",
        "schema_version": "2.0.0", "created_at_utc": at, "run_id": RUN_ID,
        "config_hash": "sha256:0" * 1, "code_revision": "rev",
        "scanner_version": "1.5.0",
        "market_rule_version": "KRX-2026-09-14-aftermarket",
        "data_revision": 1,
    }


def _panel_row(sid, day, horizon, status=LabelStatus.COMPLETE, net=0.01,
               entry=None, exit_date=None):
    complete = status == LabelStatus.COMPLETE
    entry = entry or (CALENDAR.next_trading_date(day) if complete else None)
    if complete and exit_date is None:
        exit_date = CALENDAR.shift(entry, horizon - 1)

    def _at(d):
        return (None if d is None
                else datetime(d.year, d.month, d.day, 6, 30,
                              tzinfo=timezone.utc))

    return {
        "security_id": sid, "signal_date": day, "horizon_td": horizon,
        "entry_rule": "NEXT_OPEN",
        "entry_date": entry, "exit_date": exit_date,
        "entry_at_utc": _at(entry), "exit_at_utc": _at(exit_date),
        "entry_price": Decimal("100") if complete else None,
        "exit_price": Decimal("101") if complete else None,
        "stock_return_gross": net + 0.004 if complete else None,
        "stock_return_net": net if complete else None,
        "benchmark_id": "KRX:KOSPI200",
        "benchmark_return": 0.002 if complete else None,
        "excess_return_gross": net + 0.002 if complete else None,
        "excess_return_net": net - 0.002 if complete else None,
        "mfe": 0.02 if complete else None, "mae": -0.01 if complete else None,
        "up": (net > 0) if complete else None,
        "up_gross": (net > 0) if complete else None,
        "outperform": (net > 0.002) if complete else None,
        "hit_3pct": False if complete else None,
        "hit_5pct": False if complete else None,
        "hit_minus_3pct": False if complete else None,
        "hit_minus_5pct": False if complete else None,
        "commission": 0.00015, "tax": 0.0018, "slippage": 0.001,
        "label_status": status,
        "status_reason": None if complete else "가격이 없다",
        "label_version": LABEL_VERSION, "cost_model_version": "krx_v1",
        "market_rule_version": "KRX-2026-09-14-aftermarket",
        "run_id": RUN_ID, "schema_version": "2.0.0", "data_revision": 1,
    }


@pytest.fixture()
def store(tmp_path):
    """신호 3종목 x 20일, 패널은 h=1,5."""
    st = ParquetStore(root=str(tmp_path))
    signals, panel = [], []
    for i, day in enumerate(DAYS[:20]):
        for j, sid in enumerate(SECURITIES):
            status = "BUY_CANDIDATE" if (i + j) % 4 == 0 else "WATCH"
            signals.append(_signal_row(sid, day, status, 10.0 * (j + 1), j + 1))
            for horizon in (1, 5):
                # 패널은 20거래일치뿐이다 — 마지막 h일 신호는 성숙 전이다
                pos = DAYS.index(day)
                mature = pos + horizon < 20
                panel.append(_panel_row(
                    sid, day, horizon,
                    status=(LabelStatus.COMPLETE if mature
                            else LabelStatus.PENDING_MATURITY),
                    net=0.01 if j else -0.01))

    st.write("gold", "signals", signals, SIGNAL_COLUMNS, SIGNAL_PK, RUN_ID,
             partition_fn=lambda r: [("index_id", r["index_id"])])
    st.write("gold", "daily_forward_returns", panel,
             DAILY_FORWARD_RETURN_COLUMNS, DAILY_FORWARD_RETURN_PK, RUN_ID,
             partition_fn=lambda r: [("horizon_td", r["horizon_td"])])
    return st


def _builder(store, horizons=(1, 5)):
    return SignalLabelBuilder(store, horizons=horizons, calendar=CALENDAR)


# ─────────────────────────────────────────────────────────────
# 조인과 유일성
# ─────────────────────────────────────────────────────────────

def test_신호마다_horizon별_행이_하나씩_생긴다(store):
    summary = _builder(store).build(RUN_ID, index_id="KOSPI200")
    assert summary.signals == 60
    assert summary.rows == 120
    assert summary.missing_panel == 0


def test_같은_signal_horizon_label_version은_유일하다(store):
    summary = _builder(store).build(RUN_ID, index_id="KOSPI200")
    rows = store.read("gold", "forward_returns", pk=SIGNAL_FORWARD_RETURN_PK)
    keys = [tuple(r[k] for k in SIGNAL_FORWARD_RETURN_PK) for r in rows]
    assert len(keys) == len(set(keys)) == summary.rows


def test_수익률을_다시_계산하지_않고_패널에서_가져온다(store):
    """조인이므로 패널 값과 글자 그대로 같아야 한다."""
    _builder(store).build(RUN_ID, index_id="KOSPI200")
    labels = {(r["security_id"], r["signal_date"], r["horizon_td"]):
              r["stock_return_net"]
              for r in store.read("gold", "forward_returns",
                                  pk=SIGNAL_FORWARD_RETURN_PK)}
    panel = {(r["security_id"], r["signal_date"], r["horizon_td"]):
             r["stock_return_net"]
             for r in store.read("gold", "daily_forward_returns",
                                 pk=DAILY_FORWARD_RETURN_PK)}
    for key, value in labels.items():
        assert value == panel[key]


# ─────────────────────────────────────────────────────────────
# 없는 값을 만들지 않는다
# ─────────────────────────────────────────────────────────────

def test_성숙하지_않은_구간은_PENDING_MATURITY로_남고_수익률이_null이다(store):
    _builder(store).build(RUN_ID, index_id="KOSPI200")
    rows = store.read("gold", "forward_returns", pk=SIGNAL_FORWARD_RETURN_PK)
    pending = [r for r in rows
               if r["label_status"] == LabelStatus.PENDING_MATURITY]
    assert pending, "성숙 전 행이 하나도 없다 — 픽스처를 확인하라"
    assert all(r["stock_return_net"] is None for r in pending)
    assert all(r["up"] is None for r in pending)


def test_거래정지로_청산가가_없으면_상태가_그대로_넘어온다(store):
    """0으로 채우면 정지 종목이 본전으로 보인다."""
    st = store
    day = DAYS[0]
    sid = SECURITIES[0]
    st.write("gold", "daily_forward_returns",
             [_panel_row(sid, day, 1, status=LabelStatus.EXIT_UNAVAILABLE)],
             DAILY_FORWARD_RETURN_COLUMNS, DAILY_FORWARD_RETURN_PK,
             RUN_ID + "_2", partition_fn=lambda r: [("horizon_td", 1)],
             part_key="halt")

    _builder(st, horizons=(1,)).build(RUN_ID, index_id="KOSPI200")
    rows = [r for r in st.read("gold", "forward_returns",
                               pk=SIGNAL_FORWARD_RETURN_PK)
            if r["security_id"] == sid and r["signal_date"] == day]
    assert len(rows) == 1
    assert rows[0]["label_status"] == LabelStatus.EXIT_UNAVAILABLE
    assert rows[0]["stock_return_net"] is None
    assert rows[0]["status_reason"]


def test_패널에_없는_종목날짜는_ENTRY_UNAVAILABLE이다(store):
    builder = _builder(store, horizons=(1, 5))
    signals = builder.load_signals("KOSPI200")
    panel = builder.load_panel(DAYS[0], DAYS[19])
    dropped = next(iter(panel))
    panel.pop(dropped)

    rows, missing = builder.make_rows(signals, panel, RUN_ID)
    assert missing == 1
    hit = [r for r in rows
           if (r["security_id"], r["signal_date"], r["horizon_td"]) == dropped]
    assert hit[0]["label_status"] == LabelStatus.ENTRY_UNAVAILABLE
    assert hit[0]["status_reason"] == NO_PANEL_ROW
    assert hit[0]["stock_return_net"] is None
    assert hit[0]["calendar_checked"] is False


# ─────────────────────────────────────────────────────────────
# 불변식
# ─────────────────────────────────────────────────────────────

def test_진입이_신호보다_앞서면_거부한다(store):
    builder = _builder(store, horizons=(1,))
    signals = builder.load_signals("KOSPI200")
    panel = builder.load_panel(DAYS[0], DAYS[19])
    key = next(k for k in panel if panel[k]["entry_at_utc"] is not None)
    row = panel[key]
    # 진입 시각을 신호 당일 새벽으로 되돌린다.
    day = row["signal_date"]
    row["entry_at_utc"] = datetime(day.year, day.month, day.day, 0, 0,
                                   tzinfo=timezone.utc)

    with pytest.raises(LookAheadError):
        builder.make_rows(signals, panel, RUN_ID)


def test_청산일이_캘린더와_맞지_않으면_거부한다(store):
    builder = _builder(store, horizons=(5,))
    signals = builder.load_signals("KOSPI200")
    panel = builder.load_panel(DAYS[0], DAYS[19])
    key = next(k for k in panel
               if k[2] == 5 and panel[k]["exit_date"] is not None)
    # h-1 거래일 뒤가 아니라 하루 더 뒤로 민다.
    panel[key]["exit_date"] = CALENDAR.shift(panel[key]["entry_date"], 5)

    with pytest.raises(DataQualityError) as exc:
        builder.make_rows(signals, panel, RUN_ID)
    assert "청산일" in exc.value.context["mismatches"][0]["problem"]


def test_진입일이_다음_거래일이_아니면_거부한다(store):
    builder = _builder(store, horizons=(1,))
    signals = builder.load_signals("KOSPI200")
    panel = builder.load_panel(DAYS[0], DAYS[19])
    key = next(k for k in panel if panel[k]["entry_date"] is not None)
    row = panel[key]
    row["entry_date"] = CALENDAR.shift(row["entry_date"], 2)
    row["exit_date"] = row["entry_date"]

    with pytest.raises(DataQualityError) as exc:
        builder.make_rows(signals, panel, RUN_ID)
    assert "진입일" in exc.value.context["mismatches"][0]["problem"]


def test_캘린더_대조_건수는_날짜가_있는_행만_센다(store):
    summary = _builder(store).build(RUN_ID, index_id="KOSPI200")
    rows = store.read("gold", "forward_returns", pk=SIGNAL_FORWARD_RETURN_PK)
    dated = sum(1 for r in rows if r["entry_date"] is not None)
    assert summary.calendar_checked == dated < summary.rows


def test_모든_행이_조인해서_왔다고_기록한다(store):
    _builder(store).build(RUN_ID, index_id="KOSPI200")
    rows = store.read("gold", "forward_returns", pk=SIGNAL_FORWARD_RETURN_PK)
    sources = {r["label_source"] for r in rows}
    assert sources == {"gold.daily_forward_returns"}

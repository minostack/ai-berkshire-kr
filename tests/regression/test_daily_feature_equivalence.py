# -*- coding: utf-8 -*-
"""일봉 feature가 V1.5와 같은 값을 내는지 대조한다.

Stage 2는 속도 때문에 V1.5 함수를 직접 호출하지 않고 누적합 기반 rolling으로
다시 구현했다. 그 선택이 정당하려면 **같은 값이 나온다는 증거**가 있어야 한다.
없으면 Stage 0에서 기준선을 동결한 의미가 사라진다.

비교 방식은 V1.5가 실제로 쓰는 방식과 같게 맞춘다. `analyze()`는 최근 약
220캘린더일(≈150거래일) 창을 잘라 `calc_obv`·`calc_price_trend`·
`calc_liquidity`에 넘긴다. 여기서도 같은 길이의 창을 잘라 호출한다.

OBV 절대값은 비교하지 않는다. 누적 시작점이 다르면 상수만큼 어긋나기 때문이다.
대신 Gate가 실제로 보는 값 — `obv - obv_ma20`, `normalized_slope` — 을 본다.
"""

import math
import os
import random
from datetime import date, timedelta

import pytest

from cvd_obv.baseline.scanner_v15 import load_scanner_v15
from cvd_obv.features.daily_features import DailyFeatureBuilder

V15_WINDOW_BARS = 150     # analyze()의 220캘린더일에 해당하는 거래일 수
OBV_LOOKBACK = 20
SEED = 20260918


def _trading_days(count, start=date(2015, 1, 5)):
    days, day = [], start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def _synthetic_bars(n=400, seed=7):
    """결정적 합성 일봉. 등락·거래정지·보합을 모두 포함한다."""
    rng = random.Random(seed)
    days = _trading_days(n)
    bars, price = [], 50000.0
    for i in range(n):
        # 보합일(종가 동일)을 일부러 섞는다 — OBV 부호 규칙의 경계다.
        if i % 37 == 0 and bars:
            close = bars[-1]["close"]
        else:
            price *= math.exp(rng.uniform(-0.03, 0.031))
            close = round(price, 0)
        volume = 0 if i % 91 == 0 else rng.randint(1000, 5_000_000)
        bars.append({"trading_date": days[i], "close": float(close),
                     "volume": volume,
                     "open": float(close), "high": float(close),
                     "low": float(close)})
    return bars


def _v15_window(bars, end_index, size=V15_WINDOW_BARS):
    start = max(0, end_index + 1 - size)
    return bars[start:end_index + 1]


@pytest.fixture(scope="module")
def scanner():
    return load_scanner_v15()


@pytest.fixture(scope="module")
def builder():
    return DailyFeatureBuilder(obv_lookback=OBV_LOOKBACK)


def _rows_for(builder, bars):
    return builder.build_security("KRX:000001", bars, "test_run")


@pytest.mark.parametrize("seed", [7, 11, 23])
def test_합성_시계열에서_V15와_값이_일치한다(scanner, builder, seed):
    bars = _synthetic_bars(seed=seed)
    rows = _rows_for(builder, bars)

    rng = random.Random(SEED + seed)
    # 워밍업이 끝난 구간에서만 비교한다(V1.5도 그 구간에서만 값을 낸다).
    indices = rng.sample(range(V15_WINDOW_BARS, len(bars)), 40)

    for i in indices:
        window = _v15_window(bars, i)
        row = rows[i]

        obv15 = scanner.calc_obv(window, divergence_lookback=OBV_LOOKBACK)
        price15 = scanner.calc_price_trend(window)
        liq15 = scanner.calc_liquidity(window)

        # OBV 절대값은 누적 시작점에 의존하므로 차분으로 비교한다.
        assert row["obv"] is not None and row["obv_ma20"] is not None
        gap_v2 = row["obv"] - row["obv_ma20"]
        gap_v15 = obv15["obv"] - obv15["obv_ma20"]
        assert gap_v2 == pytest.approx(gap_v15, abs=1.0), f"i={i} obv gap"
        assert row["obv_above_ma20"] is (obv15["obv"] > obv15["obv_ma20"])

        assert row["obv_normalized_slope"] == pytest.approx(
            obv15["normalized_slope"], rel=1e-12), f"i={i} normalized_slope"
        assert row["obv_up_ratio"] == pytest.approx(
            obv15["up_ratio"], rel=1e-12), f"i={i} up_ratio"
        assert row["obv_slope"] == pytest.approx(obv15["slope"], abs=1.0)
        assert row["obv_data_status"] == obv15["data_status"]

        assert row["above_ma20"] is price15["above_ma20"], f"i={i} above_ma20"
        assert row["above_ma60"] is price15["above_ma60"]
        assert row["above_ma120"] is price15["above_ma120"]
        assert row["ma20_rising"] is price15["ma20_rising"], f"i={i} ma20_rising"
        assert row["ma60_rising"] is price15["ma60_rising"]
        assert row["ma20_above_ma60"] is price15["ma20_above_ma60"]
        assert row["close_vs_ma20_pct"] == pytest.approx(
            price15["close_vs_ma20_pct"], abs=0.005)
        assert row["price_data_status"] == price15["data_status"]

        assert row["avg_traded_value_20d"] == pytest.approx(
            liq15["value"], rel=1e-12), f"i={i} liquidity"
        assert row["liquidity_days_used"] == liq15["days_used"]
        assert row["liquidity_data_status"] == liq15["data_status"]


def test_Gate_판정이_V15_조건과_같다(scanner, builder):
    """로드맵 Stage 2의 세 조건이 V1.5 Gate와 같은 값을 내는지."""
    bars = _synthetic_bars(seed=31)
    rows = _rows_for(builder, bars)
    rng = random.Random(SEED)
    checked = 0
    for i in rng.sample(range(V15_WINDOW_BARS, len(bars)), 60):
        window = _v15_window(bars, i)
        obv15 = scanner.calc_obv(window, divergence_lookback=OBV_LOOKBACK)
        price15 = scanner.calc_price_trend(window)
        liq15 = scanner.calc_liquidity(window)
        row = rows[i]

        # V1.5 `_compute_gate()`의 일봉 부분과 같은 식
        expected_obv = (obv15["normalized_slope"] is not None
                        and obv15["normalized_slope"] > 0
                        and obv15["obv_ma20"] is not None
                        and obv15["obv"] > obv15["obv_ma20"])
        assert row["cond_obv"] is expected_obv, f"i={i}"
        assert row["cond_price"] is (price15["above_ma20"] is True)
        assert row["cond_liquidity"] is (liq15["value"] is not None)
        checked += 1
    assert checked == 60


def test_워밍업_구간_상태가_V15와_같다(scanner, builder):
    """데이터가 부족한 초기 구간에서 상태 코드까지 맞는지."""
    bars = _synthetic_bars(n=40, seed=5)
    rows = _rows_for(builder, bars)
    for i in range(len(bars)):
        window = bars[:i + 1]
        obv15 = scanner.calc_obv(window, divergence_lookback=OBV_LOOKBACK)
        price15 = scanner.calc_price_trend(window)
        liq15 = scanner.calc_liquidity(window)
        assert rows[i]["obv_data_status"] == obv15["data_status"], f"i={i} obv"
        assert rows[i]["price_data_status"] == price15["data_status"], f"i={i} price"
        assert rows[i]["liquidity_data_status"] == liq15["data_status"], f"i={i} liq"


# ─────────────────────────────────────────────────────────────
# 실제 수집 데이터
# ─────────────────────────────────────────────────────────────

def _stored_bars(limit_securities=3):
    from cvd_obv.contracts.market_schema import DAILY_BAR_PK
    from cvd_obv.domain.errors import NoInputDataError
    from cvd_obv.storage.parquet_store import ParquetStore

    store = ParquetStore()
    try:
        table = store.read_arrow(
            "silver", "daily_bars",
            columns=["security_id", "trading_date", "close", "volume",
                     "quality_status"],
            dedupe_pk=DAILY_BAR_PK)
    except NoInputDataError:
        return {}
    rows = table.to_pylist()
    grouped = {}
    for row in rows:
        if row["quality_status"] == "FAILED":
            continue
        grouped.setdefault(row["security_id"], []).append(row)
    picked = sorted(grouped)[:limit_securities]
    return {sid: sorted(grouped[sid], key=lambda r: r["trading_date"])
            for sid in picked}


@pytest.mark.skipif(not os.path.isdir(
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "data", "silver", "daily_bars")),
    reason="수집된 일봉이 없다 — `daily-cache build` 먼저")
def test_실제_수집_데이터에서도_V15와_일치한다(scanner, builder):
    """합성 자료만으로는 놓칠 수 있는 실제 값 분포에서도 확인한다."""
    stored = _stored_bars()
    if not stored:
        pytest.skip("수집된 일봉이 없다")

    rng = random.Random(SEED)
    total = 0
    for security_id, bars in stored.items():
        clean = [{"trading_date": b["trading_date"],
                  "close": float(b["close"]), "volume": int(b["volume"])}
                 for b in bars]
        rows = builder.build_security(security_id, clean, "test_run")
        span = range(V15_WINDOW_BARS, len(clean))
        for i in rng.sample(list(span), min(25, len(list(span)))):
            window = _v15_window(clean, i)
            obv15 = scanner.calc_obv(window, divergence_lookback=OBV_LOOKBACK)
            price15 = scanner.calc_price_trend(window)
            liq15 = scanner.calc_liquidity(window)
            row = rows[i]

            expected_obv = (obv15["normalized_slope"] is not None
                            and obv15["normalized_slope"] > 0
                            and obv15["obv_ma20"] is not None
                            and obv15["obv"] > obv15["obv_ma20"])
            assert row["cond_obv"] is expected_obv, f"{security_id} {i}"
            assert row["cond_price"] is (price15["above_ma20"] is True)
            assert row["avg_traded_value_20d"] == pytest.approx(
                liq15["value"], rel=1e-9), f"{security_id} {i}"
            assert row["obv_normalized_slope"] == pytest.approx(
                obv15["normalized_slope"], rel=1e-9)
            total += 1
    assert total > 0

# -*- coding: utf-8 -*-
"""일봉 feature 생성 — V1.5 계산식을 전체 이력에 대해 한 번에 굴린다.

왜 V1.5를 직접 호출하지 않는가
------------------------------
V1.5의 `calc_obv`·`calc_price_trend`·`calc_liquidity`는 "시계열을 받아 마지막
시점의 값 하나"를 돌려준다. 199종목 × 6,834일마다 호출하면 136만 번이고, 각
호출이 120~220일 윈도우를 다시 훑는다. 파이썬으로 수 시간이 걸린다.

그래서 같은 계산식을 누적합 기반 rolling으로 다시 구현하되, **무작위 표본에서
V1.5 결과와 완전히 일치하는지 회귀 테스트로 증명한다**
(`tests/regression/test_daily_feature_equivalence.py`). 속도를 위해 정확성을
포기하지 않기 위한 장치다.

OBV 시작점 문제
---------------
OBV는 `obv[0]=0`에서 누적하므로 **윈도우 시작점에 따라 절대값이 달라진다.**
V1.5는 약 220캘린더일 창을 쓰고 여기서는 상장 이후 전체를 쓴다. 그래도 Gate가
보는 값은 모두 차분이라 결과가 같다.

    obv - obv_ma20   : 둘 다 같은 상수만큼 이동 → 차이는 불변
    obv[t] - obv[t-k]: 상수가 상쇄 → 불변

절대값 `obv`는 테이블에 남기되, 윈도우 간 비교에 쓰지 말라고 열 주석에 적었다.
"""

from __future__ import annotations

import numpy as np

from .. import SCHEMA_VERSION
from ..contracts.feature_schema import feature_version as _feature_version
from ..domain.errors import ConfigError
from ..domain.ids import to_utc
from ..domain.market_rules import default_registry
from ..domain.status import QualityStatus

__all__ = ["DailyFeatureBuilder", "MIN_DAYS_FOR_MA20", "LIQUIDITY_WINDOW",
           "LIQUIDITY_MIN_DAYS"]

#: V1.5 `calc_obv`·`calc_price_trend`의 MA20 최소 일수.
MIN_DAYS_FOR_MA20 = 20

#: V1.5 `calc_liquidity`의 창 길이와 최소 일수.
LIQUIDITY_WINDOW = 20
LIQUIDITY_MIN_DAYS = 5

#: V1.5 `calc_price_trend`의 MA 기울기 비교 간격(5거래일 전 MA와 비교).
MA_SLOPE_OFFSET = 5


def _rolling_mean(values: np.ndarray, window: int) -> np.ndarray:
    """마지막 `window`개의 평균. 앞쪽 부족 구간은 NaN."""
    n = values.size
    out = np.full(n, np.nan)
    if n < window:
        return out
    cumsum = np.concatenate(([0.0], np.cumsum(values)))
    out[window - 1:] = (cumsum[window:] - cumsum[:-window]) / window
    return out


def _rolling_sum(values: np.ndarray, window: int) -> np.ndarray:
    n = values.size
    out = np.full(n, np.nan)
    if n < window:
        return out
    cumsum = np.concatenate(([0.0], np.cumsum(values)))
    out[window - 1:] = cumsum[window:] - cumsum[:-window]
    return out


def _expanding_mean_upto(values: np.ndarray, window: int) -> np.ndarray:
    """`window`개가 모이기 전에는 NaN, 이후 rolling 평균.

    V1.5는 20개 미만이면 MA20을 아예 계산하지 않으므로 그 동작을 따른다.
    """
    return _rolling_mean(values, window)


class DailyFeatureBuilder:
    """한 종목의 일봉 시계열 → 날짜별 feature row."""

    def __init__(self, obv_lookback: int = 20, registry=None,
                 market: str = "KRX", data_revision: int = 1):
        if int(obv_lookback) < 1:
            raise ConfigError("obv_lookback은 1 이상이어야 한다")
        self.obv_lookback = int(obv_lookback)
        self.registry = registry or default_registry()
        self.market = market
        self.data_revision = int(data_revision)
        self.feature_version = _feature_version(self.obv_lookback)

    # ── 핵심 계산 ──────────────────────────────────────────

    def compute_arrays(self, closes, volumes):
        """종가·거래량 배열 → feature 배열 묶음.

        입력은 날짜 오름차순이어야 한다. 반환값의 i번째는 "i번째 거래일 종가까지
        아는 정보만으로 계산한 값"이다.
        """
        closes = np.asarray(closes, dtype=np.float64)
        volumes = np.asarray(volumes, dtype=np.float64)
        n = closes.size
        if n == 0:
            raise ConfigError("빈 시계열")
        if volumes.size != n:
            raise ConfigError("종가와 거래량 길이가 다르다")

        k = self.obv_lookback

        # ── OBV 누적 (V1.5와 동일한 부호 규칙) ──
        diff = np.diff(closes)
        signed = np.zeros(n)
        signed[1:] = np.where(diff > 0, volumes[1:],
                              np.where(diff < 0, -volumes[1:], 0.0))
        obv = np.cumsum(signed)

        obv_ma20 = _expanding_mean_upto(obv, MIN_DAYS_FOR_MA20)

        # slope = obv[t] - obv[t-k]. t < k인 구간은 V1.5가 k를 줄여 쓰지만,
        # 그 구간은 어차피 obv_ma20이 없어 Gate가 성립하지 않는다.
        obv_slope = np.full(n, np.nan)
        if n > k:
            obv_slope[k:] = obv[k:] - obv[:-k]

        window_volume = _rolling_sum(volumes, k)
        # V1.5의 sum(volumes[-k:])는 t 시점 포함 최근 k개다.
        with np.errstate(invalid="ignore", divide="ignore"):
            obv_norm_slope = np.where(
                (window_volume > 0) & np.isfinite(obv_slope),
                obv_slope / window_volume, np.nan)

        # up_ratio: 최근 k일 중 종가 상승일 비율
        up_flag = np.zeros(n)
        up_flag[1:] = (diff > 0).astype(np.float64)
        up_count = _rolling_sum(up_flag, k)
        with np.errstate(invalid="ignore", divide="ignore"):
            up_ratio = np.where(np.isfinite(up_count), up_count / k, np.nan)

        # ── 가격추세 ──
        ma20 = _expanding_mean_upto(closes, 20)
        ma60 = _expanding_mean_upto(closes, 60)
        ma120 = _expanding_mean_upto(closes, 120)

        ma20_prev5 = np.full(n, np.nan)
        if n > MA_SLOPE_OFFSET:
            ma20_prev5[MA_SLOPE_OFFSET:] = ma20[:-MA_SLOPE_OFFSET]
        ma60_prev5 = np.full(n, np.nan)
        if n > MA_SLOPE_OFFSET:
            ma60_prev5[MA_SLOPE_OFFSET:] = ma60[:-MA_SLOPE_OFFSET]

        # ── 유동성: 최근 20일 (종가 × 거래량) 평균 ──
        turnover = closes * volumes
        liquidity = _rolling_mean(turnover, LIQUIDITY_WINDOW)
        # 20일 미만이어도 5일 이상이면 V1.5는 있는 만큼으로 계산한다(PARTIAL).
        liquidity_days = np.minimum(np.arange(1, n + 1), LIQUIDITY_WINDOW)
        partial_cumsum = np.cumsum(turnover)
        short = liquidity_days < LIQUIDITY_WINDOW
        liquidity = np.where(short, partial_cumsum / liquidity_days, liquidity)
        liquidity = np.where(liquidity_days >= LIQUIDITY_MIN_DAYS,
                             liquidity, np.nan)

        return {
            "close": closes, "obv": obv, "obv_ma20": obv_ma20,
            "obv_slope": obv_slope, "obv_window_volume": window_volume,
            "obv_normalized_slope": obv_norm_slope, "obv_up_ratio": up_ratio,
            "ma20": ma20, "ma60": ma60, "ma120": ma120,
            "ma20_prev5": ma20_prev5, "ma60_prev5": ma60_prev5,
            "liquidity": liquidity, "liquidity_days": liquidity_days,
        }

    # ── row 변환 ───────────────────────────────────────────

    def build_security(self, security_id: str, bars, run_id: str,
                       start=None, end=None):
        """silver 일봉 row 목록 → feature row 목록."""
        bars = sorted(bars, key=lambda b: b["trading_date"])
        dates = [b["trading_date"] for b in bars]
        arr = self.compute_arrays([float(b["close"]) for b in bars],
                                  [float(b["volume"]) for b in bars])
        k = self.obv_lookback
        rows = []
        for i, day in enumerate(dates):
            if start and day < start:
                continue
            if end and day > end:
                continue
            rows.append(self._row(security_id, day, i, arr, k, run_id,
                                  len(bars)))
        return rows

    def _row(self, security_id, day, i, arr, k, run_id, total_bars):
        def _f(name):
            value = arr[name][i]
            return None if not np.isfinite(value) else float(value)

        close = _f("close")
        ma20, ma60, ma120 = _f("ma20"), _f("ma60"), _f("ma120")
        ma20_prev5, ma60_prev5 = _f("ma20_prev5"), _f("ma60_prev5")
        obv, obv_ma20 = _f("obv"), _f("obv_ma20")
        norm_slope = _f("obv_normalized_slope")
        liquidity = _f("liquidity")
        liquidity_days = int(arr["liquidity_days"][i])

        # V1.5 calc_price_trend: 20개 미만이면 전부 None + PARTIAL
        price_ready = ma20 is not None
        above_ma20 = (close > ma20) if price_ready else None
        above_ma60 = (close > ma60) if (price_ready and ma60 is not None) else None
        above_ma120 = (close > ma120) if (price_ready and ma120 is not None) else None
        ma20_rising = (ma20 > ma20_prev5) if (price_ready and ma20_prev5 is not None) else None
        ma60_rising = ((ma60 > ma60_prev5)
                       if (price_ready and ma60 is not None
                           and ma60_prev5 is not None) else None)
        ma20_above_ma60 = ((ma20 > ma60)
                           if (price_ready and ma60 is not None) else None)
        close_vs_ma20 = (round((close / ma20 - 1.0) * 100, 2)
                         if price_ready and ma20 else None)

        # V1.5 calc_obv: 표본이 max(6, k+1) 미만이면 FAILED,
        # obv_ma20이 없으면 PARTIAL
        obv_bars_seen = i + 1
        if obv_bars_seen < max(6, k + 1):
            obv_status = QualityStatus.FAILED
            obv, obv_ma20, norm_slope = None, None, None
        elif obv_ma20 is None:
            obv_status = QualityStatus.PARTIAL
        else:
            obv_status = QualityStatus.SUCCESS

        price_status = (QualityStatus.SUCCESS if price_ready
                        else QualityStatus.PARTIAL)
        if liquidity is None:
            liquidity_status = QualityStatus.FAILED
        elif liquidity_days >= LIQUIDITY_WINDOW:
            liquidity_status = QualityStatus.SUCCESS
        else:
            liquidity_status = QualityStatus.PARTIAL

        cond_price = above_ma20
        cond_obv = (None if (norm_slope is None or obv is None
                             or obv_ma20 is None)
                    else bool(norm_slope > 0 and obv > obv_ma20))
        cond_liquidity = liquidity is not None
        gate_all = (None if (cond_price is None or cond_obv is None)
                    else bool(cond_price and cond_obv and cond_liquidity))

        statuses = (price_status, obv_status, liquidity_status)
        if QualityStatus.FAILED in statuses:
            feature_status = QualityStatus.FAILED
        elif all(s == QualityStatus.SUCCESS for s in statuses):
            feature_status = QualityStatus.SUCCESS
        else:
            feature_status = QualityStatus.PARTIAL

        rule = self.registry.resolve(self.market, day)
        return {
            "security_id": security_id,
            "trading_date": day,
            "feature_version": self.feature_version,

            "close": close, "ma20": ma20, "ma60": ma60, "ma120": ma120,
            "ma20_prev5": ma20_prev5,
            "above_ma20": above_ma20, "above_ma60": above_ma60,
            "above_ma120": above_ma120,
            "ma20_rising": ma20_rising, "ma60_rising": ma60_rising,
            "ma20_above_ma60": ma20_above_ma60,
            "close_vs_ma20_pct": close_vs_ma20,
            "price_data_status": price_status,

            "obv": obv, "obv_ma20": obv_ma20,
            "obv_above_ma20": (None if (obv is None or obv_ma20 is None)
                               else bool(obv > obv_ma20)),
            "obv_slope": _f("obv_slope"),
            "obv_window_volume": _f("obv_window_volume"),
            "obv_normalized_slope": norm_slope,
            "obv_up_ratio": _f("obv_up_ratio"),
            "obv_lookback": k,
            "obv_data_status": obv_status,

            "avg_traded_value_20d": liquidity,
            "liquidity_days_used": (liquidity_days if liquidity is not None
                                    else 0),
            "liquidity_data_status": liquidity_status,

            "cond_price": cond_price,
            "cond_obv": cond_obv,
            "cond_liquidity": cond_liquidity,
            "gate_all": gate_all,
            "feature_status": feature_status,

            "available_at_utc": to_utc(rule.daily_confirmed_at(day)),
            "market_rule_version": rule.version,
            "run_id": run_id,
            "schema_version": SCHEMA_VERSION,
            "data_revision": self.data_revision,
        }

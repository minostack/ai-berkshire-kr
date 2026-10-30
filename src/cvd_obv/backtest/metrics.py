# -*- coding: utf-8 -*-
"""백테스트 성과 지표 — 로드맵 Stage 7 DoD.

여기서 답할 것은 둘이다.

    1. 이 전략은 **아무거나 산 것**보다 나은가 (base rate 대비 적중률)
    2. 이 전략은 **지수를 그냥 산 것**보다 나은가 (benchmark 초과수익)

총수익률 하나로는 어느 쪽도 답이 되지 않는다. 상승장에서 아무 종목이나 사도
총수익률은 양수다. 그래서 비교 대상을 명시하고, 구간마다 신뢰구간을 붙인다.

기준선을 왜 구간 안에서 다시 재는가
-----------------------------------
`gold.base_rates`는 2010~2026년 전체의 무조건부 확률이다. 그걸 1년짜리
백테스트와 견주면 "전략이 좋았는가"와 "그 1년이 어떤 해였는가"가 섞인다.
**같은 구간·같은 유니버스·같은 horizon**에서 다시 재야 비교가 성립한다. 재는
방법은 Stage 1과 같다(`gold.daily_forward_returns`의 같은 정의).

신뢰구간 규칙은 Stage 2·6과 같다
---------------------------------
h일 보유면 이웃한 날의 수익이 같은 가격 구간을 공유한다. 군집 robust와 블록
부트스트랩(블록 길이 >= horizon)을 함께 내고 **넓은 쪽**을 쓴다.
"""

from __future__ import annotations

import math

import numpy as np

from .. import SCHEMA_VERSION
from ..contracts.label_schema import SIGNAL_FORWARD_RETURN_PK
from ..domain.errors import NoInputDataError, StatisticalValidationError
from ..domain.status import LabelStatus, QualityStatus
from ..statistics.base_rates import block_length_for
from ..statistics.inference import block_bootstrap_mean, cluster_robust_mean
from .engine import _part_key

__all__ = ["BacktestMetrics", "TRADING_DAYS_PER_YEAR"]

#: 연율화에 쓰는 거래일 수. 한국 시장 기준 통상치이며 `spec`에 박아 두지
#: 않는다 — 바꾸면 Sharpe와 CAGR만 달라지고 원자료는 그대로다.
TRADING_DAYS_PER_YEAR = 245


class BacktestMetrics:
    """`BacktestResult` → `gold.backtest_metrics` 행."""

    LAYER = "gold"
    TABLE = "backtest_metrics"

    def __init__(self, store, result, data_revision: int = 1,
                 ci_level: float = 0.95, n_resamples: int = 1000,
                 seed: int = 20260919):
        self.store = store
        self.result = result
        self.data_revision = int(data_revision)
        self.ci_level = float(ci_level)
        self.n_resamples = int(n_resamples)
        self.seed = int(seed)
        self._base_rate = None

    # ── 기준선 ────────────────────────────────────────────────

    def base_rate(self):
        """같은 구간·같은 신호 모집단의 무조건부 상승확률.

        모집단은 그 구간 replay가 본 **모든 신호**다(BUY든 WATCH든). 즉
        "그날 유니버스에서 아무거나 하나 샀다면"에 해당한다.
        """
        import pyarrow.dataset as ds

        if self._base_rate is not None:
            return self._base_rate

        spec = self.result.spec
        days = [row["trading_date"] for row in self.result.nav]
        expr = ((ds.field("horizon_td") == spec.horizon_td)
                & (ds.field("entry_rule") == "NEXT_OPEN")
                & (ds.field("label_version") == spec.label_version)
                & (ds.field("label_status") == LabelStatus.COMPLETE)
                & (ds.field("signal_date") >= min(days))
                & (ds.field("signal_date") <= max(days)))
        table = self.store.read_arrow(
            self.LAYER, "forward_returns",
            columns=["signal_date", "stock_return_net", "up",
                     "excess_return_net"],
            filters=expr, dedupe_pk=SIGNAL_FORWARD_RETURN_PK)
        if table.num_rows == 0:
            raise NoInputDataError(
                f"horizon={spec.horizon_td} 기준선 표본이 없다")

        ups = np.asarray([1.0 if v else 0.0
                          for v in table.column("up").to_pylist()])
        nets = np.asarray([float(v) if v is not None else np.nan
                           for v in table.column("stock_return_net").to_pylist()])
        dates = np.asarray(table.column("signal_date").to_pylist())
        self._base_rate = {
            "hit_rate": float(ups.mean()),
            "mean_return": float(np.nanmean(nets)),
            "raw_n": int(ups.size),
            "dates": dates, "up": ups, "net": nets,
        }
        return self._base_rate

    # ── 계산 ──────────────────────────────────────────────────

    def compute(self, run_id: str = ""):
        rows = []
        for scope in self._scopes():
            rows += self._for_scope(scope, run_id)
        return rows

    def _scopes(self):
        if self.result.spec.oos_start is None:
            return ["FULL"]
        return ["FULL", "IN_SAMPLE", "OUT_OF_SAMPLE"]

    def _for_scope(self, scope, run_id):
        nav = [r for r in self.result.nav
               if scope == "FULL" or r["sample_scope"] == scope]
        trades = [t for t in self.result.closed()
                  if scope == "FULL" or t["sample_scope"] == scope]
        if len(nav) < 2:
            return [self._row(scope, "total_return", None, run_id,
                              status=QualityStatus.PARTIAL,
                              reason="거래일이 2일 미만이다")]

        out = []
        out += self._nav_metrics(scope, nav, run_id)
        out += self._trade_metrics(scope, trades, run_id)
        return out

    def _nav_metrics(self, scope, nav, run_id):
        """자산 곡선에서 나오는 것들."""
        first, last = nav[0], nav[-1]
        base = float(first["net_nav"])
        total = float(last["net_nav"]) / base - 1.0
        years = len(nav) / TRADING_DAYS_PER_YEAR

        daily = _daily_returns([float(r["net_nav"]) for r in nav])
        bench_series = [r["benchmark_nav"] for r in nav]
        bench_daily = (_daily_returns([float(v) for v in bench_series])
                       if all(v is not None for v in bench_series) else None)
        dates = np.asarray([r["trading_date"] for r in nav[1:]])

        vol = float(daily.std(ddof=1)) if daily.size > 1 else None
        rows = [
            self._row(scope, "total_return", total, run_id,
                      raw_n=len(nav), comparison=None),
            self._row(scope, "cagr",
                      ((1.0 + total) ** (1.0 / years) - 1.0
                       if years > 0 and total > -1 else None),
                      run_id, raw_n=len(nav)),
            self._row(scope, "annualized_volatility",
                      (vol * math.sqrt(TRADING_DAYS_PER_YEAR)
                       if vol is not None else None),
                      run_id, raw_n=int(daily.size)),
            self._row(scope, "sharpe_ratio",
                      (float(daily.mean()) / vol
                       * math.sqrt(TRADING_DAYS_PER_YEAR)
                       if vol else None),
                      run_id, raw_n=int(daily.size),
                      reason="무위험수익률 0 가정"),
            self._row(scope, "max_drawdown",
                      min(float(r["drawdown"]) for r in nav), run_id,
                      raw_n=len(nav)),
            self._row(scope, "mean_exposure",
                      float(np.mean([r["exposure"] for r in nav])), run_id,
                      raw_n=len(nav)),
            self._row(scope, "mean_open_positions",
                      float(np.mean([r["open_positions"] for r in nav])),
                      run_id, raw_n=len(nav)),
            self._row(scope, "mean_daily_turnover",
                      float(np.mean([r["turnover"] or 0.0 for r in nav])),
                      run_id, raw_n=len(nav)),
        ]

        if bench_daily is None:
            rows.append(self._row(
                scope, "benchmark_total_return", None, run_id,
                status=QualityStatus.PARTIAL,
                reason="벤치마크 일봉이 구간 전체에 없다 — 0으로 대체하지 않는다"))
            return rows

        bench_total = float(last["benchmark_nav"]) / float(
            first["benchmark_nav"]) - 1.0
        rows.append(self._row(scope, "benchmark_total_return", bench_total,
                              run_id, raw_n=len(nav)))
        rows.append(self._row(scope, "excess_total_return",
                              total - bench_total, run_id, raw_n=len(nav),
                              comparison="benchmark"))

        # 일별 초과수익의 평균 — 이게 "지수보다 나은가"의 추론 가능한 형태다.
        excess = daily - bench_daily
        rows.append(self._estimate_row(
            scope, "mean_daily_excess_return", excess, dates,
            horizon=1, run_id=run_id, comparison="benchmark"))
        return rows

    def _trade_metrics(self, scope, trades, run_id):
        if not trades:
            return [self._row(scope, "hit_rate", None, run_id,
                              status=QualityStatus.PARTIAL,
                              reason="청산된 거래가 없다")]

        horizon = self.result.spec.horizon_td
        nets = np.asarray([float(t["net_return"]) for t in trades])
        ups = (nets > 0).astype(np.float64)
        entry_dates = np.asarray([t["entry_date"] for t in trades])
        excess = np.asarray([
            float(t["excess_return_net"])
            if t["excess_return_net"] is not None else np.nan
            for t in trades])

        rows = [
            self._row(scope, "trades", float(len(trades)), run_id,
                      raw_n=len(trades)),
            self._estimate_row(scope, "hit_rate", ups, entry_dates, horizon,
                               run_id),
            self._estimate_row(scope, "mean_trade_return_net", nets,
                               entry_dates, horizon, run_id),
            self._row(scope, "median_trade_return_net",
                      float(np.median(nets)), run_id, raw_n=len(trades)),
        ]
        if np.isfinite(excess).any():
            rows.append(self._estimate_row(
                scope, "mean_trade_excess_net", excess[np.isfinite(excess)],
                entry_dates[np.isfinite(excess)], horizon, run_id,
                comparison="benchmark"))

        # 기준선 대비 적중률 — 로드맵 DoD가 이름으로 요구한 값.
        base = self.base_rate()
        rows.append(self._row(scope, "base_rate_hit_rate", base["hit_rate"],
                              run_id, raw_n=base["raw_n"],
                              comparison="base_rate",
                              reason="같은 구간·같은 신호 모집단에서 다시 잰 값"))
        rows.append(self._paired_edge_row(scope, trades, base, horizon,
                                          run_id))
        return rows

    def _paired_edge_row(self, scope, trades, base, horizon, run_id):
        """적중률 edge — **같은 날짜끼리 짝지어** 낸 차이.

        전체 평균끼리 빼면 "전략이 거래한 날"과 "거래하지 않은 날"의 시장이
        섞인다. 거래가 있었던 날만 골라 그날의 기준선과 비교한다.
        """
        # 기준선은 **신호일** 기준이다(패널의 `signal_date`). 거래를 진입일로
        # 묶으면 하루가 어긋나 다른 날의 시장과 비교하게 되므로, 거래도
        # `signal_date`로 묶는다.
        base_by_day = {}
        for day, up in zip(base["dates"], base["up"]):
            base_by_day.setdefault(day, []).append(up)

        signal_by_day = {}
        for trade in trades:
            signal_by_day.setdefault(trade["signal_date"], []).append(
                1.0 if float(trade["net_return"]) > 0 else 0.0)

        deltas, days = [], []
        for day in sorted(signal_by_day):
            if day not in base_by_day:
                continue
            deltas.append(float(np.mean(signal_by_day[day]))
                          - float(np.mean(base_by_day[day])))
            days.append(day)

        if len(deltas) < 2:
            return self._row(scope, "hit_rate_edge_vs_base_rate", None,
                             run_id, status=QualityStatus.PARTIAL,
                             comparison="base_rate",
                             reason="짝지을 수 있는 날짜가 2일 미만이다")
        return self._estimate_row(
            scope, "hit_rate_edge_vs_base_rate", np.asarray(deltas),
            np.asarray(days), horizon, run_id, comparison="base_rate")

    # ── 추론 ──────────────────────────────────────────────────

    def _estimate_row(self, scope, name, values, clusters, horizon, run_id,
                      comparison=None):
        values = np.asarray(values, dtype=np.float64)
        if values.size < 2:
            return self._row(scope, name,
                             float(values[0]) if values.size else None,
                             run_id, raw_n=int(values.size),
                             status=QualityStatus.PARTIAL,
                             comparison=comparison,
                             reason="표본이 2개 미만이다")

        block = block_length_for(horizon)
        candidates = [("CLUSTER_ROBUST_CR1",
                       cluster_robust_mean(values, clusters,
                                           level=self.ci_level))]
        try:
            candidates.append(("MOVING_BLOCK_BOOTSTRAP", block_bootstrap_mean(
                values, clusters, block_length=block,
                n_resamples=self.n_resamples, seed=self.seed,
                level=self.ci_level)))
        except StatisticalValidationError:
            pass
        method, est = max(candidates,
                          key=lambda item: item[1].ci_high - item[1].ci_low)
        return self._row(
            scope, name, float(values.mean()), run_id,
            raw_n=int(values.size), effective_n=est.effective_n,
            ci_low=est.ci_low, ci_high=est.ci_high, se=est.se,
            method=method, cluster_key="signal_date", block_length=block,
            comparison=comparison)

    def _row(self, scope, name, value, run_id, raw_n=None, effective_n=None,
             ci_low=None, ci_high=None, se=None, method=None,
             cluster_key=None, block_length=None, comparison=None,
             status=QualityStatus.SUCCESS, reason=None):
        return {
            "experiment_id": self.result.experiment_id,
            "metric_scope": scope,
            "horizon_td": self.result.spec.horizon_td,
            "segment_key": "ALL",
            "metric_name": name,
            "metric_value": value,
            "raw_n": raw_n,
            "effective_n": effective_n,
            "ci_low": ci_low,
            "ci_high": ci_high,
            "standard_error": se,
            "inference_method": method,
            "cluster_key": cluster_key,
            "block_length": block_length,
            "p_value": None,
            "adjusted_p_value": None,
            "comparison": comparison,
            "status": status,
            "status_reason": reason,
            "run_id": run_id,
            "schema_version": SCHEMA_VERSION,
            "data_revision": self.data_revision,
        }

    # ── 저장 ──────────────────────────────────────────────────

    def write(self, rows, run_id):
        from ..contracts.backtest_schema import (
            BACKTEST_METRIC_COLUMNS, BACKTEST_METRIC_PK,
        )

        return self.store.write(
            self.LAYER, self.TABLE, rows, BACKTEST_METRIC_COLUMNS,
            BACKTEST_METRIC_PK, run_id,
            partition_fn=lambda r: [
                ("experiment_id_prefix", r["experiment_id"][:10])],
            part_key=_part_key(self.result.experiment_id),
            extra_manifest={"experiment_id": self.result.experiment_id})


def _daily_returns(series):
    values = np.asarray(series, dtype=np.float64)
    if values.size < 2:
        return np.zeros(0)
    prev = values[:-1]
    out = np.zeros(values.size - 1, dtype=np.float64)
    ok = prev != 0
    out[ok] = values[1:][ok] / prev[ok] - 1.0
    return out

# -*- coding: utf-8 -*-
"""무조건부 기준선(base rate) — 로드맵 Stage 1 작업 3, 스키마 §7.

"이 종목이 5일 뒤 오를 확률 55%"가 의미가 있으려면, 아무 종목이나 아무 날에나
샀을 때의 확률이 얼마인지 먼저 알아야 한다. 그 기준선이 여기서 나온다.

기준선 없이 나온 확률은 해석할 수 없다. 코스피200 종목의 무조건부 상승확률이
이미 52%라면, 52%를 맞히는 모델은 아무것도 하지 않은 것과 같다.

모든 결과는 원표본수(raw_n)와 **유효표본수(effective_n)를 함께** 보고한다.
종목×날짜 행 수를 독립 표본수로 보고하지 않는다(아키텍처 §6).
"""

from __future__ import annotations

from datetime import date

import numpy as np

from .. import SCHEMA_VERSION
from ..contracts.label_schema import DAILY_FORWARD_RETURN_PK, LABEL_VERSION
from ..domain.errors import NoInputDataError, StatisticalValidationError
from ..domain.ids import sha256_of
from ..domain.status import LabelStatus, QualityStatus
from .inference import (
    block_bootstrap_mean, cluster_robust_mean, icc_and_design_effect,
    naive_mean,
)

__all__ = ["BaseRateComputer", "BOOLEAN_OUTCOMES", "DEFAULT_OUTCOMES",
           "block_length_for"]

#: bool 열은 평균이 곧 확률이다.
BOOLEAN_OUTCOMES = ("up", "up_gross", "outperform",
                    "hit_3pct", "hit_5pct", "hit_minus_3pct", "hit_minus_5pct")

DEFAULT_OUTCOMES = ("up", "up_gross")

#: 블록 길이 선택 규칙 — 로드맵 §8 기본값. artifact에 함께 남긴다.
BLOCK_LENGTH_RULE = "block_length = max(horizon_td, 2)"


def block_length_for(horizon: int) -> int:
    """보유기간 중첩을 흡수할 최소 블록 길이.

    horizon일 보유면 이웃한 horizon일치 신호가 같은 가격 구간을 공유한다.
    블록이 그보다 짧으면 중첩이 끊겨 신뢰구간이 다시 좁아진다. 길이 1 블록은
    블록 부트스트랩이 아니므로 하한을 2로 둔다.
    """
    return max(int(horizon), 2)


class BaseRateComputer:
    """`gold.daily_forward_returns` → `gold.base_rates`."""

    SOURCE_LAYER = "gold"
    SOURCE_TABLE = "daily_forward_returns"

    def __init__(self, store, level: float = 0.95, n_resamples: int = 1000,
                 seed: int = 20260918, data_revision: int = 1):
        self.store = store
        self.level = float(level)
        self.n_resamples = int(n_resamples)
        self.seed = int(seed)
        self.data_revision = int(data_revision)

    # ── 데이터 로드 ────────────────────────────────────────

    def _load(self, horizon: int, outcome: str, entry_rule: str,
              label_version: str, start: date = None, end: date = None):
        import pyarrow.dataset as ds

        columns = ["security_id", "signal_date", "stock_return_net",
                   "stock_return_gross", "benchmark_return", outcome]
        columns = list(dict.fromkeys(columns))
        expr = ((ds.field("horizon_td") == horizon)
                & (ds.field("entry_rule") == entry_rule)
                & (ds.field("label_version") == label_version)
                & (ds.field("label_status") == LabelStatus.COMPLETE))
        table = self.store.read_arrow(
            self.SOURCE_LAYER, self.SOURCE_TABLE, columns=columns, filters=expr,
            dedupe_pk=DAILY_FORWARD_RETURN_PK)
        if table.num_rows == 0:
            raise NoInputDataError(
                f"horizon={horizon} entry_rule={entry_rule}에 해당하는 "
                f"완료 label이 없다")

        dates = np.asarray(table.column("signal_date").to_pylist())
        securities = np.asarray(table.column("security_id").to_pylist())
        outcome_values = np.asarray(
            table.column(outcome).to_pylist(), dtype=object)
        net = table.column("stock_return_net").to_numpy(zero_copy_only=False)
        gross = table.column("stock_return_gross").to_numpy(zero_copy_only=False)
        bench = (table.column("benchmark_return").to_numpy(zero_copy_only=False)
                 if "benchmark_return" in table.column_names else None)

        keep = np.array([v is not None for v in outcome_values])
        if start is not None:
            keep &= np.array([d >= start for d in dates])
        if end is not None:
            keep &= np.array([d <= end for d in dates])
        if not keep.any():
            raise NoInputDataError("기간 필터 후 남은 표본이 없다")

        return {
            "dates": dates[keep],
            "securities": securities[keep],
            "outcome": np.asarray(
                [bool(v) for v in outcome_values[keep]], dtype=np.float64),
            "net": net[keep],
            "gross": gross[keep],
            "bench": bench[keep] if bench is not None else None,
            "dropped_null_outcome": int((~keep).sum()),
        }

    # ── 계산 ───────────────────────────────────────────────

    def compute(self, horizon: int, outcome: str = "up",
                entry_rule: str = "NEXT_OPEN",
                label_version: str = LABEL_VERSION,
                universe_id: str = "KOSPI200", regime: str = None,
                start: date = None, end: date = None, run_id: str = "",
                cost_model_version: str = "krx_v1"):
        """한 (horizon, outcome) 조합의 기준선 행을 만든다."""
        if outcome not in BOOLEAN_OUTCOMES:
            raise StatisticalValidationError(
                f"확률로 볼 수 없는 outcome: {outcome} (가능: {BOOLEAN_OUTCOMES})")

        data = self._load(horizon, outcome, entry_rule, label_version,
                          start, end)
        y = data["outcome"]
        dates = data["dates"]

        robust = cluster_robust_mean(y, dates, level=self.level)
        block = block_length_for(horizon)
        try:
            boot = block_bootstrap_mean(y, dates, block_length=block,
                                        n_resamples=self.n_resamples,
                                        seed=self.seed, level=self.level)
        except StatisticalValidationError:
            boot = None
        naive = naive_mean(y, level=self.level)
        stats = icc_and_design_effect(y, dates)

        spec = {
            "universe_id": universe_id, "horizon_td": int(horizon),
            "entry_rule": entry_rule, "outcome": outcome, "regime": regime,
            "label_version": label_version,
            "start": start, "end": end,
            "ci_level": self.level, "seed": self.seed,
            "n_resamples": self.n_resamples, "block_length": block,
        }
        base_rate_id = "br_" + sha256_of(spec)[len("sha256:"):][:32]

        bench_mean = (float(np.nanmean(data["bench"]))
                      if data["bench"] is not None
                      and np.isfinite(data["bench"]).any() else None)

        row = {
            "base_rate_id": base_rate_id,
            "universe_id": universe_id,
            "start_date": min(dates), "end_date": max(dates),
            "horizon_td": int(horizon),
            "entry_rule": entry_rule,
            "outcome": outcome,
            "regime": regime,

            "raw_n": int(y.size),
            "trading_date_n": int(np.unique(dates).size),
            "security_n": int(np.unique(data["securities"]).size),
            "positive_n": int(y.sum()),
            "probability": float(y.mean()),
            "mean_return": float(np.nanmean(data["net"])),
            "median_return": float(np.nanmedian(data["net"])),
            "benchmark_mean_return": bench_mean,
            "excess_mean_return": (float(np.nanmean(data["net"])) - bench_mean
                                   if bench_mean is not None else None),

            "cluster_key": "signal_date",
            "icc": stats["icc"],
            "design_effect": stats["design_effect"],
            "effective_n": stats["effective_n"],
            # 둘 중 더 보수적인(넓은) 구간을 대표 CI로 쓴다.
            "ci_method": None, "ci_low": None, "ci_high": None,
            "ci_level": self.level,
            "block_length": block,
            "bootstrap_replicates": (boot.detail["bootstrap_replicates"]
                                     if boot else None),
            "seed": self.seed,

            "status": QualityStatus.SUCCESS,
            "label_version": label_version,
            "cost_model_version": cost_model_version,
            "run_id": run_id,
            "schema_version": SCHEMA_VERSION,
            "data_revision": self.data_revision,
        }
        self._pick_ci(row, robust, boot, naive)

        naive_width = naive.ci_high - naive.ci_low
        if naive_width <= 0:
            # 결과가 전부 같은 값이면(확률 0 또는 1) 폭 비교가 의미 없고,
            # 그 자체가 표본이 이상하다는 신호다. 나누기 대신 사실을 남긴다.
            row["status"] = QualityStatus.PARTIAL
        row["_detail"] = {
            "naive_ci": [naive.ci_low, naive.ci_high],
            "cluster_robust_ci": [robust.ci_low, robust.ci_high],
            "block_bootstrap_ci": ([boot.ci_low, boot.ci_high] if boot else None),
            "block_length_rule": BLOCK_LENGTH_RULE,
            "dropped_null_outcome": data["dropped_null_outcome"],
            "naive_ci_width_ratio": (
                (row["ci_high"] - row["ci_low"]) / naive_width
                if naive_width > 0 else None),
            "degenerate_outcome": naive_width <= 0,
        }
        return row

    @staticmethod
    def _pick_ci(row, robust, boot, naive=None):
        """대표 신뢰구간은 가장 넓은 것으로 고른다.

        둘 중 하나를 임의로 고르면 그때그때 유리한 쪽을 쓰게 된다. 보수적인
        쪽으로 고정하고, 모든 값은 `_detail`에 남긴다.

        독립가정 CI도 후보에 넣는 이유: 군집 내 상관이 **음수**면 군집 robust
        CI가 독립가정보다 좁아질 수 있다. 추정상 타당하지만, 이 프로젝트에서
        틀릴 방향은 "실제보다 확신하는 쪽"이 아니라 그 반대여야 한다. 좁아지는
        이득은 취하지 않는다.
        """
        candidates = [("CLUSTER_ROBUST_CR1", robust)]
        if boot is not None:
            candidates.append(("MOVING_BLOCK_BOOTSTRAP", boot))
        if naive is not None:
            candidates.append(("NAIVE_IID_FLOOR", naive))
        method, chosen = max(
            candidates, key=lambda item: item[1].ci_high - item[1].ci_low)
        row["ci_method"] = method
        row["ci_low"] = chosen.ci_low
        row["ci_high"] = chosen.ci_high

    def compute_all(self, horizons, outcomes=DEFAULT_OUTCOMES, **kwargs):
        rows = []
        for horizon in horizons:
            for outcome in outcomes:
                try:
                    rows.append(self.compute(horizon, outcome, **kwargs))
                except NoInputDataError:
                    continue
        if not rows:
            raise NoInputDataError("계산된 기준선이 없다")
        return rows

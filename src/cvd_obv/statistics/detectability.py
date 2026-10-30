# -*- coding: utf-8 -*-
"""검출 가능 구간 — 사전 등록 C의 H3.

Stage 8까지 물은 것은 "edge가 있는가"였다. 답은 매번 "이 데이터로는 알 수
없다"였다. 그래서 질문을 뒤집는다.

    이 시장·이 설계에서 **애초에 무엇을 잴 수 있는가?**

이건 가설 검정이 아니라 **설계의 성질**이다. 데이터를 보고 가설을 고르는
것과 무관하므로 다중비교 문제도 없다.

무엇을 바꿔 가며 재는가
-----------------------
    보유기간 h — 길수록 효과가 쌓이지만 중첩이 심해져 유효표본이 준다
    하루 선택 종목 수 N — 적을수록 선별이 세지지만 날마다의 추정이 흔들린다

둘은 반대로 움직인다. N을 줄이면 고를 수 있는 효과는 커지지만 **재는 정밀도가
나빠진다.** 그 맞바꿈이 어디서 최선인지는 계산이 아니라 실측으로만 안다.

읽는 법
-------
각 칸의 `mde`는 그 설계로 80% 검정력에서 잡을 수 있는 **가장 작은 효과**다.
관측된 edge가 그보다 작으면, 그 효과가 진짜였더라도 이 설계로는 못 잡는다.

`mde_at`는 표본을 늘렸을 때의 투영이다. SE는 날짜 수의 제곱근에 반비례하므로

    mde(n) = mde(현재) x sqrt(현재 날짜 / n)

16년치 일봉 패널이 4,114거래일이므로, 그 길이에서 무엇이 가능한지를 같은
표에서 읽을 수 있다.
"""

from __future__ import annotations

import collections
import math

import numpy as np

from ..domain.errors import ConfigError, StatisticalValidationError
from ..domain.status import SignalStatus
from .base_rates import block_length_for
from .inference import block_bootstrap_mean, cluster_robust_mean
from .power import minimum_detectable_effect
from .score_model import ScoreModel

__all__ = ["DEFAULT_HORIZONS", "DEFAULT_SIZES", "DetectabilityFrontier"]

#: 사전 등록 C H3이 고정한 격자. h=1은 접었다.
DEFAULT_HORIZONS = (5, 10, 20)
DEFAULT_SIZES = (1, 3, 5, 10, 20, 50)


class DetectabilityFrontier:
    """(보유기간, 선택 종목 수)마다 잡을 수 있는 최소 효과."""

    def __init__(self, runner, ci_level: float = 0.95,
                 n_resamples: int = 1000, seed: int = 20260919):
        """`runner`는 `AblationRunner` — 날짜별 feature·결과를 이미 읽어 온다."""
        self.runner = runner
        self.ci_level = float(ci_level)
        self.n_resamples = int(n_resamples)
        self.seed = int(seed)

    def series(self, days, size, field="net"):
        """상위 `size`종목의 날짜별 edge 계열.

        선택 모집단은 **데이터 품질이 통과한 전체**다. Gate 통과분으로 하면
        어떤 날은 후보가 `size`보다 적어 칸마다 선택 크기가 달라지고, 그러면
        "선택 크기의 효과"를 재는 표가 아니게 된다.
        """
        model = ScoreModel()
        out, sizes = {}, []
        for day in sorted(days):
            rows = days[day]
            scores = model.score_day(rows)
            gates = [model.gate_row(row)[0] for row in rows]
            pool = [i for i, g in enumerate(gates)
                    if g != SignalStatus.DATA_INSUFFICIENT
                    and rows[i][field] is not None]
            if len(pool) < 2:
                continue
            population = [rows[i][field] for i in pool]
            picked = sorted(pool,
                            key=lambda i: (-scores[i],
                                           rows[i]["security_id"]))[:size]
            if not picked:
                continue
            sizes.append(len(picked))
            out[day] = (float(np.mean([rows[i][field] for i in picked]))
                        - float(np.mean(population)))
        return out, (float(np.mean(sizes)) if sizes else 0.0)

    def _days_for(self, horizon):
        """보유기간별 날짜 묶음. 같은 h를 여러 크기로 볼 때 다시 읽지 않는다."""
        cache = getattr(self, "_cache", None)
        if cache is None:
            cache = self._cache = {}
        if horizon not in cache:
            self.runner.horizon_td = int(horizon)
            self.runner._days = None
            cache[horizon] = self.runner.load()
        return cache[horizon]

    def cell(self, horizon, size, project_to=()):
        """한 칸 — 관측 edge, MDE, 그리고 표본을 늘렸을 때의 투영."""
        days = self._days_for(int(horizon))
        series, mean_size = self.series(days, size)
        if len(series) < 2:
            return {"horizon_td": horizon, "size": size,
                    "status": "NOT_EVALUABLE",
                    "status_reason": "날짜가 2일 미만이다"}

        dates = np.asarray(sorted(series))
        values = np.asarray([series[d] for d in dates])
        method, est = self._estimate(values, dates, horizon)
        se = ((est.ci_high - est.ci_low) / (2.0 * 1.959963985)
              if est.ci_low is not None else None)
        mde = minimum_detectable_effect(se)

        row = {
            "horizon_td": int(horizon),
            "size": int(size),
            "mean_selection_size": mean_size,
            "dates": int(dates.size),
            "edge": float(values.mean()),
            "ci_low": est.ci_low, "ci_high": est.ci_high,
            "standard_error": se,
            "mde_80": mde,
            "observed_over_mde": (abs(float(values.mean())) / mde
                                  if mde else None),
            "inference_method": method,
            "block_length": block_length_for(horizon),
            "cluster_key": "signal_date",
            "seed": self.seed,
            "ci_level": self.ci_level,
            "status": "SUCCESS",
            "status_reason": None,
        }
        # 표본을 늘렸을 때의 MDE. SE ∝ 1/sqrt(n).
        for target in project_to:
            key = f"mde_at_{int(target)}"
            row[key] = (mde * math.sqrt(dates.size / float(target))
                        if mde and target > 0 else None)
        return row

    def run(self, horizons=DEFAULT_HORIZONS, sizes=DEFAULT_SIZES,
            project_to=(4114,)):
        if not horizons or not sizes:
            raise ConfigError("보유기간과 선택 크기를 하나 이상 줘야 한다")
        return [self.cell(h, n, project_to=project_to)
                for h in horizons for n in sizes]

    def _estimate(self, values, clusters, horizon):
        """군집 robust와 블록 부트스트랩 중 넓은 쪽 — 다른 Stage와 같은 규칙."""
        candidates = [("CLUSTER_ROBUST_CR1",
                       cluster_robust_mean(values, clusters,
                                           level=self.ci_level))]
        try:
            candidates.append(("MOVING_BLOCK_BOOTSTRAP", block_bootstrap_mean(
                values, clusters, block_length=block_length_for(horizon),
                n_resamples=self.n_resamples, seed=self.seed,
                level=self.ci_level)))
        except StatisticalValidationError:
            pass
        return max(candidates,
                   key=lambda item: item[1].ci_high - item[1].ci_low)

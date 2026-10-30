# -*- coding: utf-8 -*-
"""Daily Gate 백테스터 — 로드맵 Stage 2.

CVD 없이 **일봉 성분만으로** edge가 있는지 본다. 대상 조건은 셋이다.

    cond_price      close > MA20
    cond_obv        normalized_slope > 0 AND obv > obv_ma20
    cond_liquidity  20일 평균 거래대금 존재

측정 방식
---------
조건을 만족한 (종목, 날짜)만 골라 **무조건부 기준선과 같은 방식**으로 상승확률을
구하고, 기준선과의 차이를 본다. 차이는 같은 날짜끼리 짝지어(paired) 계산한다.
조건을 만족한 날과 그렇지 않은 날의 시장 상황이 다르기 때문에, 짝을 짓지 않으면
"조건의 효과"와 "그날이 어떤 날이었나"가 뒤섞인다.

반드시 함께 보고하는 것
-----------------------
- raw N, 거래일 수, 유효표본수(n_eff), 신뢰구간과 그 산출 방법
- gross·net 수익률, 벤치마크 초과수익
- coverage(전체 대비 선택 비율), turnover(선택 종목의 날짜 간 교체율)
- 사전 등록 조합인지 탐색적 조합인지

`evidence_tier`는 `DAILY_ONLY_LONG`이다. 분봉 CVD가 들어간 신호와 같은 표에
합치지 않는다(아키텍처 §6).
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pyarrow as pa

from .. import SCHEMA_VERSION
from ..contracts.feature_schema import DAILY_FEATURE_PK
from ..contracts.label_schema import DAILY_FORWARD_RETURN_PK, LABEL_VERSION
from ..domain.errors import (
    ConfigError, NoInputDataError, StatisticalValidationError,
)
from ..domain.ids import sha256_of
from ..domain.status import EvidenceTier, LabelStatus, QualityStatus
from ..statistics.base_rates import BOOLEAN_OUTCOMES, block_length_for
from ..statistics.inference import (
    block_bootstrap_mean, cluster_robust_mean, icc_and_design_effect,
    naive_mean,
)
from .common import (
    _apply_fdr, _bool_array, _liquidity_buckets, _nanmean, _nanmedian,
    _segment_order, _turnover,
)

__all__ = ["VARIANTS", "PREREGISTERED", "DailyGateBacktester",
           "DailyGateSpec"]

#: 조건 조합. 값은 AND로 묶을 조건 열 목록이며, 빈 목록은 전체 유니버스다.
VARIANTS = {
    "ALL": [],
    "PRICE": ["cond_price"],
    "OBV": ["cond_obv"],
    "LIQUIDITY": ["cond_liquidity"],
    "PRICE+OBV": ["cond_price", "cond_obv"],
    "PRICE+LIQ": ["cond_price", "cond_liquidity"],
    "OBV+LIQ": ["cond_obv", "cond_liquidity"],
    "GATE_ALL": ["cond_price", "cond_obv", "cond_liquidity"],
}

#: 로드맵 Stage 2가 명시한 비교군 — 사전 등록된 것으로 본다.
#: 나머지(2개씩 짝지은 조합)는 상호작용을 보려고 **내가 추가한 탐색적 조합**이며,
#: 다중비교 보정 없이 유의성을 주장하면 안 된다.
PREREGISTERED = frozenset(
    {"ALL", "PRICE", "OBV", "LIQUIDITY", "GATE_ALL"})


class DailyGateSpec:
    """한 번의 평가 정의."""

    __slots__ = ("variants", "horizons", "outcomes", "label_version",
                 "feature_version", "start", "end", "universe_id",
                 "ci_level", "n_resamples", "seed", "segment", "segment_by",
                 "cost_multiplier", "base_variant")

    def __init__(self, variants=None, horizons=(1, 5, 20),
                 outcomes=("up", "up_gross"), label_version=LABEL_VERSION,
                 feature_version=None, start=None, end=None,
                 universe_id="KOSPI200", ci_level=0.95, n_resamples=1000,
                 seed=20260918, segment=None, segment_by=None,
                 cost_multiplier=1.0, base_variant="ALL"):
        self.variants = list(variants or VARIANTS)
        self.horizons = tuple(int(h) for h in horizons)
        self.outcomes = tuple(outcomes)
        self.label_version = label_version
        self.feature_version = feature_version
        self.start = start
        self.end = end
        self.universe_id = universe_id
        self.ci_level = float(ci_level)
        self.n_resamples = int(n_resamples)
        self.seed = int(seed)
        self.segment = segment
        # 연도·국면·유동성 구간으로 쪼개 본다. 효과가 특정 몇 해에만 있으면
        # 그건 edge가 아니라 그 시기의 성질이다(로드맵 Stage 2 필수 비교군).
        if segment_by not in (None, "year", "regime", "liquidity"):
            raise ConfigError(f"알 수 없는 세그먼트 기준: {segment_by}")
        self.segment_by = segment_by
        # 비용 민감도. 1.0이면 라벨에 저장된 비용 그대로, 2.0이면 두 배.
        self.cost_multiplier = float(cost_multiplier)
        # 짝지음 차이를 **무엇과** 견줄 것인가.
        #
        # 기본은 `ALL`(그날 전체)이다. 그런데 "OBV가 가격 위에 무언가를
        # 더하는가"는 그렇게 물을 수 없다 — PRICE를 기준으로 PRICE+OBV를 봐야
        # 한다. 기준을 바꾸면 **중첩(nested) 비교**가 되고, 그게 성분이
        # 기여하는지를 묻는 올바른 형태다.
        if base_variant not in VARIANTS:
            raise ConfigError(
                f"알 수 없는 기준 조합: {base_variant} "
                f"(가능: {sorted(VARIANTS)})")
        self.base_variant = base_variant

    def to_dict(self):
        return {
            "variants": self.variants, "horizons": list(self.horizons),
            "outcomes": list(self.outcomes),
            "label_version": self.label_version,
            "feature_version": self.feature_version,
            "start": self.start, "end": self.end,
            "universe_id": self.universe_id, "ci_level": self.ci_level,
            "n_resamples": self.n_resamples, "seed": self.seed,
            "segment": self.segment, "segment_by": self.segment_by,
            "cost_multiplier": self.cost_multiplier,
            "base_variant": self.base_variant,
        }


class DailyGateBacktester:
    """`gold.daily_features` × `gold.daily_forward_returns` 평가."""

    def __init__(self, store, data_revision: int = 1):
        self.store = store
        self.data_revision = int(data_revision)
        self._features = None
        self._feature_version = None
        # feature 표와 label 표가 **같은** 종목 코드북을 써야 한다.
        # 표마다 새로 만들면 조인이 조용히 엉뚱한 종목을 붙인다.
        self._security_codes = {}

    # ── 데이터 준비 ────────────────────────────────────────

    def load_features(self, feature_version: str = None, membership=None):
        """feature를 (키, 조건 배열)로 올린다. 한 번만 읽는다.

        `membership`을 주면 **그날 실제 구성종목이었던 행만** 남긴다
        (`UniverseRepository.membership_intervals()`의 결과). 이걸 주지 않으면
        모집단이 "한 번이라도 지수에 있었던 종목 전체"가 되어, 편출된 뒤의
        수익률까지 통계에 들어간다. 그것도 생존편향의 한 형태다.
        """
        import pyarrow.compute as pc

        table = self.store.read_arrow(
            "gold", "daily_features",
            columns=["security_id", "trading_date", "feature_version",
                     "cond_price", "cond_obv", "cond_liquidity", "gate_all",
                     "avg_traded_value_20d"],
            dedupe_pk=DAILY_FEATURE_PK)
        if feature_version:
            table = table.filter(
                pc.equal(table.column("feature_version"), feature_version))
        if table.num_rows == 0:
            raise NoInputDataError(
                "일봉 feature가 없다 — `features build` 먼저")
        versions = set(table.column("feature_version").to_pylist())
        if len(versions) > 1:
            raise StatisticalValidationError(
                f"feature_version이 섞여 있다: {sorted(versions)} — "
                "하나를 지정할 것")

        self._feature_version = versions.pop()
        securities = table.column("security_id").to_pylist()
        days = table.column("trading_date").to_pylist()

        self.membership_dropped = 0
        if membership is not None:
            keep = _membership_mask(securities, days, membership)
            self.membership_dropped = int((~keep).sum())
            if not keep.any():
                raise NoInputDataError(
                    "유니버스 이력과 겹치는 feature 행이 없다 — 기간을 확인하라")
            table = table.filter(pa.array(keep))
            securities = table.column("security_id").to_pylist()
            days = table.column("trading_date").to_pylist()
        self._features = {
            "key": _encode_keys(securities, days, self._security_codes),
            "security_id": np.asarray(securities),
            "trading_date": np.asarray(days),
            "cond_price": _bool_array(table.column("cond_price").to_pylist()),
            "cond_obv": _bool_array(table.column("cond_obv").to_pylist()),
            "cond_liquidity": _bool_array(
                table.column("cond_liquidity").to_pylist()),
            "liquidity": np.asarray(
                [np.nan if v is None else float(v)
                 for v in table.column("avg_traded_value_20d").to_pylist()]),
        }
        return self._features

    def _load_labels(self, horizon, outcomes, label_version, start, end,
                     cost_multiplier=1.0, base_variant="ALL"):
        import pyarrow.dataset as ds

        columns = (["security_id", "signal_date", "stock_return_net",
                    "stock_return_gross", "excess_return_net",
                    "benchmark_return", "commission", "tax",
                    "slippage"] + list(outcomes))
        columns = list(dict.fromkeys(columns))
        expr = ((ds.field("horizon_td") == horizon)
                & (ds.field("entry_rule") == "NEXT_OPEN")
                & (ds.field("label_version") == label_version)
                & (ds.field("label_status") == LabelStatus.COMPLETE))
        table = self.store.read_arrow(
            "gold", "daily_forward_returns", columns=columns, filters=expr,
            dedupe_pk=DAILY_FORWARD_RETURN_PK)
        if table.num_rows == 0:
            raise NoInputDataError(f"horizon={horizon} label이 없다")

        securities = table.column("security_id").to_pylist()
        days = table.column("signal_date").to_pylist()
        data = {
            "key": _encode_keys(securities, days, self._security_codes),
            "security_id": np.asarray(securities),
            "trading_date": np.asarray(days),
            "net": table.column("stock_return_net").to_numpy(
                zero_copy_only=False),
            "gross": table.column("stock_return_gross").to_numpy(
                zero_copy_only=False),
            "excess_net": table.column("excess_return_net").to_numpy(
                zero_copy_only=False),
        }
        for outcome in outcomes:
            data[outcome] = _bool_array(table.column(outcome).to_pylist())

        if cost_multiplier != 1.0:
            data = _rescale_costs(data, table, cost_multiplier, outcomes)

        keep = np.ones(len(days), dtype=bool)
        if start is not None:
            keep &= np.asarray([d >= start for d in days])
        if end is not None:
            keep &= np.asarray([d <= end for d in days])
        if not keep.all():
            data = {k: v[keep] for k, v in data.items()}
        return data

    # ── 평가 ───────────────────────────────────────────────

    def evaluate(self, spec: DailyGateSpec, run_id: str = "", membership=None):
        features = self._features or self.load_features(spec.feature_version,
                                                        membership=membership)
        feature_index = {int(k): i for i, k in enumerate(features["key"])}

        results = []
        for horizon in spec.horizons:
            labels = self._load_labels(horizon, spec.outcomes,
                                       spec.label_version, spec.start,
                                       spec.end,
                                       cost_multiplier=spec.cost_multiplier)
            # label 행마다 대응하는 feature 행 위치를 찾는다.
            positions = np.fromiter(
                (feature_index.get(int(k), -1) for k in labels["key"]),
                dtype=np.int64, count=labels["key"].size)
            matched = positions >= 0
            if not matched.any():
                raise NoInputDataError(
                    f"horizon={horizon}: feature와 label이 하나도 맞지 않는다")

            aligned = {name: values[matched]
                       for name, values in labels.items() if name != "key"}
            pos = positions[matched]
            conds = {name: features[name][pos]
                     for name in ("cond_price", "cond_obv", "cond_liquidity")}

            # 조건 중 하나라도 판정 불가면 어느 조합에도 넣지 않는다. 한쪽에만
            # 남기면 표본이 달라져 차이가 조건 때문인지 결측 때문인지 알 수 없다.
            evaluable = np.ones(pos.size, dtype=bool)
            for name in ("cond_price", "cond_obv", "cond_liquidity"):
                evaluable &= (conds[name] >= 0)
            if int(evaluable.sum()) < 2:
                raise StatisticalValidationError(
                    f"horizon={horizon}: 판정 가능한 표본이 없다")

            segments = self._segment_labels(spec, aligned, features, pos)
            if segments is None:
                results += self._evaluate_slice(
                    horizon, aligned, conds, evaluable, spec, run_id,
                    spec.segment)
                continue

            for value in _segment_order(segments):
                sub = segments == value
                if int(sub.sum()) < 2:
                    continue
                sub_aligned = {k: v[sub] for k, v in aligned.items()}
                sub_conds = {k: v[sub] for k, v in conds.items()}
                sub_evaluable = evaluable[sub]
                if int(sub_evaluable.sum()) < 2:
                    continue
                try:
                    results += self._evaluate_slice(
                        horizon, sub_aligned, sub_conds, sub_evaluable, spec,
                        run_id, f"{spec.segment_by}={value}")
                except StatisticalValidationError as exc:
                    # 표본이 얇은 세그먼트는 버리지 않고 사유를 남긴다.
                    results.append({"variant": None, "horizon_td": horizon,
                                    "segment": f"{spec.segment_by}={value}",
                                    "status": QualityStatus.PARTIAL,
                                    "status_reason": str(exc)})
        return _apply_fdr(results)

    def _evaluate_slice(self, horizon, aligned, conds, evaluable, spec, run_id,
                        segment):
        """한 조각(전체 또는 세그먼트)에서 조합별 결과를 만든다."""
        evaluable_n = int(evaluable.sum())
        unevaluable_n = int(evaluable.size) - evaluable_n

        def _mask_for(variant):
            mask = evaluable.copy()
            for name in VARIANTS[variant]:
                mask &= (conds[name] == 1)
            return mask

        # 짝지음의 기준이 되는 집합. `ALL`이면 판정 가능한 전체와 같다.
        base_mask = _mask_for(spec.base_variant)
        if int(base_mask.sum()) < 2:
            raise StatisticalValidationError(
                f"기준 조합 {spec.base_variant}의 표본이 2개 미만이다")

        rows = []
        for variant in spec.variants:
            mask = _mask_for(variant)
            for outcome in spec.outcomes:
                row = self._one(variant, horizon, outcome, mask, aligned,
                                spec, run_id, evaluable_n, unevaluable_n)
                row["segment"] = segment
                row["base_variant"] = spec.base_variant
                if variant != spec.base_variant:
                    row.update(self._paired_delta(
                        outcome, horizon, mask, aligned, spec, base_mask))
                rows.append(row)
        return rows

    def _segment_labels(self, spec, aligned, features, pos):
        """세그먼트 이름표 배열. 기준이 없으면 None."""
        if not spec.segment_by:
            return None
        days = aligned["trading_date"]
        if spec.segment_by == "year":
            return np.asarray([f"{d.year}" for d in days])
        if spec.segment_by == "regime":
            regime = self._regime_map(spec)
            return np.asarray([regime.get(d, "UNKNOWN") for d in days])
        if spec.segment_by == "liquidity":
            return _liquidity_buckets(features["liquidity"][pos], days)
        raise ConfigError(f"알 수 없는 세그먼트 기준: {spec.segment_by}")

    def _regime_map(self, spec):
        """거래일 → BULL/BEAR. 지수 종가가 200일 이동평균 위인가.

        이동평균은 **그날까지의 값만** 쓴다. 미래를 쓰면 국면 자체가 look-ahead가
        된다. 200일이 차기 전 날짜는 UNKNOWN으로 남기고 세그먼트에서 뺀다.
        """
        if getattr(self, "_regime", None) is not None:
            return self._regime
        table = self.store.read_arrow(
            "silver", "benchmark_bars",
            columns=["benchmark_id", "trading_date", "close"],
            dedupe_pk=("benchmark_id", "trading_date"))
        wanted = f"KRX:{spec.universe_id}"
        days, closes = [], []
        for bid, day, close in zip(table.column("benchmark_id").to_pylist(),
                                   table.column("trading_date").to_pylist(),
                                   table.column("close").to_pylist()):
            if bid != wanted or close is None:
                continue
            days.append(day)
            closes.append(float(close))
        if not days:
            raise NoInputDataError(
                f"{wanted} 벤치마크 일봉이 없다 — 국면을 정의할 수 없다")
        order = np.argsort(np.asarray(days))
        days = [days[i] for i in order]
        values = np.asarray(closes)[order]
        window = 200
        out = {}
        running = np.cumsum(values)
        for i, day in enumerate(days):
            if i + 1 < window:
                out[day] = "UNKNOWN"
                continue
            total = running[i] - (running[i - window] if i >= window else 0.0)
            out[day] = "BULL" if values[i] >= total / window else "BEAR"
        self._regime = out
        return out

    def _one(self, variant, horizon, outcome, mask, aligned, spec, run_id,
             evaluable_n, unevaluable_n):
        selected = int(mask.sum())
        if selected < 2:
            raise StatisticalValidationError(
                f"{variant} h={horizon}: 선택된 표본이 2개 미만이다")

        turnover = _turnover(aligned["security_id"][mask],
                             aligned["trading_date"][mask])
        y = aligned[outcome][mask].astype(np.float64)
        valid = aligned[outcome][mask] >= 0
        y = y[valid]
        dates = aligned["trading_date"][mask][valid]
        if y.size < 2:
            raise StatisticalValidationError(
                f"{variant} h={horizon} {outcome}: 유효 표본이 없다")

        robust = cluster_robust_mean(y, dates, level=spec.ci_level)
        block = block_length_for(horizon)
        try:
            boot = block_bootstrap_mean(y, dates, block_length=block,
                                        n_resamples=spec.n_resamples,
                                        seed=spec.seed, level=spec.ci_level)
        except StatisticalValidationError:
            boot = None
        naive = naive_mean(y, level=spec.ci_level)
        stats = icc_and_design_effect(y, dates)

        # 가장 넓은 구간을 대표로 쓴다. 독립가정 CI를 후보에 넣는 이유는
        # 군집 내 상관이 음수일 때 군집 CI가 더 좁아질 수 있기 때문이다 —
        # 좁아지는 이득은 취하지 않는다(기준선 계산과 같은 규칙).
        method, chosen = max(
            [("CLUSTER_ROBUST_CR1", robust), ("NAIVE_IID_FLOOR", naive)]
            + ([("MOVING_BLOCK_BOOTSTRAP", boot)] if boot else []),
            key=lambda item: item[1].ci_high - item[1].ci_low)

        net = aligned["net"][mask]
        gross = aligned["gross"][mask]
        excess = aligned["excess_net"][mask]

        spec_key = {
            "variant": variant, "horizon_td": horizon, "outcome": outcome,
            # 기준 조합이 다르면 다른 실험이다. 빼면 중첩 비교와 ALL 대비가
            # 같은 result_id를 받는다.
            "base_variant": spec.base_variant,
            "universe_id": spec.universe_id,
            "feature_version": self._feature_version,
            "label_version": spec.label_version,
            "segment": spec.segment,
            "start": spec.start, "end": spec.end,
            "ci_level": spec.ci_level, "seed": spec.seed,
            "n_resamples": spec.n_resamples, "block_length": block,
        }

        return {
            "result_id": "dg_" + sha256_of(spec_key)[len("sha256:"):][:32],
            "variant": variant,
            "preregistered": variant in PREREGISTERED,
            "conditions": list(VARIANTS[variant]),
            "horizon_td": horizon,
            "outcome": outcome,
            "universe_id": spec.universe_id,
            "segment": spec.segment,
            "evidence_tier": EvidenceTier.DAILY_ONLY_LONG,

            "raw_n": int(y.size),
            "trading_date_n": int(np.unique(dates).size),
            "positive_n": int(y.sum()),
            "probability": float(y.mean()),
            # 선택률은 **판정 가능한** 모집단 대비다. 그래서 ALL은 1.0이 된다.
            "coverage": round(selected / evaluable_n, 6),
            # 회전율: 하루 선택 종목 중 전 거래일에 없던 비율(로드맵 DoD).
            "turnover": (round(turnover, 6) if turnover is not None else None),
            "evaluable_n": evaluable_n,
            "unevaluable_n": unevaluable_n,

            "mean_return_net": _nanmean(net),
            "mean_return_gross": _nanmean(gross),
            "median_return_net": _nanmedian(net),
            "mean_excess_net": _nanmean(excess),

            "cluster_key": "signal_date",
            "icc": stats["icc"],
            "design_effect": stats["design_effect"],
            "effective_n": stats["effective_n"],
            "ci_method": method,
            "ci_low": chosen.ci_low,
            "ci_high": chosen.ci_high,
            "ci_level": spec.ci_level,
            "naive_ci_low": naive.ci_low,
            "naive_ci_high": naive.ci_high,
            "block_length": block,
            "bootstrap_replicates": (boot.detail["bootstrap_replicates"]
                                     if boot else None),
            "seed": spec.seed,

            "feature_version": self._feature_version,
            "label_version": spec.label_version,
            "run_id": run_id,
            "schema_version": SCHEMA_VERSION,
            "data_revision": self.data_revision,
            "status": QualityStatus.SUCCESS,
        }

    def _paired_delta(self, outcome, horizon, mask, aligned, spec,
                      base_mask):
        """같은 날짜 안에서 (조건 충족) − (전체)의 차이.

        날짜를 짝지어 비교하지 않으면 "조건의 효과"와 "조건이 켜지는 날의 시장
        상황"이 섞인다. 날짜별 차이를 만든 뒤 그 차이에 군집 추론을 적용한다.

        기준선은 **표에 함께 보고되는 집합**이어야 한다. 조건 판정이
        불가능한 워밍업 구간을 기준선에만 넣으면, 차이가 조건의 효과가 아니라
        모집단 차이를 반영하게 된다(기준 집합을 인자로 받는 이유다).

        기준은 `spec.base_variant`가 정한다. `ALL`이면 "그날 전체 대비"이고,
        `PRICE`면 "가격 조건만 건 집합 대비"다. 뒤쪽이 **중첩 비교**이고,
        "이 성분이 저 성분 위에 무언가를 더하는가"는 그렇게만 물을 수 있다.

        확률과 수익률을 모두 본다. 둘은 다른 이야기를 할 수 있다 — 적중률이
        낮아도 평균 수익이 높은 조합(꼬리가 두꺼운 신호)이 실제로 존재한다.
        하나만 보고 판단하면 그 구조를 놓친다.

        **보유기간 중첩.** h일 보유면 이웃한 날짜의 신호가 같은 가격 구간을
        공유한다. h=20이면 오늘과 내일의 보유기간이 19일 겹친다. 날짜별 차이를
        독립 관측으로 세면 그 겹침만큼 구간이 좁아지므로, 블록 부트스트랩
        (블록 길이 >= horizon)을 함께 돌리고 **더 넓은 쪽을 쓴다.**
        """
        dates = aligned["trading_date"]
        outcome_values = aligned[outcome]
        outcome_valid = outcome_values >= 0

        out = self._delta_for(
            outcome_values.astype(np.float64), dates, mask & outcome_valid,
            base_mask & outcome_valid, spec, horizon,
            keys=("delta_vs_all", "delta_ci_low", "delta_ci_high",
                  "delta_dates_n", "delta_ci_method"))

        returns = aligned["net"]
        return_valid = np.isfinite(returns)
        out.update(self._delta_for(
            returns, dates, mask & return_valid, base_mask & return_valid,
            spec, horizon,
            keys=("delta_return_net", "delta_return_ci_low",
                  "delta_return_ci_high", "delta_return_dates_n",
                  "delta_return_ci_method")))
        return out

    @staticmethod
    def _delta_for(values, dates, sel, base, spec, horizon, keys):
        """날짜별 (선택 평균 − 기준 평균). 군집 robust와 블록 중 넓은 쪽."""
        empty = dict(zip(keys, (None, None, None, 0, None)))
        if sel.sum() < 2 or base.sum() < 2:
            return empty

        all_dates, base_codes = np.unique(dates[base], return_inverse=True)
        base_sum = np.bincount(base_codes, weights=values[base],
                               minlength=all_dates.size)
        base_cnt = np.bincount(base_codes, minlength=all_dates.size)

        sel_codes = np.searchsorted(all_dates, dates[sel])
        sel_sum = np.bincount(sel_codes, weights=values[sel],
                              minlength=all_dates.size)
        sel_cnt = np.bincount(sel_codes, minlength=all_dates.size)

        has_both = (sel_cnt > 0) & (base_cnt > 0)
        if has_both.sum() < 2:
            return empty

        delta = (sel_sum[has_both] / sel_cnt[has_both]
                 - base_sum[has_both] / base_cnt[has_both])
        paired_dates = all_dates[has_both]
        candidates = [("CLUSTER_ROBUST_CR1",
                       cluster_robust_mean(delta, paired_dates,
                                           level=spec.ci_level))]
        try:
            candidates.append(("MOVING_BLOCK_BOOTSTRAP", block_bootstrap_mean(
                delta, paired_dates, block_length=block_length_for(horizon),
                n_resamples=spec.n_resamples, seed=spec.seed,
                level=spec.ci_level)))
        except StatisticalValidationError:
            # 날짜 수가 블록 길이보다 적으면 블록을 만들 수 없다.
            pass
        method, est = max(candidates,
                          key=lambda item: item[1].ci_high - item[1].ci_low)
        return dict(zip(keys, (float(np.mean(delta)), est.ci_low, est.ci_high,
                               int(paired_dates.size), method)))


# ─────────────────────────────────────────────────────────────
# 배열 도우미
# ─────────────────────────────────────────────────────────────

def _rescale_costs(data, table, multiplier, outcomes):
    """비용을 배수로 바꿔 순수익과 결과를 다시 계산한다.

    라벨의 순수익은 **가격에 비용을 곱해** 만든 값이다(costs.net_return).

        net = (1+gross) x (1-slip)(1-comm-tax) / [(1+slip)(1+comm)] - 1

    그래서 gross와 세 요율만 있으면 다른 비용 가정의 순수익을 **정확히** 다시
    만들 수 있다. 근사가 아니다. +0.086%p 같은 작은 차이는 비용 가정이 조금만
    달라져도 사라질 수 있으므로, 어디서 부호가 바뀌는지 봐야 한다.
    """
    gross = data["gross"]
    comm = table.column("commission").to_numpy(zero_copy_only=False) * multiplier
    tax = table.column("tax").to_numpy(zero_copy_only=False) * multiplier
    slip = table.column("slippage").to_numpy(zero_copy_only=False) * multiplier
    factor = ((1.0 - slip) * (1.0 - comm - tax)) / ((1.0 + slip) * (1.0 + comm))
    net = (1.0 + gross) * factor - 1.0

    benchmark = table.column("benchmark_return").to_numpy(zero_copy_only=False)
    out = dict(data)
    out["net"] = net
    out["excess_net"] = net - benchmark
    for outcome in outcomes:
        if outcome == "up":
            out["up"] = np.where(np.isnan(net), -1, (net > 0).astype(np.int8))
        elif outcome == "outperform":
            out["outperform"] = np.where(
                np.isnan(net) | np.isnan(benchmark), -1,
                (net > benchmark).astype(np.int8))
    return out


def _membership_mask(securities, days, intervals):
    """(종목, 날짜)가 그날 구성종목이었는가."""
    out = np.zeros(len(securities), dtype=bool)
    for i, (sec, day) in enumerate(zip(securities, days)):
        for start, end in intervals.get(sec, ()):
            if day < start:
                break
            if end is None or day < end:
                out[i] = True
                break
    return out


def _encode_keys(securities, days, codes=None):
    """(종목, 날짜) → int64 키. 정렬·조회를 빠르게 하기 위한 인코딩.

    **`codes`는 반드시 두 표가 공유해야 한다.** 표마다 새로 만들면 같은
    종목이 표마다 다른 정수를 받고, 조인이 조용히 **엉뚱한 종목**을 붙인다.

    2026-09-21에 실제로 그렇게 돼 있었다. `load_features`는 유니버스로
    거른 뒤 코드북을 만들고 `_load_labels`는 거르지 않은 표로 따로 만들어,
    첫 등장 순서가 어긋났다. 조인된 773,124행 중 **666,054행(86%)이 다른
    종목의 수익률**에 붙었다. 결과는 그럴듯했고 아무 예외도 나지 않았다.

    그래서 코드북을 인자로 받는다. 안 주면 지역 코드북을 만드는데, 그건
    **한 표만 쓸 때에 한해** 안전하다.
    """
    codes = {} if codes is None else codes
    sec_index = []
    for sid in securities:
        idx = codes.get(sid)
        if idx is None:
            idx = codes[sid] = len(codes)
        sec_index.append(idx)
    ordinals = np.asarray([d.toordinal() for d in days], dtype=np.int64)
    return np.asarray(sec_index, dtype=np.int64) * 10_000_000 + ordinals

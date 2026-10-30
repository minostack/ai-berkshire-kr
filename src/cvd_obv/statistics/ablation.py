# -*- coding: utf-8 -*-
"""Ablation — 로드맵 Stage 8.

성분을 하나씩 빼고 같은 데이터로 다시 재서, **그 성분이 실제로 무슨 일을
하고 있었는지** 본다. 빼도 결과가 같다면 그 성분은 아무 일도 하지 않고
있었다는 뜻이다.

어떻게 공정하게 비교하는가
--------------------------
로드맵 DoD: "ablation별 동일 날짜·동일 유니버스·동일 비용 비교가 보장된다."

같은 신호 표를 쓰고, 같은 label(=같은 비용 모델)을 쓰고, **같은 거래일에서만**
비교한다. 변형이 그날 아무것도 고르지 않으면 그날은 양쪽 모두에서 뺀다 —
한쪽에만 남기면 표본이 달라져 차이가 성분 때문인지 날짜 구성 때문인지 알 수
없게 된다.

무엇을 재는가
-------------
날짜 d에서

    edge(v, d) = (변형 v가 고른 것의 평균) − (그날 전체 평균)

`edge`를 쓰는 이유는 그날의 시장을 빼기 위해서다. 그다음 두 가지를 본다.

    변형 자체의 edge  = mean_d edge(v, d)             → 기준선보다 나은가
    ablation delta    = mean_d [edge(v,d) − edge(BASE,d)] → 빼니 달라졌는가

선택 방법 둘을 나눈다 — 로드맵이 요구한 "score와 absolute Gate의 분리 비교"다.

    BUY   = Gate 통과분 전부(점수 무시)
    TOP20 = Gate 통과분 중 점수 상위 20

세 가지가 갈린다.

    BASE의 BUY       = Gate만 쓴 경우
    BASE의 TOP20     = Gate + 점수
    NO_GATE의 TOP20  = 점수만 쓴 경우 (Gate가 데이터 품질만 남으므로)
"""

from __future__ import annotations

import collections

import numpy as np

from .. import SCHEMA_VERSION
from ..contracts.ablation_schema import ABLATION_RESULT_COLUMNS, SELECTIONS
from ..contracts.label_schema import LABEL_VERSION, SIGNAL_FORWARD_RETURN_PK
from ..contracts.schema import (
    SIGNAL_FEATURE_PK, SIGNAL_PK, assert_single_signal_source,
)
from ..domain.errors import ConfigError, NoInputDataError
from ..domain.ids import sha256_of
from ..domain.status import (
    DetailCode, LabelStatus, QualityStatus, SignalStatus)
from ..backtest.common import apply_fdr
from .inference import block_bootstrap_mean, cluster_robust_mean
from .base_rates import block_length_for
from .power import (
    minimum_detectable_effect, required_observations, verdict_for,
)
from .score_model import COMPONENTS, CVD_TERMS, ScoreModel

__all__ = ["VARIANTS", "TOP_N", "AblationRunner"]

#: 로드맵 Stage 8이 이름으로 요구한 제거 목록.
VARIANTS = {
    "BASE": (),
    "NO_GATE": ("gate",),
    "NO_CVD": ("cvd",),
    "NO_OBV": ("obv",),
    "NO_PRICE": ("price",),
    "NO_LIQUIDITY": ("liquidity",),
    "NO_CVD_DELTA": ("cvd_delta",),
    "NO_CVD_MOMENTUM": ("cvd_momentum",),
    "NO_CVD_BULL_RATIO": ("cvd_bull_ratio",),
}

#: TOP 선택의 종목 수. BUY_CANDIDATE 하루 중앙값(19)에 맞춰 20으로 둔다 —
#: 두 선택의 크기가 크게 다르면 차이가 선택 방식 때문인지 표본 크기 때문인지
#: 섞인다.
TOP_N = 20

_FEATURE_COLUMNS = [
    "signal_id",
    "cvd_normalized_delta", "cvd_normalized_momentum",
    "cvd_bull_ratio_completed", "cvd_momentum_status",
    "cvd_data_status", "cvd_regular_data_status",
    "obv", "obv_ma20", "obv_normalized_slope", "obv_data_status",
    "above_ma20", "above_ma60", "ma20_rising", "ma20_above_ma60",
    "price_data_status",
    "avg_traded_value_20d", "liquidity_data_status",
    "risk_penalty",
]

_OUTCOMES = {
    "mean_return_net": "net",
    "hit_rate": "up",
}


class AblationRunner:
    """`gold.signal_features` × `gold.forward_returns` → `gold.ablation_results`."""

    LAYER = "gold"
    TABLE = "ablation_results"

    def __init__(self, store, horizon_td: int = 20,
                 label_version: str = LABEL_VERSION, index_id="KOSPI200",
                 ci_level: float = 0.95, n_resamples: int = 1000,
                 seed: int = 20260919, data_revision: int = 1,
                 signal_source: str = None, config_hash: str = None):
        self.store = store
        # Ablation은 V1.5 점수를 재구성하므로 CVD가 있는 REPLAY 신호에서만
        # 뜻이 있다. DAILY_PANEL 신호는 점수가 null이라 섞이면 조용히
        # 모집단만 부풀린다.
        self.signal_source = signal_source
        # `signal_source`만으로는 **같은 source 안의 다른 설정**이
        # 갈리지 않는다. bdays나 period가 다른 실행이 한 백분위
        # 모집단으로 합쳐지면 점수 재구성이 통째로 어긋난다
        # (실측: 50,822 + 50,022 = 100,844행, 불일치 97.5%).
        self.config_hash = config_hash
        self.horizon_td = int(horizon_td)
        self.label_version = label_version
        self.index_id = index_id
        self.ci_level = float(ci_level)
        self.n_resamples = int(n_resamples)
        self.seed = int(seed)
        self.data_revision = int(data_revision)
        self._days = None
        self.verification = None

    # ── 입력 ──────────────────────────────────────────────────

    def load(self, start=None, end=None):
        """하루치 (feature + 결과)를 거래일별로 묶는다."""
        import pyarrow.dataset as ds

        signals = self.store.read_arrow(
            self.LAYER, "signals",
            columns=["signal_id", "security_id", "trading_date", "status",
                     "total_score", "index_id", "signal_source",
                     "config_hash"],
            dedupe_pk=SIGNAL_PK)
        if signals.num_rows == 0:
            raise NoInputDataError("신호가 없다 — `replay run` 먼저")
        assert_single_signal_source(signals.to_pylist(), self.signal_source,
                                    "ablation run")
        rows_all = signals.to_pylist()
        # 어느 설정을 평가하려는지는 호출자만 안다. 조용히 하나를
        # 고르면 나중에 "그 숫자가 어느 실행인지" 아무도 못 짚는다.
        hashes = sorted({r["config_hash"] for r in rows_all
                         if (not self.signal_source
                             or r["signal_source"] == self.signal_source)
                         and r["index_id"] == self.index_id
                         and r["config_hash"]})
        if self.config_hash is None and len(hashes) > 1:
            raise ConfigError(
                f"{self.index_id}에 설정이 {len(hashes)}개 있다 — "
                "어느 실행을 평가할지 `config_hash`로 지정하라 "
                f"(가능: {[h[:19] for h in hashes]})",
                detail_code=DetailCode.CONFIG_INVALID)
        meta = {}
        for row in rows_all:
            # 가드만 걸고 거르지 않으면 두 출처가 함께 들어온다. 실제로
            # REPLAY_AUCTION을 추가한 날 100,840행(50,420 x 2)이 섞여
            # 점수 재구성이 96% 불일치로 멈췄다 — hard fail이 잡아냈다.
            if self.signal_source and row["signal_source"] != self.signal_source:
                continue
            if row["index_id"] != self.index_id:
                continue
            if self.config_hash and row["config_hash"] != self.config_hash:
                continue
            day = row["trading_date"]
            if start is not None and day < start:
                continue
            if end is not None and day > end:
                continue
            meta[row["signal_id"]] = row
        if not meta:
            raise NoInputDataError("조건에 맞는 신호가 없다")

        features = self.store.read_arrow(
            self.LAYER, "signal_features", columns=_FEATURE_COLUMNS,
            dedupe_pk=SIGNAL_FEATURE_PK)

        expr = ((ds.field("horizon_td") == self.horizon_td)
                & (ds.field("entry_rule") == "NEXT_OPEN")
                & (ds.field("label_version") == self.label_version)
                & (ds.field("label_status") == LabelStatus.COMPLETE))
        labels = self.store.read_arrow(
            self.LAYER, "forward_returns",
            columns=["signal_id", "stock_return_net", "up"],
            filters=expr, dedupe_pk=SIGNAL_FORWARD_RETURN_PK)
        if labels.num_rows == 0:
            raise NoInputDataError(
                f"horizon={self.horizon_td} label이 없다 — "
                "`signal-label build` 먼저")
        outcome = {r["signal_id"]: r for r in labels.to_pylist()}

        by_day = collections.defaultdict(list)
        for row in features.to_pylist():
            m = meta.get(row["signal_id"])
            if m is None:
                continue
            got = outcome.get(row["signal_id"])
            row = dict(row)
            row["trading_date"] = m["trading_date"]
            row["security_id"] = m["security_id"]
            row["v15_status"] = m["status"]
            row["v15_score"] = m["total_score"]
            # 성숙하지 않았거나 가격이 없는 신호는 결과가 없다. 점수·Gate
            # 계산에는 남겨 둔다 — 백분위 모집단이 달라지면 안 되기 때문이다.
            row["net"] = (None if got is None
                          else float(got["stock_return_net"]))
            row["up"] = (None if got is None
                         else (1.0 if got["up"] else 0.0))
            by_day[m["trading_date"]].append(row)
        self._days = dict(by_day)
        return self._days

    def verify_reconstruction(self):
        """되짚은 점수·Gate가 V1.5와 같은지 먼저 확인한다.

        여기서 실패하면 ablation 전체가 무의미하므로, 실행 전에 거른다.
        """
        days = self._days or self.load()
        model = ScoreModel()
        scores, statuses = {}, {}
        for rows in days.values():
            for row in rows:
                scores[row["signal_id"]] = row["v15_score"]
                statuses[row["signal_id"]] = row["v15_status"]
        self.verification = {
            "score": model.verify(days, scores),
            "gate": model.verify_gate(days, statuses),
        }
        return self.verification

    # ── 변형별 날짜 계열 ───────────────────────────────────────

    def _edges(self, variant, days):
        """변형 v의 날짜별 edge 계열.

        반환: {(selection, outcome): {날짜: edge}} 와 선택 크기 기록.
        """
        model = ScoreModel(VARIANTS[variant])
        series = {(sel, name): {} for sel in SELECTIONS for name in _OUTCOMES}
        sizes = collections.defaultdict(list)

        for day in sorted(days):
            rows = days[day]
            scores = model.score_day(rows)
            gates = [model.gate_row(row)[0] for row in rows]

            buy = [i for i, g in enumerate(gates)
                   if g == SignalStatus.BUY_CANDIDATE]
            # TOP은 **Gate를 통과한 것 중** 점수 상위 N이다. 여기서 Gate를
            # 무시하면 모든 변형의 TOP이 같아져 "Gate가 하는 일"을 잴 수 없다
            # (처음 구현에서 실제로 그랬다 — BASE와 NO_GATE의 TOP20이
            # 글자 그대로 같은 값이 나왔다). `NO_GATE`에서는 Gate가 데이터
            # 품질만 남으므로 TOP이 곧 "점수만 쓴 경우"가 된다.
            top = sorted(buy,
                         key=lambda i: (-scores[i], rows[i]["security_id"])
                         )[:TOP_N]

            for name, field in _OUTCOMES.items():
                population = [rows[i][field] for i in range(len(rows))
                              if rows[i][field] is not None]
                if len(population) < 2:
                    continue
                base = float(np.mean(population))
                for sel, picked in (("BUY", buy), ("TOP20", top)):
                    values = [rows[i][field] for i in picked
                              if rows[i][field] is not None]
                    if not values:
                        continue
                    series[(sel, name)][day] = float(np.mean(values)) - base
                    sizes[sel].append(len(values))
        return series, {k: float(np.mean(v)) if v else 0.0
                        for k, v in sizes.items()}

    # ── 실행 ──────────────────────────────────────────────────

    def run(self, run_id: str = "", variants=None, start=None, end=None,
            dry_run: bool = False):
        # 인자 검증은 **데이터를 읽기 전에** 한다. 뒤에 두면 오타 하나 때문에
        # 수십만 행을 읽고 나서야 "알 수 없는 변형"이 나오고, 그 사이 다른
        # 오류가 먼저 터지면 진짜 원인이 가려진다.
        names = list(variants or VARIANTS)
        unknown = [v for v in names if v not in VARIANTS]
        if unknown:
            raise ConfigError(
                f"알 수 없는 변형: {unknown} (가능: {sorted(VARIANTS)})")
        if "BASE" not in names:
            names = ["BASE"] + names

        days = self.load(start, end)
        self.verify_reconstruction()

        computed = {name: self._edges(name, days) for name in names}
        base_series, base_sizes = computed["BASE"]
        ablation_run_id = "ab_" + sha256_of({
            "variants": sorted(names), "horizon_td": self.horizon_td,
            "index_id": self.index_id, "label_version": self.label_version,
            "top_n": TOP_N, "seed": self.seed,
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
        })[len("sha256:"):][:32]

        rows = []
        for name in names:
            series, sizes = computed[name]
            for sel in SELECTIONS:
                for outcome in _OUTCOMES:
                    rows.append(self._row(
                        ablation_run_id, name, sel, outcome,
                        series[(sel, outcome)], base_series[(sel, outcome)],
                        sizes.get(sel, 0.0), run_id))

        # 같은 질문끼리만 보정한다. 변형의 edge(기준선 대비)와 ablation
        # delta(BASE 대비)는 다른 질문이다.
        apply_fdr(rows, value_key="edge",
                  ci_keys=("edge_ci_low", "edge_ci_high"), family="edge")
        apply_fdr(rows, value_key="delta",
                  ci_keys=("ci_low", "ci_high"), family="ablation_delta")
        for row in rows:
            row["adjusted_p_value"] = row.pop("delta_q_value", None)
            row["p_value"] = row.pop("delta_p_value", None)
            row["adjustment_method"] = "BH_FDR"
            for key in ("edge_p_value", "edge_q_value", "edge",
                        "edge_ci_low", "edge_ci_high",
                        "edge_fdr_significant", "edge_fdr_tests",
                        "delta_fdr_significant", "delta_fdr_tests",
                        "fdr_significant", "fdr_tests", "fdr_family",
                        "fdr_alpha", "p_value_method"):
                row.pop(key, None)

        writes = []
        if not dry_run:
            writes.append(self._write(rows, run_id))
        return {"ablation_run_id": ablation_run_id, "rows": rows,
                "variants": names, "verification": self.verification,
                "trading_days": len(days),
                "artifacts": [w.manifest_path for w in writes]}

    def _row(self, ablation_run_id, variant, selection, outcome, series,
             base_series, mean_size, run_id):
        """한 변형 x 선택 x 결과의 행. `variant`가 BASE면 delta는 없다."""
        shared = sorted(set(series) & set(base_series))
        spec = {"variant": variant, "removed": list(VARIANTS[variant]),
                "selection": selection, "outcome": outcome,
                "horizon_td": self.horizon_td, "top_n": TOP_N}
        row = {
            "ablation_run_id": ablation_run_id,
            "variant_id": variant,
            "metric_name": f"{selection}_{outcome}",
            "segment_key": "ALL",
            "base_experiment_id": "BASE",
            "removed_components": list(VARIANTS[variant]),
            "variant_spec_hash": sha256_of(spec),
            "selection": selection,
            "horizon_td": self.horizon_td,
            "paired_dates": len(shared),
            "cluster_key": "signal_date",
            "seed": self.seed,
            "ci_level": self.ci_level,
            "label_version": self.label_version,
            "run_id": run_id,
            "schema_version": SCHEMA_VERSION,
            "data_revision": self.data_revision,
            "mean_selection_size": mean_size,
        }
        if len(shared) < 2:
            row.update({"status": QualityStatus.PARTIAL,
                        "status_reason": "짝지을 수 있는 날짜가 2일 미만이다",
                        "edge_verdict": "NOT_EVALUABLE",
                        "inference_method": "NONE"})
            return row

        dates = np.asarray(shared)
        variant_edge = np.asarray([series[d] for d in shared])
        base_edge = np.asarray([base_series[d] for d in shared])

        # (1) 변형 자체가 기준선보다 나은가.
        edge_est, method = self._estimate(variant_edge, dates)
        # (2) BASE에서 성분을 빼니 달라졌는가. BASE 자신은 뺄 것이 없다 —
        #     0을 "차이 없음"으로 보고하면 다른 변형의 0과 구별되지 않는다.
        if variant == "BASE":
            delta_est = {"value": None, "ci_low": None, "ci_high": None,
                         "se": None, "replicates": None}
        else:
            delta_est, method = self._estimate(variant_edge - base_edge,
                                               dates)

        mde = minimum_detectable_effect(edge_est["se"])
        row.update({
            "base_value": float(base_edge.mean()),
            "variant_value": float(variant_edge.mean()),
            "delta": delta_est["value"],
            "ci_low": delta_est["ci_low"],
            "ci_high": delta_est["ci_high"],
            "standard_error": delta_est["se"],
            "inference_method": method,
            "block_length": block_length_for(self.horizon_td),
            "bootstrap_replicates": delta_est["replicates"],
            "icc": edge_est["icc"],
            "design_effect": edge_est["design_effect"],
            "effective_n": edge_est["effective_n"],
            "mde_80": mde,
            "observed_over_mde": (abs(edge_est["value"]) / mde
                                  if mde else None),
            "required_dates_80": required_observations(
                edge_est["value"], mde, len(shared)),
            "edge_verdict": verdict_for(edge_est["value"], edge_est["ci_low"],
                                        edge_est["ci_high"], mde),
            "status": QualityStatus.SUCCESS,
            "status_reason": (None if variant != "BASE"
                              else "BASE는 자기 자신과의 차이라 delta가 없다"),
            # FDR 계산에만 쓰고 저장 전에 지운다.
            "edge": edge_est["value"],
            "edge_ci_low": edge_est["ci_low"],
            "edge_ci_high": edge_est["ci_high"],
        })
        return row

    def _estimate(self, values, clusters):
        """군집 robust와 블록 부트스트랩 중 **넓은 쪽**. Stage 2·6·7과 같은 규칙."""
        from ..domain.errors import StatisticalValidationError

        candidates = [("CLUSTER_ROBUST_CR1",
                       cluster_robust_mean(values, clusters,
                                           level=self.ci_level))]
        try:
            candidates.append(("MOVING_BLOCK_BOOTSTRAP", block_bootstrap_mean(
                values, clusters,
                block_length=block_length_for(self.horizon_td),
                n_resamples=self.n_resamples, seed=self.seed,
                level=self.ci_level)))
        except StatisticalValidationError:
            pass
        method, est = max(candidates,
                          key=lambda item: item[1].ci_high - item[1].ci_low)
        detail = est.detail or {}
        return {
            "value": float(est.value), "ci_low": est.ci_low,
            "ci_high": est.ci_high,
            # 구간에서 되짚은 SE를 쓴다 — 부트스트랩 SE와 구간이 어긋나면
            # MDE가 보고된 구간과 다른 이야기를 하게 된다.
            "se": ((est.ci_high - est.ci_low) / (2.0 * 1.959963985)
                   if est.ci_high is not None and est.ci_low is not None
                   else None),
            "replicates": detail.get("bootstrap_replicates"),
            "icc": detail.get("icc"), "design_effect": detail.get("design_effect"),
            "effective_n": est.effective_n,
        }, method

    def _write(self, rows, run_id):
        from ..contracts.ablation_schema import ABLATION_RESULT_PK

        payload = [{k: v for k, v in row.items()
                    if k in {c.name for c in ABLATION_RESULT_COLUMNS}}
                   for row in rows]
        return self.store.write(
            self.LAYER, self.TABLE, payload, ABLATION_RESULT_COLUMNS,
            ABLATION_RESULT_PK, run_id,
            partition_fn=lambda r: [("horizon_td", r["horizon_td"])],
            extra_manifest={
                "ablation_run_id": rows[0]["ablation_run_id"],
                "top_n": TOP_N, "seed": self.seed,
                "ci_level": self.ci_level, "n_resamples": self.n_resamples,
                "block_length": block_length_for(self.horizon_td),
                "cluster_key": "signal_date",
                "reconstruction_verified": self.verification,
            })

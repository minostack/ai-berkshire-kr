# -*- coding: utf-8 -*-
"""신호 성과 평가 — 로드맵 Stage 6.

V2의 원래 질문은 셋이다.

    1. BUY_CANDIDATE가 WATCH보다 나은가
    2. 점수가 높을수록 수익이 나은가 (단조성)
    3. 순위 상위를 골라 쓰면 나은가

세 질문 모두 **Stage 2와 같은 방법**으로 답한다. 전체 평균 하나로 판단하지
않는다. 같은 날짜끼리 짝지어 차이를 구하고, 날짜를 군집으로 보는 robust 구간을
붙이고, 격자 전체에 다중비교 보정을 건다.

왜 짝을 짓는가
--------------
BUY_CANDIDATE가 많이 나오는 날은 시장이 오르는 날이다. 짝짓지 않고 평균만
비교하면 "신호의 효과"와 "그 신호가 켜지는 날의 시장 상황"이 섞인다. 날짜별로
(선택 평균 − 그날 전체 평균)을 만든 뒤 그 차이에 추론을 적용한다.

단조성은 왜 IC로 보는가
-----------------------
5분위 평균만 보면 Q5가 Q1보다 높은지는 알 수 있어도, 그 사이가 순서대로인지
우연인지 구분되지 않는다. 날짜마다 (점수, 수익)의 순위상관을 구해 그 값의
평균과 구간을 보면 **모든 날에 걸쳐 순서가 유지되는가**를 하나의 수로 답할 수
있다. 이게 흔히 말하는 rank IC다. 관측 단위가 날짜이므로 군집 문제도 없다.

모집단
------
`BUY_CANDIDATE`와 `WATCH`만 본다. `DATA_INSUFFICIENT`는 점수 순위 자체가
없어서(rank가 null) 같은 자리에 놓을 수 없다. 몇 건이 빠졌는지는 결과에
`data_insufficient_n`으로 남긴다.

`label_status != COMPLETE`인 행도 뺀다. 성숙하지 않은 구간(h=20이면 마지막 20
거래일)을 0으로 취급하면 최근 신호가 전부 "수익 0"이 된다.
"""

from __future__ import annotations

import numpy as np

from .. import SCHEMA_VERSION
from ..contracts.label_schema import LABEL_VERSION, SIGNAL_FORWARD_RETURN_PK
from ..contracts.schema import SIGNAL_PK, assert_single_signal_source
from ..domain.errors import (
    ConfigError, NoInputDataError, StatisticalValidationError,
)
from ..domain.ids import sha256_of
from ..domain.status import EvidenceTier, LabelStatus, QualityStatus, SignalStatus
from ..statistics.base_rates import block_length_for
from ..statistics.inference import (
    block_bootstrap_mean, cluster_robust_mean, icc_and_design_effect,
    naive_mean,
)
from .common import apply_fdr, nanmean, nanmedian, segment_order, turnover

__all__ = ["SIGNAL_VARIANTS", "PREREGISTERED", "SignalGateSpec",
           "SignalGateEvaluator"]

#: 평가할 선택 규칙. 값은 (종류, 인자)이며 `ALL`은 그날 평가 가능한 전부다.
#:   status  — V1.5가 낸 신호 상태
#:   score_q — 그날 점수 5분위(Q5가 최상위)
#:   rank_top— 그날 순위 상위 N종목
SIGNAL_VARIANTS = {
    "ALL": ("all", None),
    "BUY_CANDIDATE": ("status", SignalStatus.BUY_CANDIDATE),
    "WATCH": ("status", SignalStatus.WATCH),
    "SCORE_Q1": ("score_q", 1),
    "SCORE_Q2": ("score_q", 2),
    "SCORE_Q3": ("score_q", 3),
    "SCORE_Q4": ("score_q", 4),
    "SCORE_Q5": ("score_q", 5),
    "RANK_TOP3": ("rank_top", 3),
    "RANK_TOP5": ("rank_top", 5),
    "RANK_TOP10": ("rank_top", 10),
    "RANK_TOP20": ("rank_top", 20),
}

#: 로드맵이 이름으로 지목한 조합. 나머지는 없다 — Stage 6·7이 요구한
#: "BUY_CANDIDATE 전체와 Top 3/5/10/20, score bucket, rank percentile"이
#: 그대로 이 목록이다. 그래도 다중비교 보정은 전부에 건다.
PREREGISTERED = frozenset(SIGNAL_VARIANTS)

_EVALUABLE_STATUS = (SignalStatus.BUY_CANDIDATE, SignalStatus.WATCH)


class SignalGateSpec:
    """한 번의 평가 정의."""

    __slots__ = ("variants", "horizons", "outcomes", "label_version",
                 "index_id", "start", "end", "ci_level", "n_resamples",
                 "seed", "segment_by", "signal_source")

    def __init__(self, variants=None, horizons=(1, 5, 20),
                 outcomes=("up", "up_gross"), label_version=LABEL_VERSION,
                 index_id="KOSPI200", start=None, end=None, ci_level=0.95,
                 n_resamples=1000, seed=20260919, segment_by=None,
                 signal_source=None):
        self.variants = list(variants or SIGNAL_VARIANTS)
        self.signal_source = signal_source
        self.horizons = tuple(int(h) for h in horizons)
        self.outcomes = tuple(outcomes)
        self.label_version = label_version
        self.index_id = index_id
        self.start = start
        self.end = end
        self.ci_level = float(ci_level)
        self.n_resamples = int(n_resamples)
        self.seed = int(seed)
        # replay 구간이 1년뿐이라 연도 분해는 성립하지 않는다. 국면과 월만 둔다.
        if segment_by not in (None, "regime", "month"):
            raise ConfigError(
                f"알 수 없는 세그먼트 기준: {segment_by} "
                "(replay 구간이 1년이라 'year'는 쓸 수 없다)")
        self.segment_by = segment_by

    def to_dict(self):
        return {
            "variants": self.variants, "horizons": list(self.horizons),
            "outcomes": list(self.outcomes),
            "label_version": self.label_version, "index_id": self.index_id,
            "start": self.start, "end": self.end, "ci_level": self.ci_level,
            "n_resamples": self.n_resamples, "seed": self.seed,
            "segment_by": self.segment_by,
        }


class SignalGateEvaluator:
    """`gold.signals` × `gold.forward_returns` 평가."""

    def __init__(self, store, data_revision: int = 1):
        self.store = store
        self.data_revision = int(data_revision)
        self._signals = None
        self._regime = None
        self.data_insufficient_n = 0

    # ── 입력 ──────────────────────────────────────────────────

    def load_signals(self, index_id=None, start=None, end=None,
                     signal_source=None):
        """신호의 상태·점수·순위를 올린다. 평가 가능한 상태만 남긴다.

        `signal_source`가 필요한 이유는 `gold.signals`에 증거 계층이 다른
        신호가 함께 들어 있기 때문이다 — REPLAY는 분봉 252거래일(CVD 포함),
        DAILY_PANEL은 일봉 4,115거래일(CVD 없음). 섞은 채로 평균 내면
        무엇을 잰 것인지 알 수 없다.
        """
        table = self.store.read_arrow(
            "gold", "signals",
            columns=["signal_id", "security_id", "trading_date", "status",
                     "total_score", "rank", "rank_population", "index_id",
                     "signal_source"],
            dedupe_pk=SIGNAL_PK)
        if table.num_rows == 0:
            raise NoInputDataError("신호가 없다 — `replay run` 먼저")

        listed = table.to_pylist()
        assert_single_signal_source(listed, signal_source,
                                    "signal-gate evaluate")

        keep, dropped = {}, 0
        for row in listed:
            if signal_source and row["signal_source"] != signal_source:
                continue
            if index_id and row["index_id"] != index_id:
                continue
            day = row["trading_date"]
            if start is not None and day < start:
                continue
            if end is not None and day > end:
                continue
            if row["status"] not in _EVALUABLE_STATUS:
                dropped += 1
                continue
            keep[row["signal_id"]] = row
        if not keep:
            raise NoInputDataError("평가 가능한 신호가 없다")
        self.data_insufficient_n = dropped
        self._signals = keep
        return keep

    def _load_labels(self, horizon, outcomes, spec):
        """성숙한 신호 label을 배열로. 신호 속성과 같은 순서로 맞춰 둔다."""
        import pyarrow.dataset as ds

        signals = self._signals or self.load_signals(
            spec.index_id, spec.start, spec.end, spec.signal_source)
        columns = (["signal_id", "security_id", "signal_date",
                    "stock_return_net", "stock_return_gross",
                    "excess_return_net"] + list(outcomes))
        expr = ((ds.field("horizon_td") == horizon)
                & (ds.field("entry_rule") == "NEXT_OPEN")
                & (ds.field("label_version") == spec.label_version)
                & (ds.field("label_status") == LabelStatus.COMPLETE))
        table = self.store.read_arrow(
            "gold", "forward_returns",
            columns=list(dict.fromkeys(columns)), filters=expr,
            dedupe_pk=SIGNAL_FORWARD_RETURN_PK)
        if table.num_rows == 0:
            raise NoInputDataError(
                f"horizon={horizon} 신호 label이 없다 — "
                "`signal-label build` 먼저")

        rows = [r for r in table.to_pylist() if r["signal_id"] in signals]
        if len(rows) < 2:
            raise NoInputDataError(
                f"horizon={horizon}: 평가 가능한 신호와 맞는 label이 없다")
        rows.sort(key=lambda r: (r["signal_date"], r["security_id"]))

        meta = [signals[r["signal_id"]] for r in rows]
        data = {
            "security_id": np.asarray([r["security_id"] for r in rows]),
            "trading_date": np.asarray([r["signal_date"] for r in rows]),
            "net": np.asarray([_f(r["stock_return_net"]) for r in rows]),
            "gross": np.asarray([_f(r["stock_return_gross"]) for r in rows]),
            "excess_net": np.asarray(
                [_f(r["excess_return_net"]) for r in rows]),
            "status": np.asarray([m["status"] for m in meta]),
            "score": np.asarray([_f(m["total_score"]) for m in meta]),
            "rank": np.asarray(
                [np.nan if m["rank"] is None else float(m["rank"])
                 for m in meta]),
        }
        for outcome in outcomes:
            data[outcome] = np.asarray(
                [-1 if r[outcome] is None else (1 if r[outcome] else 0)
                 for r in rows], dtype=np.int8)
        data["score_q"] = _within_day_quintiles(data["score"],
                                                data["trading_date"])
        return data

    # ── 평가 ──────────────────────────────────────────────────

    def evaluate(self, spec: SignalGateSpec, run_id: str = ""):
        self.load_signals(spec.index_id, spec.start, spec.end)
        results = []
        for horizon in spec.horizons:
            data = self._load_labels(horizon, spec.outcomes, spec)
            segments = self._segment_labels(spec, data["trading_date"])
            if segments is None:
                results += self._evaluate_slice(horizon, data, spec, run_id,
                                                None)
                continue
            for value in segment_order(segments):
                sub = segments == value
                if int(sub.sum()) < 2:
                    continue
                sub_data = {k: v[sub] for k, v in data.items()}
                try:
                    results += self._evaluate_slice(
                        horizon, sub_data, spec, run_id,
                        f"{spec.segment_by}={value}")
                except StatisticalValidationError as exc:
                    results.append({"variant": None, "horizon_td": horizon,
                                    "segment": f"{spec.segment_by}={value}",
                                    "status": QualityStatus.PARTIAL,
                                    "status_reason": str(exc)})
        # 가족을 셋으로 나눠 각각 보정한다. 확률 차이·수익 차이·순위상관은
        # 다른 질문이고, 한 덩어리로 묶으면 조합 수가 많은 쪽이 나머지의
        # 문턱을 좌우한다.
        #
        # 수익률 차이는 outcome에 따라 달라지지 않는다(수익률은 up이든
        # up_gross든 같은 값이다). 그래서 결과별로 복제된 행을 검정 하나로
        # 센다 — 세지 않으면 검정 수가 실제의 두 배로 보고된다.
        key = ("variant", "horizon_td", "segment")
        apply_fdr(results, value_key="delta_return_net", dedupe_by=key)
        apply_fdr(results, value_key="delta_vs_all", family="delta_vs_all")
        apply_fdr(results, value_key="rank_ic", family="rank_ic")
        return results

    def _evaluate_slice(self, horizon, data, spec, run_id, segment):
        total = data["trading_date"].size
        rows, baseline = [], None
        for variant in spec.variants:
            mask = _variant_mask(variant, data)
            for outcome in spec.outcomes:
                row = self._one(variant, horizon, outcome, mask, data, spec,
                                run_id, total, segment)
                if variant == "ALL":
                    baseline = row
                elif baseline is not None:
                    row.update(_paired_delta(outcome, horizon, mask,
                                             data, spec))
                rows.append(row)
        rows.append(self._monotonicity(horizon, data, spec, run_id, segment))
        return rows

    def _segment_labels(self, spec, days):
        if not spec.segment_by:
            return None
        if spec.segment_by == "month":
            return np.asarray([f"{d:%Y-%m}" for d in days])
        regime = self._regime_map(spec)
        return np.asarray([regime.get(d, "UNKNOWN") for d in days])

    def _regime_map(self, spec):
        """거래일 → BULL/BEAR. 지수 종가가 200일 이동평균 위인가.

        이동평균은 그날까지의 값만 쓴다. Stage 2와 같은 정의다.
        """
        if self._regime is not None:
            return self._regime
        table = self.store.read_arrow(
            "silver", "benchmark_bars",
            columns=["benchmark_id", "trading_date", "close"],
            dedupe_pk=("benchmark_id", "trading_date"))
        wanted = f"KRX:{spec.index_id}"
        pairs = [(d, float(c)) for b, d, c in zip(
            table.column("benchmark_id").to_pylist(),
            table.column("trading_date").to_pylist(),
            table.column("close").to_pylist()) if b == wanted and c is not None]
        if not pairs:
            raise NoInputDataError(
                f"{wanted} 벤치마크 일봉이 없다 — 국면을 정의할 수 없다")
        pairs.sort()
        values = np.asarray([c for _, c in pairs])
        running = np.cumsum(values)
        window, out = 200, {}
        for i, (day, _) in enumerate(pairs):
            if i + 1 < window:
                out[day] = "UNKNOWN"
                continue
            total = running[i] - (running[i - window] if i >= window else 0.0)
            out[day] = "BULL" if values[i] >= total / window else "BEAR"
        self._regime = out
        return out

    def _one(self, variant, horizon, outcome, mask, data, spec, run_id, total,
             segment=None):
        selected = int(mask.sum())
        if selected < 2:
            raise StatisticalValidationError(
                f"{variant} h={horizon}: 선택된 신호가 2건 미만이다")

        churn = turnover(data["security_id"][mask], data["trading_date"][mask])
        valid = data[outcome][mask] >= 0
        y = data[outcome][mask].astype(np.float64)[valid]
        dates = data["trading_date"][mask][valid]
        if y.size < 2:
            raise StatisticalValidationError(
                f"{variant} h={horizon} {outcome}: 유효 표본이 없다")

        robust = cluster_robust_mean(y, dates, level=spec.ci_level)
        naive = naive_mean(y, level=spec.ci_level)
        block = block_length_for(horizon)
        try:
            boot = block_bootstrap_mean(y, dates, block_length=block,
                                        n_resamples=spec.n_resamples,
                                        seed=spec.seed, level=spec.ci_level)
        except StatisticalValidationError:
            boot = None
        stats = icc_and_design_effect(y, dates)
        # 가장 넓은 구간을 대표로 쓴다. 군집 CI가 더 좁아지는 이득은 취하지
        # 않는다(기준선·Stage 2와 같은 규칙).
        method, chosen = max(
            [("CLUSTER_ROBUST_CR1", robust), ("NAIVE_IID_FLOOR", naive)]
            + ([("MOVING_BLOCK_BOOTSTRAP", boot)] if boot else []),
            key=lambda item: item[1].ci_high - item[1].ci_low)

        spec_key = {
            "kind": "signal_gate", "variant": variant, "horizon_td": horizon,
            "outcome": outcome, "index_id": spec.index_id,
            "label_version": spec.label_version, "segment": segment,
            "start": spec.start, "end": spec.end, "ci_level": spec.ci_level,
            "seed": spec.seed, "n_resamples": spec.n_resamples,
            "block_length": block,
        }
        return {
            "result_id": "sg_" + sha256_of(spec_key)[len("sha256:"):][:32],
            "variant": variant,
            "preregistered": variant in PREREGISTERED,
            "horizon_td": horizon,
            "outcome": outcome,
            "index_id": spec.index_id,
            "segment": segment,
            # 분봉 CVD가 들어간 신호다. Stage 2의 일봉 전용 결과와 같은 표에
            # 합치지 않는다(아키텍처 §6).
            "evidence_tier": EvidenceTier.FULL_SIGNAL_SHORT,

            "raw_n": int(y.size),
            "trading_date_n": int(np.unique(dates).size),
            "security_n": int(np.unique(data["security_id"][mask]).size),
            "positive_n": int(y.sum()),
            "probability": float(y.mean()),
            "coverage": round(selected / total, 6),
            "turnover": (round(churn, 6) if churn is not None else None),
            "evaluable_n": int(total),

            "mean_return_net": nanmean(data["net"][mask]),
            "mean_return_gross": nanmean(data["gross"][mask]),
            "median_return_net": nanmedian(data["net"][mask]),
            "mean_excess_net": nanmean(data["excess_net"][mask]),

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

            "label_version": spec.label_version,
            "data_insufficient_n": self.data_insufficient_n,
            "run_id": run_id,
            "schema_version": SCHEMA_VERSION,
            "data_revision": self.data_revision,
            "status": QualityStatus.SUCCESS,
        }

    def _monotonicity(self, horizon, data, spec, run_id, segment):
        """날짜별 (점수, 순수익) 순위상관의 평균 — 점수가 순서를 지키는가.

        하루가 관측 하나다. 252거래일이면 표본은 252개이고 날짜 간 중복이
        없으므로, 군집 문제 없이 평균과 구간을 낼 수 있다.
        """
        ic, days = _daily_rank_ic(data["score"], data["net"],
                                  data["trading_date"])
        row = {
            "result_id": "sg_" + sha256_of(
                {"kind": "signal_ic", "horizon_td": horizon,
                 "index_id": spec.index_id, "segment": segment,
                 "label_version": spec.label_version, "start": spec.start,
                 "end": spec.end})[len("sha256:"):][:32],
            "variant": "SCORE_IC",
            "preregistered": True,
            "horizon_td": horizon,
            "outcome": "rank_ic",
            "index_id": spec.index_id,
            "segment": segment,
            "evidence_tier": EvidenceTier.FULL_SIGNAL_SHORT,
            "label_version": spec.label_version,
            "run_id": run_id,
            "schema_version": SCHEMA_VERSION,
            "data_revision": self.data_revision,
        }
        if ic.size < 2:
            row.update({"status": QualityStatus.PARTIAL,
                        "status_reason": "순위상관을 낼 수 있는 날이 2일 미만"})
            return row
        # 날짜별 IC도 보유기간만큼 겹친다. h=20이면 232일치 IC 안에 겹치지
        # 않는 구간이 12개뿐이다. 짝지음 차이와 같은 규칙으로 더 넓은 구간을
        # 쓴다.
        candidates = [("CLUSTER_ROBUST_CR1",
                       cluster_robust_mean(ic, days, level=spec.ci_level))]
        unresolved = False
        try:
            candidates.append(("MOVING_BLOCK_BOOTSTRAP", block_bootstrap_mean(
                ic, days, block_length=block_length_for(horizon),
                n_resamples=spec.n_resamples, seed=spec.seed,
                level=spec.ci_level)))
        except StatisticalValidationError:
            unresolved = horizon > 1
        method, est = max(candidates,
                          key=lambda item: item[1].ci_high - item[1].ci_low)
        row.update({
            "status": QualityStatus.SUCCESS,
            "ci_method": method,
            "raw_n": int(ic.size),
            "trading_date_n": int(ic.size),
            "rank_ic": float(est.value),
            "rank_ic_ci_low": est.ci_low,
            "rank_ic_ci_high": est.ci_high,
            "rank_ic_positive_days": int((ic > 0).sum()),
            "rank_ic_overlap_unresolved": unresolved,
            "ci_level": spec.ci_level,
            "cluster_key": "signal_date",
        })
        return row


# ─────────────────────────────────────────────────────────────
# 선택 규칙과 통계
# ─────────────────────────────────────────────────────────────

def _variant_mask(variant, data):
    try:
        kind, arg = SIGNAL_VARIANTS[variant]
    except KeyError:
        raise ConfigError(
            f"알 수 없는 조합: {variant} (가능: {sorted(SIGNAL_VARIANTS)})")
    if kind == "all":
        return np.ones(data["trading_date"].size, dtype=bool)
    if kind == "status":
        return data["status"] == arg
    if kind == "score_q":
        return data["score_q"] == arg
    if kind == "rank_top":
        # rank가 없는 신호는 상위 N에 들 수 없다. nan 비교는 False다.
        return np.nan_to_num(data["rank"], nan=np.inf) <= arg
    raise ConfigError(f"알 수 없는 선택 종류: {kind}")


def _within_day_quintiles(score, days, n_buckets: int = 5):
    """그날 안에서의 점수 분위(1..5, 5가 최상위). 낼 수 없으면 0.

    절대 점수로 자르면 구간이 날마다 달라진다 — 점수 분포 자체가 그날 시장에
    따라 움직이기 때문이다. 그날 안에서의 순위로 자르면 그 문제가 없다.
    """
    out = np.zeros(score.size, dtype=np.int8)
    by_day = {}
    for i, day in enumerate(days):
        by_day.setdefault(day, []).append(i)
    for indices in by_day.values():
        idx = np.asarray(indices)
        values = score[idx]
        ok = ~np.isnan(values)
        if int(ok.sum()) < n_buckets:
            continue
        ranks = np.argsort(np.argsort(values[ok]))
        edges = np.clip((ranks / max(int(ok.sum()), 1) * n_buckets).astype(int),
                        0, n_buckets - 1)
        out[idx[ok]] = (edges + 1).astype(np.int8)
    return out


def _paired_delta(outcome, horizon, mask, data, spec):
    """같은 날짜 안에서 (선택) − (그날 전체)의 차이.

    h일 보유면 이웃한 날짜의 신호가 **같은 가격 구간을 공유한다.** h=20이면
    오늘 신호와 내일 신호의 보유기간이 19일 겹친다. 날짜별 차이를 독립 관측으로
    보면 그 겹침만큼 신뢰구간이 좁아진다 — 232거래일이 있어도 겹치지 않는
    20일 구간은 12개뿐이다.

    그래서 날짜 차이 계열에 블록 부트스트랩(블록 길이 >= horizon)을 함께 돌리고
    **더 넓은 구간을 쓴다.** 좁아지는 이득은 취하지 않는다.
    """
    dates = data["trading_date"]
    base = np.ones(dates.size, dtype=bool)

    values = data[outcome]
    valid = values >= 0
    out = _delta_for(values.astype(np.float64), dates, mask & valid,
                     base & valid, spec, horizon,
                     ("delta_vs_all", "delta_ci_low", "delta_ci_high",
                      "delta_dates_n", "delta_ci_method",
                      "delta_overlap_unresolved"))
    returns = data["net"]
    finite = np.isfinite(returns)
    out.update(_delta_for(returns, dates, mask & finite, base & finite, spec,
                          horizon,
                          ("delta_return_net", "delta_return_ci_low",
                           "delta_return_ci_high", "delta_return_dates_n",
                           "delta_return_ci_method",
                           "delta_return_overlap_unresolved")))
    return out


def _delta_for(values, dates, sel, base, spec, horizon, keys):
    empty = dict(zip(keys, (None, None, None, 0, None, None)))
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
    paired = all_dates[has_both]

    block = block_length_for(horizon)
    candidates = [("CLUSTER_ROBUST_CR1",
                   cluster_robust_mean(delta, paired, level=spec.ci_level))]
    unresolved = False
    try:
        candidates.append(("MOVING_BLOCK_BOOTSTRAP", block_bootstrap_mean(
            delta, paired, block_length=block, n_resamples=spec.n_resamples,
            seed=spec.seed, level=spec.ci_level)))
    except StatisticalValidationError:
        # 날짜 수가 블록 길이보다 적으면 블록을 만들 수 없다. 이때 남는
        # 군집 구간은 중첩을 전혀 반영하지 않은 값이다 — 좁게 나온다.
        # 조용히 넘어가면 그 구간이 진짜 정밀도처럼 보이므로 표시를 남긴다.
        unresolved = horizon > 1
    method, est = max(candidates,
                      key=lambda item: item[1].ci_high - item[1].ci_low)
    return dict(zip(keys, (float(np.mean(delta)), est.ci_low, est.ci_high,
                           int(paired.size), method, unresolved)))


def _daily_rank_ic(score, returns, days, min_per_day: int = 10):
    """날짜별 Spearman 순위상관. 점수나 수익이 그날 전부 같으면 건너뛴다."""
    by_day = {}
    for i, day in enumerate(days):
        by_day.setdefault(day, []).append(i)
    values, kept = [], []
    for day in sorted(by_day):
        idx = np.asarray(by_day[day])
        s, r = score[idx], returns[idx]
        ok = np.isfinite(s) & np.isfinite(r)
        if int(ok.sum()) < min_per_day:
            continue
        rs, rr = _rankdata(s[ok]), _rankdata(r[ok])
        sd = rs.std() * rr.std()
        if sd == 0:
            continue
        values.append(float(((rs - rs.mean()) * (rr - rr.mean())).mean() / sd))
        kept.append(day)
    return np.asarray(values, dtype=np.float64), np.asarray(kept)


def _rankdata(values):
    """동점은 평균 순위로. 동점을 임의 순서로 두면 상관이 흔들린다."""
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(values.size, dtype=np.float64)
    ranks[order] = np.arange(1, values.size + 1, dtype=np.float64)
    sorted_values = values[order]
    i = 0
    while i < sorted_values.size:
        j = i + 1
        while j < sorted_values.size and sorted_values[j] == sorted_values[i]:
            j += 1
        if j - i > 1:
            ranks[order[i:j]] = ranks[order[i:j]].mean()
        i = j
    return ranks


def _f(value):
    return np.nan if value is None else float(value)

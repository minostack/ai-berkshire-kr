# -*- coding: utf-8 -*-
"""포트폴리오 백테스트 엔진 — 로드맵 Stage 7.

Stage 6까지 본 것은 **신호 단위 평균**이다. "BUY_CANDIDATE의 20일 평균 수익"은
자금이 무한하고 하루에 90종목을 다 살 수 있다는 가정 위의 수치다. 실제로는
슬롯이 유한하고, 신호가 많은 날에는 골라야 하며, 산 종목이 팔릴 때까지 다음
신호를 받지 못한다.

하루의 순서 (이 순서가 `spec_hash`의 일부다)
--------------------------------------------
    1. 진입  — 전 거래일 신호로 고른 후보를 그날 **시가**에 산다
    2. 청산  — 그날이 exit_date인 포지션을 **종가**에 판다
    3. 평가  — 남은 포지션을 그날 종가로 마킹하고 NAV를 기록한다

**진입이 먼저다.** 청산을 먼저 두면 그날 종가에 판 돈으로 그날 시가에 사게
된다 — 현금이 생기기 전에 쓰는 것이므로 look-ahead다. 같은 이유로 오늘 파는
종목이 차지한 슬롯도 내일부터 빈다.

지키는 것
---------
- **체결은 신호와 분리된다.** 진입가는 `signal_date`의 다음 거래일 **시가**이고,
  그 값은 Stage 6 label에서 온다. 신호 시각 이전 체결은 `LookAheadError`다.
- **없는 가격을 만들지 않는다.** label이 `COMPLETE`가 아니면 그 신호는 아예
  체결하지 않고 `REJECTED`로 사유와 함께 남긴다. 직전가로 메우면 정지 종목이
  거래된 것처럼 보인다.
- **원장 합계 = NAV 변화.** 하루 끝마다 현금과 평가액을 다시 더해 맞춘다.
  안 맞으면 hard fail이다.
- **비용은 label과 같은 모델을 쓴다.** 그래서 거래의 `net_return`이 Stage 6
  label의 `stock_return_net`과 **정확히 같다.** 두 계층이 갈릴 수 없다.

소수 주식을 허용한다
--------------------
한국 시장은 소수점 주식을 살 수 없다. 그런데 정수 주식으로 맞추면 종목 가격에
따라 비중이 흔들리고, 그 흔들림이 전략 비교를 오염시킨다(3만원짜리와 80만원짜리의
반올림 오차가 다르다). 연구 단계에서는 비중을 정확히 맞추는 쪽이 낫다고 보고
소수 주식을 허용한다. **이건 근사이며, 실거래 수익률이 아니다.**
"""

from __future__ import annotations

import json
from datetime import datetime, time, timezone

from .. import SCHEMA_VERSION
from ..contracts.backtest_schema import (
    BACKTEST_RUN_COLUMNS, BACKTEST_RUN_PK, PORTFOLIO_NAV_COLUMNS,
    PORTFOLIO_NAV_PK, TRADE_COLUMNS, TRADE_PK,
    assert_execution_after_signal, assert_ledger_reconciles,
    assert_same_day_close_entry,
)
from ..contracts.label_schema import ENTRY_RULES, LABEL_VERSION, SIGNAL_FORWARD_RETURN_PK
from ..contracts.schema import SIGNAL_PK, assert_single_signal_source
from ..domain.errors import ConfigError, NoInputDataError
from ..domain.ids import KST, sha256_of, to_utc
from ..domain.status import LabelStatus, QualityStatus, SignalStatus
from ..labels.costs import CostModel
from ..storage.parquet_store import ParquetStore

__all__ = ["SELECTION_POLICIES", "BacktestSpec", "BacktestEngine",
           "BacktestResult"]

#: 선택 정책. 로드맵 Stage 7이 이름으로 요구한 것들이다.
#:   buy_all    — BUY_CANDIDATE 전부
#:   buy_top    — BUY_CANDIDATE 중 V1.5 순위 상위 N
#:   score_q    — 그날 점수 5분위 중 지정 분위(유니버스 전체 대상)
#:   rank_pct   — 그날 순위 백분위 상위 p% (유니버스 전체 대상)
#:   random     — **대조군**. 유니버스 전체에서 무작위(아래 설명)
SELECTION_POLICIES = {
    "BUY_ALL": ("buy_all", None),
    "BUY_TOP3": ("buy_top", 3),
    "BUY_TOP5": ("buy_top", 5),
    "BUY_TOP7": ("buy_top", 7),
    "BUY_TOP10": ("buy_top", 10),
    "BUY_TOP20": ("buy_top", 20),
    "SCORE_Q5": ("score_q", 5),
    "SCORE_Q1": ("score_q", 1),
    "RANK_PCT10": ("rank_pct", 0.10),
    "RANK_PCT25": ("rank_pct", 0.25),
    "RANDOM": ("random", None),
    "NO_GATE": ("no_gate", None),
    "GATE_FAIL": ("gate_fail", None),
}

#: RANDOM이 필요한 이유.
#:
#: 지수(코스피200)는 **시총가중**이다. 2025-09~2026-09처럼 대형주 몇 종목이
#: 끌어올린 구간에서는 지수가 +150%인데 개별 종목의 20일 상승 비율은 49%다.
#: 동일가중으로 20종목을 고르는 전략을 그런 지수와 비교하면 **종목 선정
#: 능력이 아니라 가중 방식**을 비교하게 된다.
#:
#: RANDOM은 같은 날, 같은 유니버스, 같은 슬롯 수, 같은 비용으로 **무작위로**
#: 고른다. 지표가 고른 것과 아무거나 고른 것의 차이만 남는다.
#: `--seed`로 고정하고, 여러 seed를 돌려 분포를 봐야 한다 — 한 번의 무작위는
#: 그 자체로 표본 하나일 뿐이다.
RANDOM_POLICY_SEED = 20260921

_MARKET_CLOSE = time(15, 30)

_REJECT_LABEL = "LABEL_NOT_COMPLETE"
_REJECT_SLOTS = "NO_FREE_SLOT"
_REJECT_HELD = "ALREADY_HELD"
_REJECT_CASH = "INSUFFICIENT_CASH"
_REJECT_LIQUIDITY = "LIQUIDITY_CAP"
_REJECT_DAILY_CAP = "DAILY_ENTRY_CAP"


class BacktestSpec:
    """한 번의 실험 정의. 이 값들이 `experiment_id`를 만든다."""

    __slots__ = ("selection_policy", "horizon_td", "max_positions",
                 "allow_overlap", "liquidity_cap_pct", "initial_capital",
                 "index_id", "start", "end", "oos_start", "benchmark_id",
                 "label_version", "signal_source", "min_weight_pct",
                 "max_entries_per_day", "random_seed", "entry_rule",
                 "confirmed_close_entry")

    def __init__(self, selection_policy="BUY_ALL", horizon_td=20,
                 max_positions=20, allow_overlap=False,
                 liquidity_cap_pct=0.01, initial_capital=100_000_000.0,
                 index_id="KOSPI200", start=None, end=None, oos_start=None,
                 benchmark_id="KRX:KOSPI200", label_version=LABEL_VERSION,
                 signal_source=None, min_weight_pct=0.10,
                 max_entries_per_day="AUTO",
                 random_seed=RANDOM_POLICY_SEED,
                 entry_rule="NEXT_OPEN", confirmed_close_entry=False):
        if selection_policy not in SELECTION_POLICIES:
            raise ConfigError(
                f"알 수 없는 선택 정책: {selection_policy} "
                f"(가능: {sorted(SELECTION_POLICIES)})")
        if max_positions < 1:
            raise ConfigError("max_positions는 1 이상이어야 한다")
        if initial_capital <= 0:
            raise ConfigError("초기 자금은 0보다 커야 한다")
        if oos_start is not None and start is not None and oos_start <= start:
            raise ConfigError("oos_start는 start보다 뒤여야 한다")
        self.selection_policy = selection_policy
        self.random_seed = int(random_seed)
        if entry_rule not in ENTRY_RULES:
            raise ConfigError(f"알 수 없는 entry_rule: {entry_rule}")
        self.entry_rule = entry_rule
        # 신호(15:35)보다 앞선 같은 날 종가(15:30)에 체결됐다고 **가정**한다.
        # 실행 불가능한 가정이므로 SAME_CLOSE에서만 열고, 지문에도 남긴다.
        self.confirmed_close_entry = bool(confirmed_close_entry)
        if self.confirmed_close_entry and entry_rule != "SAME_CLOSE":
            raise ConfigError(
                "confirmed_close_entry는 entry_rule=SAME_CLOSE에서만 쓴다 "
                f"(받은 값: {entry_rule})")
        self.horizon_td = int(horizon_td)
        self.max_positions = int(max_positions)
        self.allow_overlap = bool(allow_overlap)
        self.liquidity_cap_pct = (None if liquidity_cap_pct is None
                                  else float(liquidity_cap_pct))
        self.initial_capital = float(initial_capital)
        self.index_id = index_id
        self.start = start
        self.end = end
        self.oos_start = oos_start
        self.benchmark_id = benchmark_id
        self.label_version = label_version
        self.signal_source = signal_source
        # 유동성 상한에 걸려 목표의 이 비율도 못 채우면 아예 안 산다. 껍데기만
        # 남은 포지션이 슬롯을 차지하면 전략이 실제보다 분산돼 보인다.
        self.min_weight_pct = float(min_weight_pct)
        # 하루에 몇 자리까지 새로 채우는가.
        #
        # 상한이 없으면 첫날 슬롯을 전부 채우고, h일 뒤 전부 같이 팔고, 또
        # 전부 같이 산다. 그건 분산된 포트폴리오가 아니라 **h일마다 한 번
        # 베팅하는 단일 코호트**다. 진입일이 몰리면 그날의 시장이 전체 성과를
        # 좌우하고, 최대낙폭도 그 한 번의 타이밍에 달린다.
        #
        # 정상 상태로 굴리면 매일 N/h 자리가 비고 그만큼 새로 채운다. 그래서
        # 기본값을 그 수로 둔다. `None`이면 상한 없음(=단일 코호트).
        if max_entries_per_day == "AUTO":
            max_entries_per_day = max(
                1, round(self.max_positions / max(self.horizon_td, 1)))
        self.max_entries_per_day = (None if max_entries_per_day is None
                                    else int(max_entries_per_day))
        if (self.max_entries_per_day is not None
                and self.max_entries_per_day < 1):
            raise ConfigError("max_entries_per_day는 1 이상이거나 None이다")

    def to_dict(self):
        payload = {
            "selection_policy": self.selection_policy,
            "horizon_td": self.horizon_td,
            "max_positions": self.max_positions,
            "allow_overlap": self.allow_overlap,
            "liquidity_cap_pct": self.liquidity_cap_pct,
            "initial_capital": self.initial_capital,
            "index_id": self.index_id,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "oos_start": (self.oos_start.isoformat()
                          if self.oos_start else None),
            "benchmark_id": self.benchmark_id,
            "label_version": self.label_version,
            "signal_source": self.signal_source,
            "min_weight_pct": self.min_weight_pct,
            "max_entries_per_day": self.max_entries_per_day,
            "entry_rule": self.entry_rule,
            "day_order": "ENTRY_THEN_EXIT_THEN_MARK",
        }
        # seed는 RANDOM일 때만 지문에 넣는다. 다른 정책에서는 결과에 영향이
        # 없는데 넣으면 기존 실험 ID가 전부 바뀌어 저장된 결과와 끊긴다.
        if self.selection_policy == "RANDOM":
            payload["random_seed"] = self.random_seed
        # 같은 이유로 **켰을 때만** 넣는다. 항상 넣으면 기존 실험 ID가 전부
        # 바뀌어 저장된 결과와 끊긴다. 켜진 실험은 ID가 달라지므로 정상
        # 실험과 섞일 수 없다.
        if self.confirmed_close_entry:
            payload["confirmed_close_entry"] = True
        return payload

    def experiment_id(self):
        return "bt_" + sha256_of(self.to_dict())[len("sha256:"):][:32]


class _Position:
    __slots__ = ("trade", "quantity", "entry_notional", "exit_date",
                 "security_id")

    def __init__(self, trade, quantity, entry_notional, exit_date):
        self.trade = trade
        self.quantity = float(quantity)
        self.entry_notional = float(entry_notional)
        self.exit_date = exit_date
        self.security_id = trade["security_id"]


class BacktestResult:
    """한 번의 실행 결과."""

    __slots__ = ("spec", "experiment_id", "run_row", "trades", "nav",
                 "rejected", "ledger_gap", "writes")

    def __init__(self, spec, experiment_id, run_row, trades, nav, rejected,
                 ledger_gap, writes=()):
        self.spec = spec
        self.experiment_id = experiment_id
        self.run_row = run_row
        self.trades = trades
        self.nav = nav
        self.rejected = rejected
        self.ledger_gap = ledger_gap
        self.writes = list(writes)

    def closed(self):
        return [t for t in self.trades if t["trade_status"] == "CLOSED"]

    def summary(self):
        nav = self.nav
        last = nav[-1] if nav else None
        closed = self.closed()
        wins = sum(1 for t in closed if (t["net_return"] or 0) > 0)
        return {
            "experiment_id": self.experiment_id,
            "selection_policy": self.spec.selection_policy,
            "horizon_td": self.spec.horizon_td,
            "max_positions": self.spec.max_positions,
            "trading_days": len(nav),
            "trades": len(closed),
            "open_at_end": sum(1 for t in self.trades
                               if t["trade_status"] == "OPEN"),
            "rejected": len(self.rejected),
            "reject_reasons": _count(self.rejected, "reject_reason"),
            "initial_capital": self.spec.initial_capital,
            "final_nav": (last["net_nav"] if last else None),
            "total_return": ((last["net_nav"] / self.spec.initial_capital - 1)
                             if last else None),
            "benchmark_return": (
                (last["benchmark_nav"] / self.spec.initial_capital - 1)
                if last and last["benchmark_nav"] else None),
            "max_drawdown": (min((r["drawdown"] for r in nav), default=None)),
            "mean_exposure": (sum(r["exposure"] for r in nav) / len(nav)
                              if nav else None),
            "mean_open_positions": (
                sum(r["open_positions"] for r in nav) / len(nav)
                if nav else None),
            "hit_rate": (wins / len(closed) if closed else None),
            "ledger_gap": self.ledger_gap,
        }


class BacktestEngine:
    """`gold.signals` × `gold.forward_returns` → 포트폴리오 시뮬레이션."""

    LAYER = "gold"

    def __init__(self, store: ParquetStore = None, cost_model: CostModel = None,
                 data_revision: int = 1):
        self.store = store or ParquetStore()
        self.cost_model = cost_model or CostModel.from_config()
        self.data_revision = int(data_revision)
        # 정책을 여러 개 비교할 때 같은 표를 반복해서 읽지 않는다.
        self._cache = {}

    # ── 입력 ──────────────────────────────────────────────────

    def load_candidates(self, spec: BacktestSpec):
        """신호 × label을 거래일별 후보 목록으로.

        label이 `COMPLETE`가 아닌 신호도 **버리지 않고** 남긴다. 그래야 "살 수
        있었는데 안 산 것"과 "살 수 없었던 것"을 구별할 수 있다.
        """
        import pyarrow.dataset as ds

        signals = self.store.read_arrow(
            self.LAYER, "signals",
            columns=["signal_id", "security_id", "trading_date",
                     "signal_at_utc", "status", "total_score", "rank",
                     "rank_population", "index_id", "signal_source"],
            dedupe_pk=SIGNAL_PK)
        if signals.num_rows == 0:
            raise NoInputDataError("신호가 없다 — `replay run` 먼저")

        listed = signals.to_pylist()
        assert_single_signal_source(listed, spec.signal_source, "backtest run")

        by_id = {}
        for row in listed:
            if row["index_id"] != spec.index_id:
                continue
            if spec.signal_source and row["signal_source"] != spec.signal_source:
                continue
            day = row["trading_date"]
            if spec.start is not None and day < spec.start:
                continue
            if spec.end is not None and day > spec.end:
                continue
            by_id[row["signal_id"]] = row
        if not by_id:
            raise NoInputDataError("조건에 맞는 신호가 없다")

        expr = ((ds.field("horizon_td") == spec.horizon_td)
                & (ds.field("entry_rule") == spec.entry_rule)
                & (ds.field("label_version") == spec.label_version))
        labels = self.store.read_arrow(
            self.LAYER, "forward_returns",
            columns=["signal_id", "entry_date", "exit_date", "entry_at_utc",
                     "exit_at_utc", "entry_price", "exit_price",
                     "stock_return_gross", "stock_return_net",
                     "benchmark_return", "excess_return_net", "commission",
                     "tax", "slippage", "label_status", "status_reason"],
            filters=expr, dedupe_pk=SIGNAL_FORWARD_RETURN_PK)
        if labels.num_rows == 0:
            raise NoInputDataError(
                f"horizon={spec.horizon_td} 신호 label이 없다 — "
                "`signal-label build` 먼저")

        by_day = {}
        for label in labels.to_pylist():
            signal = by_id.get(label["signal_id"])
            if signal is None:
                continue
            by_day.setdefault(signal["trading_date"], []).append(
                (signal, label))
        if not by_day:
            raise NoInputDataError("신호와 label이 하나도 맞지 않는다")
        return by_day

    def _load_marks(self, securities, start, end):
        """보유 종목을 매일 종가로 평가하기 위한 표.

        평가를 하지 않으면 NAV 곡선이 청산일에만 계단처럼 움직이고, 최대낙폭이
        실제보다 작게 나온다. 낙폭은 운용 판단에 직접 쓰이는 수치다.
        """
        import pyarrow.compute as pc
        import pyarrow as pa
        from ..bar_standard import read_standard_daily

        # 봉 기준 bars-1.0: 평가가격도 정규장 종가. 하루 한 행.
        table = read_standard_daily(
            self.store,
            columns=["security_id", "trading_date", "close", "quality_status"])
        table = table.filter(
            pc.not_equal(table.column("quality_status"), "FAILED"))
        if securities:
            table = table.filter(pc.is_in(table.column("security_id"),
                                          value_set=pa.array(sorted(securities))))
        marks = {}
        for sid, day, close in zip(table.column("security_id").to_pylist(),
                                   table.column("trading_date").to_pylist(),
                                   table.column("close").to_pylist()):
            if close is None or day < start or day > end:
                continue
            marks[(sid, day)] = float(close)
        return marks

    def _load_benchmark(self, benchmark_id, start, end):
        table = self.store.read_arrow(
            "silver", "benchmark_bars",
            columns=["benchmark_id", "trading_date", "close"],
            dedupe_pk=("benchmark_id", "trading_date"))
        out = {}
        for bid, day, close in zip(table.column("benchmark_id").to_pylist(),
                                   table.column("trading_date").to_pylist(),
                                   table.column("close").to_pylist()):
            if bid != benchmark_id or close is None:
                continue
            if day < start or day > end:
                continue
            out[day] = float(close)
        return out

    def _load_liquidity(self, start, end):
        from ..contracts.feature_schema import DAILY_FEATURE_PK

        table = self.store.read_arrow(
            self.LAYER, "daily_features",
            columns=["security_id", "trading_date", "avg_traded_value_20d"],
            dedupe_pk=DAILY_FEATURE_PK)
        out = {}
        for sid, day, value in zip(
                table.column("security_id").to_pylist(),
                table.column("trading_date").to_pylist(),
                table.column("avg_traded_value_20d").to_pylist()):
            if value is None or day < start or day > end:
                continue
            out[(sid, day)] = float(value)
        return out

    def _cached(self, name, key, build):
        slot = (name, key)
        if slot not in self._cache:
            self._cache[slot] = build()
        return self._cache[slot]

    # ── 선택 ──────────────────────────────────────────────────

    @staticmethod
    def select(spec: BacktestSpec, pairs, liquidity=None):
        """그날 후보를 정책에 따라 골라 **우선순위 순서로** 돌려준다.

        슬롯이 모자라면 앞에서부터 채운다. 순서를 정하지 않으면 어떤 종목이
        슬롯을 얻는지가 읽은 파일 순서에 달린다.

        정렬 기준은 아키텍처 §6이 고정한 것이다 —
        **`total_score` 내림차순, 거래대금 내림차순, `security_id` 오름차순.**
        점수 동점은 드물지 않다(248거래일 중 138일). 하루에 1~5종목만 사므로
        동점 중 누구를 고르느냐가 결과를 바꾼다. 거래대금을 두 번째 키로 두는
        이유는, 같은 점수라면 더 크게 담을 수 있는 쪽이 실제로 살 수 있는
        종목이기 때문이다. 거래대금을 모르면 그 자리는 -inf로 둔다(뒤로 밀린다).
        """
        kind, arg = SELECTION_POLICIES[spec.selection_policy]
        liquidity = liquidity or {}

        def _order(pair):
            signal = pair[0]
            value = liquidity.get(
                (signal["security_id"], signal["trading_date"]))
            return (-(signal["total_score"] or 0.0),
                    -(value if value is not None else float("-inf")),
                    signal["security_id"])

        scored = sorted(pairs, key=_order)

        if kind == "buy_all":
            picked = [p for p in scored
                      if p[0]["status"] == SignalStatus.BUY_CANDIDATE]
        elif kind == "buy_top":
            buys = [p for p in scored
                    if p[0]["status"] == SignalStatus.BUY_CANDIDATE]
            picked = buys[:arg]
        elif kind == "score_q":
            ranked = [p for p in scored if p[0]["total_score"] is not None]
            if len(ranked) < 5:
                return []
            size = len(ranked) / 5.0
            lo = int(round((5 - arg) * size))
            hi = int(round((5 - arg + 1) * size))
            picked = ranked[lo:hi]
        elif kind == "rank_pct":
            ranked = [p for p in scored if p[0]["rank"] is not None]
            cutoff = max(1, int(len(ranked) * arg))
            picked = ranked[:cutoff]
        elif kind == "no_gate":
            # BUY_ALL과 **같은 순서 규칙**으로 고르되 Gate만 보지 않는다.
            # 점수가 없는 일봉 경로에서는 tie-break(거래대금 내림차순)가
            # 주 선택 기준이 되므로, RANDOM과만 비교하면 Gate 효과와
            # 대형주 편향이 섞인다. 이 대조군이 그 둘을 가른다.
            picked = list(scored)
        elif kind == "gate_fail":
            # Gate에 **걸린** 쪽. 지표가 정보를 담고 있다면 BUY_ALL보다
            # 나빠야 한다. 오히려 낫다면 부호가 반대라는 뜻이다.
            picked = [p for p in scored
                      if p[0]["status"] == SignalStatus.WATCH]
        elif kind == "random":
            # 유니버스 전체에서 뽑는다 — BUY 여부를 보지 않는 것이 요점이다.
            # 날짜와 seed로 섞으므로 같은 spec은 언제 돌려도 같은 결과다.
            import random as _random

            pool = list(scored)
            day = pool[0][0]["trading_date"] if pool else None
            rng = _random.Random(f"{spec.random_seed}|{day}")
            rng.shuffle(pool)
            picked = pool
        else:
            raise ConfigError(f"알 수 없는 선택 종류: {kind}")
        return picked

    # ── 실행 ──────────────────────────────────────────────────

    def run(self, spec: BacktestSpec, run_id: str = "", dry_run: bool = False):
        by_day = self.load_candidates(spec)
        signal_days = sorted(by_day)

        # 체결·평가에 필요한 거래일은 신호일보다 뒤까지 간다.
        all_dates = set(signal_days)
        securities = set()
        for pairs in by_day.values():
            for signal, label in pairs:
                securities.add(signal["security_id"])
                for key in ("entry_date", "exit_date"):
                    if label[key] is not None:
                        all_dates.add(label[key])
        days = sorted(all_dates)
        start, end = days[0], days[-1]

        marks = self._cached("marks", (start, end),
                             lambda: self._load_marks(None, start, end))
        benchmark = self._cached(
            ("benchmark", spec.benchmark_id), (start, end),
            lambda: self._load_benchmark(spec.benchmark_id, start, end))
        # 유동성 상한을 끄더라도 tie-break에 거래대금이 필요하다.
        liquidity = self._cached("liquidity", (start, end),
                                 lambda: self._load_liquidity(start, end))

        experiment_id = spec.experiment_id()
        cash = spec.initial_capital
        open_positions = []
        trades, rejected, nav_rows = [], [], []
        peak = spec.initial_capital
        cumulative_cost = 0.0
        bench_units = None
        seq = 0

        # 진입은 신호 다음 거래일이다. 날짜별로 그날 진입할 후보를 모아 둔다.
        entries_on = {}
        for day, pairs in by_day.items():
            for signal, label in pairs:
                entry = label["entry_date"]
                key = entry if entry is not None else day
                entries_on.setdefault(key, []).append((day, signal, label))

        for today in days:
            realized = 0.0
            day_cost = 0.0
            exits = 0
            exit_notional_sum = 0.0

            # 1. 진입 — 시가에 산다.
            candidates = entries_on.get(today, [])
            entered, entry_notional_sum = 0, 0.0
            if candidates:
                grouped = {}
                for signal_day, signal, label in candidates:
                    grouped.setdefault(signal_day, []).append((signal, label))
                nav_now = cash + _market_value(open_positions, marks, today)
                target = nav_now / spec.max_positions
                held = {p.security_id for p in open_positions}

                room = (spec.max_entries_per_day
                        if spec.max_entries_per_day is not None
                        else len(candidates))
                for signal_day in sorted(grouped):
                    # `select`가 우선순위 순으로 준다. 앞에서부터 자리를 준다.
                    picked = self.select(spec, grouped[signal_day],
                                         liquidity)
                    for rank, (signal, label) in enumerate(picked, start=1):
                        seq += 1
                        trade = _new_trade(
                            experiment_id, seq, signal, label, spec,
                            self.cost_model, rank, today, run_id,
                            self.data_revision)
                        reason = _reject_reason(
                            label, signal, spec, held, open_positions,
                            cash, target, liquidity, today, room)
                        if reason is not None:
                            trade["trade_status"] = "REJECTED"
                            trade["reject_reason"] = reason
                            trade["entry_date"] = None
                            trade["entry_at_utc"] = None
                            trade["exit_date"] = None
                            trade["exit_at_utc"] = None
                            rejected.append(trade)
                            continue

                        notional = min(target, cash)
                        cap = _liquidity_cap(spec, liquidity,
                                             signal["security_id"], signal_day)
                        if cap is not None:
                            notional = min(notional, cap)
                        price = float(label["entry_price"])
                        cash -= notional
                        position = _Position(trade, notional / price, notional,
                                             label["exit_date"])
                        trade["quantity"] = position.quantity
                        trade["notional"] = notional
                        trade["weight"] = (notional / nav_now
                                           if nav_now > 0 else None)
                        trade["trade_status"] = "OPEN"
                        open_positions.append(position)
                        trades.append(trade)
                        held.add(signal["security_id"])
                        entered += 1
                        room -= 1
                        entry_notional_sum += notional

            # 2. 청산 — 종가에 판다. 이 대금은 **내일부터** 쓸 수 있다.
            still_open = []
            for position in open_positions:
                if (position.exit_date is not None
                        and position.exit_date <= today):
                    trade = position.trade
                    proceeds = position.entry_notional * (
                        1.0 + float(trade["net_return"]))
                    pnl = proceeds - position.entry_notional
                    cash += proceeds
                    realized += pnl
                    exit_notional_sum += proceeds
                    day_cost += _cost_amount(position.entry_notional, trade)
                    trade["pnl"] = pnl
                    trade["trade_status"] = "CLOSED"
                    trade["exit_reason"] = "HORIZON"
                    exits += 1
                else:
                    still_open.append(position)
            open_positions = still_open

            # 3. 평가.
            market_value = _market_value(open_positions, marks, today)
            net_nav = cash + market_value
            cumulative_cost += day_cost
            peak = max(peak, net_nav)
            if bench_units is None and today in benchmark:
                bench_units = spec.initial_capital / benchmark[today]
            nav_rows.append({
                "experiment_id": experiment_id,
                "trading_date": today,
                "timestamp_utc": to_utc(
                    datetime.combine(today, _MARKET_CLOSE, tzinfo=KST)),
                "cash": cash,
                "market_value": market_value,
                "net_nav": net_nav,
                "gross_nav": net_nav + cumulative_cost,
                "benchmark_nav": (bench_units * benchmark[today]
                                  if bench_units is not None
                                  and today in benchmark else None),
                "exposure": (market_value / net_nav if net_nav > 0 else 0.0),
                "open_positions": len(open_positions),
                "entries": entered,
                "exits": exits,
                # 회전율 = 그날 오간 금액(산 것 + 판 것) / 자산.
                "turnover": ((entry_notional_sum + exit_notional_sum) / net_nav
                             if net_nav > 0 else None),
                "realized_pnl": realized,
                "cost_paid": day_cost,
                "drawdown": (net_nav / peak - 1.0) if peak > 0 else 0.0,
                "sample_scope": _scope(today, spec),
                "run_id": run_id,
                "schema_version": SCHEMA_VERSION,
                "data_revision": self.data_revision,
            })

        for position in open_positions:
            position.trade["trade_status"] = "OPEN"
            position.trade["exit_reason"] = "PERIOD_END"

        all_trades = trades + rejected
        live = [t for t in all_trades if t["trade_status"] != "REJECTED"]
        if spec.confirmed_close_entry:
            assert_same_day_close_entry(live)
        else:
            assert_execution_after_signal(live)
        gap = assert_ledger_reconciles(trades, nav_rows, spec.initial_capital)

        run_row = self._run_row(spec, experiment_id, days, all_trades,
                                rejected, run_id)
        result = BacktestResult(spec, experiment_id, run_row, all_trades,
                                nav_rows, rejected, gap)
        if not dry_run:
            result.writes = self._write(result, run_id)
        return result

    # ── 저장 ──────────────────────────────────────────────────

    def _run_row(self, spec, experiment_id, days, trades, rejected, run_id):
        return {
            "experiment_id": experiment_id,
            "spec_hash": sha256_of(spec.to_dict()),
            "spec_json": json.dumps(spec.to_dict(), ensure_ascii=False,
                                    sort_keys=True),
            "index_id": spec.index_id,
            "signal_source": spec.signal_source,
            "selection_policy": spec.selection_policy,
            "max_positions": spec.max_positions,
            "allow_overlap": spec.allow_overlap,
            "liquidity_cap_pct": spec.liquidity_cap_pct,
            "horizon_td": spec.horizon_td,
            "entry_rule": spec.entry_rule,
            "initial_capital": spec.initial_capital,
            "start_date": days[0],
            "end_date": days[-1],
            "oos_start_date": spec.oos_start,
            "trading_days": len(days),
            "benchmark_id": spec.benchmark_id,
            "cost_model_version": self.cost_model.version,
            "label_version": spec.label_version,
            "input_manifest_hash": None,
            "trades": len([t for t in trades
                           if t["trade_status"] != "REJECTED"]),
            "rejected": len(rejected),
            "status": QualityStatus.SUCCESS,
            "status_reason": None,
            "run_id": run_id,
            "created_at_utc": datetime.now(timezone.utc),
            "schema_version": SCHEMA_VERSION,
            "data_revision": self.data_revision,
        }

    def _write(self, result, run_id):
        paths = []
        for table, rows, columns, pk, partition in (
                ("backtest_runs", [result.run_row], BACKTEST_RUN_COLUMNS,
                 BACKTEST_RUN_PK, None),
                ("trades", result.trades, TRADE_COLUMNS, TRADE_PK,
                 _trade_partition),
                ("portfolio_nav", result.nav, PORTFOLIO_NAV_COLUMNS,
                 PORTFOLIO_NAV_PK, _nav_partition)):
            if not rows:
                continue
            write = self.store.write(
                self.LAYER, table, rows, columns, pk, run_id,
                partition_fn=partition,
                # 한 run_id로 여러 실험을 돌린다. `part_key`가 없으면 같은
                # 파일 이름에 겹쳐 써서 마지막 실험만 남는다. 해시 전체를
                # 쓰면 파일명이 60자를 넘어 비밀정보 탐지기에 걸리므로
                # 앞 10자만 쓴다(한 run 안에서 구별되면 충분하다).
                part_key=_part_key(result.experiment_id),
                extra_manifest={"experiment_id": result.experiment_id,
                                "selection_policy":
                                    result.spec.selection_policy,
                                "horizon_td": result.spec.horizon_td,
                                "ledger_gap": result.ledger_gap})
            paths.append(write.manifest_path)
        return paths


# ─────────────────────────────────────────────────────────────
# 도우미
# ─────────────────────────────────────────────────────────────

def _new_trade(experiment_id, seq, signal, label, spec, cost_model, rank,
               entry_day, run_id, data_revision):
    return {
        "experiment_id": experiment_id,
        "trade_id": f"t{seq:06d}",
        "signal_id": signal["signal_id"],
        "security_id": signal["security_id"],
        "side": "LONG",
        "signal_date": signal["trading_date"],
        "signal_at_utc": signal["signal_at_utc"],
        "entry_date": label["entry_date"],
        "exit_date": label["exit_date"],
        "entry_at_utc": label["entry_at_utc"],
        "exit_at_utc": label["exit_at_utc"],
        "selected_rank": rank,
        "signal_rank": signal["rank"],
        "total_score": signal["total_score"],
        "entry_price": label["entry_price"],
        "exit_price": label["exit_price"],
        "quantity": None,
        "weight": None,
        "notional": None,
        "gross_return": label["stock_return_gross"],
        "net_return": label["stock_return_net"],
        "benchmark_return": label["benchmark_return"],
        "excess_return_net": label["excess_return_net"],
        "pnl": None,
        "commission": label["commission"],
        "tax": label["tax"],
        "slippage": label["slippage"],
        "exit_reason": None,
        "trade_status": "REJECTED",
        "reject_reason": None,
        "overlap_group": f"{signal['trading_date']:%Y%m%d}",
        "sample_scope": _scope(signal["trading_date"], spec),
        "label_version": spec.label_version,
        "cost_model_version": cost_model.version,
        "run_id": run_id,
        "schema_version": SCHEMA_VERSION,
        "data_revision": data_revision,
    }


def _reject_reason(label, signal, spec, held, open_positions, cash, target,
                   liquidity, today, room):
    """살 수 없는 이유. 없으면 None.

    순서가 중요하다. 가격이 없어 못 사는 것과 슬롯이 없어 못 사는 것은 다른
    이야기다 — 앞의 것은 데이터 문제이고 뒤의 것은 전략의 성질이다.
    """
    if label["label_status"] != LabelStatus.COMPLETE:
        return _REJECT_LABEL
    if label["entry_price"] is None or float(label["entry_price"]) <= 0:
        return _REJECT_LABEL
    if not spec.allow_overlap and signal["security_id"] in held:
        return _REJECT_HELD
    if len(open_positions) >= spec.max_positions:
        return _REJECT_SLOTS
    if room <= 0:
        return _REJECT_DAILY_CAP
    # 현금이 목표의 일부밖에 안 남았으면 사지 않는다. 먼지만 한 포지션이
    # 슬롯을 차지하면 전략이 실제보다 분산돼 보인다(유동성 상한과 같은 규칙).
    if cash < target * spec.min_weight_pct:
        return _REJECT_CASH
    cap = _liquidity_cap(spec, liquidity, signal["security_id"],
                         signal["trading_date"])
    if cap is not None and cap < target * spec.min_weight_pct:
        return _REJECT_LIQUIDITY
    return None


def _liquidity_cap(spec, liquidity, security_id, signal_date):
    """한 종목에 넣을 수 있는 최대 금액.

    거래대금은 **신호일 기준**으로 본다. 진입일 거래대금을 쓰면 그날 장이
    끝나야 알 수 있는 값으로 그날 아침에 주문하는 셈이 된다.
    """
    if not spec.liquidity_cap_pct:
        return None
    value = liquidity.get((security_id, signal_date))
    if value is None:
        return None
    return value * spec.liquidity_cap_pct


def _market_value(positions, marks, today):
    total = 0.0
    for position in positions:
        price = marks.get((position.security_id, today))
        if price is None:
            # 그날 종가가 없으면(거래정지 등) 마지막으로 아는 값, 즉 진입
            # 금액으로 둔다. 0으로 두면 정지 순간 자산이 사라진 것처럼 보인다.
            total += position.entry_notional
            continue
        total += position.quantity * price
    return total


def _cost_amount(notional, trade):
    """왕복 비용의 근사 금액. NAV의 gross/net 간극을 메우는 데만 쓴다."""
    rates = sum(float(trade[k] or 0.0)
                for k in ("commission", "tax", "slippage"))
    return notional * rates


def _scope(day, spec):
    if spec.oos_start is None:
        return "FULL"
    return "OUT_OF_SAMPLE" if day >= spec.oos_start else "IN_SAMPLE"


def _count(rows, key):
    out = {}
    for row in rows:
        out[row[key]] = out.get(row[key], 0) + 1
    return out


def _part_key(experiment_id):
    """파일명에 붙일 짧은 실험 식별자. `bt_` 접두를 떼고 앞 10자."""
    return experiment_id[3:13]


def _trade_partition(row):
    day = row["entry_date"] or row["signal_date"]
    return [("experiment_id_prefix", row["experiment_id"][:10]),
            ("entry_year", day.year)]


def _nav_partition(row):
    return [("experiment_id_prefix", row["experiment_id"][:10]),
            ("year", row["trading_date"].year)]

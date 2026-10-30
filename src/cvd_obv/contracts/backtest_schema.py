# -*- coding: utf-8 -*-
"""Backtest 열 계약 — 스키마 §8, 로드맵 Stage 7.

신호 단위 평균과 포트폴리오는 다른 이야기다. "BUY_CANDIDATE의 20일 평균
수익"은 자금이 무한하고 하루에 90종목을 다 살 수 있다고 가정한 수치다. 실제로는
슬롯이 유한하고, 신호가 많은 날에는 골라야 하며, 고른 종목이 팔릴 때까지 다음
신호를 받을 수 없다.

여기서 지키는 것
----------------
- **체결은 신호와 분리된다.** 신호는 15:35에 나고 진입은 다음 거래일 시가다.
  `entry_at > signal_at`을 계약이 강제한다(불변식 5).
- **원장 합계 = NAV 변화.** 거래 손익의 합이 자산 변화와 맞지 않으면 어딘가에서
  돈이 생기거나 사라진 것이다. 검증기가 이걸 hard fail로 잡는다.
- **선택 정책과 체결 정책을 나눠 기록한다.** "무엇을 고르는가"와 "어떻게
  사는가"가 섞이면 결과 차이가 어디서 왔는지 알 수 없다.
"""

from __future__ import annotations

from ..domain.errors import DataQualityError, LookAheadError
from .schema import Column

__all__ = [
    "BACKTEST_RUN_COLUMNS", "BACKTEST_RUN_PK",
    "TRADE_COLUMNS", "TRADE_PK",
    "PORTFOLIO_NAV_COLUMNS", "PORTFOLIO_NAV_PK",
    "BACKTEST_METRIC_COLUMNS", "BACKTEST_METRIC_PK",
    "TRADE_STATUSES", "EXIT_REASONS", "SIDES", "METRIC_SCOPES",
    "assert_ledger_reconciles", "assert_execution_after_signal",
    "assert_same_day_close_entry",
]

_PRICE = "decimal"

SIDES = ("LONG",)
TRADE_STATUSES = ("CLOSED", "OPEN", "REJECTED")
EXIT_REASONS = ("HORIZON", "PERIOD_END", "PRICE_UNAVAILABLE")
#: 지표를 어느 구간에서 쟀는가. walk-forward를 실제로 구분해 보고하기 위한 축.
METRIC_SCOPES = ("FULL", "IN_SAMPLE", "OUT_OF_SAMPLE", "SEGMENT")


# ─────────────────────────────────────────────────────────────
# gold.backtest_runs
# ─────────────────────────────────────────────────────────────

BACKTEST_RUN_PK = ("experiment_id",)

BACKTEST_RUN_COLUMNS = [
    Column("experiment_id", "string", nullable=False, note="spec hash"),
    Column("spec_hash", "string", nullable=False),
    Column("spec_json", "string", nullable=False),

    Column("index_id", "string", nullable=False),
    Column("signal_source", "string"),
    Column("selection_policy", "string", nullable=False),
    Column("max_positions", "int", nullable=False),
    Column("allow_overlap", "bool", nullable=False,
           note="같은 종목을 보유 중에 다시 살 수 있는가"),
    Column("liquidity_cap_pct", "float",
           note="한 종목 진입 금액의 상한 = 20일 평균 거래대금 x 이 비율"),
    Column("horizon_td", "int", nullable=False),
    Column("entry_rule", "string", nullable=False),
    Column("initial_capital", "float", nullable=False),

    Column("start_date", "date", nullable=False),
    Column("end_date", "date", nullable=False),
    Column("oos_start_date", "date",
           note="이 날부터 out-of-sample. 없으면 구간 전체가 하나다"),
    Column("trading_days", "int", nullable=False),

    Column("benchmark_id", "string"),
    Column("cost_model_version", "string", nullable=False),
    Column("label_version", "string", nullable=False),
    Column("input_manifest_hash", "string"),

    Column("trades", "int", nullable=False),
    Column("rejected", "int", nullable=False),
    Column("status", "string", nullable=False,
           enum=("SUCCESS", "PARTIAL", "FAILED")),
    Column("status_reason", "string"),

    Column("run_id", "string", nullable=False),
    Column("created_at_utc", "timestamp", nullable=False),
    Column("schema_version", "string", nullable=False),
    Column("data_revision", "int", nullable=False),
]


# ─────────────────────────────────────────────────────────────
# gold.trades
# ─────────────────────────────────────────────────────────────

TRADE_PK = ("experiment_id", "trade_id")

TRADE_COLUMNS = [
    Column("experiment_id", "string", nullable=False),
    Column("trade_id", "string", nullable=False),
    Column("signal_id", "string", nullable=False),
    Column("security_id", "string", nullable=False),
    Column("side", "string", nullable=False, enum=SIDES),

    Column("signal_date", "date", nullable=False),
    Column("signal_at_utc", "timestamp", nullable=False),
    Column("entry_date", "date"),
    Column("exit_date", "date"),
    Column("entry_at_utc", "timestamp"),
    Column("exit_at_utc", "timestamp"),

    Column("selected_rank", "int", note="그날 후보 중 몇 번째로 골랐는가"),
    Column("signal_rank", "int", note="V1.5가 매긴 유니버스 내 순위"),
    Column("total_score", "float"),

    Column("entry_price", _PRICE),
    Column("exit_price", _PRICE),
    Column("quantity", "float", note="소수 주식 허용 — 연구용 근사다"),
    Column("weight", "float", note="진입 시점 NAV 대비 비중"),
    Column("notional", "float", note="진입 체결 금액(비용 포함)"),

    Column("gross_return", "float"),
    Column("net_return", "float"),
    Column("benchmark_return", "float"),
    Column("excess_return_net", "float"),
    Column("pnl", "float", note="원화 손익(비용 후). 원장 합계의 단위다"),

    Column("commission", "float"),
    Column("tax", "float"),
    Column("slippage", "float"),

    Column("exit_reason", "string", enum=EXIT_REASONS),
    Column("trade_status", "string", nullable=False, enum=TRADE_STATUSES),
    Column("reject_reason", "string"),
    Column("overlap_group", "string",
           note="같은 날 진입한 묶음. 중복 포지션 정책을 추적한다"),
    Column("sample_scope", "string", enum=METRIC_SCOPES),

    Column("label_version", "string", nullable=False),
    Column("cost_model_version", "string", nullable=False),
    Column("run_id", "string", nullable=False),
    Column("schema_version", "string", nullable=False),
    Column("data_revision", "int", nullable=False),
]


# ─────────────────────────────────────────────────────────────
# gold.portfolio_nav
# ─────────────────────────────────────────────────────────────

PORTFOLIO_NAV_PK = ("experiment_id", "trading_date")

PORTFOLIO_NAV_COLUMNS = [
    Column("experiment_id", "string", nullable=False),
    Column("trading_date", "date", nullable=False),
    Column("timestamp_utc", "timestamp", nullable=False),

    Column("cash", "float", nullable=False),
    Column("market_value", "float", nullable=False,
           note="보유 종목을 그날 종가로 평가한 값"),
    Column("net_nav", "float", nullable=False, note="cash + market_value"),
    Column("gross_nav", "float", nullable=False,
           note="비용을 빼기 전 기준. 비용 총액을 되돌린 값"),
    Column("benchmark_nav", "float", note="같은 자금으로 지수를 사서 든 경우"),

    Column("exposure", "float", nullable=False, note="market_value / net_nav"),
    Column("open_positions", "int", nullable=False),
    Column("entries", "int", nullable=False),
    Column("exits", "int", nullable=False),
    Column("turnover", "float", note="(진입금액+청산금액) / net_nav"),
    Column("realized_pnl", "float", nullable=False, note="그날 청산 손익 합"),
    Column("cost_paid", "float", nullable=False),
    Column("drawdown", "float", nullable=False, note="최고점 대비 하락률(<=0)"),

    Column("sample_scope", "string", enum=METRIC_SCOPES),
    Column("run_id", "string", nullable=False),
    Column("schema_version", "string", nullable=False),
    Column("data_revision", "int", nullable=False),
]


# ─────────────────────────────────────────────────────────────
# gold.backtest_metrics
# ─────────────────────────────────────────────────────────────

BACKTEST_METRIC_PK = ("experiment_id", "metric_scope", "horizon_td",
                      "segment_key", "metric_name")

BACKTEST_METRIC_COLUMNS = [
    Column("experiment_id", "string", nullable=False),
    Column("metric_scope", "string", nullable=False, enum=METRIC_SCOPES),
    Column("horizon_td", "int", nullable=False),
    Column("segment_key", "string", nullable=False,
           note="세그먼트 없으면 'ALL'. null을 PK에 쓰지 않는다"),
    Column("metric_name", "string", nullable=False),

    Column("metric_value", "float"),
    Column("raw_n", "int"),
    Column("effective_n", "float"),
    Column("ci_low", "float"),
    Column("ci_high", "float"),
    Column("standard_error", "float"),
    Column("inference_method", "string"),
    Column("cluster_key", "string"),
    Column("block_length", "int"),
    Column("p_value", "float"),
    Column("adjusted_p_value", "float"),
    Column("comparison", "string",
           note="무엇과 견준 값인가 — base_rate / benchmark / null"),
    Column("status", "string", nullable=False,
           enum=("SUCCESS", "PARTIAL", "FAILED")),
    Column("status_reason", "string"),

    Column("run_id", "string", nullable=False),
    Column("schema_version", "string", nullable=False),
    Column("data_revision", "int", nullable=False),
]


# ─────────────────────────────────────────────────────────────
# 검증기
# ─────────────────────────────────────────────────────────────

def assert_ledger_reconciles(trades, nav_rows, initial_capital,
                             tolerance: float = 1e-6):
    """거래 원장 합계가 NAV 변화와 맞는가 — 로드맵 Stage 7 DoD.

    맞지 않으면 어딘가에서 돈이 생겼거나 사라진 것이다. 백테스트에서 이게
    틀리면 수익률 전부가 의심스러워지므로, 경고가 아니라 hard fail로 둔다.

    미청산 포지션이 남아 있으면 마지막 NAV에 평가액으로 들어 있다. 그래서
    비교는 **청산 손익 + 미실현 손익**과 **NAV 변화** 사이에서 한다.
    """
    if not nav_rows:
        raise DataQualityError("NAV 기록이 없다 — 원장을 대조할 수 없다")

    last = max(nav_rows, key=lambda r: r["trading_date"])
    nav_change = float(last["net_nav"]) - float(initial_capital)
    realized = sum(float(r["realized_pnl"]) for r in nav_rows)
    unrealized = float(last["market_value"]) - sum(
        float(t["notional"]) for t in trades
        if t["trade_status"] == "OPEN")

    ledger = realized + unrealized
    gap = abs(ledger - nav_change)
    scale = max(1.0, abs(nav_change), abs(ledger))
    if gap / scale > tolerance:
        raise DataQualityError(
            f"원장 합계와 NAV 변화가 맞지 않는다: 원장 {ledger:,.4f} vs "
            f"NAV 변화 {nav_change:,.4f} (차이 {gap:,.6f})",
            context={"realized_pnl": realized, "unrealized": unrealized,
                     "nav_change": nav_change, "gap": gap})
    return gap


def assert_same_day_close_entry(trades):
    """**확정 종가 진입** — 실행 불가능한 가정을 명시적으로 허용할 때만 쓴다.

    신호는 장 종료 후 15:35에 나오는데 체결은 같은 날 15:30 종가다. 체결이
    신호보다 5분 앞선다. 동시호가는 그 종가를 **만드는** 절차이므로 확정
    종가를 보고 낸 주문은 현실에 존재할 수 없다.

    이 함수는 그 사실을 지우지 않는다. **그 한 가지 형태만** 열고 나머지
    look-ahead는 그대로 막는다.

    - 허용 — `entry_date == signal_date` (신호 당일 종가)
    - 차단 — `entry_date < signal_date`. 이건 근사가 아니라 버그다.

    결과를 읽을 때: 여기서 나온 수익은 **상한**이지 달성 가능한 수익이 아니다.
    """
    bad = [{"trade_id": t["trade_id"], "signal_date": t["signal_date"],
            "entry_date": t["entry_date"]}
           for t in trades
           if t["entry_date"] is not None and t["entry_date"] < t["signal_date"]]
    if bad:
        raise LookAheadError(
            f"신호일보다 앞선 날짜에 체결된 거래가 {len(bad)}건 있다 "
            f"— 확정 종가 가정으로도 설명되지 않는다",
            context={"violations": bad[:20]})
    return len(trades)


def assert_execution_after_signal(trades):
    """`entry_at > signal_at` — 아키텍처 §10 불변식 5.

    신호를 낸 시각보다 앞선 체결은 미래를 본 것이다. 조용히 버리지 않는다.
    """
    bad = [{"trade_id": t["trade_id"], "signal_at_utc": t["signal_at_utc"],
            "entry_at_utc": t["entry_at_utc"]}
           for t in trades
           if t["entry_at_utc"] is not None
           and t["entry_at_utc"] <= t["signal_at_utc"]]
    if bad:
        raise LookAheadError(
            f"신호 시각 이전에 체결된 거래가 {len(bad)}건 있다",
            context={"violations": bad[:20]})
    return len(trades)

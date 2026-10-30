# -*- coding: utf-8 -*-
"""Forward return과 base rate 열 계약 — 스키마 §7.

스키마 V2.0은 `gold.forward_returns`를 `signal_id` 기준으로 정의한다. 그런데
로드맵 Stage 1은 **신호와 무관한** 무조건부 기준선을 요구한다("날짜·유니버스·
보유기간별 무조건부 상승확률"). 신호가 붙지 않은 종목·날짜에도 수익률이 있어야
`gold.base_rates`를 만들 수 있다.

그래서 `gold.daily_forward_returns`를 추가한다. 같은 수익률 계산을 두 번
구현하지 않도록, Stage 6의 신호별 label은 이 패널에 조인해서 만든다.
스키마 문서에 없는 테이블이므로 추가 사실을 여기 명시한다(스키마 §10 MINOR
확장에 준해 기존 reader를 깨지 않는다).
"""

from __future__ import annotations

from ..domain.status import LabelStatus
from .schema import Column

__all__ = [
    "DAILY_FORWARD_RETURN_COLUMNS", "DAILY_FORWARD_RETURN_PK",
    "SIGNAL_FORWARD_RETURN_COLUMNS", "SIGNAL_FORWARD_RETURN_PK",
    "SIGNAL_LABEL_SOURCE",
    "BASE_RATE_COLUMNS", "BASE_RATE_PK",
    "ENTRY_RULES", "DEFAULT_HORIZONS", "RESEARCH_HORIZONS",
    "RETIRED_HORIZONS", "LABEL_VERSION", "label_version_for",
    "CONFIRMED_CLOSE_TAG", "is_confirmed_close",
]

#: 진입 규칙.
#:   NEXT_OPEN   다음 거래일 시가 — 장 마감 후 판정, 아침에 시장가
#:   NEXT_CLOSE  다음 거래일 종가 — 장 마감 후 판정, 다음날 MOC로 동시호가
#:   SAME_CLOSE  신호일 종가     — 확정 종가를 보고 시간외 종가매매(15:40~)
#:
#: **SAME_CLOSE를 동시호가로 체결할 수는 없다.** 동시호가는 종가를 만드는
#: 절차라 주문이 마감 전에 들어가야 하는데, Gate는 그 종가로 판정한다.
ENTRY_RULES = ("NEXT_OPEN", "NEXT_CLOSE", "SAME_CLOSE")


#: 확정 종가 진입 라벨의 버전 표식.
#:
#: 이 표식이 붙은 행은 **체결이 신호보다 앞선다**(신호 15:35, 체결 같은 날
#: 15:30). 실행 불가능한 가정이고, 그래서 정규 라벨과 같은 표에 있어도 행
#: 하나만 보고 구별할 수 있어야 한다. `signal_source`는 `gold.signals`에만
#: 있으므로 label 쪽 축은 `label_version`뿐이다.
CONFIRMED_CLOSE_TAG = "confirmed"


def label_version_for(entry_rule: str, base: str = None,
                      confirmed_close: bool = False) -> str:
    """진입 규칙에 맞는 `label_version`.

    버전 문자열이 규칙을 이미 담고 있다(`L1-**next_open**-close_exit-…`).
    그런데 `gold.forward_returns`의 PK는 `(signal_id, horizon_td,
    label_version)`이라 **entry_rule을 포함하지 않는다.** 두 규칙의 신호
    label을 같은 버전으로 쓰면 서로 덮어쓴다.

    그래서 규칙이 바뀌면 버전 문자열도 함께 바꾼다. 이름과 내용이 어긋난
    행(`entry_rule=SAME_CLOSE`인데 버전은 `next_open`)을 남기지 않는다.
    """
    base = base or LABEL_VERSION
    # 먼저 막는다. NEXT_OPEN/NEXT_CLOSE는 아래에서 바로 돌아가므로 검사를
    # 뒤에 두면 조용히 통과한다.
    if confirmed_close and entry_rule != "SAME_CLOSE":
        raise ValueError(
            f"confirmed_close는 SAME_CLOSE에서만 쓴다 (받은 값: {entry_rule})")
    if entry_rule == "NEXT_OPEN":
        return base
    if entry_rule == "NEXT_CLOSE":
        return base.replace("next_open", "next_close", 1)
    if entry_rule == "SAME_CLOSE":
        name = "same_close"
        if confirmed_close:
            name += "_" + CONFIRMED_CLOSE_TAG
        return base.replace("next_open", name, 1)
    raise ValueError(f"알 수 없는 entry_rule: {entry_rule}")


def is_confirmed_close(label_version: str) -> bool:
    """이 버전의 라벨이 확정 종가 가정으로 만들어졌는가.

    저장된 행을 검사하는 쪽(불변식 시험, 보고)이 예외를 **명시적으로** 집어낼
    수 있어야 한다. 문자열 포함 검사로 두는 이유는 base 버전이 올라가도
    표식이 따라가기 때문이다.
    """
    return CONFIRMED_CLOSE_TAG in (label_version or "")

#: 로드맵 §3 고정 규약 — 1/3/5/10/20 거래일.
#: **label 패널을 만들 horizon이다.** 데이터 정의이므로 줄이지 않는다 —
#: 이미 만든 label을 지우면 나중에 다시 보고 싶어도 못 본다.
DEFAULT_HORIZONS = (1, 3, 5, 10, 20)

#: 분석에서 **접은** horizon과 그 이유.
#:
#: 2026-09-19 결정. Stage 8 검정력 계산에서 h=1은 관측된 크기의 효과를 80%
#: 검정력으로 잡으려면 11,110~19,560거래일(45~80년)이 필요하다고 나왔다.
#: 1년치(252일)로는 물론이고 현실적으로 모을 수 있는 어떤 양으로도 판정할 수
#: 없다. 계속 표에 올리면 다중비교 예산만 쓰고 결론은 늘 "판정 불가"다.
#:
#: **label은 계속 만든다.** 접는 것은 보고와 검정이지 데이터가 아니다.
RETIRED_HORIZONS = {
    1: "검정력 부족 — 관측 크기의 효과를 잡으려면 45~80년치가 필요하다"
       "(Stage 8, 2026-09-19)",
}

#: 연구·보고의 기본 horizon.
RESEARCH_HORIZONS = tuple(h for h in DEFAULT_HORIZONS
                          if h not in RETIRED_HORIZONS)

#: label 정의 버전. 진입·청산·비용·barrier 기준이 바뀌면 반드시 올린다
#: (스키마 §10: "Label 의미 변경은 schema가 같아도 새 label_version").
#:   entry  = 다음 거래일 시가
#:   exit   = h 거래일 뒤 종가
#:   costs  = configs/costs/krx_v1.json
#:   MFE/MAE·barrier = 보유기간 내 **장중 고가/저가** 기준
LABEL_VERSION = "L1-next_open-close_exit-intraday_barrier-krx_v1"

_PRICE = "decimal"

DAILY_FORWARD_RETURN_PK = ("security_id", "signal_date", "horizon_td",
                           "entry_rule", "label_version")

DAILY_FORWARD_RETURN_COLUMNS = [
    Column("security_id", "string", nullable=False),
    Column("signal_date", "date", nullable=False,
           note="신호를 낼 수 있었던 거래일. 진입은 그 다음 거래일이다"),
    Column("horizon_td", "int", nullable=False),
    Column("entry_rule", "string", nullable=False, enum=ENTRY_RULES),

    Column("entry_date", "date"),
    Column("exit_date", "date"),
    Column("entry_at_utc", "timestamp"),
    Column("exit_at_utc", "timestamp"),
    Column("entry_price", _PRICE),
    Column("exit_price", _PRICE),

    Column("stock_return_gross", "float"),
    Column("stock_return_net", "float"),

    Column("benchmark_id", "string"),
    Column("benchmark_return", "float"),
    Column("excess_return_gross", "float"),
    Column("excess_return_net", "float"),

    Column("mfe", "float", note="보유기간 내 최대 유리 수익(장중 고가 기준)"),
    Column("mae", "float", note="보유기간 내 최대 불리 수익(장중 저가 기준)"),

    Column("up", "bool", note="stock_return_net > 0"),
    Column("up_gross", "bool", note="stock_return_gross > 0"),
    Column("outperform", "bool", note="벤치마크 없으면 null — 0으로 대체 금지"),
    Column("hit_3pct", "bool"),
    Column("hit_5pct", "bool"),
    Column("hit_minus_3pct", "bool"),
    Column("hit_minus_5pct", "bool"),

    Column("commission", "float"),
    Column("tax", "float"),
    Column("slippage", "float"),

    Column("label_status", "string", nullable=False, enum=LabelStatus.values()),
    Column("status_reason", "string"),
    Column("label_version", "string", nullable=False),
    Column("cost_model_version", "string", nullable=False),
    Column("market_rule_version", "string", nullable=False),
    Column("run_id", "string", nullable=False),
    Column("schema_version", "string", nullable=False),
    Column("data_revision", "int", nullable=False),
]


# ─────────────────────────────────────────────────────────────
# gold.forward_returns (스키마 §7) — 신호별 label, 로드맵 Stage 6
# ─────────────────────────────────────────────────────────────

#: 신호 label이 어디서 왔는지. 값을 다시 계산하지 않고 무조건부 패널에서
#: 가져오므로, 두 표의 수익률이 갈릴 수 없다.
SIGNAL_LABEL_SOURCE = "gold.daily_forward_returns"

SIGNAL_FORWARD_RETURN_PK = ("signal_id", "horizon_td", "label_version")

#: 스키마 §7 열 목록에 `security_id`·`signal_date`·`signal_at_utc`를 더했다
#: (§10 MINOR 확장). 이유: `signal_at < entry_at` 불변식을 **이 행 하나만 보고**
#: 확인할 수 있어야 한다. `gold.signals`와 조인해야만 검증되는 불변식은
#: 조인을 잊는 순간 사라진다.
SIGNAL_FORWARD_RETURN_COLUMNS = [
    Column("signal_id", "string", nullable=False),
    Column("horizon_td", "int", nullable=False),
    Column("entry_rule", "string", nullable=False, enum=ENTRY_RULES),

    Column("security_id", "string", nullable=False),
    Column("signal_date", "date", nullable=False),
    Column("signal_at_utc", "timestamp", nullable=False,
           note="신호 시각. entry_at_utc는 반드시 이보다 뒤다"),

    Column("entry_date", "date"),
    Column("exit_date", "date"),
    Column("entry_at_utc", "timestamp"),
    Column("exit_at_utc", "timestamp"),
    Column("entry_price", _PRICE),
    Column("exit_price", _PRICE),

    Column("stock_return_gross", "float"),
    Column("stock_return_net", "float"),

    Column("benchmark_id", "string"),
    Column("benchmark_return", "float"),
    Column("excess_return_gross", "float"),
    Column("excess_return_net", "float"),

    Column("mfe", "float"),
    Column("mae", "float"),

    Column("up", "bool"),
    Column("up_gross", "bool"),
    Column("outperform", "bool"),
    Column("hit_3pct", "bool"),
    Column("hit_5pct", "bool"),
    Column("hit_minus_3pct", "bool"),
    Column("hit_minus_5pct", "bool"),

    Column("commission", "float"),
    Column("tax", "float"),
    Column("slippage", "float"),

    Column("label_status", "string", nullable=False, enum=LabelStatus.values()),
    Column("status_reason", "string"),
    Column("calendar_checked", "bool", nullable=False,
           note="entry/exit이 거래 캘린더와 맞는지 확인했는가(Stage 6 DoD)"),
    Column("label_source", "string", nullable=False),
    Column("label_version", "string", nullable=False),
    Column("cost_model_version", "string", nullable=False),
    Column("market_rule_version", "string", nullable=False),
    Column("run_id", "string", nullable=False),
    Column("schema_version", "string", nullable=False),
    Column("data_revision", "int", nullable=False),
]


# ─────────────────────────────────────────────────────────────
# gold.base_rates (스키마 §7)
# ─────────────────────────────────────────────────────────────

BASE_RATE_PK = ("base_rate_id",)

BASE_RATE_COLUMNS = [
    Column("base_rate_id", "string", nullable=False, note="spec hash"),
    Column("universe_id", "string", nullable=False),
    Column("start_date", "date", nullable=False),
    Column("end_date", "date", nullable=False),
    Column("horizon_td", "int", nullable=False),
    Column("entry_rule", "string", nullable=False, enum=ENTRY_RULES),
    Column("outcome", "string", nullable=False,
           note="up / up_gross / outperform / hit_3pct ..."),
    Column("regime", "string", note="연도·시장국면 등 세그먼트. 전체면 null"),

    Column("raw_n", "int", nullable=False),
    Column("trading_date_n", "int", nullable=False),
    Column("security_n", "int", nullable=False),
    Column("positive_n", "int", nullable=False),
    Column("probability", "float", nullable=False),
    Column("mean_return", "float"),
    Column("median_return", "float"),
    Column("benchmark_mean_return", "float"),
    Column("excess_mean_return", "float"),

    Column("cluster_key", "string", nullable=False),
    Column("icc", "float"),
    Column("design_effect", "float"),
    Column("effective_n", "float"),
    Column("ci_method", "string", nullable=False),
    Column("ci_low", "float"),
    Column("ci_high", "float"),
    Column("ci_level", "float", nullable=False),
    Column("block_length", "int"),
    Column("bootstrap_replicates", "int"),
    Column("seed", "int"),

    Column("status", "string", nullable=False,
           enum=("SUCCESS", "PARTIAL", "FAILED")),
    Column("label_version", "string", nullable=False),
    Column("cost_model_version", "string", nullable=False),
    Column("run_id", "string", nullable=False),
    Column("schema_version", "string", nullable=False),
    Column("data_revision", "int", nullable=False),
]

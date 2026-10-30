# -*- coding: utf-8 -*-
"""일봉 패널에서 직접 신호를 만든다 — 16년을 포트폴리오로 굴리기 위해.

왜 필요한가
-----------
Stage 7 백테스트 엔진은 `gold.signals`를 읽는다. 그런데 그 표는 Stage 5
replay가 만든 것이고, replay는 **분봉 캐시가 있는 252거래일**에만 돌 수 있다.

그래서 지금까지 포트폴리오 시뮬레이션은 1년짜리 한 구간에서만 돌았다. 그
구간은 코스피200이 +150% 오른 극단적인 상승장이었고, 그 안에서 나온 성과를
일반화할 수 없다.

반면 **일봉 패널은 2010-01-04부터 4,114거래일**이 있다. 가격·OBV·유동성
조건은 분봉 없이 계산되므로 이 구간 전체에서 Gate를 판정할 수 있다. 실제로
Stage 2와 선택지 C가 이미 그렇게 했다 — 다만 **신호 단위 평균**까지만 봤고,
슬롯·자금·비용을 넣고 굴려 본 적이 없다.

이 모듈이 그 사이를 잇는다. 일봉 feature를 `gold.signals` 모양으로 내보내면
그 뒤는 기존 경로(`signal-label build` → `backtest run`)가 그대로 돈다.

무엇을 하지 않는가
------------------
**점수를 만들지 않는다.** V1.5 총점은 CVD 성분을 포함하므로 일봉만으로는
재현할 수 없다. 없는 점수를 지어내면 그 순간 "V1.5를 검증한다"가 아니라
"새 지표를 만든다"가 된다. 그래서 `total_score`는 **null**로 두고, 선택은
Gate 통과 여부와 아키텍처 §6의 tie-break(거래대금 내림차순, 종목코드
오름차순)로만 한다.

따라서 이 경로에서 쓸 수 있는 정책은 `BUY_ALL`과 `RANDOM`이다.
`SCORE_Q*`·`RANK_PCT*`는 점수가 없으므로 빈 결과가 나온다 — 막지 않고
비워 둔다. 점수가 없다는 사실 자체가 결과에 드러나는 편이 낫다.

**증거 계층을 섞지 않는다.** `signal_source=DAILY_PANEL`,
`daily_evidence_tier=DAILY_ONLY_LONG`으로 표시한다. 분봉 replay 신호와 한
덩어리로 세면 "CVD를 포함한 V1.5"와 "일봉 3조건"이 구별되지 않는다.

look-ahead를 어떻게 막는가
--------------------------
신호 시각을 **15:35 KST**로 둔다. 정규장은 15:30에 끝나고 종가도 그때
확정되지만, 15:30 정각을 쓰면 판정 여유가 0분이다. Stage 5 replay가 쓰는
값과 같게 맞춘다 — 두 경로가 다른 관행을 쓰면 나중에 섞어 읽게 된다.

진입은 label이 정한다. `NEXT_OPEN`이면 다음 거래일 시가,
`SAME_CLOSE`면 그날 종가(시간외 종가매매 15:40~16:00 경로)다.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from ..contracts.schema import (
    SIGNAL_COLUMNS, SIGNAL_PK, validate_rows,
)
from ..domain.errors import ConfigError, NoInputDataError
from ..domain.ids import KST, make_signal_id, sha256_of
from ..domain.market_rules import default_registry
from ..domain.status import (
    EvidenceTier, FeatureStatus, Session, SignalSource, SignalStatus,
)
from ..ingestion.universe_history import UniverseRepository
from ..storage.parquet_store import ParquetStore

__all__ = ["GATE_VARIANTS", "DAILY_SIGNAL_VERSION", "DailyPanelSignalEmitter"]

#: 이 경로가 만드는 신호의 버전. 계산 규칙을 바꾸면 올린다.
DAILY_SIGNAL_VERSION = "DP-1.0"

#: Gate 조합. 이름은 Stage 2·선택지 C에서 쓰던 것을 그대로 쓴다 — 같은 것을
#: 두 이름으로 부르지 않기 위해서다.
#:
#: `PRICE+OBV`가 기본값인 이유: 선택지 C에서 FDR을 통과한 유일한 후보가
#: 이 조합이고, 유동성 조건은 **집합을 전혀 바꾸지 않는다**고 두 경로에서
#: 확인됐다(delta가 추정이 아니라 정확히 0).
GATE_VARIANTS = {
    "ALL": (),
    "PRICE": ("cond_price",),
    "OBV": ("cond_obv",),
    "PRICE+OBV": ("cond_price", "cond_obv"),
    "GATE_ALL": ("cond_price", "cond_obv", "cond_liquidity"),
}

#: part_key를 짧게 쓰기 위한 약칭. 파일명이 60자를 넘으면 비밀정보 탐지기의
#: `kiwoom_token` 패턴(연속 60자 이상)에 걸려 쓰기가 막힌다 — Stage 7에서
#: 한 번 겪은 함정이다. 탐지기를 약하게 만드는 대신 이름을 줄인다.
_VARIANT_ABBREV = {"ALL": "A", "PRICE": "P", "OBV": "O",
                   "PRICE+OBV": "PO", "GATE_ALL": "G"}

#: 신호 시각. 정규장은 15:30에 끝나고 종가는 그때 확정되지만, 15:30 정각을
#: 쓰면 판정 여유가 0분이다. Stage 5 replay가 쓰는 **15:35**로 맞춘다 —
#: 두 경로가 다른 관행을 쓰면 나중에 섞어 읽게 된다.
_SIGNAL_TIME = time(15, 35)
#: 계약은 "HH:MM:SS" 문자열을 받는다(time64 변환은 write 계층).
_SIGNAL_TIME_TEXT = _SIGNAL_TIME.strftime("%H:%M:%S")

_FEATURE_COLUMNS = [
    "security_id", "trading_date", "cond_price", "cond_obv",
    "cond_liquidity", "gate_all", "avg_traded_value_20d",
    "price_data_status", "obv_data_status", "liquidity_data_status",
    "feature_status", "feature_version",
]

#: 조건별로 어느 family의 상태를 봐야 하는가. 상태가 SUCCESS가 아니면 그
#: 조건은 **거짓이 아니라 판정 불가**다. 둘을 섞으면 데이터가 없어서 못 산
#: 것과 조건에 걸려서 안 산 것이 같아진다.
_CONDITION_STATUS = {
    "cond_price": "price_data_status",
    "cond_obv": "obv_data_status",
    "cond_liquidity": "liquidity_data_status",
}


class DailyPanelSignalEmitter:
    """`gold.daily_features` × 유니버스 이력 → `gold.signals`."""

    LAYER = "gold"
    TABLE = "signals"

    def __init__(self, store: ParquetStore = None, data_revision: int = 1,
                 code_revision: str = DAILY_SIGNAL_VERSION,
                 registry=None, market: str = "KRX",
                 decision: str = "AFTER_CLOSE"):
        """`decision`은 언제 판정하는가다.

        - `AFTER_CLOSE` — 마감 뒤 15:35. 진입은 다음 거래일이다.
        - `AUCTION`     — 마감 동시호가 중(마감 -1분). **당일 종가 매수**를
          모델링할 때 쓴다. 지표를 확정 종가로 계산하는 근사가 들어간다.
        """
        if decision not in ("AFTER_CLOSE", "AUCTION"):
            raise ConfigError(f"알 수 없는 판정 시점: {decision}")
        self.decision = decision
        self.store = store or ParquetStore()
        self.data_revision = int(data_revision)
        self.code_revision = code_revision
        # 시장 규칙 버전은 계약상 필수다. 일봉 feature가 쓰는 것과 같은
        # 레지스트리를 써야 두 표의 lineage가 갈리지 않는다.
        self.registry = registry or default_registry()
        self.market = market

    # ── 입력 ──────────────────────────────────────────────────

    def load_features(self, start=None, end=None):
        import pyarrow.compute as pc

        table = self.store.read_arrow(
            self.LAYER, "daily_features", columns=_FEATURE_COLUMNS,
            dedupe_pk=("security_id", "trading_date", "feature_version"))
        if table.num_rows == 0:
            raise NoInputDataError("일봉 feature가 없다 — `features build` 먼저")
        if start is not None:
            table = table.filter(
                pc.greater_equal(table.column("trading_date"), start))
        if end is not None:
            table = table.filter(
                pc.less_equal(table.column("trading_date"), end))
        if table.num_rows == 0:
            raise NoInputDataError("그 구간에 일봉 feature가 없다")
        return table

    @property
    def signal_source(self):
        return (SignalSource.DAILY_PANEL_AUCTION
                if self.decision == "AUCTION" else SignalSource.DAILY_PANEL)

    def _signal_at(self, day):
        """판정 시각(KST). `AUCTION`이면 정규장 마감 1분 전이다.

        마감 시각은 시기마다 다르다(2016-08 이전 15:00, 이후 15:30).
        고정값을 쓰면 옛 구간에서 판정이 마감 뒤가 되어 버린다.
        """
        if self.decision != "AUCTION":
            return datetime.combine(day, _SIGNAL_TIME, tzinfo=KST)
        cached = getattr(self, "_close_cache", None)
        if cached is None:
            cached = self._close_cache = {}
        hit = cached.get(day)
        if hit is None:
            rule = self.registry.resolve(self.market, day)
            bounds = rule.session_bounds(day, Session.REGULAR)
            if bounds is None:
                # 세션이 정의되지 않은 구간(daily_only). 제도상 마감은
                # 15:00이었으므로 그 1분 전으로 둔다.
                hit = datetime.combine(day, time(14, 59), tzinfo=KST)
            else:
                hit = bounds[1] - timedelta(minutes=1)
            cached[day] = hit
        return hit

    def _rule_version(self, day):
        """그 거래일에 적용되는 시장 규칙 버전.

        날짜마다 레지스트리를 훑으면 80만 번 해석하게 되므로 캐시한다.
        """
        cached = getattr(self, "_rule_cache", None)
        if cached is None:
            cached = self._rule_cache = {}
        hit = cached.get(day)
        if hit is None:
            hit = cached[day] = self.registry.resolve(self.market, day).version
        return hit

    def membership(self, index_id: str):
        """{종목: [(시작, 종료|None)]}. 없으면 SurvivorshipError가 난다.

        오늘의 구성종목을 과거에 적용하지 않기 위해 **반드시** 통과해야 하는
        관문이다. 이 경로도 예외가 아니다.
        """
        return UniverseRepository(self.store).membership_intervals(
            index_id)

    # ── 변환 ──────────────────────────────────────────────────

    def make_rows(self, table, intervals, index_id: str, variant: str,
                  run_id: str, universe_snapshot_id: str = None):
        if variant not in GATE_VARIANTS:
            raise ConfigError(
                f"알 수 없는 Gate 조합: {variant} "
                f"(가능: {', '.join(sorted(GATE_VARIANTS))})")
        conditions = GATE_VARIANTS[variant]
        snapshot_id = universe_snapshot_id or f"daily-panel:{index_id}"
        # 판정 시점이 지문에 들어가야 두 경로의 signal_id가 갈린다.
        config_hash = sha256_of({"variant": variant,
                                 "conditions": list(conditions),
                                 "version": DAILY_SIGNAL_VERSION,
                                 "decision": self.decision})
        created = datetime.now(tz=timezone.utc)

        rows = []
        skipped_not_member = 0
        for record in table.to_pylist():
            day = record["trading_date"]
            spans = intervals.get(record["security_id"])
            if not spans or not _within(spans, day):
                skipped_not_member += 1
                continue

            passed, reasons, undecidable = _evaluate(record, conditions)
            if undecidable:
                status = SignalStatus.DATA_INSUFFICIENT
            elif passed:
                status = SignalStatus.BUY_CANDIDATE
            else:
                status = SignalStatus.WATCH

            signal_at = self._signal_at(day)
            rows.append({
                "schema_version": "2.0",
                "created_at_utc": created,
                "run_id": run_id,
                "config_hash": config_hash,
                "code_revision": self.code_revision,
                "scanner_version": DAILY_SIGNAL_VERSION,
                "market_rule_version": self._rule_version(day),
                "data_revision": self.data_revision,
                "signal_id": make_signal_id(
                    DAILY_SIGNAL_VERSION, config_hash, snapshot_id,
                    signal_at, record["security_id"]),
                "signal_source": self.signal_source,
                "signal_at_utc": signal_at.astimezone(timezone.utc),
                "signal_time_local": signal_at.strftime("%H:%M:%S"),
                "trading_date": day,
                "security_id": record["security_id"],
                "index_id": index_id,
                "universe_snapshot_id": snapshot_id,
                "status": status,
                "gate_passed": bool(passed),
                "gate_reasons": reasons,
                # 점수는 만들지 않는다 — 위 모듈 설명 참고.
                "total_score": None,
                "rank": None,
                "rank_population": None,
                "rank_percentile": None,
                "feature_status": (FeatureStatus.PARTIAL if undecidable
                                   else FeatureStatus.SUCCESS),
                "daily_evidence_tier": EvidenceTier.DAILY_ONLY_LONG,
                "last_input_bar_at_utc": None,
            })
        return rows, skipped_not_member

    # ── 실행 ──────────────────────────────────────────────────

    def build(self, index_id: str = "KOSPI200", variant: str = "PRICE+OBV",
              start=None, end=None, run_id: str = "", dry_run: bool = False,
              chunk_years: bool = True):
        table = self.load_features(start, end)
        intervals = self.membership(index_id)
        rows, skipped = self.make_rows(table, intervals, index_id, variant,
                                       run_id)
        if not rows:
            raise NoInputDataError(
                "유니버스 구간 안에 있는 일봉 feature가 없다")
        validate_rows(rows, SIGNAL_COLUMNS, SIGNAL_PK, "gold.signals")

        summary = {
            "index_id": index_id,
            "variant": variant,
            "conditions": list(GATE_VARIANTS[variant]),
            "signals": len(rows),
            "buy_candidate": sum(
                1 for r in rows if r["status"] == SignalStatus.BUY_CANDIDATE),
            "watch": sum(1 for r in rows
                         if r["status"] == SignalStatus.WATCH),
            "data_insufficient": sum(
                1 for r in rows
                if r["status"] == SignalStatus.DATA_INSUFFICIENT),
            "skipped_not_member": skipped,
            "first_date": str(min(r["trading_date"] for r in rows)),
            "last_date": str(max(r["trading_date"] for r in rows)),
            "trading_dates": len({r["trading_date"] for r in rows}),
            "securities": len({r["security_id"] for r in rows}),
            "signal_source": self.signal_source,
            "decision": self.decision,
            "evidence_tier": EvidenceTier.DAILY_ONLY_LONG,
        }
        total = summary["buy_candidate"] + summary["watch"]
        summary["selection_rate"] = (summary["buy_candidate"] / total
                                     if total else None)

        if dry_run:
            summary["artifacts"] = []
            return summary

        # 연 단위로 나눠 쓴다 — 80만 행을 한 번에 들고 있지 않기 위해서다.
        paths = []
        if chunk_years:
            by_year = {}
            for row in rows:
                by_year.setdefault(row["trading_date"].year, []).append(row)
            for year in sorted(by_year):
                write = self.store.write(
                    self.LAYER, self.TABLE, by_year[year], SIGNAL_COLUMNS,
                    SIGNAL_PK, run_id, partition_fn=_partition,
                    part_key=(f"{_VARIANT_ABBREV[variant]}"
                              f"{'X' if self.decision == 'AUCTION' else ''}"
                              f"{year}"),
                    extra_manifest={"signal_source": self.signal_source,
                                    "variant": variant,
                                    "code_revision": self.code_revision})
                paths.append(write.manifest_path)
        else:
            write = self.store.write(
                self.LAYER, self.TABLE, rows, SIGNAL_COLUMNS, SIGNAL_PK,
                run_id, partition_fn=_partition,
                extra_manifest={"signal_source": self.signal_source,
                                "variant": variant,
                                "code_revision": self.code_revision})
            paths.append(write.manifest_path)
        summary["artifacts"] = paths
        return summary


def _within(spans, day):
    for begin, finish in spans:
        if begin <= day and (finish is None or day <= finish):
            return True
    return False


def _evaluate(record, conditions):
    """(통과 여부, 이유 목록, 판정 불가 여부).

    상태가 SUCCESS가 아닌 조건은 **거짓으로 세지 않는다.** 데이터가 없어서
    못 산 것과 조건에 걸려서 안 산 것은 다른 사건이다.
    """
    reasons, undecidable = [], False
    passed = True
    for name in conditions:
        status_field = _CONDITION_STATUS[name]
        if record.get(status_field) != "SUCCESS":
            undecidable = True
            reasons.append(f"{name}:UNDECIDABLE")
            continue
        if not record.get(name):
            passed = False
            reasons.append(f"{name}:FALSE")
    if undecidable:
        passed = False
    return passed, reasons, undecidable


def _partition(row):
    day = row["trading_date"]
    return [("index_id", row["index_id"]), ("year", day.year),
            ("month", f"{day.month:02d}")]

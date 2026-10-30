# -*- coding: utf-8 -*-
"""과거 스캐너 replay — 로드맵 Stage 5.

처리 순서(아키텍처 §6)
----------------------
    market rule resolve → universe as-of 조회 → coverage 검사 → 15:35 slice
    → feature 계산 → cross-section 점수 → Gate → 적재

여기서 지키는 것
----------------
1. **현재 유니버스 파일을 읽지 않는다.** 그날의 구성종목은
   `silver.universe_membership` 이력에서만 온다. 오늘의 스냅샷을 쓰면 그 순간
   replay 전체가 생존편향이 된다(Stage 2에서 그 크기를 쟀다).
2. **15:35 이후 봉을 보지 않는다.** `MinuteWindow.slice_as_of()`가 `bar_end <=
   as_of`를 강제한다. Metamorphic 테스트가 이걸 확인한다.
3. **제외 사유를 남긴다.** 분석되지 않은 종목을 조용히 버리면 그날의 모집단이
   달라진 것을 아무도 모른다.
4. **같은 입력이면 같은 결과다.** `signal_id`는 (scanner_version, config_hash,
   universe_snapshot_id, signal_at, security_id)에서만 나온다 — `run_id`는
   섞이지 않는다. 그래서 재실행해도 같은 해시가 나온다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from ..baseline.scanner_v15 import V15_PATH
from ..contracts.v15_signals import convert_v15_snapshot, source_sha256
from ..domain.errors import ConfigError, NoInputDataError
from ..domain.ids import KST, make_run_id, to_utc
from ..domain.market_rules import default_registry
from ..domain.status import Session, SignalSource
from ..ingestion.universe_history import UniverseRepository
from ..storage.parquet_store import ParquetStore
from .scanner_core import CacheBackedScanner
from .windows import DailyWindow, MinuteWindow

__all__ = ["HistoricalScannerReplay", "ReplayResult", "SIGNAL_TIME"]

#: 연구 규약 — 신호 시각은 15:35 KST(정규장 종가 단일가 봉이 끝난 직후).
SIGNAL_TIME = (15, 35)

_EXCLUSION_NO_MINUTE = "NO_MINUTE_BARS"
_EXCLUSION_NO_DAILY = "NO_DAILY_BARS"
_EXCLUSION_ANALYZE_NONE = "ANALYZE_RETURNED_NONE"


class ReplayResult:
    """하루치 replay 결과."""

    __slots__ = ("trading_date", "signal_at", "run_row", "signals",
                 "features", "warnings", "universe", "analyzed",
                 "exclusions", "payload")

    def __init__(self, trading_date, signal_at, run_row, signals, features,
                 warnings, universe, analyzed, exclusions, payload):
        self.trading_date = trading_date
        self.signal_at = signal_at
        self.run_row = run_row
        self.signals = signals
        self.features = features
        self.warnings = warnings
        self.universe = universe
        self.analyzed = analyzed
        self.exclusions = exclusions
        self.payload = payload

    def summary(self):
        buy = sum(1 for s in self.signals
                  if s.get("status") == "BUY_CANDIDATE")
        return {
            "trading_date": self.trading_date.isoformat(),
            "signal_at": self.signal_at.isoformat(),
            "universe_n": len(self.universe),
            "analyzed_n": self.analyzed,
            "excluded_n": len(self.exclusions),
            "signals": len(self.signals),
            "buy_candidates": buy,
            "coverage": (round(self.analyzed / len(self.universe), 6)
                         if self.universe else None),
            "universe_snapshot_id": self.run_row.get("universe_snapshot_id"),
            "config_hash": self.run_row.get("config_hash"),
        }


class HistoricalScannerReplay:
    """거래일별 15:35 replay."""

    def __init__(self, store: ParquetStore = None, universe=None, scanner=None,
                 data_revision: int = 1, decision: str = "AFTER_CLOSE"):
        if decision not in ("AFTER_CLOSE", "AUCTION"):
            raise ConfigError(f"알 수 없는 판정 시점: {decision}")
        self.decision = decision
        self.store = store or ParquetStore()
        self.universe = universe or UniverseRepository(self.store)
        self.core = CacheBackedScanner(scanner)
        self.registry = default_registry()
        self.data_revision = int(data_revision)
        self.code_revision = source_sha256(V15_PATH)
        self._minute = None
        self._daily = None
        self._prepared = None

    # ── 준비 ──────────────────────────────────────────────────────
    def prepare(self, index_id: str, start: date, end: date,
                period: int = 5, bdays: int = 5, securities=None,
                minute_source: str = "standard"):
        """구간 전체에 필요한 봉을 한 번에 올린다."""
        codes = set(securities or ())
        if not codes:
            for day in (start, end):
                codes |= {m.split(":")[-1]
                          for m in self._members(index_id, day)}
        codes = sorted(codes)
        self._minute = MinuteWindow(self.store, codes, start, end,
                                    period=period, keep_days=bdays + 3,
                                    minute_source=minute_source)
        self._daily = DailyWindow(self.store, codes, start, end)
        self._prepared = (index_id, start, end, period, bdays)
        return self._minute.trading_days

    def _members(self, index_id, day):
        got = self.universe.get_members(index_id, day)
        return list(got["members"])

    @property
    def signal_source(self):
        return (SignalSource.REPLAY_AUCTION if self.decision == "AUCTION"
                else SignalSource.REPLAY)

    def _signal_at(self, rule, trading_date):
        """판정 시각(KST).

        - `AFTER_CLOSE` — 연구 규약 15:35. 종가 단일가 봉이 끝난 직후다.
        - `AUCTION`     — 정규장 마감 1분 전. 당일 종가 매수를 모델링할 때만
          쓴다. **정규장이 미완이라 CVD 성분이 온전하지 않다.**
        """
        if self.decision == "AUCTION":
            bounds = rule.session_bounds(trading_date, Session.REGULAR)
            if bounds is not None:
                return bounds[1] - timedelta(minutes=1)
        return datetime(trading_date.year, trading_date.month,
                        trading_date.day, SIGNAL_TIME[0], SIGNAL_TIME[1],
                        tzinfo=KST)

    # ── 하루 ──────────────────────────────────────────────────────
    def replay_date(self, index_id: str, trading_date: date, run_id: str = None,
                    period: int = 5, bdays: int = 5,
                    daily_scope: str = "regular", rank_session: str = None,
                    index_key: str = None):
        if self._minute is None:
            raise NoInputDataError("prepare()를 먼저 불러야 한다")

        rule = self.registry.resolve("KRX", trading_date)
        signal_at = self._signal_at(rule, trading_date)

        members = self._members(index_id, trading_date)
        codes = [m.split(":")[-1] for m in members]

        results, exclusions = [], []
        for code in codes:
            minute_bars = self._minute.slice_as_of(
                code, signal_at, lookback_trading_days=bdays)
            daily_bars = self._daily.slice_as_of(code, signal_at)
            if not minute_bars and not daily_bars:
                exclusions.append({"security_id": f"KRX:{code}",
                                   "reason": _EXCLUSION_NO_MINUTE})
                continue
            if not daily_bars:
                exclusions.append({"security_id": f"KRX:{code}",
                                   "reason": _EXCLUSION_NO_DAILY})
                continue
            got = self.core.analyze(code, signal_at, minute_bars, daily_bars,
                                    name=code, period=period, bdays=bdays,
                                    daily_scope=daily_scope,
                                    rank_session=rank_session)
            if not got:
                exclusions.append({"security_id": f"KRX:{code}",
                                   "reason": _EXCLUSION_ANALYZE_NONE})
                continue
            got["index"] = index_key or index_id.lower()
            results.append(got)

        if not results:
            raise NoInputDataError(
                f"{trading_date}: 분석된 종목이 없다 — cache 구간을 확인하라")

        self.core.finalize(results)
        run_id = run_id or make_run_id("replay", to_utc(signal_at))
        payload = self.core.snapshot_payload(
            results, signal_at, index_key or index_id.lower(), run_id,
            period=period, bdays=bdays, daily_scope=daily_scope,
            rank_session=rank_session,
            label=f"replay {trading_date} {index_id}")

        converted = convert_v15_snapshot(
            payload, code_revision=self.code_revision,
            signal_source=self.signal_source, run_id=run_id,
            data_revision=self.data_revision, universe_codes=codes)

        run_row = dict(converted.run_row)
        run_row["market_rule_version"] = rule.version
        return ReplayResult(trading_date, signal_at, run_row,
                            converted.signals, converted.features,
                            list(converted.warnings), members, len(results),
                            exclusions, payload)

    # ── 구간 ──────────────────────────────────────────────────────
    def replay_range(self, index_id: str, start: date, end: date,
                     run_id: str = None, period: int = 5, bdays: int = 5,
                     daily_scope: str = "regular", rank_session: str = None,
                     index_key: str = None, dry_run: bool = False,
                     on_progress=None, minute_source: str = "standard"):
        """구간 전체를 하루씩 replay하고 한 번에 적재한다.

        봉 기준 bars-1.0: 일봉은 정규장 봉이므로 `daily_scope="regular"`가
        기본이다(정규장 확정 15:35에 그날 일봉이 완료된다). 2026-10-03 이전
        결과를 재현하려면 `daily_scope="auto", minute_source="native"`.
        """
        days = self.prepare(index_id, start, end, period=period, bdays=bdays,
                            minute_source=minute_source)
        if not days:
            raise NoInputDataError(
                f"{start}~{end}에 분봉이 있는 거래일이 없다")
        run_id = run_id or make_run_id("replay", to_utc(datetime.now(tz=KST)))

        runs, signals, features, per_day, exclusions = [], [], [], [], []
        for day in days:
            result = self.replay_date(
                index_id, day, run_id=f"{run_id}_{day:%Y%m%d}", period=period,
                bdays=bdays, daily_scope=daily_scope,
                rank_session=rank_session, index_key=index_key)
            runs.append(result.run_row)
            signals.extend(result.signals)
            features.extend(result.features)
            exclusions.extend(dict(row, trading_date=day.isoformat())
                              for row in result.exclusions)
            per_day.append(result.summary())
            if on_progress:
                on_progress(result)

        summary = {
            "run_id": run_id,
            "index_id": index_id,
            "start": days[0].isoformat(), "end": days[-1].isoformat(),
            "trading_days": len(days),
            "signals": len(signals),
            "buy_candidates": sum(d["buy_candidates"] for d in per_day),
            "universe_min": min(d["universe_n"] for d in per_day),
            "universe_max": max(d["universe_n"] for d in per_day),
            "coverage_min": min(d["coverage"] for d in per_day),
            "excluded_rows": len(exclusions),
            "exclusion_reasons": _count_reasons(exclusions),
            "per_day": per_day,
            "signal_digest": _digest(signals),
            "artifacts": [],
        }
        if dry_run:
            return summary
        summary["artifacts"] = self._write(runs, signals, features, run_id)
        return summary

    def _write(self, runs, signals, features, run_id):
        from ..contracts.schema import (
            SCANNER_RUN_COLUMNS, SCANNER_RUN_PK, SIGNAL_COLUMNS,
            SIGNAL_FEATURE_COLUMNS, SIGNAL_FEATURE_PK, SIGNAL_PK,
        )

        paths = []
        for table, rows, columns, pk, partition in (
                ("scanner_runs", runs, SCANNER_RUN_COLUMNS, SCANNER_RUN_PK,
                 _run_partition),
                ("signals", signals, SIGNAL_COLUMNS, SIGNAL_PK,
                 _signal_partition),
                ("signal_features", features, SIGNAL_FEATURE_COLUMNS,
                 SIGNAL_FEATURE_PK, None)):
            if not rows:
                continue
            write = self.store.write(
                "gold", table, rows, columns, pk, run_id,
                partition_fn=partition,
                extra_manifest={"signal_source": self.signal_source,
                                "code_revision": self.code_revision})
            paths.append(write.manifest_path)
        return paths


def _run_partition(row):
    # scanner_runs에는 trading_date 열이 없다(계약 §7). 신호 시각에서 뽑는다.
    day = row["signal_at_utc"].astimezone(KST).date()
    return [("index_id", row["index_id"]), ("year", day.year),
            ("month", f"{day.month:02d}")]


def _signal_partition(row):
    day = row["trading_date"]
    return [("index_id", row["index_id"]), ("year", day.year),
            ("month", f"{day.month:02d}")]


def _count_reasons(rows):
    counts = {}
    for row in rows:
        counts[row["reason"]] = counts.get(row["reason"], 0) + 1
    return counts


def _digest(signals):
    """신호 집합의 결정적 지문 — 재실행이 같은 결과인지 확인하는 데 쓴다.

    `run_id`·`created_at_utc`처럼 실행마다 달라지는 값은 뺀다. 그것까지 넣으면
    "같은 입력이면 같은 결과"를 확인할 수 없다.
    """
    from ..domain.ids import sha256_of

    spec = sorted(
        (s["signal_id"], s["security_id"], s["status"],
         s.get("total_score"), s.get("rank"))
        for s in signals)
    return sha256_of(spec)

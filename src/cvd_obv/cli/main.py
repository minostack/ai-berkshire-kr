# -*- coding: utf-8 -*-
"""`cvdobv` CLI — Stage 0 명령.

아키텍처 §8의 종료 코드 계약을 지킨다. 성공 시 생성 artifact와 row count를
출력하고, 모든 쓰기 명령은 `--dry-run`·`--run-id`·`--log-json`을 받는다.

Stage 0에서 구현하는 명령:
  `baseline freeze`   기준선 동결(골든 샘플 + manifest 생성)
  `baseline verify`   현재 코드가 기준선과 같은지 검사
  `contract convert`  V1.5 live JSON → V2 gold 레코드로 변환·검증
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime

from .. import SCHEMA_VERSION
from ..baseline.golden import (
    GOLDEN_PATH, build_golden, verify_golden, write_golden,
)
from ..baseline.manifest import (
    MANIFEST_PATH, build_manifest, verify_manifest, write_manifest,
)
from ..baseline.scanner_v15 import V15_PATH, project_root
from ..contracts.v15_signals import (
    convert_v15_snapshot, load_v15_snapshot, source_sha256,
)
from ..domain.errors import CvdObvError, exit_code_for
from ..domain.ids import KST, UTC, make_run_id
from ..domain.status import ExitCode, QualityStatus, SignalSource
from ..ingestion.benchmark_history import BenchmarkHistoryRepository
from ..ingestion.calendar_store import MarketCalendarRepository
from ..ingestion.kiwoom_benchmark import BENCHMARKS
from ..ingestion.daily_history import DailyHistoryRepository, load_index_codes
from ..labels.repository import DailyForwardReturnRepository
from ..labels.signal_labels import SignalLabelBuilder
from ..statistics.base_rates import DEFAULT_OUTCOMES, BaseRateComputer
from ..backtest.daily_gate import (
    PREREGISTERED, VARIANTS, DailyGateBacktester, DailyGateSpec,
)
from ..backtest.signal_gate import (
    SIGNAL_VARIANTS, SignalGateEvaluator, SignalGateSpec,
)
from ..backtest.engine import (
    SELECTION_POLICIES, BacktestEngine, BacktestSpec,
)
from ..backtest.metrics import BacktestMetrics
from ..statistics.ablation import VARIANTS as ABLATION_VARIANTS
from ..statistics.ablation import TOP_N, AblationRunner
from ..backtest.daily_signals import (
    GATE_VARIANTS as DAILY_GATE_VARIANTS, DailyPanelSignalEmitter,
)
from ..statistics.dependence import joint_block_bootstrap
from ..statistics.economic_gate import GateThresholds, evaluate_candidate
from ..statistics.collection_plan import (
    TRADING_DAYS_PER_YEAR, plan_rows,
)
from ..statistics.detectability import (
    DEFAULT_HORIZONS as FRONTIER_HORIZONS,
    DEFAULT_SIZES as FRONTIER_SIZES,
    DetectabilityFrontier,
)
from ..contracts.label_schema import ENTRY_RULES, RETIRED_HORIZONS
from ..features.repository import DailyFeatureRepository
from ..ingestion.minute_catchup import (
    DEFAULT_MIN_COMPLETENESS, DEFAULT_PROBE_CODE, MinuteCatchup,
)
from ..ingestion.minute_history import MinuteHistoryCache
from ..ingestion.krx_notices import KrxNoticeBoard
from ..ingestion.krx_daily_panel import KrxDailyPanel
from ..ingestion.krx_openapi import (
    DATA_START, MARKETS, KrxOpenApi, market_for)
from ..ingestion.security_master import SecurityMasterBuilder
from ..ingestion.universe_history import UniverseRepository, UniverseSnapshot
from ..replay.historical import HistoricalScannerReplay
from ..ingestion.universe_rebuild import (
    UniverseRebuilder, load_notice_sources, locate_anchors,
)
from ..ingestion.universe_snapshots import (
    INDEX_PARAMS, KiwoomUniverseSource, SnapshotStore, parse_krx_csv,
)

__all__ = ["build_parser", "main"]


def _emit(payload, log_json: bool):
    if log_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         default=str))
    return payload


def _horizons(text, log_json=False):
    """쉼표 목록을 정수로. 접은 horizon이 섞였으면 이유를 알려 준다.

    막지는 않는다 — 들여다보고 싶을 수 있다. 다만 **왜 기본 표에 없는지**를
    모른 채 결과를 읽는 일이 없게 한다.
    """
    values = [int(h) for h in text.split(",") if h.strip()]
    retired = [h for h in values if h in RETIRED_HORIZONS]
    if retired and not log_json:
        for h in retired:
            print(f"  ⚠ h={h}은 분석에서 접은 보유기간이다 — "
                  f"{RETIRED_HORIZONS[h]}")
        print("    계산은 하되, 이 값으로 결론을 세우지 말 것")
    return values


def _rel(path):
    try:
        return os.path.relpath(path, project_root()).replace("\\", "/")
    except ValueError:
        return path


# ─────────────────────────────────────────────────────────────
# baseline
# ─────────────────────────────────────────────────────────────

def cmd_baseline_freeze(args) -> int:
    golden = build_golden()
    manifest_preview = None
    written = []

    if args.dry_run:
        # 파일을 쓰지 않고 무엇이 바뀌는지만 본다.
        result = {
            "command": "baseline freeze", "dry_run": True,
            "golden_path": _rel(args.golden or GOLDEN_PATH),
            "manifest_path": _rel(args.manifest or MANIFEST_PATH),
            "golden_cases": sorted(golden["inputs"]),
            "golden_functions": sorted(golden["functions"]),
        }
        _emit(result, args.log_json)
        if not args.log_json:
            print("[dry-run] 파일을 쓰지 않았다.")
            print(f"  골든 대상: {result['golden_path']}")
            print(f"  manifest 대상: {result['manifest_path']}")
            print(f"  시나리오: {', '.join(result['golden_cases'])}")
        return ExitCode.OK

    gpath = write_golden(golden, args.golden)
    written.append(gpath)
    manifest_preview = build_manifest(golden_path=gpath)
    mpath = write_manifest(manifest_preview, args.manifest)
    written.append(mpath)

    result = {
        "command": "baseline freeze",
        "run_id": args.run_id,
        "schema_version": SCHEMA_VERSION,
        "baseline_file": _rel(V15_PATH),
        "baseline_sha256": manifest_preview["baseline"]["file_sha256"],
        "scanner_version": manifest_preview["baseline"]["scanner_version"],
        "market_rule_version": manifest_preview["baseline"]["market_rule_version"],
        "golden_content_sha256": manifest_preview["golden"]["content_sha256"],
        "manifest_sha256": manifest_preview["manifest_sha256"],
        "artifacts": [_rel(p) for p in written],
        "rows": {"golden_cases": len(golden["inputs"]),
                 "golden_functions": len(golden["functions"])},
    }
    _emit(result, args.log_json)
    if not args.log_json:
        print("기준선 동결 완료")
        print(f"  기준 파일: {result['baseline_file']}")
        print(f"  파일 해시: {result['baseline_sha256']}")
        print(f"  scanner_version: {result['scanner_version']}"
              f" / market_rule_version: {result['market_rule_version']}")
        for p in result["artifacts"]:
            print(f"  생성: {p}")
        print(f"  골든 시나리오 {result['rows']['golden_cases']}개 / "
              f"함수 {result['rows']['golden_functions']}종")
    return ExitCode.OK


def cmd_baseline_verify(args) -> int:
    golden = verify_golden(args.golden)
    manifest = verify_manifest(args.manifest)
    ok = golden["ok"] and manifest["ok"]
    result = {
        "command": "baseline verify",
        "ok": ok,
        "golden_ok": golden["ok"],
        "golden_diffs": golden["diffs"],
        "manifest_ok": manifest["ok"],
        "manifest_diffs": manifest["diffs"],
        "expected_golden_hash": golden["expected_hash"],
        "actual_golden_hash": golden["actual_hash"],
    }
    _emit(result, args.log_json)
    if not args.log_json:
        print(f"골든 샘플: {'일치' if golden['ok'] else '불일치'}")
        for d in golden["diffs"][:20]:
            print(f"  - {d}")
        if len(golden["diffs"]) > 20:
            print(f"  … 외 {len(golden['diffs']) - 20}건")
        print(f"manifest: {'일치' if manifest['ok'] else '불일치'}")
        for d in manifest["diffs"]:
            print(f"  - {d}")
    return ExitCode.OK if ok else ExitCode.SCHEMA_INCOMPATIBLE


# ─────────────────────────────────────────────────────────────
# contract
# ─────────────────────────────────────────────────────────────

def cmd_contract_convert(args) -> int:
    payload = load_v15_snapshot(args.snapshot)
    code_revision = args.code_revision or source_sha256(V15_PATH)
    result = convert_v15_snapshot(
        payload, code_revision=code_revision,
        signal_source=args.signal_source, run_id=args.run_id,
        source_path=args.snapshot)

    summary = result.summary()
    summary.update({
        "command": "contract convert",
        "source": _rel(args.snapshot),
        "code_revision": code_revision,
        "rows": {"scanner_runs": 1, "signals": len(result.signals),
                 "signal_features": len(result.features)},
        "dry_run": bool(args.dry_run or not args.out),
    })

    if args.out and not args.dry_run:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({
                "scanner_runs": [result.run_row],
                "signals": result.signals,
                "signal_features": result.features,
                "warnings": result.warnings,
            }, f, ensure_ascii=False, indent=2, sort_keys=True, default=str)
            f.write("\n")
        summary["artifacts"] = [_rel(args.out)]

    _emit(summary, args.log_json)
    if not args.log_json:
        print(f"변환 완료: {summary['source']}")
        print(f"  signals {summary['rows']['signals']}행 / "
              f"BUY_CANDIDATE {summary['buy_candidates']}건")
        print(f"  universe_snapshot_id: {summary['universe_snapshot_id']}")
        print(f"  config_hash: {summary['config_hash']}")
        for w in result.warnings:
            print(f"  ⚠ {w}")
        for p in summary.get("artifacts", []):
            print(f"  생성: {p}")
    return ExitCode.OK


# ─────────────────────────────────────────────────────────────
# daily-cache (Stage 1)
# ─────────────────────────────────────────────────────────────

def _parse_day(value):
    if not value:
        return None
    return date.fromisoformat(value)


def _resolve_codes(args):
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        return codes, "--codes"
    path = args.index_file or os.path.join(project_root(), "data",
                                           "index_stocks.json")
    codes, updated = load_index_codes(path, args.index)
    if args.limit_securities:
        codes = codes[:args.limit_securities]
    return codes, f"{_rel(path)} (updated {updated})"


def cmd_daily_cache_build(args) -> int:
    codes, origin = _resolve_codes(args)
    from ..ingestion.market_source import daily_source
    repo = DailyHistoryRepository(
        source=daily_source(args.source, adjusted=not args.raw_prices,
                            max_pages=args.max_pages))

    skipped = 0
    if args.repair_failed:
        from ..domain.ids import parse_security_id

        failed = dict(repo.failed_securities())
        for sid, info in repo.suspect_truncated_securities().items():
            failed.setdefault(sid, info)
        if not failed:
            if not args.log_json:
                print("불완전하게 수집된 종목이 없다 — 복구할 것이 없다.")
            _emit({"command": "daily-cache build", "repair": True,
                   "rows": 0, "status": "SUCCESS"}, args.log_json)
            return ExitCode.OK
        codes = [parse_security_id(sid)[1] for sid in sorted(failed)]
        origin = f"bronze.ingest_runs 불완전 기록 {len(codes)}종목"
        if not args.log_json:
            print(f"복구 대상 {len(codes)}종목 "
                  f"(수집 기록 실패 + 페이지 경계 절단 의심): "
                  f"{', '.join(codes[:12])}{' …' if len(codes) > 12 else ''}")
    elif args.skip_existing:
        from ..domain.ids import normalize_security_id

        have = repo.existing_securities()
        before = len(codes)
        codes = [c for c in codes
                 if normalize_security_id(c) not in have]
        skipped = before - len(codes)
        if not codes:
            if not args.log_json:
                print(f"이미 {skipped}종목이 저장돼 있다 — 받을 것이 없다.")
            _emit({"command": "daily-cache build", "skipped": skipped,
                   "rows": 0, "status": "SUCCESS"}, args.log_json)
            return ExitCode.OK

    def _progress(i, total, record):
        if args.log_json:
            return
        # 파일로 리다이렉트하면 stdout이 버퍼링돼 장시간 실행의 진행 상황이
        # 한참 뒤에야 보인다. 진행 로그는 매 줄 내보낸다.
        note = ""
        if record["status"] != "SUCCESS":
            note = f" {record['error_code']} (시도 {record['attempts']}회"
            note += ", 미저장)" if record["rows_written"] == 0 else ")"
        print(f"  [{i}/{total}] {record['security_id']} "
              f"{record['rows']}행 {record['first_date']}~{record['last_date']} "
              f"페이지 {record['pages']} {record['seconds']}초 "
              f"{record['status']}{note}", flush=True)

    if not args.log_json:
        print(f"일봉 수집 시작 — {len(codes)}종목 ({origin})"
              + (f" / 이미 있는 {skipped}종목 건너뜀" if skipped else ""))
        print(f"  기간: {args.start or '가용 전체'} ~ {args.end or '오늘'}"
              f" / 원천: {repo.source.SOURCE if hasattr(repo.source, 'SOURCE') else 'KIWOOM_KA10081'}"
              f" / 수정주가: {'아니오' if args.raw_prices else '예'}"
              f" / dry-run: {args.dry_run}", flush=True)

    summary = repo.build(codes, run_id=args.run_id,
                         start=_parse_day(args.start),
                         end=_parse_day(args.end),
                         on_progress=_progress, dry_run=args.dry_run,
                         max_pages=args.max_pages,
                         flush_every=args.flush_every, retries=args.retries,
                         index_id=(None if args.codes else args.index))

    payload = summary.to_dict()
    payload["command"] = "daily-cache build"
    payload["source_of_codes"] = origin
    payload["skipped_existing"] = skipped
    if args.log_json:
        payload["per_security"] = summary.per_security
    _emit(payload, args.log_json)
    if not args.log_json:
        print(f"완료: {payload['rows']}행 / 성공 {payload['succeeded']} "
              f"부분 {payload['partial']} 실패 {payload['failed']}")
        print(f"  소요 {payload['elapsed_sec']}초 "
              f"(종목당 {payload['sec_per_security']}초)")
        print(f"  최초 거래일: {payload['earliest_date']}")
        for path in payload["artifacts"]:
            print(f"  manifest: {path}")
        if args.dry_run:
            print("  [dry-run] Parquet을 쓰지 않았다.")

    if summary.status == QualityStatus.FAILED:
        return ExitCode.DATA_QUALITY_FAILED
    if summary.status == QualityStatus.PARTIAL:
        return ExitCode.PARTIAL_SUCCESS
    return ExitCode.OK


def cmd_daily_cache_validate(args) -> int:
    repo = DailyHistoryRepository()
    report = repo.validate()
    report["command"] = "daily-cache validate"
    _emit(report, args.log_json)
    if not args.log_json:
        print(f"{report['table']}: {report['rows']}행 / "
              f"{report['securities']}종목")
        print(f"  기간 {report['date_range'][0]} ~ {report['date_range'][1]}")
        print(f"  종목당 행수 min {report['rows_per_security']['min']} / "
              f"median {report['rows_per_security']['median']} / "
              f"max {report['rows_per_security']['max']}")
        print(f"  price_scope: {', '.join(report['price_scopes'])}")
        print(f"  OHLC 위반 {report['ohlc_violations']}건"
              f"(격리됨 {report['ohlc_violations_quarantined']} / "
              f"미격리 {report['ohlc_violations_unquarantined']}) / "
              f"격리 행 {report['quarantined_rows']} / "
              f"부분품질 {report['partial_rows']}행")
        print(f"  패널 거래일 {report['panel_trading_days']}일")
        if report["late_listed"]:
            print(f"  · 뒤늦게 상장된 종목 {len(report['late_listed'])}개(정상)")
        if report["ends_before_panel"]:
            print(f"  · 마지막 거래일이 패널보다 이른 종목 "
                  f"{len(report['ends_before_panel'])}개(상장폐지·거래정지 후보)")
        if report["coverage_gaps"]:
            print(f"  ⚠ 자기 구간을 다 못 채운 종목 "
                  f"{len(report['coverage_gaps'])}개: "
                  f"{list(report['coverage_gaps'])[:10]}")
        if report["quality_flags"]:
            print(f"  품질 플래그: {', '.join(report['quality_flags'])}")
        print(f"  상태: {report['status']}")
    if report["status"] == QualityStatus.FAILED:
        return ExitCode.DATA_QUALITY_FAILED
    if report["status"] == QualityStatus.PARTIAL:
        return ExitCode.PARTIAL_SUCCESS
    return ExitCode.OK


# ─────────────────────────────────────────────────────────────
# calendar / forward / baserate (Stage 1)
# ─────────────────────────────────────────────────────────────

def cmd_benchmark_build(args) -> int:
    from ..ingestion.market_source import benchmark_source
    repo = BenchmarkHistoryRepository(source=benchmark_source(args.source))
    ids = (args.benchmarks.split(",") if args.benchmarks
           else ["KRX:KOSPI200"])

    def _progress(i, total, record):
        if args.log_json:
            return
        flags = (" ⚠ " + ",".join(record["quality_flags"])
                 if record["quality_flags"] else "")
        print(f"  [{i}/{total}] {record['benchmark_id']} "
              f"(업종코드 {record['inds_cd']}) {record['rows']}행 "
              f"{record['first_date']}~{record['last_date']} "
              f"{record['seconds']}초 {record['status']}{flags}", flush=True)

    summary = repo.build(ids, run_id=args.run_id,
                         start=_parse_day(args.start),
                         end=_parse_day(args.end), dry_run=args.dry_run,
                         on_progress=_progress)
    summary["command"] = "benchmark build"
    _emit(summary, args.log_json)
    if not args.log_json:
        print(f"완료: {summary['rows']}행 / {summary['elapsed_sec']}초 "
              f"/ {summary['status']}")
        print("  주의: 가격지수다 — 배당 재투자를 반영하지 않는다")
        for path in summary["artifacts"]:
            print(f"  manifest: {path}")
    if summary["status"] != "SUCCESS":
        return ExitCode.PARTIAL_SUCCESS
    return ExitCode.OK


def cmd_calendar_build(args) -> int:
    repo = MarketCalendarRepository()
    summary = repo.build(run_id=args.run_id, dry_run=args.dry_run)
    summary["command"] = "calendar build"
    _emit(summary, args.log_json)
    if not args.log_json:
        print(f"거래 캘린더 {summary['trading_days']}일 "
              f"({summary['date_range'][0]} ~ {summary['date_range'][1]})")
        print(f"  출처: {summary['source']} "
              f"(종목 {summary['derived_from_securities']}개에서 파생)")
        print(f"  시장제도 버전: {', '.join(summary['market_rule_versions'])}")
        if summary.get("thin_days"):
            thin = summary["thin_days"]
            print(f"  · 30종목 미만으로 파생된 거래일 {len(thin)}건 "
                  f"({thin[0]['date']} ~ {thin[-1]['date']}) — "
                  f"휴장과 구별할 수 없다")
        if summary["large_gaps"]:
            print(f"  · 7일 넘는 간격 {len(summary['large_gaps'])}건 "
                  f"(연휴일 수 있어 자동 판정하지 않는다)")
        for path in summary["artifacts"]:
            print(f"  manifest: {path}")
        if args.dry_run:
            print("  [dry-run] Parquet을 쓰지 않았다.")
    return ExitCode.OK


def cmd_forward_build(args) -> int:
    repo = DailyForwardReturnRepository(
        horizons=[int(h) for h in args.horizons.split(",")],
        entry_rule=args.entry_rule)

    def _progress(i, total, sid, rows):
        if not args.log_json:
            print(f"  [{i}/{total}] {sid} {rows}행", flush=True)

    benchmark_series = None
    if args.benchmark:
        benchmark_series = BenchmarkHistoryRepository().series(args.benchmark)
        if not args.log_json:
            print(f"벤치마크: {args.benchmark} ({len(benchmark_series)}일)")

    summary = repo.build(run_id=args.run_id, start=_parse_day(args.start),
                         end=_parse_day(args.end),
                         securities=(args.securities.split(",")
                                     if args.securities else None),
                         dry_run=args.dry_run, on_progress=_progress,
                         benchmark_series=benchmark_series,
                         benchmark_id=args.benchmark)
    payload = summary.to_dict()
    payload["command"] = "forward build"
    _emit(payload, args.log_json)
    if not args.log_json:
        print(f"완료: {payload['rows']}행 / {payload['securities']}종목 "
              f"/ horizon {payload['horizons']}")
        print(f"  label_version: {payload['label_version']}")
        print(f"  비용 모델: {payload['cost_model_version']}")
        print(f"  캘린더 거래일: {payload['calendar_days']}")
        for status, count in sorted(payload["status_counts"].items()):
            print(f"  {status}: {count}행")
        for path in payload["artifacts"]:
            print(f"  manifest: {path}")
    return ExitCode.OK


def cmd_baserate_compute(args) -> int:
    from ..storage.parquet_store import ParquetStore

    computer = BaseRateComputer(ParquetStore(), n_resamples=args.resamples,
                                seed=args.seed)
    horizons = [int(h) for h in args.horizons.split(",")]
    outcomes = (args.outcomes.split(",") if args.outcomes
                else list(DEFAULT_OUTCOMES))
    rows = computer.compute_all(horizons, outcomes, run_id=args.run_id,
                                universe_id=args.universe,
                                start=_parse_day(args.start),
                                end=_parse_day(args.end))

    payload = {"command": "baserate compute", "rows": len(rows),
               "results": [{k: v for k, v in r.items() if k != "_detail"}
                           for r in rows]}
    if args.out and not args.dry_run:
        directory = os.path.dirname(os.path.abspath(args.out))
        os.makedirs(directory, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2, sort_keys=True,
                      default=str)
            f.write("\n")
        payload["artifacts"] = [_rel(args.out)]

    _emit(payload, args.log_json)
    if not args.log_json:
        print("무조건부 기준선 (다음 거래일 시가 진입, h거래일 종가 청산)")
        print()
        header = (f"{'outcome':<10}{'h':>4}{'확률':>9}{'95% CI':>20}"
                  f"{'관측':>11}{'거래일':>8}{'유효표본':>11}{'ICC':>7}")
        print(header)
        print("-" * 80)
        for r in rows:
            print(f"{r['outcome']:<10}{r['horizon_td']:>4}"
                  f"{r['probability'] * 100:>8.2f}%"
                  f"{r['ci_low'] * 100:>11.2f}~{r['ci_high'] * 100:<8.2f}"
                  f"{r['raw_n']:>11,}{r['trading_date_n']:>8,}"
                  f"{r['effective_n']:>11,.0f}{r['icc']:>7.3f}")
        print()
        first = rows[0]
        print(f"  CI 방법: {first['ci_method']} (두 방법 중 넓은 쪽)")
        print(f"  군집 키: {first['cluster_key']} / "
              f"블록 길이 규칙: max(horizon, 2) / seed {first['seed']}")
        ratio = first["_detail"]["naive_ci_width_ratio"]
        if ratio is not None:
            print(f"  독립 가정 CI 대비 폭: {ratio:.1f}배 — 종목×날짜 행 수를 "
                  f"표본수로 쓰면 이만큼 좁아진다")
        degenerate = [r for r in rows if r["_detail"]["degenerate_outcome"]]
        if degenerate:
            print(f"  ⚠ 결과가 전부 같은 값인 조합 {len(degenerate)}건 — "
                  f"표본을 확인할 것")
        for path in payload.get("artifacts", []):
            print(f"  생성: {path}")
    return ExitCode.OK


# ─────────────────────────────────────────────────────────────
# features / daily-gate (Stage 2)
# ─────────────────────────────────────────────────────────────

def cmd_features_build(args) -> int:
    repo = DailyFeatureRepository(obv_lookback=args.obv_lookback)

    def _progress(i, total, sid, rows):
        if not args.log_json:
            print(f"  [{i}/{total}] {sid} {rows}행", flush=True)

    summary = repo.build(run_id=args.run_id, start=_parse_day(args.start),
                         end=_parse_day(args.end),
                         securities=(args.securities.split(",")
                                     if args.securities else None),
                         dry_run=args.dry_run, on_progress=_progress)
    summary["command"] = "features build"
    _emit(summary, args.log_json)
    if not args.log_json:
        print(f"완료: {summary['rows']}행 / {summary['securities']}종목")
        print(f"  feature_version: {summary['feature_version']}")
        print(f"  품질 FAILED 일봉 제외: {summary['excluded_failed_bars']}행")
        for status, count in sorted(summary["status_counts"].items()):
            print(f"  {status}: {count}행")
        rates = summary["gate_rates"]
        print(f"  조건 충족률 — 가격 {rates['cond_price']:.1%} / "
              f"OBV {rates['cond_obv']:.1%} / "
              f"유동성 {rates['cond_liquidity']:.1%} / "
              f"3조건 모두 {rates['gate_all']:.1%}")
        for path in summary["artifacts"]:
            print(f"  manifest: {path}")
    return ExitCode.OK


def _print_gate_table(rows):
    by_outcome = {}
    skipped = [r for r in rows if r.get("variant") is None]
    for row in rows:
        if row.get("variant") is None:
            continue
        by_outcome.setdefault(row["outcome"], []).append(row)
    segmented = any(r.get("segment") for group in by_outcome.values()
                    for r in group)

    for outcome, group in by_outcome.items():
        print()
        base = next((r.get("base_variant") for r in group
                     if r.get("base_variant")), "ALL")
        print(f"[{outcome}] 조건별 상승확률 — 기준 {base} 대비")
        head = f"{'세그먼트':<14}" if segmented else ""
        print(head + f"{'조합':<12}{'h':>3}{'확률':>9}{'확률 ALL대비':>16}"
              f"{'순수익':>10}{'수익 ALL대비':>18}{'선택률':>8}{'유효표본':>10}")
        print("-" * (96 + (14 if segmented else 0)))
        for row in sorted(group, key=lambda r: (str(r.get("segment") or ""),
                                                r["horizon_td"],
                                                r["variant"])):
            delta = row.get("delta_vs_all")
            if delta is None:
                prob_delta = "        기준선"
            else:
                sig = "" if row["delta_ci_low"] * row["delta_ci_high"] > 0                     else " ns"
                prob_delta = f"{delta * 100:+9.2f}%p{sig:<3}"
            rdelta = row.get("delta_return_net")
            if rdelta is None:
                ret_delta = "          기준선"
            else:
                rsig = "" if (row["delta_return_ci_low"]
                              * row["delta_return_ci_high"]) > 0 else " ns"
                ret_delta = (f"{rdelta * 100:+8.3f}%p"
                             f"[{row['delta_return_ci_low'] * 100:+.3f},"
                             f"{row['delta_return_ci_high'] * 100:+.3f}]{rsig}")
            mark = "" if row["preregistered"] else "*"
            q = row.get("delta_return_q_value")
            if q is not None and row.get("fdr_significant"):
                ret_delta += "+"          # 다중비교 보정 후에도 살아남음
            label = (f"{str(row.get('segment') or ''):<14}" if segmented else "")
            print(label + f"{row['variant'] + mark:<12}{row['horizon_td']:>3}"
                  f"{row['probability'] * 100:>8.2f}%"
                  f"{prob_delta:>16}"
                  f"{row['mean_return_net'] * 100:>9.3f}%"
                  f"{ret_delta:>30}"
                  f"{row['coverage']:>8.1%}{row['effective_n']:>10,.0f}")
    print()
    print("  * = 사전 등록되지 않은 탐색적 조합. 다중비교 보정 없이 "
          "유의성을 주장하지 말 것")
    print("  ns = 95% 신뢰구간이 0을 포함한다(차이를 주장할 수 없다)")
    print("  차이는 같은 날짜끼리 짝지어(paired) 계산했다")
    print("  + = BH-FDR 보정(q<=0.05) 후에도 유의 — p값은 CI에서 정규근사로 "
          "되돌린 값이라 근사다")
    for row in skipped:
        print(f"  ⚠ {row.get('segment')} h={row.get('horizon_td')} 건너뜀: "
              f"{row.get('status_reason')}")


def cmd_daily_gate_backtest(args) -> int:
    from ..storage.parquet_store import ParquetStore

    spec = DailyGateSpec(
        variants=(args.variants.split(",") if args.variants else None),
        horizons=_horizons(args.horizons, args.log_json),
        outcomes=args.outcomes.split(","),
        feature_version=args.feature_version,
        start=_parse_day(args.start), end=_parse_day(args.end),
        universe_id=args.universe, n_resamples=args.resamples,
        seed=args.seed, segment=args.segment, segment_by=args.segment_by,
        cost_multiplier=args.cost_multiplier,
        base_variant=args.base_variant)

    unknown = [v for v in spec.variants if v not in VARIANTS]
    if unknown:
        raise CvdObvError(
            f"알 수 없는 조합: {unknown} (가능: {sorted(VARIANTS)})")

    membership = None
    if not args.no_membership:
        # 기본은 **그날 실제 구성종목**만 본다. 이걸 끄면 모집단이 "한 번이라도
        # 지수에 있었던 종목 전체"가 되어 편출 뒤 수익률까지 섞인다.
        membership = UniverseRepository().membership_intervals(args.universe)

    backtester = DailyGateBacktester(ParquetStore())
    rows = backtester.evaluate(spec, run_id=args.run_id, membership=membership)

    payload = {"command": "daily-gate backtest", "rows": len(rows),
               "spec": spec.to_dict(), "results": rows,
               "membership_applied": membership is not None,
               "membership_dropped_rows":
                   getattr(backtester, "membership_dropped", 0)}
    if args.out and not args.dry_run:
        directory = os.path.dirname(os.path.abspath(args.out))
        os.makedirs(directory, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True,
                      default=str)
            f.write("\n")
        payload["artifacts"] = [_rel(args.out)]

    _emit(payload, args.log_json)
    if not args.log_json:
        first = rows[0]
        print(f"Daily Gate 백테스트 — {first['universe_id']} / "
              f"evidence_tier={first['evidence_tier']}")
        print(f"  feature_version: {first['feature_version']}")
        print(f"  label_version:   {first['label_version']}")
        _print_gate_table(rows)
        if membership is not None:
            print(f"  유니버스: {args.universe} 이력 적용 — 그날 구성종목이 "
                  f"아니었던 {payload['membership_dropped_rows']:,}행 제외")
        else:
            print("  ⚠ 유니버스 이력 필터를 껐다 — 모집단이 '한 번이라도 지수에 "
                  "있었던 종목 전체'다. 편출 뒤 수익률이 섞여 있다")
        for path in payload.get("artifacts", []):
            print(f"  생성: {path}")
    return ExitCode.OK


# ─────────────────────────────────────────────────────────────
# signal-label / signal-gate (Stage 6)
# ─────────────────────────────────────────────────────────────

def cmd_signal_label_build(args) -> int:
    """신호에 forward return을 붙인다.

    수익률을 다시 계산하지 않는다 — 무조건부 패널에서 그대로 가져온다.
    그래서 기준선과 신호의 숫자가 같은 계산에서 나온다.
    """
    from ..storage.parquet_store import ParquetStore

    builder = SignalLabelBuilder(
        ParquetStore(), horizons=[int(h) for h in args.horizons.split(",")])
    summary = builder.build(
        run_id=args.run_id, index_id=args.index,
        start=_parse_day(args.start), end=_parse_day(args.end),
        signal_source=args.signal_source, dry_run=args.dry_run)

    payload = summary.to_dict()
    payload["command"] = "signal-label build"
    _emit(payload, args.log_json)
    if not args.log_json:
        print(f"신호 label {payload['rows']:,}행 "
              f"(신호 {payload['signals']:,}건 x horizon {payload['horizons']})")
        print(f"  구간: {payload['start']} ~ {payload['end']}")
        print(f"  label_version: {payload['label_version']}")
        print(f"  거래 캘린더 {payload['calendar_days']:,}일로 진입·청산일 "
              f"{payload['calendar_checked_rows']:,}행 대조")
        for status, count in sorted(payload["status_counts"].items()):
            print(f"  {status}: {count:,}행")
        if payload["missing_panel_rows"]:
            print(f"  ⚠ 패널에 없는 (종목,날짜) "
                  f"{payload['missing_panel_rows']:,}행 — "
                  f"ENTRY_UNAVAILABLE로 남겼다")
        print("  신호 상태별 label 상태:")
        for name, counts in sorted(payload["by_signal_status"].items()):
            detail = " / ".join(f"{k} {v:,}" for k, v in sorted(counts.items()))
            print(f"    {name:<18} {detail}")
        for path in payload["artifacts"]:
            print(f"  manifest: {path}")
        if args.dry_run:
            print("  [dry-run] Parquet을 쓰지 않았다.")
    return ExitCode.OK


def _print_signal_table(rows):
    """조합별 결과. Stage 2 표와 같은 규칙으로 읽는다."""
    ic_rows = [r for r in rows if r.get("variant") == "SCORE_IC"]
    skipped = [r for r in rows if r.get("variant") is None]
    by_outcome = {}
    for row in rows:
        if row.get("variant") in (None, "SCORE_IC"):
            continue
        by_outcome.setdefault(row["outcome"], []).append(row)
    segmented = any(r.get("segment") for group in by_outcome.values()
                    for r in group)

    for outcome, group in by_outcome.items():
        print()
        print(f"[{outcome}] 선택 규칙별 성과 — 그날 전체(ALL) 대비")
        head = f"{'세그먼트':<16}" if segmented else ""
        print(head + f"{'조합':<15}{'h':>3}{'확률':>9}{'확률 ALL대비':>16}"
              f"{'순수익':>10}{'수익 ALL대비':>18}{'선택률':>8}{'유효표본':>10}")
        print("-" * (99 + (16 if segmented else 0)))
        order = list(SIGNAL_VARIANTS)
        for row in sorted(group, key=lambda r: (str(r.get("segment") or ""),
                                                r["horizon_td"],
                                                order.index(r["variant"]))):
            delta = row.get("delta_vs_all")
            if delta is None:
                prob_delta = "        기준선"
            else:
                sig = ("" if row["delta_ci_low"] * row["delta_ci_high"] > 0
                       else " ns")
                if row.get("delta_vs_all_fdr_significant"):
                    sig = "+" + sig
                prob_delta = f"{delta * 100:+9.2f}%p{sig:<4}"
            rdelta = row.get("delta_return_net")
            if rdelta is None:
                ret_delta = "          기준선"
            else:
                if row.get("delta_return_overlap_unresolved"):
                    # 구간이 보유기간 중첩을 반영하지 못했다. 숫자는 남기되
                    # 유의성은 말하지 않는다.
                    rsig = " ?"
                else:
                    rsig = ("" if (row["delta_return_ci_low"]
                                   * row["delta_return_ci_high"]) > 0
                            else " ns")
                ret_delta = (f"{rdelta * 100:+8.3f}%p"
                             f"[{row['delta_return_ci_low'] * 100:+.3f},"
                             f"{row['delta_return_ci_high'] * 100:+.3f}]{rsig}")
                if (row.get("delta_return_net_fdr_significant")
                        and not row.get("delta_return_overlap_unresolved")):
                    ret_delta += "+"
            label = (f"{str(row.get('segment') or ''):<16}"
                     if segmented else "")
            print(label + f"{row['variant']:<15}{row['horizon_td']:>3}"
                  f"{row['probability'] * 100:>8.2f}%"
                  f"{prob_delta:>16}"
                  f"{row['mean_return_net'] * 100:>9.3f}%"
                  f"{ret_delta:>30}"
                  f"{row['coverage']:>8.1%}{row['effective_n']:>10,.0f}")

    if ic_rows:
        print()
        print("[단조성] 날짜별 (점수, 순수익) 순위상관의 평균 — rank IC")
        print(f"{'세그먼트':<16}{'h':>3}{'IC':>10}{'95% 구간':>24}"
              f"{'양(+)인 날':>12}{'거래일':>8}")
        print("-" * 74)
        for row in sorted(ic_rows, key=lambda r: (str(r.get("segment") or ""),
                                                  r["horizon_td"])):
            if row.get("rank_ic") is None:
                print(f"{str(row.get('segment') or ''):<16}"
                      f"{row['horizon_td']:>3}   {row.get('status_reason')}")
                continue
            span = (f"[{row['rank_ic_ci_low']:+.4f},"
                    f"{row['rank_ic_ci_high']:+.4f}]")
            if row.get("rank_ic_overlap_unresolved"):
                mark = " ?"
            else:
                mark = ("" if (row["rank_ic_ci_low"]
                               * row["rank_ic_ci_high"]) > 0 else " ns")
                if row.get("rank_ic_fdr_significant"):
                    mark += "+"
            print(f"{str(row.get('segment') or ''):<16}{row['horizon_td']:>3}"
                  f"{row['rank_ic']:>+10.4f}{span + mark:>24}"
                  f"{row['rank_ic_positive_days']:>10}일"
                  f"{row['trading_date_n']:>8}")

    print()
    print("  ns = 95% 신뢰구간이 0을 포함한다(차이를 주장할 수 없다)")
    print("  차이는 같은 날짜끼리 짝지어(paired) 계산했다")
    print("  + = BH-FDR 보정(q<=0.05) 후에도 유의 — p값은 CI에서 정규근사로 "
          "되돌린 값이라 근사다")
    print("  IC가 0에 가까우면 점수는 순서를 담고 있지 않다는 뜻이다")
    print("  FDR 보정은 가족별로 따로 건다 — 짝지음 차이와 순위상관은 "
          "다른 질문이다")
    print("  ? = 거래일 수가 보유기간보다 적어 중첩을 반영한 구간을 만들 수 "
          "없었다. 점 추정만 읽고 유의성은 읽지 말 것")
    for row in skipped:
        print(f"  ⚠ {row.get('segment')} h={row.get('horizon_td')} 건너뜀: "
              f"{row.get('status_reason')}")


def cmd_signal_gate_evaluate(args) -> int:
    from ..storage.parquet_store import ParquetStore

    spec = SignalGateSpec(
        variants=(args.variants.split(",") if args.variants else None),
        horizons=_horizons(args.horizons, args.log_json),
        outcomes=args.outcomes.split(","), index_id=args.index,
        start=_parse_day(args.start), end=_parse_day(args.end),
        n_resamples=args.resamples, seed=args.seed,
        segment_by=args.segment_by, signal_source=args.signal_source)

    unknown = [v for v in spec.variants if v not in SIGNAL_VARIANTS]
    if unknown:
        raise CvdObvError(
            f"알 수 없는 조합: {unknown} (가능: {sorted(SIGNAL_VARIANTS)})")

    evaluator = SignalGateEvaluator(ParquetStore())
    rows = evaluator.evaluate(spec, run_id=args.run_id)

    payload = {"command": "signal-gate evaluate", "rows": len(rows),
               "spec": spec.to_dict(), "results": rows,
               "data_insufficient_n": evaluator.data_insufficient_n}
    if args.out and not args.dry_run:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True,
                      default=str)
            f.write("\n")
        payload["artifacts"] = [_rel(args.out)]

    _emit(payload, args.log_json)
    if not args.log_json:
        first = next(r for r in rows if r.get("variant"))
        print(f"신호 성과 평가 — {first['index_id']} / "
              f"evidence_tier={first['evidence_tier']}")
        print(f"  label_version: {first['label_version']}")
        print(f"  DATA_INSUFFICIENT {evaluator.data_insufficient_n:,}건 제외 "
              f"(순위가 없어 같은 자리에 놓을 수 없다)")
        _print_signal_table(rows)
        for path in payload.get("artifacts", []):
            print(f"  생성: {path}")
    return ExitCode.OK


# ─────────────────────────────────────────────────────────────
# backtest (Stage 7)
# ─────────────────────────────────────────────────────────────

def _print_backtest(summaries, metrics_by_id):
    """정책별 한 줄 비교.

    총수익률만 보면 상승장에서는 무엇이든 좋아 보인다. 그래서 **지수 대비**와
    **기준선 대비 적중률**을 같은 줄에 둔다.
    """
    print()
    print(f"{'정책':<12}{'h':>3}{'거래':>6}{'노출':>7}{'총수익':>10}{'지수':>10}"
          f"{'초과':>10}{'MDD':>9}{'Sharpe':>8}{'적중률':>8}"
          f"{'기준선대비 적중률 [95%]':>30}")
    print("-" * 125)
    for got in summaries:
        rows = metrics_by_id.get(got["experiment_id"], [])
        full = {r["metric_name"]: r for r in rows
                if r["metric_scope"] == "FULL"}
        edge = full.get("hit_rate_edge_vs_base_rate") or {}
        value = edge.get("metric_value")
        if value is None:
            edge_text = "           —"
        else:
            low, high = edge.get("ci_low"), edge.get("ci_high")
            mark = "" if (low is not None and low * high > 0) else " ns"
            edge_text = (f"{value * 100:+7.2f}%p"
                         f"[{low * 100:+.2f},{high * 100:+.2f}]{mark}")
        sharpe = (full.get("sharpe_ratio") or {}).get("metric_value")
        print(f"{got['selection_policy']:<12}{got['horizon_td']:>3}"
              f"{got['trades']:>6}"
              f"{(got['mean_exposure'] or 0) * 100:>6.0f}%"
              f"{got['total_return'] * 100:>9.1f}%"
              f"{(got['benchmark_return'] or 0) * 100:>9.1f}%"
              f"{(got['total_return'] - (got['benchmark_return'] or 0)) * 100:>9.1f}%"
              f"{got['max_drawdown'] * 100:>8.1f}%"
              f"{(sharpe if sharpe is not None else float('nan')):>8.2f}"
              f"{(got['hit_rate'] or 0) * 100:>7.1f}%"
              f"{edge_text:>30}")
    print()
    print("  초과 = 총수익 − 지수. 이 구간 지수가 크게 올라 초과가 음수여도 "
          "총수익은 양수일 수 있다")
    print("  ⚠ 노출이 다르면 총수익·MDD·Sharpe를 정책끼리 비교할 수 없다 "
          "— 절반이 현금인 전략은 변동성이 낮아 Sharpe가 좋아 보인다.")
    print("    노출과 무관하게 비교할 수 있는 것은 적중률과 거래당 수익이다")
    print("  ns = 95% 신뢰구간이 0을 포함한다(차이를 주장할 수 없다)")
    print("  구간은 군집 robust와 블록 부트스트랩 중 넓은 쪽이다 "
          "(보유기간 중첩 반영)")


def cmd_backtest_run(args) -> int:
    """선택·체결 정책을 넣어 포트폴리오를 돌린다.

    신호 생성과 체결은 분리돼 있다. 진입가는 Stage 6 label의 다음 거래일
    시가이고, 신호 시각 이전 체결이 있으면 예외로 끝난다.
    """
    from ..storage.parquet_store import ParquetStore

    policies = (args.policies.split(",") if args.policies
                else ["BUY_ALL"])
    unknown = [p for p in policies if p not in SELECTION_POLICIES]
    if unknown:
        raise CvdObvError(
            f"알 수 없는 선택 정책: {unknown} "
            f"(가능: {sorted(SELECTION_POLICIES)})")

    _horizons(str(args.horizon), args.log_json)
    store = ParquetStore()
    engine = BacktestEngine(store)
    summaries, metrics_by_id, all_metrics, artifacts = [], {}, [], []

    for policy in policies:
        spec = BacktestSpec(
            selection_policy=policy, horizon_td=args.horizon,
            max_positions=args.max_positions,
            allow_overlap=args.allow_overlap,
            liquidity_cap_pct=(None if args.no_liquidity_cap
                               else args.liquidity_cap),
            initial_capital=args.capital, index_id=args.index,
            start=_parse_day(args.start), end=_parse_day(args.end),
            oos_start=_parse_day(args.oos_start),
            max_entries_per_day=(None if args.single_cohort
                                 else args.max_entries_per_day),
            signal_source=args.signal_source,
            random_seed=args.random_seed)
        result = engine.run(spec, run_id=args.run_id, dry_run=args.dry_run)
        rows = BacktestMetrics(store, result, n_resamples=args.resamples,
                               seed=args.seed).compute(run_id=args.run_id)
        if not args.dry_run:
            write = BacktestMetrics(store, result).write(rows, args.run_id)
            artifacts.append(write.manifest_path)
            artifacts += result.writes
        summaries.append(result.summary())
        metrics_by_id[result.experiment_id] = rows
        all_metrics += rows
        if not args.log_json:
            got = summaries[-1]
            print(f"  {policy}: 거래 {got['trades']} / "
                  f"총수익 {got['total_return']:.2%} / "
                  f"원장 오차 {got['ledger_gap']:.2e}", flush=True)

    payload = {"command": "backtest run", "experiments": summaries,
               "metrics": all_metrics, "artifacts": artifacts}
    if args.out and not args.dry_run:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True,
                      default=str)
            f.write("\n")
        payload["artifacts"] = artifacts + [_rel(args.out)]

    _emit(payload, args.log_json)
    if not args.log_json:
        first = summaries[0]
        print()
        print(f"백테스트 — {args.index} / 초기자금 {args.capital:,.0f}원 / "
              f"슬롯 {args.max_positions}개")
        cap = (None if args.single_cohort
               else max(1, round(args.max_positions / max(args.horizon, 1))))
        print(f"  하루 진입 상한: "
              f"{'없음(단일 코호트)' if cap is None else f'{cap}종목 (슬롯/h)'}")
        if cap is not None:
            # 상한보다 큰 N을 고르는 정책들은 서로 같아진다. 표에 같은 줄이
            # 나오는 이유를 감추지 않는다.
            collapsed = (["BUY_ALL"] if "BUY_ALL" in policies else []) + [
                p for p in policies if p.startswith("BUY_TOP")
                and int(p[len("BUY_TOP"):]) > cap]
            if len(collapsed) > 1:
                need = max(int(p[len("BUY_TOP"):]) for p in collapsed
                           if p.startswith("BUY_TOP")) * args.horizon
                print(f"  ⚠ 하루 {cap}종목까지만 사므로 "
                      f"{', '.join(collapsed)}가 **같은 전략으로 무너진다.** "
                      f"구별하려면 슬롯이 {need}개 이상이어야 한다")
        _print_backtest(summaries, metrics_by_id)
        print(f"  원장 대조: 전 실험 통과 "
              f"(최대 오차 {max(s['ledger_gap'] for s in summaries):.2e})")
        for path in payload.get("artifacts", []):
            print(f"  생성: {_rel(path)}")
        if args.dry_run:
            print("  [dry-run] Parquet을 쓰지 않았다.")
    return ExitCode.OK


# ─────────────────────────────────────────────────────────────
# power frontier (사전 등록 C — H3)
# ─────────────────────────────────────────────────────────────

def cmd_power_plan(args) -> int:
    """확증에 **새로** 얼마가 필요한가.

    이미 본 표본을 다시 세면 같은 우연을 두 번 세는 것이다. 그래서 누적이
    아니라 추가분으로 계산한다.
    """
    import glob

    candidates = []
    for path in sorted(glob.glob(args.results)):
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        base = (payload.get("spec") or {}).get("base_variant", "?")
        for row in payload.get("results", []):
            value = row.get("delta_return_net")
            low, high = (row.get("delta_return_ci_low"),
                         row.get("delta_return_ci_high"))
            if value is None or low is None or high <= low:
                continue
            if args.only and row.get("variant") not in args.only.split(","):
                continue
            candidates.append({
                "name": f"{row['variant']} vs {base} h={row['horizon_td']}",
                "effect": value,
                "se": (high - low) / (2.0 * 1.959963985),
                "n": row.get("delta_return_dates_n")
                     or row.get("trading_date_n"),
                "note": f"q={row.get('delta_return_net_q_value')}",
            })
    if not candidates:
        raise CvdObvError(
            f"확증할 후보를 찾지 못했다: {args.results} "
            "(--only로 조합을 지정했는지 확인하라)")

    rows = plan_rows(candidates, alpha=args.alpha, power=args.power,
                     days_per_year=args.days_per_year)
    payload = {"command": "power plan", "results": rows,
               "alpha": args.alpha, "power": args.power,
               "days_per_year": args.days_per_year}
    if args.out and not args.dry_run:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True,
                      default=str)
            f.write("\n")
        payload["artifacts"] = [_rel(args.out)]

    _emit(payload, args.log_json)
    if not args.log_json:
        print(f"확증 표본 계획 — 유의수준 {args.alpha}, 검정력 {args.power}")
        print(f"  {'후보':<34}{'효과':>11}{'SE':>10}{'발견 표본':>10}"
              f"{'양측 필요':>12}{'년':>7}{'단측 필요':>12}{'년':>7}")
        print("  " + "-" * 103)
        for row in rows:
            print(f"  {row['name']:<34}{row['effect'] * 100:>10.4f}%p"
                  f"{row['standard_error'] * 100:>9.4f}%p"
                  f"{row['discovery_n']:>10,}"
                  f"{row['required_new_two_sided']:>12,}"
                  f"{row['years_two_sided']:>7.1f}"
                  f"{row['required_new_one_sided']:>12,}"
                  f"{row['years_one_sided']:>7.1f}")
        print()
        print("  '필요'는 **새로** 모아야 하는 거래일 수다. 이미 본 표본을 "
              "다시 세면 같은 우연을 두 번 센다")
        print("  단측은 **방향을 미리 걸었을 때**만 쓸 수 있다. 반대로 나오면 "
              "그것으로 끝이고, 사후에 양측으로 바꿔 읽으면 안 된다")
        for path in payload.get("artifacts", []):
            print(f"  생성: {path}")
    return ExitCode.OK


def cmd_power_frontier(args) -> int:
    """이 설계로 **애초에 무엇을 잴 수 있었나**.

    가설 검정이 아니라 설계의 성질을 재는 것이다. 데이터를 보고 가설을 고르는
    것과 무관하므로 다중비교 문제도 없다.
    """
    from ..storage.parquet_store import ParquetStore

    horizons = _horizons(args.horizons, args.log_json)
    sizes = [int(n) for n in args.sizes.split(",") if n.strip()]
    targets = [int(n) for n in args.project_to.split(",") if n.strip()]

    frontier = DetectabilityFrontier(
        AblationRunner(ParquetStore(), index_id=args.index,
                       signal_source="REPLAY"),
        n_resamples=args.resamples, seed=args.seed)
    rows = frontier.run(horizons=horizons, sizes=sizes, project_to=targets)

    payload = {"command": "power frontier", "results": rows,
               "horizons": horizons, "sizes": sizes, "project_to": targets,
               "seed": args.seed, "n_resamples": args.resamples}
    if args.out and not args.dry_run:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True,
                      default=str)
            f.write("\n")
        payload["artifacts"] = [_rel(args.out)]

    _emit(payload, args.log_json)
    if not args.log_json:
        print("검출 가능 구간 — 이 설계로 잡을 수 있는 최소 효과")
        head = "".join(f"{f'MDE@{t:,}일':>14}" for t in targets)
        print(f"  {'h':>3}{'N':>5}{'거래일':>8}{'관측 edge':>12}"
              f"{'MDE80':>11}{'관측/MDE':>10}{head}{'':>3}")
        print("  " + "-" * (49 + 14 * len(targets) + 3))
        for row in rows:
            if row["status"] != "SUCCESS":
                print(f"  {row['horizon_td']:>3}{row['size']:>5}   "
                      f"{row['status_reason']}")
                continue
            cells, reachable = "", False
            for t in targets:
                value = row.get(f"mde_at_{t}")
                cells += (f"{value * 100:>13.3f}%p" if value else f"{'—':>14}")
                if value and abs(row["edge"]) >= value:
                    reachable = True
            mark = " ←" if reachable else ""
            print(f"  {row['horizon_td']:>3}{row['size']:>5}{row['dates']:>8}"
                  f"{row['edge'] * 100:>11.3f}%p{row['mde_80'] * 100:>10.3f}%p"
                  f"{row['observed_over_mde']:>9.2f}x{cells}{mark}")
        print()
        print("  MDE80 = 유의수준 5%·검정력 80%에서 잡을 수 있는 최소 효과")
        print("  MDE@n일 = 날짜가 n개였다면 얼마까지 잡히나 (SE ∝ 1/√n)")
        print("  ← = 그 표본 크기라면 **지금 관측된 크기**가 잡힌다는 뜻이다.")
        print("      관측치가 그 크기로 유지된다는 보장은 없다 — 설계의 한계를")
        print("      재는 표이지 수익을 약속하는 표가 아니다")
        for path in payload.get("artifacts", []):
            print(f"  생성: {path}")
    return ExitCode.OK


# ─────────────────────────────────────────────────────────────
# ablation (Stage 8)
# ─────────────────────────────────────────────────────────────

def _print_ablation(rows, metrics):
    """성분을 뺐을 때 무엇이 달라졌나.

    두 가지를 나눠 읽어야 한다 — **변형 자체가 기준선보다 나은가**(edge)와
    **BASE에서 그 성분을 빼니 달라졌는가**(delta)는 다른 질문이다.
    """
    for metric in metrics:
        group = [r for r in rows if r["metric_name"] == metric]
        if not group:
            continue
        print()
        print(f"[{metric}]")
        print(f"  {'변형':<18}{'edge':>10}{'MDE80':>10}{'판정':>20}"
              f"{'BASE 대비 delta':>18}{'95% 구간':>22}{'q':>8}{'하루 n':>8}")
        print("  " + "-" * 112)
        for row in group:
            delta, q = row["delta"], row["adjusted_p_value"]
            if delta is None:
                span, mark, shown = "", "", "—"
            else:
                span = (f"[{row['ci_low'] * 100:+.3f},"
                        f"{row['ci_high'] * 100:+.3f}]")
                mark = ""
                if row["ci_low"] * row["ci_high"] > 0:
                    mark = "+" if (q is not None and q <= 0.05) else "*"
                shown = f"{delta * 100:+.3f}%p"
            print(f"  {row['variant_id']:<18}"
                  f"{row['variant_value'] * 100:+9.3f}%p"
                  f"{(row['mde_80'] or 0) * 100:9.3f}%p"
                  f"{row['edge_verdict']:>20}"
                  f"{shown:>18}{span + mark:>22}"
                  f"{'—' if q is None else f'{q:.3f}':>8}"
                  f"{row['mean_selection_size']:>8.1f}")
    print()
    print("  edge  = 변형이 고른 것 − 그날 전체. 판정은 이 값이 기준선과 "
          "구분되는가다")
    print("  delta = BASE에서 그 성분을 뺐을 때 edge가 얼마나 움직였나 "
          "(같은 날짜끼리 짝지음)")
    print("  UNDERPOWERED = 관측 효과가 MDE80보다 작다 — **이 데이터로는 "
          "판정할 수 없다**는 뜻이지")
    print("                 '효과가 없다'는 뜻이 아니다")
    print("  * = 구간이 0을 비켰으나 FDR 보정 후 탈락 / + = 보정 후에도 유의")
    print("  하루 n이 전체에 가까우면 edge는 0에 가까워지고 구간은 좁아진다 "
          "— 크기를 같이 보라")


def cmd_ablation_run(args) -> int:
    """성분을 빼 보고, 동시에 **얼마나 작은 효과까지 잡을 수 있었는지** 낸다.

    되짚은 점수·Gate가 V1.5와 같은지 먼저 확인하고, 다르면 거기서 멈춘다 —
    조립 규칙이 틀렸다면 ablation 전부가 무의미하다.
    """
    from ..storage.parquet_store import ParquetStore

    _horizons(str(args.horizon), args.log_json)
    runner = AblationRunner(
        ParquetStore(), horizon_td=args.horizon, index_id=args.index,
        n_resamples=args.resamples, seed=args.seed,
        signal_source=args.signal_source, config_hash=args.config_hash)
    summary = runner.run(
        run_id=args.run_id,
        variants=(args.variants.split(",") if args.variants else None),
        start=_parse_day(args.start), end=_parse_day(args.end),
        dry_run=args.dry_run)

    payload = {k: v for k, v in summary.items() if k != "rows"}
    payload["command"] = "ablation run"
    payload["results"] = summary["rows"]
    if args.out and not args.dry_run:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True,
                      default=str)
            f.write("\n")
        payload.setdefault("artifacts", []).append(_rel(args.out))

    _emit(payload, args.log_json)
    if not args.log_json:
        check = summary["verification"]
        print(f"Ablation — {args.index} / h={args.horizon} / "
              f"{summary['trading_days']}거래일")
        print(f"  되짚기 검증: 점수 {check['score']['rows']:,}행 중 불일치 "
              f"{check['score']['mismatches']}건 / "
              f"Gate 불일치 {check['gate']['mismatches']}건")
        print(f"  TOP 선택 크기: {TOP_N}종목")
        _print_ablation(summary["rows"],
                        ["BUY_mean_return_net", "TOP20_mean_return_net",
                         "BUY_hit_rate", "TOP20_hit_rate"])
        verdicts = {}
        for row in summary["rows"]:
            verdicts[row["edge_verdict"]] = verdicts.get(
                row["edge_verdict"], 0) + 1
        print(f"  판정 분포: {verdicts}")
        for path in payload.get("artifacts", []):
            print(f"  생성: {path}")
        if args.dry_run:
            print("  [dry-run] Parquet을 쓰지 않았다.")
    return ExitCode.OK


# ─────────────────────────────────────────────────────────────
# replay (Stage 5)
# ─────────────────────────────────────────────────────────────

def cmd_replay_run(args) -> int:
    """거래일별 15:35 스캐너 replay.

    그날의 구성종목은 유니버스 이력에서 온다 — 오늘의 스냅샷 파일을 읽지
    않는다. 분봉은 `bar_end <= 15:35`인 것만 넘어간다.
    """
    replay = HistoricalScannerReplay(decision=args.decision)
    start = _parse_day(args.start)
    end = _parse_day(args.end) or date.today()
    if start is None:
        print("--start가 필요하다")
        return ExitCode.CLI_CONFIG_ERROR

    def _tick(result):
        if args.log_json:
            return
        got = result.summary()
        print(f"  {got['trading_date']} 유니버스 {got['universe_n']} / "
              f"분석 {got['analyzed_n']} / BUY {got['buy_candidates']} / "
              f"제외 {got['excluded_n']}", flush=True)

    summary = replay.replay_range(
        args.index, start, end, run_id=args.run_id, period=args.period,
        bdays=args.bdays, daily_scope=args.daily_scope,
        rank_session=args.rank_session, dry_run=args.dry_run,
        on_progress=_tick, minute_source=args.minute_source)
    summary["command"] = "replay run"

    if args.out and not args.dry_run:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fp:
            json.dump(summary, fp, ensure_ascii=False, indent=2, default=str)
            fp.write("\n")
        summary.setdefault("artifacts", []).append(_rel(args.out))

    payload = {k: v for k, v in summary.items() if k != "per_day"}
    _emit(payload, args.log_json)
    if not args.log_json:
        print(f"replay 완료: {summary['trading_days']}거래일 / "
              f"신호 {summary['signals']:,}건 / "
              f"BUY_CANDIDATE {summary['buy_candidates']:,}건")
        print(f"  유니버스 {summary['universe_min']}~{summary['universe_max']}종목 "
              f"/ 최저 coverage {summary['coverage_min']}")
        if summary["exclusion_reasons"]:
            print(f"  제외 사유: {summary['exclusion_reasons']}")
        print(f"  신호 지문: {summary['signal_digest']}")
        for path in summary.get("artifacts", []):
            print(f"  생성: {path}")
    return ExitCode.OK


# ─────────────────────────────────────────────────────────────
# universe (Stage 3)
# ─────────────────────────────────────────────────────────────

_NOMINAL_SIZE = {"KOSPI200": 200, "KOSDAQ150": 150}


def cmd_universe_snapshot(args) -> int:
    """오늘 구성종목을 스냅샷으로 남긴다.

    키움은 과거 구성종목을 주지 않는다. 오늘 저장해 두면 내일부터 이력이 된다.
    """
    from ..ingestion.market_source import universe_source
    from ..universe import current_universe

    store = SnapshotStore()
    as_of = _parse_day(args.as_of) or date.today()
    # KIS는 종목마다 묻는다 — 후보에 직전 명단을 더해 빠진 종목이 없게 한다
    try:
        previous = {args.index: current_universe((args.index,)).codes(args.index)}
    except Exception:                                   # noqa: BLE001
        previous = {}
    source = universe_source(args.source, previous=previous,
                             log=None if args.log_json else print)
    result = (source.fetch(args.index, as_of=as_of)
              if getattr(source, "SOURCE", "").startswith("KIS")
              else source.fetch(args.index))

    expected = result["expected"]
    warning = None
    if expected and len(result["codes"]) != expected:
        warning = (f"구성종목이 {len(result['codes'])}개로 예상({expected})과 "
                   f"다르다 — 지수 편입 변경 직후이거나 응답이 잘렸을 수 있다")
    # 키움은 코스닥150 전용 코드가 없어 코스닥 전체(2026-09-29: 1,823종목)를
    # 돌려준다. 이런 응답이 최신 스냅샷이 되면 `universe rebuild`의 씨앗이 된다.
    nominal = _NOMINAL_SIZE.get(args.index)
    if nominal and len(result["codes"]) > nominal * 1.2:
        print(f"{args.index} 응답이 {len(result['codes'])}종목이다 — 지수 "
              f"구성({nominal})이 아니라 시장 전체로 보여 저장하지 않는다. "
              f"rebuild는 기존 스냅샷을 씨앗으로 쓰고 검산으로 바로잡는다")
        return ExitCode.DATA_QUALITY_FAILED

    check = result.get("checksum")
    if check and check.get("diff") not in (0, None):
        note = (f"KRX {check['bas_dd']} 시총 검산 차이 {check['diff']:,}원 — 그날 뒤 "
                "구성 변경이 있었거나 명단이 틀렸다")
        warning = f"{warning} / {note}" if warning else note
    # 시총 검산이 0원이면 명단이 맞다고 증명된 것 — 후보 부족 경고는 그때만 낸다
    if result.get("members_in_candidate_tail") and not (check and check.get("diff") == 0):
        note = (f"시총 후보 끝 20위 안에 구성종목 {len(result['members_in_candidate_tail'])}개 "
                "— 후보(--top)가 모자랐을 수 있다")
        warning = f"{warning} / {note}" if warning else note
    payload = {"command": "universe snapshot", "index_id": args.index,
               "as_of": as_of.isoformat(), "members": len(result["codes"]),
               "pages": result["pages"], "source": result["source"],
               "warning": warning, "artifacts": [],
               "checksum": check, "candidate_basis": result.get("candidate_basis"),
               "labels": result.get("labels")}
    if not args.dry_run:
        kis = result["source"].startswith("KIS")
        path = store.save(
            args.index, as_of, result["codes"], result["source"],
            note=(("KIS 종목 현재가 rprs_mrkt_kor_name(KOSPI200/KSQ150)으로 고른 현재 "
                   "구성종목. 후보: " + str(result.get("candidate_basis"))) if kis else
                  "키움 ka20002는 현재 구성종목만 준다.")
                 + " 이 파일은 이 날짜의 관측 기록이다.",
            extra=({"checksum": check} if check else None))
        payload["artifacts"] = [_rel(path)]

    _emit(payload, args.log_json)
    if not args.log_json:
        print(f"{args.index} 구성종목 {payload['members']}개 "
              f"({as_of}, {payload['source']}, 호출 {payload['pages']})")
        if check and check.get("target") is not None:
            print(f"  KRX {check['bas_dd']} 시총 검산: 지수 {check['target']:,} / "
                  f"명단 합 {check['ours']:,} / 차이 {check['diff']:,}원")
        if warning:
            print(f"  ⚠ {warning}")
        for path in payload["artifacts"]:
            print(f"  저장: {path}")
        if args.dry_run:
            print("  [dry-run] 파일을 쓰지 않았다.")
    return ExitCode.OK


def cmd_universe_import_csv(args) -> int:
    """KRX에서 내려받은 구성종목 CSV를 스냅샷으로 넣는다."""
    as_of = _parse_day(args.as_of)
    if as_of is None:
        raise CvdObvError("--as-of가 필요하다 (그 CSV가 어느 날짜 구성종목인가)")
    parsed = parse_krx_csv(args.csv, args.index, as_of)
    store = SnapshotStore()
    payload = {"command": "universe import-csv", "index_id": args.index,
               "as_of": as_of.isoformat(), "members": len(parsed["codes"]),
               "column": parsed["column"], "encoding": parsed["encoding"],
               "artifacts": []}
    if not args.dry_run:
        path = store.save(args.index, as_of, parsed["codes"], "KRX_CSV",
                          note=f"KRX CSV 수동 임포트: {os.path.basename(args.csv)}",
                          extra={"source_file": os.path.basename(args.csv)})
        payload["artifacts"] = [_rel(path)]
    _emit(payload, args.log_json)
    if not args.log_json:
        print(f"{args.index} {as_of} 구성종목 {payload['members']}개 "
              f"(열 '{parsed['column']}', 인코딩 {parsed['encoding']})")
        for path in payload["artifacts"]:
            print(f"  저장: {path}")
    return ExitCode.OK


def cmd_universe_build(args) -> int:
    """스냅샷들을 membership 구간으로 바꿔 저장한다."""
    snapshots = SnapshotStore().load_all(args.index)
    repo = UniverseRepository()
    summary = repo.import_snapshots(snapshots, run_id=args.run_id,
                                    dry_run=args.dry_run)
    summary["command"] = "universe build"
    _emit(summary, args.log_json)
    if not args.log_json:
        print(f"{summary['index_id']} 유니버스 이력 — "
              f"{summary['rows']}구간 / {summary['securities']}종목")
        print(f"  universe_version: {summary['universe_version']}")
        print(f"  관측 구간: {summary['coverage_start']} ~ "
              f"{summary['coverage_end']} (스냅샷 {len(summary['snapshots'])}개)")
        for snap in summary["snapshots"]:
            print(f"    {snap['as_of']}  {snap['members']:>4}종목  "
                  f"{snap['source_ref']}")
        if summary["max_gap_days"] > 0:
            print(f"  ⚠ 스냅샷 최대 간격 {summary['max_gap_days']}일 — "
                  f"편입·편출 시점이 그만큼 불확실하다")
        if len(summary["snapshots"]) < 2:
            print("  ⚠ 스냅샷이 1개뿐이라 아직 '이력'이 아니다. "
                  "과거 replay에는 쓸 수 없다")
        for path in summary["artifacts"]:
            print(f"  manifest: {path}")
    return ExitCode.OK


def _trading_days(start, end):
    """시장 달력에서 구간 거래일을 꺼낸다. 달력이 없으면 만들 수 없다."""
    calendar = MarketCalendarRepository().load_calendar()
    return [d for d in calendar.days if start <= d <= end]


def _notices_path():
    return os.path.join(project_root(), "data", "bronze", "krx_notices",
                        "notices.json")


def cmd_daily_cache_regular_from_minute(args) -> int:
    """EXTENDED만 있는 날짜에 1분봉으로 정규장 일봉을 만들어 넣는다.

    규칙과 근거는 `ingestion/regular_from_minute.py` 머리말. 원본 EXTENDED 행은
    그대로 두고 `price_scope=REGULAR, source=DERIVED_MINUTE_1M` 행을 추가한다.
    이미 REGULAR가 있는 날은 건드리지 않으므로 여러 번 돌려도 된다.
    """
    from ..domain.ids import normalize_security_id
    from ..ingestion.regular_from_minute import RegularFromMinuteBuilder

    securities = None
    if args.codes:
        securities = {normalize_security_id(c.strip())
                      for c in args.codes.split(",") if c.strip()}
    summary = RegularFromMinuteBuilder().build(
        run_id=args.run_id, start=_parse_day(args.start),
        end=_parse_day(args.end), securities=securities,
        dry_run=args.dry_run)
    summary["command"] = "daily-cache regular-from-minute"
    _emit(summary, args.log_json)
    if not args.log_json:
        print(f"정규장 일봉 복원 {summary['start']} ~ {summary['end']}: "
              f"대상(EXTENDED만 있는 봉) {summary['targets']:,} / "
              f"생성 {summary['written']:,}")
        for reason, n in sorted(summary["skipped"].items()):
            print(f"  · 만들지 않음 {reason}: {n:,} — EXTENDED 그대로 남는다")
        for flag, n in sorted(summary["flags"].items()):
            print(f"  · 플래그 {flag}: {n:,}")
        for path in summary["artifacts"]:
            print(f"  manifest: {path}")
        if args.dry_run:
            print("  [dry-run] Parquet을 쓰지 않았다.")
    return ExitCode.OK


#: 일일 적재의 일봉·readjust를 확인 구간보다 앞당기는 달력일 — 주말을 끼어도 확정 뒤
#: 두 번 받은 거래일이 하나 이상 들어가게
READJUST_LOOKBACK_DAYS = 5


def daily_load_steps(today: date, days: int = 10, since: date = None,
                     indices=("KOSPI200", "KOSDAQ150"),
                     provisional: bool = False) -> list:
    """일일 적재 순서(봉 기준 bars-1.0). 순수 함수 — 테스트 대상.

    밀린 날(확인 구간 ~ 어제)을 먼저 끝내고, 그다음 이 작업 몫(16:10이면 오늘 잠정
    봉)을 한다. 16:10·08:30 어느 쪽을 놓쳐도 다음 작업이 같은 순서로 메운다.

    1. 1분봉 catchup — 빠진 날의 1분봉과, 애프터마켓 확정 전에 받은 날을 다시
       받는다(`minute_catchup` 머리말). 16:10에 돌면 오늘 정규장도 받는다
    2. 밀린 날 잠정 봉 — 확정 봉도 잠정 봉도 없는 종목만(`--missing-only`). 16:10
       작업을 놓친 날이 여기서 잠정 봉을 갖는다. 확정이 실패하는 종목도 봉이 남는다
    3. 일봉 build — 미확정 당일 봉은 적재 단계가 버린다
    4. 정규장 복원(확정) — 1·3이 끝난 뒤에만 의미가 있다(애프터마켓 분봉·확정 일봉 필요).
       2의 잠정 봉을 덮는다(리비전 1 > 0)
    5. (`provisional`) 오늘 잠정 일봉 — 밀린 날을 1~4로 끝낸 뒤다
    6. KIS 매수/매도 체결량(`trade-sides daily`, 기본 UN) — 선택 단계. 계획은 명령이
       시각으로 정한다(16:10 오늘 잠정, 08:30 어제 확정, 밀린 날 메우기)
    7. (`provisional`) 구성종목 이력 갱신(`universe refresh`) — 선택 단계. 이력과
       `index_stocks.json`을 KRX 자료가 있는 마지막 거래일(16:10이면 어제)까지 늘린다

    `since`를 주면 catchup이 그날까지 거슬러 가도록 `--days`도 달력일 수로 넓힌다
    (catchup은 `--days`거래일을 먼저 받고 `--since`로 자른다 — 기본 10이면 오래
    밀린 날이 잘린다).
    """
    from datetime import timedelta

    start = since or (today - timedelta(days=int(days * 7 / 5) + 7))
    catchup = ["minute-cache", "catchup", "--period", "1",
               "--index", ",".join(indices)]
    if since:
        span = max((today - since).days + 1, days)
        catchup += ["--since", f"{since}", "--days", str(span)]
    else:
        catchup += ["--days", str(days)]
    # NXT 1분봉(A안 — 메인마켓 거래량을 정규장 일봉에 더한다). 선택 단계: 실패하면
    # 일봉에 NXT_VOLUME_MISSING 표시가 붙고, 다음 적재가 받으면 확정 봉을 다시 만든다
    steps = [catchup, catchup[:4] + ["--venue", "NXT"] + catchup[4:]]
    yesterday = today - timedelta(days=1)
    if start <= yesterday:
        steps.append(["daily-cache", "provisional", "--start", f"{start}",
                      "--end", f"{yesterday}", "--missing-only",
                      "--index", ",".join(indices)])
    # 일봉은 확인 구간보다 며칠 앞에서부터 받는다 — 확정 뒤 두 번 받은 날이 있어야
    # readjust가 작은 소급도 잡는다. 최근 거래일 종가는 다음 거래일 전까지 바뀌므로
    # 그 날만으로는 시간외 가격제한폭 안의 변화를 넘어간다(`adjustment_watch` 머리말)
    bars_from = start - timedelta(days=READJUST_LOOKBACK_DAYS)
    for idx in indices:
        steps.append(["daily-cache", "build", "--index", idx,
                      "--start", f"{bars_from}", "--end", f"{today}"])
    # 수정주가 소급 감지 → 그 종목 이력 전체 재수집·파생 봉 재생성(선택 단계)
    steps.append(["daily-cache", "readjust", "--since", f"{bars_from}"])
    steps.append(["daily-cache", "regular-from-minute",
                  "--start", f"{max(start, date(2026, 9, 14))}", "--end", f"{today}"])
    if provisional:
        steps.append(["daily-cache", "provisional", "--date", f"{today}",
                      "--index", ",".join(indices)])
    # KIS 매수/매도 체결량(UN = KRX+NXT) — 16:10은 오늘(잠정), 08:30은 어제(확정).
    # 선택 단계: 실패해도 일봉 장부를 막지 않는다(`_data_load_body`)
    steps.append(["trade-sides", "daily", "--index", ",".join(indices)])
    # (`provisional`) 구성종목 이력 정기 갱신 — 선택 단계, 이력 끝은 KRX 자료가 있는 어제
    if provisional:
        steps.append(["universe", "refresh", "--index", ",".join(indices)])
    return steps


#: 실패해도 적재를 멈추지 않는 단계 — 일봉 기준(bars-1.0)과 무관한 보조 데이터
OPTIONAL_LOAD_GROUPS = ("trade-sides", "universe")


def _is_optional_step(step) -> bool:
    """실패해도 적재를 멈추지 않는 단계 — 체결량, NXT 1분봉."""
    return step[0] in OPTIONAL_LOAD_GROUPS or step[:2] == ["daily-cache", "readjust"] or (
        "--venue" in step and step[step.index("--venue") + 1] == "NXT")


def data_load_run_id(now: datetime = None) -> str:
    """일일 적재 run_id — `topup_load_<UTC yyyymmddHHMMSS>_<nonce>`."""
    return make_run_id("topup_load", now or datetime.now(tz=UTC))


def cmd_data_load_daily(args) -> int:
    """확정 적재 — 밀린 날의 1분봉 → 일봉 → 정규장 복원. 스케줄 익일 08:30.

    애프터마켓 확정(20:05) 뒤면 언제 돌려도 된다. 마지막 확정 이후(장부
    `data/manifests/data_load_state.json`) 비어 있는 날을 스스로 찾는다.
    """
    return _data_load(args, provisional=False)


def cmd_data_load_provisional(args) -> int:
    """잠정 적재 — 밀린 날을 먼저 확정하고, 오늘 1분봉으로 잠정 일봉. 스케줄 16:10."""
    return _data_load(args, provisional=True)


def _data_load(args, provisional: bool) -> int:
    """실행 잠금을 잡고 적재한다(`ingestion/run_lock.py`).

    잠금을 잡은 **뒤에** 장부를 읽고 계획을 세운다 — 앞 적재를 기다린 실행은 그
    결과를 보고 남은 것만 한다. dry-run은 쓰지 않으므로 잠그지 않는다.
    """
    if args.dry_run:
        return _data_load_body(args, provisional)
    from ..ingestion.run_lock import LockTimeout, exclusive

    command = "data-load provisional" if provisional else "data-load daily"
    try:
        with exclusive(command, log=None if args.log_json else print):
            return _data_load_body(args, provisional)
    except LockTimeout as exc:
        _emit({"command": command, "error": "LOCK_TIMEOUT", "message": str(exc)},
              args.log_json)
        if not args.log_json:
            print(f"  ❌ {exc} — 이번 실행은 건너뛴다. 장부를 옮기지 않았으므로 "
                  "다음 실행이 같은 구간을 본다")
        return ExitCode.CLI_CONFIG_ERROR


def _data_load_body(args, provisional: bool) -> int:
    from datetime import timedelta

    from ..bar_standard import (
        DAILY_LOAD_KST, PROVISIONAL_LOAD_KST, provisional_days, unrestored_days,
    )
    from ..ingestion import load_state
    from ..live_feed import index_codes

    command = "data-load provisional" if provisional else "data-load daily"
    now = datetime.now(tz=KST)
    today = now.date()
    indices = tuple(x.strip().upper() for x in args.index.split(",") if x.strip())
    state = load_state.load_state()
    since, days, basis = load_state.backlog_since(
        today, state, since=_parse_day(args.since), days=args.days)
    steps = daily_load_steps(today, days=days or load_state.DEFAULT_DAYS,
                             since=since, indices=indices, provisional=provisional)
    codes = {f"KRX:{c}" for c in index_codes()}
    checked_from = since or (today - timedelta(days=int(
        (days or load_state.DEFAULT_DAYS) * 7 / 5) + 7))
    before = load_state.day_status(None, checked_from, today, codes)
    if not args.log_json:
        kind = (f"잠정(스케줄 {PROVISIONAL_LOAD_KST:%H:%M})" if provisional
                else f"확정(스케줄 익일 {DAILY_LOAD_KST:%H:%M})")
        print(f"일일 적재 {kind} — 봉 기준 bars-1.0, {now:%Y-%m-%d %H:%M} KST")
        print(f"  확인 구간: {since or f'최근 {days}거래일'} ~ {today} ({basis})")
        _print_day_status("선행 점검", before, today, len(codes))
        print("  순서: 밀린 날 1분봉 → 빠진 잠정 봉 → 확정 봉"
              + (" → 오늘 잠정 봉" if provisional else ""))
        for st in steps:
            print("  · python cvdobv.py " + " ".join(st))
    if args.dry_run:
        return ExitCode.OK

    results = []
    for st in steps:
        # run_id는 단계마다 새로 만든다. 같은 봉이 여러 run에 있으면 reader는 "나중
        # run_id"(사전순)를 쓰는데, 자동 id `cli_…`는 기존 수동 id(`topup-…`,
        # `min1-…`)보다 앞에 정렬돼 새로 받은 확정값이 옛 값에 가려진다.
        # `topup_load_<UTC>_<nonce>`는 `_`가 `-`보다 뒤라 그 모두보다 나중이고,
        # 실행끼리는 시각 순으로 정렬되며, 스키마 §2 시각 형식을 지킨다.
        # (잠정 봉은 리비전 0이라 run_id와 무관하게 확정 봉에 진다.)
        st = st + ["--run-id", data_load_run_id()]
        if not args.log_json:
            print(f"\n▶ {' '.join(st)}", flush=True)
        rc = int(main(st + (["--log-json"] if args.log_json else [])))
        optional = _is_optional_step(st)
        results.append({"step": " ".join(st), "exit": rc, "optional": optional})
        if optional and rc not in (ExitCode.OK, ExitCode.PARTIAL_SUCCESS):
            if not args.log_json:
                print(f"  ⚠ 선택 단계 종료 코드 {rc} — 일봉 적재는 계속한다. "
                      "다음 적재가 이 단계의 밀린 날을 다시 본다")
            continue
        if rc not in (ExitCode.OK, ExitCode.PARTIAL_SUCCESS):
            cause = None
            if rc == ExitCode.EXTERNAL_API_FAILED:
                from ..ingestion.market_source import diagnose
                cause = diagnose()
            _emit({"command": command, "steps": results, "api_diagnosis": cause,
                   "stopped_at": " ".join(st)}, args.log_json)
            if not args.log_json:
                if cause:
                    print(f"  🔎 원천 진단 — {cause}")
                print(f"  ❌ 종료 코드 {rc} — 이후 단계를 멈춘다. 장부는 그대로 "
                      "두므로 다음 실행이 같은 구간을 다시 본다")
            return rc

    # 장부는 확정 안 된 가장 이른 거래일 앞까지만 옮긴다 — 수집이 조용히 비어도
    # 다음 실행이 그날을 다시 본다(2026-10-13 빈 응답 사고 뒤 추가).
    horizon = load_state.confirmed_horizon(datetime.now(tz=KST))
    stuck = load_state.first_unconfirmed_day(None, checked_from, horizon, codes)
    confirmed = load_state.confirmed_through(horizon, stuck)
    update = {"confirmed_through": confirmed.isoformat(),
              f"last_{'provisional' if provisional else 'final'}_run":
                  datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M:%S")}
    state_file = load_state.save_state(update)
    horizon_note = (None if confirmed == horizon else
                    f"{stuck}은 지수 구성종목의 확정 정규장 봉이 "
                    f"{load_state.MIN_CONFIRMED_RATIO:.0%} 미만이라 장부를 {confirmed}에 둔다")

    window = today - timedelta(days=30)
    pending = unrestored_days(securities=codes or None, start=window)
    prov = provisional_days(securities=codes or None, start=window)
    summary = {"command": command, "steps": results,
               "confirmed_through": confirmed.isoformat(), "state": state_file,
               "first_unconfirmed_day": str(stuck) if stuck else None,
               "unrestored_days": {str(d): n for d, n in pending.items()},
               "provisional_days": {str(d): n for d, n in prov.items()}}
    after = load_state.day_status(None, checked_from, today, codes)
    summary["day_status"] = [dict(r, date=str(r["date"])) for r in after]
    _emit(summary, args.log_json)
    if not args.log_json:
        print()
        _print_day_status("적재 후", after, today, len(codes))
        print(f"\n  장부: confirmed_through={confirmed} — 다음 적재는 이날부터 확인한다")
        if horizon_note:
            print(f"  ⚠ {horizon_note}")
        if prov:
            print("  잠정 일봉(거래량 근사)인 (날짜: 종목 수) — 다음 확정 적재가 바꾼다: "
                  + ", ".join(f"{d} {n}" for d, n in prov.items()))
        if pending:
            print("  확정 정규장 봉이 없는 (날짜: 종목 수): "
                  + ", ".join(f"{d} {n}" for d, n in pending.items()))
            print("  애프터마켓 확정 전 날짜면 다음 확정 적재에서 채워진다. 지난 날짜는 "
                  "종가 체결 1분봉이 없는 경우다(`daily-cache regular-from-minute` 출력 참조).")
        if not prov and not pending:
            print("  ✅ 지수 구성종목의 최근 일봉이 모두 확정 정규장 봉이다")
    worst = max((r["exit"] for r in results), default=0)
    if worst == ExitCode.PARTIAL_SUCCESS or any(
            r["optional"] and r["exit"] != ExitCode.OK for r in results):
        return ExitCode.PARTIAL_SUCCESS
    return ExitCode.OK


def _print_day_status(title, rows, today, members):
    """선행 점검·적재 후 표 — 날짜별 1분봉·잠정·확정 종목 수."""
    print(f"  [{title}] 지수 구성종목 {members}개 기준")
    print("    날짜        1분봉  잠정  확정  상태")
    for r in rows:
        note = " (오늘)" if r["date"] == today else ""
        print(f"    {r['date']}  {r['minutes']:5} {r['provisional']:5} "
              f"{r['final']:5}  {r['status']}{note}")


def cmd_trade_sides_daily(args) -> int:
    """KIS 매수/매도 체결량 일일 적재 — 시각으로 계획을 정한다(`kis_trade_sides.plan_daily`)."""
    from ..ingestion.kis_trade_sides import (
        REVISION_FINAL, TradeSidesLoader, plan_daily,
    )
    from ..universe import current_universe

    indices = tuple(x.strip().upper() for x in args.index.split(",") if x.strip())
    codes = sorted(current_universe(indices).codes())
    now = datetime.now(tz=KST)
    markets = tuple(x.strip().upper() for x in args.market_div.split(",") if x.strip())
    plan = plan_daily(codes, now, since=_parse_day(args.since), markets=markets)
    if not args.log_json:
        print(f"매수/매도 체결량(KIS FHKST03010800) — {now:%Y-%m-%d %H:%M} KST, "
              f"종목 {len(codes)}개")
        if not plan:
            print(f"  받을 것이 없다({','.join(markets)} — 밀린 날 없음, 오늘 잠정은 "
                  "15:35~20:05 사이에만)")
        for item in plan:
            prov = (f", {item['provisional_from']}부터 잠정" if item["provisional_from"]
                    else "")
            print(f"  · {item['market_div']}: {item['start']} ~ {item['end']}{prov}"
                  f" — 확정 미완 거래일 {len(item['undone_days'])}일")
    if args.dry_run or not plan:
        _emit({"command": "trade-sides daily", "plan": plan, "dry_run": args.dry_run},
              args.log_json)
        return ExitCode.OK

    results, failed = [], 0
    for item in plan:
        loader = TradeSidesLoader(market_div=item["market_div"])
        res = loader.run(codes, start=item["start"], end=item["end"],
                         run_id=args.run_id, provisional_from=item["provisional_from"],
                         final_revision=REVISION_FINAL,
                         log=(lambda m: None) if args.log_json else print)
        failed += res["failed_calls"]
        results.append(res)
        if not args.log_json:
            print(f"  {item['market_div']}: 행 {res['rows']:,} · 호출 {res['calls']:,} · "
                  f"실패 {res['failed_calls']} · {res['elapsed_sec']:.0f}초")
    _emit({"command": "trade-sides daily", "plan": plan, "results": results},
          args.log_json)
    return ExitCode.PARTIAL_SUCCESS if failed else ExitCode.OK


def cmd_daily_cache_readjust(args) -> int:
    """수정주가 소급을 감지해 그 종목의 일봉 이력 전체를 다시 받고, 파생 봉을 다시 만든다.

    1) 감지: 같은 날을 다시 받았는데 종가가 바뀐 종목(`adjustment_watch`). `--audit`이면
       지금 적재 원천(기본 KIS) 값과 저장값을 직접 대조(종목당 1회 호출) — 한 번만 받은 이력도 본다
    2) 그 종목의 일봉 이력 전체를 다시 받는다(`daily-cache build --codes`)
    3) 9/14 이후 복원 봉: 시가가 원본과 달라진 날을 다시 만든다(`regular-from-minute`)
    4) 9/14 이전 NXT 포함 봉: 기준 행이 바뀌었으면 다시 만든다(`nxt-history --no-fetch`)
    """
    from datetime import timedelta

    import pyarrow.dataset as ds

    from ..ingestion.adjustment_watch import adjustment_events, audit_against_api
    from ..storage.parquet_store import ParquetStore
    from ..universe import current_universe

    today = datetime.now(tz=KST).date()
    since = _parse_day(args.since) or (today - timedelta(days=30))
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        codes = sorted(current_universe().codes())
    sids = {f"KRX:{c}" for c in codes}
    events = adjustment_events(ParquetStore(), since, today, sids)
    if args.audit:
        if not args.log_json:
            print(f"적재 원천의 현재 수정주가와 저장값 대조 — {len(codes)}종목", flush=True)
        found, audit_failed = audit_against_api(codes, log=None if args.log_json else print)
        if audit_failed and not args.log_json:
            from ..ingestion.market_source import diagnose
            print(f"  ⚠ 받지 못해 대조 못 한 종목 {len(audit_failed)}개 — "
                  "API가 회복된 뒤 --audit을 다시 돌려야 한다")
            print(f"  🔎 원천 진단 — {diagnose()}")
        for sid, e in found.items():
            if sid not in events or e["date"] < events[sid]["date"]:
                events[sid] = e
    if not args.log_json:
        print(f"수정주가 소급 감지 — {since} ~ {today}: {len(events)}종목")
        for sid, e in sorted(events.items()):
            print(f"  · {sid} {e['date']} {e['scope']} 종가 {e['old_close']:,.0f} → "
                  f"{e['new_close']:,.0f} (×{e['ratio']:.5f})")
    payload = {k: dict(v, date=str(v["date"])) for k, v in events.items()}
    audit_failed = audit_failed if args.audit else []
    if args.dry_run or not events:
        _emit({"command": "daily-cache readjust", "events": payload,
               "audit_failed": audit_failed, "dry_run": args.dry_run}, args.log_json)
        return ExitCode.PARTIAL_SUCCESS if audit_failed else ExitCode.OK

    store = ParquetStore()
    results = []
    for sid in sorted(events):
        code = sid.split(":")[1]
        t = store.read_arrow("silver", "daily_bars", columns=["trading_date"],
                             filters=ds.field("security_id") == sid)
        first = min(t.column("trading_date").to_pylist(), default=date(2010, 1, 1))
        for step in (
                ["daily-cache", "build", "--codes", code, "--start", f"{first}",
                 "--end", f"{today}"],
                ["daily-cache", "regular-from-minute", "--codes", code,
                 "--start", "2026-09-14", "--end", f"{today}"],
                ["daily-cache", "nxt-history", "--codes", code, "--no-fetch"]):
            step = step + ["--run-id", data_load_run_id()]
            if not args.log_json:
                print(f"\n▶ {' '.join(step)}", flush=True)
            rc = int(main(step + (["--log-json"] if args.log_json else [])))
            results.append({"step": " ".join(step), "exit": rc})
            if rc not in (ExitCode.OK, ExitCode.PARTIAL_SUCCESS):
                break
    _emit({"command": "daily-cache readjust", "events": payload, "steps": results,
           "audit_failed": audit_failed}, args.log_json)
    worst = max((r["exit"] for r in results), default=0)
    return ExitCode.OK if worst == 0 and not audit_failed else ExitCode.PARTIAL_SUCCESS


def cmd_daily_cache_nxt_history(args) -> int:
    """9/14 전 정규장 일봉에 NXT 메인마켓 거래량(저장된 NXT 1분봉, 없으면 30분봉)을 더한다."""
    from ..ingestion.nxt_history import (
        NXT_HISTORY_END, NXT_HISTORY_START, NxtHistoryBuilder, nxt_history_codes,
    )
    from ..storage.parquet_store import ParquetStore

    start = max(_parse_day(args.start) or NXT_HISTORY_START, NXT_HISTORY_START)
    end = min(_parse_day(args.end) or NXT_HISTORY_END, NXT_HISTORY_END)
    store = ParquetStore()
    codes = ([c.strip() for c in args.codes.split(",") if c.strip()] if args.codes
             else nxt_history_codes(store, start, end))
    builder = NxtHistoryBuilder(store)
    if not args.log_json:
        print(f"9/14 전 일봉 + NXT 메인마켓 — {start} ~ {end} 전날, 종목 {len(codes)}개",
              flush=True)
    fetched = None
    if not args.no_fetch and not args.dry_run:
        def _progress(i, total, record):
            if not args.log_json and (i % 25 == 0 or i == total):
                print(f"  30분봉 {i}/{total} — {record['security_id']} {record['status']}",
                      flush=True)
        fetched = builder.fetch(codes, run_id=args.run_id, start=start,
                                on_progress=_progress)
        if not args.log_json:
            print(f"  NXT 30분봉 저장 {fetched['bars']:,}봉 / 받음 {fetched['fetched_bars']:,}봉"
                  f" · 실패 {fetched['failed']}종목")
    summary = builder.build(args.run_id, start, end, {f"KRX:{c}" for c in codes},
                            dry_run=args.dry_run)
    summary["command"] = "daily-cache nxt-history"
    if fetched:
        summary["fetch"] = {k: fetched[k] for k in ("attempted", "failed", "bars",
                                                   "fetched_bars", "status")}
    _emit(summary, args.log_json)
    if not args.log_json:
        print(f"  NXT 메인마켓이 있는 (종목, 날) {summary['nxt_pairs']:,}"
              f"(1분봉 {summary['from_1m']:,} · 30분봉 {summary['from_30m']:,}"
              f" · 계수 적용 {summary['rescaled']:,}) → 생성 "
              f"{summary['written']:,}(다시 만듦 {summary['rebuilt']:,}) · 이미 맞음 "
              f"{summary['up_to_date']:,} · 기준 일봉 없음 {summary['no_base']:,}")
        if args.dry_run:
            print("  [dry-run] Parquet을 쓰지 않았다.")
    failed = (fetched or {}).get("failed", 0)
    return ExitCode.PARTIAL_SUCCESS if failed else ExitCode.OK


def cmd_daily_cache_provisional(args) -> int:
    """당일 1분봉 합으로 잠정 정규장 일봉(거래량 근사, 리비전 0)."""
    from ..domain.ids import normalize_security_id
    from ..ingestion.provisional_daily import ProvisionalDailyBuilder
    from ..universe import current_universe

    day = _parse_day(args.date) or datetime.now(tz=KST).date()
    if args.codes:
        securities = {normalize_security_id(c.strip())
                      for c in args.codes.split(",") if c.strip()}
    else:
        indices = tuple(x.strip().upper() for x in args.index.split(",")
                        if x.strip())
        securities = set(current_universe(indices).security_ids())
    if args.start:
        start = _parse_day(args.start)
        end = _parse_day(args.end) or day
        summary = ProvisionalDailyBuilder().build_range(
            run_id=args.run_id, start=start, end=end, securities=securities,
            missing_only=args.missing_only, dry_run=args.dry_run)
        summary["command"] = "daily-cache provisional"
        _emit(summary, args.log_json)
        if not args.log_json:
            print(f"밀린 날 잠정 일봉 {start} ~ {end}"
                  + (" (빠진 것만)" if args.missing_only else ""))
            if not summary["days"]:
                print("  저장소가 아는 거래일이 없다")
            for d, got in summary["days"].items():
                print(f"  {d}: 생성 {got['written']:,} / 이미 확정 "
                      f"{got['already_final']:,} / 이미 잠정 "
                      f"{got['already_provisional']:,} — {got['status']}")
            if args.dry_run:
                print("  [dry-run] Parquet을 쓰지 않았다.")
        return ExitCode.OK
    summary = ProvisionalDailyBuilder().build(
        run_id=args.run_id, day=day, securities=securities,
        missing_only=args.missing_only, dry_run=args.dry_run)
    summary["command"] = "daily-cache provisional"
    _emit(summary, args.log_json)
    if not args.log_json:
        status = summary["status"]
        if status == "TOO_EARLY":
            print(f"잠정 일봉 {day}: 아직 만들 수 없다 — {summary['ready_at']} 이후")
        elif status == "NOT_NEEDED":
            print(f"잠정 일봉 {day}: 애프터마켓이 없는 날 — 확정 적재로 충분하다")
        elif status == "NO_MINUTES":
            print(f"잠정 일봉 {day}: 그날 1분봉이 없다(휴장 또는 미수집)")
        else:
            print(f"잠정 일봉 {day}: 대상 {summary['targets']:,} / 생성 "
                  f"{summary['written']:,} / 이미 확정 {summary['already_final']:,}")
            for reason, n in sorted(summary["skipped"].items()):
                print(f"  · 만들지 않음 {reason}: {n:,}")
            for path in summary["artifacts"]:
                print(f"  manifest: {path}")
        if args.dry_run:
            print("  [dry-run] Parquet을 쓰지 않았다.")
    return ExitCode.OK


def cmd_daily_cache_krx_panel(args) -> int:
    """KRX 패널에서 일봉을 만든다 — 상장폐지 종목용.

    키움에는 폐지 종목이 없다. KRX 일별매매정보에는 있지만 수정주가가 아니라서
    분할을 직접 보정한다. 보정 기준은 "주식수 k배 · 주가 1/k배 · **시가총액
    그대로**"이며, 시가총액이 함께 달라지는 자본변동(인적분할·유상증자)은
    보정하지 않고 표시만 남긴다.
    """
    api = KrxOpenApi()
    start = _parse_day(args.start) or DATA_START
    end = _parse_day(args.end) or date.today()
    days = _trading_days(max(start, DATA_START), end)
    if not days:
        print("거래일이 없다 — 달력을 먼저 만들어야 한다")
        return ExitCode.INSUFFICIENT_DATA

    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    elif args.missing_file:
        with open(args.missing_file, encoding="utf-8") as fp:
            payload = json.load(fp)
        group = payload.get("delisted" if args.delisted_only else "live", [])
        codes = [row["code"] for row in group]
    else:
        print("--codes 또는 --missing-file이 필요하다")
        return ExitCode.CLI_CONFIG_ERROR

    missing = [d for d in days
               if not api.cached("sto/stk_bydd_trd", d.strftime("%Y%m%d"))]
    if missing:
        print(f"캐시에 없는 거래일이 {len(missing)}일 있다 — "
              f"`universe krx-cache`를 먼저 돌려라 (예: {missing[0]})")
        return ExitCode.INSUFFICIENT_DATA

    panel = KrxDailyPanel(api)
    if not args.log_json:
        print(f"KRX 패널에서 일봉 생성 — {len(codes)}종목 / "
              f"{len(days):,}거래일 ({days[0]} ~ {days[-1]})", flush=True)

    summary = panel.build(codes, days, run_id=args.run_id,
                          dry_run=args.dry_run)
    summary["command"] = "daily-cache krx-panel"
    _emit(summary, args.log_json)
    if not args.log_json:
        print(f"완료: {summary['rows']:,}행 / 종목 {summary['with_bars']}"
              f"/{summary['securities']}")
        print(f"  분할 보정 {summary['splits']}건 / "
              f"보정하지 않은 자본변동 {summary['capital_changes']}건")
        for row in summary["per_security"]:
            if row["bars"]:
                continue
            print(f"  ⚠ {row['code']} {row['status']}")
        for event in summary["events"][:15]:
            print(f"    {event['code']} {event['trading_date']} "
                  f"{event['kind']} 계수 {event['factor']:.4f} "
                  f"시총비 {event.get('mktcap_ratio')}")
        for path in summary["artifacts"]:
            print(f"  manifest: {path}")
    return ExitCode.OK


def cmd_universe_krx_cache(args) -> int:
    """KRX Open API 응답을 거래일 단위로 받아 둔다(검산 재료).

    구간 조회가 없는 API라 거래일마다 2회씩 부른다. 과거 데이터는 확정값이므로
    한 번 받아 두면 다시 받지 않는다.
    """
    from concurrent.futures import ThreadPoolExecutor

    from ..ingestion.krx_openapi import market_for

    api = KrxOpenApi()
    markets = (sorted(MARKETS) if args.market == "BOTH" else [args.market])
    services = []
    for name in markets:
        mk = market_for(name)
        services.extend([mk.index_service, mk.stock_service])
    start = _parse_day(args.start) or DATA_START
    end = _parse_day(args.end) or date.today()
    if start < DATA_START:
        print(f"  KRX Open API는 {DATA_START}부터다 — 시작일을 올린다")
        start = DATA_START
    days = _trading_days(start, end)
    todo = [(svc, d.strftime("%Y%m%d")) for d in days
            for svc in services]
    todo = [job for job in todo if not api.cached(*job)]

    failures = []

    def _one(job):
        try:
            api.fetch(*job)
        except Exception as exc:                          # noqa: BLE001
            failures.append({"service": job[0], "bas_dd": job[1],
                             "error": str(exc)})

    if not args.log_json:
        print(f"거래일 {len(days)}일 / 받을 응답 {len(todo)}건 "
              f"({start} ~ {end}) / 시장 {', '.join(markets)}", flush=True)
    if todo and not args.dry_run:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(_one, todo))

    payload = {"command": "universe krx-cache", "markets": markets,
               "trading_days": len(days),
               "requested": len(todo), "failed": len(failures),
               "calls": api.calls, "cache_hits": api.cache_hits,
               "failures": failures[:20],
               "status": "FAILED" if failures else "SUCCESS"}
    _emit(payload, args.log_json)
    if not args.log_json:
        print(f"완료: 호출 {api.calls:,} / 실패 {len(failures)}")
    return ExitCode.DATA_QUALITY_FAILED if failures else ExitCode.OK


def cmd_universe_notices(args) -> int:
    """KRX 지수운영공지와 첨부를 받는다(명단 출처).

    `index.krx.co.kr`은 로그인이 필요 없다. 첨부 엑셀에 편입·편출과 변경 후
    구성종목 전량이 들어 있다.
    """
    from ..ingestion.krx_index_files import parse_attachment

    board = KrxNoticeBoard()
    rows, seen = [], set()
    for year in range(args.start_year, args.end_year + 1):
        got = board.list_all(f"{year}0101", f"{year}1231")
        for notice in got:
            if notice["seq"] in seen:
                continue
            seen.add(notice["seq"])
            rows.append(notice)
        if not args.log_json:
            print(f"  {year}: {len(got)}건", flush=True)
    rows.sort(key=lambda r: r["posted_date"])

    targets = [n for n in rows if board.is_universe_related(n)]
    files = parsed = failed = 0
    if not args.no_attachments:
        for i, notice in enumerate(targets, 1):
            try:
                atts = board.attachments(notice["seq"])
            except Exception as exc:                      # noqa: BLE001
                notice["attachments_error"] = str(exc)
                continue
            notice["attachments"] = []
            for att in atts:
                if not att["name"].lower().endswith((".xls", ".xlsx", ".zip",
                                                     ".csv")):
                    notice["attachments"].append(
                        dict(att, status="SKIPPED_FORMAT"))
                    continue
                try:
                    path, size = board.download(notice["seq"], att["file_seq"],
                                                att["name"])
                    files += 1
                except Exception as exc:                  # noqa: BLE001
                    notice["attachments"].append(
                        dict(att, status=f"DOWNLOAD_FAILED: {exc}"))
                    failed += 1
                    continue
                record = dict(att, status="OK", path=path, bytes=size)
                try:
                    got = parse_attachment(path)
                    record["full"] = {k: len(v) for k, v in got["full"].items()}
                    record["delta"] = {k: {kk: len(vv) for kk, vv in v.items()}
                                       for k, v in got["delta"].items()}
                    if got["full"] or got["delta"]:
                        parsed += 1
                except Exception as exc:                  # noqa: BLE001
                    record["status"] = f"PARSE_FAILED: {exc}"
                    failed += 1
                notice["attachments"].append(record)
            if not args.log_json and i % 50 == 0:
                print(f"  [{i}/{len(targets)}] 파일 {files} / 파싱 {parsed} / "
                      f"실패 {failed}", flush=True)

    path = _notices_path()
    # 받은 연도 밖의 공지는 기존 파일에서 그대로 가져온다. 덮어쓰면
    # `--start-year 2026` 한 번에 2009년 이후 이력이 사라진다(2026-09-29).
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fp:
            kept = [n for n in json.load(fp)
                    if n["seq"] not in seen
                    and not (args.start_year <= int(n["posted_date"][:4])
                             <= args.end_year)]
        rows = sorted(rows + kept, key=lambda r: r["posted_date"])
    if not args.dry_run:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(rows, fp, ensure_ascii=False, indent=1)

    payload = {"command": "universe notices", "notices": len(rows),
               "universe_related": len(targets), "files": files,
               "parsed": parsed, "failed": failed,
               "artifacts": [_rel(path)] if not args.dry_run else []}
    _emit(payload, args.log_json)
    if not args.log_json:
        print(f"공지 {len(rows)}건 / 유니버스 관련 {len(targets)}건 / "
              f"첨부 {files}개 (명단 파싱 {parsed})")
    return ExitCode.OK


def cmd_universe_rebuild(args) -> int:
    """오늘 스냅샷에서 거꾸로 걸으며 과거 구성종목을 복원한다.

    매 거래일 시총 항등식으로 검산한다. 0원이 아니면 그날 구성종목이 바뀐
    것이고, 공시 델타·전체 명단·역산 순으로 메운 뒤 다시 검산한다.
    """
    api = KrxOpenApi()
    mk = market_for(args.index)
    start = _parse_day(args.start) or mk.start
    end = _parse_day(args.end) or date.today()
    days = _trading_days(max(start, mk.start), end)
    if len(days) < 2:
        print("거래일이 2일 미만이다 — 달력을 먼저 만들어야 한다")
        return ExitCode.INSUFFICIENT_DATA

    snapshots = SnapshotStore().load_all(args.index)
    if not snapshots:
        print("오늘 스냅샷이 없다 — `universe snapshot`을 먼저 실행하라")
        return ExitCode.INSUFFICIENT_DATA
    latest = snapshots[-1]
    members = [sid.split(":", 1)[1] for sid in latest.securities]

    with open(_notices_path(), encoding="utf-8") as fp:
        notices = json.load(fp)
    deltas, anchors, skipped = load_notice_sources(notices, days, args.index)
    # 앵커의 시행일은 공지 표기 대신 검산으로 확정한다. 한 날도 맞지 않는
    # 앵커는 쓰지 않는다 — 틀린 명단을 쓰면 구간이 통째로 오염된다.
    anchors, dropped = locate_anchors(api, anchors, days,
                                      window=args.anchor_window, market=mk)
    if not args.log_json:
        print(f"거래일 {len(days)}일 ({days[0]} ~ {days[-1]})")
        print(f"  공시 델타 {len(deltas)}건 / 앵커 {len(anchors)}건 확정 "
              f"(검산 실패로 버린 앵커 {len(dropped)}건) / "
              f"첨부 보류 {len(skipped)}건", flush=True)

    progress = {"n": 0}

    def _tick(day, res):
        progress["n"] += 1
        if not args.log_json and progress["n"] % 250 == 0:
            print(f"  {day} 검산 {res.checked:,} / 일치 {res.matched:,} / "
                  f"미해결 {res.unresolved} / 구간 {len(res.periods)}",
                  flush=True)

    rebuilder = UniverseRebuilder(api, deltas=deltas, anchors=anchors,
                                  market=mk)
    result = rebuilder.walk_backward(members, days, on_progress=_tick)
    # 뒤로 걷다 막힌 구간은 반대 방향에서 한 번 더 메운다. 정기변경일은
    # 역산으로는 어렵지만 앵커가 있으면 앞에서 오는 쪽이 오히려 쉽다.
    filled = rebuilder.fill_forward(result, days)
    if filled and not args.log_json:
        print(f"  앞으로 걸어 {len(filled)}일을 추가로 메웠다", flush=True)
    summary = result.summary()
    summary["filled_forward"] = len(filled)

    ref = f"rebuild:{latest.source_ref}"
    observations = result.snapshots()
    # 마지막 거래일도 관측점으로 남긴다. 구간 시작일만 남기면 이력의 관측
    # 구간이 '마지막 변경일'에서 끝난 것처럼 보이고, 그 뒤 날짜 조회가
    # 관측 구간 밖이라는 이유로 막힌다.
    if observations and observations[-1][0] != days[-1]:
        observations.append((days[-1], observations[-1][1]))
    snaps = [UniverseSnapshot(args.index, day, codes, ref)
             for day, codes in observations]
    repo = UniverseRepository()
    imported = repo.import_snapshots(
        snaps, run_id=args.run_id, dry_run=args.dry_run,
        date_resolution="EXACT_ANNOUNCEMENT",
        evidence={"checksum": summary,
                  "source": "KRX_OPENAPI+KRX_INDEX_NOTICE"})

    payload = {"command": "universe rebuild", "index_id": args.index}
    payload.update(summary)
    payload["periods_written"] = imported["rows"]
    payload["universe_version"] = imported["universe_version"]
    # 앞으로 걸어 메운 날은 더 이상 미해결이 아니다. 이벤트 로그에는 그대로
    # 남기되(무엇을 어떻게 메웠는지가 증거다) 보고에서는 뺀다.
    still_open = [e for e in result.events
                  if "UNRESOLVED" in e["kind"]
                  and _parse_day(e["date"]) not in result.verified]
    payload["unresolved_events"] = still_open[:50]
    payload["skipped_attachments"] = skipped[:50]
    payload["dropped_anchors"] = dropped[:50]
    payload["anchor_dates"] = [d.isoformat() for d in sorted(anchors)]
    payload["artifacts"] = imported.get("artifacts", [])
    _emit(payload, args.log_json)
    if not args.log_json:
        print(f"검산 {summary['trading_days_checked']:,}일 중 "
              f"{summary['days_matched']:,}일 일치 "
              f"(일치율 {summary['match_rate']})")
        print(f"  구간 {summary['periods']}개 / 변경 이벤트 "
              f"{summary['events']}건 / 미해결 {summary['unresolved']}건")
        # 응답이 통째로 빈 거래일은 검산 일수에서 빠진다 — 일치율 100%가
        # 그 날을 덮지 않는다는 걸 드러낸다(2026-09-18 캐시 공백으로 발견).
        no_data = [e["date"] for e in result.events if e["kind"] == "NO_DATA"]
        if no_data:
            print(f"  ⚠ KRX 응답 없는 거래일 {len(no_data)}일 — 검산하지 않음 "
                  f"(예: {', '.join(no_data[:5])}). 캐시 파일을 지우고 "
                  f"krx-cache를 다시 돌려라")
        for lo, hi in summary["unresolved_ranges"][:10]:
            print(f"    ⚠ 믿을 수 없는 구간: {lo} ~ {hi}")
        print(f"  universe_version: {imported['universe_version']}")
        for event in payload["unresolved_events"][:10]:
            print(f"    ⚠ {event['date']} diff={event['diff']:,}")
    if summary["unresolved"]:
        return ExitCode.PARTIAL_SUCCESS
    return ExitCode.OK


#: 정기 갱신이 다시 검산하는 최근 구간(달력일) — 전 구간 verify는 수십 분이라 수동으로
REFRESH_VERIFY_DAYS = 60


def refresh_end(today: date, days, cached) -> date:
    """이력을 늘릴 마지막 날 — KRX 검산 재료가 있는 가장 최근 거래일(오늘 이하).

    KRX Open API는 그날 자료를 다음 날 아침에 올린다. 16:10에는 오늘 자료가 없으므로
    보통 어제가 된다. 자료 없는 날까지 이력을 늘리면 검산하지 않은 날이 관측일이 된다.
    `cached(day)` — 그날 지수·종목 응답이 둘 다 있는가.
    """
    for day in sorted((d for d in days if d <= today), reverse=True):
        if cached(day):
            return day
    return None


def cmd_universe_refresh(args) -> int:
    """구성종목 이력 정기 갱신 — `/update-index` 0·1단계를 한 번에(16:10 적재의 마지막 단계).

    순서: 달력 연장 → 오늘 KIS 스냅샷 → KRX 검산 재료 → 지수운영공지 → 복원 시험
    (dry-run) → 두 지수 모두 미해결 0이면 복원 기록 → 최근 구간 재검산 →
    `index_stocks.json` 갱신.

    - 이력 끝은 KRX 자료가 있는 마지막 거래일(`refresh_end`, 16:10이면 어제)이다
    - 복원에 미해결이 있으면 **아무것도 쓰지 않는다** — rebuild는 미해결이 있어도
      새 버전을 쓰므로 먼저 dry-run으로 본다. 구성 변경 시행일 당일은 KRX 자료가
      아직 없어 하루 미해결로 나올 수 있고, 다음 날 실행이 메운다
    - 재검산이 틀리면 `index_stocks.json`을 바꾸지 않는다
    """
    import subprocess
    from datetime import timedelta

    from ..ingestion.universe_history import UniverseRepository
    from ..universe import read_index_file

    indices = [x.strip().upper() for x in args.index.split(",") if x.strip()]
    today = _parse_day(args.as_of) or datetime.now(tz=KST).date()
    results = []

    def _run(step, dry=False):
        # 단계마다 새 run_id — 두 지수 rebuild가 같은 id면 manifest 이름이 겹친다
        step = step + ["--run-id", data_load_run_id()]
        if dry:
            step.append("--dry-run")
        if not args.log_json:
            print(f"\n▶ {' '.join(step)}", flush=True)
        rc = int(main(step + (["--log-json"] if args.log_json else [])))
        results.append({"step": " ".join(step), "exit": rc})
        return rc

    def _done(status, message, rc):
        _emit({"command": "universe refresh", "status": status, "message": message,
               "steps": results}, args.log_json)
        if not args.log_json:
            print(f"\n  {'✅' if rc == ExitCode.OK else '⚠'} 구성종목 이력 갱신: {message}")
        return rc

    if args.dry_run:
        return _done("DRY_RUN", "dry-run — 아무것도 쓰지 않는다", ExitCode.OK)

    repo = UniverseRepository()

    def _observed_end(idx):
        try:
            return repo.coverage(idx)["observed_end"]
        except Exception:                                 # noqa: BLE001
            return None

    before = {idx: _observed_end(idx) for idx in indices}

    rc = _run(["calendar", "build"])
    if rc not in (ExitCode.OK, ExitCode.PARTIAL_SUCCESS):
        return _done("FAILED", f"달력 연장 실패(종료 코드 {rc})", rc)
    days = _trading_days(date(2010, 1, 1), today)
    if today not in days:
        return _done("SKIPPED", f"{today}은 거래일이 아니다 — 갱신하지 않는다", ExitCode.OK)

    for idx in indices:
        rc = _run(["universe", "snapshot", "--index", idx, "--as-of", f"{today}"])
        if rc != ExitCode.OK:
            return _done("FAILED", f"{idx} 스냅샷 실패(종료 코드 {rc})", rc)

    known = [d for d in before.values() if d]
    start = min(known) if known else today - timedelta(days=30)
    rc = _run(["universe", "krx-cache", "--start", f"{start}", "--end", f"{today}",
               "--market", "BOTH"])
    if rc != ExitCode.OK:
        return _done("FAILED", f"KRX 검산 재료 수집 실패(종료 코드 {rc})", rc)
    # 올해 공지만 다시 받는다 — 지난 연도 공지는 기존 파일에서 보존된다(cmd_universe_notices)
    rc = _run(["universe", "notices", "--start-year", str(today.year - (today.month == 1))])
    if rc != ExitCode.OK:
        return _done("FAILED", f"지수운영공지 수집 실패(종료 코드 {rc})", rc)

    api = KrxOpenApi()
    markets = [market_for(idx) for idx in indices]
    end = refresh_end(today, days, lambda d: all(
        api.cached(svc, d.strftime("%Y%m%d"))
        for mk in markets for svc in (mk.index_service, mk.stock_service)))
    if end is None:
        return _done("FAILED", "KRX 검산 재료가 있는 거래일이 없다", ExitCode.INSUFFICIENT_DATA)
    if all(before.get(idx) and before[idx] >= end for idx in indices):
        return _done("UP_TO_DATE", f"이력이 이미 {end}까지 있다", ExitCode.OK)

    # 시험 복원 — 둘 다 미해결 0일 때만 기록한다
    for idx in indices:
        rc = _run(["universe", "rebuild", "--index", idx, "--end", f"{end}"], dry=True)
        if rc != ExitCode.OK:
            return _done("UNRESOLVED", f"{idx} {end}까지 복원에 미해결이 있다 — 이력을 "
                         "쓰지 않았다(구성 변경 시행일이면 다음 실행이 메운다)",
                         ExitCode.PARTIAL_SUCCESS)
    for idx in indices:
        rc = _run(["universe", "rebuild", "--index", idx, "--end", f"{end}"])
        if rc != ExitCode.OK:
            return _done("FAILED", f"{idx} 복원 기록 실패(종료 코드 {rc})", rc)
    for idx in indices:
        rc = _run(["universe", "verify", "--index", idx, "--end", f"{end}",
                   "--start", f"{end - timedelta(days=REFRESH_VERIFY_DAYS)}"])
        if rc != ExitCode.OK:
            return _done("FAILED", f"{idx} 재검산 불일치 — index_stocks.json은 그대로 "
                         "둔다. `universe verify` 전 구간으로 확인", rc)

    # index_stocks.json — 쓰는 곳은 update_index.py 하나다(유니버스 기준 universe-1.0)
    tool = os.path.join(project_root(), "tools", "update_index.py")
    if not args.log_json:
        print(f"\n▶ python tools/update_index.py", flush=True)
    proc = subprocess.run([sys.executable, tool], cwd=project_root(),
                          capture_output=args.log_json)
    file_data = read_index_file()
    keys = {"KOSPI200": "kospi200", "KOSDAQ150": "kosdaq150"}
    stale = [idx for idx in indices
             if str((file_data.get(idx) or file_data.get(keys[idx]) or {}).get("as_of"))
             != f"{end}"]
    results.append({"step": "tools/update_index.py", "exit": proc.returncode})
    if proc.returncode or stale:
        return _done("FAILED", f"index_stocks.json 갱신 실패({', '.join(stale)}) — "
                     "이력은 갱신됐다. `python tools/update_index.py`로 다시",
                     ExitCode.DATA_QUALITY_FAILED)
    moved = ", ".join(f"{idx} {before.get(idx)}→{end}" for idx in indices)
    return _done("SUCCESS", f"이력·index_stocks.json {end} 기준 ({moved})", ExitCode.OK)


def cmd_universe_security_master(args) -> int:
    """상장폐지 종목까지 포함한 종목 명부를 만든다(스키마 §5).

    키움 종목 목록에는 폐지 종목이 없다. KRX 일별매매정보는 그날 상장돼 있던
    종목을 그날 기준으로 주므로, 하루씩 훑으면 사라진 종목도 사라진 날짜와 함께
    남는다.
    """
    from ..ingestion.krx_openapi import market_for

    api = KrxOpenApi()
    markets = (sorted(MARKETS) if args.market == "BOTH" else [args.market])
    start = _parse_day(args.start) or DATA_START
    end = _parse_day(args.end) or date.today()
    days = _trading_days(max(start, DATA_START), end)
    if not days:
        print("거래일이 없다 — 달력을 먼저 만들어야 한다")
        return ExitCode.INSUFFICIENT_DATA

    # `Market.start`는 **지수** 이력 시작일이다(코스닥150은 2015-07-13).
    # 종목 일별매매정보는 두 시장 모두 DATA_START(2010-01-04)부터 있다 —
    # 실측: sto/ksq_bydd_trd 20100104에 1,036종목. 그러므로 여기서는
    # `Market.start`로 완화하지 않고 구간 전체를 요구한다.
    for name in markets:
        mk = market_for(name)
        missing = [d for d in days
                   if not api.cached(mk.stock_service, d.strftime("%Y%m%d"))]
        if missing:
            print(f"[{name}] 캐시에 없는 거래일이 {len(missing)}일 있다 — "
                  f"`universe krx-cache --market {name}`를 먼저 돌려라 "
                  f"(예: {missing[0]})")
            return ExitCode.INSUFFICIENT_DATA

    def _tick(day, n):
        if not args.log_json and day.month == 1 and day.day <= 5:
            print(f"  {day} — 누적 종목 {n:,}", flush=True)

    builder = SecurityMasterBuilder(api, markets=markets)
    summary = builder.build(days, run_id=args.run_id, dry_run=args.dry_run,
                            on_progress=_tick)
    summary["command"] = "universe security-master"
    summary["markets"] = markets
    _emit(summary, args.log_json)
    if not args.log_json:
        print(f"종목 {summary['securities']:,}개 / 행 {summary['rows']:,}개 "
              f"({summary['panel_start']} ~ {summary['panel_end']}, "
              f"{summary['trading_days']:,}거래일)")
        print(f"  구간 내 상장폐지 {summary['delisted']:,}종목 / "
              f"구간 내 신규상장 {summary['listed_during_panel']:,}종목 / "
              f"사명변경 {summary['renamed']:,}종목")
        for path in summary["artifacts"]:
            print(f"  manifest: {path}")
    return ExitCode.OK


def cmd_universe_verify(args) -> int:
    """적재된 이력을 시총 항등식으로 다시 검산한다(독립 확인)."""
    api = KrxOpenApi()
    repo = UniverseRepository()
    # 검산도 지수마다 시장이 다르다. 시장을 안 넘기면 코스닥 명단을 유가증권
    # 응답에서 찾아 합이 0이 되고, 차액이 곧 코스피200 지수값으로 나온다 —
    # "전부 불일치"로 보이지만 실제로는 다른 시장을 본 것이다.
    mk = market_for(args.index)
    start = _parse_day(args.start) or mk.start
    end = _parse_day(args.end) or date.today()
    days = _trading_days(max(start, mk.start), end)
    if args.sample and args.sample < len(days):
        step = len(days) / args.sample
        days = [days[int(i * step)] for i in range(args.sample)]

    checked = matched = 0
    bad = []
    for day in days:
        got = repo.get_members(args.index, day)
        codes = [sid.split(":", 1)[1] for sid in got["members"]]
        result = api.checksum(codes, day.strftime("%Y%m%d"), market=mk)
        checked += 1
        if result["diff"] == 0:
            matched += 1
        else:
            bad.append({"date": day.isoformat(), "diff": result["diff"],
                        "members": result["members"]})

    payload = {"command": "universe verify", "index_id": args.index,
               "checked": checked, "matched": matched,
               "mismatched": len(bad), "samples": bad[:20],
               "status": "SUCCESS" if not bad else "FAILED"}
    _emit(payload, args.log_json)
    if not args.log_json:
        print(f"{args.index} 검산 {checked}일 중 {matched}일 일치")
        for row in bad[:10]:
            # 지수값이 응답에 없으면 diff가 None이다 — 명단이 틀린 것과 구별한다
            shown = ("지수값 없음(KRX 응답)" if row["diff"] is None
                     else f"{row['diff']:,}")
            print(f"  ⚠ {row['date']} diff={shown}")
    return ExitCode.DATA_QUALITY_FAILED if bad else ExitCode.OK


def cmd_universe_members(args) -> int:
    repo = UniverseRepository()
    as_of = _parse_day(args.as_of) or date.today()
    got = repo.get_members(args.index, as_of,
                           max_gap_days=args.max_gap_days)
    payload = dict(got)
    payload["command"] = "universe members"
    payload["as_of"] = got["as_of"].isoformat()
    _emit(payload, args.log_json)
    if not args.log_json:
        print(f"{got['index_id']} {as_of} 구성종목 {got['member_count']}개")
        print(f"  universe_version: {got['universe_version']} / "
              f"경계 해상도: {got['date_resolution']}")
        print(f"  최대 불확실성: {got['max_uncertainty_days']}일")
        print(f"  {', '.join(got['members'][:12])}"
              f"{' …' if got['member_count'] > 12 else ''}")
    return ExitCode.OK


def cmd_universe_validate(args) -> int:
    repo = UniverseRepository()
    cov = repo.coverage(args.index)
    cov["command"] = "universe validate"
    _emit(cov, args.log_json)
    if not args.log_json:
        print(f"{cov['index_id']} 유니버스 이력")
        print(f"  구간 {cov['rows']}개 / 종목 {cov['securities']}개")
        print(f"  관측 {cov['observed_start']} ~ {cov['observed_end']} "
              f"(스냅샷 {len(cov['snapshot_dates'])}개)")
        print(f"  최대 간격 {cov['max_gap_days']}일 / "
              f"열린 구간 {cov['open_ended']}개")
        if len(cov["snapshot_dates"]) < 2:
            print("  ⚠ 스냅샷이 1개뿐 — 과거 replay 불가")
    return ExitCode.OK


# ─────────────────────────────────────────────────────────────
# minute-cache (Stage 4)
# ─────────────────────────────────────────────────────────────

def _resolve_minute_codes(args, cache):
    if args.codes:
        return [c.strip() for c in args.codes.split(",") if c.strip()], "--codes"
    path = args.index_file or os.path.join(project_root(), "data",
                                           "index_stocks.json")
    # 오늘 스냅샷이 있으면 그것을 우선한다(구성종목이 바뀌었을 수 있다).
    snap_store = SnapshotStore()
    files = snap_store.list_files(args.index)
    if files:
        snapshot = snap_store.load_all(args.index)[-1]
        codes = [sid.split(":", 1)[1] for sid in snapshot.securities]
        origin = f"유니버스 스냅샷 {snapshot.as_of}"
    else:
        codes, updated = load_index_codes(path, args.index)
        origin = f"{_rel(path)} (updated {updated})"
    if args.limit_securities:
        codes = codes[:args.limit_securities]
    return codes, origin


def cmd_minute_cache_build(args) -> int:
    from ..ingestion.market_source import minute_source
    cache = MinuteHistoryCache(source=minute_source(args.source))
    codes, origin = _resolve_minute_codes(args, cache)

    skipped = 0
    if args.skip_existing:
        from ..domain.ids import normalize_security_id

        have = cache.existing_securities(period=args.period)
        before = len(codes)
        codes = [c for c in codes if normalize_security_id(c) not in have]
        skipped = before - len(codes)
        if not codes:
            if not args.log_json:
                print(f"이미 {skipped}종목이 저장돼 있다 — 받을 것이 없다.")
            _emit({"command": "minute-cache build", "skipped": skipped,
                   "bars": 0, "status": "SUCCESS"}, args.log_json)
            return ExitCode.OK

    def _progress(i, total, record):
        if args.log_json:
            return
        note = ""
        if record["status"] != "SUCCESS":
            note = f" {record['error_code']} (시도 {record['attempts']}회"
            note += ", 미저장)" if record["rows_written"] == 0 else ")"
        print(f"  [{i}/{total}] {record['security_id']} "
              f"{record['bars']:>6}봉 {record['trading_days']:>3}일 "
              f"{record['first_date']}~{record['last_date']} "
              f"페이지 {record['pages']:>2} {record['seconds']}초 "
              f"{record['status']}{note}", flush=True)

    if not args.log_json:
        print(f"분봉 수집 시작 — {len(codes)}종목 ({origin})"
              + (f" / 이미 있는 {skipped}종목 건너뜀" if skipped else ""))
        print(f"  주기: {args.period}분 / 원천 {getattr(cache.source, 'SOURCE', 'KIWOOM_KA10080')}"
              f" — API가 주는 구간 전체를 받는다"
              + (f" / {args.end}까지만 저장" if args.end else ""), flush=True)

    summary = cache.build(codes, run_id=args.run_id, period=args.period,
                          retries=args.retries, flush_every=args.flush_every,
                          dry_run=args.dry_run, on_progress=_progress,
                          max_pages=args.max_pages,
                          end=_parse_day(getattr(args, "end", None)))
    summary["command"] = "minute-cache build"
    summary["source_of_codes"] = origin
    summary["skipped_existing"] = skipped
    _emit(summary, args.log_json)
    if not args.log_json:
        print(f"완료: {summary['bars']:,}봉 / 성공 {summary['succeeded']} "
              f"부분 {summary['partial']} 실패 {summary['failed']}")
        print(f"  소요 {summary['elapsed_sec']}초 "
              f"(종목당 {summary['sec_per_security']}초)")
        print(f"  가장 오래된 거래일: {summary['earliest_date']}")
        for path in summary["artifacts"][:4]:
            print(f"  manifest: {path}")
        if len(summary["artifacts"]) > 4:
            print(f"  … manifest {len(summary['artifacts'])}개")
    if summary["status"] == "FAILED":
        return ExitCode.DATA_QUALITY_FAILED
    if summary["status"] == "PARTIAL":
        return ExitCode.PARTIAL_SUCCESS
    return ExitCode.OK


def _parse_minute_as_of(value):
    """분봉 누적의 기준시각 — 날짜만 주면 그날 장 마감 뒤로 본다."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    day = date.fromisoformat(value)
    return datetime(day.year, day.month, day.day, 23, 59)


def cmd_minute_cache_catchup(args) -> int:
    """작업일 기준으로 비어 있는 분봉만 채운다(Stage 4B).

    하루 이틀 늦게 돌려도 되는 것이 요구사항이다. 그래서 "어제"를 기준으로
    삼지 않고 **커버리지와 API가 주는 거래일을 대조**한다.
    """
    from ..ingestion.market_source import minute_source
    catchup = MinuteCatchup(cache=MinuteHistoryCache(source=minute_source(args.source)))
    indices = tuple(x.strip().upper()
                    for x in (args.index or "").split(",") if x.strip())

    def _progress(i, total, record):
        if args.log_json:
            return
        note = "" if record["status"] == "SUCCESS" else f" {record['error_code']}"
        print(f"  [{i}/{total}] {record['security_id']} "
              f"{record['bars']:>6}봉 {record['trading_days']:>2}일 "
              f"페이지 {record['pages']:>2} {record['seconds']}초 "
              f"{record['status']}{note}", flush=True)

    if not args.log_json:
        print(f"분봉 일일 누적 — {args.period}분봉 / 최근 {args.days}거래일 확인",
              flush=True)

    summary = catchup.run(
        run_id=args.run_id, period=args.period, days=args.days,
        indices=indices or ("KOSPI200", "KOSDAQ150"),
        codes=([c.strip() for c in args.codes.split(",") if c.strip()]
               if args.codes else None),
        as_of=_parse_minute_as_of(getattr(args, "as_of", None)),
        since=_parse_day(getattr(args, "since", None)),
        min_completeness=args.min_completeness, probe_code=args.probe_code,
        dry_run=args.dry_run, retries=args.retries,
        flush_every=args.flush_every, on_progress=_progress,
        limit_securities=args.limit_securities,
        recheck=args.recheck, venue=args.venue)
    summary["command"] = "minute-cache catchup"
    _emit(summary, args.log_json)

    if not args.log_json:
        probe = summary["probe"]
        days = summary.get("target_days", summary["plan"]["target_days"]
                           if "plan" in summary else [])
        print(f"  기준 종목 {probe['probe_code']}: 거래일 {len(probe['days'])}일 "
              f"확인 (API 최고(最古) {probe['api_oldest']})")
        if probe["dropped_open"]:
            print(f"  ⚠ 아직 끝나지 않은 세션 제외: "
                  + ", ".join(str(d) for d in probe["dropped_open"]))
        if "plan" in summary:
            plan = summary["plan"]
            print(f"  대상 거래일 {len(plan['target_days'])}일 "
                  f"× 종목 {plan['universe_size']}개 "
                  f"→ 결측 {plan['pairs_missing']:,}쌍 / "
                  f"부분 {plan['pairs_partial']:,}쌍 / "
                  f"애프터마켓 미확정 {plan.get('pairs_after_pending', 0):,}쌍 "
                  f"(기대 {plan['expected_bars_per_day']}봉, "
                  f"기준 {plan['min_completeness']:.0%})")
            for day, info in sorted(summary["by_day"].items()):
                if info["missing"] or info["partial"] or                         info.get("after_pending"):
                    print(f"    {day}  결측 {info['missing']:>3} "
                          f"부분 {info['partial']:>3} "
                          f"애프터 미확정 {info.get('after_pending', 0):>3} "
                          f"완전 {info['complete']:>3}")
        if summary.get("fetch"):
            f = summary["fetch"]
            print(f"  수집: {f['securities']}종목, {f['min_date']} 이후, "
                  f"페이지 상한 {f['max_pages']}")
        if summary.get("note"):
            print(f"  {summary['note']}")
        if summary.get("bars"):
            print(f"완료: {summary['bars']:,}봉 저장, "
                  f"목표 {summary.get('targeted_pairs', 0):,}쌍 중 "
                  f"메워진 (종목,일) {summary.get('recovered_pairs', 0):,}쌍, "
                  f"{summary['elapsed_sec']}초")
            if summary.get("settled_dropped"):
                print(f"  장부에서 해제(정상화됨): "
                      f"{summary['settled_dropped']:,}쌍")
        if summary.get("settled_skipped_in_plan"):
            print(f"  장부상 더 받을 것이 없다고 확인된 건너뜀: "
                  f"{summary['settled_skipped_in_plan']:,}쌍 "
                  f"(--recheck로 다시 받을 수 있다)")
        for kind, count in (summary.get("unresolved_counts") or {}).items():
            label = {"BEFORE_API_HORIZON": "API 지평 밖(복구 불가)",
                     "NO_DATA": "응답에 해당일 없음",
                     "STILL_PARTIAL": "덜 받음 또는 무체결"}.get(kind, kind)
            print(f"  ⚠ 미해결 {label}: {count:,}쌍")

    if summary["status"] == "FAILED":
        return ExitCode.DATA_QUALITY_FAILED
    if summary["status"] == "PARTIAL":
        return ExitCode.PARTIAL_SUCCESS
    return ExitCode.OK


def cmd_minute_cache_cum_value(args) -> int:
    """과거 1분봉에 누적거래대금을 채운다 — KIS로 다시 받아 덮는다(`cum_value_backfill`).

    끊겨도 다시 돌리면 남은 (종목, 날짜)만 받는다. KIS는 약 1년만 거슬러 준다.
    """
    from datetime import timedelta

    from ..ingestion.cum_value_backfill import (
        PAGES_PER_DAY, CumValueBackfill, cum_value_targets,
    )
    from ..storage.parquet_store import ParquetStore

    today = datetime.now(tz=KST).date()
    start = _parse_day(args.start) or date(2026, 9, 1)
    end = _parse_day(args.end) or (today - timedelta(days=1))
    venues = tuple(v.strip().upper() for v in args.venue.split(",") if v.strip())
    secs = None
    if args.codes:
        secs = {f"{v}:{c.strip()}" for c in args.codes.split(",") if c.strip()
                for v in venues}
    store = ParquetStore()
    targets = cum_value_targets(store, start, end, venues=venues, securities=secs)
    if args.limit_securities:
        targets = dict(sorted(targets.items())[:args.limit_securities])
    pairs = sum(len(v) for v in targets.values())
    est_calls = sum(len(d) * PAGES_PER_DAY.get(sid.split(":")[0], 6)
                    for sid, d in targets.items())
    est_min = est_calls * 0.27 / max(1, args.workers) / 60
    if not args.log_json:
        by_venue = {}
        for sid, d in targets.items():
            v = sid.split(":")[0]
            by_venue[v] = by_venue.get(v, 0) + len(d)
        print(f"누적거래대금 채우기 — {start} ~ {end}, 종목 {len(targets)}개, "
              f"(종목,날) {pairs:,}쌍 {by_venue}")
        print(f"  예상 KIS 호출 약 {est_calls:,.0f}회 · 동시 {args.workers} · "
              f"약 {est_min:.0f}분(추정)", flush=True)
    run_id = args.run_id if args.run_id and args.run_id.startswith("topup_load_")         else data_load_run_id()
    # 파일로 보내면 print가 버퍼에 쌓여 진행 상황이 끝날 때까지 안 보인다
    summary = CumValueBackfill(store, workers=args.workers,
                               log=None if args.log_json else
                               (lambda m: print(m, flush=True))).run(
        targets, run_id, dry_run=args.dry_run)
    summary.update({"command": "minute-cache cum-value", "start": str(start),
                    "end": str(end), "venues": list(venues),
                    "estimated_calls": int(est_calls)})
    _emit(summary, args.log_json)
    if not args.log_json:
        if args.dry_run:
            print("  [dry-run] 받지 않았다.")
        else:
            print(f"완료: 채운 (종목,날) {summary['filled_pairs']:,} / 쓴 봉 "
                  f"{summary['written_rows']:,} / KIS 호출 {summary['api_calls']:,} / "
                  f"{summary['elapsed_sec'] / 60:.1f}분")
            if summary["missing_pairs"]:
                print(f"  ⚠ KIS가 주지 않은 (종목,날) {summary['missing_pairs']:,} — "
                      "수집 지평(약 1년) 밖이거나 그날 KIS 봉이 없다")
            if summary["failed"]:
                print(f"  ⚠ 받지 못한 종목 {len(summary['failed'])}개 — 다시 돌리면 이어서 받는다")
    if summary["failed"]:
        return ExitCode.PARTIAL_SUCCESS
    return ExitCode.OK


def cmd_minute_cache_validate(args) -> int:
    report = MinuteHistoryCache().validate(period=args.period)
    report["command"] = "minute-cache validate"
    _emit(report, args.log_json)
    if not args.log_json:
        print(f"{report['table']} (period={report['period_min']}분)")
        print(f"  종목 {report['securities']} / 거래일 {report['trading_dates']}")
        print(f"  구간 {report['date_range'][0]} ~ {report['date_range'][1]}")
        for session, stat in sorted(report["by_session"].items()):
            ratio = (stat["actual"] / stat["expected"]
                     if stat["expected"] else float("nan"))
            shown = f"{ratio:.3f}" if stat["expected"] else "기대치 미정의"
            print(f"  {session:<8} 일수 {stat['days']:>7,} / "
                  f"실제 {stat['actual']:>9,}봉 / 기대 대비 {shown} / "
                  f"불완전 {stat['incomplete']:,}일")
        print(f"  정규장 거래일 중앙값: {report['regular_days_median']}일")
        print("  · 기대 봉 수는 상한이다 — 5분봉은 그 구간에 체결이 있어야 "
              "생기므로 유동성이 낮으면 못 채우는 게 정상이다")
        if report.get("late_listed"):
            print(f"  · 뒤늦게 상장된 종목 {len(report['late_listed'])}개(정상)")
        if report["short_securities"]:
            print(f"  ⚠ 자기 구간을 다 못 채운 종목 "
                  f"{len(report['short_securities'])}개: "
                  f"{list(report['short_securities'])[:8]}")
        if report["pagination_incomplete_rows"]:
            print(f"  ⚠ 페이지네이션 미완결 행 "
                  f"{report['pagination_incomplete_rows']:,}건")
        print(f"  상태: {report['status']}")
    return (ExitCode.PARTIAL_SUCCESS if report["status"] == "PARTIAL"
            else ExitCode.OK)


def cmd_minute_cache_slice(args) -> int:
    """`as_of` 시점에 알 수 있었던 분봉만 잘라 본다(불변식 2 확인용)."""
    cache = MinuteHistoryCache()
    as_of = datetime.strptime(args.as_of, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    bars = cache.slice_as_of(args.security, as_of,
                             lookback_trading_days=args.bdays,
                             period=args.period)
    payload = {"command": "minute-cache slice", "security": args.security,
               "as_of": args.as_of, "bars": len(bars),
               "bdays": args.bdays, "period_min": args.period}
    if bars:
        payload["first_bar"] = bars[0]["bar_start"].isoformat()
        payload["last_bar_end"] = bars[-1]["bar_end"].isoformat()
        payload["sessions"] = sorted({b["session"] for b in bars})
    _emit(payload, args.log_json)
    if not args.log_json:
        print(f"{args.security} as_of={args.as_of} → {len(bars)}봉 "
              f"(최근 {args.bdays}거래일)")
        if bars:
            print(f"  첫 봉 시작: {payload['first_bar']}")
            print(f"  마지막 봉 종료: {payload['last_bar_end']} "
                  f"(as_of 이하여야 한다)")
            print(f"  세션: {', '.join(payload['sessions'])}")
    return ExitCode.OK


# ─────────────────────────────────────────────────────────────
# parser
# ─────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────
# candidate gate — 통계만으로 통과시키지 않는다
# ─────────────────────────────────────────────────────────────

def cmd_candidate_gate(args) -> int:
    """후보를 다섯 축(통계·경제성·안정성·실행가능성·확률품질)으로 판정한다.

    선택지 C 후보는 q=0.020으로 FDR을 통과했지만 20거래일에 +0.0802%p다.
    "유의하다"와 "거래할 가치가 있다"는 다른 질문이고, 두 번째 질문을 사람이
    문장으로만 적으면 다음 후보에서 기준이 또 흔들린다.
    """
    with open(args.candidates, encoding="utf-8") as f:
        payload = json.load(f)
    items = payload if isinstance(payload, list) else payload.get("candidates")
    if not items:
        raise CvdObvError(f"후보가 없다: {args.candidates}")

    thresholds = GateThresholds(
        alpha_q=args.alpha_q,
        min_annual_excess_bp=args.min_annual_excess_bp,
        min_observed_to_mde=args.min_observed_to_mde,
        min_daily_value_krw=args.min_daily_value_krw,
        max_participation_rate=args.max_participation_rate)
    rows = [evaluate_candidate(item, thresholds) for item in items]

    out = {"command": "candidate gate", "results": rows,
           "thresholds": thresholds.to_dict()}
    if args.out and not args.dry_run:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2, sort_keys=True,
                      default=str)
            f.write("\n")
        out["artifacts"] = [_rel(args.out)]

    _emit(out, args.log_json)
    if not args.log_json:
        print("후보 판정 — 축 하나라도 FAIL이면 전체 FAIL이다")
        for row in rows:
            mark = {"PASS": "O", "FAIL": "X"}.get(row["verdict"], "?")
            print(f"  [{mark}] {row['name']} h={row['horizon_td']} "
                  f"→ {row['verdict']}")
            for axis in ("statistical", "economic", "stability",
                         "executability", "probability"):
                print(f"        {axis:<14}{row['axes'][axis]:<8}"
                      f"{row['reasons'][axis]}")
        print()
        print("  UNKNOWN은 통과가 아니다 — 모르는 것을 통과로 세지 않는다")
        for path in out.get("artifacts", []):
            print(f"  생성: {path}")
    failed = sum(1 for r in rows if r["verdict"] == "FAIL")
    return ExitCode.OK if not failed else ExitCode.STATISTICAL_VALIDATION_FAILED


# ─────────────────────────────────────────────────────────────
# dependence measure — ρ를 문턱이 아니라 분산에 넣는다
# ─────────────────────────────────────────────────────────────

def cmd_dependence_measure(args) -> int:
    """두 유니버스를 **같은 날짜 블록으로 동시에** 재표집한다.

    따로 재표집하면 그 순간 독립이 가정으로 들어가고, 그 가정이 바로
    검사하려는 것이다.
    """
    def _load(path):
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        series = payload.get("series") if isinstance(payload, dict) else None
        if series is None and isinstance(payload, dict):
            series = payload
        if not series:
            raise CvdObvError(f"날짜별 계열을 찾지 못했다: {path}")
        return {str(k): float(v) for k, v in series.items()}

    result = joint_block_bootstrap(
        _load(args.series_a), _load(args.series_b),
        block_length=args.block_length, name_a=args.name_a,
        name_b=args.name_b, n_resamples=args.resamples, seed=args.seed)
    row = result.to_row()
    row["verdict"] = result.verdict()

    out = {"command": "dependence measure", "results": [row]}
    if args.out and not args.dry_run:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2, sort_keys=True,
                      default=str)
            f.write("\n")
        out["artifacts"] = [_rel(args.out)]

    _emit(out, args.log_json)
    if not args.log_json:
        print(f"의존성 — {args.name_a} vs {args.name_b} "
              f"(공통 {row['n_dates']:,}거래일, 블록 {row['block_length']})")
        rho = row["correlation"]
        if rho is None:
            print("  상관: 분산이 0이라 정의되지 않는다")
        else:
            print(f"  상관 rho      {rho:+.3f}  "
                  f"[{row['correlation_ci_low']:+.3f}, "
                  f"{row['correlation_ci_high']:+.3f}]")
            print(f"  유효 독립 표본 {row['effective_studies']:.2f}개  "
                  f"[{row['effective_studies_ci_low']:.2f}, "
                  f"{row['effective_studies_ci_high']:.2f}]  (최대 2개)")
        print(f"  결합 추정     {row['pooled_mean'] * 100:+.4f}%p  "
              f"[{row['pooled_ci_low'] * 100:+.4f}, "
              f"{row['pooled_ci_high'] * 100:+.4f}]")
        if row["pooled_ci_width_vs_independent"] is not None:
            print(f"  독립 가정 대비 구간 폭 "
                  f"{row['pooled_ci_width_vs_independent']:.2f}배")
        print(f"  판정: {row['verdict']}")
        print()
        print("  rho를 재기 전에 '표본이 두 배'라고 쓰지 않는다")
        for path in out.get("artifacts", []):
            print(f"  생성: {path}")
    return ExitCode.OK


# ─────────────────────────────────────────────────────────────
# cli reference — 문서를 손으로 적지 않는다
# ─────────────────────────────────────────────────────────────

def _walk_parser(parser, path=(), help_text=""):
    """(경로, 파서, 도움말)을 깊이 우선으로.

    argparse는 `add_parser(name, help=...)`의 help를 **부모 액션**에 달고
    자식 파서의 `description`에는 넣지 않는다. 그래서 부모에서 내려받는다 —
    안 그러면 설명 칸이 전부 빈다.
    """
    yield path, parser, help_text
    for action in parser._actions:                        # noqa: SLF001
        if not isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            continue
        helps = {a.dest: (a.help or "")
                 for a in getattr(action, "_choices_actions", [])}
        for name, sub in action.choices.items():
            yield from _walk_parser(sub, path + (name,), helps.get(name, ""))


def _summary(parser, help_text):
    """한 줄 설명. help가 있으면 그것, 없으면 함수 docstring 첫 줄."""
    if help_text:
        return help_text.strip()
    if parser.description:
        return parser.description.strip().split("\n")[0]
    func = parser.get_default("func")
    doc = (getattr(func, "__doc__", "") or "").strip()
    return doc.split("\n")[0] if doc else ""


def _detail(parser):
    """함수 docstring의 나머지 — **왜** 그 명령이 있는지가 여기 있다."""
    func = parser.get_default("func")
    doc = (getattr(func, "__doc__", "") or "").strip()
    if not doc:
        return ""
    rest = doc.split("\n", 1)
    return rest[1].strip() if len(rest) > 1 else ""


def _option_rows(parser):
    """그 파서가 **직접** 받는 옵션들. 공통 옵션도 숨기지 않는다."""
    rows = []
    for action in parser._actions:                        # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            continue
        if not action.option_strings:
            rows.append(("(위치)" + action.dest, "", action.help or ""))
            continue
        flags = ", ".join(f"`{s}`" for s in action.option_strings)
        default = action.default
        shown = "" if default in (None, False) else f"`{default}`"
        rows.append((flags, shown, (action.help or "").replace("|", "\\|")))
    return rows


def cmd_cli_reference(args) -> int:
    """파서에서 CLI 문서를 만든다.

    손으로 적은 명령 목록은 **반드시 코드와 어긋난다.** 실제로 어긋나 있었다
    (`cvdobv ...`와 `python cvdobv.py ...`가 섞여 있고 계획 명령과 구현
    이름이 달랐다). 파서가 유일한 출처가 되게 한다.
    """
    parser = build_parser()
    lines = ["# CLI 레퍼런스", "",
             "**이 파일은 `python cvdobv.py cli reference`가 만든다 — "
             "손으로 고치지 말 것.**", "",
             "실행 형태는 `python cvdobv.py <그룹> <동작> [옵션]` 하나다.",
             ""]

    leaves = [(p, q, h) for p, q, h in _walk_parser(parser) if p]
    by_path = {p: (q, h) for p, q, h in leaves}
    groups = sorted({p[0] for p, _, _ in leaves})
    lines += ["## 그룹", "", "| 그룹 | 설명 |", "|---|---|"]
    for name in groups:
        sub, help_text = by_path[(name,)]
        lines.append(f"| [`{name}`](#{name}) | {_summary(sub, help_text)} |")
    lines.append("")

    for name in groups:
        actions = sorted(p for p in by_path if len(p) == 2 and p[0] == name)
        if not actions:
            continue
        lines += [f"## `{name}`", ""]
        for path in actions:
            sub, help_text = by_path[path]
            lines += [f"### `{' '.join(path)}`", ""]
            summary = _summary(sub, help_text)
            if summary:
                lines += [summary, ""]
            detail = _detail(sub)
            if detail:
                lines += ["> " + line if line.strip() else ">"
                          for line in detail.split("\n")]
                lines.append("")
            rows = _option_rows(sub)
            if rows:
                lines += ["| 옵션 | 기본값 | 설명 |", "|---|---|---|"]
                lines += [f"| {f} | {d} | {h} |" for f, d, h in rows]
                lines.append("")

    text = "\n".join(lines).rstrip() + "\n"
    payload = {"command": "cli reference", "groups": len(groups),
               "commands": len([p for p in by_path if len(p) == 2])}
    if args.out and not args.dry_run:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        payload["artifacts"] = [_rel(args.out)]
    if args.check:
        current = ""
        if os.path.exists(args.out):
            with open(args.out, encoding="utf-8") as f:
                current = f.read()
        payload["up_to_date"] = (current == text)
        _emit(payload, args.log_json)
        if not payload["up_to_date"]:
            if not args.log_json:
                print("CLI 레퍼런스가 코드와 어긋난다 — "
                      "`cli reference`를 다시 돌려라")
            return ExitCode.SCHEMA_INCOMPATIBLE
        if not args.log_json:
            print("CLI 레퍼런스가 코드와 일치한다")
        return ExitCode.OK

    _emit(payload, args.log_json)
    if not args.log_json:
        print(f"CLI 레퍼런스 — 그룹 {payload['groups']}개 / "
              f"명령 {payload['commands']}개")
        for path in payload.get("artifacts", []):
            print(f"  생성: {path}")
    return ExitCode.OK


# ─────────────────────────────────────────────────────────────
# daily-signal — 일봉 패널을 포트폴리오 엔진에 연결한다
# ─────────────────────────────────────────────────────────────

def cmd_daily_signal_build(args) -> int:
    """일봉 feature에서 신호를 만들어 16년을 포트폴리오로 굴릴 수 있게 한다.

    Stage 7 엔진은 `gold.signals`를 읽는데, 그 표는 분봉 캐시가 있는
    252거래일짜리 replay가 만든 것이다. 그래서 포트폴리오 시뮬레이션이
    **코스피200이 +150% 오른 한 구간**에서만 돌았다.

    일봉 패널은 2010-01-04부터 4,114거래일이 있고 가격·OBV·유동성 조건은
    분봉 없이 계산된다. 이 명령이 그 사이를 잇는다.

    **점수는 만들지 않는다.** V1.5 총점은 CVD를 포함하므로 일봉만으로
    재현할 수 없고, 지어내면 검증이 아니라 새 지표 만들기가 된다.
    """
    emitter = DailyPanelSignalEmitter()
    summary = emitter.build(
        index_id=args.index, variant=args.variant,
        start=_parse_day(args.start), end=_parse_day(args.end),
        run_id=args.run_id, dry_run=args.dry_run)

    _emit(summary, args.log_json)
    if not args.log_json:
        print(f"일봉 패널 신호 — {args.index} / {args.variant} "
              f"({', '.join(summary['conditions']) or '조건 없음'})")
        print(f"  기간      {summary['first_date']} ~ {summary['last_date']}"
              f"  ({summary['trading_dates']:,}거래일, "
              f"{summary['securities']}종목)")
        print(f"  신호      {summary['signals']:,}건")
        print(f"    BUY     {summary['buy_candidate']:,}"
              f"  (선택률 {summary['selection_rate']:.1%})")
        print(f"    WATCH   {summary['watch']:,}")
        print(f"    자료부족 {summary['data_insufficient']:,}")
        print(f"  유니버스 밖이라 제외 {summary['skipped_not_member']:,}건")
        print()
        print("  이 신호에는 CVD가 없다 — evidence_tier=DAILY_ONLY_LONG,")
        print("  signal_source=DAILY_PANEL로 분봉 replay와 구별해 둔다")
        for path in summary.get("artifacts", []):
            print(f"  생성: {_rel(path)}")
    return ExitCode.OK


def _add_common(sub):
    sub.add_argument("--run-id", default=None,
                     help="실행 ID를 고정한다(미지정 시 자동 생성)")
    sub.add_argument("--config", default=None, help="설정 파일 경로")
    sub.add_argument("--log-json", action="store_true",
                     help="사람이 읽는 출력 대신 JSON 한 줄")
    sub.add_argument("--dry-run", action="store_true",
                     help="파일을 쓰지 않고 결과만 확인")


def build_parser():
    p = argparse.ArgumentParser(
        prog="cvdobv", description="CVD/OBV 검증 시스템 V2 CLI")
    sp = p.add_subparsers(dest="group", required=True)

    baseline = sp.add_parser("baseline", help="기준선 동결·검증(Stage 0)")
    bsub = baseline.add_subparsers(dest="action", required=True)

    freeze = bsub.add_parser("freeze", help="골든 샘플과 manifest 생성")
    freeze.add_argument("--golden", default=None, help="골든 샘플 파일 경로")
    freeze.add_argument("--manifest", default=None, help="manifest 파일 경로")
    _add_common(freeze)
    freeze.set_defaults(func=cmd_baseline_freeze)

    verify = bsub.add_parser("verify", help="현재 코드와 기준선 비교")
    verify.add_argument("--golden", default=None)
    verify.add_argument("--manifest", default=None)
    _add_common(verify)
    verify.set_defaults(func=cmd_baseline_verify)

    dload = sp.add_parser(
        "data-load", help="일일 적재 — 16:10 잠정 일봉, 익일 08:30 정규장 확정(봉 기준 bars-1.0)")
    dlsub = dload.add_subparsers(dest="action", required=True)
    for name, func, helptext in (
            ("daily", cmd_data_load_daily,
             "확정 — 익일 08:30(애프터마켓 확정 뒤면 언제든). 밀린 날을 정규장 복원"),
            ("provisional", cmd_data_load_provisional,
             "잠정 — 당일 16:10. 밀린 날을 먼저 확정하고 오늘 1분봉으로 잠정 일봉")):
        dl = dlsub.add_parser(name, help=helptext)
        dl.add_argument("--days", type=int, default=None,
                        help="최근 몇 거래일을 확인할지(기본: 장부의 마지막 확정일부터)")
        dl.add_argument("--since", default=None, help="이 날짜 이후(YYYY-MM-DD)")
        dl.add_argument("--index", default="KOSPI200,KOSDAQ150")
        _add_common(dl)
        dl.set_defaults(func=func)

    tsides = sp.add_parser("trade-sides",
                           help="KIS 매수/매도 체결량(FHKST03010800) 일일 적재")
    tssub = tsides.add_subparsers(dest="action", required=True)
    tsday = tssub.add_parser(
        "daily", help="16:10 오늘 잠정, 익일 08:30 어제 확정, 밀린 날 메우기(기본 UN)")
    tsday.add_argument("--market-div", default="UN",
                       help="쉼표 구분 J,NX,UN (기본 UN = KRX+NXT 통합)")
    tsday.add_argument("--index", default="KOSPI200,KOSDAQ150")
    tsday.add_argument("--since", default=None,
                       help="이 날짜부터 확정 미완 거래일을 본다(기본 30일 전)")
    _add_common(tsday)
    tsday.set_defaults(func=cmd_trade_sides_daily)

    daily = sp.add_parser("daily-cache", help="일봉 이력 수집·검증(Stage 1)")
    dsub = daily.add_subparsers(dest="action", required=True)

    dpanel = dsub.add_parser("krx-panel",
                             help="KRX 패널에서 일봉 생성(상장폐지 종목용)")
    dpanel.add_argument("--codes", default=None, help="쉼표로 구분한 종목코드")
    dpanel.add_argument("--missing-file", default=None,
                        help="누락 종목 JSON(`live`/`delisted` 키)")
    dpanel.add_argument("--delisted-only", action="store_true",
                        help="--missing-file의 delisted 그룹만 처리")
    dpanel.add_argument("--start", default=None)
    dpanel.add_argument("--end", default=None)
    _add_common(dpanel)
    dpanel.set_defaults(func=cmd_daily_cache_krx_panel)

    dreg = dsub.add_parser(
        "regular-from-minute",
        help="2026-09-14 이후 EXTENDED 일봉 → 1분봉으로 정규장(REGULAR) 일봉 복원")
    dreg.add_argument("--start", default=None, help="기본 2026-09-14")
    dreg.add_argument("--end", default=None, help="기본 오늘")
    dreg.add_argument("--codes", default=None, help="쉼표로 구분한 종목코드")
    _add_common(dreg)
    dreg.set_defaults(func=cmd_daily_cache_regular_from_minute)

    dreadj = dsub.add_parser(
        "readjust",
        help="수정주가 소급 감지 → 그 종목 일봉 이력 전체 재수집·파생 봉 재생성")
    dreadj.add_argument("--since", default=None, help="감지 구간 시작(기본 30일 전)")
    dreadj.add_argument("--codes", default=None,
                        help="쉼표로 구분한 종목코드(기본 현재 구성종목)")
    dreadj.add_argument("--audit", action="store_true",
                        help="지금 적재 원천(기본 KIS) 값과 저장값을 직접 대조(종목당 1회 호출)")
    _add_common(dreadj)
    dreadj.set_defaults(func=cmd_daily_cache_readjust)

    dnxth = dsub.add_parser(
        "nxt-history",
        help="9/14 전 정규장 일봉에 NXT 메인마켓 거래량(NXT 30분봉)을 더한다")
    dnxth.add_argument("--start", default=None, help="기본 2025-10-01(30분봉 시작)")
    dnxth.add_argument("--end", default=None, help="이 날 전까지(기본 2026-09-14)")
    dnxth.add_argument("--codes", default=None,
                       help="쉼표로 구분한 종목코드(기본: 그 기간 KIS NX 체결이 있는 종목)")
    dnxth.add_argument("--no-fetch", action="store_true",
                       help="30분봉을 받지 않고 저장분으로만 다시 만든다")
    _add_common(dnxth)
    dnxth.set_defaults(func=cmd_daily_cache_nxt_history)

    dprov = dsub.add_parser(
        "provisional",
        help="당일 1분봉 합으로 잠정 정규장 일봉(16:00 이후, 거래량 근사, 리비전 0)")
    dprov.add_argument("--date", default=None, help="기본 오늘")
    dprov.add_argument("--start", default=None,
                       help="구간 모드 — 이 날부터 --end(기본 오늘)까지 저장소가 아는 거래일")
    dprov.add_argument("--end", default=None)
    dprov.add_argument("--missing-only", action="store_true",
                       help="확정 봉도 잠정 봉도 없는 종목만(밀린 날 메우기)")
    dprov.add_argument("--index", default="KOSPI200,KOSDAQ150")
    dprov.add_argument("--codes", default=None, help="쉼표로 구분한 종목코드")
    _add_common(dprov)
    dprov.set_defaults(func=cmd_daily_cache_provisional)

    dbuild = dsub.add_parser("build", help="일봉을 수집해 Parquet에 저장")
    dbuild.add_argument("--index", default="KOSPI200",
                        choices=("KOSPI200", "KOSDAQ150"))
    dbuild.add_argument("--index-file", default=None)
    dbuild.add_argument("--codes", default=None,
                        help="쉼표로 구분한 종목코드. 지정하면 --index를 무시한다")
    dbuild.add_argument("--limit-securities", type=int, default=None,
                        help="앞에서 N종목만 — 소규모 실측용")
    dbuild.add_argument("--start", default=None, help="YYYY-MM-DD")
    dbuild.add_argument("--end", default=None, help="YYYY-MM-DD")
    dbuild.add_argument("--max-pages", type=int, default=120)
    dbuild.add_argument("--raw-prices", action="store_true",
                        help="수정주가 대신 원본 가격(키움 upd_stkpc_tp=0, KIS FID_ORG_ADJ_PRC=1)")
    dbuild.add_argument("--source", default=None, choices=("kis", "kiwoom"),
                        help="적재 원천(기본 환경변수 CVD_MARKET_SOURCE, 없으면 kis)")
    dbuild.add_argument("--flush-every", type=int, default=20,
                        help="N종목마다 Parquet으로 내려쓴다(메모리 상한)")
    dbuild.add_argument("--repair-failed", action="store_true",
                        help="이전 실행에서 불완전하게 수집된 종목만 다시 받는다. "
                             "--run-id를 이전보다 나중 값으로 주어야 새 데이터가 "
                             "우선된다")
    dbuild.add_argument("--retries", type=int, default=2,
                        help="종목별 재시도 횟수. 끝내 실패하면 그 종목은 "
                             "저장하지 않는다(잘린 시계열 방지)")
    dbuild.add_argument("--skip-existing", action="store_true",
                        help="이미 저장된 종목을 건너뛴다(중단된 수집 재개용). "
                             "행 존재 여부로만 판단하므로 부분 수집 종목도 "
                             "건너뛸 수 있다")
    _add_common(dbuild)
    dbuild.set_defaults(func=cmd_daily_cache_build)

    dvalidate = dsub.add_parser("validate", help="저장된 일봉 불변식 점검")
    _add_common(dvalidate)
    dvalidate.set_defaults(func=cmd_daily_cache_validate)

    minute = sp.add_parser("minute-cache", help="분봉 cache(Stage 4)")
    msub = minute.add_subparsers(dest="action", required=True)

    mbuild = msub.add_parser("build", help="API 가용 구간 전체 분봉 수집")
    mbuild.add_argument("--index", default="KOSPI200",
                        choices=("KOSPI200", "KOSDAQ150"))
    mbuild.add_argument("--index-file", default=None)
    mbuild.add_argument("--codes", default=None)
    mbuild.add_argument("--limit-securities", type=int, default=None)
    mbuild.add_argument("--period", type=int, default=1,
                        help="분봉 주기(분). 기본 1 — 봉 기준 bars-1.0은 1분봉만 "
                             "적재하고 다른 주기는 합성한다")
    mbuild.add_argument("--end", default=None,
                        help="이 거래일까지만 저장(YYYY-MM-DD). ka10080은 "
                             "호출 시점까지 주므로 장중에 돌리면 오늘의 "
                             "반쪽 세션이 섞인다. 기본은 자르지 않는다")
    mbuild.add_argument("--max-pages", type=int, default=None)
    mbuild.add_argument("--flush-every", type=int, default=10)
    mbuild.add_argument("--retries", type=int, default=2)
    mbuild.add_argument("--skip-existing", action="store_true",
                        help="이미 저장된 종목을 건너뛴다(중단된 수집 재개용)")
    mbuild.add_argument("--source", default=None, choices=("kis", "kiwoom"),
                        help="적재 원천(기본 환경변수 CVD_MARKET_SOURCE, 없으면 kis). "
                             "kis는 1분봉만 준다")
    _add_common(mbuild)
    mbuild.set_defaults(func=cmd_minute_cache_build)

    mcatch = msub.add_parser(
        "catchup",
        help="작업일 기준으로 비어 있는 분봉만 수집(일일 누적, 늦게 돌려도 됨)")
    mcatch.add_argument("--period", type=int, default=1,
                        help="분봉 주기(분). 기본 1")
    mcatch.add_argument("--days", type=int, default=10,
                        help="최근 몇 거래일을 확인할지(기본 10)")
    mcatch.add_argument("--since", default=None,
                        help="이 거래일 이후만 확인(YYYY-MM-DD)")
    mcatch.add_argument("--index", default="KOSPI200,KOSDAQ150",
                        help="쉼표 구분(기본 KOSPI200,KOSDAQ150)")
    mcatch.add_argument("--venue", default="KRX", choices=("KRX", "NXT"),
                        help="KRX(기본) 또는 NXT(넥스트레이드 봉, KIS NX·키움 <코드>_NX)")
    mcatch.add_argument("--source", default=None, choices=("kis", "kiwoom"),
                        help="적재 원천(기본 환경변수 CVD_MARKET_SOURCE, 없으면 kis)")
    mcatch.add_argument("--codes", default=None,
                        help="지수 대신 종목코드 직접 지정(쉼표 구분)")
    mcatch.add_argument("--limit-securities", type=int, default=None)
    mcatch.add_argument("--min-completeness", type=float,
                        default=DEFAULT_MIN_COMPLETENESS,
                        help="이 비율 미만이면 그날은 다시 받는다(기본 0.8)")
    mcatch.add_argument("--probe-code", default=DEFAULT_PROBE_CODE,
                        help="거래일 확인용 기준 종목(기본 005930)")
    mcatch.add_argument("--recheck", action="store_true",
                        help="재시도 억제 장부를 무시하고 기준 미달인 날을 "
                             "모두 다시 받는다(장부도 갱신하지 않는다)")
    mcatch.add_argument("--as-of", default=None,
                        help='기준시각 고정 "YYYY-MM-DD HH:MM"')
    mcatch.add_argument("--flush-every", type=int, default=10)
    mcatch.add_argument("--retries", type=int, default=2)
    _add_common(mcatch)
    mcatch.set_defaults(func=cmd_minute_cache_catchup)

    mcum = msub.add_parser(
        "cum-value",
        help="과거 1분봉에 누적거래대금(KIS) 채우기 — 다시 받아 덮는다, 재개 가능")
    mcum.add_argument("--start", default=None, help="기본 2026-09-01")
    mcum.add_argument("--end", default=None, help="기본 어제")
    mcum.add_argument("--venue", default="KRX,NXT", help="쉼표 구분(기본 KRX,NXT)")
    mcum.add_argument("--codes", default=None, help="쉼표로 구분한 종목코드")
    mcum.add_argument("--limit-securities", type=int, default=None)
    mcum.add_argument("--workers", type=int, default=4,
                      help="동시에 받는 종목 수(기본 4 — KIS 초당 한도 안)")
    _add_common(mcum)
    mcum.set_defaults(func=cmd_minute_cache_cum_value)

    mval = msub.add_parser("validate", help="커버리지·완결성 점검")
    mval.add_argument("--period", type=int, default=5)
    _add_common(mval)
    mval.set_defaults(func=cmd_minute_cache_validate)

    mslice = msub.add_parser("slice", help="as_of 시점 분봉 잘라 보기")
    mslice.add_argument("security", help="종목코드 또는 KRX:코드")
    mslice.add_argument("--as-of", required=True, help='"YYYY-MM-DD HH:MM"')
    mslice.add_argument("--bdays", type=int, default=5)
    mslice.add_argument("--period", type=int, default=5)
    _add_common(mslice)
    mslice.set_defaults(func=cmd_minute_cache_slice)

    replay = sp.add_parser("replay", help="과거 스캐너 replay(Stage 5)")
    rsub = replay.add_subparsers(dest="action", required=True)

    rrun = rsub.add_parser("run", help="거래일별 15:35 replay")
    rrun.add_argument("--index", default="KOSPI200",
                      choices=sorted(INDEX_PARAMS))
    rrun.add_argument("--start", default=None, help="YYYY-MM-DD")
    rrun.add_argument("--end", default=None)
    rrun.add_argument("--period", type=int, default=5,
                      help="V1.5 계산 해상도(분). 원천은 1분봉 합성(봉 기준 bars-1.0)")
    rrun.add_argument("--bdays", type=int, default=5)
    rrun.add_argument("--daily-scope", default="regular",
                      choices=("regular", "auto", "extended"),
                      help="기본 regular(정규장 일봉, 봉 기준 bars-1.0). "
                           "2026-10-03 이전 결과 재현은 auto")
    rrun.add_argument("--minute-source", default="standard",
                      choices=("standard", "native"),
                      help="standard=1분봉에서 --period 합성(기본), "
                           "native=저장된 원본 N분봉(과거 결과 재현용)")
    rrun.add_argument("--decision", default="AFTER_CLOSE",
                      choices=["AFTER_CLOSE", "AUCTION"],
                      help="AFTER_CLOSE=15:35 판정(연구 규약), "
                           "AUCTION=마감 -1분 판정(당일 종가 매수용). "
                           "AUCTION은 정규장이 미완이라 CVD 성분이 "
                           "온전하지 않다")
    rrun.add_argument("--rank-session", default=None,
                      choices=("REGULAR", "AFTER"))
    rrun.add_argument("--out", default=None, help="요약 JSON 경로")
    _add_common(rrun)
    rrun.set_defaults(func=cmd_replay_run)

    universe = sp.add_parser("universe", help="유니버스 이력(Stage 3)")
    usub = universe.add_subparsers(dest="action", required=True)

    usnap = usub.add_parser("snapshot", help="오늘 구성종목을 스냅샷으로 저장")
    usnap.add_argument("--index", default="KOSPI200",
                       choices=sorted(INDEX_PARAMS))
    usnap.add_argument("--as-of", default=None,
                       help="관측 날짜(기본 오늘). 과거 날짜를 넣어도 KIS·키움 모두 "
                            "현재 구성종목을 준다는 점에 주의")
    usnap.add_argument("--source", default=None, choices=("kis", "kiwoom"),
                       help="원천(기본 환경변수 CVD_MARKET_SOURCE, 없으면 kis). "
                            "kiwoom은 코스닥150을 찍지 못한다")
    _add_common(usnap)
    usnap.set_defaults(func=cmd_universe_snapshot)

    ucsv = usub.add_parser("import-csv", help="KRX 구성종목 CSV를 스냅샷으로")
    ucsv.add_argument("csv", help="KRX에서 내려받은 CSV 경로")
    ucsv.add_argument("--index", default="KOSPI200",
                      choices=sorted(INDEX_PARAMS))
    ucsv.add_argument("--as-of", required=True,
                      help="이 CSV가 어느 날짜 구성종목인가 (YYYY-MM-DD)")
    _add_common(ucsv)
    ucsv.set_defaults(func=cmd_universe_import_csv)

    ubuild = usub.add_parser("build", help="스냅샷 → membership 구간")
    ubuild.add_argument("--index", default="KOSPI200",
                        choices=sorted(INDEX_PARAMS))
    _add_common(ubuild)
    ubuild.set_defaults(func=cmd_universe_build)

    ucache = usub.add_parser("krx-cache",
                             help="KRX Open API 일별 응답 캐시(검산 재료)")
    ucache.add_argument("--start", default=None, help="YYYY-MM-DD")
    ucache.add_argument("--end", default=None, help="YYYY-MM-DD")
    ucache.add_argument("--workers", type=int, default=4)
    ucache.add_argument("--market", default="KOSPI200",
                        choices=sorted(MARKETS) + ["BOTH"],
                        help="받을 시장. 코스닥 종목 명부를 만들려면 코스닥 "
                             "일별매매정보도 캐시에 있어야 한다")
    _add_common(ucache)
    ucache.set_defaults(func=cmd_universe_krx_cache)

    unotice = usub.add_parser("notices", help="KRX 지수운영공지·첨부 수집")
    unotice.add_argument("--start-year", type=int, default=2009)
    unotice.add_argument("--end-year", type=int, default=date.today().year)
    unotice.add_argument("--no-attachments", action="store_true")
    _add_common(unotice)
    unotice.set_defaults(func=cmd_universe_notices)

    urebuild = usub.add_parser("rebuild",
                               help="항등식 검산으로 과거 구성종목 복원")
    urebuild.add_argument("--index", default="KOSPI200",
                          choices=sorted(INDEX_PARAMS))
    urebuild.add_argument("--start", default=None,
                          help="기본 2010-01-04(Open API 시작일)")
    urebuild.add_argument("--end", default=None)
    urebuild.add_argument("--anchor-window", type=int, default=20,
                          help="앵커 시행일을 검산으로 찾을 때 볼 거래일 폭. "
                               "공지는 시행 2~3주 전에 나오므로 기본 20거래일")
    _add_common(urebuild)
    urebuild.set_defaults(func=cmd_universe_rebuild)

    urefresh = usub.add_parser(
        "refresh", help="구성종목 이력 정기 갱신(16:10 적재 마지막 단계) — 스냅샷→"
                        "검산 재료→공지→복원→재검산→index_stocks.json")
    urefresh.add_argument("--index", default="KOSPI200,KOSDAQ150")
    urefresh.add_argument("--as-of", default=None,
                          help="스냅샷 날짜(기본 오늘). KIS는 언제나 현재 구성을 준다")
    _add_common(urefresh)
    urefresh.set_defaults(func=cmd_universe_refresh)

    umaster = usub.add_parser("security-master",
                              help="상장폐지 포함 종목 명부(스키마 §5)")
    umaster.add_argument("--start", default=None)
    umaster.add_argument("--end", default=None)
    umaster.add_argument("--market", default="KOSPI200",
                         choices=sorted(MARKETS) + ["BOTH"],
                         help="훑을 시장. BOTH는 유가증권+코스닥을 **같은 "
                              "패널 경계**로 한 번에 훑는다")
    _add_common(umaster)
    umaster.set_defaults(func=cmd_universe_security_master)

    uverify = usub.add_parser("verify", help="적재된 이력을 다시 검산")
    uverify.add_argument("--index", default="KOSPI200",
                         choices=sorted(INDEX_PARAMS))
    uverify.add_argument("--start", default=None)
    uverify.add_argument("--end", default=None)
    uverify.add_argument("--sample", type=int, default=0,
                         help="구간에서 N일만 뽑아 검산(0이면 전부)")
    _add_common(uverify)
    uverify.set_defaults(func=cmd_universe_verify)

    umem = usub.add_parser("members", help="특정 시점 구성종목 조회")
    umem.add_argument("--index", default="KOSPI200",
                      choices=sorted(INDEX_PARAMS))
    umem.add_argument("--as-of", default=None, help="YYYY-MM-DD")
    umem.add_argument("--max-gap-days", type=int, default=None,
                      help="경계 불확실성이 이보다 크면 조회를 막는다")
    _add_common(umem)
    umem.set_defaults(func=cmd_universe_members)

    uval = usub.add_parser("validate", help="이력 커버리지 점검")
    uval.add_argument("--index", default="KOSPI200",
                      choices=sorted(INDEX_PARAMS))
    _add_common(uval)
    uval.set_defaults(func=cmd_universe_validate)

    features = sp.add_parser("features", help="일봉 feature(Stage 2)")
    ftsub = features.add_subparsers(dest="action", required=True)
    ftbuild = ftsub.add_parser("build", help="일봉 지표와 Gate 조건 생성")
    ftbuild.add_argument("--obv-lookback", type=int, default=20,
                         help="OBV slope 계산 구간(거래일). feature_version에 포함된다")
    ftbuild.add_argument("--start", default=None, help="YYYY-MM-DD")
    ftbuild.add_argument("--end", default=None, help="YYYY-MM-DD")
    ftbuild.add_argument("--securities", default=None)
    _add_common(ftbuild)
    ftbuild.set_defaults(func=cmd_features_build)

    gate = sp.add_parser("daily-gate", help="Daily Gate 백테스트(Stage 2)")
    gsub = gate.add_subparsers(dest="action", required=True)
    gback = gsub.add_parser("backtest", help="조건별 상승확률과 기준선 대비 차이")
    gback.add_argument("--variants", default=None,
                       help=f"쉼표 구분. 기본 전체 (가능: {','.join(VARIANTS)})")
    gback.add_argument("--horizons", default="5,20",
                       help="h=1은 접었다 — 검정력이 모자란다(--horizons로 "
                            "직접 지정하면 경고와 함께 계산은 한다)")
    gback.add_argument("--outcomes", default="up,up_gross")
    gback.add_argument("--feature-version", default=None)
    gback.add_argument("--universe", default="KOSPI200")
    gback.add_argument("--no-membership", action="store_true",
                       help="유니버스 이력 필터를 끈다(모집단이 '한 번이라도 "
                            "지수에 있었던 종목 전체'가 된다)")
    gback.add_argument("--segment", default=None,
                       help="세그먼트 이름표(결과에 기록만 한다)")
    gback.add_argument("--segment-by", default=None,
                       choices=("year", "regime", "liquidity"),
                       help="연도·시장국면·유동성 구간으로 쪼개 평가한다")
    gback.add_argument("--base-variant", default="ALL",
                       help="짝지음 차이를 무엇과 견줄 것인가. 기본 ALL(그날 "
                            "전체). PRICE로 두면 '가격 위에 더하는 것이 "
                            "있는가'를 묻는 중첩 비교가 된다")
    gback.add_argument("--cost-multiplier", type=float, default=1.0,
                       help="비용 민감도. 2.0이면 수수료·세금·슬리피지를 두 배로")
    gback.add_argument("--start", default=None)
    gback.add_argument("--end", default=None)
    gback.add_argument("--resamples", type=int, default=1000)
    gback.add_argument("--seed", type=int, default=20260918)
    gback.add_argument("--out", default=None, help="결과 JSON 경로")
    _add_common(gback)
    gback.set_defaults(func=cmd_daily_gate_backtest)

    slabel = sp.add_parser("signal-label",
                           help="신호별 forward return(Stage 6)")
    slsub = slabel.add_subparsers(dest="action", required=True)
    slbuild = slsub.add_parser("build", help="신호에 진입·청산 수익률을 붙인다")
    slbuild.add_argument("--index", default="KOSPI200")
    slbuild.add_argument("--horizons", default="1,3,5,10,20")
    slbuild.add_argument("--signal-source", default=None,
                         choices=("LIVE", "REPLAY"),
                         help="기본은 전부. replay만 보려면 REPLAY")
    slbuild.add_argument("--start", default=None, help="YYYY-MM-DD")
    slbuild.add_argument("--end", default=None, help="YYYY-MM-DD")
    _add_common(slbuild)
    slbuild.set_defaults(func=cmd_signal_label_build)

    sgate = sp.add_parser("signal-gate", help="신호 성과 평가(Stage 6)")
    sgsub = sgate.add_subparsers(dest="action", required=True)
    sgeval = sgsub.add_parser(
        "evaluate", help="BUY_CANDIDATE vs WATCH, 점수·순위 단조성")
    sgeval.add_argument("--variants", default=None,
                        help="쉼표 구분. 기본 전체 (가능: %s)"
                             % ",".join(SIGNAL_VARIANTS))
    sgeval.add_argument("--horizons", default="5,20",
                        help="h=1은 접었다 — 검정력이 모자란다")
    sgeval.add_argument("--outcomes", default="up,up_gross")
    sgeval.add_argument("--index", default="KOSPI200")
    sgeval.add_argument("--segment-by", default=None,
                        choices=("regime", "month"),
                        help="시장국면·월로 쪼개 평가한다 "
                             "(replay 구간이 1년이라 연도는 쓸 수 없다)")
    sgeval.add_argument("--start", default=None)
    sgeval.add_argument("--end", default=None)
    sgeval.add_argument("--resamples", type=int, default=1000)
    sgeval.add_argument("--seed", type=int, default=20260919)
    sgeval.add_argument("--signal-source", default=None,
                        choices=list(SignalSource.values()),
                        help="신호 출처. 표에 둘 이상 섞여 있으면 필수다 — REPLAY는 분봉 252거래일(CVD 포함), DAILY_PANEL은 일봉 4,115거래일(CVD 없음)")
    sgeval.add_argument("--out", default=None, help="결과 JSON 경로")
    _add_common(sgeval)
    sgeval.set_defaults(func=cmd_signal_gate_evaluate)

    bt = sp.add_parser("backtest", help="포트폴리오 백테스트(Stage 7)")
    btsub = bt.add_subparsers(dest="action", required=True)
    btrun = btsub.add_parser("run", help="선택·체결 정책을 넣어 돌린다")
    btrun.add_argument("--policies", default="BUY_ALL",
                       help="쉼표 구분 (가능: %s)"
                            % ",".join(SELECTION_POLICIES))
    btrun.add_argument("--horizon", type=int, default=20)
    btrun.add_argument("--max-positions", type=int, default=20)
    btrun.add_argument("--max-entries-per-day", default="AUTO",
                       help="기본 AUTO = 슬롯수/horizon. 진입일이 몰리는 것을 "
                            "막는다")
    btrun.add_argument("--single-cohort", action="store_true",
                       help="하루 진입 상한을 없앤다 — 첫날 슬롯을 다 채우고 "
                            "h일마다 통째로 교체하는 단일 코호트가 된다")
    btrun.add_argument("--allow-overlap", action="store_true",
                       help="보유 중인 종목을 다시 살 수 있게 한다")
    btrun.add_argument("--liquidity-cap", type=float, default=0.01,
                       help="한 종목 진입 금액 <= 20일 평균 거래대금 x 이 값")
    btrun.add_argument("--no-liquidity-cap", action="store_true")
    btrun.add_argument("--capital", type=float, default=100_000_000.0)
    btrun.add_argument("--index", default="KOSPI200")
    btrun.add_argument("--start", default=None)
    btrun.add_argument("--end", default=None)
    btrun.add_argument("--oos-start", default=None,
                       help="이 날부터 out-of-sample로 따로 집계한다")
    btrun.add_argument("--signal-source", default=None,
                       choices=list(SignalSource.values()),
                       help="신호 출처. 표에 둘 이상 섞여 있으면 필수다 — "
                            "증거 계층이 다른 신호를 한 덩어리로 세지 않는다")
    btrun.add_argument("--random-seed", type=int, default=20260921,
                       help="RANDOM 대조군의 seed. 여러 개 돌려 분포를 봐야 "
                            "한다 — 한 번의 무작위도 표본 하나일 뿐이다")
    btrun.add_argument("--resamples", type=int, default=1000)
    btrun.add_argument("--seed", type=int, default=20260919)
    btrun.add_argument("--out", default=None, help="결과 JSON 경로")
    _add_common(btrun)
    btrun.set_defaults(func=cmd_backtest_run)

    power = sp.add_parser("power", help="검정력과 검출 가능 구간")
    psub = power.add_subparsers(dest="action", required=True)
    pplan = psub.add_parser(
        "plan", help="확증에 새로 얼마가 필요한가")
    pplan.add_argument("--results", default="reports/prereg-c/h1-*.json",
                       help="확증 대상 결과 JSON(글롭)")
    pplan.add_argument("--only", default=None,
                       help="이 조합만. 쉼표 구분 (예: PRICE+OBV)")
    pplan.add_argument("--alpha", type=float, default=0.05)
    pplan.add_argument("--power", type=float, default=0.80)
    pplan.add_argument("--days-per-year", type=int,
                       default=TRADING_DAYS_PER_YEAR)
    pplan.add_argument("--out", default=None, help="결과 JSON 경로")
    _add_common(pplan)
    pplan.set_defaults(func=cmd_power_plan)

    pfront = psub.add_parser(
        "frontier", help="보유기간 x 선택 크기별로 잡을 수 있는 최소 효과")
    pfront.add_argument("--horizons",
                        default=",".join(str(h) for h in FRONTIER_HORIZONS))
    pfront.add_argument("--sizes",
                        default=",".join(str(n) for n in FRONTIER_SIZES),
                        help="하루에 고를 종목 수")
    pfront.add_argument("--project-to", default="4114",
                        help="이만큼의 거래일이 있었다면 얼마까지 잡히나 "
                             "(기본 4,114 = 2010~2026 일봉 패널 길이)")
    pfront.add_argument("--index", default="KOSPI200")
    pfront.add_argument("--resamples", type=int, default=1000)
    pfront.add_argument("--seed", type=int, default=20260919)
    pfront.add_argument("--out", default=None, help="결과 JSON 경로")
    _add_common(pfront)
    pfront.set_defaults(func=cmd_power_frontier)

    dsig = sp.add_parser("daily-signal",
                         help="일봉 패널 → 신호(16년 포트폴리오용)")
    dsigsub = dsig.add_subparsers(dest="action", required=True)
    dsbuild = dsigsub.add_parser(
        "build", help="일봉 feature에서 Gate 신호를 만든다")
    dsbuild.add_argument("--index", default="KOSPI200")
    dsbuild.add_argument("--variant", default="PRICE+OBV",
                         choices=sorted(DAILY_GATE_VARIANTS),
                         help="Gate 조합 (기본: 선택지 C 후보)")
    dsbuild.add_argument("--start", default=None, help="YYYY-MM-DD")
    dsbuild.add_argument("--end", default=None, help="YYYY-MM-DD")
    _add_common(dsbuild)
    dsbuild.set_defaults(func=cmd_daily_signal_build)

    cand = sp.add_parser("candidate",
                         help="후보 판정 게이트(통계+경제성+안정성)")
    csub = cand.add_subparsers(dest="action", required=True)
    cgate = csub.add_parser(
        "gate", help="다섯 축으로 판정 — 하나라도 FAIL이면 FAIL")
    cgate.add_argument("--candidates", required=True,
                       help="후보 JSON(목록 또는 {\"candidates\": [...]})")
    cgate.add_argument("--alpha-q", type=float, default=0.05)
    cgate.add_argument("--min-annual-excess-bp", type=float, default=100.0,
                       help="비용 후 최소 연 초과수익(bp)")
    cgate.add_argument("--min-observed-to-mde", type=float, default=1.0,
                       help="관측 효과가 MDE의 몇 배 이상이어야 하는가")
    cgate.add_argument("--min-daily-value-krw", type=float, default=1e9)
    cgate.add_argument("--max-participation-rate", type=float, default=0.01)
    cgate.add_argument("--out", default=None, help="결과 JSON 경로")
    _add_common(cgate)
    cgate.set_defaults(func=cmd_candidate_gate)

    dep = sp.add_parser("dependence", help="두 유니버스의 의존성 측정")
    dsub2 = dep.add_subparsers(dest="action", required=True)
    dmeas = dsub2.add_parser(
        "measure", help="같은 날짜 블록으로 동시 재표집 — rho와 결합 추정")
    dmeas.add_argument("--series-a", required=True,
                       help="{날짜: 값} JSON")
    dmeas.add_argument("--series-b", required=True)
    dmeas.add_argument("--name-a", default="KOSPI200")
    dmeas.add_argument("--name-b", default="KOSDAQ150")
    dmeas.add_argument("--block-length", type=int, default=20,
                       help="보유기간 이상이어야 한다")
    dmeas.add_argument("--resamples", type=int, default=2000)
    dmeas.add_argument("--seed", type=int, default=20260921)
    dmeas.add_argument("--out", default=None, help="결과 JSON 경로")
    _add_common(dmeas)
    dmeas.set_defaults(func=cmd_dependence_measure)

    clig = sp.add_parser("cli", help="CLI 문서 생성·검사")
    clisub = clig.add_subparsers(dest="action", required=True)
    cliref = clisub.add_parser(
        "reference", help="파서에서 CLI 레퍼런스 문서를 만든다")
    cliref.add_argument("--out", default="docs/CLI_REFERENCE.md")
    cliref.add_argument("--check", action="store_true",
                        help="쓰지 않고 코드와 일치하는지만 확인")
    _add_common(cliref)
    cliref.set_defaults(func=cmd_cli_reference)

    abl = sp.add_parser("ablation", help="성분 제거와 검정력(Stage 8)")
    absub = abl.add_subparsers(dest="action", required=True)
    abrun = absub.add_parser(
        "run", help="성분을 빼 보고 MDE(잡을 수 있었던 최소 효과)를 낸다")
    abrun.add_argument("--variants", default=None,
                       help="쉼표 구분. 기본 전체 (가능: %s)"
                            % ",".join(ABLATION_VARIANTS))
    abrun.add_argument("--horizon", type=int, default=20)
    abrun.add_argument("--index", default="KOSPI200")
    abrun.add_argument("--start", default=None)
    abrun.add_argument("--end", default=None)
    abrun.add_argument("--resamples", type=int, default=1000)
    abrun.add_argument("--seed", type=int, default=20260919)
    abrun.add_argument("--config-hash", default=None,
                       help="평가할 replay 설정(config_hash). 한 지수에 설정이 "
                            "둘 이상이면 필수 — bdays나 period가 다른 실행이 "
                            "한 백분위 모집단으로 섞이면 점수 재구성이 "
                            "통째로 어긋난다")
    abrun.add_argument("--signal-source", default="REPLAY",
                       choices=list(SignalSource.values()),
                       help="기본 REPLAY — ablation은 V1.5 점수를 재구성하므로 CVD가 있는 신호에서만 뜻이 있다")
    abrun.add_argument("--out", default=None, help="결과 JSON 경로")
    _add_common(abrun)
    abrun.set_defaults(func=cmd_ablation_run)

    benchmark = sp.add_parser("benchmark", help="벤치마크 지수 일봉(Stage 1)")
    bmsub = benchmark.add_subparsers(dest="action", required=True)
    bmbuild = bmsub.add_parser("build", help="지수 일봉 수집")
    bmbuild.add_argument("--benchmarks", default=None,
                         help=f"쉼표 구분. 기본 KRX:KOSPI200 "
                              f"(가능: {', '.join(sorted(BENCHMARKS))})")
    bmbuild.add_argument("--start", default=None, help="YYYY-MM-DD")
    bmbuild.add_argument("--end", default=None, help="YYYY-MM-DD")
    bmbuild.add_argument("--source", default=None, choices=("kis", "kiwoom"),
                         help="적재 원천(기본 환경변수 CVD_MARKET_SOURCE, 없으면 kis)")
    _add_common(bmbuild)
    bmbuild.set_defaults(func=cmd_benchmark_build)

    calendar = sp.add_parser("calendar", help="거래 캘린더(Stage 1)")
    calsub = calendar.add_subparsers(dest="action", required=True)
    calbuild = calsub.add_parser("build", help="일봉에서 캘린더를 파생해 저장")
    _add_common(calbuild)
    calbuild.set_defaults(func=cmd_calendar_build)

    forward = sp.add_parser("forward", help="forward return 패널(Stage 1)")
    fsub = forward.add_subparsers(dest="action", required=True)
    fbuild = fsub.add_parser("build", help="무조건부 forward return 생성")
    fbuild.add_argument("--horizons", default="1,3,5,10,20")
    fbuild.add_argument("--entry-rule", default="NEXT_OPEN",
                        choices=list(ENTRY_RULES),
                        help="NEXT_OPEN=익일 시가, "
                             "NEXT_CLOSE=익일 종가(마감 동시호가 MOC), "
                             "SAME_CLOSE=당일 종가(시간외 종가매매)")
    fbuild.add_argument("--start", default=None, help="YYYY-MM-DD")
    fbuild.add_argument("--end", default=None, help="YYYY-MM-DD")
    fbuild.add_argument("--securities", default=None,
                        help="쉼표로 구분한 security_id(예: KRX:005930)")
    fbuild.add_argument("--benchmark", default=None,
                        help="초과수익 계산에 쓸 benchmark_id(예: KRX:KOSPI200). "
                             "없으면 초과수익은 null로 남는다")
    _add_common(fbuild)
    fbuild.set_defaults(func=cmd_forward_build)

    baserate = sp.add_parser("baserate", help="무조건부 기준선(Stage 1)")
    brsub = baserate.add_subparsers(dest="action", required=True)
    bcompute = brsub.add_parser("compute", help="상승확률 기준선과 신뢰구간")
    bcompute.add_argument("--horizons", default="1,3,5,10,20")
    bcompute.add_argument("--outcomes", default=None, help="기본: up,up_gross")
    bcompute.add_argument("--universe", default="KOSPI200")
    bcompute.add_argument("--start", default=None)
    bcompute.add_argument("--end", default=None)
    bcompute.add_argument("--resamples", type=int, default=1000)
    bcompute.add_argument("--seed", type=int, default=20260918)
    bcompute.add_argument("--out", default=None, help="결과 JSON 경로")
    _add_common(bcompute)
    bcompute.set_defaults(func=cmd_baserate_compute)

    contract = sp.add_parser("contract", help="스키마 계약 변환(Stage 0)")
    csub = contract.add_subparsers(dest="action", required=True)

    convert = csub.add_parser("convert", help="V1.5 live JSON → V2 gold 레코드")
    convert.add_argument("snapshot", help="V1.5 스냅샷 JSON 경로")
    convert.add_argument("--out", default=None, help="출력 JSON 경로")
    convert.add_argument("--signal-source", default="LIVE",
                         choices=("LIVE", "REPLAY"))
    convert.add_argument("--code-revision", default=None,
                         help="미지정 시 기준 구현 파일 해시를 쓴다")
    _add_common(convert)
    convert.set_defaults(func=cmd_contract_convert)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "run_id", None) is None:
        kind = args.group.replace("-", "_")
        args.run_id = make_run_id(f"cli_{kind}", datetime.now(tz=UTC))
    try:
        return int(args.func(args))
    except CvdObvError as exc:
        payload = exc.to_dict()
        if getattr(args, "log_json", False):
            print(json.dumps(payload, ensure_ascii=False, default=str),
                  file=sys.stderr)
        else:
            print(f"[{payload['error_type']}] {payload['message']}",
                  file=sys.stderr)
            if payload["detail_code"]:
                print(f"  detail_code: {payload['detail_code']}", file=sys.stderr)
        return int(exit_code_for(exc))
    except KeyboardInterrupt:
        print("중단됨", file=sys.stderr)
        return int(ExitCode.CLI_CONFIG_ERROR)

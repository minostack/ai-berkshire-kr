# -*- coding: utf-8 -*-
"""기준선 manifest — 무엇을 동결했는지 한 파일에 적는다.

Stage 0 작업 1: V1.5 파일의 SHA-256과 `_SCANNER_VERSION`,
`_MARKET_RULE_VERSION`을 manifest에 기록한다.

manifest는 "이 시점의 기준선은 이것"이라는 단 하나의 사실을 담는다. 값이
달라지면 `verify_manifest()`가 어디가 달라졌는지 말해 준다. 조용히 갱신하는
경로는 만들지 않는다 — 갱신은 `cvdobv baseline freeze`를 사람이 실행할 때만
일어난다.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from datetime import datetime

from .. import MARKET_RULE_VERSION, SCANNER_VERSION, SCHEMA_VERSION
from ..contracts.schema import (
    SCANNER_RUN_COLUMNS, SIGNAL_COLUMNS, SIGNAL_FEATURE_COLUMNS, column_names,
)
from ..contracts.v15_signals import CONTRACT_GAPS, source_sha256
from ..domain.errors import SchemaError
from ..domain.ids import UTC, sha256_of, utc_iso
from ..domain.redaction import assert_no_secrets
from ..domain.status import DetailCode, ExitCode
from . import fixtures as fx
from .golden import GOLDEN_PATH, GOLDEN_VERSION, load_golden
from .scanner_v15 import V15_PATH, load_scanner_v15, project_root

__all__ = ["MANIFEST_PATH", "FROZEN_FUNCTIONS", "build_manifest",
           "write_manifest", "load_manifest", "verify_manifest", "git_revision"]

MANIFEST_PATH = os.path.join(project_root(), "data", "manifests",
                             "baseline_v1_5.json")

#: 골든 샘플로 동결한 V1.5 함수(로드맵 Stage 0 작업 2).
FROZEN_FUNCTIONS = (
    "analyze", "calc_cvd", "calc_obv", "calc_price_trend", "calc_liquidity",
    "calc_risk", "_finalize_scores", "_compute_gate",
)


def git_revision(root: str = None):
    """HEAD SHA. git이 없거나 저장소가 아니면 None — 실패로 보지 않는다."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root or project_root(),
            capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    sha = (out.stdout or "").strip()
    return sha if out.returncode == 0 and len(sha) == 40 else None


def build_manifest(scanner=None, golden_path: str = None,
                   created_at: datetime = None):
    """현재 기준선 상태를 읽어 manifest 트리를 만든다."""
    sc = scanner or load_scanner_v15()
    source_hash = source_sha256(V15_PATH)

    declared = {
        "scanner_version": getattr(sc, "_SCANNER_VERSION", None),
        "market_rule_version": getattr(sc, "_MARKET_RULE_VERSION", None),
    }
    if declared["scanner_version"] != SCANNER_VERSION:
        raise SchemaError(
            f"기준 구현의 _SCANNER_VERSION이 {declared['scanner_version']}로, "
            f"V2가 기대하는 {SCANNER_VERSION}과 다르다")
    if declared["market_rule_version"] != MARKET_RULE_VERSION:
        raise SchemaError(
            f"기준 구현의 _MARKET_RULE_VERSION이 "
            f"{declared['market_rule_version']}로, V2 기대값과 다르다")

    gpath = golden_path or GOLDEN_PATH
    golden_info = {"path": os.path.relpath(gpath, project_root()).replace("\\", "/"),
                   "exists": os.path.exists(gpath)}
    if golden_info["exists"]:
        tree = load_golden(gpath)
        golden_info.update({
            "file_sha256": source_sha256(gpath),
            "content_sha256": sha256_of(tree),
            "golden_version": tree.get("golden_version"),
            "fixture_version": tree.get("fixture_version"),
            "as_of": tree.get("as_of"),
            "cases": sorted(tree.get("inputs", {})),
            "functions": sorted(tree.get("functions", {})),
        })

    manifest = {
        "manifest_type": "baseline_freeze",
        "schema_version": SCHEMA_VERSION,
        "created_at_utc": utc_iso(created_at or datetime.now(tz=UTC)),
        "baseline": {
            "path": os.path.relpath(V15_PATH, project_root()).replace("\\", "/"),
            "file_sha256": source_hash,
            "scanner_version": declared["scanner_version"],
            "market_rule_version": declared["market_rule_version"],
            "frozen_functions": list(FROZEN_FUNCTIONS),
            "session_config": sc._session_config_snapshot(),
        },
        "golden": golden_info,
        "fixture": {
            "version": fx.FIXTURE_VERSION,
            "scenarios": [
                {"name": n, "code": c, "drift": d, "shock_scale": sh,
                 "buy_bias": b, "daily_days": dd}
                for n, c, _label, d, sh, b, dd in fx.SCENARIOS
            ],
            "limitations": [
                "휴장일 미반영(주말만 제외)",
                "합성 가격·거래량 — 절대 수준에 의미 없음",
            ],
        },
        "contracts": {
            "gold.scanner_runs": column_names(SCANNER_RUN_COLUMNS),
            "gold.signals": column_names(SIGNAL_COLUMNS),
            "gold.signal_features": column_names(SIGNAL_FEATURE_COLUMNS),
            "v15_gaps": CONTRACT_GAPS,
        },
        "codes": {
            "exit_codes": {c.name: int(c) for c in ExitCode},
            "detail_codes": sorted(DetailCode.values()),
        },
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "git_revision": git_revision(),
        },
    }
    # manifest 자체의 내용 해시(자기 자신 제외)를 마지막에 붙인다.
    manifest["manifest_sha256"] = sha256_of(manifest)
    assert_no_secrets(manifest, what="baseline manifest")
    return manifest


def write_manifest(manifest=None, path: str = None) -> str:
    manifest = manifest if manifest is not None else build_manifest()
    target = path or MANIFEST_PATH
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    return target


def load_manifest(path: str = None):
    target = path or MANIFEST_PATH
    if not os.path.exists(target):
        raise SchemaError(
            f"기준선 manifest가 없다: {target} — `cvdobv baseline freeze` 먼저")
    with open(target, encoding="utf-8") as f:
        return json.load(f)


def verify_manifest(path: str = None, scanner=None):
    """저장된 manifest와 현재 상태를 비교한다.

    `created_at_utc`·`environment`처럼 실행마다 달라지는 값은 비교에서 뺀다.
    바뀌면 안 되는 것만 본다: 기준 파일 해시, 버전, 세션 설정, 열 계약, 골든
    샘플 내용 해시.
    """
    stored = load_manifest(path)
    current = build_manifest(scanner=scanner)
    checks, diffs = [], []

    def _cmp(label, a, b):
        same = a == b
        checks.append({"item": label, "ok": same,
                       "expected": a, "actual": b})
        if not same:
            diffs.append(label)

    _cmp("baseline.file_sha256",
         stored["baseline"]["file_sha256"], current["baseline"]["file_sha256"])
    _cmp("baseline.scanner_version",
         stored["baseline"]["scanner_version"],
         current["baseline"]["scanner_version"])
    _cmp("baseline.market_rule_version",
         stored["baseline"]["market_rule_version"],
         current["baseline"]["market_rule_version"])
    _cmp("baseline.session_config",
         stored["baseline"]["session_config"],
         current["baseline"]["session_config"])
    _cmp("golden.content_sha256",
         stored["golden"].get("content_sha256"),
         current["golden"].get("content_sha256"))
    _cmp("fixture.version",
         stored["fixture"]["version"], current["fixture"]["version"])
    for table in ("gold.scanner_runs", "gold.signals", "gold.signal_features"):
        _cmp(f"contracts.{table}",
             stored["contracts"][table], current["contracts"][table])
    _cmp("codes.exit_codes",
         stored["codes"]["exit_codes"], current["codes"]["exit_codes"])

    return {"ok": not diffs, "diffs": diffs, "checks": checks,
            "stored_manifest_sha256": stored.get("manifest_sha256"),
            "golden_version": GOLDEN_VERSION}

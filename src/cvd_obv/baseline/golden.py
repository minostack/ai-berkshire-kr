# -*- coding: utf-8 -*-
"""V1.5 핵심 함수의 골든 샘플 생성·검증.

Stage 0 작업 2: `analyze`, `calc_cvd`, `calc_obv`, `calc_price_trend`,
`calc_liquidity`, `_finalize_scores`, `_compute_gate`의 입출력을 고정한다.

골든 샘플은 "지금 값이 옳다"는 주장이 아니다. **바뀌면 반드시 알아차린다**는
장치다. 값이 달라졌는데 테스트가 통과하면 이후 모든 검증의 전제가 사라진다.
"""

from __future__ import annotations

import json
import os
from datetime import datetime

from .. import MARKET_RULE_VERSION, SCANNER_VERSION
from ..domain.errors import DataQualityError, SchemaError
from ..domain.ids import canonical_json, sha256_of
from . import fixtures as fx
from .scanner_v15 import load_scanner_v15, project_root

__all__ = [
    "GOLDEN_VERSION", "GOLDEN_DIR", "GOLDEN_PATH", "GOLDEN_AS_OF",
    "GOLDEN_END_DATE", "GOLDEN_PERIOD", "GOLDEN_BDAYS",
    "build_golden", "write_golden", "load_golden", "verify_golden", "diff_trees",
]

GOLDEN_VERSION = "1.0.0"

#: 기준 시각 — 정규장 마감 후, 애프터마켓 시작 전. 연구 규약의 신호 시각과 같다.
#: V1.5는 naive datetime을 쓰므로 여기서도 naive(KST 의미)로 둔다.
GOLDEN_AS_OF = datetime(2026, 9, 16, 15, 35, 0)
GOLDEN_END_DATE = "20260916"
GOLDEN_PERIOD = 5
GOLDEN_BDAYS = 5

GOLDEN_DIR = os.path.join(project_root(), "tests", "golden")
GOLDEN_PATH = os.path.join(GOLDEN_DIR, "v15_baseline.json")


def _digest(obj):
    """naive datetime을 문자열로 바꾼 뒤 해시한다.

    V1.5 봉 dict는 tz 없는 datetime(`bar_start`/`bar_end`)을 들고 있다.
    `canonical_json()`은 그런 값을 일부러 거부하므로, 골든 샘플 지문을 만들 때만
    KST 의미의 문자열로 바꿔 해시한다.
    """
    return sha256_of(_jsonable(obj))


def _bar_digest(bars):
    """입력 봉 묶음의 지문. 전량을 파일에 넣지 않고 해시로 고정한다."""
    return {
        "count": len(bars),
        "sha256": _digest(bars),
        "first": _jsonable(bars[0]) if bars else None,
        "last": _jsonable(bars[-1]) if bars else None,
    }


def _jsonable(value):
    """datetime을 문자열로 바꾼 JSON 직렬화 가능 트리."""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def build_golden(scanner=None):
    """현재 코드로 골든 트리를 만든다(파일에 쓰지는 않는다)."""
    sc = scanner or load_scanner_v15()

    cases = [fx.build_case(sc, name, GOLDEN_END_DATE,
                           period=GOLDEN_PERIOD, bdays=GOLDEN_BDAYS)
             for name, *_ in fx.SCENARIOS]

    inputs, functions = {}, {
        "calc_cvd": [], "calc_obv": [], "calc_price_trend": [],
        "calc_liquidity": [], "calc_risk": [], "analyze": [],
    }

    for case in cases:
        code = case["code"]
        minute = [b for b in case["minute_bars"] if b["bar_end"] <= GOLDEN_AS_OF]
        daily = case["daily_bars"]
        # V1.5 analyze()와 같은 전처리: 미완성 일봉 제외(기본 scope=auto)
        daily_done = [b for b in daily
                      if sc._is_bar_day_complete(b["dt"][:8], GOLDEN_AS_OF, "auto")]
        by_session = sc.split_bars_by_session(minute)
        trading_days = sorted({b["dt"][:8] for b in daily if len(b["dt"]) >= 8})

        inputs[case["scenario"]] = {
            "code": code,
            "name": case["name"],
            "daily_days": case["daily_days"],
            "minute_bars": _bar_digest(minute),
            "daily_bars": _bar_digest(daily),
            "daily_bars_completed": _bar_digest(daily_done),
            "bars_by_session": {k: len(v) for k, v in by_session.items()},
        }

        for session in (sc._SESSION_REGULAR, sc._SESSION_AFTER):
            kwargs = {
                "requested_days": GOLDEN_BDAYS if session == sc._SESSION_REGULAR else None,
                "period": GOLDEN_PERIOD,
                "session": session,
                "min_bars": 10 if session == sc._SESSION_REGULAR else sc._AFTER_MIN_BARS,
                "absent_is_error": session == sc._SESSION_REGULAR,
            }
            out = sc.calc_cvd(by_session[session], as_of=GOLDEN_AS_OF,
                              trading_days=trading_days, **kwargs)
            functions["calc_cvd"].append({
                "case": case["scenario"], "session": session,
                "args": dict(kwargs, as_of=GOLDEN_AS_OF.isoformat(sep=" "),
                             trading_days_count=len(trading_days),
                             bars_sha256=_digest(by_session[session])),
                "output": _jsonable(out),
            })

        obv = sc.calc_obv(daily_done, divergence_lookback=GOLDEN_BDAYS)
        functions["calc_obv"].append({
            "case": case["scenario"],
            "args": {"divergence_lookback": GOLDEN_BDAYS,
                     "bars_sha256": _digest(daily_done)},
            "output": _jsonable(obv)})

        price = sc.calc_price_trend(daily_done)
        functions["calc_price_trend"].append({
            "case": case["scenario"],
            "args": {"bars_sha256": _digest(daily_done)},
            "output": _jsonable(price)})

        liq = sc.calc_liquidity(daily_done)
        functions["calc_liquidity"].append({
            "case": case["scenario"],
            "args": {"n": 20, "bars_sha256": _digest(daily_done)},
            "output": _jsonable(liq)})

        risk = sc.calc_risk(daily_done,
                            close_vs_ma20_pct=price.get("close_vs_ma20_pct"))
        functions["calc_risk"].append({
            "case": case["scenario"],
            "args": {"close_vs_ma20_pct": price.get("close_vs_ma20_pct"),
                     "bars_sha256": _digest(daily_done)},
            "output": _jsonable(risk)})

    # analyze() — fetch 계열을 fixture로 바꿔치기해 네트워크 없이 통째로 태운다.
    original = fx.stub_fetchers(sc, cases)
    try:
        results = []
        for case in cases:
            out = sc.analyze(case["code"], token="GOLDEN_NO_TOKEN",
                             period=GOLDEN_PERIOD, bdays=GOLDEN_BDAYS,
                             name=case["name"], as_of=GOLDEN_AS_OF,
                             daily_scope="auto",
                             rank_session=sc._SESSION_REGULAR)
            if out is None:
                raise DataQualityError(
                    f"analyze()가 None을 반환했다: {case['scenario']}")
            out["index"] = "golden"
            results.append(out)
            functions["analyze"].append({
                "case": case["scenario"],
                "args": {"period": GOLDEN_PERIOD, "bdays": GOLDEN_BDAYS,
                         "as_of": GOLDEN_AS_OF.isoformat(sep=" "),
                         "daily_scope": "auto",
                         "rank_session": sc._SESSION_REGULAR},
                "output": _jsonable(out)})
    finally:
        fx.restore_fetchers(sc, original)

    # 횡단면 단계 — 유니버스 전체를 한 번에 본다.
    sc._finalize_scores(results)
    finalize_out = [{
        "code": r["code"],
        "cvd_points": r["cvd_points"], "obv_points": r["obv_points"],
        "price_points": r["price_points"], "liquidity_points": r["liquidity_points"],
        "liquidity_percentile": r["liquidity_percentile"],
        "risk_penalty": r["risk_penalty"], "total": r["total"],
        "gate_passed": r["gate_passed"], "status": r["status"],
        "gate_reasons": r["gate_reasons"],
    } for r in results]

    gate_out = [{"code": r["code"], "output": _jsonable(sc._compute_gate(r))}
                for r in results]

    return {
        "golden_version": GOLDEN_VERSION,
        "fixture_version": fx.FIXTURE_VERSION,
        "scanner_version": getattr(sc, "_SCANNER_VERSION", SCANNER_VERSION),
        "market_rule_version": getattr(sc, "_MARKET_RULE_VERSION",
                                       MARKET_RULE_VERSION),
        "as_of": GOLDEN_AS_OF.isoformat(sep=" "),
        "params": {"end_date": GOLDEN_END_DATE, "period": GOLDEN_PERIOD,
                   "bdays": GOLDEN_BDAYS, "daily_scope": "auto",
                   "rank_session": "REGULAR"},
        "inputs": _jsonable(inputs),
        "functions": functions,
        "pipeline": {
            "_finalize_scores": finalize_out,
            "_compute_gate": gate_out,
        },
    }


def write_golden(tree=None, path: str = None) -> str:
    tree = tree if tree is not None else build_golden()
    target = path or GOLDEN_PATH
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(tree, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    return target


def load_golden(path: str = None):
    target = path or GOLDEN_PATH
    if not os.path.exists(target):
        raise SchemaError(
            f"골든 샘플이 없다: {target} — `cvdobv baseline freeze`로 먼저 만든다")
    with open(target, encoding="utf-8") as f:
        return json.load(f)


def diff_trees(expected, actual, path: str = "$", out=None, limit: int = 50):
    """두 트리의 차이를 경로 문자열 목록으로. 값 비교는 정확히 한다."""
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if type(expected) is not type(actual) and not (
            isinstance(expected, (int, float)) and isinstance(actual, (int, float))):
        out.append(f"{path}: 타입 {type(expected).__name__} → {type(actual).__name__}")
        return out
    if isinstance(expected, dict):
        for k in sorted(set(expected) | set(actual)):
            if k not in expected:
                out.append(f"{path}.{k}: 새 키(값 {actual[k]!r})")
            elif k not in actual:
                out.append(f"{path}.{k}: 사라진 키(값 {expected[k]!r})")
            else:
                diff_trees(expected[k], actual[k], f"{path}.{k}", out, limit)
    elif isinstance(expected, list):
        if len(expected) != len(actual):
            out.append(f"{path}: 길이 {len(expected)} → {len(actual)}")
        for i in range(min(len(expected), len(actual))):
            diff_trees(expected[i], actual[i], f"{path}[{i}]", out, limit)
    elif expected != actual:
        out.append(f"{path}: {expected!r} → {actual!r}")
    return out


def verify_golden(path: str = None, scanner=None):
    """현재 코드 결과와 저장된 골든 샘플을 비교한다.

    돌려주는 dict: `{"ok": bool, "diffs": [...], "expected_hash", "actual_hash"}`.
    """
    expected = load_golden(path)
    actual = build_golden(scanner=scanner)
    # JSON 왕복을 거쳐 비교한다 — 저장된 쪽은 이미 왕복을 거쳤으므로 대칭을 맞춘다.
    actual = json.loads(json.dumps(actual, ensure_ascii=False, sort_keys=True))
    diffs = diff_trees(expected, actual)
    return {
        "ok": not diffs,
        "diffs": diffs,
        "expected_hash": _digest(expected),
        "actual_hash": _digest(actual),
        "canonical_len": len(canonical_json(actual)),
    }

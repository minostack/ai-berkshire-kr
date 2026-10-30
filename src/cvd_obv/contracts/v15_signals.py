# -*- coding: utf-8 -*-
"""V1.5 live JSON 스냅샷 → V2 `gold.*` 레코드 변환.

Stage 0 DoD: "V1.5 live JSON을 새 `signals` 스키마로 변환하는 contract test가
통과한다."

원칙
----
- **계산하지 않는다.** V1.5가 낸 값을 옮기고 이름을 스키마에 맞추기만 한다.
  순위처럼 스냅샷에 없는 값만 고정 규칙으로 파생하고, 그 규칙을 여기 적는다.
- **없는 값을 만들어내지 않는다.** V1.5가 기록하지 않은 열(`close`, `ma20` 등)은
  null로 두고 `CONTRACT_GAPS`에 남긴다. 0이나 평균으로 채우지 않는다.
- **조용한 실패가 없다.** 전제가 깨지면 예외를 던지거나 `warnings`에 남긴다.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from .. import MARKET_RULE_VERSION, SCANNER_VERSION, SCHEMA_VERSION
from ..domain.errors import ConfigError, SchemaError
from ..domain.ids import (
    KST, canonical_config_hash, kst_trading_date, legacy_v15_config_hash,
    make_run_id, make_signal_id, make_universe_snapshot_id,
    normalize_security_id, to_utc,
)
from ..domain.redaction import assert_no_secrets
from ..domain.status import (
    EvidenceTier, FeatureStatus, Session, SignalSource, SignalStatus,
)
from .schema import (
    SCANNER_RUN_COLUMNS, SCANNER_RUN_PK, SIGNAL_COLUMNS, SIGNAL_FEATURE_COLUMNS,
    SIGNAL_FEATURE_PK, SIGNAL_PK, assert_no_lookahead, validate_rows,
)

__all__ = [
    "CONTRACT_GAPS", "INDEX_ID_MAP", "ConversionResult",
    "convert_v15_snapshot", "load_v15_snapshot", "source_sha256",
    "parse_v15_datetime",
]

#: V1.5가 기록하지 않아 V2 스키마에서 null로 남는 열과 그 이유.
#: Stage 5(replay)에서 cache-backed 계산으로 채운다.
CONTRACT_GAPS = {
    "close": "V1.5 calc_price_trend()가 종가 수치를 반환하지 않는다",
    "ma20": "V1.5가 MA 수치를 반환하지 않는다(상회 여부만)",
    "ma60": "V1.5가 MA 수치를 반환하지 않는다",
    "ma120": "V1.5가 MA 수치를 반환하지 않는다",
    "ma20_slope": "V1.5는 기울기 방향(ma20_rising)만 기록한다",
    "obv_max_available_at_utc": "V1.5가 일봉 수신 시각을 기록하지 않는다",
    "price_max_available_at_utc": "V1.5가 일봉 수신 시각을 기록하지 않는다",
    "liquidity_max_available_at_utc": "V1.5가 일봉 수신 시각을 기록하지 않는다",
    "input_manifest_hash": "V1.5가 원시 분봉/일봉을 저장하지 않는다",
}

INDEX_ID_MAP = {
    "kospi200": "KOSPI200",
    "kosdaq150": "KOSDAQ150",
}

_V15_DT_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
)


class ConversionResult:
    """변환 결과 묶음."""

    __slots__ = ("run_row", "signals", "features", "warnings",
                 "config_hash", "universe_snapshot_id", "source_path")

    def __init__(self, run_row, signals, features, warnings,
                 config_hash, universe_snapshot_id, source_path=None):
        self.run_row = run_row
        self.signals = signals
        self.features = features
        self.warnings = list(warnings)
        self.config_hash = config_hash
        self.universe_snapshot_id = universe_snapshot_id
        self.source_path = source_path

    def summary(self):
        return {
            "run_id": self.run_row["run_id"],
            "signal_at_utc": self.run_row["signal_at_utc"].isoformat(),
            "index_id": self.run_row["index_id"],
            "universe_snapshot_id": self.universe_snapshot_id,
            "config_hash": self.config_hash,
            "signals": len(self.signals),
            "buy_candidates": sum(
                1 for s in self.signals
                if s["status"] == SignalStatus.BUY_CANDIDATE),
            "warnings": self.warnings,
        }


# ─────────────────────────────────────────────────────────────
# 입력 파싱
# ─────────────────────────────────────────────────────────────

def parse_v15_datetime(value, field: str = "datetime"):
    """V1.5가 쓴 naive 문자열을 KST tz-aware datetime으로."""
    if value in (None, ""):
        return None
    s = str(value).strip()
    for fmt in _V15_DT_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=KST)
        except ValueError:
            continue
    raise SchemaError(f"{field}: V1.5 시각 형식을 해석할 수 없다 — {value!r}")


def load_v15_snapshot(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def source_sha256(path) -> str:
    """`code_revision`용 소스 파일 해시 — `sha256:<64 hex>`."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


# ─────────────────────────────────────────────────────────────
# 값 변환 도우미
# ─────────────────────────────────────────────────────────────

def _f(value):
    """숫자만 float로. bool·문자열은 null로 떨어뜨린다(조용한 강제변환 금지)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _i(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _b(value):
    return value if isinstance(value, bool) else None


def _s(value):
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _status(value):
    s = _s(value)
    return s if s in ("SUCCESS", "PARTIAL", "FAILED") else None


def _feature_status(result):
    """family status → `gold.signals.feature_status`.

    CVD·OBV·가격추세 중 하나라도 FAILED면 FAILED(= V1.5 Gate가
    DATA_INSUFFICIENT로 보내는 축), 네 family 모두 SUCCESS면 SUCCESS,
    그 사이는 PARTIAL이다.
    """
    core = [
        (result.get("cvd") or {}).get("data_status"),
        (result.get("obv") or {}).get("data_status"),
        (result.get("price") or {}).get("data_status"),
    ]
    liq = (result.get("liquidity") or {}).get("data_status")
    if any(s == "FAILED" for s in core):
        return FeatureStatus.FAILED
    if all(s == "SUCCESS" for s in core) and liq == "SUCCESS":
        return FeatureStatus.SUCCESS
    return FeatureStatus.PARTIAL


# ─────────────────────────────────────────────────────────────
# 변환
# ─────────────────────────────────────────────────────────────

def convert_v15_snapshot(payload, *, code_revision: str,
                         signal_source: str = SignalSource.LIVE,
                         created_at: datetime = None,
                         data_revision: int = 1,
                         run_id: str = None,
                         source_path=None,
                         universe_codes=None) -> ConversionResult:
    """V1.5 스냅샷 dict → (`scanner_runs` row, `signals`, `signal_features`).

    `code_revision`은 호출자가 반드시 준다. 어떤 코드가 만든 값인지 모르는
    artifact는 재현성 주장을 할 수 없다.
    """
    if not isinstance(payload, dict):
        raise SchemaError("V1.5 스냅샷은 dict여야 한다")
    if not str(code_revision or "").strip():
        raise ConfigError("code_revision은 필수다")
    SignalSource.validate(signal_source)

    warnings = []

    scanner_version = _s(payload.get("scanner_version"))
    market_rule_version = _s(payload.get("market_rule_version"))
    if scanner_version is None or market_rule_version is None:
        # V1.5 이전 스냅샷은 어떤 계산식으로 만들었는지 알 수 없다. 기본값을
        # 씌워 "V1.5 결과"인 척하게 만들지 않는다.
        raise SchemaError(
            "scanner_version/market_rule_version이 없는 스냅샷은 변환하지 "
            "않는다 — V1.5 이전 형식으로 보인다")
    if scanner_version != SCANNER_VERSION:
        warnings.append(
            f"scanner_version이 기준선과 다르다: {scanner_version}"
            f" (기준 {SCANNER_VERSION})")
    if market_rule_version != MARKET_RULE_VERSION:
        warnings.append(
            f"market_rule_version이 기준선과 다르다: {market_rule_version}")

    config = payload.get("config")
    if not isinstance(config, dict):
        raise SchemaError("V1.5 스냅샷에 config가 없다")
    assert_no_secrets(config, what="V1.5 config")
    config_hash = canonical_config_hash(config)

    stored_short = _s(payload.get("config_hash"))
    if stored_short and legacy_v15_config_hash(config) != stored_short:
        warnings.append(
            "V1.5 config_hash 재계산값이 스냅샷 기록과 다르다 — "
            "설정 직렬화 방식이 바뀌었을 수 있다")

    signal_at = parse_v15_datetime(payload.get("as_of"), "as_of")
    if signal_at is None:
        raise SchemaError("V1.5 스냅샷에 as_of가 없다")
    signal_at_utc = to_utc(signal_at)
    signal_time_local = signal_at.astimezone(KST).strftime("%H:%M:%S")
    trading_date = kst_trading_date(signal_at)
    if signal_time_local != "15:35:00":
        warnings.append(
            f"신호 시각이 연구 규약(15:35 KST)과 다르다: {signal_time_local} — "
            "15:35 표본과 섞어 집계하지 말 것")

    index_key = _s(config.get("index")) or ""
    index_id = INDEX_ID_MAP.get(index_key.lower(), index_key.upper())
    if not index_id:
        raise SchemaError("config.index가 비어 있다")

    results = payload.get("results")
    if not isinstance(results, list) or not results:
        raise SchemaError("V1.5 스냅샷에 results가 없다")

    codes = [_s(r.get("code")) for r in results]
    if any(c is None for c in codes):
        raise SchemaError("results에 code가 없는 행이 있다")
    if len(set(codes)) != len(codes):
        raise SchemaError("results에 중복 종목이 있다")

    if universe_codes:
        # Stage 3 이후 경로: 그 시점 **구성종목 이력**에서 유니버스를 받는다.
        # 분석에 실패한 종목도 유니버스의 일부이므로, 결과 종목으로 유니버스를
        # 대신 만들면 그날의 모집단이 조용히 작아진다.
        universe_list = sorted({_s(c) for c in universe_codes if _s(c)})
        missing = sorted(set(codes) - set(universe_list))
        if missing:
            raise SchemaError(
                f"결과에 유니버스 밖 종목이 있다: {missing[:5]}"
                f"{' …' if len(missing) > 5 else ''}")
        universe_snapshot_id = make_universe_snapshot_id(
            index_id, signal_at, universe_list)
        if len(universe_list) != len(codes):
            warnings.append(
                f"유니버스 {len(universe_list)}종목 중 {len(codes)}종목만 "
                f"결과가 있다 — 나머지는 데이터 부족으로 분석되지 않았다")
    else:
        universe_snapshot_id = make_universe_snapshot_id(index_id, signal_at,
                                                        codes)
        warnings.append(
            "유니버스를 스냅샷 결과 종목으로 대신 구성했다 — V1.5는 시점별 "
            "구성종목 이력을 기록하지 않는다(universe_codes를 주면 이력을 쓴다)")

    created = created_at or datetime.now(tz=KST)
    created_utc = to_utc(created)
    out_run_id = run_id or make_run_id(
        "v15import", created_utc,
        nonce=(_s(payload.get("run_id")) or "00000000").lower()[-8:].rjust(8, "0"))

    last_input_bar = parse_v15_datetime(
        payload.get("last_input_bar_at"), "last_input_bar_at")
    last_input_bar_utc = to_utc(last_input_bar) if last_input_bar else None

    lineage = {
        "schema_version": SCHEMA_VERSION,
        "created_at_utc": created_utc,
        "run_id": out_run_id,
        "config_hash": config_hash,
        "code_revision": str(code_revision).strip(),
        "scanner_version": scanner_version,
        "market_rule_version": market_rule_version,
        "data_revision": int(data_revision),
    }

    signals, features = [], []
    for result in results:
        sig, feat = _convert_one(
            result, lineage=lineage, signal_source=signal_source,
            signal_at_utc=signal_at_utc, signal_time_local=signal_time_local,
            trading_date=trading_date, index_id=index_id,
            universe_snapshot_id=universe_snapshot_id,
            scanner_version=scanner_version, config_hash=config_hash,
            signal_at=signal_at, warnings=warnings)
        signals.append(sig)
        features.append(feat)

    _assign_ranks(signals, features)

    status_counts = {
        "SUCCESS": sum(1 for s in signals if s["feature_status"] == "SUCCESS"),
        "PARTIAL": sum(1 for s in signals if s["feature_status"] == "PARTIAL"),
        "FAILED": sum(1 for s in signals if s["feature_status"] == "FAILED"),
    }
    run_status = ("SUCCESS" if status_counts["FAILED"] == 0
                                and status_counts["PARTIAL"] == 0
                  else "PARTIAL" if status_counts["SUCCESS"] > 0 else "FAILED")

    run_row = dict(lineage)
    run_row.update({
        "signal_at_utc": signal_at_utc,
        "signal_time_local": signal_time_local,
        "signal_source": signal_source,
        "index_id": index_id,
        "universe_snapshot_id": universe_snapshot_id,
        "member_count": len(results),
        "attempted_count": int(payload.get("count") or len(results)),
        "success_count": status_counts["SUCCESS"],
        "partial_count": status_counts["PARTIAL"],
        "failed_count": status_counts["FAILED"],
        "last_input_bar_at_utc": last_input_bar_utc,
        "status": run_status,
        "error_code": None,
        "input_manifest_hash": None,
    })

    validate_rows([run_row], SCANNER_RUN_COLUMNS, SCANNER_RUN_PK,
                  "gold.scanner_runs")
    validate_rows(signals, SIGNAL_COLUMNS, SIGNAL_PK, "gold.signals")
    validate_rows(features, SIGNAL_FEATURE_COLUMNS, SIGNAL_FEATURE_PK,
                  "gold.signal_features")
    assert_no_lookahead(signals, features)
    assert_no_secrets(run_row, what="gold.scanner_runs")

    return ConversionResult(run_row, signals, features, warnings,
                            config_hash, universe_snapshot_id, source_path)


def _convert_one(result, *, lineage, signal_source, signal_at_utc,
                 signal_time_local, trading_date, index_id,
                 universe_snapshot_id, scanner_version, config_hash,
                 signal_at, warnings):
    code = _s(result.get("code"))
    security_id = normalize_security_id(code)
    signal_id = make_signal_id(scanner_version, config_hash,
                               universe_snapshot_id, signal_at, security_id)

    cvd = result.get("cvd") or {}
    cvd_regular = result.get("cvd_regular") or cvd
    cvd_after = result.get("cvd_after") or {}
    obv = result.get("obv") or {}
    price = result.get("price") or {}
    liquidity = result.get("liquidity") or {}
    risk = result.get("risk") or {}

    status = _s(result.get("status"))
    SignalStatus.validate(status)
    gate_reasons = result.get("gate_reasons") or []
    if not isinstance(gate_reasons, list):
        raise SchemaError(f"{code}: gate_reasons가 list가 아니다")

    rank_session = _s(result.get("rank_session")) or Session.REGULAR
    Session.validate(rank_session)

    last_bar = parse_v15_datetime(cvd.get("last_bar_at"), "cvd.last_bar_at")
    last_bar_utc = to_utc(last_bar) if last_bar else None
    if last_bar_utc is not None and last_bar_utc > signal_at_utc:
        # V1.5는 bar_end > as_of인 봉을 버리므로 여기 걸리면 데이터 문제다.
        raise SchemaError(
            f"{code}: 마지막 입력 봉({last_bar_utc.isoformat()})이 "
            f"신호 시각({signal_at_utc.isoformat()})보다 늦다")

    signal = dict(lineage)
    signal.update({
        "signal_id": signal_id,
        "signal_source": signal_source,
        "signal_at_utc": signal_at_utc,
        "signal_time_local": signal_time_local,
        "trading_date": trading_date,
        "security_id": security_id,
        "index_id": index_id,
        "universe_snapshot_id": universe_snapshot_id,
        "status": status,
        "gate_passed": bool(result.get("gate_passed")),
        "gate_reasons": [str(x) for x in gate_reasons],
        "total_score": _f(result.get("total")),
        "rank": None,
        "rank_population": None,
        "rank_percentile": None,
        "feature_status": _feature_status(result),
        # 분봉 CVD가 들어간 신호이므로 항상 단기 통합 증거 계층이다.
        "daily_evidence_tier": EvidenceTier.FULL_SIGNAL_SHORT,
        "last_input_bar_at_utc": last_bar_utc,
    })

    feature = {
        "signal_id": signal_id,
        "schema_version": lineage["schema_version"],
        "run_id": lineage["run_id"],
        "data_revision": lineage["data_revision"],

        "rank_session": rank_session,
        "cvd_normalized_delta": _f(cvd.get("normalized_delta")),
        "cvd_normalized_momentum": _f(cvd.get("normalized_momentum")),
        "cvd_bull_ratio_completed": _f(cvd_regular.get("bull_ratio_for_rank")),
        "cvd_bull_days": _i(cvd.get("bull_days")),
        "cvd_total_days": _i(cvd.get("total_days")),
        "cvd_completed_total_days": _i(cvd.get("completed_total_days")),
        "cvd_buy_ratio_pct": _f(cvd.get("buy_ratio")),
        "cvd_value": _f(cvd.get("cvd")),
        "cvd_intraday_delta": _f(cvd.get("intraday_delta")),
        "cvd_completeness": _f(cvd.get("completeness")),
        "cvd_latest_day_traded_value": _f(cvd.get("latest_day_traded_value")),
        "cvd_last_bar_age_min": _f(cvd.get("last_bar_age_min")),
        "cvd_momentum_minutes_actual": _i(cvd.get("momentum_minutes_actual")),
        "cvd_momentum_status": _s(cvd.get("momentum_status")),
        "cvd_data_status": _status(cvd.get("data_status")),
        "cvd_regular_data_status": _status(cvd_regular.get("data_status")),
        "cvd_after_data_status": _status(cvd_after.get("data_status")),
        "cvd_stale": _b(cvd.get("stale")),

        "obv": _f(obv.get("obv")),
        "obv_ma20": _f(obv.get("obv_ma20")),
        "obv_normalized_slope": _f(obv.get("normalized_slope")),
        "obv_up_ratio": _f(obv.get("up_ratio")),
        "obv_divergence": _s(obv.get("divergence")),
        "obv_pivot_divergence": _s(obv.get("pivot_divergence")),
        "obv_data_status": _status(obv.get("data_status")),

        # V1.5 미기록 — CONTRACT_GAPS 참고. 0으로 채우지 않는다.
        "close": None,
        "ma20": None,
        "ma60": None,
        "ma120": None,
        "ma20_slope": None,
        "above_ma20": _b(price.get("above_ma20")),
        "above_ma60": _b(price.get("above_ma60")),
        "above_ma120": _b(price.get("above_ma120")),
        "ma20_rising": _b(price.get("ma20_rising")),
        "ma60_rising": _b(price.get("ma60_rising")),
        "ma20_above_ma60": _b(price.get("ma20_above_ma60")),
        "close_vs_ma20_pct": _f(price.get("close_vs_ma20_pct")),
        "price_data_status": _status(price.get("data_status")),

        "avg_traded_value_20d": _f(liquidity.get("value")),
        "liquidity_days_used": _i(liquidity.get("days_used")),
        "liquidity_percentile": _f(result.get("liquidity_percentile")),
        "liquidity_data_status": _status(liquidity.get("data_status")),

        "atr": _f(risk.get("atr")),
        "atr_pct": _f(risk.get("atr_pct")),
        "overheated": _b(risk.get("overheated")),
        "risk_penalty": _f(result.get("risk_penalty")),
        "risk_data_status": _status(risk.get("data_status")),

        "cvd_score": _f(result.get("cvd_points")),
        "obv_score": _f(result.get("obv_points")),
        "price_score": _f(result.get("price_points")),
        "liquidity_score": _f(result.get("liquidity_points")),

        "cvd_max_available_at_utc": last_bar_utc,
        "obv_max_available_at_utc": None,
        "price_max_available_at_utc": None,
        "liquidity_max_available_at_utc": None,
    }
    return signal, feature


def _assign_ranks(signals, features):
    """횡단면 순위 — 아키텍처 §7·불변식 4, tie-break는 §6 `backtest_engine` 규칙.

    모집단: 같은 run 안에서 `feature_status == SUCCESS`이고 총점이 있는 종목.
    정렬: `total_score desc, avg_traded_value_20d desc, security_id asc`.
    백분위: 1위 = 1.0, 꼴찌 = 0.0. 모집단이 1이면 상대비교가 불가능하므로
    V1.5 `_percentile_ranks()`와 같은 관례로 0.5(중립)를 쓴다.
    """
    liq = {f["signal_id"]: f.get("avg_traded_value_20d") for f in features}
    pool = [s for s in signals
            if s["feature_status"] == FeatureStatus.SUCCESS
            and s["total_score"] is not None]
    pool.sort(key=lambda s: (-s["total_score"],
                             -(liq.get(s["signal_id"]) or 0.0),
                             s["security_id"]))
    n = len(pool)
    for i, s in enumerate(pool, start=1):
        s["rank"] = i
        s["rank_population"] = n
        s["rank_percentile"] = 0.5 if n == 1 else (n - i) / (n - 1)

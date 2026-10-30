# -*- coding: utf-8 -*-
"""정규화 직렬화와 ID 생성 규칙.

출처: `docs/DATA_SCHEMA_V2.md` §2, `docs/SYSTEM_ARCHITECTURE_V2.md` §6 `domain.ids`.

핵심 성질
---------
- `signal_id`는 **논리 신호**에 결정적이다. 같은 scanner_version·config·유니버스
  스냅샷·시각·종목이면 언제 몇 번을 재계산해도 같은 값이 나온다.
- `run_id`는 **실행 시도**마다 다르다. 같은 신호를 다시 만들면 `signal_id`는
  같고 `run_id`만 달라진다.
- 모든 해시 입력은 `canonical_json()` 한 가지 방식으로만 직렬화한다. 직렬화가
  흔들리면 해시가 흔들리고, 해시가 흔들리면 재현성 주장이 무너진다.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from .errors import ConfigError

__all__ = [
    "KST", "UTC",
    "canonical_json", "sha256_hex", "sha256_of", "canonical_config_hash",
    "legacy_v15_config_hash",
    "make_run_id", "run_started_at", "make_signal_id",
    "make_universe_snapshot_id",
    "make_experiment_id", "make_model_id",
    "normalize_security_id", "parse_security_id",
    "to_utc", "utc_iso", "kst_trading_date",
]

KST = ZoneInfo("Asia/Seoul")
UTC = timezone.utc

_HASH_PREFIX = "sha256:"
_RUN_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_NONCE_RE = re.compile(r"^[0-9a-f]{8}$")
_LOCAL_CODE_RE = re.compile(r"^[0-9A-Z]{4,12}$")
_SECURITY_ID_RE = re.compile(r"^(?P<market>[A-Z]{2,8}):(?P<code>[0-9A-Z]{4,12})$")
_SUFFIX_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


# ─────────────────────────────────────────────────────────────
# 정규화 직렬화
# ─────────────────────────────────────────────────────────────

def _normalize(value):
    """해시 입력으로 쓸 수 있는 형태로만 좁힌다.

    허용: None/bool/int/float/str/Decimal/datetime/date/Mapping/Sequence.
    datetime은 UTC ISO-8601(`...Z`)로, date는 `YYYY-MM-DD`로 고정한다.
    NaN·Inf는 직렬화 자체를 거부한다 — 해시가 같아도 의미가 같다고 볼 수 없다.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ConfigError(f"정규화 직렬화 불가: 비유한 실수 {value!r}")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ConfigError(f"정규화 직렬화 불가: 비유한 Decimal {value!r}")
        # 지수 표기를 피해 사람이 읽는 형태로 고정한다.
        return format(value, "f")
    if isinstance(value, datetime):
        return utc_iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        out = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise ConfigError(f"정규화 직렬화 불가: 문자열이 아닌 key {k!r}")
            out[k] = _normalize(v)
        return out
    if isinstance(value, (set, frozenset)):
        # 집합은 순서가 없으므로 정렬해 고정한다.
        return sorted(_normalize(v) for v in value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_normalize(v) for v in value]
    raise ConfigError(
        f"정규화 직렬화 불가: 지원하지 않는 타입 {type(value).__name__}")


def canonical_json(obj) -> str:
    """해시 전용 정규화 JSON 문자열.

    - key 정렬, 공백 없음, non-ASCII 그대로(UTF-8로 인코딩해 해시)
    - NaN/Inf 거부
    """
    return json.dumps(_normalize(obj), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def sha256_hex(data) -> str:
    """`sha256:<64 hex>` — 저장 형식(스키마 §2)."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return _HASH_PREFIX + hashlib.sha256(raw).hexdigest()


def sha256_of(obj) -> str:
    """임의 객체의 정규화 해시."""
    return sha256_hex(canonical_json(obj))


def canonical_config_hash(config) -> str:
    """설정 해시 — `sha256:<64 hex>` 전체값(축약 금지, 스키마 §2)."""
    if not isinstance(config, Mapping):
        raise ConfigError("config는 Mapping이어야 한다")
    return sha256_of(config)


def legacy_v15_config_hash(config) -> str:
    """V1.5 `_save_scan_snapshot()`이 쓰던 12자 축약 해시를 그대로 재현한다.

    V1.5 JSON과 V2 레코드를 연결할 때만 쓴다. 새 artifact에는
    `canonical_config_hash()`를 쓴다. V1.5는 `indent` 없이
    `sort_keys=True, ensure_ascii=False` 기본 구분자를 썼으므로 그 형태를
    그대로 유지한다.
    """
    raw = json.dumps(config, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


# ─────────────────────────────────────────────────────────────
# 시각
# ─────────────────────────────────────────────────────────────

def to_utc(value, assume_tz=None) -> datetime:
    """tz-aware datetime을 UTC로. naive는 기본적으로 거부한다.

    naive datetime을 조용히 KST로 간주하면 replay 시각이 9시간 밀려도
    아무도 모른다. 변환이 필요하면 `assume_tz`를 명시하게 한다.
    """
    if not isinstance(value, datetime):
        raise ConfigError(f"datetime이 아니다: {value!r}")
    if value.tzinfo is None:
        if assume_tz is None:
            raise ConfigError(
                "timezone 없는 datetime은 쓸 수 없다 — assume_tz를 명시할 것")
        value = value.replace(tzinfo=assume_tz)
    return value.astimezone(UTC)


def utc_iso(value, assume_tz=None) -> str:
    """`2026-09-16T06:35:00Z` 형태. 마이크로초는 0일 때 생략한다."""
    dt = to_utc(value, assume_tz=assume_tz)
    if dt.microsecond:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def kst_trading_date(value, assume_tz=None) -> date:
    """UTC timestamp에서 KST 거래일(달력일)을 뽑는다.

    거래일 = KST 달력일이라는 가정은 정규장·애프터마켓(~20:00 KST)이 모두
    같은 KST 날짜에 들어가므로 성립한다. 자정을 넘는 세션이 생기면
    `silver.market_calendar`를 근거로 바꿔야 한다.
    """
    return to_utc(value, assume_tz=assume_tz).astimezone(KST).date()


# ─────────────────────────────────────────────────────────────
# 종목 ID
# ─────────────────────────────────────────────────────────────

def normalize_security_id(code, market: str = "KRX") -> str:
    """`005930` → `KRX:005930`. 이미 정규형이면 검증만 하고 돌려준다."""
    s = str(code or "").strip().upper()
    if ":" in s:
        if not _SECURITY_ID_RE.match(s):
            raise ConfigError(f"security_id 형식 오류: {code!r}")
        return s
    if not _LOCAL_CODE_RE.match(s):
        raise ConfigError(f"종목코드 형식 오류: {code!r}")
    mk = str(market or "").strip().upper()
    if not re.match(r"^[A-Z]{2,8}$", mk):
        raise ConfigError(f"market 형식 오류: {market!r}")
    return f"{mk}:{s}"


def parse_security_id(security_id):
    """`KRX:005930` → `("KRX", "005930")`."""
    m = _SECURITY_ID_RE.match(str(security_id or "").strip().upper())
    if not m:
        raise ConfigError(f"security_id 형식 오류: {security_id!r}")
    return m.group("market"), m.group("code")


# ─────────────────────────────────────────────────────────────
# ID 생성
# ─────────────────────────────────────────────────────────────

_RUN_STAMP_RE = re.compile(r"(?<!\d)(\d{14})(?!\d)")
_RUN_DATE_RE = re.compile(r"(?<!\d)(\d{8})(?!\d)")


def run_started_at(run_id: str):
    """run_id에 박힌 실행 시각(UTC) — `make_run_id` 형식이면 초 단위.

    손으로 지은 id(`min1-20260926`, `topup-20260929-kospi200`)는 날짜만 있으므로
    그날 00:00 KST를 돌려준다 — 실제 실행 시각의 **하한**이라 "그 시각 전에
    받았는가" 판정에서 보수적인 쪽(전에 받았다고 봄)으로 틀린다. 시각을 읽을 수
    없으면 None.
    """
    hit = _RUN_STAMP_RE.search(run_id or "")
    if hit:
        return datetime.strptime(hit.group(1), "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc)
    hit = _RUN_DATE_RE.search(run_id or "")
    if hit:
        day = datetime.strptime(hit.group(1), "%Y%m%d")
        return day.replace(tzinfo=KST).astimezone(timezone.utc)
    return None


def make_run_id(kind: str, started_at: datetime, nonce=None) -> str:
    """`<kind>_<UTC yyyymmddHHMMSS>_<random8>` (스키마 §2).

    `nonce`를 주면 결정적이다 — 테스트와 재현 실행에서 쓴다.
    """
    k = str(kind or "").strip().lower()
    if not _RUN_KIND_RE.match(k):
        raise ConfigError(f"run kind 형식 오류: {kind!r} (소문자/숫자/밑줄)")
    stamp = to_utc(started_at).strftime("%Y%m%d%H%M%S")
    n = nonce if nonce is not None else secrets.token_hex(4)
    n = str(n).strip().lower()
    if not _NONCE_RE.match(n):
        raise ConfigError(f"nonce는 8자리 hex여야 한다: {nonce!r}")
    return f"{k}_{stamp}_{n}"


def make_signal_id(scanner_version: str, config_hash: str,
                   universe_snapshot_id: str, signal_at: datetime,
                   security_id: str) -> str:
    """논리 신호 ID — `sig_<64 hex>`.

    해시 입력은 스키마 §2가 정한 5개뿐이다. `run_id`, 수집 시각, 파일 경로처럼
    실행마다 달라지는 값은 절대 섞지 않는다.

    아키텍처 §6의 시그니처는 세 번째 인자를 `universe_id`로 적었지만,
    스키마 §2가 `universe_snapshot_id`를 해시 입력으로 못박았다. 지수 이름만
    쓰면 같은 지수의 구성 변경이 ID에 반영되지 않으므로 스키마를 따른다.
    """
    if not str(scanner_version or "").strip():
        raise ConfigError("scanner_version이 비어 있다")
    if not str(config_hash or "").startswith(_HASH_PREFIX):
        raise ConfigError(
            f"config_hash는 'sha256:' 전체값이어야 한다: {config_hash!r}")
    if not str(universe_snapshot_id or "").strip():
        raise ConfigError("universe_snapshot_id가 비어 있다")
    payload = {
        "scanner_version": str(scanner_version).strip(),
        "config_hash": str(config_hash).strip(),
        "universe_snapshot_id": str(universe_snapshot_id).strip(),
        "signal_at": utc_iso(signal_at),
        "security_id": normalize_security_id(security_id),
    }
    return "sig_" + sha256_of(payload)[len(_HASH_PREFIX):]


def make_universe_snapshot_id(index_id: str, as_of: datetime, members) -> str:
    """특정 시점 구성종목 스냅샷 ID — `usnap_<64 hex>`.

    `members`는 종목코드 문자열 목록이거나 membership row Mapping 목록이다.
    어느 쪽이든 정규화 후 정렬하므로 입력 순서에 의존하지 않는다.
    """
    rows = []
    for m in members:
        if isinstance(m, Mapping):
            rows.append(canonical_json(m))
        else:
            rows.append(normalize_security_id(m))
    if not rows:
        raise ConfigError("구성종목이 비어 있는 스냅샷은 만들 수 없다")
    if len(set(rows)) != len(rows):
        raise ConfigError("구성종목에 중복이 있다")
    payload = {
        "index_id": str(index_id).strip().upper(),
        "as_of": utc_iso(as_of),
        "members": sorted(rows),
        "member_count": len(rows),
    }
    return "usnap_" + sha256_of(payload)[len(_HASH_PREFIX):]


def make_experiment_id(spec, run_suffix: str) -> str:
    """백테스트 실험 ID — `exp_<16 hex>_<suffix>` (스키마 §2)."""
    suffix = str(run_suffix or "").strip().lower()
    if not _SUFFIX_RE.match(suffix):
        raise ConfigError(f"run_suffix 형식 오류: {run_suffix!r}")
    digest = sha256_of(spec)[len(_HASH_PREFIX):]
    return f"exp_{digest[:16]}_{suffix}"


def make_model_id(target_name: str, feature_schema_hash: str,
                  train_start: date, train_end: date,
                  algorithm: str, run_suffix: str) -> str:
    """모델 artifact ID — `mdl_<16 hex>_<suffix>` (스키마 §2)."""
    suffix = str(run_suffix or "").strip().lower()
    if not _SUFFIX_RE.match(suffix):
        raise ConfigError(f"run_suffix 형식 오류: {run_suffix!r}")
    payload = {
        "target_name": str(target_name).strip(),
        "feature_schema_hash": str(feature_schema_hash).strip(),
        "train_start": train_start,
        "train_end": train_end,
        "algorithm": str(algorithm).strip().lower(),
    }
    digest = sha256_of(payload)[len(_HASH_PREFIX):]
    return f"mdl_{digest[:16]}_{suffix}"

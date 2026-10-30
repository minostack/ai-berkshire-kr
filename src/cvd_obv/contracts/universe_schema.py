# -*- coding: utf-8 -*-
"""유니버스 이력과 종목 마스터 열 계약 — 스키마 §5.

로드맵 Stage 3이 요구하는 것은 "임의 날짜의 구성종목을 결정론적으로 조회"다.
그러려면 구성 시점이 구간으로 표현돼야 한다: `[effective_from, effective_to)`.

날짜 불확실성을 숨기지 않는다
-----------------------------
구성종목 이력을 **변경 공시**로 얻으면 편입·편출 시점이 하루 단위로 정확하다.
하지만 날짜별 **스냅샷**에서 역산하면 "두 스냅샷 사이 어딘가"까지만 알 수 있다.
그 차이를 `date_resolution`과 `uncertainty_days`로 남긴다. 구간을 정확한 것처럼
저장해 두면, 나중에 그 경계 근처 replay 결과가 왜 틀렸는지 알 수 없게 된다.
"""

from __future__ import annotations

from .schema import Column

__all__ = [
    "UNIVERSE_MEMBERSHIP_COLUMNS", "UNIVERSE_MEMBERSHIP_PK",
    "SECURITY_MASTER_COLUMNS", "SECURITY_MASTER_PK",
    "CHANGE_TYPES", "DATE_RESOLUTIONS",
]

#: 스키마 §5 `change_type`.
CHANGE_TYPES = ("INITIAL", "ADD", "REMOVE", "CORRECTION")

#: 구간 경계를 어떻게 알아냈는가.
#:   EXACT_ANNOUNCEMENT  변경 공시에서 직접 — 하루 단위로 정확
#:   SNAPSHOT_DERIVED    두 스냅샷 사이에서 역산 — 그 간격만큼 불확실
DATE_RESOLUTIONS = ("EXACT_ANNOUNCEMENT", "SNAPSHOT_DERIVED")

UNIVERSE_MEMBERSHIP_PK = ("index_id", "security_id", "effective_from",
                          "universe_version")

UNIVERSE_MEMBERSHIP_COLUMNS = [
    Column("index_id", "string", nullable=False),
    Column("security_id", "string", nullable=False),
    Column("effective_from", "timestamp", nullable=False, note="포함 시작"),
    Column("effective_to", "timestamp",
           note="제외 시각. null이면 현재까지 유효(open ended)"),
    Column("known_at_utc", "timestamp", nullable=False,
           note="이 사실을 알 수 있었던 시각. 사후 정정 look-ahead 검사용"),
    Column("change_type", "string", nullable=False, enum=CHANGE_TYPES),

    Column("date_resolution", "string", nullable=False,
           enum=DATE_RESOLUTIONS),
    Column("from_uncertainty_days", "int", nullable=False,
           note="effective_from이 앞으로 최대 며칠 틀릴 수 있는가"),
    Column("to_uncertainty_days", "int", nullable=False),
    Column("observed_from", "date",
           note="이 종목이 처음 관측된 스냅샷 날짜"),
    Column("observed_to", "date", note="마지막으로 관측된 스냅샷 날짜"),

    Column("source_ref", "string", nullable=False,
           note="공시 식별자 또는 스냅샷 파일명"),
    Column("universe_version", "string", nullable=False),
    Column("run_id", "string", nullable=False),
    Column("schema_version", "string", nullable=False),
    Column("data_revision", "int", nullable=False),
]


SECURITY_MASTER_PK = ("security_id", "valid_from")

SECURITY_MASTER_COLUMNS = [
    Column("security_id", "string", nullable=False),
    Column("local_code", "string", nullable=False),
    Column("market", "string", nullable=False),
    Column("name", "string", nullable=False),
    Column("isin", "string"),
    Column("listed_from", "date"),
    Column("delisted_on", "date",
           note="상장폐지일. 공급자가 폐지 종목을 주지 않으면 알 수 없어 null"),
    Column("predecessor_security_id", "string"),
    Column("successor_security_id", "string"),
    Column("valid_from", "date", nullable=False),
    Column("valid_to", "date"),
    Column("known_at_utc", "timestamp", nullable=False),
    Column("source", "string", nullable=False),
    Column("revision", "int", nullable=False),
    Column("run_id", "string", nullable=False),
    Column("schema_version", "string", nullable=False),
    Column("data_revision", "int", nullable=False),
]

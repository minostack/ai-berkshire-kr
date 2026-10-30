# -*- coding: utf-8 -*-
"""유니버스 기준 `universe-1.0` — 모든 스캐너·적재가 쓰는 코스피200·코스닥150 명단.

왜 필요한가 (2026-10-05 점검)
------------------------------
스캐너마다 명단을 따로 만들고 있었다.

- `data/index_stocks.json`(오늘 명단)을 직접 여는 곳: V1.5 `load_index`, CVD 가속,
  종가매수(`--date`로 과거를 돌려도 오늘 명단 — 생존편향), 데일리 스캐너 2곳,
  `live_feed.index_codes`, `load_index_codes`(일봉 적재)
- 날짜별 이력(`silver.universe_membership`)을 쓰는 곳: A·B 선정기, 분봉 catchup,
  replay, 백테스트
- 이력 밖 날짜 처리: A·B는 "가장 가까운 관측일 구성", catchup은 "이력 ∪ 오늘 명단",
  replay는 오류. 같은 날 도구마다 다른 명단이 나올 수 있었다.

규칙
----
| 함수 | 용도 | 이력 밖 날짜 |
|---|---|---|
| `universe_on(day)` | **스캐너**(실시간·`--date`) | 이력 마지막 관측일 뒤면 `index_stocks.json`과 이력 마지막 구성 중 **더 최근 것**, 경고를 단다. 이력 시작 전이면 오류(오늘 명단을 과거에 쓰지 않는다) |
| `universe_on(day, strict=True)` | 연구(replay·백테스트 표본) | 항상 오류 |
| `membership_intervals()` | 백테스트 모집단 필터 | 이력만 |
| `current_universe()` | 수집 대상 선정(적재), 종목 마스터 조회 | `index_stocks.json`만 |

`index_stocks.json`은 `tools/update_index.py`가 이력의 마지막 관측 구성(또는
키움·CSV)으로 쓴다. 쓰는 곳은 그 도구 하나다.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime

from .domain.errors import ConfigError, NoInputDataError, SurvivorshipError

__all__ = [
    "UNIVERSE_STANDARD_VERSION", "INDEXES", "FILE_KEYS", "parse_indexes",
    "index_file_path", "read_index_file", "Member", "Universe",
    "current_universe", "universe_on", "membership_intervals",
    "flat_intervals", "member_index", "STATUS_HISTORICAL",
    "STATUS_INDEX_FILE", "STATUS_HISTORY_NEAREST",
]

UNIVERSE_STANDARD_VERSION = "universe-1.0"
INDEXES = ("KOSPI200", "KOSDAQ150")
#: `index_stocks.json`의 키
FILE_KEYS = {"KOSPI200": "kospi200", "KOSDAQ150": "kosdaq150"}

_ALIASES = {
    "kospi200": ("KOSPI200",), "kosdaq150": ("KOSDAQ150",),
    "kospi": ("KOSPI200",), "kosdaq": ("KOSDAQ150",),
    "both": INDEXES, "all": INDEXES,
}

#: 지수별 명단의 출처
STATUS_HISTORICAL = "HISTORICAL"            # 이력이 그 날짜를 덮는다
STATUS_INDEX_FILE = "INDEX_FILE"            # 이력 밖 → index_stocks.json
STATUS_HISTORY_NEAREST = "HISTORY_NEAREST"  # 이력 밖 → 이력 마지막 구성


def parse_indexes(value=None) -> tuple:
    """`"kospi200"`, `"both"`, `"KOSPI200,KOSDAQ150"`, 목록 → `("KOSPI200", …)`."""
    if value is None:
        return INDEXES
    parts = value.split(",") if isinstance(value, str) else list(value)
    out = []
    for p in parts:
        p = str(p).strip()
        if not p:
            continue
        got = _ALIASES.get(p.lower()) or ((p.upper(),) if p.upper() in INDEXES else None)
        if got is None:
            raise ConfigError(f"알 수 없는 지수: {p} — {INDEXES} 또는 {sorted(_ALIASES)}")
        out += [g for g in got if g not in out]
    if not out:
        raise ConfigError("지수가 비어 있다")
    return tuple(out)


def index_file_path() -> str:
    from .baseline.scanner_v15 import project_root
    return os.path.join(project_root(), "data", "index_stocks.json")


def _as_date(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def read_index_file(path: str = None) -> dict:
    """`index_stocks.json` → `{지수: {"as_of", "updated", "source", "stocks"}}`.

    파일이 없거나 깨졌으면 빈 dict. 지수 블록이 목록(예전 형식)이어도 읽는다.
    """
    try:
        with open(path or index_file_path(), encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return {}
    out = {}
    for index_id, key in FILE_KEYS.items():
        block = payload.get(key)
        if block is None:
            continue
        if isinstance(block, dict):
            stocks, meta = block.get("stocks") or [], block
        else:
            stocks, meta = block or [], {}
        rows = []
        for item in stocks:
            code = item.get("code") if isinstance(item, dict) else item
            code = str(code or "").strip()
            if code:
                name = item.get("name") if isinstance(item, dict) else None
                rows.append({"code": code, "name": name or code})
        out[index_id] = {"as_of": _as_date(meta.get("as_of")),
                         "updated": meta.get("updated"),
                         "source": meta.get("source"), "stocks": rows}
    return out


class Member:
    __slots__ = ("code", "name", "index_id")

    def __init__(self, code: str, name: str, index_id: str):
        self.code, self.name, self.index_id = str(code), name or str(code), index_id

    @property
    def security_id(self) -> str:
        return f"KRX:{self.code}"

    def __repr__(self):
        return f"Member({self.code}, {self.name}, {self.index_id})"


class Universe:
    """어떤 날짜의 명단 — 지수별 출처와 경고를 함께 들고 다닌다."""

    def __init__(self, as_of, indexes, members, status, sources, warnings):
        self.as_of = as_of
        self.indexes = tuple(indexes)
        self._members = list(members)
        self.status = dict(status)        # 지수 → STATUS_*
        self.sources = dict(sources)      # 지수 → 사람이 읽는 출처
        self.warnings = list(warnings)
        self._by_code = {}
        for m in self._members:
            self._by_code.setdefault(m.code, m)

    def __len__(self):
        return len(self._by_code)

    def __contains__(self, code_or_sid):
        return str(code_or_sid).split(":", 1)[-1] in self._by_code

    def members(self, index_id: str = None) -> list:
        return [m for m in self._members if index_id is None or m.index_id == index_id]

    def codes(self, index_id: str = None) -> list:
        return sorted({m.code for m in self.members(index_id)})

    def security_ids(self, index_id: str = None) -> set:
        return {m.security_id for m in self.members(index_id)}

    def name(self, code_or_sid, default=None):
        m = self._by_code.get(str(code_or_sid).split(":", 1)[-1])
        return m.name if m else (default if default is not None else str(code_or_sid))

    def index_of(self, code_or_sid):
        m = self._by_code.get(str(code_or_sid).split(":", 1)[-1])
        return m.index_id if m else None

    def stocks(self, index_id: str) -> list:
        """V1.5 `load_index()`와 같은 형태 — `[{"code", "name"}, …]`."""
        return [{"code": m.code, "name": m.name} for m in self.members(index_id)]

    @property
    def historical(self) -> bool:
        return all(s == STATUS_HISTORICAL for s in self.status.values())

    def describe(self) -> dict:
        """스냅샷·리포트에 남길 요약."""
        return {"universe_standard": UNIVERSE_STANDARD_VERSION,
                "as_of": self.as_of.isoformat() if self.as_of else None,
                "counts": {i: len(self.codes(i)) for i in self.indexes},
                "status": self.status, "sources": self.sources}


# ─────────────────────────────────────────────────────────────
# 명단 만들기
# ─────────────────────────────────────────────────────────────

def _security_master_names(store=None) -> dict:
    try:
        from .storage.parquet_store import ParquetStore
        t = (store or ParquetStore()).read_arrow(
            "silver", "security_master", columns=["security_id", "name"])
        return {sid.split(":", 1)[-1]: n
                for sid, n in zip(t.column("security_id").to_pylist(),
                                  t.column("name").to_pylist()) if n}
    except Exception:           # 이름은 표시용 — 없으면 코드로 둔다
        return {}


def current_universe(indexes=None, path: str = None) -> Universe:
    """`index_stocks.json`의 명단 그대로 — 수집 대상 선정·종목 마스터 조회용.

    스캐너 판정에는 `universe_on()`을 쓴다(날짜가 있는 명단이어야 한다).
    """
    indexes = parse_indexes(indexes)
    data = read_index_file(path)
    members, status, sources, warns = [], {}, {}, []
    for idx in indexes:
        block = data.get(idx)
        if not block or not block["stocks"]:
            warns.append(f"{idx}: index_stocks.json에 명단이 없다 — "
                         f"`python tools/update_index.py`")
            continue
        members += [Member(s["code"], s["name"], idx) for s in block["stocks"]]
        status[idx] = STATUS_INDEX_FILE
        sources[idx] = f"index_stocks.json(as_of {block['as_of']}, 갱신 {block['updated']})"
    as_of = max((data[i]["as_of"] for i in status if data[i]["as_of"]), default=None)
    return Universe(as_of, indexes, members, status, sources, warns)


def universe_on(day=None, indexes=None, strict: bool = False, store=None,
                repo=None, path: str = None) -> Universe:
    """`day`(기본 오늘)의 명단 — 스캐너가 쓰는 단일 입구.

    1. 이력(`silver.universe_membership`)이 그날을 덮으면 그 구성
    2. 이력 마지막 관측일 **뒤**면 `index_stocks.json`과 이력 마지막 구성 중 관측일이
       더 최근인 것. 같으면 index_stocks.json(정기변경 직후 CSV·키움으로 먼저 갱신된
       경우를 반영). 며칠 차이인지 경고에 남긴다
    3. 이력 시작 **전**이면 `SurvivorshipError` — 오늘 명단을 과거에 쓰지 않는다
    4. 이력이 아예 없으면 index_stocks.json의 관측일 이후 날짜에만 그것을 쓴다

    `strict=True`면 1만 허용한다(연구용).
    """
    from .ingestion.universe_history import UniverseRepository

    day = _as_date(day) or date.today()
    indexes = parse_indexes(indexes)
    repo = repo if repo is not None else UniverseRepository(store)
    file_data = None
    members, status, sources, warns = [], {}, {}, []
    names = {}

    def _file():
        nonlocal file_data
        if file_data is None:
            file_data = read_index_file(path)
        return file_data

    for idx in indexes:
        try:
            cov = repo.coverage(idx)
        except NoInputDataError:
            cov = None
        if cov is not None and cov["observed_start"] <= day <= cov["observed_end"]:
            got = repo.get_members(idx, day)
            codes = [s.split(":", 1)[-1] for s in got["members"]]
            status[idx] = STATUS_HISTORICAL
            sources[idx] = f"universe_membership {got['universe_version']}"
            if not names:
                names = {s["code"]: s["name"] for b in _file().values() for s in b["stocks"]}
            members += [Member(c, names.get(c), idx) for c in codes]
            continue
        if strict:
            raise SurvivorshipError(
                f"{idx} {day}: 유니버스 이력 밖이다"
                + (f"({cov['observed_start']}~{cov['observed_end']})" if cov else "(이력 없음)"))
        if cov is not None and day < cov["observed_start"]:
            raise SurvivorshipError(
                f"{idx} {day}: 이력 시작({cov['observed_start']}) 전이다 — "
                f"오늘 명단을 과거에 적용하지 않는다")
        block = _file().get(idx)
        file_as_of = block["as_of"] if block and block["stocks"] else None
        hist_end = cov["observed_end"] if cov else None
        if file_as_of is not None and (hist_end is None or file_as_of >= hist_end):
            if hist_end is None and day < file_as_of:
                raise SurvivorshipError(
                    f"{idx} {day}: 이력이 없고 index_stocks.json은 {file_as_of} 명단이다")
            members += [Member(s["code"], s["name"], idx) for s in block["stocks"]]
            status[idx] = STATUS_INDEX_FILE
            sources[idx] = f"index_stocks.json(as_of {file_as_of})"
            lag = (day - file_as_of).days
        elif hist_end is not None:
            got = repo.get_members(idx, hist_end)
            codes = [s.split(":", 1)[-1] for s in got["members"]]
            if not names:
                names = {s["code"]: s["name"] for b in _file().values() for s in b["stocks"]}
            members += [Member(c, names.get(c), idx) for c in codes]
            status[idx] = STATUS_HISTORY_NEAREST
            sources[idx] = f"universe_membership {got['universe_version']} @ {hist_end}"
            lag = (day - hist_end).days
        else:
            raise NoInputDataError(
                f"{idx}: 유니버스 이력도 index_stocks.json 명단도 없다 — "
                f"`python tools/update_index.py`")
        if lag > 0:
            warns.append(f"{idx}: {day}는 구성종목 이력({hist_end or '없음'}) 밖 — "
                         f"{sources[idx]} 명단 사용({lag}일 차이)")

    missing = [m for m in members if m.name == m.code]
    if missing:
        master = _security_master_names(store)
        for m in missing:
            m.name = master.get(m.code, m.code)
    return Universe(day, indexes, members, status, sources, warns)


# ─────────────────────────────────────────────────────────────
# 백테스트 — 구간
# ─────────────────────────────────────────────────────────────

def membership_intervals(indexes=None, store=None, repo=None) -> dict:
    """`{지수: {security_id: [(시작, 종료|None) …]}}` — 이력만(연구용)."""
    from .ingestion.universe_history import UniverseRepository

    repo = repo or UniverseRepository(store)
    return {idx: repo.membership_intervals(idx) for idx in parse_indexes(indexes)}


def flat_intervals(indexes=None, store=None, repo=None) -> dict:
    """`{security_id: [(지수, 시작, 종료|None) …]}`."""
    out = {}
    for idx, by_sid in membership_intervals(indexes, store, repo).items():
        for sid, ivs in by_sid.items():
            out.setdefault(sid, []).extend((idx, s, e) for s, e in ivs)
    for ivs in out.values():
        ivs.sort(key=lambda x: (x[1], x[0]))
    return out


def member_index(intervals, day: date):
    """`flat_intervals()`의 한 종목 구간 목록에서 그날의 지수(없으면 None)."""
    for idx, s, e in intervals:
        if s <= day and (e is None or day < e):
            return idx
    return None

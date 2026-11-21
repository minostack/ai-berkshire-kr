# -*- coding: utf-8 -*-
"""수집 현황·내보내기·가져오기 — 날짜 구간의 silver 행을 zip 묶음 하나로 옮긴다.

옮기는 것
---------
| 테이블 | 내용 |
|---|---|
| `daily_bars` | 일봉 — 잠정(rev 0, 16:10)·확정(08:30) 리비전 모두 |
| `minute_bars` | 1분봉(KRX·NXT). 5·30분봉은 1분봉에서 합성하므로 따로 없다 |
| `daily_trade_sides` | KIS 매수·매도 체결량(J·NX·UN) |
| `bar_coverage` | 분봉 수집 기록 — 받는 PC가 다시 받으려 들지 않게 한다 |
| `benchmark_bars` | 지수 일봉 |

구성종목 이력(`universe_membership`)은 날짜 구간과 상관없이 **지수별 최신 버전 전체**를
넣는다(`manifest["universe"]`). 이력은 날짜별 행이 아니라 `[effective_from, effective_to)`
구간이고, reader(`UniverseRepository.load`)는 가장 최근에 적재된 버전 하나만 읽기
때문이다. 받는 PC에 같은 버전이 있거나 더 최근 버전이 있으면 쓰지 않는다.

옮기지 않는 것: `market_calendar`(일봉에서 파생 — 가져온 뒤 `calendar build`로 다시
만든다), `index_stocks.json`·`universe_snapshots/`(git이 추적한다 — 받는 PC가 쓰면
다음 `git pull`이 충돌한다. 스캐너의 `universe_on()`은 이력을 먼저 쓴다), bronze 원천
응답.

적재 장부(`data_load_state.json`)는 값을 복사하지 않는다. 가져온 뒤 **받는 PC에 실제로
있는 확정 봉**으로 `confirmed_through`를 계산해 옮긴다(`update_ledger`). 보낸 PC의 값을
그대로 쓰면, 일부 구간만 가져왔을 때 받지 않은 날까지 확정으로 적혀 그 뒤 적재가 그 날을
다시 보지 않는다.

규칙
----
- **행을 바꾸지 않는다.** run_id·data_revision을 그대로 둔다. 받는 PC의 reader가
  운영 PC와 같은 규칙(리비전 큰 것, 같으면 나중 run_id)으로 최신 봉을 고른다.
- **같은 행을 두 번 쓰지 않는다.** 가져올 때 받는 PC에 이미 있는 행(PK + run_id가
  같은 행)은 뺀다. 같은 묶음을 두 번 가져와도 두 번째는 0행이다. reader의 중복
  제거는 run이 하나뿐이면 건너뛰므로(`ParquetStore._dedupe_arrow`) 똑같은 행이
  두 벌 있으면 그대로 두 번 읽힌다 — 그래서 여기서 막는다.
- **기존 파일을 지우거나 덮지 않는다.** 새 행은 새 파일(`part-import_<묶음>-NNNN`)로
  쓴다. 저장소의 append-only 규칙(`parquet_store.py` 머리말)과 같다.
- 날짜 기준은 `trading_date`다. 수정주가 재조정(`daily-cache readjust`)이 과거
  날짜의 봉을 새 리비전으로 다시 쓰면, 그 날짜까지 구간에 넣어야 옮겨진다.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import subprocess
import zipfile
from datetime import date, datetime, timedelta

from . import DOWNLOAD_DIR, FORMAT_VERSION

from cvd_obv.contracts.market_schema import (
    BAR_COVERAGE_COLUMNS, BAR_COVERAGE_PK,
    BENCHMARK_BAR_COLUMNS, BENCHMARK_BAR_PK,
    DAILY_BAR_COLUMNS, DAILY_BAR_PK,
    DAILY_TRADE_SIDE_COLUMNS, DAILY_TRADE_SIDE_PK,
    MINUTE_BAR_COLUMNS, MINUTE_BAR_PK,
)
from cvd_obv.contracts.universe_schema import (
    UNIVERSE_MEMBERSHIP_COLUMNS, UNIVERSE_MEMBERSHIP_PK,
)
from cvd_obv.domain.ids import KST
from cvd_obv.storage.parquet_store import ParquetStore, arrow_schema, require_pyarrow

__all__ = ["TABLES", "PackageError", "collect_status", "export_package",
           "read_package", "import_package", "find_packages", "update_ledger",
           "UNIVERSE_INDEXES", "universe_heads"]

#: 테이블 → (열 계약, 저장 PK). PK에는 data_revision이 들어 있다.
TABLES = {
    "daily_bars": (DAILY_BAR_COLUMNS, DAILY_BAR_PK),
    "minute_bars": (MINUTE_BAR_COLUMNS, MINUTE_BAR_PK),
    "daily_trade_sides": (DAILY_TRADE_SIDE_COLUMNS, DAILY_TRADE_SIDE_PK),
    "bar_coverage": (BAR_COVERAGE_COLUMNS, BAR_COVERAGE_PK),
    "benchmark_bars": (BENCHMARK_BAR_COLUMNS, BENCHMARK_BAR_PK),
}
LAYER = "silver"
DATE_COLUMN = "trading_date"
UNIVERSE_TABLE = "universe_membership"
UNIVERSE_INDEXES = ("KOSPI200", "KOSDAQ150")


class PackageError(RuntimeError):
    """묶음이 깨졌거나 형식이 다르다."""


# ── 파티션 고르기 ─────────────────────────────────────────────

def _parts(rel_dir: str) -> dict:
    out = {}
    for piece in rel_dir.replace("\\", "/").split("/"):
        if "=" in piece:
            k, v = piece.split("=", 1)
            out[k] = v
    return out


def _overlaps(parts: dict, start: date, end: date) -> bool:
    """파티션 경로(year=·month=)가 `[start, end]`와 겹치는가. 모르면 True."""
    try:
        year = int(parts["year"])
    except (KeyError, ValueError):
        return True
    if not start.year <= year <= end.year:
        return False
    try:
        month = int(parts["month"])
    except (KeyError, ValueError):
        return True
    return (start.year, start.month) <= (year, month) <= (end.year, end.month)


def _partitions(store: ParquetStore, table: str, start: date, end: date) -> dict:
    """{파티션 상대 경로: [파일 경로]} — 구간과 겹치는 파티션만."""
    base = store.table_dir(LAYER, table)
    out = {}
    for path in store.files_of(LAYER, table):
        rel = os.path.relpath(os.path.dirname(path), base).replace("\\", "/")
        rel = "" if rel == "." else rel
        if _overlaps(_parts(rel), start, end):
            out.setdefault(rel, []).append(path)
    return out


def _range_filter(start: date, end: date):
    import pyarrow.dataset as ds
    return (ds.field(DATE_COLUMN) >= start) & (ds.field(DATE_COLUMN) <= end)


def _read_files(paths, start: date, end: date, columns=None):
    """파일들에서 구간 행만 읽는다. partition 추론은 끈다(`ParquetStore.read` 설명)."""
    pa, _ = require_pyarrow()
    import pyarrow.dataset as ds
    tables = []
    flt = _range_filter(start, end)
    for path in paths:
        dataset = ds.dataset(path, format="parquet", partitioning=None)
        have = set(dataset.schema.names)
        cols = None if columns is None else [c for c in columns if c in have]
        tbl = dataset.to_table(columns=cols, filter=flt)
        if tbl.num_rows:
            tables.append(tbl)
    if not tables:
        return None
    return pa.concat_tables(tables, promote_options="permissive")


def _conform(tbl, columns):
    """열 계약 순서·타입으로 맞춘다. 나중에 생긴 열(옛 파일에 없음)은 null로 채운다."""
    pa, _ = require_pyarrow()
    schema = arrow_schema(columns)
    for field in schema:
        if field.name not in tbl.column_names:
            if not field.nullable:
                raise PackageError(f"필수 열이 없다: {field.name}")
            tbl = tbl.append_column(field.name, pa.nulls(tbl.num_rows, field.type))
    return tbl.select(schema.names).cast(schema)


def _identity(table: str):
    """같은 행인지 판정하는 열 — 저장 PK(리비전 포함) + run_id."""
    _cols, pk = TABLES[table]
    return list(pk) + ["run_id"]


# ── 수집 현황 ─────────────────────────────────────────────────

def _distinct_by_date(store, table, start, end, columns, keep=None):
    """{trading_date: {그룹: 고유 security_id(또는 benchmark_id) 수}}."""
    sets = {}
    for _rel, paths in sorted(_partitions(store, table, start, end).items()):
        tbl = _read_files(paths, start, end, columns=columns)
        if tbl is None:
            continue
        rows = zip(*(tbl.column(c).to_pylist() for c in columns))
        for row in rows:
            got = keep(row) if keep else (row[0], "all")
            if got is None:
                continue
            ident, group = got
            sets.setdefault(row[1], {}).setdefault(group, set()).add(ident)
    return {d: {g: len(s) for g, s in groups.items()} for d, groups in sets.items()}


def _minute_venues(store, start, end) -> dict:
    """{trading_date: {"KRX": n, "NXT": n}} — 정규장 1분봉이 있는 종목 수.

    분봉 파일(수천 개) 대신 수집 기록(`bar_coverage`)을 센다 — 적재가 판정에
    쓰는 것과 같은 기록이다(`MinuteCatchup.coverage_since`).
    """
    from cvd_obv.ingestion.minute_catchup import MinuteCatchup
    cov = MinuteCatchup(store=store).coverage_since(1, start)
    out = {}
    for (sid, d), bars in cov.items():
        if d <= end and bars > 0:
            venue = sid.split(":", 1)[0]
            out.setdefault(d, {}).setdefault(venue, 0)
            out[d][venue] += 1
    return out, cov


def _day_status(store, start, end, securities, minutes) -> list:
    """`load_state.day_status`와 같은 표·같은 판정 — 일봉은 구간의 월 파티션만 읽는다.

    `day_status`는 일봉 파일 전부(2만여 개)를 훑어 3분 가까이 걸린다(2026-11-04 실측
    153초). 판정 규칙(최신 리비전, 수정주가, 정규장, 구성종목 90%)은 그대로 쓴다.
    """
    import pyarrow.compute as pc
    from cvd_obv.bar_standard import DAILY_ADJUSTMENT, DAILY_PRICE_SCOPE, is_provisional
    from cvd_obv.contracts.market_schema import DAILY_BAR_LOGICAL_KEY
    from cvd_obv.ingestion import load_state as L

    securities = set(securities)
    final, prov = {}, {}
    cols = list(DAILY_BAR_LOGICAL_KEY) + ["source", "run_id", "data_revision"]
    for _rel, paths in sorted(_partitions(store, "daily_bars", start, end).items()):
        tbl = _read_files(paths, start, end, columns=cols)
        if tbl is None:
            continue
        tbl = tbl.filter(pc.is_in(tbl.column("security_id"),
                                  value_set=_pa().array(sorted(securities))))
        tbl = ParquetStore._dedupe_arrow(tbl, DAILY_BAR_LOGICAL_KEY)
        for d, scope, adj, source in zip(*(tbl.column(c).to_pylist() for c in (
                DATE_COLUMN, "price_scope", "adjustment_type", "source"))):
            if scope != DAILY_PRICE_SCOPE or adj != DAILY_ADJUSTMENT:
                continue
            bucket = prov if is_provisional(source) else final
            bucket[d] = bucket.get(d, 0) + 1
    need = len(securities) * float(L.MIN_CONFIRMED_RATIO)
    known = set(final) | set(prov)
    out, d = [], start
    while d <= end:
        if d.weekday() < 5 or d in known:
            m = sum(1 for sid in securities if minutes.get((sid, d), 0) > 0)
            f, p = final.get(d, 0), prov.get(d, 0)
            if f >= need:
                status = L.STATUS_FINAL
            elif f + p >= need:
                status = L.STATUS_PROVISIONAL
            elif m >= need:
                status = L.STATUS_MINUTES
            else:
                status = L.STATUS_EMPTY
            out.append({"date": d, "minutes": m, "provisional": p, "final": f,
                        "status": status})
        d += timedelta(days=1)
    return out


def _pa():
    pa, _ = require_pyarrow()
    return pa


def _table_summary(store, table):
    paths = store.files_of(LAYER, table)
    return {"files": len(paths),
            "bytes": sum(os.path.getsize(p) for p in paths)}


def collect_status(start: date, end: date, store: ParquetStore = None,
                   indexes=("KOSPI200", "KOSDAQ150")) -> dict:
    """`[start, end]` 날짜별 수집 현황 + 장부 + 테이블 크기.

    일봉 잠정·확정 수는 적재 로그의 "[선행 점검]" 표와 같은 함수(`day_status`,
    지수 구성종목 기준·최신 리비전)로 센다. 나머지는 저장된 고유 종목 수다.
    """
    from cvd_obv.ingestion.load_state import load_state
    from cvd_obv.universe import current_universe

    store = store or ParquetStore()
    # data-load의 선행 점검과 같은 명단·형식(KRX:<코드>)
    codes = sorted(f"KRX:{c}" for c in current_universe(indexes).codes())
    minutes, coverage = _minute_venues(store, start, end)
    days = _day_status(store, start, end, codes, coverage)
    sides = _distinct_by_date(store, "daily_trade_sides", start, end,
                              ["security_id", DATE_COLUMN, "market_div"],
                              keep=lambda r: (r[0], r[2]))
    bench = _distinct_by_date(store, "benchmark_bars", start, end,
                              ["benchmark_id", DATE_COLUMN])
    for row in days:
        d = row["date"]
        row["krx_minutes_all"] = minutes.get(d, {}).get("KRX", 0)
        row["nxt_minutes_all"] = minutes.get(d, {}).get("NXT", 0)
        row["sides_un"] = sides.get(d, {}).get("UN", 0)
        row["benchmarks"] = bench.get(d, {}).get("all", 0)
    ledger_path = _ledger_path(store)
    return {
        "host": socket.gethostname(),
        "checked_at": datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M:%S"),
        "store_root": store.root,
        "members": len(codes),
        "ledger": load_state(ledger_path),
        "universe": universe_heads(store),
        "index_file": _index_file_as_of(),
        "days": days,
        "tables": {t: _table_summary(store, t) for t in TABLES},
    }


# ── 구성종목 이력 ─────────────────────────────────────────────

def _universe_files(store: ParquetStore, index_id: str) -> dict:
    """{파티션 상대 경로: [파일]} — 그 지수의 이력 파일(`index_id=…/effective_year=…`)."""
    base = store.table_dir(LAYER, UNIVERSE_TABLE)
    out = {}
    for path in store.files_of(LAYER, UNIVERSE_TABLE):
        rel = os.path.relpath(os.path.dirname(path), base).replace("\\", "/")
        if _parts(rel).get("index_id") == index_id:
            out.setdefault(rel, []).append(path)
    return out


def _read_universe(paths, columns=None, version: str = None):
    """이력 파일을 읽는다(버전을 주면 그 버전만). 날짜 구간 필터는 걸지 않는다."""
    pa, _ = require_pyarrow()
    import pyarrow.dataset as ds
    flt = (ds.field("universe_version") == version) if version else None
    tables = []
    for path in paths:
        dataset = ds.dataset(path, format="parquet", partitioning=None)
        have = set(dataset.schema.names)
        cols = None if columns is None else [c for c in columns if c in have]
        tbl = dataset.to_table(columns=cols, filter=flt)
        if tbl.num_rows:
            tables.append(tbl)
    if not tables:
        return None
    return pa.concat_tables(tables, promote_options="permissive")


def universe_heads(store: ParquetStore = None, indexes=UNIVERSE_INDEXES) -> dict:
    """{지수: {"universe_version", "rows", "observed_to", "known_at_utc"} 또는 None}.

    `UniverseRepository.load()`가 고르는 것과 같은 버전 — 가장 늦게 알려진
    (`known_at_utc` 최대) 버전 하나다.
    """
    store = store or ParquetStore()
    cols = list(UNIVERSE_MEMBERSHIP_PK) + ["known_at_utc", "observed_to"]
    out = {}
    for idx in indexes:
        paths = [p for ps in _universe_files(store, idx).values() for p in ps]
        tbl = _read_universe(paths, columns=cols) if paths else None
        if tbl is None:
            out[idx] = None
            continue
        known, observed, keys = {}, {}, {}
        for v, k, o, sid, ef in zip(*(tbl.column(c).to_pylist() for c in (
                "universe_version", "known_at_utc", "observed_to",
                "security_id", "effective_from"))):
            known[v] = max(known.get(v, k), k)
            if o is not None:
                observed[v] = max(observed.get(v, o), o)
            keys.setdefault(v, set()).add((sid, ef))
        latest = max(known, key=known.get)
        out[idx] = {"universe_version": latest, "rows": len(keys[latest]),
                    "observed_to": observed.get(latest), "known_at_utc": known[latest]}
    return out


def _index_file_as_of() -> dict:
    """{지수: as_of} — `index_stocks.json`(git 추적)의 기준일. 읽기는 `read_index_file`로만."""
    from cvd_obv.universe import read_index_file
    return {idx: block.get("as_of") for idx, block in read_index_file().items()}


def _export_universe(zf, store: ParquetStore, log=print) -> dict:
    """지수별 최신 버전 전체를 묶음에 넣는다. 날짜 구간과 상관없다."""
    _, pq = require_pyarrow()
    heads = universe_heads(store)
    out = {"indexes": {}, "files": []}
    seq = 0
    for idx, head in heads.items():
        if not head:
            continue
        rows = 0
        for rel, paths in sorted(_universe_files(store, idx).items()):
            tbl = _read_universe(paths, version=head["universe_version"])
            if tbl is None:
                continue
            tbl = _conform(tbl, UNIVERSE_MEMBERSHIP_COLUMNS)
            # 같은 버전을 여러 run이 썼으면 reader 규칙대로 하나만 남긴다
            tbl = ParquetStore._dedupe_arrow(tbl, UNIVERSE_MEMBERSHIP_PK)
            seq += 1
            buf = io.BytesIO()
            pq.write_table(tbl, buf, compression="zstd")
            data = buf.getvalue()
            member = f"{LAYER}/{UNIVERSE_TABLE}/{rel}/part-{seq:04d}.parquet"
            zf.writestr(member, data)
            out["files"].append({"member": member, "partition": rel, "index_id": idx,
                                 "rows": tbl.num_rows, "sha256": _sha256(data)})
            rows += tbl.num_rows
        out["indexes"][idx] = {
            "universe_version": head["universe_version"], "rows": rows,
            "observed_to": head["observed_to"].isoformat() if head["observed_to"] else None,
            "known_at_utc": head["known_at_utc"].isoformat()}
        if log:
            log(f"  {'universe ' + idx:18} {rows:>10,}행  {head['universe_version']} "
                f"(관측 {out['indexes'][idx]['observed_to']}까지)")
    return out


def _import_universe(zf, store: ParquetStore, manifest: dict, dry_run: bool,
                     written: list, log=print):
    """묶음의 이력 버전을 받는 PC에 쓴다 — 같은 버전이 있거나 더 최근 버전이 있으면 건너뛴다."""
    pa, pq = require_pyarrow()
    uni = manifest.get("universe")
    if not uni:
        return None
    local = universe_heads(store)
    keys = list(UNIVERSE_MEMBERSHIP_PK)
    out = {}
    for idx, info in uni["indexes"].items():
        mine = local.get(idx)
        theirs_known = datetime.fromisoformat(info["known_at_utc"])
        if mine and mine["universe_version"] == info["universe_version"]:
            action = "same"
        elif mine and mine["known_at_utc"] >= theirs_known:
            action = "local_newer"
        else:
            action = "write"
        res = {"universe_version": info["universe_version"],
               "observed_to": info["observed_to"],
               "local_version": mine["universe_version"] if mine else None,
               "local_observed_to": (mine["observed_to"].isoformat()
                                     if mine and mine["observed_to"] else None),
               "action": action, "package_rows": info["rows"], "new_rows": 0,
               "skipped_rows": 0, "files": []}
        for f in (f for f in uni["files"] if f["index_id"] == idx):
            incoming = _conform(pq.read_table(pa.BufferReader(zf.read(f["member"]))),
                                UNIVERSE_MEMBERSHIP_COLUMNS)
            if action != "write":
                res["skipped_rows"] += incoming.num_rows
                continue
            parts = f["partition"].split("/")
            directory = os.path.join(store.table_dir(LAYER, UNIVERSE_TABLE), *parts)
            existing = None
            if os.path.isdir(directory):
                paths = sorted(os.path.join(directory, n) for n in os.listdir(directory)
                               if n.endswith(".parquet"))
                existing = _read_universe(paths, columns=keys) if paths else None
            # 같은 PK(버전 포함)면 run_id가 달라도 같은 행이다 — 버전 단위로 옮기기 때문
            fresh = _new_rows(incoming, existing, keys)
            res["new_rows"] += fresh.num_rows
            res["skipped_rows"] += incoming.num_rows - fresh.num_rows
            if dry_run or fresh.num_rows == 0:
                continue
            os.makedirs(directory, exist_ok=True)
            seq = f["member"].rsplit("-", 1)[-1].split(".")[0]
            target = os.path.join(
                directory, f"part-import_{manifest['package_id']}-u{seq}.parquet")
            if os.path.exists(target):
                raise PackageError(f"같은 이름 파일이 이미 있다: {target}")
            pq.write_table(fresh, target, compression="zstd")
            with open(target, "rb") as fh:
                digest = _sha256(fh.read())
            rel_path = os.path.relpath(target, store.root).replace("\\", "/")
            res["files"].append({"path": rel_path, "rows": fresh.num_rows, "sha256": digest})
            written.append(rel_path)
        out[idx] = res
        if log:
            note = {"same": "이 PC에 같은 버전이 있다",
                    "local_newer": f"이 PC 버전({res['local_version']})이 더 최근이다",
                    "write": "가져온다"}[action]
            log(f"  {'universe ' + idx:18} 묶음 {res['package_rows']:>10,}행 → 새 행 "
                f"{res['new_rows']:>10,} / 건너뜀 {res['skipped_rows']:>8,}  "
                f"{info['universe_version']} 관측 {info['observed_to']}까지 — {note}")
    return out


# ── 내보내기 ─────────────────────────────────────────────────

def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _git_head(root: str):
    try:
        out = subprocess.run(["git", "-C", root, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def _package_name(start: date, end: date, host: str, now: datetime) -> str:
    span = f"{start:%Y%m%d}" if start == end else f"{start:%Y%m%d}-{end:%Y%m%d}"
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in host)
    return f"bars_{span}_{safe}_{now:%Y%m%d%H%M%S}"


def _daily_kinds(tbl) -> dict:
    """{trading_date: {"provisional": n, "final": n}} — 정규장 행 수(리비전 전부)."""
    from cvd_obv.bar_standard import DAILY_PRICE_SCOPE, is_provisional
    out = {}
    for d, scope, source in zip(tbl.column(DATE_COLUMN).to_pylist(),
                                tbl.column("price_scope").to_pylist(),
                                tbl.column("source").to_pylist()):
        if scope != DAILY_PRICE_SCOPE:
            continue
        kind = "provisional" if is_provisional(source) else "final"
        bucket = out.setdefault(d.isoformat(), {"provisional": 0, "final": 0})
        bucket[kind] += 1
    return out


def export_package(start: date, end: date, tables=None, out_dir: str = None,
                   store: ParquetStore = None, with_status: bool = True,
                   log=print, include_universe: bool = True) -> tuple:
    """`[start, end]`의 행을 zip 하나로 쓴다. (경로, manifest)를 돌려준다.

    `include_universe`면 구성종목 이력의 지수별 최신 버전도 넣는다(구간과 무관).
    """
    _, pq = require_pyarrow()
    if end < start:
        raise ValueError(f"종료일({end})이 시작일({start})보다 앞이다")
    store = store or ParquetStore()
    tables = list(tables or TABLES)
    out_dir = out_dir or DOWNLOAD_DIR
    os.makedirs(out_dir, exist_ok=True)
    host = socket.gethostname()
    now = datetime.now(tz=KST)
    package_id = _package_name(start, end, host, now)
    final_path = os.path.join(out_dir, package_id + ".zip")
    temp_path = final_path + ".part"

    manifest = {
        "format_version": FORMAT_VERSION,
        "package_id": package_id,
        "created_at": now.strftime("%Y-%m-%d %H:%M:%S %z"),
        "source_host": host,
        "source_git_head": _git_head(os.path.dirname(store.root)),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "tables": {},
    }
    if with_status:
        try:
            status = collect_status(start, end, store=store)
            manifest["source_ledger"] = status["ledger"]
            manifest["source_days"] = [
                {**r, "date": r["date"].isoformat()} for r in status["days"]]
        except Exception as exc:  # noqa: BLE001 — 현황은 참고용, 내보내기를 막지 않는다
            manifest["source_status_error"] = str(exc)

    with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_STORED) as zf:
        for table in tables:
            columns, _pk = TABLES[table]
            entry = {"rows": 0, "files": [], "rows_by_date": {}}
            if table == "daily_bars":
                entry["daily_kinds"] = {}
            parts = _partitions(store, table, start, end)
            for seq, (rel, paths) in enumerate(sorted(parts.items()), start=1):
                tbl = _read_files(paths, start, end)
                if tbl is None:
                    continue
                tbl = _conform(tbl, columns)
                buf = io.BytesIO()
                pq.write_table(tbl, buf, compression="zstd")
                data = buf.getvalue()
                member = "/".join(p for p in (LAYER, table, rel, f"part-{seq:04d}.parquet") if p)
                zf.writestr(member, data)
                entry["files"].append({"member": member, "partition": rel,
                                       "rows": tbl.num_rows, "sha256": _sha256(data)})
                entry["rows"] += tbl.num_rows
                for d in tbl.column(DATE_COLUMN).to_pylist():
                    key = d.isoformat()
                    entry["rows_by_date"][key] = entry["rows_by_date"].get(key, 0) + 1
                if table == "daily_bars":
                    for d, kinds in _daily_kinds(tbl).items():
                        tot = entry["daily_kinds"].setdefault(d, {"provisional": 0, "final": 0})
                        for k, v in kinds.items():
                            tot[k] += v
            entry["rows_by_date"] = dict(sorted(entry["rows_by_date"].items()))
            manifest["tables"][table] = entry
            if log:
                log(f"  {table:18} {entry['rows']:>10,}행  파일 {len(entry['files'])}개")
        if include_universe:
            manifest["universe"] = _export_universe(zf, store, log=log)
        zf.writestr("manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2, default=str))
    os.replace(temp_path, final_path)
    return final_path, manifest


# ── 묶음 읽기 ─────────────────────────────────────────────────

def find_packages(directory: str = None) -> list:
    """DOWNLOAD의 묶음(zip) — 오래된 것부터."""
    directory = directory or DOWNLOAD_DIR
    if not os.path.isdir(directory):
        return []
    paths = [os.path.join(directory, n) for n in os.listdir(directory)
             if n.startswith("bars_") and n.endswith(".zip")]
    return sorted(paths, key=os.path.getmtime)


def read_package(path: str, verify: bool = True) -> dict:
    """manifest를 읽고(기본) 모든 파일의 해시를 확인한다."""
    try:
        zf = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise PackageError(f"zip을 열 수 없다: {path} ({exc})") from exc
    with zf:
        try:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        except KeyError as exc:
            raise PackageError("manifest.json이 없다") from exc
        if manifest.get("format_version") != FORMAT_VERSION:
            raise PackageError(f"형식이 다르다: {manifest.get('format_version')} "
                               f"(이 도구는 {FORMAT_VERSION})")
        unknown = set(manifest["tables"]) - set(TABLES)
        if unknown:
            raise PackageError(f"모르는 테이블: {sorted(unknown)}")
        if verify:
            entries = list(manifest["tables"].values())
            if manifest.get("universe"):
                entries.append(manifest["universe"])
            for entry in entries:
                for f in entry["files"]:
                    try:
                        data = zf.read(f["member"])
                    except KeyError as exc:
                        raise PackageError(f"파일이 빠졌다: {f['member']}") from exc
                    if _sha256(data) != f["sha256"]:
                        raise PackageError(f"해시가 다르다(손상): {f['member']}")
    return manifest


# ── 가져오기 ─────────────────────────────────────────────────

def _existing_keys(store, table, rel, start, end):
    """받는 PC의 같은 파티션에 이미 있는 행의 식별 열."""
    directory = os.path.join(store.table_dir(LAYER, table), *rel.split("/")) if rel \
        else store.table_dir(LAYER, table)
    if not os.path.isdir(directory):
        return None
    paths = sorted(os.path.join(directory, n) for n in os.listdir(directory)
                   if n.endswith(".parquet"))
    if not paths:
        return None
    return _read_files(paths, start, end, columns=_identity(table))


def _new_rows(incoming, existing, keys):
    """`incoming` 중 `existing`에 없는 행만. 열 전체를 join하지 않고 행 번호로 고른다."""
    pa, _ = require_pyarrow()
    if existing is None or existing.num_rows == 0:
        return incoming
    left = incoming.select(keys).append_column(
        "__row", pa.array(range(incoming.num_rows), pa.int64()))
    right = existing.select(keys).cast(incoming.select(keys).schema)
    anti = left.join(right, keys=keys, join_type="left anti")
    keep = sorted(anti.column("__row").to_pylist())
    return incoming.take(pa.array(keep, pa.int64()))


def import_manifest_path(store: ParquetStore, package_id: str) -> str:
    # `silver_*.json`은 .gitignore 대상이다 — 수집 manifest와 같이 git에 올라가지 않는다
    return os.path.join(store.manifest_dir, f"silver_transfer_import_{package_id}.json")


def _ledger_path(store: ParquetStore) -> str:
    return os.path.join(store.manifest_dir, "data_load_state.json")


def _walk_confirmed(rows, package_start: date, package_days) -> tuple:
    """날짜별 상태를 앞에서부터 보며 (연속으로 확정된 마지막 날, 멈춘 날, 멈춘 까닭).

    묶음 구간 안에서 행이 하나도 없는 평일은 보낸 PC에도 없는 날, 곧 휴장으로 보고
    넘어간다. 묶음 앞쪽의 빈 평일은 휴장인지 받지 않은 날인지 알 수 없어 멈춘다.
    """
    from cvd_obv.ingestion import load_state as L

    last = None
    for row in rows:
        d = row["date"]
        if row["status"] == L.STATUS_FINAL:
            last = d
            continue
        empty = row["final"] + row["provisional"] == 0
        if empty and d >= package_start and d.isoformat() not in package_days:
            continue
        if empty and d < package_start:
            return last, d, "묶음 앞쪽의 빈 평일 — 휴장인지 받지 않은 날인지 알 수 없다"
        return last, d, f"확정 봉 부족({row['status']})"
    return last, None, None


def update_ledger(store: ParquetStore, manifest: dict, codes=None, now: datetime = None) -> dict:
    """받는 PC의 적재 장부를 가져온 데이터까지 옮긴다. 되돌리지 않는다.

    - `confirmed_through`: 지금 장부 다음 날(장부가 없으면 묶음 시작일)부터 확정 봉이
      구성종목의 90% 이상인 날이 이어지는 마지막 날. 적재와 같은 판정이다.
    - `last_final_run`: 보낸 PC의 확정까지 이 PC에 들어왔을 때만 가져온다.
    - `last_provisional_run`: 그 실행이 만든 날의 잠정 봉이 묶음에 있을 때만 가져온다.
    - `last_import`: 이 값들이 가져오기로 온 것임을 남긴다.
    """
    from cvd_obv.ingestion import load_state as L
    from cvd_obv.universe import current_universe

    now = now or datetime.now(tz=KST)
    path = _ledger_path(store)
    state = L.load_state(path)
    before = L._as_day(state.get("confirmed_through"))
    package_start = date.fromisoformat(manifest["start"])
    package_end = date.fromisoformat(manifest["end"])
    target = min(package_end, L.confirmed_horizon(now))
    if codes is None:
        codes = [f"KRX:{c}" for c in current_universe(("KOSPI200", "KOSDAQ150")).codes()]
    if not codes:
        raise ValueError("지수 구성종목 명단이 비어 있다")

    tables = manifest["tables"]
    package_days = set()
    for table in ("daily_bars", "minute_bars"):
        package_days |= set(tables.get(table, {}).get("rows_by_date", {}))

    first = before + timedelta(days=1) if before else package_start
    last, stopped_at, reason = None, None, None
    if first <= target:
        rows = _day_status(store, first, target, codes, {})
        last, stopped_at, reason = _walk_confirmed(rows, package_start, package_days)
    after = max(d for d in (before, last) if d) if (before or last) else None

    source = manifest.get("source_ledger") or {}
    update = {"last_import": {"package_id": manifest["package_id"],
                              "source_host": manifest.get("source_host"),
                              "imported_at": now.strftime("%Y-%m-%d %H:%M:%S")}}
    if after:
        update["confirmed_through"] = after.isoformat()
    source_confirmed = L._as_day(source.get("confirmed_through"))
    final_run = source.get("last_final_run")
    if final_run and after and source_confirmed and after >= source_confirmed:
        if final_run > state.get("last_final_run", ""):
            update["last_final_run"] = final_run
    prov_run = source.get("last_provisional_run")
    kinds = tables.get("daily_bars", {}).get("daily_kinds", {})
    if prov_run and kinds.get(prov_run[:10], {}).get("provisional", 0) > 0:
        if prov_run > state.get("last_provisional_run", ""):
            update["last_provisional_run"] = prov_run
    L.save_state(update, path)
    return {"before": before.isoformat() if before else None,
            "confirmed_through": after.isoformat() if after else None,
            "stopped_at": stopped_at.isoformat() if stopped_at else None,
            "reason": reason,
            "last_final_run": update.get("last_final_run", state.get("last_final_run")),
            "last_provisional_run": update.get("last_provisional_run",
                                               state.get("last_provisional_run"))}


def import_package(path: str, store: ParquetStore = None, dry_run: bool = False,
                   log=print, codes=None) -> dict:
    """묶음의 행 중 받는 PC에 없는 것만 새 파일로 쓰고 장부를 옮긴다. 요약을 돌려준다.

    잠금은 호출하는 쪽(`transfer.py`)이 잡는다 — 적재와 동시에 쓰지 않게.
    새 행이 0이어도 장부는 계산한다 — 장부가 빠진 채 먼저 가져온 경우를 바로잡는다.
    장부 계산이 실패해도 행은 이미 썼으므로 `summary["ledger"]["error"]`로만 알린다.
    """
    pa, pq = require_pyarrow()
    import pyarrow.compute as pc
    store = store or ParquetStore()
    manifest = read_package(path, verify=True)
    package_id = manifest["package_id"]
    summary = {"package_id": package_id, "dry_run": dry_run, "tables": {},
               "already_imported": os.path.exists(import_manifest_path(store, package_id))}
    written = []
    with zipfile.ZipFile(path) as zf:
        for table, entry in manifest["tables"].items():
            columns, _pk = TABLES[table]
            keys = _identity(table)
            res = {"package_rows": entry["rows"], "new_rows": 0, "skipped_rows": 0,
                   "files": []}
            for seq, f in enumerate(entry["files"], start=1):
                incoming = pq.read_table(pa.BufferReader(zf.read(f["member"])))
                incoming = _conform(incoming, columns)
                dates = incoming.column(DATE_COLUMN)
                lo, hi = pc.min(dates).as_py(), pc.max(dates).as_py()
                existing = _existing_keys(store, table, f["partition"], lo, hi)
                fresh = _new_rows(incoming, existing, keys)
                res["new_rows"] += fresh.num_rows
                res["skipped_rows"] += incoming.num_rows - fresh.num_rows
                if dry_run or fresh.num_rows == 0:
                    continue
                parts = f["partition"].split("/") if f["partition"] else []
                directory = os.path.join(store.table_dir(LAYER, table), *parts)
                os.makedirs(directory, exist_ok=True)
                target = os.path.join(directory, f"part-import_{package_id}-{seq:04d}.parquet")
                if os.path.exists(target):
                    raise PackageError(f"같은 이름 파일이 이미 있다: {target}")
                pq.write_table(fresh, target, compression="zstd")
                with open(target, "rb") as fh:
                    digest = _sha256(fh.read())
                rel_path = os.path.relpath(target, store.root).replace("\\", "/")
                res["files"].append({"path": rel_path, "rows": fresh.num_rows,
                                     "sha256": digest})
                written.append(rel_path)
            summary["tables"][table] = res
            if log:
                log(f"  {table:18} 묶음 {res['package_rows']:>10,}행 → 새 행 "
                    f"{res['new_rows']:>10,} / 이미 있음 {res['skipped_rows']:>10,}")
        summary["universe"] = _import_universe(zf, store, manifest, dry_run, written, log=log)
    if not dry_run:
        try:
            summary["ledger"] = update_ledger(store, manifest, codes=codes)
        except Exception as exc:  # noqa: BLE001 — 행은 이미 썼다. 장부는 다음에 다시 계산한다
            summary["ledger"] = {"error": f"{type(exc).__name__}: {exc}"}
    if not dry_run and written:
        record = {
            "dataset_name": "silver.transfer_import",
            "package_id": package_id,
            "package_file": os.path.basename(path),
            "imported_at": datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M:%S %z"),
            "target_host": socket.gethostname(),
            "source_host": manifest.get("source_host"),
            "source_git_head": manifest.get("source_git_head"),
            "start": manifest["start"],
            "end": manifest["end"],
            "source_ledger": manifest.get("source_ledger"),
            "ledger": summary["ledger"],
            "tables": summary["tables"],
            "universe": summary["universe"],
            "status": "SUCCESS",
        }
        os.makedirs(store.manifest_dir, exist_ok=True)
        rec_path = import_manifest_path(store, package_id)
        if os.path.exists(rec_path):
            rec_path = rec_path[:-5] + f"_{datetime.now():%Y%m%d%H%M%S}.json"
        with open(rec_path, "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False, indent=2, default=str)
            fh.write("\n")
        summary["record"] = os.path.relpath(rec_path, store.root).replace("\\", "/")
    summary["manifest"] = manifest
    return summary


def default_status_range(today: date = None, days: int = 14) -> tuple:
    today = today or datetime.now(tz=KST).date()
    return today - timedelta(days=days - 1), today

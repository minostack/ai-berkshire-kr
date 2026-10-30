# -*- coding: utf-8 -*-
"""Parquet 저장 계층 — 스키마 §10(manifest)·§11(물리 경로).

규칙
----
- **검증 후에만 쓴다.** 열 계약·null·enum·PK를 통과하지 못한 행은 파일에
  닿지 않는다. Parquet은 아무것도 강제해 주지 않으므로 여기가 마지막 방어선이다.
- **쓸 때마다 manifest를 남긴다.** 어떤 run이 어떤 partition에 몇 행을 썼는지,
  파일 해시가 무엇인지 기록하지 않으면 나중에 재현을 증명할 수 없다.
- **조용히 덮지 않는다.** 같은 partition에 다시 쓰면 그 run의 파일만 교체하고
  이전 run 파일은 남긴다. 읽을 때 `data_revision`과 run 순서로 최신을 고른다.
"""

from __future__ import annotations

import glob
import json
import os
from datetime import datetime

from ..domain.errors import ConfigError, NoInputDataError, SchemaError
from ..domain.ids import UTC, sha256_of, utc_iso
from ..domain.redaction import assert_no_secrets
from .. import SCHEMA_VERSION
from ..contracts.schema import validate_rows

__all__ = ["ParquetStore", "WriteResult", "arrow_schema", "require_pyarrow"]


def require_pyarrow():
    """pyarrow를 지연 import한다. 없으면 무엇을 설치해야 하는지 말해 준다."""
    try:
        import pyarrow as pa  # noqa: F401
        import pyarrow.parquet as pq  # noqa: F401
    except ImportError as exc:  # pragma: no cover - 환경 의존
        raise ConfigError(
            "pyarrow가 필요하다 — `pip install 'cvd-obv[data]'` 또는 "
            "`pip install pyarrow`") from exc
    return pa, pq


def arrow_schema(columns):
    """`Column` 목록 → pyarrow schema."""
    pa, _ = require_pyarrow()
    mapping = {
        "string": pa.string(),
        "int": pa.int64(),
        "float": pa.float64(),
        "bool": pa.bool_(),
        "date": pa.date32(),
        "timestamp": pa.timestamp("us", tz="UTC"),
        "time": pa.string(),
        "list<string>": pa.list_(pa.string()),
    }
    fields = []
    for col in columns:
        if col.kind == "decimal":
            typ = pa.decimal128(col.precision, col.scale)
        else:
            typ = mapping.get(col.kind)
            if typ is None:
                raise SchemaError(f"Parquet 타입을 모르는 열: {col.name} ({col.kind})")
        fields.append(pa.field(col.name, typ, nullable=col.nullable))
    return pa.schema(fields)


class WriteResult:
    """한 번의 write가 만든 것."""

    __slots__ = ("table", "run_id", "row_count", "partitions", "manifest_path",
                 "files")

    def __init__(self, table, run_id, row_count, partitions, manifest_path,
                 files):
        self.table = table
        self.run_id = run_id
        self.row_count = row_count
        self.partitions = partitions
        self.manifest_path = manifest_path
        self.files = files

    def to_dict(self):
        return {"table": self.table, "run_id": self.run_id,
                "row_count": self.row_count,
                "partition_count": len(self.partitions),
                "manifest": self.manifest_path,
                "files": self.files}


def _partition_path(parts):
    return "/".join(f"{k}={v}" for k, v in parts)


def _file_sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


class ParquetStore:
    """`data/` 아래 layer/table 단위 Parquet 저장소."""

    def __init__(self, root=None):
        if root is None:
            here = os.path.dirname(os.path.abspath(__file__))
            root = os.path.abspath(os.path.join(here, "..", "..", "..", "data"))
        self.root = root
        self.manifest_dir = os.path.join(root, "manifests")

    # ── 경로 ───────────────────────────────────────────────

    def table_dir(self, layer: str, table: str) -> str:
        return os.path.join(self.root, layer, table)

    # ── 쓰기 ───────────────────────────────────────────────

    def write(self, layer: str, table: str, rows, columns, pk, run_id: str,
              partition_fn=None, extra_manifest=None, input_manifests=(),
              validators=(), part_key: str = None):
        """행을 검증하고 partition별 Parquet + manifest를 쓴다.

        `part_key`는 같은 run을 여러 번에 나눠 쓸 때 파일명을 구분한다. 수십만
        행을 한 번에 메모리에 들고 있지 않기 위한 장치이며, `run_id`는 그대로
        유지되므로 lineage는 하나로 남는다.
        """
        pa, pq = require_pyarrow()
        if not rows:
            raise NoInputDataError(f"{table}: 쓸 행이 없다")

        validate_rows(rows, columns, pk, f"{layer}.{table}")
        for fn in validators:
            fn(rows)

        schema = arrow_schema(columns)
        names = [c.name for c in columns]
        base = self.table_dir(layer, table)

        buckets = {}
        for row in rows:
            parts = tuple(partition_fn(row)) if partition_fn else ()
            buckets.setdefault(parts, []).append(row)

        files, partitions = [], []
        for parts, group in sorted(buckets.items()):
            rel = _partition_path(parts)
            directory = os.path.join(base, *(f"{k}={v}" for k, v in parts))
            os.makedirs(directory, exist_ok=True)
            token = f"{run_id}-{part_key}" if part_key else run_id
            path = os.path.join(directory, f"part-{token}.parquet")
            arrays = {name: [r.get(name) for r in group] for name in names}
            table_obj = pa.Table.from_pydict(arrays, schema=schema)
            pq.write_table(table_obj, path, compression="zstd")
            files.append({
                "path": os.path.relpath(path, self.root).replace("\\", "/"),
                "rows": len(group),
                "sha256": _file_sha256(path),
            })
            partitions.append({"partition": rel or "(none)", "rows": len(group)})

        manifest_path = self._write_manifest(
            layer, table, rows, run_id, files, partitions,
            extra_manifest or {}, input_manifests, part_key)

        return WriteResult(f"{layer}.{table}", run_id, len(rows), partitions,
                           manifest_path, files)

    def _write_manifest(self, layer, table, rows, run_id, files, partitions,
                        extra, input_manifests, part_key=None):
        os.makedirs(self.manifest_dir, exist_ok=True)
        event_times = [r.get("trading_date") for r in rows
                       if r.get("trading_date") is not None]
        manifest = {
            "dataset_name": f"{layer}.{table}",
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "created_at_utc": utc_iso(datetime.now(tz=UTC)),
            "row_counts": {"total": len(rows),
                           "by_partition": partitions},
            "partition_paths": [f["path"] for f in files],
            "files": files,
            "min_event_time": min(event_times).isoformat() if event_times else None,
            "max_event_time": max(event_times).isoformat() if event_times else None,
            "input_manifest_hashes": list(input_manifests),
            "status": "SUCCESS",
        }
        manifest.update(extra)
        if part_key:
            manifest["part_key"] = part_key
        assert_no_secrets(manifest, what=f"{layer}.{table} manifest")
        # 해시는 자기 자신을 제외한 전체 내용을 덮는다 — part_key도 포함된다.
        manifest["manifest_sha256"] = sha256_of(manifest)
        token = f"{run_id}-{part_key}" if part_key else run_id
        path = os.path.join(self.manifest_dir, f"{layer}_{table}_{token}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True,
                      default=str)
            f.write("\n")
        return os.path.relpath(path, self.root).replace("\\", "/")

    # ── 읽기 ───────────────────────────────────────────────

    def files_of(self, layer: str, table: str):
        base = self.table_dir(layer, table)
        return sorted(glob.glob(os.path.join(base, "**", "*.parquet"),
                                recursive=True))

    def read(self, layer: str, table: str, pk=None, columns=None,
             where=None, dedupe: bool = True):
        """테이블 전체를 dict 목록으로 읽는다.

        같은 PK가 여러 run 파일에 있으면 `data_revision`이 큰 행, 같으면 나중
        run_id 행을 남긴다. 어느 쪽을 택했는지 헷갈리지 않도록 규칙을 하나로
        고정한다.
        """
        pa, pq = require_pyarrow()
        paths = self.files_of(layer, table)
        if not paths:
            raise NoInputDataError(
                f"{layer}.{table}: 저장된 Parquet 파일이 없다")
        out = []
        for path in paths:
            # `pq.read_table()`은 디렉터리 이름에서 partition 열을 추론해 붙인다.
            # 그러면 계약에 없는 열이 생기고(`year`, `month`), partition 키와
            # 이름이 같은 실제 열(`dataset`)과 타입이 충돌한다. 파일을 직접
            # 읽어 저장한 열만 돌려준다.
            pf = pq.ParquetFile(path)
            # 나중에 추가된 열은 옛 파일에 없다 — 있는 열만 읽는다(dict에 키가 없다)
            cols = (columns if columns is None else
                    [c for c in columns if c in set(pf.schema_arrow.names)])
            tbl = pf.read(columns=cols)
            out.extend(tbl.to_pylist())
        if where is not None:
            out = [r for r in out if where(r)]
        if dedupe and pk:
            best = {}
            for row in out:
                key = tuple(row.get(k) for k in pk)
                prev = best.get(key)
                if prev is None or _prefer(row, prev):
                    best[key] = row
            out = list(best.values())
        return out

    def read_arrow(self, layer: str, table: str, columns=None, filters=None,
                   dedupe_pk=None, latest_by=None):
        """열 부분집합을 pyarrow Table로 읽는다.

        `read()`는 행마다 파이썬 dict를 만들기 때문에 수백만 행에서는 메모리가
        감당이 안 된다. 통계 계산은 열 단위면 충분하므로 Arrow로 받는다.
        `filters`는 pyarrow expression(예: `ds.field("horizon_td") == 5`)이다.

        `dedupe_pk`를 주면 같은 PK가 여러 run 파일에 있을 때 `read()`와 **같은
        규칙**으로 하나만 남긴다(data_revision 큰 것, 같으면 나중 run_id).
        재수집한 종목이 이전 실행의 잘린 행과 함께 읽히는 것을 막는다.
        중복 제거를 하려면 `run_id`와 `data_revision`이 읽는 열에 포함돼야 한다.

        `latest_by`(논리 키)를 주면 그 키별로 **가장 최신 리비전 한 행**만
        남긴다. 저장 PK(`DAILY_BAR_PK` 등)에는 `data_revision`이 들어 있어
        `dedupe_pk`만으로는 리비전 1과 2가 둘 다 남는다 — 조회 시점에 "현재
        유효한 값"이 필요하면 이것을 쓴다. 저장 PK는 바꾸지 않는다(이력 보존).
        """
        if latest_by and not dedupe_pk:
            dedupe_pk = tuple(latest_by) + ("data_revision",)
        pa, pq = require_pyarrow()
        import pyarrow.dataset as ds

        paths = self.files_of(layer, table)
        if not paths:
            raise NoInputDataError(
                f"{layer}.{table}: 저장된 Parquet 파일이 없다")

        needed = None
        if columns is not None:
            needed = list(dict.fromkeys(
                list(columns)
                + (list(dedupe_pk) + ["run_id", "data_revision"]
                   if dedupe_pk else [])))

        tables = []
        for path in paths:
            # partition 추론을 끄기 위해 파일을 개별 dataset으로 연다.
            dataset = ds.dataset(path, format="parquet", partitioning=None)
            have = set(dataset.schema.names)
            # 열을 나중에 추가한 테이블(예: minute_bars.cum_traded_value, 2026-10-23)은
            # 옛 파일에 그 열이 없다 — 없는 열은 null로 채운다
            cols = needed if needed is None else [c for c in needed if c in have]
            tbl = dataset.to_table(columns=cols, filter=filters)
            if needed is not None and len(cols) != len(needed):
                for c in needed:
                    if c not in have:
                        tbl = tbl.append_column(c, pa.nulls(tbl.num_rows))
                tbl = tbl.select(needed)
            tables.append(tbl)
        merged = pa.concat_tables(tables, promote_options="permissive")
        if not dedupe_pk or merged.num_rows == 0:
            return merged

        merged = self._dedupe_arrow(merged, dedupe_pk)
        if latest_by:
            merged = self._dedupe_arrow(merged, tuple(latest_by))
        if columns is not None:
            merged = merged.select(list(columns))
        return merged

    @staticmethod
    def _dedupe_arrow(table, pk):
        """PK별로 `data_revision` 큰 행, 같으면 나중 `run_id` 행만 남긴다."""
        import pyarrow.compute as pc

        missing = [c for c in list(pk) + ["run_id", "data_revision"]
                   if c not in table.column_names]
        if missing:
            raise SchemaError(
                f"중복 제거에 필요한 열이 없다: {missing}")

        # 한 번만 쓴 테이블이면 중복이 있을 수 없다. 수백만 행을 파이썬 루프로
        # 훑기 전에 이 경우를 먼저 걸러낸다.
        if (len(pc.unique(table.column("run_id"))) == 1
                and len(pc.unique(table.column("data_revision"))) == 1):
            return table

        key_columns = [table.column(c).to_pylist() for c in pk]
        revisions = table.column("data_revision").to_pylist()
        run_ids = table.column("run_id").to_pylist()

        best = {}
        for i in range(table.num_rows):
            key = tuple(col[i] for col in key_columns)
            rank = (revisions[i] or 0, run_ids[i] or "")
            current = best.get(key)
            if current is None or rank > current[1]:
                best[key] = (i, rank)
        if len(best) == table.num_rows:
            return table
        keep = sorted(idx for idx, _rank in best.values())
        return table.take(keep)

    def stats(self, layer: str, table: str):
        """파일 수·행 수 요약. 전체를 메모리에 올리지 않는다."""
        _, pq = require_pyarrow()
        paths = self.files_of(layer, table)
        rows = 0
        for path in paths:
            rows += pq.ParquetFile(path).metadata.num_rows
        return {"table": f"{layer}.{table}", "files": len(paths),
                "rows": rows,
                "bytes": sum(os.path.getsize(p) for p in paths)}


def _prefer(candidate, current) -> bool:
    """중복 PK에서 어느 행을 남길지. revision 우선, 그다음 run_id."""
    a = candidate.get("data_revision") or 0
    b = current.get("data_revision") or 0
    if a != b:
        return a > b
    return str(candidate.get("run_id") or "") > str(current.get("run_id") or "")

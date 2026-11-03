# -*- coding: utf-8 -*-
"""내보내기·가져오기 단위 테스트 — 임시 저장소 두 개(PC1·PC2)에 합성 일봉만 쓴다.

    python -m pytest data_transfer/tests -q
"""

import os
import sys
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import data_transfer  # noqa: E402,F401 — src 경로를 올린다
from data_transfer.package import (  # noqa: E402
    PackageError, _overlaps, _parts, export_package, import_package, read_package,
)
from cvd_obv.bar_standard import PROVISIONAL_SOURCE  # noqa: E402
from cvd_obv.contracts.market_schema import (  # noqa: E402
    DAILY_BAR_COLUMNS, DAILY_BAR_LOGICAL_KEY, DAILY_BAR_PK,
)
from cvd_obv.storage.parquet_store import ParquetStore  # noqa: E402

UTC = timezone.utc


def _daily(sid, day, close, source, run_id, revision):
    at = datetime(day.year, day.month, day.day, 7, 0, tzinfo=UTC)
    return {
        "security_id": sid, "trading_date": day,
        "open": Decimal(close), "high": Decimal(close), "low": Decimal(close),
        "close": Decimal(close),
        "volume": 1000, "traded_value": None,
        "price_scope": "REGULAR", "adjustment_type": "SPLIT_ADJUSTED",
        "adjustment_factor": 1.0, "adjustment_as_of_utc": at,
        "available_at_utc": at, "source": source,
        "quality_status": "SUCCESS", "quality_flags": [],
        "run_id": run_id, "market_rule_version": "KRX-2026-09-14-aftermarket",
        "schema_version": "2.0.0", "data_revision": revision,
    }


def _write(store, rows, run_id):
    store.write("silver", "daily_bars", rows, DAILY_BAR_COLUMNS, DAILY_BAR_PK, run_id,
                partition_fn=lambda r: [("market", "KRX"),
                                        ("year", r["trading_date"].year),
                                        ("month", f"{r['trading_date'].month:02d}")])


D1, D2, OUTSIDE = date(2026, 11, 2), date(2026, 11, 3), date(2026, 10, 30)
PROV_RUN, FINAL_RUN, PROV2_RUN = ("topup_load_20261102071255_a",
                                  "topup_load_20261102234513_b",
                                  "topup_load_20261103072636_c")


@pytest.fixture
def pc1(tmp_path):
    store = ParquetStore(root=str(tmp_path / "pc1" / "data"))
    _write(store, [_daily("KRX:005930", OUTSIDE, 100, "KIS", "old_run", 1)], "old_run")
    _write(store, [_daily("KRX:005930", D1, 101, PROVISIONAL_SOURCE, PROV_RUN, 0)], PROV_RUN)
    _write(store, [_daily("KRX:005930", D1, 102, "KIS", FINAL_RUN, 1)], FINAL_RUN)
    _write(store, [_daily("KRX:005930", D2, 103, PROVISIONAL_SOURCE, PROV2_RUN, 0)], PROV2_RUN)
    return store


def _latest(store, start, end):
    import pyarrow.dataset as ds
    tbl = store.read_arrow(
        "silver", "daily_bars", columns=["security_id", "trading_date", "close", "source"],
        filters=(ds.field("trading_date") >= start) & (ds.field("trading_date") <= end),
        latest_by=DAILY_BAR_LOGICAL_KEY)
    return sorted(tbl.to_pylist(), key=lambda r: (r["security_id"], r["trading_date"]))


def _export(store, tmp_path, start=D1, end=D2):
    path, manifest = export_package(start, end, tables=["daily_bars"],
                                    out_dir=str(tmp_path / "DOWNLOAD"), store=store,
                                    with_status=False, log=None)
    return path, manifest


def test_파티션_경로가_구간과_겹치는지():
    rng = (date(2026, 10, 27), date(2026, 11, 3))
    assert _overlaps(_parts("market=KRX/year=2026/month=11"), *rng)
    assert _overlaps(_parts("market=KRX/year=2026/month=10"), *rng)
    assert not _overlaps(_parts("market=KRX/year=2026/month=09"), *rng)
    assert not _overlaps(_parts("year=2025"), *rng)
    assert _overlaps(_parts("dataset=minute/year=2026"), *rng)
    assert _overlaps(_parts("market=KOSPI"), *rng)       # 연도 없는 파티션은 읽는다


def test_내보내기는_구간_행만_리비전째_담는다(pc1, tmp_path):
    path, manifest = _export(pc1, tmp_path)
    entry = manifest["tables"]["daily_bars"]
    assert entry["rows"] == 3                             # 10/30은 빠진다
    assert entry["rows_by_date"] == {"2026-11-02": 2, "2026-11-03": 1}
    assert entry["daily_kinds"] == {"2026-11-02": {"provisional": 1, "final": 1},
                                    "2026-11-03": {"provisional": 1, "final": 0}}
    assert read_package(path)["package_id"] == manifest["package_id"]


def test_가져오면_PC1과_같은_최신_봉을_읽는다(pc1, tmp_path):
    path, _ = _export(pc1, tmp_path)
    pc2 = ParquetStore(root=str(tmp_path / "pc2" / "data"))
    summary = import_package(path, store=pc2, log=None)
    assert summary["tables"]["daily_bars"]["new_rows"] == 3
    assert _latest(pc2, D1, D2) == _latest(pc1, D1, D2)
    got = {r["trading_date"]: r["source"] for r in _latest(pc2, D1, D2)}
    assert got == {D1: "KIS", D2: PROVISIONAL_SOURCE}     # 11/2는 확정이 잠정을 이긴다
    assert os.path.exists(os.path.join(pc2.root, summary["record"]))


def test_같은_묶음을_다시_가져오면_0행(pc1, tmp_path):
    path, _ = _export(pc1, tmp_path)
    pc2 = ParquetStore(root=str(tmp_path / "pc2" / "data"))
    import_package(path, store=pc2, log=None)
    files = pc2.files_of("silver", "daily_bars")
    again = import_package(path, store=pc2, log=None)
    assert again["already_imported"]
    assert again["tables"]["daily_bars"]["new_rows"] == 0
    assert pc2.files_of("silver", "daily_bars") == files  # 새 파일 없음
    assert len(pc2.read("silver", "daily_bars", dedupe=False)) == 3


def test_이미_있는_행은_빼고_없는_것만_쓴다(pc1, tmp_path):
    pc2 = ParquetStore(root=str(tmp_path / "pc2" / "data"))
    # PC2는 11/2 잠정 봉까지만 받아 둔 상태
    _write(pc2, [_daily("KRX:005930", D1, 101, PROVISIONAL_SOURCE, PROV_RUN, 0)], PROV_RUN)
    path, _ = _export(pc1, tmp_path)
    summary = import_package(path, store=pc2, log=None)
    res = summary["tables"]["daily_bars"]
    assert (res["new_rows"], res["skipped_rows"]) == (2, 1)
    assert _latest(pc2, D1, D2) == _latest(pc1, D1, D2)


def test_확인만_하면_쓰지_않는다(pc1, tmp_path):
    path, _ = _export(pc1, tmp_path)
    pc2 = ParquetStore(root=str(tmp_path / "pc2" / "data"))
    summary = import_package(path, store=pc2, dry_run=True, log=None)
    assert summary["tables"]["daily_bars"]["new_rows"] == 3
    assert pc2.files_of("silver", "daily_bars") == []
    assert "record" not in summary


def test_손상된_묶음은_거부한다(pc1, tmp_path):
    path, manifest = _export(pc1, tmp_path)
    member = manifest["tables"]["daily_bars"]["files"][0]["member"]
    broken = str(tmp_path / "broken.zip")
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(broken, "w") as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            dst.writestr(info, data[:-8] + b"\0" * 8 if info.filename == member else data)
    with pytest.raises(PackageError, match="해시"):
        read_package(broken)
    pc2 = ParquetStore(root=str(tmp_path / "pc2" / "data"))
    with pytest.raises(PackageError):
        import_package(broken, store=pc2, log=None)
    assert pc2.files_of("silver", "daily_bars") == []


def test_종료일이_시작일보다_앞이면_오류(pc1, tmp_path):
    with pytest.raises(ValueError):
        _export(pc1, tmp_path, start=D2, end=D1)

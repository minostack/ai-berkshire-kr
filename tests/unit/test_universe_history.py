# -*- coding: utf-8 -*-
"""유니버스 이력 — 생존편향을 막는 마지막 방어선.

여기서 지키는 것은 하나다: **모르는 것을 아는 척하지 않는다.**
- 이력이 없으면 오늘 구성종목을 과거에 적용하지 않고 막는다.
- 스냅샷에서 역산한 구간은 경계가 불확실하다는 사실을 데이터에 남긴다.
- 관측 구간 밖 날짜는 빈 목록이 아니라 오류로 답한다.

빈 목록을 돌려주면 호출부가 "그날은 구성종목이 없었나 보다"로 받아들이고 조용히
넘어간다. 그게 생존편향이 들어오는 전형적인 경로다.
"""

from datetime import date, datetime, timezone

import pytest

from cvd_obv.contracts.schema import validate_rows
from cvd_obv.contracts.universe_schema import (
    UNIVERSE_MEMBERSHIP_COLUMNS, UNIVERSE_MEMBERSHIP_PK,
)
from cvd_obv.domain.errors import ConfigError, NoInputDataError, SurvivorshipError
from cvd_obv.ingestion.universe_history import (
    UniverseRepository, UniverseSnapshot, derive_intervals,
)
from cvd_obv.storage.parquet_store import ParquetStore

RUN_ID = "test_20260918000000_aaaaaaaa"


def _snap(day, codes, ref="snap"):
    return UniverseSnapshot("KOSPI200", day, codes, f"{ref}-{day}")


@pytest.fixture
def snapshots():
    return [
        _snap(date(2024, 1, 2), ["005930", "000660", "035420"]),
        _snap(date(2025, 1, 2), ["005930", "000660", "051910"]),
        _snap(date(2026, 1, 2), ["005930", "051910", "006400"]),
    ]


@pytest.fixture
def repo(tmp_path, snapshots):
    r = UniverseRepository(store=ParquetStore(root=str(tmp_path)))
    r.import_snapshots(snapshots, run_id=RUN_ID)
    return r


def _row_for(rows, code, index=0):
    matches = sorted((r for r in rows if r["security_id"] == f"KRX:{code}"),
                     key=lambda r: r["effective_from"])
    return matches[index]


# ─────────────────────────────────────────────────────────────
# 구간 역산
# ─────────────────────────────────────────────────────────────

def test_계속_남은_종목은_구간이_이어진다(snapshots):
    rows = derive_intervals(snapshots, "uv_test", RUN_ID)
    row = _row_for(rows, "005930")
    assert row["change_type"] == "INITIAL"
    assert row["effective_to"] is None          # 마지막까지 유효
    assert row["observed_from"] == date(2024, 1, 2)
    assert row["observed_to"] == date(2026, 1, 2)


def test_중간에_빠진_종목은_구간이_닫힌다(snapshots):
    rows = derive_intervals(snapshots, "uv_test", RUN_ID)
    row = _row_for(rows, "035420")              # 2025 스냅샷에서 사라짐
    assert row["effective_to"].date() == date(2025, 1, 1)   # UTC 저장
    assert row["observed_to"] == date(2024, 1, 2)


def test_나중에_들어온_종목은_ADD로_표시된다(snapshots):
    rows = derive_intervals(snapshots, "uv_test", RUN_ID)
    row = _row_for(rows, "006400")
    assert row["change_type"] == "ADD"
    assert row["observed_from"] == date(2026, 1, 2)


def test_스냅샷_간격이_불확실성으로_남는다(snapshots):
    """연 1회 스냅샷이면 '어느 날 편입'인지 1년 안에서만 알 수 있다."""
    rows = derive_intervals(snapshots, "uv_test", RUN_ID)
    added = _row_for(rows, "006400")
    assert added["from_uncertainty_days"] == 365
    assert added["date_resolution"] == "SNAPSHOT_DERIVED"
    # 첫 스냅샷 종목은 그 이전을 알 수 없다 → -1(미상)
    assert _row_for(rows, "005930")["from_uncertainty_days"] == -1


def test_빠졌다_다시_들어오면_구간이_두_개다():
    snaps = [
        _snap(date(2024, 1, 2), ["005930", "000660"]),
        _snap(date(2025, 1, 2), ["005930"]),
        _snap(date(2026, 1, 2), ["005930", "000660"]),
    ]
    rows = derive_intervals(snaps, "uv_test", RUN_ID)
    intervals = [r for r in rows if r["security_id"] == "KRX:000660"]
    assert len(intervals) == 2
    UniverseRepository.validate_rows(rows)      # 겹치지 않아야 한다


def test_같은_날짜_스냅샷이_두_번이면_거부한다():
    snaps = [_snap(date(2024, 1, 2), ["005930"]),
             _snap(date(2024, 1, 2), ["000660"])]
    with pytest.raises(ConfigError):
        derive_intervals(snaps, "uv_test", RUN_ID)


def test_빈_스냅샷은_거부한다():
    with pytest.raises(ConfigError):
        _snap(date(2024, 1, 2), [])


def test_지수가_섞이면_거부한다():
    snaps = [UniverseSnapshot("KOSPI200", date(2024, 1, 2), ["005930"], "a"),
             UniverseSnapshot("KOSDAQ150", date(2025, 1, 2), ["035420"], "b")]
    with pytest.raises(ConfigError):
        derive_intervals(snaps, "uv_test", RUN_ID)


# ─────────────────────────────────────────────────────────────
# 조회 — 생존편향 차단
# ─────────────────────────────────────────────────────────────

def test_시점별_구성종목을_결정론적으로_조회한다(repo):
    got = repo.get_members("KOSPI200", date(2024, 6, 1))
    assert got["members"] == ["KRX:000660", "KRX:005930", "KRX:035420"]
    later = repo.get_members("KOSPI200", date(2025, 6, 1))
    assert later["members"] == ["KRX:000660", "KRX:005930", "KRX:051910"]


def test_같은_질의는_같은_답을_준다(repo):
    a = repo.get_members("KOSPI200", date(2025, 6, 1))
    b = repo.get_members("KOSPI200", date(2025, 6, 1))
    assert a["members"] == b["members"]
    assert a["universe_version"] == b["universe_version"]


def test_이력이_없으면_빈_목록_대신_막는다(tmp_path):
    """오늘 구성종목을 과거에 적용하는 것이 생존편향의 전형적 경로다."""
    empty = UniverseRepository(store=ParquetStore(root=str(tmp_path)))
    with pytest.raises((SurvivorshipError, NoInputDataError)):
        empty.get_members("KOSPI200", date(2020, 1, 2))


def test_관측_구간_밖_날짜는_거부한다(repo):
    with pytest.raises(SurvivorshipError) as exc:
        repo.get_members("KOSPI200", date(2015, 1, 2))
    assert exc.value.detail_code == "UNIVERSE_INCOMPLETE"

    with pytest.raises(SurvivorshipError):
        repo.get_members("KOSPI200", date(2030, 1, 2))


def test_불확실성이_크면_막을_수_있다(repo):
    """경계가 1년씩 흔들리는 이력으로 replay하면 안 되는 날짜가 있다."""
    # 허용치를 넉넉히 주면 통과
    assert repo.get_members("KOSPI200", date(2025, 6, 1),
                            max_gap_days=400)["member_count"] == 3
    # 30일로 조이면 막힌다
    with pytest.raises(SurvivorshipError) as exc:
        repo.get_members("KOSPI200", date(2025, 6, 1), max_gap_days=30)
    assert exc.value.detail_code == "UNIVERSE_INCOMPLETE"


def test_조회_결과가_불확실성을_함께_알려준다(repo):
    got = repo.get_members("KOSPI200", date(2025, 6, 1))
    assert got["max_uncertainty_days"] > 0
    assert got["date_resolution"] == "SNAPSHOT_DERIVED"


# ─────────────────────────────────────────────────────────────
# 저장 계약
# ─────────────────────────────────────────────────────────────

def test_저장_행이_스키마를_통과한다(repo):
    rows = repo.load("KOSPI200")
    validate_rows(rows, UNIVERSE_MEMBERSHIP_COLUMNS, UNIVERSE_MEMBERSHIP_PK,
                  "silver.universe_membership")
    # 5종목 × 각 1구간 (빠졌다 다시 들어온 종목이 없다)
    assert len(rows) == 5


def test_유효구간이_겹치지_않는다(repo):
    assert UniverseRepository.validate_rows(repo.load("KOSPI200")) == 5


def test_겹치는_구간은_거부한다():
    base = {
        "index_id": "KOSPI200", "security_id": "KRX:005930",
        "known_at_utc": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "change_type": "ADD", "date_resolution": "EXACT_ANNOUNCEMENT",
        "from_uncertainty_days": 0, "to_uncertainty_days": 0,
        "observed_from": date(2024, 1, 2), "observed_to": date(2026, 1, 2),
        "source_ref": "x", "universe_version": "uv", "run_id": RUN_ID,
        "schema_version": "2.0.0", "data_revision": 1,
    }
    overlapping = [
        dict(base, effective_from=datetime(2024, 1, 1, tzinfo=timezone.utc),
             effective_to=datetime(2025, 6, 1, tzinfo=timezone.utc)),
        dict(base, effective_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
             effective_to=None),
    ]
    with pytest.raises(ConfigError):
        UniverseRepository.validate_rows(overlapping)


def test_이력_버전은_스냅샷_집합에_결정적이다(snapshots):
    a = UniverseRepository.make_version(snapshots)
    b = UniverseRepository.make_version(list(reversed(snapshots)))
    assert a == b
    changed = snapshots[:-1] + [_snap(date(2026, 1, 2), ["005930"])]
    assert UniverseRepository.make_version(changed) != a


def test_커버리지_요약이_관측_구간과_간격을_보여준다(repo):
    cov = repo.coverage("KOSPI200")
    assert cov["observed_start"] == date(2024, 1, 2)
    assert cov["observed_end"] == date(2026, 1, 2)
    assert cov["max_gap_days"] == 366
    assert cov["securities"] == 5
    assert len(cov["snapshot_dates"]) == 3


# ─────────────────────────────────────────────────────────────
# 현재 구성종목을 과거에 쓰는 경우 (생존편향을 감수하되 표시한다)
# ─────────────────────────────────────────────────────────────

def test_현재_구성종목_사용은_기본적으로_막힌다(repo):
    with pytest.raises(SurvivorshipError):
        repo.get_members("KOSPI200", date(2015, 1, 2))


def test_명시적으로_허용하면_상태를_붙여_돌려준다(repo):
    """과거 구성종목을 구할 수 없을 때의 타협 — 대신 사실을 결과에 박는다."""
    got = repo.get_members("KOSPI200", date(2015, 1, 2),
                           allow_current_snapshot=True)
    assert got["survivorship_status"] == "CURRENT_SNAPSHOT_ONLY"
    assert got["snapshot_drift_days"] > 3000      # 2015 → 2024 관측 시작
    # 가장 가까운 관측 시점(첫 스냅샷)의 구성으로 대체한다.
    assert got["members"] == ["KRX:000660", "KRX:005930", "KRX:035420"]


def test_이력이_덮는_날짜는_허용해도_HISTORICAL이다(repo):
    """타협 플래그를 켰다고 해서 멀쩡한 이력까지 오염시키지 않는다."""
    got = repo.get_members("KOSPI200", date(2025, 6, 1),
                           allow_current_snapshot=True)
    assert got["survivorship_status"] == "HISTORICAL"
    assert got["snapshot_drift_days"] == 0


def test_미래_날짜도_마지막_관측으로_대체된다(repo):
    got = repo.get_members("KOSPI200", date(2030, 1, 2),
                           allow_current_snapshot=True)
    assert got["survivorship_status"] == "CURRENT_SNAPSHOT_ONLY"
    assert got["members"] == ["KRX:005930", "KRX:006400", "KRX:051910"]

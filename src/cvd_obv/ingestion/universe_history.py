# -*- coding: utf-8 -*-
"""유니버스 이력 — 아키텍처 §6 `universe_history`, 로드맵 Stage 3.

두 가지 입력을 받는다.

1. **변경 공시** — 편입/편출 시점이 하루 단위로 정확하다.
2. **날짜별 스냅샷** — "그 날짜에 이 종목들이 들어 있었다"는 사실만 안다.
   연속한 두 스냅샷을 비교해 구간을 역산하되, 경계가 그 간격만큼 불확실하다는
   사실을 행마다 남긴다.

왜 불확실성을 남기는가
----------------------
스냅샷이 1년 간격이면 "2016년 어느 날 편입"까지만 알 수 있다. 이걸 특정 날짜로
저장해 버리면 그 근처 replay가 왜 어긋나는지 영영 모른다. 경계 근처 날짜는
`UNIVERSE_INCOMPLETE`로 막을 수 있어야 하고, 그러려면 불확실성이 데이터에
있어야 한다.

생존편향 경고
-------------
오늘의 구성종목 하나만 있는 상태는 유니버스 이력이 **아니다**. 그 경우
`get_members()`는 과거 날짜에 대해 값을 돌려주지 않고 막는다. 조용히 오늘
구성종목을 과거에 적용하는 것이 생존편향의 전형적인 경로이기 때문이다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .. import SCHEMA_VERSION
from ..contracts.universe_schema import (
    DATE_RESOLUTIONS, UNIVERSE_MEMBERSHIP_COLUMNS, UNIVERSE_MEMBERSHIP_PK,
)
from ..domain.errors import ConfigError, NoInputDataError, SurvivorshipError
from ..domain.ids import KST, normalize_security_id, sha256_of, to_utc
from ..domain.status import DetailCode
from ..storage.parquet_store import ParquetStore

__all__ = ["UniverseSnapshot", "UniverseRepository", "derive_intervals"]


class UniverseSnapshot:
    """어떤 날짜에 관측한 구성종목 목록."""

    __slots__ = ("index_id", "as_of", "securities", "source_ref")

    def __init__(self, index_id: str, as_of: date, securities,
                 source_ref: str):
        self.index_id = str(index_id).upper()
        self.as_of = as_of
        self.securities = sorted({normalize_security_id(s) for s in securities})
        if not self.securities:
            raise ConfigError(f"{index_id} {as_of}: 구성종목이 비어 있다")
        self.source_ref = str(source_ref)

    def __len__(self):
        return len(self.securities)

    def __repr__(self):
        return (f"UniverseSnapshot({self.index_id}, {self.as_of}, "
                f"{len(self.securities)}종목)")


def _utc_midnight(day: date) -> datetime:
    """KST 그날 0시를 UTC로. 구간 경계는 날짜 단위로 다룬다."""
    return to_utc(datetime(day.year, day.month, day.day, tzinfo=KST))


def derive_intervals(snapshots, universe_version: str, run_id: str,
                     known_at: datetime = None, data_revision: int = 1,
                     date_resolution: str = "SNAPSHOT_DERIVED"):
    """날짜별 스냅샷 목록 → membership 구간 행.

    연속한 스냅샷 `S(t1)`, `S(t2)`를 비교한다.
      - 양쪽에 있으면 구간이 이어진다.
      - `t1`에만 있으면 `(t1, t2]` 사이에 빠졌다 → `effective_to = t2`,
        불확실성 `t2 - t1`일.
      - `t2`에만 있으면 `(t1, t2]` 사이에 들어왔다 → `effective_from = t2`,
        불확실성 `t2 - t1`일.

    첫 스냅샷의 종목은 `INITIAL`이며, 그 이전 상황은 알 수 없으므로
    `from_uncertainty_days`를 `-1`(미상)로 둔다.

    `date_resolution="EXACT_ANNOUNCEMENT"`를 주면 경계를 정확한 날짜로 본다.
    거래일마다 시총 항등식으로 검산해 변경일을 특정한 경우(`universe_rebuild`)가
    여기에 해당한다. 이때 불확실성은 0일이다. 스냅샷을 띄엄띄엄 관측한 경우에
    이 값을 주면 안 된다 — 모르는 것을 아는 것처럼 저장하게 된다.
    """
    if date_resolution not in DATE_RESOLUTIONS:
        raise ConfigError(f"date_resolution이 계약에 없다: {date_resolution}")
    exact = date_resolution == "EXACT_ANNOUNCEMENT"
    snapshots = sorted(snapshots, key=lambda s: s.as_of)
    if not snapshots:
        raise NoInputDataError("스냅샷이 없다")
    index_ids = {s.index_id for s in snapshots}
    if len(index_ids) != 1:
        raise ConfigError(f"한 번에 한 지수만 처리한다: {sorted(index_ids)}")
    dates = [s.as_of for s in snapshots]
    if len(set(dates)) != len(dates):
        raise ConfigError(f"같은 날짜 스냅샷이 두 번 있다: {dates}")

    index_id = snapshots[0].index_id
    known = to_utc(known_at or datetime.now(tz=KST))

    # 종목별로 (등장 스냅샷 인덱스) 목록을 만든다.
    seen = {}
    for i, snap in enumerate(snapshots):
        for sid in snap.securities:
            seen.setdefault(sid, []).append(i)

    rows = []
    for sid, indices in sorted(seen.items()):
        # 연속한 구간으로 쪼갠다(중간에 빠졌다 다시 들어온 경우 대비).
        runs, start = [], indices[0]
        for prev, cur in zip(indices, indices[1:]):
            if cur != prev + 1:
                runs.append((start, prev))
                start = cur
        runs.append((start, indices[-1]))

        for first, last in runs:
            is_initial = (first == 0)
            from_gap = (-1 if is_initial else 0 if exact
                        else (dates[first] - dates[first - 1]).days)
            if last + 1 < len(snapshots):
                effective_to = _utc_midnight(dates[last + 1])
                to_gap = (0 if exact
                          else (dates[last + 1] - dates[last]).days)
            else:
                effective_to = None       # 마지막 스냅샷까지 유효
                to_gap = -1
            rows.append({
                "index_id": index_id,
                "security_id": sid,
                "effective_from": _utc_midnight(dates[first]),
                "effective_to": effective_to,
                "known_at_utc": known,
                "change_type": "INITIAL" if is_initial else "ADD",
                "date_resolution": date_resolution,
                "from_uncertainty_days": int(from_gap),
                "to_uncertainty_days": int(to_gap),
                "observed_from": dates[first],
                "observed_to": dates[last],
                "source_ref": snapshots[first].source_ref,
                "universe_version": universe_version,
                "run_id": run_id,
                "schema_version": SCHEMA_VERSION,
                "data_revision": int(data_revision),
            })
    return rows


class UniverseRepository:
    """`silver.universe_membership` 적재·조회·검증."""

    LAYER = "silver"
    TABLE = "universe_membership"

    def __init__(self, store: ParquetStore = None, data_revision: int = 1):
        self.store = store or ParquetStore()
        self.data_revision = int(data_revision)

    @staticmethod
    def _partition(row):
        return [("index_id", row["index_id"]),
                ("effective_year", row["effective_from"].year)]

    @staticmethod
    def make_version(snapshots) -> str:
        """스냅샷 집합에 결정적인 이력 버전."""
        spec = [{"index_id": s.index_id, "as_of": s.as_of,
                 "n": len(s), "members": s.securities} for s in
                sorted(snapshots, key=lambda x: x.as_of)]
        return "uv_" + sha256_of(spec)[len("sha256:"):][:16]

    # ── 적재 ───────────────────────────────────────────────

    def import_snapshots(self, snapshots, run_id: str,
                         universe_version: str = None, dry_run: bool = False,
                         date_resolution: str = "SNAPSHOT_DERIVED",
                         evidence: dict = None):
        """스냅샷 목록을 구간으로 바꿔 적재한다.

        `date_resolution="EXACT_ANNOUNCEMENT"`는 경계를 거래일 단위로 특정한
        경우에만 쓴다(`universe_rebuild`의 시총 항등식 검산). `evidence`는
        매니페스트에 남길 근거 — 검산 일치율 같은 것이다.
        """
        snapshots = list(snapshots)
        version = universe_version or self.make_version(snapshots)
        rows = derive_intervals(snapshots, version, run_id,
                                data_revision=self.data_revision,
                                date_resolution=date_resolution)
        self.validate_rows(rows)

        summary = {
            "run_id": run_id,
            "index_id": rows[0]["index_id"],
            "universe_version": version,
            "snapshots": [{"as_of": s.as_of.isoformat(), "members": len(s),
                           "source_ref": s.source_ref}
                          for s in sorted(snapshots, key=lambda x: x.as_of)],
            "rows": len(rows),
            "securities": len({r["security_id"] for r in rows}),
            "coverage_start": min(s.as_of for s in snapshots).isoformat(),
            "coverage_end": max(s.as_of for s in snapshots).isoformat(),
            "max_gap_days": max((r["from_uncertainty_days"] for r in rows),
                                default=-1),
            "artifacts": [],
        }
        if dry_run:
            return summary

        write = self.store.write(
            self.LAYER, self.TABLE, rows, UNIVERSE_MEMBERSHIP_COLUMNS,
            UNIVERSE_MEMBERSHIP_PK, run_id, partition_fn=self._partition,
            extra_manifest={
                "universe_version": version,
                "snapshot_dates": [s.as_of.isoformat() for s in snapshots],
                "date_resolution": date_resolution,
                "evidence": evidence or {},
                "note": ("거래일마다 시총 항등식으로 검산해 경계를 특정했다"
                         if date_resolution == "EXACT_ANNOUNCEMENT" else
                         "스냅샷에서 역산한 구간이다. 경계는 스냅샷 간격만큼 "
                         "불확실하며 행마다 uncertainty_days로 기록했다"),
            })
        summary["artifacts"] = [write.manifest_path]
        return summary

    # ── 검증 ───────────────────────────────────────────────

    @staticmethod
    def validate_rows(rows):
        """같은 지수·종목의 유효구간이 겹치지 않는지 확인한다(스키마 §5 불변식)."""
        by_key = {}
        for row in rows:
            by_key.setdefault((row["index_id"], row["security_id"]),
                              []).append(row)
        for key, group in by_key.items():
            group.sort(key=lambda r: r["effective_from"])
            for prev, cur in zip(group, group[1:]):
                if prev["effective_to"] is None:
                    raise ConfigError(
                        f"{key}: 열린 구간 뒤에 또 구간이 있다")
                if cur["effective_from"] < prev["effective_to"]:
                    raise ConfigError(
                        f"{key}: 유효구간이 겹친다 "
                        f"{prev['effective_from']}~{prev['effective_to']} vs "
                        f"{cur['effective_from']}")
        return len(rows)

    # ── 조회 ───────────────────────────────────────────────

    def load(self, index_id: str = None, universe_version: str = None):
        """이력 행을 읽는다.

        버전을 지정하지 않으면 **가장 최근에 적재된 한 버전만** 돌려준다.
        복원을 여러 번 돌리면 `universe_version`이 다른 행이 함께 쌓이는데,
        이것을 섞어 읽으면 같은 날 구성종목이 400종목이 되는 식으로 조용히
        망가진다(2026-09-18 실측). 섞어 읽느니 한 버전만 읽는 편이 낫다.
        """
        def _where(row):
            if index_id and row["index_id"] != index_id.upper():
                return False
            if universe_version and row["universe_version"] != universe_version:
                return False
            return True

        rows = self.store.read(self.LAYER, self.TABLE,
                               pk=UNIVERSE_MEMBERSHIP_PK, where=_where)
        if universe_version or not rows:
            return rows
        versions = {r["universe_version"] for r in rows}
        if len(versions) == 1:
            return rows
        latest = max(versions,
                     key=lambda v: max(r["known_at_utc"] for r in rows
                                       if r["universe_version"] == v))
        return [r for r in rows if r["universe_version"] == latest]

    def coverage(self, index_id: str, universe_version: str = None):
        """이력이 실제로 덮는 구간과 불확실성."""
        rows = self.load(index_id, universe_version)
        if not rows:
            raise NoInputDataError(
                f"{index_id} 유니버스 이력이 없다 — `universe import` 먼저")
        observed = [r["observed_from"] for r in rows] + \
                   [r["observed_to"] for r in rows]
        gaps = [r["from_uncertainty_days"] for r in rows
                if r["from_uncertainty_days"] > 0]
        return {
            "index_id": index_id.upper(),
            "universe_version": rows[0]["universe_version"],
            "rows": len(rows),
            "securities": len({r["security_id"] for r in rows}),
            "observed_start": min(observed),
            "observed_end": max(observed),
            "snapshot_dates": sorted({r["observed_from"] for r in rows}
                                     | {r["observed_to"] for r in rows}),
            "max_gap_days": max(gaps) if gaps else 0,
            "open_ended": sum(1 for r in rows if r["effective_to"] is None),
        }

    def membership_intervals(self, index_id: str,
                             universe_version: str = None):
        """{종목: [(시작일, 종료일|None) …]} — 백테스트 모집단 필터용.

        `get_members`를 날짜마다 부르면 이력을 매번 훑는다. 백테스트는 수백만
        (종목, 날짜) 쌍을 걸러야 하므로 구간을 한 번만 꺼내 쓴다.
        """
        rows = self.load(index_id, universe_version)
        if not rows:
            raise SurvivorshipError(
                f"{index_id} 유니버스 이력이 없다 — 오늘 구성종목을 과거에 "
                f"적용하지 않는다",
                detail_code=DetailCode.UNIVERSE_INCOMPLETE)
        out = {}
        for row in rows:
            start = row["effective_from"].astimezone(KST).date()
            end = (row["effective_to"].astimezone(KST).date()
                   if row["effective_to"] is not None else None)
            out.setdefault(row["security_id"], []).append((start, end))
        for intervals in out.values():
            intervals.sort()
        return out

    def get_members(self, index_id: str, as_of, universe_version: str = None,
                    max_gap_days: int = None,
                    allow_current_snapshot: bool = False):
        """그 시점의 구성종목.

        이력이 그 날짜를 덮지 않으면 기본적으로 **빈 목록 대신 오류**를 낸다.
        조용히 빈 값이나 오늘 구성종목을 돌려주면 그게 곧 생존편향이다.

        `allow_current_snapshot=True`
        ------------------------------
        과거 구성종목을 구할 수 없을 때(키움은 제공하지 않고 KRX는 계정이
        필요하다) 현재 구성종목을 과거에 적용하는 것을 **의도적으로** 허용한다.
        그 대신 결과에 `survivorship_status="CURRENT_SNAPSHOT_ONLY"`를 붙여
        호출부가 그 사실을 잊지 못하게 한다.

        이 값이 붙은 결과로 해도 되는 것과 안 되는 것:
          할 수 있다 — 조건 A와 조건 B의 **비교**. 같은 편향이 양쪽에 걸린다.
          하면 안 된다 — 절대 수치를 "실제 성과"로 인용, 확률 모델 학습,
                         로드맵 §9 출시 판단.
        """
        rows = self.load(index_id, universe_version)
        if not rows:
            raise SurvivorshipError(
                f"{index_id} 유니버스 이력이 없다 — 오늘 구성종목을 과거에 "
                f"적용하지 않는다",
                detail_code=DetailCode.UNIVERSE_INCOMPLETE)

        at = as_of if isinstance(as_of, datetime) else _utc_midnight(as_of)
        at = to_utc(at)

        observed = sorted({r["observed_from"] for r in rows}
                          | {r["observed_to"] for r in rows})
        first, last = _utc_midnight(observed[0]), _utc_midnight(observed[-1])
        outside = at < first or at > last
        if outside and not allow_current_snapshot:
            raise SurvivorshipError(
                f"{index_id} {at.date()}는 이력 관측 구간 "
                f"({observed[0]} ~ {observed[-1]}) 밖이다",
                detail_code=DetailCode.UNIVERSE_INCOMPLETE)

        # 관측 구간 밖이면 가장 가까운 관측 시점의 구성으로 대체한다.
        lookup_at = min(max(at, first), last) if outside else at

        members, worst_gap = [], 0
        for row in rows:
            if row["effective_from"] > lookup_at:
                continue
            if (row["effective_to"] is not None
                    and row["effective_to"] <= lookup_at):
                continue
            members.append(row["security_id"])
            worst_gap = max(worst_gap, row["from_uncertainty_days"],
                            row["to_uncertainty_days"])

        if max_gap_days is not None and worst_gap > max_gap_days:
            raise SurvivorshipError(
                f"{index_id} {at.date()}: 구간 경계 불확실성이 "
                f"{worst_gap}일로 허용치({max_gap_days}일)를 넘는다",
                detail_code=DetailCode.UNIVERSE_INCOMPLETE)

        if outside:
            status = "CURRENT_SNAPSHOT_ONLY"
            drift_days = abs((at.date() - lookup_at.date()).days)
        else:
            status = "HISTORICAL"
            drift_days = 0

        return {
            "index_id": index_id.upper(),
            "as_of": at,
            "members": sorted(members),
            "member_count": len(members),
            "universe_version": rows[0]["universe_version"],
            "max_uncertainty_days": worst_gap,
            "date_resolution": rows[0]["date_resolution"],
            # 이 두 값은 결과 artifact까지 전파돼야 한다.
            "survivorship_status": status,
            "snapshot_drift_days": drift_days,
        }

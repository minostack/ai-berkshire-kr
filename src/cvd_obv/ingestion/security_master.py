# -*- coding: utf-8 -*-
"""종목 마스터 — 상장폐지된 종목까지 남기는 명부(스키마 §5).

왜 필요한가
-----------
키움 `ka10099`(종목정보 리스트)에는 **현재 상장 종목만** 있다. 한진해운(117930)도
STX조선해양(067250)도 목록에 없다. 그 목록으로 과거를 보면, 망한 회사는 애초에
존재한 적이 없는 것이 된다. 생존편향이 데이터 수집 단계에서 들어오는 경로다.

KRX Open API의 유가증권 일별매매정보는 다르다. **그날 상장돼 있던 종목을 그날
기준으로** 준다. 그래서 2010-01-04부터 하루씩 훑으면 사라진 종목도 사라진
날짜와 함께 남는다. 이 모듈이 하는 일이 그것이다.

알 수 없는 것은 null로 둔다
---------------------------
- `listed_from`: 관측 첫날이 패널 시작일(2010-01-04)과 같으면 그 이전부터
  상장돼 있었다는 뜻이므로 **null**이다. 그날 상장했다고 적으면 거짓이 된다.
- `delisted_on`: 마지막 관측 다음 거래일. 패널 마지막 날까지 보이면 null이다.
- 사명 변경은 `valid_from`/`valid_to`로 끊어 여러 행이 된다(SCD2).
"""

from __future__ import annotations

from datetime import datetime

from .. import SCHEMA_VERSION
from ..domain.errors import NoInputDataError
from ..domain.ids import KST, UTC, normalize_security_id
from ..contracts.universe_schema import (
    SECURITY_MASTER_COLUMNS, SECURITY_MASTER_PK,
)
from ..storage.parquet_store import ParquetStore
from .krx_openapi import KRX_OPENAPI_SOURCE, STOCK_DAILY, market_for

__all__ = ["SecurityMasterBuilder"]


class SecurityMasterBuilder:
    """KRX 일별매매정보 캐시를 훑어 종목 명부를 만든다."""

    LAYER = "silver"
    TABLE = "security_master"

    def __init__(self, api, store: ParquetStore = None, data_revision: int = 1,
                 markets=None):
        """`markets`는 훑을 지수 이름들(`"KOSPI200"`, `"KOSDAQ150"`).

        기본값은 유가증권 하나다 — 기존 호출부의 동작을 바꾸지 않는다.
        여러 개를 주면 **같은 패널 경계**로 한 번에 훑는다. 따로 돌리면
        시장마다 패널 끝이 달라져 폐지일 역산이 어긋난다.
        """
        self.api = api
        self.store = store or ParquetStore()
        self.data_revision = int(data_revision)
        self.services = ([STOCK_DAILY] if not markets else
                         [market_for(m).stock_service for m in markets])

    @staticmethod
    def _partition(row):
        return [("market", row["market"] or "KRX")]

    def scan(self, days, on_progress=None):
        """거래일별 응답에서 (종목, 이름) 관측 구간을 모은다."""
        if not days:
            raise NoInputDataError("거래일이 없다")
        runs = {}          # code -> [{"name", "first", "last", "market", "isin"}]
        seen_days = []
        for day in days:
            bas = day.strftime("%Y%m%d")
            rows = []
            for service in self.services:
                rows.extend(self.api.fetch(service, bas) or [])
            if not rows:
                continue
            seen_days.append(day)
            for row in rows:
                code = str(row.get("ISU_CD") or "").strip()
                if not code:
                    continue
                name = str(row.get("ISU_NM") or "").strip()
                # 예전에는 비면 "KOSPI"로 뒀다. 코스닥을 함께 훑는 지금은
                # 그 기본값이 **코스닥 종목을 유가증권으로 적는다.** 모르면
                # 모른다고 둔다 — partition이 "KRX"로 떨어진다.
                market = str(row.get("MKT_NM") or "").strip() or None
                bucket = runs.setdefault(code, [])
                if bucket and bucket[-1]["name"] == name:
                    bucket[-1]["last"] = day
                else:
                    bucket.append({"name": name, "first": day, "last": day,
                                   "market": market})
            if on_progress:
                on_progress(day, len(runs))
        if not seen_days:
            raise NoInputDataError("응답이 있는 거래일이 하나도 없다")
        return runs, seen_days

    def make_rows(self, days, run_id: str, on_progress=None):
        """명부 행과 요약을 만든다(저장은 하지 않는다)."""
        runs, seen_days = self.scan(days, on_progress=on_progress)
        panel_start, panel_end = seen_days[0], seen_days[-1]
        known = datetime.now(tz=KST).astimezone(UTC)
        index = {d: i for i, d in enumerate(seen_days)}

        rows = []
        for code, spans in sorted(runs.items()):
            first_seen = spans[0]["first"]
            last_seen = spans[-1]["last"]
            # 마지막 관측 다음 거래일이 '더 이상 상장돼 있지 않은' 첫날이다.
            delisted_on = None
            if last_seen != panel_end:
                nxt = index[last_seen] + 1
                delisted_on = seen_days[nxt] if nxt < len(seen_days) else None
            for i, span in enumerate(spans):
                rows.append({
                    "security_id": normalize_security_id(code),
                    "local_code": code,
                    "market": span["market"],
                    "name": span["name"],
                    "isin": None,
                    "listed_from": (None if first_seen == panel_start
                                    else first_seen),
                    "delisted_on": delisted_on,
                    "predecessor_security_id": None,
                    "successor_security_id": None,
                    "valid_from": span["first"],
                    "valid_to": (spans[i + 1]["first"]
                                 if i + 1 < len(spans) else None),
                    "known_at_utc": known,
                    "source": KRX_OPENAPI_SOURCE,
                    "revision": i + 1,
                    "run_id": run_id,
                    "schema_version": SCHEMA_VERSION,
                    "data_revision": self.data_revision,
                })

        summary = {
            "run_id": run_id,
            "securities": len(runs),
            "rows": len(rows),
            "panel_start": panel_start.isoformat(),
            "panel_end": panel_end.isoformat(),
            "trading_days": len(seen_days),
            "delisted": len({r["local_code"] for r in rows
                             if r["delisted_on"]}),
            "renamed": len([code for code, spans in runs.items()
                            if len(spans) > 1]),
            "listed_during_panel": len({r["local_code"] for r in rows
                                        if r["listed_from"]}),
            "artifacts": [],
        }
        return rows, summary

    def build(self, days, run_id: str, dry_run: bool = False,
              on_progress=None):
        rows, summary = self.make_rows(days, run_id, on_progress=on_progress)
        if dry_run:
            return summary

        write = self.store.write(
            self.LAYER, self.TABLE, rows, SECURITY_MASTER_COLUMNS,
            SECURITY_MASTER_PK, run_id, partition_fn=self._partition,
            extra_manifest={
                "source": KRX_OPENAPI_SOURCE,
                "panel": [summary["panel_start"], summary["panel_end"]],
                "note": ("그날 상장돼 있던 종목을 그날 기준으로 받은 패널에서 "
                         "역산했다. 패널 시작일 이전의 상장일과, 패널 밖에서 "
                         "폐지된 종목은 알 수 없어 null이다"),
            })
        summary["artifacts"] = [write.manifest_path]
        return summary

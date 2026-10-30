# -*- coding: utf-8 -*-
"""KIS로 **현재** 지수 구성종목 스냅샷 — 코스피200·코스닥150 둘 다(2026-10-23).

KIS에는 "구성종목 목록" API가 없다. 대신 종목 현재가(`FHKST01010100`) 응답의
`rprs_mrkt_kor_name`(대표시장)이 지수 소속을 알려 준다 — 코스피200 구성종목은
`KOSPI200`, 코스닥150 구성종목은 `KSQ150`, 아니면 `KOSPI`/`KOSDAQ`.

실측(2026-10-23)
----------------
- 현재 구성종목 351개(코스피200 201·코스닥150 150): **351/351** 표시 일치
- 과거 편입됐다 빠진, 아직 상장된 376종목: 잘못 표시된 종목 **0**
- 공식 문서에서 "지수 소속" 의미를 확인하지는 못했다 — 실측으로만 확인한 규칙이다.
  정기변경(6·12월) 때 값이 언제 바뀌는지는 아직 관측하지 않았다

키움 `ka20002`와 다른 점: 코스닥150 전용 코드가 없어 키움으로는 코스닥150을 찍지
못했다(코스닥 전체 약 1,800종목). 여기서는 둘 다 정확히 나온다.

후보와 검산
-----------
종목마다 한 번씩 물어야 하므로 후보를 고른다 — KRX Open API 일별매매정보(로컬 캐시
`data/bronze/krx_openapi`)의 그 시장 시가총액 상위 `top`개 + 직전 명단. 고른 명단은
같은 날 **지수 상장시가총액 = 구성종목 시가총액 합**(`KrxOpenApi.checksum`, 오차 0원이
정상)으로 검산해 결과에 남긴다.
"""

from __future__ import annotations

from datetime import date, timedelta

from .kis_client import KisClient
from ..domain.errors import ConfigError, NoInputDataError

__all__ = ["KIS_UNIVERSE_SOURCE", "KisUniverseSource", "MEMBER_LABEL"]

KIS_UNIVERSE_SOURCE = "KIS_FHKST01010100"
_TR = "FHKST01010100"
_PATH = "/uapi/domestic-stock/v1/quotations/inquire-price"
#: 지수 → 대표시장 표시
MEMBER_LABEL = {"KOSPI200": "KOSPI200", "KOSDAQ150": "KSQ150"}
#: 시총 상위 후보 수 — 지수 크기보다 넉넉히(정기변경 편입 후보 포함)
DEFAULT_TOP = {"KOSPI200": 350, "KOSDAQ150": 400}
EXPECTED = {"KOSPI200": 200, "KOSDAQ150": 150}


class KisUniverseSource:
    """`KiwoomUniverseSource`와 같은 `fetch(index_id)` 모양."""

    SOURCE = KIS_UNIVERSE_SOURCE

    def __init__(self, client: KisClient = None, krx=None, top: int = None,
                 previous=None, log=None):
        self.client = client or KisClient()
        self._krx = krx
        self.top = top
        self.previous = previous          # index_id → 직전 명단(코드)
        self.log = log or (lambda msg: None)

    def token(self):
        return self.client.token()

    @property
    def krx(self):
        if self._krx is None:
            from .krx_openapi import KrxOpenApi
            self._krx = KrxOpenApi()
        return self._krx

    def latest_caps(self, index_id: str, as_of: date):
        """(기준일 YYYYMMDD, {코드: 시총}) — as_of 전 가장 최근 거래일."""
        for back in range(1, 15):
            d = (as_of - timedelta(days=back)).strftime("%Y%m%d")
            try:
                caps = self.krx.stock_caps(d, index_id)
            except Exception as exc:                  # noqa: BLE001
                self.log(f"  ⚠ KRX {d} 조회 실패: {exc}")
                continue
            if caps:
                return d, caps
        raise NoInputDataError(f"{index_id}: 최근 14일 KRX 일별매매정보가 없다")

    def fetch(self, index_id: str, token: str = None, as_of: date = None):
        index_id = index_id.upper()
        label = MEMBER_LABEL.get(index_id)
        if label is None:
            raise ConfigError(f"알 수 없는 index_id: {index_id} (가능: {sorted(MEMBER_LABEL)})")
        as_of = as_of or date.today()
        bas_dd, caps = self.latest_caps(index_id, as_of)
        top = int(self.top or DEFAULT_TOP[index_id])
        ranked = sorted(caps, key=lambda c: -caps[c])[:top]
        previous = set((self.previous or {}).get(index_id) or ())
        candidates = sorted(set(ranked) | previous)

        members, labels, failed = [], {}, []
        for i, code in enumerate(candidates, 1):
            res = self.client.get(_PATH, {"FID_COND_MRKT_DIV_CODE": "J",
                                          "FID_INPUT_ISCD": code}, _TR)
            out = (res or {}).get("output") or {}
            if res is None:
                failed.append(code)
                continue
            name = out.get("rprs_mrkt_kor_name")
            labels[name] = labels.get(name, 0) + 1
            if name == label:
                members.append(code)
            if i % 100 == 0:
                self.log(f"  {index_id} 후보 {i}/{len(candidates)} — 구성 {len(members)}")
        if failed:
            # 받지 못한 후보가 있으면 명단이 모자랄 수 있다 — 저장하지 않게 알린다
            raise NoInputDataError(
                f"{index_id}: 후보 {len(failed)}종목을 KIS에서 받지 못했다"
                f"({', '.join(failed[:5])}) — {self.client.last_error}")
        if not members:
            raise NoInputDataError(f"{index_id} 구성종목이 비어 있다")
        # 시총 상위 후보 끝까지 구성종목이 나왔으면 후보가 모자랐을 수 있다
        tail = [c for c in ranked[-20:] if c in members]
        check = self.krx.checksum(members, bas_dd, market=index_id)
        return {
            "index_id": index_id,
            "codes": sorted(members),
            "pages": len(candidates),
            "expected": EXPECTED[index_id],
            "source": self.SOURCE,
            "candidates": len(candidates),
            "candidate_basis": f"KRX {bas_dd} 시총 상위 {top} + 직전 명단 {len(previous)}",
            "labels": labels,
            "members_in_candidate_tail": tail,
            "checksum": check,
        }

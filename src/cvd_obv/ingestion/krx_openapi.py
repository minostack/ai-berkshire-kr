# -*- coding: utf-8 -*-
"""KRX Open API 어댑터 — 로드맵 Stage 3.

왜 이 API가 Stage 3의 열쇠인가
------------------------------
KRX Open API는 **구성종목을 주지 않는다**(서비스 목록 전체 확인, 2026-09-18).
그런데 지수의 상장시가총액을 준다. 그리고 실측 결과

    코스피 200 MKTCAP == 구성종목 상장시가총액의 단순 합

이 **오차 0원**으로 성립한다(2026-09-17, 오늘 201종목 기준). 섹터지수 11종의
합도 지수 전체와 0원으로 맞고, 이 가산 구조는 데이터 시작일인 2010-01-04까지
그대로 유지된다.

그래서 이 어댑터는 시세 수집기가 아니라 **검산기**다. 구성종목 명단을 공지에서
받아 오든 역산하든, 그 명단이 그날 맞았는지 매 거래일 증명할 수 있다. 명단을
"믿는" 대신 "확인하는" 경로가 생긴다.

실측으로 확인한 제약 (2026-09-18, **2026-09-21 갱신**)
---------------------------------------------------
- 데이터 시작일은 **2010-01-04**다. 2009-12-30은 빈 배열을 준다.
- 인증키는 서비스별로 열린다. 401은 경로가 틀린 것이 아니라 그 서비스가 키에
  열려 있지 않다는 뜻이다(구독 추가로 200이 되는 것을 2026-09-21에 확인했다).
- 날짜 파라미터는 `basDd` 하나뿐이다. 구간 조회가 없으므로 거래일마다 한 번씩
  부른다. 그래서 캐시가 선택이 아니라 필수다.

구독 상태 실측 (2026-09-21, basDd=20260918)
-------------------------------------------
=========================  ====  ==================================
서비스                     결과  비고
=========================  ====  ==================================
``idx/kospi_dd_trd``       200   지수 54종
``idx/kosdaq_dd_trd``      200   지수 40종 — **2026-09-21 구독 추가**
``idx/krx_dd_trd``         200   지수 40종 — **2026-09-21 구독 추가**
``sto/stk_bydd_trd``       200   942종목
``sto/ksq_bydd_trd``       200   1,820종목
``idx/bon_dd_trd``         401   채권 지수
``idx/drvprod_dd_trd``     401   파생 지수
``sto/knx_bydd_trd``       401   코넥스
=========================  ====  ==================================

코스닥은 코스피와 성질이 다르다 (2026-09-21 실측)
-------------------------------------------------
구독이 열렸다고 코스피200과 똑같이 쓸 수 있는 것은 아니다. 세 가지가 다르다.

1. **전체 가산 항등식은 성립한다.** ``코스닥 (외국주포함)`` MKTCAP이
   ``sto/ksq_bydd_trd`` 전 종목 합과 **0원**으로 맞는다. 즉 명단을 검산하는
   1차 경로는 코스피200과 동일하게 쓸 수 있다.
2. **섹터 분해는 성립하지 않는다.** 코스피200은 섹터 11종 합이 지수와 0원으로
   맞지만(2026-09-18 재확인), 코스닥150은 섹터 7종 합이 지수보다
   **3.19조원(1.2%) 작다.** 다건 변경을 섹터별로 쪼개 좁히는 Stage 3의 보조
   수단은 **코스닥150에서 쓸 수 없다.**
3. **이력이 짧다.** 코스닥150 지수는 **2015-07-13**에 처음 나온다(그 전날까지
   없음). 섹터 7종은 2016-01-04에 없고 2018-01-02에 있다. 코스피200 패널이
   2010년부터인 것과 달리 코스닥150 재현 패널은 **2015-07-13부터 약 11.2년**
   이다. 재현 표본을 16년으로 계산하면 안 된다.
"""

from __future__ import annotations

import gzip
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import date, timedelta

from ..domain.errors import ConfigError, ExternalApiError
from ..domain.status import DetailCode

__all__ = ["KRX_OPENAPI_SOURCE", "DATA_START", "KOSPI200_INDEX",
           "KOSPI200_SECTORS", "KOSDAQ150_INDEX", "KOSDAQ150_SECTORS",
           "KOSDAQ150_START", "Market", "MARKETS", "market_for",
           "KrxOpenApi", "load_auth_key"]

KRX_OPENAPI_SOURCE = "KRX_OPENAPI"
_BASE_URL = "https://data-dbg.krx.co.kr/svc/apis/"

#: 실측: 2010-01-04부터 응답이 있고 2009-12-30은 빈 배열이다.
DATA_START = date(2010, 1, 4)

INDEX_DAILY = "idx/kospi_dd_trd"
STOCK_DAILY = "sto/stk_bydd_trd"

KOSPI200_INDEX = "코스피 200"

#: 코스피200을 빈틈없이 나누는 섹터지수. 종목 수가 아니라 **시총 합**이 지수
#: 전체와 0원으로 맞는다(2010-01-04부터 확인). 다건 변경을 섹터별로 쪼개
#: 좁히는 데 쓸 수 있다. 섹터 개수는 시기마다 다르다(2010년 8종 → 2020년 11종).
KOSPI200_SECTORS = (
    "코스피 200 커뮤니케이션서비스", "코스피 200 건설", "코스피 200 중공업",
    "코스피 200 철강/소재", "코스피 200 에너지/화학", "코스피 200 정보기술",
    "코스피 200 금융", "코스피 200 생활소비재", "코스피 200 경기소비재",
    "코스피 200 산업재", "코스피 200 헬스케어",
)

KOSDAQ_INDEX_DAILY = "idx/kosdaq_dd_trd"
KOSDAQ_STOCK_DAILY = "sto/ksq_bydd_trd"

KOSDAQ150_INDEX = "코스닥 150"

#: 코스닥150 지수가 처음 나온 날(실측 2026-09-21). 2015-07-10은 없고
#: 2015-07-13은 있다. 재현 패널의 시작일이므로 계산에 그대로 쓴다.
KOSDAQ150_START = date(2015, 7, 13)

#: 코스닥150 섹터지수 7종. **코스피200과 달리 합이 지수와 맞지 않는다** —
#: 2026-09-18 기준 3.19조원(1.2%) 부족하다. 그래서 `sectors_additive=False`이고
#: 섹터를 이용한 변경 좁히기는 이 지수에서 쓰지 않는다. 목록을 남기는 것은
#: 쓰기 위해서가 아니라 **왜 못 쓰는지 확인할 수 있게** 하기 위해서다.
KOSDAQ150_SECTORS = (
    "코스닥 150 정보기술", "코스닥 150 헬스케어",
    "코스닥 150 커뮤니케이션서비스", "코스닥 150 소재",
    "코스닥 150 산업재", "코스닥 150 필수소비재", "코스닥 150 자유소비재",
)


class Market:
    """지수 하나를 검산하는 데 필요한 것들.

    코스피200과 코스닥150은 **서비스 경로도, 이력 시작일도, 섹터 가산성도**
    다르다. 그 차이를 호출부가 매번 기억하는 대신 여기 한 곳에 모은다.
    """

    __slots__ = ("key", "index_service", "stock_service", "index_name",
                 "sectors", "sectors_additive", "start")

    def __init__(self, key, index_service, stock_service, index_name,
                 sectors, sectors_additive, start):
        self.key = key
        self.index_service = index_service
        self.stock_service = stock_service
        self.index_name = index_name
        self.sectors = sectors
        self.sectors_additive = sectors_additive
        self.start = start

    def __repr__(self):                                   # pragma: no cover
        return f"<Market {self.key} {self.index_name} start={self.start}>"


MARKETS = {
    "KOSPI200": Market(
        "KOSPI200", INDEX_DAILY, STOCK_DAILY, KOSPI200_INDEX,
        KOSPI200_SECTORS, True, DATA_START),
    "KOSDAQ150": Market(
        "KOSDAQ150", KOSDAQ_INDEX_DAILY, KOSDAQ_STOCK_DAILY, KOSDAQ150_INDEX,
        KOSDAQ150_SECTORS, False, KOSDAQ150_START),
}


def market_for(key):
    """`"KOSPI200"` 같은 이름이나 `Market` 자체를 받아 `Market`으로."""
    if isinstance(key, Market):
        return key
    try:
        return MARKETS[str(key).upper()]
    except KeyError:
        raise ConfigError(
            f"모르는 지수다: {key} (가능: {', '.join(sorted(MARKETS))})",
            detail_code=DetailCode.CONFIG_INVALID) from None


def project_root() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", "..", ".."))


def load_auth_key(env_path: str = None) -> str:
    """OS 환경변수 우선, 없으면 `.env`에서 읽는다.

    키를 인자로 받아 넘기는 경로를 만들지 않는다. 로그·매니페스트에 섞여
    들어갈 여지를 줄이기 위해서다.
    """
    key = os.environ.get("KRX_AUTH_KEY", "").strip()
    if key:
        return key
    path = env_path or os.path.join(project_root(), ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("KRX_AUTH_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise ConfigError("KRX_AUTH_KEY가 없다 — .env에 넣거나 환경변수로 설정하라",
                      detail_code=DetailCode.CONFIG_INVALID)


class KrxOpenApi:
    """`basDd` 단위 조회 + 로컬 캐시.

    같은 날짜를 두 번 받지 않는다. 과거 데이터는 확정값이라 캐시가 늙지 않는다.
    """

    def __init__(self, cache_dir: str = None, key: str = None,
                 min_interval_sec: float = 0.0):
        self._key = key or load_auth_key()
        self.cache_dir = cache_dir or os.path.join(
            project_root(), "data", "bronze", "krx_openapi")
        self.min_interval = float(min_interval_sec)
        self._lock = threading.Lock()
        self._last_call = 0.0
        self.calls = 0
        self.cache_hits = 0

    # ── 캐시 ──────────────────────────────────────────────────────
    def _cache_path(self, service: str, bas_dd: str) -> str:
        return os.path.join(self.cache_dir, service.replace("/", "_"),
                            bas_dd[:4], f"{bas_dd}.json.gz")

    def cached(self, service: str, bas_dd: str) -> bool:
        return os.path.exists(self._cache_path(service, bas_dd))

    # ── 호출 ──────────────────────────────────────────────────────
    def fetch(self, service: str, bas_dd: str, retries: int = 3):
        """한 거래일의 원본 행들. 빈 배열이면 비거래일이거나 제공 구간 밖이다."""
        path = self._cache_path(service, bas_dd)
        if os.path.exists(path):
            self.cache_hits += 1
            with gzip.open(path, "rt", encoding="utf-8") as f:
                return json.load(f)

        last = None
        for attempt in range(retries):
            try:
                rows = self._call(service, bas_dd)
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    raise ExternalApiError(
                        f"{service}는 이 인증키에 열려 있지 않다(401)",
                        detail_code=DetailCode.AUTH_FAILED) from exc
                last = exc
            except Exception as exc:                      # noqa: BLE001
                last = exc
            else:
                # 빈 응답은 "비거래일"과 "아직 안 올라옴"을 구별하지 못한다.
                # 과거 날짜면 확정이라 캐시해도 되지만 **오늘 이후**는 아니다.
                # 캐시하면 "오늘은 비거래일"이 영구히 굳는다.
                # 당일 오후에 받은 빈 응답이 며칠 뒤 공표분을 가린 적이 있다
                # (2026-09-18 코스피 지수·종목) — 최근 3일은 캐시하지 않는다.
                recent = (date.today() - timedelta(days=3)).strftime("%Y%m%d")
                if not rows and bas_dd >= recent:
                    return rows
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = path + ".tmp"
                with gzip.open(tmp, "wt", encoding="utf-8") as f:
                    json.dump(rows, f, ensure_ascii=False)
                os.replace(tmp, path)
                return rows
            time.sleep(1.5 * (attempt + 1))
        raise ExternalApiError(f"{service} {bas_dd} 수집 실패: {last}",
                               detail_code=DetailCode.API_NO_RESPONSE)

    def _call(self, service: str, bas_dd: str):
        if self.min_interval:
            with self._lock:
                wait = self.min_interval - (time.time() - self._last_call)
                if wait > 0:
                    time.sleep(wait)
                self._last_call = time.time()
        req = urllib.request.Request(f"{_BASE_URL}{service}?basDd={bas_dd}",
                                     headers={"AUTH_KEY": self._key})
        with urllib.request.urlopen(req, timeout=90) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        self.calls += 1
        return payload.get("OutBlock_1", [])

    # ── 파생 ──────────────────────────────────────────────────────
    def index_caps(self, bas_dd: str, market="KOSPI200"):
        """{지수명: 상장시가총액}. 값이 빈 지수(수익지수 등)는 뺀다."""
        svc = market_for(market).index_service
        return {r["IDX_NM"].strip(): int(r["MKTCAP"])
                for r in self.fetch(svc, bas_dd) if r.get("MKTCAP")}

    def stock_caps(self, bas_dd: str, market="KOSPI200"):
        """{종목코드: 상장시가총액} — 그 시장 전 종목."""
        svc = market_for(market).stock_service
        return {r["ISU_CD"]: int(r["MKTCAP"])
                for r in self.fetch(svc, bas_dd) if r.get("MKTCAP")}

    def stock_names(self, bas_dd: str, market="KOSPI200"):
        svc = market_for(market).stock_service
        return {r["ISU_CD"]: r["ISU_NM"] for r in self.fetch(svc, bas_dd)}

    def checksum(self, members, bas_dd: str, index_name: str = None,
                 market="KOSPI200"):
        """명단이 그날 맞았는지 검산한다.

        `diff == 0`이면 명단이 맞다. 아니면 그날 구성종목이 달랐다는 뜻이고,
        `diff`의 크기가 바뀐 종목들의 시총 차이다.

        `index_name`을 주면 그 지수로, 안 주면 `market`의 대표지수로 검산한다.
        """
        mk = market_for(market)
        name = index_name or mk.index_name
        caps = self.stock_caps(bas_dd, mk)
        target = self.index_caps(bas_dd, mk).get(name)
        listed = [c for c in members if c in caps]
        ours = sum(caps[c] for c in listed)
        return {
            "bas_dd": bas_dd,
            "market": mk.key,
            "index_name": name,
            "target": target,
            "ours": ours,
            "diff": None if target is None else target - ours,
            "members": len(members),
            "listed": len(listed),
            "not_listed": sorted(set(members) - set(listed)),
        }

    def sector_caps(self, bas_dd: str, market="KOSPI200"):
        """그날 존재하는 섹터지수만 돌려준다.

        **가산성이 확인된 지수에서만 쓴다.** 코스닥150은 섹터 합이 지수와 맞지
        않으므로(2026-09-21 실측, 1.2% 부족) 여기서 거부한다. 조용히 틀린 값을
        돌려주느니 부르는 쪽을 멈추는 편이 낫다.
        """
        mk = market_for(market)
        if not mk.sectors_additive:
            raise ConfigError(
                f"{mk.key} 섹터지수는 합이 지수와 맞지 않는다 — 검산에 쓸 수 없다",
                detail_code=DetailCode.CONFIG_INVALID)
        caps = self.index_caps(bas_dd, mk)
        return {name: caps[name] for name in mk.sectors if name in caps}

    def sector_gap(self, bas_dd: str, market="KOSPI200"):
        """섹터 합과 지수의 차이. 가산성을 **주장하지 않고 재는** 경로다.

        `sector_caps`가 거부하는 지수에서도 부를 수 있다. 가산성이 정말
        깨졌는지 날짜별로 확인할 때 쓴다.
        """
        mk = market_for(market)
        caps = self.index_caps(bas_dd, mk)
        present = {name: caps[name] for name in mk.sectors if name in caps}
        total = caps.get(mk.index_name)
        ours = sum(present.values())
        return {
            "bas_dd": bas_dd,
            "market": mk.key,
            "index_name": mk.index_name,
            "sectors_present": len(present),
            "sectors_expected": len(mk.sectors),
            "index_mktcap": total,
            "sector_sum": ours,
            "diff": None if total is None else total - ours,
            "declared_additive": mk.sectors_additive,
        }

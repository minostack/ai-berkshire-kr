# -*- coding: utf-8 -*-
"""KIS 업종(지수) 일봉 수집 — `FHKUP03500100`, 2026-10-23부터 `silver.benchmark_bars` 원천.

키움 `ka20006`(`kiwoom_benchmark`)과 같은 인터페이스(`token`·`fetch_benchmark`)다.

실측(2026-10-23)
----------------
- 업종코드: 코스피 `0001`, 코스닥 `1001`, 코스피200 `2001`(`FID_COND_MRKT_DIV_CODE=U`).
  2024-04-02 종가 2,753.16 / 891.59 / 376.34 — 키움 기준값(anchor)과 같다
- 값은 실제 지수값 그대로 온다(키움은 ×100 정수)
- 2026-10-01~10-21 세 지수 42일: 시가·고가·저가·종가 **42/42 일치**, 거래량 41/42
  (KOSDAQ 10/19 448,135 vs 키움 448,134 — 1단위 차이)
- 한 번에 최대 **50행**. 날짜 구간을 뒤로 옮기며 받는다. 1990년대 행도 있다
- 가격지수다 — 배당 재투자 미반영(키움과 같다)
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from .kis_client import KisClient
from .kiwoom_benchmark import BENCHMARKS, KiwoomBenchmarkSource
from .kiwoom_daily import DailyFetchResult
from ..domain.errors import ExternalApiError
from ..domain.status import DetailCode, QualityStatus

__all__ = ["KIS_BENCHMARK_SOURCE", "KisBenchmarkSource"]

KIS_BENCHMARK_SOURCE = "KIS_FHKUP03500100"
_TR = "FHKUP03500100"
_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice"
PAGE_MAX = 50
#: 1페이지 50행 — 40년치(약 1만 행)를 덮는다
_DEFAULT_MAX_PAGES = 250
_EARLIEST = date(1980, 1, 1)


def _dec(value):
    s = str(value if value is not None else "").replace(",", "").strip()
    if not s:
        return None
    try:
        return abs(Decimal(s))
    except (InvalidOperation, ValueError):
        return None


class KisBenchmarkSource:
    """`FHKUP03500100` 지수 일봉 조회."""

    SOURCE = KIS_BENCHMARK_SOURCE
    CODE_KEY = "kis_cd"

    def __init__(self, client: KisClient = None, max_pages: int = _DEFAULT_MAX_PAGES):
        self.client = client or KisClient()
        self.max_pages = int(max_pages)

    def token(self):
        return self.client.token()

    def fetch_benchmark(self, benchmark_id: str, token: str = None,
                        start: date = None, end: date = None,
                        max_pages: int = None) -> DailyFetchResult:
        spec = BENCHMARKS.get(benchmark_id)
        if spec is None:
            raise ExternalApiError(
                f"알 수 없는 benchmark_id: {benchmark_id} (가능: {sorted(BENCHMARKS)})")
        end = end or date.today()
        lo = start or _EARLIEST
        cap = int(max_pages or self.max_pages)
        requested = {"kis_cd": spec["kis_cd"], "start": lo.isoformat(),
                     "end": end.isoformat(), "max_pages": cap}

        by_date, flags = {}, set()
        pages = rows_received = 0
        stop_reason = "END_OF_DATA"
        cur_end = end
        for _page in range(cap):
            if cur_end < lo:
                stop_reason = "REACHED_START"
                break
            res = self.client.get(_PATH, {
                "FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": spec["kis_cd"],
                "FID_INPUT_DATE_1": lo.strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": cur_end.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": "D"}, _TR)
            pages += 1
            if res is None:
                return DailyFetchResult(
                    benchmark_id, self._sorted(by_date), pages, rows_received, False,
                    QualityStatus.FAILED, DetailCode.API_NO_RESPONSE,
                    f"API 응답 없음(재시도 후에도 실패): {self.client.last_error}",
                    "NO_RESPONSE", flags, requested)
            raw = [r for r in res.get("output2") or [] if r.get("stck_bsop_date")]
            if not raw:
                stop_reason = "EMPTY_PAGE"
                break
            rows_received += len(raw)
            oldest = None
            for item in raw:
                row = self._normalize(item, flags)
                if row is None:
                    continue
                d = row["trading_date"]
                oldest = d if oldest is None or d < oldest else oldest
                if lo <= d <= end:
                    by_date[row["dt"]] = row
            if len(raw) < PAGE_MAX or oldest is None:
                stop_reason = "NO_MORE_PAGES"
                break
            nxt = oldest - timedelta(days=1)
            if nxt >= cur_end:
                flags.add("PAGINATION_CURSOR_REPEAT")
                stop_reason = "CURSOR_REPEAT"
                break
            cur_end = nxt
        else:
            stop_reason = "PAGE_CAP"

        rows = self._sorted(by_date)
        # 업종코드가 바뀌지 않았는지 같은 기준일 값으로 검산한다(키움과 같은 규칙)
        KiwoomBenchmarkSource._check_anchor(spec, rows, flags)
        complete = stop_reason in ("REACHED_START", "NO_MORE_PAGES", "END_OF_DATA",
                                   "EMPTY_PAGE")
        if stop_reason in ("PAGE_CAP", "CURSOR_REPEAT"):
            status, code = QualityStatus.PARTIAL, DetailCode.API_PARTIAL_PAGE
            message = f"페이지네이션이 {stop_reason}로 중단됐다"
        elif not rows:
            status, code, message = (QualityStatus.FAILED, DetailCode.MISSING_BARS,
                                     "수집된 지수 일봉이 없다")
        else:
            status, code, message = QualityStatus.SUCCESS, None, None
        return DailyFetchResult(benchmark_id, rows, pages, rows_received, complete,
                                status, code, message, stop_reason, flags, requested)

    @staticmethod
    def _sorted(by_date):
        return [by_date[k] for k in sorted(by_date)]

    @staticmethod
    def _normalize(item, flags):
        dt = str(item.get("stck_bsop_date") or "").strip()
        if len(dt) != 8 or not dt.isdigit():
            flags.add("BAD_DATE_FIELD")
            return None
        close, open_ = _dec(item.get("bstp_nmix_prpr")), _dec(item.get("bstp_nmix_oprc"))
        high, low = _dec(item.get("bstp_nmix_hgpr")), _dec(item.get("bstp_nmix_lwpr"))
        if None in (close, open_, high, low):
            flags.add("INCOMPLETE_ROW")
            return None
        if min(open_, high, low, close) <= 0:
            flags.add("NON_POSITIVE_VALUE")
            return None
        vol = _dec(item.get("acml_vol"))
        return {"dt": dt, "trading_date": date(int(dt[:4]), int(dt[4:6]), int(dt[6:8])),
                "open": open_, "high": high, "low": low, "close": close,
                "volume": int(vol) if vol is not None else None,
                "quality_flags": []}

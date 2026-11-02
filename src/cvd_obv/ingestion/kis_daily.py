# -*- coding: utf-8 -*-
"""KIS 일봉 수집 어댑터 — `FHKST03010100`(국내주식기간별시세), 2026-10-23부터 적재 원천.

키움 `ka10081`(`kiwoom_daily`)과 같은 인터페이스(`fetch_security`·`token`·
`adjustment_type`)로 같은 모양의 행을 돌려준다.

실측(2026-10-23, KRX `J`, 수정주가 `FID_ORG_ADJ_PRC=0`)
-----------------------------------------------------
- 2026-09-11 이전: 키움 일봉과 시가·고가·저가·종가·거래량·거래대금이 같다
  (005930·000660 2010·2020·2025년 전 거래일). 수정주가 종목은 원 단위 반올림이
  다를 수 있다(035720 2020-01-02 30,609 vs 키움 30,607)
- **2026-09-14 이후 정의가 키움과 다르다**: 가격은 **정규장**(종가 = 15:30 종가,
  고가·저가에 애프터마켓 미포함)이고 거래량·거래대금은 **애프터마켓까지 포함**
  (키움 EXTENDED와 같은 값). 005930 10/20: 종가 211,500(키움 EXTENDED 209,000),
  거래량 16,561,224(같음)
  → `price_scope`는 거래량 기준으로 키움과 같이 `EXTENDED`로 저장하고
  `KIS_PRICE_REGULAR_SESSION` 표시를 단다. 정규장 확정 봉은 여전히
  `regular_from_minute`가 만든다(거래량 = 이 값 − 애프터마켓 1분봉)
- **단, 가장 최근 거래일은 다음 거래일 전까지 가격이 애프터마켓 최종가·고저다**
  (`adjustment_watch` 머리말, 2026-10-26·11-01 실측). 053800 10/30을 10/31(토)·11/1(일)에
  받으면 종가 121,100·고가 127,400(= 애프터마켓 19:59 종가·19:15 고가), 15:30 종가 단일가는
  118,100. 10/23 행은 10/24(토) 수집 213,500 → 10/26(월) 08:34 수집 215,500(정규장)으로
  바뀌었다(005930). 그래서 그날 뒤 첫 평일 09:00 전에 받은 행(`adjustment_watch.settled`가
  거짓)에는 `KIS_PRICE_REGULAR_SESSION` 대신 `KIS_LAST_DAY_UNSETTLED`를 단다. 일일 적재가
  매번 일봉을 며칠 앞부터 다시 받으므로 다음 적재가 정산된 값으로 덮는다
  (`docs/KIS-일봉-최근일-애프터마켓가격-20261101.md`)
- 거래대금 `acml_tr_pbmn`은 **원** 단위(키움은 백만원)
- 한 번에 최대 100행. 날짜 구간을 뒤로 옮기며 받는다. 005930은 1980년대까지 있다
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from .adjustment_watch import settled
from .kis_client import KisClient
from .kiwoom_daily import DailyFetchResult
from ..domain.status import DetailCode, QualityStatus

__all__ = ["KIS_DAILY_SOURCE", "KisDailySource", "FLAG_KIS_REGULAR_PRICE",
           "FLAG_KIS_UNSETTLED"]

KIS_DAILY_SOURCE = "KIS_FHKST03010100"
_TR = "FHKST03010100"
_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
PAGE_MAX = 100
#: 1페이지 100행 — 40년치(약 1만 행)를 덮는다
_DEFAULT_MAX_PAGES = 130
#: 이력 시작(요청 하한). 이보다 오래된 행은 없다
_EARLIEST = date(1980, 1, 1)
#: 2026-09-14 이후 KIS 일봉은 가격이 정규장, 거래량이 애프터마켓 포함이다
FLAG_KIS_REGULAR_PRICE = "KIS_PRICE_REGULAR_SESSION"
#: 그날 뒤 첫 평일 09:00 전에 받은 행 — 가격이 애프터마켓 최종가·고저일 수 있다
FLAG_KIS_UNSETTLED = "KIS_LAST_DAY_UNSETTLED"
_AFTERMARKET_START = date(2026, 9, 14)

#: 거래대금과 거래량×가격이 이 배수 밖으로 어긋나면 표시한다(키움 어댑터와 같은 기준)
_TRADED_VALUE_TOLERANCE = (Decimal("0.5"), Decimal("2.0"))


def _dec(value):
    s = str(value if value is not None else "").replace(",", "").strip()
    if not s:
        return None
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


class KisDailySource:
    """`FHKST03010100` 일봉 조회."""

    SOURCE = KIS_DAILY_SOURCE

    def __init__(self, client: KisClient = None, max_pages: int = _DEFAULT_MAX_PAGES,
                 adjusted: bool = True, market_div: str = "J", clock=None):
        self.client = client or KisClient()
        self.max_pages = int(max_pages or _DEFAULT_MAX_PAGES)
        self.adjusted = bool(adjusted)
        self.market_div = market_div
        #: 수집 시각(미정산 판정) — 테스트에서 바꿔 끼운다
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def token(self):
        return self.client.token()

    @property
    def adjustment_type(self) -> str:
        return "SPLIT_ADJUSTED" if self.adjusted else "RAW"

    def fetch_security(self, code: str, token: str = None, start: date = None,
                       end: date = None, max_pages: int = None) -> DailyFetchResult:
        """한 종목의 [start, end] 일봉(날짜 오름차순). `start=None`이면 주는 데까지."""
        end = end or date.today()
        lo = start or _EARLIEST
        cap = int(max_pages or self.max_pages)
        requested = {"market_div": self.market_div, "start": lo.isoformat(),
                     "end": end.isoformat(), "adjusted": self.adjusted,
                     "max_pages": cap}
        by_date, flags = {}, set()
        pages = rows_received = 0
        stop_reason = "END_OF_DATA"
        cur_end = end
        fetched_at = self.clock()
        for _page in range(cap):
            if cur_end < lo:
                stop_reason = "REACHED_START"
                break
            res = self.client.get(_PATH, {
                "FID_COND_MRKT_DIV_CODE": self.market_div,
                "FID_INPUT_ISCD": str(code).split(":")[-1],
                "FID_INPUT_DATE_1": lo.strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": cur_end.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": "D",
                "FID_ORG_ADJ_PRC": "0" if self.adjusted else "1"}, _TR)
            pages += 1
            if res is None:
                return DailyFetchResult(
                    code, self._sorted(by_date), pages, rows_received, False,
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
                row = self._normalize(item, flags, fetched_at)
                if row is None:
                    continue
                d = row["trading_date"]
                oldest = d if oldest is None or d < oldest else oldest
                if lo <= d <= end:
                    by_date[d] = row
                elif d > end:
                    flags.add("BAR_AFTER_END")
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
        complete = stop_reason in ("REACHED_START", "NO_MORE_PAGES", "END_OF_DATA",
                                   "EMPTY_PAGE")
        if stop_reason == "PAGE_CAP":
            status, error = QualityStatus.PARTIAL, DetailCode.API_PARTIAL_PAGE
            message = f"페이지 상한 {cap}에 걸렸다 — 더 과거 데이터가 남아 있다"
        elif stop_reason == "CURSOR_REPEAT":
            status, error = QualityStatus.PARTIAL, DetailCode.API_PARTIAL_PAGE
            message = "같은 구간이 반복돼 중단했다"
        elif not rows:
            status, error, message = (QualityStatus.FAILED, DetailCode.MISSING_BARS,
                                      "수집된 일봉이 없다")
        else:
            status, error, message = QualityStatus.SUCCESS, None, None
        return DailyFetchResult(code, rows, pages, rows_received, complete, status,
                                error, message, stop_reason, flags, requested)

    @staticmethod
    def _sorted(by_date):
        return [by_date[k] for k in sorted(by_date)]

    @staticmethod
    def _normalize(item, flags, fetched_at=None):
        dt = str(item.get("stck_bsop_date") or "").strip()
        if len(dt) != 8 or not dt.isdigit():
            flags.add("BAD_DATE_FIELD")
            return None
        open_, high = _dec(item.get("stck_oprc")), _dec(item.get("stck_hgpr"))
        low, close = _dec(item.get("stck_lwpr")), _dec(item.get("stck_clpr"))
        volume, value = _dec(item.get("acml_vol")), _dec(item.get("acml_tr_pbmn"))
        if None in (open_, high, low, close) or volume is None:
            flags.add("INCOMPLETE_ROW")
            return None
        if min(open_, high, low, close) <= 0:
            # 거래 없는 날(거래정지 등)은 가격이 0으로 온다 — 키움 어댑터처럼 버린다
            flags.add("NON_POSITIVE_PRICE")
            return None
        day = date(int(dt[:4]), int(dt[4:6]), int(dt[6:8]))
        row_flags = []
        if day >= _AFTERMARKET_START:
            # 다음 거래일 전에 받은 최근 거래일 행은 가격이 애프터마켓 값일 수 있다
            row_flags.append(FLAG_KIS_REGULAR_PRICE if settled(day, fetched_at)
                             else FLAG_KIS_UNSETTLED)
        if value is not None and volume > 0:
            approx = volume * (high + low + close) / 3
            lo, hi = _TRADED_VALUE_TOLERANCE
            if approx > 0 and not (lo <= value / approx <= hi):
                row_flags.append("TRADED_VALUE_INCONSISTENT")
        return {"dt": dt, "trading_date": day,
                "open": open_, "high": high, "low": low, "close": close,
                "volume": int(volume), "traded_value": value,
                "quality_flags": row_flags}

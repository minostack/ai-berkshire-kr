# -*- coding: utf-8 -*-
"""KIS 1분봉 수집 어댑터 — `FHKST03010230`(주식일별분봉조회), 2026-10-23부터 적재 원천.

키움 `ka10080`(`kiwoom_minute`)과 **같은 인터페이스**(`fetch_security`·`token`·
`scanner`)로 같은 모양의 봉을 돌려준다. 저장·커버리지·catchup 로직은 그대로다.

실측(2026-10-23)
----------------
- 005930 2026-10-22 하루: KRX(J) 621봉, NXT(NX) 690봉이 키움 1분봉과 **봉 단위로
  시가·고가·저가·종가·거래량 모두 일치**. 시각은 봉 시작(`stck_cntg_hour`)
- 한 번에 120봉. `FID_PW_DATA_INCU_YN=Y`면 날짜를 넘어 이전 거래일로 이어진다
  → 최신에서 과거로 커서(날짜, 시각)를 옮기며 페이지를 넘긴다
- 시작 커서는 (날짜, `235959`). 휴장일 날짜에 다른 시각을 주면 직전 거래일의 **그
  시각부터** 주므로(일요일 12:00 → 금요일 12:00) 그 뒤 봉을 놓친다
- 과거 범위 약 1년(2026-10-23 기준 2025-10-15 09:00부터)
- 수정주가가 적용돼 온다(207940 2026-09-30 종가 1,380,293 — 10/02 이벤트 반영)
- J는 09:00~19:59(애프터마켓 포함), NX는 08:00~19:59
- **키움에 있던 15:35 봉(장후 시간외 종가 체결)이 없다.** 잠정 일봉 거래량에서
  시간외 종가 몫이 빠진다(005930 10/22: 10,972주, 약 0.1%). 확정 봉 거래량은
  일봉 거래량 − 애프터마켓 분봉이라 영향이 없다

누적거래대금
------------
응답의 `acml_tr_pbmn`(그날 그 시장의 누적 거래대금, 원)을 `cum_traded_value`로
저장한다. 봉 거래대금 `traded_value`는 같은 날 직전 봉과의 차이로 채운다(그날 첫
봉은 누적값 그대로). 그날 첫 봉을 받았는지 모르면(수집 지평 끝의 날) 첫 봉은 비운다.
KRX 첫 봉 누적에는 장전 시간외(08:30~08:40) 체결이 들어 있을 수 있다 — 확인하지 않았다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from .kis_client import KisClient
from .kiwoom_minute import MinuteFetchResult
from ..baseline.scanner_v15 import load_scanner_v15
from ..domain.errors import ConfigError
from ..domain.ids import KST
from ..domain.status import DetailCode, QualityStatus

__all__ = ["KIS_MINUTE_SOURCE", "KisMinuteSource", "kis_market_code"]

KIS_MINUTE_SOURCE = "KIS_FHKST03010230"
_TR = "FHKST03010230"
_PATH = "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"

#: 봉 타임스탬프 의미 — 2026-10-23 실측: 키움 봉 시작 시각과 같다
TIMESTAMP_SEMANTICS = "BAR_START"
#: 한 번 응답 봉 수
ROWS_PER_PAGE = 120
#: 하루 J 약 631봉·NX 약 691봉 = 6페이지. 1년(약 250일)을 덮도록 잡는다
_DEFAULT_MAX_PAGES = 1600
FLAG_NO_FIRST_VALUE = "KIS_FIRST_BAR_VALUE_UNKNOWN"
FLAG_CUM_DECREASED = "KIS_CUM_VALUE_DECREASED"


def kis_market_code(code):
    """수집 대상 → (KIS 시장 구분, 종목코드). `NXT:005930` → ("NX", "005930")."""
    s = str(code).strip().upper()
    if s.startswith("NXT:"):
        return "NX", s[4:]
    if s.startswith("KRX:"):
        return "J", s[4:]
    return "J", s


def _dec(value):
    s = str(value if value is not None else "").replace(",", "").strip()
    if not s:
        return None
    try:
        return abs(Decimal(s))
    except (InvalidOperation, ValueError):
        return None


def _prev_minute(day: str, hhmmss: str):
    t = datetime.strptime(day + hhmmss, "%Y%m%d%H%M%S") - timedelta(minutes=1)
    return t.strftime("%Y%m%d"), t.strftime("%H%M%S")


class KisMinuteSource:
    """`FHKST03010230` 1분봉 조회 — 최신에서 과거로 주는 데까지 받는다."""

    SOURCE = KIS_MINUTE_SOURCE
    ROWS_PER_PAGE = ROWS_PER_PAGE
    TIMESTAMP_SEMANTICS = TIMESTAMP_SEMANTICS

    def __init__(self, scanner=None, client: KisClient = None,
                 max_pages: int = _DEFAULT_MAX_PAGES, clock=None):
        # 봉 경계·세션 판정은 V1.5 함수를 그대로 쓴다(키움 어댑터와 같은 판정)
        self.scanner = scanner or load_scanner_v15()
        self.client = client or KisClient()
        self.max_pages = int(max_pages)
        self.clock = clock or (lambda: datetime.now(tz=KST))
        self.adjusted = True

    def token(self):
        return self.client.token()

    def fetch_security(self, code: str, token: str = None, period: int = 1,
                       max_pages: int = None, min_date: str = None):
        """한 종목 1분봉을 최신부터 `min_date`(YYYYMMDD)에 닿을 때까지 받는다."""
        if int(period) != 1:
            raise ConfigError(f"KIS 분봉 원천은 1분봉만 준다(period={period})")
        market, iscd = kis_market_code(code)
        cap = int(max_pages or self.max_pages)
        cur_day, cur_hour = self.clock().strftime("%Y%m%d"), "235959"
        requested = {"market_div": market, "code": iscd, "max_pages": cap,
                     "min_date": min_date, "start_cursor": f"{cur_day}{cur_hour}"}

        by_key, flags = {}, set()
        pages = rows_received = 0
        stop_reason = "END_OF_DATA"
        reached_min = False
        for _page in range(cap):
            res = self.client.get(_PATH, {
                "FID_COND_MRKT_DIV_CODE": market, "FID_INPUT_ISCD": iscd,
                "FID_INPUT_HOUR_1": cur_hour, "FID_INPUT_DATE_1": cur_day,
                "FID_PW_DATA_INCU_YN": "Y", "FID_FAKE_TICK_INCU_YN": ""}, _TR)
            pages += 1
            if res is None:
                return MinuteFetchResult(
                    code, 1, self._finish(by_key, False, flags), pages, rows_received,
                    False, QualityStatus.FAILED, DetailCode.API_NO_RESPONSE,
                    f"API 응답 없음(재시도 후에도 실패): {self.client.last_error}",
                    "NO_RESPONSE", flags, requested)
            rows = [r for r in res.get("output2") or [] if r.get("stck_bsop_date")]
            if not rows:
                stop_reason = "EMPTY_PAGE"
                break
            rows_received += len(rows)
            oldest = None
            for item in rows:
                key = (item["stck_bsop_date"], item.get("stck_cntg_hour", ""))
                if oldest is None or key < oldest:
                    oldest = key
                bar = self._normalize(item, flags)
                if bar is None:
                    continue
                if min_date and bar["dt"][:8] < min_date:
                    reached_min = True
                    continue
                by_key[(bar["bar_start"], bar["session"])] = bar
            if reached_min:
                stop_reason = "REACHED_MIN_DATE"
                break
            nxt = _prev_minute(*oldest)
            if nxt >= (cur_day, cur_hour):
                flags.add("PAGINATION_CURSOR_REPEAT")
                stop_reason = "CURSOR_REPEAT"
                break
            cur_day, cur_hour = nxt
        else:
            stop_reason = "PAGE_CAP"

        bars = self._finish(by_key, stop_reason == "REACHED_MIN_DATE", flags)
        complete = stop_reason in ("REACHED_MIN_DATE", "END_OF_DATA", "EMPTY_PAGE")
        if stop_reason in ("PAGE_CAP", "CURSOR_REPEAT"):
            status, error = QualityStatus.PARTIAL, DetailCode.API_PARTIAL_PAGE
            message = f"페이지네이션이 {stop_reason}로 중단됐다"
        elif not bars:
            status, error, message = (QualityStatus.FAILED, DetailCode.MISSING_BARS,
                                      "수집된 분봉이 없다")
        else:
            status, error, message = QualityStatus.SUCCESS, None, None
        return MinuteFetchResult(code, 1, bars, pages, rows_received, complete,
                                 status, error, message, stop_reason, flags, requested)

    def _normalize(self, item, flags):
        sc = self.scanner
        dt = f"{item.get('stck_bsop_date', '')}{item.get('stck_cntg_hour', '')}".strip()
        if len(dt) != 14 or not dt.isdigit():
            flags.add("BAD_TIMESTAMP_FIELD")
            return None
        close, open_ = _dec(item.get("stck_prpr")), _dec(item.get("stck_oprc"))
        high, low = _dec(item.get("stck_hgpr")), _dec(item.get("stck_lwpr"))
        volume = _dec(item.get("cntg_vol"))
        if None in (close, open_, high, low) or volume is None:
            flags.add("INCOMPLETE_ROW")
            return None
        if min(open_, high, low, close) <= 0:
            flags.add("NON_POSITIVE_PRICE")
            return None
        bar_start, bar_end = sc.bar_bounds(dt, 1)
        if bar_start is None or bar_end is None:
            flags.add("BAR_BOUNDS_UNRESOLVED")
            return None
        return {
            "dt": dt, "bar_start": bar_start, "bar_end": bar_end,
            "trading_date": bar_start.date(), "session": sc.session_of(dt),
            "open": open_, "high": high, "low": low, "close": close,
            "volume": int(volume),
            "cum_traded_value": _dec(item.get("acml_tr_pbmn")),
            "traded_value": None,
            "source_timestamp_raw": dt,
        }

    @staticmethod
    def _finish(by_key, oldest_day_complete: bool, flags):
        """정렬하고 봉 거래대금(누적 차이)을 채운다. 문제는 그 봉에만 표시한다."""
        bars = [by_key[k] for k in sorted(by_key)]
        oldest_day = bars[0]["trading_date"] if bars else None
        prev = {}
        for bar in bars:
            day, cum = bar["trading_date"], bar.get("cum_traded_value")
            if cum is None:
                continue
            if day in prev:
                diff = cum - prev[day]
                if diff < 0:
                    bar["quality_flags"] = [FLAG_CUM_DECREASED]
                else:
                    bar["traded_value"] = diff
            elif day != oldest_day or oldest_day_complete:
                bar["traded_value"] = cum
            else:
                # 수집 지평 끝의 날 — 그 앞 봉을 받았는지 모른다
                bar["quality_flags"] = [FLAG_NO_FIRST_VALUE]
            prev[day] = cum
        return bars

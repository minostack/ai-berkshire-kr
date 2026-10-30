# -*- coding: utf-8 -*-
"""키움 ka10080 분봉 수집 어댑터 — 로드맵 Stage 4.

일봉과 결정적으로 다른 점
-------------------------
`ka10081`(일봉)에는 `base_dt`가 있어 과거 어느 시점부터든 받을 수 있다.
`ka10080`(분봉)에는 **날짜 파라미터가 없다.** 최신 시점부터 과거로 페이지를
넘기는 것만 가능하고, 어느 지점에서 끊길지는 서버가 정한다.

그래서 전략이 달라진다. 일봉은 "필요한 구간을 요청"하지만 분봉은 "주는 데까지
한 번에 다 받아 두고 offline에서 잘라 쓴다". 그 잘라 쓰기가
`MinuteHistoryCache.slice_as_of()`이며, Stage 5 replay가 API를 다시 부르지
않는 근거다.

세션은 요청이 아니라 응답으로 정한다
------------------------------------
키움이 애프터마켓 체결을 분봉에 포함하는지는 문서로 확인되지 않았다. 그래서
요청 파라미터로 세션을 고르지 않고, **받은 봉의 타임스탬프로 분류한다**
(V1.5가 택한 방식과 같다). 포함되든 아니든 각 봉이 어느 세션인지 항상 드러난다.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

from .kiwoom_auth import token_with_retry
from ..baseline.scanner_v15 import load_scanner_v15
from ..domain.errors import ExternalApiError
from ..domain.status import DetailCode, QualityStatus

__all__ = ["KIWOOM_MINUTE_SOURCE", "MinuteFetchResult", "KiwoomMinuteSource"]

KIWOOM_MINUTE_SOURCE = "KIWOOM_KA10080"
_API_ID = "ka10080"
_PATH = "/api/dostk/chart"

#: 실측(2026-09-16 V1.5 주석): 1회 응답이 약 900건이다. 255거래일 × 약 78봉이면
#: 약 2만 봉 = 약 22페이지. 상한은 넉넉히 둔다.
_DEFAULT_MAX_PAGES = 80

#: 봉 타임스탬프 의미 — 2026-09-16 실측으로 "봉의 시작 시각" 확인(V1.5 주석).
TIMESTAMP_SEMANTICS = "BAR_START"


def _price(value):
    """등락 부호가 붙은 가격 문자열 → Decimal 절대값."""
    s = str(value if value is not None else "").replace(",", "").strip()
    if not s:
        return None, False
    signed = s[0] in "+-"
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        return None, signed
    return (-d if d < 0 else d), signed


def _int(value):
    s = str(value if value is not None else "").replace(",", "").strip()
    if not s:
        return None
    try:
        return int(Decimal(s))
    except (InvalidOperation, ValueError):
        return None


def kiwoom_stock_code(code) -> str:
    """수집 대상 → 키움 `stk_cd`. `NXT:005930`은 `005930_NX`(넥스트레이드 봉).

    키움 REST는 종목코드 뒤 `_NX`를 붙이면 넥스트레이드 체결만으로 만든 봉을 따로
    준다(2026-10-20 실측: 005930_NX 1분봉 08:00~19:59, 하루 합 15,604,675 = `_NX`
    일봉 거래량, KIS NX 체결량과 99.9%). `_AL`(통합) 분봉은 응답이 없었다.
    공식 명세 문구로는 확인하지 않았다 — 실측으로만 확인한 요청 방식이다.
    """
    s = str(code).strip().upper()
    if s.startswith("NXT:"):
        return s[4:] + "_NX"
    if s.startswith("KRX:"):
        return s[4:]
    return s


class MinuteFetchResult:
    """한 종목의 분봉 수집 결과."""

    __slots__ = ("security_code", "period_min", "bars", "pages",
                 "rows_received", "pagination_complete", "status",
                 "error_code", "error_message", "stop_reason", "quality_flags",
                 "requested")

    def __init__(self, security_code, period_min, bars, pages, rows_received,
                 pagination_complete, status, error_code=None,
                 error_message=None, stop_reason="", quality_flags=(),
                 requested=None):
        self.security_code = security_code
        self.period_min = int(period_min)
        self.bars = bars
        self.pages = pages
        self.rows_received = rows_received
        self.pagination_complete = pagination_complete
        self.status = status
        self.error_code = error_code
        self.error_message = error_message
        self.stop_reason = stop_reason
        self.quality_flags = sorted(set(quality_flags))
        self.requested = requested or {}

    @property
    def first_bar(self):
        return self.bars[0] if self.bars else None

    @property
    def last_bar(self):
        return self.bars[-1] if self.bars else None

    @property
    def trading_dates(self):
        return sorted({b["trading_date"] for b in self.bars})

    def to_dict(self):
        dates = self.trading_dates
        return {
            "security_code": self.security_code,
            "period_min": self.period_min,
            "bars": len(self.bars),
            "rows_received": self.rows_received,
            "pages": self.pages,
            "trading_days": len(dates),
            "first_date": dates[0].isoformat() if dates else None,
            "last_date": dates[-1].isoformat() if dates else None,
            "pagination_complete": self.pagination_complete,
            "status": self.status,
            "error_code": self.error_code,
            "stop_reason": self.stop_reason,
            "quality_flags": self.quality_flags,
        }


class KiwoomMinuteSource:
    """ka10080 분봉 조회 — 주는 데까지 모두 받는다."""

    def __init__(self, scanner=None, max_pages: int = _DEFAULT_MAX_PAGES,
                 adjusted: bool = True):
        self.scanner = scanner or load_scanner_v15()
        self.max_pages = int(max_pages)
        self.adjusted = bool(adjusted)

    def token(self):
        tok = token_with_retry(self.scanner)   # 일시 실패(부팅 직후 등) 재시도
        if not tok:
            raise ExternalApiError("키움 토큰 발급 실패",
                                   detail_code=DetailCode.AUTH_FAILED)
        return tok

    def fetch_security(self, code: str, token: str = None, period: int = 5,
                       max_pages: int = None, min_date: str = None):
        """한 종목의 분봉을 API가 주는 끝까지 받는다.

        `min_date`(YYYYMMDD)를 주면 그보다 과거에 닿는 순간 멈춘다 —
        증분 갱신에서 이미 가진 구간을 다시 받지 않기 위한 것이다.
        """
        sc = self.scanner
        tok = token or self.token()
        cap = int(max_pages or self.max_pages)
        body = {"stk_cd": kiwoom_stock_code(code), "tic_scope": str(int(period)),
                "upd_stkpc_tp": "1" if self.adjusted else "0"}

        by_key, flags = {}, set()
        next_key = cont_yn = ""
        seen_keys = set()
        pages = rows_received = 0
        stop_reason = "END_OF_DATA"
        reached_min = False

        for _page in range(cap):
            headers = ([f"next-key: {next_key}", f"cont-yn: {cont_yn}"]
                       if next_key else [])
            data = sc._kiwoom(_PATH, body, _API_ID, tok, extra_headers=headers)
            pages += 1
            if not data:
                return MinuteFetchResult(
                    code, period, self._sorted(by_key), pages, rows_received,
                    False, QualityStatus.FAILED, DetailCode.API_NO_RESPONSE,
                    "API 응답 없음(재시도 후에도 실패)", "NO_RESPONSE", flags,
                    dict(body, max_pages=cap))

            rows = (data.get("stk_min_pole_chart_qry")
                    or data.get("output") or data.get("stk_chart") or [])
            if not rows:
                stop_reason = "EMPTY_PAGE"
                break
            rows_received += len(rows)

            for item in rows:
                bar = self._normalize(item, period, flags)
                if bar is None:
                    continue
                if min_date and bar["dt"][:8] < min_date:
                    reached_min = True
                    continue
                by_key[(bar["bar_start"], bar["session"])] = bar

            next_key = data.get("_next_key", "") or ""
            cont_yn = data.get("_cont_yn", "") or ""
            if reached_min:
                stop_reason = "REACHED_MIN_DATE"
                break
            if cont_yn != "Y" or not next_key:
                stop_reason = "NO_MORE_PAGES"
                break
            if next_key in seen_keys:
                flags.add("PAGINATION_CURSOR_REPEAT")
                stop_reason = "CURSOR_REPEAT"
                break
            seen_keys.add(next_key)
        else:
            stop_reason = "PAGE_CAP"

        bars = self._sorted(by_key)
        complete = stop_reason in ("REACHED_MIN_DATE", "NO_MORE_PAGES",
                                   "END_OF_DATA", "EMPTY_PAGE")
        if stop_reason in ("PAGE_CAP", "CURSOR_REPEAT"):
            status, error = QualityStatus.PARTIAL, DetailCode.API_PARTIAL_PAGE
            message = f"페이지네이션이 {stop_reason}로 중단됐다"
        elif not bars:
            status, error = QualityStatus.FAILED, DetailCode.MISSING_BARS
            message = "수집된 분봉이 없다"
        else:
            status, error, message = QualityStatus.SUCCESS, None, None

        return MinuteFetchResult(code, period, bars, pages, rows_received,
                                 complete, status, error, message, stop_reason,
                                 flags, dict(body, max_pages=cap,
                                             min_date=min_date))

    @staticmethod
    def _sorted(by_key):
        return [by_key[k] for k in sorted(by_key)]

    def _normalize(self, item, period, flags):
        sc = self.scanner
        dt = str(item.get("cntr_tm") or item.get("dt") or "").strip()
        if len(dt) < 12 or not dt[:12].isdigit():
            flags.add("BAD_TIMESTAMP_FIELD")
            return None

        close, c_signed = _price(item.get("cur_prc", item.get("close_pric")))
        open_, o_signed = _price(item.get("open_pric"))
        high, h_signed = _price(item.get("high_pric"))
        low, l_signed = _price(item.get("low_pric"))
        if any((c_signed, o_signed, h_signed, l_signed)):
            flags.add("SIGNED_PRICE_FIELD")

        volume = _int(item.get("trde_qty"))
        if None in (close, open_, high, low) or volume is None:
            flags.add("INCOMPLETE_ROW")
            return None
        if min(open_, high, low, close) <= 0:
            flags.add("NON_POSITIVE_PRICE")
            return None

        # 봉 경계와 세션은 V1.5 함수를 그대로 쓴다 — 판정이 두 곳에서
        # 갈리면 replay가 live와 달라진다.
        bar_start, bar_end = sc.bar_bounds(dt, period)
        if bar_start is None or bar_end is None:
            flags.add("BAR_BOUNDS_UNRESOLVED")
            return None

        traded_value = _int(item.get("trde_prica", item.get("trde_amt")))
        return {
            "dt": dt,
            "bar_start": bar_start,
            "bar_end": bar_end,
            "trading_date": bar_start.date(),
            "session": sc.session_of(dt),
            "open": open_, "high": high, "low": low, "close": close,
            "volume": volume,
            "traded_value": (Decimal(traded_value)
                             if traded_value is not None else None),
            "source_timestamp_raw": dt,
        }

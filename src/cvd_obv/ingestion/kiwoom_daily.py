# -*- coding: utf-8 -*-
"""키움 ka10081 일봉 수집 어댑터.

HTTP·인증·재시도·호출간격은 V1.5(`tools/kiwoom_cvd_obv_scanner.py`)의 검증된
`_kiwoom()`을 그대로 쓴다. 같은 일을 두 번 구현하면 두 번 틀린다.

V2가 더하는 것은 세 가지다.
1. **끝까지 페이지를 넘긴다.** V1.5는 `days` 창과 20페이지 상한으로 최근
   구간만 본다. 11년을 받으려면 `cont-yn`이 끝날 때까지 간다.
2. **중단을 성공으로 처리하지 않는다.** 페이지 상한이나 커서 반복으로 멈추면
   `API_PARTIAL_PAGE`를 남기고 PARTIAL로 보고한다.
3. **원본 필드 의미를 기록한다.** 키움은 절대가격에도 등락 부호를 붙여
   내려보낸다(예: 저가 `-52700`). 절대값을 쓰되 부호가 있었다는 사실을
   품질 플래그로 남긴다.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from .kiwoom_auth import token_with_retry
from ..baseline.scanner_v15 import load_scanner_v15
from ..domain.errors import ExternalApiError
from ..domain.status import DetailCode, QualityStatus

__all__ = ["KIWOOM_DAILY_SOURCE", "DailyFetchResult", "KiwoomDailySource",
           "parse_price", "parse_int"]

KIWOOM_DAILY_SOURCE = "KIWOOM_KA10081"
_API_ID = "ka10081"
_PATH = "/api/dostk/chart"

#: 실측(2026-09-16): 삼성전자 10,931행 = 19페이지(1페이지 약 600행), 1985-01-04부터.
#: 로드맵이 전제한 "약 11년"보다 훨씬 길다. 상한은 40년치를 덮도록 잡는다.
_DEFAULT_MAX_PAGES = 120

#: `trde_prica`(거래대금)의 단위 — **백만원**.
#: 근거(2026-09-16 실측, 3종목 24,175행): `trde_prica × 10^6`을
#: `거래량 × (고가+저가+종가)/3`으로 나눈 중앙값이 2000년 이후 전 구간에서
#: 0.9994~1.0014였다. 원 단위였다면 10^-6 수준이어야 한다.
TRADED_VALUE_UNIT = Decimal(10) ** 6

#: 거래대금과 거래량×가격이 이 배수 밖으로 어긋나면 플래그를 남긴다.
#: 같은 실측에서 2000년 이전 구간의 비율 중앙값이 정확히 0.0100이었다 —
#: 수정주가와 거래대금의 기준이 그 시기에 어긋난다는 뜻이다. 어느 쪽이 맞는지
#: 확인하기 전에는 값을 고치지 않고 플래그만 남긴다.
_TRADED_VALUE_TOLERANCE = (Decimal("0.5"), Decimal("2.0"))


def parse_price(value):
    """키움 가격 문자열 → Decimal(절대값). `(값, 부호있었음)`을 돌려준다."""
    s = str(value if value is not None else "").replace(",", "").strip()
    if not s:
        return None, False
    signed = s[0] in "+-"
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        return None, signed
    return (-d if d < 0 else d), signed


def parse_int(value):
    s = str(value if value is not None else "").replace(",", "").strip()
    if not s:
        return None
    try:
        return int(Decimal(s))
    except (InvalidOperation, ValueError):
        return None


class DailyFetchResult:
    """한 종목의 수집 결과."""

    __slots__ = ("security_code", "rows", "pages", "rows_received",
                 "pagination_complete", "status", "error_code", "error_message",
                 "stop_reason", "quality_flags", "requested")

    def __init__(self, security_code, rows, pages, rows_received,
                 pagination_complete, status, error_code=None,
                 error_message=None, stop_reason="", quality_flags=(),
                 requested=None):
        self.security_code = security_code
        self.rows = rows
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
    def first_date(self):
        return self.rows[0]["trading_date"] if self.rows else None

    @property
    def last_date(self):
        return self.rows[-1]["trading_date"] if self.rows else None

    def to_dict(self):
        return {
            "security_code": self.security_code,
            "rows": len(self.rows),
            "rows_received": self.rows_received,
            "pages": self.pages,
            "pagination_complete": self.pagination_complete,
            "status": self.status,
            "error_code": self.error_code,
            "stop_reason": self.stop_reason,
            "quality_flags": self.quality_flags,
            "first_date": self.first_date.isoformat() if self.first_date else None,
            "last_date": self.last_date.isoformat() if self.last_date else None,
        }


class KiwoomDailySource:
    """ka10081 일봉 조회."""

    def __init__(self, scanner=None, max_pages: int = _DEFAULT_MAX_PAGES,
                 adjusted: bool = True):
        self.scanner = scanner or load_scanner_v15()
        self.max_pages = int(max_pages)
        self.adjusted = bool(adjusted)

    # ── 인증 ───────────────────────────────────────────────

    def token(self):
        tok = token_with_retry(self.scanner)   # 일시 실패(부팅 직후 등) 재시도
        if not tok:
            raise ExternalApiError(
                "키움 토큰 발급 실패 — KIWOOM_APP_KEY/KIWOOM_SECRET_KEY 확인",
                detail_code=DetailCode.AUTH_FAILED)
        return tok

    @property
    def adjustment_type(self) -> str:
        return "SPLIT_ADJUSTED" if self.adjusted else "RAW"

    # ── 수집 ───────────────────────────────────────────────

    def fetch_security(self, code: str, token: str = None,
                       start: date = None, end: date = None,
                       max_pages: int = None) -> DailyFetchResult:
        """한 종목의 일봉을 `start` 이전에 닿을 때까지 받아온다.

        `start=None`이면 API가 주는 데까지 전부 받는다(11년 실측).
        결과는 날짜 오름차순이며 중복이 제거돼 있다.
        """
        sc = self.scanner
        tok = token or self.token()
        end_str = (end or date.today()).strftime("%Y%m%d")
        start_str = start.strftime("%Y%m%d") if start else None
        cap = int(max_pages or self.max_pages)

        body = {"stk_cd": str(code), "base_dt": end_str,
                "upd_stkpc_tp": "1" if self.adjusted else "0"}

        by_date, flags = {}, set()
        next_key = cont_yn = ""
        seen_keys = set()
        pages = rows_received = 0
        stop_reason = "END_OF_DATA"
        reached_start = False

        for page in range(cap):
            headers = ([f"next-key: {next_key}", f"cont-yn: {cont_yn}"]
                       if next_key else [])
            data = sc._kiwoom(_PATH, body, _API_ID, tok, extra_headers=headers)
            pages += 1
            if not data:
                return DailyFetchResult(
                    code, self._sorted(by_date), pages, rows_received, False,
                    QualityStatus.FAILED, DetailCode.API_NO_RESPONSE,
                    "API 응답 없음(재시도 후에도 실패)", "NO_RESPONSE",
                    flags, {"base_dt": end_str, "start": start_str})

            raw_rows = (data.get("stk_dt_pole_chart_qry")
                        or data.get("output") or data.get("stk_chart") or [])
            if not raw_rows:
                stop_reason = "EMPTY_PAGE"
                break
            rows_received += len(raw_rows)

            for item in raw_rows:
                parsed = self._normalize(item, flags)
                if parsed is None:
                    continue
                if start_str and parsed["dt"] < start_str:
                    reached_start = True
                    continue
                if parsed["dt"] > end_str:
                    # base_dt 이후 날짜가 섞이면 요청 의미가 깨진 것이다.
                    flags.add("BAR_AFTER_BASE_DT")
                    continue
                by_date[parsed["dt"]] = parsed

            next_key = data.get("_next_key", "") or ""
            cont_yn = data.get("_cont_yn", "") or ""

            if reached_start:
                stop_reason = "REACHED_START"
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

        rows = self._sorted(by_date)
        complete = stop_reason in ("REACHED_START", "NO_MORE_PAGES",
                                   "END_OF_DATA", "EMPTY_PAGE")
        if stop_reason == "PAGE_CAP":
            status, error_code = QualityStatus.PARTIAL, DetailCode.API_PARTIAL_PAGE
            message = f"페이지 상한 {cap}에 걸렸다 — 더 과거 데이터가 남아 있다"
        elif stop_reason == "CURSOR_REPEAT":
            status, error_code = QualityStatus.PARTIAL, DetailCode.API_PARTIAL_PAGE
            message = "동일 next-key가 반복돼 중단했다"
        elif not rows:
            status, error_code = QualityStatus.FAILED, DetailCode.MISSING_BARS
            message = "수집된 일봉이 없다"
        else:
            status, error_code, message = QualityStatus.SUCCESS, None, None

        return DailyFetchResult(
            code, rows, pages, rows_received, complete, status, error_code,
            message, stop_reason, flags,
            {"base_dt": end_str, "start": start_str,
             "upd_stkpc_tp": body["upd_stkpc_tp"], "max_pages": cap})

    # ── 정규화 ─────────────────────────────────────────────

    @staticmethod
    def _sorted(by_date):
        return [by_date[k] for k in sorted(by_date)]

    @staticmethod
    def _normalize(item, flags):
        """API row → 중간 dict. 값이 불완전하면 None.

        `flags`는 종목 단위 집합이고, row 단위 문제는 `row["quality_flags"]`에
        따로 담는다. 한 행의 문제가 종목 전체의 품질처럼 보이면 안 된다.
        """
        dt = str(item.get("dt") or item.get("cntr_tm") or "").strip()[:8]
        if len(dt) != 8 or not dt.isdigit():
            flags.add("BAD_DATE_FIELD")
            return None

        close, c_signed = parse_price(
            item.get("cur_prc", item.get("close_pric")))
        open_, o_signed = parse_price(item.get("open_pric"))
        high, h_signed = parse_price(item.get("high_pric"))
        low, l_signed = parse_price(item.get("low_pric"))
        if any(s for s in (c_signed, o_signed, h_signed, l_signed)):
            flags.add("SIGNED_PRICE_FIELD")

        volume = parse_int(item.get("trde_qty"))
        traded_value = parse_int(
            item.get("trde_prica", item.get("trde_amt")))

        if None in (close, open_, high, low) or volume is None:
            flags.add("INCOMPLETE_ROW")
            return None
        if min(open_, high, low, close) <= 0:
            flags.add("NON_POSITIVE_PRICE")
            return None

        row_flags = []
        value_krw = None
        if traded_value is not None:
            value_krw = Decimal(traded_value) * TRADED_VALUE_UNIT
            typical = (high + low + close) / 3
            approx = Decimal(volume) * typical
            if approx > 0:
                ratio = value_krw / approx
                lo, hi = _TRADED_VALUE_TOLERANCE
                if not (lo <= ratio <= hi):
                    # 값을 버리지도, 고치지도 않는다. 어긋났다는 사실을 남긴다.
                    row_flags.append("TRADED_VALUE_INCONSISTENT")

        return {
            "dt": dt,
            "trading_date": date(int(dt[:4]), int(dt[4:6]), int(dt[6:8])),
            "open": open_, "high": high, "low": low, "close": close,
            "volume": volume,
            "traded_value": value_krw,
            "quality_flags": row_flags,
        }

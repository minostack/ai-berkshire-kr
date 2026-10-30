# -*- coding: utf-8 -*-
"""키움 ka20006 업종(지수) 일봉 수집 — `silver.benchmark_bars`.

실측으로 확인한 사실(2026-09-18)
--------------------------------
- `ka20006`은 `inds_cd`(업종코드)와 `base_dt`를 받아 지수 일봉을 준다.
  응답 키는 `inds_dt_pole_qry`이고, 페이지당 600행 + `cont-yn` 방식은 ka10081과 같다.
- **값은 ×100 스케일이다.** `inds_cd=001`(KOSPI 종합)의 2024-04-02 종가가
  `275316`으로 내려오는데 실제 KOSPI 종가는 2,753.16이다.
- 업종코드는 응답 값으로 판별했다. 문서를 추측해 박지 않았다.
    001 → 2024-04-02 = 2,753.16 (KOSPI 종합)
    101 → 2024-04-02 =   891.59 (KOSDAQ 종합)
    201 → 2024-04-02 =   376.34 (코스피200)
  100/180/181/182/301은 값이 비어 있었다(해당 업종 없음).

한계
----
이 지수는 **가격지수**다. 배당 재투자를 반영하지 않으므로, 배당을 포함한
초과수익을 보려면 total return 지수가 따로 필요하다. `total_return_close`를
null로 두고 그 사실을 남긴다.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from .kiwoom_auth import token_with_retry
from ..baseline.scanner_v15 import load_scanner_v15
from ..domain.errors import ExternalApiError
from ..domain.status import DetailCode, QualityStatus
from .kiwoom_daily import DailyFetchResult

__all__ = ["KIWOOM_BENCHMARK_SOURCE", "BENCHMARKS", "INDEX_SCALE",
           "KiwoomBenchmarkSource"]

KIWOOM_BENCHMARK_SOURCE = "KIWOOM_KA20006"
_API_ID = "ka20006"
_PATH = "/api/dostk/chart"
_DEFAULT_MAX_PAGES = 120

#: 응답 정수를 실제 지수값으로 바꾸는 배수(실측).
INDEX_SCALE = Decimal(100)

#: 실측으로 확인한 업종코드. 확인 근거는 모듈 docstring에 있다.
#: `kis_cd`는 KIS 업종코드(`FHKUP03500100`) — 2026-10-23 같은 기준일 종가로 확인
BENCHMARKS = {
    "KRX:KOSPI200": {"inds_cd": "201", "kis_cd": "2001", "name": "코스피200",
                     "anchor": ("20240402", Decimal("376.34"))},
    "KRX:KOSPI": {"inds_cd": "001", "kis_cd": "0001", "name": "코스피 종합",
                  "anchor": ("20240402", Decimal("2753.16"))},
    "KRX:KOSDAQ": {"inds_cd": "101", "kis_cd": "1001", "name": "코스닥 종합",
                   "anchor": ("20240402", Decimal("891.59"))},
}


def _value(raw):
    """`+`/`-` 부호와 콤마를 떼고 ×100 스케일을 되돌린다."""
    s = str(raw if raw is not None else "").replace(",", "").strip()
    if not s:
        return None
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        return None
    return (-d if d < 0 else d) / INDEX_SCALE


def _int(raw):
    s = str(raw if raw is not None else "").replace(",", "").strip()
    if not s:
        return None
    try:
        return int(Decimal(s))
    except (InvalidOperation, ValueError):
        return None


class KiwoomBenchmarkSource:
    """ka20006 지수 일봉 조회. 페이지네이션 규칙은 ka10081과 같다."""

    def __init__(self, scanner=None, max_pages: int = _DEFAULT_MAX_PAGES):
        self.scanner = scanner or load_scanner_v15()
        self.max_pages = int(max_pages)

    def token(self):
        tok = token_with_retry(self.scanner)   # 일시 실패(부팅 직후 등) 재시도
        if not tok:
            raise ExternalApiError(
                "키움 토큰 발급 실패", detail_code=DetailCode.AUTH_FAILED)
        return tok

    def fetch_benchmark(self, benchmark_id: str, token: str = None,
                        start: date = None, end: date = None,
                        max_pages: int = None) -> DailyFetchResult:
        spec = BENCHMARKS.get(benchmark_id)
        if spec is None:
            raise ExternalApiError(
                f"알 수 없는 benchmark_id: {benchmark_id} "
                f"(가능: {sorted(BENCHMARKS)})")

        sc = self.scanner
        tok = token or self.token()
        end_str = (end or date.today()).strftime("%Y%m%d")
        start_str = start.strftime("%Y%m%d") if start else None
        cap = int(max_pages or self.max_pages)
        body = {"inds_cd": spec["inds_cd"], "base_dt": end_str}

        by_date, flags = {}, set()
        next_key = cont_yn = ""
        seen_keys = set()
        pages = rows_received = 0
        stop_reason = "END_OF_DATA"
        reached_start = False

        for _page in range(cap):
            headers = ([f"next-key: {next_key}", f"cont-yn: {cont_yn}"]
                       if next_key else [])
            data = sc._kiwoom(_PATH, body, _API_ID, tok, extra_headers=headers)
            pages += 1
            if not data:
                return DailyFetchResult(
                    benchmark_id, self._sorted(by_date), pages, rows_received,
                    False, QualityStatus.FAILED, DetailCode.API_NO_RESPONSE,
                    "API 응답 없음(재시도 후에도 실패)", "NO_RESPONSE", flags,
                    {"inds_cd": spec["inds_cd"], "base_dt": end_str,
                     "start": start_str})

            rows = data.get("inds_dt_pole_qry") or []
            if not rows:
                stop_reason = "EMPTY_PAGE"
                break
            rows_received += len(rows)

            for item in rows:
                parsed = self._normalize(item, flags)
                if parsed is None:
                    continue
                if start_str and parsed["dt"] < start_str:
                    reached_start = True
                    continue
                if parsed["dt"] > end_str:
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

        out_rows = self._sorted(by_date)
        self._check_anchor(spec, out_rows, flags)

        complete = stop_reason in ("REACHED_START", "NO_MORE_PAGES",
                                   "END_OF_DATA", "EMPTY_PAGE")
        if stop_reason in ("PAGE_CAP", "CURSOR_REPEAT"):
            status, code = QualityStatus.PARTIAL, DetailCode.API_PARTIAL_PAGE
            message = f"페이지네이션이 {stop_reason}로 중단됐다"
        elif not out_rows:
            status, code = QualityStatus.FAILED, DetailCode.MISSING_BARS
            message = "수집된 지수 일봉이 없다"
        else:
            status, code, message = QualityStatus.SUCCESS, None, None

        return DailyFetchResult(
            benchmark_id, out_rows, pages, rows_received, complete, status,
            code, message, stop_reason, flags,
            {"inds_cd": spec["inds_cd"], "base_dt": end_str,
             "start": start_str, "max_pages": cap})

    @staticmethod
    def _check_anchor(spec, rows, flags):
        """알려진 기준일 값과 대조해 업종코드가 바뀌지 않았는지 확인한다.

        업종코드는 문서가 아니라 실측으로 정했다. 공급자가 코드 체계를 바꾸면
        조용히 다른 지수를 벤치마크로 쓰게 되므로, 수집할 때마다 검산한다.
        """
        anchor_dt, anchor_value = spec["anchor"]
        for row in rows:
            if row["dt"] != anchor_dt:
                continue
            if abs(row["close"] - anchor_value) > Decimal("0.5"):
                flags.add("ANCHOR_MISMATCH")
            return
        flags.add("ANCHOR_DATE_ABSENT")

    @staticmethod
    def _sorted(by_date):
        return [by_date[k] for k in sorted(by_date)]

    @staticmethod
    def _normalize(item, flags):
        dt = str(item.get("dt") or "").strip()[:8]
        if len(dt) != 8 or not dt.isdigit():
            flags.add("BAD_DATE_FIELD")
            return None
        close = _value(item.get("cur_prc"))
        open_ = _value(item.get("open_pric"))
        high = _value(item.get("high_pric"))
        low = _value(item.get("low_pric"))
        if None in (close, open_, high, low):
            flags.add("INCOMPLETE_ROW")
            return None
        if min(open_, high, low, close) <= 0:
            flags.add("NON_POSITIVE_VALUE")
            return None
        return {
            "dt": dt,
            "trading_date": date(int(dt[:4]), int(dt[4:6]), int(dt[6:8])),
            "open": open_, "high": high, "low": low, "close": close,
            "volume": _int(item.get("trde_qty")),
            "quality_flags": [],
        }

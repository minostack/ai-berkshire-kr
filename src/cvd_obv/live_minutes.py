# -*- coding: utf-8 -*-
"""스캐너 실행 중 1분봉 — **지난 날은 로컬 저장분, 오늘은 KIS API**(2026-10-23).

왜 이렇게 나누나
----------------
CVD/OBV(`tools/cvd_obv_scan.py`)·CVD 가속(`tools/cvd_accel_scanner.py`)은 실행할
때마다 최근 N거래일 1분봉이 필요하다(5일·12일). 예전에는 키움 `ka10080`을 매번
불렀다(한 번에 약 900봉). KIS 1분봉은 한 번에 120봉이라 같은 일을 하면 종목당
수십 번을 불러야 한다(351종목 × 12일 ≈ 2.6만 회).

그런데 지난 날 1분봉은 이미 적재돼 있다 — 16:10·08:30 적재가 **같은 KIS API**로 받아
`silver.minute_bars`에 둔다(봉 단위로 키움과 같다는 실측은 `ingestion/kis_minute.py`).
그래서 지난 날은 로컬에서 한 번에 읽고, 아직 적재되지 않은 **오늘**만 KIS로 받는다
(장중 종목당 1~4회).

로컬에 빠진 날이 있으면(적재 실패·신규 편입) 그 종목만 그날까지 KIS로 받는다 —
조용히 짧은 시계열로 계산하지 않는다. 거래일 목록은 KIS 일봉(005930) 1회 조회로
얻는다(로컬 달력은 적재가 밀리면 같이 밀린다).

돌려주는 봉은 V1.5 `fetch_minute_bars()`와 같은 모양이다:
`{"dt", "open", "high", "low", "close", "volume", "bar_start", "bar_end", "session"}`
(시각은 naive KST), `bar_end <= as_of`만, 시각 순.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

from .domain.ids import KST

__all__ = ["KisLiveMinutes"]

_PROBE = "005930"
_COLS = ["security_id", "trading_date", "session", "bar_start_at_utc",
         "bar_end_at_utc", "open", "high", "low", "close", "volume",
         "quality_status", "run_id", "data_revision", "period_min"]


def _naive_kst(ts):
    return ts.astimezone(KST).replace(tzinfo=None)


class KisLiveMinutes:
    """V1.5 `fetch_minute_bars(code, token, period=1, bdays, as_of)` 자리에 쓰는 1분봉."""

    def __init__(self, scanner, store=None, source=None, daily_source=None, log=None):
        from .storage.parquet_store import ParquetStore
        self.sc = scanner
        self.store = store or ParquetStore()
        self._source = source
        self._daily_source = daily_source
        self.log = log or (lambda msg: None)
        self._local = None              # (start, end, {sid: [bar]})
        self._calendar = None           # (as_of_date, [거래일])
        self.local_hits = 0
        self.api_calls = 0
        self.fallback = {}              # code → 로컬에 없어 KIS로 받은 날
        self.failed = []                # 오늘 봉을 KIS에서 못 받은 종목

    # ── 원천 ──────────────────────────────────────────────────────
    @property
    def source(self):
        if self._source is None:
            from .ingestion.kis_minute import KisMinuteSource
            self._source = KisMinuteSource(scanner=self.sc)
        return self._source

    def trading_days(self, as_of: datetime) -> list:
        """as_of 날짜 **전** 거래일(오름차순, 최근 약 60일) — KIS 일봉 1회."""
        day = as_of.date()
        if self._calendar and self._calendar[0] == day:
            return self._calendar[1]
        if self._daily_source is None:
            from .ingestion.kis_daily import KisDailySource
            self._daily_source = KisDailySource()
        res = self._daily_source.fetch_security(
            _PROBE, start=day - timedelta(days=90), end=day - timedelta(days=1))
        days = sorted(r["trading_date"] for r in res.rows)
        if not days:
            # 달력을 못 얻으면 평일로 근사한다(휴장일은 로컬 부족으로 보여 KIS로 받는다)
            days = [day - timedelta(days=i) for i in range(90, 0, -1)
                    if (day - timedelta(days=i)).weekday() < 5]
            self.log("  ⚠ [1분봉] KIS 거래일 조회 실패 — 평일로 근사한다")
        self._calendar = (day, days)
        return days

    # ── 로컬 ──────────────────────────────────────────────────────
    def _load_local(self, start: date, end: date) -> dict:
        """{sid: [V1.5 봉]} — `silver.minute_bars` 1분봉, KRX, 최신 리비전, FAILED 제외.

        파일이 수만 개라(2026-10: 약 1.9만) 그 달 파티션만 하나의 dataset으로 연다
        (전체를 훑으면 약 2분, 이렇게 하면 몇 초).
        """
        if self._local and self._local[0] <= start and self._local[1] >= end:
            return self._local[2]
        import pyarrow.dataset as ds
        from .ingestion.minute_history import month_dataset

        out = {}
        data = month_dataset(self.store, start, end) if start <= end else None
        if data is not None:
            table = data.to_table(columns=_COLS, filter=(
                (ds.field("trading_date") >= start) & (ds.field("trading_date") <= end)
                & (ds.field("period_min") == 1)
                & (ds.field("quality_status") != "FAILED")))
            df = table.to_pandas()
            df = df[df["security_id"].str.startswith("KRX:")]
            # 같은 봉은 리비전 큰 것, 같으면 나중 run_id(ParquetStore 규칙과 같다)
            df = df.sort_values(["data_revision", "run_id"]).drop_duplicates(
                ["security_id", "bar_start_at_utc", "session"], keep="last")
            df = df.sort_values(["security_id", "bar_start_at_utc"])
            for sid, g in df.groupby("security_id", sort=False):
                out[sid] = [{
                    "dt": _naive_kst(s).strftime("%Y%m%d%H%M%S"),
                    "open": float(o), "high": float(h), "low": float(lo),
                    "close": float(c), "volume": int(v),
                    "bar_start": _naive_kst(s), "bar_end": _naive_kst(e),
                    "session": sess,
                } for s, e, o, h, lo, c, v, sess in zip(
                    g["bar_start_at_utc"], g["bar_end_at_utc"], g["open"], g["high"],
                    g["low"], g["close"], g["volume"], g["session"]) if int(v) > 0]
        self._local = (start, end, out)
        return out

    # ── KIS ───────────────────────────────────────────────────────
    def _fetch(self, code, as_of: datetime, min_day: date):
        src = self.source
        src.clock = lambda: as_of
        before = src.client.calls
        res = src.fetch_security(f"KRX:{code}", period=1,
                                 min_date=min_day.strftime("%Y%m%d"))
        self.api_calls += src.client.calls - before
        if res.status == "FAILED":
            self.failed.append(str(code))
        return [{
            "dt": b["dt"], "open": float(b["open"]), "high": float(b["high"]),
            "low": float(b["low"]), "close": float(b["close"]),
            "volume": int(b["volume"]), "bar_start": b["bar_start"],
            "bar_end": b["bar_end"], "session": b["session"],
        } for b in res.bars if b["volume"] > 0]

    # ── 진입점 ────────────────────────────────────────────────────
    def bars(self, code, bdays: int, as_of: datetime) -> list:
        code = str(code)
        today = as_of.date()
        past = self.trading_days(as_of)
        need = past[-int(bdays):] if bdays else past
        start = need[0] if need else today
        # V1.5와 같은 여유(달력일 = 영업일 × 7/5 + 10) — 세션 판정 일관성을 위해
        start = min(start, today - timedelta(days=math.ceil(bdays * 7 / 5) + 10))
        local = self._load_local(start, today - timedelta(days=1)).get(f"KRX:{code}", [])
        have = {b["bar_start"].date() for b in local if b["session"] == "REGULAR"}
        missing = [d for d in need if d not in have]
        if missing:
            self.fallback[code] = missing
            fetched = self._fetch(code, as_of, missing[0])
        else:
            self.local_hits += 1
            fetched = self._fetch(code, as_of, today)
        merged = {(b["bar_start"], b["session"]): b for b in local}
        for b in fetched:
            merged[(b["bar_start"], b["session"])] = b
        return [merged[k] for k in sorted(merged) if merged[k]["bar_end"] <= as_of]

    def summary_lines(self):
        out = [f"  [1분봉] 지난 날 로컬 저장분 {self.local_hits}종목 + 오늘 KIS "
               f"(호출 {self.api_calls}회)"]
        if self.fallback:
            out.append(f"  ⚠ 로컬 1분봉이 빠져 KIS로 받은 종목 {len(self.fallback)}개 "
                       f"({', '.join(sorted(self.fallback)[:8])}"
                       f"{' …' if len(self.fallback) > 8 else ''}) — "
                       "`python cvdobv.py data-load daily` 확인")
        if self.failed:
            out.append(f"  ⚠ 오늘 1분봉을 KIS에서 받지 못한 종목 {len(self.failed)}개 "
                       f"({', '.join(self.failed[:8])}) — 지난 날 봉으로만 계산됐다")
        return out

# -*- coding: utf-8 -*-
"""스캐너용 1분봉 — **적재된 것만 읽는다. API를 부르지 않는다.**

왜 바꿨나 (2026-10-26)
----------------------
예전 공급기(`live_minutes.KisLiveMinutes`)는 **실행하는 순간까지의 오늘 분봉**을
API로 받아 섞었다. 그러면 같은 날에도 **몇 시에 돌렸느냐에 따라 결과가 달라진다** —
10:30에 돌린 CVD와 14:00에 돌린 CVD가 다르고, 둘 다 "오늘"이라고 불린다.
재현도 안 되고(나중에 같은 시각을 못 만든다), 적재된 봉과 대조할 수도 없다.

그래서 스캐너는 **적재가 끝난 봉만** 본다. 기준일은 시계가 아니라
적재 단계가 정한다(`bar_standard.resolve_scan_day`).

    16:10 이전 → 전 거래일 **확정** 봉으로 스캔 (오늘은 아직 적재 전)
    16:10 이후 → 오늘 **잠정** 봉으로 스캔 (16:10 적재가 넣은 것)

돌려주는 봉은 V1.5 `fetch_minute_bars()`와 같은 모양이다:
`{"dt", "open", "high", "low", "close", "volume", "bar_start", "bar_end", "session"}`
(시각은 naive KST), 시각 순.

없는 날은 **조용히 넘어가지 않는다** — 종목별로 세어 두고 실행 끝에 알린다.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

from .domain.ids import KST

__all__ = ["StoredMinutes"]

_COLS = ["security_id", "trading_date", "session", "bar_start_at_utc",
         "bar_end_at_utc", "open", "high", "low", "close", "volume",
         "quality_status", "run_id", "data_revision", "period_min"]


def _naive_kst(ts):
    return ts.astimezone(KST).replace(tzinfo=None)


class StoredMinutes:
    """`silver.minute_bars`의 1분봉만 주는 공급기 — 네트워크를 쓰지 않는다."""

    def __init__(self, store=None, log=None):
        from .storage.parquet_store import ParquetStore
        self.store = store or ParquetStore()
        self.log = log or (lambda msg: None)
        self._cache = None              # (start, end, {sid: [bar]})
        self.hits = 0
        self.empty = []                 # 저장 분봉이 하나도 없던 종목
        self.short = {}                 # 코드 → 요청 거래일 중 빠진 날

    # ── 적재분 읽기 ───────────────────────────────────────────────
    def _load(self, start: date, end: date) -> dict:
        """{sid: [V1.5 봉]} — 1분봉, KRX, 최신 리비전, FAILED 제외.

        파일이 수만 개라 그 달 파티션만 하나의 dataset으로 연다
        (`ingestion.minute_history.month_dataset`).
        """
        if self._cache and self._cache[0] <= start and self._cache[1] >= end:
            return self._cache[2]
        import pyarrow.dataset as ds
        from .ingestion.minute_history import month_dataset

        out = {}
        data = month_dataset(self.store, start, end) if start <= end else None
        if data is not None:
            table = data.to_table(columns=_COLS, filter=(
                (ds.field("trading_date") >= start)
                & (ds.field("trading_date") <= end)
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
                    g["bar_start_at_utc"], g["bar_end_at_utc"], g["open"],
                    g["high"], g["low"], g["close"], g["volume"], g["session"])
                    if int(v) > 0]
        self._cache = (start, end, out)
        return out

    # ── 진입점 ────────────────────────────────────────────────────
    def bars(self, code, bdays: int, as_of: datetime) -> list:
        """`as_of`가 속한 거래일까지의 1분봉. `as_of`보다 늦게 끝난 봉은 뺀다.

        `as_of`는 **기준 거래일의 끝**으로 들어온다(`resolve_scan_day`가 정한다).
        그래서 그날 적재된 봉이 전부 들어오고, 실행 시각은 결과에 영향을 주지 않는다.
        """
        code = str(code)
        day = as_of.date()
        # V1.5와 같은 여유(달력일 = 영업일 × 7/5 + 10) — 세션 판정 일관성을 위해
        span = math.ceil(int(bdays or 5) * 7 / 5) + 10
        start = day - timedelta(days=span)
        rows = self._load(start, day).get(f"KRX:{code}", [])
        rows = [b for b in rows if b["bar_end"] <= as_of]
        if not rows:
            self.empty.append(code)
            return []
        self.hits += 1
        have = sorted({b["bar_start"].date() for b in rows
                       if b["session"] == "REGULAR"})
        if bdays and len(have) < int(bdays):
            self.short[code] = int(bdays) - len(have)
        return rows

    def summary_lines(self):
        out = [f"  [1분봉] 적재분만 읽었다 — {self.hits}종목 (API 호출 0회)"]
        if self.empty:
            u = sorted(set(self.empty))
            out.append(f"  ⚠ 저장 1분봉이 없는 종목 {len(u)}개"
                       f"({', '.join(u[:8])}{' …' if len(u) > 8 else ''}) — "
                       f"CVD가 결측으로 처리된다. "
                       f"`python cvdobv.py minute-cache catchup`으로 적재")
        if self.short:
            u = sorted(self.short)
            out.append(f"  ⚠ 요청 거래일만큼 1분봉이 없는 종목 {len(u)}개"
                       f"({', '.join(u[:8])}{' …' if len(u) > 8 else ''}) — "
                       f"짧은 구간으로 계산됐다")
        return out

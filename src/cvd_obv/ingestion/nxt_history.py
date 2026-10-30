# -*- coding: utf-8 -*-
"""애프터마켓 시행(2026-09-14) **전** 정규장 일봉에 NXT 메인마켓 거래량을 더한다.

`nxt_volume`(A안)은 9/14 이후의 복원 봉에만 NXT를 더한다. 그 전 날의 정규장 일봉은 키움
원본(KRX만)이라 거래량 정의가 9/14에서 바뀌었다. 이 모듈이 그 전 날도 같은 정의로 맞춘다.

원천 — NXT **30분봉**(키움 `<코드>_NX`, `tic_scope=30`)
----------------------------------------------------
- 1분봉으로 넥스트레이드 개시(2025-03-04)까지 받으려면 종목당 약 300페이지가 든다. 메인마켓
  거래량만 필요하므로 30분봉으로 충분하다 — NXT 세션 경계(08:50, 09:00:30, 15:20, 15:30)가
  30분 경계와 겹치지 않는다: 08:30 봉 = 프리마켓만, 09:00~15:00 봉 = 메인마켓만, 15:30 봉부터
  애프터마켓. 2026-10-21 실측: 005930 9/14~10/20 23일, 30분봉 메인마켓 합 = 1분봉 합 23/23
- **30분봉은 2025-10-01부터만 있다**(같은 실측: 더 과거 페이지 없음). 그래서 2025-03-04 ~
  2025-09-30은 채울 수 없어 KRX만인 채로 남는다 — 거래량 정의 경계가 2025-10-01로 당겨진다
- 30분봉은 `silver.minute_bars`에 `period_min=30`, `NXT:<코드>`로 둔다. 분봉 기준(1분봉
  하나)의 예외이며 이 소급에만 쓴다(9/14 이전 날만 저장)

원천 2 — 저장된 NXT **1분봉**(2026-10-29 추가)
----------------------------------------------
`minute-cache catchup --venue NXT --since …`로 9/14 전 날의 NXT 1분봉을 받아 두었으면(KIS,
2026-10-28 실측 2026-08-03~09-11 299종목) 그 날은 1분봉 메인마켓 합을 쓴다 — 30분봉보다
먼저다. 받은 날의 판정은 1분봉 커버리지(`nxt_volume`과 같다). 1분봉은 받은 시점 기준
수정주가이므로 기준 일봉과 기준이 다를 수 있다: NXT 메인마켓 마지막 1분봉 종가가 기준 행의
[저가, 고가]를 5% 넘게 벗어나면 기준 행 종가 ÷ 그 종가를 계수 f로 보고 NXT 거래량을 f로
나눈다(`NXT_VOLUME_RESCALED`, 복원 봉의 규칙과 같다). 범위 안이면 같은 기준으로 본다 —
종가끼리만 비교하면 NXT 체결이 드문 날 마지막 체결가가 종가와 5% 넘게 달라 오판한다
(2026-10-29 실측: 268280·002840 8/13, NXT 313·367주)

만드는 행
--------
기준 행 = 그날 키움 원본 정규장 일봉(리비전 ≤ 1, NXT 표시 없음) 중 가장 늦게 받은 것.
새 행 = 기준 행 그대로에 거래량만 + NXT 메인마켓, 표시 `NXT_MAIN_VOLUME_INCLUDED`, 리비전 2.
가격·거래대금은 기준 행 그대로다(거래대금은 KRX만 — 30분봉에는 거래대금이 없다).

다시 만들기: 기준 행이 나중에 다시 받아져(수정주가 소급 등) 시가가 달라지거나 거래량이
안 맞으면 다시 만든다. 그래서 몇 번을 돌려도 결과가 같다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .. import SCHEMA_VERSION
from ..contracts.market_schema import (
    DAILY_BAR_COLUMNS, DAILY_BAR_PK, validate_quarantined_ohlc,
    validate_series_order,
)
from ..domain.ids import KST, UTC
from ..storage.parquet_store import ParquetStore
from .nxt_volume import (
    FLAG_NXT_INCLUDED, FLAG_NXT_RESCALED, NXT_MAIN_END, NXT_MAIN_START,
    _coverage_pairs, krx_id, nxt_id,
)

__all__ = ["NXT_HISTORY_PERIOD", "NXT_HISTORY_START", "NXT_HISTORY_END",
           "NXT_HISTORY_REVISION", "nxt_history_volume", "nxt_history_codes",
           "nxt_minute_history",
           "NxtHistoryBuilder"]

NXT_HISTORY_PERIOD = 30
#: NXT 30분봉이 있는 첫 날(2026-10-21 실측)
NXT_HISTORY_START = date(2025, 10, 1)
#: 이 날 **전**까지만 — 그 뒤는 1분봉 경로(`regular_from_minute`)가 맡는다
NXT_HISTORY_END = date(2026, 9, 14)
NXT_HISTORY_REVISION = 2
#: NXT 1분봉 종가가 기준 일봉 [저가, 고가]의 이 폭 안이면 같은 수정주가 기준으로 본다
PRICE_BASIS_TOL = 0.05


def nxt_history_codes(store, start: date, end: date) -> list:
    """그 기간 NXT 체결이 있었던 종목(KIS NX 체결량 행) — 로컬 코드."""
    import pyarrow.dataset as ds
    from ..domain.errors import NoInputDataError
    from .kis_trade_sides import read_trade_sides

    try:
        t = read_trade_sides(
            store, columns=["security_id", "trading_date", "market_div",
                            "buy_qty", "sell_qty"],
            filters=(ds.field("market_div") == "NX")
            & (ds.field("trading_date") >= start) & (ds.field("trading_date") < end))
    except NoInputDataError:
        return []
    out = {sid for sid, b, s in zip(t.column("security_id").to_pylist(),
                                    t.column("buy_qty").to_pylist(),
                                    t.column("sell_qty").to_pylist()) if (b or 0) + (s or 0) > 0}
    return sorted(s.split(":")[-1] for s in out)


def nxt_history_volume(store, start: date, end: date, securities=None) -> dict:
    """{(KRX 종목 ID, 날짜): NXT 메인마켓 거래량} — 저장된 NXT 30분봉에서."""
    import pyarrow.dataset as ds
    from ..contracts.market_schema import MINUTE_BAR_LOGICAL_KEY
    from ..domain.errors import NoInputDataError

    flt = ((ds.field("period_min") == NXT_HISTORY_PERIOD)
           & (ds.field("trading_date") >= start) & (ds.field("trading_date") < end))
    if securities is not None:
        flt = flt & ds.field("security_id").isin(sorted(nxt_id(s) for s in securities))
    try:
        t = store.read_arrow(
            "silver", "minute_bars",
            columns=["security_id", "period_min", "trading_date", "session",
                     "bar_start_at_utc", "volume", "quality_status", "run_id",
                     "data_revision"],
            filters=flt, latest_by=MINUTE_BAR_LOGICAL_KEY)
    except NoInputDataError:
        return {}
    out = {}
    for sid, d, at, vol, status in zip(
            t.column("security_id").to_pylist(), t.column("trading_date").to_pylist(),
            t.column("bar_start_at_utc").to_pylist(), t.column("volume").to_pylist(),
            t.column("quality_status").to_pylist()):
        if not sid.startswith("NXT:") or status == "FAILED":
            continue
        key = (krx_id(sid), d)
        out.setdefault(key, 0)
        if NXT_MAIN_START <= at.astimezone(KST).time() < NXT_MAIN_END:
            out[key] += int(vol or 0)
    return out


def nxt_minute_history(store, start: date, end: date, securities=None) -> dict:
    """{(KRX 종목 ID, 날짜): (NXT 메인마켓 거래량, 메인마켓 마지막 종가 | None)}
    — 저장된 NXT 1분봉에서, 그날 1분봉을 받은 쌍만(받았는데 메인마켓 봉이 없으면 0)."""
    import pyarrow.dataset as ds
    from ..contracts.market_schema import MINUTE_BAR_LOGICAL_KEY
    from ..domain.errors import NoInputDataError

    end = min(end, NXT_HISTORY_END)
    ids = None if securities is None else {nxt_id(s) for s in securities}
    fetched = {(sid, d) for sid, d in _coverage_pairs(store, start, end - timedelta(days=1), ids)
               if d < end}
    if not fetched:
        return {}
    try:
        t = store.read_arrow(
            "silver", "minute_bars",
            columns=["security_id", "period_min", "trading_date", "session",
                     "bar_start_at_utc", "close", "volume", "quality_status", "run_id",
                     "data_revision"],
            filters=((ds.field("period_min") == 1)
                     & (ds.field("trading_date") >= start) & (ds.field("trading_date") < end)
                     & ds.field("security_id").isin(sorted({s for s, _ in fetched}))),
            latest_by=MINUTE_BAR_LOGICAL_KEY)
    except NoInputDataError:
        t = None
    vol = {k: 0 for k in fetched}
    last = {}
    if t is not None:
        for sid, d, at, close, v, status in zip(
                t.column("security_id").to_pylist(), t.column("trading_date").to_pylist(),
                t.column("bar_start_at_utc").to_pylist(), t.column("close").to_pylist(),
                t.column("volume").to_pylist(), t.column("quality_status").to_pylist()):
            if status == "FAILED" or (sid, d) not in fetched:
                continue
            if NXT_MAIN_START <= at.astimezone(KST).time() < NXT_MAIN_END:
                vol[(sid, d)] += int(v or 0)
                if v and close and ((sid, d) not in last or at > last[(sid, d)][0]):
                    last[(sid, d)] = (at, float(close))
    return {(krx_id(sid), d): (v, last[(sid, d)][1] if (sid, d) in last else None)
            for (sid, d), v in vol.items()}


def _basis_factor(base: dict, nxt_close) -> float:
    """기준 일봉 가격 ÷ NXT 1분봉 가격 — NXT 가격이 그날 범위(±5%) 안이면 1."""
    if not nxt_close or not base.get("close"):
        return 1.0
    p = float(nxt_close)
    lo, hi = float(base["low"]), float(base["high"])
    if lo * (1 - PRICE_BASIS_TOL) <= p <= hi * (1 + PRICE_BASIS_TOL):
        return 1.0
    return float(base["close"]) / p


class NxtHistoryBuilder:
    def __init__(self, store: ParquetStore = None):
        self.store = store or ParquetStore()

    def fetch(self, codes, run_id: str, start: date = NXT_HISTORY_START,
              on_progress=None, max_pages: int = 20):
        """NXT 30분봉을 받아 9/14 전 날만 저장한다."""
        from .kiwoom_minute import KiwoomMinuteSource
        from .minute_history import MinuteHistoryCache

        # 30분봉은 키움만 준다(KIS 분봉 원천은 1분봉, 과거 약 1년) — 적재 원천과 무관하게 키움
        cache = MinuteHistoryCache(store=self.store, source=KiwoomMinuteSource())
        keep = lambda sid, d, session: d < NXT_HISTORY_END   # noqa: E731
        return cache.build([f"NXT:{c}" for c in codes], run_id=run_id,
                           period=NXT_HISTORY_PERIOD, max_pages=max_pages,
                           min_date=start.strftime("%Y%m%d"),
                           end=NXT_HISTORY_END - timedelta(days=1),
                           on_progress=on_progress, keep_bar=keep, keep_coverage=keep)

    def _daily_rows(self, start, end, securities):
        """{(종목, 날짜): (기준 행, 지금 NXT 행 | None)} — 리비전 정리 없이 읽어 고른다."""
        import pyarrow.dataset as ds
        from ..domain.errors import NoInputDataError

        flt = ((ds.field("trading_date") >= start) & (ds.field("trading_date") < end)
               & (ds.field("price_scope") == "REGULAR")
               & (ds.field("adjustment_type") == "SPLIT_ADJUSTED")
               & ds.field("security_id").isin(sorted(securities)))
        try:
            rows = self.store.read_arrow("silver", "daily_bars",
                                         columns=[c.name for c in DAILY_BAR_COLUMNS],
                                         filters=flt).to_pylist()
        except NoInputDataError:
            return {}
        base, cur = {}, {}
        for r in rows:
            k = (r["security_id"], r["trading_date"])
            rank = (r["data_revision"], r["run_id"])
            if FLAG_NXT_INCLUDED in (r["quality_flags"] or []):
                if k not in cur or rank > (cur[k]["data_revision"], cur[k]["run_id"]):
                    cur[k] = r
            elif r["data_revision"] <= 1:
                if k not in base or rank > (base[k]["data_revision"], base[k]["run_id"]):
                    base[k] = r
        return {k: (b, cur.get(k)) for k, b in base.items()}

    def build(self, run_id: str, start: date = NXT_HISTORY_START,
              end: date = NXT_HISTORY_END, securities=None, dry_run: bool = False):
        end = min(end, NXT_HISTORY_END)
        # 30분봉 → 1분봉 순으로 덮는다(1분봉이 있는 날은 1분봉)
        nxt = {k: (v, None) for k, v in
               nxt_history_volume(self.store, start, end, securities).items()}
        minute = nxt_minute_history(self.store, start, end, securities)
        nxt.update(minute)
        summary = {"start": start.isoformat(), "end": (end - timedelta(days=1)).isoformat(),
                   "nxt_pairs": len(nxt), "from_1m": len(minute),
                   "from_30m": len(nxt) - len(minute), "rescaled": 0,
                   "written": 0, "up_to_date": 0,
                   "no_base": 0, "rebuilt": 0, "artifacts": []}
        if not nxt:
            return summary
        pairs = self._daily_rows(start, end, {k[0] for k in nxt})
        now = datetime.now(tz=UTC)
        out = []
        for k, (add, nxt_close) in sorted(nxt.items()):
            if k not in pairs:
                summary["no_base"] += 1
                continue
            b, cur = pairs[k]
            f = _basis_factor(b, nxt_close)
            flags = {FLAG_NXT_INCLUDED}
            if f != 1.0:
                flags.add(FLAG_NXT_RESCALED)
                summary["rescaled"] += 1
            want = int(b["volume"]) + int(round(int(add) / f))
            if (cur is not None and int(cur["volume"]) == want
                    and all(float(cur[c]) == float(b[c])
                            for c in ("open", "high", "low", "close"))):
                summary["up_to_date"] += 1
                continue
            if cur is not None:
                summary["rebuilt"] += 1
            row = dict(b)
            row.update(volume=want, run_id=run_id, data_revision=NXT_HISTORY_REVISION,
                       schema_version=SCHEMA_VERSION,
                       quality_flags=sorted(set(b["quality_flags"] or []) | flags))
            row["adjustment_as_of_utc"] = b["adjustment_as_of_utc"] or now
            out.append(row)
        summary["written"] = len(out)
        if out and not dry_run:
            out.sort(key=lambda r: (r["security_id"], r["trading_date"]))
            w = self.store.write(
                "silver", "daily_bars", out, DAILY_BAR_COLUMNS, DAILY_BAR_PK, run_id,
                partition_fn=lambda r: [("market", "KRX"),
                                        ("year", r["trading_date"].year),
                                        ("month", f"{r['trading_date'].month:02d}")],
                validators=[validate_quarantined_ohlc, validate_series_order],
                extra_manifest={"derived_from": ["silver.daily_bars:REGULAR",
                                                 "silver.minute_bars:NXT 1m",
                                                 "silver.minute_bars:NXT 30m"],
                                "note": "9/14 전 정규장 일봉 + NXT 메인마켓 거래량"})
            summary["artifacts"] = [w.manifest_path]
        return summary

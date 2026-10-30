# -*- coding: utf-8 -*-
"""넥스트레이드(NXT) 메인마켓 거래량 — 정규장 일봉 거래량에 더한다(A안, 2026-10-20).

왜
--
NXT 거래량은 무시할 수준이 아니다. 2026-10-19 삼성전자: KRX 정규장 1분봉 14,350,140주,
NXT 메인마켓(09:00~15:29) 7,821,239주. 정규장 일봉 거래량을 KRX만으로 두면 실제
거래의 상당 부분이 빠진다.

A안 — 무엇을 더하나
-------------------
- **NXT 메인마켓(09:00:30~15:20)만** 더한다. KRX 정규장과 같은 시간대다. 1분봉 시작
  시각이 [09:00, 15:30)인 NXT 봉의 거래량 합이다(15:20~15:29는 거래가 없다)
- 프리마켓(08:00~08:50)과 애프터마켓(15:30~20:00)은 더하지 않는다
- **가격(시가·고가·저가·종가)은 KRX 그대로다.** 공식 종가는 KRX 15:30 종가 단일가다

원천
----
키움 1분봉 `stk_cd=<코드>_NX`(`kiwoom_minute.kiwoom_stock_code`). 저장 종목 ID는
`NXT:<코드>` — `silver.minute_bars`에 KRX 봉과 같이 있지만 종목 ID가 달라 KRX 리더에
섞이지 않는다. NXT 1분봉 합은 `_NX` 일봉 거래량과 정확히 같았다(KRX 1분봉은 일부
체결을 빠뜨리는 것과 다르다).

표시
----
- `NXT_MAIN_VOLUME_INCLUDED` — NXT 메인마켓 거래량을 더했다(그날 NXT 1분봉을 받았다)
- `NXT_VOLUME_MISSING` — NXT 거래 종목인데 그날 NXT 1분봉을 아직 못 받았다. 거래량은
  KRX만이다. 확정 복원이 나중에 NXT 1분봉을 받으면 다시 만든다(`regular_from_minute`)

NXT 거래 종목은 지수 구성종목 일부뿐이다(2026-10 실측 351개 중 299개). 판정은 최근
90일 KIS NX 체결량 행(`silver.daily_trade_sides`)과 최근 30일 NXT 1분봉 커버리지의
합집합이다.

한계
----
- 확정 봉에 NXT를 더하는 것은 애프터마켓 시행(2026-09-14) 이후 날만이다. 그 전 날의
  정규장 일봉(키움 원본)은 KRX 거래량만이다 — 거래량 시계열의 정의가 9/14 앞뒤로
  바뀐다. 2026-10-29에 2026-08-03~09-11은 NXT 1분봉 소급(`nxt_history`)으로 채웠다 —
  경계는 2026-08-03이다
- 키움 분봉 API는 페이지 중간에 간헐적으로 응답을 주지 않는다. 받지 못한 날은
  `NXT_VOLUME_MISSING`으로 남고 다음 적재가 메운다

수정주가 계수(2026-10-26)
------------------------
확정 복원은 저장된 1분봉이 지금 일봉과 기준이 다르면 계수 f(= 일봉 시가 ÷ 첫 분봉 시가)로
KRX 애프터마켓 거래량을 나눠 맞춘다. NXT 1분봉도 같은 날 같은 기준으로 받았으므로 같은
f로 나눠야 한다 — 예전에는 그대로 더했다(290650 1:2 분할: 9/14~10/23 26일의 NXT 몫이
절반만 들어갔다, 10/23 123,377주 → 245,527주). 나눠 더한 봉은 `NXT_VOLUME_RESCALED`로
표시하고, 이 표시 없이 계수가 걸린 옛 봉은 복원 단계가 다시 만든다.
"""

from __future__ import annotations

from datetime import date, time, timedelta

from ..domain.ids import KST
from ..storage.parquet_store import ParquetStore

__all__ = ["NXT_MAIN_START", "NXT_MAIN_END", "FLAG_NXT_INCLUDED",
           "FLAG_NXT_MISSING", "NXT_START", "nxt_id", "krx_id",
           "FLAG_NXT_RESCALED", "nxt_traded", "nxt_main_volume", "apply_nxt"]

NXT_MAIN_START = time(9, 0)
NXT_MAIN_END = time(15, 30)
FLAG_NXT_INCLUDED = "NXT_MAIN_VOLUME_INCLUDED"
FLAG_NXT_MISSING = "NXT_VOLUME_MISSING"
FLAG_NXT_RESCALED = "NXT_VOLUME_RESCALED"
#: 넥스트레이드 개시일
NXT_START = date(2025, 3, 4)
_SIDES_LOOKBACK = 90
_COVERAGE_LOOKBACK = 30


def nxt_id(security_id: str) -> str:
    """`KRX:005930` → `NXT:005930`."""
    return "NXT:" + str(security_id).split(":")[-1]


def krx_id(security_id: str) -> str:
    return "KRX:" + str(security_id).split(":")[-1]


def _coverage_pairs(store, start, end, nxt_ids=None) -> set:
    """NXT 1분봉을 받은 (NXT 종목, 날짜) — 커버리지 행이 있으면 받은 것이다."""
    import pyarrow.dataset as ds
    from ..contracts.market_schema import BAR_COVERAGE_PK
    from ..domain.errors import NoInputDataError

    flt = ((ds.field("dataset") == "minute") & (ds.field("period_min") == 1)
           & (ds.field("trading_date") >= start) & (ds.field("trading_date") <= end))
    try:
        t = store.read_arrow("silver", "bar_coverage",
                             columns=["dataset", "security_id", "period_min",
                                      "trading_date", "session", "run_id"],
                             filters=flt, dedupe_pk=BAR_COVERAGE_PK)
    except NoInputDataError:
        return set()
    out = set()
    for sid, d in zip(t.column("security_id").to_pylist(),
                      t.column("trading_date").to_pylist()):
        if sid.startswith("NXT:") and (nxt_ids is None or sid in nxt_ids):
            out.add((sid, d))
    return out


def nxt_traded(store: ParquetStore, securities, as_of: date) -> set:
    """NXT에서 거래되는 종목(KRX 종목 ID) — KIS NX 체결량 ∪ NXT 1분봉 커버리지."""
    import pyarrow.dataset as ds
    from ..domain.errors import NoInputDataError

    store = store or ParquetStore()
    wanted = {krx_id(s) for s in securities}
    out = set()
    try:
        from .kis_trade_sides import read_trade_sides
        t = read_trade_sides(
            store, columns=["security_id", "trading_date", "market_div"],
            filters=(ds.field("market_div") == "NX")
            & (ds.field("trading_date") >= as_of - timedelta(days=_SIDES_LOOKBACK)))
        out |= set(t.column("security_id").to_pylist()) & wanted
    except NoInputDataError:
        pass
    for sid, _ in _coverage_pairs(store, as_of - timedelta(days=_COVERAGE_LOOKBACK),
                                  as_of):
        if krx_id(sid) in wanted:
            out.add(krx_id(sid))
    return out


def nxt_main_volume(store: ParquetStore, start: date, end: date, securities) -> dict:
    """{(KRX 종목 ID, 날짜): NXT 메인마켓 거래량} — 그날 NXT 1분봉을 받은 쌍만.

    받았는데 메인마켓 봉이 없으면 0이다(받지 못한 쌍과 구별된다).
    """
    import pyarrow.dataset as ds
    from ..contracts.market_schema import MINUTE_BAR_LOGICAL_KEY
    from ..domain.errors import NoInputDataError

    store = store or ParquetStore()
    ids = {nxt_id(s) for s in securities}
    fetched = _coverage_pairs(store, start, end, ids)
    out = {(krx_id(sid), d): 0 for sid, d in fetched}
    if not fetched:
        return out
    try:
        t = store.read_arrow(
            "silver", "minute_bars",
            columns=["security_id", "period_min", "trading_date", "session",
                     "bar_start_at_utc", "volume", "quality_status", "run_id",
                     "data_revision"],
            filters=((ds.field("period_min") == 1)
                     & (ds.field("trading_date") >= start)
                     & (ds.field("trading_date") <= end)
                     & ds.field("security_id").isin(sorted({s for s, _ in fetched}))),
            latest_by=MINUTE_BAR_LOGICAL_KEY)
    except NoInputDataError:
        return out
    for sid, d, at, vol, status in zip(
            t.column("security_id").to_pylist(), t.column("trading_date").to_pylist(),
            t.column("bar_start_at_utc").to_pylist(), t.column("volume").to_pylist(),
            t.column("quality_status").to_pylist()):
        if status == "FAILED" or (sid, d) not in fetched:
            continue
        hm = at.astimezone(KST).time()
        if NXT_MAIN_START <= hm < NXT_MAIN_END:
            out[(krx_id(sid), d)] += int(vol or 0)
    return out


def apply_nxt(volume: int, flags: list, key, nxt_volumes: dict, traded: set,
              factor: float = 1.0):
    """KRX 거래량 + NXT 메인마켓 → (거래량, 플래그).

    `factor`: 복원 봉의 수정주가 계수 f — NXT 1분봉 거래량을 f로 나눠 일봉 기준에 맞춘다.
    """
    flags = [f for f in flags
             if f not in (FLAG_NXT_INCLUDED, FLAG_NXT_MISSING, FLAG_NXT_RESCALED)]
    if key in nxt_volumes:
        f = float(factor or 1.0)
        add = [FLAG_NXT_INCLUDED]
        if abs(f - 1.0) > 1e-4:
            add.append(FLAG_NXT_RESCALED)
        return (int(volume) + int(round(int(nxt_volumes[key]) / f)),
                sorted(flags + add))
    if key[0] in traded and key[1] >= NXT_START:
        return int(volume), sorted(flags + [FLAG_NXT_MISSING])
    return int(volume), sorted(flags)

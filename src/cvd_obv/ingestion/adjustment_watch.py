# -*- coding: utf-8 -*-
"""수정주가 소급 감지 — 같은 날 일봉을 다시 받았는데 가격이 바뀐 종목을 찾는다.

왜 필요한가 (2026-10-21 실측)
----------------------------
키움 일봉(`upd_stkpc_tp=1`)은 **조회 기준일(`base_dt`)까지의 수정주가**로 준다.
207940은 2026-10-02에 주식 수가 바뀌는 이벤트가 있었고, 10/02 이후를 기준으로 받으면
그 전 가격이 모두 ×0.9923이 된다. 그런데 적재는 최근 며칠만 다시 받으므로

- 9/18에 받은 9/11 이전 이력: 조정 **전**
- 10/03 이후 다시 받은 9/14 이후: 조정 **후**

가 저장소에 섞여 9/11→9/14 사이에 약 0.8%의 가짜 가격 단절이 생겼다. 정규장 복원 봉도
옛 원본으로 만들어져 원본과 어긋났다.

방법
----
같은 (종목, 날짜, price_scope)를 두 번 이상 받았으면, 가장 늦게 받은 값과 그 직전에 받은
값의 종가를 비교한다. 다르면(비율 ≠ 1) 그 사이에 수정주가가 소급됐다는 뜻이다 — 그 종목은
**일봉 이력 전체를 다시 받아** 한 기준으로 맞춘다(`cvdobv.py daily-cache readjust`).
다시 받은 뒤에는 최근 두 값이 같아지므로 같은 종목을 반복해서 다시 받지 않는다.

원천이 다르면 비교하지 않는다(2026-10-23 KIS 전환)
-------------------------------------------------
2026-09-14 이후 키움 일봉 종가는 애프터마켓 최종가, KIS 일봉 종가는 정규장 종가다 —
같은 `EXTENDED` 행이라도 원천이 바뀌면 종가가 달라진다. 그래서 같은 원천의 수집끼리만
비교한다. `audit_against_api`의 원천 간 대조는 2026-09-11 이전 날만, 원 단위 반올림
차이(1원)는 넘어가서 본다(실측 035720 2020-01-02 KIS 30,609 vs 키움 30,607).

확정 전에 받은 행은 시간외 가격제한폭 안의 변화를 넘어간다(2026-10-26)
------------------------------------------------------------------------
KIS 일봉의 **가장 최근 거래일 종가는 다음 거래일 전까지 바뀐다** — 005930 2026-10-23을
10-24(토) 08:44에 받으면 213,500(시간외 가격), 10-26(월) 08:34에 받으면 215,500(정규장
종가). 이 차이를 소급으로 보면 지수 구성종목 323종목이 한꺼번에 "소급"으로 잡혀 전체
이력을 다시 받는다(2026-10-26 08:30 실측, 실제 이벤트는 290650 1:2 하나).

그래서 그날 뒤 **첫 평일 09:00(KST) 전**에 받은 행(`_settled`가 거짓)이 비교에 끼면
시간외 단일가 가격제한폭(±10%)을 넘는 변화만 소급으로 본다(`UNSETTLED_RATIO_TOL`).
분할·병합 같은 큰 이벤트는 그대로 바로 잡히고, 작은 소급(207940 ×0.9923 같은)은 확정 뒤
두 번 받은 앞선 날에서 잡힌다 — 일일 적재는 그래서 일봉을 확정일보다 며칠 앞에서부터
받는다(`cli.main.daily_load_steps`).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from ..storage.parquet_store import ParquetStore

__all__ = ["KIWOOM_DAILY_SOURCE", "RATIO_TOL", "UNSETTLED_RATIO_TOL", "adjustment_events",
           "audit_against_api"]

KIWOOM_DAILY_SOURCE = "KIWOOM_KA10081"
#: 원천 간 대조가 의미 있는 마지막 날 — 그 뒤는 원천마다 종가 정의가 다르다
_CROSS_SOURCE_LAST = date(2026, 9, 11)
#: 종가 비율이 이만큼 넘게 1에서 벗어나면 소급으로 본다(호가 단위 반올림보다 크다)
RATIO_TOL = 1e-4
#: 확정 전 수집이 낀 비교의 허용 폭 — 시간외 단일가 가격제한폭(정규장 종가 ±10%)보다 크게
UNSETTLED_RATIO_TOL = 0.105


def _settled(trading_date: date, fetched_at) -> bool:
    """그날 뒤 첫 평일 09:00(KST)에 또는 그 뒤에 받았는가 — 그 전 종가는 바뀔 수 있다.

    휴장일은 모른다(평일로 본다). 수집 시각을 모르면 확정으로 본다(예전 행).
    """
    from ..domain.ids import KST

    if fetched_at is None:
        return True
    nxt = trading_date + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return fetched_at.astimezone(KST) >= datetime.combine(nxt, time(9, 0), tzinfo=KST)


def adjustment_events(store: ParquetStore, start: date, end: date,
                      securities=None) -> dict:
    """{종목 ID: {"date", "scope", "old_close", "new_close", "ratio"}} — 가장 이른 날 기준.

    리비전 정리를 하지 않고 모든 수집 행을 읽는다(`latest_by` 없이).
    """
    import pyarrow.dataset as ds
    from ..domain.errors import NoInputDataError

    from .market_source import DAILY_SOURCE_IDS

    store = store or ParquetStore()
    flt = ((ds.field("trading_date") >= start) & (ds.field("trading_date") <= end)
           & ds.field("source").isin(list(DAILY_SOURCE_IDS))
           & (ds.field("adjustment_type") == "SPLIT_ADJUSTED"))
    if securities is not None:
        flt = flt & ds.field("security_id").isin(sorted(securities))
    try:
        t = store.read_arrow(
            "silver", "daily_bars",
            columns=["security_id", "trading_date", "price_scope", "close",
                     "adjustment_as_of_utc", "run_id", "source"], filters=flt)
    except NoInputDataError:
        return {}
    fetched = {}
    for sid, d, scope, close, at, run, src in zip(
            t.column("security_id").to_pylist(), t.column("trading_date").to_pylist(),
            t.column("price_scope").to_pylist(), t.column("close").to_pylist(),
            t.column("adjustment_as_of_utc").to_pylist(), t.column("run_id").to_pylist(),
            t.column("source").to_pylist()):
        if close is None or float(close) <= 0:
            continue
        # 수집 시각을 모르면 run_id 순서로 대신한다. 원천이 다르면 따로 본다
        fetched.setdefault((sid, d, scope, src), []).append(
            ((at.isoformat() if at else ""), run, float(close), _settled(d, at)))
    events = {}
    for (sid, d, scope, _src), rows in fetched.items():
        if len({r[1] for r in rows}) < 2:
            continue
        rows.sort()
        new = rows[-1]
        prev = next((r for r in reversed(rows[:-1]) if r[1] != new[1]), None)
        if prev is None:
            continue
        ratio = new[2] / prev[2]
        tol = RATIO_TOL if (new[3] and prev[3]) else UNSETTLED_RATIO_TOL
        if abs(ratio - 1.0) <= tol:
            continue
        if sid not in events or d < events[sid]["date"]:
            events[sid] = {"date": d, "scope": scope, "old_close": prev[2],
                           "new_close": new[2], "ratio": ratio}
    return events


def audit_against_api(codes, store: ParquetStore = None, source=None,
                      start: date = None, log=None):
    """저장된 원본 일봉 종가를 **지금** 적재 원천의 수정주가와 대조한다 — 한 번만 받은 이력도 본다.

    `adjustment_events`는 같은 날을 두 번 이상 받은 경우만 비교할 수 있다. 이력을 한 번만
    받은 종목에 그 뒤 수정주가 이벤트가 있었다면 여기서만 드러난다. 종목당 한 번 호출
    (`start`까지, 기본 최근 약 2년). 돌려주는 값: (어긋난 종목 dict, 받지 못한 종목 list).
    """
    import pyarrow.dataset as ds
    from datetime import timedelta
    from ..contracts.market_schema import DAILY_BAR_LOGICAL_KEY
    from .market_source import DAILY_SOURCE_IDS, daily_source

    store = store or ParquetStore()
    if source is None:
        source = daily_source()
    live_source = getattr(source, "SOURCE", KIWOOM_DAILY_SOURCE)
    start = start or (date.today() - timedelta(days=730))
    token = source.token()
    events, failed = {}, []
    for i, code in enumerate(codes, 1):
        sid = f"KRX:{code}"
        res = source.fetch_security(code, token=token, start=start)
        live = {r["trading_date"]: float(r["close"]) for r in (res.rows or [])}
        if not live or res.status == "FAILED":
            # 받지 못했으면 "어긋남 없음"이 아니라 "대조 못 함"이다(2026-10-21 실측: API
            # 무응답을 조용히 건너뛰어 351종목 대조가 사실상 0건이었다)
            failed.append(code)
            continue
        t = store.read_arrow(
            "silver", "daily_bars",
            columns=["security_id", "trading_date", "price_scope", "adjustment_type",
                     "close", "source", "run_id", "data_revision"],
            filters=(ds.field("security_id") == sid) & (ds.field("trading_date") >= start)
            & ds.field("source").isin(list(DAILY_SOURCE_IDS)),
            latest_by=DAILY_BAR_LOGICAL_KEY)
        first = None
        for d, scope, adj, close, src in zip(
                t.column("trading_date").to_pylist(), t.column("price_scope").to_pylist(),
                t.column("adjustment_type").to_pylist(), t.column("close").to_pylist(),
                t.column("source").to_pylist()):
            if adj != "SPLIT_ADJUSTED" or d not in live or not close:
                continue
            same = src == live_source
            if not same and d > _CROSS_SOURCE_LAST:
                continue        # 원천마다 종가 정의가 다르다
            ratio = live[d] / float(close)
            # 원천이 다르면 원 단위 반올림 차이(1원)는 소급으로 보지 않는다
            tol = RATIO_TOL if same else max(RATIO_TOL, 1.0 / float(close))
            if abs(ratio - 1.0) > tol and (first is None or d < first["date"]):
                first = {"date": d, "scope": scope, "old_close": float(close),
                         "new_close": live[d], "ratio": ratio}
        if first:
            events[sid] = first
        if log and i % 50 == 0:
            log(f"  대조 {i}/{len(codes)} — 어긋난 종목 {len(events)} · 대조 못 함 {len(failed)}")
    return events, failed

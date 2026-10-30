# -*- coding: utf-8 -*-
"""봉 기준 `bars-1.0` — 모든 스캐너·백테스트·적재가 따르는 단일 정의.

왜 필요한가 (2026-10-03 점검, `docs/BAR_STANDARD.md`)
----------------------------------------------------
스캐너마다 봉을 다르게 읽고 있었다.

- 일봉: V1.5·데일리 스캐너·CVD 가속은 키움 API 일봉을 그대로 썼다. 2026-09-14
  이후 그 봉은 애프터마켓까지 반영한 `EXTENDED`(종가 = 20:00 최종 체결가)다.
  종가매수·A/B 선정기는 정규장(`REGULAR`)으로 복원해 썼다. 같은 "MA20"이
  도구마다 다른 종가로 계산됐다.
- 분봉: V1.5는 API 5분봉, CVD 가속은 1분봉, replay는 저장된 5분봉(2026-09-22에
  수집 중단)을 읽었다.
- 적재: 분봉은 16:10, 일봉은 아무 때나, 정규장 복원은 수동으로 돌았다.

기준
----
| 항목 | 기준 |
|---|---|
| 일봉 | 정규장(`REGULAR`) · 수정주가(`SPLIT_ADJUSTED`). 2026-09-14 이후는 1분봉으로 복원한 정규장 봉(`DERIVED_MINUTE_1M`) 또는 15:35~16:00에 받은 당일 봉 |
| `EXTENDED` 일봉 | 판정에 쓰지 않는다. 쓰려면 `allow_extended=True`로 명시하고 결과에 표시한다 |
| 분봉 원천 | 1분봉 하나만 적재·수집한다(`STORED_MINUTE_PERIOD`) |
| N분봉 | 1분봉을 합성한다(`resample_*`). 실측 일치율 99.97%(키움 원본 5분봉 대비) |
| 신호 세션 | 정규장(`REGULAR`) |
| 당일 일봉 | 정규장 확정(15:35) 뒤, 애프터마켓 시작(16:00) 전에 받은 것만 정규장 봉이다 |
| 적재 | 2단계. ① 16:10 **잠정** 일봉: 당일 1분봉 합(거래량 근사, `PROVISIONAL_SOURCE`, 리비전 0) ② 익일 08:30 **확정**: 애프터마켓 확정 뒤 정규장 복원(리비전 1이 잠정을 덮는다). 두 작업 모두 마지막 확정 이후 밀린 날을 먼저 확정한다 |

이 모듈은 저장소를 직접 읽는 함수도 담는다. 상수만 따로 두면 "읽는 방법"이
도구마다 다시 갈라진다.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from .domain.errors import ConfigError
from .domain.ids import KST
from .domain.market_rules import default_registry
from .domain.status import Session

__all__ = [
    "BAR_STANDARD_VERSION", "STORED_MINUTE_PERIOD", "SIGNAL_SESSION",
    "DAILY_PRICE_SCOPE", "EXTENDED_SCOPE", "DAILY_ADJUSTMENT",
    "AFTERMARKET_START", "DAILY_LOAD_KST", "PROVISIONAL_LOAD_KST",
    "PROVISIONAL_SOURCE", "PROVISIONAL_REVISION", "SUPPORTED_PERIODS",
    "FETCH_REGULAR", "FETCH_EXTENDED", "FETCH_INCOMPLETE",
    "classify_fetched_daily", "select_standard_daily", "read_standard_daily",
    "StandardDailyCache", "unrestored_days", "provisional_days",
    "is_provisional", "resample_minute_table",
    "ScanDay", "resolve_scan_day", "NoScanDataError",
    "resample_bars",
    "describe",
]

BAR_STANDARD_VERSION = "bars-1.0"

#: 적재·수집하는 유일한 분봉 주기. 다른 주기는 이것을 합성한다.
STORED_MINUTE_PERIOD = 1
#: 지표 계산에 쓰는 세션.
SIGNAL_SESSION = Session.REGULAR
#: 일봉 기준.
DAILY_PRICE_SCOPE = "REGULAR"
EXTENDED_SCOPE = "EXTENDED"
DAILY_ADJUSTMENT = "SPLIT_ADJUSTED"
#: 애프터마켓 시행일 — 이날부터 키움 일봉은 EXTENDED다.
AFTERMARKET_START = date(2026, 9, 14)
#: 확정 적재 스케줄(KST, **익일** 오전). 애프터마켓 확정(20:00 + 여유 5분) 뒤면
#: 언제든 되지만, 저녁에 못 돌리는 날이 있어 다음 날 장 시작 전으로 정했다.
DAILY_LOAD_KST = time(8, 30)
#: 잠정 적재 스케줄(KST, 당일). 장후 시간외 종가(~16:00)가 끝난 뒤.
PROVISIONAL_LOAD_KST = time(16, 10)
#: 잠정 일봉 — 당일 1분봉(정규장 + 15:35~16:00 시간외 종가) 합. 가격은 정규장 값과
#: 같지만 거래량은 분봉이 빠뜨린 체결만큼 작을 수 있다(9/1~10/2 실측 하위 1% 0.77배).
#: 리비전 0이라 같은 날 확정 봉(리비전 1)이 들어오면 reader가 확정 봉을 고른다.
PROVISIONAL_SOURCE = "DERIVED_MINUTE_1M_PROVISIONAL"
PROVISIONAL_REVISION = 0
#: 1분봉으로 합성할 수 있는 주기 — 시계 정각에 맞물리는 것만.
#: 45분처럼 60을 나누지 못하는 주기는 키움이 경계를 어디에 두는지 실측하지 않았다.
SUPPORTED_PERIODS = (1, 2, 3, 5, 10, 15, 20, 30, 60)

#: API로 받은 일봉 한 개의 성격(`classify_fetched_daily`)
FETCH_REGULAR = "REGULAR"        # 정규장 확정값
FETCH_EXTENDED = "EXTENDED"      # 애프터마켓까지 확정된 값
FETCH_INCOMPLETE = "INCOMPLETE"  # 아직 확정되지 않은 값 — 저장·판정 금지

_DAILY_KEY_COLUMNS = ("security_id", "trading_date", "price_scope",
                      "adjustment_type", "quality_status")


def describe() -> dict:
    """스냅샷·리포트에 남길 기준 요약."""
    return {
        "bar_standard": BAR_STANDARD_VERSION,
        "daily": f"{DAILY_PRICE_SCOPE}/{DAILY_ADJUSTMENT}",
        "minute_source_period": STORED_MINUTE_PERIOD,
        "signal_session": SIGNAL_SESSION,
    }


# ─────────────────────────────────────────────────────────────
# API 일봉 — 받은 시각으로 성격을 정한다
# ─────────────────────────────────────────────────────────────

def _as_kst(at: datetime) -> datetime:
    """naive면 KST로 본다(도구들의 `datetime.now()`는 KST 로컬 시각이다)."""
    return at.replace(tzinfo=KST) if at.tzinfo is None else at.astimezone(KST)


def classify_fetched_daily(trading_date: date, fetched_at: datetime,
                           registry=None, market: str = "KRX") -> str:
    """`fetched_at`에 받은 `trading_date` 일봉이 무엇을 담고 있는가.

    - 그날 일봉 확정 시각(애프터마켓 시행 후 20:05, 전 15:35) 뒤 → 그날의 scope
      (`REGULAR` 또는 `EXTENDED`)
    - 정규장 확정(15:35) 뒤 ~ 애프터마켓 시작(16:00) 전 → `REGULAR`.
      애프터마켓 체결이 아직 없으므로 종가·거래량이 정규장 값이다
    - 그 밖(장중, 애프터마켓 진행 중) → `INCOMPLETE`

    ⚠ 미확인: 15:40~16:00 장후 시간외 종가매매 체결이 이 시간대 일봉 거래량에
    들어가는지 실측하지 않았다. 들어간다면 거래량이 정규장보다 약간 크다.
    """
    registry = registry or default_registry()
    rule = registry.resolve(market, trading_date)
    fetched = _as_kst(fetched_at)
    scope = rule.daily_scope_of(trading_date)
    if fetched >= rule.daily_confirmed_at(trading_date):
        return scope
    if rule.daily_only:
        return FETCH_INCOMPLETE
    if fetched < rule.session_complete_at(trading_date, Session.REGULAR):
        return FETCH_INCOMPLETE
    if scope == EXTENDED_SCOPE:
        after = rule.session_bounds(trading_date, Session.AFTER)
        if after is not None and fetched < after[0]:
            return FETCH_REGULAR
    return FETCH_INCOMPLETE


# ─────────────────────────────────────────────────────────────
# 저장된 일봉 — 한 (종목, 날짜)에 한 행
# ─────────────────────────────────────────────────────────────

def _fill_traded_value(regular, extended):
    """거래대금이 비어 있는 정규장 봉을 추정으로 채우고 `traded_value_est` 열을 붙인다.

    1분봉 복원 봉은 거래대금이 없다(분봉이 주지 않는다). 추정 규칙은 하나다:
      1순위 같은 날 EXTENDED 공식 거래대금 × (정규장 거래량 / EXTENDED 거래량)
            — 체결가 분포를 반영한다
      2순위 종가 × 거래량 — V1.5 `calc_liquidity`와 같은 근사
    """
    import pyarrow as pa
    import pyarrow.compute as pc

    tv = regular.column("traded_value")
    missing = pc.is_null(tv)
    n_missing = pc.sum(pc.cast(missing, "int64")).as_py() or 0
    values = pc.cast(tv, pa.float64()).to_pylist()
    est = [False] * regular.num_rows
    if n_missing:
        ext = {}
        if extended is not None and extended.num_rows:
            for sid, d, v, t in zip(extended.column("security_id").to_pylist(),
                                    extended.column("trading_date").to_pylist(),
                                    extended.column("volume").to_pylist(),
                                    extended.column("traded_value").to_pylist()):
                if t is not None and v:
                    ext[(sid, d)] = (float(t), int(v))
        idx = [i for i, m in enumerate(missing.to_pylist()) if m]
        sids = regular.column("security_id").to_pylist()
        days = regular.column("trading_date").to_pylist()
        closes = regular.column("close").to_pylist()
        vols = regular.column("volume").to_pylist()
        for i in idx:
            got = ext.get((sids[i], days[i]))
            values[i] = (got[0] * int(vols[i]) / got[1] if got
                         else float(closes[i]) * int(vols[i]))
            est[i] = True
    pos = regular.schema.get_field_index("traded_value")
    regular = regular.set_column(pos, "traded_value",
                                 pa.array(values, type=pa.float64()))
    return regular.append_column("traded_value_est",
                                 pa.array(est, type=pa.bool_()))


def select_standard_daily(table, allow_extended: bool = False,
                          fill_traded_value: bool = False):
    """`silver.daily_bars` Arrow 테이블 → 기준 일봉만.

    입력은 논리 키별 최신 리비전으로 이미 정리돼 있어야 한다
    (`read_arrow(latest_by=DAILY_BAR_LOGICAL_KEY)`). 그래도 같은 날에
    `REGULAR`(복원)와 `EXTENDED`(원본)가 둘 다 남는다 — 논리 키에 `price_scope`가
    들어 있기 때문이다. 여기서 하루 한 행으로 줄인다.

    `allow_extended=True`면 `REGULAR`가 없는 날만 `EXTENDED`로 채운다. 결과의
    `price_scope` 열로 어느 봉이 섞였는지 드러난다.

    `fill_traded_value=True`면 빈 거래대금을 하나의 규칙으로 추정해 채우고
    `traded_value_est` 열을 붙인다(`_fill_traded_value`). 입력에 `traded_value`,
    `close`, `volume` 열이 있어야 한다.
    """
    import pyarrow as pa
    import pyarrow.compute as pc

    table = table.filter(
        pc.equal(table.column("adjustment_type"), DAILY_ADJUSTMENT))
    scope = table.column("price_scope")
    regular = table.filter(pc.equal(scope, DAILY_PRICE_SCOPE))
    extended = table.filter(pc.equal(scope, EXTENDED_SCOPE))
    if fill_traded_value:
        regular = _fill_traded_value(regular, extended)
    if not allow_extended:
        return regular
    if extended.num_rows == 0:
        return regular
    if fill_traded_value:
        extended = _fill_traded_value(extended, None)
    first = pc.min(extended.column("trading_date"))
    recent = regular.filter(
        pc.greater_equal(regular.column("trading_date"), first))
    have = set(zip(recent.column("security_id").to_pylist(),
                   recent.column("trading_date").to_pylist()))
    keep = [(s, d) not in have for s, d in
            zip(extended.column("security_id").to_pylist(),
                extended.column("trading_date").to_pylist())]
    extended = extended.filter(pa.array(keep, type=pa.bool_()))
    return pa.concat_tables([regular, extended]).sort_by(
        [("security_id", "ascending"), ("trading_date", "ascending")])


def read_standard_daily(store=None, columns=None, filters=None,
                        allow_extended: bool = False, drop_failed: bool = False,
                        fill_traded_value: bool = False):
    """기준 일봉을 읽는다 — 모든 일봉 reader의 단일 입구.

    `columns`에 없더라도 판정에 필요한 열(`price_scope` 등)은 함께 읽는다.
    `drop_failed=False`(기본)면 품질 FAILED 행을 남긴다 — 제외 건수를 직접
    세는 reader(feature·label)가 있기 때문이다.
    """
    import pyarrow.compute as pc
    from .contracts.market_schema import DAILY_BAR_LOGICAL_KEY
    from .storage.parquet_store import ParquetStore

    store = store or ParquetStore()
    cols = None
    if columns is not None:
        extra = list(_DAILY_KEY_COLUMNS)
        if fill_traded_value:
            extra += ["traded_value", "close", "volume"]
        cols = list(dict.fromkeys(list(columns) + extra))
    table = store.read_arrow("silver", "daily_bars", columns=cols,
                             filters=filters, latest_by=DAILY_BAR_LOGICAL_KEY)
    table = select_standard_daily(table, allow_extended=allow_extended,
                                  fill_traded_value=fill_traded_value)
    if drop_failed:
        table = table.filter(
            pc.not_equal(table.column("quality_status"), "FAILED"))
    return table


class NoScanDataError(RuntimeError):
    """스캔에 쓸 적재 일봉이 없다."""


def is_provisional(source) -> bool:
    """잠정 일봉(16:10, 1분봉 합)인가 — 확정 정규장 봉이 아니다."""
    return source == PROVISIONAL_SOURCE


class ScanDay:
    """스캔 기준 거래일과 그 봉의 상태. `resolve_scan_day()`가 만든다."""

    __slots__ = ("day", "provisional", "cutoff_passed", "note")

    def __init__(self, day: date, provisional: bool, cutoff_passed: bool,
                 note: str = ""):
        self.day = day
        self.provisional = bool(provisional)
        self.cutoff_passed = bool(cutoff_passed)
        self.note = note

    @property
    def end_of_day(self) -> datetime:
        """그 거래일의 끝 — 분봉을 자를 기준. 실행 시각과 무관하다."""
        return datetime.combine(self.day, time(23, 59, 59))

    @property
    def label(self) -> str:
        return "잠정" if self.provisional else "확정"

    def __repr__(self):
        return (f"ScanDay({self.day}, {self.label}, "
                f"cutoff_passed={self.cutoff_passed})")


def resolve_scan_day(now: datetime = None, store=None,
                     securities=None) -> ScanDay:
    """스캔 기준 거래일을 **적재 상태**로 정한다 — 실행 시각으로 자르지 않는다.

    규칙(2026-10-26 확정):

        16:10(`PROVISIONAL_LOAD_KST`) 이전 → **오늘을 뺀** 마지막 적재 거래일
        16:10 이후                        → 오늘 포함 마지막 적재 거래일

    왜: 장중에 "지금까지의 분봉"을 쓰면 **몇 시에 돌렸느냐에 따라 결과가
    달라진다.** 적재가 끝난 봉만 보면 같은 날 몇 번을 돌려도 같은 값이 나오고,
    저장된 봉과 그대로 대조된다.

    16:10이 지났는데 오늘 봉이 아직 없으면(적재 지연·휴장) **전 거래일로
    물러난다** — 없는 날을 만들어 내지 않는다. 그 사실은 `note`에 적는다.
    """
    import pyarrow.dataset as ds

    now = _as_kst(now or datetime.now())
    today = now.date()
    passed = now.time() >= PROVISIONAL_LOAD_KST

    flt = ((ds.field("trading_date") > today - timedelta(days=40))
           & (ds.field("trading_date") <= today))
    if securities:
        flt = flt & ds.field("security_id").isin(sorted(securities))
    try:
        table = read_standard_daily(store, columns=["source"], filters=flt,
                                    allow_extended=False, drop_failed=True)
        rows = list(zip(table.column("trading_date").to_pylist(),
                        table.column("source").to_pylist()))
    except Exception as e:
        from .domain.errors import NoInputDataError
        if not isinstance(e, NoInputDataError):
            raise
        rows = []
    if not rows:
        raise NoScanDataError(
            f"최근 40일 기준 일봉이 없다 — 적재 상태를 확인하라 (기준 {today})")

    by_day = {}
    for d, src in rows:
        by_day.setdefault(d, []).append(src)
    days = sorted(by_day)
    allowed = [d for d in days if (d <= today if passed else d < today)]
    if not allowed:
        raise NoScanDataError(
            f"{'16:10 이후' if passed else '16:10 이전'}에 쓸 적재 거래일이 없다 "
            f"(저장분 마지막 {days[-1]}, 오늘 {today})")

    day = allowed[-1]
    prov = any(is_provisional(s) for s in by_day[day])
    note = ""
    if passed and day != today:
        note = (f"16:10이 지났지만 오늘({today}) 봉이 아직 없다 — "
                f"{day} 봉으로 스캔한다. 16:10 잠정 적재를 확인하라")
    elif not passed:
        note = (f"16:10 이전이라 오늘({today})을 빼고 "
                f"{day} {'잠정' if prov else '확정'} 봉으로 스캔한다")
    return ScanDay(day, prov, passed, note)


def _daily_scopes(store, start, end, securities):
    """[(security_id, trading_date, price_scope, source)] — 논리 키별 최신 리비전."""
    import pyarrow.compute as pc
    import pyarrow.dataset as ds
    from .contracts.market_schema import DAILY_BAR_LOGICAL_KEY
    from .domain.errors import NoInputDataError
    from .storage.parquet_store import ParquetStore

    store = store or ParquetStore()
    flt = ds.field("trading_date") >= (start or AFTERMARKET_START)
    if end is not None:
        flt = flt & (ds.field("trading_date") <= end)
    if securities is not None:
        flt = flt & ds.field("security_id").isin(sorted(securities))
    try:
        table = store.read_arrow(
            "silver", "daily_bars",
            columns=list(_DAILY_KEY_COLUMNS) + ["source"],
            filters=flt, latest_by=DAILY_BAR_LOGICAL_KEY)
    except NoInputDataError:
        return []
    table = table.filter(
        pc.equal(table.column("adjustment_type"), DAILY_ADJUSTMENT))
    return list(zip(table.column("security_id").to_pylist(),
                    table.column("trading_date").to_pylist(),
                    table.column("price_scope").to_pylist(),
                    table.column("source").to_pylist()))


def unrestored_days(store=None, start: date = None, end: date = None,
                    securities=None) -> dict:
    """{날짜: 종목 수} — 확정 일봉(EXTENDED)은 있는데 **확정** 정규장 봉이 없는
    (종목, 날짜).

    잠정 봉(16:10)만 있는 날도 여기 들어간다 — 정규장 복원이 아직 남았다. 기준
    일봉 reader는 잠정 봉이 있으면 그것을, 없으면 그날을 건너뛴다.
    """
    have, ext = set(), set()
    for sid, d, scope, source in _daily_scopes(store, start, end, securities):
        if scope == DAILY_PRICE_SCOPE and not is_provisional(source):
            have.add((sid, d))
        elif scope == EXTENDED_SCOPE:
            ext.add((sid, d))
    out = {}
    for sid, d in ext - have:
        out[d] = out.get(d, 0) + 1
    return dict(sorted(out.items()))


def provisional_days(store=None, start: date = None, end: date = None,
                     securities=None) -> dict:
    """{날짜: 종목 수} — 기준 일봉이 아직 잠정 봉(거래량 근사)인 (종목, 날짜)."""
    final, prov = set(), set()
    for sid, d, scope, source in _daily_scopes(store, start, end, securities):
        if scope != DAILY_PRICE_SCOPE:
            continue
        (prov if is_provisional(source) else final).add((sid, d))
    out = {}
    for sid, d in prov - final:
        out[d] = out.get(d, 0) + 1
    return dict(sorted(out.items()))


class StandardDailyCache:
    """실시간 스캐너용 일봉 공급기 — 로컬 기준 일봉 + 당일 정규장 봉.

    V1.5 `fetch_daily_bars()`와 같은 형태(`{"dt": "YYYYMMDD", open, high, low,
    close, volume}`)를 돌려준다. 그래서 V1.5 계산 함수에 그대로 넣을 수 있다.

    - 과거 봉: `silver.daily_bars`의 기준 일봉. 종목마다 저장소를 다시 읽지
      않도록 생성 시 한 번 읽는다.
    - 당일 봉: `live_fetch(code)`가 준 API 봉 중 오늘 것을, 지금 시각이
      `classify_fetched_daily() == REGULAR`(15:35~16:00)일 때만 붙인다. 로컬에
      이미 오늘 봉이 있으면 로컬 것을 쓴다.
    """

    def __init__(self, codes, as_of: datetime, days: int = 400, store=None,
                 allow_extended: bool = False):
        import pyarrow.dataset as ds

        self.as_of = as_of
        end = _as_kst(as_of).date()
        start = end - timedelta(days=int(days))
        sids = sorted(f"KRX:{c}" for c in codes)
        flt = ((ds.field("trading_date") >= start)
               & (ds.field("trading_date") <= end)
               & ds.field("security_id").isin(sids))
        try:
            table = read_standard_daily(
                store, columns=["open", "high", "low", "close", "volume",
                                "traded_value"],
                filters=flt, allow_extended=allow_extended, drop_failed=True,
                fill_traded_value=True)
            rows = table.sort_by([("security_id", "ascending"),
                                  ("trading_date", "ascending")]).to_pylist()
        except Exception as e:      # 저장소가 비었으면 API 폴백 판단은 호출자가 한다
            from .domain.errors import NoInputDataError
            if not isinstance(e, NoInputDataError):
                raise
            rows = []
        self._bars = {}
        for r in rows:
            code = r["security_id"].split(":", 1)[-1]
            self._bars.setdefault(code, []).append({
                "dt": r["trading_date"].strftime("%Y%m%d"),
                "open": float(r["open"]), "high": float(r["high"]),
                "low": float(r["low"]), "close": float(r["close"]),
                "volume": int(r["volume"]),
                "traded_value": r["traded_value"],
                "traded_value_est": r["traded_value_est"],
                "scope": r["price_scope"],
            })
        self.today_scope = classify_fetched_daily(end, as_of)

    def latest_date(self, code):
        bars = self._bars.get(str(code)) or []
        return bars[-1]["dt"] if bars else None

    def bars(self, code, days: int = None, live_fetch=None) -> list:
        """기준 일봉(오름차순). `days`는 캘린더일 창."""
        code = str(code)
        out = list(self._bars.get(code) or [])
        today = _as_kst(self.as_of).strftime("%Y%m%d")
        if (live_fetch is not None and self.today_scope == FETCH_REGULAR
                and not (out and out[-1]["dt"] == today)):
            for b in live_fetch(code) or ():
                if str(b.get("dt", ""))[:8] == today:
                    out.append(dict(b, dt=today, scope=FETCH_REGULAR))
                    break
        if days is not None:
            lower = (_as_kst(self.as_of).date()
                     - timedelta(days=int(days))).strftime("%Y%m%d")
            out = [b for b in out if b["dt"] >= lower]
        return out


# ─────────────────────────────────────────────────────────────
# 분봉 — 1분봉에서 N분봉 합성
# ─────────────────────────────────────────────────────────────

def _check_period(period: int) -> int:
    period = int(period)
    if period not in SUPPORTED_PERIODS:
        raise ConfigError(
            f"{period}분봉은 1분봉으로 합성하지 않는다 — 지원 {SUPPORTED_PERIODS}")
    return period


def resample_minute_table(table, period: int):
    """`silver.minute_bars`(1분) Arrow 테이블 → N분봉 테이블.

    필요한 열: security_id, trading_date, session, bar_start_at_utc,
    open, high, low, close, volume. 결과에는 `bar_end_at_utc`와
    `period_min`이 붙는다.

    경계는 시계 정각 기준(09:00, 09:05 …)이다. 15:30 종가 단일가 봉은
    5·10·15·30분에서는 따로, 60분에서는 15:00 봉에 들어간다 — 키움 원본과
    같은 방식이다(V1.5 `expected_bars_per_day` 주석). 묶음의 세션은 첫 1분봉의
    세션이다. 늦게 찍힌 종가 체결(15:32 OTHER)이 15:30 정규장 봉에 합쳐진다.

    실측(2026-10-03, 10종목 2025-10~12 + 2026-09): 원본 5분봉 54,612개 중
    54,595개(99.97%)가 OHLCV까지 일치.
    """
    import pyarrow as pa
    import pyarrow.compute as pc

    period = _check_period(period)
    if period == 1:
        if "bar_end_at_utc" not in table.column_names:
            table = table.append_column(
                "bar_end_at_utc",
                pc.add(table.column("bar_start_at_utc"),
                       pa.scalar(timedelta(minutes=1))))
        return table
    bucket = pc.floor_temporal(table.column("bar_start_at_utc"),
                               multiple=period, unit="minute")
    work = table.select(["security_id", "trading_date", "session",
                         "bar_start_at_utc", "open", "high", "low", "close",
                         "volume"]).append_column("_bucket", bucket)
    # 저장 가격은 decimal이고, Arrow의 first/last 집계는 decimal을 받지 않는다.
    # 원화 가격은 정수라 float64로 바꿔도 값이 그대로다.
    for name in ("open", "high", "low", "close"):
        work = work.set_column(work.schema.get_field_index(name), name,
                               pc.cast(work.column(name), pa.float64()))
    work = work.sort_by([("security_id", "ascending"),
                         ("bar_start_at_utc", "ascending")])
    grouped = work.group_by(
        ["security_id", "trading_date", "_bucket"], use_threads=False
    ).aggregate([("session", "first"), ("open", "first"), ("high", "max"),
                 ("low", "min"), ("close", "last"), ("volume", "sum")])
    grouped = grouped.rename_columns({
        "_bucket": "bar_start_at_utc", "session_first": "session",
        "open_first": "open", "high_max": "high", "low_min": "low",
        "close_last": "close", "volume_sum": "volume"})
    end = pc.add(grouped.column("bar_start_at_utc"),
                 pa.scalar(timedelta(minutes=period)))
    grouped = grouped.append_column("bar_end_at_utc", end)
    grouped = grouped.append_column(
        "period_min", pa.array([period] * grouped.num_rows, type=pa.int32()))
    return grouped.sort_by([("trading_date", "ascending"),
                            ("security_id", "ascending"),
                            ("bar_start_at_utc", "ascending")])


def resample_bars(bars, period: int, as_of: datetime = None, session_of=None):
    """V1.5 형태 1분봉 목록 → N분봉 목록(실시간 스캐너용).

    `bars`: `[{"dt": "YYYYMMDDHHMM[SS]", open, high, low, close, volume, …}]`
    `session_of`: 봉 시각 → 세션 함수(V1.5 `session_of`). 주지 않으면 첫 1분봉의
    `session`을 쓴다.
    `as_of`를 주면 끝나지 않은 묶음(`bar_end > as_of`)을 버린다 — 15:33에
    15:30~15:35 묶음을 완성된 5분봉으로 보지 않게 한다.
    """
    period = _check_period(period)
    groups = {}
    for b in sorted(bars, key=lambda x: str(x["dt"])):
        dt = str(b["dt"])
        start = datetime.strptime(dt[:12], "%Y%m%d%H%M")
        start = start.replace(minute=start.minute - start.minute % period)
        groups.setdefault(start, []).append(b)
    out = []
    for start, rs in sorted(groups.items()):
        end = start + timedelta(minutes=period)
        if as_of is not None and end > as_of.replace(tzinfo=None):
            continue
        dt = start.strftime("%Y%m%d%H%M%S")
        out.append({
            "dt": dt,
            "open": rs[0]["open"], "high": max(r["high"] for r in rs),
            "low": min(r["low"] for r in rs), "close": rs[-1]["close"],
            "volume": sum(r["volume"] for r in rs),
            "bar_start": start, "bar_end": end,
            "session": (session_of(dt) if session_of
                        else rs[0].get("session")),
        })
    return out

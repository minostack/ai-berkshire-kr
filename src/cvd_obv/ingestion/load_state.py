# -*- coding: utf-8 -*-
"""일일 적재의 "어디까지 확정했나" 장부 — `data-load daily|provisional`이 쓴다.

일정상 하루 이틀(또는 연휴 내내) 적재를 못 돌릴 수 있다. 그래서 적재는 "어제"를
기준으로 삼지 않고 **마지막으로 확정한 날 이후**를 다시 본다. 이 장부는 그 날짜
하나(`confirmed_through`)만 기억한다.

- `confirmed_through` = 마지막 확정 적재가 끝났을 때 애프터마켓 확정이 지난 마지막
  달력일. 08:30에 돌면 전날, 20:05 뒤에 돌면 그날이다.
- 다음 적재는 그 날짜부터(그날 포함, 하루 겹침) 오늘까지를 확인한다. 이미 확정된
  날은 수집·복원 단계가 알아서 건너뛰므로(멱등) 겹쳐도 비용이 거의 없다.
- 장부가 없거나 깨졌으면 최근 `DEFAULT_DAYS`거래일을 본다. 지워도 안전하다.
- 실패한 실행은 장부를 옮기지 않는다 — 다음 실행이 같은 구간을 다시 본다.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta

from ..domain.ids import KST
from ..domain.market_rules import default_registry

__all__ = ["DEFAULT_DAYS", "MIN_CONFIRMED_RATIO", "PROBE_SECURITY",
           "state_path", "load_state", "save_state", "confirmed_horizon",
           "backlog_since", "trading_days", "first_unconfirmed_day",
           "confirmed_through"]

DEFAULT_DAYS = 10
#: 거래일 하나를 "확정했다"고 보는 기준 — 지수 구성종목 중 확정 정규장 봉이 있는 비율.
#: 100%로 두면 종가 체결 1분봉이 없는 저유동성·거래정지 종목 하나가 장부를 영원히
#: 붙잡는다. 그런 종목은 하루 몇 개 수준이다(2026-10 실측: 미해결 분봉 15~28쌍).
MIN_CONFIRMED_RATIO = 0.9
#: 거래일 판정용 기준 종목 — catchup의 기준 종목과 같다.
PROBE_SECURITY = "KRX:005930"


def state_path() -> str:
    from ..baseline.scanner_v15 import project_root
    return os.path.join(project_root(), "data", "manifests",
                        "data_load_state.json")


def load_state(path: str = None) -> dict:
    try:
        with open(path or state_path(), encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_state(update: dict, path: str = None) -> str:
    """기존 장부에 `update`를 덮어 쓴다(다른 키는 남긴다)."""
    path = path or state_path()
    payload = load_state(path)
    payload.update(update)
    payload["version"] = 1
    payload["note"] = ("data-load가 마지막으로 확정한 날(confirmed_through). 다음 "
                       "적재는 그날부터 오늘까지를 확인한다. 지워도 안전하다 — "
                       f"최근 {DEFAULT_DAYS}거래일을 확인할 뿐이다.")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1, sort_keys=True)
    return path


def confirmed_horizon(now: datetime, registry=None, market: str = "KRX") -> date:
    """`now`에 애프터마켓 확정이 지난 마지막 달력일."""
    now = now if now.tzinfo else now.replace(tzinfo=KST)
    today = now.astimezone(KST).date()
    rule = (registry or default_registry()).resolve(market, today)
    if now >= rule.daily_confirmed_at(today):
        return today
    return today - timedelta(days=1)


def _as_day(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def backlog_since(today: date, state: dict = None, since: date = None,
                  days: int = None):
    """(since | None, days | None, 근거) — 이번 적재가 확인할 구간의 시작.

    우선순위: `--since` > `--days` > 장부의 `confirmed_through` > 기본 10거래일.
    """
    if since is not None:
        return since, None, "--since"
    if days is not None:
        return None, int(days), "--days"
    last = _as_day((state or {}).get("confirmed_through"))
    if last is not None:
        return min(last, today), None, f"장부 confirmed_through={last}"
    return None, DEFAULT_DAYS, f"장부 없음 — 최근 {DEFAULT_DAYS}거래일"


def trading_days(store, start: date, end: date,
                 probe: str = PROBE_SECURITY) -> list:
    """`[start, end]` 중 저장소가 아는 거래일 — 기준 종목의 1분봉 정규장 또는 일봉.

    달력(`silver.market_calendar`)은 일봉에서 파생돼 뒤처질 수 있어 쓰지 않는다.
    기준 종목 봉이 하나도 없는 날은 여기서 알 수 없다 — 그런 날은 수집 단계가
    실패로 끝나도록 했다(catchup: 빈 응답 = API 장애, 일봉 build: 전 종목 실패).
    """
    import pyarrow.dataset as ds
    from ..domain.errors import NoInputDataError

    days = set()
    rng = (ds.field("trading_date") >= start) & (ds.field("trading_date") <= end)
    for table, flt in (
            ("minute_bars", rng & (ds.field("security_id") == probe)
             & (ds.field("period_min") == 1) & (ds.field("session") == "REGULAR")),
            ("daily_bars", rng & (ds.field("security_id") == probe))):
        try:
            got = store.read_arrow("silver", table, columns=["trading_date"],
                                   filters=flt)
        except NoInputDataError:
            continue
        days |= set(got.column("trading_date").to_pylist())
    return sorted(days)


def first_unconfirmed_day(store, start: date, end: date, securities,
                          min_ratio: float = MIN_CONFIRMED_RATIO):
    """`[start, end]` 거래일 중 확정 정규장 봉이 `min_ratio` 미만인 첫 날. 없으면 None."""
    from ..bar_standard import DAILY_PRICE_SCOPE, _daily_scopes, is_provisional
    from ..storage.parquet_store import ParquetStore

    store = store or ParquetStore()
    securities = set(securities)
    if not securities:
        return None
    days = trading_days(store, start, end)
    if not days:
        return None
    final = {}
    for sid, d, scope, source in _daily_scopes(store, start, end, securities):
        if scope == DAILY_PRICE_SCOPE and not is_provisional(source):
            final[d] = final.get(d, 0) + 1
    need = len(securities) * float(min_ratio)
    for d in days:
        if final.get(d, 0) < need:
            return d
    return None


def confirmed_through(horizon: date, first_unconfirmed) -> date:
    """장부에 쓸 날짜 — 확정 안 된 가장 이른 날 **앞**까지만 옮긴다."""
    if first_unconfirmed is None or first_unconfirmed > horizon:
        return horizon
    return first_unconfirmed - timedelta(days=1)


# ── 선행 점검 — 날짜별 적재 상태 ──────────────────────────────

STATUS_FINAL = "확정 완료"
STATUS_PROVISIONAL = "잠정만"
STATUS_MINUTES = "1분봉만"
STATUS_EMPTY = "미적재(또는 휴장)"


def day_status(store, start: date, end: date, securities,
               min_ratio: float = MIN_CONFIRMED_RATIO) -> list:
    """`[start, end]` 평일마다 지수 구성종목의 1분봉·잠정 봉·확정 봉 수와 상태.

    작업을 시작하기 전에 무엇이 비었는지 보여 주는 용도다. 저장소에 아무것도
    없는 평일은 휴장인지 미적재인지 여기서는 모른다 — 1분봉 catchup이 API로
    실제 거래일을 확인해 받는다.
    """
    from ..bar_standard import DAILY_PRICE_SCOPE, _daily_scopes, is_provisional
    from ..storage.parquet_store import ParquetStore
    from .minute_catchup import MinuteCatchup

    store = store or ParquetStore()
    securities = set(securities)
    need = len(securities) * float(min_ratio)
    cov = MinuteCatchup(store=store).coverage_since(1, start)
    minutes, final, prov = {}, {}, {}
    for (sid, d), bars in cov.items():
        if sid in securities and d <= end and bars > 0:
            minutes[d] = minutes.get(d, 0) + 1
    for sid, d, scope, source in _daily_scopes(store, start, end, securities):
        if scope != DAILY_PRICE_SCOPE:
            continue
        bucket = prov if is_provisional(source) else final
        bucket[d] = bucket.get(d, 0) + 1
    known = set(trading_days(store, start, end))
    out, d = [], start
    while d <= end:
        if d.weekday() < 5 or d in known:
            m, f, p = minutes.get(d, 0), final.get(d, 0), prov.get(d, 0)
            if f >= need:
                status = STATUS_FINAL
            elif f + p >= need:
                status = STATUS_PROVISIONAL
            elif m >= need:
                status = STATUS_MINUTES
            else:
                status = STATUS_EMPTY
            out.append({"date": d, "minutes": m, "provisional": p, "final": f,
                        "status": status})
        d += timedelta(days=1)
    return out

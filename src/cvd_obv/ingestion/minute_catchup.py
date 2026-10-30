# -*- coding: utf-8 -*-
"""작업일 기준으로 **비어 있는 분봉만** 채운다 — 로드맵 Stage 4B(일일 누적).

왜 `minute-cache build`로는 안 되는가
------------------------------------
`build`는 종목마다 API가 주는 끝까지 받는다. 매일 돌리면 같은 1년치를 매일 다시
받는다 — 1분봉 350종목이면 하루에 3천만 봉이 넘는다. 이 명령은 **무엇이 비어
있는지 먼저 확인하고, 비어 있는 구간만** 받는다(`fetch_security(min_date=…)`).

하루 이틀 늦게 돌려도 되는 이유
------------------------------
"어제"를 기준으로 삼지 않는다. 저장소 커버리지와 API가 실제로 주는 거래일을
대조해 **비어 있는 날 전부**를 목표로 잡는다. 하루 늦으면 이틀이, 연휴 뒤에
돌리면 연휴 앞 거래일까지 함께 목표가 된다. 같은 날 두 번 돌리면 두 번째는
받을 것이 없다(멱등).

거래일을 캘린더에서 얻지 않는 이유
--------------------------------
`silver.market_calendar`는 **수집한 일봉에서 파생**되므로 저장소보다 뒤처질 수
있다(실측 2026-09-27: 캘린더 최신 2026-09-22, 실제 마지막 거래일 2026-09-23).
뒤처진 캘린더를 믿으면 "빠진 날"을 애초에 목표로 삼지 못한다. 그래서 기준
종목(기본 삼성전자) 분봉을 먼저 받아 **API가 실제로 주는 거래일**을 목표 집합으로
쓴다. 이 한 번의 호출이 캘린더 역할을 하고, 동시에 "API가 어디까지 과거를
주는가"(수집 지평)도 같이 알려 준다.

완료되지 않은 세션은 목표가 아니다
--------------------------------
정규장이 끝나지 않은 당일을 목표에 넣으면 반쪽 하루가 저장되고, 다음 날에는
"이미 있다"고 판단해 영구히 반쪽으로 남는다. 완료 판정은 V1.5
`is_session_complete()`를 그대로 쓴다 — 판정이 두 곳에서 갈리면 스캐너와
저장소가 어긋난다.

애프터마켓이 끝나기 전에 받은 날은 다시 받는다
--------------------------------------------
16:10 실행은 정규장은 다 받지만 애프터마켓(16:00~20:00)은 앞부분만 받는다.
정규장 봉 수만 보면 "완료"라서 다시는 받지 않고, 그날 애프터마켓 분봉은 영구히
반쪽이 된다. 1분봉으로 정규장 일봉을 복원할 때(`regular_from_minute`) 애프터마켓
거래량을 빼야 하므로 반쪽이면 그날은 복원되지 않는다. 그래서 커버리지 행의
`run_id`에서 수집 시각을 읽어, **애프터마켓 확정 전에 받았고 지금은 확정이
지난 날**을 한 번 더 받는다(`after_pending_since`).
"""

from __future__ import annotations

import json
import math
import os
import time as _time
from datetime import date, datetime

import pyarrow.dataset as ds

from ..contracts.market_schema import BAR_COVERAGE_PK
from ..domain.errors import ConfigError, NoInputDataError
from ..domain.ids import KST, normalize_security_id, run_started_at, to_utc
from ..domain.market_rules import default_registry
from ..domain.status import QualityStatus
from ..storage.parquet_store import ParquetStore
from .minute_history import MinuteHistoryCache
from .universe_history import UniverseRepository

__all__ = ["MinuteCatchup", "SessionStorePolicy", "DEFAULT_PROBE_CODE",
           "DEFAULT_MIN_COMPLETENESS"]

#: 거래일 확인용 기준 종목 — 거래정지·무체결 가능성이 가장 낮은 종목.
DEFAULT_PROBE_CODE = "005930"

#: 이 비율 미만이면 "그날은 덜 받았다"로 보고 다시 받는다.
DEFAULT_MIN_COMPLETENESS = 0.80

#: 1회 응답 행 수 실측 근사(키움) — 페이지 상한 추정에만 쓴다. 원천이
#: `ROWS_PER_PAGE`를 밝히면 그 값을 쓴다(KIS 120).
_ROWS_PER_PAGE = 900

_SESSION_REGULAR = "REGULAR"


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _state_key(security_id: str, day: date) -> str:
    return f"{security_id}|{day.isoformat()}"


class SessionStorePolicy:
    """같은 1분봉을 두 번 저장하지 않는 규칙(2026-10-16).

    일일 누적은 같은 날을 두 번 받는다 — 16:10(정규장 확정 뒤)과 다음 날
    08:30(애프터마켓 확정 뒤). 예전에는 두 번 다 모든 봉을 저장해 정규장 봉
    약 12만 개(하루 약 4.5MB)가 중복됐다. API는 세션을 골라 주지 않으므로 받은
    뒤 저장할 것만 고른다.

    - 애프터마켓 확정 **전** 수집: 애프터마켓(`AFTER`) 봉을 저장하지 않는다 —
      진행 중이라 어차피 확정 뒤에 다시 받는다. 커버리지도 쓰지 않는다
    - 애프터마켓 확정 **뒤** 다시 받기(`after_pending`)이고 그날 정규장 봉이
      이미 완전하면(기준 이상 또는 장부상 더 받을 것 없음): 정규장(`REGULAR`)
      봉을 저장하지 않는다. 정규장 **커버리지는 쓴다** — 그날을 확정 뒤에 받았다는
      근거(`last_fetched_since`)이고, 다시 받기 대상에서 빠지게 한다
    - 계획에 없는 (종목, 날짜): 저장하지 않는다. 종목마다 가장 오래된 결측일까지
      거슬러 받으므로 이미 완전한 날도 딸려 온다
    - 그 밖(처음 받는 날, 정규장이 덜 있는 날): 모두 저장한다
    """

    def __init__(self, as_of: datetime, keep_regular_skip=(), targets=None,
                 registry=None, market: str = "KRX"):
        self.now = to_utc(as_of, assume_tz=KST)
        self.skip_regular = set(keep_regular_skip)
        self.targets = None if targets is None else set(targets)
        self.registry = registry or default_registry()
        self.market = market
        self._finals = {}

    def _after_final(self, day) -> bool:
        day = _as_date(day)
        if day not in self._finals:
            rule = self.registry.resolve(self.market, day)
            self._finals[day] = to_utc(rule.daily_confirmed_at(day))
        return self.now >= self._finals[day]

    def _targeted(self, security_id, day) -> bool:
        return self.targets is None or (security_id, _as_date(day)) in self.targets

    def keep_bar(self, security_id, day, session) -> bool:
        if not self._targeted(security_id, day):
            return False
        if session == "AFTER" and not self._after_final(day):
            return False
        if session == _SESSION_REGULAR and \
                (security_id, _as_date(day)) in self.skip_regular:
            return False
        return True

    def keep_coverage(self, security_id, day, session) -> bool:
        if not self._targeted(security_id, day):
            return False
        return not (session == "AFTER" and not self._after_final(day))


class MinuteCatchup:
    """비어 있는 분봉을 찾아 그 구간만 받는다."""

    LAYER = "silver"
    COVERAGE_TABLE = "bar_coverage"
    DATASET = "minute"

    def __init__(self, cache: MinuteHistoryCache = None,
                 store: ParquetStore = None,
                 universe: UniverseRepository = None,
                 index_file: str = None):
        self.store = store or ParquetStore()
        self.cache = cache or MinuteHistoryCache(store=self.store)
        self.universe = universe or UniverseRepository(store=self.store)
        self.index_file = index_file

    # ── 기준 구현 재사용 ────────────────────────────────────────
    @property
    def scanner(self):
        """세션·봉 판정은 V1.5 함수를 그대로 쓴다."""
        return self._source().scanner

    def _source(self):
        """적재 원천(`market_source`, 기본 KIS) — 주입된 것이 있으면 그것."""
        if self.cache.source is None:
            from .market_source import minute_source
            self.cache.source = minute_source()
        return self.cache.source

    def expected_bars(self, period: int) -> int:
        """정규장 하루 기대 봉 수(V1.5 세션 모델)."""
        return int(self.scanner.expected_bars_per_day(_SESSION_REGULAR, period))

    def estimate_pages(self, period: int, days: int) -> int:
        """`days`거래일을 덮으려면 몇 페이지가 필요한가(여유 포함)."""
        sc = self.scanner
        per_day = (sc.expected_bars_per_day(_SESSION_REGULAR, period)
                   + sc.expected_bars_per_day(sc._SESSION_AFTER, period))
        # NXT는 프리마켓(08:00~08:50)·15:30~16:00 봉이 더 있다(실측 하루 691봉 vs KRX 622)
        per_day *= getattr(self, "_page_factor", 1.0)
        rows = getattr(self._source(), "ROWS_PER_PAGE", _ROWS_PER_PAGE)
        return max(3, math.ceil(max(1, days) * per_day / rows) + 3)

    # ── 1. 목표 거래일 — API가 실제로 주는 날 ────────────────────
    def probe(self, token=None, period: int = 1, days: int = 10,
              as_of: datetime = None, probe_code: str = DEFAULT_PROBE_CODE):
        """기준 종목 하나를 받아 **완료된 거래일 목록**을 만든다.

        돌려주는 dict:
          `days`          완료된 정규장 거래일(오래된 → 최신), 최대 `days`개
          `dropped_open`  완료되지 않아 제외한 날(장중 실행 시 오늘)
          `api_oldest`    이 호출이 준 가장 오래된 거래일 = 수집 지평
          `bars_by_day`   기준 종목의 날짜별 정규장 봉 수(참고)
        """
        as_of = as_of or datetime.now()
        source = self._source()
        # API가 페이지 중간에 간헐적으로 응답을 주지 않는다(키움 실측). 그때까지 받은 봉만으로
        # 거래일을 정하면 오래된 날이 목표에서 빠진다(2026-10-20 실측: NXT 소급이 4일만
        # 잡혔다). 실패면 몇 번 다시 받고, 끝내 실패하면 결과에 그대로 드러낸다.
        for attempt in range(3):
            result = source.fetch_security(
                probe_code, token=token, period=period,
                max_pages=self.estimate_pages(period, days + 2))
            if result.status != QualityStatus.FAILED:
                break
            _time.sleep(2 * (attempt + 1))

        bars_by_day = {}
        for bar in result.bars:
            if bar["session"] != _SESSION_REGULAR:
                continue
            key = bar["trading_date"]
            bars_by_day[key] = bars_by_day.get(key, 0) + 1

        all_days = sorted(bars_by_day)
        complete, dropped = [], []
        for day in all_days:
            if self.scanner.is_session_complete(day.strftime("%Y%m%d"),
                                                _SESSION_REGULAR, as_of):
                complete.append(day)
            else:
                dropped.append(day)
        return {
            "probe_code": normalize_security_id(probe_code),
            "status": result.status,
            "pages": result.pages,
            "days": complete[-days:] if days else complete,
            "dropped_open": dropped,
            "api_oldest": all_days[0] if all_days else None,
            "bars_by_day": bars_by_day,
        }

    # ── 재시도 억제 장부 ───────────────────────────────────────
    def state_path(self) -> str:
        from ..baseline.scanner_v15 import project_root
        return os.path.join(project_root(), "data", "manifests",
                            "minute_catchup_state.json")

    def load_state(self, period: int) -> dict:
        """{'종목|날짜': 봉 수} — "더 받아도 안 늘어난다"고 확인된 것."""
        try:
            with open(self.state_path(), encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, ValueError):
            return {}
        return dict((payload.get("period_min") or {}).get(str(period)) or {})

    def save_state(self, period: int, settled: dict) -> str:
        """장부를 덮어쓴다. 다른 주기의 기록은 건드리지 않는다."""
        path = self.state_path()
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, ValueError):
            payload = {}
        payload.setdefault("version", 1)
        payload.setdefault("period_min", {})
        payload["period_min"][str(period)] = settled
        payload["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        payload["note"] = ("다시 받아도 봉 수가 늘지 않은 (종목|거래일)과 그때의 "
                           "봉 수. 저장된 봉 수가 이 값과 달라지면 다시 대상이 "
                           "된다. 지워도 안전하다 — 다음 실행이 한 번 더 "
                           "받아 볼 뿐이다.")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1, sort_keys=True)
        return path

    # ── 2. 이미 가진 것 ────────────────────────────────────────
    def coverage_since(self, period: int, since: date) -> dict:
        """`since` 이후 정규장 커버리지 — {(종목, 날짜): 봉 수}.

        `bar_coverage`를 쓴다(작고 빠르다). 커버리지 행이 없는 (종목, 날짜)는
        **결측으로 본다** — 봉이 실제로 있는데 커버리지만 없는 경우에도 다시
        받아 쓰기만 하므로 안전한 방향으로 틀린다(중복은 PK로 합쳐진다).
        """
        try:
            table = self.store.read_arrow(
                self.LAYER, self.COVERAGE_TABLE,
                columns=["dataset", "security_id", "period_min",
                         "trading_date", "session", "actual_bars"],
                filters=((ds.field("dataset") == self.DATASET)
                         & (ds.field("period_min") == int(period))
                         & (ds.field("session") == _SESSION_REGULAR)
                         & (ds.field("trading_date") >= since)),
                dedupe_pk=BAR_COVERAGE_PK)
        except NoInputDataError:
            return {}
        cols = {c: table.column(c).to_pylist()
                for c in ("security_id", "trading_date", "actual_bars")}
        out = {}
        for sec, day, bars in zip(cols["security_id"], cols["trading_date"],
                                  cols["actual_bars"]):
            key = (sec, _as_date(day))
            out[key] = max(out.get(key, 0), int(bars or 0))
        return out

    def last_fetched_since(self, period: int, since: date,
                           until: date = None) -> dict:
        """{(종목, 날짜): 그날을 마지막으로 받은 시각(UTC)} — 정규장 커버리지 `run_id`.

        일일 누적은 같은 날을 16:10(정규장)과 다음 날 08:30(애프터마켓)에 나눠
        저장한다. 08:30에는 정규장 봉을 다시 저장하지 않지만 정규장 커버리지는 새로
        쓰므로, 이 시각이 "그날을 애프터마켓 확정 뒤에 받았는가"의 근거다.
        시각을 모르는 run_id는 뺀다.
        """
        flt = ((ds.field("dataset") == self.DATASET)
               & (ds.field("period_min") == int(period))
               & (ds.field("session") == _SESSION_REGULAR)
               & (ds.field("trading_date") >= since))
        if until is not None:
            flt = flt & (ds.field("trading_date") <= until)
        try:
            table = self.store.read_arrow(
                self.LAYER, self.COVERAGE_TABLE,
                columns=["dataset", "security_id", "period_min",
                         "trading_date", "session", "run_id"],
                filters=flt, dedupe_pk=BAR_COVERAGE_PK)
        except NoInputDataError:
            return {}
        latest = {}
        for sec, day, run_id in zip(table.column("security_id").to_pylist(),
                                    table.column("trading_date").to_pylist(),
                                    table.column("run_id").to_pylist()):
            got = run_started_at(run_id)
            if got is None:
                continue
            key = (sec, _as_date(day))
            latest[key] = max(latest.get(key, got), got)
        return latest

    def after_pending_since(self, period: int, since: date,
                            as_of: datetime) -> set:
        """애프터마켓 확정 **전에** 받았고 `as_of`에는 확정이 지난 (종목, 날짜).

        정규장 커버리지 행의 `run_id`로 수집 시각을 읽는다(`last_fetched_since`).
        시각을 모르는 run_id는 대상에서 뺀다 — 모르는 것을 매일 다시 받지 않는다.
        """
        now = to_utc(as_of, assume_tz=KST)
        registry, finals = default_registry(), {}
        pending = set()
        for (sec, day), got in self.last_fetched_since(period, since).items():
            if day not in finals:
                finals[day] = to_utc(registry.resolve("KRX", day)
                                     .daily_confirmed_at(day))
            if got < finals[day] <= now:
                pending.add((sec, day))
        return pending

    # ── 3. 대상 종목 ──────────────────────────────────────────
    def resolve_members(self, indices, days, allow_index_file: bool = True):
        """목표 거래일들의 구성종목 합집합.

        날짜별 명단은 유니버스 기준 `universe-1.0`(`cvd_obv.universe.universe_on`)
        으로 만든다 — 스캐너가 그날 쓰는 명단과 같다. 수집 대상을 정하는 일이라
        생존편향 문제가 없으므로 오늘 명단(`index_stocks.json`)도 합친다(오늘 편입분
        등). 어느 쪽에서 왔는지는 남긴다.
        """
        from ..universe import current_universe, universe_on

        members, sources = {}, {}
        for index_id in indices:
            got, src = set(), []
            for day in days:
                try:
                    u = universe_on(day, (index_id,), repo=self.universe,
                                    path=self.index_file)
                except Exception:                      # noqa: BLE001
                    continue
                got |= set(u.codes(index_id))
                src.append(u.status.get(index_id))
            if got and "HISTORICAL" in src:
                sources[index_id] = "universe_membership"
            if allow_index_file:
                cur = current_universe((index_id,), path=self.index_file)
                codes = cur.codes(index_id)
                if codes:
                    before = len(got)
                    got |= set(codes)
                    if len(got) > before or not sources.get(index_id):
                        sources[index_id] = (
                            f"{sources.get(index_id, '')}+{cur.sources[index_id]}"
                        ).lstrip("+")
            members[index_id] = sorted(got)
        return members, sources

    # ── 4. 계획 ───────────────────────────────────────────────
    def plan(self, target_days, codes, coverage, period: int,
             min_completeness: float = DEFAULT_MIN_COMPLETENESS,
             settled: dict = None, after_pending=None):
        """무엇이 비어 있는가. API를 부르지 않는다(그래서 테스트가 쉽다).

        `after_pending`은 정규장은 다 있지만 애프터마켓 확정 전에 받은
        (종목, 날짜) 집합이다(`after_pending_since`). 그날은 `after_pending`으로
        세고 다시 받는다.

        `settled`는 "다시 받아도 더 주지 않는다"고 확인된 (종목, 날짜)의
        봉 수다(`state` 파일). 무체결·거래정지로 영구히 기준 미달인 날을
        매일 다시 받지 않기 위한 것이며, **봉 수가 그때와 달라지면 다시
        대상이 된다** — 저장소가 바뀌었다는 뜻이므로 재확인이 맞다.
        """
        if not target_days:
            raise ConfigError("목표 거래일이 없다 — probe 결과가 비어 있다")
        expected = self.expected_bars(period)
        floor = expected * float(min_completeness)
        settled = settled or {}
        after_pending = after_pending or set()

        gaps, by_day = {}, {}
        settled_skips = 0
        for day in target_days:
            missing, partial, pending, complete = [], [], [], 0
            for code in codes:
                sec = normalize_security_id(code)
                have = coverage.get((sec, day))
                if have is not None and (sec, day) in after_pending:
                    # 봉 수·장부와 무관하게 다시 받는다. 정규장 봉이 기준 미달인
                    # 저유동성 종목도 정규장 일봉 복원에는 확정 뒤 분봉이 필요하다
                    # (2026-10-04 실측: 10/2 12종목이 장부에 걸려 복원되지 않았다).
                    # 확정 뒤에 받으면 이 집합에서 빠지므로 반복 수집은 없다.
                    pending.append(code)
                elif have is None:
                    missing.append(code)
                elif have < floor:
                    if settled.get(_state_key(sec, day)) == have:
                        settled_skips += 1      # 더 받을 것이 없다고 확인된 날
                        continue
                    partial.append(code)
                else:
                    complete += 1
            by_day[day] = {"missing": len(missing), "partial": len(partial),
                           "after_pending": len(pending),
                           "complete": complete,
                           "missing_codes": missing, "partial_codes": partial,
                           "after_pending_codes": pending}
            for code in missing + partial + pending:
                gaps.setdefault(code, []).append(day)

        oldest = min((min(v) for v in gaps.values()), default=None)
        return {
            "expected_bars_per_day": expected,
            "min_completeness": float(min_completeness),
            "settled_skipped": settled_skips,
            "target_days": list(target_days),
            "universe_size": len(codes),
            "gap_codes": sorted(gaps),
            "gaps": {c: sorted(v) for c, v in gaps.items()},
            "oldest_gap_day": oldest,
            "pairs_missing": sum(v["missing"] for v in by_day.values()),
            "pairs_partial": sum(v["partial"] for v in by_day.values()),
            "pairs_after_pending": sum(v["after_pending"]
                                       for v in by_day.values()),
            "by_day": by_day,
        }

    # ── 5. 재확인 ─────────────────────────────────────────────
    def verify_gaps(self, gaps, coverage, period: int,
                    min_completeness: float = DEFAULT_MIN_COMPLETENESS,
                    api_oldest: date = None, settled: dict = None):
        """받은 뒤 상태를 **이번에 목표로 잡은 (종목, 날짜)에 한해서만** 센다.

        예전 구현은 계획(전체 유니버스, 장부 제외)과 재확인(수집 종목만, 장부
        미적용)을 서로 다른 집합에서 세서 `recovered_pairs`가 **음수**로 나왔다
        (실측 2026-09-27: 계획 63쌍, 재확인 130쌍 → -67). 집계는 같은 집합
        위에서만 뺄 수 있다.
        """
        expected = self.expected_bars(period)
        floor = expected * float(min_completeness)
        settled = dict(settled or {})
        dropped = 0

        recovered, targeted, unresolved = 0, 0, {}
        for code, days in (gaps or {}).items():
            sec = normalize_security_id(code)
            for day in days:
                targeted += 1
                bars = coverage.get((sec, day))
                if bars is not None and bars >= floor:
                    recovered += 1
                    # 정상화된 날은 장부에서 빼 둔다 — 낡은 기록을 남기지 않는다.
                    if settled.pop(_state_key(sec, day), None) is not None:
                        dropped += 1
                    continue
                if api_oldest is not None and day < api_oldest:
                    kind = "BEFORE_API_HORIZON"      # 지평 밖 — 복구 불가
                elif bars is None:
                    kind = "NO_DATA"                 # 응답에 그 날이 없다
                else:
                    kind = "STILL_PARTIAL"           # 덜 받았거나 무체결
                    # 방금 받아 봤는데도 기준 미달이다. 봉 수를 적어 두고
                    # 다음 실행에서는 건너뛴다(늘어나면 다시 대상이 된다).
                    settled[_state_key(sec, day)] = bars
                unresolved.setdefault(kind, []).append(
                    {"code": code, "day": day.isoformat(), "bars": bars})
        return {"targeted": targeted, "recovered": recovered,
                "unresolved": unresolved, "settled": settled,
                "settled_dropped": dropped}

    # ── 6. 실행 ───────────────────────────────────────────────
    def run(self, run_id: str, period: int = 1, days: int = 10,
            indices=("KOSPI200", "KOSDAQ150"), codes=None,
            as_of: datetime = None, since=None,
            min_completeness: float = DEFAULT_MIN_COMPLETENESS,
            probe_code: str = DEFAULT_PROBE_CODE, dry_run: bool = False,
            retries: int = 2, flush_every: int = 10, on_progress=None,
            limit_securities: int = None, recheck: bool = False,
            venue: str = "KRX"):
        """확인 → 계획 → 부족한 구간만 수집 → 재확인.

        `venue="NXT"`면 넥스트레이드 1분봉을 받는다(`NXT:<코드>` — KIS `NX`,
        키움 `<코드>_NX`).
        대상은 NXT 거래 종목만이다(`nxt_volume.nxt_traded`, 판정 못 하면 전부).
        """
        venue = str(venue or "KRX").upper()
        if venue not in ("KRX", "NXT"):
            raise ConfigError(f"venue는 KRX 또는 NXT: {venue!r}")
        if venue == "NXT" and ":" not in str(probe_code):
            probe_code = f"NXT:{probe_code}"
        self._page_factor = 1.2 if venue == "NXT" else 1.0
        as_of = as_of or datetime.now()
        started = _time.monotonic()
        source = self._source()
        token = source.token()

        probe = self.probe(token=token, period=period, days=days,
                           as_of=as_of, probe_code=probe_code)
        if probe["api_oldest"] is None:
            # 휴장일이면 기준 종목은 지난 거래일 봉이라도 준다. 아무 봉도 없으면 API
            # 장애다 — "목표 거래일 없음(휴장)"으로 성공 처리하면 적재가 그날을
            # 확정한 것으로 넘어간다(2026-10-13 08:30 실측: 빈 응답이 SUCCESS).
            from ..domain.errors import ExternalApiError
            from ..domain.status import DetailCode
            raise ExternalApiError(
                f"기준 종목 {probe['probe_code']} 분봉 응답이 비었다"
                f"(status={probe['status']}, pages={probe['pages']}) — API 장애로 본다",
                detail_code=DetailCode.API_NO_RESPONSE)
        target_days = list(probe["days"])
        if since is not None:
            since_day = _as_date(since)
            target_days = [d for d in target_days if d >= since_day]
        if not target_days:
            return {"status": QualityStatus.SUCCESS, "probe": probe,
                    "target_days": [], "gap_codes": [],
                    "note": "완료된 목표 거래일이 없다(장중 실행 또는 휴장)",
                    "elapsed_sec": round(_time.monotonic() - started, 1)}

        if codes:
            universe = [str(c).strip() for c in codes if str(c).strip()]
            member_sources = {"--codes": "직접 지정"}
        else:
            members, member_sources = self.resolve_members(indices, target_days)
            universe = sorted({c for v in members.values() for c in v})
        if venue == "NXT":
            from .nxt_volume import nxt_traded
            local = sorted({str(c).split(":")[-1] for c in universe})
            traded = nxt_traded(self.store, [f"KRX:{c}" for c in local],
                                target_days[-1])
            if traded:
                local = [c for c in local if f"KRX:{c}" in traded]
            universe = [f"NXT:{c}" for c in local]
            member_sources = dict(member_sources, venue=(
                f"NXT 거래 종목 {len(universe)}개" if traded
                else "NXT 거래 종목을 판정 못 해 전부"))
        if limit_securities:
            universe = universe[:limit_securities]

        coverage = self.coverage_since(period, target_days[0])
        settled = {} if recheck else self.load_state(period)
        pending = self.after_pending_since(period, target_days[0], as_of)
        plan = self.plan(target_days, universe, coverage, period,
                         min_completeness=min_completeness, settled=settled,
                         after_pending=pending)

        summary = {
            "command": "minute-cache catchup",
            "venue": venue,
            "run_id": run_id,
            "period_min": period,
            "as_of": as_of.strftime("%Y-%m-%d %H:%M:%S"),
            "probe": {k: v for k, v in probe.items() if k != "bars_by_day"},
            "member_sources": member_sources,
            "plan": {k: v for k, v in plan.items()
                     if k not in ("gaps", "by_day")},
            "by_day": {d.isoformat(): {k: v for k, v in info.items()
                                       if not k.endswith("_codes")}
                       for d, info in plan["by_day"].items()},
            "dry_run": bool(dry_run),
        }

        if not plan["gap_codes"]:
            summary.update({"status": QualityStatus.SUCCESS, "bars": 0,
                            "fetched_securities": 0,
                            "note": "비어 있는 구간이 없다",
                            "elapsed_sec": round(_time.monotonic() - started, 1)})
            return summary

        # 가장 오래된 결측일까지만 되돌아간다 — 그 앞은 이미 가지고 있다.
        oldest = plan["oldest_gap_day"]
        depth = sum(1 for d in probe["bars_by_day"] if d >= oldest)
        max_pages = self.estimate_pages(period, depth + 1)
        summary["fetch"] = {
            "securities": len(plan["gap_codes"]),
            "min_date": oldest.strftime("%Y%m%d"),
            "end": target_days[-1].isoformat(),
            "max_pages": max_pages,
            "depth_trading_days": depth,
        }

        if dry_run:
            summary.update({"status": QualityStatus.SUCCESS, "bars": 0,
                            "fetched_securities": 0,
                            "elapsed_sec": round(_time.monotonic() - started, 1)})
            return summary

        # 정규장이 이미 완전한 날의 확정 뒤 다시 받기는 애프터마켓만 저장한다
        floor = plan["expected_bars_per_day"] * float(min_completeness)
        skip_regular = set()
        for sec, day in pending:
            have = coverage.get((sec, day))
            if have is not None and (have >= floor or
                                     settled.get(_state_key(sec, day)) == have):
                skip_regular.add((sec, day))
        targets = {(normalize_security_id(c), d)
                   for c, days_ in plan["gaps"].items() for d in days_}
        policy = SessionStorePolicy(as_of, keep_regular_skip=skip_regular,
                                    targets=targets)
        summary["store_policy"] = {"target_pairs": len(targets),
                                   "regular_not_restored_pairs": len(skip_regular)}
        build = self.cache.build(
            plan["gap_codes"], run_id=run_id, period=period, retries=retries,
            flush_every=flush_every, on_progress=on_progress,
            max_pages=max_pages, end=target_days[-1],
            min_date=oldest.strftime("%Y%m%d"),
            keep_bar=policy.keep_bar, keep_coverage=policy.keep_coverage)
        summary["build"] = {k: v for k, v in build.items()
                            if k not in ("artifacts",)}
        summary["artifacts"] = build["artifacts"]
        summary["bars"] = build["bars"]
        summary["fetched_securities"] = build["attempted"]

        # ── 재확인: 받은 뒤에도 비어 있으면 그 이유를 분류한다 ──
        after = self.coverage_since(period, target_days[0])
        checked = self.verify_gaps(plan["gaps"], after, period,
                                   min_completeness=min_completeness,
                                   api_oldest=probe["api_oldest"],
                                   settled=settled)
        if checked["settled"] != settled and not recheck:
            summary["state_path"] = self.save_state(period, checked["settled"])
        summary["settled_total"] = len(checked["settled"])
        summary["settled_added"] = len(checked["settled"]) - len(settled)
        summary["settled_dropped"] = checked["settled_dropped"]
        summary["settled_skipped_in_plan"] = plan["settled_skipped"]
        summary["targeted_pairs"] = checked["targeted"]
        summary["recovered_pairs"] = checked["recovered"]
        summary["unresolved"] = checked["unresolved"]
        summary["unresolved_counts"] = {k: len(v)
                                        for k, v in checked["unresolved"].items()}
        summary["elapsed_sec"] = round(_time.monotonic() - started, 1)
        summary["status"] = (
            QualityStatus.SUCCESS if not checked["unresolved"]
            else QualityStatus.PARTIAL if summary["bars"]
            else QualityStatus.FAILED)
        return summary

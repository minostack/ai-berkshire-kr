# -*- coding: utf-8 -*-
"""거래 캘린더 — `silver.market_calendar`(스키마 §5).

Horizon과 `next_open` 계산의 **유일한** 달력이다. 다른 곳에서 "영업일 = 주말
제외"로 계산하면 휴장일에서 틀리고, 틀린 줄도 모른다.

출처의 한계를 분명히 한다: KRX 공식 휴장일 API를 쓰지 않고 수집한 일봉의
거래일 합집합에서 캘린더를 만든다. 200종목 중 단 한 종목도 거래되지 않은 날은
사실상 휴장일이지만, 원리적으로는 "전 종목 거래정지"와 구분되지 않는다. 그래서
`source`에 `DERIVED_FROM_DAILY_BARS`를 남기고, 파생에 쓴 종목 수도 기록한다.
"""

from __future__ import annotations

import bisect
from datetime import date, datetime

from .errors import ConfigError, NoInputDataError
from .ids import to_utc
from .market_rules import MarketRuleRegistry, default_registry
from .status import Session

__all__ = ["TradingCalendar", "CALENDAR_SOURCE_DERIVED"]

CALENDAR_SOURCE_DERIVED = "DERIVED_FROM_DAILY_BARS"


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if len(s) == 8 and s.isdigit():
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    return date.fromisoformat(s)


class TradingCalendar:
    """정렬된 거래일 목록과 그 위의 이동 연산."""

    __slots__ = ("market", "_days", "_index", "registry", "source",
                 "derived_from_securities", "_per_day_securities")

    def __init__(self, market: str, trading_dates, registry=None,
                 source: str = CALENDAR_SOURCE_DERIVED,
                 derived_from_securities: int = 0,
                 per_day_securities=None):
        days = sorted({_as_date(d) for d in trading_dates})
        if not days:
            raise NoInputDataError("거래일이 하나도 없다")
        self.market = str(market)
        self._days = days
        self._index = {d: i for i, d in enumerate(days)}
        self.registry = registry or default_registry()
        self.source = source
        self.derived_from_securities = int(derived_from_securities)
        # 하루별 파생 근거 종목 수. 없으면 전체 수로 떨어진다(옛 동작).
        self._per_day_securities = dict(per_day_securities or {})

    def securities_on(self, day) -> int:
        """그 거래일이 **몇 종목의 일봉에서** 파생됐는가.

        얇을수록 "휴장"과 "그 종목들이 안 움직였다"를 구별하기 어렵다.
        """
        return self._per_day_securities.get(
            _as_date(day), self.derived_from_securities)

    def thin_days(self, min_securities: int):
        """근거가 `min_securities`에 못 미치는 거래일."""
        return [d for d in self._days
                if self.securities_on(d) < min_securities]

    # ── 기본 ───────────────────────────────────────────────

    def __len__(self):
        return len(self._days)

    def __contains__(self, value):
        return _as_date(value) in self._index

    @property
    def start(self) -> date:
        return self._days[0]

    @property
    def end(self) -> date:
        return self._days[-1]

    @property
    def days(self):
        return tuple(self._days)

    def is_trading_day(self, value) -> bool:
        return _as_date(value) in self._index

    # ── 생성 ───────────────────────────────────────────────

    @classmethod
    def from_daily_bars(cls, rows, market: str = "KRX",
                        registry: MarketRuleRegistry = None):
        """일봉 row 목록에서 캘린더를 만든다.

        row는 `trading_date`(또는 `dt`)와 `security_id`(또는 `code`)를 갖는
        Mapping이면 된다.
        """
        days, securities = set(), set()
        per_day = {}
        for row in rows:
            raw = row.get("trading_date", row.get("dt"))
            if raw is None:
                continue
            day = _as_date(str(raw)[:10]
                           if not isinstance(raw, (date, datetime)) else raw)
            days.add(day)
            sec = row.get("security_id", row.get("code"))
            if sec:
                securities.add(sec)
                per_day.setdefault(day, set()).add(sec)
        if not days:
            raise NoInputDataError("일봉에서 거래일을 찾지 못했다")
        return cls(market, days, registry=registry,
                   source=CALENDAR_SOURCE_DERIVED,
                   derived_from_securities=len(securities),
                   per_day_securities={d: len(v) for d, v in per_day.items()})

    # ── 이동 ───────────────────────────────────────────────

    def position(self, value):
        """거래일의 인덱스. 거래일이 아니면 None."""
        return self._index.get(_as_date(value))

    def next_trading_date(self, value):
        """그 날짜 **다음** 거래일. 범위를 넘으면 None(추정하지 않는다).

        입력이 거래일이 아니어도 된다 — 그 날짜보다 큰 첫 거래일을 돌려준다.
        """
        d = _as_date(value)
        i = bisect.bisect_right(self._days, d)
        return self._days[i] if i < len(self._days) else None

    def previous_trading_date(self, value):
        d = _as_date(value)
        i = bisect.bisect_left(self._days, d)
        return self._days[i - 1] if i > 0 else None

    def shift(self, value, n: int):
        """거래일 기준 `n`일 이동. 캘린더 밖이면 None.

        `n=0`은 그 날짜 자신이며, 입력이 거래일이 아니면 오류다 — 조용히
        가까운 거래일로 밀어내면 horizon이 한 칸 어긋난다.
        """
        d = _as_date(value)
        i = self._index.get(d)
        if i is None:
            raise ConfigError(f"거래일이 아니다: {d}")
        j = i + int(n)
        if j < 0 or j >= len(self._days):
            return None
        return self._days[j]

    def count_between(self, start, end) -> int:
        """`(start, end]` 구간의 거래일 수."""
        a = bisect.bisect_right(self._days, _as_date(start))
        b = bisect.bisect_right(self._days, _as_date(end))
        return max(0, b - a)

    # ── 세션 ───────────────────────────────────────────────

    def rule_for(self, value):
        return self.registry.resolve(self.market, _as_date(value))

    def session_bounds_utc(self, value, session: str):
        """세션 경계를 UTC로. 그 날짜에 없는 세션이면 None."""
        d = _as_date(value)
        bounds = self.rule_for(d).session_bounds(d, session)
        if bounds is None:
            return None
        return (to_utc(bounds[0]), to_utc(bounds[1]))

    # ── 저장 형식 ──────────────────────────────────────────

    def to_rows(self, market_rule_version: str = None):
        """`silver.market_calendar` row 목록.

        PK: `market`, `trading_date`, `market_rule_version`.
        """
        rows = []
        for i, d in enumerate(self._days):
            rule = self.rule_for(d)
            if market_rule_version and rule.version != market_rule_version:
                continue
            regular = self.session_bounds_utc(d, Session.REGULAR)
            after = self.session_bounds_utc(d, Session.AFTER)
            rows.append({
                "market": self.market,
                "trading_date": d,
                "market_rule_version": rule.version,
                "is_trading_day": True,
                "regular_open_utc": regular[0] if regular else None,
                "regular_close_utc": regular[1] if regular else None,
                "after_open_utc": after[0] if after else None,
                "after_close_utc": after[1] if after else None,
                "next_trading_date": (self._days[i + 1]
                                      if i + 1 < len(self._days) else None),
                "previous_trading_date": self._days[i - 1] if i > 0 else None,
                "special_session": False,
                "daily_price_scope": rule.daily_scope_of(d),
                "source": self.source,
                "derived_from_securities": self.securities_on(d),
            })
        return rows

    def gaps(self, max_gap_days: int = 7):
        """연속 거래일 사이 간격이 비정상적으로 큰 구간.

        연휴는 정상이지만 수집 누락도 같은 모습이라 자동 판정하지 않는다.
        사람이 볼 수 있게 목록만 돌려준다.
        """
        out = []
        for a, b in zip(self._days, self._days[1:]):
            gap = (b - a).days
            if gap > max_gap_days:
                out.append({"from": a, "to": b, "calendar_days": gap})
        return out

    def __repr__(self):
        return (f"TradingCalendar({self.market}, {len(self._days)}일, "
                f"{self.start}~{self.end}, source={self.source})")

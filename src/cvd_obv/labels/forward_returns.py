# -*- coding: utf-8 -*-
"""무조건부 forward return 패널 — 로드맵 Stage 1 작업 4.

`signal_date`마다 **다음 거래일 시가**에 사고 **h 거래일 뒤 종가**에 파는 수익률을
계산한다. 신호와 무관하게 모든 종목·날짜에 대해 만들기 때문에, 이 패널이
기준선(base rate)의 모집단이자 Stage 6 신호별 label의 조인 대상이 된다.

지켜야 할 것
------------
- **진입은 반드시 신호 시각 이후다.** `entry_date > signal_date`를 코드가 강제한다
  (아키텍처 §10 불변식 5). 당일 종가 체결은 없다.
- **없는 가격을 만들지 않는다.** 진입일·청산일 봉이 없으면 수익률을 null로 두고
  `ENTRY_UNAVAILABLE`/`EXIT_UNAVAILABLE` 사유를 남긴다. 직전 가격으로 메우면
  거래정지 종목의 수익률이 0으로 보이고, 그게 표본에 섞인다.
- **아직 끝나지 않은 구간은 `PENDING_MATURITY`다.** 캘린더 밖을 추정하지 않는다.
- **벤치마크가 없으면 초과수익은 null이다.** 0으로 대체하지 않는다(Stage 1 DoD).

보유기간 정의
-------------
`h=1`은 "진입일 종가 청산"이다. 즉 `exit_date = shift(entry_date, h - 1)`.
이 정의는 `label_version`에 묶여 있으므로 바꾸려면 버전을 올려야 한다.
"""

from __future__ import annotations

import bisect
from datetime import date
from decimal import Decimal

from .. import SCHEMA_VERSION
from ..contracts.label_schema import (
    DEFAULT_HORIZONS, ENTRY_RULES, LABEL_VERSION,
)
from ..domain.errors import ConfigError
from ..domain.ids import to_utc
from ..domain.market_rules import default_registry
from ..domain.status import LabelStatus, Session
from .costs import CostModel

__all__ = ["ForwardReturnBuilder", "BarSeries"]

_ONE = Decimal(1)


class BarSeries:
    """한 종목의 일봉을 날짜로 찾아 쓰기 좋게 감싼 것.

    고가·저가는 별도 배열로도 들고 있다. 보유기간 최대/최소를 구할 때 날짜를
    하나씩 캘린더로 따라가면 199종목 × 6,000일 × 5개 horizon에서 조회가 수천만
    번이 된다. 정렬된 날짜 위에서 구간을 잘라 쓰면 그 비용이 사라진다.
    """

    __slots__ = ("security_id", "by_date", "dates", "_highs", "_lows")

    def __init__(self, security_id: str, bars):
        self.security_id = security_id
        self.by_date = {}
        for bar in bars:
            day = bar["trading_date"]
            if day in self.by_date:
                raise ConfigError(
                    f"{security_id}: 같은 거래일이 두 번 있다 — {day}")
            self.by_date[day] = bar
        self.dates = sorted(self.by_date)
        self._highs = [Decimal(self.by_date[d]["high"]) for d in self.dates]
        self._lows = [Decimal(self.by_date[d]["low"]) for d in self.dates]

    def __len__(self):
        return len(self.dates)

    def get(self, day):
        return self.by_date.get(day)

    def window_extremes(self, entry_date, exit_date):
        """`[entry_date, exit_date]` 구간의 (최고가, 최저가, 봉 개수).

        이 종목이 그 구간에 가진 봉만 본다. 거래정지로 빠진 날은 세지 않으며,
        몇 개가 빠졌는지는 호출부가 캘린더 길이와 비교해 판단한다.
        """
        lo = bisect.bisect_left(self.dates, entry_date)
        hi = bisect.bisect_right(self.dates, exit_date)
        if lo >= hi:
            return None, None, 0
        return max(self._highs[lo:hi]), min(self._lows[lo:hi]), hi - lo

    @classmethod
    def group(cls, rows):
        """silver 일봉 row 목록 → {security_id: BarSeries}."""
        buckets = {}
        for row in rows:
            buckets.setdefault(row["security_id"], []).append(row)
        return {sid: cls(sid, bars) for sid, bars in buckets.items()}


class ForwardReturnBuilder:
    """일봉 패널에서 forward return row를 만든다."""

    def __init__(self, calendar, cost_model: CostModel = None,
                 horizons=DEFAULT_HORIZONS, entry_rule: str = "NEXT_OPEN",
                 registry=None, market: str = "KRX",
                 label_version: str = LABEL_VERSION, data_revision: int = 1):
        if entry_rule not in ENTRY_RULES:
            raise ConfigError(f"알 수 없는 entry_rule: {entry_rule}")
        self.calendar = calendar
        self.costs = cost_model or CostModel.from_config()
        self.horizons = tuple(sorted(int(h) for h in horizons))
        if any(h < 1 for h in self.horizons):
            raise ConfigError("horizon은 1 이상이어야 한다")
        self.entry_rule = entry_rule
        self.registry = registry or default_registry()
        self.market = market
        self.label_version = label_version
        self.data_revision = int(data_revision)

    # ── 단일 종목 ──────────────────────────────────────────

    def build_security(self, series: BarSeries, run_id: str,
                       benchmark: BarSeries = None, benchmark_id: str = None,
                       start: date = None, end: date = None):
        rows = []
        for signal_date in series.dates:
            if start and signal_date < start:
                continue
            if end and signal_date > end:
                continue
            if not self.calendar.is_trading_day(signal_date):
                # 캘린더에 없는 날짜는 신호일이 될 수 없다. 조용히 넘기지 않고
                # 캘린더 파생이 잘못됐다는 뜻이므로 바로 알린다.
                raise ConfigError(
                    f"{series.security_id}: 캘린더에 없는 거래일 {signal_date}")
            for horizon in self.horizons:
                rows.append(self._one(series, signal_date, horizon, run_id,
                                      benchmark, benchmark_id))
        return rows

    def build(self, series_map, run_id: str, benchmark: BarSeries = None,
              benchmark_id: str = None, start: date = None, end: date = None,
              on_progress=None):
        rows = []
        items = sorted(series_map.items())
        for i, (_sid, series) in enumerate(items, start=1):
            rows.extend(self.build_security(
                series, run_id, benchmark, benchmark_id, start, end))
            if on_progress:
                on_progress(i, len(items), len(rows))
        return rows

    # ── 한 (종목, 날짜, horizon) ───────────────────────────

    def _one(self, series, signal_date, horizon, run_id, benchmark,
             benchmark_id):
        row = self._blank(series.security_id, signal_date, horizon, run_id)

        if self.entry_rule == "NEXT_CLOSE":
            # 전일 종가로 판정하고 다음 거래일 마감 동시호가에 산다.
            # 주문은 장중에 MOC로 넣으면 되므로 순환이 없다.
            entry_date = self.calendar.next_trading_date(signal_date)
            if entry_date is None:
                row["label_status"] = LabelStatus.PENDING_MATURITY
                row["status_reason"] = "다음 거래일이 아직 캘린더에 없다"
                return row
            exit_date = self.calendar.shift(entry_date, horizon)
        elif self.entry_rule == "SAME_CLOSE":
            # 노출 구간을 NEXT_OPEN과 맞추려면 h-1이 아니라 h를 민다.
            entry_date = signal_date
            exit_date = self.calendar.shift(signal_date, horizon)
        else:
            entry_date = self.calendar.next_trading_date(signal_date)
            if entry_date is None:
                row["label_status"] = LabelStatus.PENDING_MATURITY
                row["status_reason"] = "다음 거래일이 아직 캘린더에 없다"
                return row
            # 불변식 5: 진입은 신호 시각 이후여야 한다.
            if entry_date <= signal_date:
                raise ConfigError(
                    f"진입일({entry_date})이 신호일({signal_date}) 이후가 아니다")
            exit_date = self.calendar.shift(entry_date, horizon - 1)

        if exit_date is None:
            row["label_status"] = LabelStatus.PENDING_MATURITY
            row["status_reason"] = f"{horizon}거래일 뒤가 캘린더 범위를 넘는다"
            return row
        # SAME_CLOSE는 진입일 종가에 사므로 같은 날 청산하면 수익이 항상
        # 0이다. 반면 NEXT_OPEN h=1은 "진입일 시가에 사서 그날 종가에 판다"
        # 이므로 exit == entry가 정상이다. 규칙별로 다르게 본다.
        if self.entry_rule in ("SAME_CLOSE", "NEXT_CLOSE") \
                and exit_date <= entry_date:
            raise ConfigError(
                f"청산일({exit_date})이 진입일({entry_date}) 이후가 아니다 "
                f"— SAME_CLOSE는 같은 날 사고 팔 수 없다")

        entry_bar = series.get(entry_date)
        if entry_bar is None:
            row["entry_date"] = entry_date
            row["exit_date"] = exit_date
            row["label_status"] = LabelStatus.ENTRY_UNAVAILABLE
            row["status_reason"] = "진입일 봉이 없다(거래정지·상장폐지 등)"
            return row

        exit_bar = series.get(exit_date)
        if exit_bar is None:
            row["entry_date"] = entry_date
            row["exit_date"] = exit_date
            row["label_status"] = LabelStatus.EXIT_UNAVAILABLE
            row["status_reason"] = "청산일 봉이 없다(거래정지·상장폐지 등)"
            return row

        entry_price = Decimal(
            entry_bar["close"]
            if self.entry_rule in ("SAME_CLOSE", "NEXT_CLOSE")
            else entry_bar["open"])
        exit_price = Decimal(exit_bar["close"])
        if entry_price <= 0 or exit_price <= 0:
            row["entry_date"] = entry_date
            row["exit_date"] = exit_date
            row["label_status"] = LabelStatus.ENTRY_UNAVAILABLE
            row["status_reason"] = "가격이 0 이하다"
            return row

        gross = float(exit_price / entry_price - _ONE)
        net = self.costs.net_return(entry_price, exit_price)
        mfe, mae, window_gaps = self._excursion(
            series, entry_date, exit_date, entry_price, horizon)

        rule = self.registry.resolve(self.market, exit_date)
        row.update({
            "entry_date": entry_date,
            "exit_date": exit_date,
            "entry_at_utc": (
                self._regular_close_utc(entry_date)
                if self.entry_rule in ("SAME_CLOSE", "NEXT_CLOSE")
                else self._session_open_utc(entry_date)),
            "exit_at_utc": to_utc(rule.daily_confirmed_at(exit_date)),
            "entry_price": entry_price,
            "exit_price": exit_price,
            "stock_return_gross": gross,
            "stock_return_net": net,
            "mfe": mfe,
            "mae": mae,
            "up": net > 0,
            "up_gross": gross > 0,
            "hit_3pct": (mfe >= 0.03) if mfe is not None else None,
            "hit_5pct": (mfe >= 0.05) if mfe is not None else None,
            "hit_minus_3pct": (mae <= -0.03) if mae is not None else None,
            "hit_minus_5pct": (mae <= -0.05) if mae is not None else None,
            "commission": float(self.costs.commission_rate),
            "tax": float(self.costs.tax_rate_sell),
            "slippage": float(self.costs.slippage_rate),
            "label_status": LabelStatus.COMPLETE,
            "market_rule_version": rule.version,
        })
        if window_gaps:
            row["status_reason"] = (
                f"보유기간 중 {window_gaps}거래일의 봉이 없어 MFE/MAE가 "
                f"그만큼 덜 반영됐다")

        self._attach_benchmark(row, benchmark, benchmark_id, entry_date,
                               exit_date, gross, net)
        return row

    #: 정규장 마감 후 종가로 체결할 수 있게 되기까지의 지연(분).
    #: 한국의 시간외 종가매매는 마감 10분 뒤에 열린다.
    #:
    #: **2026-09-22 이후 SAME_CLOSE는 이 경로를 쓰지 않는다.** 사용자 지정에
    #: 따라 마감 동시호가 체결로 바꿨다(`_regular_close_utc`). 시간외 경로를
    #: 다시 보고 싶을 때를 위해 헬퍼는 남겨 둔다.
    AFTER_HOURS_CLOSE_LAG_MIN = 10

    def _regular_close_utc(self, trading_date):
        """정규장 마감 시각 = 마감 동시호가 체결 시각.

        NEXT_CLOSE는 이 시각에 체결된다. 신호는 **전 거래일** 마감 뒤이므로
        주문 시각이 체결 시각보다 앞선다 — 순환이 없다.
        """
        rule = self.registry.resolve(self.market, trading_date)
        bounds = rule.session_bounds(trading_date, Session.REGULAR)
        if bounds is None:
            return to_utc(rule.daily_confirmed_at(trading_date))
        return to_utc(bounds[1])

    def _after_hours_close_utc(self, trading_date):
        """종가에 **실제로 체결할 수 있는** 가장 이른 시각.

        SAME_CLOSE는 15:30에 확정된 가격을 그 뒤에 사는 것이므로, 정규장
        안에서는 성립하지 않는다. 유일한 경로가 시간외 종가매매이고 그
        창은 마감 10분 뒤에 열린다. 이 시각을 진입 시각으로 박아 두면
        look-ahead 검사가 **의미 있게** 작동한다 — 신호 시각이 이보다
        늦으면 그날은 살 수 없다는 뜻이다.

        세션이 정의되지 않은 구간(daily_only)은 일봉 확정 시각을 쓴다.
        """
        from datetime import timedelta

        rule = self.registry.resolve(self.market, trading_date)
        bounds = rule.session_bounds(trading_date, Session.REGULAR)
        if bounds is None:
            # 세션이 정의되지 않은 구간(daily_only) — 일봉 확정 시각을 쓴다.
            return to_utc(rule.daily_confirmed_at(trading_date))
        return to_utc(bounds[1]
                      + timedelta(minutes=self.AFTER_HOURS_CLOSE_LAG_MIN))

    def _excursion(self, series, entry_date, exit_date, entry_price, horizon):
        """보유기간 내 장중 고가·저가 기준 최대 유리/불리 수익.

        `horizon`은 캘린더 기준 거래일 수다. 이 종목이 그중 몇 개를 실제로
        가지고 있는지 비교해, 빠진 날이 있으면 그 수를 함께 돌려준다.
        """
        # SAME_CLOSE는 진입일 **종가**에 사므로 그날의 장중 고저에는
        # 노출되지 않는다. 진입일을 창에 넣으면 사기 전의 움직임을
        # 최대 유리/불리로 세게 된다.
        first = entry_date
        if self.entry_rule in ("SAME_CLOSE", "NEXT_CLOSE"):
            # 종가에 샀으므로 진입일 장중 고저에는 노출되지 않는다.
            first = self.calendar.shift(entry_date, 1) or entry_date
        high, low, count = series.window_extremes(first, exit_date)
        missing = max(0, horizon - count)
        if high is None:
            return None, None, missing
        mfe = float(high / entry_price - _ONE)
        mae = float(low / entry_price - _ONE)
        return mfe, mae, missing

    def _attach_benchmark(self, row, benchmark, benchmark_id, entry_date,
                          exit_date, gross, net):
        if benchmark is None:
            # 벤치마크가 없으면 초과수익은 계산하지 않는다. 0으로 대체하면
            # "시장만큼은 했다"는 거짓 정보가 표본에 들어간다.
            return
        b_entry = benchmark.get(entry_date)
        b_exit = benchmark.get(exit_date)
        if b_entry is None or b_exit is None:
            row["status_reason"] = (row.get("status_reason") or "") + \
                " 벤치마크 가격 없음".strip()
            return
        b_open = Decimal(
            b_entry["close"]
            if self.entry_rule in ("SAME_CLOSE", "NEXT_CLOSE")
            else b_entry["open"])
        b_close = Decimal(b_exit["close"])
        if b_open <= 0:
            return
        b_return = float(b_close / b_open - _ONE)
        row["benchmark_id"] = benchmark_id
        row["benchmark_return"] = b_return
        row["excess_return_gross"] = gross - b_return
        row["excess_return_net"] = net - b_return
        row["outperform"] = net > b_return

    def _session_open_utc(self, entry_date):
        rule = self.registry.resolve(self.market, entry_date)
        bounds = rule.session_bounds(entry_date, "REGULAR")
        # 세션이 정의되지 않은 과거 구간은 시가 체결 시각을 모른다 — null.
        return to_utc(bounds[0]) if bounds else None

    def _blank(self, security_id, signal_date, horizon, run_id):
        return {
            "security_id": security_id,
            "signal_date": signal_date,
            "horizon_td": int(horizon),
            "entry_rule": self.entry_rule,
            "entry_date": None, "exit_date": None,
            "entry_at_utc": None, "exit_at_utc": None,
            "entry_price": None, "exit_price": None,
            "stock_return_gross": None, "stock_return_net": None,
            "benchmark_id": None, "benchmark_return": None,
            "excess_return_gross": None, "excess_return_net": None,
            "mfe": None, "mae": None,
            "up": None, "up_gross": None, "outperform": None,
            "hit_3pct": None, "hit_5pct": None,
            "hit_minus_3pct": None, "hit_minus_5pct": None,
            "commission": None, "tax": None, "slippage": None,
            "label_status": LabelStatus.PENDING_MATURITY,
            "status_reason": None,
            "label_version": self.label_version,
            "cost_model_version": self.costs.version,
            "market_rule_version": self.registry.resolve(
                self.market, signal_date).version,
            "run_id": run_id,
            "schema_version": SCHEMA_VERSION,
            "data_revision": self.data_revision,
        }

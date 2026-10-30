# -*- coding: utf-8 -*-
"""시장제도 레지스트리 — 아키텍처 §6 `market_rules`.

`market_rule_version`은 코드 버전과 독립이다. 과거 날짜를 오늘의 제도로 다시
해석하지 않기 위해, 시행일 구간별로 규칙을 고르고 그 버전을 artifact에 남긴다.

2026-09-14 애프터마켓 신설이 대표적인 예다. 시행일 이전의 16:00~18:00 체결은
폐지된 시간외단일가라 성격이 다르므로, 같은 시간대를 같은 세션으로 묶으면
안 된다.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, time, timedelta

from .errors import ConfigError
from .ids import KST
from .status import DetailCode, Session

__all__ = ["MarketRule", "MarketRuleRegistry", "default_registry",
           "CONFIG_DIR"]

CONFIG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))),
    "configs", "market_rules")


def _hhmm_to_time(value) -> time:
    """`900` / `1530` 정수를 `time`으로. V1.5 세션 정의 형식을 그대로 받는다."""
    v = int(value)
    return time(hour=v // 100, minute=v % 100)


class MarketRule:
    """한 시행 구간의 시장 규칙."""

    __slots__ = ("version", "market", "timezone", "effective_from", "sessions",
                 "after_market_start_date", "bar_timestamp_is_start",
                 "session_complete_grace_min", "daily_close_semantics",
                 "price_limit_pct", "source", "daily_only",
                 "daily_confirmed_at_kst", "verification_status")

    def __init__(self, payload, source=None):
        self.version = str(payload["market_rule_version"])
        self.market = str(payload.get("market", "KRX"))
        self.timezone = str(payload.get("timezone", "Asia/Seoul"))
        self.effective_from = date.fromisoformat(payload["effective_from"])
        self.sessions = {
            name: {
                "start": _hhmm_to_time(cfg["start"]),
                "end": _hhmm_to_time(cfg["end"]),
                "minutes": int(cfg["minutes"]),
                "continuous_minutes": int(cfg["continuous_minutes"]),
            }
            for name, cfg in payload["sessions"].items()
        }
        for name in self.sessions:
            Session.validate(name)
        self.after_market_start_date = str(
            payload.get("after_market_start_date") or "")
        self.bar_timestamp_is_start = bool(
            payload.get("bar_timestamp_is_start", True))
        self.session_complete_grace_min = int(
            payload.get("session_complete_grace_min", 0))
        self.daily_close_semantics = dict(
            payload.get("daily_close_semantics") or {})
        self.price_limit_pct = payload.get("price_limit_pct")
        self.source = source
        # 세션을 정의하지 않은 구간(1차 출처 미확인). 일봉 전용으로만 쓴다.
        self.daily_only = bool(payload.get("daily_only", False))
        self.daily_confirmed_at_kst = payload.get("daily_confirmed_at_kst")
        self.verification_status = payload.get(
            "verification_status", "VERIFIED")
        if not self.sessions and not self.daily_only:
            raise ConfigError(
                f"{self.version}: 세션이 없는 규칙은 daily_only여야 한다")

    def __repr__(self):
        return f"MarketRule({self.version!r}, from {self.effective_from})"

    # ── 세션 ───────────────────────────────────────────────

    def session_exists(self, trading_date: date, session: str) -> bool:
        """그 날짜에 해당 세션이 제도적으로 존재했는가.

        애프터마켓은 시행일 이전에는 존재하지 않는다. "무체결"과 "제도 없음"은
        전혀 다른 사실이므로 구분한다.
        """
        Session.validate(session)
        if session not in self.sessions:
            return False
        if session == Session.AFTER and self.after_market_start_date:
            start = datetime.strptime(
                self.after_market_start_date, "%Y%m%d").date()
            return trading_date >= start
        return True

    def session_bounds(self, trading_date: date, session: str):
        """세션의 `[시작, 종료)` KST datetime. 없는 세션이면 None."""
        if not self.session_exists(trading_date, session):
            return None
        cfg = self.sessions[session]
        start = datetime.combine(trading_date, cfg["start"], tzinfo=KST)
        end = datetime.combine(trading_date, cfg["end"], tzinfo=KST)
        return (start, end)

    def session_complete_at(self, trading_date: date, session: str):
        """세션이 확정됐다고 볼 수 있는 시각(종료 + 여유)."""
        bounds = self.session_bounds(trading_date, session)
        if bounds is None:
            return None
        return bounds[1] + timedelta(minutes=self.session_complete_grace_min)

    def is_bar_complete(self, bar_end_at, as_of) -> bool:
        """봉이 `as_of` 시점에 완결됐는가. 경계는 포함한다(`bar_end <= as_of`)."""
        return bar_end_at <= as_of

    def daily_scope_of(self, trading_date: date) -> str:
        """그 날짜 일봉이 어느 세션까지 반영하는가 → 스키마 `price_scope`.

        2026-09-16 실측: 키움 일봉 종가는 애프터마켓 최종 체결가다. 애프터마켓
        시행 이전 일봉은 정규장까지만 반영한다. 같은 열에 두 정의가 섞이므로
        `price_scope`로 구분해 저장한다.
        """
        if self.session_exists(trading_date, Session.AFTER) and \
                self.daily_close_semantics.get("includes_after_market"):
            return "EXTENDED"
        return "REGULAR"

    def daily_confirmed_at(self, trading_date: date):
        """그 날짜 일봉이 확정되는 시각(KST).

        세션을 정의하지 않은 구간(`daily_only`)에서는 설정에 적힌 보수적
        가정 시각을 쓴다. 추정임을 잊지 않도록 `verification_status`에 남긴다.
        """
        if self.daily_only:
            if not self.daily_confirmed_at_kst:
                raise ConfigError(
                    f"{self.version}: daily_confirmed_at_kst가 없다")
            hh, mm = (int(x) for x in
                      str(self.daily_confirmed_at_kst).split(":")[:2])
            return datetime.combine(trading_date, time(hh, mm), tzinfo=KST)
        scope = self.daily_scope_of(trading_date)
        session = Session.AFTER if scope == "EXTENDED" else Session.REGULAR
        return self.session_complete_at(trading_date, session)


class MarketRuleRegistry:
    """시행일 구간별 규칙 모음."""

    def __init__(self, rules=()):
        self._rules = sorted(rules, key=lambda r: r.effective_from)
        versions = [r.version for r in self._rules]
        if len(set(versions)) != len(versions):
            raise ConfigError(f"market_rule_version 중복: {versions}")

    def __len__(self):
        return len(self._rules)

    def versions(self):
        return [r.version for r in self._rules]

    @classmethod
    def from_dir(cls, directory: str = None):
        target = directory or CONFIG_DIR
        if not os.path.isdir(target):
            raise ConfigError(f"market rule 디렉터리가 없다: {target}")
        rules = []
        for name in sorted(os.listdir(target)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(target, name)
            with open(path, encoding="utf-8") as f:
                rules.append(MarketRule(json.load(f), source=path))
        if not rules:
            raise ConfigError(f"market rule 파일이 없다: {target}")
        return cls(rules)

    def resolve(self, market: str, at) -> MarketRule:
        """그 시점에 유효한 규칙. 없으면 실행을 막는다."""
        day = at.date() if isinstance(at, datetime) else at
        found = None
        for rule in self._rules:
            if rule.market == market and rule.effective_from <= day:
                found = rule
        if found is None:
            raise ConfigError(
                f"{market} {day}에 적용할 market rule이 없다 — "
                f"보유 버전 {self.versions()}",
                detail_code=DetailCode.MARKET_RULE_UNRESOLVED)
        return found

    def by_version(self, version: str) -> MarketRule:
        for rule in self._rules:
            if rule.version == version:
                return rule
        raise ConfigError(
            f"알 수 없는 market_rule_version: {version}",
            detail_code=DetailCode.MARKET_RULE_UNRESOLVED)


_default = None


def default_registry() -> MarketRuleRegistry:
    global _default
    if _default is None:
        _default = MarketRuleRegistry.from_dir()
    return _default

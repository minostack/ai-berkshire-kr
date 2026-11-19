# -*- coding: utf-8 -*-
"""장 시간이 통째로 늦춰진 날(수능 등) — 세션 경계 이동.

2026-11-19(수능)은 KRX·NXT 모두 개장·마감이 1시간 늦었다(실측: KRX 10:00~16:19
접속매매, 16:30 종가 단일가, 17:00 애프터마켓). 세션 판정(V1.5 `session_of`, 해시 동결)과
종가 단일가 시각(15:30)은 고정 시각이라, 그런 날은 봉 시각을 이동분만큼 **앞으로 당겨**
평일 시계로 판정한다. 봉의 실제 시각(`bar_start_at_utc`)은 바꾸지 않는다.

설정: `configs/session_shifts.json` — 실측으로 확인한 날만 넣는다.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone

__all__ = ["SHIFT_FILE", "shift_of", "shifted_days", "clock_dt_str",
           "clock_time", "dedupe_shifted_bars", "relabel_session"]

SHIFT_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))),
    "configs", "session_shifts.json")

_cache = None


def _load():
    global _cache
    if _cache is None:
        days = {}
        if os.path.exists(SHIFT_FILE):
            with open(SHIFT_FILE, encoding="utf-8") as f:
                payload = json.load(f)
            for key, cfg in (payload.get("days") or {}).items():
                days[date.fromisoformat(key)] = int(cfg["shift_min"])
        _cache = days
    return _cache


def _as_date(day):
    if isinstance(day, datetime):
        return day.date()
    if isinstance(day, str):
        s = day.replace("-", "")[:8]
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    return day


def shifted_days() -> dict:
    """{날짜: 이동 분}."""
    return dict(_load())


def shift_of(day) -> timedelta:
    """그날 세션 경계 이동량(평일이면 0)."""
    return timedelta(minutes=_load().get(_as_date(day), 0))


def clock_dt_str(dt_str: str) -> str:
    """`YYYYMMDDHHMMSS` 봉 시각 → 평일 시계 시각 문자열(이동 없는 날은 그대로)."""
    s = str(dt_str)
    delta = shift_of(s[:8]) if len(s) >= 8 and s[:8].isdigit() else timedelta(0)
    if not delta or len(s) < 12:
        return s
    t = datetime.strptime(s[:14].ljust(14, "0"), "%Y%m%d%H%M%S") - delta
    return t.strftime("%Y%m%d%H%M%S")[:len(s)]


def clock_time(at_kst: datetime, day=None):
    """KST 시각 → 평일 시계의 `time`."""
    delta = shift_of(day if day is not None else at_kst)
    return (at_kst - delta).time()


def dedupe_shifted_bars(rows):
    """이동한 날은 같은 봉 시각에 행이 하나만 남게 한다(최신 리비전, 그다음 나중 run_id).

    세션이 분봉 논리 키에 들어 있어, 고정 시각 라벨(OTHER)로 먼저 저장된 봉과 평일 시계
    라벨(REGULAR)로 다시 받은 봉이 둘 다 남는다 — 그대로 더하면 거래량이 두 번 잡힌다.
    행: `security_id`, `trading_date`, `bar_start_at_utc`, `data_revision`, `run_id` 열이 있는 dict.
    평일 행은 손대지 않는다.
    """
    from .ids import run_started_at
    days = _load()
    if not days:
        return list(rows)
    out, best = [], {}
    for r in rows:
        if _as_date(r["trading_date"]) not in days:
            out.append(r)
            continue
        key = (r["security_id"], r["bar_start_at_utc"])
        rank = (int(r.get("data_revision") or 0),
                run_started_at(r.get("run_id")) or datetime.min.replace(
                    tzinfo=timezone.utc),
                str(r.get("run_id") or ""))
        if key not in best or rank > best[key][0]:
            best[key] = (rank, r)
    out.extend(r for _rank, r in best.values())
    return out


def relabel_session(session: str, at_kst: datetime, day=None, scanner=None) -> str:
    """이동한 날이면 평일 시계로 세션을 다시 판정한다. 저장된 라벨이 고정 시각으로
    붙었을 수 있어서다(V1.5 `session_of`). 평일이면 저장된 라벨 그대로."""
    if not shift_of(day if day is not None else at_kst):
        return session
    if scanner is None:
        from ..baseline.scanner_v15 import load_scanner_v15
        scanner = load_scanner_v15()
    return scanner.session_of(clock_dt_str(at_kst.strftime("%Y%m%d%H%M%S")))

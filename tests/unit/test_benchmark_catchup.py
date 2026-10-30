# -*- coding: utf-8 -*-
"""지수 일봉 보충기 — "마지막 적재일 다음부터만 받는다"를 고정한다.

이 규칙이 깨지면 `silver.benchmark_bars`에 중복 키가 하루 13,000행씩 쌓인다.
적재기가 `data_revision=1`을 고정으로 쓰고 reader가 날짜 키로 덮어쓰기 때문에,
중복이 생기면 **어느 값이 쓰일지가 파일 순서에 달린다**
(`reports/NXTBASIS-KRX단독-통합기준-비교-20261019.md` §7-7).
"""

import os
import sys
from datetime import date

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (os.path.join(_ROOT, "tools"), os.path.join(_ROOT, "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import benchmark_catchup as bc                  # noqa: E402

IDS = ["KRX:KOSPI200", "KRX:KOSPI", "KRX:KOSDAQ"]
END = date(2026, 10, 19)


def test_마지막_적재일_다음_거래일부터_받는다():
    last = {b: date(2026, 9, 22) for b in IDS}
    groups, skipped = bc.plan(last, IDS, END)
    assert skipped == []
    assert list(groups) == [date(2026, 9, 23)]
    assert sorted(groups[date(2026, 9, 23)]) == sorted(IDS)


def test_이미_최신이면_아무것도_받지_않는다():
    last = {b: END for b in IDS}
    groups, skipped = bc.plan(last, IDS, END)
    assert groups == {}
    assert [b for b, _ in skipped] == IDS


def test_끝보다_앞서_있어도_받지_않는다():
    """지수가 end보다 더 최신이면(수동 적재 등) 덮어쓰지 않는다."""
    last = {b: date(2026, 10, 30) for b in IDS}
    groups, _ = bc.plan(last, IDS, END)
    assert groups == {}


def test_마지막_적재일이_다르면_따로_받는다():
    last = {"KRX:KOSPI200": date(2026, 10, 19),
            "KRX:KOSPI": date(2026, 9, 30),
            "KRX:KOSDAQ": date(2026, 9, 30)}
    groups, skipped = bc.plan(last, IDS, END)
    assert [b for b, _ in skipped] == ["KRX:KOSPI200"]
    assert list(groups) == [date(2026, 10, 1)]
    assert sorted(groups[date(2026, 10, 1)]) == ["KRX:KOSDAQ", "KRX:KOSPI"]


def test_적재된_것이_없으면_전_구간이다():
    groups, skipped = bc.plan({}, IDS, END)
    assert skipped == []
    assert list(groups) == [None]          # None = 전 구간
    assert sorted(groups[None]) == sorted(IDS)


def test_일부만_비어_있으면_전_구간과_증분을_나눈다():
    last = {"KRX:KOSPI200": date(2026, 9, 22)}
    groups, _ = bc.plan(last, IDS, END)
    assert set(groups) == {None, date(2026, 9, 23)}
    assert groups[date(2026, 9, 23)] == ["KRX:KOSPI200"]
    assert sorted(groups[None]) == ["KRX:KOSDAQ", "KRX:KOSPI"]


def test_받는_구간은_절대_겹치지_않는다():
    """어떤 마지막 적재일 조합에도 start > last 여야 한다 — 중복 방지의 핵심."""
    for day in (date(2026, 1, 2), date(2026, 6, 30), date(2026, 10, 16)):
        last = {b: day for b in IDS}
        groups, _ = bc.plan(last, IDS, END)
        for start in groups:
            assert start is not None and start > day


def test_기본_지수는_셋이다():
    assert bc.DEFAULT_BENCHMARKS == ("KRX:KOSPI200", "KRX:KOSPI", "KRX:KOSDAQ")

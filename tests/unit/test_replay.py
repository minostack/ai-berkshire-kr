# -*- coding: utf-8 -*-
"""과거 replay — 15:35에 알 수 없던 것은 보이지 않아야 한다(Stage 5 DoD).

replay의 값어치는 전부 한 가지에 달려 있다. **그 시점에 알 수 있었던 것만
보는가.** 15:40에 끝난 봉이 15:35 신호에 섞이면 그 백테스트는 미래를 본
것이고, 뒤에 나오는 통계는 전부 의미가 없다.

그래서 여기서는 metamorphic으로 확인한다 — **미래 봉을 일부러 넣어도 결과가
바뀌지 않아야 한다.** 불변식을 코드로 읽는 대신 결과로 확인하는 편이 낫다.
"""

from datetime import date, datetime, timedelta

import pytest

from cvd_obv.domain.ids import KST, to_utc
from cvd_obv.replay.historical import _digest
from cvd_obv.replay.scanner_core import CacheBackedScanner, ReplayNetworkError
from cvd_obv.replay.windows import MinuteWindow

pa = pytest.importorskip("pyarrow")

DAY = date(2026, 9, 16)


def _bar(code, hhmm, close=1000.0, volume=10):
    start = datetime(DAY.year, DAY.month, DAY.day, hhmm // 100, hhmm % 100,
                     tzinfo=KST)
    end = start + timedelta(minutes=5)
    return {"security_id": f"KRX:{code}", "period_min": 5,
            "trading_date": DAY, "session": "REGULAR",
            "bar_start_at_utc": to_utc(start), "bar_end_at_utc": to_utc(end),
            "open": close, "high": close, "low": close, "close": close,
            "volume": volume, "quality_status": "SUCCESS"}


class FakeStore:
    """`read_arrow`만 흉내 내는 최소 저장소."""

    def __init__(self, rows):
        self.rows = rows

    def read_arrow(self, layer, table, columns=None, dedupe_pk=None, **kwargs):
        data = {name: [row[name] for row in self.rows]
                for name in (columns or self.rows[0])}
        return pa.table(data)


@pytest.fixture()
def window():
    rows = [_bar("005930", hhmm) for hhmm in (900, 1520, 1530, 1540, 1700)]
    return MinuteWindow(FakeStore(rows), ["005930"], DAY, DAY, minute_source="native")


def test_15시35분에는_그때_끝난_봉까지만_보인다(window):
    as_of = datetime(2026, 9, 16, 15, 35, tzinfo=KST)
    bars = window.slice_as_of("005930", as_of)
    ends = [b["bar_end"].strftime("%H:%M") for b in bars]
    assert ends == ["09:05", "15:25", "15:35"]


def test_미래_봉을_넣어도_15시35분_결과는_같다():
    """metamorphic — 15:35 이후 봉을 인위적으로 더해도 결과가 변하지 않는다."""
    as_of = datetime(2026, 9, 16, 15, 35, tzinfo=KST)
    base = [_bar("005930", hhmm) for hhmm in (900, 1520, 1530)]
    tampered = base + [_bar("005930", hhmm, close=99999.0, volume=10 ** 9)
                       for hhmm in (1540, 1700, 1900)]

    before = MinuteWindow(FakeStore(base), ["005930"], DAY, DAY, minute_source="native")
    after = MinuteWindow(FakeStore(tampered), ["005930"], DAY, DAY, minute_source="native")
    assert (before.slice_as_of("005930", as_of)
            == after.slice_as_of("005930", as_of))


def test_품질_실패_봉은_쓰지_않는다():
    rows = [_bar("005930", 900), _bar("005930", 1520)]
    rows[1]["quality_status"] = "FAILED"
    window = MinuteWindow(FakeStore(rows), ["005930"], DAY, DAY, minute_source="native")
    bars = window.slice_as_of("005930",
                              datetime(2026, 9, 16, 15, 35, tzinfo=KST))
    assert [b["bar_end"].strftime("%H:%M") for b in bars] == ["09:05"]


def test_replay_중_API를_부르면_예외다():
    """cache가 비어 조용히 API로 새는 것을 막는다."""
    core = CacheBackedScanner()
    with core._injected("005930", [], [], "테스트"):
        with pytest.raises(ReplayNetworkError):
            core.scanner._kiwoom("/x", {}, "ka10081", "token")
        with pytest.raises(ReplayNetworkError):
            core.scanner._get_token()
    # 빠져나오면 원래 함수가 돌아와야 한다.
    assert core.scanner._kiwoom.__name__ != "_blocked"


def test_다른_종목을_요청하면_막는다():
    from cvd_obv.domain.errors import ConfigError

    core = CacheBackedScanner()
    with core._injected("005930", [], [], "테스트"):
        with pytest.raises(ConfigError):
            core.scanner.fetch_minute_bars("000660", "token")


def test_지문은_실행마다_달라지는_값을_빼고_계산한다():
    """같은 입력이면 같은 지문이어야 재실행 검증이 된다."""
    def signal(run_id, created):
        return {"signal_id": "sig_a", "security_id": "KRX:005930",
                "status": "WATCH", "total_score": 50.0, "rank": 3,
                "run_id": run_id, "created_at_utc": created}

    first = [signal("run_1", "2026-09-18T00:00:00Z")]
    second = [signal("run_2", "2026-09-19T00:00:00Z")]
    assert _digest(first) == _digest(second)

    changed = [dict(first[0], status="BUY_CANDIDATE")]
    assert _digest(changed) != _digest(first)

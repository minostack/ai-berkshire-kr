# -*- coding: utf-8 -*-
"""실제 cache로 확인하는 metamorphic 성질 (Stage 5 DoD).

합성 데이터로 "미래 봉을 빼면 결과가 같다"를 확인하는 것은 쉽다. 진짜 확인은
**실제로 미래 봉이 들어 있는 저장소**에서 하는 것이다. 2026-09-16 분봉에는
15:35 이후 애프터마켓 봉이 실제로 들어 있다.

여기서 확인하는 것:
  1. 15:35 slice에는 그 이후 봉이 하나도 없다.
  2. 저장소에서 미래 봉을 아예 지우고 만든 slice와 **완전히 같다**.
  3. 그 두 입력으로 V1.5 `analyze()`를 돌린 결과가 같다.

cache가 없으면 건너뛴다. 네트워크는 타지 않는다.
"""

import glob
import os
from datetime import date, datetime

import pytest

from cvd_obv.domain.ids import KST, to_utc
from cvd_obv.replay.scanner_core import CacheBackedScanner
from cvd_obv.replay.windows import MinuteWindow

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PART = os.path.join(ROOT, "data", "silver", "minute_bars",
                    "period_min=5", "year=2026", "month=09")
CODE = "005930"
DAY = date(2026, 9, 16)
AS_OF = datetime(2026, 9, 16, 15, 35, tzinfo=KST)

COLUMNS = ["security_id", "period_min", "trading_date", "session",
           "bar_start_at_utc", "bar_end_at_utc", "open", "high", "low",
           "close", "volume", "quality_status"]


class FakeStore:
    def __init__(self, rows):
        self.rows = rows

    def read_arrow(self, layer, table, columns=None, dedupe_pk=None, **kwargs):
        names = columns or COLUMNS
        return pa.table({name: [row[name] for row in self.rows]
                         for name in names})


def _rows():
    files = glob.glob(os.path.join(PART, "**", "*.parquet"), recursive=True)
    if not files:
        pytest.skip("분봉 cache가 없다 — `minute-cache build` 먼저")
    out = []
    for path in files:
        table = pq.ParquetFile(path).read(columns=COLUMNS)
        for row in table.to_pylist():
            if row["security_id"] != f"KRX:{CODE}":
                continue
            out.append(row)
    if not out:
        pytest.skip(f"{CODE} 분봉이 없다")
    return out


@pytest.fixture(scope="module")
def rows():
    return _rows()


def test_15시35분_slice에는_그_이후_봉이_없다(rows):
    window = MinuteWindow(FakeStore(rows), [CODE], DAY, DAY, minute_source="native")
    bars = window.slice_as_of(CODE, AS_OF, lookback_trading_days=5)
    same_day = [b for b in bars if b["dt"].startswith("20260916")]
    assert same_day, "그날 봉이 하나도 없다"
    assert max(b["bar_end"] for b in same_day) == datetime(2026, 9, 16, 15, 35)


def test_미래_봉을_지워도_slice가_같다(rows):
    cutoff = to_utc(AS_OF)
    future = [r for r in rows if r["bar_end_at_utc"] > cutoff]
    assert future, "이 날짜에는 15:35 이후 봉이 없다 — 검증이 성립하지 않는다"

    full = MinuteWindow(FakeStore(rows), [CODE], DAY, DAY, minute_source="native")
    trimmed = MinuteWindow(
        FakeStore([r for r in rows if r["bar_end_at_utc"] <= cutoff]),
        [CODE], DAY, DAY, minute_source="native")
    assert (full.slice_as_of(CODE, AS_OF, lookback_trading_days=5)
            == trimmed.slice_as_of(CODE, AS_OF, lookback_trading_days=5))


def test_미래_봉이_있든_없든_분석_결과가_같다(rows):
    """계산까지 통과시켜 본다 — slice가 같으면 점수도 같아야 한다."""
    cutoff = to_utc(AS_OF)
    full = MinuteWindow(FakeStore(rows), [CODE], DAY, DAY, minute_source="native")
    trimmed = MinuteWindow(
        FakeStore([r for r in rows if r["bar_end_at_utc"] <= cutoff]),
        [CODE], DAY, DAY, minute_source="native")

    core = CacheBackedScanner()
    daily = [{"dt": f"2026090{i}", "open": 70000.0, "high": 71000.0,
              "low": 69000.0, "close": 70000.0 + i * 10, "volume": 1000000}
             for i in range(1, 10)]

    def _run(window):
        bars = window.slice_as_of(CODE, AS_OF, lookback_trading_days=5)
        return core.analyze(CODE, AS_OF, bars, daily, name=CODE)

    left, right = _run(full), _run(trimmed)
    assert (left is None) == (right is None)
    if left is None:
        pytest.skip("분석이 성립하지 않는 입력이다")
    assert left["raw_score"] == right["raw_score"]
    assert left["cvd"]["normalized_delta"] == right["cvd"]["normalized_delta"]
    assert left["bars_by_session"] == right["bars_by_session"]

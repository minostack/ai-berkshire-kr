# -*- coding: utf-8 -*-
"""거래 캘린더 — horizon과 `next_open`의 유일한 기준.

"영업일 = 주말 제외"로 계산하면 휴장일에서 어긋난다. 여기서 검증하는 핵심은
**휴장일을 건너뛰고, 범위를 벗어나면 추정하지 않고 None을 돌려준다**는 것이다.
성숙하지 않은 label을 0이나 마지막 값으로 채우면 수익률이 거짓이 된다.
"""

from datetime import date

import pytest

from cvd_obv.domain.calendar import CALENDAR_SOURCE_DERIVED, TradingCalendar
from cvd_obv.domain.errors import ConfigError, NoInputDataError
from cvd_obv.domain.status import Session

# 2026-09-12(토)·13(일) 휴장, 09-15는 거래일로 가정한 합성 캘린더
DAYS = ["20260908", "20260909", "20260910", "20260911",
        "20260914", "20260915", "20260916"]


@pytest.fixture
def cal():
    return TradingCalendar("KRX", DAYS)


def test_거래일_수와_범위(cal):
    assert len(cal) == 7
    assert cal.start == date(2026, 9, 8)
    assert cal.end == date(2026, 9, 16)


def test_주말은_거래일이_아니다(cal):
    assert cal.is_trading_day(date(2026, 9, 11))
    assert not cal.is_trading_day(date(2026, 9, 12))


def test_다음_거래일은_휴장일을_건너뛴다(cal):
    assert cal.next_trading_date(date(2026, 9, 11)) == date(2026, 9, 14)
    # 입력이 거래일이 아니어도 그 뒤 첫 거래일을 돌려준다.
    assert cal.next_trading_date(date(2026, 9, 13)) == date(2026, 9, 14)


def test_마지막_거래일_다음은_None이다(cal):
    """캘린더 밖을 추정하지 않는다 — PENDING_MATURITY의 근거."""
    assert cal.next_trading_date(date(2026, 9, 16)) is None


def test_이전_거래일(cal):
    assert cal.previous_trading_date(date(2026, 9, 14)) == date(2026, 9, 11)
    assert cal.previous_trading_date(date(2026, 9, 8)) is None


def test_horizon_이동은_거래일_기준(cal):
    assert cal.shift(date(2026, 9, 11), 1) == date(2026, 9, 14)
    # [08, 09, 10, 11, 14, 15, 16] — 08에서 4칸이 14다(주말 2일은 세지 않는다)
    assert cal.shift(date(2026, 9, 8), 3) == date(2026, 9, 11)
    assert cal.shift(date(2026, 9, 8), 4) == date(2026, 9, 14)
    assert cal.shift(date(2026, 9, 16), 0) == date(2026, 9, 16)
    assert cal.shift(date(2026, 9, 14), -1) == date(2026, 9, 11)


def test_범위를_벗어난_horizon은_None(cal):
    assert cal.shift(date(2026, 9, 16), 1) is None
    assert cal.shift(date(2026, 9, 8), -1) is None


def test_거래일이_아닌_날짜에서의_이동은_오류다(cal):
    """조용히 가까운 거래일로 밀어내면 horizon이 한 칸 어긋난다."""
    with pytest.raises(ConfigError):
        cal.shift(date(2026, 9, 12), 1)


def test_구간_거래일_수(cal):
    assert cal.count_between(date(2026, 9, 8), date(2026, 9, 16)) == 6
    assert cal.count_between(date(2026, 9, 16), date(2026, 9, 16)) == 0


def test_일봉에서_캘린더를_만든다():
    rows = [{"trading_date": d, "security_id": f"KRX:00593{i % 2}"}
            for i, d in enumerate(DAYS)]
    cal = TradingCalendar.from_daily_bars(rows)
    assert len(cal) == 7
    assert cal.source == CALENDAR_SOURCE_DERIVED
    assert cal.derived_from_securities == 2


def test_빈_입력은_거부한다():
    with pytest.raises(NoInputDataError):
        TradingCalendar("KRX", [])
    with pytest.raises(NoInputDataError):
        TradingCalendar.from_daily_bars([])


def test_저장용_row가_스키마_형태를_갖춘다(cal):
    rows = cal.to_rows()
    assert len(rows) == 7
    first, last = rows[0], rows[-1]
    assert first["previous_trading_date"] is None
    assert last["next_trading_date"] is None
    assert rows[3]["next_trading_date"] == date(2026, 9, 14)
    # 애프터마켓 시행 전후로 세션 정보가 달라진다.
    assert rows[3]["after_open_utc"] is None          # 2026-09-11
    assert rows[4]["after_open_utc"] is not None      # 2026-09-14
    assert rows[4]["daily_price_scope"] == "EXTENDED"
    assert all(r["is_trading_day"] for r in rows)


def test_세션_경계는_UTC로_나온다(cal):
    bounds = cal.session_bounds_utc(date(2026, 9, 16), Session.REGULAR)
    assert bounds[0].isoformat() == "2026-09-16T00:00:00+00:00"
    assert bounds[1].isoformat() == "2026-09-16T06:30:00+00:00"


def test_간격이_큰_구간을_보고한다():
    cal = TradingCalendar("KRX", ["20260101", "20260102", "20260201"])
    gaps = cal.gaps(max_gap_days=7)
    assert len(gaps) == 1
    assert gaps[0]["from"] == date(2026, 1, 2)
    assert gaps[0]["calendar_days"] == 30

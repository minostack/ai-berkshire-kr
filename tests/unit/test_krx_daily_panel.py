# -*- coding: utf-8 -*-
"""KRX 패널 일봉 — 수정주가가 아닌 데이터를 쓸 수 있게 만드는 보정.

상장폐지 종목의 일봉은 이 경로밖에 없다. 그런데 KRX 일별매매정보는 수정주가가
아니라서, 액면분할이 있으면 그날 수익률이 −98%가 된다(실측: 삼성전자
2018-05-04).

여기서 지키는 것은 **보정할 것만 보정하는 것**이다.
- 액면분할·병합: 주식수 k배 · 주가 1/k배 · 시가총액 그대로 · **정수비** → 보정
- 인적분할·유상증자: 시가총액이 함께 달라지거나 비율이 정수비가 아니다 → 보정
  하지 않고 `CAPITAL_CHANGE_UNADJUSTED`로 표시만 한다

잘못 보정하면 틀린 수익률이 맞는 것처럼 보인다. 그래서 애매하면 손대지 않는다.
"""

from datetime import date
from decimal import Decimal

import pytest

from cvd_obv.contracts.market_schema import DAILY_BAR_COLUMNS, DAILY_BAR_PK
from cvd_obv.contracts.schema import validate_rows
from cvd_obv.ingestion.krx_daily_panel import (
    CAPITAL_FLAG, PANEL_FLAG, SPLIT_FLAG, KrxDailyPanel, _clean_ratio,
)

RUN_ID = "test_20260918000000_aaaaaaaa"


def _bar(day, close, shares, volume=1000):
    return {"trading_date": day, "name": "테스트",
            "open": close, "high": close, "low": close, "close": close,
            "volume": volume, "traded_value": close * volume,
            "shares": shares, "mktcap": close * shares}


@pytest.fixture()
def panel():
    return KrxDailyPanel(api=None)


def test_정수비만_분할로_본다():
    assert _clean_ratio(50.0) and _clean_ratio(5.0)
    assert _clean_ratio(0.2) and _clean_ratio(0.1)      # 액면병합
    assert not _clean_ratio(0.6849)                      # NAVER 인적분할
    assert not _clean_ratio(1.3918) and not _clean_ratio(21.125)


def test_액면분할은_잡아서_과거를_보정한다(panel):
    series = [_bar(date(2026, 3, 2), 50000, 100),
              _bar(date(2026, 3, 3), 50000, 100),
              _bar(date(2026, 3, 4), 10000, 500),        # 5:1 분할
              _bar(date(2026, 3, 5), 10100, 500)]
    adjusted, events = panel.adjust(series)
    assert [e["kind"] for e in events] == ["SPLIT"]
    assert events[0]["factor"] == pytest.approx(5.0)
    # 분할 전 가격은 1/5로, 거래량은 5배로 맞춰진다.
    assert adjusted[0]["close"] == Decimal("10000.0000")
    assert adjusted[0]["volume"] == 5000
    assert adjusted[0]["adjustment_factor"] == pytest.approx(5.0)
    assert SPLIT_FLAG in adjusted[0]["flags"]
    # 분할 이후는 건드리지 않는다.
    assert adjusted[-1]["close"] == Decimal("10100")
    assert adjusted[-1]["adjustment_factor"] == 1.0


def test_인적분할은_보정하지_않고_표시만_한다(panel):
    """주식수와 주가가 반대로 움직여도 시가총액이 달라지면 분할이 아니다."""
    series = [_bar(date(2026, 3, 2), 100000, 1000),
              _bar(date(2026, 3, 3), 164000, 685)]       # 시총 +12%
    adjusted, events = panel.adjust(series)
    assert [e["kind"] for e in events] == ["CAPITAL"]
    assert all(b["adjustment_factor"] == 1.0 for b in adjusted)
    assert CAPITAL_FLAG in adjusted[1]["flags"]
    assert adjusted[0]["close"] == Decimal("100000")     # 원본 그대로


def test_주가가_그대로인_증자는_분할이_아니다(panel):
    """주식수만 21배가 되고 주가는 그대로면 자본이 들어온 것이다."""
    series = [_bar(date(2026, 3, 2), 5000, 100),
              _bar(date(2026, 3, 3), 5000, 2112)]
    adjusted, events = panel.adjust(series)
    assert [e["kind"] for e in events] == ["CAPITAL"]
    assert all(b["adjustment_factor"] == 1.0 for b in adjusted)


def test_작은_주식수_변동은_이벤트가_아니다(panel):
    series = [_bar(date(2026, 3, 2), 5000, 1000),
              _bar(date(2026, 3, 3), 5050, 1010)]        # +1%
    adjusted, events = panel.adjust(series)
    assert events == []
    assert all(b["adjustment_factor"] == 1.0 for b in adjusted)


def test_가격이_없는_날은_저장하지_않는다(panel):
    """거래정지일의 0원을 가격으로 저장하면 수익률이 −100%가 된다."""
    from datetime import datetime

    from cvd_obv.domain.ids import KST

    series = [_bar(date(2026, 3, 2), 5000, 1000),
              _bar(date(2026, 3, 3), 0, 1000, volume=0),
              _bar(date(2026, 3, 4), 5100, 1000)]
    adjusted, _ = panel.adjust(series)
    rows = panel.to_rows("005930", adjusted, RUN_ID, datetime.now(tz=KST))
    assert [r["trading_date"] for r in rows] == [date(2026, 3, 2),
                                                 date(2026, 3, 4)]
    validate_rows(rows, DAILY_BAR_COLUMNS, DAILY_BAR_PK, "silver.daily_bars")


def test_패널_행은_정규장_종가로_표시한다(panel):
    """2026-09-14 애프터마켓 시행 뒤에도 KRX 일별 종가는 정규장 종가다.

    실측(000660 2026-09-15): KRX 1,690,000 = 정규장 15:35 종가,
    키움 1,712,000 = 20:00 애프터마켓 종가. 시장 규칙의 기본 scope를 그대로
    쓰면 이 행이 EXTENDED로 잘못 표시된다.
    """
    from datetime import datetime

    from cvd_obv.domain.ids import KST

    series = [_bar(date(2026, 9, 17), 1745000, 730492365)]
    adjusted, _ = panel.adjust(series)
    rows = panel.to_rows("000660", adjusted, RUN_ID, datetime.now(tz=KST))
    assert rows[0]["price_scope"] == "REGULAR"
    assert rows[0]["source"] == "KRX_OPENAPI"
    assert PANEL_FLAG in rows[0]["quality_flags"]
    assert rows[0]["adjustment_type"] == "SPLIT_ADJUSTED"

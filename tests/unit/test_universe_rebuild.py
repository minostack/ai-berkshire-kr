# -*- coding: utf-8 -*-
"""구성종목 복원 — 검산을 통과하지 못한 추정은 받아들이지 않는다.

이 엔진의 전제는 하나다. 코스피200 지수의 상장시가총액은 구성종목 시총의 단순
합이라는 것(실측 오차 0원). 그래서 명단이 맞았는지 **매 거래일 증명**할 수 있다.

여기서 지키는 것:
- 차액이 한 종목의 시총과 정확히 같으면 그 종목을 특정한다.
- 같은 차액을 만드는 후보가 둘 이상이면 **손대지 않는다**(모호한 추정 금지).
- 공시 델타를 역적용해도 검산이 0원이 아니면 UNRESOLVED로 남긴다.
"""

from datetime import date

from cvd_obv.ingestion.krx_index_files import isin_to_code
from cvd_obv.ingestion.krx_notices import parse_effective_dates
from cvd_obv.ingestion.krx_openapi import market_for
from cvd_obv.ingestion.universe_rebuild import (
    UniverseRebuilder, locate_anchors,
)

DAYS = [date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4), date(2026, 3, 5)]


class FakeApi:
    """거래일별 시총과 지수 시총을 직접 주는 가짜 API."""

    def __init__(self, caps_by_day, membership_by_day):
        self._caps = caps_by_day
        self._members = membership_by_day

    # 복원기는 시장을 두 번째 인자로 넘긴다. 받아만 두고 쓰지 않는다 —
    # 이 가짜는 시장이 하나뿐이다.
    def stock_caps(self, bas_dd, market="KOSPI200"):
        return dict(self._caps[bas_dd])

    def index_caps(self, bas_dd, market="KOSPI200"):
        caps = self._caps[bas_dd]
        total = sum(caps[c] for c in self._members[bas_dd])
        return {"코스피 200": total}

    def checksum(self, members, bas_dd, index_name=None, market="KOSPI200"):
        caps = self.stock_caps(bas_dd)
        target = self.index_caps(bas_dd)["코스피 200"]
        ours = sum(caps[c] for c in members if c in caps)
        return {"diff": target - ours, "target": target, "ours": ours}


def _fixture(membership_by_day):
    caps = {}
    for i, day in enumerate(DAYS):
        bas = day.strftime("%Y%m%d")
        caps[bas] = {"000001": 1000 + i, "000002": 2000 + i,
                     "000003": 3000 + i, "000004": 7000 + i}
    return FakeApi(caps, membership_by_day)


def _members_map(by_day):
    return {day.strftime("%Y%m%d"): set(codes) for day, codes in by_day.items()}


def test_변경이_없으면_모든_거래일이_0원으로_맞는다():
    truth = _members_map({d: {"000001", "000002"} for d in DAYS})
    api = _fixture(truth)
    res = UniverseRebuilder(api).walk_backward(["000001", "000002"], DAYS)
    # 4거래일 전부 — 거꾸로 걸은 3일에 **출발점 하루**를 더한다.
    assert res.checked == 4
    assert res.matched == 4
    assert res.unresolved == 0
    assert len(res.periods) == 1
    assert res.periods[0] == (DAYS[0], frozenset({"000001", "000002"}))


def test_1대1_교체는_역산으로_특정한다():
    # 3/4부터 000003이 들어오고 000002가 빠졌다.
    truth = _members_map({
        DAYS[0]: {"000001", "000002"}, DAYS[1]: {"000001", "000002"},
        DAYS[2]: {"000001", "000003"}, DAYS[3]: {"000001", "000003"},
    })
    res = UniverseRebuilder(_fixture(truth)).walk_backward(
        ["000001", "000003"], DAYS)
    assert res.unresolved == 0
    assert [sorted(m) for _, m in res.periods] == [
        ["000001", "000002"], ["000001", "000003"]]
    assert res.periods[1][0] == DAYS[2]          # 시행일이 정확히 3/4
    solved = [e for e in res.events if "SOLVED" in e["kind"]]
    assert len(solved) == 1
    assert solved[0]["added_backward"] == ["000002"]
    assert solved[0]["removed_backward"] == ["000003"]


def test_공시_델타를_역적용한다():
    truth = _members_map({
        DAYS[0]: {"000001", "000002"}, DAYS[1]: {"000001", "000002"},
        DAYS[2]: {"000001", "000004"}, DAYS[3]: {"000001", "000004"},
    })
    deltas = {DAYS[2]: {"in": ["000004"], "out": ["000002"],
                        "source_ref": "KRX공지#1"}}
    res = UniverseRebuilder(_fixture(truth), deltas=deltas).walk_backward(
        ["000001", "000004"], DAYS)
    assert res.unresolved == 0
    announced = [e for e in res.events if "ANNOUNCED" in e["kind"]]
    assert len(announced) == 1
    assert announced[0]["note"].startswith("KRX공지#1")


def _ambiguous_api(membership_by_day):
    """시총이 똑같은 종목이 둘 있는 시장 — 차액만으로는 구별할 수 없다."""
    caps = {}
    for i, day in enumerate(DAYS):
        bas = day.strftime("%Y%m%d")
        caps[bas] = {"000001": 1000 + i, "000002": 2000 + i,
                     "000009": 2000 + i, "000004": 7000 + i}
    return FakeApi(caps, membership_by_day)


def test_같은_차액을_만드는_후보가_둘이면_손대지_않는다():
    """13자리가 우연히 맞을 수는 있다. 맞았을 때 조용히 심는 것이 더 나쁘다."""
    truth = _members_map({
        DAYS[0]: {"000001", "000002"}, DAYS[1]: {"000001", "000002"},
        DAYS[2]: {"000001"}, DAYS[3]: {"000001"},
    })
    api = _ambiguous_api(truth)
    res = UniverseRebuilder(api).walk_backward(["000001"], DAYS)
    assert res.unresolved >= 1
    assert any("UNRESOLVED" in e["kind"] for e in res.events)
    # 000002와 000009 중 어느 것도 명단에 넣지 않았다.
    assert all(sorted(m) == ["000001"] for _, m in res.periods)


def test_두_종목이_한날에_들어와도_푼다():
    """특례편입은 두 종목이 같은 날 들어오는 일이 흔하다(2021-09-10 실측)."""
    truth = _members_map({
        DAYS[0]: {"000001"}, DAYS[1]: {"000001"},
        DAYS[2]: {"000001", "000003", "000004"},
        DAYS[3]: {"000001", "000003", "000004"},
    })
    res = UniverseRebuilder(_fixture(truth)).walk_backward(
        ["000001", "000003", "000004"], DAYS)
    assert res.unresolved == 0
    assert sorted(res.periods[0][1]) == ["000001"]
    solved = [e for e in res.events if "SOLVED" in e["kind"]]
    assert len(solved) == 1
    assert solved[0]["removed_backward"] == ["000003", "000004"]


def test_전체명단_앵커가_있으면_그대로_교체하고_검산한다():
    truth = _members_map({
        DAYS[0]: {"000002", "000003"}, DAYS[1]: {"000002", "000003"},
        DAYS[2]: {"000001", "000004"}, DAYS[3]: {"000001", "000004"},
    })
    anchors = {DAYS[1]: {"members": ["000002", "000003"],
                         "source_ref": "첨부.xlsx"}}
    res = UniverseRebuilder(_fixture(truth), anchors=anchors).walk_backward(
        ["000001", "000004"], DAYS)
    assert res.unresolved == 0
    assert [sorted(m) for _, m in res.periods] == [
        ["000002", "000003"], ["000001", "000004"]]


def test_상장_전_종목은_명단에서_빠지고_기록된다():
    caps = {}
    for i, day in enumerate(DAYS):
        bas = day.strftime("%Y%m%d")
        caps[bas] = {"000001": 1000 + i}
        if i >= 2:                                 # 000005는 3/4 상장
            caps[bas]["000005"] = 500 + i
    truth = {d.strftime("%Y%m%d"): ({"000001", "000005"} if i >= 2
                                    else {"000001"})
             for i, d in enumerate(DAYS)}
    api = FakeApi(caps, truth)
    res = UniverseRebuilder(api).walk_backward(["000001", "000005"], DAYS)
    assert res.unresolved == 0
    assert any("NOT_LISTED" in e["kind"] for e in res.events)
    assert sorted(res.periods[0][1]) == ["000001"]


def test_ISIN을_종목코드로_바꾼다():
    assert isin_to_code("KR7000990002") == "000990"
    assert isin_to_code("005930") == "005930"
    assert isin_to_code("KR7  ") == ""
    assert isin_to_code(None) == ""


def test_시행일은_반영일_키워드를_먼저_본다():
    body = "심사기준일 '26.4.30 ㅇ 반영일 : '26.6.12(금) 세부내용은"
    assert parse_effective_dates(body, "2026-06-05")[0] == date(2026, 6, 12)


def test_앵커의_시행일은_공지_표기가_아니라_검산이_정한다():
    """공지 날짜 표기는 20년 동안 제각각이다. 항등식이 있으니 추측하지 않는다."""
    truth = _members_map({
        DAYS[0]: {"000001", "000002"}, DAYS[1]: {"000001", "000002"},
        DAYS[2]: {"000001", "000003"}, DAYS[3]: {"000001", "000003"},
    })
    api = _fixture(truth)
    # 공지에는 3/5로 적혀 있지만 실제로 이 명단이 맞는 첫날은 3/4다.
    anchors = {DAYS[3]: {"members": ["000001", "000003"], "source_ref": "첨부"}}
    located, dropped = locate_anchors(api, anchors, DAYS, window=3)
    assert dropped == []
    assert list(located) == [DAYS[2]]
    assert located[DAYS[2]]["parsed_date"] == DAYS[3].isoformat()
    assert located[DAYS[2]]["located_by"] == "CHECKSUM"


def test_어느_날도_맞지_않는_앵커는_버린다():
    """틀린 명단을 억지로 쓰면 그 구간이 통째로 오염된다. 쓰지 않는 편이 낫다."""
    truth = _members_map({d: {"000001", "000002"} for d in DAYS})
    anchors = {DAYS[2]: {"members": ["000003", "000004"], "source_ref": "첨부"}}
    located, dropped = locate_anchors(_fixture(truth), anchors, DAYS, window=3)
    assert located == {}
    assert len(dropped) == 1
    assert "검산이 맞는 날이 없다" in dropped[0]["reason"]


def test_못_메운_날은_구간으로_보고한다():
    """한 날을 놓치면 그 이전이 전부 어긋난다 — '며칠'이 아니라 '어느 구간'이다."""
    truth = _members_map({
        DAYS[0]: {"000001", "000002"}, DAYS[1]: {"000001", "000002"},
        DAYS[2]: {"000001"}, DAYS[3]: {"000001"},
    })
    res = UniverseRebuilder(_ambiguous_api(truth)).walk_backward(["000001"], DAYS)
    ranges = res.summary()["unresolved_ranges"]
    assert ranges
    assert ranges[0][0] <= DAYS[1].isoformat()


class _MarketRecordingApi:
    """어느 시장으로 불렸는지 기록하는 가짜 API.

    코스닥150을 복원하는데 유가증권 응답을 보면 명단 중 한 종목도 안 잡혀
    합이 0이 되고, 차액이 영영 안 닫힌다. 그러면 결과가 "앵커 0건 확정"으로
    조용히 비어 나올 뿐 오류가 아니다 — 그래서 호출 인자를 직접 붙잡는다.
    """

    def __init__(self):
        self.markets = []

    def stock_caps(self, bas_dd, market="KOSPI200"):
        self.markets.append(market_for(market).key)
        return {"000001": 100}

    def index_caps(self, bas_dd, market="KOSPI200"):
        mk = market_for(market)
        self.markets.append(mk.key)
        return {mk.index_name: 100}

    def checksum(self, members, bas_dd, index_name=None, market="KOSPI200"):
        mk = market_for(market)
        self.markets.append(mk.key)
        return {"diff": 0, "target": 100, "ours": 100}


def test_복원기는_받은_시장으로_KRX를_부른다():
    api = _MarketRecordingApi()
    UniverseRebuilder(api, market="KOSDAQ150").walk_backward(["000001"], DAYS)
    assert api.markets, "KRX를 한 번도 부르지 않았다"
    assert set(api.markets) == {"KOSDAQ150"}


def test_복원기는_시장의_대표지수로_검산한다():
    """지수명을 따로 안 주면 시장이 정한다. 기본값 '코스피 200'이 남으면
    코스닥 응답에서 그 이름을 못 찾아 target이 None이 된다."""
    assert UniverseRebuilder(_fixture({}), market="KOSDAQ150").index_name \
        == "코스닥 150"
    assert UniverseRebuilder(_fixture({})).index_name == "코스피 200"


def test_앵커_시행일_탐색도_받은_시장으로_부른다():
    api = _MarketRecordingApi()
    anchors = {DAYS[2]: {"members": ["000001"], "source_ref": "첨부"}}
    locate_anchors(api, anchors, DAYS, window=3, market="KOSDAQ150")
    assert set(api.markets) == {"KOSDAQ150"}

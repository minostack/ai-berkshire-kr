# -*- coding: utf-8 -*-
"""유니버스 기준 `universe-1.0` — `src/cvd_obv/universe.py`.

지키는 것
1. 이력이 덮는 날은 이력 구성이다.
2. 이력 마지막 관측일 뒤는 index_stocks.json과 이력 마지막 구성 중 더 최근 것이고, 경고가 붙는다.
3. 이력 시작 전에는 오늘 명단을 쓰지 않는다(생존편향) — 오류.
4. `strict=True`는 이력만 허용한다(연구용).
5. index_stocks.json 해석은 한 곳이다(새·옛 형식 모두).
6. 스캐너 입구(V1.5 `load_index` 대체, 종가매수·A·B)가 같은 명단을 낸다.
"""

import json
import os
import sys
from datetime import date

import pytest

from cvd_obv.domain.errors import ConfigError, NoInputDataError, SurvivorshipError
from cvd_obv.universe import (
    STATUS_HISTORICAL, STATUS_HISTORY_NEAREST, STATUS_INDEX_FILE,
    current_universe, flat_intervals, member_index, parse_indexes,
    read_index_file, universe_on,
)

HIST_START, HIST_END = date(2020, 1, 2), date(2026, 9, 28)


class FakeRepo:
    """이력: 2026-09-01 전에는 A·B, 그 뒤로는 A·C (KOSPI200). KOSDAQ150은 D."""

    def __init__(self, end=HIST_END, empty=()):
        self.end, self.empty = end, set(empty)

    def coverage(self, idx):
        if idx in self.empty:
            raise NoInputDataError("이력 없음")
        return {"observed_start": HIST_START, "observed_end": self.end}

    def get_members(self, idx, day):
        if idx == "KOSDAQ150":
            codes = ["000004"]
        else:
            codes = ["000001", "000002"] if day < date(2026, 9, 1) else ["000001", "000003"]
        return {"members": [f"KRX:{c}" for c in codes], "universe_version": "uv_test"}

    def membership_intervals(self, idx):
        if idx == "KOSDAQ150":
            return {"KRX:000004": [(HIST_START, None)]}
        return {"KRX:000002": [(HIST_START, date(2026, 9, 1))],
                "KRX:000003": [(date(2026, 9, 1), None)]}


def _write_file(tmp_path, as_of="2026-09-28", kospi=("000001", "000003"), old=False):
    stocks = [{"code": c, "name": f"종목{c}"} for c in kospi]
    if old:
        payload = {"kospi200": [s["code"] for s in stocks]}
    else:
        payload = {"kospi200": {"as_of": as_of, "updated": "2026-09-29 09:11",
                                "stocks": stocks},
                   "kosdaq150": {"as_of": as_of, "updated": "x",
                                 "stocks": [{"code": "000004", "name": "코스닥4"}]}}
    path = tmp_path / "index_stocks.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_지수_인자_해석():
    assert parse_indexes("both") == ("KOSPI200", "KOSDAQ150")
    assert parse_indexes("kospi200") == ("KOSPI200",)
    assert parse_indexes("KOSDAQ150,kospi") == ("KOSDAQ150", "KOSPI200")
    assert parse_indexes(None) == ("KOSPI200", "KOSDAQ150")
    with pytest.raises(ConfigError):
        parse_indexes("nasdaq")


def test_index_파일은_새_형식과_옛_형식을_모두_읽는다(tmp_path):
    new = read_index_file(_write_file(tmp_path))
    assert new["KOSPI200"]["as_of"] == date(2026, 9, 28)
    assert new["KOSPI200"]["stocks"][0] == {"code": "000001", "name": "종목000001"}
    old = read_index_file(_write_file(tmp_path, old=True))
    assert [s["code"] for s in old["KOSPI200"]["stocks"]] == ["000001", "000003"]
    assert read_index_file(str(tmp_path / "없음.json")) == {}


def test_이력이_덮는_날은_이력_구성이다(tmp_path):
    u = universe_on(date(2026, 8, 3), repo=FakeRepo(), path=_write_file(tmp_path))
    assert u.codes("KOSPI200") == ["000001", "000002"]       # 오늘 명단(000003)이 아니다
    assert u.status == {"KOSPI200": STATUS_HISTORICAL, "KOSDAQ150": STATUS_HISTORICAL}
    assert u.warnings == [] and u.historical
    assert u.name("000001") == "종목000001"


def test_이력_뒤는_index_파일_명단이고_경고가_붙는다(tmp_path):
    u = universe_on(date(2026, 10, 5), "kospi200", repo=FakeRepo(),
                    path=_write_file(tmp_path))
    assert u.codes() == ["000001", "000003"]
    assert u.status["KOSPI200"] == STATUS_INDEX_FILE
    assert "7일 차이" in u.warnings[0]


def test_이력이_index_파일보다_최근이면_이력_마지막_구성을_쓴다(tmp_path):
    """정기변경 뒤 이력은 갱신했지만 update_index를 다시 돌리지 않은 경우."""
    path = _write_file(tmp_path, as_of="2026-08-01", kospi=("000001", "000002"))
    u = universe_on(date(2026, 10, 5), "kospi200", repo=FakeRepo(), path=path)
    assert u.codes() == ["000001", "000003"]
    assert u.status["KOSPI200"] == STATUS_HISTORY_NEAREST


def test_이력_시작_전에는_오늘_명단을_쓰지_않는다(tmp_path):
    with pytest.raises(SurvivorshipError):
        universe_on(date(2015, 1, 2), repo=FakeRepo(), path=_write_file(tmp_path))


def test_strict는_이력_밖을_막는다(tmp_path):
    with pytest.raises(SurvivorshipError):
        universe_on(date(2026, 10, 5), strict=True, repo=FakeRepo(),
                    path=_write_file(tmp_path))


def test_이력이_없으면_index_파일_관측일_이후에만_쓴다(tmp_path):
    repo, path = FakeRepo(empty={"KOSPI200"}), _write_file(tmp_path)
    assert universe_on(date(2026, 10, 5), "kospi200", repo=repo, path=path).codes() == [
        "000001", "000003"]
    with pytest.raises(SurvivorshipError):
        universe_on(date(2026, 1, 5), "kospi200", repo=repo, path=path)


def test_현재_명단은_index_파일_그대로(tmp_path):
    u = current_universe(path=_write_file(tmp_path))
    assert u.codes() == ["000001", "000003", "000004"]
    assert u.index_of("KRX:000004") == "KOSDAQ150"
    assert u.stocks("KOSPI200")[1] == {"code": "000003", "name": "종목000003"}
    assert u.as_of == date(2026, 9, 28)


def test_백테스트_구간은_이력만(tmp_path):
    ivs = flat_intervals(repo=FakeRepo())
    assert member_index(ivs["KRX:000002"], date(2026, 8, 31)) == "KOSPI200"
    assert member_index(ivs["KRX:000002"], date(2026, 9, 1)) is None
    assert member_index(ivs["KRX:000004"], date(2026, 9, 1)) == "KOSDAQ150"


def test_V15_load_index_자리에_같은_명단을_준다(tmp_path, monkeypatch):
    """`cvd_obv_scan.py`·CVD 가속은 V1.5 `load_index`를 이 명단으로 바꿔 쓴다."""
    import cvd_obv.universe as uni
    from datetime import datetime
    from cvd_obv.live_feed import StandardLiveFeed

    path = _write_file(tmp_path)
    real = uni.universe_on
    monkeypatch.setattr(uni, "universe_on",
                        lambda day=None, *a, **k: real(day, repo=FakeRepo(), path=path))

    class FakeV15:
        fetch_daily_bars = fetch_minute_bars = staticmethod(lambda *a, **k: [])
        load_index = staticmethod(lambda name: ["원래 것"])

    sc = FakeV15()
    feed = StandardLiveFeed(sc, as_of=datetime(2026, 8, 3, 15, 40), log=lambda m: None)
    with feed.installed():
        got = sc.load_index("kospi200")
        assert [s["code"] for s in got] == ["000001", "000002"]      # 8/3 이력 구성
        assert got[0]["name"] == "종목000001"
    assert sc.load_index("kospi200") == ["원래 것"]           # 끝나면 되돌린다


def test_종가매수와_AB는_같은_기준일_명단을_쓴다(tmp_path, monkeypatch):
    import cvd_obv.universe as uni

    path = _write_file(tmp_path)
    real = uni.universe_on
    monkeypatch.setattr(uni, "universe_on",
                        lambda day=None, idx=None, *a, **k: real(day, idx, repo=FakeRepo(),
                                                                 path=path))
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))
    import ab_candidate_scanner as ab
    import closing_bet_scanner as cb

    warns = []
    closing = cb.load_universe(date(2026, 8, 3), warns)
    members, ab_warns = ab.load_universe(date(2026, 8, 3), ("KOSPI200", "KOSDAQ150"))
    assert sorted(closing) == sorted(s.split(":")[1] for s in members) == [
        "000001", "000002", "000004"]
    assert closing["000004"][1] == "KOSDAQ150" and warns == ab_warns == []

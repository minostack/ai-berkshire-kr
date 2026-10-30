# -*- coding: utf-8 -*-
"""분봉 일일 누적(`minute-cache catchup`) 단위 테스트 — Stage 4B.

이 테스트가 지키는 요구사항은 네 가지다.

1. **하루 이틀 늦게 돌려도 빠진 날이 모두 목표가 된다.** "어제"가 아니라
   커버리지와 대조해 결정한다.
2. **끝나지 않은 세션은 목표가 아니다.** 반쪽 하루를 저장하면 다음 날
   "이미 있다"고 판단해 영구히 반쪽으로 남는다.
3. **가진 구간은 다시 받지 않는다.** 가장 오래된 결측일까지만 되돌아간다.
4. **영구 미달인 날을 매일 다시 받지 않는다.** 장부에 적고 건너뛰되, 봉 수가
   달라지면 다시 받는다.

네트워크는 쓰지 않는다 — `plan()`은 API를 부르지 않고, `probe()`는 가짜
source로 대체한다.
"""

import json
from datetime import date, datetime

import pytest

from cvd_obv.domain.errors import ConfigError
from cvd_obv.ingestion.minute_catchup import (
    DEFAULT_MIN_COMPLETENESS, MinuteCatchup, _state_key,
)

D17, D18, D21, D22, D23 = (date(2026, 9, 17), date(2026, 9, 18),
                           date(2026, 9, 21), date(2026, 9, 22),
                           date(2026, 9, 23))
ALL_DAYS = [D17, D18, D21, D22, D23]


class _FakeResult:
    """`MinuteFetchResult` 중 catchup이 쓰는 부분만."""

    def __init__(self, bars):
        self.bars = bars
        self.status = "SUCCESS"
        self.pages = 1


class _FakeSource:
    """ka10080 대신 정해진 봉을 돌려준다. 호출 인자를 기록한다."""

    def __init__(self, scanner, days, bars_per_day=381):
        self.scanner = scanner
        self.days = list(days)
        self.bars_per_day = bars_per_day
        self.calls = []

    def token(self):
        return "FAKE"

    def fetch_security(self, code, token=None, period=1, max_pages=None,
                       min_date=None):
        self.calls.append({"code": code, "period": period,
                           "max_pages": max_pages, "min_date": min_date})
        bars = []
        for day in self.days:
            for i in range(self.bars_per_day):
                bars.append({"trading_date": day, "session": "REGULAR",
                             "index": i})
        return _FakeResult(bars)


@pytest.fixture
def catchup():
    from cvd_obv.baseline.scanner_v15 import load_scanner_v15

    obj = MinuteCatchup.__new__(MinuteCatchup)      # 저장소를 만들지 않는다
    obj.store = None
    obj.universe = None
    obj.index_file = None

    class _Cache:
        source = None

    obj.cache = _Cache()
    obj.cache.source = _FakeSource(load_scanner_v15(), ALL_DAYS)
    return obj


# ── 1. 목표 거래일 ────────────────────────────────────────────

def test_거래일은_API_응답에서_나온다(catchup):
    """저장된 캘린더가 뒤처져도(실측: 09-22까지) 09-23이 목표가 된다."""
    probe = catchup.probe(token="FAKE", period=1, days=10,
                          as_of=datetime(2026, 9, 27, 10, 0))
    assert probe["days"] == ALL_DAYS
    assert probe["api_oldest"] == D17
    assert probe["dropped_open"] == []


def test_끝나지_않은_세션은_목표에서_빠진다(catchup):
    """09-23 11:00에 돌리면 그날은 아직 완료가 아니다."""
    probe = catchup.probe(token="FAKE", period=1, days=10,
                          as_of=datetime(2026, 9, 23, 11, 0))
    assert D23 not in probe["days"]
    assert probe["dropped_open"] == [D23]
    assert probe["days"][-1] == D22


def test_days로_목표_개수를_자른다(catchup):
    probe = catchup.probe(token="FAKE", period=1, days=2,
                          as_of=datetime(2026, 9, 27, 10, 0))
    assert probe["days"] == [D22, D23]


# ── 2. 계획 ──────────────────────────────────────────────────

def _cov(pairs):
    return {(sec, day): bars for sec, day, bars in pairs}


def test_커버리지에_없는_날이_결측이다(catchup):
    cov = _cov([("KRX:005930", D22, 381), ("KRX:000660", D22, 381)])
    plan = catchup.plan([D22, D23], ["005930", "000660"], cov, period=1)
    assert plan["by_day"][D22]["missing"] == 0
    assert plan["by_day"][D23]["missing"] == 2
    assert plan["pairs_missing"] == 2
    assert plan["gap_codes"] == ["000660", "005930"]


def test_이틀_늦게_돌리면_이틀이_목표가_된다(catchup):
    """09-22까지만 있는 저장소를 09-24에 돌린 상황."""
    cov = _cov([("KRX:005930", d, 381) for d in (D17, D18, D21, D22)])
    plan = catchup.plan(ALL_DAYS, ["005930"], cov, period=1)
    assert plan["gaps"]["005930"] == [D23]
    cov2 = _cov([("KRX:005930", d, 381) for d in (D17, D18, D21)])
    plan2 = catchup.plan(ALL_DAYS, ["005930"], cov2, period=1)
    assert plan2["gaps"]["005930"] == [D22, D23]
    assert plan2["oldest_gap_day"] == D22


def test_기준_미달인_날은_부분으로_다시_받는다(catchup):
    """기대 381봉의 80% = 304.8봉 미만이면 다시 받는다."""
    cov = _cov([("KRX:005930", D22, 300), ("KRX:000660", D22, 305)])
    plan = catchup.plan([D22], ["005930", "000660"], cov, period=1)
    assert plan["by_day"][D22]["partial_codes"] == ["005930"]
    assert plan["by_day"][D22]["complete"] == 1


def test_가장_오래된_결측일만큼만_되돌아간다(catchup):
    cov = _cov([("KRX:005930", d, 381) for d in (D17, D18)])
    plan = catchup.plan(ALL_DAYS, ["005930"], cov, period=1)
    assert plan["oldest_gap_day"] == D21          # D17·D18은 다시 받지 않는다


def test_목표일이_없으면_오류다(catchup):
    with pytest.raises(ConfigError):
        catchup.plan([], ["005930"], {}, period=1)


# ── 3. 재시도 억제 장부 ───────────────────────────────────────

def test_장부에_적힌_날은_건너뛴다(catchup):
    """무체결로 영구히 기준 미달인 날을 매일 다시 받지 않는다."""
    cov = _cov([("KRX:005930", D22, 12)])
    settled = {_state_key("KRX:005930", D22): 12}
    plan = catchup.plan([D22], ["005930"], cov, period=1, settled=settled)
    assert plan["gap_codes"] == []
    assert plan["settled_skipped"] == 1


def test_봉_수가_달라지면_다시_대상이_된다(catchup):
    cov = _cov([("KRX:005930", D22, 40)])          # 12 → 40으로 늘었다
    settled = {_state_key("KRX:005930", D22): 12}
    plan = catchup.plan([D22], ["005930"], cov, period=1, settled=settled)
    assert plan["gap_codes"] == ["005930"]
    assert plan["settled_skipped"] == 0


def test_결측은_장부와_무관하게_받는다(catchup):
    """커버리지에 아예 없는 날은 장부가 있어도 받아야 한다."""
    settled = {_state_key("KRX:005930", D22): 0}
    plan = catchup.plan([D22], ["005930"], {}, period=1, settled=settled)
    assert plan["gap_codes"] == ["005930"]


def test_장부는_주기별로_따로_기록된다(catchup, tmp_path, monkeypatch):
    path = tmp_path / "minute_catchup_state.json"
    monkeypatch.setattr(catchup, "state_path", lambda: str(path))
    catchup.save_state(1, {_state_key("KRX:005930", D22): 12})
    catchup.save_state(5, {_state_key("KRX:000660", D22): 3})
    assert catchup.load_state(1) == {_state_key("KRX:005930", D22): 12}
    assert catchup.load_state(5) == {_state_key("KRX:000660", D22): 3}
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload["period_min"]) == {"1", "5"}
    assert "note" in payload            # 사람이 읽고 지울 수 있게 설명을 남긴다


def test_장부가_없으면_빈_dict다(catchup, tmp_path, monkeypatch):
    monkeypatch.setattr(catchup, "state_path",
                        lambda: str(tmp_path / "없는파일.json"))
    assert catchup.load_state(1) == {}


# ── 4. 재확인 집계 (음수 복구 버그 회귀) ──────────────────────

def test_복구_집계는_목표로_잡은_쌍만_센다(catchup):
    """계획과 재확인을 다른 집합에서 세면 복구 수가 음수가 된다.

    실측(2026-09-27): 계획은 351종목·장부 제외로 63쌍, 재확인은 23종목·장부
    미적용으로 130쌍을 세어 `recovered_pairs = -67`이 나왔다. 목표로 잡은
    (종목, 날짜)에 한해서만 세면 그런 일이 없다.
    """
    gaps = {"005930": [D22, D23]}
    after = _cov([("KRX:005930", D22, 381), ("KRX:005930", D23, 381)])
    got = catchup.verify_gaps(gaps, after, period=1)
    assert got["targeted"] == 2
    assert got["recovered"] == 2
    assert got["unresolved"] == {}
    assert 0 <= got["recovered"] <= got["targeted"]


def test_목표가_아니었던_쌍은_미해결로_보고하지_않는다(catchup):
    """장부로 건너뛴 날이 미해결 수를 부풀리면 안 된다."""
    gaps = {"005930": [D23]}                       # D22는 이번 목표가 아니다
    after = _cov([("KRX:005930", D22, 12), ("KRX:005930", D23, 381)])
    got = catchup.verify_gaps(gaps, after, period=1)
    assert got["targeted"] == 1
    assert got["unresolved"] == {}


def test_받아도_미달이면_장부에_적는다(catchup):
    gaps = {"005930": [D23]}
    after = _cov([("KRX:005930", D23, 40)])
    got = catchup.verify_gaps(gaps, after, period=1)
    assert got["recovered"] == 0
    assert [x["bars"] for x in got["unresolved"]["STILL_PARTIAL"]] == [40]
    assert got["settled"][_state_key("KRX:005930", D23)] == 40


def test_정상화된_날은_장부에서_빠진다(catchup):
    """한때 미달이던 날이 채워지면 낡은 기록을 남기지 않는다."""
    gaps = {"005930": [D23]}
    after = _cov([("KRX:005930", D23, 381)])
    settled = {_state_key("KRX:005930", D23): 40}
    got = catchup.verify_gaps(gaps, after, period=1, settled=settled)
    assert got["recovered"] == 1
    assert got["settled"] == {}
    assert got["settled_dropped"] == 1


def test_지평_밖과_데이터_없음을_구분한다(catchup):
    gaps = {"005930": [date(2026, 8, 3)], "000660": [D23]}
    got = catchup.verify_gaps(gaps, {}, period=1, api_oldest=D17)
    assert [x["code"] for x in got["unresolved"]["BEFORE_API_HORIZON"]] \
        == ["005930"]
    assert [x["code"] for x in got["unresolved"]["NO_DATA"]] == ["000660"]
    # 지평 밖·데이터 없음은 장부에 적지 않는다 — 다시 시도할 여지를 남긴다
    assert got["settled"] == {}


# ── 5. 페이지 추정 ────────────────────────────────────────────

def test_페이지_추정은_거래일_수에_따라_늘어난다(catchup):
    one = catchup.estimate_pages(1, 1)
    ten = catchup.estimate_pages(1, 10)
    assert 3 <= one < ten
    # 5분봉은 같은 거래일 수에 더 적은 페이지가 든다
    assert catchup.estimate_pages(5, 10) < ten


def test_기대_봉수는_V15_세션모델과_같다(catchup):
    sc = catchup.cache.source.scanner
    assert catchup.expected_bars(1) == sc.expected_bars_per_day("REGULAR", 1)
    assert catchup.expected_bars(5) == sc.expected_bars_per_day("REGULAR", 5)


def test_기본_기준은_80퍼센트다():
    assert DEFAULT_MIN_COMPLETENESS == 0.80


# ── 애프터마켓 확정 전에 받은 날 ─────────────────────────────

def test_애프터마켓_확정_전에_받은_날은_정규장이_완전해도_다시_받는다(catchup):
    """16:10 실행분 — 정규장은 다 있지만 애프터마켓이 반쪽이다."""
    cov = _cov([("KRX:005930", d, 381) for d in (D22, D23)])
    plan = catchup.plan([D22, D23], ["005930"], cov, period=1,
                        after_pending={("KRX:005930", D23)})
    assert plan["gaps"]["005930"] == [D23]
    assert plan["by_day"][D23]["after_pending"] == 1
    assert plan["pairs_after_pending"] == 1
    assert plan["by_day"][D22]["complete"] == 1


def test_애프터마켓_확정_전에_받은_저유동성_날은_장부에_있어도_다시_받는다(catchup):
    """2026-10-04 실측: 10/2 15:40에 받은 저유동성 12종목이 정규장 봉 기준 미달로
    장부에 걸려 다시 받지 않았고, 확정 뒤 분봉이 없어 정규장 일봉 복원이 안 됐다."""
    cov = _cov([("KRX:000080", D23, 96)])                 # 381봉 기준 미달
    settled = {_state_key("KRX:000080", D23): 96}         # 장부에도 적혀 있다
    plan = catchup.plan([D23], ["000080"], cov, period=1, settled=settled,
                        after_pending={("KRX:000080", D23)})
    assert plan["gaps"]["000080"] == [D23]
    assert plan["by_day"][D23]["after_pending"] == 1
    assert plan["settled_skipped"] == 0


def test_확정_뒤에_받은_저유동성_날은_장부대로_건너뛴다(catchup):
    """다시 받은 뒤에는 after_pending에서 빠지므로 매일 반복해서 받지 않는다."""
    cov = _cov([("KRX:000080", D23, 96)])
    settled = {_state_key("KRX:000080", D23): 96}
    plan = catchup.plan([D23], ["000080"], cov, period=1, settled=settled,
                        after_pending=set())
    assert plan["gap_codes"] == [] and plan["settled_skipped"] == 1


def test_run_id에서_실행_시각을_읽는다():
    from cvd_obv.domain.ids import UTC, run_started_at
    assert (run_started_at("cli_minute_cache_20260923071000_d6614ce9")
            == datetime(2026, 9, 23, 7, 10, tzinfo=UTC))
    assert run_started_at("noid") is None


def test_기준_종목_응답이_비면_휴장이_아니라_API_장애다(catchup):
    """2026-10-13 08:30 실측: 빈 응답이 '목표 거래일 없음'으로 SUCCESS가 됐다."""
    from cvd_obv.domain.errors import ExternalApiError

    catchup.cache.source.days = []
    with pytest.raises(ExternalApiError):
        catchup.run(run_id="t", period=1, days=10, since=D22,
                    as_of=datetime(2026, 9, 27, 10, 0))


def test_휴장일에는_지난_거래일만_있어_목표가_비어도_성공이다(catchup):
    out = catchup.run(run_id="t", period=1, days=10, since=date(2026, 9, 26),
                      as_of=datetime(2026, 9, 27, 10, 0))
    assert out["status"] == "SUCCESS" and out["target_days"] == []

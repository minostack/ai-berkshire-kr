# -*- coding: utf-8 -*-
"""KIS 일별 매수/매도 체결량 적재기 — 파싱·플래그·페이지 경계."""

from datetime import date, datetime, timezone

import pytest

from cvd_obv.contracts.market_schema import (
    DAILY_TRADE_SIDE_COLUMNS, DAILY_TRADE_SIDE_LOGICAL_KEY,
    DAILY_TRADE_SIDE_PK, TRADE_SIDE_ZERO_FLAG,
)
from cvd_obv.ingestion.kis_trade_sides import (
    HISTORY_START, KIS_SOURCE, PAGE_MAX, TradeSidesLoader, parse_rows,
)


def _item(d, buy, sell):
    return {"stck_bsop_date": d, "total_shnu_qty": str(buy),
            "total_seln_qty": str(sell)}


def test_파싱이_필드를_그대로_옮긴다():
    rows = parse_rows([_item("20261016", 8739743, 7544926)], "005930", "J",
                      "run1", now=datetime(2026, 10, 19, tzinfo=timezone.utc))
    assert len(rows) == 1
    r = rows[0]
    assert r["security_id"] == "KRX:005930"
    assert r["trading_date"] == date(2026, 10, 16)
    assert (r["buy_qty"], r["sell_qty"]) == (8739743, 7544926)
    assert r["market_div"] == "J"
    assert r["source"] == KIS_SOURCE
    assert r["quality_status"] == "SUCCESS"
    assert r["quality_flags"] == []


def test_둘_다_0이면_버리지_않고_표시한다():
    """공급자 이력 시작 전을 '수집 실패'와 구별해야 한다."""
    rows = parse_rows([_item("20100226", 0, 0)], "005930", "J", "r")
    assert len(rows) == 1
    assert rows[0]["quality_flags"] == [TRADE_SIDE_ZERO_FLAG]
    assert rows[0]["quality_status"] == "PARTIAL"


def test_거래일_형식이_아니면_오류다():
    with pytest.raises(ValueError):
        parse_rows([_item("2026-10-16", 1, 1)], "005930", "J", "r")
    with pytest.raises(ValueError):
        parse_rows([_item("", 1, 1)], "005930", "J", "r")


def test_스키마가_PK를_담고_있다():
    names = {c.name for c in DAILY_TRADE_SIDE_COLUMNS}
    assert set(DAILY_TRADE_SIDE_PK) <= names
    assert set(DAILY_TRADE_SIDE_LOGICAL_KEY) <= names
    assert "data_revision" not in DAILY_TRADE_SIDE_LOGICAL_KEY


def test_이력_시작은_실측값이다():
    assert HISTORY_START == date(2010, 5, 24)
    assert PAGE_MAX == 100


# ── 페이지 경계 ──────────────────────────────────────────────

class _FakeLoader(TradeSidesLoader):
    """`_call`만 가짜로 바꿔 페이지 논리를 본다."""

    def __init__(self, pages):
        super().__init__(store=object(), sleep=0)
        self.pages = pages
        self.asked = []

    def _call(self, code, d1, d2):
        self.asked.append((d1, d2))
        self.calls += 1
        return self.pages.pop(0) if self.pages else []


def _page(start_ord, n):
    """최신→과거 정렬로 n건."""
    return [_item(date.fromordinal(start_ord - i).strftime("%Y%m%d"), 10, 5)
            for i in range(n)]


def test_100건이_오면_더_과거를_이어_받는다():
    end = date(2026, 10, 16)
    first = _page(end.toordinal(), PAGE_MAX)
    second = _page(end.toordinal() - PAGE_MAX, 10)
    ld = _FakeLoader([first, second])
    rows = ld.fetch_security("005930", date(2026, 1, 1), end)
    assert ld.calls == 2
    # 두 번째 호출의 end는 첫 응답의 최소일 - 1일이다
    assert ld.asked[1][1] == date.fromordinal(end.toordinal() - PAGE_MAX + 1 - 1)
    assert len(rows) == PAGE_MAX + 10


def test_100건_미만이면_한_번만_부른다():
    ld = _FakeLoader([_page(date(2026, 10, 16).toordinal(), 30)])
    rows = ld.fetch_security("005930", date(2026, 9, 1), date(2026, 10, 16))
    assert ld.calls == 1
    assert len(rows) == 30


def test_페이지_경계_중복은_한_번만_남는다():
    end = date(2026, 10, 16)
    first = _page(end.toordinal(), PAGE_MAX)
    # 두 번째 응답이 첫 응답의 마지막 날을 다시 보냄
    second = _page(end.toordinal() - PAGE_MAX + 1, 5)
    ld = _FakeLoader([first, second])
    rows = ld.fetch_security("005930", date(2026, 1, 1), end)
    days = [r["trading_date"] for r in rows]
    assert len(days) == len(set(days))


def test_호출_실패는_조용히_넘기지_않고_센다():
    class Dead(_FakeLoader):
        def _call(self, code, d1, d2):
            self.calls += 1
            self.fails += 1
            return None
    ld = Dead([])
    rows = ld.fetch_security("005930", date(2026, 1, 1), date(2026, 10, 16))
    assert rows == []
    assert ld.fails == 1


# ── 패딩 행 (실측 2026-10-19) ─────────────────────────────────

def test_패딩_행은_세어서_건너뛴다():
    """응답에 날짜가 '0'·''인 빈 행이 섞인다 — 조용히 사라지면 안 된다."""
    st = {}
    rows = parse_rows([
        {"stck_bsop_date": "0", "total_shnu_qty": "0", "total_seln_qty": "0"},
        {"stck_bsop_date": "", "total_shnu_qty": "0", "total_seln_qty": "0"},
        {"stck_bsop_date": "00000000", "total_shnu_qty": "0", "total_seln_qty": "0"},
        _item("20261016", 10, 5),
    ], "000030", "J", "r", stats=st)
    assert len(rows) == 1
    assert st["padding"] == 3


def test_날짜_모양인데_파싱_불가면_예외다():
    """형식이 바뀐 신호는 삼키지 않는다."""
    with pytest.raises(ValueError):
        parse_rows([_item("2026101", 1, 1)], "005930", "J", "r")
    with pytest.raises(ValueError):
        parse_rows([_item("2026-1016", 1, 1)], "005930", "J", "r")


def test_전부_패딩이면_더_과거로_가지_않는다():
    pad = [{"stck_bsop_date": "0", "total_shnu_qty": "0",
            "total_seln_qty": "0"}] * PAGE_MAX
    ld = _FakeLoader([pad, _page(date(2026, 1, 1).toordinal(), 50)])
    rows = ld.fetch_security("000030", date(2010, 5, 24), date(2026, 10, 16))
    assert rows == []
    assert ld.calls == 1


# ── 일일 적재: 16:10 J 오늘 잠정, 익일 08:30 NX·J 어제 확정 ──────────

from datetime import timedelta  # noqa: E402
from decimal import Decimal  # noqa: E402

from cvd_obv.contracts.market_schema import DAILY_BAR_COLUMNS, DAILY_BAR_PK  # noqa: E402
from cvd_obv.domain.ids import KST  # noqa: E402
from cvd_obv.ingestion.kis_trade_sides import (  # noqa: E402
    FLAG_PROVISIONAL, REVISION_FINAL, REVISION_PROVISIONAL, plan_daily,
    read_trade_sides,
)
from cvd_obv.storage.parquet_store import ParquetStore  # noqa: E402

D0, D1 = date(2026, 10, 19), date(2026, 10, 20)        # 월, 화
CODES = ["005930", "000660"]


def _probe_day(st, d):
    """거래일 판정용 기준 종목 일봉 한 행."""
    c = Decimal("1")
    st.write("silver", "daily_bars", [{
        "security_id": "KRX:005930", "trading_date": d, "open": c, "high": c,
        "low": c, "close": c, "volume": 1, "traded_value": None,
        "price_scope": "EXTENDED", "adjustment_type": "SPLIT_ADJUSTED",
        "adjustment_factor": None, "adjustment_as_of_utc": None,
        "available_at_utc": datetime(d.year, d.month, d.day, tzinfo=timezone.utc),
        "quality_status": "SUCCESS", "quality_flags": [], "source": "T",
        "market_rule_version": "T", "run_id": f"b{d}", "schema_version": "2.0.0",
        "data_revision": 1}], DAILY_BAR_COLUMNS, DAILY_BAR_PK, f"b{d}",
        partition_fn=lambda r: [("year", r["trading_date"].year)], part_key=str(d))


def _sides(st, d, md, codes=CODES, flags=(), rev=1, run_id="s"):
    rows = [{"security_id": f"KRX:{c}", "trading_date": d, "buy_qty": 2,
             "sell_qty": 1, "market_div": md, "available_at_utc": datetime.now(timezone.utc),
             "source": KIS_SOURCE, "quality_status": "SUCCESS",
             "quality_flags": list(flags), "run_id": run_id, "schema_version": "2.0.0",
             "data_revision": rev} for c in codes]
    st.write("silver", "daily_trade_sides", rows, DAILY_TRADE_SIDE_COLUMNS,
             DAILY_TRADE_SIDE_PK, run_id,
             partition_fn=lambda r: [("year", r["trading_date"].year)],
             part_key=f"{md}{d}")


@pytest.fixture
def st(tmp_path):
    s = ParquetStore(root=str(tmp_path))
    _probe_day(s, D0)
    _sides(s, D0, "J")
    _sides(s, D0, "NX", codes=CODES[:1])        # NXT는 일부 종목만 거래한다
    return s


def _plan(st, when):
    return {p["market_div"]: p for p in plan_daily(CODES, when, store=st,
                                                   since=D0, markets=("J", "NX"))}


def test_J를_직접_지정해도_16시10분_잠정은_받지_않는다(st):
    """2026-10-23: 잠정 행을 받는 구분은 UN 하나다(`PROVISIONAL_MARKETS`)."""
    _probe_day(st, D1)                       # catchup이 오늘 1분봉을 받은 뒤
    assert _plan(st, datetime(2026, 10, 20, 16, 10, tzinfo=KST)) == {}


def test_정규장_전에는_J_잠정을_받지_않는다(st):
    _probe_day(st, D1)
    assert _plan(st, datetime(2026, 10, 20, 15, 0, tzinfo=KST)) == {}


def test_익일_08시30분에는_NX와_J_어제를_확정으로_받는다(st):
    _probe_day(st, D1)
    _sides(st, D1, "J", flags=[FLAG_PROVISIONAL], rev=0, run_id="p")   # 16:10 잠정
    plan = _plan(st, datetime(2026, 10, 21, 8, 30, tzinfo=KST))
    assert plan["J"]["start"] == D1 and plan["J"]["end"] == D1
    assert plan["J"]["provisional_from"] is None
    assert plan["NX"]["start"] == D1 and plan["NX"]["end"] == D1


def test_NX_기준은_NXT에서_거래되는_종목_수다(st):
    # NX 행은 1종목뿐이지만 그 종목이 NXT 거래 종목 전부라 D0는 확정이다
    plan = _plan(st, datetime(2026, 10, 20, 8, 30, tzinfo=KST))
    assert plan == {}


def test_확정_행은_잠정_행을_이긴다(st):
    _sides(st, D1, "J", flags=[FLAG_PROVISIONAL], rev=REVISION_PROVISIONAL,
           run_id="zzz")
    _sides(st, D1, "J", rev=REVISION_FINAL, run_id="aaa")
    t = read_trade_sides(st, columns=["trading_date", "market_div", "quality_flags"])
    got = [r for r in t.to_pylist() if r["trading_date"] == D1]
    assert len(got) == 2 and all(r["quality_flags"] == [] for r in got)


class _OneDayLoader(TradeSidesLoader):
    def __init__(self, store, md):
        super().__init__(store=store, market_div=md, sleep=0)

    def _call(self, code, d1, d2):
        self.calls += 1
        return [_item(D1.strftime("%Y%m%d"), 3, 1), _item(D0.strftime("%Y%m%d"), 2, 2)]


def test_잠정_구간은_표시와_리비전_0_그_앞은_확정_리비전(tmp_path):
    s = ParquetStore(root=str(tmp_path))
    _OneDayLoader(s, "J").run(["005930"], start=D0, end=D1, run_id="r1",
                              provisional_from=D1, final_revision=REVISION_FINAL,
                              log=lambda m: None)
    rows = {r["trading_date"]: r for r in read_trade_sides(
        s, columns=["trading_date", "quality_flags", "quality_status"]).to_pylist()}
    assert FLAG_PROVISIONAL in rows[D1]["quality_flags"]
    assert rows[D1]["data_revision"] == REVISION_PROVISIONAL
    assert rows[D0]["data_revision"] == REVISION_FINAL and rows[D0]["quality_flags"] == []


def test_같은_run_id로_J와_NX를_써도_서로_덮어쓰지_않는다(tmp_path):
    """2026-10-20 실측: 같은 파일 이름이 되어 J 10/19 행이 사라졌다."""
    s = ParquetStore(root=str(tmp_path))
    for md in ("J", "NX"):
        _OneDayLoader(s, md).run(["005930"], start=D0, end=D1, run_id="same",
                                 log=lambda m: None)
    t = read_trade_sides(s, columns=["trading_date", "market_div"])
    assert sorted(set(t.column("market_div").to_pylist())) == ["J", "NX"]
    assert t.num_rows == 4


# ── 2026-10-20부터 기본 수집 구분은 UN(KRX+NXT) ──────────────────

def _plan_un(st, when):
    return {p["market_div"]: p for p in plan_daily(CODES, when, store=st, since=D0)}


def test_기본은_UN이고_16시10분에는_오늘을_잠정으로(st):
    _probe_day(st, D1)
    plan = _plan_un(st, datetime(2026, 10, 20, 16, 10, tzinfo=KST))
    assert set(plan) == {"UN"}
    assert plan["UN"]["start"] == D0                 # UN은 아직 하나도 없다 — D0부터
    assert plan["UN"]["provisional_from"] == D1


def test_UN_익일_08시30분에는_어제를_확정으로(st):
    _probe_day(st, D1)
    _sides(st, D0, "UN")
    _sides(st, D1, "UN", flags=[FLAG_PROVISIONAL], rev=0, run_id="p")
    plan = _plan_un(st, datetime(2026, 10, 21, 8, 30, tzinfo=KST))
    assert plan["UN"]["start"] == D1 and plan["UN"]["end"] == D1
    assert plan["UN"]["provisional_from"] is None


def test_UN은_구성종목_전부가_기준이다(st):
    # NX처럼 거래 종목 수로 줄이지 않는다 — NXT 미거래 종목도 UN(=J) 행이 있어야 한다
    _sides(st, D0, "UN", codes=CODES[:1])
    plan = _plan_un(st, datetime(2026, 10, 20, 8, 30, tzinfo=KST))
    assert plan["UN"]["start"] == D0

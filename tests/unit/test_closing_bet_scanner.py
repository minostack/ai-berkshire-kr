#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`tools/closing_bet_scanner.py`(closing-0.3) 회귀 테스트.

검토 보고서 `tools/closing_bet_scanner_review.md`의 지적과, 그 검토가 놓친
애프터마켓 종가 문제를 고정한다.

지키는 것
1. 같은 봉의 리비전이 여럿이면 입력 순서와 무관하게 최신 리비전을 쓴다.
2. 같은 날 REGULAR와 EXTENDED가 있으면 REGULAR를 쓴다(입력 순서 무관).
3. 2026-09-14 이후 EXTENDED 봉은 저장소의 정규장 복원본으로 바꾼다(복원 규칙은
   `ingestion/regular_from_minute.py` 하나 — 그 테스트는 `test_regular_from_minute.py`).
4. 복원 못 한 EXTENDED 봉이 판정 창에 있으면 판정 불가(허용 옵션 제외).
5. 조정 구간 거래량이 0이어도 예외가 나지 않고, 거래정지 봉은 도지로 인정되지 않는다.
6. 거래대금 추정치는 추정으로 표시된다. 키움 trde_prica는 백만원 단위다.
7. 중간 거래일 누락·봉 부족은 "후보 없음"이 아니라 판정 불가로 분류된다.
8. D형 60일 신고가는 60봉부터 판정 가능하다.
9. CLI: 잘못된 --types·--date·--live 조합은 오류, 보고서는 덮어쓰지 않는다.
"""

import os
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

import closing_bet_scanner as s  # noqa: E402


# ─────────────────────────────────────────────────────────────
# 합성 봉
# ─────────────────────────────────────────────────────────────

START = date(2026, 1, 1)


def _bar(i, close=100.0, open_=None, high=None, low=None, volume=1000,
         value=2e9, scope=s.SCOPE_REGULAR, value_est=False):
    open_ = close if open_ is None else open_
    return {"date": START + timedelta(days=i), "open": open_,
            "high": high if high is not None else max(open_, close) + 1,
            "low": low if low is not None else min(open_, close) - 1,
            "close": close, "volume": volume, "value": value,
            "value_est": value_est, "scope": scope}


def _flat(n, **kw):
    return [_bar(i, **kw) for i in range(n)]


def _cal(bars):
    return {b["date"]: i for i, b in enumerate(bars)}


def _with_long_bull(n_before=25, vol=10_000):
    """평평한 봉 n_before개 + 장대 양봉(100→110, 거래량 10배)."""
    bars = _flat(n_before)
    bars.append(_bar(n_before, open_=100, close=110, high=110.5, low=99.5, volume=vol))
    return bars


# ─────────────────────────────────────────────────────────────
# 1·2. 리비전·세션 선택
# ─────────────────────────────────────────────────────────────

def _daily_row(close, rev, run_id, scope="REGULAR", day=date(2026, 9, 1)):
    c = Decimal(str(close))
    return {"security_id": "KRX:000001", "trading_date": day,
            "open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 100,
            "traded_value": Decimal(str(close * 100)), "price_scope": scope,
            "adjustment_type": "SPLIT_ADJUSTED", "adjustment_factor": 1.0,
            "adjustment_as_of_utc": None,
            "available_at_utc": datetime(2026, 9, 1, 11, tzinfo=timezone.utc),
            "quality_status": "SUCCESS", "quality_flags": [], "source": "TEST",
            "market_rule_version": "TEST", "run_id": run_id,
            "schema_version": "2.0.0", "data_revision": rev}


@pytest.mark.parametrize("order", ["old_first", "new_first"])
def test_같은_봉은_입력_순서와_무관하게_최신_리비전(tmp_path, order):
    from cvd_obv.contracts.market_schema import (DAILY_BAR_COLUMNS, DAILY_BAR_PK,
                                                 DAILY_BAR_LOGICAL_KEY)
    from cvd_obv.storage.parquet_store import ParquetStore

    st = ParquetStore(root=str(tmp_path))
    # run_id 사전순이 리비전 순서와 반대여도 리비전이 이긴다
    old = ("run_z", _daily_row(100, 1, "run_z"))
    new = ("run_a", _daily_row(110, 2, "run_a"))
    for run_id, row in ([old, new] if order == "old_first" else [new, old]):
        st.write("silver", "daily_bars", [row], DAILY_BAR_COLUMNS, DAILY_BAR_PK, run_id,
                 partition_fn=lambda r: [("year", r["trading_date"].year)])

    both = st.read_arrow("silver", "daily_bars", columns=["close", "data_revision"],
                         dedupe_pk=DAILY_BAR_PK)
    assert both.num_rows == 2          # 저장 PK만으로는 둘 다 남는다 — 검토 지적 재현

    t = st.read_arrow("silver", "daily_bars", columns=["close", "data_revision"],
                      latest_by=DAILY_BAR_LOGICAL_KEY)
    assert t.num_rows == 1
    assert t.column("data_revision").to_pylist() == [2]
    assert float(t.column("close").to_pylist()[0]) == 110


@pytest.mark.parametrize("reverse", [False, True])
def test_같은_날_REGULAR가_EXTENDED보다_우선(reverse):
    rows = [_daily_row(100, 1, "r", "REGULAR"), _daily_row(105, 1, "r", "EXTENDED")]
    picked = s.pick_daily(rows[::-1] if reverse else rows)
    bar = picked[("KRX:000001", date(2026, 9, 1))]
    assert bar["scope"] == s.SCOPE_REGULAR and bar["close"] == 100


# ─────────────────────────────────────────────────────────────
# 3. 정규장 복원본으로 바꾸기
# ─────────────────────────────────────────────────────────────

def test_restore_regular는_저장된_복원본으로_바꾸고_없으면_EXTENDED로_둔다(monkeypatch):
    d1, d2 = date(2026, 9, 22), date(2026, 9, 23)
    picked = {("KRX:1", d1): dict(_bar(0), date=d1, scope=s.SCOPE_EXTENDED, close=999),
              ("KRX:1", d2): dict(_bar(1), date=d2, scope=s.SCOPE_EXTENDED)}
    stored = [dict(_daily_row(100, 1, "r", "REGULAR"), security_id="KRX:1",
                   trading_date=d1),
              dict(_daily_row(105, 1, "r", "EXTENDED"), security_id="KRX:1",
                   trading_date=d2)]
    monkeypatch.setattr(s, "_read_standard", lambda sids, lo, hi: stored)
    out = s.restore_regular(picked)
    assert out[("KRX:1", d1)]["close"] == 100
    assert out[("KRX:1", d1)]["scope"] == s.SCOPE_REGULAR
    assert out[("KRX:1", d2)]["scope"] == s.SCOPE_EXTENDED


@pytest.mark.parametrize("bar_date,at,live,expected", [
    (date(2026, 9, 11), datetime(2026, 9, 30, 15, 45), False, s.SCOPE_REGULAR),   # 도입 전
    (date(2026, 9, 22), datetime(2026, 9, 30, 15, 45), False, s.SCOPE_EXTENDED),  # 도입 후 과거
    (date(2026, 9, 30), datetime(2026, 9, 30, 15, 20), True, s.SCOPE_LIVE),       # 장중
    (date(2026, 9, 30), datetime(2026, 9, 30, 15, 45), False, s.SCOPE_REGULAR),   # 15:35~16:00
    (date(2026, 9, 30), datetime(2026, 9, 30, 15, 32), False, s.SCOPE_EXTENDED),  # 확정 여유 전
    (date(2026, 9, 30), datetime(2026, 9, 30, 15, 32), True, s.SCOPE_LIVE),
    (date(2026, 9, 30), datetime(2026, 9, 30, 17, 0), False, s.SCOPE_EXTENDED),   # 애프터마켓 중
])
def test_키움_일봉_성격_판정(bar_date, at, live, expected):
    assert s.kiwoom_scope(bar_date, at, live) == expected


# ─────────────────────────────────────────────────────────────
# 4·7. 판정 가능 여부
# ─────────────────────────────────────────────────────────────

def test_복원_못_한_EXTENDED가_창에_있으면_판정_불가_허용하면_판정():
    bars = _flat(40)
    bars[-3]["scope"] = s.SCOPE_EXTENDED
    as_of = bars[-1]["date"]
    assert s.eligibility(bars, as_of, _cal(bars), False) == s.EX_EXTENDED
    assert s.eligibility(bars, as_of, _cal(bars), True) is None


def test_창_밖의_오래된_EXTENDED는_판정을_막지_않는다():
    bars = _flat(s.EVAL_BARS + 5)
    bars[0]["scope"] = s.SCOPE_EXTENDED
    assert s.eligibility(bars, bars[-1]["date"], _cal(bars), False) is None


def test_중간_거래일이_빠지면_판정_불가():
    bars = _flat(40)
    cal = _cal(bars)
    del bars[30]
    assert s.eligibility(bars, bars[-1]["date"], cal, False) == s.EX_GAP


def test_기준일_봉이_없거나_봉이_모자라면_판정_불가():
    bars = _flat(40)
    assert s.eligibility(bars, bars[-1]["date"] + timedelta(days=1), _cal(bars), False) \
        == s.EX_NO_TODAY
    short = _flat(s.LIQ_DAYS + 1)
    assert s.eligibility(short, short[-1]["date"], _cal(short), False) == s.EX_SHORT


def test_판정_중_예외는_종목_단위로_격리된다(monkeypatch):
    bars = _flat(40)

    def boom(*a, **k):
        raise ValueError("x")
    monkeypatch.setattr(s, "evaluate", boom)
    j = s.judge(bars, bars[-1]["date"], _cal(bars), 60, False)
    assert j["status"] == s.EX_ERROR and "ValueError" in j["error"]


@pytest.mark.parametrize("n,d_na", [(59, True), (60, False)])
def test_D형_60일은_60봉부터_판정_가능(n, d_na):
    res = s.evaluate(_flat(n), 60)
    assert ("D" in res["na"]) is d_na


# ─────────────────────────────────────────────────────────────
# 5. 거래량 0(거래정지)
# ─────────────────────────────────────────────────────────────

def test_C형_조정_거래량이_모두_0이어도_예외_없이_불충족():
    """검토 보고서 §2의 재현 입력 그대로."""
    bars = _flat(20, volume=100)
    bars.append(_bar(20, open_=100, close=130, high=130, low=100, volume=1000))
    bars += [_bar(21 + k, close=110, volume=0) for k in range(8)]
    bars.append(_bar(29, open_=110, close=115, high=116, low=109, volume=100))
    res = s.evaluate(bars, 60)          # 0.1에서는 ZeroDivisionError
    assert res.get("C") is None


def _a_setup(correction_volume):
    bars = _with_long_bull()
    n = len(bars)
    bars += [_bar(n + k, open_=110, close=110, high=110.2, low=109.8,
                  volume=correction_volume) for k in range(2)]
    return bars


def test_A형_정상_도지는_통과():
    res = s.evaluate(_a_setup(3000), 60)
    assert res["A"] and res["A"]["adj_days"] == 2


def test_A형_거래정지_봉은_도지로_인정하지_않는다():
    assert s.evaluate(_a_setup(0), 60).get("A") is None


# ─────────────────────────────────────────────────────────────
# 6. 거래대금 출처
# ─────────────────────────────────────────────────────────────

def test_원천_거래대금이_없으면_추정으로_표시():
    row = _daily_row(100, 1, "r")
    row["traded_value"] = None
    bar = s.pick_daily([row])[("KRX:000001", date(2026, 9, 1))]
    assert bar["value_est"] is True and bar["value"] == 100 * 100
    assert "(추정)" in s.describe("D", {"new_high": [60], "value": 2e10,
                                        "value_mult": 2.0, "value_est": True})


def test_키움_일봉_거래대금은_백만원_단위로_읽는다(monkeypatch):
    import daily_scanner as ds_
    monkeypatch.setattr(ds_, "_kiwoom_get_token", lambda: "tok")
    payload = {"stk_dt_pole_chart_qry": [
        {"dt": "20260922", "cur_prc": "31400", "open_pric": "29250", "high_pric": "31650",
         "low_pric": "29150", "trde_qty": "6800000", "trde_prica": "210000"},
        {"dt": "20260921", "cur_prc": "28850", "open_pric": "29150", "high_pric": "29300",
         "low_pric": "28700", "trde_qty": "1100000"}]}
    monkeypatch.setattr(ds_, "_kiwoom_request", lambda *a, **k: payload)
    rows = ds_.kiwoom_daily_bars("003490", days=5, as_of=datetime(2026, 9, 23, 9))
    by = {r["date"]: r for r in rows}
    assert by["2026-09-22"]["traded_value"] == 210_000 * 1_000_000
    assert by["2026-09-21"]["traded_value"] is None


# ─────────────────────────────────────────────────────────────
# 9. CLI·저장
# ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", ["INVALID", "A,Z", "", " , "])
def test_잘못된_유형_입력은_오류(raw):
    with pytest.raises(SystemExit):
        s.build_parser().parse_args(["--types", raw])


def test_유형_입력은_토큰_단위로_정규화():
    assert s.build_parser().parse_args(["--types", "c, a"]).types == ["A", "C"]


def test_잘못된_날짜는_CLI_오류():
    with pytest.raises(SystemExit):
        s.build_parser().parse_args(["--date", "2026-13-01"])


def test_로컬에서_live는_거부(capsys):
    with pytest.raises(SystemExit):
        s.main(["--live"])


def test_같은_초에_두_번_저장해도_덮어쓰지_않는다(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "REPORT_DIR", str(tmp_path))
    gen = datetime(2026, 9, 30, 15, 45, 0)
    p1 = s.report_path(date(2026, 9, 30), gen)
    s._atomic_write(p1, "first")
    p2 = s.report_path(date(2026, 9, 30), gen)
    assert p1 != p2
    s._atomic_write(p2, "second")
    assert open(p1, encoding="utf-8").read() == "first"
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]


# 11. 데이터 기준 알림 — 스캐너는 적재하지 않는다(0.4)
# ─────────────────────────────────────────────────────────────

T = START + timedelta(days=39)       # _flat(40)의 마지막 날


def _universe(n_codes=100, n_bars=40):
    return {f"{i:06d}": _flat(n_bars) for i in range(n_codes)}


def test_모두_있으면_적재_안내_없음():
    bars = _universe()
    b = s.data_basis(sorted(bars), bars, T, reference=T, ref_src="test")
    assert not b["needs_load"] and not b["behind"]
    text = " | ".join(s.basis_lines(b, "local"))
    assert "bars-1.0" in text and s.LOAD_COMMAND not in text


def test_로컬이_시장보다_뒤처지면_적재_프로세스를_안내만_한다():
    bars = _universe()
    b = s.data_basis(sorted(bars), bars, T, reference=T + timedelta(days=1), ref_src="test")
    assert b["behind"] and b["needs_load"]
    text = " | ".join(s.basis_lines(b, "local"))
    assert s.LOAD_COMMAND in text and "적재하지 않는다" in text


def test_기준일_봉_없는_종목과_복원_전_봉을_센다():
    bars = _universe()
    bars["000007"] = bars["000007"][:-1]
    bars["000001"][-3]["scope"] = s.SCOPE_EXTENDED
    bars["000002"][-3]["scope"] = s.SCOPE_EXTENDED
    b = s.data_basis(sorted(bars), bars, T)
    assert b["missing_today"] == ["000007"]
    assert b["unrestored"] == {bars["000001"][-3]["date"]: 2}
    assert b["needs_load"]


def test_판정_창_밖의_EXTENDED는_알리지_않는다():
    bars = _universe(n_bars=s.EVAL_BARS + 5)
    for bs in bars.values():
        bs[0]["scope"] = s.SCOPE_EXTENDED
    b = s.data_basis(sorted(bars), bars, bars["000000"][-1]["date"])
    assert b["unrestored"] == {}


def test_스캐너에는_적재_옵션이_없다():
    for opt in ("--check", "--ensure-data", "--json"):
        with pytest.raises(SystemExit):
            s.build_parser().parse_args([opt])
    assert not hasattr(s, "run_load_commands") and not hasattr(s, "readiness")


@pytest.mark.parametrize("now,expected", [
    (datetime(2026, 10, 2, 10, 0), date(2026, 10, 1)),    # 금요일 장중 → 목요일
    (datetime(2026, 10, 2, 15, 40), date(2026, 10, 2)),   # 금요일 마감 후 → 당일
    (datetime(2026, 10, 4, 12, 0), date(2026, 10, 2)),    # 일요일 → 금요일
    (datetime(2026, 10, 5, 9, 0), date(2026, 10, 2)),     # 월요일 개장 전 → 금요일
])
def test_키가_없으면_평일_근사로_기준일(monkeypatch, now, expected):
    for k in ("KIWOOM_APP_KEY", "KIS_APP_KEY"):
        monkeypatch.delenv(k, raising=False)
    days, src = s.reference_days(now)
    assert days[-1] == expected and "근사" in src
    assert all(d.weekday() < 5 for d in days)


def test_잠정_일봉은_정규장으로_쓰되_알림에_표시한다():
    row = _daily_row(100, 0, "r")
    row["source"] = "DERIVED_MINUTE_1M_PROVISIONAL"
    bar = s.pick_daily([row])[("KRX:000001", date(2026, 9, 1))]
    assert bar["scope"] == s.SCOPE_REGULAR and bar["provisional"] is True
    bars = _universe()
    for bs in bars.values():
        bs[-1]["provisional"] = True
    b = s.data_basis(sorted(bars), bars, T, reference=T)
    assert b["provisional"] == {T: 100}
    assert any("잠정 일봉" in x for x in s.basis_lines(b, "local"))


def _probe_env(monkeypatch, kis=True, kiwoom=True, source=None):
    for k in ("KIS_APP_KEY", "KIS_APP_SECRET"):
        monkeypatch.setenv(k, "x") if kis else monkeypatch.delenv(k, raising=False)
    for k in ("KIWOOM_APP_KEY", "KIWOOM_APP_SECRET"):
        monkeypatch.setenv(k, "x") if kiwoom else monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CVD_MARKET_SOURCE", source) if source else         monkeypatch.delenv("CVD_MARKET_SOURCE", raising=False)


def _fake(days, calls, name):
    def probe(now):
        calls.append(name)
        if isinstance(days, Exception):
            raise days
        return list(days)
    return probe


def _set_probes(monkeypatch, kis, kiwoom, calls):
    monkeypatch.setitem(s._PROBES, "kis", ("KIS", s._PROBES["kis"][1], _fake(kis, calls, "kis")))
    monkeypatch.setitem(s._PROBES, "kiwoom",
                        ("키움", s._PROBES["kiwoom"][1], _fake(kiwoom, calls, "kiwoom")))


HOLI = [date(2026, 9, 30), date(2026, 10, 2)]      # 10/1 휴장 가정


def test_기준_거래일은_KIS_먼저_조회한다(monkeypatch):
    _probe_env(monkeypatch)
    calls = []
    _set_probes(monkeypatch, HOLI, RuntimeError("안 불려야 함"), calls)
    days, src = s.reference_days(datetime(2026, 10, 2, 16, 0))
    assert calls == ["kis"] and src.startswith("KIS") and days == HOLI


def test_KIS_실패하면_키움_그다음_근사(monkeypatch):
    _probe_env(monkeypatch)
    calls = []
    _set_probes(monkeypatch, RuntimeError("8050"), HOLI, calls)
    days, src = s.reference_days(datetime(2026, 10, 2, 16, 0))
    assert calls == ["kis", "kiwoom"] and src.startswith("키움")
    calls.clear()
    _set_probes(monkeypatch, RuntimeError("a"), RuntimeError("b"), calls)
    days, src = s.reference_days(datetime(2026, 10, 2, 16, 0))
    assert calls == ["kis", "kiwoom"] and "근사" in src


def test_CVD_MARKET_SOURCE_kiwoom이면_키움_먼저(monkeypatch):
    _probe_env(monkeypatch, source="kiwoom")
    calls = []
    _set_probes(monkeypatch, HOLI, HOLI, calls)
    _, src = s.reference_days(datetime(2026, 10, 2, 16, 0))
    assert calls == ["kiwoom"] and src.startswith("키움")


def test_키_없는_원천은_건너뛴다(monkeypatch):
    _probe_env(monkeypatch, kis=False)
    calls = []
    _set_probes(monkeypatch, HOLI, HOLI, calls)
    _, src = s.reference_days(datetime(2026, 10, 2, 16, 0))
    assert calls == ["kiwoom"]


def test_장중_당일_봉은_거래일에서_뺀다(monkeypatch):
    _probe_env(monkeypatch)
    calls = []
    _set_probes(monkeypatch, HOLI, HOLI, calls)
    days, _ = s.reference_days(datetime(2026, 10, 2, 11, 0))
    assert days[-1] == date(2026, 9, 30)


# ── API 원천(KIS 기본·키움 대체): 지난 날 로컬 + 오늘 API ──────────────────

def _api_row(d, close=100.0, volume=1000, tv=1e5):
    return {"date": d, "open": close, "high": close, "low": close, "close": close,
            "volume": volume, "traded_value": tv}


def _local_bar(d, close=100.0):
    return {"date": d, "open": close, "high": close, "low": close, "close": close,
            "volume": 900, "value": 9e4, "value_est": False, "scope": s.SCOPE_REGULAR}


D1, D2 = date(2026, 10, 22), date(2026, 10, 23)


def test_API_지난_날은_로컬_오늘만_API():
    local = {("KRX:000001", D1): _local_bar(D1)}
    api = {"000001": [_api_row(D1, volume=5555), _api_row(D2, close=101)]}
    picked, mm = s.merge_api(local, api, datetime(2026, 10, 23, 15, 40), live=False)
    assert picked[("KRX:000001", D1)]["volume"] == 900          # 로컬 그대로
    today = picked[("KRX:000001", D2)]
    assert today["scope"] == s.SCOPE_REGULAR and today["close"] == 101 and today["api"]
    assert today["value"] == 1e5 and not today["value_est"] and mm == []


def test_API_장중_당일_봉은_live가_아니면_버리고_live면_LIVE():
    local = {("KRX:000001", D1): _local_bar(D1)}
    api = {"000001": [_api_row(D1), _api_row(D2)]}
    now = datetime(2026, 10, 23, 11, 0)
    picked, _ = s.merge_api(local, api, now, live=False)
    assert ("KRX:000001", D2) not in picked
    picked, _ = s.merge_api(local, api, now, live=True)
    assert picked[("KRX:000001", D2)]["scope"] == s.SCOPE_LIVE


def test_API_애프터마켓_중_당일_봉은_EXTENDED_로컬_잠정봉이_있으면_로컬():
    api = {"000001": [_api_row(D2)]}
    now = datetime(2026, 10, 23, 17, 0)
    picked, _ = s.merge_api({}, api, now, live=False)
    assert picked[("KRX:000001", D2)]["scope"] == s.SCOPE_EXTENDED
    picked, _ = s.merge_api({("KRX:000001", D2): _local_bar(D2, 99)}, api, now, live=False)
    assert picked[("KRX:000001", D2)]["close"] == 99


def test_API_직전_거래일_종가가_로컬과_다르면_수정주가_불일치():
    local = {("KRX:000001", D1): _local_bar(D1, 100), ("KRX:000002", D1): _local_bar(D1, 100)}
    api = {"000001": [_api_row(D1, close=50)], "000002": [_api_row(D1, close=100.4)]}
    _, mm = s.merge_api(local, api, datetime(2026, 10, 23, 15, 40), live=False)
    assert mm == ["000001"]


def test_live는_API_원천에서만():
    with pytest.raises(SystemExit):
        s.main(["--live"])
    assert s.build_parser().parse_args(["--source", "kis", "--live"]).live

# -*- coding: utf-8 -*-
"""NXT 메인마켓 거래량을 정규장 일봉에 더한다(A안, 2026-10-20).

지키는 것
1. NXT 봉은 `NXT:<코드>`로 저장하고 키움에는 `<코드>_NX`로 요청한다.
2. 더하는 것은 NXT 메인마켓(봉 시작 09:00~15:29)뿐 — 프리·애프터마켓은 뺀다.
3. NXT 1분봉을 받지 않은 날은 더하지 않고 `NXT_VOLUME_MISSING`으로 표시한다(받았는데
   0인 날과 구별한다). NXT 미거래 종목은 표시하지 않는다.
4. 잠정·확정 봉 모두 더한다. KRX만인 예전 확정 봉은 NXT 1분봉이 생기면 다시 만든다.
"""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from cvd_obv.baseline.scanner_v15 import load_scanner_v15
from cvd_obv.bar_standard import read_standard_daily
from cvd_obv.contracts.market_schema import DAILY_BAR_COLUMNS, DAILY_BAR_PK
from cvd_obv.domain.ids import KST
from cvd_obv.ingestion.kiwoom_minute import MinuteFetchResult, kiwoom_stock_code
from cvd_obv.ingestion.minute_catchup import SessionStorePolicy
from cvd_obv.ingestion.minute_history import MinuteHistoryCache
from cvd_obv.ingestion.nxt_volume import (
    FLAG_NXT_INCLUDED, FLAG_NXT_MISSING, apply_nxt, nxt_main_volume,
)
from cvd_obv.ingestion.provisional_daily import ProvisionalDailyBuilder
from cvd_obv.ingestion.regular_from_minute import (
    FINAL_REVISION, RegularFromMinuteBuilder,
)
from cvd_obv.storage.parquet_store import ParquetStore

pytest.importorskip("pyarrow")

SID = "KRX:000001"
DAY = date(2026, 9, 22)
UTC = timezone.utc
KRX_SPEC = [((9, 0), 100, 1000), ((12, 0), 102, 500), ((15, 30), 103, 300),
            ((15, 40), 103, 20), ((16, 0), 104, 150), ((19, 59), 98, 30)]
# NXT: 프리 08:10(400) · 메인 09:00(70)·15:19(30) · 애프터 15:45(5)·16:00(9)
NXT_SPEC = [((8, 10), 99, 400), ((9, 0), 100, 70), ((15, 19), 103, 30),
            ((15, 45), 103, 5), ((16, 0), 104, 9)]


def _bars(spec):
    out = []
    for (hh, mm), px, vol in spec:
        start = datetime.combine(DAY, time(hh, mm))
        out.append({"trading_date": DAY, "bar_start": start,
                    "bar_end": start + timedelta(minutes=1),
                    "session": load_scanner_v15().session_of(start.strftime("%Y%m%d%H%M%S")),
                    "open": Decimal(px), "high": Decimal(px), "low": Decimal(px),
                    "close": Decimal(px), "volume": vol, "traded_value": None,
                    "source_timestamp_raw": start.strftime("%Y%m%d%H%M%S")})
    return out


class _Source:
    def __init__(self):
        self.scanner = load_scanner_v15()

    def token(self):
        return "T"

    def fetch_security(self, code, token=None, period=1, max_pages=None, min_date=None):
        bars = _bars(NXT_SPEC if str(code).startswith("NXT:") else KRX_SPEC)
        return MinuteFetchResult(code, period, bars, 1, len(bars), True, "SUCCESS")


def _load(st, codes, run_id):
    cache = MinuteHistoryCache(store=st)
    cache.source = _Source()
    policy = SessionStorePolicy(datetime(2026, 9, 23, 8, 30, tzinfo=KST))
    cache.build(codes, run_id=run_id, period=1, keep_bar=policy.keep_bar,
                keep_coverage=policy.keep_coverage)


def _ext(st, volume=2000):
    row = {"security_id": SID, "trading_date": DAY, "open": Decimal("100"),
           "high": Decimal("104"), "low": Decimal("98"), "close": Decimal("98"),
           "volume": volume, "traded_value": None, "price_scope": "EXTENDED",
           "adjustment_type": "SPLIT_ADJUSTED", "adjustment_factor": None,
           "adjustment_as_of_utc": datetime(2026, 9, 22, 23, 31, tzinfo=UTC),
           "available_at_utc": datetime(2026, 9, 22, 11, 5, tzinfo=UTC),
           "quality_status": "SUCCESS", "quality_flags": [], "source": "TEST",
           "market_rule_version": "TEST", "run_id": "x", "schema_version": "2.0.0",
           "data_revision": 1}
    st.write("silver", "daily_bars", [row], DAILY_BAR_COLUMNS, DAILY_BAR_PK, "x",
             partition_fn=lambda r: [("year", r["trading_date"].year)], part_key="ext")


def _std(st):
    return {r["security_id"]: r for r in read_standard_daily(
        st, columns=["security_id", "volume", "quality_flags", "data_revision"]).to_pylist()}


def test_키움_요청_코드():
    assert kiwoom_stock_code("NXT:005930") == "005930_NX"
    assert kiwoom_stock_code("KRX:005930") == "005930"
    assert kiwoom_stock_code("005930") == "005930"


def test_메인마켓만_더하고_받지_않은_날은_없다(tmp_path):
    st = ParquetStore(root=str(tmp_path))
    _load(st, ["NXT:000001"], "topup_load_20260922233000_aaaa0001")
    got = nxt_main_volume(st, DAY, DAY, {SID, "KRX:000002"})
    assert got == {(SID, DAY): 100}            # 프리 400·애프터 14는 빠진다


def test_표시_규칙():
    vols = {(SID, DAY): 100}
    assert apply_nxt(1000, ["A"], (SID, DAY), vols, {SID}) == (1100, ["A", FLAG_NXT_INCLUDED])
    assert apply_nxt(1000, [], ("KRX:2", DAY), vols, {"KRX:2"}) == (1000, [FLAG_NXT_MISSING])
    assert apply_nxt(1000, [], ("KRX:3", DAY), vols, set()) == (1000, [])     # NXT 미거래
    # 다시 만들 때 예전 표시는 지운다
    assert apply_nxt(1000, [FLAG_NXT_MISSING], (SID, DAY), vols, {SID})[1] == [FLAG_NXT_INCLUDED]


def test_확정_봉에_NXT_메인마켓을_더한다(tmp_path):
    st = ParquetStore(root=str(tmp_path))
    _load(st, ["000001", "NXT:000001"], "topup_load_20260922233000_aaaa0001")
    _ext(st)
    out = RegularFromMinuteBuilder(st).build("f1", DAY, DAY, {SID})
    assert out["written"] == 1, out["skipped"]
    row = _std(st)[SID]
    assert row["volume"] == (2000 - 180) + 100              # EXT − KRX 애프터 + NXT 메인
    assert FLAG_NXT_INCLUDED in row["quality_flags"]
    assert row["data_revision"] == FINAL_REVISION


def test_KRX만인_확정_봉은_NXT_1분봉이_생기면_다시_만든다(tmp_path):
    st = ParquetStore(root=str(tmp_path))
    _load(st, ["000001"], "topup_load_20260922233000_aaaa0001")
    _ext(st)
    RegularFromMinuteBuilder(st).build("f1", DAY, DAY, {SID})
    assert _std(st)[SID]["volume"] == 1820                  # NXT 아직 없음
    _load(st, ["NXT:000001"], "topup_load_20260922233500_bbbb0002")
    out = RegularFromMinuteBuilder(st).build("f2", DAY, DAY, {SID})
    assert out["nxt_upgrades"] == 1 and out["written"] == 1
    assert _std(st)[SID]["volume"] == 1920
    # 한 번 넣은 뒤에는 다시 만들지 않는다
    assert RegularFromMinuteBuilder(st).build("f3", DAY, DAY, {SID})["targets"] == 0


def test_잠정_봉에도_NXT_메인마켓을_더한다(tmp_path):
    st = ParquetStore(root=str(tmp_path))
    _load(st, ["000001", "NXT:000001"], "topup_load_20260922071000_aaaa0001")
    out = ProvisionalDailyBuilder(st).build("p1", DAY, {SID},
                                            now=datetime(2026, 9, 22, 16, 10, tzinfo=KST))
    assert out["written"] == 1
    row = _std(st)[SID]
    assert row["volume"] == (1000 + 500 + 300 + 20) + 100
    assert FLAG_NXT_INCLUDED in row["quality_flags"]


def _ext_split(st):
    """1:2 분할 뒤 다시 받은 일봉 — 가격 ×0.5·거래량 ×2, 1분봉은 분할 전 기준 그대로."""
    _ext(st, volume=4000)
    import pyarrow.dataset as ds  # noqa: F401
    row = read_standard_daily(st, columns=None, allow_extended=True).to_pylist()[0]
    row.update(open=Decimal("50"), high=Decimal("52"), low=Decimal("49"),
               close=Decimal("49"), run_id="y")
    st.write("silver", "daily_bars", [row], DAILY_BAR_COLUMNS, DAILY_BAR_PK, "y",
             partition_fn=lambda r: [("year", r["trading_date"].year)], part_key="ext2")


def test_수정주가_계수가_걸리면_NXT_몫도_같은_계수로_맞춘다(tmp_path):
    """2026-10-26 실측 290650 — NXT 몫이 분할 전 기준(절반)으로 더해졌다."""
    from cvd_obv.ingestion.nxt_volume import FLAG_NXT_RESCALED
    from cvd_obv.ingestion.regular_from_minute import FLAG_ADJUSTED

    st = ParquetStore(root=str(tmp_path))
    _load(st, ["000001", "NXT:000001"], "topup_load_20260922233000_aaaa0001")
    _ext_split(st)
    out = RegularFromMinuteBuilder(st).build("f1", DAY, DAY, {SID})
    assert out["written"] == 1, out["skipped"]
    row = _std(st)[SID]
    assert row["volume"] == (4000 - 180 * 2) + 100 * 2     # KRX·NXT 모두 ÷0.5
    assert {FLAG_ADJUSTED, FLAG_NXT_INCLUDED, FLAG_NXT_RESCALED} <= set(row["quality_flags"])
    assert RegularFromMinuteBuilder(st).build("f2", DAY, DAY, {SID})["targets"] == 0


def test_옛_규칙으로_NXT를_더한_계수_봉은_다시_만든다(tmp_path):
    from cvd_obv.ingestion.regular_from_minute import FLAG_ADJUSTED

    st = ParquetStore(root=str(tmp_path))
    _load(st, ["000001", "NXT:000001"], "topup_load_20260922233000_aaaa0001")
    _ext_split(st)
    RegularFromMinuteBuilder(st).build("f1", DAY, DAY, {SID})
    old = read_standard_daily(st, columns=None).to_pylist()[0]
    old.update(volume=(4000 - 360) + 100, run_id="zz_old",
               quality_flags=sorted([FLAG_ADJUSTED, FLAG_NXT_INCLUDED]))
    st.write("silver", "daily_bars", [old], DAILY_BAR_COLUMNS, DAILY_BAR_PK, "zz_old",
             partition_fn=lambda r: [("year", r["trading_date"].year)], part_key="old")
    assert _std(st)[SID]["volume"] == 3740
    out = RegularFromMinuteBuilder(st).build("zzz_f2", DAY, DAY, {SID})
    assert out["written"] == 1
    assert _std(st)[SID]["volume"] == 3840

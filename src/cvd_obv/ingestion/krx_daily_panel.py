# -*- coding: utf-8 -*-
"""KRX 일별매매정보 → 일봉 — 상장폐지 종목을 메우는 유일한 경로.

왜 이게 필요한가
----------------
Stage 3에서 복원한 코스피200 이력에는 387종목이 들어 있는데, 키움에서 받은
일봉은 201종목뿐이다. 없는 186종목 중 **39종목은 이미 상장폐지**돼 키움이
아예 주지 않는다. 그리고 그 39종목이야말로 생존편향의 핵심이다.

KRX Open API의 유가증권 일별매매정보는 **그날 상장돼 있던 종목을 그날 기준으로**
주므로 폐지 종목도 폐지 전까지는 그대로 들어 있다. 문제는 하나다.

    이 패널의 가격은 **수정주가가 아니다.**

실측(2018-05-04 삼성전자 50:1 액면분할): 종가가 2,650,000 → 51,900으로 꺾이고
상장주식수가 128,386,494 → 6,419,324,700이 된다. 이대로 수익률을 계산하면
그날 −98%가 된다.

어떻게 보정하는가
-----------------
분할·무상증자는 **상장주식수는 k배, 주가는 1/k배, 시가총액은 그대로**라는
서명을 남긴다. 그래서 추측할 필요가 없다.

    f_shares = 주식수(t) / 주식수(t-1)
    f_price  = 종가(t-1) / 종가(t)
    두 값이 서로 맞고( |f_price/f_shares - 1| <= tol ) 1에서 충분히 벗어나면
    → 그날 분할 계수 f_shares 를 적용한다.

유상증자처럼 **시가총액이 함께 늘어나는** 변경은 이 서명을 만들지 않는다. 그런
날은 보정하지 않고 `CAPITAL_CHANGE_UNADJUSTED`로 남긴다. 조용히 보정하면
틀린 수익률이 맞는 것처럼 보이기 때문이다.

보정한 행에는 누적 계수를 `adjustment_factor`에 남긴다. 나중에 원본으로 되돌릴
수 있어야 한다.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from .. import MARKET_RULE_VERSION, SCHEMA_VERSION
from ..contracts.market_schema import (
    DAILY_BAR_COLUMNS, DAILY_BAR_PK, OHLC_VIOLATION_FLAG, ohlc_violations_of,
)
from ..domain.errors import NoInputDataError
from ..domain.ids import KST, UTC, normalize_security_id, to_utc
from ..domain.market_rules import default_registry
from ..domain.status import QualityStatus
from ..storage.parquet_store import ParquetStore
from .krx_openapi import KRX_OPENAPI_SOURCE, STOCK_DAILY

__all__ = ["KrxDailyPanel", "PANEL_FLAG", "SPLIT_FLAG", "CAPITAL_FLAG",
           "NO_TRADE_FLAG"]

PANEL_FLAG = "KRX_PANEL_DERIVED"
SPLIT_FLAG = "SPLIT_ADJUSTED_LOCALLY"
CAPITAL_FLAG = "CAPITAL_CHANGE_UNADJUSTED"
NO_TRADE_FLAG = "NO_TRADE"

#: 주식수가 이만큼 바뀌면 자본변동으로 본다(±20%).
_SHARES_TOLERANCE = 0.2

#: 주가 비율이 주식수 비율과 이만큼 안에서 맞으면 분할 후보로 본다(±15%).
#: 그날의 진짜 등락이 섞이므로 정확히 일치할 수는 없다.
_MATCH_TOLERANCE = 0.15

#: 액면분할·병합의 비율은 **깔끔한 정수비**다(2:1, 5:1, 10:1, 50:1 …).
#: 인적분할은 그렇지 않다 — NAVER 2013-08-29은 0.6849배(=1/1.46)였다.
#: 이 조건 하나가 시가총액 조건보다 훨씬 잘 가른다. 재상장일에는 가격이 크게
#: 움직여 시가총액 조건만으로는 진짜 병합까지 놓치기 때문이다(실측: 008000
#: 2013-05-30 10:1 병합, 그날 시가총액 +14.7%).
_CLEAN_RATIOS = (2, 3, 4, 5, 6, 8, 10, 20, 25, 50, 100)
_CLEAN_TOLERANCE = 0.005

#: 시가총액은 보조 조건으로만 쓴다(±30%). 여기서 벗어나면 분할이 아니라
#: 자본변동이다.
#: 인적분할·유상증자는 주식수와 주가가 반대로 움직이면서도 시가총액이 함께
#: 달라진다. 실측: NAVER 2013-08-29 인적분할은 주식수 0.685배·주가 1.64배로
#: 가격 대조만으로는 분할과 구별되지 않지만 시가총액이 +12%였다. 이 조건을
#: 넣기 전에는 그날 수익률이 52%p 틀렸다.
_MKTCAP_TOLERANCE = 0.30

_QUANT = Decimal("0.0001")


def _clean_ratio(factor) -> bool:
    """액면분할·병합처럼 깔끔한 정수비인가."""
    for ratio in _CLEAN_RATIOS:
        if abs(factor - ratio) <= ratio * _CLEAN_TOLERANCE:
            return True
        inverse = 1.0 / ratio
        if abs(factor - inverse) <= inverse * _CLEAN_TOLERANCE:
            return True
    return False


def _int(value):
    text = str(value if value is not None else "").replace(",", "").strip()
    if not text:
        return None
    try:
        return int(Decimal(text))
    except Exception:                                     # noqa: BLE001
        return None


class KrxDailyPanel:
    """KRX 패널 캐시에서 종목별 일봉을 만든다(분할 보정 포함)."""

    LAYER = "silver"
    TABLE = "daily_bars"

    def __init__(self, api, store: ParquetStore = None, registry=None,
                 market: str = "KRX", data_revision: int = 1):
        self.api = api
        self.store = store or ParquetStore()
        self.registry = registry or default_registry()
        self.market = market
        self.data_revision = int(data_revision)

    # ── 읽기 ──────────────────────────────────────────────────────
    def raw_series(self, codes, days, on_progress=None):
        """{종목코드: [일별 원본 …]} — 캐시된 날짜만 훑는다."""
        wanted = {str(c).split(":")[-1] for c in codes}
        series = {code: [] for code in wanted}
        scanned = 0
        for day in days:
            bas = day.strftime("%Y%m%d")
            rows = self.api.fetch(STOCK_DAILY, bas)
            if not rows:
                continue
            scanned += 1
            for row in rows:
                code = str(row.get("ISU_CD") or "").strip()
                if code not in wanted:
                    continue
                series[code].append({
                    "trading_date": day,
                    "name": str(row.get("ISU_NM") or "").strip(),
                    "open": _int(row.get("TDD_OPNPRC")),
                    "high": _int(row.get("TDD_HGPRC")),
                    "low": _int(row.get("TDD_LWPRC")),
                    "close": _int(row.get("TDD_CLSPRC")),
                    "volume": _int(row.get("ACC_TRDVOL")) or 0,
                    "traded_value": _int(row.get("ACC_TRDVAL")),
                    "shares": _int(row.get("LIST_SHRS")),
                    "mktcap": _int(row.get("MKTCAP")),
                })
            if on_progress:
                on_progress(day, scanned)
        return series

    # ── 보정 ──────────────────────────────────────────────────────
    @staticmethod
    def detect_events(series):
        """분할·무상증자로 보이는 날을 찾는다.

        반환: [{"trading_date", "factor", "kind", …}] — `kind`는 SPLIT 또는
        CAPITAL(보정하지 않고 표시만 하는 자본변동).
        """
        events = []
        for prev, cur in zip(series, series[1:]):
            if not (prev["shares"] and cur["shares"]):
                continue
            if not (prev["close"] and cur["close"]):
                continue
            f_shares = cur["shares"] / prev["shares"]
            if abs(f_shares - 1.0) < _SHARES_TOLERANCE:
                continue
            f_price = prev["close"] / cur["close"]
            match = abs(f_price / f_shares - 1.0)
            f_cap = ((cur["mktcap"] / prev["mktcap"])
                     if (prev["mktcap"] and cur["mktcap"]) else None)
            cap_ok = (f_cap is not None
                      and abs(f_cap - 1.0) <= _MKTCAP_TOLERANCE)
            is_split = (match <= _MATCH_TOLERANCE and cap_ok
                        and _clean_ratio(f_shares))
            events.append({
                "trading_date": cur["trading_date"],
                "factor": f_shares,
                "price_ratio": f_price,
                "mktcap_ratio": round(f_cap, 6) if f_cap else None,
                "mismatch": round(match, 6),
                "clean_ratio": _clean_ratio(f_shares),
                "kind": "SPLIT" if is_split else "CAPITAL",
            })
        return events

    def adjust(self, series):
        """원본 시계열 → (보정된 시계열, 이벤트). 보정은 과거 쪽에 적용한다."""
        events = self.detect_events(series)
        splits = {e["trading_date"]: e["factor"]
                  for e in events if e["kind"] == "SPLIT"}
        capital = {e["trading_date"] for e in events if e["kind"] == "CAPITAL"}

        # 뒤에서 앞으로 누적한다. 어떤 날의 계수는 그날 **이후** 분할의 곱이다.
        factors, running = {}, 1.0
        for bar in reversed(series):
            factors[bar["trading_date"]] = running
            if bar["trading_date"] in splits:
                running *= splits[bar["trading_date"]]

        out = []
        for bar in series:
            factor = factors[bar["trading_date"]]
            record = dict(bar)
            record["adjustment_factor"] = factor
            record["flags"] = set()
            if factor != 1.0:
                record["flags"].add(SPLIT_FLAG)
            if bar["trading_date"] in capital:
                record["flags"].add(CAPITAL_FLAG)
            if not bar["volume"]:
                record["flags"].add(NO_TRADE_FLAG)
            for field in ("open", "high", "low", "close"):
                value = bar[field]
                if value is None or value <= 0:
                    record[field] = None
                    continue
                record[field] = (Decimal(value) / Decimal(str(factor))
                                 ).quantize(_QUANT) if factor != 1.0 \
                    else Decimal(value)
            record["volume"] = (int(round(bar["volume"] * factor))
                                if factor != 1.0 else bar["volume"])
            out.append(record)
        return out, events

    # ── 적재 ──────────────────────────────────────────────────────
    def to_rows(self, code, adjusted, run_id, fetched_at):
        security_id = normalize_security_id(code)
        fetched_utc = to_utc(fetched_at)
        rows = []
        for bar in adjusted:
            if None in (bar["open"], bar["high"], bar["low"], bar["close"]):
                # 가격이 없는 날(정지·미거래)은 저장하지 않는다. 0을 가격으로
                # 저장하면 수익률이 −100%가 되고, 그게 조용히 통계로 간다.
                continue
            day = bar["trading_date"]
            rule = self.registry.resolve(self.market, day)
            flags = set(bar["flags"]) | {PANEL_FLAG}
            status = QualityStatus.SUCCESS
            if ohlc_violations_of(bar):
                flags.add(OHLC_VIOLATION_FLAG)
                status = QualityStatus.FAILED
            rows.append({
                "security_id": security_id,
                "trading_date": day,
                "open": bar["open"], "high": bar["high"],
                "low": bar["low"], "close": bar["close"],
                "volume": int(bar["volume"]),
                "traded_value": (Decimal(bar["traded_value"])
                                 if bar["traded_value"] is not None else None),
                # KRX 일별매매정보의 종가는 **정규장 종가**다. 2026-09-14
                # 애프터마켓 시행 뒤에도 그렇다. 실측(000660 2026-09-15):
                # KRX 1,690,000 = 정규장 15:35 종가, 키움 1,712,000 = 20:00
                # 애프터마켓 종가. 시장 규칙의 기본 scope를 그대로 쓰면 이
                # 행들이 EXTENDED로 잘못 표시된다.
                "price_scope": "REGULAR",
                "adjustment_type": "SPLIT_ADJUSTED",
                "adjustment_factor": float(bar["adjustment_factor"]),
                "adjustment_as_of_utc": fetched_utc,
                "available_at_utc": to_utc(rule.daily_confirmed_at(day)),
                "source": KRX_OPENAPI_SOURCE,
                "quality_status": status,
                "quality_flags": sorted(flags),
                "run_id": run_id,
                "market_rule_version": rule.version,
                "schema_version": SCHEMA_VERSION,
                "data_revision": self.data_revision,
            })
        return rows

    @staticmethod
    def _partition(row):
        day = row["trading_date"]
        return [("market", "KRX"), ("year", day.year),
                ("month", f"{day.month:02d}")]

    def build(self, codes, days, run_id: str, dry_run: bool = False,
              on_progress=None):
        if not codes:
            raise NoInputDataError("종목이 없다")
        fetched_at = datetime.now(tz=KST)
        series = self.raw_series(codes, days, on_progress=on_progress)

        rows, per_security, events_all = [], [], []
        for code, bars in sorted(series.items()):
            if not bars:
                per_security.append({"code": code, "bars": 0,
                                     "status": "NO_DATA"})
                continue
            adjusted, events = self.adjust(bars)
            got = self.to_rows(code, adjusted, run_id, fetched_at)
            rows.extend(got)
            splits = [e for e in events if e["kind"] == "SPLIT"]
            capital = [e for e in events if e["kind"] == "CAPITAL"]
            per_security.append({
                "code": code, "name": bars[-1]["name"], "bars": len(got),
                "first_date": got[0]["trading_date"].isoformat() if got else None,
                "last_date": got[-1]["trading_date"].isoformat() if got else None,
                "splits": len(splits), "capital_changes": len(capital),
                "status": "SUCCESS" if got else "NO_PRICE",
            })
            for event in events:
                events_all.append(dict(event, code=code,
                                       trading_date=event["trading_date"].isoformat()))

        summary = {
            "run_id": run_id,
            "securities": len(series),
            "with_bars": sum(1 for s in per_security if s["bars"]),
            "rows": len(rows),
            "splits": sum(1 for e in events_all if e["kind"] == "SPLIT"),
            "capital_changes": sum(1 for e in events_all if e["kind"] == "CAPITAL"),
            "per_security": per_security,
            "events": events_all,
            "artifacts": [],
        }
        if dry_run or not rows:
            return summary

        write = self.store.write(
            self.LAYER, self.TABLE, rows, DAILY_BAR_COLUMNS, DAILY_BAR_PK,
            run_id, partition_fn=self._partition,
            extra_manifest={
                "source": KRX_OPENAPI_SOURCE,
                "note": ("KRX 일별매매정보는 수정주가가 아니다. 상장주식수와 "
                         "가격이 서로 역수로 움직인 날을 분할로 보고 과거 쪽을 "
                         "보정했다. 시가총액이 함께 늘어난 자본변동은 보정하지 "
                         "않고 CAPITAL_CHANGE_UNADJUSTED로 표시했다"),
                "splits": summary["splits"],
                "capital_changes": summary["capital_changes"],
            })
        summary["artifacts"] = [write.manifest_path]
        return summary

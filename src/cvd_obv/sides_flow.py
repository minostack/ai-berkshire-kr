# -*- coding: utf-8 -*-
"""체결량 흐름 기준 (sides-1.0) — 스캐너·백테스트가 공유하는 **하나의** 정의.

무엇을 푸는가
------------
`silver.daily_trade_sides`에는 시장 구분이 셋 있다(대비검토 §8 실측).

    J  = KRX 단독      NX = NXT 단독      UN = J + NX (잔차 0으로 확인)

2026-10-20부터 **매일 받는 구분은 `UN` 하나**이고 `J`·`NX`는 과거 행만 남아
있다(`docs/BAR_STANDARD.md`). 그래서 어느 한 구분만 읽으면 구간마다 끊긴다.
이 모듈이 **구간별로 옳은 행을 골라** 통합 기준 흐름 하나로 돌려준다.

    2025-03-04(NXT 개시) 이후 : 저장된 `UN`. 없으면 `J + NX`로 합성
    그 전                     : `UN = J` (NXT가 존재하지 않았다)

왜 통합 기준인가 — 일봉과 맞추기 위해서다
-----------------------------------------
2026-10-20 A안으로 **정규장 일봉 거래량에 NXT 메인마켓이 들어왔다**
(`NXT_MAIN_VOLUME_INCLUDED`, 리비전 2). OBV·유동성·MA20은 그 거래량에서
나오므로, 흐름만 `J`(KRX 단독)로 두면 **같은 봉에서 분자와 분모의 시장이
어긋난다**. `UN`을 쓰면 둘이 같은 시장을 본다.

**다만 수익 근거는 아니다.** 사전등록 NXTBASIS(대장 9번)에서 기준을 `J`→`UN`
으로 바꾼 효과는 `UNDERPOWERED`였다(가속 +0.0363%p [−0.009, +0.080],
MDE 0.064, 358거래일). 통합 기준을 쓰는 근거는 **일봉과의 정합성**이지
"더 번다"가 아니다 — `reports/NXTBASIS-KRX단독-통합기준-비교-20261019.md`

흐름 정의
--------
    f(d) = (buy − sell) / (buy + sell)        범위 −1 … +1

`buy + sell`을 **거래량으로 쓰지 않는다**. 정규장 거래량의 92~96%이고
수정주가가 반영되지 않은 원시 주식수다(대비검토 §6·§7). 흐름은 같은 날
자기 합으로 정규화되므로 그 둘 다 영향이 없다.
"""

from __future__ import annotations

from datetime import date

from .contracts.market_schema import TRADE_SIDE_ZERO_FLAG
from .ingestion.kis_trade_sides import (
    FLAG_PROVISIONAL, NXT_START, read_trade_sides,
)

__all__ = ["BASIS_VERSION", "NXT_START", "SidesDay", "read_sides_flow",
           "flow_series"]

#: 이 기준의 이름. 스캐너·결과 JSON에 그대로 적는다.
BASIS_VERSION = "sides-1.0"

#: 어느 구분에서 왔는지 — 결과에 남겨 구간을 되짚을 수 있게 한다
SOURCE_UN = "UN"            # 저장된 통합 행
SOURCE_JNX = "J+NX"         # UN이 없어 합성
SOURCE_J = "J"              # NXT 개시 전


class SidesDay:
    """하루치 체결량 — 불변으로 다룬다."""

    __slots__ = ("buy", "sell", "source", "provisional")

    def __init__(self, buy: int, sell: int, source: str, provisional: bool):
        self.buy = int(buy)
        self.sell = int(sell)
        self.source = source
        self.provisional = bool(provisional)

    @property
    def total(self) -> int:
        return self.buy + self.sell

    @property
    def f(self):
        """(매수 − 매도) / (매수 + 매도). 합이 0이면 None(흐름 없음)."""
        t = self.total
        return ((self.buy - self.sell) / t) if t > 0 else None

    def __repr__(self):
        return (f"SidesDay(buy={self.buy:,}, sell={self.sell:,}, "
                f"f={self.f}, source={self.source!r}, "
                f"provisional={self.provisional})")


def _usable(flags, buy, sell) -> bool:
    """흐름을 만들 수 있는 행인가 — 0/0은 '흐름 없음'이지 0이 아니다."""
    if TRADE_SIDE_ZERO_FLAG in (flags or []):
        return False
    return (buy + sell) > 0


def read_sides_flow(security_ids, start: date, end: date, store=None) -> dict:
    """{종목코드: {거래일: SidesDay}} — 구간별로 옳은 구분을 골라 통합한다.

    `security_ids`는 `KRX:005930` 형태이고 돌려주는 키는 `005930`이다
    (스캐너가 코드로 다루기 때문). 한 번만 읽고 메모리에서 고른다.
    """
    import pyarrow.dataset as ds

    sids = sorted(set(security_ids))
    t = read_trade_sides(
        store=store,
        columns=["security_id", "trading_date", "buy_qty", "sell_qty",
                 "market_div", "quality_flags"],
        filters=(ds.field("trading_date") >= start)
        & (ds.field("trading_date") <= end)
        & ds.field("security_id").isin(sids))

    per = {}            # {(code, date): {market_div: (buy, sell, provisional)}}
    for sid, d, b, s, md, fl in zip(
            t.column("security_id").to_pylist(),
            t.column("trading_date").to_pylist(),
            t.column("buy_qty").to_pylist(),
            t.column("sell_qty").to_pylist(),
            t.column("market_div").to_pylist(),
            t.column("quality_flags").to_pylist()):
        if not _usable(fl, b, s):
            continue
        code = sid.split(":")[-1]
        per.setdefault((code, d), {})[md] = (
            int(b), int(s), FLAG_PROVISIONAL in (fl or []))

    out = {}
    for (code, d), by_div in per.items():
        if d < NXT_START:
            # NXT가 없던 때 — J가 곧 통합이다
            hit = by_div.get("J")
            src = SOURCE_J
        elif "UN" in by_div:
            hit = by_div["UN"]
            src = SOURCE_UN
        elif "J" in by_div:
            # 저장된 UN이 없는 구간 — J + NX로 합성한다. NX가 없으면
            # 그날 NXT 체결이 없는 것으로 본다(사전등록 NXTBASIS §1.1과 같은 규칙).
            jb, js, jp = by_div["J"]
            nb, ns, npv = by_div.get("NX", (0, 0, False))
            hit = (jb + nb, js + ns, jp or npv)
            src = SOURCE_JNX if (nb + ns) > 0 else SOURCE_J
        else:
            # NX만 있는 날 — 기준이 비대칭해지므로 쓰지 않는다
            continue
        if hit is None:
            continue
        b, s, prov = hit
        if b + s <= 0:
            continue
        out.setdefault(code, {})[d] = SidesDay(b, s, src, prov)
    return out


def flow_series(days: dict, calendar) -> tuple:
    """{거래일: SidesDay} → 캘린더 길이의 (f, 가중치, 잠정여부) 배열.

    `f`는 흐름 없는 날 NaN, 가중치는 `buy + sell`(창 가중평균용),
    잠정 여부는 그날 행이 16:10 잠정인지다. numpy 배열 세 개를 돌려준다.
    """
    import numpy as np
    n = len(calendar)
    f = np.full(n, np.nan)
    w = np.full(n, np.nan)
    prov = np.zeros(n, bool)
    pos = {d: i for i, d in enumerate(calendar)}
    for d, sd in days.items():
        i = pos.get(d)
        if i is None:
            continue
        v = sd.f
        if v is None:
            continue
        f[i] = v
        w[i] = sd.total
        prov[i] = sd.provisional
    return f, w, prov

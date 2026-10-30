# -*- coding: utf-8 -*-
"""사전등록 CVDSEL 전향 기록 — 스캐너 결과에 S0~S3 통과 여부를 남긴다.

`docs/PREREGISTRATION_CVDSEL.md` §3 보조: "2026-10-26 이후 `cvd_selection.scan` 결과에
S0~S3 통과 여부를 함께 남긴다. 표본이 쌓일 때까지 판정하지 않는다."

정의는 백테스트(`tools/cvdsel_backtest.sets`)와 같은 **일봉 재현 조건**이다. 판정에 쓰지
않는다 — 스캐너 선정(`filter_pass`)은 그대로다.

백테스트와 다른 점(기록에 남긴다):
- RVOL·AVG_A20은 스캐너 값(1분봉 범위, 블록딜 제외). 백테스트는 일봉 거래량(블록딜 포함)
- 체결량은 sides-1.0 통합(UN). 백테스트 기간(2010~2021)은 KRX 단독(J)
"""

from __future__ import annotations

PREREG_ID = "CVDSEL"
PREREG_SHA8 = "20a44155"
SETS = ("S0", "S1", "S3", "S2")

MIN_AVG_A20 = 100e8
MIN_DR5, MIN_POS5, MIN_RVOL, MIN_CLV = 5.0, 3, 1.3, 0.7
MAX_ATRP = 4.0
MIN_DDR = 10.0


def _ge(v, th):
    return v is not None and v >= th


def _gt(a, b):
    return a is not None and b is not None and a > b


def cvdsel_sets(row: dict) -> dict:
    """{S0, S1, S3, S2: bool} — 계산할 수 없는 지표는 불통과(백테스트와 같다)."""
    liq = _ge(row.get("avg_a20"), MIN_AVG_A20)
    flow = (_ge(row.get("dr5"), MIN_DR5) and _ge(row.get("pos5"), MIN_POS5)
            and _gt(row.get("dr2"), 0) and _gt(row.get("delta_prev"), 0))
    common = (liq and flow and _ge(row.get("rvol"), MIN_RVOL)
              and _ge(row.get("clv"), MIN_CLV))
    c, ma20, ma60, lag = (row.get(k) for k in ("krx_close", "ma20", "ma60", "ma20_lag"))
    up = _gt(ma20, lag)
    trend_spec = _gt(c, ma20) and _gt(ma20, ma60) and up
    trend_relax = _gt(c, ma60) and up
    atr = row.get("atrp14")
    atrp_ok = atr is not None and atr <= MAX_ATRP
    dr5, dr20 = row.get("dr5"), row.get("dr20")
    ddr = (dr5 - dr20) if dr5 is not None and dr20 is not None else None
    return {
        "S0": bool(common and trend_spec and atrp_ok),
        "S1": bool(common and trend_relax and atrp_ok),
        "S3": bool(common and trend_spec),
        "S2": bool(liq and _ge(ddr, MIN_DDR)),
    }


def as_text(sets: dict) -> str:
    return ";".join(f"{k}={int(sets[k])}" for k in SETS)


__all__ = ["PREREG_ID", "PREREG_SHA8", "SETS", "cvdsel_sets", "as_text"]

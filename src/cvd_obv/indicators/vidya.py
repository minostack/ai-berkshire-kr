# -*- coding: utf-8 -*-
"""VIDYA (Variable Index Dynamic Average) — Tushar Chande, CMO 판(1995 개정).

정의는 `docs/PREREGISTRATION_VIDYA.md` §1.2에 고정돼 있다. 바꾸면 사전등록
위반이다.

    ΔP_t  = P_t − P_{t−1}
    Up_t  = max(ΔP_t, 0),  Down_t = max(−ΔP_t, 0)
    CMO_t = (ΣUp − ΣDown) / (ΣUp + ΣDown)      최근 n_cmo개, 범위 −1 … +1
    k     = 2 / (n_vidya + 1)
    α_t   = k × |CMO_t|
    VIDYA_t = α_t × P_t + (1 − α_t) × VIDYA_{t−1}

척도 주의 — CMO를 **−1~+1**로 쓴다(×100 하지 않는다). 그래서 α ≤ k ≤ 1이
보장된다. ×100 척도를 쓰는 구현과 값을 비교하려면 그쪽 α를 100으로 나눠야
한다.

움직임이 없는 구간(ΣUp + ΣDown = 0)은 CMO가 정의되지 않는다. 이때 α = 0으로
본다 — 가격이 변하지 않았으므로 평균도 변하지 않는다는 뜻이고, 0으로 나누지
않는다.
"""

from __future__ import annotations

import numpy as np

#: 사전등록 §1.2 고정값
CMO_LENGTH = 9
VIDYA_LENGTH = 20

__all__ = ["CMO_LENGTH", "VIDYA_LENGTH", "cmo", "cmo_series", "vidya"]


def cmo(prices, length: int = CMO_LENGTH) -> float:
    """마지막 봉 기준 CMO 한 값(−1 … +1). 봉이 부족하면 NaN."""
    s = cmo_series(prices, length)
    return float(s[-1]) if len(s) else float("nan")


def cmo_series(prices, length: int = CMO_LENGTH) -> np.ndarray:
    """각 봉을 기준일로 본 CMO 배열. 앞 `length`개는 NaN.

    `length`개의 **변화량**(= `length + 1`개의 가격)이 필요하다.
    """
    if length < 1:
        raise ValueError(f"length는 1 이상이어야 한다: {length}")
    p = np.asarray(prices, dtype=float)
    n = len(p)
    out = np.full(n, np.nan)
    if n < length + 1:
        return out
    d = np.diff(p)
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    # 길이 length의 이동합 — cumsum 차분
    cu = np.concatenate(([0.0], np.cumsum(up)))
    cd = np.concatenate(([0.0], np.cumsum(dn)))
    su = cu[length:] - cu[:-length]
    sd = cd[length:] - cd[:-length]
    tot = su + sd
    val = np.divide(su - sd, tot, out=np.zeros_like(tot), where=tot > 0)
    # d[i] = p[i+1] - p[i] 이므로 su[j]는 p[j+length]를 기준일로 한 값이다
    out[length:] = val
    return out


def vidya(prices, length: int = VIDYA_LENGTH,
          cmo_length: int = CMO_LENGTH) -> np.ndarray:
    """각 봉을 기준일로 본 VIDYA 배열. 시드 전은 NaN.

    시드 = 첫 `length`개 가격의 단순평균이며, 그 값은 인덱스 `length − 1`에
    놓는다. 재귀는 CMO가 정의되는 봉(`cmo_length` 이후)부터 돈다. 두 조건을
    모두 만족하는 첫 봉은 `max(length − 1, cmo_length)`다.

    시드 방식은 구현마다 다르다. 워밍업을 충분히(수백 봉) 주면 시드 영향은
    사라진다 — 사전등록 §1.2가 신호 시작 250거래일 전부터 돌리라고 한 이유다.
    """
    if length < 1:
        raise ValueError(f"length는 1 이상이어야 한다: {length}")
    p = np.asarray(prices, dtype=float)
    n = len(p)
    out = np.full(n, np.nan)
    start = max(length - 1, cmo_length)
    if n <= start:
        return out
    c = cmo_series(p, cmo_length)
    k = 2.0 / (length + 1.0)
    prev = float(np.mean(p[:length]))
    out[start] = prev
    for i in range(start + 1, n):
        a = k * abs(c[i])
        if not np.isfinite(a):
            a = 0.0
        prev = a * p[i] + (1.0 - a) * prev
        out[i] = prev
    return out

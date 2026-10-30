# -*- coding: utf-8 -*-
"""백테스터 공용 도우미 — Stage 2와 Stage 6이 같은 계산을 쓴다.

같은 통계를 두 번 구현하면 둘이 갈릴 수 있다. 다중비교 보정·회전율·유동성
분위처럼 **결론의 강도를 정하는 계산**은 특히 그렇다. 그래서 한 곳에 둔다.
"""

from __future__ import annotations

import numpy as np

__all__ = ["apply_fdr", "turnover", "segment_order", "liquidity_buckets",
           "nanmean", "nanmedian", "bool_array"]

#: 신뢰구간 열 이름이 `<추정치>_ci_low` 규칙을 따르지 않는 몇 개. 결과 행의
#: 열 이름은 Stage 2가 먼저 정했고 그 이름으로 보고서가 나가 있어서, 규칙을
#: 바꾸는 대신 예외를 여기 적어 둔다.
_CI_KEYS = {
    "delta_return_net": ("delta_return_ci_low", "delta_return_ci_high"),
    "delta_vs_all": ("delta_ci_low", "delta_ci_high"),
}


def turnover(securities, days):
    """하루 선택 종목 중 전 거래일에 없던 비율의 평균(로드맵 Stage 2 DoD).

    선택률(coverage)이 "얼마나 많이 고르는가"라면 회전율은 "얼마나 자주 바꾸는가"다.
    같은 선택률이어도 회전율이 높으면 비용이 그만큼 더 든다.
    """
    by_day = {}
    for sec, day in zip(securities, days):
        by_day.setdefault(day, set()).add(sec)
    ordered = sorted(by_day)
    if len(ordered) < 2:
        return None
    ratios = []
    for prev, cur in zip(ordered, ordered[1:]):
        current = by_day[cur]
        if not current:
            continue
        fresh = current - by_day[prev]
        ratios.append(len(fresh) / len(current))
    return float(np.mean(ratios)) if ratios else None


def segment_order(labels):
    """세그먼트를 보기 좋은 순서로. 숫자는 숫자순, 나머지는 사전순."""
    values = sorted(set(labels.tolist()))
    numeric = [v for v in values if str(v).isdigit()]
    if len(numeric) == len(values):
        return [str(v) for v in sorted(values, key=int)]
    return values


def liquidity_buckets(liquidity, days, n_buckets: int = 5):
    """그날 유니버스 안에서의 상대 유동성 5분위.

    절대 금액으로 자르면 구간이 시대에 따라 달라진다(2010년의 100억과 2026년의
    100억은 다른 위치다). 그날 안에서의 순위로 자르면 그 문제가 없다.
    """
    labels = np.full(liquidity.size, "LIQ_NA", dtype=object)
    by_day = {}
    for i, day in enumerate(days):
        by_day.setdefault(day, []).append(i)
    for indices in by_day.values():
        idx = np.asarray(indices)
        values = liquidity[idx]
        ok = ~np.isnan(values)
        if ok.sum() < n_buckets:
            continue
        ranks = np.argsort(np.argsort(values[ok]))
        edges = (ranks / max(ok.sum(), 1) * n_buckets).astype(int)
        edges = np.clip(edges, 0, n_buckets - 1)
        labels[idx[ok]] = [f"Q{e + 1}" for e in edges]
    return np.asarray(labels.tolist())


def apply_fdr(rows, alpha: float = 0.05, value_key="delta_return_net",
              ci_keys=None, family=None, dedupe_by=None):
    """한 검정 가족에 Benjamini-Hochberg 보정을 건다.

    조합 x horizon x 결과를 한꺼번에 보면 우연히 유의한 것이 나온다. CI만으로
    "유의하다"고 말하면 그 다중성이 감춰진다. p값은 추정치와 그 신뢰구간에서
    정규근사로 되돌린 값이므로 **근사**다(`p_value_method`에 적어 둔다).

    `value_key`가 검정할 추정치의 열 이름이고, 구간은 `ci_keys`(주지 않으면
    `_CI_KEYS`의 규칙)가 가리키는 두 열에서 읽는다. 결과는
    `<value_key>_q_value`와
    `<value_key>_fdr_significant`에 쓴다. 가족을 나누는 이유는, 서로 다른
    질문(확률 차이·수익 차이·순위상관)을 한 덩어리로 묶으면 조합 수가 많은
    쪽이 다른 쪽의 문턱을 좌우하기 때문이다.

    `dedupe_by`를 주면 그 키가 같은 행들을 **검정 하나로** 센다. 수익률 차이는
    outcome(up·up_gross)에 따라 달라지지 않으므로, 결과별로 행이 복제된 채
    세면 검정 수가 실제보다 많아 보인다. q값은 묶인 행 전부에 같이 쓴다.
    """
    from math import erf, sqrt

    low_key, high_key = (ci_keys or _CI_KEYS.get(
        value_key, (f"{value_key}_ci_low", f"{value_key}_ci_high")))
    p_key, q_key = f"{value_key}_p_value", f"{value_key}_q_value"
    sig_key = f"{value_key}_fdr_significant"
    label = family or value_key

    groups = {}
    for row in rows:
        value = row.get(value_key)
        low, high = row.get(low_key), row.get(high_key)
        if value is None or low is None or high is None or high <= low:
            continue
        se = (high - low) / (2.0 * 1.959963985)
        if se <= 0:
            continue
        z = abs(value) / se
        row[p_key] = 2.0 * (1.0 - 0.5 * (1.0 + erf(z / sqrt(2.0))))
        row["p_value_method"] = "NORMAL_APPROX_FROM_CI"
        key = (tuple(row.get(k) for k in dedupe_by) if dedupe_by
               else id(row))
        groups.setdefault(key, []).append(row)

    ordered = sorted(groups.values(), key=lambda g: g[0][p_key])
    total = len(ordered)
    smallest = 1.0
    for rank in range(total, 0, -1):
        group = ordered[rank - 1]
        q = min(smallest, group[0][p_key] * total / rank)
        smallest = q
        for row in group:
            row[q_key] = q
            row[sig_key] = bool(q <= alpha)
            row["fdr_alpha"] = alpha
            row[f"{value_key}_fdr_tests"] = total
            if value_key == "delta_return_net":
                # Stage 2 표와 기존 테스트가 쓰는 이름. 기본 가족에만 남긴다.
                row["fdr_significant"] = row[sig_key]
                row["fdr_tests"] = total
                row["fdr_family"] = label
    return rows


def bool_array(values):
    """None을 -1로 두는 int8 배열. 판정 불가와 False를 구분한다."""
    return np.asarray([-1 if v is None else (1 if v else 0) for v in values],
                      dtype=np.int8)


def nanmean(values):
    values = np.asarray(values, dtype=np.float64)
    return float(np.nanmean(values)) if np.isfinite(values).any() else None


def nanmedian(values):
    values = np.asarray(values, dtype=np.float64)
    return float(np.nanmedian(values)) if np.isfinite(values).any() else None



# `daily_gate`가 모듈 내부 이름으로 쓰던 밑줄 별칭. 호출부와 기존 테스트를
# 건드리지 않으려고 남긴다.
_apply_fdr = apply_fdr
_turnover = turnover
_segment_order = segment_order
_liquidity_buckets = liquidity_buckets
_nanmean = nanmean
_nanmedian = nanmedian
_bool_array = bool_array

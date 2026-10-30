# -*- coding: utf-8 -*-
"""V1.5 점수·Gate를 저장된 feature에서 되짚는다 — 로드맵 Stage 8의 토대.

Ablation을 하려면 "CVD를 뺀 점수"를 만들 수 있어야 한다. 그런데 V1.5 계산
코어는 Stage 0에서 동결했고 고칠 수 없다. 그래서 **점수 조립 규칙만** 여기에
다시 적고, `gold.signal_features`에 저장된 원시 값에서 점수를 다시 만든다.

이게 정당한지는 한 가지로 확인한다 — **되짚은 점수가 V1.5가 낸 `total_score`와
같은가.** 같으면 조립 규칙이 맞다는 뜻이고, 그 위에서 항을 빼는 것도 믿을 수
있다. 다르면 ablation 전부가 무의미하므로 hard fail로 둔다.

조립 규칙 (V1.5 `_finalize_scores`)
-----------------------------------
    cvd_pts   = pct(normalized_delta) x 20
              + pct(normalized_momentum) x 10
              + pct(bull_ratio_completed) x 10        (최대 40)
    obv_pts   = 15 x (obv > obv_ma20) + pct(obv_normalized_slope) x 20  (최대 35)
    price_pts = 5 x above_ma20 + 4 x ma20_rising
              + 3 x above_ma60 + 3 x ma20_above_ma60  (최대 15)
    liq_pts   = pct(avg_traded_value_20d) x 10        (최대 10)
    total     = clip(cvd + obv + price + liq + risk_penalty, 0, 100)

`pct`는 그날 스캔 대상 중 **그 항목의 품질이 SUCCESS인 종목**만으로 매긴
백분위다(동률은 평균 순위). 품질 필터를 빠뜨리면 PARTIAL·FAILED 종목의 수치가
정상 종목의 상대 순위를 밀어 올린다 — 실제로 처음 구현에서 이걸 빠뜨려
50,420행 중 10,703행이 V1.5와 어긋났다. 그래서 한 종목의 점수는 그날 누가
같이 스캔됐는지에 달려 있고, 항을 빼면 백분위를 다시 계산해야 한다.

Gate는 절대 조건이라 백분위와 무관하다. 조건 하나하나가 어느 성분에서 오는지
`GATE_COMPONENT`에 적어 두고, 성분을 뺄 때 그 조건도 같이 뺀다.
"""

from __future__ import annotations

import math

from ..domain.errors import DataQualityError
from ..domain.status import SignalStatus

__all__ = ["COMPONENTS", "CVD_TERMS", "GATE_COMPONENT", "ScoreModel",
           "percentile_ranks"]

#: 점수를 이루는 성분과 배점. 로드맵 Stage 8이 이름으로 요구한 제거 단위다.
COMPONENTS = ("cvd", "obv", "price", "liquidity")

#: CVD 안의 개별 항 — 이것도 따로 뺄 수 있어야 한다(로드맵 Stage 8).
#: (값 열, 배점, **백분위 모집단을 정하는 품질 열**).
CVD_TERMS = {
    "cvd_delta": ("cvd_normalized_delta", 20.0, "cvd_data_status"),
    "cvd_momentum": ("cvd_normalized_momentum", 10.0, "cvd_data_status"),
    # 지속성은 기준 세션과 무관하게 **정규장** 완료 거래일 값을 쓰므로
    # 모집단도 정규장 품질로 거른다(V1.5 `br_map`).
    "cvd_bull_ratio": ("cvd_bull_ratio_completed", 10.0,
                       "cvd_regular_data_status"),
}

#: Gate 조건이 어느 성분에 속하는가. 성분을 빼면 그 조건도 함께 빠진다.
#: 조건만 빼고 점수를 남기거나 그 반대로 하면, 결과 차이가 어디서 왔는지
#: 알 수 없게 된다.
GATE_COMPONENT = {
    "cvd_data": "cvd",
    "cvd_delta": "cvd_delta",
    "cvd_momentum": "cvd_momentum",
    "cvd_momentum_status": "cvd_momentum",
    "cvd_bull_ratio": "cvd_bull_ratio",
    "obv_data": "obv",
    "obv_trend": "obv",
    "price_data": "price",
    "price_above_ma20": "price",
    "liquidity_present": "liquidity",
}

_WARMING = ("WARMING_UP", "NO_DATA")


def _eligible(rows, field, status_field):
    """백분위 모집단 — 그 항목의 품질이 SUCCESS인 행만 값으로 남긴다."""
    return [row.get(field) if row.get(status_field) == "SUCCESS" else None
            for row in rows]


def percentile_ranks(values):
    """V1.5 `_percentile_ranks`와 같은 규칙.

    동률은 평균 순위로 백분위를 매기고, 유효값이 1개면 0.5(중립),
    없으면 전부 None이다. 이 규칙이 다르면 점수가 통째로 어긋난다.
    """
    valid = sorted(((i, v) for i, v in enumerate(values) if v is not None),
                   key=lambda item: item[1])
    out = [None] * len(values)
    n = len(valid)
    if n == 0:
        return out
    if n == 1:
        out[valid[0][0]] = 0.5
        return out
    i = 0
    while i < n:
        j = i
        while j + 1 < n and valid[j + 1][1] == valid[i][1]:
            j += 1
        pct = ((i + j) / 2) / (n - 1)
        for idx in range(i, j + 1):
            out[valid[idx][0]] = pct
        i = j + 1
    return out


class ScoreModel:
    """하루치 feature에서 점수와 Gate를 다시 만든다."""

    def __init__(self, drop=()):
        """`drop`은 제거할 성분·항 이름의 집합.

        `COMPONENTS`의 이름을 주면 그 성분의 배점과 Gate 조건이 모두 빠진다.
        `CVD_TERMS`의 이름을 주면 그 항의 배점과 대응 Gate 조건만 빠진다.
        `"gate"`를 주면 절대 조건 전체가 빠진다(점수는 그대로).
        """
        self.drop = frozenset(drop)
        unknown = self.drop - set(COMPONENTS) - set(CVD_TERMS) - {"gate"}
        if unknown:
            raise DataQualityError(
                f"제거할 수 없는 이름: {sorted(unknown)} "
                f"(가능: {sorted(set(COMPONENTS) | set(CVD_TERMS) | {'gate'})})")

    # ── 점수 ──────────────────────────────────────────────────

    def score_day(self, rows):
        """그날 행 목록에 점수를 매겨 리스트로 돌려준다.

        백분위가 그날 모집단에 걸려 있으므로 **하루 단위로만** 계산한다.
        """
        cvd_pts = [0.0] * len(rows)
        if "cvd" not in self.drop:
            for term, (field, weight, status_field) in CVD_TERMS.items():
                if term in self.drop:
                    continue
                pct = percentile_ranks(_eligible(rows, field, status_field))
                for i, value in enumerate(pct):
                    if value is not None:
                        cvd_pts[i] += value * weight

        obv_pts = [0.0] * len(rows)
        if "obv" not in self.drop:
            slope = percentile_ranks(
                _eligible(rows, "obv_normalized_slope", "obv_data_status"))
            for i, row in enumerate(rows):
                ma20 = row.get("obv_ma20")
                obv = row.get("obv")
                if ma20 is not None and obv is not None:
                    obv_pts[i] += 15.0 if obv > ma20 else 0.0
                if slope[i] is not None:
                    obv_pts[i] += slope[i] * 20.0

        price_pts = [0.0] * len(rows)
        if "price" not in self.drop:
            for i, row in enumerate(rows):
                if row.get("above_ma20") is True:
                    price_pts[i] += 5.0
                if row.get("ma20_rising") is True:
                    price_pts[i] += 4.0
                if row.get("above_ma60") is True:
                    price_pts[i] += 3.0
                if row.get("ma20_above_ma60") is True:
                    price_pts[i] += 3.0

        liq_pts = [0.0] * len(rows)
        if "liquidity" not in self.drop:
            pct = percentile_ranks(
                _eligible(rows, "avg_traded_value_20d",
                          "liquidity_data_status"))
            for i, value in enumerate(pct):
                liq_pts[i] = value * 10.0 if value is not None else 0.0

        out = []
        for i, row in enumerate(rows):
            penalty = row.get("risk_penalty") or 0.0
            total = cvd_pts[i] + obv_pts[i] + price_pts[i] + liq_pts[i] + penalty
            out.append(round(max(0.0, min(100.0, total)), 1))
        return out

    # ── Gate ──────────────────────────────────────────────────

    def gate_row(self, row):
        """한 행의 절대 조건 판정 — (status, 미충족 사유 목록).

        V1.5 `_compute_gate`의 정규장 경로와 같다. 애프터마켓 추가 조건은
        replay가 정규장 기준(15:35)으로만 돌았으므로 여기서는 쓰지 않는다.
        """
        if "gate" in self.drop:
            # 조건을 전부 없앤 변형. 데이터 품질만 남긴다 — 품질까지 없애면
            # 계산할 수 없는 종목이 후보에 들어와 비교가 성립하지 않는다.
            ok = self._data_ok(row)
            return (SignalStatus.BUY_CANDIDATE if ok
                    else SignalStatus.DATA_INSUFFICIENT), []

        reasons = []
        data_ok = self._data_ok(row)
        if not data_ok:
            reasons.append("cvd_data" if self._active("cvd") else "other_data")

        if self._active("cvd_delta"):
            value = row.get("cvd_normalized_delta")
            if value is None or value <= 0:
                reasons.append("cvd_delta")
        if self._active("cvd_momentum"):
            value = row.get("cvd_normalized_momentum")
            if value is None or value <= 0:
                reasons.append("cvd_momentum")
            if row.get("cvd_momentum_status") in _WARMING:
                reasons.append("cvd_momentum_status")
        if self._active("cvd_bull_ratio"):
            value = row.get("cvd_bull_ratio_completed")
            if value is None or value < 0.50:
                reasons.append("cvd_bull_ratio")
        if self._active("price"):
            if row.get("above_ma20") is not True:
                reasons.append("price_above_ma20")
        if self._active("obv"):
            slope = row.get("obv_normalized_slope")
            ma20 = row.get("obv_ma20")
            obv = row.get("obv")
            if not (slope is not None and slope > 0 and ma20 is not None
                    and obv is not None and obv > ma20):
                reasons.append("obv_trend")
        if self._active("liquidity"):
            if row.get("avg_traded_value_20d") is None:
                reasons.append("liquidity_present")

        if not data_ok:
            return SignalStatus.DATA_INSUFFICIENT, reasons
        if not reasons:
            return SignalStatus.BUY_CANDIDATE, reasons
        return SignalStatus.WATCH, reasons

    def _active(self, name):
        """그 조건이 아직 살아 있는가. CVD 항은 `cvd`를 빼도 같이 빠진다."""
        if name in self.drop:
            return False
        if name in CVD_TERMS and "cvd" in self.drop:
            return False
        return True

    def _data_ok(self, row):
        """데이터 품질. 성분을 빼면 그 성분의 품질 조건도 뺀다."""
        checks = []
        if self._active("cvd"):
            checks.append(row.get("cvd_data_status"))
        if self._active("obv"):
            checks.append(row.get("obv_data_status"))
        if self._active("price"):
            checks.append(row.get("price_data_status"))
        if not checks:
            # 성분을 다 뺐다면 품질로 거를 것이 없다.
            return True
        return all(status == "SUCCESS" for status in checks)

    # ── 검증 ──────────────────────────────────────────────────

    def verify(self, by_day, expected, tolerance: float = 0.05,
               max_mismatch_ratio: float = 0.0):
        """되짚은 점수가 V1.5의 `total_score`와 같은지 확인한다.

        `drop`이 비어 있을 때만 의미가 있다. 다르면 조립 규칙이 틀린 것이고,
        그 위의 ablation은 전부 무의미하므로 예외를 던진다.
        """
        if self.drop:
            raise DataQualityError(
                "검증은 아무것도 빼지 않은 모델에서만 할 수 있다")

        mismatches, total = [], 0
        for day in sorted(by_day):
            rows = by_day[day]
            got = self.score_day(rows)
            for row, value in zip(rows, got):
                total += 1
                want = expected.get(row["signal_id"])
                if want is None:
                    continue
                if not math.isclose(value, want, abs_tol=tolerance):
                    mismatches.append({
                        "signal_id": row["signal_id"],
                        "trading_date": day.isoformat(),
                        "reconstructed": value, "v15": want,
                        "diff": round(value - want, 4)})
        ratio = len(mismatches) / max(total, 1)
        if ratio > max_mismatch_ratio:
            raise DataQualityError(
                f"되짚은 점수가 V1.5와 다르다: {len(mismatches):,}/{total:,}행 "
                f"({ratio:.4%}) — 조립 규칙이 틀렸다면 ablation 전부가 "
                f"무의미하다",
                context={"mismatches": mismatches[:20],
                         "mismatch_n": len(mismatches), "rows": total})
        return {"rows": total, "mismatches": len(mismatches),
                "tolerance": tolerance}

    def verify_gate(self, by_day, expected, max_mismatch_ratio: float = 0.0):
        """되짚은 Gate 판정이 V1.5의 `status`와 같은지 확인한다."""
        if self.drop:
            raise DataQualityError(
                "검증은 아무것도 빼지 않은 모델에서만 할 수 있다")

        mismatches, total = [], 0
        for day in sorted(by_day):
            for row in by_day[day]:
                want = expected.get(row["signal_id"])
                if want is None:
                    continue
                total += 1
                status, reasons = self.gate_row(row)
                if status != want:
                    mismatches.append({
                        "signal_id": row["signal_id"],
                        "trading_date": day.isoformat(),
                        "reconstructed": status, "v15": want,
                        "reasons": reasons})
        ratio = len(mismatches) / max(total, 1)
        if ratio > max_mismatch_ratio:
            raise DataQualityError(
                f"되짚은 Gate 판정이 V1.5와 다르다: {len(mismatches):,}/"
                f"{total:,}행 ({ratio:.4%})",
                context={"mismatches": mismatches[:20],
                         "mismatch_n": len(mismatches), "rows": total})
        return {"rows": total, "mismatches": len(mismatches)}

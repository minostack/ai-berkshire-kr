# -*- coding: utf-8 -*-
"""선정 임계값·배점 — 명세 5·6장의 **검증용 시작값**(실측 최적값이 아니다)."""

from dataclasses import asdict, dataclass
from datetime import time


@dataclass(frozen=True)
class Thresholds:
    # ── 품질(3.2) ──
    min_daily_bars: int = 60          # 최소 유효 일봉 수(D 포함)
    flow_days: int = 5                # 확정 체결량 D-5..D-1
    vol_days: int = 20                # RVOL·AVG_A20 비교 구간(D 제외)
    # ── 필수 조건(5장) ──
    min_avg_value: float = 100e8      # AVG_A20 ≥ 100억 원
    ma_short: int = 20
    ma_long: int = 60
    ma_slope_lag: int = 5             # MA20_D > MA20_(D-5)
    min_dr5: float = 5.0              # DR5 ≥ +5%
    min_pos5: int = 3                 # POS5 ≥ 3
    min_rvol: float = 1.3
    min_clv: float = 0.70
    max_gap_vwap: float = 0.05        # 0 < GAPVWAP ≤ 0.05
    min_rlate: float = 0.0
    min_hold_close: float = -0.005
    max_atrp: float = 4.0             # ATRP14 ≤ 4%
    atr_days: int = 14
    hh_days: int = 20
    # ── 마감 30분 창(4.4) — KRX 연속매매 마지막 30분, 1분봉 시작 시각 기준 ──
    late_start: time = time(14, 50)
    late_end: time = time(15, 20)     # [14:50, 15:20) 봉 30개
    # ── VWAP(4.3) — 거래대금 없는 1분봉(키움에만 있고 KIS가 주지 않은 봉) 허용 상한.
    #    그 봉은 분자·분모에서 함께 뺀다(추정하지 않음). 넘으면 VWAP 계산 불가로 탈락.
    #    2026-10-22 실측: 해당 2종목 모두 NXT 1봉(10주·3주)
    vwap_max_unvalued_share: float = 0.001
    # ── 순위(6장) ──
    daily_pick: int = 3               # 일별 신규 최대 3종목

    def to_dict(self) -> dict:
        d = asdict(self)
        d["late_start"] = self.late_start.strftime("%H:%M")
        d["late_end"] = self.late_end.strftime("%H:%M")
        return d


#: (하한, 점수) — 값이 하한 이상인 마지막 구간의 점수. 필수 조건 통과 종목만 매긴다.
SCORE_TABLE = {
    "dr5": [(5.0, 15), (10.0, 20), (15.0, 25)],
    "pos5": [(3, 10), (4, 15), (5, 20)],
    "rvol": [(1.3, 5), (1.5, 10), (2.0, 15)],
    "clv": [(0.70, 5), (0.85, 10), (0.95, 15)],
}
BREAKOUT_SCORE = (15, 5)              # C_D > HH20_prev 이면 15, 아니면 5
VWAP_SCORE = ((0.02, 10), (0.05, 5))  # 0 초과~2% 이하 10 / 2% 초과~5% 이하 5


def default_thresholds() -> Thresholds:
    return Thresholds()


def thresholds_from(overrides: dict) -> Thresholds:
    """CLI 덮어쓰기 — 바꾼 값은 결과 JSON의 `thresholds_changed`에 남는다."""
    base = asdict(Thresholds())
    base.update({k: v for k, v in overrides.items() if v is not None})
    return Thresholds(**base)


def changed_fields(t: Thresholds) -> dict:
    d0 = Thresholds()
    return {k: v for k, v in asdict(t).items() if getattr(d0, k) != v}


__all__ = ["Thresholds", "SCORE_TABLE", "BREAKOUT_SCORE", "VWAP_SCORE",
           "default_thresholds", "thresholds_from", "changed_fields"]

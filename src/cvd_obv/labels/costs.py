# -*- coding: utf-8 -*-
"""거래비용 모델.

로드맵 §3: "비용 = 수수료·세금·슬리피지 구성값, gross와 net 동시 저장".

비용을 빼지 않은 수익률만 보면 회전율이 높은 전략이 실제보다 좋아 보인다.
반대로 비용을 아무 값이나 넣으면 결론이 그 값에 휘둘린다. 그래서 값은 설정
파일에 두고 `cost_model_version`으로 결과에 박아 둔다.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal

from ..domain.errors import ConfigError

__all__ = ["CostModel", "DEFAULT_COST_CONFIG"]

DEFAULT_COST_CONFIG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))),
    "configs", "costs", "krx_v1.json")


class CostModel:
    """매수·매도 1회 왕복 비용.

    적용 순서(이 순서가 `label_version`의 일부다):
      매수체결가 = 진입가 × (1 + 슬리피지)
      매도체결가 = 청산가 × (1 - 슬리피지)
      순수익률  = 매도체결가 × (1 - 수수료 - 세금) ÷ (매수체결가 × (1 + 수수료)) - 1

    증권거래세는 매도에만 붙는다. 매수 쪽에 세금을 넣으면 비용을 이중으로
    계산하게 된다.
    """

    __slots__ = ("version", "commission_rate", "tax_rate_sell",
                 "slippage_rate", "source")

    def __init__(self, version, commission_rate, tax_rate_sell, slippage_rate,
                 source=None):
        self.version = str(version)
        self.commission_rate = Decimal(str(commission_rate))
        self.tax_rate_sell = Decimal(str(tax_rate_sell))
        self.slippage_rate = Decimal(str(slippage_rate))
        self.source = source
        for name in ("commission_rate", "tax_rate_sell", "slippage_rate"):
            if getattr(self, name) < 0:
                raise ConfigError(f"{name}은 음수일 수 없다")

    @classmethod
    def from_config(cls, path: str = None):
        target = path or DEFAULT_COST_CONFIG
        if not os.path.exists(target):
            raise ConfigError(f"비용 설정 파일이 없다: {target}")
        with open(target, encoding="utf-8") as f:
            cfg = json.load(f)
        return cls(cfg["cost_model_version"], cfg["commission_rate"],
                   cfg["tax_rate_sell"], cfg["slippage_rate"], source=target)

    @classmethod
    def zero(cls):
        """비용 0 — gross와 net을 비교할 때 쓴다."""
        return cls("zero", 0, 0, 0)

    def net_return(self, entry_price, exit_price) -> float:
        """비용 반영 수익률."""
        entry = Decimal(str(entry_price))
        exit_ = Decimal(str(exit_price))
        if entry <= 0:
            raise ConfigError(f"진입가가 0 이하다: {entry_price}")
        buy = entry * (Decimal(1) + self.slippage_rate)
        sell = exit_ * (Decimal(1) - self.slippage_rate)
        gross_out = sell * (Decimal(1) - self.commission_rate - self.tax_rate_sell)
        gross_in = buy * (Decimal(1) + self.commission_rate)
        return float(gross_out / gross_in - Decimal(1))

    def to_dict(self):
        return {
            "cost_model_version": self.version,
            "commission_rate": float(self.commission_rate),
            "tax_rate_sell": float(self.tax_rate_sell),
            "slippage_rate": float(self.slippage_rate),
        }

    def __repr__(self):
        return (f"CostModel({self.version!r}, comm={self.commission_rate}, "
                f"tax={self.tax_rate_sell}, slip={self.slippage_rate})")

# -*- coding: utf-8 -*-
"""통계 추론 — 군집·중첩을 반영한 신뢰구간과 유효표본수."""

from .base_rates import (
    BOOLEAN_OUTCOMES, DEFAULT_OUTCOMES, BaseRateComputer, block_length_for,
)
from .inference import (
    Estimate, block_bootstrap_mean, cluster_robust_mean,
    icc_and_design_effect, naive_mean, normal_quantile,
)

from .dependence import (
    JointBootstrapResult, align_by_date, effective_independent_studies,
    joint_block_bootstrap,
)
from .economic_gate import (
    DEFAULT_THRESHOLDS, GateThresholds, annualize, evaluate_candidate,
)

__all__ = ["Estimate", "naive_mean", "icc_and_design_effect",
           "cluster_robust_mean", "block_bootstrap_mean", "normal_quantile",
           "BaseRateComputer", "BOOLEAN_OUTCOMES", "DEFAULT_OUTCOMES",
           "block_length_for",
           "joint_block_bootstrap", "JointBootstrapResult", "align_by_date",
           "effective_independent_studies",
           "evaluate_candidate", "GateThresholds", "DEFAULT_THRESHOLDS",
           "annualize"]

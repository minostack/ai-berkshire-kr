# -*- coding: utf-8 -*-
"""Ablation 열 계약 — 스키마 §8, 로드맵 Stage 8.

Stage 2·5·6·7이 전부 "효과를 주장할 수 없다"로 끝났다. 남은 질문은 둘이다.

    1. 성분을 빼도 결과가 같은가 — 같다면 그 성분은 아무 일도 하지 않았다
    2. 지금 표본으로 **얼마나 작은 효과까지** 잡을 수 있었나

첫째가 ablation, 둘째가 검정력이다. 둘을 같이 보고해야 "효과가 없다"와
"표본이 모자라 못 봤다"를 구별할 수 있다.

`NO_SIGNIFICANT_EDGE`
---------------------
로드맵 DoD가 이름으로 요구한 표시다. 기준선과 통계적으로 구분되지 않으면
그렇게 적는다 — "0에 가깝다"가 아니라 **"구분할 수 없다"**가 정확한 말이다.
검정력이 낮아 못 구별한 것인지는 `mde_80`이 같이 답한다.
"""

from __future__ import annotations

from .schema import Column

__all__ = [
    "ABLATION_RESULT_COLUMNS", "ABLATION_RESULT_PK",
    "EDGE_VERDICTS", "SELECTIONS",
]

#: 기준선 대비 판정.
EDGE_VERDICTS = ("SIGNIFICANT_EDGE", "NO_SIGNIFICANT_EDGE", "UNDERPOWERED",
                 "NOT_EVALUABLE")

#: 무엇을 골라서 재는가. 둘을 나눠 두는 이유는 로드맵이 요구한
#: "score와 absolute Gate의 분리 비교" 때문이다 —
#:   BUY   = Gate만 쓴다(점수 무시)
#:   TOP20 = 점수만 쓴다(Gate 무시 여부는 변형이 정한다)
SELECTIONS = ("BUY", "TOP20")

ABLATION_RESULT_PK = ("ablation_run_id", "variant_id", "metric_name",
                      "segment_key")

ABLATION_RESULT_COLUMNS = [
    Column("ablation_run_id", "string", nullable=False),
    Column("variant_id", "string", nullable=False),
    Column("metric_name", "string", nullable=False),
    Column("segment_key", "string", nullable=False,
           note="세그먼트가 없으면 'ALL'. null을 PK에 쓰지 않는다"),

    Column("base_experiment_id", "string", nullable=False,
           note="무엇을 기준으로 뺐는가 — 보통 BASE 변형"),
    Column("removed_components", "list<string>", nullable=False),
    Column("variant_spec_hash", "string", nullable=False),
    Column("selection", "string", nullable=False, enum=SELECTIONS),
    Column("horizon_td", "int", nullable=False),

    Column("paired_dates", "int", nullable=False,
           note="짝지어 비교한 거래일 수. 이게 유효표본의 상한이다"),
    Column("mean_selection_size", "float",
           note="하루 평균 몇 종목을 골랐는가. 거의 전부를 고르면 edge가 0에 "
                "가까워지고 구간은 좁아진다 — 크기를 모르면 오해한다"),
    Column("base_value", "float"),
    Column("variant_value", "float"),
    Column("delta", "float", note="variant − base, 같은 날짜끼리 짝지어"),
    Column("ci_low", "float"),
    Column("ci_high", "float"),
    Column("standard_error", "float"),
    Column("p_value", "float"),
    Column("adjusted_p_value", "float"),
    Column("adjustment_method", "string"),

    Column("inference_method", "string", nullable=False),
    Column("cluster_key", "string", nullable=False),
    Column("block_length", "int"),
    Column("bootstrap_replicates", "int"),
    Column("seed", "int", nullable=False),
    Column("ci_level", "float", nullable=False),
    Column("icc", "float"),
    Column("design_effect", "float"),
    Column("effective_n", "float"),

    # 검정력 — "못 찾았다"와 "없다"를 나누는 값.
    Column("mde_80", "float",
           note="유의수준 5%, 검정력 80%에서 잡을 수 있었던 최소 효과"),
    Column("observed_over_mde", "float",
           note="|delta| / mde_80. 1보다 작으면 애초에 못 잡을 크기였다"),
    Column("required_dates_80", "int",
           note="관측된 크기의 효과를 80% 검정력으로 잡으려면 필요한 거래일 수. "
                "'데이터를 더 모을 가치가 있는가'에 대한 답이다"),

    Column("edge_verdict", "string", enum=EDGE_VERDICTS,
           note="기준선 대비 판정. 로드맵 DoD가 요구한 표시"),
    Column("status", "string", nullable=False,
           enum=("SUCCESS", "PARTIAL", "FAILED")),
    Column("status_reason", "string"),

    Column("label_version", "string", nullable=False),
    Column("run_id", "string", nullable=False),
    Column("schema_version", "string", nullable=False),
    Column("data_revision", "int", nullable=False),
]

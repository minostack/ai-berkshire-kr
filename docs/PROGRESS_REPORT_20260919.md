# CVD/OBV 검증 시스템 V2 — 진행 보고와 검증 재료 위치

작성일: 2026-09-19
기준 문서: `STAGE_STATUS_V2.md` v2.6
목적: 지금까지 한 일 / 앞으로의 순서 / **검증할 파일·데이터·문서의 위치**

> 이 문서의 모든 경로와 수치는 2026-09-19 기준 **실측**이다. 추정값은 없다.
> 상세 기록은 `STAGE_STATUS_V2.md`에 있고, 이 문서는 그 요약과 색인이다.

> ## 2026-09-21 — 낡은 부분 안내
>
> 이 문서는 2026-09-19 상태의 기록으로 **그대로 둔다.** 그 뒤에 바뀐 것은
> 아래뿐이고, 최신 상태는 `STAGE_STATUS_V2.md` v2.7에 있다.
>
> | 이 문서가 적은 것 | 2026-09-21 이후 |
> |---|---|
> | `idx/kosdaq_dd_trd` ✗ 401, "사용자 조치 필요" | **구독 완료, 200** (`idx/krx_dd_trd`도) |
> | 코스닥150 재현 패널 암묵적으로 16년 | **2015-07-13~, 약 11.2년** |
> | ρ 구간 3단계 판정 | **결합 부트스트랩**으로 대체(`dependence measure`) |
> | 테스트 650개 / 41파일 | **705개 / 44파일** |
> | Stage 4 "완료" | **4A 완료 / 4B·4C 미착수** |
> | 문서 6종 | `PREREGISTRATION_KOSDAQ150.md`, `CLI_REFERENCE.md` 추가 |
>
> 새로 드러난 것 둘: 코스닥150 **섹터 가산성이 깨져 있다**(−1.2%), 그리고
> `data/index_stocks.json`의 코스닥150 명단이 **−88.84%로 틀려 있었다**
> (Stage 1~8은 이 파일을 쓰지 않아 영향 없음).

---

# 1. 지금까지 한 일

## 1.1 전체 그림

V1.5 스캐너(CVD/OBV 기반)가 실제로 예측력이 있는지를 **검증 가능한 형태로**
다시 지은 작업이다. Stage 0~8 완료, Stage 9~10 보류.

| Stage | 내용 | 결과 |
|---|---|---|
| 0 | 기준선 동결·골든 샘플 | 완료 — V1.5 계산 코어 동결, 회귀 테스트가 지킴 |
| 1 | 일봉 이력·기준선 | 완료 |
| 2 | Daily Gate 백테스트 | 완료 — **2026-09-19 중첩 보정으로 결론 뒤집힘** |
| 3 | 유니버스 이력 | 완료 — 2010-01-04~2026-09-18, 검산 4,114일 전부 0원 |
| 4 | 분봉 캐시 | 완료 — 257거래일 |
| 5 | 과거 스캐너 replay | 완료 — 252거래일 / 신호 50,420건 |
| 6 | Forward Return Collector | 완료 — label 252,100행 / **전부 ns** |
| 7 | 백테스트 엔진 | 완료 — 실험 20건 / **전부 ns** |
| 8 | 통계 검정·Ablation | 완료 — 관측 효과가 MDE의 **0.11~0.53배** |
| C | 사전 등록 후 가설 재정의 | 완료 — **FDR 통과 후보 1건** |
| 9 | Probability Model | **보류** — 예측할 것이 있다는 증거가 없다 |
| 10 | 운영 전환 | 보류 |

## 1.2 결론이 실제로 바뀐 3가지

### ① 보유기간 중첩을 빠뜨려 Stage 2가 틀려 있었다

h일 보유면 인접한 날의 신호가 h−1일의 가격 경로를 공유한다. 날짜를 독립으로
세면 구간이 약 √h만큼 좁아진다. 블록 부트스트랩(블록 길이 ≥ 보유기간)을
군집-강건(CR1) 구간과 나란히 돌리고 **둘 중 넓은 쪽**을 쓰도록 고친 뒤 전면
재실행했다.

| | 보정 전 | 보정 후 |
|---|---|---|
| h=20 GATE_ALL | +0.086%p [+0.040, +0.132] **유의** | [−0.022, +0.197] **ns** |
| 유의한 연도 | 17년 중 5년 | 17년 중 **1년** |
| FDR 생존 | 일부 | **0건** |

문서에 정정 배너를 붙였다. h=1 기록도 삭제하지 않았다 — 그게 h=1을 접은
근거이기 때문이다.

### ② "없다"가 아니라 "잴 수 없다"였다

MDE(최소검출가능효과)를 도입해 두 상태를 구분했다.

- `UNDERPOWERED` — 그 크기의 효과라면 애초에 측정할 수 없었다
- `NO_SIGNIFICANT_EDGE` — 잴 수 있었는데 없었다

대부분은 전자였다. h=1은 관측 크기의 효과를 잡으려면 **45~80년치**가 필요해
분석 격자에서 뺐다(`RESEARCH_HORIZONS` = 3/5/10/20). **label 생성은 1/3/5/10/20
그대로 유지한다** — 접는 것은 보고이지 데이터가 아니다.

### ③ 비교 방식을 바꾸자 처음으로 FDR을 통과했다

Stage 2는 모든 조합을 전체(ALL)와만 비교했다. 그런데 "OBV가 **가격 조건 위에**
무언가를 더하는가"는 그렇게 물을 수 없다. PRICE를 기준으로 PRICE+OBV를 봐야
한다. 표본을 대부분 공유하므로 짝지음 차이의 분산이 훨씬 작다.

| 대비 | h | delta | 95% 구간 | q(BH) |
|---|---:|---:|---|---:|
| PRICE+OBV vs PRICE | 5 | +0.0234%p | [+0.0051, +0.0432] | **0.024** |
| PRICE+OBV vs PRICE | 20 | +0.0802%p | [+0.0248, +0.1408] | **0.020** |

이 프로젝트에서 다중비교 보정을 통과한 첫 결과다.

**단, 발견이 아니라 후보다.** 사전 등록 판정 규칙 4번에 미리 적어 둔 대로다.

- 같은 데이터를 여러 번 봤다 — 중첩 대비 자체는 처음이지만 자료는 처음이 아니다
- 검출 한계에 붙어 있다 — 관측/MDE가 0.86(h=5), 0.97(h=20)
- 적중률은 움직이지 않았다 — h=20 확률 차이 +0.195%p [−0.001, +0.404] ns
- 경제적으로 작다 — 20거래일 보유에 +0.080%p, 2배 비용이면 절대 수익이 사라진다

### 덤 — 유동성 조건은 정확히 아무 일도 하지 않는다

추정이 아니라 **집합이 같다.** PRICE+OBV와 GATE_ALL은 선택률 35.5%, raw_n
275,121로 글자 그대로 같은 행을 고른다. 서로 다른 데이터(일봉 경로와 분봉
경로)에서 같은 결론이 나왔다. 설계에서 뺄 수 있다 — 성능이 좋아져서가 아니라
**아무 일도 하지 않아서**다.

## 1.3 잡은 결함 — 전부 결론을 바꿨거나 바꿀 수 있었던 것

| 결함 | 영향 |
|---|---|
| 짝지음 차이 구간이 보유기간 중첩을 무시 | 위 ① — Stage 2 결론 전복 |
| Stage 7 현금 look-ahead | 당일 매도대금을 같은 날 시가에 사용. 순서를 진입→청산→평가로 |
| 하루 진입 상한 없음 | 첫날 전 슬롯 충전 후 통째 회전. `max_entries_per_day="AUTO"` 도입 |
| tie-break에 유동성 누락 | 248일 중 **138일**이 동점. 고치니 SCORE_Q5 수익 +45.7% → +37.8% |
| 점수 재구성 21% 불일치 | 백분위 모집단을 family별 `data_status==SUCCESS`로 안 걸렀음. 수정 후 **0/50,420** |
| TOP 선택이 게이트를 무시 | BASE와 NO_GATE가 바이트 동일한 TOP20을 냄 |
| `part_key` 쓰기 충돌 | `backtest_runs`가 20행이어야 하는데 5행 |
| `apply_fdr` CI 키 불일치 | 실제 컬럼명이 `delta_return_ci_low`. 예외 맵으로 처리 |
| FDR 검정 수 부풀림 | 결과가 같은 대비가 중복 계수됨. `dedupe_by` 추가 |

---

# 2. 앞으로의 순서

## 2.1 왜 "기다리기"는 선택지가 아닌가

후보를 확증하려면 **발견에 쓰지 않은 새 표본**이 필요하다. 계산해 보면:

| 후보 | 효과 | SE | 발견 표본 | 양측 필요 | 단측 필요 |
|---|---:|---:|---:|---:|---:|
| PRICE+OBV vs PRICE, h=5 | +0.0234%p | 0.0097%p | 4,110일 | 5,544일 (22.6년) | **17.8년** |
| PRICE+OBV vs PRICE, h=20 | +0.0802%p | 0.0296%p | 4,094일 | 4,381일 (17.9년) | **14.1년** |

누적이 아니라 **추가분**이다. 단측(14.1년)은 **방향을 미리 걸었을 때만** 쓸 수
있고, 결과가 반대로 나오면 그것으로 끝이다 — 사후에 양측으로 바꿔 읽으면 안
된다.

그래서 시간이 아니라 **다른 단면**을 산다.

## 2.2 우선순위

| 순위 | 항목 | 비용 | 상태 |
|---:|---|---|---|
| **1** | 분봉 매일 쌓기 자동화 | 거의 없음 | **미착수 — 매일 잃는 중** |
| **2** | `idx/kosdaq_dd_trd` 구독 신청 | 무료·수 분 | **사용자 조치 필요** |
| 3 | 코스닥150 유니버스·일봉 구축 | 수 시간~하루 | 2번 대기 |
| 4 | 두 유니버스 delta 상관 ρ 측정 | 분 단위 | 3번 대기 |
| 5 | 과거 분봉 구매 경로 조사 | 문의 | — |

1·2번은 오늘 할 수 있고 되돌릴 것이 없다. 나머지는 2번 결과를 보고 정한다.

### 1순위 — 분봉 매일 쌓기 자동화 (가장 급함)

키움 `ka10080`에는 날짜 파라미터가 없다. API가 주는 구간이 전부이고 그 구간은
굴러간다 — **오늘 받아 두지 않은 날은 영원히 복구할 수 없다.**

현재 257거래일 확보. `minute-cache build` 명령은 있지만 자동 실행이 없다.

### 2순위 — KRX Open API 구독 1건 (사용자 조치, 무료, 수 분)

실측한 현재 구독 상태:

| 서비스 | 결과 |
|---|---|
| `idx/kospi_dd_trd` | ✓ |
| `sto/stk_bydd_trd` (코스피 종목별) | ✓ 943종목 |
| `sto/ksq_bydd_trd` (코스닥 종목별) | ✓ **1,822종목** |
| `idx/kosdaq_dd_trd` (코스닥 지수 — 검산에 필요) | ✗ **401** |
| `idx/krx_dd_trd` / `idx/bon_dd_trd` / `idx/drvprod_dd_trd` / `sto/knx_bydd_trd` | ✗ 401 |

**전부 401이지 404가 아니다.** 서비스는 존재하고 이 키에 구독만 걸려 있지 않다.
같은 화면에서 `idx/krx_dd_trd`도 받아 두면 KRX300까지 열린다.

### 3순위 — 코스닥150 구축

Stage 3 복원 기계가 이미 코스닥150을 안다 — 크기 범위, 변경 폭 상한, 첨부 시트
인식, 스냅샷 파라미터가 들어 있다. 8단계 절차는 코스피200에서 실측한 시간
그대로이고, 일봉 수집(수 시간)이 가장 길다.

### 4순위 — ρ를 먼저 잰다 (순서를 뒤집으면 안 됨)

같은 기간이라 두 유니버스의 delta 계열은 시장 요인을 공유한다.

| ρ | 판정 |
|---|---|
| < 0.3 | 재현 표본으로 사용 |
| 0.3~0.7 | 할인해서 사용 |
| > 0.7 | **사용하지 않음** — 같은 것을 두 번 보는 것 |

**ρ를 재기 전에 "표본이 두 배"라고 쓰지 않는 것**이 이 계획에서 가장 틀리기
쉬운 지점이다.

### 5순위 — 과거 분봉 구매 경로

가격과 제공 범위를 지어내지 않고 **확인 대상 목록으로만** 적었다. 문의 시
반드시 물을 4가지: 5분봉 제공 여부 / 정규장·애프터마켓 구분 / 분할조정 기준 /
상장폐지 종목 포함 여부.

## 2.3 하지 않을 것

- **Stage 9 (Probability Model)** — 예측할 것이 있다는 증거 위에서만 의미가
  있다. 지금은 그 증거가 없다. 없다고 밝혀진 게 아니라 **확인할 수 없는**
  상태다. 확률 모델을 얹으면 잡음에 곡선을 맞추게 된다.
- **Stage 10 (운영 전환)** — 더 멀다.
- **유의한 칸을 찾은 뒤 연도·국면으로 쪼개기** — 쪼개면 반드시 "어떤 해에 몰려
  있다"가 나오고, 그건 검정이 아니라 사후 이야기다. 사전 등록에서 금지했고
  지켰다.
- **h=1 되살리기** — 어떤 현실적 데이터 양으로도 판정할 수 없다.

---

# 3. 검증 재료 위치

## 3.1 문서 — 먼저 볼 순서

| 파일 | 크기 | 무엇 |
|---|---:|---|
| `docs/STAGE_STATUS_V2.md` | 2,076줄 | **여기부터.** v2.6. 전체 진행 + 결정 기록 + 단계별 상세 |
| `docs/DATA_COLLECTION_PLAN.md` | 210줄 | 확증 수집 계획 |
| `docs/PREREGISTRATION_C.md` | 6KB | 사전 등록. **선택지 C 결과보다 먼저 읽어야 의미가 있다** (sha256 `8fd07b2548fad5ae1e4bc91c7cc075724513691c47656980bc7b2b8984d3973a`) |
| `docs/SYSTEM_ARCHITECTURE_V2.md` | 20KB | 설계 (§6에 tie-break 규칙) |
| `docs/DATA_SCHEMA_V2.md` | 23KB | 테이블 계약 |
| `docs/DEVELOPMENT_ROADMAP_V2.md` | 16KB | 로드맵 |

**`STAGE_STATUS_V2.md` 내부 바로가기**

| 줄 | 절 |
|---:|---|
| 29 | ▶ 다음 작업 |
| 98 | 결정 기록 |
| 113 | 현재 데이터 상태 |
| 138 | 선택지 C — 사전 등록 후 실행 |
| 284 | Stage 8 — 없는 게 아니라 잴 수 없었다 |
| 555 | Stage 7 |
| 758 | Stage 6 |
| 942 / 1013 / 1071 | Stage 5 / 4 / 3 |
| 1303 / 1360 / 1585 | Stage 2 / 1 / 0 |
| 1653 | 사용법 (재현 명령 모음) |
| **1797** | **Stage 2 마무리 — 2026-09-19 수정** ← 정정 배너 |

## 3.2 결과 JSON — 수치의 원본

각 JSON에는 사전 등록 sha256과 실행 파라미터가 박혀 있다.

| 경로 | 내용 |
|---|---|
| `reports/prereg-c/h1-nested-price.json` | **후보의 근거.** PRICE 기준 중첩 대비 |
| `reports/prereg-c/h2-nested-liquidity.json` | 유동성 delta = 정확히 0 |
| `reports/prereg-c/h3-frontier.json` | 검출 가능 구간(MDE 격자) |
| `reports/prereg-c/confirmation-plan.json` | 14~18년 계산 결과 |
| `reports/daily-gate/stage2-overlap-corrected.json` | **중첩 보정 후** Stage 2 본체 |
| `reports/daily-gate/stage2-{year,regime,liquidity}-overlap.json` | 보정 후 세그먼트 3종 |
| `reports/daily-gate/gate-final-20260918.json` 외 | 보정 **전** 기록(대조용, 보존) |
| `reports/signal-gate/stage6-{overall,month,regime}.json` | Stage 6 |
| `reports/backtest/stage7-{h5,h20-wide,h20-concentrated,walkforward}.json` | Stage 7 |
| `reports/ablation/stage8-h{1,5,20}.json` | Stage 8 |
| `reports/baserate/kospi200-1999-2026.json` | 무조건부 기준선 |
| `reports/replay/kospi200-15h35-20260918.json` | Stage 5 replay |

## 3.3 데이터 레이크 — 총 1,423.8MB

| 계층 | 파일 수 | 크기 | 내용 |
|---|---:|---:|---|
| `data/bronze` | 8,473 | 259.5MB | `krx_openapi` 8,228응답, `krx_notices` 첨부 |
| `data/silver` | 8,250 | 196.9MB | `daily_bars`, `minute_bars`, `universe_membership` 등 7테이블 |
| `data/gold` | 7,908 | 967.4MB | `signals`, `forward_returns`, `backtest_*`, `ablation_results` 등 10테이블 |

| 테이블 | 규모 | 비고 |
|---|---:|---|
| `silver.daily_bars` | 1,928,783행 / 387종목 | 키움 348 + KRX 패널 39(상장폐지) |
| `silver.minute_bars` | 3,999,286봉 / 201종목 / **257거래일** | 2025-09-01~ |
| `silver.universe_membership` | 433행 / 84구간 | 검산 4,114일 전부 0원 |
| `silver.security_master` | 1,723행 / 1,251종목 | 구간 내 상장폐지 309종목 |
| `gold.daily_features` | 1,928,782행 | |
| `gold.daily_forward_returns` | 9,643,910행 | horizon 1·3·5·10·20 |
| `gold.signals` | 50,420행 | Stage 5 replay, 252거래일 |
| `gold.forward_returns` | 252,100행 | Stage 6 신호별 label |
| `gold.backtest_runs / trades / portfolio_nav / backtest_metrics` | 20 / 112,303 / 5,040 / 432행 | Stage 7 실험 20건 |
| `gold.ablation_results` | 108행 | h(1·5·20) x 변형 9 x 지표 4 |

## 3.4 코드 — 검증 시 중요도 순

| 파일 | 왜 중요한가 |
|---|---|
| `src/cvd_obv/statistics/score_model.py` | **Ablation 전체의 전제.** V1.5 점수를 저장된 feature에서 재구성하고 불일치 시 hard-fail. 현재 **0/50,420** |
| `src/cvd_obv/statistics/power.py` | MDE / 필요 표본 / 판정(`verdict_for`) |
| `src/cvd_obv/statistics/collection_plan.py` | 확증 표본 계산 (신규) |
| `src/cvd_obv/statistics/detectability.py` | 검출 가능 구간 (신규) |
| `src/cvd_obv/statistics/ablation.py` | 성분 제거 9변형 |
| `src/cvd_obv/statistics/inference.py` | 구간·FDR 기반 |
| `src/cvd_obv/backtest/daily_gate.py` | `--base-variant` 중첩 대비 |
| `src/cvd_obv/backtest/signal_gate.py` | 중첩 보정 CI, 순위 IC |
| `src/cvd_obv/backtest/engine.py` | 진입→청산→평가 순서, tie-break, 일일 진입 상한 |
| `src/cvd_obv/backtest/common.py` | `apply_fdr`, turnover, 세그먼트 |
| `src/cvd_obv/contracts/backtest_schema.py` | `assert_ledger_reconciles`, `assert_execution_after_signal` |
| `src/cvd_obv/contracts/label_schema.py` | `RESEARCH_HORIZONS`, `RETIRED_HORIZONS` |
| `src/cvd_obv/labels/signal_labels.py` | 재계산이 아닌 **조인** + 캘린더 검증 |

전체 모듈 70개: `src/cvd_obv/` 하위 `backtest`, `baseline`, `cli`, `contracts`,
`domain`, `features`, `ingestion`, `labels`, `replay`, `statistics`, `storage`.

## 3.5 테스트 — 650개 / 41파일 (2026-09-19 수집 확인)

```bash
cd C:\a-ai\ai-berkshire-kr && set PYTHONIOENCODING=utf-8 && python -m pytest -q
```

이번 작업에서 추가된 테스트 파일:

| 파일 | 대상 |
|---|---|
| `tests/unit/test_signal_labels.py` | Stage 6 label |
| `tests/unit/test_signal_gate.py` | Stage 6 평가 |
| `tests/unit/test_backtest_engine.py` | Stage 7 엔진·원장 |
| `tests/unit/test_score_model.py` | 점수 재구성 |
| `tests/unit/test_ablation.py` | Stage 8 |
| `tests/unit/test_detectability.py` | MDE 격자 |
| `tests/unit/test_collection_plan.py` | 확증 표본 (8개) |
| `tests/integration/test_stage6_labels_real.py` | 실제 데이터 통합 |

기존 `test_daily_gate.py`, `test_cli_exit_codes.py`에도 추가했다.

## 3.6 재현 명령

```bash
# 선택지 C — H1 중첩 대비 (후보의 근거)
python cvdobv.py daily-gate backtest --base-variant PRICE \
  --variants ALL,PRICE,PRICE+OBV,PRICE+LIQ,GATE_ALL \
  --horizons 5,20 --outcomes up --out reports/prereg-c/h1-nested-price.json

# H2 — 유동성 성분
python cvdobv.py daily-gate backtest --base-variant PRICE+OBV \
  --variants PRICE+OBV,GATE_ALL --horizons 5,20 --outcomes up \
  --out reports/prereg-c/h2-nested-liquidity.json

# H3 — 검출 가능 구간
python cvdobv.py power frontier --out reports/prereg-c/h3-frontier.json

# 확증 표본 계산
python cvdobv.py power plan --out reports/prereg-c/confirmation-plan.json
```

CLI 하위 명령 16개: `baseline` `daily-cache` `minute-cache` `replay` `universe`
`features` `daily-gate` `signal-label` `signal-gate` `backtest` `power`
`ablation` `benchmark` `calendar` `forward` `baserate` `contract`

## 3.7 형상 관리 상태

`src/`, `tests/`, `data/`, `pyproject.toml`, `cvdobv.py`, `configs/`와 `docs/`·
`reports/`의 신규 파일은 **git 미추적 상태**다(최근 커밋 `3083100 cvd-obv-scan
고도화`). 검증할 때 diff가 아니라 **파일 자체**를 봐야 한다.

---

## 부록 — 한 문장 요약

8단계를 다 지었고 결론은 **"효과가 없다"가 아니라 "이 데이터로는 판정할 수
없다"**다. 비교 방식을 중첩으로 바꿔 후보 하나를 찾았지만 확증에는 14~18년이
필요하므로, 기다리는 대신 코스닥150이라는 **다른 단면**을 사되 ρ를 먼저 잰다.
그리고 **분봉은 오늘 안 받으면 오늘치가 영원히 사라진다.**

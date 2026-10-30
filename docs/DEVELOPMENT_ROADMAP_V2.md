# CVD/OBV 검증 시스템 V2 개발 로드맵

문서 버전: 2.1  
기준일: 2026-09-16 (2026-09-19 개정 — h=1 보유기간 접음)  
기준 구현: `kiwoom_cvd_obv_scanner.py` V1.5  
기본 신호 시각: 15:35 KST  
기본 진입 규칙: 다음 거래일 시가(`next_open`)

## 1. 목적

V1.5 스캐너의 `BUY_CANDIDATE`, 점수, 순위가 미래 수익률을 실제로 개선하는지 재현 가능하게 검증하고, 충분한 증거가 있을 때만 보정된 상승확률을 제공한다. 현재 점수는 유니버스 안의 상대 강도이고 Gate 임계값과 가중치는 검증 전 값이므로 확률로 해석하지 않는다.

본 로드맵의 핵심 순서는 다음과 같다.

```text
계약·버전 고정
  → 일봉 장기 기준선 및 Daily Gate 검증(연구 패널 2010~2026, 약 16년)
  → 시점별 Universe 복원
  → 분봉 약 255일 Cache 구축
  → Historical Scanner Replay
  → Forward Return 및 거래 시뮬레이션
  → 통계 검정·Ablation
  → Probability Model 및 Calibration
  → 제한적 운영
```

분봉은 `ka10080`에서 과거 기준일을 직접 지정할 수 없고 최신 시점부터 약 255거래일만 얻을 수 있다.

> **정정 (2026-09-21).** 이 문단은 원래 "일봉 `ka10081`은 약 11년(실측 2,684거래일)"이라고
> 적었다. 둘 다 틀렸다.
>
> - **봉 깊이는 11년이 아니라 상장일까지다.** 삼성전자 1985-01-04, 10,931행(Stage 1 실측).
>   2,684거래일은 처음 시험한 한 종목의 값이었을 뿐이다.
> - **연구 패널을 묶는 것은 봉이 아니라 유니버스다.** 2010-01-04 이전은 그날의 구성종목을
>   모른다(KRX Open API 시작일). 그래서 실제 연구 구간은 **2010~2026년 약 16년**,
>   4,114거래일이다.
>
> 두 수치를 섞어 쓰지 말 것 — **API 깊이**(상장일까지)와 **연구 패널**(16년)은 다른 것이다.

긴 일봉 검증과 짧은 통합 검증은 별도 증거 계층으로 유지한다.

## 2. 목표와 비범위

### 목표

- V1.5 계산식과 Gate를 재사용 가능한 순수 계산 코어로 분리한다.
- 모든 결과를 Parquet 기반 데이터 레이어에 기록하고 원천 데이터에서 재현한다.
- 15:35에 알 수 있었던 정보만 사용해 신호를 만들고 다음 거래일 시가에 진입한다.
- 시점별 구성종목으로 생존편향을 차단한다.
- 시장 기준선, 절대·초과수익, 비용 반영 수익을 함께 평가한다.
- 날짜 군집과 보유기간 중첩을 고려한 유효표본 수와 신뢰구간(confidence interval)을 보고한다.
- 각 Gate와 점수 성분의 추가 가치를 ablation test로 검증한다.
- 모델 확률은 시간 외 표본 검증과 calibration 기준을 통과한 경우에만 노출한다.

### 비범위

- 자동 주문, 포지션 운용, 실시간 주문 라우팅
- 255거래일 이전 분봉을 합성하거나 일봉에서 CVD를 역추정하는 작업
- V1.5의 Estimated CVD를 실제 체결 방향 기반 True CVD로 표현하는 작업
- 검증 전에 점수 가중치나 Gate 임계값을 최적화하는 작업
- 하나의 점추정 확률만으로 투자 판단을 제공하는 작업

## 3. 고정 연구 규약

| 항목 | V2 기본값 | 변경 규칙 |
|---|---|---|
| 시간대 | `Asia/Seoul` | 저장은 UTC timestamp와 KST date를 병기 |
| 신호 시각 | 15:35 KST | 실험 ID에 포함; 다른 시각과 표본 혼합 금지 |
| Feature cutoff | `bar_end <= signal_at` | 위반 행은 `LOOKAHEAD_DETECTED` |
| 진입 | 다음 거래일 시가 | 가격 부재·거래정지는 별도 상태 |
| 청산 | label은 1/3/5/10/20 거래일 종가, **분석은 3/5/10/20** | 거래 캘린더로 계산. h=1은 2026-09-19에 접었다(아래) |
| 기본 세션 | 정규장 | 애프터마켓은 별도 feature/regime |
| 유니버스 | 해당 날짜에 유효한 구성종목 | 현재 구성종목의 과거 소급 사용 금지 |
| 비용 | 수수료·세금·슬리피지 구성값 | gross와 net 동시 저장 |
| 벤치마크 | 종목 소속 지수 | 버전이 지정된 지수 가격 사용 |
| 실험 분할 | 시간순 walk-forward | 무작위 K-fold 금지 |

### h=1을 접은 이유 (2026-09-19 결정)

Stage 8 검정력 계산에서 h=1은 **관측된 크기의 효과를 80% 검정력으로 잡으려면
11,110~19,560거래일(45~80년)**이 필요하다고 나왔다. 현실적으로 모을 수 있는
어떤 양으로도 판정할 수 없다는 뜻이다. 계속 격자에 올리면 다중비교 예산만 쓰고
결론은 늘 "판정 불가"가 된다.

접는 것은 **보고와 검정**이지 데이터가 아니다.

| | h=1 |
|---|---|
| `gold.daily_forward_returns` / `gold.forward_returns` label | **계속 만든다**(`DEFAULT_HORIZONS`) |
| 분석·보고 기본 격자 | **뺀다**(`RESEARCH_HORIZONS`) |
| 명시적으로 지정하면 | 계산은 하되 이유를 경고로 찍는다 |

Stage 5~8 리포트에 남아 있는 h=1 수치는 **이 결정의 근거**이므로 지우지
않는다. 그 이후의 격자에는 넣지 않는다.

15:35 신호에서 당일 최종 일봉이 20:00까지 변할 수 있는 시장 제도라면, `daily_scope=regular`인 15:30 확정 데이터 또는 전 거래일까지 완료된 일봉만 사용한다. 입력 가격 정의는 `market_rule_version`과 별도로 관리한다. 15:35 이후 확정되는 extended-session 일봉을 같은 날 feature에 사용하지 않는다.

## 4. 우선순위

- **P0:** 재현성, 편향 방지, 데이터 계약, 일봉 기준선, 유니버스 이력, cache 무결성
- **P1:** 통합 replay, forward return, 비용·포트폴리오 백테스트, 통계 검정과 ablation
- **P2:** 확률 모델, calibration, 모델 레지스트리와 운영 표시

## 5. 단계별 개발 순서와 완료 기준

### Stage 0 — 기준선 동결과 골든 샘플 (P0)

작업:

1. V1.5 파일의 SHA-256과 `_SCANNER_VERSION=1.5`, `_MARKET_RULE_VERSION=KRX-2026-09-14-aftermarket`을 manifest에 기록한다.
2. `analyze`, `calc_cvd`, `calc_obv`, `calc_price_trend`, `calc_liquidity`, `_finalize_scores`, `_compute_gate`의 입출력을 골든 샘플로 저장한다.
3. `signal_id`, `run_id`, `config_hash` 생성 규칙과 canonical JSON 직렬화를 고정한다.
4. 공통 상태 코드와 exit code를 구현한다.

DoD:

- 동일 입력·동일 버전·동일 설정의 replay가 byte-equivalent 핵심 feature와 같은 `signal_id`를 만든다.
- V1.5 live JSON을 새 `signals` 스키마로 변환하는 contract test가 통과한다.
- 비밀키나 API 토큰이 manifest·로그·Parquet에 없다.

### Stage 1 — Daily History와 Benchmark/Base Rate (P0)

작업:

1. 일봉과 지수 일봉을 가능한 전체 기간(상장일까지) 수집한다. 연구에 쓰는 구간은 유니버스 이력이 있는 2010~2026년이다.
2. 거래 캘린더, corporate action 조정 여부, 가격 scope를 기록한다.
3. 날짜·유니버스·보유기간별 무조건부 상승확률과 벤치마크 수익률을 계산한다.
4. `next_open` 진입 기준 1/3/5/10/20일 forward return을 만든다.

DoD:

- 종목별 날짜가 정렬·중복 제거되고 OHLCV 불변식이 통과한다.
- base rate가 재실행 시 동일하며 표본수, 날짜수, n_eff, 95% CI를 포함한다.
- 벤치마크가 누락되면 초과수익을 0으로 대체하지 않고 nullable과 상태로 남긴다.

### Stage 2 — Daily Gate Backtester (P0)

대상 조건은 `close > MA20`, `OBV normalized_slope > 0 AND OBV > OBV_MA20`, 유동성 존재이다. CVD 조건 없이 2010~2026년(약 16년, 4,114거래일)을 검증해 일봉 성분 자체의 edge를 측정한다.

필수 비교군:

- 전체 유니버스 base rate
- 조건 하나씩 단독 적용
- 3개 조건의 조합
- score/rank를 제외한 절대 Gate
- 시장 regime, 지수, 연도, 유동성 구간별 결과

DoD:

- 각 결과에 raw N, 거래일 수, n_eff, cluster-robust 또는 block-bootstrap CI가 있다.
- gross/net return, benchmark excess return, turnover와 coverage가 함께 출력된다.
- 사전 등록된 조합과 탐색적 조합이 구분된다.

### Stage 3 — Universe History (P0)

작업:

- 코스피200·코스닥150의 `effective_from <= as_of < effective_to` 이력을 구축한다.
- 편입·편출 공시의 출처, 수집시각, revision을 저장한다.
- 상장폐지, 코드 변경, 합병을 security master와 연결한다.

DoD:

- 임의 날짜의 구성종목을 결정론적으로 조회할 수 있다.
- 한 지수·종목의 유효구간이 겹치지 않는다.
- 알려진 정기변경일 전후 회귀 테스트가 통과한다.
- 이력이 불완전한 날짜는 replay하지 않고 `UNIVERSE_INCOMPLETE`로 차단한다.

### Stage 4 — Minute History Cache (P0)

작업:

- `ka10080` 전체 가용 5분봉을 종목별 1회 수집하고 이후 증분 갱신한다.
- 원본 수신 값과 정규화 bar를 분리 저장한다.
- 정규장/애프터마켓/기타 세션, bar start/end, 수집 run을 기록한다.
- offline `slice_as_of()`가 `bar_end <= 15:35`만 반환하도록 한다.

DoD:

- 동일 키 중복이 없고, cache update가 idempotent하다.
- 삼성전자 기준 가용 깊이가 API 실측 범위와 설명 가능하게 일치한다.
- 누락일, 세션별 기대 bar 수, stale bar, partial page를 품질 테이블에 기록한다.
- API 재호출 없이 모든 가용 과거 as-of slice를 만든다.

### Stage 5 — Historical Scanner Replay (P1)

작업:

- V1.5 계산 코어에 API 대신 cache-backed repository를 주입한다.
- 각 거래일 15:35에 당시 유니버스 전체를 한 실행 단위로 계산한다.
- 백분위 점수는 그 시점의 성공 상태 종목만 모집단으로 사용한다.
- live와 replay를 동일한 `SignalRecord`로 출력한다.

DoD:

- 15:35 이후 bar를 인위적으로 넣어도 결과가 변하지 않는 metamorphic test가 통과한다.
- 과거 replay가 현재 유니버스 파일을 읽지 않는다.
- replay coverage와 제외 사유가 종목·날짜별로 집계된다.
- 동일 `run_id` 재실행은 overwrite하지 않고 동일 hash 결과를 확인한다.

### Stage 6 — Forward Return Collector (P1)

작업:

- live/replay signal에 `next_open` 진입가와 1/3/5/10/20일 청산가를 연결한다.
- MFE, MAE, 목표수익/손실 barrier label, benchmark 및 excess return을 계산한다.
- 미래 horizon이 아직 끝나지 않은 live signal은 `PENDING_MATURITY`로 둔다.

DoD:

- entry session과 exit session이 거래 캘린더로 검증된다.
- 거래정지·가격제한·결측·상장폐지 처리 정책이 테스트된다.
- 같은 signal/horizon/label_version 조합은 유일하다.

### Stage 7 — Backtest Engine (P1)

필수 전략:

- `BUY_CANDIDATE` 전체와 Top 3/5/10/20
- score bucket과 rank percentile
- 3/5/10/20일 보유 (h=1은 2026-09-19에 접었다 — §3)
- 중복 포지션 허용/비허용 정책
- 동일가중, 유동성 제한, 최대 종목 수
- 수수료·세금·슬리피지 전후

DoD:

- signal 생성과 체결이 분리되고 `entry_at > signal_at`이 강제된다.
- 거래 ledger 합계가 portfolio NAV 변화와 일치한다.
- base rate 대비 hit-rate edge와 benchmark excess return을 보고한다.
- walk-forward 기간 밖의 데이터를 튜닝에 사용하지 않는다.

### Stage 8 — 통계 검정과 Ablation (P1)

필수 분석:

- 날짜 cluster-robust standard error
- horizon 이상 길이의 moving/stationary block bootstrap
- 날짜 내 상관을 반영한 design effect와 n_eff
- Gate 전체 제거, CVD 제거, OBV 제거, 가격 제거, 유동성 제거
- 개별 CVD delta/momentum/bull-ratio 제거
- score와 absolute Gate의 분리 비교
- multiple testing 보정과 탐색 결과 라벨링

DoD:

- CI 방식, cluster key, block length, seed, 반복 횟수가 artifact에 남는다.
- ablation별 동일 날짜·동일 유니버스·동일 비용 비교가 보장된다.
- 결과가 base rate와 통계적으로 구분되지 않으면 `NO_SIGNIFICANT_EDGE`로 표시한다.

### Stage 9 — Probability Model (P2)

순서:

1. empirical bucket estimator
2. regularized logistic regression
3. 필요할 때만 tree boosting 비교
4. isotonic 또는 Platt calibration

시간순 train/calibration/test를 분리하고, feature는 signal 시점의 값만 허용한다. 출력은 `P(up)`, base rate, excess probability, 95% CI, n_eff, calibration status, OOS status를 포함한다.

노출 게이트 기본안:

```text
effective_n >= 사전 지정 최소값
95% CI가 base rate와 구분됨
OOS Brier score가 base-rate predictor보다 개선
calibration slope/intercept와 ECE 기준 통과
데이터·모델 버전이 재현 가능
```

DoD:

- 모델 카드, feature schema hash, train window, calibration window, test window가 저장된다.
- `INSUFFICIENT_EVIDENCE`일 때 확률 숫자를 사용자 화면에 표시하지 않는다.
- champion이 baseline을 이기지 못하면 배포하지 않는다.

### Stage 10 — 운영 전환 (P2)

DoD:

- 매일 15:35 signal 생성과 다음 날 성과 성숙 작업이 재실행 가능하다.
- 데이터 지연, universe 누락, schema drift, model drift 경보가 있다.
- 운영 결과를 원본 run과 model run으로 추적할 수 있다.
- rollback 대상은 코드, config, market rule, schema, model 각각 독립 버전이다.

## 6. 권장 디렉터리 구조

```text
project/
├─ pyproject.toml
├─ configs/
│  ├─ scanner/v1_5.yaml
│  ├─ costs/krx_v1.yaml
│  ├─ market_rules/KRX-2026-09-14-aftermarket.yaml
│  └─ experiments/*.yaml
├─ src/cvd_obv/
│  ├─ domain/                 # 타입, ID, 상태, 거래 캘린더
│  ├─ scanner_core/           # V1.5 순수 계산 로직
│  ├─ ingestion/              # daily/minute/universe/benchmark
│  ├─ replay/
│  ├─ labels/
│  ├─ backtest/
│  ├─ statistics/
│  ├─ modeling/
│  └─ cli/
├─ data/
│  ├─ bronze/                 # API 원본에 가까운 append-only 데이터
│  ├─ silver/                 # 정규화 daily/minute/universe
│  ├─ gold/                   # signals, labels, backtests, model datasets
│  └─ manifests/
├─ models/
├─ reports/
└─ tests/{unit,contract,integration,replay,statistical}/
```

## 7. 공통 CLI 계획

```bash
cvdobv daily-cache build --start 2015-10-01 --end 2026-09-16
cvdobv universe import --index kospi200 --source krx_changes.csv
cvdobv minute-cache build --index kospi200 --period 5
cvdobv minute-cache update --through 2026-09-16
cvdobv replay run --from 2025-10-01 --to 2026-09-16 --signal-time 15:35
cvdobv forward collect --horizons 1,3,5,10,20 --entry next_open
cvdobv daily-gate backtest --from 2015-10-05 --to 2026-09-16
cvdobv backtest run --experiment configs/experiments/top5_5d.yaml
cvdobv stats ablation --experiment-id <id> --bootstrap block
cvdobv model train --target up_5d --split walk-forward
cvdobv model evaluate --model-id <id> --period test
```

모든 쓰기 CLI는 `--dry-run`, `--run-id`, `--config`, `--log-json`을 지원하고 성공 시 생성 artifact와 row count를 출력한다.

## 8. 테스트 전략

- **단위:** ID 결정성, bar 경계, 거래일 horizon, 수익률, 비용, Gate 조건
- **계약:** V1.5 JSON→SignalRecord, Parquet schema, nullable 및 enum 검증
- **통합:** API fixture→cache→replay→label→backtest의 작은 종단 흐름
- **회귀:** V1.5 골든 종목/날짜의 feature·score·status 비교
- **편향 방지:** 미래 bar 주입, 미래 universe 변경 주입, 미래 조정가격 주입 테스트
- **통계:** 합성 독립/군집 자료에서 CI coverage와 n_eff 방향성 검증
- **장애:** 페이지 중단, rate limit, 중복 응답, 부분 cache, benchmark 결측

## 9. 출시 판단 기준

확률 기능은 다음 모두를 만족할 때만 운영 후보가 된다.

1. 데이터 lineage와 재현성 검증 통과
2. survivorship/look-ahead 검사 0건
3. 적절한 비용 후 OOS 성과가 사전 기준 충족
4. base-rate predictor 대비 확률 손실 개선
5. calibration과 CI 기준 통과
6. ablation으로 핵심 feature의 기여가 설명 가능
7. 다른 시장 regime에서도 결과가 한 기간에만 의존하지 않음

미충족 시 스캐너는 기존 상대 점수와 Gate만 제공하고, 검증 화면에는 `INSUFFICIENT_EVIDENCE`와 근거를 표시한다.

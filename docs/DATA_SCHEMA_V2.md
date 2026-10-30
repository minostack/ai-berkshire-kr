# CVD/OBV 검증 시스템 데이터 스키마 V2

문서 버전: 2.1 (2026-09-19 — Stage 6~8 MINOR 확장 반영)  
저장 형식: Apache Parquet  
기본 시간대: timestamp는 UTC 저장, `trading_date`와 표시 시각은 Asia/Seoul 기준

## 1. 설계 원칙

- Bronze는 원본에 가까운 append-only, Silver는 정규화·중복제거, Gold는 신호·라벨·실험 결과이다.
- null은 0이나 false가 아니다. 반드시 `quality_status` 또는 상세 reason과 함께 해석한다.
- PK는 논리 키이며 Parquet 자체가 강제하지 않으므로 write 전후 validation으로 보장한다.
- 돈과 가격은 가능하면 `decimal128`; 비율·모델 feature는 `float64`; 수량은 `int64`를 사용한다.
- 공급자 정정은 기존 파일을 조용히 덮지 않고 `data_revision`과 ingest manifest로 추적한다.
- 날짜/종목 단위의 작은 파일 폭증을 피하고 월 partition 안에서 정렬한다.

## 2. ID 규칙

| ID | 의미 | 생성 규칙 |
|---|---|---|
| `run_id` | 실행 시도 | `<kind>_<UTC yyyymmddHHMMSS>_<random8>` |
| `signal_id` | 논리 신호 | SHA-256(`scanner_version`,`config_hash`,`universe_snapshot_id`,`signal_at`,`security_id`) |
| `config_hash` | 정규화 설정 | canonical JSON UTF-8의 SHA-256 전체값 |
| `universe_snapshot_id` | 특정 시점 구성 | index, as_of, membership row hash |
| `experiment_id` | 백테스트 정의 | experiment spec hash + run suffix |
| `model_id` | 모델 artifact | target, feature schema hash, train window, algorithm, run |

해시는 저장 시 `sha256:<64 hex>` 형식을 사용한다. 표시용 축약값은 별도 필드로만 허용한다.

## 3. 공통 열

모든 주요 Gold 테이블은 다음 lineage를 가진다.

| 열 | 타입 | Null | 설명 |
|---|---|---:|---|
| `schema_version` | string | N | SemVer, 예: `2.0.0` |
| `created_at_utc` | timestamp[us, UTC] | N | 생성 시각 |
| `run_id` | string | N | 실행 시도 |
| `config_hash` | string | N | 설정 hash |
| `code_revision` | string | N | Git SHA 또는 source SHA-256 |
| `scanner_version` | string | 조건부 | 신호 계산 관련 테이블에서 필수 |
| `market_rule_version` | string | 조건부 | 시장 시간/제도 관련 테이블에서 필수 |
| `data_revision` | int32 | N | 공급자 정정 세대, 시작 1 |

## 4. Bronze 테이블

### `bronze.ingest_runs`

PK: `run_id`

| 열 | 타입 | Null | 설명 |
|---|---|---:|---|
| `run_id` | string | N | 수집 실행 ID |
| `source` | string | N | `KIWOOM_KA10080`, `KIWOOM_KA10081`, `KRX` 등 |
| `dataset` | string | N | minute/daily/universe/benchmark |
| `started_at_utc`, `finished_at_utc` | timestamp[us, UTC] | finished Y | 실행 구간 |
| `request_params_json` | string | N | 비밀정보 제외 canonical JSON |
| `status` | string enum | N | SUCCESS/PARTIAL/FAILED |
| `rows_received`, `rows_written` | int64 | N | 행 수 |
| `page_count`, `retry_count` | int32 | N | API 관측치 |
| `error_code`, `error_message` | string | Y | 실패 정보 |
| `payload_hash` | string | Y | 저장 payload hash |

Partition: `dataset`, `started_year`, `started_month`.

### `bronze.raw_api_records`

원본 JSON을 감사용으로 유지할 때 사용한다. PK: `source`,`api_id`,`request_fingerprint`,`page_no`,`record_no`,`run_id`. 응답 header와 pagination token은 비밀정보 제거 후 저장한다. Partition: `source/api_id/ingest_date`.

## 5. Silver 시장 데이터

### `silver.daily_bars`

PK: `security_id`,`trading_date`,`price_scope`,`adjustment_type`,`data_revision`

| 열 | 타입 | Null | 설명 |
|---|---|---:|---|
| `security_id` | string | N | 예: `KRX:005930` |
| `trading_date` | date32 | N | KST 거래일 |
| `open`, `high`, `low`, `close` | decimal128(20,4) | N | 가격 |
| `volume` | int64 | N | 거래량, `>=0` |
| `traded_value` | decimal128(24,0) | Y | 거래대금 |
| `price_scope` | enum | N | REGULAR/EXTENDED/VENDOR_DAILY |
| `adjustment_type` | enum | N | RAW/SPLIT_ADJUSTED/TOTAL_RETURN |
| `adjustment_factor` | float64 | Y | 조정계수 |
| `available_at_utc` | timestamp[us, UTC] | N | 이 row를 알 수 있었던 시각 |
| `source` | string | N | 공급자/API |
| `quality_status` | enum | N | SUCCESS/PARTIAL/FAILED |
| `run_id`, `market_rule_version` | string | N | lineage |
| `data_revision` | int32 | N | 정정 세대 |

검증: `low <= min(open,close)`, `high >= max(open,close)`, 음수 가격 금지, security/date 중복 금지.  
Partition: `market`, `year`, `month`; 정렬: `security_id,trading_date,price_scope`.

### `silver.minute_bars`

PK: `security_id`,`period_min`,`bar_start_at_utc`,`session`,`data_revision`

| 열 | 타입 | Null | 설명 |
|---|---|---:|---|
| `security_id` | string | N | 종목 ID |
| `period_min` | int16 | N | 1/3/5/10/15/30/60 |
| `bar_start_at_utc`, `bar_end_at_utc` | timestamp[us, UTC] | N | 반개구간 `[start,end)` |
| `trading_date` | date32 | N | KST 거래일 |
| `session` | enum | N | REGULAR/AFTER/OTHER |
| `open`, `high`, `low`, `close` | decimal128(20,4) | N | 절대 가격으로 정규화 |
| `volume` | int64 | N | 거래량 |
| `traded_value` | decimal128(24,0) | Y | 공급 또는 계산값. KIS 봉은 같은 날 직전 봉과의 누적거래대금 차이(그날 첫 봉은 누적값, 첫 봉을 받았는지 모르면 null) |
| `cum_traded_value` | decimal128(24,0) | Y | 그날 그 시장(KRX `J`/NXT `NX`)의 **누적 거래대금(원)** — KIS `acml_tr_pbmn`. 2026-10-23 추가, 키움 봉·옛 파일은 null |
| `source_timestamp_raw` | string | N | 원본 타임스탬프 |
| `timestamp_semantics` | enum | N | BAR_START/BAR_END |
| `available_at_utc` | timestamp[us, UTC] | N | 수신 가능 시각 |
| `quality_status`, `quality_flags` | enum, list<string> | N | 품질 |
| `run_id`, `market_rule_version`, `data_revision` | string,string,int32 | N | lineage |

Partition: `period_min`, `year`, `month`, `security_bucket`(종목 hash mod 32). 날짜별 파일 partition은 피한다. 정렬: `security_id,bar_start_at_utc`.

### `silver.bar_coverage`

PK: `dataset`,`security_id`,`period_min`,`trading_date`,`session`,`data_revision`

열: expected/actual bar count(int32), first/last bar, missing interval list, stale minutes, pagination_complete(bool), status, reason codes, run_id. Cache 신뢰성 판정에 사용한다.

### `silver.security_master`

PK: `security_id`,`valid_from`; unique: `market`,`local_code`,`valid_from`.

열: local_code, ISIN(nullable), name, market, listed_from, delisted_on(nullable), predecessor/successor security_id(nullable), valid_from/to, known_at, revision, source.

### `silver.universe_membership`

PK: `index_id`,`security_id`,`effective_from`,`universe_version`

| 열 | 타입 | Null | 설명 |
|---|---|---:|---|
| `index_id` | string | N | KOSPI200/KOSDAQ150 |
| `security_id` | string | N | 구성종목 |
| `effective_from` | timestamp[us, UTC] | N | 포함 시작 |
| `effective_to` | timestamp[us, UTC] | Y | 제외 시각, open ended 가능 |
| `known_at_utc` | timestamp[us, UTC] | N | 시스템이 변경을 알 수 있던 시각 |
| `change_type` | enum | N | ADD/REMOVE/INITIAL/CORRECTION |
| `source_ref` | string | N | 공시/파일 식별자 |
| `universe_version` | string | N | 이력 revision |
| `run_id`, `data_revision` | string,int32 | N | lineage |

불변식: 동일 index/security의 유효구간 겹침 금지; snapshot 사용 시 `effective_from <= as_of < effective_to` 및 연구 정책에 따라 `known_at <= as_of`.  
Partition: `index_id`, `effective_year`.

### `silver.benchmark_bars`

PK: `benchmark_id`,`trading_date`,`price_scope`,`data_revision`. Daily bars와 같은 OHLC 구조, `total_return_close` nullable, available_at, source, market_rule_version 포함. Partition: `benchmark_id`,`year`.

### `silver.market_calendar`

PK: `market`,`trading_date`,`market_rule_version`.

열: is_trading_day, regular_open/close UTC, after_open/close(nullable), next_trading_date, previous_trading_date, special_session(bool), source. Horizon과 next_open 계산의 유일한 달력이다.

## 6. Gold 신호 데이터

### `gold.scanner_runs`

PK: `run_id`; unique 권고: `signal_at_utc`,`universe_snapshot_id`,`scanner_version`,`config_hash`,`signal_source`.

열: signal_at, signal_time_local, signal_source(LIVE/REPLAY), index_id, universe_snapshot_id, member_count, attempted/success/partial/failed count, last_input_bar_at, scanner_version, market_rule_version, config_hash, code_revision, status, error_code, input_manifest_hash.

Partition: `signal_source`, `signal_year`, `signal_month`.

### `gold.signals`

PK: `signal_id`; unique: `run_id`,`security_id`. 동일 논리 신호를 다른 run에서 재생한 결과는 `signal_observations`로 보관하거나 canonical row와 run link를 분리한다.

| 열 | 타입 | Null | 설명 |
|---|---|---:|---|
| `signal_id`, `run_id` | string | N | 식별자 |
| `signal_source` | enum | N | LIVE/REPLAY |
| `signal_at_utc` | timestamp[us, UTC] | N | 기본 15:35 KST |
| `signal_time_local` | time64[us] | N | 시각 규약 |
| `trading_date` | date32 | N | KST |
| `security_id`, `index_id` | string | N | 대상 |
| `universe_snapshot_id` | string | N | 당시 유니버스 |
| `status` | enum | N | BUY_CANDIDATE/WATCH/DATA_INSUFFICIENT |
| `gate_passed` | bool | N | Gate 결과 |
| `gate_reasons` | list<string> | N | 빈 list 허용 |
| `total_score` | float64 | Y | 품질 부족 시 null 가능 |
| `rank`, `rank_population` | int32 | Y | 횡단면 순위 |
| `rank_percentile` | float64 | Y | `[0,1]` |
| `feature_status` | enum | N | SUCCESS/PARTIAL/FAILED |
| `daily_evidence_tier` | enum | N | FULL_SIGNAL_SHORT/DAILY_ONLY_LONG |
| `scanner_version`, `market_rule_version`, `config_hash` | string | N | 버전 |
| `last_input_bar_at_utc` | timestamp[us, UTC] | Y | 입력 최신성 |

Partition: `signal_source`, `signal_year`, `signal_month`, `index_id`. 정렬: `signal_at_utc,index_id,rank,security_id`.

### `gold.signal_features`

PK: `signal_id`. 주요 typed columns:

- CVD: `rank_session`, `cvd_normalized_delta`, `cvd_normalized_momentum`, `cvd_bull_ratio_completed`, `cvd_bull_days`, `cvd_total_days`, regular/after data status, momentum status, last bar age
- OBV: `obv`, `obv_ma20`, `obv_normalized_slope`, `obv_divergence`, `obv_data_status`
- Price: `close`, `ma20`, `ma60`, `ma120`, `above_ma20`, `ma20_slope`, `close_vs_ma20_pct`, `price_data_status`
- Liquidity: `avg_traded_value_20d`, `liquidity_percentile`, `liquidity_data_status`
- Risk: `atr`, `atr_pct`, `overheated`, `risk_penalty`
- Components: `cvd_score`, `obv_score`, `price_score`, `liquidity_score`
- Availability: 각 feature family의 `max_available_at_utc`

각 수치 feature는 nullable이며 해당 family status가 SUCCESS가 아닐 때의 null을 허용한다. `available_at > signal_at`이면 write를 거부한다.

## 7. Forward Return과 Label

### `gold.forward_returns`

PK: `signal_id`,`horizon_td`,`label_version`

| 열 | 타입 | Null | 설명 |
|---|---|---:|---|
| `signal_id` | string | N | 신호 |
| `horizon_td` | int16 | N | 1/3/5/10/20 거래일. label은 전부 만들되 **분석 기본 격자는 3/5/10/20**이다(로드맵 §3, 2026-09-19) |
| `entry_rule` | enum | N | NEXT_OPEN |
| `entry_date`, `exit_date` | date32 | Y | 성숙 전 null |
| `entry_at_utc`, `exit_at_utc` | timestamp[us, UTC] | Y | 체결 기준시각 |
| `entry_price`, `exit_price` | decimal128(20,4) | Y | 가격 |
| `stock_return_gross` | float64 | Y | 단순수익률 |
| `stock_return_net` | float64 | Y | 비용 후 |
| `benchmark_id` | string | Y | 기준 지수 |
| `benchmark_return` | float64 | Y | 동일 기간 |
| `excess_return_gross`, `excess_return_net` | float64 | Y | 종목-벤치마크 |
| `mfe`, `mae` | float64 | Y | horizon 내 최대 유리/불리 수익 |
| `up`, `outperform` | bool | Y | 방향 label |
| `hit_3pct`, `hit_5pct`, `hit_minus_3pct`, `hit_minus_5pct` | bool | Y | barrier label |
| `commission`, `tax`, `slippage` | float64 | Y | 비용률 |
| `label_status` | enum | N | COMPLETE/PENDING_MATURITY/ENTRY_UNAVAILABLE/EXIT_UNAVAILABLE |
| `label_version`, `market_rule_version`, `run_id` | string | N | lineage |

`up`의 기본 정의는 `stock_return_net > 0`; gross 방향도 별도 열이 필요하면 schema minor에서 추가한다. Barrier가 종가 기준인지 intraday high/low 기준인지 label version에 고정한다.

Partition: `horizon_td`, `entry_year`, `entry_month`. 성숙 전이라 `entry_date`가
없는 행은 `signal_date` 기준으로 나눈다 — 파티션 없는 바구니에 몰면 그 행만
따로 읽을 수 없다.

**2026-09-19 MINOR 확장(Stage 6 구현).** 위 열 목록에 셋을 더했다.

| 추가 열 | 타입 | 이유 |
|---|---|---|
| `security_id` | string | 조인 없이 종목을 알 수 있어야 한다 |
| `signal_date` | date32 | 신호 거래일. 파티션 대체값으로도 쓴다 |
| `signal_at_utc` | timestamp | **`signal_at < entry_at` 불변식을 이 행 하나만 보고 확인하려고.** `gold.signals`와 조인해야만 검증되는 불변식은 조인을 잊는 순간 사라진다 |
| `calendar_checked` | bool | 진입·청산일을 거래 캘린더로 대조했는가(Stage 6 DoD) |
| `label_source` | string | 값이 어디서 왔는지. 지금은 항상 `gold.daily_forward_returns` |

`label_source`가 뜻하는 것은 **수익률을 여기서 다시 계산하지 않는다**는 사실이다.
Stage 1의 무조건부 패널을 조인해서 옮긴다. 같은 진입·청산 규칙을 두 번 구현하면
두 표가 갈릴 수 있고, 갈렸을 때 어느 쪽이 맞는지 알 방법이 없다. 조인이므로
Stage 1의 base rate와 Stage 6의 신호 수익률은 같은 계산에서 나온다.

### `gold.base_rates`

PK: `base_rate_id`(spec hash); unique: universe/index, start/end, horizon, entry_rule, label, regime, label_version.

열: raw_n, trading_date_n, positive_n, probability, mean/median return, benchmark mean, excess mean, cluster_key, ICC, design_effect, effective_n, ci_method, ci_low/high, block_length, bootstrap_replicates, seed, status, run_id.

## 8. Backtest 데이터

### `gold.backtest_runs`

PK: `experiment_id`.

열: spec_json, spec_hash, signal query, start/end, selection policy, execution policy, cost_model_version, benchmark_id, label_version, input_manifest_hash, status, run_id, created_at.

### `gold.trades`

PK: `experiment_id`,`trade_id`; unique: experiment, signal_id, entry_at.

열: signal_id, security_id, side, quantity/weight, selected_rank, entry/exit date and price, gross/net return, benchmark/excess return, all costs, exit_reason, overlap_group, trade_status.

Partition: `experiment_id_prefix`, `entry_year`. 체결되지 않은 신호도
`trade_status=REJECTED`로 남기고 `reject_reason`에 사유를 적는다(슬롯 부족·
중복 보유·하루 진입 상한·유동성 상한·label 미완성). 조용히 버리면 "살 수
있었는데 안 산 것"과 "살 수 없었던 것"을 구별할 수 없다. 체결되지 않은 행은
`entry_date`가 없으므로 `signal_date`로 파티션한다.

**2026-09-19 구현 시 추가한 열(§10 MINOR).** `signal_date`·`signal_at_utc`
(`entry_at > signal_at`를 이 행만 보고 확인하기 위해), `reject_reason`,
`sample_scope`(walk-forward IS/OOS), `pnl`(원장 합계의 단위).

한 `run_id`로 여러 실험을 쓰므로 파일명에 실험 식별자를 붙인다. 붙이지 않으면
같은 경로에 겹쳐 써서 **마지막 실험만 남는다.**

### `gold.portfolio_nav`

PK: `experiment_id`,`timestamp_utc`.

열: cash, market_value, gross_nav, net_nav, benchmark_nav, exposure, open_positions, turnover, drawdown.

### `gold.backtest_metrics`

PK: `experiment_id`,`metric_scope`,`horizon_td`,`segment_key`,`metric_name`.

열: metric_value, raw_n, effective_n, ci_low/high, standard_error, inference_method, cluster_key, block_length, p_value(nullable), adjusted_p_value(nullable), status.

### `gold.ablation_results`

PK: `ablation_run_id`,`variant_id`,`metric_name`,`segment_key`.

열: base_experiment_id, removed_components(list<string>), variant_spec_hash, paired_dates, base/variant value, delta, CI, p-value, adjustment_method, status. 동일 표본의 paired 비교를 원칙으로 한다.

**2026-09-19 구현 시 추가한 열(§10 MINOR).**

| 추가 열 | 이유 |
|---|---|
| `selection` | `BUY`(Gate만) / `TOP20`(Gate+점수). 로드맵이 요구한 "score와 absolute Gate의 분리 비교"를 이 축으로 낸다 |
| `mean_selection_size` | 하루 평균 몇 종목을 골랐는가. **거의 전부를 고르면 edge가 0에 가까워지고 구간이 좁아진다** — 크기를 모르면 "유의하다"를 오해한다 |
| `mde_80`, `observed_over_mde`, `required_dates_80` | 검정력. 신뢰구간이 0을 포함하는 것과 **애초에 잡을 수 없는 크기였던 것**은 다르다 |
| `edge_verdict` | `SIGNIFICANT_EDGE` / `NO_SIGNIFICANT_EDGE` / **`UNDERPOWERED`** / `NOT_EVALUABLE` |
| `inference_method`, `cluster_key`, `block_length`, `bootstrap_replicates`, `seed`, `ci_level`, `icc`, `design_effect`, `effective_n` | Stage 8 DoD가 artifact에 남기라고 요구한 것들 |

`UNDERPOWERED`를 `NO_SIGNIFICANT_EDGE`와 구별하는 것이 이 표의 요점이다.
앞은 "잴 수 없었다", 뒤는 "잴 수 있었는데 안 나왔다"다.

## 9. 모델 데이터와 레지스트리

### `gold.model_examples`

PK: `dataset_id`,`signal_id`,`target_name`.

열: feature_schema_hash, target_name/value, base_rate, sample_weight, split(TRAIN/CALIBRATION/TEST), fold_id, embargo_start/end, feature_available_at_max, label_available_at, included(bool), exclusion_reason. Feature 값은 `signal_features` snapshot과 immutable join manifest로 연결한다.

Partition: `dataset_id`, `split`, `signal_year`.

### `gold.model_registry`

PK: `model_id`.

| 열 | 타입 | Null | 설명 |
|---|---|---:|---|
| `model_id` | string | N | 모델 ID |
| `algorithm` | string | N | empirical/logistic/lightgbm 등 |
| `target_name`, `horizon_td` | string,int16 | N | 목표 |
| `feature_schema_hash` | string | N | feature 계약 |
| `train_start/end`, `calibration_start/end`, `test_start/end` | date32 | N | 시간 분할 |
| `hyperparameters_json` | string | N | canonical JSON |
| `calibration_method` | enum | Y | NONE/PLATT/ISOTONIC |
| `artifact_uri`, `artifact_hash` | string | N | 모델 파일 |
| `status` | enum | N | CANDIDATE/APPROVED/REJECTED/RETIRED |
| `scanner_version`, `market_rule_version`, `label_version` | string | N | 호환성 |
| `run_id`, `created_at_utc` | string,timestamp | N | lineage |

### `gold.model_metrics`

PK: `model_id`,`split`,`segment_key`,`metric_name`.

필수 metric: Brier, log loss, ROC-AUC, PR-AUC, ECE, calibration intercept/slope, hit rate, expected/realized return, base-rate Brier, delta Brier, raw N, effective N, CI. `publication_status`와 차단 사유를 포함한다.

### `gold.probability_predictions`

PK: `model_id`,`signal_id`.

열: raw_probability, calibrated_probability, base_rate, excess_probability, ci_low/high, effective_n_reference, calibration_status, oos_status, evidence_status, published_probability(nullable), predicted_at, model/input hashes. 증거 기준 미충족 시 `published_probability`는 null이다.

## 10. Manifest와 Versioning

### `data.manifests`

각 dataset write마다 JSON 또는 작은 Parquet manifest를 생성한다.

필수 필드:

```text
dataset_name, schema_version, run_id, created_at_utc
partition_paths, row_counts, min/max event time
file sha256, input manifest hashes
config_hash, code_revision
scanner_version, market_rule_version, universe_version
quality summary, status
```

Schema SemVer 규칙:

- PATCH: 문서·metadata 수정, 데이터 의미 불변
- MINOR: nullable 열 추가 또는 enum 확장; 기존 reader 호환
- MAJOR: PK, 타입, 단위, 시간 의미, label 정의 변경

Major는 `.../schema_major=3/` 같은 새 root에 dual-write/backfill 후 전환한다. Label 의미 변경은 schema가 같아도 반드시 새 `label_version`을 사용한다.

## 11. 권장 물리 경로

```text
data/
├─ bronze/raw_api_records/source=.../api_id=.../ingest_date=YYYY-MM-DD/*.parquet
├─ bronze/ingest_runs/dataset=.../started_year=YYYY/started_month=MM/*.parquet
├─ silver/daily_bars/market=KRX/year=YYYY/month=MM/*.parquet
├─ silver/minute_bars/period_min=5/year=YYYY/month=MM/security_bucket=NN/*.parquet
├─ silver/universe_membership/index_id=KOSPI200/effective_year=YYYY/*.parquet
├─ silver/benchmark_bars/benchmark_id=KOSPI200/year=YYYY/*.parquet
├─ gold/signals/signal_source=REPLAY/signal_year=YYYY/signal_month=MM/index_id=KOSPI200/*.parquet
├─ gold/forward_returns/horizon_td=5/entry_year=YYYY/entry_month=MM/*.parquet
└─ gold/model_examples/dataset_id=.../split=TRAIN/signal_year=YYYY/*.parquet
```

Target file size는 대략 128~512 MiB로 compaction한다. 독립 종목별 파일은 조회 편의보다 파일 수 비용이 커서 기본 설계로 사용하지 않는다.

## 12. 데이터 품질 규칙

### Hard fail

- PK 중복, schema/type 불일치
- `bar_end_at <= bar_start_at`
- `available_at > signal_at`인 feature
- 현재 universe snapshot으로 과거 replay
- `entry_at <= signal_at`
- 알 수 없는 `market_rule_version`
- train/calibration/test 시간 중첩 또는 embargo 위반

### Row/partition quarantine

- OHLC 불변식 위반, 음수 거래량
- 페이지 미완료, 비정상 bar gap, stale latest bar
- security master 매핑 실패
- benchmark 또는 entry/exit 가격 부재

### 경고

- 예상 bar 수의 경미한 차이
- 특정 feature null률 급증
- signal coverage 감소
- base rate 또는 calibration drift

품질 결과는 `quality_status`, `quality_flags`, 별도 quality report에 모두 남기며, 실패 행을 조용히 제거하지 않는다.

## 13. 샘플 조인 규칙

```sql
-- 개념적 예시
SELECT s.*, f.*, r.stock_return_net, r.excess_return_net
FROM gold.signals s
JOIN gold.signal_features f USING (signal_id)
JOIN gold.forward_returns r USING (signal_id)
WHERE r.horizon_td = 5
  AND r.label_status = 'COMPLETE'
  AND f.max_available_at_utc <= s.signal_at_utc;
```

모델용 dataset builder만 이 join을 수행하며, future return 열은 feature allow-list에 들어갈 수 없다. Feature schema hash는 열 이름, dtype, null policy, 변환 버전을 포함한다.

## 14. 보존과 정정 정책

- Bronze와 manifests: 영구 보존 또는 감사 요구 기간 이상
- Silver: revision별 보존; 최신 view는 명시적 pointer/catalog로 제공
- Gold signals/backtests/models: 실험 재현 기간 동안 immutable
- 삭제나 compaction은 원본 hash와 replacement manifest를 남긴다.
- 공급자 정정 시 영향을 받는 signal/label/model을 lineage graph로 찾아 새 revision을 생성하며 과거 artifact를 덮어쓰지 않는다.

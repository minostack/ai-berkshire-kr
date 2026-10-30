# CVD/OBV 검증 시스템 아키텍처 V2

문서 버전: 2.1 (2026-09-19 — Stage 6~8 모듈·CLI 반영)  
기준 구현: `kiwoom_cvd_obv_scanner.py` V1.5

## 1. 설계 목표

V1.5의 신호 계산을 보존하면서 데이터 취득, 과거 재생, 미래성과 수집, 백테스트, 통계 추론, 확률 모델을 분리한다. 각 계층은 Parquet 계약과 immutable manifest로 연결하며, live와 historical replay가 같은 계산 코어와 출력 형식을 사용한다.

## 2. 현재 V1.5에서 유지할 계약

V1.5가 제공하는 핵심은 다음과 같다.

- 모든 종목에 공통 `as_of`를 전달하고 `bar_end > as_of`인 분봉을 제외한다.
- 미완성 일봉을 OBV·MA·ATR·유동성 계산에서 제외한다.
- 정규장과 애프터마켓 CVD를 분리하고 기준 세션을 선택한다.
- CVD/OBV/가격/유동성별 `SUCCESS/PARTIAL/FAILED` 품질을 점수 모집단에 반영한다.
- CVD 40, OBV 35, 가격 15, 유동성 10, 과열 -5의 상대 점수와 Absolute Gate를 분리한다.
- `BUY_CANDIDATE/WATCH/DATA_INSUFFICIENT`, Gate 사유, 원시 feature를 결과에 포함한다.
- JSON snapshot에 `scanner_version`, `market_rule_version`, `run_id`, `as_of`, `last_input_bar_at`, config와 `config_hash`를 저장한다.

V2는 이를 `scanner_core`의 호환 기준으로 사용한다. 계산식 변경은 새 `scanner_version`으로만 허용한다.

## 3. 핵심 제약과 아키텍처 결정

| 제약 | 결정 |
|---|---|
| 일봉 연구 패널 2010~2026년(약 16년, 4,114거래일), 분봉 약 255거래일 | daily-only 장기 연구와 full-signal 단기 연구를 별도 evidence tier로 유지. 일봉 **API 깊이**는 상장일까지지만 **연구 패널**은 유니버스 이력이 있는 2010년부터다 |
| 분봉 API에 `base_dt` 없음 | 최신 전체 범위를 한 번 cache하고 과거 시점은 offline slice |
| 현재 index snapshot만 사용하면 생존편향 | effective-dated universe history가 없는 날짜는 replay 금지 |
| 15:35와 장 마감 데이터 정의 차이 | fixed signal time과 price scope를 manifest에 고정 |
| 다음 거래 가능 시점 | signal 당일 종가 체결을 금지하고 기본 `next_open` |
| 같은 날 종목과 겹치는 보유기간의 상관 | cluster robust, block bootstrap, n_eff를 통계 계층에 필수화 |
| 시장제도 변경 | `market_rule_version`을 scanner/config/schema/model 버전과 독립 관리 |

## 4. 논리 구조

```text
Kiwoom/KRX sources
      │
      ├── DailyHistoryIngestor ───────┐
      ├── MinuteHistoryCache ─────────┤
      ├── UniverseHistory ────────────┤
      └── BenchmarkIngestor ──────────┘
                                      ▼
                           Parquet Bronze/Silver
                                      │
                ┌─────────────────────┴────────────────────┐
                ▼                                          ▼
       DailyGateBacktester                    HistoricalScannerReplay
                │                                          │
                └──────────────┐                 SignalRecord
                               ▼                     │
                         BaseRateStore               │
                                                     ▼
                                         ForwardReturnCollector
                                                     │
                                                     ▼
                                              BacktestEngine
                                                     │
                         ┌───────────────────────────┴───────────┐
                         ▼                                       ▼
                  StatisticalInference                  ProbabilityModel
                         │                                       │
                         └──────── Reports / Model Registry ─────┘
```

## 5. 런타임 경계

### Online 경로

15:35 scheduler → 해당 시점 유니버스 확인 → cache 최신성 확인 → scanner core → signals 저장. 데이터가 불완전하면 확률을 보간하지 않고 상태 코드로 제외한다.

### Offline 연구 경로

버전이 고정된 silver 데이터 → 거래일별 replay → forward labels → backtest/statistics/model. 연구 artifact는 사용한 파일 hash와 partition 목록을 manifest에 남긴다.

## 6. 모듈 책임과 주요 API

### `domain.ids`

```python
def canonical_config_hash(config: Mapping[str, Any]) -> str
def make_run_id(kind: str, started_at: datetime, nonce: str | None = None) -> str
def make_signal_id(scanner_version: str, config_hash: str,
                   universe_id: str, signal_at: datetime, security_id: str) -> str
```

`signal_id`는 같은 논리 신호에 결정적이며, `run_id`는 실행 시도 단위이다. 동일 신호의 재수집은 같은 `signal_id`와 다른 `run_id`를 가질 수 있다.

### `market_rules`

```python
class MarketRuleRegistry:
    def resolve(self, market: str, at: datetime) -> MarketRule
    def session_bounds(self, trading_date: date, session: str) -> Interval
    def is_bar_complete(self, bar: Bar, as_of: datetime) -> bool
```

`market_rule_version`은 시행일별 세션, bar timestamp 의미, 일봉 scope, 가격제한 등 시장 규칙을 정의한다. 코드 버전과 분리해 과거 규칙을 다시 해석하지 않도록 한다.

### `daily_history_cache`

```python
class DailyHistoryRepository:
    def upsert_raw(self, rows: Iterable[RawDailyBar], run: IngestRun) -> WriteResult
    def load(self, security_id: str, start: date, end: date,
             price_scope: str, adjusted: bool) -> Table
    def validate(self, partition: PartitionRef) -> QualityReport
```

Corporate action 조정 전후를 명시하고, 수정된 공급자 값을 revision으로 보존한다.

### `minute_history_cache`

```python
class MinuteHistoryCache:
    def build(self, securities: Sequence[str], period_min: int) -> IngestSummary
    def update(self, through: datetime) -> IngestSummary
    def load_bars(self, security_id: str, start: datetime, end: datetime,
                  session: str | None = None) -> Table
    def slice_as_of(self, security_id: str, as_of: datetime,
                    lookback_trading_days: int, period_min: int) -> Table
    def validate(self, security_id: str, trading_date: date) -> QualityReport
```

`slice_as_of`는 반드시 `bar_end_at <= as_of`를 강제한다. Cache build는 append/upsert 후 원본 partition checksum을 남기며 pagination 중단을 성공으로 처리하지 않는다.

### `universe_history`

```python
class UniverseRepository:
    def get_members(self, index_id: str, as_of: datetime) -> UniverseSnapshot
    def import_changes(self, changes: Iterable[MembershipChange]) -> ImportResult
    def validate_coverage(self, index_id: str, start: date, end: date) -> CoverageReport
```

유효구간은 `[effective_from, effective_to)`이다. `known_at`도 저장해 사후 정정이나 공시 인지 시점에 의한 look-ahead를 검사한다.

**출처와 검산 (2026-09-18 추가).** 과거 구성종목은 두 출처를 교차해 만든다.

| 모듈 | 역할 |
|---|---|
| `krx_openapi` | KRX Open API(2010-01-04~). 구성종목 API는 **없다**. 지수·종목 시가총액을 받아 **검산**에 쓴다 |
| `krx_notices` | `index.krx.co.kr` 지수운영공지(로그인 불필요). 변경일·사유와 첨부 엑셀 |
| `krx_index_files` | 첨부 엑셀 파서. 편입·편출과 변경 후 구성종목 전량을 뽑는다 |
| `universe_rebuild` | 오늘 명단에서 거꾸로 걸으며 매 거래일 검산하고, 깨진 날을 공시·앵커·역산으로 메운다 |
| `security_master` | 그날 상장돼 있던 종목 패널에서 상장·폐지 이력을 역산한다 |

검산식은 실측으로 확인한 항등식이다.

```
코스피 200 MKTCAP == Σ(구성종목 상장시가총액)      # 오차 0원
Σ(코스피 200 섹터지수 11종 MKTCAP) == 코스피 200 MKTCAP
```

이 식이 성립하므로 **명단의 정오를 거래일마다 증명할 수 있다.** 복원 결과를
받아들이는 기준은 "그럴듯함"이 아니라 "차액 0원"이다. 메우지 못한 날은
`UNRESOLVED`로 남기고 그 구간을 신뢰 구간에서 제외한다.

### `scanner_core`

```python
class ScannerV15Adapter:
    def analyze_security(self, ctx: SignalContext,
                         minute_bars: Table, daily_bars: Table) -> FeatureRecord
    def finalize_cross_section(self, ctx: SignalContext,
                               features: Sequence[FeatureRecord]) -> list[SignalRecord]
```

횡단면 백분위 때문에 종목별 분석과 유니버스 전체 finalize를 분리한다. 외부 API 호출, 현재시각 읽기, 파일 읽기를 core에서 제거하고 모두 context로 주입한다.

### `historical_scanner_replay`

```python
class HistoricalScannerReplay:
    def replay_date(self, index_id: str, signal_at: datetime,
                    config: ScannerConfig) -> ReplayResult
    def replay_range(self, start: date, end: date,
                     schedule: SignalSchedule) -> ReplaySummary
```

처리 순서는 market rule resolve → universe as-of 조회 → 데이터 coverage 검사 → 15:35 slice → feature 계산 → cross-section 점수 → Gate → atomic write이다.

### `forward_return_collector`

```python
class ForwardReturnCollector:
    def collect(self, signals: Table, horizons: Sequence[int],
                entry_rule: str = "next_open") -> LabelBatch
    def mature_pending(self, through: date) -> LabelBatch
```

산출 항목은 stock/benchmark gross return, excess return, net return, MFE/MAE, `up`, `outperform`, `hit_target`, `hit_stop`이다. 향후 가격이 부족하면 null과 `PENDING_MATURITY`를 기록한다.

### `daily_gate_backtester`

```python
class DailyGateBacktester:
    def generate_daily_features(self, as_of_date: date) -> Table
    def evaluate(self, spec: DailyGateSpec) -> DailyGateReport
```

2010~2026년 자료(약 16년)에서 일봉 3조건과 ablation을 평가한다. full V1.5 성과로 합치지 않고 `evidence_tier=DAILY_ONLY_LONG`으로 표시한다.

### `backtest_engine`

```python
class BacktestEngine:
    def select(self, signals: Table, policy: SelectionPolicy) -> Table
    def simulate(self, selected: Table, execution: ExecutionPolicy,
                 costs: CostModel) -> BacktestRun
    def summarize(self, run: BacktestRun, benchmark: Table) -> BacktestReport
```

선택, 체결, 회계, 평가를 분리한다. Top-K tie-break는 `total_score desc, liquidity desc, security_id asc`처럼 고정한다. 같은 종목의 중복 보유, 자본 제한, 거래정지는 명시적 policy로 처리한다.

### `signal_labels` · `signal_gate` (Stage 6)

```python
class SignalLabelBuilder:
    def build(self, run_id, index_id=None, start=None, end=None) -> SignalLabelSummary

class SignalGateEvaluator:
    def evaluate(self, spec: SignalGateSpec, run_id: str = "") -> list[dict]
```

label은 **계산하지 않고 무조건부 패널에 조인한다.** 같은 진입·청산 규칙을 두 번
구현하면 두 표가 갈릴 수 있고, 갈렸을 때 어느 쪽이 맞는지 알 방법이 없다.

### `score_model` · `ablation` · `power` (Stage 8)

```python
class ScoreModel:
    def score_day(self, rows) -> list[float]
    def gate_row(self, row) -> tuple[str, list[str]]
    def verify(self, by_day, expected)        # V1.5와 같은지 hard fail
    def verify_gate(self, by_day, expected)

class AblationRunner:
    def run(self, run_id, variants=None, ...) -> dict

def minimum_detectable_effect(standard_error, alpha=0.05, power=0.80)
def required_observations(value, mde, current_n)
def verdict_for(value, ci_low, ci_high, mde)  # UNDERPOWERED를 구별한다
```

V1.5 코어는 동결돼 있으므로 ablation은 **저장된 feature에서 점수를 되짚어**
만든다. 되짚은 값이 V1.5와 다르면 실행 전에 멈춘다 — 조립 규칙이 틀렸다면
ablation 전부가 무의미하다.

### `statistics`

```python
def effective_sample_size(frame: Table, cluster: str, horizon: int) -> Estimate
def cluster_robust_interval(frame: Table, outcome: str,
                            cluster: str = "signal_date") -> Interval
def block_bootstrap(frame: Table, statistic: Callable,
                    block_length: int, n_resamples: int, seed: int) -> BootstrapResult
def run_ablation(base_spec: ExperimentSpec,
                 components: Sequence[str]) -> AblationReport
```

기본 block length는 최소 horizon이며 자동 선택 시 선택 규칙도 artifact에 기록한다. 종목×날짜 행 수를 독립 표본수로 보고하지 않으며 모든 confidence interval은 산출 방법을 함께 기록한다.

### `probability_model`

```python
class DatasetBuilder:
    def build(self, feature_set: str, target: str, split: TimeSplit) -> DatasetBundle

class ProbabilityTrainer:
    def fit(self, train: Table, algorithm: str) -> FittedModel
    def calibrate(self, model: FittedModel, calibration: Table,
                  method: str) -> CalibratedModel
    def evaluate(self, model: CalibratedModel, test: Table) -> ModelReport

class ProbabilityPublisher:
    def eligibility(self, report: ModelReport) -> PublicationDecision
```

모델은 base-rate predictor와 비교한다. AUC만으로 승격하지 않고 Brier, log loss, calibration intercept/slope, ECE, OOS edge와 n_eff를 본다.

## 7. 입출력 계약

### 공통 실행 입력 `RunContext`

```json
{
  "run_id": "replay_20260916_153500_ab12cd34",
  "as_of": "2026-09-16T15:35:00+09:00",
  "signal_time": "15:35:00",
  "scanner_version": "1.5",
  "market_rule_version": "KRX-2026-09-14-aftermarket",
  "schema_version": "2.0.0",
  "config_hash": "sha256:...",
  "code_revision": "git-sha-or-source-sha256"
}
```

### `SignalRecord` 최소 계약

```json
{
  "signal_id": "sig_...",
  "run_id": "replay_...",
  "signal_source": "REPLAY",
  "signal_at": "2026-09-16T15:35:00+09:00",
  "security_id": "KRX:005930",
  "universe_id": "KOSPI200",
  "status": "BUY_CANDIDATE",
  "gate_passed": true,
  "total_score": 87.3,
  "rank": 2,
  "feature_status": "SUCCESS",
  "scanner_version": "1.5",
  "market_rule_version": "KRX-2026-09-14-aftermarket",
  "config_hash": "sha256:..."
}
```

세부 feature는 중첩 JSON 대신 typed Parquet columns로 펼친다. Gate reasons는 `list<string>`으로 저장한다.

## 8. CLI와 종료 코드

| 명령 | 핵심 옵션 | 출력 |
|---|---|---|
| `daily-cache build` | `--start --end --adjustment` | daily bars, manifest |
| `minute-cache build/update` | `--index --period --through` | minute bars, quality |
| `universe import/validate` | `--index --source` | membership intervals |
| `replay run` | `--index --start --end` | signals, replay runs |
| `forward build` | `--horizons --benchmark` | 무조건부 forward return 패널 |
| `daily-gate backtest` | 기간, 조건 spec, `--segment-by` | daily evidence |
| `signal-label build` | `--index --horizons` | 신호별 forward return |
| `signal-gate evaluate` | `--horizons --segment-by` | BUY vs WATCH, 점수·순위 단조성 |
| `backtest run` | `--policies --horizon --max-positions --oos-start` | trades, NAV, metrics |
| `ablation run` | `--variants --horizon` | paired deltas, CI, MDE |
| `model train/evaluate` | target, split, algorithm | model artifact/report (미구현) |

구현 시 이름이 로드맵 초안과 달라진 것 — `forward collect` → `forward build`,
`stats ablation` → `ablation run`. 실제 CLI가 기준이다.

**분석 명령의 기본 보유기간은 3/5/10/20이다.** h=1은 2026-09-19에 접었다
(검정력 부족). label 생성 명령은 1/3/5/10/20을 그대로 만든다.

| Exit | 의미 |
|---:|---|
| 0 | 성공 |
| 2 | CLI/config 오류 |
| 3 | 입력 데이터 없음 또는 coverage 부족 |
| 4 | 데이터 품질 실패 |
| 5 | 외부 API/auth/rate-limit 실패 |
| 6 | schema/version 비호환 |
| 7 | look-ahead/survivorship invariant 위반 |
| 8 | 통계 또는 모델 검증 실패 |
| 9 | 부분 성공; 실패 partition 존재 |

## 9. 상태 코드

공통 상태는 `SUCCESS`, `PARTIAL`, `FAILED`, `NOT_APPLICABLE`, `PENDING`이다. 상세 코드는 다음을 사용한다.

| 코드 | 처리 |
|---|---|
| `AUTH_FAILED`, `RATE_LIMITED`, `API_PARTIAL_PAGE` | ingest 실패/재시도 |
| `STALE_DATA`, `MISSING_BARS`, `DUPLICATE_KEY` | 품질 보고, 신호 제외 가능 |
| `UNIVERSE_INCOMPLETE`, `UNKNOWN_SECURITY` | replay 날짜 차단 |
| `MARKET_RULE_UNRESOLVED` | 실행 차단 |
| `LOOKAHEAD_DETECTED`, `SURVIVORSHIP_RISK` | hard fail |
| `ENTRY_UNAVAILABLE`, `EXIT_UNAVAILABLE` | label nullable, 원인 보존 |
| `PENDING_MATURITY` | 미래 horizon 대기 |
| `NO_SIGNIFICANT_EDGE`, `INSUFFICIENT_EFFECTIVE_N` | 결과 표시 제한 |
| `CALIBRATION_FAILED`, `OOS_FAILED` | 모델 publish 차단 |

## 10. 편향 방지 불변식

1. 모든 input row의 `available_at <= signal_at`.
2. 분봉은 `bar_end_at <= signal_at`.
3. universe membership의 `effective_from <= signal_at < effective_to`이며 필요 시 `known_at <= signal_at`.
4. 순위 모집단은 그 날짜의 같은 index, 같은 config, 성공 상태 종목이다.
5. 진입은 `entry_at > signal_at`; 기본값은 다음 거래일 시가다.
6. label과 benchmark future rows는 feature store에 join되지 않는다.
7. split 경계에서 horizon embargo를 두어 label 기간 중첩을 막는다.
8. 조정주가를 사용할 때 조정계수의 실제 가용 시점을 기록하거나 연구용 시계열임을 분리한다.

## 11. 버전과 lineage

각 artifact는 다음을 독립 기록한다.

- `scanner_version`: 계산식과 Gate
- `market_rule_version`: 세션 및 시장제도
- `schema_version`: 열과 의미
- `data_revision`: 공급자 정정 반영 세대
- `universe_version`: 구성종목 이력 revision
- `label_version`: 진입/청산/비용 정의
- `experiment_version`: 선택·포트폴리오·통계 설정
- `model_version`: 학습 artifact
- `config_hash`, `code_revision`, 입력 partition hash

호환되지 않는 major schema는 in-place rewrite하지 않고 새 경로에 작성한다.

## 12. 테스트 전략

- V1.5 결과와 adapter의 골든 회귀 비교
- 세션 경계 15:29/15:30/15:35/16:00/20:00 테스트
- 시장제도 시행일 전후 rule resolution 테스트
- 미래 bar와 미래 구성종목을 주입한 hard-fail 테스트
- 부분 pagination과 중복 cache update 테스트
- synthetic clustered return에서 naive CI보다 cluster CI가 넓어지는지 검증
- replay 신호에서 forward label을 제거해도 feature hash가 같은지 검증
- backtest ledger/NAV/비용 회계 property test
- 시간 split과 embargo 누출 검사
- **되짚은 V1.5 점수·Gate가 원본과 같은지** 검증(Stage 8 ablation의 전제)
- 검정력 계산에서 `UNDERPOWERED`와 `NO_SIGNIFICANT_EDGE`가 갈리는지 검증
- 한 `run_id`로 여러 실험을 써도 서로 덮어쓰지 않는지 검증

## 13. 운영 관측성

각 run은 종목 수, 성공/부분/실패 수, cache freshness, 마지막 input bar, universe coverage, feature null률, 신호 수, label maturity, API retry를 구조화 로그로 남긴다. 데이터 품질 실패와 통계적 증거 부족을 시스템 장애와 구분한다.

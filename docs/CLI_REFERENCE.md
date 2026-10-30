# CLI 레퍼런스

**이 파일은 `python cvdobv.py cli reference`가 만든다 — 손으로 고치지 말 것.**

실행 형태는 `python cvdobv.py <그룹> <동작> [옵션]` 하나다.

## 그룹

| 그룹 | 설명 |
|---|---|
| [`ablation`](#ablation) | 성분 제거와 검정력(Stage 8) |
| [`backtest`](#backtest) | 포트폴리오 백테스트(Stage 7) |
| [`baseline`](#baseline) | 기준선 동결·검증(Stage 0) |
| [`baserate`](#baserate) | 무조건부 기준선(Stage 1) |
| [`benchmark`](#benchmark) | 벤치마크 지수 일봉(Stage 1) |
| [`calendar`](#calendar) | 거래 캘린더(Stage 1) |
| [`candidate`](#candidate) | 후보 판정 게이트(통계+경제성+안정성) |
| [`cli`](#cli) | CLI 문서 생성·검사 |
| [`contract`](#contract) | 스키마 계약 변환(Stage 0) |
| [`daily-cache`](#daily-cache) | 일봉 이력 수집·검증(Stage 1) |
| [`daily-gate`](#daily-gate) | Daily Gate 백테스트(Stage 2) |
| [`daily-signal`](#daily-signal) | 일봉 패널 → 신호(16년 포트폴리오용) |
| [`data-load`](#data-load) | 일일 적재 — 16:10 잠정 일봉, 익일 08:30 정규장 확정(봉 기준 bars-1.0) |
| [`dependence`](#dependence) | 두 유니버스의 의존성 측정 |
| [`features`](#features) | 일봉 feature(Stage 2) |
| [`forward`](#forward) | forward return 패널(Stage 1) |
| [`minute-cache`](#minute-cache) | 분봉 cache(Stage 4) |
| [`power`](#power) | 검정력과 검출 가능 구간 |
| [`replay`](#replay) | 과거 스캐너 replay(Stage 5) |
| [`signal-gate`](#signal-gate) | 신호 성과 평가(Stage 6) |
| [`signal-label`](#signal-label) | 신호별 forward return(Stage 6) |
| [`trade-sides`](#trade-sides) | KIS 매수/매도 체결량(FHKST03010800) 일일 적재 |
| [`universe`](#universe) | 유니버스 이력(Stage 3) |

## `ablation`

### `ablation run`

성분을 빼 보고 MDE(잡을 수 있었던 최소 효과)를 낸다

> 되짚은 점수·Gate가 V1.5와 같은지 먼저 확인하고, 다르면 거기서 멈춘다 —
> 조립 규칙이 틀렸다면 ablation 전부가 무의미하다.

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--variants` |  | 쉼표 구분. 기본 전체 (가능: BASE,NO_GATE,NO_CVD,NO_OBV,NO_PRICE,NO_LIQUIDITY,NO_CVD_DELTA,NO_CVD_MOMENTUM,NO_CVD_BULL_RATIO) |
| `--horizon` | `20` |  |
| `--index` | `KOSPI200` |  |
| `--start` |  |  |
| `--end` |  |  |
| `--resamples` | `1000` |  |
| `--seed` | `20260919` |  |
| `--config-hash` |  | 평가할 replay 설정(config_hash). 한 지수에 설정이 둘 이상이면 필수 — bdays나 period가 다른 실행이 한 백분위 모집단으로 섞이면 점수 재구성이 통째로 어긋난다 |
| `--signal-source` | `REPLAY` | 기본 REPLAY — ablation은 V1.5 점수를 재구성하므로 CVD가 있는 신호에서만 뜻이 있다 |
| `--out` |  | 결과 JSON 경로 |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

## `backtest`

### `backtest run`

선택·체결 정책을 넣어 돌린다

> 신호 생성과 체결은 분리돼 있다. 진입가는 Stage 6 label의 다음 거래일
> 시가이고, 신호 시각 이전 체결이 있으면 예외로 끝난다.

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--policies` | `BUY_ALL` | 쉼표 구분 (가능: BUY_ALL,BUY_TOP3,BUY_TOP5,BUY_TOP7,BUY_TOP10,BUY_TOP20,SCORE_Q5,SCORE_Q1,RANK_PCT10,RANK_PCT25,RANDOM,NO_GATE,GATE_FAIL) |
| `--horizon` | `20` |  |
| `--max-positions` | `20` |  |
| `--max-entries-per-day` | `AUTO` | 기본 AUTO = 슬롯수/horizon. 진입일이 몰리는 것을 막는다 |
| `--single-cohort` |  | 하루 진입 상한을 없앤다 — 첫날 슬롯을 다 채우고 h일마다 통째로 교체하는 단일 코호트가 된다 |
| `--allow-overlap` |  | 보유 중인 종목을 다시 살 수 있게 한다 |
| `--liquidity-cap` | `0.01` | 한 종목 진입 금액 <= 20일 평균 거래대금 x 이 값 |
| `--no-liquidity-cap` |  |  |
| `--capital` | `100000000.0` |  |
| `--index` | `KOSPI200` |  |
| `--start` |  |  |
| `--end` |  |  |
| `--oos-start` |  | 이 날부터 out-of-sample로 따로 집계한다 |
| `--signal-source` |  | 신호 출처. 표에 둘 이상 섞여 있으면 필수다 — 증거 계층이 다른 신호를 한 덩어리로 세지 않는다 |
| `--random-seed` | `20260921` | RANDOM 대조군의 seed. 여러 개 돌려 분포를 봐야 한다 — 한 번의 무작위도 표본 하나일 뿐이다 |
| `--resamples` | `1000` |  |
| `--seed` | `20260919` |  |
| `--out` |  | 결과 JSON 경로 |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

## `baseline`

### `baseline freeze`

골든 샘플과 manifest 생성

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--golden` |  | 골든 샘플 파일 경로 |
| `--manifest` |  | manifest 파일 경로 |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

### `baseline verify`

현재 코드와 기준선 비교

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--golden` |  |  |
| `--manifest` |  |  |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

## `baserate`

### `baserate compute`

상승확률 기준선과 신뢰구간

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--horizons` | `1,3,5,10,20` |  |
| `--outcomes` |  | 기본: up,up_gross |
| `--universe` | `KOSPI200` |  |
| `--start` |  |  |
| `--end` |  |  |
| `--resamples` | `1000` |  |
| `--seed` | `20260918` |  |
| `--out` |  | 결과 JSON 경로 |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

## `benchmark`

### `benchmark build`

지수 일봉 수집

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--benchmarks` |  | 쉼표 구분. 기본 KRX:KOSPI200 (가능: KRX:KOSDAQ, KRX:KOSPI, KRX:KOSPI200) |
| `--start` |  | YYYY-MM-DD |
| `--end` |  | YYYY-MM-DD |
| `--source` |  | 적재 원천(기본 환경변수 CVD_MARKET_SOURCE, 없으면 kis) |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

## `calendar`

### `calendar build`

일봉에서 캘린더를 파생해 저장

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

## `candidate`

### `candidate gate`

다섯 축으로 판정 — 하나라도 FAIL이면 FAIL

> 선택지 C 후보는 q=0.020으로 FDR을 통과했지만 20거래일에 +0.0802%p다.
> "유의하다"와 "거래할 가치가 있다"는 다른 질문이고, 두 번째 질문을 사람이
> 문장으로만 적으면 다음 후보에서 기준이 또 흔들린다.

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--candidates` |  | 후보 JSON(목록 또는 {"candidates": [...]}) |
| `--alpha-q` | `0.05` |  |
| `--min-annual-excess-bp` | `100.0` | 비용 후 최소 연 초과수익(bp) |
| `--min-observed-to-mde` | `1.0` | 관측 효과가 MDE의 몇 배 이상이어야 하는가 |
| `--min-daily-value-krw` | `1000000000.0` |  |
| `--max-participation-rate` | `0.01` |  |
| `--out` |  | 결과 JSON 경로 |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

## `cli`

### `cli reference`

파서에서 CLI 레퍼런스 문서를 만든다

> 손으로 적은 명령 목록은 **반드시 코드와 어긋난다.** 실제로 어긋나 있었다
> (`cvdobv ...`와 `python cvdobv.py ...`가 섞여 있고 계획 명령과 구현
> 이름이 달랐다). 파서가 유일한 출처가 되게 한다.

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--out` | `docs/CLI_REFERENCE.md` |  |
| `--check` |  | 쓰지 않고 코드와 일치하는지만 확인 |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

## `contract`

### `contract convert`

V1.5 live JSON → V2 gold 레코드

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| (위치)snapshot |  | V1.5 스냅샷 JSON 경로 |
| `--out` |  | 출력 JSON 경로 |
| `--signal-source` | `LIVE` |  |
| `--code-revision` |  | 미지정 시 기준 구현 파일 해시를 쓴다 |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

## `daily-cache`

### `daily-cache build`

일봉을 수집해 Parquet에 저장

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--index` | `KOSPI200` |  |
| `--index-file` |  |  |
| `--codes` |  | 쉼표로 구분한 종목코드. 지정하면 --index를 무시한다 |
| `--limit-securities` |  | 앞에서 N종목만 — 소규모 실측용 |
| `--start` |  | YYYY-MM-DD |
| `--end` |  | YYYY-MM-DD |
| `--max-pages` | `120` |  |
| `--raw-prices` |  | 수정주가 대신 원본 가격(키움 upd_stkpc_tp=0, KIS FID_ORG_ADJ_PRC=1) |
| `--source` |  | 적재 원천(기본 환경변수 CVD_MARKET_SOURCE, 없으면 kis) |
| `--flush-every` | `20` | N종목마다 Parquet으로 내려쓴다(메모리 상한) |
| `--repair-failed` |  | 이전 실행에서 불완전하게 수집된 종목만 다시 받는다. --run-id를 이전보다 나중 값으로 주어야 새 데이터가 우선된다 |
| `--retries` | `2` | 종목별 재시도 횟수. 끝내 실패하면 그 종목은 저장하지 않는다(잘린 시계열 방지) |
| `--skip-existing` |  | 이미 저장된 종목을 건너뛴다(중단된 수집 재개용). 행 존재 여부로만 판단하므로 부분 수집 종목도 건너뛸 수 있다 |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

### `daily-cache krx-panel`

KRX 패널에서 일봉 생성(상장폐지 종목용)

> 키움에는 폐지 종목이 없다. KRX 일별매매정보에는 있지만 수정주가가 아니라서
> 분할을 직접 보정한다. 보정 기준은 "주식수 k배 · 주가 1/k배 · **시가총액
> 그대로**"이며, 시가총액이 함께 달라지는 자본변동(인적분할·유상증자)은
> 보정하지 않고 표시만 남긴다.

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--codes` |  | 쉼표로 구분한 종목코드 |
| `--missing-file` |  | 누락 종목 JSON(`live`/`delisted` 키) |
| `--delisted-only` |  | --missing-file의 delisted 그룹만 처리 |
| `--start` |  |  |
| `--end` |  |  |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

### `daily-cache nxt-history`

9/14 전 정규장 일봉에 NXT 메인마켓 거래량(NXT 30분봉)을 더한다

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--start` |  | 기본 2025-10-01(30분봉 시작) |
| `--end` |  | 이 날 전까지(기본 2026-09-14) |
| `--codes` |  | 쉼표로 구분한 종목코드(기본: 그 기간 KIS NX 체결이 있는 종목) |
| `--no-fetch` |  | 30분봉을 받지 않고 저장분으로만 다시 만든다 |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

### `daily-cache provisional`

당일 1분봉 합으로 잠정 정규장 일봉(16:00 이후, 거래량 근사, 리비전 0)

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--date` |  | 기본 오늘 |
| `--start` |  | 구간 모드 — 이 날부터 --end(기본 오늘)까지 저장소가 아는 거래일 |
| `--end` |  |  |
| `--missing-only` |  | 확정 봉도 잠정 봉도 없는 종목만(밀린 날 메우기) |
| `--index` | `KOSPI200,KOSDAQ150` |  |
| `--codes` |  | 쉼표로 구분한 종목코드 |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

### `daily-cache readjust`

수정주가 소급 감지 → 그 종목 일봉 이력 전체 재수집·파생 봉 재생성

> 1) 감지: 같은 날을 다시 받았는데 종가가 바뀐 종목(`adjustment_watch`). `--audit`이면
>    지금 적재 원천(기본 KIS) 값과 저장값을 직접 대조(종목당 1회 호출) — 한 번만 받은 이력도 본다
> 2) 그 종목의 일봉 이력 전체를 다시 받는다(`daily-cache build --codes`)
> 3) 9/14 이후 복원 봉: 시가가 원본과 달라진 날을 다시 만든다(`regular-from-minute`)
> 4) 9/14 이전 NXT 포함 봉: 기준 행이 바뀌었으면 다시 만든다(`nxt-history --no-fetch`)

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--since` |  | 감지 구간 시작(기본 30일 전) |
| `--codes` |  | 쉼표로 구분한 종목코드(기본 현재 구성종목) |
| `--audit` |  | 지금 적재 원천(기본 KIS) 값과 저장값을 직접 대조(종목당 1회 호출) |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

### `daily-cache regular-from-minute`

2026-09-14 이후 EXTENDED 일봉 → 1분봉으로 정규장(REGULAR) 일봉 복원

> 규칙과 근거는 `ingestion/regular_from_minute.py` 머리말. 원본 EXTENDED 행은
> 그대로 두고 `price_scope=REGULAR, source=DERIVED_MINUTE_1M` 행을 추가한다.
> 이미 REGULAR가 있는 날은 건드리지 않으므로 여러 번 돌려도 된다.

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--start` |  | 기본 2026-09-14 |
| `--end` |  | 기본 오늘 |
| `--codes` |  | 쉼표로 구분한 종목코드 |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

### `daily-cache validate`

저장된 일봉 불변식 점검

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

## `daily-gate`

### `daily-gate backtest`

조건별 상승확률과 기준선 대비 차이

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--variants` |  | 쉼표 구분. 기본 전체 (가능: ALL,PRICE,OBV,LIQUIDITY,PRICE+OBV,PRICE+LIQ,OBV+LIQ,GATE_ALL) |
| `--horizons` | `5,20` | h=1은 접었다 — 검정력이 모자란다(--horizons로 직접 지정하면 경고와 함께 계산은 한다) |
| `--outcomes` | `up,up_gross` |  |
| `--feature-version` |  |  |
| `--universe` | `KOSPI200` |  |
| `--no-membership` |  | 유니버스 이력 필터를 끈다(모집단이 '한 번이라도 지수에 있었던 종목 전체'가 된다) |
| `--segment` |  | 세그먼트 이름표(결과에 기록만 한다) |
| `--segment-by` |  | 연도·시장국면·유동성 구간으로 쪼개 평가한다 |
| `--base-variant` | `ALL` | 짝지음 차이를 무엇과 견줄 것인가. 기본 ALL(그날 전체). PRICE로 두면 '가격 위에 더하는 것이 있는가'를 묻는 중첩 비교가 된다 |
| `--cost-multiplier` | `1.0` | 비용 민감도. 2.0이면 수수료·세금·슬리피지를 두 배로 |
| `--start` |  |  |
| `--end` |  |  |
| `--resamples` | `1000` |  |
| `--seed` | `20260918` |  |
| `--out` |  | 결과 JSON 경로 |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

## `daily-signal`

### `daily-signal build`

일봉 feature에서 Gate 신호를 만든다

> Stage 7 엔진은 `gold.signals`를 읽는데, 그 표는 분봉 캐시가 있는
> 252거래일짜리 replay가 만든 것이다. 그래서 포트폴리오 시뮬레이션이
> **코스피200이 +150% 오른 한 구간**에서만 돌았다.
>
> 일봉 패널은 2010-01-04부터 4,114거래일이 있고 가격·OBV·유동성 조건은
> 분봉 없이 계산된다. 이 명령이 그 사이를 잇는다.
>
> **점수는 만들지 않는다.** V1.5 총점은 CVD를 포함하므로 일봉만으로
> 재현할 수 없고, 지어내면 검증이 아니라 새 지표 만들기가 된다.

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--index` | `KOSPI200` |  |
| `--variant` | `PRICE+OBV` | Gate 조합 (기본: 선택지 C 후보) |
| `--start` |  | YYYY-MM-DD |
| `--end` |  | YYYY-MM-DD |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

## `data-load`

### `data-load daily`

확정 — 익일 08:30(애프터마켓 확정 뒤면 언제든). 밀린 날을 정규장 복원

> 애프터마켓 확정(20:05) 뒤면 언제 돌려도 된다. 마지막 확정 이후(장부
> `data/manifests/data_load_state.json`) 비어 있는 날을 스스로 찾는다.

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--days` |  | 최근 몇 거래일을 확인할지(기본: 장부의 마지막 확정일부터) |
| `--since` |  | 이 날짜 이후(YYYY-MM-DD) |
| `--index` | `KOSPI200,KOSDAQ150` |  |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

### `data-load provisional`

잠정 — 당일 16:10. 밀린 날을 먼저 확정하고 오늘 1분봉으로 잠정 일봉

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--days` |  | 최근 몇 거래일을 확인할지(기본: 장부의 마지막 확정일부터) |
| `--since` |  | 이 날짜 이후(YYYY-MM-DD) |
| `--index` | `KOSPI200,KOSDAQ150` |  |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

## `dependence`

### `dependence measure`

같은 날짜 블록으로 동시 재표집 — rho와 결합 추정

> 따로 재표집하면 그 순간 독립이 가정으로 들어가고, 그 가정이 바로
> 검사하려는 것이다.

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--series-a` |  | {날짜: 값} JSON |
| `--series-b` |  |  |
| `--name-a` | `KOSPI200` |  |
| `--name-b` | `KOSDAQ150` |  |
| `--block-length` | `20` | 보유기간 이상이어야 한다 |
| `--resamples` | `2000` |  |
| `--seed` | `20260921` |  |
| `--out` |  | 결과 JSON 경로 |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

## `features`

### `features build`

일봉 지표와 Gate 조건 생성

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--obv-lookback` | `20` | OBV slope 계산 구간(거래일). feature_version에 포함된다 |
| `--start` |  | YYYY-MM-DD |
| `--end` |  | YYYY-MM-DD |
| `--securities` |  |  |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

## `forward`

### `forward build`

무조건부 forward return 생성

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--horizons` | `1,3,5,10,20` |  |
| `--entry-rule` | `NEXT_OPEN` | NEXT_OPEN=익일 시가, NEXT_CLOSE=익일 종가(마감 동시호가 MOC), SAME_CLOSE=당일 종가(시간외 종가매매) |
| `--start` |  | YYYY-MM-DD |
| `--end` |  | YYYY-MM-DD |
| `--securities` |  | 쉼표로 구분한 security_id(예: KRX:005930) |
| `--benchmark` |  | 초과수익 계산에 쓸 benchmark_id(예: KRX:KOSPI200). 없으면 초과수익은 null로 남는다 |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

## `minute-cache`

### `minute-cache build`

API 가용 구간 전체 분봉 수집

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--index` | `KOSPI200` |  |
| `--index-file` |  |  |
| `--codes` |  |  |
| `--limit-securities` |  |  |
| `--period` | `1` | 분봉 주기(분). 기본 1 — 봉 기준 bars-1.0은 1분봉만 적재하고 다른 주기는 합성한다 |
| `--end` |  | 이 거래일까지만 저장(YYYY-MM-DD). ka10080은 호출 시점까지 주므로 장중에 돌리면 오늘의 반쪽 세션이 섞인다. 기본은 자르지 않는다 |
| `--max-pages` |  |  |
| `--flush-every` | `10` |  |
| `--retries` | `2` |  |
| `--skip-existing` |  | 이미 저장된 종목을 건너뛴다(중단된 수집 재개용) |
| `--source` |  | 적재 원천(기본 환경변수 CVD_MARKET_SOURCE, 없으면 kis). kis는 1분봉만 준다 |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

### `minute-cache catchup`

작업일 기준으로 비어 있는 분봉만 수집(일일 누적, 늦게 돌려도 됨)

> 하루 이틀 늦게 돌려도 되는 것이 요구사항이다. 그래서 "어제"를 기준으로
> 삼지 않고 **커버리지와 API가 주는 거래일을 대조**한다.

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--period` | `1` | 분봉 주기(분). 기본 1 |
| `--days` | `10` | 최근 몇 거래일을 확인할지(기본 10) |
| `--since` |  | 이 거래일 이후만 확인(YYYY-MM-DD) |
| `--index` | `KOSPI200,KOSDAQ150` | 쉼표 구분(기본 KOSPI200,KOSDAQ150) |
| `--venue` | `KRX` | KRX(기본) 또는 NXT(넥스트레이드 봉, KIS NX·키움 <코드>_NX) |
| `--source` |  | 적재 원천(기본 환경변수 CVD_MARKET_SOURCE, 없으면 kis) |
| `--codes` |  | 지수 대신 종목코드 직접 지정(쉼표 구분) |
| `--limit-securities` |  |  |
| `--min-completeness` | `0.8` | 이 비율 미만이면 그날은 다시 받는다(기본 0.8) |
| `--probe-code` | `005930` | 거래일 확인용 기준 종목(기본 005930) |
| `--recheck` |  | 재시도 억제 장부를 무시하고 기준 미달인 날을 모두 다시 받는다(장부도 갱신하지 않는다) |
| `--as-of` |  | 기준시각 고정 "YYYY-MM-DD HH:MM" |
| `--flush-every` | `10` |  |
| `--retries` | `2` |  |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

### `minute-cache cum-value`

과거 1분봉에 누적거래대금(KIS) 채우기 — 다시 받아 덮는다, 재개 가능

> 끊겨도 다시 돌리면 남은 (종목, 날짜)만 받는다. KIS는 약 1년만 거슬러 준다.

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--start` |  | 기본 2026-09-01 |
| `--end` |  | 기본 어제 |
| `--venue` | `KRX,NXT` | 쉼표 구분(기본 KRX,NXT) |
| `--codes` |  | 쉼표로 구분한 종목코드 |
| `--limit-securities` |  |  |
| `--workers` | `4` | 동시에 받는 종목 수(기본 4 — KIS 초당 한도 안) |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

### `minute-cache slice`

as_of 시점 분봉 잘라 보기

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| (위치)security |  | 종목코드 또는 KRX:코드 |
| `--as-of` |  | "YYYY-MM-DD HH:MM" |
| `--bdays` | `5` |  |
| `--period` | `5` |  |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

### `minute-cache validate`

커버리지·완결성 점검

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--period` | `5` |  |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

## `power`

### `power frontier`

보유기간 x 선택 크기별로 잡을 수 있는 최소 효과

> 가설 검정이 아니라 설계의 성질을 재는 것이다. 데이터를 보고 가설을 고르는
> 것과 무관하므로 다중비교 문제도 없다.

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--horizons` | `5,10,20` |  |
| `--sizes` | `1,3,5,10,20,50` | 하루에 고를 종목 수 |
| `--project-to` | `4114` | 이만큼의 거래일이 있었다면 얼마까지 잡히나 (기본 4,114 = 2010~2026 일봉 패널 길이) |
| `--index` | `KOSPI200` |  |
| `--resamples` | `1000` |  |
| `--seed` | `20260919` |  |
| `--out` |  | 결과 JSON 경로 |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

### `power plan`

확증에 새로 얼마가 필요한가

> 이미 본 표본을 다시 세면 같은 우연을 두 번 세는 것이다. 그래서 누적이
> 아니라 추가분으로 계산한다.

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--results` | `reports/prereg-c/h1-*.json` | 확증 대상 결과 JSON(글롭) |
| `--only` |  | 이 조합만. 쉼표 구분 (예: PRICE+OBV) |
| `--alpha` | `0.05` |  |
| `--power` | `0.8` |  |
| `--days-per-year` | `245` |  |
| `--out` |  | 결과 JSON 경로 |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

## `replay`

### `replay run`

거래일별 15:35 replay

> 그날의 구성종목은 유니버스 이력에서 온다 — 오늘의 스냅샷 파일을 읽지
> 않는다. 분봉은 `bar_end <= 15:35`인 것만 넘어간다.

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--index` | `KOSPI200` |  |
| `--start` |  | YYYY-MM-DD |
| `--end` |  |  |
| `--period` | `5` | V1.5 계산 해상도(분). 원천은 1분봉 합성(봉 기준 bars-1.0) |
| `--bdays` | `5` |  |
| `--daily-scope` | `regular` | 기본 regular(정규장 일봉, 봉 기준 bars-1.0). 2026-10-03 이전 결과 재현은 auto |
| `--minute-source` | `standard` | standard=1분봉에서 --period 합성(기본), native=저장된 원본 N분봉(과거 결과 재현용) |
| `--decision` | `AFTER_CLOSE` | AFTER_CLOSE=15:35 판정(연구 규약), AUCTION=마감 -1분 판정(당일 종가 매수용). AUCTION은 정규장이 미완이라 CVD 성분이 온전하지 않다 |
| `--rank-session` |  |  |
| `--out` |  | 요약 JSON 경로 |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

## `signal-gate`

### `signal-gate evaluate`

BUY_CANDIDATE vs WATCH, 점수·순위 단조성

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--variants` |  | 쉼표 구분. 기본 전체 (가능: ALL,BUY_CANDIDATE,WATCH,SCORE_Q1,SCORE_Q2,SCORE_Q3,SCORE_Q4,SCORE_Q5,RANK_TOP3,RANK_TOP5,RANK_TOP10,RANK_TOP20) |
| `--horizons` | `5,20` | h=1은 접었다 — 검정력이 모자란다 |
| `--outcomes` | `up,up_gross` |  |
| `--index` | `KOSPI200` |  |
| `--segment-by` |  | 시장국면·월로 쪼개 평가한다 (replay 구간이 1년이라 연도는 쓸 수 없다) |
| `--start` |  |  |
| `--end` |  |  |
| `--resamples` | `1000` |  |
| `--seed` | `20260919` |  |
| `--signal-source` |  | 신호 출처. 표에 둘 이상 섞여 있으면 필수다 — REPLAY는 분봉 252거래일(CVD 포함), DAILY_PANEL은 일봉 4,115거래일(CVD 없음) |
| `--out` |  | 결과 JSON 경로 |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

## `signal-label`

### `signal-label build`

신호에 진입·청산 수익률을 붙인다

> 수익률을 다시 계산하지 않는다 — 무조건부 패널에서 그대로 가져온다.
> 그래서 기준선과 신호의 숫자가 같은 계산에서 나온다.

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--index` | `KOSPI200` |  |
| `--horizons` | `1,3,5,10,20` |  |
| `--signal-source` |  | 기본은 전부. replay만 보려면 REPLAY |
| `--start` |  | YYYY-MM-DD |
| `--end` |  | YYYY-MM-DD |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

## `trade-sides`

### `trade-sides daily`

16:10 오늘 잠정, 익일 08:30 어제 확정, 밀린 날 메우기(기본 UN)

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--market-div` | `UN` | 쉼표 구분 J,NX,UN (기본 UN = KRX+NXT 통합) |
| `--index` | `KOSPI200,KOSDAQ150` |  |
| `--since` |  | 이 날짜부터 확정 미완 거래일을 본다(기본 30일 전) |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

## `universe`

### `universe build`

스냅샷 → membership 구간

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--index` | `KOSPI200` |  |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

### `universe import-csv`

KRX 구성종목 CSV를 스냅샷으로

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| (위치)csv |  | KRX에서 내려받은 CSV 경로 |
| `--index` | `KOSPI200` |  |
| `--as-of` |  | 이 CSV가 어느 날짜 구성종목인가 (YYYY-MM-DD) |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

### `universe krx-cache`

KRX Open API 일별 응답 캐시(검산 재료)

> 구간 조회가 없는 API라 거래일마다 2회씩 부른다. 과거 데이터는 확정값이므로
> 한 번 받아 두면 다시 받지 않는다.

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--start` |  | YYYY-MM-DD |
| `--end` |  | YYYY-MM-DD |
| `--workers` | `4` |  |
| `--market` | `KOSPI200` | 받을 시장. 코스닥 종목 명부를 만들려면 코스닥 일별매매정보도 캐시에 있어야 한다 |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

### `universe members`

특정 시점 구성종목 조회

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--index` | `KOSPI200` |  |
| `--as-of` |  | YYYY-MM-DD |
| `--max-gap-days` |  | 경계 불확실성이 이보다 크면 조회를 막는다 |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

### `universe notices`

KRX 지수운영공지·첨부 수집

> `index.krx.co.kr`은 로그인이 필요 없다. 첨부 엑셀에 편입·편출과 변경 후
> 구성종목 전량이 들어 있다.

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--start-year` | `2009` |  |
| `--end-year` | `2026` |  |
| `--no-attachments` |  |  |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

### `universe rebuild`

항등식 검산으로 과거 구성종목 복원

> 매 거래일 시총 항등식으로 검산한다. 0원이 아니면 그날 구성종목이 바뀐
> 것이고, 공시 델타·전체 명단·역산 순으로 메운 뒤 다시 검산한다.

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--index` | `KOSPI200` |  |
| `--start` |  | 기본 2010-01-04(Open API 시작일) |
| `--end` |  |  |
| `--anchor-window` | `20` | 앵커 시행일을 검산으로 찾을 때 볼 거래일 폭. 공지는 시행 2~3주 전에 나오므로 기본 20거래일 |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

### `universe refresh`

구성종목 이력 정기 갱신(16:10 적재 마지막 단계) — 스냅샷→검산 재료→공지→복원→재검산→index_stocks.json

> 순서: 달력 연장 → 오늘 KIS 스냅샷 → KRX 검산 재료 → 지수운영공지 → 복원 시험
> (dry-run) → 두 지수 모두 미해결 0이면 복원 기록 → 최근 구간 재검산 →
> `index_stocks.json` 갱신.
>
> - 이력 끝은 KRX 자료가 있는 마지막 거래일(`refresh_end`, 16:10이면 어제)이다
> - 복원에 미해결이 있으면 **아무것도 쓰지 않는다** — rebuild는 미해결이 있어도
>   새 버전을 쓰므로 먼저 dry-run으로 본다. 구성 변경 시행일 당일은 KRX 자료가
>   아직 없어 하루 미해결로 나올 수 있고, 다음 날 실행이 메운다
> - 재검산이 틀리면 `index_stocks.json`을 바꾸지 않는다

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--index` | `KOSPI200,KOSDAQ150` |  |
| `--as-of` |  | 스냅샷 날짜(기본 오늘). KIS는 언제나 현재 구성을 준다 |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

### `universe security-master`

상장폐지 포함 종목 명부(스키마 §5)

> 키움 종목 목록에는 폐지 종목이 없다. KRX 일별매매정보는 그날 상장돼 있던
> 종목을 그날 기준으로 주므로, 하루씩 훑으면 사라진 종목도 사라진 날짜와 함께
> 남는다.

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--start` |  |  |
| `--end` |  |  |
| `--market` | `KOSPI200` | 훑을 시장. BOTH는 유가증권+코스닥을 **같은 패널 경계**로 한 번에 훑는다 |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

### `universe snapshot`

오늘 구성종목을 스냅샷으로 저장

> 키움은 과거 구성종목을 주지 않는다. 오늘 저장해 두면 내일부터 이력이 된다.

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--index` | `KOSPI200` |  |
| `--as-of` |  | 관측 날짜(기본 오늘). 과거 날짜를 넣어도 KIS·키움 모두 현재 구성종목을 준다는 점에 주의 |
| `--source` |  | 원천(기본 환경변수 CVD_MARKET_SOURCE, 없으면 kis). kiwoom은 코스닥150을 찍지 못한다 |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

### `universe validate`

이력 커버리지 점검

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--index` | `KOSPI200` |  |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

### `universe verify`

적재된 이력을 다시 검산

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-h`, `--help` | `==SUPPRESS==` | show this help message and exit |
| `--index` | `KOSPI200` |  |
| `--start` |  |  |
| `--end` |  |  |
| `--sample` |  | 구간에서 N일만 뽑아 검산(0이면 전부) |
| `--run-id` |  | 실행 ID를 고정한다(미지정 시 자동 생성) |
| `--config` |  | 설정 파일 경로 |
| `--log-json` |  | 사람이 읽는 출력 대신 JSON 한 줄 |
| `--dry-run` |  | 파일을 쓰지 않고 결과만 확인 |

# 봉 리비전 조회 정리(`latest_by`) — 진행사항 및 결과

작성일: 2026-09-30
범위: `silver.daily_bars`·`silver.minute_bars`를 읽는 모든 reader
상태: 코드·테스트 완료, **커밋 전**(src/·tests/ 전체를 나중에 한꺼번에 올릴 예정)

## 1. 요약

| 항목 | 결과 |
|---|---|
| 문제 | 저장 PK에 `data_revision`이 들어 있어 `dedupe_pk=DAILY_BAR_PK`/`MINUTE_BAR_PK`로 읽으면 같은 봉의 리비전 1과 2가 **둘 다** 남는다 |
| 조치 | "현재 값"이 필요한 reader 9곳을 `latest_by=`(논리 키)로 바꿨다. 집합만 보는 reader 3곳은 그대로 두고 이유를 주석으로 남겼다 |
| 테스트 | 회귀 테스트 11건 추가. 수정 전 코드에서는 reader 테스트 10건이 모두 실패하고, 수정 후에는 전부 통과한다. 전체 스위트 **906 passed** |
| 실데이터 영향 | **없음.** 저장된 봉은 전부 리비전 1이다. 봉 조회와 B·A 사전등록 백테스트, 포트폴리오 엔진 결과가 수정 전과 같다 |
| DAILY_PANEL `SCORE_Q5` 거래 0건 | **설계된 동작이다.** 일봉 경로의 `total_score`가 823,604건 모두 null이라 점수 분위 정책은 매일 빈 결과를 낸다 |

## 2. 배경

`DAILY_BAR_PK = (security_id, trading_date, price_scope, adjustment_type, data_revision)`
`MINUTE_BAR_PK = (security_id, period_min, bar_start_at_utc, session, data_revision)`

이력을 보존하려고 저장 PK에 `data_revision`을 넣었다. 그래서 `read_arrow(dedupe_pk=저장 PK)`는
재수집 중복(같은 리비전)은 없애지만, **리비전이 다른 같은 봉**은 별개 행으로 남긴다.
`ParquetStore.read_arrow`에 추가된 `latest_by=`(논리 키 = 저장 PK에서 `data_revision`을
뺀 것)는 논리 키별로 리비전이 가장 큰 행 하나를 남긴다. 리비전이 같으면 나중 run_id 행을 남긴다.

`tools/closing_bet_scanner.py`와 `tools/ab_candidate_scanner.py`는 이미 `latest_by`를
쓰고 있었다. 이번 작업은 나머지 reader를 정리한 것이다.

## 3. reader별 판단

### 3.1 `latest_by`로 바꾼 곳 — 현재 값이 필요

| 위치 | 용도 | 리비전 2개일 때 수정 전 증상 |
|---|---|---|
| `src/cvd_obv/backtest/engine.py` `_load_marks` | 보유 종목 일별 평가가격 | 파일 순서에 따라 옛 리비전 종가가 dict를 덮어씀 |
| `src/cvd_obv/features/repository.py` `_load_series` | MA·OBV 계산용 일봉 | 같은 날이 두 번 들어가 rolling 창이 오염됨 |
| `src/cvd_obv/labels/repository.py` `_load_series` | forward return label | `BarSeries`가 "같은 거래일이 두 번" `ConfigError` 발생 |
| `src/cvd_obv/replay/windows.py` `MinuteWindow` | replay 분봉 | 같은 시각 봉이 중복돼 V1.5에 전달됨 |
| `src/cvd_obv/replay/windows.py` `DailyWindow` | replay 일봉 | 같은 날 봉이 중복됨 |
| `src/cvd_obv/ingestion/daily_history.py` `suspect_truncated_securities` | 페이지 잘림 의심 판정(행 수 기준) | 리비전까지 세어 행 수가 부풀고, 600 배수 판정이 빗나감 |
| `src/cvd_obv/ingestion/daily_history.py` `load()` | 일봉 조회·`validate()` 입력 | 같은 봉이 두 번 반환됨. `read()`에 `pk=DAILY_BAR_LOGICAL_KEY`를 줘서 해결 |
| `src/cvd_obv/ingestion/minute_history.py` `load_bars` | 분봉 조회·`slice_as_of` | 같은 봉이 중복됨 |
| `tools/ab_backtest_b.py` `load_all_bars` | B·A 사전등록 백테스트 일봉 | 같은 날이 중복돼 지표·수익률 오염. 테스트용으로 `store=None` 인자 추가(기존 호출은 그대로 동작) |

한 가지 의미 변화가 있다. **최신 리비전이 `FAILED`면 그 봉은 빠진다.** 옛 리비전으로
되돌아가지 않는다. "현재 값이 격리 상태"라는 뜻이므로 의도한 동작이며, 테스트로 고정했다.

### 3.2 그대로 둔 곳 — 집합만 봄

| 위치 | 이유 |
|---|---|
| `src/cvd_obv/ingestion/calendar_store.py` `derive` | (거래일, 종목) **집합**만 만든다. 리비전 수와 무관하다 |
| `src/cvd_obv/ingestion/daily_history.py` `existing_securities` | 종목 **집합** |
| `src/cvd_obv/ingestion/minute_history.py` `existing_securities` | 종목 **집합** |

세 곳 모두 이유를 한 줄 주석으로 남겼다.

## 4. 테스트

새 파일: `tests/unit/test_latest_revision_readers.py`(11건)

- 리비전 1은 run_id `run_z`, 리비전 2는 `run_a`로 쓴다. run_id 사전순이 리비전 순서와
  반대이므로, 리비전이 아닌 run_id나 파일 순서로 고르는 구현이면 옛 값이 나와 드러난다.
- 전제 확인 1건: `dedupe_pk=DAILY_BAR_PK`만으로는 두 리비전이 모두 남는다.
- reader별 9건 + `FAILED` 최신 리비전 1건.
- **수정 전 코드로 되돌려 실행한 결과, reader 테스트 10건이 모두 실패했다.** 실패 사유는
  옛 종가, 중복 행, `ConfigError`로 모두 리비전 문제다. 수정 후에는 11건 모두 통과한다.
- 전체 스위트: **906 passed**(추가 전 895 + 11)

## 5. 실데이터 검증

### 5.1 저장 데이터 현황

| 테이블 | 저장 행 | 리비전 분포 | run 수 |
|---|---:|---|---:|
| `silver.daily_bars` | 3,414,539 | 전부 1 | 15 |
| `silver.minute_bars` | 51,904,427 | 전부 1 | 14 |

일봉 논리 키는 3,328,940개다. 이 중 **83,944개는 두 개 이상의 run에 저장돼 있다**(재수집).
리비전이 모두 1이므로 수정 전 방식(저장 PK, 나중 run_id 우선)과 수정 후 방식(논리 키)이
같은 행을 고른다.

### 5.2 수정 전후 비교

"수정 전"은 현재 코드에서 `latest_by=K`를 `dedupe_pk=K+(data_revision,)`로 바꿔치기해
재현했다. 그 밖의 코드는 같다.

| 비교 대상 | 결과 |
|---|---|
| 일봉 조회(OHLCV·품질·run_id·리비전 전 열) | 3,328,940행, **완전 일치** |
| 분봉 조회(1분·5분, 연도별) | 47,160,181행, **완전 일치** |
| 후보 B 사전등록 백테스트(`ab_backtest_b.run`, 재표본 4,000) | `generated_at` 타임스탬프 외 **완전 일치** |
| 후보 A 사전등록 백테스트(`ab_backtest_a.run`) | `generated_at` 외 **완전 일치** |
| 포트폴리오 엔진(`BacktestEngine`, 4개 spec, dry run) | 거래·NAV 전체 JSON(약 400MB) **바이트 단위 일치** |

참고로, 변하지 않은 결과 수치는 다음과 같다.

- 후보 B: H1 평균 +1.09%, q=0.0000 → `SIGNIFICANT_EDGE` / H2 평균 +0.007%, q=0.97 → `UNDERPOWERED` / 억제 후 신호 6,136건
- 후보 A: H1 평균 +0.62%, q=0.64 / H2 평균 −3.52%, q=0.17 → 둘 다 `UNDERPOWERED` / 신호 2,364건
- 포트폴리오 엔진(KOSPI200, 슬롯 100):

| signal_source | 정책 | h | 일 진입 상한 | 거래 | 총수익 |
|---|---|---:|---:|---:|---:|
| REPLAY | BUY_ALL | 5 | 20 | 2,303 | +2.46% |
| REPLAY | SCORE_Q5 | 20 | 5 | 1,130 | +23.57% |
| DAILY_PANEL | BUY_ALL | 5 | 20 | 65,134 | −91.93% |
| DAILY_PANEL | SCORE_Q5 | 20 | 5 | **0** | 0.00% |

이 spec들은 비교용으로 고른 것이며 새 실험 결과가 아니다. 저장소에는 아무것도 쓰지 않았다.
DAILY_PANEL `BUY_ALL` h=5의 −91.9%(65,134건)는 `STAGE_STATUS_V2.md`에 이미 기록된 결과와
거래 수까지 같다. 해당 문서는 원인을 "h=5는 지표와 무관하게 전멸한다", 즉 왕복 비용
0.412%p × 연 49회 회전으로 설명한다.

## 6. DAILY_PANEL `SCORE_Q5` 거래 0건의 원인

### 6.1 데이터

`gold.signals`의 출처별 점수 결측 현황:

| signal_source | index | 신호 | `total_score` null | `rank` null | 기간 |
|---|---|---:|---:|---:|---|
| REPLAY | KOSPI200 | 150,866 | 0 | 3,052 | 2025-09-08~2026-09-22 |
| REPLAY | KOSDAQ150 | 74,420 | 0 | 1,350 | 2025-09-12~2026-09-22 |
| REPLAY_AUCTION | KOSPI200 | 50,420 | 0 | 1,882 | 2025-09-08~2026-09-18 |
| DAILY_PANEL | KOSPI200 | 823,604 | **823,604** | **823,604** | 2010-01-04~2026-09-18 |
| DAILY_PANEL_AUCTION | KOSPI200 | 823,604 | **823,604** | **823,604** | 2010-01-04~2026-09-18 |

### 6.2 코드 경로

`BacktestEngine.select()`의 `score_q` 분기(`src/cvd_obv/backtest/engine.py`):

```python
ranked = [p for p in scored if p[0]["total_score"] is not None]
if len(ranked) < 5:
    return []
```

DAILY_PANEL은 매일 `ranked`가 비어 있으므로 모든 날 빈 목록을 반환한다. 그래서 거래가 0건이다.
`RANK_PCT*`도 `rank`가 전부 null이라 같은 이유로 0건이 된다.

### 6.3 의도된 동작인가

그렇다. `src/cvd_obv/backtest/daily_signals.py` 모듈 설명에 근거가 적혀 있다.

- V1.5 총점은 CVD 성분을 포함하므로 일봉만으로는 재현할 수 없다. 점수를 지어내면
  "V1.5 검증"이 "새 지표 만들기"가 되므로 `total_score`를 null로 둔다.
- 이 경로에서 쓸 수 있는 정책은 `BUY_ALL`과 `RANDOM`이다(이후 `NO_GATE`, `GATE_FAIL`도
  추가됐다). `SCORE_Q*`·`RANK_PCT*`는 **막지 않고 빈 결과로 둔다.** 점수가 없다는 사실이
  결과에 드러나게 하려는 것이다.

### 6.4 반론·주의

- 빈 결과는 조용히 지나가기 쉽다. 요약에는 "거래 0 / 총수익 0.00%"만 나오고, 그 이유가
  점수 결측인지 신호 부재인지 출력만으로는 구별되지 않는다. 오류나 경고를 내도록 바꾸면
  더 명확해진다. 다만 그러면 위 설계 의도("막지 않는다")를 바꾸게 되므로 결정이 필요하다.
  이번 작업에서는 **변경하지 않았다.**
- 현재 `STAGE_STATUS_V2.md`의 SCORE_Q5·SCORE_Q1 수치는 REPLAY 경로 결과다. DAILY_PANEL
  결과로 인용된 것은 확인되지 않았다.

## 7. 남은 일

- **커밋:** src/·tests/는 아직 git에 한 번도 올라가지 않았다. 나중에 전체를 한꺼번에 올릴 예정이다.
  인덱스에 `.kiwoom_token.json` 삭제가 스테이징돼 있으니 함께 확인해야 한다.
- **(선택) DAILY_PANEL + 점수 정책 경고:** 6.4 참고. 설계 의도와 부딪히므로 결정이 필요하다.
- **리비전 2가 처음 생길 때:** 이번 수정으로 조회 경로는 준비됐다. 다만 이미 만들어진
  gold 산출물(features·labels·signals)은 리비전 1 봉으로 계산된 것이다. 재계산 여부는
  리비전 2를 쓰는 시점에 따로 정해야 한다.

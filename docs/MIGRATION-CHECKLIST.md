# 운영 PC 이관 체크리스트

작성일: 2026-10-31 (파일 수·용량은 이 날 실측, 적재 장부 `confirmed_through` = 2026-10-30)
절차 설명: [MIGRATION.md](MIGRATION.md) — 이 문서는 **빠짐없이 옮겼는지 확인하는 표**다

- 대상은 **git에 올라가지 않는 자료**(`.gitignore` 대상)와 프로젝트 밖 자료다. git에 올라간 파일(783개)은 맨 아래 한 줄로 확인한다
- 파일 수가 10개를 넘는 폴더는 **대표 파일 2개 + "외 N개"**로, 10개 이하는 파일명 단위로 적었다
- 구분: **필수** = 없으면 적재·연구를 이어갈 수 없다 / **선택** = 다시 만들 수 있지만 시간이 든다 / **제외** = 옮기지 않는다(새 PC에서 다시 생긴다)
- 체크는 두 번 한다: 기존 PC에서 복사본에 들어갔는지, 새 PC에서 같은 수가 있는지
  (`scripts\migrate-export.ps1`이 끝에 필수·선택 항목 포함 여부를 출력한다)

파일 수 세기(새 PC, PowerShell):

```powershell
(Get-ChildItem data\silver\minute_bars -Recurse -File).Count
```

---

## 0. 복사 전 (기존 PC)

- [ ] 적재가 돌고 있지 않다 — 10:00~15:00 또는 21:00 이후에 복사
- [ ] 작업 스케줄러 두 작업을 껐다(`migrate-export.ps1`이 기본으로 끈다)
  - [ ] `CVD 잠정 일봉 적재` → `scripts\provisional-load.bat` (월~금 16:10)
  - [ ] `CVD 정규장 확정 적재` → `scripts\daily-load.bat` (월~토 08:30)
- [ ] `data\manifests\data_load_state.json`의 `confirmed_through`를 적어 둔다: 2026-10-30

---

## 1. 수집 데이터 — `data\silver` (필수, 약 3.44GB, 54,008개)

| 확인 | 폴더 | 파일 수 | 용량 | 내용 | 대표 파일 |
|:-:|------|------:|------:|------|------|
| [ ] | `data\silver\minute_bars\period_min=1` | 21,687 | 2.83GB | 1분봉(KRX·NXT), 2025-09~2026-10 | `year=2025\month=09\security_bucket=00\part-min1-20260926-c003.parquet`, `year=2026\month=10\security_bucket=31\part-topup_load_20261030233817_9a506cf5-c026.parquet` 외 21,685개 |
| [ ] | `data\silver\minute_bars\period_min=5` | 6,878 | 0.33GB | 5분봉(구 방식), 2025-09~2026-09 | `year=2025\month=09\security_bucket=00\part-cli_minute_cache_20260923175713_7c054075-c004.parquet`, `year=2026\month=09\security_bucket=31\part-min_20260923_kospi-c009.parquet` 외 6,876개 |
| [ ] | `data\silver\daily_bars\market=KRX` | 21,392 | 237.3MB | 일봉, 1996-07~2026-10 | `year=1996\month=07\part-daily_20260923_kosdaq-c001.parquet`, `year=2026\month=10\part-topup_load_20261030234858_1869c520.parquet` 외 21,390개 |
| [ ] | `data\silver\daily_trade_sides` | 1,063 | 30.3MB | 체결량(매수·매도), 2010~2026 | `year=2010\part-kis_sides_20261019051637-p001.parquet`, `year=2026\part-topup_load_20261030235127_a0fbea83-UN-p001.parquet` 외 1,061개 |
| [ ] | `data\silver\bar_coverage\dataset=minute` | 1,689 | 11.6MB | 분봉 수집 범위 기록 | `year=2025\part-cli_minute_cache_20260923175713_7c054075-c001.parquet`, `year=2026\part-topup_load_20261030233817_9a506cf5-c030.parquet` 외 1,687개 |
| [ ] | `data\silver\market_calendar\market=KRX` | 862 | 6.6MB | 거래일 달력, 1996~2026 | `year=1996\part-cal_20260923.parquet`, `year=2026\part-topup_load_20261029073009_b35da6d1.parquet` 외 860개 |
| [ ] | `data\silver\universe_membership` | 262 | 1.4MB | 코스피200(153)·코스닥150(109) 편입 이력 | `index_id=KOSDAQ150\effective_year=2015\part-cli_universe_20260923153254_0949b250.parquet`, `index_id=KOSPI200\effective_year=2026\part-topup_load_20261029073635_9ac6c828.parquet` 외 260개 |
| [ ] | `data\silver\benchmark_bars` | 163 | 1.8MB | 지수 일봉(KOSPI·KOSPI200·KOSDAQ) | `benchmark_id=KRX_KOSDAQ\year=1999\part-bench_20260923.parquet`, `benchmark_id=KRX_KOSPI200\year=2026\part-bm_20260918060000_krx.parquet` 외 161개 |

`data\silver\security_master` (2개, 120KB) — 종목 기본 정보

- [ ] `data\silver\security_master\market=KOSDAQ\part-secmaster_20260923.parquet` (83KB)
- [ ] `data\silver\security_master\market=KOSPI\part-secmaster_20260923.parquet` (37KB)

---

## 2. 원천 응답 — `data\bronze` (필수, 약 588MB, 18,139개)

KRX·DART에서 받은 원본이다. 다시 받으려면 API 호출이 수만 번 필요하다.

| 확인 | 폴더 | 파일 수 | 용량 | 내용 | 대표 파일 |
|:-:|------|------:|------:|------|------|
| [ ] | `data\bronze\krx_openapi` | 16,566 | 520.9MB | KRX 일별 시세 원본, 2010~2026-10-28 (지수 코스피·코스닥 각 4,139 / 종목 코스피·코스닥 각 4,144) | `idx_kosdaq_dd_trd\2010\20100104.json.gz`, `sto_stk_bydd_trd\2026\20261028.json.gz` 외 16,564개 |
| [ ] | `data\bronze\krx_notices` | 238 | 49.8MB | KRX 구성종목 공지(`notices.json` + 첨부 237개, 공지별 폴더 201개) | `notices.json`, `attachments\1003\1_25년3월KOSPI시가총액규모별지수구성종목현황.xlsx` 외 236개 |
| [ ] | `data\bronze\dart_list_I002` | 797 | 2.0MB | DART 공시 목록(회사별) | `00100601.json`, `02042949.json` 외 795개 |
| [ ] | `data\bronze\dart_list_I002_live` | 351 | 300KB | DART 공시 목록(최근) | `00102858.json`, `02042949.json` 외 349개 |
| [ ] | `data\bronze\ingest_runs\dataset=daily\started_year=2026` | 142 | 1.0MB | 적재 실행 기록 | `part-cli_daily_cache_20260918130149_cf7b5244.parquet`, `part-topup_load_20261030234753_533fb221.parquet` 외 140개 |
| [ ] | `data\bronze\dart_fnltt_multi` | 44 | 14.3MB | DART 재무제표, 2016~2026 분기별 | `2016_11011.json`, `2026_11014.json` 외 42개 |

- [ ] `data\bronze\dart_corp_codes.json` (86KB) — DART 회사 코드표

---

## 3. 장부·manifest — `data\manifests` (필수, 약 35.9MB, 4,870개)

| 확인 | 대상 | 파일 수 | 용량 | 내용 | 대표 파일 |
|:-:|------|------:|------:|------|------|
| [ ] | `data\manifests\silver_*.json` | 4,340 | 28.2MB | silver 적재 실행별 manifest | `silver_bar_coverage_cli_minute_cache_20260923175713_7c054075-c001.json`, `silver_universe_membership_uni_kosdaq_20260923.json` 외 4,338개 |
| [ ] | `data\manifests\gold_*.json` | 386 | 7.4MB | gold 계산 실행별 manifest | `gold_ablation_results_s8_h1.json`, `gold_trades_s7d-c15303be8a.json` 외 384개 |
| [ ] | `data\manifests\bronze_*.json` | 142 | 132KB | bronze 적재 실행별 manifest | `bronze_ingest_runs_cli_daily_cache_20260918130149_cf7b5244.json`, `bronze_ingest_runs_topup_load_20261030234753_533fb221.json` 외 140개 |

- [ ] `data\manifests\data_load_state.json` (354B) — **적재 장부(필수)**. 마지막 확정일 `confirmed_through`
- [ ] `data\manifests\minute_catchup_state.json` (183KB) — 분봉 재시도 억제 장부(선택 — 없으면 얇은 종목을 다시 시도할 뿐이다)
- [ ] `data\manifests\baseline_v1_5.json` — git으로도 옮겨진다(추적 파일)

---

## 4. 연구 결과 — `cvd_selection\research\output` (필수, 6개, 261.2MB)

CVD 종가매수 선정 연구의 패널·백테스트 결과다. 패널을 다시 만들려면 오래 걸린다.

- [ ] `cvd_selection\research\output\panel-20100701-20220331.parquet` (190.5MB)
- [ ] `cvd_selection\research\output\panel-20220103-20261023.parquet` (70.4MB)
- [ ] `cvd_selection\research\output\backtest.json` (55KB)
- [ ] `cvd_selection\research\output\backtest-clvbug.json` (55KB)
- [ ] `cvd_selection\research\output\onset.json` (60KB)
- [ ] `cvd_selection\research\output\onset-clvbug.json` (60KB)

연구 코드(`cvd_selection\research\*.py`)는 git에 올라가 있지만 폴더째 복사해도 같이 간다.

---

## 5. 계산 결과 — `data\gold` (선택, 약 2.39GB, 14,853개)

silver에서 다시 계산할 수 있다. `migrate-export.ps1 -SkipGold`로 빼면 새 PC에서 백테스트 전에 다시 계산해야 한다.

| 확인 | 폴더 | 파일 수 | 용량 | 내용 | 대표 파일 |
|:-:|------|------:|------:|------|------|
| [ ] | `data\gold\daily_forward_returns` | 10,731 | 1.20GB | 일봉 신호 이후 수익률(1·3·5·10·20거래일) | `horizon_td=1\signal_year=1999\part-cli_forward_20260918132018_d29abe19-c001.parquet`, `horizon_td=5\signal_year=2026\part-fwd_20260918070000_kospi200bm-c020.parquet` 외 10,729개 |
| [ ] | `data\gold\forward_returns` | 1,995 | 729.3MB | 진입 이후 수익률 라벨 | `horizon_td=1\entry_year=2025\entry_month=09\part-stage6_labels_20260919.parquet`, `horizon_td=5\entry_year=2026\entry_month=09\part-v15s_20260922055846_c04e7a5f.parquet` 외 1,993개 |
| [ ] | `data\gold\daily_features\feature_version=F1-v15_daily-obv20` | 1,436 | 365.2MB | 일봉 특징값, 1996~2026 | `year=1996\part-feat-b10-20260924-c007.parquet`, `year=2026\part-feat_20260918150000_kospi200-c011.parquet` 외 1,434개 |
| [ ] | `data\gold\signals` | 481 | 71.0MB | 스캐너 신호(코스피200 455·코스닥150 26) | `index_id=KOSDAQ150\year=2025\month=09\part-kosdaq150-b10-20260924.parquet`, `index_id=KOSPI200\year=2026\month=09\part-replay_20260923_tail.parquet` 외 479개 |
| [ ] | `data\gold\scanner_runs` | 79 | 696KB | 스캐너 실행 기록 | `index_id=KOSDAQ150\year=2025\month=09\part-kosdaq150-b10-20260924.parquet`, `index_id=KOSPI200\year=2026\month=09\part-replay_20260923_tail.parquet` 외 77개 |
| [ ] | `data\gold\portfolio_nav` | 40 | 721KB | 백테스트 순자산(실험 20개) | `experiment_id_prefix=bt_00fe157\year=2025\part-s7a-00fe157039.parquet`, `experiment_id_prefix=bt_fe4c6fd\year=2026\part-s7c-fe4c6fd546.parquet` 외 38개 |
| [ ] | `data\gold\trades` | 40 | 9.0MB | 백테스트 거래 내역(실험 20개) | `experiment_id_prefix=bt_00fe157\entry_year=2025\part-s7a-00fe157039.parquet`, `experiment_id_prefix=bt_fe4c6fd\entry_year=2026\part-s7c-fe4c6fd546.parquet` 외 38개 |
| [ ] | `data\gold\backtest_metrics` | 20 | 148KB | 백테스트 지표 | `experiment_id_prefix=bt_00fe157\part-s7a-00fe157039.parquet`, `experiment_id_prefix=bt_fe4c6fd\part-s7c-fe4c6fd546.parquet` 외 18개 |
| [ ] | `data\gold\backtest_runs` | 20 | 212KB | 백테스트 실행 기록 | `part-s7a-00fe157039.parquet`, `part-s7d-c15303be8a.parquet` 외 18개 |

`data\gold\signal_features` (7개, 30.8MB) — 신호 시점 특징값

- [ ] `part-cli_replay_20260918141459_125cff58.parquet` (5.5MB)
- [ ] `part-cli_replay_20260922055507_0be2c0ef.parquet` (5.7MB)
- [ ] `part-kosdaq150-b10-20260924.parquet` (4.1MB)
- [ ] `part-kosdaq150-p1b10-20260926.parquet` (4.2MB)
- [ ] `part-kospi200-b10-20260924.parquet` (5.5MB)
- [ ] `part-kospi200-p1b10-20260926.parquet` (5.6MB)
- [ ] `part-replay_20260923_tail.parquet` (65KB)

`data\gold\ablation_results` (3개, 50KB) — 제거 실험 결과

- [ ] `horizon_td=1\part-s8_h1.parquet`
- [ ] `horizon_td=5\part-s8_h5.parquet`
- [ ] `horizon_td=20\part-s8_h20.parquet`

단일 파일

- [ ] `data\gold\cvd_daily_flow.json` (6.4MB) — CVD 일별 흐름 집계

---

## 6. 키·설정·기타 파일 (프로젝트 폴더 안)

- [ ] `.env` (4.4KB) — **필수**. KIS·키움·KRX·DART API 키. USB·사내 공유로만 옮긴다
- [ ] `.claude\settings.local.json` (714B) — 선택. Claude Code 로컬 권한 설정
- [ ] `scratchx.py` (886B) — 선택. 일회성 점검 스크립트
- [ ] `local\` — 지금 없음(생기면 민감 정보이므로 함께 옮긴다)
- [ ] `reports\portfolio-latest.md` — 지금 없음(생기면 함께 옮긴다)

`logs\` 중 git 제외 로그 (선택, 6개, 62KB) — 과거 수집 기록

- [ ] `logs\daily-cache-kospi200-1999.log`
- [ ] `logs\daily-cache-kospi200-1999-resume.log`
- [ ] `logs\features-build.log`
- [ ] `logs\forward-build.log`
- [ ] `logs\forward-build-bm.log`
- [ ] `logs\minute-cache-kospi200.log`

---

## 7. 프로젝트 폴더 밖

| 확인 | 대상 | 위치 | 비고 |
|:-:|------|------|------|
| [ ] | 작업 스케줄러 `CVD 잠정 일봉 적재` | 작업 스케줄러 | `migrate-export.ps1`이 `tasks\task-provisional-load.xml`로 내보낸다 |
| [ ] | 작업 스케줄러 `CVD 정규장 확정 적재` | 작업 스케줄러 | `tasks\task-daily-load.xml` |
| [ ] | 파이썬 패키지 버전 | `pip freeze` | `pip-freeze.txt`로 남긴다. 기존 PC는 Python 3.14 |
| [ ] | 설치 스킬 중 `skills\`에 원본이 없는 것 | `%USERPROFILE%\.claude\commands\` | 지금 1개: `kr-stock-scanner - 복사본.md` → `claude-commands-extra\` (나머지 31개는 `skills\`에서 다시 설치) |

Claude 작업 메모리 (4개) — `%USERPROFILE%\.claude\projects\C--a-ai-ai-berkshire-kr\memory\` → `claude-memory\`

- [ ] `MEMORY.md`
- [ ] `data-load-pipeline-state.md`
- [ ] `cvd-selection-scanner.md`
- [ ] `feedback-korean-wrapup.md`

> 새 PC에서 프로젝트 경로가 바뀌면 메모리 폴더 이름도 바뀐다(경로의 `:` `\`를 `-`로 바꾼 이름). `migrate-import.ps1`이 맞춰 준다.

---

## 8. 옮기지 않는 것 (제외)

복사본에 있으면 지운다 — 새 PC에서 저절로 다시 생기고, 토큰은 다른 PC에서 쓰면 충돌한다.

- [ ] `.kis_token.json` (400B) — KIS 접속 토큰
- [ ] `.kiwoom_token.json` (159B) — 키움 접속 토큰
- [ ] `data\manifests\data_load.lock`, `data\manifests\data_load.lock.json` — 적재 잠금
- [ ] `C83F901C09BC4A43BF0D43811169DB9A66EDA04F\` (3개, 158KB) — KRX 조회 캐시
- [ ] `__pycache__\`, `.pytest_cache\` — 파이썬 캐시

---

## 9. git에 올라간 파일

- [ ] 프로젝트 파일 783개(`git ls-files`) — 폴더째 복사하면 같이 간다. git clone으로 받았다면
      새 PC에서 `git log -1`이 기존 PC와 같은지 확인(2026-10-31 기준 `3db928f`)

---

## 10. 새 PC 확인

- [ ] 1~4절 필수 항목의 파일 수가 위 표와 같다
- [ ] `.env`가 있다
- [ ] 키움 Open API 사이트에 새 PC 공인 IP 등록(사용자 직접)
- [ ] `python -m pytest -q tests` 통과
- [ ] `python cvdobv.py data-load daily --dry-run`이 `confirmed_through` 2026-10-30(또는 그 뒤) 다음 날부터 확인한다고 나온다
- [ ] 작업 스케줄러 두 작업을 새 PC에서 켰다
- [ ] 기존 PC 두 작업은 **꺼진 채**다 — 두 PC가 동시에 적재하면 안 된다
- [ ] 첫 정기 실행 뒤 `logs\provisional-load\`, `logs\daily-load\` 로그의 종료 코드가 0(또는 9)

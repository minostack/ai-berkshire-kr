# 운영 PC 이관 절차

작성일: 2026-10-31 (용량·파일 수는 이 날 실측)
대상: 프로젝트 폴더 `C:\a-ai\ai-berkshire-kr` — 약 7.0GB, 파일 약 9.2만 개

## 요약

- **폴더 전체를 같은 경로에 복사한다.** git clone으로는 안 된다 — `data\`·`cvd_selection\research\`·`.env`가
  `.gitignore` 대상이고, 코드도 커밋 전 변경이 많다
- 복사 전에 기존 PC의 정기 작업 두 개를 끈다
- 새 PC에서 파이썬 환경·API 키·작업 스케줄러를 다시 맞춘다
- **두 PC가 동시에 적재하면 안 된다** — KIS 토큰이 충돌하고 데이터가 갈라진다
- git으로 옮길 경우 수집 데이터 말고도 따로 옮겨야 하는 자료가 있다 — **6절**

## 0. 스크립트로 옮기기 (권장)

아래 3절 ①~⑥과 4절을 스크립트 두 개가 한다(UTF-8 BOM으로 저장 — 고칠 때 BOM을 유지할 것).

**기존 PC** — 적재가 돌지 않는 시간(10:00~15:00 또는 21:00 이후)에:

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\migrate-export.ps1 -Dest E:\berkshire-migrate
```

- 적재 프로세스가 돌고 있으면 멈춘다
- "CVD*" 작업을 XML로 내보내고 **끈다**(`-KeepTasksEnabled`면 끄지 않음 — 시험 복사용)
- 프로젝트 폴더를 `<Dest>\project`로 복사. 빼는 것은 새 PC에서 저절로 다시 생기는 것뿐이다 — 토큰
  캐시·적재 잠금·`__pycache__`·`.pytest_cache`·KRX 조회 캐시(`C83F901C…`). `-SkipGold`면 `data\gold`도 뺀다.
  `.gitignore`에 걸린 나머지(`.env`, 수집 데이터, 연구 코드·결과, `scratchx.py` 등)는 git으로 못 옮기므로 복사한다
- 복사 뒤 git에 없는 자료가 들어갔는지 점검해 출력하고 `export-info.txt`에 남긴다(필수: `data\silver`·
  `data\bronze`·적재 장부·`.env`·연구 코드·연구 결과 / 선택: `data\gold`·Claude 로컬 설정·`local\` 등).
  가져오기도 필수 6개가 있는지 다시 확인한다
- Claude 메모리, `skills\`에 원본 없는 설치 스킬, `pip freeze`, 요약(`export-info.txt`)을 함께 둔다

**새 PC** — `<Dest>` 폴더를 옮긴 뒤:

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File E:\berkshire-migrate\project\scripts\migrate-import.ps1 -Source E:\berkshire-migrate -InstallPackages
```

- `-Target`(기본 `C:\a-ai\ai-berkshire-kr`)에 복사. 대상에 파일이 있으면 `-Force` 없이는 멈춘다(지우지 않음)
- 작업 스케줄러를 지금 사용자로, 경로를 `-Target`으로 바꿔 **꺼진 상태로** 등록(`-EnableTasks`면 켬,
  같은 이름 작업이 있으면 건드리지 않음)
- Claude 메모리(이미 있는 파일은 덮지 않음)·스킬 설치, 테스트(`-SkipTests`로 건너뜀), `data-load daily --dry-run`
- 키움 IP 등록과 작업 켜기는 끝에 안내만 한다 — 확인 뒤 사람이 한다

2026-10-31 시험: 작은 가짜 프로젝트로 내보내기→가져오기를 돌려 제외 규칙(토큰·잠금·gold·캐시), 장부·`.env`
복사, 경로 치환, 작업 등록(꺼짐·현재 사용자·StartWhenAvailable 유지)을 확인했다. 실제 7GB 복사는 돌려 보지 않았다.

## 1. 옮겨야 할 데이터

### 1-1. `data\silver` — 시장 데이터 (3.6GB, 필수)

| 폴더 | 내용 | 용량 | 파일 수 |
|---|---|---|---|
| `minute_bars` | 1분봉. KRX 2025-09-01~, NXT 2026-08-03~. 과거 5분봉 포함 | 3.3GB | 28,004 |
| `daily_bars` | 일봉. 1996~, 확정 봉·NXT 소급분 포함 | 280MB | 21,371 |
| `daily_trade_sides` | KIS 매수·매도 체결량(UN·J·NX) | 33MB | 1,062 |
| `bar_coverage` | 분봉 수집 기록 — 무엇을 받았는지 판정하는 데 쓴다 | 16MB | 1,623 |
| `market_calendar` | 거래일 달력 | 8.2MB | 831 |
| `benchmark_bars` | 지수 일봉(코스피·코스닥·코스피200) | 2.4MB | 160 |
| `universe_membership` | 코스피200·코스닥150 구성종목 이력 | 2.2MB | 262 |
| `security_master` | 종목 기본정보 | 124KB | 2 |

1분봉은 **다시 받을 수 없는 부분이 있다.** KIS는 약 1년치만 주므로 오래된 날부터 사라진다
(2025-09~10월분은 이미 다시 받을 수 없다).

### 1-2. `data\bronze` — 원천 응답 (623MB, 필요)

| 폴더 | 내용 | 용량 | 파일 수 |
|---|---|---|---|
| `krx_openapi` | KRX 일별 시세 원본 — 구성종목 검산 재료 | 553MB | 16,566 |
| `krx_notices` | 지수 운영 공지 | 51MB | 238 |
| `dart_*` | DART 재무·공시 목록 캐시 | 약 20MB | 약 1,200 |
| `ingest_runs` | 수집 실행 기록 | 1.2MB | 140 |

### 1-3. `data\gold` — 계산 결과 (2.5GB, 선택)

| 폴더 | 용량 |
|---|---|
| `daily_forward_returns` | 1.3GB (10,731개) |
| `forward_returns` | 735MB |
| `daily_features` | 369MB |
| `signals`·`trades`·백테스트 결과 등 | 약 115MB |

일봉에서 다시 계산할 수 있다. 다만 오래 걸리고, 이미 돌린 백테스트를 그대로 재현하려면 옮기는 편이 낫다.
빼면 전체가 약 4.5GB로 줄어든다.

### 1-4. 장부와 기타 파일 (필요)

| 위치 | 내용 | 용량 |
|---|---|---|
| `data\manifests\` | 저장 기록 4,715개 + 적재 장부 | 51MB |
| └ `data_load_state.json` | 마지막 확정일(`confirmed_through`) — 다음 적재가 여기서부터 이어간다 | 1KB |
| └ `minute_catchup_state.json` | 분봉 재시도 억제 장부 | — |
| └ `baseline_v1_5.json` | V1.5 동결 해시 | 7KB |
| `data\index_stocks.json`, `data\universe_snapshots\` | 현재 구성종목 명단·스냅샷 | 124KB |
| `cvd_selection\` | `research`(262MB)·`output`(25MB) 포함 | 287MB |
| `reports\` | 리포트 | 32MB |
| `logs\` | 적재 로그 | 2.5MB |
| `.dart_corp_cache.json` | DART 기업코드 캐시 | 276KB |
| `C83F901C09BC4A43BF0D43811169DB9A66EDA04F\` | 2026-09-19에 생긴 KRX 조회 캐시로 보인다 | 168KB |

## 2. 민감 정보 — 복사하되 외부에 올리지 않는다

| 파일 | 내용 | 처리 |
|---|---|---|
| `.env` | KIS·키움·DART·KRX·토스·GitHub 등의 키 | 복사. USB·사내 공유로만 옮기고 클라우드에 올리지 않는다 |
| `local\` | 민감 정보 폴더(현재 비어 있음) | 생기면 함께 복사 |
| `.kis_token.json`, `.kiwoom_token.json` | 접근 토큰 캐시 | **복사하지 않는다** — 새 PC에서 자동으로 다시 발급된다 |

## 3. 옮기는 순서

### ① 기존 PC — 정기 작업을 끈다

실행 중인 적재가 없을 때(예: 10:00~15:00, 또는 21:00 이후) 두 작업을 끈다.

```bash
powershell -NoProfile -Command "Get-ScheduledTask | Where-Object { $_.TaskName -like 'CVD*' } | Disable-ScheduledTask"
```

이때 작업 정의를 XML로 내보내 둔다(⑤에서 쓴다).

```bash
powershell -NoProfile -Command "Get-ScheduledTask | Where-Object { $_.TaskName -like 'CVD*' } | ForEach-Object { Export-ScheduledTask -TaskName $_.TaskName -TaskPath $_.TaskPath | Out-File -Encoding unicode (\"E:\task_\" + $_.Actions[0].Execute.Split('\')[-1] + '.xml') }"
```

### ② 폴더째 복사한다

같은 경로 `C:\a-ai\ai-berkshire-kr`를 권한다. 외장 디스크(예: `E:`)로 옮기는 예:

```bash
robocopy "C:\a-ai\ai-berkshire-kr" "E:\ai-berkshire-kr" /E /COPY:DAT /DCOPY:T /R:1 /W:1 /MT:16 /XD __pycache__ .pytest_cache /XF .kis_token.json .kiwoom_token.json data_load.lock data_load.lock.json /LOG:E:\copy.log
```

- 새 PC에서는 원본과 대상을 바꿔 `C:\a-ai\ai-berkshire-kr`로 복사한다
- 작은 파일이 9만 개라 7z 무압축 저장으로 한 파일로 묶어 옮기면 더 빠르다
- 다른 경로에 둬도 동작한다(코드는 상대 경로, 저장소 루트 = `<프로젝트>\data`). 다만 작업 스케줄러
  경로를 고쳐야 하고 Claude 메모리 폴더 이름도 경로에 따라 바뀐다(4절)

### ③ 새 PC — 파이썬 환경

기존 PC는 Python 3.14.5, 최소 요구는 3.11이다.

```bash
pip install pyarrow pandas numpy requests python-dotenv openpyxl xlrd matplotlib pytest pytest-mock
```

기존 버전: pyarrow 24.0.0, pandas 2.3.3, numpy 2.4.5, requests 2.34.2, python-dotenv 1.2.2,
openpyxl 3.1.5, matplotlib 3.11.1, pytest 9.1.1. `pyproject.toml`에는 pyarrow·pytest만 적혀 있어
나머지는 직접 설치해야 한다.

### ④ 새 PC — 키와 접속 권한

- **키움**: 새 PC의 공인 IP를 키움 홈페이지에 등록해야 한다(8050 "IP 미등록"). 일일 적재 원천은
  KIS라 적재에는 영향이 없지만, V1.5 스캐너·`update_index`·NXT 30분봉 소급에는 필요하다
- **KIS**: 두 PC에서 동시에 쓰지 않는다. 같은 앱키로 토큰을 새로 발급하면 다른 쪽 토큰이 무효가 될 수
  있다(`src/cvd_obv/ingestion/kis_client.py` 설명)

### ⑤ 새 PC — 작업 스케줄러

| 작업 이름 | 실행 파일 | 시작 폴더 | 일정 |
|---|---|---|---|
| CVD 잠정 일봉 적재 | `scripts\provisional-load.bat` | 프로젝트 폴더 | 월~금 16:10 |
| CVD 정규장 확정 적재 | `scripts\daily-load.bat` | 프로젝트 폴더 | 월~토 08:30 |

- 두 작업 모두 "놓친 작업 즉시 실행"(StartWhenAvailable)을 켠다
- ①에서 내보낸 XML을 작업 스케줄러 → "작업 가져오기"로 불러온 뒤 실행 계정을 새 PC 사용자로 지정한다
- 경로를 바꿨다면 동작의 실행 파일·시작 폴더를 새 경로로 고친다
- 배치 파일은 ASCII 전용이다 — 한글 주석을 넣지 않는다(cmd가 깨뜨린다)

### ⑥ 새 PC — 동작 확인

```bash
python -m pytest -q tests
```

```bash
python cvdobv.py data-load daily --dry-run
```

- 테스트가 모두 통과해야 한다
- dry-run은 장부 `confirmed_through`부터 할 일을 출력한다. 이관하는 동안 밀린 날은 첫 정기 실행이 메운다

### ⑦ 기존 PC는 작업을 꺼 둔 채로 둔다

두 PC가 같은 날 적재하면 KIS 토큰이 충돌하고 데이터가 갈라진다.

## 4. Claude Code를 새 PC에서도 쓸 경우

| 위치 | 내용 |
|---|---|
| `C:\Users\<사용자>\.claude\projects\C--a-ai-ai-berkshire-kr\memory\` | 작업 상태 메모리. 새 PC가 같은 프로젝트 경로를 쓰면 같은 위치에 복사(폴더 이름이 프로젝트 경로에서 나온다) |
| `C:\Users\<사용자>\.claude\commands\` | 설치된 스킬 — 새 PC에서 `scripts\install-claude-commands.bat`을 다시 실행 |

## 5. 참고

- `CLAUDE.md`의 "로컬 클론 경로"(`C:\myProject_room\agentic_AI\ai-berkshire-kr`)는 실제 운영 경로
  (`C:\a-ai\ai-berkshire-kr`)와 다르다
- 이관 후 첫 정기 실행 로그(`logs\provisional-load\`, `logs\daily-load\`)에서 종료 코드와
  "적재 후" 표를 확인한다

## 6. git에 올라가지 않는 자료 — 수집 데이터 외

2026-10-31 `git ls-files -o -i --exclude-standard`로 확인했다. 1절의 수집 데이터(`data\bronze`·`data\silver`·
`data\gold`·`data\manifests\{bronze,silver,gold}_*.json`)를 빼고 `.gitignore` 때문에 git에 올라가지
않는 것은 아래뿐이다. git push/clone으로 옮기면 이것들은 **빠지므로 따로 복사**해야 한다.

### 6-1. 프로젝트 폴더 안

| 항목 | 위치 | 크기 | 걸리는 규칙(`.gitignore`) | 처리 |
|---|---|---|---|---|
| CVD 선정 연구 코드 | `cvd_selection\research\` `__init__.py`·`backtest.py`(8.5KB)·`features.py`(7.3KB)·`onset.py`(8.1KB) | 24KB | (2026-10-31 예외 추가 전) `research/` | 이제 git 추적 대상 — **커밋하기 전까지는** 폴더째 복사 |
| CVD 선정 연구 결과 | `cvd_selection\research\output\` — `panel-20100701-20220331.parquet`·`panel-20220103-20261023.parquet`(패널), `backtest*.json`·`onset*.json` 4개 | 262MB | `cvd_selection/research/output/` | 복사(패널은 일봉에서 다시 만들 수 있지만 오래 걸린다) |
| API 키 | `.env` | 8KB | `.env` | **복사, 외부 업로드 금지**(2절) |
| 토큰 캐시 | `.kis_token.json`, `.kiwoom_token.json` | 1KB씩 | `/.kis_token.json`, `/.kiwoom_token.json` | 복사하지 않는다(자동 재발급) |
| Claude Code 로컬 권한 설정 | `.claude\settings.local.json` | 714B | `.claude/` | 선택 — Claude Code를 쓰면 복사 |
| 옛 수집 로그 | `logs\daily-cache-kospi200-1999*.log`(2개), `logs\features-build.log`, `logs\forward-build*.log`(2개), `logs\minute-cache-kospi200.log` | 약 76KB | `/logs/daily-cache-*.log` 등 | 선택(기록용) |
| 민감 정보 폴더 | `local\` | 현재 없음 | `/local/` | 생기면 복사, 외부 업로드 금지 |
| 실제 포지션 리포트 | `reports\portfolio-latest.md` | 현재 없음 | 같은 이름 | 생기면 복사 |
| 명령어 로그 | `logs\command-log.jsonl` | 현재 없음 | 같은 이름 | 생기면 복사 |

`.gitignore`에는 이 밖에 `*_draft.md`, `*_data_collection.md`, `/실盘기록-원본/`, 미디어 파일(`*.mp4` 등)
규칙도 있다 — 지금 해당 파일은 없지만, 새로 생기면 역시 git에 올라가지 않는다.

**연구 코드 예외(2026-10-31 적용)**: `research/` 규칙은 원래 리서치 메모용인데
`cvd_selection\research\`의 파이썬 코드까지 막았다. 지금은 `.gitignore`에 아래 예외가 있어 코드 4개를
git이 추적한다(아직 커밋 전). 결과물 `output\`(262MB)은 계속 무시되므로 따로 복사해야 한다.

```
!cvd_selection/research/
cvd_selection/research/output/
```

### 6-2. 프로젝트 폴더 밖

| 항목 | 위치 | 처리 |
|---|---|---|
| 작업 스케줄러 정의 | Windows 작업 스케줄러 "CVD 잠정 일봉 적재", "CVD 정규장 확정 적재" | XML로 내보내 가져온다(3절 ①·⑤) |
| 파이썬 패키지 | 기존 PC 파이썬 환경(`pip list`) | 새로 설치(3절 ③). `pyproject.toml`에는 pyarrow·pytest만 있다 |
| Claude 작업 메모리 | `C:\Users\<사용자>\.claude\projects\C--a-ai-ai-berkshire-kr\memory\` (4개 파일) | Claude Code를 쓰면 복사(4절) |
| 설치된 스킬 | `C:\Users\<사용자>\.claude\commands\` (32개) | `scripts\install-claude-commands.bat`으로 다시 설치. **단 `kr-stock-scanner - 복사본.md`는 `skills\`에 원본이 없어** 설치 스크립트로 복원되지 않는다 — 필요하면 이 파일만 직접 복사 |
| 키움 IP 등록 | 키움 Open API 사이트 | 새 PC 공인 IP 등록(3절 ④) |

Windows 사용자 환경변수에는 API 키 관련 변수(KIS·KIWOOM·DART·KRX·TOSS 등)가 없다(2026-10-31 확인) —
키는 `.env` 하나만 옮기면 된다.

### 6-3. 반대로 git에 올라가는 것 중 주의할 것

`data\manifests\baseline_v1_5.json`(V1.5 동결 해시), `logs\daily-load\`·`logs\provisional-load\`·
`logs\nxt-backfill\` 로그, `data\index_stocks.json`·`data\universe_snapshots\`는 무시 규칙에 걸리지 않는다 —
`git add`하면 올라간다.

2026-10-31부터 무시하는 것:

| 항목 | 이관 때 |
|---|---|
| 적재 장부 `data_load_state.json`·`minute_catchup_state.json` — PC마다 다른 운영 상태 | **복사한다**(`data_load_state.json`은 필수 점검 대상) |
| 적재 잠금 `data_load.lock`·`data_load.lock.json` | 뺀다(다시 생긴다) |
| KRX 조회 캐시 `C83F901C09BC4A43BF0D43811169DB9A66EDA04F\` | 뺀다(다시 생긴다) |
| 일회성 점검 스크립트 `scratchx.py`(886B) | 복사한다 |

git으로만 옮기면 적재 장부가 없다. 그래도 적재는 동작한다(장부가 없으면 최근 10거래일을 확인한다).
다만 마지막 확정일이 10거래일보다 오래됐으면 그 사이를 놓치므로, 기존 PC의 장부를 복사하는 편이 안전하다.

## 7. git에 올리기 — `scripts\git-upload.ps1`

변경을 묶음별로 커밋하고 올린다. 푸시 여부는 사용자가 정한다 — 스크립트도 푸시 전에 묻는다.

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\git-upload.ps1 -DryRun
```

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\git-upload.ps1
```

| 순서 | 하는 일 |
|---|---|
| 1 | `git fetch origin`, 상태 출력(main 브랜치·rebase 진행 중이 아닌지 확인) |
| 2 | 안전 점검 — `.env`·토큰·수집 데이터(`data\bronze·silver·gold`)·적재 장부·잠금·연구 결과·포지션 리포트, 50MB 넘는 파일이 올라갈 목록에 있으면 아무것도 하지 않고 멈춘다 |
| 3 | 묶음별 커밋(변경 없는 묶음은 건너뜀) ① `.gitignore`(+ 스테이징된 토큰 추적 해제) ② 코드·테스트 `src tests tools configs scripts cvdobv.py pyproject.toml cvd_selection` ③ 문서·스킬 `docs skills CLAUDE.md` ④ 나머지(리포트·스캔 결과·로그·구성종목 명단 등) |
| 4 | `git pull --rebase origin main`(프로젝트 규칙). 충돌이면 멈추고 안내 — 고친 뒤 `git add` → `git rebase --continue` → 스크립트 다시 실행 |
| 5 | 올릴 커밋 목록을 보여 주고 "푸시할까요? (y/N)" — `-Yes`면 묻지 않음, `-NoPush`면 푸시하지 않음 |

2026-10-31 시험 실행(`-DryRun`): 변경 599개·약 60MB, 금지 파일 0, 묶음별 1(+삭제 1)·283·44·270개.

같은 일을 손으로 하려면(Git Bash):

```bash
git fetch origin && git status -sb
```

```bash
git add .gitignore && git commit -m "gitignore 정리: 토큰·적재 잠금·적재 장부·KRX 캐시·점검 스크립트 제외, CVD 선정 연구 코드 추적"
```

```bash
git add -A src tests tools configs scripts cvdobv.py pyproject.toml cvd_selection && git commit -m "CVD/OBV V2 데이터 레이어: KIS 적재 원천, NXT 메인마켓 거래량(8/3~), 체결량 UN, 선정 스캐너, 이관·업로드 스크립트"
```

```bash
git add -A docs skills CLAUDE.md && git commit -m "문서·스킬 갱신: 봉 기준(NXT 경계 8/3), 사전등록, 이관 절차(MIGRATION)"
```

```bash
git add -A && git commit -m "리포트·스캔 결과·적재 로그·구성종목 명단 갱신"
```

```bash
git pull --rebase origin main
```

```bash
git push origin main
```

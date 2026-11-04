# data_transfer — PC 간 수집 데이터 이관

운영 PC(PC1)가 매일 적재한 **잠정·확정 봉**을 임시 PC(PC2)로 옮겨, PC2에서도 스캐너를 돌릴 수 있게 한다.
PC2는 적재를 돌리지 않는다. 두 PC가 함께 적재하면 KIS 토큰이 충돌한다(`docs/MIGRATION.md` 3절 ⑦).

```
data_transfer/
├── transfer.py      — 실행 입구 (status / export / list / import)
├── package.py       — 현황·내보내기·가져오기 로직
├── tests/           — 단위 테스트 (python -m pytest data_transfer/tests -q)
└── DOWNLOAD/        — 묶음(zip)을 두는 곳. git에 올라가지 않는다
```

## 사용 순서

**PC1(운영)** — 수집 현황을 보고 내보낸다.

```bash
python -m data_transfer.transfer status
```

```bash
python -m data_transfer.transfer export --date 2026-11-03
```

```bash
python -m data_transfer.transfer export --start 2026-10-27 --end 2026-11-03
```

`DOWNLOAD\bars_<구간>_<PC이름>_<만든시각>.zip`이 생긴다. 이 파일을 USB 등으로 PC2의 `data_transfer\DOWNLOAD\`에 옮긴다.

**PC2(임시)** — 먼저 확인만 하고, 그다음 가져온다.

```bash
python -m data_transfer.transfer list
```

```bash
python -m data_transfer.transfer import --latest --dry-run
```

```bash
python -m data_transfer.transfer import --latest
```

```bash
python -m data_transfer.transfer status
```

특정 묶음을 가져오려면 `import DOWNLOAD\bars_....zip`처럼 경로를 준다. DOWNLOAD 안의 파일이면 파일명만 줘도 된다.

## 명령

| 명령 | 하는 일 | 주요 옵션 |
|---|---|---|
| `status` | 장부(`confirmed_through`, 마지막 확정·잠정 실행 시각), 날짜별 표, 테이블별 파일 수·용량 | `--date` / `--start --end` / `--days N`(기본 14일) / `--json` |
| `export` | 지정일 또는 구간의 행을 zip 하나로 쓴다 | `--date` 또는 `--start [--end]`, `--tables`, `--out` |
| `list` | DOWNLOAD의 묶음 목록(구간·보낸 PC·행 수) | `--dir` |
| `import` | 묶음의 해시를 확인한 뒤, 받는 PC에 **없는 행만** 새 파일로 쓰고 적재 장부를 옮긴다. 끝에 `calendar build` 실행 | `--latest`, `--dry-run`, `--no-calendar` |

**status의 날짜별 표**
- 왼쪽 4열(1분봉·잠정·확정·상태)은 적재 로그의 `[선행 점검]` 표와 **같은 판정**이다. 지수 구성종목 기준, 최신 리비전, 90% 규칙을 쓴다.
- 오른쪽 4열은 저장된 전체 종목 수다. KRX·NXT 1분봉(수집 기록 기준), 체결량 UN, 지수 일봉을 센다.

export와 import는 적재 잠금(`data/manifests/data_load.lock`)을 잡는다. 정기 적재가 도는 중이면 끝날 때까지 기다린다(`--lock-wait`초, 기본 3600).

## 옮기는 것과 옮기지 않는 것

| 테이블 | 옮김 | 비고 |
|---|---|---|
| `silver.daily_bars` | ○ | 잠정(rev 0)·확정 리비전 모두. 받는 PC의 reader가 최신을 고른다 |
| `silver.minute_bars` | ○ | KRX·NXT 1분봉. 5·30분봉은 1분봉에서 합성한다 |
| `silver.daily_trade_sides` | ○ | KIS 매수·매도 체결량(J·NX·UN) |
| `silver.bar_coverage` | ○ | 분봉 수집 기록 |
| `silver.benchmark_bars` | ○ | 지수 일봉 |
| `silver.market_calendar` | × | 일봉에서 파생된다. 가져온 뒤 `calendar build`로 다시 만든다(자동) |
| 구성종목 이력·`index_stocks.json` | × | 쓰기 경로가 정해져 있다(`universe refresh`, `tools/update_index.py`). 이력 뒤 날짜는 `universe_on()`이 `index_stocks.json`으로 대신하고 경고를 낸다 |
| 적재 장부 `data_load_state.json` | 계산 | 값을 복사하지 않고, 받는 PC에 들어온 확정 봉으로 다시 계산한다(아래 절) |
| bronze 원천 응답 | × | |

## 적재 장부 갱신

`import`는 끝날 때 받는 PC의 `data/manifests/data_load_state.json`을 옮긴다. 새 행이 0이어도 계산한다. `--dry-run`은 쓰지 않는다.

| 키 | 값 |
|---|---|
| `confirmed_through` | 지금 장부 다음 날(장부가 없으면 묶음 시작일)부터 확정 봉이 구성종목의 90% 이상인 날이 이어지는 마지막 날. 적재와 같은 판정이다 |
| `last_final_run` | 보낸 PC의 실행 시각. 보낸 PC의 `confirmed_through`까지 이 PC에 들어왔을 때만 가져온다 |
| `last_provisional_run` | 보낸 PC의 실행 시각. 그 실행이 만든 날의 잠정 봉이 묶음에 있을 때만 가져온다 |
| `last_import` | 마지막으로 가져온 묶음·보낸 PC·가져온 시각 |

- **뒤로 가지 않는다.** 옛 묶음을 다시 가져와도 장부는 그대로다.
- **잠정 봉은 `confirmed_through`를 옮기지 않는다.** 애프터마켓 확정 전인 날도 옮기지 않는다.
- **묶음 안의 빈 평일은 넘어간다.** 보낸 PC에도 행이 없으므로 휴장으로 본다.
- **묶음 앞쪽의 빈 평일에서는 멈추고 경고한다.** 휴장인지 받지 않은 날인지 알 수 없기 때문이다. 그날부터 들어 있는 묶음을 가져오면 이어서 옮긴다. 휴장일이었다면 그 휴장일을 구간에 넣어 내보내면 된다.
- 장부 계산이 실패해도 행은 이미 쓴 상태다. 경고만 내고, 같은 묶음을 다시 가져오면 장부만 다시 계산한다.

## 규칙

- **행을 바꾸지 않는다.** `run_id`와 `data_revision`을 그대로 옮긴다. 그래서 PC2에서도 같은 규칙으로 최신 봉을 고른다: 리비전이 큰 것, 같으면 나중 run_id.
- **같은 행을 두 번 쓰지 않는다.** PK와 run_id가 같은 행은 이미 있는 것으로 보고 뺀다. 같은 묶음을 다시 가져오거나 구간이 겹쳐도 새 행만 들어간다.
- **기존 파일을 지우거나 덮지 않는다.** 새 행은 `part-import_<묶음>-NNNN.parquet` 새 파일로 쓴다.
- **가져올 때마다 기록을 남긴다.** 위치는 `data/manifests/silver_transfer_import_<묶음>.json`이고, git 비추적이다.
- **손상된 묶음은 받지 않는다.** 모든 파일의 sha256이 manifest와 맞아야 쓴다.

## 주의

- **잠정 봉은 거래량이 근사값이다.** 16:10 잠정 봉은 1분봉 합이다. 다음 날 08:30 확정 뒤에 다시 내보내면, PC2에서도 확정 봉이 잠정 봉을 이긴다.
- **날짜 기준은 `trading_date`다.** 수정주가 재조정(`daily-cache readjust`)이 과거 날짜의 봉을 새 리비전으로 다시 쓴 경우, 그 날짜까지 구간에 넣어야 옮겨진다.
- **PC2에서 따로 적재한 행은 남는다.** PC2가 같은 날을 따로 적재했다면 그 행도 남고, 두 PC의 행 중 리비전·run_id 규칙에 따라 최신이 쓰인다.

## 실측 (2026-11-04, PC1 = 작업실)

| 작업 | 결과 |
|---|---|
| `status` 9일 구간 | 14초. 같은 표를 만드는 기존 `day_status`는 153초 |
| `export` 11/2~11/3 | 53만 행 12.1MB, 23초 |
| 같은 PC로 `import --dry-run` | 새 행 0 |
| 빈 저장소(PC2 가정)로 `import` | 일봉 reader 702행, 최신 1분봉 509,330행이 PC1과 같음. 다시 가져오면 0행 |

# PC 간 수집 데이터 이관 (잠정·확정 봉 내보내기·가져오기)

> **봉 기준 `bars-1.0`** — 운영 PC(PC1)만 정기 적재(16:10 잠정 / 익일 08:30 확정)를 돌린다.
> 임시 PC(PC2)는 PC1이 내보낸 묶음을 가져와 스캐너만 돌린다. **PC2에서 적재를 돌리지 않는다** —
> 두 PC가 같은 앱키로 적재하면 KIS 토큰이 충돌한다(`docs/MIGRATION.md` 3절 ⑦).
> 도구: `data_transfer/`(상세 `data_transfer/README.md`)

날짜별로 수집된 일봉(잠정·확정 리비전 전부)·1분봉·체결량·분봉 수집 기록·지수 일봉을 zip
묶음 하나로 내보내고, 받는 PC에 **없는 행만** 가져옵니다. 같은 묶음을 두 번 가져와도 두 번째는
0행입니다(멱등).

**지원 입력 형식**:

```
/data-transfer                                  # = status (최근 14일 수집 현황)
/data-transfer status                           # 수집 현황
/data-transfer status 2026-11-03                # 지정일
/data-transfer status 2026-10-27 2026-11-03     # 구간
/data-transfer export 2026-11-03                # 지정일 내보내기
/data-transfer export 2026-10-27 2026-11-03     # 구간 내보내기
/data-transfer list                             # DOWNLOAD의 묶음 목록
/data-transfer import                           # DOWNLOAD의 최신 묶음 가져오기
/data-transfer import bars_20261103_작업실_….zip  # 특정 묶음
/data-transfer check                            # 최신 묶음을 쓰지 않고 새 행 수만 확인
```

날짜를 하나만 주면 지정일, 두 개면 시작일·종료일입니다. 날짜 형식은 `YYYY-MM-DD`이고
`20261103`·`11/3`처럼 주면 `YYYY-MM-DD`로 바꿔 실행합니다(연도가 없으면 올해).

---

## 실행

프로젝트 루트(`C:\A-AI\ai-berkshire-kr`)에서 실행합니다.

```bash
# 1. 수집 현황
python -m data_transfer.transfer status
python -m data_transfer.transfer status --date 2026-11-03
python -m data_transfer.transfer status --start 2026-10-27 --end 2026-11-03

# 2. 내보내기 (PC1) — data_transfer\DOWNLOAD\bars_<구간>_<PC이름>_<시각>.zip
python -m data_transfer.transfer export --date 2026-11-03
python -m data_transfer.transfer export --start 2026-10-27 --end 2026-11-03

# 묶음 목록
python -m data_transfer.transfer list

# 3. 가져오기 (PC2) — 확인 후 실제 쓰기
python -m data_transfer.transfer import --latest --dry-run
python -m data_transfer.transfer import --latest
python -m data_transfer.transfer import bars_20261103_작업실_20261104090000.zip
```

### 에이전트 실행 순서

| 입력 | 순서 |
|---|---|
| `status` | `status` 실행 → 아래 "출력 읽기"대로 요약 |
| `export` | ① 같은 구간 `status`로 그 날짜가 **확정 완료**인지 본다 ② `export` ③ 묶음 경로·크기·테이블별 행 수·날짜별 잠정/확정 수를 보고 |
| `import` | ① `import --dry-run`으로 새 행 수를 보여 준다 ② 이어서 `import` ③ 끝난 뒤 `status`로 가져온 날짜의 상태를 보고 |
| `check` | `import --latest --dry-run`만 — 쓰지 않는다 |

- export 전 상태가 **잠정만**이면 그대로 내보내되 "잠정 봉(거래량 근사)이 들어간다 — 다음 날 08:30 확정 뒤 다시 내보내면 확정 봉으로 바뀐다"고 알린다
- import가 `❌`(해시 불일치·형식 다름)로 끝나면 아무것도 쓰지 않은 것이다 — 묶음을 다시 복사하라고 안내한다
- 이 스킬은 git 커밋·푸시를 하지 않는다. DOWNLOAD 폴더는 git 비추적이다

---

## 출력 읽기

```
수집 현황 — 작업실  2026-11-04 06:29:21 KST
  장부: confirmed_through=2026-11-02  마지막 확정 2026-11-03 08:55:10  마지막 잠정 2026-11-03 16:48:04
    날짜        1분봉  잠정  확정  상태              KRX분봉 NXT분봉 체결량UN 지수
    2026-11-02    351     0   351  확정 완료             351     299      351    3
    2026-11-03    351   351     0  잠정만                351     299      351    0
```

| 열 | 뜻 |
|---|---|
| 1분봉·잠정·확정·상태 | 지수 구성종목 기준. 적재 로그 `[선행 점검]` 표와 **같은 판정**(최신 리비전, 구성종목 90%) |
| KRX분봉·NXT분봉 | 정규장 1분봉이 있는 종목 수(수집 기록 `bar_coverage` 기준). NXT는 약 299종목이 평소 값 |
| 체결량UN | KIS 매수·매도 체결량(통합) 종목 수 |
| 지수 | 지수 일봉 수(코스피·코스닥·코스피200 = 3). 당일분은 다음 날 08:30에 들어오므로 0이 정상 |

상태: `확정 완료` / `잠정만`(16:10 잠정 봉, 다음 08:30 확정이 덮는다) / `1분봉만` /
`미적재(또는 휴장)`(오늘·주말·휴장일이면 정상).

가져오기 출력의 `새 행 N / 이미 있음 M` — 받는 PC에 PK·run_id가 같은 행이 이미 있으면 뺍니다.

---

## 옮기는 것과 옮기지 않는 것

| 대상 | 옮김 | 비고 |
|---|---|---|
| `silver.daily_bars` | ○ | 잠정·확정 리비전 모두 — 받는 PC의 reader가 최신을 고른다 |
| `silver.minute_bars` | ○ | KRX·NXT 1분봉 |
| `silver.daily_trade_sides` | ○ | J·NX·UN |
| `silver.bar_coverage` | ○ | 분봉 수집 기록 |
| `silver.benchmark_bars` | ○ | 지수 일봉 |
| `silver.market_calendar` | × | 가져온 뒤 `calendar build`로 자동 재생성 |
| 구성종목 이력·`index_stocks.json` | × | 쓰기 경로가 정해져 있다(`/update-index`). 이력보다 뒤 날짜는 `universe_on()`이 `index_stocks.json`으로 대신하고 경고 |
| 적재 장부·bronze | × | PC마다 다른 운영 상태 |

---

## 주의사항

1. **행을 바꾸지 않는다** — run_id·data_revision 그대로. 기존 파일을 지우거나 덮지 않고
   `part-import_<묶음>-NNNN.parquet` 새 파일로 쓴다. 기록은 `data/manifests/silver_transfer_import_<묶음>.json`
2. **날짜 기준은 `trading_date`** — 수정주가 재조정(`daily-cache readjust`)으로 과거 봉이 바뀌면
   그 날짜까지 구간에 넣어야 옮겨진다
3. **적재 잠금을 잡는다** — 정기 적재가 도는 중이면 끝날 때까지 기다린다(`--lock-wait`, 기본 3600초).
   16:10~16:50, 08:30~08:55는 피하면 바로 실행된다
4. 전체 묶음 크기 실측: 이틀치 53만 행 12.1MB(2026-11-04)

---

## 다음 단계 연계

```
/data-transfer import   → /cvd-selection-scan, /ab-candidate-scan, /closing-bet-scan 등 스캐너 실행
/update-index           → PC2 구성종목 명단이 오래됐을 때
```

# 유니버스 기준 `universe-1.0` — 코스피200·코스닥150 명단의 단일 입구

작성일: 2026-10-05
코드: `src/cvd_obv/universe.py` · 테스트: `tests/unit/test_universe_standard.py`
상태: 코드·테스트 완료(전체 스위트 980 통과), **커밋 전**

## 1. 문제 — 명단을 만드는 곳이 도구마다 달랐다

| 도구 | 변경 전 명단 | 이력 밖 날짜 |
|---|---|---|
| CVD/OBV V1.5 `load_index` | `index_stocks.json` 직접 | — (항상 오늘 명단) |
| CVD 가속 | V1.5 `load_index` | — |
| 종가매수 | `index_stocks.json` 직접 | `--date`로 과거를 봐도 **오늘 명단**(생존편향) |
| 데일리 스캐너(gap-up·시장 접미사) | `index_stocks.json` 직접(2곳) | — |
| A·B 선정기 | 이력 `get_members` | 가장 가까운 관측일 구성 |
| 분봉 catchup | 이력 ∪ `index_stocks.json` | 이력 실패는 건너뜀 |
| 일봉 적재(`load_index_codes`), `live_feed.index_codes` | `index_stocks.json` 직접 | — |
| 백테스트(종가매수·A·B) | 이력 `membership_intervals` 각자 | 이력만 |

같은 날에도 도구마다 명단이 다를 수 있었고, `index_stocks.json`을 해석하는 코드가 6벌이었다.

## 2. 기준

| 함수 | 쓰는 곳 | 규칙 |
|---|---|---|
| `universe_on(day)` | **모든 스캐너**(실시간·`--date`) | ① 이력이 그날을 덮으면 이력 구성 ② 이력 마지막 관측일 **뒤**면 `index_stocks.json`과 이력 마지막 구성 중 관측일이 더 최근인 것(같으면 파일) + 경고("N일 차이") ③ 이력 시작 **전**이면 오류 — 오늘 명단을 과거에 쓰지 않는다 ④ 이력이 없으면 파일 관측일 이후에만 파일 |
| `universe_on(day, strict=True)` | 연구 | 이력만 |
| `membership_intervals()` / `flat_intervals()` / `member_index()` | 백테스트 모집단 | 이력만 |
| `current_universe()` | 수집 대상 선정, 종목 마스터(시장 접미사·이름) | `index_stocks.json`만 |
| `read_index_file()` | 위 함수들 | `index_stocks.json` 해석은 여기 하나(새·옛 형식) |

`Universe` 객체는 지수별 출처(`status`: `HISTORICAL` / `INDEX_FILE` / `HISTORY_NEAREST`),
경고, 종목명(파일 → `security_master` 순)을 함께 들고 다니고, `describe()`로 스냅샷에 남긴다.

`index_stocks.json`을 **쓰는** 곳은 `tools/update_index.py` 하나다(이력 마지막 관측 구성,
또는 키움·CSV).

## 3. 바뀐 곳

| 도구 | 변경 |
|---|---|
| `tools/cvd_obv_scan.py`(V1.5 입구) | `live_feed`가 V1.5 `load_index`를 `universe_on(오늘)`로 바꿔 넣는다(V1.5 파일은 그대로) |
| `tools/cvd_accel_scanner.py` | 같은 공급기로 명단을 만든다. 스냅샷에 `universe_standard` 기록 |
| `tools/closing_bet_scanner.py` | `load_universe(day)` = `universe_on(기준일)`. **`--date` 과거 스캔이 그날의 구성종목으로 바뀐다** |
| `tools/ab_candidate_scanner.py` | `load_universe` = `universe_on`. 이력 밖은 "가장 가까운 관측일" 대신 파일·이력 중 최근 것(지금은 둘 다 9/28 — 결과 동일) |
| `tools/daily_scanner.py` | gap-up 명단 = `universe_on(오늘)`, 시장 접미사 = `current_universe` |
| `src/.../daily_history.load_index_codes`, `live_feed.index_codes` | `read_index_file` / `current_universe`로 위임 |
| `src/.../minute_catchup.resolve_members` | 날짜별 `universe_on` ∪ `current_universe` |
| `tools/closing_bet_backtest.py`, `tools/ab_backtest_b.py` | `flat_intervals` / `membership_intervals` |
| `tools/baserate_probe.py` | `current_universe` |

그대로 둔 곳: replay·daily signals(이력 `get_members` 엄격 사용 — 연구 경로라 맞다),
`tools/update_index.py`(파일을 쓰는 쪽), V1.5 원본 `load_index`(해시 동결).

## 4. 확인

- 실데이터: 2026-10-05 → `INDEX_FILE`(이력 9/28, 7일 차이 경고), 오늘 명단과 동일 351종목 /
  2026-09-15·2018-03-02 → `HISTORICAL` / 2012-01-02 → KOSDAQ150 이력 시작(2015-07-13) 전이라 오류
- 종가매수 `--date 2026-09-22`, A·B 선정기 B모드 로컬 실행 정상
- 새 테스트 12건 + 전체 스위트 980 통과

## 5. 운영

- 이력이 오래될수록 "N일 차이" 경고가 커진다. 정기변경(6·12월)과 수시변경 뒤에는
  `/update-index` 절차(이력 갱신 → `update_index.py`)를 돌린다.
- 2026-10-24부터 16:10 잠정 적재 끝에 `universe refresh`가 이 절차를 매 거래일 돈다 —
  이력 끝 = KRX 자료가 있는 어제, 그래서 오늘 스캔의 차이 경고는 보통 1일이다.
- 경고가 나와도 스캔은 계속된다 — 최근 며칠은 명단이 바뀌지 않았을 가능성이 높지만,
  그 사이 편입·편출이 있었다면 반영되지 않는다(**불확실**).

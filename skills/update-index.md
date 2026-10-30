# 코스피200/코스닥150 구성종목 목록 갱신

**유니버스 이력**(`silver.universe_membership` — KRX 지수운영공지 첨부와 시총 항등식
검산으로 복원한 구성종목 이력)에서 마지막 관측일의 구성을 꺼내
`data/index_stocks.json` 파일 하나에 저장합니다.

`/cvd-obv-scan`, `daily-cache build`(일봉 수집 대상), `daily_scanner` 등이 이 파일을
대상 종목 목록으로 사용합니다. 백테스트·A/B 스캐너가 쓰는 유니버스와 **같은 원천**이라
두 목록이 어긋나지 않습니다.

> **2026-09-27 변경** — 예전 기본값(키움 ka20002)은 코스닥150 전용 코드가 없어 **코스닥
> 시총 상위 150개로 근사**했습니다. 2026-09-23 명단이 실제 구성과 37종목 달랐고, 일봉
> 수집에서 실제 구성종목 39개가 빠졌습니다. 키움 방식은 `--source kiwoom`으로만 씁니다.

**지원 입력 형식**:

```
/update-index                         # 코스피200 + 코스닥150 전체 갱신 (유니버스 이력)
/update-index kospi200                # 코스피200만 갱신
/update-index kosdaq150               # 코스닥150만 갱신
/update-index as-of 2026-06-12        # 이력의 특정 날짜 구성
/update-index show                    # 저장된 목록 확인 (갱신 안 함)
/update-index show kospi200           # 코스피200 저장 목록만 확인
/update-index verify                  # 저장 목록을 KRX 지수 시총과 대조
/update-index kiwoom                  # (근사) 키움 ka20002 — 이력을 쓸 수 없을 때만
/update-index from-csv 파일경로.csv kospi200   # KRX CSV 수동 임포트
```

---

## 실행 흐름

### 0단계: 정기 변경 후라면 — 유니버스 이력 먼저 갱신

> **2026-10-24부터 16:10 잠정 적재(`data-load provisional`)가 매 거래일 아래 0·1단계를 대신 돈다** —
> `python cvdobv.py universe refresh`(선택 단계). 이력 끝은 KRX 자료가 있는 어제, 복원에 미해결이
> 있으면 아무것도 쓰지 않는다. 결과는 `logs/provisional-load/YYYYMMDD.log`의 "구성종목 이력 갱신" 줄.
> 아래 수동 절차는 그 단계가 실패했거나 전 구간 `verify`(수십 분)가 필요할 때 쓴다.

코스피200/코스닥150은 매년 **6·12월** 정기 변경됩니다(2026-09-29 KRX 공지 목록으로 확인 —
9월 공지는 시가총액규모별·KRX100·KTOP30 등 다른 지수). 그 사이 합병·상장폐지 등 수시변경도
있습니다. 변경이 반영되려면 이력이 먼저 갱신돼야 합니다. 이력 마지막 관측일이 80일 넘게
지났으면 도구가 경고합니다.

> **2026-11-02 시행 예정 — KRX 대표지수 방법론 개정**(공지 2026-09-28): KRICS 산업분류
> 도입, 예비종목 제도 폐지. 대상 KOSPI 200·KOSDAQ 150·KRX 300·코리아밸류업. 12월
> 정기변경은 새 방법론으로 나올 가능성이 크다(추정).

```bash
python cvdobv.py data-load daily                       # 일봉(+1분봉·정규장 복원) — run-id는 자동(topup_load_…)
python cvdobv.py calendar build                        # 일봉에서 거래일 달력 연장 — rebuild가 달력 마지막 날에 씨앗을 놓는다
python cvdobv.py universe snapshot --index KOSPI200    # 오늘 구성(KIS, 2026-10-23~) — 역행 복원의 출발점. KRX 시총 검산 차이 0원이어야 정상
python cvdobv.py universe snapshot --index KOSDAQ150   # KIS는 코스닥150도 찍는다(키움은 불가)
python cvdobv.py universe krx-cache --start {이력 마지막일} --market BOTH    # 검산 재료
python cvdobv.py universe notices                      # 지수운영공지·첨부(정기변경 명단)
python cvdobv.py universe rebuild --index KOSPI200     # 매 거래일 시총 항등식 검산하며 복원
python cvdobv.py universe rebuild --index KOSDAQ150
python cvdobv.py universe verify --index KOSPI200      # 독립 재검산(수십 분)
python cvdobv.py universe verify --index KOSDAQ150
```

| 단계 | 하는 일 |
|---|---|
| `data-load daily` + `calendar build` | 거래일 달력은 일봉에서 파생된다. 달력이 늘지 않으면 krx-cache·rebuild가 새 거래일을 보지 못한다. 일봉을 `daily-cache build`로 따로 받을 때는 `--run-id`를 주지 않거나 `topup_load_` 형식을 쓴다 — 같은 봉은 사전순으로 나중 run_id가 이기는데, `topup-{날짜}-…` 같은 예전 형식은 `data-load`가 받은 값보다 앞에 정렬돼 새로 받은 값이 무시된다(`docs/BAR_STANDARD.md` §4) |
| `snapshot` | 키움 `ka20002`로 **오늘** 구성을 저장 — 복원의 출발점(씨앗). **코스피200만** 찍는다. 키움은 코스닥150 코드가 없어 코스닥 전체(2026-09-29: 1,823종목)를 주므로, 도구가 지수 크기의 1.2배를 넘는 응답은 저장을 거부한다. 코스닥150은 기존 스냅샷을 씨앗으로 쓰고, 복원이 씨앗을 검산해 틀리면 공지 명단으로 바로잡는다 |
| `krx-cache` | KRX Open API 일별 응답(종목별 상장시가총액·지수 시가총액)을 캐시 — 검산 재료. `.env`의 `KRX_AUTH_KEY` 필요 |
| `notices` | `index.krx.co.kr` 지수운영공지와 첨부 엑셀(편입·편출, 변경 후 전체 명단) 수집. 첨부는 캐시되어 전체 연도를 받아도 수 분. `--start-year`를 줘도 범위 밖 연도는 기존 파일에서 보존한다(2026-09-29 이전에는 덮어써서 이력이 사라졌다) |
| `rebuild` | 매 거래일 **지수 시가총액 = 구성종목 상장시가총액 합**(오차 0원)을 검산하며 명단을 복원 |
| `verify` | 적재된 이력을 다시 항등식에 넣어 독립 확인 |

### 1단계: 목록 갱신

```bash
python tools/update_index.py                       # 전체 (유니버스 이력 마지막 관측일)
python tools/update_index.py --index kosdaq150      # 코스닥150만
python tools/update_index.py --as-of 2026-06-12     # 특정 날짜 구성
```

| 출처 | 옵션 | 정확도 |
|------|------|--------|
| 유니버스 이력 | 기본 (`--source history`) | 실제 구성. 저장 직후 KRX 지수 시총 검산(2026-09-27: 두 지수 모두 오차 0.00%) |
| KRX CSV | `--from-csv 파일 --index …` | 실제 구성(수동 다운로드) |
| 키움 ka20002 | `--source kiwoom` | 코스피200은 직접 조회, **코스닥150은 시총 상위 근사**(2026-09-18 실측 시총합 +7.2%) |

동작 규칙:
- 이력 관측 구간 밖 날짜(`--as-of`)는 **거부**합니다 — 가까운 관측으로 조용히 대체하지 않습니다.
- 이력 조회에 실패하면 **기존 파일을 그대로 둡니다**. 키움 근사로 자동 대체하지 않습니다.
- 저장 후 자동으로 KRX 지수 시총과 대조합니다(`--verify`와 같은 검산). 오차 1% 이상이면 경고합니다.

### 2단계: 저장 확인

```bash
python tools/update_index.py --show
python tools/update_index.py --show --index kospi200
```

저장 경로: `data/index_stocks.json`

```json
{
  "kospi200": {
    "updated": "2026-09-27 19:23",
    "count":   201,
    "as_of":   "2026-09-22",
    "source":  "silver.universe_membership uv_… (KRX 지수 공시 기반 구성 이력)",
    "stocks":  [{"code": "000080", "name": "하이트진로"}, ...]
  },
  "kosdaq150": { ... }
}
```

`as_of`(이력 기준일)와 `source`(출처)는 추가 필드입니다. 기존 소비 코드는 `stocks`만
읽으므로 영향이 없습니다. 종목은 코드 오름차순이며, 순서에 의미는 없습니다.

---

## 이력을 쓸 수 없을 때 — 대체 경로

**방법 A — KRX 정보데이터시스템 CSV 다운로드 후 임포트** (정확)

1. https://data.krx.co.kr 접속
2. 기본통계 → 주가 → 지수 → 구성종목
3. 코스피200 / 코스닥150 선택 후 CSV 다운로드
4. 아래 명령으로 임포트:

```bash
python tools/update_index.py --from-csv ~/Downloads/kospi200.csv --index kospi200
```

**방법 B — 키움 ka20002** (코스닥150은 근사)

```bash
python tools/update_index.py --source kiwoom
```

`.env`에 `KIWOOM_APP_KEY` / `KIWOOM_APP_SECRET`이 필요합니다(.env.example 참고).

---

## 다음 단계 연계

```
/update-index                                 # 정기·수시 변경 후 목록 최신화
      ↓
python cvdobv.py data-load daily              # 갱신된 목록으로 1분봉·일봉·정규장 복원 적재
/cvd-obv-scan kospi200 --days 5                # 갱신된 목록으로 스캔
```

---

## 주요 주의사항

1. **6·12월 정기 변경 후 실행** — **이력 → 목록** 순서로 갱신
2. **이력이 오래되면 목록도 오래된다** — 도구는 이력에 없는 변경을 알 수 없다. 80일 경고를 무시하지 말 것
3. **키움 방식은 근사** — 코스닥150을 시총 상위로 대신하므로 실제 구성과 다르다. 이력·CSV가 없을 때만

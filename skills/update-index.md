# 코스피200/코스닥150 구성종목 목록 갱신

키움증권 REST API(`ka20002` 업종별주가요청)로 코스피200 / 코스닥150 구성종목을
조회해서 `data/index_stocks.json` 파일 하나에 저장합니다.

`/cvd-obv-scan` 스킬이 스캔 대상 종목 목록으로 이 파일을 사용하므로,
코스피200/코스닥150이 정기 변경되는 **분기 1회(3·6·9·12월) 재실행**이 필요합니다.

**지원 입력 형식**:

```
/update-index                         # 코스피200 + 코스닥150 전체 갱신
/update-index kospi200                # 코스피200만 갱신
/update-index kosdaq150               # 코스닥150만 갱신
/update-index show                    # 저장된 목록 확인 (갱신 안 함)
/update-index show kospi200           # 코스피200 저장 목록만 확인
/update-index from-csv 파일경로.csv kospi200   # KRX CSV 수동 임포트
```

---

## 사전 준비 (최초 1회)

### 키움 API 키 등록

`.env` 파일에 아래 항목을 설정합니다(.env.example 참고):

```
KIWOOM_APP_KEY=앱키
KIWOOM_APP_SECRET=앱시크릿
```

키 발급: https://openapi.kiwoom.com

키가 없으면 자동 조회가 모두 실패하고, 아래 수동 방법으로 안내됩니다.

---

## 실행 흐름

### 1단계: 갱신 실행

```bash
python3 tools/update_index.py                     # 전체 갱신 (코스피200 + 코스닥150)
python3 tools/update_index.py --index kospi200     # 코스피200만
python3 tools/update_index.py --index kosdaq150    # 코스닥150만
```

내부 동작:

| 인덱스 | 키움 파라미터 | 방식 |
|--------|--------------|------|
| 코스피200 | `mrkt_tp=2, inds_cd=201` | ka20002로 직접 지원 — 200종목 그대로 저장 |
| 코스닥150 | `mrkt_tp=1, inds_cd=101` | 코스닥 전체 조회 후 응답 순서 상위 150개로 근사 (코스닥150 전용 코드 없음) |

`next-key` / `cont-yn` 응답 헤더 기반으로 자동 페이지네이션(최대 20회)합니다.

### 2단계: 저장 확인

```bash
python3 tools/update_index.py --show
python3 tools/update_index.py --show --index kospi200
```

저장 경로: `data/index_stocks.json`

```json
{
  "kospi200": {
    "updated": "2026-09-08 11:20",
    "count":   200,
    "stocks":  [{"code": "005930", "name": "삼성전자"}, ...]
  },
  "kosdaq150": {
    "updated": "2026-09-08 11:20",
    "count":   150,
    "stocks":  [{"code": "247540", "name": "에코프로비엠"}, ...]
  }
}
```

---

## 키움 API 실패 시 — 수동 대체 경로

키움 API 미연결이거나 조회에 실패하면 자동으로 아래 안내가 출력됩니다.

**방법 A — KRX 정보데이터시스템 CSV 다운로드 후 임포트**

1. https://data.krx.co.kr 접속
2. 기본통계 → 주가 → 지수 → 구성종목
3. 코스피200 / 코스닥150 선택 후 CSV 다운로드
4. 아래 명령으로 임포트:

```bash
python3 tools/update_index.py --from-csv ~/Downloads/kospi200.csv --index kospi200
```

**방법 B — 네이버금융에서 수동 확인**

```
https://finance.naver.com/sise/sise_index.naver?code=KPI200
https://finance.naver.com/sise/sise_index.naver?code=KSDAQ150
```

---

## 다음 단계 연계

목록 갱신 후 `/cvd-obv-scan`이 이 파일을 스캔 대상으로 사용합니다:

```
/update-index                       # 분기 변경 후 목록 최신화
      ↓
/cvd-obv-scan kospi200 --days 5      # 갱신된 목록으로 CVD/OBV 스캔
```

---

## 주요 주의사항

1. **분기별 정기 실행 필요**: 코스피200/코스닥150은 매년 3·6·9·12월 정기 변경 — 변경 시점마다 재실행
2. **코스닥150은 근사값**: 키움 API가 코스닥150 전용 코드를 제공하지 않아 코스닥 전체 응답 순서 상위 150개를 사용 — 실제 지수 구성종목과 일부 다를 수 있음(공식 확정은 방법 A의 KRX CSV로 교차 확인 권장)
3. **키움 API 키 필요**: 미설정 시 자동 조회 불가 → CSV 수동 임포트로 대체
4. **API 호출 제한**: 키움 REST API 초당 5회 제한 — 페이지네이션으로 여러 번 호출되므로 완료까지 수 초~수십 초 소요

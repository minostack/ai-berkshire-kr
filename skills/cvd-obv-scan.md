# CVD/OBV 기반 당일 투자 후보 스캐너

키움증권 REST API로 코스피200 / 코스닥150 종목의 분봉 데이터를 수집해서
CVD(Cumulative Volume Delta)와 OBV(On-Balance Volume)를 계산하고
스코어 상위 종목을 출력합니다.

**지원 입력 형식**:

```
/cvd-obv-scan                              # 코스피200 기본 스캔 (5일, 5분봉, 상위10)
/cvd-obv-scan kospi200                     # 코스피200 상위 10개
/cvd-obv-scan kosdaq150                    # 코스닥150 상위 10개
/cvd-obv-scan both                         # 코스피200 + 코스닥150 동시
/cvd-obv-scan kospi200 --days 10           # 10영업일 CVD
/cvd-obv-scan kospi200 --days 20           # 20영업일 CVD
/cvd-obv-scan kospi200 --top 20            # 상위 20개
/cvd-obv-scan kospi200 --period 1          # 1분봉 (더 정밀)
/cvd-obv-scan detail 009150                # 단일 종목 상세 분석
/cvd-obv-scan update-index                 # 종목 목록 최신화
```

---

## 사전 준비 (최초 1회)

### 환경변수 등록 (키움 API 키)

```powershell
# Windows
[System.Environment]::SetEnvironmentVariable("KIWOOM_APP_KEY","앱키","User")
[System.Environment]::SetEnvironmentVariable("KIWOOM_APP_SECRET","시크릿","User")
```

```bash
# macOS
export KIWOOM_APP_KEY=앱키
export KIWOOM_APP_SECRET=시크릿
```

### 종목 목록 생성 (분기별 변경 시 재실행)

```bash
python3 tools/update_index.py
```

코스피200은 키움 `ka20002` (업종별주가요청, `inds_cd=201`)로,
코스닥150은 코스닥 전체 조회 후 상위 150개로 자동 구성합니다.
결과는 `data/index_stocks.json`에 저장됩니다.

---

## 실행 흐름

### 0단계: 사전 확인

```bash
# 키움 API 연결 및 장 상태 확인
python3 tools/daily_scanner.py check-market
```

출력 예시:
```
📅 날짜: 2026-09-04 (목요일)
🏛️  장 상태: 정규장 운영 중 (09:00~15:30)
🔑 키움 API: ✅ 연결됨
```

---

### 1단계: 종목 목록 로드

`data/index_stocks.json`에서 코스피200 / 코스닥150 종목 목록을 로드합니다.

```bash
python3 tools/kiwoom_cvd_obv_scanner.py scan --index kospi200 --top 10
```

파일이 없으면:
```bash
python3 tools/update_index.py   # 먼저 실행
```

---

### 2단계: 분봉 데이터 수집 (키움 ka10080)

키움 `ka10080` (주식분봉조회) API로 각 종목의 분봉 데이터를 수집합니다.

| 파라미터 | 값 | 설명 |
|---------|-----|------|
| `stk_cd` | 종목코드 | 6자리 |
| `tic_scope` | 1/3/5/10/15/30/60 | 분봉 단위 (기본 5분) |
| `upd_stkpc_tp` | 1 | 수정주가 적용 |

키움 분봉 API는 **최대 160일치 과거 데이터**를 제공하므로
5일 / 10일 / 20일 모두 단 한 번 실행으로 수집됩니다.
일별 누적 불필요 — 오늘 한 번으로 완료.

---

### 3단계: CVD 계산 (분봉 기반)

분봉 캔들 몸통 비율로 매수/매도 체결량을 추정해서 누적합니다.

```
양봉: 매수 체결 = 거래량 × (0.5 + |종가-시가|/|고가-저가| × 0.5)
음봉: 매도 체결 = 거래량 × (0.5 + |종가-시가|/|고가-저가| × 0.5)
도지: 매수 = 매도 = 거래량 × 0.5

CVD = Σ(매수 체결 - 매도 체결)  ← 기간 전체 누적
```

**bull_days**: N영업일 중 매수 우위 일수 (예: 3/5일)

---

### 4단계: OBV 계산 (일봉 기반)

키움 `ka10081` (주식일봉조회) API로 60일치 일봉을 수집해서 계산합니다.

```
종가 상승 → 거래량 더함
종가 하락 → 거래량 뺌
보합       → 변화 없음

5영업일 다이버전스:
  가격↓ + OBV↑ → 🟢 강세 다이버전스 (저점 매집, 반등 기대)
  가격↑ + OBV↓ → 🔴 약세 다이버전스 (상승 신뢰도 저하)
```

---

### 5단계: 종합 스코어 산출 및 출력

| 신호 | 점수 |
|------|------|
| CVD > CVD 이동평균 | +2 |
| CVD 모멘텀 양전 (최근 5봉 기울기) | +1 |
| 매수 비율 > 55% | +1 |
| bull_days ≥ 3 (N일 중 3일 이상 매수 우위) | +1 |
| CVD 급등 (현재 CVD > MA × 2배) | +1 |
| OBV > OBV MA20 | +2 |
| OBV 5일 기울기 양전 | +1 |
| **OBV 강세 다이버전스** | **+2** |
| OBV 약세 다이버전스 | -2 |

**판정 기준**:
- 8점 이상 → 🟢 강한 매수 신호
- 5점 이상 → 🟢 매수 신호
- 2점 이상 → 🟡 중립
- 2점 미만 → 🔴 신호 약함

---

### 6단계: 리포트 저장

```
reports/daily-scan/cvd-obv-{인덱스}-{YYYYMMDD}.md
```

---

## 보조 명령어

```bash
# 단일 종목 CVD/OBV 상세 분석
python3 tools/kiwoom_cvd_obv_scanner.py detail --code 009150

# 10일 / 20일 CVD로 더 긴 추세 확인
python3 tools/kiwoom_cvd_obv_scanner.py scan --index kospi200 --days 10 --top 10
python3 tools/kiwoom_cvd_obv_scanner.py scan --index kospi200 --days 20 --top 10

# 1분봉으로 더 정밀한 CVD
python3 tools/kiwoom_cvd_obv_scanner.py scan --index kospi200 --period 1 --top 10

# 종목 목록 확인
python3 tools/update_index.py --show
python3 tools/update_index.py --show --index kospi200
```

---

## 데이터 수집 규칙

```
방법 1 (필수): python3 tools/kiwoom_cvd_obv_scanner.py — 키움 API 직접 호출
방법 2 (보완): python3 tools/daily_scanner.py — Yahoo Finance 기반
❌ 금지: Fetch(finance.naver.com/...) 직접 접속
```

---

## 다음 단계 연계

CVD/OBV 스캔으로 발굴된 후보 종목은 아래로 심층 분석합니다:

```
/investment-team 종목명(종목코드)   → 4대가 관점 심층 분석 (권장)
/investment-research 종목명(코드)   → 단일 에이전트 심층 분석
/daily-scanner indicators 종목코드  → RSI/MACD/볼린저밴드 추가 확인
```

**권장 사용 흐름**:
```
/cvd-obv-scan kospi200 --days 5     # 코스피200 CVD/OBV 스캔
      ↓ 상위 후보 선정
/daily-scanner indicators 009150     # 기술적 지표 교차 확인
      ↓ 유망 종목 확정
/investment-team 삼성전기(009150)    # 4대가 심층 분석
```

---

## 주요 주의사항

1. **장 중 실행 권장**: CVD는 분봉 기반이므로 장 중(09:00~15:30) 또는 장 마감 직후 실행이 가장 의미 있습니다
2. **CVD는 근사값**: 틱 데이터가 아닌 분봉 캔들로 추정하므로 절대값보다 **방향과 다이버전스**가 중요합니다
3. **API 호출 제한**: 키움 REST API 초당 5회 제한 — 200개 전종목 스캔 시 약 1~2분 소요
4. **종목 목록 갱신**: 코스피200/코스닥150은 매년 6월 정기 변경 → `update_index.py` 재실행 필요
5. **이 스캔 결과는 투자 권유가 아닙니다** — 반드시 심층 분석 후 최종 판단

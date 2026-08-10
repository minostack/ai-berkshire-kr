# 당일 투자 후보 스캐너: 기술적 분석 + 전일 특이 종목

당일 또는 전일 시장 데이터를 기반으로 기술적 조건을 충족하는 투자 후보 종목을 발굴합니다.

**지원 입력 형식**:

```
/daily-scanner                          # 기본 스캔 (전체 조건)
/daily-scanner momentum                 # 모멘텀 돌파 종목
/daily-scanner volume                   # 거래량 급증 종목
/daily-scanner gap                      # 갭 상승/하락 종목
/daily-scanner oversold                 # 과매도 반등 후보
/daily-scanner sector 반도체            # 특정 섹터 기술적 스캔
/daily-scanner broker 한국투자          # 증권사 API 연동 (KIS)
/daily-scanner broker 키움              # 키움 OpenAPI 연동
```

---

## 설계 철학

> "차트는 시장 참여자들의 심리가 응축된 것이다. 패턴이 반복되는 이유는 사람의 탐욕과 공포가 반복되기 때문이다."

이 스킬은 **단기 기술적 신호**를 포착해서 당일 또는 단기 투자 후보를 발굴합니다.
`/kr-stock-scanner`가 중장기 가치 기반 스크리닝이라면, 이 스킬은 **단기 모멘텀과 가격 패턴** 기반입니다.

**두 스킬의 역할 분리**:

| 스킬 | 기준 | 시간 지평 | 용도 |
|------|------|---------|------|
| `/kr-stock-scanner` | 재무·가치 | 중장기 (1년+) | 종목 발굴 |
| `/daily-scanner` | 기술적·모멘텀 | 단기 (당일~수주) | 진입 타이밍 |

두 스킬을 함께 사용하면 **가치 있는 종목의 좋은 타이밍**을 잡을 수 있습니다.

---

## ⚠️ 데이터 수집 규칙

```
방법 1 (최우선): python3 tools/daily_scanner.py — 증권사 API 또는 Yahoo Finance
방법 2 (보완):   WebSearch — 전일 특이 종목, 뉴스 기반 이슈
방법 3 (최후):   URL 안내 후 수동 확인
❌ 금지: Fetch(finance.naver.com/...) 직접 접속
```

---

## 실행 흐름

### 0단계: 실행 환경 확인

```bash
# 오늘 날짜 및 장 운영 여부 확인
python3 tools/daily_scanner.py check-market

# 증권사 API 연동 여부 확인
python3 tools/daily_scanner.py check-broker
```

출력 예시:
```
📅 날짜: 2026-08-10 (월요일)
🏛️  장 상태: 정규장 운영 중 (09:00~15:30)
🔑 KIS API: ✅ 연결됨
🔑 키움 API: ❌ 미연결
```

---

### 1단계: 전일 특이 종목 수집

전일 마감 기준 아래 조건으로 특이 종목을 수집합니다:

#### 1-1. 거래량 급증 종목

```bash
python3 tools/daily_scanner.py volume-surge \
  --ratio 3.0 \
  --min-cap 500
```

기준:
- 당일 거래량 ≥ 20일 평균 거래량 × 3배
- 시가총액 500억 이상 (소형주 노이즈 제거)
- 주가 변동률 +3% 이상 동반 (매도 급증 제외)

#### 1-2. 신고가 돌파 종목

```bash
python3 tools/daily_scanner.py new-high \
  --period 52w \
  --min-cap 1000
```

기준:
- 52주 신고가 돌파
- 또는 전일 종가 기준 20일/60일 고점 돌파
- 거래량 평균 대비 1.5배 이상 동반

#### 1-3. 갭 상승 종목 (전일 종가 대비)

```bash
python3 tools/daily_scanner.py gap-up \
  --min-gap 3.0
```

기준:
- 당일 시가 > 전일 종가 × 1.03 (3% 이상 갭 상승)
- 갭 상승 후 유지 여부 (시가 = 고점 근처)

#### 1-4. 과매도 반등 후보

```bash
python3 tools/daily_scanner.py oversold \
  --rsi-max 30 \
  --days 5
```

기준:
- RSI(14) ≤ 30 (과매도 구간)
- 5일 연속 하락 후 반등 시작 신호
- 볼린저 밴드 하단 터치 후 반등

---

### 2단계: 기술적 지표 분석

1단계에서 수집한 후보 종목에 대해 핵심 기술적 지표를 계산합니다.

```bash
python3 tools/daily_scanner.py indicators \
  --tickers 005930 000660 035720 \
  --indicators RSI,MACD,BB,MA,STOCH
```

#### 계산 지표 목록

| 지표 | 파라미터 | 매수 신호 조건 |
|------|---------|------------|
| **RSI** | 14일 | 30 이하 (과매도) 후 반등 / 50 상향 돌파 |
| **MACD** | 12,26,9 | 골든크로스 (MACD > Signal) |
| **볼린저밴드** | 20일, 2σ | 하단 터치 후 중심선 회복 / 상단 돌파 |
| **이동평균** | 5/20/60/120일 | 정배열 (5>20>60>120) |
| **스토캐스틱** | 14,3,3 | %K가 %D 상향 돌파, 20 이하에서 |
| **OBV** | — | OBV 상승 + 주가 횡보 (매집 신호) |
| **거래량 이평** | 20일 | 당일 거래량 > 20일 평균 × 2 |

---

### 3단계: 패턴 인식

```bash
python3 tools/daily_scanner.py patterns \
  --tickers 005930 000660 \
  --patterns all
```

#### 인식 패턴

**강세 패턴** (매수 신호):
- 망치형 (Hammer): 긴 아래꼬리 + 작은 몸통
- 상승 장악형 (Bullish Engulfing): 전일 음봉을 완전히 감싸는 양봉
- 샛별형 (Morning Star): 3일 패턴, 바닥 반전
- 컵앤핸들 (Cup & Handle): 중장기 돌파 패턴

**중립/주의 패턴** (추가 확인 필요):
- 도지 (Doji): 시가 ≈ 종가, 방향 불확실
- 팽이형 (Spinning Top): 작은 몸통, 긴 윗꼬리+아래꼬리

**약세 패턴** (매수 보류):
- 교수형 (Hanging Man): 고점에서의 망치형 → 하락 전환 신호
- 저녁별형 (Evening Star): 고점 3일 패턴, 하락 전환

---

### 4단계: 증권사 API 연동 (선택)

DART나 Yahoo Finance보다 **실시간·정확한** 데이터가 필요할 때 사용합니다.

#### 한국투자증권 KIS API

```bash
# API 키 환경변수 설정 (최초 1회)
export KIS_APP_KEY="발급받은앱키"
export KIS_APP_SECRET="발급받은시크릿"
export KIS_ACCOUNT_NO="계좌번호-상품코드"  # 예: 50123456-01

# 연결 테스트
python3 tools/daily_scanner.py check-broker --broker kis

# KIS API로 실시간 체결 데이터 수집
python3 tools/daily_scanner.py kis-realtime \
  --tickers 005930 000660 009150
```

KIS API 주요 기능:
```
국내주식 현재가 조회     → /uapi/domestic-stock/v1/quotations/inquire-price
국내주식 거래량 순위     → /uapi/domestic-stock/v1/quotations/volume-rank
국내주식 등락률 순위     → /uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice
프로그램매매 현황        → /uapi/domestic-stock/v1/quotations/program-trade-by-stock
외국인/기관 순매수 현황  → /uapi/domestic-stock/v1/quotations/inquire-investor
```

#### 키움증권 OpenAPI

```bash
export KIWOOM_APP_KEY="발급받은키"

python3 tools/daily_scanner.py kiwoom-screen \
  --condition "거래량급증"
```

키움 OpenAPI 주요 기능:
```
조건검색 (사전 설정 조건 실행)  → 가장 강력한 기능
실시간 데이터 구독              → 체결가, 호가, 잔량
차트 데이터 조회 (분봉/일봉)    → 기술적 지표 계산용
```

---

### 5단계: WebSearch로 뉴스·이슈 교차 확인

기술적 신호가 있는 종목에 대해 당일 뉴스를 확인합니다.

```
WebSearch: "{종목명} 오늘 뉴스 {날짜}"
WebSearch: "{종목명} 공시 오늘"
WebSearch: "오늘 상한가 종목 이유 {날짜}"
WebSearch: "전일 거래량 급증 종목 {날짜}"
```

**뉴스 기반 필터**:
- 호재 뉴스 + 기술적 신호 = 강한 매수 후보
- 악재 뉴스 + 거래량 급증 = 제외 (세력 매도 가능성)
- 뉴스 없음 + 거래량 급증 = 세력 매집 가능성 → 추가 관찰

---

### 6단계: 최종 후보 리스트 출력

---

## 📋 당일 투자 후보 스캔 결과

**스캔 일시**: {날짜} {시각}
**데이터 기준**: 전일 종가 / 당일 실시간 (증권사 API 연동 시)

---

### 🔴 핵심 후보 (당일 진입 검토)

| 종목명 | 코드 | 시장 | 현재가 | 등락률 | 거래량비 | RSI | MACD | 신호 | 진입 전략 |
|--------|------|------|--------|--------|---------|-----|------|------|---------|
| | | | | | | | | | |

**신호 범례**: 🔺거래량급증 ⭐신고가 📈골든크로스 🔄갭상승 💚과매도반등

---

### 🟡 관심 후보 (추가 확인 후 판단)

| 종목명 | 코드 | 주목 이유 | 확인 필요 사항 |
|--------|------|---------|-------------|

---

### ⚠️ 제외 종목 및 사유

| 종목명 | 기술적 신호 | 제외 사유 |
|--------|----------|---------|

---

### 📊 당일 시장 전체 요약

| 항목 | 값 |
|------|-----|
| KOSPI 등락률 | |
| KOSDAQ 등락률 | |
| 상한가 종목 수 | |
| 거래량 급증 종목 수 | |
| 신고가 돌파 종목 수 | |
| 외국인 순매수 / 순매도 | |
| 기관 순매수 / 순매도 | |

---

### 7단계: 리포트 저장

`reports/daily-scan/daily-scan-{YYYYMMDD}.md`로 저장합니다.

누적 관리:
```
reports/daily-scan/
├── daily-scan-20260810.md
├── daily-scan-20260811.md
└── daily-scan-20260812.md
```

---

## 주요 주의사항

1. **기술적 분석은 확률이지 확실성이 아닙니다** — 신호가 맞을 확률을 높이는 것이 목적
2. **단기 스캔 결과로 장기 투자 결정 금지** — 당일 신호는 당일 또는 단기 판단에만 활용
3. **뉴스 없는 거래량 급증 주의** — 세력 매집일 수도 있지만 작전주일 수도 있음
4. **증권사 API 사용 시 모의투자 계좌로 먼저 테스트** 권장
5. **이 스캔 결과는 투자 권유가 아닙니다** — 최종 판단은 본인 책임
6. **심층 분석 필수**: 당일 스캔 후보는 반드시 `/investment-team`으로 검증 후 투자 결정

# 재무 데이터 수집 및 교차 검증 규칙

모든 기업 재무 데이터가 포함된 리서치에 적용됩니다.
**핵심 데이터는 반드시 2개 이상의 독립 출처에서 수집하고, 편차 > 1% 시 반드시 표기합니다.**

---

## ⚠️ 데이터 수집 도구 사용 제한 (반드시 숙지)

Claude Code의 내장 Fetch 도구는 아래 사이트에서 **접속이 차단**됩니다.
이 사이트들은 반드시 **WebSearch 도구** 또는 **python3 tools/ 스크립트**로만 접근해야 합니다.

| 차단 사이트 | 오류 유형 | 대체 방법 |
|-----------|---------|---------|
| `finance.naver.com` | Claude Code unable to fetch | WebSearch 또는 `kstock_data.py` |
| `comp.fnguide.com` | Parse Error: Missing expected CR | WebSearch로 대체 |
| `data.krx.co.kr` | 403 Forbidden | `kr_scanner.py` 또는 WebSearch |

**올바른 데이터 수집 우선순위 (방법 1 → 2 → 3 순서로 시도)**:

```
방법 1: python3 tools/kstock_data.py 또는 kr_scanner.py (DART API, 가장 정확)
방법 2: WebSearch 도구 (검색엔진 경유, 차단 우회 가능)
방법 3: DART 웹사이트 URL 안내 후 수동 확인
```

**절대 사용 금지**: `Fetch(https://finance.naver.com/...)` 형태의 직접 접속

---

## 시장별 데이터 출처 우선순위

### 🇰🇷 한국주 (KOSPI / KOSDAQ)

| 우선순위 | 출처 | 수집 방법 | 비고 |
|---------|------|---------|------|
| 1순위 ✅ | **DART Open API** | `python3 tools/kstock_data.py financials {코드}` | API 키 필요, 가장 정확 |
| 2순위 ✅ | **DART 재무비율 일괄** | `python3 tools/kr_scanner.py dart-ratio` | 전 상장사 일괄 조회 |
| 3순위 ✅ | **WebSearch** | `WebSearch: "{종목명} ROE 영업이익률 재무제표 2024"` | 차단 없이 접근 가능 |
| 원본 확인 | **DART 사업보고서** | WebSearch: `"dart.fss.or.kr {종목코드} 사업보고서"` | 원문 PDF |
| ❌ 사용금지 | **네이버금융 직접 접속** | `Fetch(finance.naver.com/...)` | 차단됨 — WebSearch로 대체 |
| ❌ 사용금지 | **fnguide 직접 접속** | `Fetch(comp.fnguide.com/...)` | 차단됨 — WebSearch로 대체 |

**자동 수집 명령어 (Bash로 반드시 실행):**
```bash
# [방법 1] DART API로 핵심 재무 데이터 (API 키 필요)
python3 tools/kstock_data.py financials {종목코드}

# [방법 1] 실시간 시세 및 밸류에이션
python3 tools/kstock_data.py quote {종목코드}

# [방법 1] 전 상장사 재무비율 일괄 조회 (스크리닝용)
python3 tools/kr_scanner.py dart-ratio --year 2024

# [방법 1] 종목 코드 검색
python3 tools/kstock_data.py search {기업명}
```

**방법 1 실패 시 방법 2 (WebSearch) 검색 키워드:**
```
"{종목명} {종목코드} 재무제표 ROE 영업이익 2024"
"{종목명} 사업보고서 매출액 순이익 site:dart.fss.or.kr"
"{종목명} 주가 PER PBR 배당수익률"
```

> DART API 키 등록: `export DART_API_KEY=발급받은키`
> API 키 발급: https://opendart.fss.or.kr/intro/main.do (무료, 1~2일 소요)

---

### 🇺🇸 미국주

| 우선순위 | 출처 | URL | 수집 방법 |
|---------|------|-----|---------|
| 1순위 (주) | **macrotrends** | macrotrends.net/stocks/charts/{ticker} | WebSearch 또는 직접 접속 |
| 2순위 (부) | **stockanalysis** | stockanalysis.com/stocks/{ticker}/financials | WebSearch 또는 직접 접속 |
| 원본 1차 | **SEC EDGAR** | sec.gov/cgi-bin/browse-edgar | 10-K / 10-Q 원문 |

---

### 🇭🇰 홍콩주

| 우선순위 | 출처 | URL | 수집 방법 |
|---------|------|-----|---------|
| 1순위 (주) | **aastocks** | aastocks.com/tc/stocks/analysis/company-fundamental | 직접 접속 |
| 2순위 (부) | **macrotrends** (ADR 코드) | 삼성전자 → SSNLF, LG에너지 → LGCLF | 직접 접속 |
| 원본 1차 | **HKEX 공시** | hkexnews.hk | 연간보고서 PDF |

---

## 실행 규칙

### 1단계: 데이터 수집

**한국주는 `kstock_data.py`를 먼저 실행한 뒤 부족한 데이터를 WebSearch로 보완합니다.**

각 재무 지표(매출액, 순이익, 영업이익, 영업현금흐름, 부채비율 등)를 **1순위 출처**와 **2순위 출처**에서 각각 수집합니다.

### 2단계: 편차 계산 및 표기

```
편차율 = |1순위 값 - 2순위 값| / 1순위 값 × 100%
```

| 편차 | 처리 방법 |
|------|---------|
| ≤ 1% | ✅ 일치 — 1순위 값 사용, 두 출처 모두 표기 |
| 1% ~ 5% | ⚠️ "데이터 차이 있음" 표기, 두 값과 가능한 원인 명시 |
| > 5% | ❌ "중대 차이" 표기 — 반드시 원본 재무제표(DART 사업보고서) 확인 후 사용 |

### 3단계: 데이터 표기 형식

핵심 데이터는 반드시 아래 형식으로 출처를 표기합니다:

```
매출액: 302.2조원 ✅
  - DART:     302.3조원
  - 네이버금융: 302.1조원
  - 편차: 0.07%
```

편차 있는 경우:
```
영업이익: 32.7조원 ⚠️ 데이터 차이 있음
  - DART:     32.7조원 (K-IFRS 연결)
  - 네이버금융: 30.1조원 (별도 재무제표)
  - 편차: 7.9% — 원인: 연결 vs 별도 재무제표 기준 차이
```

---

## 자주 발생하는 편차 원인

| 원인 | 설명 |
|------|------|
| 연결 vs 별도 재무제표 | 한국주에서 가장 흔함 — 연결 기준이 기본, 별도는 지주사 단독 |
| K-IFRS vs K-GAAP | 회계 기준 차이, 특히 이익 항목에서 발생 |
| 환율 환산 시점 | 달러/원 환산 시 기준일 차이로 편차 발생 |
| 잠정 실적 vs 확정 실적 | 분기 잠정 실적 발표 후 감사보고서 확정치와 차이 |
| 재무연도 정의 | 12월 결산 vs 3월 결산 기업 혼용 시 |
| 데이터 업데이트 지연 | 일부 플랫폼이 최신 분기 실적을 아직 미반영 |

---

## 특별 규칙

1. **비상장 기업** (비상장 자회사 등): 단일 출처만 가능한 경우 데이터 앞에 `[추정]` 표기, 교차 검증 생략
2. **분기 vs 연간 데이터**: 교차 검증은 연간 데이터 우선 — 분기 데이터는 일부 플랫폼에서 지연될 수 있음
3. **원본 재무제표 우선**: 두 출처 모두 DART 원본과 다를 경우 DART 사업보고서를 최우선으로 하고 출처 오류 표기
4. **kstock_data.py 실패 시**: 도구 응답이 없으면 WebSearch로 DART 또는 네이버금융에서 직접 수집하고 수동 확인으로 대체

---

## 주가 복권 기준 (역사적 시계열 필수)

주가 복권 방식을 혼용하면 역사적 주가 위치, 장기 수익률, 역사적 밸류에이션 분위가 모두 왜곡됩니다.

| 기준 | 의미 | 용도 |
|------|------|------|
| 무복권 | 실제 체결가, 배당락/권리락일 갭 발생 | 현시점 스냅샷에만 사용 |
| 전복권 | 최신가 기준으로 과거 주가를 소급 조정 | 역사적 주가 비교, N년 수익률, 역사적 PER 밴드 — **반드시 이것 사용** |
| 후복권 | 상장 첫날 기준으로 주가를 전진 조정 | 역사적 총수익률/연환산 수익률 계산 |

**적용 규칙:**

1. 역사적 가격 분석은 **전복권** 통일 — 같은 분석 내에서 복권/무복권 혼용 금지
2. 현재 시가총액/현재 PER은 **현재 실제 주가 × 현재 발행주식수** — 복권과 무관
3. 분할/무상증자를 거친 주당 지표(역사적 EPS, 역사적 주가)는 복권 후 비교
4. 총수익률/연환산 수익률은 배당 포함 필요 (후복권이 배당 포함됨)
5. 유상증자/자사주 소각 후 시가총액 검증은 최신 발행주식수 기준 사용
   (`financial_rigor.py verify-market-cap` — 편차 > 5% 시 경고 출력)

---

## 한국주 종목별 빠른 출처 색인

| 종목 | 주요 출처 | 보조 출처 |
|------|---------|---------|
| 삼성전자 (005930) | `kstock_data.py financials 005930` | dart.fss.or.kr → 삼성전자 |
| SK하이닉스 (000660) | `kstock_data.py financials 000660` | finance.naver.com/item/coinfo.naver?code=000660 |
| LG에너지솔루션 (373220) | `kstock_data.py financials 373220` | dart.fss.or.kr → LG에너지솔루션 |
| 카카오 (035720) | `kstock_data.py financials 035720` | finance.naver.com/item/coinfo.naver?code=035720 |
| NAVER (035420) | `kstock_data.py financials 035420` | dart.fss.or.kr → NAVER |
| 현대차 (005380) | `kstock_data.py financials 005380` | finance.naver.com/item/coinfo.naver?code=005380 |
| 삼성바이오로직스 (207940) | `kstock_data.py financials 207940` | dart.fss.or.kr → 삼성바이오로직스 |

**미국주 빠른 색인:**

| 종목 | 주요 출처 | 보조 출처 |
|------|---------|---------|
| NVDA | macrotrends.net/stocks/charts/NVDA | stockanalysis.com/stocks/nvda |
| TSMC (TSM) | macrotrends.net/stocks/charts/TSM | stockanalysis.com/stocks/tsm |
| Apple (AAPL) | macrotrends.net/stocks/charts/AAPL | stockanalysis.com/stocks/aapl |

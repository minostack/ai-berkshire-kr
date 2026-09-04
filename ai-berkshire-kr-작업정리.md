# AI Berkshire KR — 작업 정리 문서

**작성일**: 2026-08-18  
**기준 레포**: `github.com/(내 계정)/ai-berkshire-kr`  
**원본**: `github.com/xbtlin/ai-berkshire` 포크

---

## 1. 프로젝트 개요

AI Berkshire KR은 Warren Buffett, Charlie Munger, 단용핑(段永平), 리루(Li Lu) 4대 가치투자 대가의 방법론을 Claude Code 기반 멀티 에이전트로 구현한 투자 리서치 프레임워크입니다. 원본(중국어)을 한국 시장과 한국어에 맞게 전면 재구성했습니다.

---

## 2. 한국어화 작업 목록

### 2-1. Skills 파일 (`.claude/commands/` 설치)

| 파일명 | 용도 | 작업 내용 |
|--------|------|---------|
| `investment-team.md` | 4개 에이전트 병렬 분석 | 전면 한국어화, 1.76~1.8단계 신규 추가, Fetch 제한 규칙 추가 |
| `earnings-review.md` | 실적 정밀 분석 | 전면 한국어화, 한국주 출처(DART) 반영 |
| `financial-data.md` | 재무 데이터 수집 규칙 | 한국주 우선순위 재편, Fetch 차단 사이트 명시 |
| `kr-stock-scanner.md` | 한국 주식 종목 발굴 | **신규 스킬** — 가치 기반 중장기 스크리닝 |
| `daily-scanner.md` | 당일 투자 후보 발굴 | **신규 스킬** — 기술적 분석 + 증권사 API |
| `income-investment.md` | 배당·인컴 투자 분석 | **신규 스킬** — 한국 세금 체계 반영 |
| `thesis-drift.md` | 두 시점 투자 논거 비교 | **신규 스킬** — 논거 침식 탐지 |

### 2-2. Tools 파일 (`tools/` 폴더)

| 파일명 | 용도 | 작업 내용 |
|--------|------|---------|
| `financial_rigor.py` | 재무 정밀 검증 | 한국어화, 단위 포맷(조/억/만), 출처 DART로 변경 |
| `report_audit.py` | 리포트 출고 전 검수 | 한국어화, 출처 DART/네이버금융/KRX로 교체 |
| `kstock_data.py` | 한국주 데이터 수집 | **신규** — DART API + 네이버금융, corpCode.xml ZIP 방식 |
| `kr_scanner.py` | 섹터 스캔 + DART 재무비율 | **신규** — 섹터별 종목 리스트, dart-ratio 일괄 조회 |
| `daily_scanner.py` | 기술적 분석 + KIS API | **신규** — RSI/MACD/BB/스토캐스틱, KIS 실시간 연동 |
| `stock_screener.py` | 모멘텀 스크리닝 | 한국주 watchlist 추가, .KS 자동 접미사 |
| `log-command.sh` | 명령어 로그 | 한국어화 |

### 2-3. 설정 파일

| 파일명 | 작업 내용 |
|--------|---------|
| `CLAUDE.md` | 전면 한국어 재작성, 리포트 폴더 구조 변경, GitHub 푸시 자동 제안 비활성화 |
| `.gitignore` | 한국 환경 추가 (KIS 토큰, `.env`, Windows 파일) |
| `.editorconfig` | `.md` 줄바꿈 예외, `.bat` CRLF 추가 |
| `settings.local.json` | `defaultMode: auto`, `WebFetch(domain:*)` 전체 허용 |
| `.github/ISSUE_TEMPLATE/` | 이슈 템플릿 5개 한국어화 |

---

## 3. 리포트 폴더 구조

### `/investment-team` 실행 시

```
reports/{기업명}_{YYYYMMDD}/
├── 01-비즈니스모델분석-단용핑관점.md
├── 02-재무밸류에이션분석-버핏관점.md
├── 03-산업경쟁분석-멍거관점.md
├── 04-리스크경영진평가-리루관점.md
└── 최종리포트.md
```

같은 종목을 여러 날 분석하면 날짜별로 폴더가 분리되어 이력 관리가 됩니다.

```
reports/
├── 삼성전기_20260101/   ← 1월 분석
├── 삼성전기_20260401/   ← 4월 분석 (분기 실적 후)
└── 삼성전기_20260701/   ← 7월 분석
```

---

## 4. 종목 검색 방법 및 종류

### 4-1. 중장기 가치 기반 스크리닝 — `/kr-stock-scanner`

**용도**: 중장기 투자 후보 발굴 (1년 이상 보유 전제)  
**방법**: DART API + WebSearch 기반 재무 필터링

```
/kr-stock-scanner                        # 기본 전 섹터 스캔
/kr-stock-scanner AI 반도체              # 테마/섹터 지정
/kr-stock-scanner KOSPI 대형주           # 시장 전체
/kr-stock-scanner ROE 15% 이상           # 조건 지정
```

**2단계 필터 방식**:

| 단계 | 기준 | 설명 |
|------|------|------|
| 1단계 정량 필터 | ROE ≥ 10%, 영업이익률 ≥ 8%, 부채비율 ≤ 150%, PBR ≤ 1.5배 | DART API 자동 조회 |
| 2단계 정성 필터 | 비즈니스 지속성, 가격결정권, 주주친화, 안전마진 | 4가지 질문 중 3개 이상 YES |

**보조 도구**:
```bash
python3 tools/kr_scanner.py list-sectors        # 섹터 목록
python3 tools/kr_scanner.py sector 반도체       # 섹터 종목
python3 tools/kr_scanner.py info 009150         # 단일 종목 요약
python3 tools/kr_scanner.py dart-ratio          # 전 상장사 재무비율 일괄 (DART API 필요)
```

---

### 4-2. 당일 기술적 분석 스캐닝 — `/daily-scanner`

**용도**: 당일/단기 투자 후보 발굴 (당일~수주)  
**방법**: Yahoo Finance + KIS API + WebSearch

```
/daily-scanner                           # 전체 조건 스캔
/daily-scanner momentum                  # 모멘텀 돌파
/daily-scanner volume                    # 거래량 급증
/daily-scanner gap                       # 갭 상승/하락
/daily-scanner oversold                  # 과매도 반등 후보
/daily-scanner sector 반도체             # 섹터 기술적 스캔
```

**기술적 지표 (순수 Python 계산 — 외부 라이브러리 불필요)**:

| 지표 | 파라미터 | 매수 신호 조건 |
|------|---------|------------|
| RSI | 14일 | 30 이하 과매도 후 반등 |
| MACD | 12,26,9 | 골든크로스 (히스토그램 양전환) |
| 볼린저밴드 | 20일, 2σ | 하단 터치 후 중심선 회복 |
| 이동평균 | 5/20/60/120일 | 정배열 확인 |
| 스토캐스틱 | 14,3,3 | %K > %D, 20 이하에서 |
| 캔들 패턴 | — | 망치형, 상승장악형, 샛별형 등 |

**보조 도구**:
```bash
python3 tools/daily_scanner.py check-market         # 장 운영 상태
python3 tools/daily_scanner.py check-broker         # API 연결 확인
python3 tools/daily_scanner.py indicators 009150    # 단일 종목 지표
python3 tools/daily_scanner.py indicators 005930 000660 009150  # 다중 종목
python3 tools/daily_scanner.py volume-surge         # 거래량 급증
python3 tools/daily_scanner.py oversold             # 과매도 후보
python3 tools/daily_scanner.py kis-realtime 009150  # KIS 실시간
```

---

### 4-3. 심층 분석 — `/investment-team` `/investment-research`

**용도**: 종목 확정 후 4대가 관점 심층 분석  
**방법**: 4개 에이전트 병렬 실행 → Team Lead 종합

```
/investment-team 삼성전기(009150)        # 4대가 병렬 분석 (권장)
/investment-research 삼성전기(009150)    # 단일 에이전트 심층 분석
/investment-checklist 삼성전기, SK하이닉스  # 여러 종목 동시 체크
```

---

### 4-4. 실적 분석 — `/earnings-review` `/earnings-team`

```
/earnings-review 삼성전자 2025Q4         # 단일 실적 분석
/earnings-team 삼성전자 2025Q4           # 4대가 병렬 실적 해석
```

---

### 4-5. 기타 스킬

| 스킬 | 용도 |
|------|------|
| `/portfolio-review` | 보유 포지션 전체 리뷰 |
| `/thesis-tracker 삼성전기` | 투자 논거 지속 추적 |
| `/thesis-drift 삼성전기 파일1.md 파일2.md` | 두 시점 논거 변화 비교 |
| `/news-pulse 삼성전기` | 뉴스 4차원 병렬 분석 |
| `/income-investment 삼성전자 mode=new` | 배당·인컴 포지션 분석 |
| `/industry-research 반도체` | 산업 전체 리서치 |
| `/industry-funnel 반도체` | 산업 → 최종 3종목 압축 |

---

## 5. 권장 사용 흐름

```
① /kr-stock-scanner 또는 /daily-scanner
   → 후보 종목 발굴 (중장기 or 단기)
        ↓
② /investment-checklist 종목A, 종목B
   → 후보군 빠른 10분 체크리스트
        ↓
③ /investment-team 종목명(코드)
   → 4대가 관점 심층 분석 및 최종 리포트 생성
        ↓
④ /thesis-tracker 종목명
   → 매수 후 투자 논거 지속 추적
        ↓
⑤ /thesis-drift 종목명 이전.md 최신.md
   → 분기별 논거 변화 점검
```

---

## 6. 데이터 출처 및 API

| API | 용도 | 등록 방법 |
|-----|------|---------|
| **DART Open API** | 공식 재무제표, 전 상장사 재무비율 | `export DART_API_KEY=키` |
| **KIS API** | 실시간 시세, 거래량 순위, 외국인·기관 순매수 | `export KIS_APP_KEY=키` `export KIS_APP_SECRET=시크릿` |
| **Yahoo Finance** | OHLCV 15분 지연 데이터 (무료, 키 불필요) | 자동 |
| **네이버금융** | WebSearch 경유로만 접근 (Fetch 직접 차단) | WebSearch 도구 |

### Fetch 차단 사이트 (WebSearch로 대체 필수)

```
❌ Fetch(finance.naver.com/...)   → WebSearch 또는 kstock_data.py
❌ Fetch(comp.fnguide.com/...)    → WebSearch로 대체
❌ Fetch(data.krx.co.kr/...)     → kr_scanner.py 또는 WebSearch
```

---

## 7. 주요 설정 파일 위치

| 파일 | 위치 | 용도 |
|------|------|------|
| `CLAUDE.md` | 레포 루트 | 에이전트 행동 규칙, 리포트 저장 형식 |
| `settings.local.json` | `~/.claude/` | 권한 설정, defaultMode: auto |
| `.gitignore` | 레포 루트 | GitHub 업로드 제외 목록 |
| `.editorconfig` | 레포 루트 | 코드 편집 형식 통일 |

---

## 8. 알려진 주의사항

| 항목 | 내용 |
|------|------|
| KIS API 토큰 | 24시간 유효, 자동 갱신 (`daily_scanner.py`가 처리) |
| KIS 토큰 파일 | `.kis_token.json` — `.gitignore`에 추가 및 `git rm --cached` 필수 |
| Windows 인코딩 | `PYTHONIOENCODING=utf-8` 또는 `daily_scanner.py` 내 자동 처리 |
| Fetch 차단 | `finance.naver.com`, `comp.fnguide.com` 직접 접속 불가 |
| 백그라운드 에이전트 | WebSearch 권한이 `~/.claude/settings.json`에 있어야 함 |
| 리포트 GitHub 푸시 | 에이전트가 자동 제안하지 않음 — 사용자가 직접 결정 |

---

## 9. 스킬 설치 방법

**Windows:**
```bat
scripts\install-claude-commands.bat
```

**macOS:**
```bash
./scripts/install-claude-commands.sh
```

개별 스킬 수동 설치:
```bash
# macOS
cp skills/daily-scanner.md ~/.claude/commands/

# Windows
copy skills\daily-scanner.md %USERPROFILE%\.claude\commands\
```

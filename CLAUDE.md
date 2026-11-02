# AI Berkshire KR — 프로젝트 지침

## 프로젝트 개요

Claude Code 기반의 가치투자 리서치 스킬 모음 (한국어 버전).
4대가 프레임워크: 버핏, 멍거, 단용핑, 리루.
원본 GitHub: xbtlin/ai-berkshire
포크 GitHub: (내 계정)/ai-berkshire-kr

## 프로젝트 구조

```
skills/          — 투자 리서치 스킬 정의 (.md), ~/.claude/commands/에 복사하여 사용
tools/           — 보조 도구 (financial_rigor.py 정밀 계산)
reports/         — 투자 리서치 리포트 출력
assets/          — 이미지 등 정적 자원
local/           — 로컬 전용 (Git 비추적, 민감 정보)
```

## 리포트 디렉토리 구조

`/investment-team` 리포트는 **종목명_날짜** 폴더로 저장합니다.
같은 종목을 여러 시점에 분석하면 날짜별 폴더로 이력이 관리됩니다.

```
reports/
├── 삼성전기_20260101/           — 종목명 + 조회날짜 폴더 (investment-team)
│   ├── 01-비즈니스모델분석-단용핑관점.md
│   ├── 02-재무밸류에이션분석-버핏관점.md
│   ├── 03-산업경쟁분석-멍거관점.md
│   ├── 04-리스크경영진평가-리루관점.md
│   └── 최종리포트.md
├── 삼성전기_20260401/           — 동일 종목 다음 분기 분석
│   └── 최종리포트.md
├── 삼성전자_20260101/           — 다른 종목
│   └── 최종리포트.md
├── 삼성전자/                    — 단일 리포트 스킬 (기업명 폴더)
│   ├── 삼성전자-research-20260101.md
│   ├── 삼성전자-earnings-2025Q4.md
│   └── 삼성전자-thesis.md
├── 반도체-industry-20260101.md  — 산업 리포트는 루트에
├── AI인프라-funnel-20260101.md  — 펀넬 스크리닝 리포트는 루트에
├── portfolio-latest.md           — 포트폴리오 리포트는 루트에
└── 멀티기업-checklist-20260101.md
```

## 리포트 명명 규칙

| 스킬 | 파일명 형식 | 예시 |
|------|-----------|------|
| /investment-team | `{기업명}_{YYYYMMDD}/` 디렉토리 내 4개 관점 + 최종 리포트 | `reports/삼성전기_20260101/최종리포트.md` |
| /investment-research | `{기업명}-research-{YYYYMMDD}.md` | `reports/삼성전자/삼성전자-research-20260101.md` |
| /investment-checklist | `{기업명}-checklist-{YYYYMMDD}.md` | `reports/삼성전자/삼성전자-checklist-20260101.md` |
| /industry-research | `{산업명}-industry-{YYYYMMDD}.md` (루트) | `reports/반도체-industry-20260101.md` |
| /industry-funnel | `{산업명}-funnel-{YYYYMMDD}.md` (루트) | `reports/AI인프라-funnel-20260101.md` |
| /private-company-research | `{기업명}-private-{YYYYMMDD}.md` | `reports/카카오페이/카카오페이-private-20260101.md` |
| /earnings-review | `{기업명}-earnings-{기간}.md` | `reports/삼성전자/삼성전자-earnings-2025Q4.md` |
| /earnings-team | `{기업명}/` 디렉토리 내 4개 대가 관점 + 리서치 초안 | `reports/삼성전자/삼성전자-earnings-2025Q4.md` |
| /thesis-tracker | `{기업명}-thesis.md` (장기 유지) | `reports/삼성전자/삼성전자-thesis.md` |
| /portfolio-review | `portfolio-latest.md` (루트, 지속 업데이트) | `reports/portfolio-latest.md` |
| /management-deep-dive | `{기업명}-management-{YYYYMMDD}.md` | `reports/삼성전자/삼성전자-management-20260101.md` |

## /investment-team 파일 구조

폴더명은 **종목명_조회날짜(YYYYMMDD)** 형식입니다.
같은 종목을 여러 시점에 분석하면 날짜별로 폴더가 생성되어 이력 관리가 됩니다.

```
reports/{기업명}_{YYYYMMDD}/
├── 01-비즈니스모델분석-단용핑관점.md
├── 02-재무밸류에이션분석-버핏관점.md
├── 03-산업경쟁분석-멍거관점.md
├── 04-리스크경영진평가-리루관점.md
└── 최종리포트.md                     — Team Lead 종합 리포트

예시:
reports/삼성전기_20260101/   ← 1월 분석
reports/삼성전기_20260401/   ← 4월 분석 (분기 실적 반영)
reports/삼성전기_20260701/   ← 7월 분석
```

## 투자 리서치 핵심 원칙 (최우선)

- **객관성, 객관성, 객관성** — 모든 투자 분석은 사실과 데이터에 근거해야 하며, 주관적 추측을 금합니다
- **'사실'과 '의견' 엄격 구분**: 사실은 데이터로 뒷받침하고, 의견은 반드시 "의견" 또는 "추정"으로 표기
- **선입견 금지**: 매수/매도 관점을 미리 설정하지 않고, 데이터 → 논리 → 결론 순으로 전개. 결론은 데이터에서 자연스럽게 도출
- "나는 생각한다", "명백히" 등 주관적 표현 금지 → "데이터에 따르면", "증거에 의하면", "출처 X에 따르면"으로 대체
- **양면 제시**: 모든 핵심 판단에는 반론도 첨부("반면에..."), 독자가 스스로 판단하도록
- 불확실한 사항은 솔직하게 "불확실" 또는 "데이터 부족"으로 표기하고 추측으로 채우지 않기
- 모든 스킬(investment-team, investment-research, earnings-review 등) 실행 시 위 원칙 준수 필수

## 리포트 언어 및 스타일

- 모든 리포트는 **한국어** 작성
- 스타일: 직접적이고 명확하게, 불필요한 표현 없이
- 데이터에는 반드시 출처 표기, 핵심 데이터는 최소 2개 이상 출처 교차 검증
- 추정값은 반드시 "추정"으로 명시
- 평점은 ★ 기호 사용 (★1~5점), 반점 없음
- 버핏/멍거/단용핑/리루의 어록 인용으로 포인트 강화

## 한국 데이터 출처 기준

| 시장 | 1차 출처 | 2차 출처 |
|------|---------|---------|
| 한국주 (KOSPI/KOSDAQ) | 전자공시시스템 DART (dart.fss.or.kr) | 네이버금융 / KRX |
| 미국주 | SEC EDGAR / 회사 IR | macrotrends / stockanalysis |
| 홍콩주 | 홍콩거래소 공시 | aastocks / macrotrends |
| 중국주 (A주) | 거래소(SSE/SZSE) | 동방재부 / 거조자순 |

두 출처 간 오차 >1% 시 반드시 표시

## GitHub 운영

- 로컬 클론 경로: `C:\A-AI\ai-berkshire-kr\`
- 원격 저장소: `https://github.com/(내 계정)/ai-berkshire-kr.git`
- 푸시 전 반드시 `git pull --rebase origin main` (원격에 새 커밋이 있을 수 있음)
- 커밋 메시지는 **한국어**로, 변경 내용을 명확히 기술
- 중간 과정 파일은 푸시하지 않음 (최종 리포트만 푸시)
- **리포트 완성 후 GitHub 푸시는 사용자가 직접 결정한다 — 에이전트가 푸시를 제안하거나 묻지 않는다**

## 자주 쓰는 명령어

```bash
# 리포트를 GitHub에 푸시 (사용자가 직접 실행)
cd C:\A-AI\ai-berkshire-kr
git add reports/삼성전기_20260101/
git commit -m "삼성전기 20260101 투자 리서치 리포트 추가"
git pull --rebase origin main
git push origin main
```

## 주의사항

- 시가총액은 반드시 수동 검증: 주가 × 총발행주식수, 리포트 시총과 대조
- 통화 단위 명확히 표기 (원/달러/홍콩달러), 혼동 방지
- PER/ROE 등 지표는 tools/financial_rigor.py로 정밀 계산

## 봉 기준 (bars-1.0) — 모든 스캐너·백테스트 공통

상세: `docs/BAR_STANDARD.md`, 코드: `src/cvd_obv/bar_standard.py`

- 일봉 = **정규장·수정주가** 하루 한 행. 2026-09-14 이후 키움 일봉(EXTENDED, 종가 = 애프터마켓 최종가)은 판정에 쓰지 않는다
- 일봉 거래량 = KRX 정규장 + 시간외 종가, **2026-08-03 이후는 NXT 메인마켓(09:00:30~15:20) 포함**(A안, 표시 `NXT_MAIN_VOLUME_INCLUDED` — 9/14 이후는 확정 복원, 8/3~9/11은 NXT 1분봉 소급 `nxt-history`). 가격은 KRX. 그 전 날은 KRX만 — 거래량 시계열 정의가 8/3에서 바뀐다(A·B 스캐너는 RVOL 창이 경계를 걸치면 경고하고 스냅샷 `volume_definition`에 남긴다)
- 분봉 원천 = **1분봉 하나**. 5분·30분봉은 1분봉을 합성한다
- 적재 원천 = **KIS**(2026-10-23부터, 일봉 `FHKST03010100`·1분봉 `FHKST03010230`·지수 일봉 `FHKUP03500100`). 키움은 `--source kiwoom`/`CVD_MARKET_SOURCE=kiwoom` 대체 원천. KIS 1분봉은 누적거래대금(`cum_traded_value`)을 준다. 스캐너 실행 중 1분봉(`live_feed`)도 지난 날 = 로컬, 오늘 = KIS(`live_minutes.py`). 구성종목 스냅샷도 KIS(`kis_universe.py`, 코스닥150 포함)
- 일봉 reader는 `read_standard_daily()`만 쓴다 — `silver.daily_bars`를 직접 읽어 `price_scope`를 거르지 않으면 같은 날이 두 행이 된다
- 적재 = 2단계. 16:10 `data-load provisional`(밀린 날 확정 → 오늘 1분봉 합 **잠정** 일봉, 거래량 근사) / 익일 08:30 `data-load daily`(정규장 **확정** 복원, 잠정 봉을 덮는다). 둘 다 마지막 확정일 이후를 스스로 찾는다
- CVD/OBV 실행 입구는 `tools/cvd_obv_scan.py`. `tools/kiwoom_cvd_obv_scanner.py`(V1.5)는 해시 동결 — 수정 금지

## 유니버스 기준 (universe-1.0) — 코스피200·코스닥150 명단

상세: `docs/UNIVERSE_STANDARD.md`, 코드: `src/cvd_obv/universe.py`

- 이력 갱신 = 16:10 잠정 적재 끝 `universe refresh`(선택 단계, 2026-10-24부터) — 이력·`index_stocks.json`을 KRX 자료가 있는 어제까지 늘린다. 미해결이 있으면 쓰지 않는다
- 스캐너 명단은 `universe_on(기준일)` 하나 — 이력이 덮으면 이력, 이력 뒤면 `index_stocks.json`(경고), 이력 전이면 오류
- 백테스트 모집단은 `membership_intervals()`/`flat_intervals()`(이력만), 수집 대상은 `current_universe()`
- `data/index_stocks.json`을 직접 열지 않는다 — 해석은 `read_index_file()`, 쓰기는 `tools/update_index.py`만

## .gitignore 핵심 항목

```
local/                    # 민감 정보, 절대 공개 금지
reports/portfolio-latest.md   # 실제 포지션 비공개
logs/command-log.jsonl    # 명령어 로그 비공개
```

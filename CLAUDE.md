# AI Berkshire KR — 프로젝트 지침

## 프로젝트 개요

Claude Code 기반의 가치투자 리서치 스킬 모음 (한국어 버전).
4대가 프레임워크: 버핏, 멍거, 단용핑, 리루.
원본 GitHub: xbtlin/ai-berkshire
포크 GitHub: minostack/ai-berkshire-kr

## 프로젝트 구조

```
skills/          — 투자 리서치 스킬 정의 (.md), ~/.claude/commands/에 복사하여 사용
tools/           — 보조 도구 (financial_rigor.py 정밀 계산)
reports/         — 투자 리서치 리포트 출력
assets/          — 이미지 등 정적 자원
local/           — 로컬 전용 (Git 비추적, 민감 정보)
```

## 리포트 디렉토리 구조

모든 리포트는 **기업명** 폴더로 구분하고, 해당 기업 관련 리포트를 모두 그 안에 저장합니다:

```
reports/
├── 삼성전자/                    — 삼성전자 관련 모든 리포트
│   ├── 삼성전자-research-20260101.md
│   ├── 삼성전자-earnings-2025Q4.md
│   └── 삼성전자-thesis.md
├── 카카오/                      — 카카오 관련 모든 리포트
├── SK하이닉스/
├── 반도체-industry-20260101.md  — 산업 리포트는 루트에
├── AI인프라-funnel-20260101.md  — 펀넬 스크리닝 리포트는 루트에
├── portfolio-latest.md           — 포트폴리오 리포트는 루트에
└── 멀티기업-checklist-20260101.md
```

## 리포트 명명 규칙

| 스킬 | 파일명 형식 | 예시 |
|------|-----------|------|
| /investment-team | `{기업명}/` 디렉토리 내 4개 관점 + 최종 리포트 | `reports/삼성전자/최종리포트.md` |
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

```
reports/{기업명}/
├── README.md                         — 리서치 프레임워크 개요 + 핵심 결론
├── 01-비즈니스모델분석-단용핑관점.md
├── 02-재무밸류에이션분석-버핏관점.md
├── 03-산업경쟁분석-멍거관점.md
├── 04-리스크경영진평가-리루관점.md
└── 최종리포트.md                     — Team Lead 종합 리포트
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

<!--

## GitHub 운영

- 로컬 클론 경로: `C:\myProject_room\agentic_AI\ai-berkshire-kr\`
- 원격 저장소: `https://github.com/(내 계정)/ai-berkshire-kr.git`
- 푸시 전 반드시 `git pull --rebase origin main` (원격에 새 커밋이 있을 수 있음)
- 커밋 메시지는 **한국어**로, 변경 내용을 명확히 기술
- 중간 과정 파일은 푸시하지 않음 (최종 리포트만 푸시)

## 자주 쓰는 명령어

```bash
# 리포트를 GitHub에 푸시
cd C:\myProject_room\agentic_AI\ai-berkshire-kr
git add reports/기업명/리포트.md
git commit -m "삼성전자 리서치 리포트 추가"
git pull --rebase origin main
git push origin main
```

-->

## 주의사항

- 시가총액은 반드시 수동 검증: 주가 × 총발행주식수, 리포트 시총과 대조
- 통화 단위 명확히 표기 (원/달러/홍콩달러), 혼동 방지
- PER/ROE 등 지표는 tools/financial_rigor.py로 정밀 계산
- 리포트 완성 후 GitHub 푸시 여부 능동적으로 확인

## .gitignore 핵심 항목

```
local/                    # 민감 정보, 절대 공개 금지
reports/portfolio-latest.md   # 실제 포지션 비공개
logs/command-log.jsonl    # 명령어 로그 비공개
```

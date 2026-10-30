# CVD 종가매수 종목 선정 (설계 cvdsel-2.0)

코스피200 + 코스닥150 종목에서 **매수 우위 체결량 흐름(CVD) + 추세 + 거래 참여 + 마감 수급**을
모두 만족하는 종가매수 후보를 하루 최대 3종목 고릅니다. 전략 명세
`CVD_stock_selection_strategy.md` v2.0의 **종목 선정**만 구현한 것입니다(매수 주문·청산·자금
관리는 없음). 코드: `cvd_selection/`, 설명: `cvd_selection/README.md`.

**지원 입력 형식**:

```
/cvd-selection-scan                      # 최근 일봉 거래일(잠정 봉 포함), 저장
/cvd-selection-scan date 2026-10-29      # 기준일 지정
/cvd-selection-scan rejects 20           # 근접 탈락 20종목 사유까지
/cvd-selection-scan nosave               # 저장하지 않음
/cvd-selection-scan rvol 1.5             # 임계값 덮어쓰기(명세 시작값이 아님 — 결과에 경고)
```

인자는 조합할 수 있습니다(예: `/cvd-selection-scan date 2026-10-29 rejects 10`).

**이 스킬은 데이터를 적재하지 않습니다.** 기적재분만 읽습니다(API 호출·저장소 쓰기 없음).
16:10 `data-load provisional` 뒤에는 당일 **잠정** 봉(1분봉 합)으로, 익일 08:30
`data-load daily` 뒤에는 **확정** 봉으로 판정합니다.

---

## 실행 절차

### 1단계: 인자 → 명령 변환

| 입력 | 옵션 |
|---|---|
| (없음) | `--save` |
| `date YYYY-MM-DD` | `--as-of YYYY-MM-DD` |
| `rejects N` | `--show-rejects N` |
| `nosave` | `--save` 빼기 |
| `dr5 X` / `rvol X` / `clv X` / `atrp X` / `avg X`(원) | `--min-dr5` / `--min-rvol` / `--min-clv` / `--max-atrp` / `--min-avg-value` |

```bash
python -m cvd_selection.scan --save --show-rejects 10 [옵션]
```

- 351종목 약 1분 → Bash `timeout` 600000 지정
- 기준일 일봉이 없으면 "휴장일이거나 16:10 잠정 적재 전"으로 끝난다 — 사용자에게 그대로 전달하고
  적재 명령은 실행하지 않는다

### 2단계: 결과 보고

저장 위치: `cvd_selection/output/cvdsel-{D}-{run_id}.json|csv`(실행마다 새 파일).
사용자에게 다음을 간결하게 전달합니다.

1. 기준일 D · 수급 확정 기준일 D-1 · 당일 봉 **잠정/확정** 여부 · 유니버스 경고
2. 깔때기: 유니버스 → 품질 통과 → 필수 조건 통과 → 선정
3. 선정 종목(코드, 종목명, 점수, DR5·POS5·RVOL·CLV·VWAP 이격). 0종목이면 "신규 매수 없음"
4. 탈락 사유 상위와 근접 탈락 종목(미충족 조건)
5. `[사전등록 CVDSEL 전향 기록]` 줄 — **판정이 아닌 기록**이라고 밝힌다
6. 저장 파일 경로

보고 원칙(CLAUDE.md): 조건 충족 **사실**만 전달합니다. "매수 적기" 같은 평가를 붙이지 않고,
개별 종목 의견은 "의견"으로 표기하며 반대 가능성도 함께 적습니다.

---

## 판정 정의 (값은 `cvd_selection/config.py` — 명세의 검증용 시작값)

| 단계 | 조건 |
|---|---|
| 유동성 | AVG_A20(직전 20일 평균 거래대금, 1분봉 범위) ≥ 100억 원 |
| 추세 | C > MA20 > MA60, MA20 > MA20(D-5) |
| 수급(확정 D-5..D-1) | DR5 ≥ +5%, POS5 ≥ 3, DR2 > 0, 직전일 흐름 > 0 — KIS 체결량 UN(KRX+NXT) |
| 거래 참여 | RVOL ≥ 1.3 — 당일·과거 20일 모두 1분봉 범위 거래량(블록딜 제외) |
| 마감 | CLV ≥ 0.70, 0 < VWAP 이격 ≤ 5%, 마감 30분 Rlate ≥ 0, HoldClose ≥ −0.5% |
| 변동성 | ATRP14 ≤ 4% |
| 순위 | DR5·POS5·RVOL·CLV·돌파·VWAP 배점 합 → 상위 3 |

데이터 기준:
- 1분봉 범위 = KRX(애프터마켓 제외) + **2026-08-03부터** NXT 메인마켓(bars-1.0 경계)
- 1분봉은 원주가 — 일봉 종가와 5% 넘게 다르면 수정 계수로 보고 거래량·가격을 환산
  (`MINUTE_VOLUME_ADJUSTED`, `MINUTE_PRICE_ADJUSTED`)
- 체결량은 KRX 애프터마켓·NXT 프리/애프터 포함(세션 분리 불가) — 흐름 비율로만 쓴다

---

## 주의사항

1. **수익성이 확인되지 않았습니다.** 사전등록 CVDSEL 백테스트(2010~2021, 일봉 재현)에서 명세
   일봉 조건 S0은 유니버스 대비 −0.55%p, 지수 대비 −0.95%p였고, 완화 가설 3개는 판정
   불가(UNDERPOWERED)였습니다 — `reports/CVDSEL-선정조건완화-백테스트-20261025.md`
2. 필수 조건을 모두 통과하는 날이 드뭅니다(0종목인 날이 흔함)
3. 임계값을 바꾸면 명세 시작값이 아니며, 바꾼 값의 성과를 주장하려면 `/prereg-backtest`로 검증
4. 회귀 테스트: `python -m pytest cvd_selection/tests tests/unit/test_cvdsel_backtest.py -q`
5. **이 스캔 결과는 투자 권유가 아닙니다** — 반드시 심층 분석 후 최종 판단

---

## 다음 단계 연계

```
/news-pulse 종목명              → 수급 유입 원인 확인
/investment-team 종목명(코드)   → 4대가 관점 심층 분석
/closing-bet-scan               → 같은 날 일봉 패턴 후보와 대조
```

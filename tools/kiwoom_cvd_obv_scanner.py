#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
키움증권 REST API 기반 CVD / OBV 스캐너 (V1.5)
코스피200 / 코스닥150 종목의 최근 N영업일 분봉을 수집해서
CVD·OBV를 계산하고 스코어 상위 종목을 출력합니다.

V1.1 변경사항 (변별력 개선 — 리뷰 반영):
  - CVD/OBV를 거래량 규모에 무관한 정규화 값(-1~1)으로 계산
  - 총점을 0~11 정수 버킷 대신, 스캔 유니버스 내 백분위 기반
    0~100점 연속 점수로 산출 (동점 다수 발생 문제 완화)
  - bull_days(절대 일수) → bull_ratio/up_ratio(비율)로 변경
  - 가격추세(MA20 대비 위치·기울기), 유동성(거래대금 백분위) 반영
  - 실제 수집된 영업일 수로 N영업일을 정확히 자름(달력일 근사치 보정)
  - OBV 다이버전스 lookback을 표시 기간(--days)과 일치
  - 토큰 디버그 로그 마스킹, update-index 하위호환 오류 제거

V1.2 변경사항 (재점검 리뷰 반영):
  - [P0] `_percentile_ranks()`가 종목코드 문자열로 정렬되던 치명적 버그 수정
    (튜플 정렬 시 첫 원소인 code 기준으로 정렬돼, 실제 지표값과 무관하게
    순위가 매겨지고 있었다 — 이 버그가 있던 리포트는 순위를 신뢰할 수 없음)
  - [P1] data_status PARTIAL 판정을 "80% 이상이면 SUCCESS"에서
    "요청일수 미만이면 무조건 PARTIAL"로 강화
  - [P1] Absolute Gate 도입: 총점(상대 백분위)과 별개로 CVD Delta·모멘텀·
    bull_ratio·MA20·유동성이 모두 절대 기준을 충족하는지 판정해
    BUY_CANDIDATE / WATCH / DATA_INSUFFICIENT로 분류. 정렬을
    "총점 내림차순"에서 "Gate 상태 → 총점" 순으로 변경
  - [P1] OBV 다이버전스 배점을 ±15 → ±5로 축소(OBV 최대 35점의 43%를
    차지해 과대배점이었음), 축소분은 up_ratio 백분위로 재배분
  - [P1] 분봉 완성도(completeness) 검증 추가: 날짜 수만으로는 못 잡는
    일중 누락을 완료된 거래일 기준 기대 봉 수 대비로 확인, 80% 미만이면
    PARTIAL. 당일(장중 미완료)은 분모에서 제외
  - 수집 버퍼를 +5 → +10 영업일로 확대(긴 연휴 대비, 보장은 아님)
  - 미사용 import 정리

V1.3 변경사항 (재점검 리뷰의 남은 P2 항목 반영):
  - [P2] 유동성 결측·기간 처리: calc_liquidity()가 실제 사용 일수(days_used)와
    PARTIAL 상태를 반환하도록 구조화. 결측 시 총점에 중립(5점) 대신 0점 부여
    (결측 종목이 실측 저유동성 종목보다 유리해지지 않도록)
  - [P2] OBV normalized_slope 추가(OBV 변화량/구간거래량, -1~1 정규화):
    up_ratio(상승일 비율, 방향만 반영) 대신 OBV 배점의 추세 항목에 사용
  - [P2] 가격추세에 MA60/MA120 추가: MA60 상회, MA60 기울기, MA20>MA60
    정배열 여부를 반영(가격추세 15점 배분 재조정), MA120은 표시 전용
  - [P2] ATR14(Wilder) 기반 변동성·과열 필터 추가(calc_risk()): 종가가
    MA20 대비 +15% 이상 이격되면 총점 -5점 페널티(검증 전 초기값)
  - [P2] API 재시도: curl 자체 실패(네트워크/타임아웃)·빈 응답만 최대 3회
    재시도(1·2·4초+jitter). JSON 파싱 실패·API 명시적 오류는 재시도하지 않음
  - [P2] Pivot 기반 OBV 다이버전스: 기존 endpoint 2점 비교 대신 실제
    swing low/high(확정 지연 반영, 미래정보 미참조)로 판정 — 아직 배점에는
    반영하지 않는 참고용 표시(검증 전 자동 증액 금지 — 리뷰 지적)
  - [P2] Price-CVD Pivot 다이버전스: 분봉을 일별로 다운샘플한 종가·일말
    누적 CVD를 같은 Pivot 로직으로 비교 — 역시 참고용 표시 전용
  - 이 모두를 지원하기 위해 일봉 수집 범위를 60일 → 220캘린더일(≈150거래일)로
    확대(MA120·Pivot 탐지에 필요한 워밍업 확보)
  - 모든 "estimated" 값은 체결 데이터가 아닌 분봉 캔들 추정치이며,
    아래 가중치·임계값은 검증된 전략이 아니라 초기 설계 가설(의견)입니다.

V1.4 변경사항 (코드 리뷰 재검토본 반영 — 2026-09-11 리뷰 문서 기준):
  - [P0] 장중 미완성 일봉 제외: as_of(스캔 1회 공통 기준시각) 도입,
    정규장 마감(15:30) 이전의 당일 일봉은 OBV·MA·ATR·유동성 계산에서
    제외(_is_bar_day_complete()). 분봉 기반 CVD는 당일 데이터를 계속 사용.
  - [P0] BUY Gate에 OBV 방향 조건 추가: normalized_slope > 0 이고
    OBV > OBV MA20 이어야 BUY_CANDIDATE (기존엔 CVD·가격 조건만으로
    OBV 하락 종목도 후보가 될 수 있었음).
  - [P1] 백분위 모집단을 해당 지표의 data_status == SUCCESS 종목으로 제한
    (PARTIAL 종목의 유효값이 정상 종목 순위를 왜곡하지 않도록 CVD·OBV·
    유동성에 동일 기준 적용).
  - [P1] CVD Momentum을 봉 개수(최대 5봉) 대신 시간 기준(기본 30분,
    k=ceil(30/period)봉)으로 계산해 분봉 주기와 무관하게 동일한 시간
    의미를 갖도록 변경.
  - [P1] bull_ratio를 완료 거래일 기준(completed_bull_ratio)과 당일
    장중 부분값(intraday_delta)으로 분리. Gate 판정은 완료일 비율을 사용.
  - [P1] 분봉 completeness를 집계 비율뿐 아니라 거래일별로도 검사해
    특정일 집중 누락을 탐지(missing_days), 최신 거래일이 as_of 대비
    지나치게 오래됐으면 최신성 문제로 표시(stale). 기대 봉 수 계산을
    정수 나눗셈(내림) → 올림으로 변경(60분봉 등에서 느슨한 누락 판정 방지).
  - 일봉 조회(fetch_daily_bars)에 연속조회(next-key/cont-yn) 페이지네이션
    추가 — 기존엔 1회 호출로 끝나 220일 요청이 첫 페이지 분량으로 잘릴 수
    있었음(MA120·백테스트 워밍업 부족 우려).
  - [위험 Gate] calc_risk()의 과열 판정(MA20 대비 +15% 초과 이격)을
    ATR14 계산 성공 여부와 분리 — 고가/저가 결측으로 ATR을 못 구해도
    이격 과열 페널티는 그대로 적용되도록 수정. ATR 자체는 여전히
    참고용(필수 Gate 조건 아님)으로 유지하고 그렇게 명시.
  - [반올림] normalized_delta/momentum/slope, bull_ratio 등 순위·Gate
    판정에 쓰이는 내부 값의 조기 반올림 제거(표시 시점에만 반올림) —
    작은 양수가 반올림으로 0이 되어 신호·Gate 판정이 표시값과 달라지는
    문제 방지.
  - [P2] OBV endpoint 다이버전스(구간 양끝 2점 비교)를 배점(±5)에서
    제외하고 표시 전용으로 전환(Pivot 다이버전스와 동일하게 "참고, 미배점"
    처리). 축소분은 OBV normalized_slope 백분위 배점에 재배분(15→20,
    OBV 합계 35점 유지).
  - [P2] 단일 종목 상세(detail)는 비교 모집단이 없어 상대 백분위 총점이
    무의미하므로 총점/등급 출력을 제거하고 Absolute Gate 판정과 미충족
    사유만 표시하도록 변경.
  - [P2] `scan --index both`에 `--rank-mode {separate,combined}` 추가.
    기본(separate)은 기존처럼 지수별 개별 백분위 순위, combined는
    코스피200+코스닥150을 하나의 모집단으로 통합해 순위 계산.
  - [데이터 검증] 일봉 수집 시 거래량이 0 이하인 행 추가 필터링,
    ATR 계산에서 저가>고가 등 비정상 OHLC 행 제외.
  - [기록] `--save-json` 옵션 추가 — 스캔/상세 결과(원시 지표·점수·Gate
    사유·as_of·설정)를 reports/daily-scan/*.json으로 저장해 이후 검증에
    재사용 가능하게 함(원시 분봉/일봉 원본까지 저장하지는 않음).
  - 최소 강도 임계값(P1-4, 예: normalized_delta ≥ 0.03) 등은 리뷰에서도
    "검증 전 후보값"으로 명시한 항목이라 이번 버전에서 확정 기준으로
    코드에 넣지 않았다 — 백테스트 없이 임의 숫자를 절대 기준화하면
    이 프로젝트의 "추측 금지" 원칙에 어긋난다(CLAUDE.md 핵심 원칙 참고).
    RVOL·Volume Z-score 등 신규 지표 추가와 API 호출 중앙화(RateLimiter)도
    이번 버전 범위 밖(리뷰의 7·8단계, 우선순위상 후순위).

V1.5 변경사항 (2026-09-14 KRX 애프터마켓 신설 대응 — 재검토 리뷰 반영):
  - [P0-1] 단일 마감시각(15:30) 가정을 폐기하고 "세션 모델"을 도입.
    정규장(09:00~15:30)·애프터마켓(16:00~20:00)·기타(시간외종가 등)를
    봉 타임스탬프로 분류하고, 세션별 종료시각 + 수신지연 여유(5분)로
    완료 여부를 판정한다(is_session_complete). 일봉 완료 판정은
    --daily-scope로 분리(auto=보수적: 애프터마켓 시행일 이후엔 20:05 이후
    확정) — API 일봉이 정규장 전용인지 확장 세션 포함인지 미확인이기 때문.
  - [P0-2] 수집 데이터에 세션 메타데이터(bar_start/bar_end/session) 부여.
    확인되지 않은 요청 파라미터·종목코드 접미사는 추가하지 않고, 응답 봉의
    시각으로 세션을 판별한다 — API가 애프터마켓을 주든 안 주든 계산 의미가
    달라지지 않도록 하기 위함.
  - [P1-1] 390분 고정 상수를 세션별 기대 봉 수로 교체. 기대치를 초과하는
    날(세션 혼입 의심)은 completeness를 100%로 클램프하고 over_count_days로
    표시해 PARTIAL 처리한다(기존엔 126봉/78봉=161%가 SUCCESS로 통과).
  - [P1-2] 정규장 CVD와 애프터마켓 CVD를 분리 계산해 함께 출력.
    --session {regular,after}로 점수·순위·Gate 기준 세션을 선택한다.
    애프터마켓 순위는 애프터마켓 데이터가 있는 종목끼리만 비교한다.
  - [P1-3] as_of 이후 봉 절단: bar_end <= as_of인 완료 봉만 사용한다.
    --as-of로 기준시각을 고정할 수 있어 회귀 테스트가 가능하다.
  - [P1-4] 모멘텀을 "봉 수 환산"이 아니라 실제 시간 구간으로 계산.
    구간이 부족하면 WARMING_UP, 분봉 주기가 요청 구간보다 길면
    RESOLUTION_LIMITED로 표시하고 실제 사용 분수를 함께 출력한다.
  - [P1-5] 날짜 전체 누락 탐지: 일봉에서 얻은 거래일 목록과 대조해
    분봉이 아예 없는 거래일(absent_days)을 잡는다. 최신성은 캘린더일
    기준뿐 아니라 "세션이 열려 있는데 마지막 봉이 오래됨"도 검사한다.
  - [P1-6] 토큰 만료를 공식 필드(expires_dt, YYYYMMDDHHMMSS)로 파싱.
    임의 24시간 가정 제거(불명 시 1시간만 캐시), 401/403·토큰 만료 응답에
    한해 1회 재발급 후 원 요청 재시도. API 호출 간격은 중앙 RateLimiter로
    일원화.
  - [P2-1] 점수와 Gate의 bull_ratio 정의를 완료 거래일 기준으로 통일.
    완료 거래일이 2일 미만이면 사용하지 않는다(당일 장중 값으로 폴백 금지).
  - [P2-2] Price-CVD Pivot 다이버전스를 완료 거래일 자료로만 계산.
  - [P2-3] 스냅샷 파일명에 초·실행ID를 포함하고 설정 해시·코드 버전·
    세션 제도 버전·실제 마지막 입력 시각을 함께 저장한다.
  - 미확인(코드에 가정으로 넣지 않은 것): 키움 ka10080/ka10081이
    애프터마켓 체결을 포함하는지, 분봉 cntr_tm이 봉 시작/종료 중 무엇인지,
    종목별 애프터마켓 대상 여부. 애프터마켓 관련 수치 임계값도 검증 전이라
    부호(>0) 조건과 데이터 최신성만 사용한다.

사전 준비:
  1. 키움 REST API 신청: https://openapi.kiwoom.com
  2. App Key / App Secret 발급
  3. 프로젝트 루트 .env 파일에 키 등록 (.env.example 참고):
     KIWOOM_APP_KEY=키
     KIWOOM_APP_SECRET=시크릿

사용법:
  # 종목 목록 먼저 생성 (최초 1회 및 분기별 변경 시)
  python3 tools/update_index.py

  # 스캔 실행
  python3 tools/kiwoom_cvd_obv_scanner.py scan --index kospi200 --top 10
  python3 tools/kiwoom_cvd_obv_scanner.py scan --index kosdaq150 --top 10
  python3 tools/kiwoom_cvd_obv_scanner.py scan --index both --top 10

  # CVD 산출 기간 변경 (기본 5일, 10일·20일도 가능)
  python3 tools/kiwoom_cvd_obv_scanner.py scan --index kospi200 --days 10
  python3 tools/kiwoom_cvd_obv_scanner.py scan --index kospi200 --days 20
  python3 tools/kiwoom_cvd_obv_scanner.py detail --code 005930 --days 10

  # 단일 종목 상세 분석
  python3 tools/kiwoom_cvd_obv_scanner.py detail --code 009150

  # 애프터마켓 기준 스캔 / 기준시각 고정(회귀 검증)
  python3 tools/kiwoom_cvd_obv_scanner.py scan --index kospi200 --session after
  python3 tools/kiwoom_cvd_obv_scanner.py detail --code 005930 --as-of "2026-09-14 16:05"
"""

import argparse
import hashlib
import json
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional

# Windows 한글 깨짐 방지
# sys.stdout을 새 TextIOWrapper로 "교체"하면 교체된 래퍼가 GC될 때 하위
# 버퍼까지 닫아, 같은 프로세스의 다른 stdout 사용처(pytest 출력 캡처 등)가
# "I/O operation on closed file"로 깨진다. reconfigure는 같은 스트림의
# 인코딩만 바꾸므로 그런 부작용이 없다.
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

# ── .env 로더: 프로젝트 루트의 .env를 os.environ에 주입 ──
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))
except ImportError:
    pass  # python-dotenv 미설치 시 OS 환경변수만 사용

# ─────────────────────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────────────────────

_BASE          = os.path.join(os.path.dirname(__file__), "..")
_DATA_DIR      = os.path.join(_BASE, "data")
_KIWOOM_TOKEN  = os.path.join(_BASE, ".kiwoom_token.json")
_TIMEOUT       = 30
_RATE_DELAY    = 0.22   # 초당 5회 제한 준수 (0.2초 + 여유)

_KIWOOM_BASE      = "https://api.kiwoom.com"       # 실전투자
_KIWOOM_BASE_MOCK = "https://mockapi.kiwoom.com"   # 모의투자

# update_index.py가 생성하는 단일 파일에서 종목 목록 로드
_INDEX_FILE = os.path.join(_DATA_DIR, "index_stocks.json")
_SNAPSHOT_DIR = os.path.join(_BASE, "reports", "daily-scan")

os.makedirs(_DATA_DIR, exist_ok=True)

# CVD Momentum 시간 기준(분) — 봉 개수가 아닌 시간으로 고정해 분봉 주기가
# 달라도 같은 의미를 갖도록 한다(리뷰 P1-2 반영, 검증 전 초기값).
_MOMENTUM_MINUTES = 30

# 코드 버전 · 세션 제도 버전 — 스냅샷에 함께 기록해 점수 변화가 데이터
# 때문인지 로직/제도 변경 때문인지 구분할 수 있게 한다(리뷰 P2-3).
_SCANNER_VERSION = "1.5"
_MARKET_RULE_VERSION = "KRX-2026-09-14-aftermarket"

# ─────────────────────────────────────────────────────────────
# 거래시간(세션) 모델 — 2026-09-14 KRX 애프터마켓 신설 대응 (V1.5)
# ─────────────────────────────────────────────────────────────
#
# 제도(사실 — 2026-09 시행 공지·언론 보도 기준):
#   - 2026-09-14부터 정규장(09:00~15:30) 종료 후 16:00~20:00 애프터마켓
#     (시간외 접속매매) 운영. 기존 16:00~18:00 시간외단일가는 폐지.
#   - 가격제한폭 ±30%(전일 정규장 종가 기준), 동적·정적 VI 적용.
#   - 대상: 코스피·코스닥 주식·DR / 제외: ETF·ETN, 관리종목, 투자경고·투자위험,
#     이상급등, 정리매매, 초저유동성, 당일 정규장 미거래 종목.
#   - 애프터마켓 체결가·거래량은 당일 공식 종가(15:30 확정)를 바꾸지 않고,
#     다음 거래일 기준가격도 정규장 종가다.
#
# 미확인(가정하지 않는다 — 실측 후 확정할 것):
#   - 키움 ka10080(분봉)·ka10081(일봉)이 애프터마켓 체결을 포함하는지
#   - 일봉 OHLC·거래량이 어느 세션까지 반영하는지
#   - 종목별 애프터마켓 대상 여부(조회 수단 없음 → "무체결"과 구분 불가)
#
# 그래서 이 도구는 "요청 파라미터로 세션을 고르는" 방식이 아니라 "응답 봉의
# 타임스탬프로 세션을 분류하는" 방식을 쓴다. API가 애프터마켓을 포함하든
# 아니든 각 지표가 어느 세션의 값인지 항상 명확해진다.

_SESSION_REGULAR = "REGULAR"
_SESSION_AFTER   = "AFTER"
_SESSION_OTHER   = "OTHER"

_SESSION_LABEL = {
    _SESSION_REGULAR: "정규장",
    _SESSION_AFTER:   "애프터마켓",
    _SESSION_OTHER:   "기타(시간외종가·장외 등)",
}

# 애프터마켓 시행일 — 이 날짜 이전의 16:00~18:00 체결은 폐지된 시간외단일가라
# 성격이 달라 애프터마켓 세션으로 묶지 않는다(리뷰 P1-1: 시행일 이전/이후에
# 같은 기대 봉 수를 적용하지 말 것).
_AFTER_MARKET_START_DATE = "20260914"

# 세션 정의: (시작 HHMM, 종료 HHMM, 세션 총 분, 접속매매 분)
# 정규장 390분 중 접속매매는 09:00~15:20(380분)이고 15:20~15:30은 종가 단일가
# 구간이라 접속 체결이 없다 — 실측상 이 구간 분봉은 생성되지 않고, 15:30에
# 단일가 체결 봉이 하나 생긴다(아래 expected_bars_per_day 참고).
_SESSION_WINDOWS = {
    _SESSION_REGULAR: (900,  1530, 390, 380),
    _SESSION_AFTER:   (1600, 2000, 240, 240),
}

# 세션 종료 직후 최종 봉 수신 지연 여유(분) — 경계 시각에 곧바로 "완료"로
# 보면 마지막 봉이 빠진 채 확정될 수 있다(리뷰 P0-1).
_SESSION_COMPLETE_GRACE_MIN = 5

# 분봉 타임스탬프(cntr_tm) 해석 — 2026-09-16 실측으로 "봉의 시작 시각" 확인.
#   · 09:01 조회 시 당일 첫 봉이 090000으로 존재(진행 중인 봉)
#   · 1분봉 애프터마켓 마지막 봉이 195900(19:59~20:00), 20:00 봉은 없음
# 따라서 bar_end = cntr_tm + period로 계산한다.
_BAR_TIMESTAMP_IS_START = True

# 일봉 완료 판정 범위(--daily-scope):
#   auto(기본)/extended — 애프터마켓 종료(20:00)+여유에 확정
#   regular             — 정규장 마감(15:30)+여유에 확정
#
# 2026-09-16 ka10081 실측 결과, 키움 일봉은 **애프터마켓을 반영한다**:
#   · 종가(cur_prc) = 애프터마켓 최종 체결가
#     (2026-09-15 삼성전자 일봉 종가 250,500 = 애프터 종가, 정규장 종가는 248,500)
#   · 저가/고가 = 애프터마켓 포함 전 세션 기준
#     (2026-09-14 삼성전자 일봉 저가 247,000 = 애프터 저가, 정규장 저가는 248,500)
#   · 거래량 = 정규장 + 애프터마켓 (일봉/정규장 비율 1.03~1.07)
# 즉 일봉은 20:00 이전에는 확정되지 않으므로 auto(=extended)가 정답이고,
# regular는 "정규장만 반영한다"는 잘못된 전제가 되므로 권장하지 않는다.
#
# ⚠ 해석 주의: 이 종가는 제도상 공식 종가(15:30 확정, 지수·기준가격 산출 기준)가
# 아니라 "마지막 체결가"다. 2026-09-14 이전 일봉의 종가는 정규장 종가였으므로,
# OBV·MA·ATR 시계열은 그 날짜를 경계로 정의가 한 번 바뀐 셈이다(한계로 명시).
_DAILY_SCOPE_CHOICES = ("auto", "regular", "extended")

# 완성도 관련 임계값(의견, 검증 전)
_MIN_DAY_COMPLETENESS   = 0.50  # 거래일·세션별 최소 분봉 완성도
_MAX_STALE_CALENDAR_DAYS = 5    # 최신 거래일이 이보다 오래되면 최신성 문제로 표시(의견)
# 완료 거래일이 이보다 적으면 완료일 기준 bull_ratio를 점수·Gate에 쓰지 않는다
# — 당일 장중 값으로 폴백하지 않는다(리뷰 P2-1).
_MIN_COMPLETED_DAYS_FOR_RATIO = 2


def _bar_dt(dt_str: str) -> Optional[datetime]:
    """YYYYMMDDHHMM(SS) 문자열을 datetime으로. 형식이 다르면 None."""
    s = str(dt_str or "").strip()
    for fmt, ln in (("%Y%m%d%H%M%S", 14), ("%Y%m%d%H%M", 12), ("%Y%m%d", 8)):
        if len(s) >= ln:
            try:
                return datetime.strptime(s[:ln], fmt)
            except ValueError:
                continue
    return None


def _hhmm(dt_str: str) -> Optional[int]:
    """타임스탬프에서 HHMM 정수를 뽑는다(시간 정보가 없으면 None)."""
    s = str(dt_str or "").strip()
    if len(s) < 12 or not s[:12].isdigit():
        return None
    return int(s[8:12])


def after_market_active(date_str: str) -> bool:
    """해당 거래일에 KRX 애프터마켓 제도가 시행 중인지(시행일 이후인지)."""
    return bool(date_str) and str(date_str)[:8] >= _AFTER_MARKET_START_DATE


def session_of(dt_str: str) -> str:
    """
    봉 타임스탬프가 속한 세션을 판정한다.
    15:30~16:00 구간(시간외종가 등)은 연속 접속매매 구간이 아니므로
    정규장으로 채우지 않고 OTHER로 분류한다(리뷰 P1-1).
    시행일 이전의 16:00~18:00(폐지된 시간외단일가)도 OTHER다.
    """
    hm = _hhmm(dt_str)
    if hm is None:
        return _SESSION_OTHER
    r0, r1 = _SESSION_WINDOWS[_SESSION_REGULAR][:2]
    if r0 <= hm <= r1:
        return _SESSION_REGULAR
    if after_market_active(str(dt_str)[:8]):
        a0, a1 = _SESSION_WINDOWS[_SESSION_AFTER][:2]
        if a0 <= hm <= a1:
            return _SESSION_AFTER
    return _SESSION_OTHER


def bar_bounds(dt_str: str, period: int):
    """봉의 (시작, 종료) datetime. 타임스탬프 해석은 _BAR_TIMESTAMP_IS_START."""
    t = _bar_dt(dt_str)
    if t is None:
        return None, None
    if _BAR_TIMESTAMP_IS_START:
        return t, t + timedelta(minutes=period)
    return t - timedelta(minutes=period), t


def expected_bars_per_day(session: str, period: int) -> int:
    """
    세션 1회당 기대 분봉 수 — 2026-09-16 ka10080 실측 응답으로 검증한 값.

      정규장 = ceil(380/period) + (390 % period == 0 이면 종가 단일가 봉 1개)
        · 09:00~15:20 접속매매 구간의 봉
        · 15:20~15:30은 단일가 구간이라 접속 체결이 없어 봉이 생성되지 않음
        · 15:30 단일가 체결은 별도 봉으로 잡히지만, 15:30이 봉 경계가 아닌
          주기(예: 60분봉)에서는 직전 봉(15:00)에 흡수된다
      애프터마켓 = ceil(240/period)  (16:00~20:00, 별도 마감 단일가 봉 없음)

    실측 대조(삼성전자·SK하이닉스, 2026-09-14/15):
      1분봉 381/240 · 5분봉 77/48 · 30분봉 14/8 · 60분봉 7/4
    """
    window = _SESSION_WINDOWS.get(session)
    if not window or period <= 0:
        return 0
    total_min, continuous_min = window[2], window[3]
    n = math.ceil(continuous_min / period)
    if session == _SESSION_REGULAR and total_min % period == 0:
        n += 1      # 15:30 종가 단일가 봉
    return max(1, n)


def max_bars_per_day(session: str, period: int) -> int:
    """
    세션당 "허용 가능한" 최대 분봉 수. 이 값을 넘으면 다른 세션 혼입 등을
    의심한다(completeness 분모는 expected_bars_per_day를 그대로 쓴다).

    정규장은 접속매매 종료 시각(15:20) 정각에 체결이 있으면 그 시각 봉이
    하나 더 생긴다 — 실측: LG디스플레이 2026-09-15 정규장 78봉
    (09:00~15:20 + 15:30 단일가). 삼성전자·SK하이닉스는 같은 날 77봉이었다.
    즉 정규장 봉 수는 종목·날짜에 따라 expected 또는 expected+1이 정상이다.
    15:20이 봉 경계가 아닌 주기(30·60분 등)에서는 직전 봉에 흡수되므로
    상한이 늘지 않는다.
    """
    n = expected_bars_per_day(session, period)
    if session == _SESSION_REGULAR and period > 0 and 380 % period == 0:
        n += 1
    return n


def session_end_dt(date_str: str, session: str) -> Optional[datetime]:
    """해당 거래일 해당 세션의 종료 datetime. 그 날짜에 없는 세션이면 None."""
    window = _SESSION_WINDOWS.get(session)
    if not window or not date_str:
        return None
    if session == _SESSION_AFTER and not after_market_active(date_str):
        return None
    hm = window[1]
    try:
        base = datetime.strptime(str(date_str)[:8], "%Y%m%d")
    except ValueError:
        return None
    return base + timedelta(hours=hm // 100, minutes=hm % 100)


def session_start_dt(date_str: str, session: str) -> Optional[datetime]:
    """해당 거래일 해당 세션의 시작 datetime. 그 날짜에 없는 세션이면 None."""
    window = _SESSION_WINDOWS.get(session)
    if not window or not date_str:
        return None
    if session == _SESSION_AFTER and not after_market_active(date_str):
        return None
    hm = window[0]
    try:
        base = datetime.strptime(str(date_str)[:8], "%Y%m%d")
    except ValueError:
        return None
    return base + timedelta(hours=hm // 100, minutes=hm % 100)


def is_session_complete(date_str: str, session: str, as_of: datetime) -> bool:
    """
    date_str 거래일의 session이 as_of 시점에 "완료"인지 판정한다(리뷰 P0-1).
    세션 종료 + 수신지연 여유(_SESSION_COMPLETE_GRACE_MIN) 이후여야 완료다.
    그 날짜에 존재하지 않는 세션(시행일 이전 애프터마켓 등)은 False.
    거래소 휴장일력은 조회하지 않으므로, 데이터에 존재하는 날짜를 거래일로
    간주하는 근사치다(의견).
    """
    end = session_end_dt(date_str, session)
    if end is None:
        return False
    return as_of >= end + timedelta(minutes=_SESSION_COMPLETE_GRACE_MIN)


def session_is_open(as_of: datetime, session: str) -> bool:
    """as_of가 해당 세션 운영시간 안(경계 포함)인지 — 최신성 검사용."""
    window = _SESSION_WINDOWS.get(session)
    if not window:
        return False
    if session == _SESSION_AFTER and not after_market_active(as_of.strftime("%Y%m%d")):
        return False
    hm = as_of.hour * 100 + as_of.minute
    return window[0] <= hm <= window[1]


def _is_bar_day_complete(date_str: str, as_of: datetime,
                          daily_scope: str = "auto") -> bool:
    """
    date_str(YYYYMMDD) "일봉"을 완료로 볼 수 있는지 판정한다.

    실측(2026-09-16) 결과 키움 일봉은 애프터마켓 체결까지 반영하므로
    20:00 이전에는 확정되지 않는다.
    - auto(기본)/extended : 애프터마켓 종료(20:00)+여유 이후 확정
    - regular             : 정규장 종료(15:30)+여유 이후 확정(실측과 불일치,
      과거 정의를 재현해야 할 때만 사용)
    시행일 이전 날짜는 애프터마켓 자체가 없으므로 항상 정규장 기준이다.
    """
    if daily_scope == "regular" or not after_market_active(date_str):
        return is_session_complete(date_str, _SESSION_REGULAR, as_of)
    return is_session_complete(date_str, _SESSION_AFTER, as_of)


def load_index(name: str) -> list:
    """
    update_index.py가 생성한 index_stocks.json에서 종목 목록 로드.
    반환: [{"code": "005930", "name": "삼성전자"}, ...]
    """
    if not os.path.exists(_INDEX_FILE):
        print(f"  ❌ 종목 목록 파일 없음: {_INDEX_FILE}")
        print(f"  먼저 실행: python3 tools/update_index.py")
        return []

    with open(_INDEX_FILE, encoding="utf-8") as f:
        data = json.load(f)

    idx_data = data.get(name, {})
    stocks   = idx_data.get("stocks", [])
    updated  = idx_data.get("updated", "?")

    if not stocks:
        print(f"  ❌ {name} 종목 데이터 없음")
        print(f"  실행: python3 tools/update_index.py --index {name}")
        return []

    print(f"  📋 {name}: {len(stocks)}개 종목 로드 (갱신: {updated})")
    return stocks


# ─────────────────────────────────────────────────────────────
# 키움 REST API 인증
# ─────────────────────────────────────────────────────────────

def _mask_token_log(raw: str) -> str:
    """디버그 로그에서 토큰류 값(token/access_token 등)을 마스킹한다."""
    def _mask(m):
        val = m.group(2)
        if len(val) <= 8:
            masked = "***"
        else:
            masked = f"{val[:4]}…(마스킹)…{val[-4:]}"
        return m.group(1) + masked + m.group(3)

    return re.sub(
        r'("(?:token|access_token|tokenValue|accessToken)"\s*:\s*")([^"]+)(")',
        _mask, raw
    )


_TOKEN_FALLBACK_TTL = 3600   # 만료 정보가 없을 때의 보수적 캐시 수명(초)

# 발급된 토큰의 단일 소스. 인증 만료로 재발급하면 이후 호출도 새 토큰을
# 쓰도록 여기에 보관한다(호출부가 들고 있는 예전 토큰보다 우선).
_TOKEN_STATE = {"token": None, "expires_at": 0.0, "source": ""}


def _parse_token_expiry(data: dict):
    """
    토큰 만료 시각(epoch)과 그 근거 필드명을 반환한다.

    키움 au10001 공식 응답은 만료 "시각"을 expires_dt(YYYYMMDDHHMMSS)로 준다.
    기존 구현은 token_expired/expires_in만 보고, 없으면 임의로 86400초를
    가정해 캐시가 실제 만료와 어긋날 수 있었다(리뷰 P1-6). 만료 정보를
    전혀 못 찾으면 24시간을 가정하지 않고 1시간만 캐시한다.
    """
    for key in ("expires_dt", "token_expired"):
        raw = str(data.get(key, "") or "").strip()
        if len(raw) == 14 and raw.isdigit():
            try:
                return datetime.strptime(raw, "%Y%m%d%H%M%S").timestamp(), key
            except ValueError:
                pass
    raw_in = data.get("expires_in")
    if raw_in is not None:
        try:
            return time.time() + int(str(raw_in).strip()), "expires_in"
        except (TypeError, ValueError):
            pass
    return time.time() + _TOKEN_FALLBACK_TTL, "fallback"


def _get_token(force: bool = False) -> Optional[str]:
    """
    키움 REST API 토큰 발급. 만료 시각은 응답의 expires_dt를 그대로 사용한다.

    force=True면 메모리·파일 캐시를 무시하고 재발급한다 — 인증 만료로 보이는
    응답(HTTP 401/403 등)을 받았을 때만 쓴다(리뷰 P1-6). 애프터마켓 시간까지
    실행이 길어지면 실행 도중 토큰 만료가 현실적인 실패 요인이 된다.
    """
    now = time.time()
    if not force and _TOKEN_STATE["token"] and _TOKEN_STATE["expires_at"] > now + 60:
        return _TOKEN_STATE["token"]

    app_key    = os.environ.get("KIWOOM_APP_KEY")
    app_secret = os.environ.get("KIWOOM_APP_SECRET")
    if not app_key or not app_secret:
        return None

    # 파일 캐시 확인
    if not force and os.path.exists(_KIWOOM_TOKEN):
        try:
            with open(_KIWOOM_TOKEN) as f:
                cache = json.load(f)
            if cache.get("expires_at", 0) > now + 60 and cache.get("token"):
                _TOKEN_STATE.update(token=cache["token"],
                                    expires_at=cache["expires_at"],
                                    source=cache.get("expires_source", "cache"))
                return cache["token"]
        except Exception:
            pass

    # 신규 발급 — 두 가지 필드명 시도 (키움 API 버전별 차이)
    for secret_field in ["secretkey", "appsecret", "appSecret"]:
        body = json.dumps({
            "grant_type": "client_credentials",
            "appkey":     app_key,
            secret_field: app_secret,
        })
        result = subprocess.run(
            ["curl", "-s", "-X", "POST",
             f"{_KIWOOM_BASE}/oauth2/token",
             "-H", "Content-Type: application/json;charset=UTF-8",
             "-H", "Accept: application/json",
             "-d", body],
            capture_output=True, timeout=_TIMEOUT
        )
        raw_bytes = result.stdout.strip()
        raw_text  = raw_bytes.decode("utf-8", errors="ignore") if isinstance(raw_bytes, bytes) else raw_bytes
        print(f"  [디버그] 토큰 응답 ({secret_field}): {_mask_token_log(raw_text)[:200]}")

        if not raw_bytes:
            continue
        # HTML 응답 감지
        if raw_bytes.startswith(b"<") if isinstance(raw_bytes, bytes) else raw_bytes.startswith("<"):
            print(f"  ⚠️  HTML 응답 — URL 또는 인증 방식 확인 필요")
            continue
        try:
            data  = json.loads(raw_bytes)
            token = (data.get("token") or data.get("access_token") or
                     data.get("tokenValue") or data.get("accessToken"))
            if token:
                expires_at, source = _parse_token_expiry(data)
                if source == "fallback":
                    print(f"  ⚠️  만료 필드(expires_dt) 없음 — {_TOKEN_FALLBACK_TTL // 60}분만 캐시합니다 "
                          f"(응답 키: {list(data.keys())})")
                _TOKEN_STATE.update(token=token, expires_at=expires_at, source=source)
                try:
                    with open(_KIWOOM_TOKEN, "w") as f:
                        json.dump({"token": token, "expires_at": expires_at,
                                   "expires_source": source}, f)
                except OSError as e:
                    print(f"  ⚠️  토큰 캐시 저장 실패(메모리 캐시만 사용): {e}")
                print(f"  ✅ 키움 토큰 발급 완료 (필드: {secret_field}, "
                      f"만료 {datetime.fromtimestamp(expires_at):%Y-%m-%d %H:%M:%S}, 근거: {source})")
                return token
            else:
                print(f"  ❌ 토큰 필드 없음 (응답 키: {list(data.keys())})")
        except Exception as e:
            print(f"  ❌ JSON 파싱 오류: {e}")

    return None


_MAX_RETRIES    = 3            # 최초 1회 + 최대 3회 재시도 = 총 4회 시도
_RETRY_BACKOFF  = [1, 2, 4]     # 초 (+ 소폭 jitter)

_AUTH_FAIL_STATUS = (401, 403)
_AUTH_FAIL_HINTS  = ("token", "토큰", "만료", "expired", "unauthorized")


class _RateLimiter:
    """
    API 호출 간 최소 간격을 보장하는 중앙 리미터(리뷰 P1-6).
    기존에는 수집 루프마다 time.sleep()이 흩어져 있어 실제 호출 간격이
    코드 경로(페이지 수·종목 수)에 따라 달라졌다. 모든 REST 호출이
    _kiwoom()을 지나므로 여기서 한 번만 통제한다.
    """

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last = 0.0
        self.calls = 0

    def wait(self) -> None:
        gap = self.min_interval - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()
        self.calls += 1


_LIMITER = _RateLimiter(_RATE_DELAY)


def _parse_response_headers(path: str) -> dict:
    """curl -D로 저장한 헤더 파일에서 HTTP 상태코드·next-key·cont-yn 추출."""
    info = {"status": 0, "next_key": "", "cont_yn": ""}
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line.upper().startswith("HTTP/"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        info["status"] = int(parts[1])   # 리다이렉트 시 마지막 값
                    continue
                if ":" not in line:
                    continue
                k, _, v = line.partition(":")
                k, v = k.strip().lower(), v.strip()
                if k == "next-key":
                    info["next_key"] = v
                elif k == "cont-yn":
                    info["cont_yn"] = v
    except OSError:
        pass
    return info


def _looks_like_auth_error(data: dict) -> bool:
    """
    본문 메시지가 인증/토큰 만료를 가리키는지 판정한다. 키움 반환코드 체계가
    전부 문서화돼 있지 않아 메시지 힌트도 함께 본다 — 과도한 재발급을 막기
    위해 재발급 재시도는 요청당 1회로 제한한다.
    """
    msg = f"{data.get('return_msg', '')} {data.get('msg1', '')}".lower()
    return any(hint in msg for hint in _AUTH_FAIL_HINTS)


def _kiwoom(path: str, body: dict, api_id: str, token: str,
            extra_headers: list = None, _retry: int = 0,
            _auth_retry: int = 0) -> Optional[dict]:
    """
    키움 REST API POST 호출.
    키움 REST API는 모든 조회를 POST + Body + api-id 헤더로 처리합니다.

    페이지네이션 계속 여부(cont-yn)와 다음 페이지 커서(next-key)는
    응답 "본문"이 아니라 응답 "HTTP 헤더"로 반환된다(실측 확인:
    ka10080 응답 헤더에 `next-key: ...`, `cont-yn: Y`, `resp-cnt: 900` 존재,
    본문 JSON에는 해당 필드가 아예 없음). 따라서 curl로 헤더를 별도
    캡처해서 반환 dict에 `_next_key`/`_cont_yn`으로 실어 보낸다.
    다음 요청 시에는 이 값을 다시 `next-key`/`cont-yn` 요청 헤더로
    그대로 돌려줘야 다음 페이지가 내려온다.

    재시도(리뷰 반영): curl 프로세스 자체 실패(네트워크/타임아웃)나 빈 응답처럼
    "일시적일 가능성이 있는" 실패만 최대 3회 재시도(1·2·4초 + jitter)한다.
    JSON 파싱 실패나 API가 명시적으로 반환한 오류 코드(return_code≠0, 잘못된
    요청·권한 문제 등)는 재시도해도 같은 결과일 가능성이 높아 재시도하지 않는다.
    같은 next-key/cont-yn을 그대로 재사용하므로 페이지 진행 상태는 보존된다.

    인증 만료(HTTP 401/403 또는 토큰 만료 메시지)만 예외로, 토큰을 1회
    재발급하고 같은 요청을 다시 보낸다(리뷰 P1-6).
    호출 간격은 중앙 _LIMITER가 통제한다.
    """
    # 재발급이 있었다면 호출부가 들고 있는 토큰보다 최신 토큰이 우선이다.
    token   = _TOKEN_STATE.get("token") or token
    app_key = os.environ.get("KIWOOM_APP_KEY", "")
    url     = f"{_KIWOOM_BASE}{path}"
    body_str = json.dumps(body)
    cmd = ["curl", "-s", "--noproxy", "*",
           "-X", "POST", url,
           "-H", f"authorization: Bearer {token}",
           "-H", f"api-id: {api_id}",
           "-H", f"appkey: {app_key}",
           "-H", "Content-Type: application/json;charset=UTF-8"]
    if extra_headers:
        for h in extra_headers:
            cmd += ["-H", h]
    cmd += ["-d", body_str]

    def _retry_or_none():
        if _retry < _MAX_RETRIES:
            time.sleep(_RETRY_BACKOFF[_retry] + random.uniform(0, 0.3))
            return _kiwoom(path, body, api_id, token, extra_headers,
                           _retry=_retry + 1, _auth_retry=_auth_retry)
        return None

    def _auth_retry_or_none():
        if _auth_retry >= 1:
            return None
        new_token = _get_token(force=True)
        if not new_token:
            return None
        return _kiwoom(path, body, api_id, new_token, extra_headers,
                       _retry=_retry, _auth_retry=_auth_retry + 1)

    header_path = None
    try:
        fd, header_path = tempfile.mkstemp(suffix=".hdr")
        os.close(fd)
        cmd += ["-D", header_path]

        _LIMITER.wait()
        result = subprocess.run(cmd, capture_output=True, timeout=_TIMEOUT)

        if result.returncode != 0:
            # curl 자체 실패(연결 오류·타임아웃 등) — 일시적 오류로 간주해 재시도
            return _retry_or_none()

        hdr = _parse_response_headers(header_path)
        if hdr["status"] in _AUTH_FAIL_STATUS:
            return _auth_retry_or_none()

        raw = result.stdout.strip()
        if not raw:
            return _retry_or_none()
        try:
            data = json.loads(raw)
        except Exception:
            return None   # 응답 형식 문제 — 재시도해도 동일할 가능성이 높음

        rc = data.get("return_code", 0)
        if rc not in (0, "0", None, "00000"):
            if _looks_like_auth_error(data):
                return _auth_retry_or_none()
            return None   # API가 명시적으로 반환한 오류 — 재시도하지 않음

        data["_next_key"]    = hdr["next_key"]
        data["_cont_yn"]     = hdr["cont_yn"]
        data["_http_status"] = hdr["status"]
        return data
    except subprocess.TimeoutExpired:
        return _retry_or_none()
    finally:
        if header_path and os.path.exists(header_path):
            try:
                os.remove(header_path)
            except OSError:
                pass


# ─────────────────────────────────────────────────────────────
# 데이터 수집
# ─────────────────────────────────────────────────────────────

def fetch_stock_name(code: str, token: str) -> str:
    """
    키움 ka10001 (주식기본정보) 으로 종목명 조회.
    실패 시 빈 문자열 반환.
    """
    data = _kiwoom("/api/dostk/stkinfo",
                   {"stk_cd": code}, "ka10001", token)
    if not data:
        return ""
    # 응답 구조: stk_nm이 최상위 필드로 바로 존재
    # {"stk_cd":"005930", "stk_nm":"삼성전자", "cur_prc":"+257000", ...}
    name = str(data.get("stk_nm", "")).strip()
    if name:
        return name
    # 혹시 output 안에 있을 경우 대비
    info = data.get("output") or {}
    if isinstance(info, list) and info:
        info = info[0]
    return str(info.get("stk_nm", "")).strip()


_MIN_BARS_PER_PAGE_EST = 900   # ka10080 1회 응답 실측 기준 대략적 상한


def _expected_bars_per_calendar_day(period: int, include_after: bool) -> int:
    """
    페이지 수 추정용 하루치 기대 봉 수. 390분 고정 상수 대신 세션 모델을
    쓴다(리뷰 P1-1 — 수집 페이지 수 계산도 같은 세션 모델을 사용해야 한다).
    """
    n = expected_bars_per_day(_SESSION_REGULAR, period)
    if include_after:
        n += expected_bars_per_day(_SESSION_AFTER, period)
    return max(1, n)


def _dedupe_sort_bars(bars: list) -> list:
    """dt 기준 중복 제거 후 오름차순 정렬 (페이지 재시도 시 중복 방지)."""
    seen  = set()
    dedup = []
    for b in bars:
        if b["dt"] in seen:
            continue
        seen.add(b["dt"])
        dedup.append(b)
    return sorted(dedup, key=lambda b: b["dt"])


def _trim_to_last_n_trading_days(bars: list, n: int) -> list:
    """
    달력일 기준으로 여유 있게 수집된 분봉을, 실제 거래가 발생한
    거래일 기준 최근 n영업일로 정확히 자른다.
    (기존 구현은 달력일 근사치만 사용해 --days 값이 정확히
    N거래일을 보장하지 않는 문제가 있었다.)
    """
    if not bars or n <= 0:
        return bars
    dates = sorted({b["dt"][:8] for b in bars if len(b["dt"]) >= 8})
    if not dates:
        return bars
    keep = set(dates[-n:])
    return [b for b in bars if b["dt"][:8] in keep]


def fetch_minute_bars(code: str, token: str,
                      period: int = 5, bdays: int = 5,
                      as_of: Optional[datetime] = None) -> list:
    """
    키움 ka10080 (주식분봉조회)로 최근 N영업일 분봉 수집 (페이지네이션).

    키움 조회 API는 next_key/cont_yn을 "다음 요청의 헤더"(next-key, cont-yn)로
    그대로 되돌려줘야 다음 페이지를 반환한다 — body에 next_key를 넣는 방식으로는
    항상 동일한 첫 페이지만 반환되어, bdays 값과 무관하게 1회 응답 분량
    (약 900건)에서 수집이 멈추는 문제가 있었다.

    V1.5 변경:
      - 각 봉에 세션 메타데이터(bar_start/bar_end/session)를 붙인다.
        정규장/애프터마켓/기타는 요청 파라미터가 아니라 응답 봉의 시각으로
        판별한다 — 키움 API가 애프터마켓을 포함하는지 미확인이기 때문에,
        포함되든 아니든 각 봉이 어느 세션인지 항상 드러나게 한다(리뷰 P0-2).
      - as_of 시점에 아직 끝나지 않은 봉(bar_end > as_of)은 제외한다.
        기존에는 as_of를 과거 cutoff 계산에만 써서, 스캔 도중 뒤에 조회된
        종목일수록 더 최신 봉으로 평가되는 문제가 있었다(리뷰 P1-3).
      - 애프터마켓 시행 이후에는 하루 기대 봉 수가 늘어나므로 페이지 상한도
        같은 세션 모델로 계산한다.

    as_of: 스캔 1회 실행 기준 공통 시각(cmd_scan/cmd_detail에서 1회 생성해
    전달). 미지정 시 호출 시점의 datetime.now()를 사용한다.
    """
    as_of = as_of or datetime.now()
    all_bars = []
    next_key = ""
    cont_yn  = ""
    seen_keys = set()

    # 영업일 → 캘린더일 환산(주 5일 근무 기준 7/5배) + 공휴일 여유분
    # (+10: 긴 연휴가 있어도 요청 N영업일을 최대한 확보하기 위한 여유분.
    #  이것만으로 N일 확보를 "보장"하지는 않으며, 부족하면 calc_cvd()의
    #  data_status="PARTIAL"로 드러난다 — 의견/리뷰 반영)
    calendar_days = math.ceil(bdays * 7 / 5) + 10
    cutoff = (as_of - timedelta(days=calendar_days)).strftime("%Y%m%d")

    # bdays가 클수록 더 많은 페이지가 필요 — 요청 기간에 맞춰 안전 상한을 늘림
    include_after = after_market_active(as_of.strftime("%Y%m%d"))
    bars_per_day = _expected_bars_per_calendar_day(period, include_after)
    max_pages = min(60, max(10,
        math.ceil(bdays * bars_per_day / _MIN_BARS_PER_PAGE_EST) + 3))

    for page in range(max_pages):
        body = {
            "stk_cd":       code,
            "tic_scope":    str(period),
            "upd_stkpc_tp": "1",   # 수정주가 적용
        }
        headers_extra = []
        if next_key:
            headers_extra = [f"next-key: {next_key}", f"cont-yn: {cont_yn}"]

        # ka10080: 주식분봉조회
        # (공식 명세에 없는 세션 선택 파라미터나 종목코드 접미사는 추가하지
        #  않는다 — 리뷰 P0-2: 확인되지 않은 요청 방식은 쓰지 않는다)
        data = _kiwoom("/api/dostk/chart", body, "ka10080", token,
                       extra_headers=headers_extra)

        if not data:
            break

        # 응답 구조: {"stk_cd":"005930", "stk_min_pole_chart_qry":[...]}
        rows = (data.get("stk_min_pole_chart_qry") or
                data.get("output") or
                data.get("stk_chart") or [])
        if not rows:
            break

        reached_cutoff = False
        for item in rows:
            # 필드명: cntr_tm(체결시간), open_pric, high_pric, low_pric
            #         cur_prc(종가), trde_qty(체결량)
            dt    = str(item.get("cntr_tm", item.get("dt", "")))
            open_ = _f(item.get("open_pric", 0))
            high  = _f(item.get("high_pric",  0))
            low   = _f(item.get("low_pric",   0))
            close = _f(item.get("cur_prc",    0) or
                       item.get("cur_pric",   0) or
                       item.get("close_pric", 0))
            vol   = _i(item.get("trde_qty",   0))

            if not (close and vol and vol > 0):
                continue

            bar_date = dt[:8] if len(dt) >= 8 else ""
            if bar_date and bar_date < cutoff:
                reached_cutoff = True
                break

            bar_start, bar_end = bar_bounds(dt, period)
            if bar_end is None:
                continue
            # 아직 진행 중이거나 기준시각 이후의 봉은 사용하지 않는다(리뷰 P1-3)
            if bar_end > as_of:
                continue

            all_bars.append({
                "dt": dt, "open": open_, "high": high,
                "low": low, "close": close, "volume": vol,
                "bar_start": bar_start, "bar_end": bar_end,
                "session": session_of(dt),
            })

        next_key = data.get("_next_key", "")
        cont_yn  = data.get("_cont_yn", "")

        # next_key가 이전과 동일하면 더 이상 새 페이지가 없는 것 — 무한루프 방지
        if reached_cutoff or not next_key or cont_yn != "Y" or next_key in seen_keys:
            break
        seen_keys.add(next_key)

    # API 응답 정렬을 가정하지 않고, timestamp 기준으로 명시 정렬 + 중복 제거
    return _dedupe_sort_bars(all_bars)


def split_bars_by_session(bars: list) -> dict:
    """봉 목록을 세션별로 분리한다(정규장/애프터마켓/기타)."""
    out = {_SESSION_REGULAR: [], _SESSION_AFTER: [], _SESSION_OTHER: []}
    for b in bars:
        sess = b.get("session", _SESSION_OTHER)
        out[sess if sess in out else _SESSION_OTHER].append(b)
    return out


_MAX_DAILY_PAGES = 20   # 일봉 연속조회 안전 상한(220캘린더일 확보에 충분한 여유 — 리뷰 추가 P1-1)


def fetch_daily_bars(code: str, token: str, days: int = 60,
                      as_of: Optional[datetime] = None) -> list:
    """
    키움 일봉 조회 (OBV/MA/ATR/유동성용). 연속조회(next-key/cont-yn)로
    페이지네이션 처리한다.

    기존 구현은 _kiwoom()을 1회만 호출해, 요청한 `days`가 API 1페이지
    분량을 넘으면(예: MA120 워밍업을 위한 220캘린더일 요청) 실제로는
    첫 페이지만큼만 수집되고도 조용히 넘어가는 문제가 있었다(리뷰 추가
    P1-1). fetch_minute_bars()와 동일한 방식으로 cont-yn이 이어지는 한
    페이지를 계속 조회하고, 시작일(start)에 도달하거나 더 이상 다음
    페이지가 없으면 멈춘다.
    """
    as_of = as_of or datetime.now()
    end   = as_of.strftime("%Y%m%d")
    start = (as_of - timedelta(days=days)).strftime("%Y%m%d")

    all_rows = []
    next_key = ""
    cont_yn  = ""
    seen_keys = set()

    for page in range(_MAX_DAILY_PAGES):
        body = {
            "stk_cd":       code,
            "base_dt":      end,
            "upd_stkpc_tp": "1",   # 수정주가
        }
        headers_extra = []
        if next_key:
            headers_extra = [f"next-key: {next_key}", f"cont-yn: {cont_yn}"]

        # ka10081: 주식일봉조회
        data = _kiwoom("/api/dostk/chart", body, "ka10081", token,
                       extra_headers=headers_extra)
        if not data:
            break

        # 일봉 응답 구조: {"stk_dt_pole_chart_qry": [...]}
        # 필드: dt(날짜), cur_prc(종가), trde_qty(거래량),
        #       open_pric, high_pric, low_pric
        rows = (data.get("stk_dt_pole_chart_qry") or
                data.get("output") or
                data.get("stk_chart") or [])
        if not rows:
            break

        reached_start = False
        for item in rows:
            dt    = str(item.get("dt", item.get("cntr_tm", "")))
            close = _f(item.get("cur_prc",    0) or
                       item.get("cur_pric",   0) or
                       item.get("close_pric", 0))
            vol   = _i(item.get("trde_qty",   0))
            if not (close and vol and vol > 0):
                continue
            if len(dt) >= 8 and dt[:8] < start:
                reached_start = True
                break
            all_rows.append({
                "dt": dt, "close": close, "volume": vol,
                "open": _f(item.get("open_pric", 0)),
                "high": _f(item.get("high_pric", 0)),
                "low":  _f(item.get("low_pric",  0)),
            })

        next_key = data.get("_next_key", "")
        cont_yn  = data.get("_cont_yn", "")

        if reached_start or not next_key or cont_yn != "Y" or next_key in seen_keys:
            break
        seen_keys.add(next_key)

    return _dedupe_sort_bars(all_rows)


# ─────────────────────────────────────────────────────────────
# 수치 변환
# ─────────────────────────────────────────────────────────────

def _f(v) -> float:
    """
    가격(OHLC) 필드 파싱 전용. 키움 API는 등락 방향 표시를 위해
    절대가격 필드에도 '+'/'-' 부호를 붙여 반환하므로(예: 저가가 "-52700"),
    부호를 그대로 두면 음수 가격으로 오인식되어 CVD 매수비율 계산이
    왜곡된다(예: -121% 같은 범위 밖 값). 항상 절대값으로 반환한다.
    """
    try:
        return abs(float(str(v).replace(",", "").replace("+", "")))
    except (ValueError, TypeError):
        return 0.0


def _i(v) -> int:
    try:
        return int(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return 0


def _fmt(v) -> str:
    if v is None:
        return "-"
    try:
        v = float(str(v).replace(",", ""))
        if abs(v) >= 1e12: return f"{v/1e12:.2f}조"
        if abs(v) >= 1e8:  return f"{v/1e8:.1f}억"
        if abs(v) >= 1e4:  return f"{v/1e4:.0f}만"
        return f"{v:,.0f}"
    except (ValueError, TypeError):
        return str(v)


def _pct(v, digits: int = 1) -> str:
    return f"{v*100:.{digits}f}%" if v is not None else "  -"


def _name6(name: str) -> str:
    """종목명 한글 6자(표시폭 12) 고정."""
    def dw(s):
        w = 0
        for ch in s:
            cp = ord(ch)
            if (0xAC00 <= cp <= 0xD7A3 or 0x1100 <= cp <= 0x11FF or
                0x3130 <= cp <= 0x318F or 0x4E00 <= cp <= 0x9FFF or
                0xFF00 <= cp <= 0xFFEF):
                w += 2
            else:
                w += 1
        return w
    TARGET = 12
    t, w = "", 0
    for ch in name:
        cw = 2 if ord(ch) >= 0x1100 else 1
        if w + cw > TARGET:
            break
        t += ch
        w += cw
    return t + " " * (TARGET - dw(t))


# ─────────────────────────────────────────────────────────────
# CVD 계산 (분봉 기반 — N영업일 전체)
# ─────────────────────────────────────────────────────────────

_CVD_EMPTY_KEYS = (
    "estimated_cvd", "cvd", "cvd_ma", "buy_vol", "sell_vol", "buy_ratio",
    "normalized_delta", "normalized_momentum", "momentum",
    "bull_ratio", "completed_bull_ratio", "bull_ratio_for_rank",
    "intraday_delta", "completeness", "stale", "last_bar_at",
    "last_bar_age_min", "latest_day_traded_value",
    "price_cvd_divergence", "price_cvd_divergence_lag",
)


def _empty_cvd(session: str, status: str, period: int) -> dict:
    """데이터가 없을 때의 CVD 결과 골격(키 구성은 정상 결과와 동일)."""
    out = {k: None for k in _CVD_EMPTY_KEYS}
    out.update({
        "session": session,
        "session_label": _SESSION_LABEL.get(session, session),
        "period": period,
        "momentum_minutes": _MOMENTUM_MINUTES,
        "momentum_minutes_actual": None,
        "momentum_status": "NO_DATA",
        "momentum_bars": 0,
        "bull_days": 0, "total_days": 0, "completed_total_days": 0,
        "bars_used": 0, "expected_bars_per_day": expected_bars_per_day(session, period),
        "missing_days": [], "absent_days": [], "over_count_days": [],
        "signal": "데이터 부족", "score": 0,
        "data_status": status,
    })
    return out


def _momentum_window(bars: list, minutes: int, period: int):
    """
    마지막 봉의 종료 시각 기준 `minutes` 구간에 완전히 들어오는 봉 수(k),
    실제로 커버한 분(actual), 상태를 반환한다(리뷰 P1-4).

    기존 구현은 k = ceil(30/period)로 봉 수만 환산해서, 60분봉이면 60분을
    쓰고도 "30분 모멘텀"이라고 표시했고 무체결·누락·세션 공백이 있으면
    최근 k봉이 30분보다 훨씬 오래된 구간일 수도 있었다. 여기서는 같은
    거래일·같은 세션 안에서 실제 시간 구간으로 자른다.

    상태:
      SUCCESS            — 요청 구간을 모두 채움
      WARMING_UP         — 세션 시작 직후 등 구간이 부족(가진 만큼만 사용)
      RESOLUTION_LIMITED — 분봉 주기가 요청 구간보다 길어 요청 해상도 불가
      NO_DATA            — 사용할 봉이 없음
    """
    if not bars:
        return 0, None, "NO_DATA"
    last = bars[-1]
    last_end = last.get("bar_end")
    if last_end is None:
        return 0, None, "NO_DATA"

    if period > minutes:
        # 요청한 시간 해상도를 이 분봉 주기로는 만들 수 없다 — 보장한다고
        # 표시하지 않고, 실제 사용한 분(period)을 그대로 알린다.
        return 1, period, "RESOLUTION_LIMITED"

    win_start = last_end - timedelta(minutes=minutes)
    k = 0
    first_start = None
    for b in reversed(bars):
        if (b.get("bar_start") is None or
                b.get("session") != last.get("session") or
                b["dt"][:8] != last["dt"][:8] or
                b["bar_start"] < win_start):
            break
        k += 1
        first_start = b["bar_start"]
    if k == 0 or first_start is None:
        return 0, None, "NO_DATA"
    # 커버 구간은 "봉 수 × 주기"가 아니라 실제 벽시계 구간으로 잰다.
    # 정규장 15:20~15:30 단일가 구간처럼 체결이 없어 봉이 없는 구간이 있으면
    # 봉 수로 환산한 값은 실제보다 짧게 나온다(예: 15:35 기준 30분 구간에
    # 봉은 4개=20분). 무체결 구간도 "그 시간 동안 순매수가 0이었다"는 정보이므로
    # 구간 자체는 채워진 것으로 본다 — 봉 누락 여부는 completeness가 따로 본다.
    actual = int((last_end - first_start).total_seconds() // 60)
    return k, actual, ("SUCCESS" if actual >= minutes else "WARMING_UP")


def calc_cvd(bars: list, requested_days: Optional[int] = None,
             period: int = 5, as_of: Optional[datetime] = None,
             session: str = _SESSION_REGULAR,
             trading_days: Optional[list] = None,
             min_bars: int = 10,
             absent_is_error: bool = True) -> dict:
    """
    한 세션(정규장 또는 애프터마켓)의 분봉으로 Estimated CVD를 계산한다.

    체결 데이터가 아닌 분봉 캔들에서 추정한 값이다(추정치, True CVD 아님).
      CLV  = (2×Close - High - Low) / (High - Low)   범위 [-1, 1]
      Body = (Close - Open) / (High - Low)             범위 [-1, 1]
      Direction = 0.6×CLV + 0.4×Body  (혼합 가중치는 초기 설계값, 검증 전)
      Delta_est = Volume × Direction
    High == Low(도지·거래정지 등)이거나 OHLC 관계가 비정상인 봉은
    방향성 0(중립, 50/50 분할)으로 처리한다.

    거래량 규모가 종목마다 크게 다르므로, 원시 누적값 대신
    normalized_delta = ΣDelta / ΣVolume (범위 -1~1)를 채택해
    종목 간 비교 가능한 값으로 정규화한다.

    V1.5(애프터마켓 대응):
      - 세션을 인자로 받는다. 정규장과 애프터마켓을 한 계산에 섞지 않는다
        (리뷰 P1-2 — 여러 날의 양수 Delta 때문에 당일 저녁 매도 흐름이
        가려지는 것을 막는다). 호출부에서 세션별로 따로 호출한다.
      - 완료 판정은 세션 종료시각 + 수신지연 여유 기준(is_session_complete).
      - 기대 봉 수는 세션별(정규장 390분 / 애프터마켓 240분)로 계산하고,
        기대치를 넘는 날은 completeness를 100%로 클램프한 뒤 over_count_days로
        표시한다(리뷰 P1-1 — 기존엔 정규장 78 + 애프터 48 = 126봉이
        completeness 161%로 SUCCESS 판정되어 정규장 누락을 가릴 수 있었다).
      - trading_days(일봉에서 얻은 거래일 목록)와 대조해 분봉이 아예 없는
        거래일(absent_days)을 잡는다(리뷰 P1-5 — 관측된 날짜만 검사하면
        빠진 날이 더 오래된 거래일로 조용히 대체된다).
      - 모멘텀은 실제 시간 구간으로 계산하고 상태를 함께 표시한다(P1-4).
      - Price-CVD Pivot은 완료된 거래일 자료로만 계산한다(P2-2).

    bull_ratio는 요청 구간 전체(당일 포함) 기준 하위호환 필드이고,
    completed_bull_ratio는 완료된 거래일만으로 계산한 값이다. 점수·Gate에는
    완료 거래일이 _MIN_COMPLETED_DAYS_FOR_RATIO일 이상일 때의
    bull_ratio_for_rank만 사용한다 — 당일 장중 값으로 폴백하지 않는다(P2-1).
    """
    as_of = as_of or datetime.now()

    if not bars:
        if session == _SESSION_AFTER:
            applicable = [d for d in (trading_days or [])
                          if after_market_active(d) and
                          is_session_complete(d, session, as_of)]
            has_today = after_market_active(as_of.strftime("%Y%m%d"))
            status = "NO_DATA" if (applicable or has_today) else "NOT_APPLICABLE"
        else:
            status = "FAILED"
        return _empty_cvd(session, status, period)

    cvd_s = [0.0]
    total_buy = total_sell = total_vol = 0.0

    for bar in bars:
        o, h, l, c, v = (bar["open"], bar["high"], bar["low"],
                          bar["close"], bar["volume"])
        if v <= 0:
            cvd_s.append(cvd_s[-1])
            continue

        hl = h - l
        valid_ohlc = hl > 0 and l <= o <= h and l <= c <= h
        if valid_ohlc:
            clv    = (2 * c - h - l) / hl
            body   = (c - o) / hl
            direction = max(-1.0, min(1.0, 0.6 * clv + 0.4 * body))
        else:
            direction = 0.0   # 도지/비정상 OHLC → 중립(50/50)

        buy_v  = v * (0.5 + direction * 0.5)
        sell_v = v - buy_v

        total_buy  += buy_v
        total_sell += sell_v
        total_vol  += v
        cvd_s.append(cvd_s[-1] + (buy_v - sell_v))

    cur = cvd_s[-1]
    n   = min(20, len(cvd_s))
    ma  = sum(cvd_s[-n:]) / n if n else 0.0

    normalized_delta = (cur / total_vol) if total_vol > 0 else None
    buy_ratio = (total_buy / total_vol * 100) if total_vol > 0 else None

    # 시간 기준 모멘텀 — 봉 개수 환산이 아니라 실제 구간으로 자른다(P1-4)
    k, momentum_minutes_actual, momentum_status = _momentum_window(
        bars, _MOMENTUM_MINUTES, period)
    k = min(k, len(bars))
    if k > 0 and len(cvd_s) > k:
        momentum = cvd_s[-1] - cvd_s[-1 - k]
        recent_vol = sum(b["volume"] for b in bars[-k:])
        normalized_momentum = (momentum / recent_vol) if recent_vol > 0 else None
    else:
        momentum, normalized_momentum = 0.0, None

    # 거래일별 순매수(Delta) 우위 여부 — 절대 일수가 아닌 비율(bull_ratio)로 판단.
    # 동시에 Price-CVD Pivot 다이버전스용 일별 종가·일말 누적CVD도 수집한다.
    day_deltas = []
    day_bar_counts = {}
    day_dates = []
    day_last_close = []
    day_end_cvd = []
    prev_date  = ""
    day_start  = 0.0
    last_close_seen = None
    for i, bar in enumerate(bars):
        d = bar["dt"][:8]
        day_bar_counts[d] = day_bar_counts.get(d, 0) + 1
        if d != prev_date:
            if prev_date:
                day_deltas.append(cvd_s[i] - day_start)
                day_dates.append(prev_date)
                day_last_close.append(last_close_seen)
                day_end_cvd.append(cvd_s[i])
            day_start = cvd_s[i]
            prev_date = d
        last_close_seen = bar["close"]
    if prev_date:
        day_deltas.append(cvd_s[-1] - day_start)
        day_dates.append(prev_date)
        day_last_close.append(last_close_seen)
        day_end_cvd.append(cvd_s[-1])

    total_days = len(day_deltas)
    bull_days  = sum(1 for d in day_deltas if d > 0)
    bull_ratio = (bull_days / total_days) if total_days > 0 else None

    # 완료 세션(세션 종료 + 여유 경과) 기준 bull_ratio와, 진행 중인 세션의
    # 부분 Delta를 분리한다(리뷰 P1-3).
    completed_idx = [i for i, d in enumerate(day_dates)
                     if is_session_complete(d, session, as_of)]
    completed_total_days = len(completed_idx)
    completed_bull_days  = sum(1 for i in completed_idx if day_deltas[i] > 0)
    completed_bull_ratio = (completed_bull_days / completed_total_days
                            if completed_total_days > 0 else None)
    completed_set = set(completed_idx)
    incomplete_idx = [i for i in range(len(day_dates)) if i not in completed_set]
    intraday_delta = day_deltas[incomplete_idx[0]] if incomplete_idx else None

    # 점수·Gate에 쓸 값 — 완료 거래일이 너무 적으면 사용하지 않는다(P2-1)
    bull_ratio_for_rank = (completed_bull_ratio
                           if completed_total_days >= _MIN_COMPLETED_DAYS_FOR_RATIO
                           else None)

    # Price-CVD Pivot 다이버전스 — 완료된 거래일 자료로만 계산한다(P2-2).
    # (진행 중인 마지막 봉이 섞이면 장중에 확정 표시가 다시 바뀔 수 있다)
    closes_c = [day_last_close[i] for i in completed_idx]
    cvds_c   = [day_end_cvd[i] for i in completed_idx]
    price_cvd_div, price_cvd_confirmed_idx = _pivot_divergence(
        closes_c, cvds_c, left=1, right=1, min_gap=2)
    price_cvd_lag_days = ((len(closes_c) - 1 - price_cvd_confirmed_idx)
                          if price_cvd_confirmed_idx is not None else None)

    # 완성도: 완료된 세션만으로 계산. 기대 봉 수는 세션 분수 ÷ period(올림).
    expected_per_day = expected_bars_per_day(session, period)
    complete_dates = [d for d in day_bar_counts
                      if is_session_complete(d, session, as_of)]
    expected_total = expected_per_day * len(complete_dates)
    # 기대치를 초과한 날은 100%로 클램프 — 초과분이 다른 날 누락을 상쇄해
    # 완성도가 100%를 넘기면서 누락이 가려지는 문제를 막는다(리뷰 P1-1).
    actual_total = sum(min(day_bar_counts[d], expected_per_day) for d in complete_dates)
    completeness = (actual_total / expected_total) if expected_total > 0 else None
    # 기대치를 1봉 넘는 것(15:20 정각 체결 봉)은 정상이므로 상한으로 판정한다
    max_per_day = max_bars_per_day(session, period)
    over_count_days = [d for d in complete_dates
                       if day_bar_counts[d] > max_per_day]

    # 거래일별 완성도 — 집계 비율만으로는 가려지는 "특정일 집중 누락" 탐지
    missing_days = [d for d in complete_dates
                    if day_bar_counts[d] < expected_per_day * _MIN_DAY_COMPLETENESS]

    # 분봉이 아예 없는 거래일(일봉 거래일 목록과 대조) — 리뷰 P1-5
    absent_days = []
    if trading_days:
        lo, hi = min(day_bar_counts), max(day_bar_counts)
        for d in trading_days:
            if not (lo <= d <= hi) or d in day_bar_counts:
                continue
            if not is_session_complete(d, session, as_of):
                continue
            absent_days.append(d)

    # 최신성 — (1) 완료 거래일이 캘린더 기준으로 오래됨,
    #          (2) 세션이 열려 있는데 마지막 봉이 오래됨(리뷰 P1-5)
    stale = None
    stale_reason = ""
    if complete_dates:
        try:
            last_complete_dt = datetime.strptime(max(complete_dates), "%Y%m%d")
            stale = (as_of - last_complete_dt).days > _MAX_STALE_CALENDAR_DAYS
            if stale:
                stale_reason = f"최근 완료 거래일 {max(complete_dates)}"
        except ValueError:
            stale = None

    last_bar_end = bars[-1].get("bar_end")
    last_bar_age_min = (((as_of - last_bar_end).total_seconds() / 60.0)
                        if last_bar_end else None)
    today_str = as_of.strftime("%Y%m%d")
    today_is_trading = (today_str in set(trading_days)) if trading_days else None
    if (last_bar_age_min is not None and session_is_open(as_of, session) and
            today_is_trading is not False):
        # 진행 중 세션에서 최근 봉이 오래 끊겼으면 최신성 문제로 본다.
        # 단 세션이 막 시작된 구간(첫 봉이 아직 완성되지 않은 시점)은 제외한다 —
        # 09:04에 "09:00봉 미완성"은 결함이 아니라 아직 데이터가 없는 것이다.
        # 무체결과 API 누락은 이 정보만으로 구분할 수 없어 "표시"만 한다.
        sess_start = session_start_dt(today_str, session)
        elapsed_min = ((as_of - sess_start).total_seconds() / 60.0
                       if sess_start else None)
        warmup_min = period + _SESSION_COMPLETE_GRACE_MIN
        stale_limit = max(3 * period, 15)
        if (elapsed_min is not None and elapsed_min >= warmup_min and
                last_bar_age_min > stale_limit):
            stale = True
            stale_reason = (f"진행 중 세션인데 마지막 봉이 {last_bar_age_min:.0f}분 전"
                            f"(기준 {stale_limit}분, 무체결/수집누락 구분 불가)")

    data_status = "SUCCESS"
    reasons = []
    if len(bars) < min_bars:
        data_status = "PARTIAL"
        reasons.append(f"봉 수 부족({len(bars)}<{min_bars})")
    # 수집 "일수" 충족 여부는 당일(진행 중) 세션도 하루로 인정하는 total_days로
    # 판정한다 — 완료일 기준으로 바꾸면 장중에는 항상 요청일수보다 1일 부족해져
    # 모든 종목이 상시 PARTIAL이 되는 회귀가 생긴다.
    if requested_days and total_days < requested_days:
        data_status = "PARTIAL"
        reasons.append(f"수집일수 {total_days}/{requested_days}일")
    if completeness is not None and completeness < 0.80:
        data_status = "PARTIAL"
        reasons.append(f"완성도 {completeness*100:.0f}%<80%")
    if missing_days:
        data_status = "PARTIAL"
        reasons.append(f"부분누락일 {len(missing_days)}일")
    if over_count_days:
        data_status = "PARTIAL"
        reasons.append(f"기대 초과 봉 {len(over_count_days)}일(세션 혼입 의심)")
    if absent_days and absent_is_error:
        data_status = "PARTIAL"
        reasons.append(f"전체누락일 {len(absent_days)}일")
    if stale:
        data_status = "PARTIAL"
        reasons.append(f"최신성 문제({stale_reason})" if stale_reason else "최신성 문제")

    # 등급화 점수(참고용 배지) — 정규화 값 기준. 순위 산정의 주 척도는
    # cmd_scan 단계에서 스캔 유니버스 백분위로 별도 계산한다.
    score = 0.0
    if normalized_delta is not None:
        if normalized_delta >= 0.10:      score += 2
        elif normalized_delta >= 0.03:    score += 1
        elif normalized_delta <= -0.10:   score -= 2
        elif normalized_delta <= -0.03:   score -= 1
    if normalized_momentum is not None:
        if normalized_momentum >= 0.05:   score += 1
        elif normalized_momentum <= -0.05: score -= 1
    if bull_ratio is not None:
        if bull_ratio >= 0.65:   score += 2
        elif bull_ratio >= 0.55: score += 1
        elif bull_ratio <= 0.35: score -= 1

    nd = normalized_delta or 0.0
    nm = normalized_momentum or 0.0
    sig = ("🟢강한매수" if nd >= 0.10 and nm > 0 else
           "🟢매수우위" if nd > 0 else
           "🔴강한매도" if nd <= -0.10 and nm < 0 else
           "🔴매도우위" if nd < 0 else "⬜중립")

    # 최근 거래일(해당 세션)의 거래대금 근사치 — 애프터마켓 유동성 확인용
    latest_date = max(day_bar_counts)
    latest_day_traded_value = sum(b["close"] * b["volume"] for b in bars
                                  if b["dt"][:8] == latest_date)

    return {
        "session":       session,
        "session_label": _SESSION_LABEL.get(session, session),
        "period":        period,
        "estimated_cvd": round(cur),
        "cvd":            round(cur),   # 하위호환 필드명(값은 동일)
        "cvd_ma":         round(ma),
        "buy_vol":        round(total_buy),
        "sell_vol":       round(total_sell),
        "buy_ratio":      round(buy_ratio, 1) if buy_ratio is not None else None,
        # 순위·Gate 판정에 쓰이는 값은 조기 반올림하지 않는다(표시 시점에만
        # 반올림). 화면 출력은 f"{v:.4f}" 등 포맷 지정자로 반올림한다.
        "normalized_delta":    normalized_delta,
        "normalized_momentum": normalized_momentum,
        "momentum":       round(momentum),
        "momentum_minutes":        _MOMENTUM_MINUTES,
        "momentum_minutes_actual": momentum_minutes_actual,
        "momentum_status":         momentum_status,
        "momentum_bars":           k,
        "bull_days":      bull_days,
        "total_days":     total_days,
        "bull_ratio":     bull_ratio,
        "completed_bull_ratio": completed_bull_ratio,
        "completed_total_days": completed_total_days,
        "bull_ratio_for_rank":  bull_ratio_for_rank,
        "intraday_delta": round(intraday_delta) if intraday_delta is not None else None,
        "completeness":   completeness,
        "expected_bars_per_day": expected_per_day,
        "bars_used":      len(bars),
        "missing_days":   missing_days,
        "absent_days":    absent_days,
        "over_count_days": over_count_days,
        "stale":          stale,
        "last_bar_at":    last_bar_end.strftime("%Y-%m-%d %H:%M") if last_bar_end else None,
        "last_bar_age_min": round(last_bar_age_min, 1) if last_bar_age_min is not None else None,
        "latest_day_traded_value": latest_day_traded_value,
        "price_cvd_divergence":     price_cvd_div,
        "price_cvd_divergence_lag": price_cvd_lag_days,
        "signal":         sig,
        "score":          score,
        "data_status":    data_status,
        "status_reasons": reasons,
    }


# ─────────────────────────────────────────────────────────────
# Pivot 기반 다이버전스 (OBV·Price-CVD 공용 — 리뷰 반영)
# ─────────────────────────────────────────────────────────────
#
# 기존 다이버전스(calc_obv의 "divergence")는 구간 양 끝점(endpoint) 2개만
# 비교하는 방향불일치(direction mismatch) 판정이다 — 저점/고점 구조를
# 확인하지 않는다. 여기서는 실제 swing low/high(Pivot)를 찾아 그 지점의
# 값을 비교한다. Pivot은 오른쪽 `right`개 봉이 더 지나야 "확정"되므로
# (그래야 그 지점이 진짜 저점/고점이었는지 알 수 있다), 확정되지 않은
# 최근 Pivot은 사용하지 않는다 — 미래정보 참조 방지.
#
# 아직 배점(스코어)에는 반영하지 않고 참고용 표시로만 제공한다(리뷰 지적:
# "Pivot 구현만으로 자동 증액하지 말고 검증 후 배점을 정한다").

def _find_pivots(values: list, left: int = 2, right: int = 2):
    """
    values에서 좌우 `left`/`right`개 이웃보다 엄격히 낮은/높은 지점을
    각각 pivot low / pivot high로 찾는다(동률 구간은 모호해 제외).
    반환: (lows, highs), 각 원소는 (index, value, confirmed_index).
    confirmed_index = index + right — 이 시점 데이터까지 있어야 확정된 pivot.
    """
    lows, highs = [], []
    n = len(values)
    for i in range(left, n - right):
        window = values[i - left:i + right + 1]
        v = values[i]
        if v == min(window) and window.count(v) == 1:
            lows.append((i, v, i + right))
        if v == max(window) and window.count(v) == 1:
            highs.append((i, v, i + right))
    return lows, highs


def _pivot_divergence(price_series: list, indicator_series: list,
                       left: int = 2, right: int = 2, min_gap: int = 3):
    """
    가격 Pivot 시점의 보조지표(OBV/CVD 누적값) 비교로 강세/약세 다이버전스를
    판정한다(확정된 Pivot만 사용).
      강세: Low2 < Low1  (가격 저점 하락)  그리고 지표(Low2 시점) > 지표(Low1 시점)
      약세: High2 > High1(가격 고점 상승)  그리고 지표(High2 시점) < 지표(High1 시점)
    최소 Pivot 간격(min_gap)·좌우 확인 봉 수(left/right)는 초기 설계값(의견).
    반환: (label, confirmed_index) 또는 (None, None).
    """
    if len(price_series) != len(indicator_series) or len(price_series) < left + right + 1:
        return None, None

    lows, highs = _find_pivots(price_series, left, right)
    n = len(price_series)
    lows  = [p for p in lows  if p[2] < n]
    highs = [p for p in highs if p[2] < n]

    label, confirmed_at = None, None

    if len(lows) >= 2:
        (i1, p1, _), (i2, p2, c2) = lows[-2], lows[-1]
        if i2 - i1 >= min_gap and p2 < p1 and indicator_series[i2] > indicator_series[i1]:
            label, confirmed_at = "강세", c2

    if len(highs) >= 2:
        (i1, p1, _), (i2, p2, c2) = highs[-2], highs[-1]
        if i2 - i1 >= min_gap and p2 > p1 and indicator_series[i2] < indicator_series[i1]:
            if confirmed_at is None or c2 >= confirmed_at:
                label, confirmed_at = "약세", c2

    return label, confirmed_at


# ─────────────────────────────────────────────────────────────
# OBV 계산 (일봉 기반)
# ─────────────────────────────────────────────────────────────

def calc_obv(daily_bars: list, divergence_lookback: int = 5) -> dict:
    """
    일봉 기반 OBV 계산 + 지정 기간(divergence_lookback) 기울기/다이버전스.
    lookback은 실제 보유한 일봉 길이로 자동 상한된다.
    up_ratio는 lookback 구간 중 종가 상승일 비율(방향만 반영).
    normalized_slope = OBV 변화량(slope) / 구간 거래량 합 — 방향뿐 아니라
    크기까지 반영하는 정규화 지표로, CVD의 normalized_delta와 같은 방식이다
    (리뷰가 제시한 계산식; up_ratio를 대체할지 병행할지는 검증 전이라
    둘 다 노출한다).
    pivot_divergence는 endpoint 비교가 아닌 실제 swing low/high 기반
    다이버전스로, 아직 배점에는 반영하지 않는 참고용 표시다(리뷰 반영).
    """
    if len(daily_bars) < max(6, divergence_lookback + 1):
        return {
            "obv": None, "obv_ma20": None, "slope": None,
            "up_ratio": None, "normalized_slope": None,
            "signal": "데이터 부족", "divergence": None,
            "pivot_divergence": None, "pivot_divergence_lag": None,
            "score": 0, "data_status": "FAILED", "lookback": 0,
        }

    closes  = [b["close"]  for b in daily_bars]
    volumes = [b["volume"] for b in daily_bars]

    obv_s = [0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i-1]:
            obv_s.append(obv_s[-1] + volumes[i])
        elif closes[i] < closes[i-1]:
            obv_s.append(obv_s[-1] - volumes[i])
        else:
            obv_s.append(obv_s[-1])

    cur = obv_s[-1]

    MIN_DAYS_FOR_MA20 = 20
    ma20_ready = len(obv_s) >= MIN_DAYS_FOR_MA20
    ma20 = (sum(obv_s[-MIN_DAYS_FOR_MA20:]) / MIN_DAYS_FOR_MA20) if ma20_ready else None

    k = max(0, min(divergence_lookback, len(obv_s) - 1))
    slope = (obv_s[-1] - obv_s[-1 - k]) if k > 0 else None

    up_days = 0
    for i in range(len(closes) - k, len(closes)):
        if i <= 0:
            continue
        if closes[i] > closes[i - 1]:
            up_days += 1
    up_ratio = (up_days / k) if k > 0 else None

    window_volume = sum(volumes[-k:]) if k > 0 else 0
    normalized_slope = (slope / window_volume) if (k > 0 and window_volume > 0 and slope is not None) else None

    div = None
    if k > 0:
        pc = closes[-1] - closes[-1 - k]
        oc = obv_s[-1]  - obv_s[-1 - k]
        if pc < 0 and oc > 0:
            div = "강세"   # 가격 하락 + OBV 상승 → 저점 매집(의견)
        elif pc > 0 and oc < 0:
            div = "약세"   # 가격 상승 + OBV 하락 → 상승 신뢰도 저하(의견)

    # Pivot 기반(swing low/high) 다이버전스 — 위 endpoint 기반 div와 별개
    pivot_div, pivot_confirmed_idx = _pivot_divergence(closes, obv_s, left=2, right=2, min_gap=3)
    pivot_lag = (len(closes) - 1 - pivot_confirmed_idx) if pivot_confirmed_idx is not None else None

    score = 0.0
    if ma20 is not None:
        score += 2 if cur > ma20 else (-2 if cur < ma20 else 0)
    if slope is not None:
        score += 1 if slope > 0 else (-1 if slope < 0 else 0)
    if div == "강세": score += 2
    if div == "약세": score -= 2

    sig = ("🟢매수축적" if (ma20 is not None and slope is not None and cur > ma20 and slope > 0) else
           "🔴매도이탈" if (ma20 is not None and slope is not None and cur < ma20 and slope < 0) else
           "⬜중립")

    return {
        "obv":        round(cur),
        "obv_ma20":   round(ma20) if ma20 is not None else None,
        "slope":      round(slope) if slope is not None else None,
        # up_ratio/normalized_slope는 Gate·순위 판정에 쓰이므로 조기
        # 반올림하지 않는다(표시 시점에만 반올림 — 리뷰 추가 P2-1).
        "up_ratio":   up_ratio,
        "normalized_slope": normalized_slope,
        "signal":     sig,
        "divergence": div,
        "pivot_divergence":     pivot_div,
        "pivot_divergence_lag": pivot_lag,
        "score":      score,
        "data_status": "SUCCESS" if ma20 is not None else "PARTIAL",
        "lookback":   k,
    }


# ─────────────────────────────────────────────────────────────
# 가격추세 · 유동성 (일봉 재사용 — 추가 API 호출 없음)
# ─────────────────────────────────────────────────────────────

def calc_price_trend(daily_bars: list) -> dict:
    """
    일봉 종가 기준 추세: MA20/60/120 대비 위치, MA20/60 기울기(5일 전 대비),
    MA20>MA60 정배열 여부. MA20은 핵심(20일 미만이면 PARTIAL), MA60/120은
    데이터가 부족하면 조용히 None으로 빠지는 보조 지표다(리뷰 반영 — MA60/120
    추가, 워밍업 부족 시 명시적 결측).
    """
    if len(daily_bars) < 20:
        return {"above_ma20": None, "ma20_rising": None, "close_vs_ma20_pct": None,
                "above_ma60": None, "ma60_rising": None, "ma20_above_ma60": None,
                "above_ma120": None, "score": 0, "data_status": "PARTIAL"}

    closes = [b["close"] for b in daily_bars]
    n = len(closes)

    def _ma(period: int, offset: int = 0) -> Optional[float]:
        """offset일 전 시점 기준 period일 이동평균. 데이터 부족하면 None."""
        end = n - offset
        start = end - period
        if start < 0:
            return None
        return sum(closes[start:end]) / period

    ma20_now  = _ma(20)
    ma60_now  = _ma(60)
    ma120_now = _ma(120)

    above_ma20  = closes[-1] > ma20_now
    above_ma60  = (closes[-1] > ma60_now) if ma60_now is not None else None
    above_ma120 = (closes[-1] > ma120_now) if ma120_now is not None else None
    ma20_above_ma60 = (ma20_now > ma60_now) if ma60_now is not None else None

    ma20_prev = _ma(20, offset=5)
    ma60_prev = _ma(60, offset=5)
    ma20_rising = (ma20_now > ma20_prev) if ma20_prev is not None else None
    ma60_rising = (ma60_now > ma60_prev) if (ma60_now is not None and ma60_prev is not None) else None

    score = 0.0
    score += 1 if above_ma20 else -1
    if ma20_rising is True:
        score += 1
    elif ma20_rising is False:
        score -= 1

    return {
        "above_ma20":      above_ma20,
        "ma20_rising":     ma20_rising,
        "close_vs_ma20_pct": round((closes[-1] / ma20_now - 1) * 100, 2),
        "above_ma60":      above_ma60,
        "ma60_rising":     ma60_rising,
        "ma20_above_ma60": ma20_above_ma60,
        "above_ma120":     above_ma120,
        "score": score,
        "data_status": "SUCCESS",
    }


def calc_risk(daily_bars: list, close_vs_ma20_pct: Optional[float] = None) -> dict:
    """
    ATR14(Wilder 평활) 기반 변동성 및 단기 과열 참고 지표(리뷰 반영).

      TR_t  = max(High_t-Low_t, |High_t-Close_(t-1)|, |Low_t-Close_(t-1)|)
      ATR14 = TR의 14기간 Wilder 평활값 (첫 값은 단순평균, 이후 재귀평활)
      ATR% = ATR14 / Close × 100  — 종목 간 상대 변동성 비교용

    overheated: 종가가 MA20 대비 +15% 초과 이격되면 과열로 간주(리뷰 예시값,
    검증 전 초기 설계값 — 정확히 15%가 아니라 "15% 초과"이다). 과열이면
    총점에 -5점 페널티를 적용한다(_finalize_scores).
    위험이 클수록 점수가 낮아지는 방향이며, "위험할수록 높은 점수"가 아니다.

    과열 판정은 close_vs_ma20_pct만 있으면 계산 가능하므로 ATR14 계산
    성공 여부와 분리한다(리뷰 추가 P1-4 — 기존엔 고가/저가 결측 등으로
    ATR14를 못 구하면 이미 계산된 이격 과열 페널티까지 조기 return으로
    함께 사라졌다). ATR14 자체(변동성 수치)는 여전히 참고용이며 Gate의
    필수 조건은 아니다 — data_status가 PARTIAL이면 ATR 값이 없다는 뜻일
    뿐, 과열 페널티 적용 여부와는 무관하다.
    """
    overheated = (close_vs_ma20_pct is not None and close_vs_ma20_pct > 15)
    overheat_penalty = -5.0 if overheated else 0.0

    MIN_DAYS = 15
    if len(daily_bars) < MIN_DAYS:
        return {"atr": None, "atr_pct": None, "overheated": overheated,
                "overheat_penalty": overheat_penalty, "data_status": "PARTIAL"}

    trs = []
    for i in range(1, len(daily_bars)):
        h, l = daily_bars[i]["high"], daily_bars[i]["low"]
        prev_close = daily_bars[i - 1]["close"]
        # 저가>고가 등 비정상 OHLC 행은 TR 계산에서 제외한다(리뷰 추가 P2-2).
        if h <= 0 or l <= 0 or prev_close <= 0 or l > h:
            continue
        tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)

    period = 14
    if len(trs) < period:
        return {"atr": None, "atr_pct": None, "overheated": overheated,
                "overheat_penalty": overheat_penalty, "data_status": "PARTIAL"}

    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period

    close = daily_bars[-1]["close"]
    atr_pct = (atr / close * 100) if close > 0 else None

    return {
        "atr":     round(atr, 2),
        "atr_pct": round(atr_pct, 2) if atr_pct is not None else None,
        "overheated": overheated,
        "overheat_penalty": overheat_penalty,
        "data_status": "SUCCESS",
    }


def calc_liquidity(daily_bars: list, n: int = 20) -> dict:
    """
    최근 최대 n거래일 평균 거래대금(종가×거래량, 근사치 — 실제 거래대금
    필드가 아니라 종가×거래량으로 근사한 값).

    기존 구현은 5~19일치만 있어도 조용히 그 평균을 반환해 "20일 평균"으로
    오인될 수 있었다(리뷰 지적). 실제 사용 일수(days_used)와 20일 미달 시
    PARTIAL 상태를 함께 반환한다.
    """
    MIN_DAYS = 5
    if len(daily_bars) < MIN_DAYS:
        return {"value": None, "days_used": 0, "data_status": "FAILED"}
    recent = daily_bars[-n:]
    days_used = len(recent)
    value = sum(b["close"] * b["volume"] for b in recent) / days_used
    return {
        "value": value,
        "days_used": days_used,
        "data_status": "SUCCESS" if days_used >= n else "PARTIAL",
    }


# ─────────────────────────────────────────────────────────────
# 단일 종목 분석
# ─────────────────────────────────────────────────────────────

# 애프터마켓은 세션이 짧고(240분) 제도 시행 초기라 역사 데이터가 적어
# 정규장과 같은 최소 봉 수를 요구하면 상시 데이터부족이 된다(의견, 검증 전).
_AFTER_MIN_BARS = 3


def compare_regular_and_daily_close(regular_bars: list, daily_bars: list) -> Optional[dict]:
    """
    같은 거래일의 "정규장 마지막 분봉 종가"와 "일봉 종가"를 비교한다.

    키움 일봉 종가는 애프터마켓 최종 체결가여서 제도상 공식 종가(15:30)와
    다를 수 있다(2026-09-16 실측). MA·OBV·ATR·과열 판정이 모두 일봉 종가를
    쓰므로, 둘의 괴리를 표시해 해석 오류를 막는다.
    반환: {date, regular_close, daily_close, diff_pct} 또는 None.
    """
    if not regular_bars or not daily_bars:
        return None
    last_reg_by_date = {}
    for b in regular_bars:
        last_reg_by_date[b["dt"][:8]] = b["close"]
    daily_by_date = {b["dt"][:8]: b["close"] for b in daily_bars}
    common = sorted(set(last_reg_by_date) & set(daily_by_date))
    if not common:
        return None
    d = common[-1]
    reg_close, day_close = last_reg_by_date[d], daily_by_date[d]
    if not reg_close:
        return None
    return {
        "date": d,
        "regular_close": reg_close,
        "daily_close": day_close,
        "diff_pct": (day_close / reg_close - 1) * 100,
    }


def analyze(code: str, token: str,
            period: int = 5, bdays: int = 5,
            name: str = "",
            as_of: Optional[datetime] = None,
            daily_scope: str = "auto",
            rank_session: str = _SESSION_REGULAR) -> Optional[dict]:
    """
    단일 종목 CVD(정규장·애프터마켓 분리) + OBV + 가격추세 + 유동성 분석.

    as_of: 스캔/조회 1회 실행 기준 공통 시각. cmd_scan()·cmd_detail()에서
    1회 생성해 모든 종목에 동일하게 전달한다.
    daily_scope: 일봉 완료 판정 기준(--daily-scope 참고).
    rank_session: 점수·Gate 판정의 기준 세션(REGULAR 기본 / AFTER).
      두 세션 지표는 항상 모두 계산해 cvd_regular·cvd_after로 함께 반환한다
      (리뷰 P1-2 — 정규장 점수는 유지하고 애프터마켓 값을 추가한다).
    """
    as_of = as_of or datetime.now()
    # 종목명 조회 (캐시된 이름 없으면 API 호출)
    stock_name = name or fetch_stock_name(code, token) or code

    # 분봉: 최근 bdays영업일치 한 번에 수집 후 정확히 N영업일로 자름.
    # 세션 분류는 봉 타임스탬프 기준(정규장/애프터마켓/기타).
    all_min_bars = fetch_minute_bars(code, token, period=period, bdays=bdays, as_of=as_of)
    all_min_bars = _trim_to_last_n_trading_days(all_min_bars, bdays)
    by_session = split_bars_by_session(all_min_bars)
    reg_bars = by_session[_SESSION_REGULAR]
    aft_bars = by_session[_SESSION_AFTER]
    oth_bars = by_session[_SESSION_OTHER]

    # 일봉: OBV·MA20/60/120·가격추세·유동성·ATR용 — 요청 bdays, MA20 워밍업,
    # MA120(+기울기 비교용 여유) 워밍업을 모두 커버하도록 최소 220 캘린더일 확보
    daily_fetch_days = max(220, math.ceil(bdays * 7 / 5) + 15)
    daily_bars = fetch_daily_bars(code, token, days=daily_fetch_days, as_of=as_of)

    # 일봉에 존재하는 날짜를 거래일 목록의 근사치로 쓴다 — 분봉이 통째로
    # 빠진 거래일을 잡기 위해서다(리뷰 P1-5). 분봉과 다른 API라서 분봉
    # 누락과 독립적이다.
    trading_days = sorted({b["dt"][:8] for b in daily_bars if len(b["dt"]) >= 8})

    # 미완성 일봉은 OBV·MA·ATR·유동성 계산에서 제외한다(리뷰 P0-1).
    # 완료 시점은 --daily-scope 정책에 따른다(기본 auto = 애프터마켓 종료 후).
    daily_bars_completed = [b for b in daily_bars
                             if _is_bar_day_complete(b["dt"][:8], as_of, daily_scope)]

    if not all_min_bars and not daily_bars_completed:
        return None

    cvd_regular = calc_cvd(reg_bars, requested_days=bdays, period=period,
                           as_of=as_of, session=_SESSION_REGULAR,
                           trading_days=trading_days)
    # 애프터마켓: 요청 일수 기준을 적용하지 않는다 — 제도 시행일 이후의
    # 거래일만 존재하므로 "N영업일 미확보"는 데이터 결함이 아니다(리뷰 P1-1).
    cvd_after = calc_cvd(aft_bars, requested_days=None, period=period,
                         as_of=as_of, session=_SESSION_AFTER,
                         trading_days=trading_days,
                         min_bars=_AFTER_MIN_BARS, absent_is_error=False)

    obv   = calc_obv(daily_bars_completed, divergence_lookback=bdays)
    price = calc_price_trend(daily_bars_completed)
    liquidity = calc_liquidity(daily_bars_completed)
    risk  = calc_risk(daily_bars_completed, close_vs_ma20_pct=price.get("close_vs_ma20_pct"))

    cvd = cvd_after if rank_session == _SESSION_AFTER else cvd_regular
    close_check = compare_regular_and_daily_close(reg_bars, daily_bars_completed)

    return {
        "code": code,
        "name": stock_name,
        "cvd": cvd,                 # 점수·Gate 기준 세션의 CVD
        "cvd_regular": cvd_regular,
        "cvd_after":   cvd_after,
        "rank_session": rank_session,
        "obv": obv,
        "price": price,
        "liquidity": liquidity,
        "risk": risk,
        "raw_score": cvd["score"] + obv["score"] + price["score"],
        "min_bars": len(all_min_bars),
        "bars_by_session": {
            _SESSION_REGULAR: len(reg_bars),
            _SESSION_AFTER:   len(aft_bars),
            _SESSION_OTHER:   len(oth_bars),
        },
        "days": cvd.get("total_days", 0),
        "daily_bars_used": len(daily_bars_completed),
        "daily_scope": daily_scope,
        "close_check": close_check,
        "as_of": as_of.strftime("%Y-%m-%d %H:%M:%S"),
    }


# ─────────────────────────────────────────────────────────────
# 100점 합성 점수 (스캔 유니버스 내 백분위 기반)
# ─────────────────────────────────────────────────────────────

def _percentile_ranks(values_map: dict) -> dict:
    """
    {key: value(or None)} → {key: 백분위(0~1) or None}.
    동률 값은 평균 순위로 백분위를 매긴다. 유효값이 1개면 0.5(중립).
    """
    valid = sorted(
        ((k, v) for k, v in values_map.items() if v is not None),
        key=lambda item: item[1],
    )
    ranks = {}
    n = len(valid)
    if n == 0:
        return {k: None for k in values_map}
    if n == 1:
        ranks[valid[0][0]] = 0.5
    else:
        i = 0
        while i < n:
            j = i
            while j + 1 < n and valid[j + 1][1] == valid[i][1]:
                j += 1
            avg_rank = (i + j) / 2
            pct = avg_rank / (n - 1)
            for idx in range(i, j + 1):
                ranks[valid[idx][0]] = pct
            i = j + 1
    for k in values_map:
        ranks.setdefault(k, None)
    return ranks


def _finalize_scores(results: list) -> None:
    """
    스캔 유니버스 내 상대 백분위를 반영한 0~100점 합성 점수를 계산해
    각 result dict에 total/세부 점수를 채워 넣는다.

    배점 구성(검증 전 잠정 가중치, 의견):
      CVD 40  = 정규화delta 백분위 20 + 정규화모멘텀 백분위 10 + bull_ratio 백분위 10
                (delta·모멘텀은 기준 세션(--session)의 값, bull_ratio는 항상
                 정규장 완료 거래일 기준값(bull_ratio_for_rank) — 완료 거래일이
                 _MIN_COMPLETED_DAYS_FOR_RATIO일 미만이면 결측 처리해 0점.
                 Gate와 점수가 같은 정의를 쓰도록 통일했다, 리뷰 P2-1)
      OBV 35  = MA20 상회(절대) 15 + normalized_slope 백분위 20
                (normalized_slope = OBV 변화량 / 구간 거래량, up_ratio(상승일 비율)보다
                 크기까지 반영하는 정규화 지표 — 리뷰가 제시한 계산식 채택)
      가격추세 15 = MA20 상회(절대) 5 + MA20 상승(절대) 4 + MA60 상회(절대) 3
                  + MA20>MA60 정배열(절대) 3
      유동성 10 = 20일 평균 거래대금 백분위 10 (결측 시 0점 — 중립 가점 금지, 리뷰 지적)
      과열 페널티 = ATR·MA20 이격 기반, 과열 시 -5(그 외 0) — calc_risk() 참고

    OBV endpoint 다이버전스(구간 양끝 2점 비교)는 더 이상 배점에 반영하지
    않는다(P2-1) — 구간 양끝값에만 의존해 구조 확인이 없는 판정이 총점에
    직접 영향을 주는 문제가 있었다는 리뷰 지적을 반영해, Pivot 다이버전스와
    동일하게 참고용 표시 전용으로 전환했다. 축소분(기존 ±5)은
    normalized_slope 백분위 배점(15→20)으로 재배분해 OBV 합계 35점을 유지한다.

    백분위 모집단은 해당 지표의 data_status == SUCCESS인 종목으로 제한한다
    (P1-1) — PARTIAL/FAILED 종목의 유효 수치가 남아 있으면 그 값이 정상
    종목의 상대 순위에까지 영향을 준다는 리뷰 지적 반영. CVD 계열(정규화
    Delta·모멘텀·bull_ratio)은 cvd.data_status, OBV 계열은 obv.data_status,
    유동성은 liquidity.data_status 기준으로 각각 독립 필터링한다. 제외된
    종목은 해당 항목 백분위가 None(→ 0점)이 되며, 대개 Gate에서
    DATA_INSUFFICIENT로 별도 분류된다.

    종목이 1개뿐인 경우(detail) 백분위 항목은 0.5(중립)로 처리되어
    상대비교가 불가능함을 반영한다. 이 총점은 스캔 유니버스 내 "상대" 강도일
    뿐이며, 절대 신호 기준 충족 여부는 별도로 _compute_gate()가 판정한다
    (Absolute Gate — 리뷰 반영). 총점이 높아도 Gate 미통과(WATCH)일 수 있다.
    Pivot 기반 다이버전스(OBV·Price-CVD)는 아직 배점에 반영하지 않는다 —
    "구현만으로 자동 증액하지 말고 검증 후 배점을 정한다"는 리뷰 지적에 따라
    현재는 참고용 표시 전용이다.
    """
    def _only_success(getter, status_getter):
        return {r["code"]: getter(r) for r in results if status_getter(r) == "SUCCESS"}

    cvd_ok = lambda r: r["cvd"].get("data_status")
    reg_ok = lambda r: r.get("cvd_regular", r["cvd"]).get("data_status")
    obv_ok = lambda r: r["obv"].get("data_status")
    liq_ok = lambda r: r["liquidity"].get("data_status")

    nd_map  = _only_success(lambda r: r["cvd"].get("normalized_delta"),    cvd_ok)
    nm_map  = _only_success(lambda r: r["cvd"].get("normalized_momentum"), cvd_ok)
    # 지속성(매수우위일 비율)은 기준 세션과 무관하게 항상 정규장 완료 거래일
    # 기준값을 쓴다 — 애프터마켓은 2026-09-14 시행이라 완료 세션 이력이
    # 며칠뿐이어서 "여러 날 지속성"의 모집단으로 쓸 수 없다(리뷰 P1-2).
    br_map  = _only_success(
        lambda r: r.get("cvd_regular", r["cvd"]).get("bull_ratio_for_rank"), reg_ok)
    ns_map  = _only_success(lambda r: r["obv"].get("normalized_slope"),    obv_ok)
    liq_map = _only_success(lambda r: r["liquidity"].get("value"),         liq_ok)

    # 백분위 조회 시 모집단에서 빠진 종목도 키는 존재해야 하므로(값 None) 보강
    for code_map in (nd_map, nm_map, br_map, ns_map, liq_map):
        for r in results:
            code_map.setdefault(r["code"], None)

    nd_pct  = _percentile_ranks(nd_map)
    nm_pct  = _percentile_ranks(nm_map)
    br_pct  = _percentile_ranks(br_map)
    ns_pct  = _percentile_ranks(ns_map)
    liq_pct = _percentile_ranks(liq_map)

    for r in results:
        code = r["code"]
        obv, price, risk = r["obv"], r["price"], r.get("risk", {})

        cvd_pts = 0.0
        if nd_pct[code] is not None: cvd_pts += nd_pct[code] * 20
        if nm_pct[code] is not None: cvd_pts += nm_pct[code] * 10
        if br_pct[code] is not None: cvd_pts += br_pct[code] * 10

        # OBV 추세 항목은 up_ratio(상승일 비율, 방향만 반영) 대신
        # normalized_slope(거래량으로 정규화한 기울기, 크기까지 반영)를
        # 사용한다 — CVD의 normalized_delta와 같은 방식이라 일관성도 있다
        # (리뷰가 제시한 계산식을 채택, up_ratio는 계속 별도로 노출/표시).
        # endpoint 다이버전스(divergence)는 배점에서 제외하고 표시 전용으로
        # 전환했다(P2-1) — 축소분은 normalized_slope 배점(15→20)에 반영.
        obv_pts = 0.0
        if obv.get("obv_ma20") is not None:
            obv_pts += 15 if obv["obv"] > obv["obv_ma20"] else 0
        if ns_pct[code] is not None:
            obv_pts += ns_pct[code] * 20

        # 가격추세 15 = MA20 상회 5 + MA20 상승 4 + MA60 상회 3 + MA20>MA60(정배열) 3
        price_pts = 0.0
        if price.get("above_ma20") is True:      price_pts += 5
        if price.get("ma20_rising") is True:     price_pts += 4
        if price.get("above_ma60") is True:      price_pts += 3
        if price.get("ma20_above_ma60") is True: price_pts += 3

        # 유동성 결측은 중립(5점)이 아니라 0점 — 결측 종목이 실측 저유동성
        # 종목보다 유리해지지 않도록 한다(리뷰 지적 반영)
        liq_pts = (liq_pct[code] * 10) if liq_pct[code] is not None else 0.0

        # 과열 페널티(ATR·MA20 이격 기반) — 검증 전 초기 설계값(의견)
        risk_penalty = risk.get("overheat_penalty", 0.0) or 0.0

        total = cvd_pts + obv_pts + price_pts + liq_pts + risk_penalty

        r["cvd_points"]           = round(cvd_pts, 1)
        r["obv_points"]           = round(obv_pts, 1)
        r["price_points"]         = round(price_pts, 1)
        r["liquidity_points"]     = round(liq_pts, 1)
        r["liquidity_percentile"] = round(liq_pct[code] * 100, 1) if liq_pct[code] is not None else None
        r["risk_penalty"]         = round(risk_penalty, 1)
        r["total"] = round(max(0.0, min(100.0, total)), 1)

        gate = _compute_gate(r)
        r["gate_passed"]  = gate["gate_passed"]
        r["gate_reasons"] = gate["gate_reasons"]
        r["status"]       = gate["status"]


_STATUS_ORDER = {"BUY_CANDIDATE": 0, "WATCH": 1, "DATA_INSUFFICIENT": 2}
_STATUS_LABEL = {"BUY_CANDIDATE": "🟩후보", "WATCH": "🟨관찰", "DATA_INSUFFICIENT": "⬜부족"}
_STATUS_DESC = {
    "BUY_CANDIDATE":     "🟩 BUY_CANDIDATE (절대조건 모두 충족)",
    "WATCH":             "🟨 WATCH (데이터는 충분하나 절대조건 일부 미충족)",
    "DATA_INSUFFICIENT": "⬜ DATA_INSUFFICIENT (CVD/OBV/가격추세 중 품질 미충족)",
}


def _parse_as_of_str(s) -> Optional[datetime]:
    """결과 dict에 문자열로 담긴 기준시각을 datetime으로 되돌린다."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(str(s), fmt)
        except (TypeError, ValueError):
            continue
    return None


def _compute_gate(r: dict) -> dict:
    """
    상대 백분위(총점)와 별개로, 절대 신호 기준(Absolute Gate)을 모두
    충족하는지 판정한다. 총점이 높아도 CVD가 하락(🔴)이거나 가격이 MA20
    아래인 종목은 여기서 걸러진다.

    - DATA_INSUFFICIENT: 기준 세션 CVD/OBV/가격추세 중 하나 이상 품질 미충족
    - BUY_CANDIDATE:     데이터 품질 충족 + 아래 절대조건 모두 충족
    - WATCH:             데이터는 충분하나 절대조건 중 일부 미충족

    공통 절대조건(모두 검증 전 초기 설계값 — 의견):
      기준 세션 normalized_delta > 0, normalized_momentum > 0,
      정규장 완료 거래일 기준 매수우위일 비율 >= 50%(완료 거래일 2일 이상),
      종가 > MA20, OBV normalized_slope > 0 이고 OBV > OBV MA20,
      유동성(20일 평균 거래대금 근사치) 데이터 존재.

    --session after(애프터마켓 기준)일 때 추가되는 조건(리뷰 P1-2 —
    "정규장 추세 조건 + 애프터마켓의 최신 데이터·유동성·방향 조건"):
      애프터마켓 데이터 품질 SUCCESS, 해당 세션 최근 거래일 거래대금 > 0,
      세션이 열려 있는 동안에는 마지막 봉이 최근(max(2×분봉주기, 10분) 이내).
      수치 임계값은 실측·검증 전이므로 부호(>0)와 데이터 최신성만 본다.
    """
    cvd, obv, price = r["cvd"], r["obv"], r["price"]
    cvd_regular = r.get("cvd_regular", cvd)
    session = r.get("rank_session", _SESSION_REGULAR)
    after_mode = (session == _SESSION_AFTER)

    data_ok = (cvd.get("data_status") == "SUCCESS" and
               obv.get("data_status") == "SUCCESS" and
               price.get("data_status") == "SUCCESS")
    if after_mode:
        # 정규장 추세 조건을 함께 쓰므로 정규장 데이터 품질도 필요하다
        data_ok = data_ok and cvd_regular.get("data_status") == "SUCCESS"

    nd = cvd.get("normalized_delta")
    nm = cvd.get("normalized_momentum")
    # 완료 거래일 기준 비율만 사용한다 — 완료 거래일이 부족하면 당일 장중
    # 값으로 폴백하지 않고 조건 미충족으로 본다(리뷰 P2-1).
    br = cvd_regular.get("bull_ratio_for_rank")

    obv_slope = obv.get("normalized_slope")
    obv_ma20  = obv.get("obv_ma20")
    obv_cur   = obv.get("obv")
    obv_ok = (obv_slope is not None and obv_slope > 0 and
              obv_ma20 is not None and obv_cur is not None and
              obv_cur > obv_ma20)

    reasons = []
    if not data_ok:
        reasons.append("데이터 품질 미충족(CVD/OBV/가격추세 중 PARTIAL·FAILED)")
    if nd is None or nd <= 0:
        reasons.append(f"CVD 정규화 Delta ≤ 0({_SESSION_LABEL.get(session, session)})")
    if nm is None or nm <= 0:
        reasons.append(f"CVD 정규화 모멘텀 ≤ 0({_SESSION_LABEL.get(session, session)})")
    if cvd.get("momentum_status") in ("WARMING_UP", "NO_DATA"):
        reasons.append(f"모멘텀 구간 부족({cvd.get('momentum_status')})")
    if br is None:
        reasons.append(f"정규장 완료 거래일 부족(<{_MIN_COMPLETED_DAYS_FOR_RATIO}일)")
    elif br < 0.50:
        reasons.append("정규장 매수우위일 비율 < 50%")
    if price.get("above_ma20") is not True:
        reasons.append("종가가 MA20 이하")
    if not obv_ok:
        reasons.append("OBV 상승 조건 미충족(정규화 기울기>0 & OBV>MA20)")
    if r.get("liquidity", {}).get("value") is None:
        reasons.append("유동성 데이터 없음")

    if after_mode:
        if not cvd.get("latest_day_traded_value"):
            reasons.append("애프터마켓 거래대금 없음(대상 제외 또는 무체결)")
        as_of_dt = _parse_as_of_str(r.get("as_of"))
        if as_of_dt is not None and session_is_open(as_of_dt, _SESSION_AFTER):
            age = cvd.get("last_bar_age_min")
            fresh_limit = max(2 * int(cvd.get("period") or 5), 10)
            if age is None or age > fresh_limit:
                reasons.append(f"애프터마켓 데이터 최신성 부족"
                               f"(마지막 봉 {age}분 전 > 기준 {fresh_limit}분)")

    if not data_ok:
        status = "DATA_INSUFFICIENT"
    elif not reasons:
        status = "BUY_CANDIDATE"
    else:
        status = "WATCH"

    return {"gate_passed": status == "BUY_CANDIDATE", "gate_reasons": reasons, "status": status}


# ─────────────────────────────────────────────────────────────
# 스캔 출력
# ─────────────────────────────────────────────────────────────

def _after_line(cvd_after: dict) -> Optional[str]:
    """
    애프터마켓 한 줄 요약. 정규장 기준 스캔에서도 항상 함께 보여준다
    (리뷰 P1-2 — 저녁 흐름이 정규장 점수에 묻히지 않게 별도 표기).
    """
    if not cvd_after:
        return None
    st = cvd_after.get("data_status")
    if st == "NOT_APPLICABLE":
        return None
    if st in ("NO_DATA", "FAILED"):
        return ("🌙 애프터마켓: 데이터 없음 — 대상 제외/무체결/API 미제공을 "
                "이 정보만으로는 구분할 수 없음")
    nd = cvd_after.get("normalized_delta")
    nm = cvd_after.get("normalized_momentum")
    nd_s = f"{nd:+.4f}" if nd is not None else "-"
    nm_s = f"{nm:+.4f}" if nm is not None else "-"
    tv = cvd_after.get("latest_day_traded_value")
    parts = [f"🌙 애프터마켓[{st}]: Δ {nd_s}",
             f"모멘텀 {nm_s}({cvd_after.get('momentum_status')},"
             f" {cvd_after.get('momentum_minutes_actual')}분)"]
    if tv:
        parts.append(f"최근일 거래대금(추정) {_fmt(tv)}원")
    if cvd_after.get("last_bar_at"):
        parts.append(f"마지막 봉 {cvd_after['last_bar_at']}")
    return " · ".join(parts)


def _print_results(results: list, top: int, label: str, period: int, bdays: int,
                    as_of: Optional[datetime] = None,
                    rank_session: str = _SESSION_REGULAR,
                    daily_scope: str = "auto"):
    """스캔 결과 출력. 정렬은 Absolute Gate 상태 → 총점 순."""
    results.sort(key=lambda x: (_STATUS_ORDER.get(x.get("status"), 1), -x["total"]))
    # 데이터 수집 기준 시각(as_of)을 표시한다 — 스캔은 종목이 많으면 수 분
    # 걸릴 수 있어, 출력 시점의 datetime.now()는 실제 데이터 기준시각과
    # 어긋날 수 있다.
    now = (as_of or datetime.now()).strftime("%Y-%m-%d %H:%M")

    n_buy   = sum(1 for r in results if r.get("status") == "BUY_CANDIDATE")
    n_watch = sum(1 for r in results if r.get("status") == "WATCH")
    n_data  = sum(1 for r in results if r.get("status") == "DATA_INSUFFICIENT")

    n_after_data = sum(1 for r in results
                       if r.get("cvd_after", {}).get("data_status")
                       not in ("NOT_APPLICABLE", "NO_DATA", "FAILED", None))
    n_other_bars = sum(r.get("bars_by_session", {}).get(_SESSION_OTHER, 0)
                       for r in results)

    print(f"\n{'='*70}")
    print(f"  {label} CVD/OBV 스캐너 V{_SCANNER_VERSION}  |  {now}")
    print(f"  분봉: {period}분  |  기간: 최근 {bdays}영업일(요청) 한 번에 수집")
    print(f"  점수·Gate 기준 세션: {_SESSION_LABEL.get(rank_session, rank_session)}"
          f"  |  일봉 완료 기준: --daily-scope {daily_scope}")
    print(f"  분석 종목: {len(results)}개  |  상위 {top}개 출력")
    print(f"  Gate 판정: 🟩후보 {n_buy} · 🟨관찰 {n_watch} · ⬜부족 {n_data}")
    print(f"  애프터마켓 분봉 확보: {n_after_data}/{len(results)}종목"
          f"  |  세션 밖(15:30~16:00 등) 봉: {n_other_bars}개")
    print(f"{'='*70}")

    print(f"\n  [범례]")
    print(f"  CVD: 정규화 분봉 매수/매도 강도(-1~1, 체결데이터 아닌 캔들 추정치)")
    print(f"       모멘텀은 최근 {_MOMENTUM_MINUTES}분 목표 — 실제 사용 구간과 상태를 함께 표기")
    print(f"       (SUCCESS/WARMING_UP=구간부족/RESOLUTION_LIMITED=분봉주기가 더 김)")
    print(f"  세션: 정규장 09:00~15:30 · 애프터마켓 16:00~20:00(2026-09-14 시행)")
    print(f"        15:30~16:00 등 세션 밖 체결은 어느 쪽에도 합산하지 않음")
    print(f"  OBV: 완료 일봉 누적 매수/매도 추세(완료 기준은 --daily-scope)")
    print(f"       ⚠ 키움 일봉 종가·고저·거래량은 애프터마켓까지 반영한 값이다(실측).")
    print(f"       제도상 공식 종가(15:30 확정)와 다를 수 있으므로 MA·OBV·ATR·과열")
    print(f"       판정도 '마지막 체결가' 기준으로 읽어야 한다.")
    print(f"  총점: 스캔 유니버스 내 백분위 기반 100점 합성 점수(상대 강도)")
    print(f"        (CVD 40 + OBV 35 + 가격추세 15 + 유동성 10, 잠정 가중치·의견)")
    print(f"        백분위 모집단은 해당 지표 data_status=SUCCESS 종목만 포함")
    print(f"  상태: Absolute Gate 판정(총점과 별개, 절대 신호 기준) — 정렬은 상태→총점 순")
    print(f"        🟩후보=BUY_CANDIDATE(절대조건 모두 충족)")
    print(f"        🟨관찰=WATCH(절대조건 일부 미충족) · ⬜부족=DATA_INSUFFICIENT(데이터 품질 미충족)")
    print(f"  🔎 다이버전스(endpoint·Pivot 공통): 아직 배점 미반영(참고용)")
    print(f"  ⚠과열: 종가가 MA20 대비 +15%초과 이격 시 총점 -5(검증 전 초기값)")
    print()

    H = (f"  {'순':>3}  {'종목명':12}  {'코드':6}  "
         f"{'CVD신호':^8}  {'OBV신호':^8}  "
         f"{'매수%':>5}  {'bull%':>5}  {'상태':^6}  {'총점':>5}")
    print(H)
    print(f"  {'─'*3}  {'─'*12}  {'─'*6}  {'─'*8}  {'─'*8}  "
          f"{'─'*5}  {'─'*5}  {'─'*6}  {'─'*5}")

    for rank, r in enumerate(results[:top], 1):
        cvd    = r["cvd"]
        obv    = r["obv"]
        name   = _name6(r["name"])
        div    = obv.get("divergence")
        status = r.get("status", "WATCH")

        if cvd.get("cvd") is not None and cvd.get("cvd_ma") is not None:
            cvd_sym = "🟢CVD↑" if cvd["cvd"] > cvd["cvd_ma"] else "🔴CVD↓"
        else:
            cvd_sym = "⬜ -  "

        if obv.get("obv") is not None and obv.get("obv_ma20") is not None:
            obv_sym = "🟢OBV↑" if obv["obv"] > obv["obv_ma20"] else "🔴OBV↓"
        else:
            obv_sym = "⬜ -  "

        bratio_s = f"{cvd['buy_ratio']:.1f}%" if cvd.get("buy_ratio") is not None else "  - "
        # 표시용 bull%도 Gate·점수와 같은 정의(정규장 완료 거래일)를 쓴다
        bull_src = r.get("cvd_regular", cvd)
        bull_s   = _pct(bull_src.get("bull_ratio_for_rank"), 0)
        status_s = _STATUS_LABEL.get(status, status)

        print(f"  {rank:>3}위  {name}  {r['code']:6}  "
              f"{cvd_sym:^8}  {obv_sym:^8}  "
              f"{bratio_s:>5}  {bull_s:>5}  {status_s:^6}  {r['total']:>5.1f}점")

        after_s = _after_line(r.get("cvd_after"))
        if after_s:
            print(f"        {after_s}")

        if div:
            icon = "🟢" if div == "강세" else "🔴"
            print(f"        ⚡ OBV {div} 다이버전스(참고, 미배점) {icon}  "
                  f"(최근 {obv.get('lookback', bdays)}거래일: "
                  f"가격{'↓' if div=='강세' else '↑'} + OBV{'↑' if div=='강세' else '↓'})")

        piv_obv = obv.get("pivot_divergence")
        if piv_obv:
            icon = "🟢" if piv_obv == "강세" else "🔴"
            print(f"        🔎 OBV Pivot 다이버전스(참고, 미배점) {icon}  "
                  f"{piv_obv} · 확인 {obv.get('pivot_divergence_lag', '?')}거래일 전")

        piv_cvd = cvd.get("price_cvd_divergence")
        if piv_cvd:
            icon = "🟢" if piv_cvd == "강세" else "🔴"
            print(f"        🔎 Price-CVD Pivot 다이버전스(참고, 미배점) {icon}  "
                  f"{piv_cvd} · 확인 {cvd.get('price_cvd_divergence_lag', '?')}거래일 전")

        if status != "BUY_CANDIDATE" and r.get("gate_reasons"):
            print(f"        · Gate 미통과 사유: {', '.join(r['gate_reasons'])}")

        notes = []
        if cvd.get("data_status") not in ("SUCCESS", None):
            reasons = cvd.get("status_reasons") or []
            notes.append(f"{cvd.get('session_label','')} CVD {cvd.get('data_status')}"
                         f"({', '.join(reasons) if reasons else '사유 불명'})")
        if obv.get("data_status") in ("PARTIAL", "FAILED"):
            notes.append("OBV 데이터부족(MA20 미충족)")
        if r.get("risk", {}).get("overheated"):
            atr_pct = r["risk"].get("atr_pct")
            atr_s = f", ATR {atr_pct:.1f}%" if atr_pct is not None else ""
            notes.append(f"⚠과열(MA20 대비 +15%↑{atr_s}, 총점 -5)")
        if notes:
            print(f"        ⚠ {', '.join(notes)}")

    print(f"\n  {'─'*60}")
    if len(results) > top:
        print(f"  전체 {len(results)}개 분석 완료  |  하위 {len(results)-top}개 생략")


# ─────────────────────────────────────────────────────────────
# 결과 스냅샷 저장(리뷰 추가 P2-3)
# ─────────────────────────────────────────────────────────────
#
# 콘솔 출력만으로는 특정 시점의 점수 변화가 데이터 변화 때문인지 가중치
# 변경 때문인지 추적하기 어렵다는 지적을 반영해, 스캔 결과(원시 지표·점수·
# Gate 사유·as_of·설정)를 JSON으로 남긴다. 원본 분봉/일봉 자체는 용량 문제로
# 저장하지 않는다(한계로 명시) — 인증 정보는 애초에 결과 dict에 없으므로
# 별도 마스킹 없이 그대로 저장해도 안전하다.

def _json_default(o):
    if isinstance(o, datetime):
        return o.isoformat()
    return str(o)


def _session_config_snapshot() -> dict:
    """세션·완성도 관련 설정을 그대로 기록한다(재현성 — 리뷰 P2-3)."""
    return {
        "sessions": {k: {"start": v[0], "end": v[1], "minutes": v[2],
                         "continuous_minutes": v[3]}
                     for k, v in _SESSION_WINDOWS.items()},
        "after_market_start_date": _AFTER_MARKET_START_DATE,
        "session_complete_grace_min": _SESSION_COMPLETE_GRACE_MIN,
        "bar_timestamp_is_start": _BAR_TIMESTAMP_IS_START,
        "min_day_completeness": _MIN_DAY_COMPLETENESS,
        "max_stale_calendar_days": _MAX_STALE_CALENDAR_DAYS,
        "min_completed_days_for_ratio": _MIN_COMPLETED_DAYS_FOR_RATIO,
        "momentum_minutes": _MOMENTUM_MINUTES,
        "after_min_bars": _AFTER_MIN_BARS,
    }


def _last_input_bar_at(results: list) -> Optional[str]:
    """실제로 사용된 마지막 입력 봉 시각 — 기준시각(as_of)과 구분해 기록한다."""
    stamps = []
    for r in results:
        for key in ("cvd_regular", "cvd_after", "cvd"):
            v = (r.get(key) or {}).get("last_bar_at")
            if v:
                stamps.append(v)
    return max(stamps) if stamps else None


def _unique_snapshot_path(directory: str, fname: str) -> str:
    """같은 이름이 있으면 덮어쓰지 않고 -2, -3 … 을 붙인다(리뷰 P2-3)."""
    base, ext = os.path.splitext(fname)
    path = os.path.join(directory, fname)
    i = 2
    while os.path.exists(path):
        path = os.path.join(directory, f"{base}-{i}{ext}")
        i += 1
    return path


def _save_scan_snapshot(results: list, index_key: str, label: str,
                         period: int, bdays: int, as_of: datetime,
                         rank_session: str = _SESSION_REGULAR,
                         daily_scope: str = "auto",
                         run_id: str = "") -> str:
    """
    스캔 결과(원시 지표·점수·Gate 사유·as_of·설정)를 JSON으로 남긴다.

    파일명에 초와 실행 ID를 포함하고, 설정 해시·코드 버전·거래제도 버전·
    실제 마지막 입력 봉 시각을 함께 저장한다(리뷰 P2-3 — 기존엔 분 단위
    파일명 + "w" 모드라 같은 분 재실행이나 다른 period/days 실행이
    서로를 덮어쓸 수 있었다). 원시 분봉/일봉 자체는 용량 문제로 저장하지
    않는다(한계로 명시).
    """
    os.makedirs(_SNAPSHOT_DIR, exist_ok=True)
    run_id = run_id or uuid.uuid4().hex[:8]
    config = {
        "period": period, "bdays": bdays,
        "rank_session": rank_session,
        "daily_scope": daily_scope,
        "index": index_key,
        "session_config": _session_config_snapshot(),
    }
    config_hash = hashlib.sha256(
        json.dumps(config, sort_keys=True, ensure_ascii=False,
                   default=_json_default).encode("utf-8")).hexdigest()[:12]

    fname = f"cvd-obv-{index_key}-{as_of:%Y%m%d_%H%M%S}-{run_id}.json"
    path = _unique_snapshot_path(_SNAPSHOT_DIR, fname)
    payload = {
        "scanner_version":     _SCANNER_VERSION,
        "market_rule_version": _MARKET_RULE_VERSION,
        "run_id":              run_id,
        "as_of":               as_of.isoformat(),
        "saved_at":            datetime.now().isoformat(),
        "last_input_bar_at":   _last_input_bar_at(results),
        "label": label,
        "config": config,
        "config_hash": config_hash,
        "count": len(results),
        "results": results,
        "note": ("원시 분봉/일봉 원본은 저장하지 않음(용량). 재계산 검증 시 참고용. "
                 "키움 API가 애프터마켓 체결을 포함하는지는 미확인이며, 세션은 "
                 "봉 타임스탬프로 분류한 값이다."),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=_json_default)
    print(f"  💾 스냅샷 저장: {path}")
    return path


# ─────────────────────────────────────────────────────────────
# 명령 구현
# ─────────────────────────────────────────────────────────────

def cmd_scan(index_name: str, top: int = 10,
             period: int = 5, bdays: int = 5,
             rank_mode: str = "separate", save_json: bool = False,
             session: str = _SESSION_REGULAR,
             daily_scope: str = "auto",
             as_of: Optional[datetime] = None):
    """
    코스피200 또는 코스닥150 전종목 스캔.

    rank_mode: --index both일 때만 의미가 있다.
      separate(기본) — 지수별로 각각 백분위 모집단을 계산.
      combined       — 코스피200+코스닥150을 하나의 모집단으로 통합.

    session: 점수·순위·Gate의 기준 세션(regular/after). 애프터마켓 순위는
      같은 세션 데이터를 확보한 종목끼리만 비교된다 — 백분위 모집단이
      data_status==SUCCESS 종목으로 제한되기 때문이다(리뷰 P1-2).
    daily_scope: 일봉 완료 판정 기준(auto/regular/extended).
    as_of: 기준시각 고정(회귀 검증용). 미지정 시 실행 시점.
    """
    token = _get_token()
    if not token:
        print("\n  ❌ 키움 API 미연결")
        print("  .env 파일에 아래 항목을 설정하세요 (.env.example 참고):")
        print("    KIWOOM_APP_KEY=앱키")
        print("    KIWOOM_APP_SECRET=앱시크릿")
        print("  키움 REST API 신청: https://openapi.kiwoom.com")
        return

    as_of = as_of or datetime.now()
    run_id = uuid.uuid4().hex[:8]
    indices = (["kospi200", "kosdaq150"] if index_name == "both"
               else [index_name])
    _INDEX_LABEL = {"kospi200": "코스피200", "kosdaq150": "코스닥150"}

    combined_results = []

    for idx in indices:
        stocks = load_index(idx)          # [{"code":..., "name":...}, ...]
        label  = _INDEX_LABEL.get(idx, idx)
        total  = len(stocks)

        if not stocks:
            continue

        print(f"\n  {label} 스캔 시작: {total}개 종목  (기준시각 {as_of:%Y-%m-%d %H:%M})")
        print(f"  분봉({period}분) 최근 {bdays}영업일 한 번에 수집 중... "
              f"[기준 세션 {_SESSION_LABEL.get(session, session)}, 일봉 완료기준 {daily_scope}]")

        results = []
        for i, s in enumerate(stocks, 1):
            code = s["code"]
            name = s["name"]
            print(f"  [{i:3}/{total}] {code} {name[:8]} ...", end="\r")
            r = analyze(code, token, period=period, bdays=bdays, name=name,
                        as_of=as_of, daily_scope=daily_scope, rank_session=session)
            if r:
                r["index"] = idx
                results.append(r)
            # 호출 간격은 중앙 _LIMITER가 통제한다(여기서 추가 sleep 불필요)

        if index_name == "both" and rank_mode == "combined":
            combined_results.extend(results)
            continue

        _finalize_scores(results)
        _print_results(results, top, label, period, bdays, as_of=as_of,
                       rank_session=session, daily_scope=daily_scope)
        if save_json:
            _save_scan_snapshot(results, idx, label, period, bdays, as_of,
                                rank_session=session, daily_scope=daily_scope,
                                run_id=run_id)

    if index_name == "both" and rank_mode == "combined" and combined_results:
        _finalize_scores(combined_results)
        _print_results(combined_results, top, "코스피200+코스닥150(통합)",
                       period, bdays, as_of=as_of,
                       rank_session=session, daily_scope=daily_scope)
        if save_json:
            _save_scan_snapshot(combined_results, "both_combined",
                                 "코스피200+코스닥150(통합)", period, bdays, as_of,
                                 rank_session=session, daily_scope=daily_scope,
                                 run_id=run_id)


def cmd_detail(code: str, token: str, period: int = 5, bdays: int = 5,
               save_json: bool = False, session: str = _SESSION_REGULAR,
               daily_scope: str = "auto",
               as_of: Optional[datetime] = None):
    """
    단일 종목 상세 분석.

    비교 모집단이 없어 상대 백분위 총점(_finalize_scores)은 의미가 없으므로
    (유효값 1개짜리 백분위는 항상 0.5) 여기서는 계산하지 않고, 절대 기준인
    Absolute Gate만 판정해 표시한다. 정규장·애프터마켓 지표는 항상 함께
    출력하고, Gate 기준 세션만 --session으로 고른다.
    """
    as_of = as_of or datetime.now()
    print(f"\n  [{code}] 분봉({period}분) 최근 {bdays}영업일 데이터 수집 중... (기준시각 {as_of:%Y-%m-%d %H:%M})")
    r = analyze(code, token, period=period, bdays=bdays, as_of=as_of,
                daily_scope=daily_scope, rank_session=session)
    if not r:
        print(f"  ❌ 데이터 수집 실패")
        return

    gate = _compute_gate(r)
    r["gate_passed"]  = gate["gate_passed"]
    r["gate_reasons"] = gate["gate_reasons"]
    r["status"]       = gate["status"]

    cvd, obv, price, risk = r["cvd"], r["obv"], r["price"], r.get("risk", {})
    liq = r.get("liquidity", {})
    div = obv.get("divergence")

    print(f"\n{'='*62}")
    print(f"  CVD/OBV 상세 분석: {r['name']} ({code})")
    print(f"  분봉 {period}분  |  최근 {bdays}영업일  |  {r['min_bars']}봉 수집")
    print(f"{'='*62}")

    by_sess = r.get("bars_by_session", {})
    print(f"\n  [CVD — {cvd.get('session_label','정규장')} · {period}분봉 "
          f"최근 {bdays}영업일(요청) 기준, 정규화 추정치]")
    print(f"  수집 봉 수:      {r['min_bars']}봉  ({r['days']}일 포함)  "
          f"[정규장 {by_sess.get(_SESSION_REGULAR,0)} · "
          f"애프터마켓 {by_sess.get(_SESSION_AFTER,0)} · "
          f"세션밖 {by_sess.get(_SESSION_OTHER,0)}]")
    print(f"  기대 봉 수/세션: {cvd.get('expected_bars_per_day','-')}봉  "
          f"(일봉 완료기준 --daily-scope {r.get('daily_scope','auto')})")
    print(f"  Estimated CVD:   {_fmt(cvd.get('estimated_cvd'))}  (캔들 추정, 체결데이터 아님)")
    print(f"  정규화 Delta:    {cvd['normalized_delta']:.4f}" if cvd.get('normalized_delta') is not None else "  정규화 Delta:    -")
    if cvd.get('normalized_momentum') is not None:
        print(f"  정규화 모멘텀:   {cvd['normalized_momentum']:.4f}  "
              f"(목표 {cvd.get('momentum_minutes', _MOMENTUM_MINUTES)}분 / 실제 "
              f"{cvd.get('momentum_minutes_actual')}분, {cvd.get('momentum_bars')}봉, "
              f"{cvd.get('momentum_status')})")
    else:
        print(f"  정규화 모멘텀:   -  ({cvd.get('momentum_status')})")
    print(f"  추정 매수 비율:  {cvd['buy_ratio']:.1f}%" if cvd.get('buy_ratio') is not None else "  추정 매수 비율:  -")
    if cvd.get('bull_ratio') is not None:
        print(f"  매수우위일 비율(요청구간 전체): {cvd['bull_ratio']*100:.0f}%  ({cvd.get('bull_days',0)}/{cvd.get('total_days',0)}일)")
    else:
        print(f"  매수우위일 비율(요청구간 전체): -")
    if cvd.get('completed_bull_ratio') is not None:
        usable = cvd.get('bull_ratio_for_rank') is not None
        print(f"  매수우위일 비율(완료 세션만): {cvd['completed_bull_ratio']*100:.0f}%  "
              f"({cvd.get('completed_total_days',0)}일, "
              f"{'점수·Gate 사용' if usable else f'완료일 부족으로 점수·Gate 미사용(<{_MIN_COMPLETED_DAYS_FOR_RATIO}일)'})")
    if cvd.get('intraday_delta') is not None:
        print(f"  당일 장중 Delta(참고, 완료일 집계 제외): {_fmt(cvd['intraday_delta'])}")
    print(f"  신호:            {cvd.get('signal','-')}")
    if cvd.get("completeness") is not None:
        print(f"  분봉 완성도:     {cvd['completeness']*100:.0f}%  (완료된 거래일만, 당일 장중 제외)")
    if cvd.get("data_status") != "SUCCESS":
        reason = cvd.get("status_reasons") or []
        print(f"  ⚠ 데이터 상태 {cvd.get('data_status')}"
              f"({', '.join(reason) if reason else '사유 불명'})")
    if cvd.get("absent_days"):
        print(f"  ⚠ 분봉이 전혀 없는 거래일: {', '.join(cvd['absent_days'])}")
    if cvd.get("over_count_days"):
        print(f"  ⚠ 기대 봉 수를 초과한 거래일(세션 혼입 의심): "
              f"{', '.join(cvd['over_count_days'])}")

    # ── 애프터마켓(항상 별도 표시) ──────────────────────────
    aft = r.get("cvd_after", {})
    print(f"\n  [애프터마켓 — 16:00~20:00, {_AFTER_MARKET_START_DATE} 시행]")
    if aft.get("data_status") == "NOT_APPLICABLE":
        print(f"  해당 기간에 애프터마켓 거래일이 없습니다(시행일 이전).")
    elif aft.get("data_status") in ("NO_DATA", "FAILED", None):
        print(f"  수집된 애프터마켓 분봉 없음 — 아래 중 무엇인지 이 데이터만으로는")
        print(f"  구분할 수 없습니다: (1) 종목이 애프터마켓 거래 대상 제외")
        print(f"  (ETF·ETN·관리·투자경고/위험·정리매매·초저유동·당일 정규장 미거래),")
        print(f"  (2) 대상이지만 무체결, (3) 키움 차트 API가 해당 세션을 제공하지 않음.")
    else:
        nd_a = aft.get("normalized_delta")
        nm_a = aft.get("normalized_momentum")
        print(f"  데이터 상태:   {aft.get('data_status')}"
              f"{'(' + ', '.join(aft.get('status_reasons') or []) + ')' if aft.get('status_reasons') else ''}")
        print(f"  수집 봉/세션일: {aft.get('bars_used',0)}봉 / {aft.get('total_days',0)}일"
              f"  (완료 세션 {aft.get('completed_total_days',0)}일)")
        print(f"  정규화 Delta:   {nd_a:.4f}" if nd_a is not None else "  정규화 Delta:   -")
        if nm_a is not None:
            print(f"  정규화 모멘텀:  {nm_a:.4f}  (실제 {aft.get('momentum_minutes_actual')}분, "
                  f"{aft.get('momentum_status')})")
        print(f"  최근일 거래대금(추정): {_fmt(aft.get('latest_day_traded_value'))}원")
        print(f"  마지막 봉:      {aft.get('last_bar_at') or '-'}"
              f"  ({aft.get('last_bar_age_min')}분 전)")
        print(f"  ※ 애프터마켓 체결은 제도상 공식 종가(15:30 확정)와 다음 거래일")
        print(f"     기준가격을 바꾸지 않는다. 다만 키움 일봉의 종가·고저·거래량은")
        print(f"     애프터마켓을 반영한 '마지막 체결가' 기준이다 — 아래 [종가 정의 확인] 참고.")

    print(f"\n  [OBV — 일봉 기반, 최근 {obv.get('lookback', bdays)}거래일 기울기/다이버전스]")
    print(f"  현재 OBV:      {_fmt(obv.get('obv'))}")
    print(f"  OBV MA20:      {_fmt(obv.get('obv_ma20'))}")
    print(f"  기울기:        {_fmt(obv.get('slope'))}")
    print(f"  상승일 비율:   {_pct(obv.get('up_ratio'), 0)}")
    print(f"  신호:          {obv.get('signal','-')}")
    if div:
        icon = "🟢" if div == "강세" else "🔴"
        print(f"  ⚡ OBV {div} 다이버전스(참고, 미배점) {icon}")
        desc = ("가격 하락 + OBV 상승 → 저점 매집 가능성(의견)" if div == "강세"
                else "가격 상승 + OBV 하락 → 매수세 이탈 가능성(의견)")
        print(f"     {desc}")
    if obv.get("data_status") != "SUCCESS":
        print(f"  ⚠ 일봉 데이터가 20거래일 미만이라 OBV MA20을 계산하지 못했습니다.")
    ns = obv.get("normalized_slope")
    ns_s = f"{ns:.4f}" if ns is not None else "-"
    print(f"  정규화 기울기:  {ns_s}  (OBV 변화량/구간거래량 — up_ratio와 별개 지표)")
    piv_obv = obv.get("pivot_divergence")
    if piv_obv:
        icon = "🟢" if piv_obv == "강세" else "🔴"
        print(f"  🔎 OBV Pivot 다이버전스(참고, 미배점) {icon} {piv_obv}  "
              f"확인 {obv.get('pivot_divergence_lag','?')}거래일 전")

    cc = r.get("close_check")
    if cc:
        print(f"\n  [종가 정의 확인 — 일봉 종가는 애프터마켓 최종가]")
        print(f"  {cc['date']}: 정규장 마지막 분봉 종가 {cc['regular_close']:,.0f}"
              f"  vs  일봉 종가 {cc['daily_close']:,.0f}  ({cc['diff_pct']:+.2f}%)")
        if abs(cc["diff_pct"]) >= 0.01:
            print(f"  → 아래 MA·OBV·ATR·과열 판정은 모두 일봉(애프터마켓 포함) 종가 기준이다.")

    print(f"\n  [가격추세 — 일봉 재사용, 추가 API 호출 없음]")
    if price.get("above_ma20") is not None:
        print(f"  종가 vs MA20:  {'상회' if price['above_ma20'] else '하회'}  ({price['close_vs_ma20_pct']:+.2f}%)")
        print(f"  MA20 기울기:   {'상승' if price.get('ma20_rising') else ('하락' if price.get('ma20_rising') is False else '데이터부족')}")
    else:
        print(f"  가격추세:      데이터 부족(20거래일 미만)")
    if price.get("above_ma60") is not None:
        print(f"  종가 vs MA60:  {'상회' if price['above_ma60'] else '하회'}"
              f"  |  MA60 기울기: {'상승' if price.get('ma60_rising') else ('하락' if price.get('ma60_rising') is False else '데이터부족')}")
        print(f"  MA20 vs MA60:  {'정배열(MA20>MA60)' if price.get('ma20_above_ma60') else '역배열(MA20<MA60)'}")
    else:
        print(f"  MA60:          데이터 부족(60거래일 미만)")
    if price.get("above_ma120") is not None:
        print(f"  종가 vs MA120: {'상회' if price['above_ma120'] else '하회'}")
    else:
        print(f"  MA120:         데이터 부족(120거래일 미만)")

    piv_cvd = cvd.get("price_cvd_divergence")
    if piv_cvd:
        icon = "🟢" if piv_cvd == "강세" else "🔴"
        print(f"  🔎 Price-CVD Pivot 다이버전스(참고, 미배점) {icon} {piv_cvd}  "
              f"확인 {cvd.get('price_cvd_divergence_lag','?')}거래일 전")

    print(f"\n  [변동성·과열 — ATR14(Wilder), 검증 전 초기 임계값]")
    if risk.get("atr_pct") is not None:
        print(f"  ATR14:         {_fmt(risk.get('atr'))}  ({risk['atr_pct']:.2f}% of Close)")
        print(f"  과열 여부:     {'⚠ 과열 (MA20 대비 +15%↑, 총점 -5)' if risk.get('overheated') else '정상'}")
    else:
        print(f"  ATR14:         데이터 부족(15거래일 미만)")

    print(f"\n  [유동성 — 일봉 재사용, 추가 API 호출 없음]")
    if liq.get("value") is not None:
        partial_s = f" (요청 20일 중 {liq['days_used']}일만 확보)" if liq.get("data_status") == "PARTIAL" else ""
        print(f"  평균 거래대금(추정): {_fmt(liq['value'])}원  [{liq['days_used']}일 평균{partial_s}]")
    else:
        print(f"  평균 거래대금:  데이터 부족")

    print(f"\n  {'═'*42}")
    print(f"  총점(상대 백분위): N/A — 단일 종목 조회는 비교 모집단이 없어")
    print(f"    백분위 기반 총점을 계산하지 않습니다(리뷰 P2-3). scan 결과와")
    print(f"    점수를 비교하려면 스캔 유니버스 안에서 함께 계산해야 합니다.")
    print()
    print(f"  Gate 판정(절대 기준, 총점과 무관): {_STATUS_DESC.get(r.get('status'), r.get('status'))}")
    if r.get("gate_reasons"):
        print(f"    미충족 사유: {', '.join(r['gate_reasons'])}")
    print(f"  {'═'*42}")

    if save_json:
        _save_scan_snapshot([r], f"detail-{code}", f"{r['name']}({code}) 상세",
                            period, bdays, as_of, rank_session=session,
                            daily_scope=daily_scope)


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def _positive_int(value):
    try:
        iv = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"정수가 아닙니다: {value}")
    if iv <= 0:
        raise argparse.ArgumentTypeError(f"1 이상의 정수여야 합니다: {value}")
    return iv


def _as_of_arg(value):
    """
    --as-of 파싱. 기준시각을 고정하면 세션 경계(15:29/15:30/16:00/19:59/20:00)
    회귀 검증을 실제 시계와 무관하게 재현할 수 있다(리뷰 5절 필수 회귀 사례).
    """
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y%m%d%H%M%S", "%Y%m%d%H%M"):
        try:
            return datetime.strptime(str(value), fmt)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"기준시각 형식이 올바르지 않습니다: {value} "
        f"(예: '2026-09-14 16:05' 또는 '202609141605')")


_SESSION_ARG = {"regular": _SESSION_REGULAR, "after": _SESSION_AFTER}

_PERIOD_CHOICES = [1, 3, 5, 10, 15, 30, 60]


def main():
    parser = argparse.ArgumentParser(
        description="키움 REST API CVD/OBV 스캐너 — 최근 N영업일 분봉 한 번에 수집",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""사용 예시:
  # 종목 목록 먼저 생성 (최초 1회, 분기별 변경 시 재실행)
  python3 tools/update_index.py

  # 5영업일 스캔 (기본)
  python3 tools/kiwoom_cvd_obv_scanner.py scan --index kospi200 --top 10
  python3 tools/kiwoom_cvd_obv_scanner.py scan --index kosdaq150 --top 10
  python3 tools/kiwoom_cvd_obv_scanner.py scan --index both --top 10

  # CVD 산출 기간 변경
  python3 tools/kiwoom_cvd_obv_scanner.py scan --index kospi200 --days 10
  python3 tools/kiwoom_cvd_obv_scanner.py scan --index kospi200 --days 20

  # 단일 종목 상세
  python3 tools/kiwoom_cvd_obv_scanner.py detail --code 005930 --days 10

  # --index both 통합 순위(코스피200+코스닥150을 하나의 모집단으로)
  python3 tools/kiwoom_cvd_obv_scanner.py scan --index both --rank-mode combined

  # 결과를 JSON 스냅샷으로 저장(reports/daily-scan/)
  python3 tools/kiwoom_cvd_obv_scanner.py scan --index kospi200 --save-json

  # 애프터마켓(16:00~20:00) 기준 저녁 스캔
  python3 tools/kiwoom_cvd_obv_scanner.py scan --index kospi200 --session after

  # 세션 경계 회귀 검증 — 기준시각 고정
  python3 tools/kiwoom_cvd_obv_scanner.py detail --code 005930 --as-of "2026-09-14 16:05"

키 설정 — 프로젝트 루트 .env 파일 (.env.example 참고):
  KIWOOM_APP_KEY=앱키
  KIWOOM_APP_SECRET=앱시크릿

종목 목록 갱신은 이 스크립트가 아니라 별도 스크립트를 사용합니다:
  python3 tools/update_index.py"""
    )

    sub = parser.add_subparsers(dest="cmd")

    # scan
    ps = sub.add_parser("scan", help="코스피200/코스닥150 스캔")
    ps.add_argument("--index",  default="kospi200",
                    choices=["kospi200", "kosdaq150", "both"])
    ps.add_argument("--top",    type=_positive_int, default=15,
                    help="출력 종목 수 (기본: 15)")
    ps.add_argument("--period", type=int, default=5,
                    choices=_PERIOD_CHOICES)
    ps.add_argument("--days",   type=_positive_int, default=5,
                    help="최근 영업일 수 (기본: 5)")
    ps.add_argument("--rank-mode", default="separate",
                    choices=["separate", "combined"],
                    help="--index both일 때 지수별 개별 순위(separate, 기본) "
                         "또는 통합 순위(combined)")
    ps.add_argument("--session", default="regular", choices=list(_SESSION_ARG),
                    help="점수·순위·Gate 기준 세션 — regular(정규장, 기본) 또는 "
                         "after(애프터마켓 16:00~20:00). 두 세션 지표는 항상 함께 출력")
    ps.add_argument("--daily-scope", default="auto", choices=list(_DAILY_SCOPE_CHOICES),
                    help="일봉 완료 판정 기준 — auto(기본, 보수적: 애프터마켓 종료 후) / "
                         "regular(15:30 확정) / extended(20:00 확정)")
    ps.add_argument("--as-of", type=_as_of_arg, default=None,
                    help="기준시각 고정(예: '2026-09-14 16:05') — 세션 경계 회귀 검증용")
    ps.add_argument("--save-json", action="store_true",
                    help="결과(원시 지표·점수·Gate 사유)를 reports/daily-scan/*.json으로 저장")

    # detail
    pd = sub.add_parser("detail", help="단일 종목 상세 분석")
    pd.add_argument("--code",   required=True)
    pd.add_argument("--period", type=int, default=5,
                    choices=_PERIOD_CHOICES)
    pd.add_argument("--days",   type=_positive_int, default=5)
    pd.add_argument("--session", default="regular", choices=list(_SESSION_ARG),
                    help="Gate 기준 세션 — regular(기본) / after(애프터마켓)")
    pd.add_argument("--daily-scope", default="auto", choices=list(_DAILY_SCOPE_CHOICES),
                    help="일봉 완료 판정 기준 — auto(기본) / regular / extended")
    pd.add_argument("--as-of", type=_as_of_arg, default=None,
                    help="기준시각 고정(예: '2026-09-14 16:05')")
    pd.add_argument("--save-json", action="store_true",
                    help="결과를 reports/daily-scan/*.json으로 저장")

    # update-index (하위호환 — 실제 구현은 tools/update_index.py)
    sub.add_parser("update-index", help="[사용 중단] tools/update_index.py를 사용하세요")

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        sys.exit(0)

    if args.cmd == "scan":
        cmd_scan(args.index, top=args.top,
                 period=args.period, bdays=args.days,
                 rank_mode=args.rank_mode, save_json=args.save_json,
                 session=_SESSION_ARG[args.session],
                 daily_scope=args.daily_scope, as_of=args.as_of)

    elif args.cmd == "detail":
        token = _get_token()
        if not token:
            print("❌ 키움 API 미연결")
            sys.exit(1)
        cmd_detail(args.code, token, period=args.period, bdays=args.days,
                   save_json=args.save_json,
                   session=_SESSION_ARG[args.session],
                   daily_scope=args.daily_scope, as_of=args.as_of)

    elif args.cmd == "update-index":
        print("  이 명령은 더 이상 지원되지 않습니다(이전 구현에 미정의 참조로")
        print("  인한 실행 오류가 있었습니다).")
        print("  종목 목록 갱신은 아래 별도 스크립트를 사용하세요:")
        print("    python3 tools/update_index.py")
        sys.exit(1)


if __name__ == "__main__":
    main()

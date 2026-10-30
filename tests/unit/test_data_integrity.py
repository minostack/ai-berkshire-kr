#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""완료봉/미완료봉 판정(_is_bar_date_complete) 단위 테스트 — 가이드 5.2."""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

import daily_scanner as ds


def test_past_date_is_complete():
    as_of = datetime(2026, 9, 15, 10, 0)
    assert ds._is_bar_date_complete("2026-09-14", as_of) is True


def test_today_before_market_close_is_incomplete():
    as_of = datetime(2026, 9, 15, 10, 0)  # 장중, 15:30 이전
    assert ds._is_bar_date_complete("2026-09-15", as_of) is False


def test_today_after_market_close_is_complete():
    # 봉 기준 bars-1.0: 정규장 확정은 15:30 + 여유 5분
    assert ds._is_bar_date_complete("2026-09-15", datetime(2026, 9, 15, 15, 30)) is False
    assert ds._is_bar_date_complete("2026-09-15", datetime(2026, 9, 15, 15, 35)) is True


def test_aftermarket_in_progress_is_incomplete():
    """애프터마켓 시행일 이후 16:00~20:05에 받은 당일 일봉은 진행 중 값이다."""
    assert ds._is_bar_date_complete("2026-09-15", datetime(2026, 9, 15, 18, 0)) is False
    assert ds._is_bar_date_complete("2026-09-15", datetime(2026, 9, 15, 20, 5)) is True


def test_before_aftermarket_regime_18h_is_complete():
    """시행일(2026-09-14) 전에는 애프터마켓이 없어 15:35 뒤면 계속 완료다."""
    assert ds._is_bar_date_complete("2026-09-11", datetime(2026, 9, 11, 18, 0)) is True


def test_future_date_is_incomplete():
    as_of = datetime(2026, 9, 15, 18, 0)
    assert ds._is_bar_date_complete("2026-09-16", as_of) is False


# ─────────────────────────────────────────────────────────────
# 오류 로깅 — 가이드 9단계("오류에 종목·작업·API ID·시각·재시도 수를
# 남기며 인증정보는 제거한다"). _log_kiwoom_error는 stderr에만 쓰고
# appkey/secretkey/token 같은 인증정보를 포함하면 안 된다.
# ─────────────────────────────────────────────────────────────

def test_log_kiwoom_error_includes_required_fields(capsys):
    ds._log_kiwoom_error("ka10001", {"stk_cd": "005930"}, "테스트 사유", retry_count=2)
    err = capsys.readouterr().err
    assert "api_id=ka10001" in err
    assert "subject=005930" in err
    assert "retry=2" in err
    assert "테스트 사유" in err


def test_log_kiwoom_error_never_leaks_credentials(capsys):
    # 함수 시그니처 자체가 appkey/secretkey/token을 받지 않으므로 새어나갈
    # 값이 없다는 것을, 흔한 키워드가 로그에 없다는 사실로 재확인한다.
    ds._log_kiwoom_error("au10001", {}, "토큰 발급 빈 응답", retry_count=0)
    err = capsys.readouterr().err
    for leaked in ("appkey", "secretkey", "Bearer", "access_token"):
        assert leaked not in err


def test_log_kiwoom_error_subject_defaults_when_no_stk_cd():
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        ds._log_kiwoom_error("ka10023", {"mrkt_tp": "000"}, "사유", retry_count=0)
    assert "subject=N/A" in buf.getvalue()

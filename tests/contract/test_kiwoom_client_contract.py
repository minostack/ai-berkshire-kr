#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
키움 REST 클라이언트(_kiwoom_request/_kiwoom_get_token) 계약 테스트 — 가이드
10단계("계약" 테스트 계층).

실제 키/인터넷 없이 오프라인으로 실행된다. subprocess.run을 모킹해
curl 자체 실패, 빈 응답, JSON 파싱 실패, 업무 오류(return_code!=0),
연속조회(next-key/cont-yn), 타임아웃, 토큰 캐시 재사용/만료를 검증한다.
정상 흐름뿐 아니라 실패 경로가 재시도하는지/하지 않는지까지 확인한다.
"""

import json
import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

import daily_scanner as ds


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stdout=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = b""


def _extract_header_path(cmd):
    if "-D" in cmd:
        return cmd[cmd.index("-D") + 1]
    return None


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """재시도 backoff(최대 1+2+4=7초+jitter)로 테스트가 느려지지 않게 한다."""
    monkeypatch.setattr(ds.time, "sleep", lambda *_a, **_k: None)


@pytest.fixture(autouse=True)
def _isolated_token_cache(monkeypatch, tmp_path):
    """실제 프로젝트의 .kiwoom_token.json을 건드리지 않도록 격리한다."""
    monkeypatch.setattr(ds, "_KIWOOM_TOKEN_CACHE", str(tmp_path / "token_cache.json"))


# ─────────────────────────────────────────────────────────────
# _kiwoom_request — 재시도 가능한 오류 (curl 실패 / 빈 응답 / 타임아웃)
# ─────────────────────────────────────────────────────────────

def test_curl_failure_retries_then_gives_up(monkeypatch):
    calls = []

    def fake_run(cmd, capture_output, timeout):
        calls.append(cmd)
        hp = _extract_header_path(cmd)
        if hp:
            open(hp, "w").close()
        return _FakeCompletedProcess(returncode=1, stdout=b"")

    monkeypatch.setattr(ds.subprocess, "run", fake_run)
    result = ds._kiwoom_request("/api/dostk/stkinfo", {"stk_cd": "005930"}, "ka10001", "fake-token")

    assert result is None
    # 최초 1회 + 재시도 _KIWOOM_MAX_RETRIES회 = 총 4회 호출
    assert len(calls) == 1 + ds._KIWOOM_MAX_RETRIES


def test_empty_response_retries_then_gives_up(monkeypatch):
    calls = []

    def fake_run(cmd, capture_output, timeout):
        calls.append(cmd)
        hp = _extract_header_path(cmd)
        if hp:
            open(hp, "w").close()
        return _FakeCompletedProcess(returncode=0, stdout=b"")

    monkeypatch.setattr(ds.subprocess, "run", fake_run)
    result = ds._kiwoom_request("/api/dostk/stkinfo", {"stk_cd": "005930"}, "ka10001", "fake-token")

    assert result is None
    assert len(calls) == 1 + ds._KIWOOM_MAX_RETRIES


def test_timeout_retries_then_gives_up(monkeypatch):
    calls = {"n": 0}

    def fake_run(cmd, capture_output, timeout):
        calls["n"] += 1
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    monkeypatch.setattr(ds.subprocess, "run", fake_run)
    result = ds._kiwoom_request("/api/dostk/stkinfo", {"stk_cd": "005930"}, "ka10001", "fake-token")

    assert result is None
    assert calls["n"] == 1 + ds._KIWOOM_MAX_RETRIES


def test_transient_failure_then_success_recovers(monkeypatch):
    """처음 1회는 curl 실패, 재시도에서 성공하면 결과를 정상 반환해야 한다."""
    attempts = {"n": 0}

    def fake_run(cmd, capture_output, timeout):
        attempts["n"] += 1
        hp = _extract_header_path(cmd)
        if attempts["n"] == 1:
            if hp:
                open(hp, "w").close()
            return _FakeCompletedProcess(returncode=1, stdout=b"")
        if hp:
            with open(hp, "w") as f:
                f.write("next-key: \r\ncont-yn: N\r\n")
        body = json.dumps({"return_code": 0, "stk_cd": "005930", "stk_nm": "삼성전자"}).encode()
        return _FakeCompletedProcess(returncode=0, stdout=body)

    monkeypatch.setattr(ds.subprocess, "run", fake_run)
    result = ds._kiwoom_request("/api/dostk/stkinfo", {"stk_cd": "005930"}, "ka10001", "fake-token")

    assert result is not None
    assert result["stk_nm"] == "삼성전자"
    assert attempts["n"] == 2


# ─────────────────────────────────────────────────────────────
# _kiwoom_request — 재시도하면 안 되는 오류 (JSON 파싱 실패 / 업무 오류)
# ─────────────────────────────────────────────────────────────

def test_malformed_json_does_not_retry(monkeypatch):
    calls = {"n": 0}

    def fake_run(cmd, capture_output, timeout):
        calls["n"] += 1
        hp = _extract_header_path(cmd)
        if hp:
            open(hp, "w").close()
        return _FakeCompletedProcess(returncode=0, stdout=b"{not-valid-json")

    monkeypatch.setattr(ds.subprocess, "run", fake_run)
    result = ds._kiwoom_request("/api/dostk/stkinfo", {"stk_cd": "005930"}, "ka10001", "fake-token")

    assert result is None
    assert calls["n"] == 1  # 재시도하지 않음


def test_business_error_does_not_retry(monkeypatch):
    calls = {"n": 0}

    def fake_run(cmd, capture_output, timeout):
        calls["n"] += 1
        hp = _extract_header_path(cmd)
        if hp:
            open(hp, "w").close()
        body = json.dumps({"return_code": 3, "return_msg": "존재하지 않는 종목코드"}).encode()
        return _FakeCompletedProcess(returncode=0, stdout=body)

    monkeypatch.setattr(ds.subprocess, "run", fake_run)
    result = ds._kiwoom_request("/api/dostk/stkinfo", {"stk_cd": "999999"}, "ka10001", "fake-token")

    assert result is None
    assert calls["n"] == 1  # 업무 오류는 재시도해도 같은 결과일 가능성이 높아 재시도 안 함


def test_business_error_logs_without_credentials(monkeypatch, capsys):
    def fake_run(cmd, capture_output, timeout):
        hp = _extract_header_path(cmd)
        if hp:
            open(hp, "w").close()
        body = json.dumps({"return_code": 3, "return_msg": "존재하지 않는 종목코드"}).encode()
        return _FakeCompletedProcess(returncode=0, stdout=body)

    monkeypatch.setattr(ds.subprocess, "run", fake_run)
    ds._kiwoom_request("/api/dostk/stkinfo", {"stk_cd": "999999"}, "ka10001", "super-secret-token-value")

    err = capsys.readouterr().err
    assert "api_id=ka10001" in err
    assert "subject=999999" in err
    assert "super-secret-token-value" not in err


# ─────────────────────────────────────────────────────────────
# _kiwoom_request — 연속조회(next-key/cont-yn) 헤더 전달
# ─────────────────────────────────────────────────────────────

def test_pagination_headers_are_captured_from_response_headers(monkeypatch):
    def fake_run(cmd, capture_output, timeout):
        hp = _extract_header_path(cmd)
        if hp:
            with open(hp, "w") as f:
                f.write("HTTP/1.1 200 OK\r\nnext-key: abc123\r\ncont-yn: Y\r\n\r\n")
        body = json.dumps({"return_code": 0, "stk_dt_pole_chart_qry": []}).encode()
        return _FakeCompletedProcess(returncode=0, stdout=body)

    monkeypatch.setattr(ds.subprocess, "run", fake_run)
    result = ds._kiwoom_request("/api/dostk/chart", {"stk_cd": "005930"}, "ka10081", "fake-token")

    assert result["_next_key"] == "abc123"
    assert result["_cont_yn"] == "Y"


# ─────────────────────────────────────────────────────────────
# _kiwoom_get_token — 캐시 재사용/만료, 실패 경로
# ─────────────────────────────────────────────────────────────

def test_token_missing_env_returns_none_without_network(monkeypatch):
    monkeypatch.delenv("KIWOOM_APP_KEY", raising=False)
    monkeypatch.delenv("KIWOOM_APP_SECRET", raising=False)
    called = {"n": 0}
    monkeypatch.setattr(ds.subprocess, "run", lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    assert ds._kiwoom_get_token() is None
    assert called["n"] == 0  # 키 없으면 네트워크 호출 자체를 하지 않아야 함


def test_token_cache_hit_skips_network(monkeypatch, tmp_path):
    monkeypatch.setenv("KIWOOM_APP_KEY", "k")
    monkeypatch.setenv("KIWOOM_APP_SECRET", "s")
    cache_path = tmp_path / "token_cache.json"
    cache_path.write_text(json.dumps({"token": "cached-token", "expires_at": time.time() + 3600}))
    monkeypatch.setattr(ds, "_KIWOOM_TOKEN_CACHE", str(cache_path))

    called = {"n": 0}
    monkeypatch.setattr(ds.subprocess, "run", lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    token = ds._kiwoom_get_token()
    assert token == "cached-token"
    assert called["n"] == 0


def test_token_cache_expired_triggers_fresh_request(monkeypatch, tmp_path):
    monkeypatch.setenv("KIWOOM_APP_KEY", "k")
    monkeypatch.setenv("KIWOOM_APP_SECRET", "s")
    cache_path = tmp_path / "token_cache.json"
    cache_path.write_text(json.dumps({"token": "stale-token", "expires_at": time.time() - 10}))
    monkeypatch.setattr(ds, "_KIWOOM_TOKEN_CACHE", str(cache_path))

    def fake_run(cmd, capture_output, timeout):
        body = json.dumps({"token": "fresh-token", "token_expired": 3600}).encode()
        return _FakeCompletedProcess(returncode=0, stdout=body)

    monkeypatch.setattr(ds.subprocess, "run", fake_run)
    token = ds._kiwoom_get_token()
    assert token == "fresh-token"


def test_token_empty_response_logs_and_returns_none(monkeypatch, capsys):
    monkeypatch.setenv("KIWOOM_APP_KEY", "MYSECRETAPPKEY12345")
    monkeypatch.setenv("KIWOOM_APP_SECRET", "MYSECRETSECRET67890")
    monkeypatch.setattr(ds.subprocess, "run",
                         lambda *a, **k: _FakeCompletedProcess(returncode=0, stdout=b""))

    assert ds._kiwoom_get_token() is None
    err = capsys.readouterr().err
    assert "api_id=au10001" in err
    assert "MYSECRETAPPKEY12345" not in err
    assert "MYSECRETSECRET67890" not in err

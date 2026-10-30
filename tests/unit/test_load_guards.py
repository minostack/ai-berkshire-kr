# -*- coding: utf-8 -*-
"""적재 안전장치 — 실행 잠금, 키움 토큰 재시도.

1. 적재 둘이 동시에 돌지 않는다. 뒤 실행은 기다리고, 너무 오래면 포기한다.
2. 잠금은 본문이 끝나거나 예외가 나면 풀린다.
3. 토큰 발급이 일시 실패하면 기다렸다가 다시 시도하고, 끝내 실패하면 None.
"""

import subprocess
import sys
import textwrap

import pytest

from cvd_obv.ingestion.kiwoom_auth import token_with_retry
from cvd_obv.ingestion.run_lock import LockTimeout, exclusive


def test_잠금은_한_번에_하나만_잡는다(tmp_path):
    path = str(tmp_path / "x.lock")
    sleeps, logs = [], []
    with exclusive("앞 작업", path=path, log=None):
        with pytest.raises(LockTimeout) as err:
            with exclusive("뒤 작업", path=path, wait_sec=0, poll_sec=0,
                           log=logs.append, sleep=sleeps.append):
                pytest.fail("잠금이 겹쳤다")
        assert "앞 작업" in str(err.value)


def test_뒤_실행은_앞_실행이_끝나면_시작한다(tmp_path):
    path = str(tmp_path / "x.lock")
    holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {repr(str(__import__('pathlib').Path(__file__).parents[2] / 'src'))})
        from cvd_obv.ingestion.run_lock import exclusive
        with exclusive("다른 프로세스", path={path!r}, log=None):
            print("locked", flush=True)
            time.sleep(1.5)
    """)], stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "locked"
    logs = []
    with exclusive("뒤 작업", path=path, wait_sec=30, poll_sec=0.2, log=logs.append):
        pass
    holder.wait(timeout=10)
    assert any("기다린다" in m and "다른 프로세스" in m for m in logs)
    assert any("시작한다" in m for m in logs)


def test_예외가_나도_잠금이_풀린다(tmp_path):
    path = str(tmp_path / "x.lock")
    with pytest.raises(RuntimeError):
        with exclusive("a", path=path, log=None):
            raise RuntimeError("단계 실패")
    with exclusive("b", path=path, wait_sec=0, log=None):
        pass


class _Scanner:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def _get_token(self):
        self.calls += 1
        return self.answers.pop(0) if self.answers else None


def test_토큰_일시_실패는_기다렸다_다시_시도한다():
    sc, sleeps, logs = _Scanner([None, None, "TOKEN"]), [], []
    assert token_with_retry(sc, waits=(30, 60, 120), sleep=sleeps.append,
                            log=logs.append) == "TOKEN"
    assert sc.calls == 3 and sleeps == [30, 60] and len(logs) == 2


def test_토큰이_끝내_실패하면_None():
    sc, sleeps = _Scanner([]), []
    assert token_with_retry(sc, waits=(1, 2), sleep=sleeps.append, log=None) is None
    assert sc.calls == 3 and sleeps == [1, 2]


def test_첫_시도에_성공하면_기다리지_않는다():
    sc, sleeps = _Scanner(["T"]), []
    assert token_with_retry(sc, waits=(30,), sleep=sleeps.append, log=None) == "T"
    assert sleeps == []


def test_재시도_간격은_환경변수로_바꾼다(monkeypatch):
    monkeypatch.setenv("KIWOOM_TOKEN_RETRY_WAITS", "")
    sc, sleeps = _Scanner([]), []
    assert token_with_retry(sc, sleep=sleeps.append, log=None) is None
    assert sc.calls == 1 and sleeps == []

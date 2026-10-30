# -*- coding: utf-8 -*-
"""적재 실행 잠금 — `data-load daily|provisional`이 동시에 돌지 않게 한다.

왜 필요한가
-----------
작업 스케줄러의 "놓친 작업 즉시 실행"(StartWhenAvailable)과 "이미 실행 중이면 새로
시작 안 함"(MultipleInstances=IgnoreNew)은 **같은 작업끼리만** 막는다. 08:00에 부팅하면
놓친 16:10 잠정 작업이 08:10에, 확정 작업이 08:30에 돈다. 밀린 날이 많아 앞 작업이
길어지면 두 프로세스가 같은 Parquet 테이블·장부 파일에 동시에 쓴다.

동작
----
OS 파일 잠금(Windows `msvcrt.locking`, 그 밖 `fcntl.flock`)을 잡는다. 프로세스가
죽으면 OS가 잠금을 푼다 — 남은 잠금 파일 때문에 영원히 막히는 일이 없다. 다른
적재가 잡고 있으면 끝날 때까지 기다린다(기본 최대 3시간). 기다린 쪽은 앞 작업이
채운 것을 건너뛰므로 빨리 끝난다. 누가 잡고 있는지는 옆 파일(`.json`)에 남긴다.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime

__all__ = ["LOCK_WAIT_SEC", "LockTimeout", "lock_path", "exclusive"]

LOCK_WAIT_SEC = 3 * 3600
_POLL_SEC = 10


class LockTimeout(RuntimeError):
    """`wait_sec` 안에 잠금을 얻지 못했다."""


def lock_path() -> str:
    from ..baseline.scanner_v15 import project_root
    return os.path.join(project_root(), "data", "manifests", "data_load.lock")


def _try_lock(fh) -> bool:
    try:
        import msvcrt
        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    except ImportError:
        import fcntl
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False


def _unlock(fh):
    try:
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except ImportError:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _holder(info_path):
    try:
        with open(info_path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


@contextmanager
def exclusive(label: str, path: str = None, wait_sec: float = LOCK_WAIT_SEC,
              poll_sec: float = _POLL_SEC, log=print, sleep=time.sleep):
    """잠금을 잡은 동안만 본문을 실행한다. 못 잡으면 기다리고, 시간이 지나면 `LockTimeout`."""
    path = path or lock_path()
    info_path = path + ".json"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, "a+")
    try:
        started = time.monotonic()
        announced = False
        while not _try_lock(fh):
            waited = time.monotonic() - started
            if waited >= wait_sec:
                h = _holder(info_path)
                raise LockTimeout(
                    f"다른 적재가 {wait_sec / 60:.0f}분 넘게 실행 중이다 — "
                    f"{h.get('label', '?')} (pid {h.get('pid', '?')}, "
                    f"시작 {h.get('started', '?')})")
            if not announced and log:
                h = _holder(info_path)
                log(f"  ⏳ 다른 적재가 실행 중이라 끝날 때까지 기다린다 — "
                    f"{h.get('label', '?')} (pid {h.get('pid', '?')}, "
                    f"시작 {h.get('started', '?')})")
                announced = True
            sleep(poll_sec)
        if announced and log:
            log(f"  ▶ 앞 적재가 끝났다({time.monotonic() - started:.0f}초 대기) — 시작한다")
        try:
            with open(info_path, "w", encoding="utf-8") as f:
                json.dump({"label": label, "pid": os.getpid(),
                           "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                          f, ensure_ascii=False)
        except OSError:
            pass
        try:
            yield
        finally:
            _unlock(fh)
    finally:
        fh.close()

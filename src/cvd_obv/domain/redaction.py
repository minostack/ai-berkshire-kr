# -*- coding: utf-8 -*-
"""비밀정보 탐지·제거.

Stage 0 DoD: "비밀키나 API 토큰이 manifest·로그·Parquet에 없다."

키움 REST API는 `appkey`/`secretkey`로 Bearer 토큰을 받는다. 그 값들이 설정
스냅샷이나 요청 파라미터를 타고 artifact에 섞여 들어가는 경로를 막는다.
탐지는 **키 이름**과 **값 형태** 양쪽을 본다. 이름만 보면 `{"a": "<토큰>"}`을
놓치고, 값만 보면 짧은 토큰을 놓친다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from .errors import DataQualityError
from .status import DetailCode

__all__ = ["SECRET_KEY_RE", "SECRET_VALUE_PATTERNS", "REDACTED",
           "redact", "find_secrets", "assert_no_secrets"]

REDACTED = "<REDACTED>"

#: 이 이름을 가진 key의 값은 내용과 무관하게 지운다.
SECRET_KEY_RE = re.compile(
    r"(?i)(token|access_?token|refresh_?token|appkey|app_?key|secret|"
    r"secretkey|secret_?key|password|passwd|api_?key|authorization|"
    r"credential|private_?key|client_?secret)")

#: 값 자체가 비밀로 보이는 형태.
SECRET_VALUE_PATTERNS = (
    ("bearer_header", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\."
                       r"[A-Za-z0-9_\-]{8,}\b")),
    ("kiwoom_token", re.compile(r"(?i)\b[A-Za-z0-9_\-]{60,}\b")),
)

#: 오탐을 줄이기 위해 비밀로 보지 않는 값 형태(해시는 artifact의 정상 내용).
_ALLOW_VALUE_RE = re.compile(
    r"^(sha256:[0-9a-f]{64}|[0-9a-f]{64}|sig_[0-9a-f]{64}|"
    r"usnap_[0-9a-f]{64})$")


def _looks_secret_value(value: str):
    if _ALLOW_VALUE_RE.match(value):
        return None
    for name, pat in SECRET_VALUE_PATTERNS:
        if pat.search(value):
            return name
    return None


def find_secrets(obj, path: str = "$"):
    """의심 지점 목록을 돌려준다. `[(경로, 사유)]`."""
    hits = []
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            child = f"{path}.{k}"
            if (isinstance(k, str) and SECRET_KEY_RE.search(k)
                    and v not in (None, "", REDACTED)):
                hits.append((child, "secret_key_name"))
                continue
            hits.extend(find_secrets(v, child))
    elif isinstance(obj, Sequence) and not isinstance(obj, (str, bytes)):
        for i, v in enumerate(obj):
            hits.extend(find_secrets(v, f"{path}[{i}]"))
    elif isinstance(obj, str):
        reason = _looks_secret_value(obj)
        if reason:
            hits.append((path, reason))
    return hits


def redact(obj):
    """비밀로 보이는 값을 `<REDACTED>`로 바꾼 복사본을 돌려준다."""
    if isinstance(obj, Mapping):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and SECRET_KEY_RE.search(k) and v not in (None, ""):
                out[k] = REDACTED
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, Sequence) and not isinstance(obj, (str, bytes)):
        return [redact(v) for v in obj]
    if isinstance(obj, str) and _looks_secret_value(obj):
        return REDACTED
    return obj


def assert_no_secrets(obj, *, what: str = "artifact") -> None:
    """비밀정보가 남아 있으면 hard fail. 경고로 낮추지 않는다."""
    hits = find_secrets(obj)
    if hits:
        raise DataQualityError(
            f"{what}에 비밀정보로 보이는 값이 있다: "
            + ", ".join(f"{p}({r})" for p, r in hits[:10]),
            detail_code=DetailCode.SECRET_LEAK,
            context={"hits": hits[:50], "hit_count": len(hits)},
        )

# -*- coding: utf-8 -*-
"""구성종목 스냅샷 수집·보관.

실측으로 확인한 제약(2026-09-18)
--------------------------------
- 키움 `ka20002`(업종별주가요청)는 **현재 구성종목만** 준다. `base_dt`·`dt`를
  넣어도 무시하고 같은 결과를 돌려준다. 과거 구성종목을 얻을 수 없다.
- 키움 `ka10099`(종목정보 리스트)에는 **상장폐지 종목이 없다**. 한진해운
  (117930)·STX조선해양(067250)을 조회했으나 목록에 없었다.
- KRX 정보데이터시스템 JSON 엔드포인트는 세션 없이 호출하면 `LOGOUT`을 준다.

그래서 이력은 두 경로로만 만들 수 있다.
1. **오늘부터 스냅샷을 쌓는다.** 지금 저장해 두면 내일부터는 이력이 된다.
   비용이 들지 않으므로 무조건 한다.
2. **KRX에서 과거 구성종목 CSV를 받아 넣는다.** 수작업이지만 진짜 과거다.

두 경로 모두 같은 스냅샷 파일 형식으로 모이고, `derive_intervals()`가 구간으로
바꾼다.
"""

from __future__ import annotations

import csv
import json
import os
import re
from datetime import date, datetime

from .kiwoom_auth import token_with_retry
from ..domain.errors import ConfigError, ExternalApiError, NoInputDataError
from ..domain.ids import KST, normalize_security_id, utc_iso
from ..domain.status import DetailCode
from .universe_history import UniverseSnapshot

__all__ = ["KiwoomUniverseSource", "SnapshotStore", "INDEX_PARAMS",
           "load_snapshot_file", "parse_krx_csv"]

#: 실측 확인: `inds_cd=201`은 지수 일봉(ka20006)과 구성종목(ka20002) 양쪽에서
#: 코스피200을 가리킨다. 코스닥150은 전용 코드가 없어 코스닥 전체를 받는다.
INDEX_PARAMS = {
    "KOSPI200": {"mrkt_tp": "2", "inds_cd": "201", "stex_tp": "3",
                 "expected": 200},
    "KOSDAQ150": {"mrkt_tp": "1", "inds_cd": "101", "stex_tp": "3",
                  "expected": None},
}

_CODE_RE = re.compile(r"^[0-9A-Z]{6}$")


def _clean_code(raw):
    """`000080_AL` 같은 접미사를 떼고 6자리 종목코드만 남긴다."""
    code = str(raw or "").strip().upper().split("_")[0]
    return code if _CODE_RE.match(code) else None


class KiwoomUniverseSource:
    """ka20002로 **현재** 구성종목을 받는다."""

    API_ID = "ka20002"
    PATH = "/api/dostk/sect"
    SOURCE = "KIWOOM_KA20002"

    def __init__(self, scanner=None, max_pages: int = 20):
        from ..baseline.scanner_v15 import load_scanner_v15
        self.scanner = scanner or load_scanner_v15()
        self.max_pages = int(max_pages)

    def token(self):
        tok = token_with_retry(self.scanner)   # 일시 실패(부팅 직후 등) 재시도
        if not tok:
            raise ExternalApiError("키움 토큰 발급 실패",
                                   detail_code=DetailCode.AUTH_FAILED)
        return tok

    def fetch(self, index_id: str, token: str = None):
        params = INDEX_PARAMS.get(index_id.upper())
        if params is None:
            raise ConfigError(
                f"알 수 없는 index_id: {index_id} (가능: {sorted(INDEX_PARAMS)})")
        tok = token or self.token()
        body = {k: v for k, v in params.items() if k != "expected"}

        codes, next_key, cont_yn = [], "", ""
        seen_keys, pages = set(), 0
        for _ in range(self.max_pages):
            headers = ([f"next-key: {next_key}", f"cont-yn: {cont_yn}"]
                       if next_key else [])
            data = self.scanner._kiwoom(self.PATH, body, self.API_ID, tok,
                                        extra_headers=headers)
            pages += 1
            if not data:
                raise ExternalApiError(
                    f"{index_id} 구성종목 조회 실패(응답 없음)",
                    detail_code=DetailCode.API_NO_RESPONSE)
            rows = data.get("inds_stkpc") or []
            if not rows:
                break
            for row in rows:
                code = _clean_code(row.get("stk_cd"))
                if code:
                    codes.append(code)
            next_key = data.get("_next_key", "") or ""
            cont_yn = data.get("_cont_yn", "") or ""
            if cont_yn != "Y" or not next_key or next_key in seen_keys:
                break
            seen_keys.add(next_key)

        unique = sorted(set(codes))
        if not unique:
            raise NoInputDataError(f"{index_id} 구성종목이 비어 있다")
        return {
            "index_id": index_id.upper(),
            "codes": unique,
            "pages": pages,
            "expected": params["expected"],
            "source": self.SOURCE,
        }


class SnapshotStore:
    """스냅샷 파일 보관 — `data/universe_snapshots/{index}_{YYYYMMDD}.json`.

    Parquet이 아니라 JSON으로 둔다. 사람이 손으로 넣는 KRX 자료와 API 결과가
    같은 자리에 모여야 하고, 파일 하나가 곧 "그날 관측"이라 눈으로 확인할 수
    있어야 하기 때문이다.
    """

    def __init__(self, root: str = None):
        if root is None:
            here = os.path.dirname(os.path.abspath(__file__))
            root = os.path.abspath(os.path.join(
                here, "..", "..", "..", "data", "universe_snapshots"))
        self.root = root

    def path_for(self, index_id: str, as_of: date) -> str:
        return os.path.join(self.root,
                            f"{index_id.upper()}_{as_of:%Y%m%d}.json")

    def save(self, index_id: str, as_of: date, codes, source: str,
             note: str = None, extra: dict = None) -> str:
        os.makedirs(self.root, exist_ok=True)
        payload = {
            "index_id": index_id.upper(),
            "as_of": as_of.isoformat(),
            "captured_at_utc": utc_iso(datetime.now(tz=KST)),
            "source": source,
            "member_count": len(codes),
            "members": sorted({normalize_security_id(c) for c in codes}),
            "note": note,
        }
        if extra:
            payload.update(extra)
        path = self.path_for(index_id, as_of)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        return path

    def list_files(self, index_id: str = None):
        if not os.path.isdir(self.root):
            return []
        out = []
        for name in sorted(os.listdir(self.root)):
            if not name.endswith(".json"):
                continue
            if index_id and not name.upper().startswith(
                    f"{index_id.upper()}_"):
                continue
            out.append(os.path.join(self.root, name))
        return out

    def load_all(self, index_id: str = None):
        files = self.list_files(index_id)
        if not files:
            raise NoInputDataError(
                f"스냅샷 파일이 없다: {self.root} — `universe snapshot` 먼저")
        return [load_snapshot_file(p) for p in files]


def load_snapshot_file(path: str) -> UniverseSnapshot:
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    return UniverseSnapshot(
        payload["index_id"],
        date.fromisoformat(payload["as_of"]),
        payload["members"],
        source_ref=os.path.basename(path))


def parse_krx_csv(path: str, index_id: str, as_of: date):
    """KRX 정보데이터시스템에서 내려받은 구성종목 CSV를 읽는다.

    KRX CSV는 인코딩(EUC-KR/UTF-8)과 열 이름이 화면마다 다르다. 종목코드로
    보이는 열을 찾아 쓰되, 못 찾으면 **추측하지 않고 실패**한다 — 잘못된 열을
    종목코드로 읽으면 유니버스가 통째로 틀린다.
    """
    rows, used_encoding = None, None
    for encoding in ("utf-8-sig", "cp949", "euc-kr", "utf-8"):
        try:
            with open(path, encoding=encoding, newline="") as f:
                rows = list(csv.DictReader(f))
            used_encoding = encoding
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if not rows:
        raise ConfigError(f"CSV를 읽지 못했다: {path}")

    headers = list(rows[0])
    candidates = [h for h in headers
                  if any(t in h.replace(" ", "")
                         for t in ("종목코드", "단축코드", "Code", "code"))]
    if not candidates:
        raise ConfigError(
            f"종목코드 열을 찾지 못했다: {headers} — 열 이름을 확인할 것")

    column = candidates[0]
    codes = []
    for row in rows:
        code = _clean_code(str(row.get(column, "")).strip().strip("'\"= "))
        if code:
            codes.append(code)
    if not codes:
        raise ConfigError(f"{path}: '{column}' 열에서 종목코드를 찾지 못했다")
    return {
        "index_id": index_id.upper(),
        "as_of": as_of,
        "codes": sorted(set(codes)),
        "column": column,
        "encoding": used_encoding,
        "rows": len(rows),
    }

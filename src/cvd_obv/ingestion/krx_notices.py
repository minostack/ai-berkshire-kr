# -*- coding: utf-8 -*-
"""KRX 지수운영공지 수집 — 로드맵 Stage 3의 '명단' 경로.

왜 이 게시판인가
----------------
`data.krx.co.kr`은 2026년 Data Marketplace로 개편되며 화면 전체가 로그인 뒤로
들어갔다. 반면 **`index.krx.co.kr`의 지수운영공지는 로그인 없이 열린다**
(2026-09-18 실측). 그리고 여기에 정기변경 첨부 엑셀이 붙어 있는데, 그 안에는
편입·편출 종목뿐 아니라 **변경 후 구성종목 전체 명단**이 들어 있다.

즉 이 게시판은 "무엇이 언제 바뀌었나"와 "그래서 명단이 무엇이 되었나"를 둘 다
준다. `krx_openapi`의 시총 항등식이 그 명단을 검산한다. 두 출처가 서로를
확인하므로 어느 한쪽의 오독이 조용히 통과하지 않는다.

실측으로 확인한 것 (2026-09-18)
-------------------------------
- 게시판 목록은 POST `/board/MKD01040000/list`. 세션 쿠키가 필요 없다.
- 첨부 다운로드는 2단계다. `/contents/COM/GenerateOTP.jspx`로 코드를 받고
  `https://file.krx.co.kr/download.jspx`에 그 코드를 POST한다.
- 첨부가 붙기 시작한 것은 **2003-05-29 정기변경 공지**부터다. 2001~2002년
  공지는 본문만 있다.
- 최근 공지(2020년 이후)는 본문에 종목명을 적지 않고 "지수 구성정보 상품
  참조"로 넘기는 경우가 많다. 그래서 **첨부가 있는 공지가 1순위**다.
"""

from __future__ import annotations

import http.cookiejar
import os
import re
import urllib.parse
import urllib.request
from datetime import date as date_cls
from datetime import datetime

from ..domain.errors import ExternalApiError
from ..domain.status import DetailCode

__all__ = ["KRX_NOTICE_SOURCE", "BOARD_ID", "KrxNoticeBoard",
           "parse_effective_dates", "EFFECTIVE_KEYWORDS"]

KRX_NOTICE_SOURCE = "KRX_INDEX_NOTICE"
BOARD_ID = "MKD01040000"
_BASE = "https://index.krx.co.kr"
_FILE_SERVER = "https://file.krx.co.kr"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128.0"

#: 코스피200 유니버스에 영향을 주는 공지만 고른다. 지나치게 좁히면 수시변경을
#: 놓치므로, 지수명이 없어도 '특례편입'·'특별변경' 같은 말은 남긴다.
KOSPI200_PATTERNS = (
    r"(KOSPI|코스피)\s*200",
    r"특례\s*편입", r"특별\s*변경", r"수시\s*변경", r"정기\s*변경",
)


def project_root() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", "..", ".."))


class KrxNoticeBoard:
    """지수운영공지 목록·첨부 수집기."""

    def __init__(self, cache_dir: str = None):
        self.cache_dir = cache_dir or os.path.join(
            project_root(), "data", "bronze", "krx_notices")
        jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar))

    # ── HTTP ──────────────────────────────────────────────────────
    def _post(self, path, params, host=_BASE, binary=False):
        body = urllib.parse.urlencode(params, encoding="utf-8").encode()
        req = urllib.request.Request(host + path, data=body, headers={
            "User-Agent": _UA, "Referer": f"{_BASE}/board/{BOARD_ID}/bbs",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
        try:
            with self._opener.open(req, timeout=120) as resp:
                data = resp.read()
                disposition = resp.headers.get("Content-Disposition", "")
        except Exception as exc:                          # noqa: BLE001
            raise ExternalApiError(f"KRX 공지 요청 실패({path}): {exc}",
                                   detail_code=DetailCode.API_NO_RESPONSE) from exc
        if binary:
            return data, disposition
        return data.decode("utf-8", "replace")

    # ── 목록 ──────────────────────────────────────────────────────
    def page(self, start: str, end: str, page: int = 1, word: str = ""):
        """한 페이지. `start`/`end`는 YYYYMMDD."""
        html = self._post(f"/board/{BOARD_ID}/list", {
            "bbsId": BOARD_ID, "bbsUrl": BOARD_ID, "curPage": str(page),
            "srchTitle": "TTL" if word else "", "srchWord": word,
            "startDate": start, "endDate": end, "listOrder": "new",
            "boardStyle": "blog"})
        total = re.search(r"totalCount'>(\d+)<", html)
        rows = []
        for blk in re.split(r"<li[^>]*>", html)[1:]:
            title = re.search(
                r"data-view='(\d+)'[^>]*>(?:<span[^>]*></span>)?(.*?)</a>",
                blk, flags=re.S)
            posted = re.search(r"작성일\s*:\s*(\d{4}-\d{2}-\d{2})", blk)
            if not (title and posted):
                continue
            body = re.sub(r"<[^>]+>", " ", blk)
            body = re.sub(r"&nbsp;|&middot;", " ", body).replace("&amp;", "&")
            rows.append({
                "seq": title.group(1),
                "posted_date": posted.group(1),
                "title": re.sub(r"\s+", " ", title.group(2)).strip(),
                "body": re.sub(r"\s+", " ", body).strip(),
                "source": KRX_NOTICE_SOURCE,
            })
        return (int(total.group(1)) if total else 0), rows

    def list_all(self, start: str, end: str, word: str = "", max_pages=400):
        """구간 전체. 공지 고정글이 매 페이지에 끼므로 seq로 중복을 건다."""
        seen, out = set(), []
        total, _ = self.page(start, end, 1, word)
        for page in range(1, max_pages + 1):
            _, rows = self.page(start, end, page, word)
            fresh = [r for r in rows if r["seq"] not in seen]
            if not fresh:
                break
            for r in fresh:
                seen.add(r["seq"])
            out.extend(fresh)
            if len(out) >= total:
                break
        out.sort(key=lambda r: (r["posted_date"], r["seq"]))
        return out

    @staticmethod
    def is_universe_related(notice) -> bool:
        text = notice["title"]
        return any(re.search(p, text) for p in KOSPI200_PATTERNS)

    # ── 첨부 ──────────────────────────────────────────────────────
    def attachments(self, seq):
        html = self._post(f"/board/{BOARD_ID}/list?cmd=filelist",
                          {"bbsSeq": str(seq)})
        return [{"file_seq": m.group(1), "name": m.group(2).strip()}
                for m in re.finditer(r"data-file-seq='(\d+)'>([^<]+)</a>", html)]

    def download(self, seq, file_seq, name_hint: str = ""):
        """첨부 1개를 bronze에 저장하고 (경로, 바이트수)를 돌려준다."""
        out_dir = os.path.join(self.cache_dir, "attachments", str(seq))
        safe = re.sub(r"[^0-9A-Za-z가-힣._-]+", "_", name_hint or "file")
        path = os.path.join(out_dir, f"{file_seq}_{safe}")
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path, os.path.getsize(path)

        code = self._post("/contents/COM/GenerateOTP.jspx", {
            "url": "COM/board_attach_down", "filetype": "att",
            "bbsId": BOARD_ID, "bbsSeq": str(seq),
            "attachFileSeq": str(file_seq), "name": "fileDown"})
        data, disposition = self._post("/download.jspx", {"code": code.strip()},
                                       host=_FILE_SERVER, binary=True)
        if not data:
            raise ExternalApiError(f"첨부 {seq}/{file_seq} 응답이 비어 있다",
                                   detail_code=DetailCode.API_NO_RESPONSE)
        got = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition or "")
        if got and not name_hint:
            safe = re.sub(r"[^0-9A-Za-z가-힣._-]+", "_",
                          urllib.parse.unquote(got.group(1)))
            path = os.path.join(out_dir, f"{file_seq}_{safe}")
        os.makedirs(out_dir, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path, len(data)


#: 시행일을 가리키는 말. 날짜가 **뒤에** 오는 양식.
#:   "ㅇ 반영일 : '26.6.12(금)"
EFFECTIVE_KEYWORDS = ("반영일자", "반영일", "적용일", "시행일", "정기변경일",
                      "변경일")

#: 날짜가 **앞에** 오는 양식.
#:   "구성종목을 ’26. 6. 12(금)에 정기변경할 예정입니다"
EFFECTIVE_KEYWORDS_TRAILING = ("에 정기변경", "에 정기 변경", "부터 반영",
                               "에 반영", "에 변경", "에 실시")

_DATE_RE = re.compile(
    r"[’'‘‘’]?\s*(\d{2,4})\s*[.\-/]\s*(\d{1,2})\s*[.\-/]\s*(\d{1,2})")


def _to_date(year, month, day):
    year, month, day = int(year), int(month), int(day)
    if year < 100:
        year += 2000
    if not (1990 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31):
        return None
    try:
        return datetime(year, month, day).date()
    except ValueError:
        return None


def _dates_in(text):
    out = []
    for found in _DATE_RE.finditer(text):
        day = _to_date(*found.groups())
        if day:
            out.append(day)
    return out


def parse_effective_dates(text, posted_date=None):
    """본문에서 시행일 후보를 뽑는다. 키워드 옆 날짜를 앞에 둔다.

    공지에는 '작성일'·'심사기준일'·'매매거래정지일' 등 여러 날짜가 섞여 있다.
    그래서 날짜를 전부 긁어 아무거나 쓰지 않고

    1. '반영일' 같은 말 **뒤**, 또는 '…에 정기변경' 같은 말 **앞**의 날짜를 먼저,
    2. 그중에서도 공지 작성일 **이후**의 날짜를 먼저 둔다(공지는 예고다).

    고르지 못하면 후보를 순서대로 돌려주고 판단을 호출부에 남긴다. 여기서 하나를
    임의로 찍으면 구간 경계가 조용히 틀어지고, 나중에 그 사실을 알 방법이 없다.
    """
    near = []
    for keyword in EFFECTIVE_KEYWORDS:
        at = text.find(keyword)
        while at >= 0:
            near += _dates_in(text[at:at + 60])
            at = text.find(keyword, at + len(keyword))
    for keyword in EFFECTIVE_KEYWORDS_TRAILING:
        at = text.find(keyword)
        while at >= 0:
            near += _dates_in(text[max(0, at - 40):at])
            at = text.find(keyword, at + len(keyword))

    posted = None
    if posted_date:
        posted = (posted_date if isinstance(posted_date, date_cls)
                  else datetime.strptime(posted_date, "%Y-%m-%d").date())

    def _rank(day):
        if posted is None:
            return (0, 0)
        return (0, (day - posted).days) if day >= posted else (1, (posted - day).days)

    ordered, seen = [], set()
    for group in (near, _dates_in(text)):
        for day in sorted(group, key=_rank):
            if day in seen:
                continue
            if posted and abs((day - posted).days) > 400:
                continue
            seen.add(day)
            ordered.append(day)
    return ordered

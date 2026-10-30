# -*- coding: utf-8 -*-
"""KRX 공지 첨부(엑셀) 파서 — 구성종목 명단과 편입·편출을 뽑는다.

첨부 양식은 20년 동안 여러 번 바뀌었다(2003 zip, 2005~2012 .xls, 2020~ .xlsx).
그래서 시트 이름이나 열 위치를 고정으로 믿지 않는다. 대신

1. '종목코드'와 '종목명'이 나란히 있는 **헤더 행을 찾고**,
2. 그 위쪽 셀에서 '편입'/'제외' 같은 **구간 이름을 읽고**,
3. 시트 이름과 표 제목에서 **어느 지수인지**를 읽는다.

읽지 못한 파일은 조용히 건너뛰지 않고 `unparsed`로 남긴다. 명단이 하나라도
빠지면 그 구간의 유니버스가 통째로 틀리기 때문이다.
"""

from __future__ import annotations

import os
import re
import zipfile

__all__ = ["parse_attachment", "isin_to_code", "INDEX_ALIASES"]

#: 표 안에서 지수를 알아보는 말. **완전일치**로 본다.
#: "코스피200 고배당", "코스피200 저변동성", "코스피200 ESG"는 전부 다른 지수다.
#: 부분일치로 잡으면 고배당지수 정기변경 명단이 코스피200 편출입으로 둔갑한다
#: (2026-09-18 실측: 2013~2020 델타가 이렇게 오염돼 복원이 통째로 어긋났다).
INDEX_ALIASES = (
    ("KOSDAQ150", r"(KOSDAQ|코스닥)150"),
    ("KRX300", r"KRX300"),
    ("KOSPI200", r"(KOSPI|코스피)200"),
)

#: 표 제목에서 지워도 되는 구조어. 지수 이름의 일부가 아니다.
_STRUCTURE_WORDS = ("구성종목", "편출입", "변경내역", "교체내역", "정기변경",
                    "수시변경", "특별변경", "교체", "변경", "내역", "선정",
                    "지수", "종목", "현황", "목록", "대외공지", "공지용",
                    "최종", "결과", "및", "등", "const", "changes")

_IN_WORDS = ("편입", "선정", "신규", "추가", "진입")
_OUT_WORDS = ("편출", "제외", "탈락", "삭제")
_FULL_WORDS = ("구성종목", "구성 종목", "종목현황", "구성내역")


def isin_to_code(value) -> str:
    """ISIN(KR7000990002) 또는 6자리 코드를 6자리 종목코드로 맞춘다.

    옛 .xls는 종목코드를 **숫자 셀**로 저장한 것이 많다. 그러면 xlrd가 5930.0
    같은 실수로 돌려주고, 앞의 0도 이미 사라져 있다. 그래서 숫자는 정수로 되돌려
    6자리로 채운다. 이걸 놓치면 그 시트의 명단이 통째로 비어 조용히 사라진다.
    """
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        if value != value or value <= 0 or value > 999999:   # NaN·범위 밖
            return ""
        if float(value) != int(value):
            return ""
        return str(int(value)).zfill(6)
    s = str(value or "").strip().upper().replace("'", "")
    if not s:
        return ""
    if re.fullmatch(r"\d{1,6}\.0", s):
        return s[:-2].zfill(6)
    m = re.fullmatch(r"KR[A-Z0-9](\w{6})\w{3}", s)
    if m:
        return m.group(1)
    m = re.fullmatch(r"[A-Z0-9]{6}", s)
    if m:
        return s
    if s.isdigit() and len(s) < 6:
        return s.zfill(6)
    return ""


def _sheets(path):
    """(시트명, 행 리스트) 목록. .xlsx/.xls/.zip을 모두 받는다."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".zip" or (zipfile.is_zipfile(path) and ext not in (".xlsx", ".xlsm")):
        out = []
        with zipfile.ZipFile(path) as zf:
            for inner in zf.namelist():
                if not inner.lower().endswith((".xls", ".xlsx")):
                    continue
                tmp = os.path.join(os.path.dirname(path),
                                   "_unzip_" + os.path.basename(inner))
                with open(tmp, "wb") as f:
                    f.write(zf.read(inner))
                out += [(f"{os.path.basename(inner)}::{n}", rows)
                        for n, rows in _sheets(tmp)]
        return out
    if ext in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        return [(ws.title, [list(r) for r in ws.iter_rows(values_only=True)])
                for ws in wb.worksheets]
    if ext == ".xls":
        import xlrd
        book = xlrd.open_workbook(path)
        return [(sh.name, [sh.row_values(i) for i in range(sh.nrows)])
                for sh in book.sheets()]
    raise ValueError(f"지원하지 않는 첨부 형식: {ext}")


def _index_of(text, allow_sector: bool = False):
    """시트 이름·표 제목에서 지수를 알아본다.

    `allow_sector=True`면 "KOSPI 200 Sector(구성종목)"도 코스피200으로 본다.
    섹터지수 11종을 합치면 코스피200 구성종목 전량이고, 실제로 옛 첨부는 전체
    명단을 그 시트에만 담았다. 다만 **편출입에는 쓰지 않는다** — 섹터 사이
    이동은 코스피200 편출입이 아니기 때문이다.
    """
    cleaned = re.sub(r"[\s()\[\]·,~\-_]", "", str(text or ""))
    if allow_sector:
        cleaned = re.sub(r"sector|섹터", "", cleaned, flags=re.I)
    for word in _STRUCTURE_WORDS:
        cleaned = cleaned.replace(word, "")
    for index_id, pattern in INDEX_ALIASES:
        if re.fullmatch(pattern, cleaned, flags=re.I):
            return index_id
    return None


def _code_columns(row):
    """헤더 행에서 '종목코드' 열 번호들을 찾는다."""
    cols = []
    for i, cell in enumerate(row):
        text = re.sub(r"\s+", "", str(cell or ""))
        if text in ("종목코드", "단축코드", "표준코드", "코드", "종목번호"):
            cols.append(i)
    return cols


def _detect_code_columns(rows, min_hits=3):
    """헤더가 없는 옛 양식용 — 종목코드처럼 보이는 셀이 모인 열을 찾는다.

    2009년 이전 첨부에는 '종목코드'라는 머리글이 없다. 열 이름 대신 값의 모양을
    보고 찾되, 우연히 6자리 숫자가 몇 개 있는 열을 잡지 않도록 최소 개수를 건다.
    """
    hits = {}
    for row in rows:
        for i, cell in enumerate(row):
            if isin_to_code(cell):
                hits[i] = hits.get(i, 0) + 1
    return sorted(i for i, n in hits.items() if n >= min_hits)


def _label_for(rows, header_row, col):
    """열 위쪽에서 '편입'/'제외' 같은 구간 이름을 찾는다."""
    # 머리글 행 자체에 '편입 종목코드'처럼 붙어 있는 양식도 있다.
    for r in range(header_row, max(-1, header_row - 4), -1):
        for c in range(col, max(-1, col - 2), -1):
            text = re.sub(r"\s+", "", str(rows[r][c] or "")) if c < len(rows[r]) else ""
            if any(w in text for w in _IN_WORDS + _OUT_WORDS):
                return text
    return ""


def parse_attachment(path):
    """첨부 1개에서 전체 명단과 변경 내역을 뽑는다.

    반환: {"full": {index_id: [코드…]}, "delta": {index_id: {"in": […], "out": […]}},
           "sheets": [진단용 …]}
    """
    result = {"full": {}, "delta": {}, "sheets": [], "path": path}
    for name, rows in _sheets(path):
        title_text = " ".join(str(c or "") for r in rows[:4] for c in r).strip()
        delta_index = _index_of(name) or _index_of(title_text)
        full_index = (_index_of(name, allow_sector=True)
                      or _index_of(title_text, allow_sector=True))
        header_row, cols = None, []
        for i, row in enumerate(rows[:40]):
            if _code_columns(row):
                header_row, cols = i, _code_columns(row)
                break
        if header_row is None:
            # 옛 양식: 머리글이 없다. 값의 모양으로 열을 찾고, 코드가 처음
            # 나오는 행 바로 위를 머리글 자리로 본다.
            cols = _detect_code_columns(rows)
            if not cols:
                result["sheets"].append({"sheet": name, "index": full_index,
                                         "delta_index": delta_index,
                                         "kind": "코드열없음", "rows": 0})
                continue
            first = next((i for i, row in enumerate(rows)
                          if any(c < len(row) and isin_to_code(row[c])
                                 for c in cols)), 0)
            header_row = max(0, first - 1)
        buckets = {}
        for col in cols:
            label = _label_for(rows, header_row, col)
            kind = ("out" if any(w in label for w in _OUT_WORDS)
                    else "in" if any(w in label for w in _IN_WORDS) else "full")
            codes = []
            for row in rows[header_row + 1:]:
                if col >= len(row):
                    continue
                code = isin_to_code(row[col])
                if code:
                    codes.append(code)
            if codes:
                buckets.setdefault(kind, []).extend(codes)

        is_full = (any(w in name for w in _FULL_WORDS)
                   or any(w in title_text for w in _FULL_WORDS))
        for kind, codes in buckets.items():
            uniq = list(dict.fromkeys(codes))
            if kind == "full" and is_full and full_index:
                result["full"].setdefault(full_index, [])
                result["full"][full_index] = list(
                    dict.fromkeys(result["full"][full_index] + uniq))
            elif kind in ("in", "out") and delta_index:
                slot = result["delta"].setdefault(delta_index,
                                                  {"in": [], "out": []})
                slot[kind] = list(dict.fromkeys(slot[kind] + uniq))
        result["sheets"].append({"sheet": name, "index": full_index,
                                 "delta_index": delta_index,
                                 "kind": "full" if is_full else "delta",
                                 "rows": sum(len(v) for v in buckets.values())})
    return result

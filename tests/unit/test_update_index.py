#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`tools/update_index.py` — 유니버스 이력에서 명단을 만드는 경로.

2026-09-23 명단이 코스닥150을 시총 상위로 근사해 실제 구성과 37종목 달랐고,
일봉 수집기가 그 명단으로 대상을 골라 실제 구성종목 39개가 빠졌다. 이 테스트는
이력 경로가 (1) 이력의 구성을 그대로 쓰고 (2) 관측 구간 밖 날짜를 거부하며
(3) 대체 구성(CURRENT_SNAPSHOT_ONLY)을 받아들이지 않는다는 것을 고정한다.
"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

import update_index as ui  # noqa: E402


class FakeRepo:
    def __init__(self, members, start=date(2015, 7, 13), end=date(2026, 9, 22),
                 status="HISTORICAL"):
        self.members, self.start, self.end, self.status = members, start, end, status
        self.asked = None

    def coverage(self, index_id):
        return {"observed_start": self.start, "observed_end": self.end}

    def get_members(self, index_id, as_of):
        self.asked = as_of
        return {"members": self.members, "survivorship_status": self.status,
                "universe_version": "uv_test"}


def test_이력_마지막_관측일_구성을_코드순으로():
    repo = FakeRepo(["KRX:247540", "KRX:009520", "KRX:0009K0"])
    stocks, meta = ui.fetch_from_history("kosdaq150", repo=repo,
                                         names={"KRX:009520": "포스코엠텍"})
    assert [s["code"] for s in stocks] == ["0009K0", "009520", "247540"]
    assert stocks[1]["name"] == "포스코엠텍"
    assert repo.asked == date(2026, 9, 22)
    assert meta["as_of"] == "2026-09-22" and meta["universe_version"] == "uv_test"


def test_이름은_마스터_기존파일_코드_순으로_채운다():
    got = ui.members_to_stocks(["KRX:000001", "KRX:000002", "KRX:000003"],
                               {"KRX:000001": "마스터"}, {"000002": "기존"})
    assert [s["name"] for s in got] == ["마스터", "기존", "000003"]


def test_관측_구간_밖_날짜는_거부():
    repo = FakeRepo(["KRX:005930"])
    stocks, meta = ui.fetch_from_history("kospi200", as_of=date(2026, 9, 25), repo=repo,
                                         names={})
    assert stocks == [] and "관측 구간" in meta["error"]
    assert repo.asked is None


def test_대체_구성은_받아들이지_않는다():
    repo = FakeRepo(["KRX:005930"], status="CURRENT_SNAPSHOT_ONLY")
    stocks, meta = ui.fetch_from_history("kospi200", repo=repo, names={})
    assert stocks == [] and "CURRENT_SNAPSHOT_ONLY" in meta["error"]


def test_이력_실패시_기존_파일을_건드리지_않는다(tmp_path, monkeypatch):
    path = tmp_path / "index_stocks.json"
    path.write_text('{"kospi200": {"count": 1, "stocks": [{"code": "005930"}]}}',
                    encoding="utf-8")
    monkeypatch.setattr(ui, "_INDEX_FILE", str(path))
    monkeypatch.setattr(ui, "fetch_from_history",
                        lambda *a, **k: ([], {"error": "이력 없음"}))
    assert ui.update_index("kospi200", source="history") is False
    assert '"005930"' in path.read_text(encoding="utf-8")

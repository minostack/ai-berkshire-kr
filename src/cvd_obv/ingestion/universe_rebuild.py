# -*- coding: utf-8 -*-
"""과거 구성종목 복원 — 로드맵 Stage 3의 엔진.

바탕이 되는 사실 하나
---------------------
    코스피 200 지수 MKTCAP == 구성종목 상장시가총액의 단순 합   (오차 0원)

2026-09-17에 오늘 201종목으로 확인했고, 섹터지수 11종의 합도 지수 전체와 0원으로
맞는다. 이 가산 구조는 KRX Open API 데이터 시작일(2010-01-04)까지 유지된다.

그래서 이 모듈은 명단을 "추측"하지 않는다. **오늘의 명단에서 하루씩 거꾸로
걸으며 매일 검산한다.** 검산이 0원이면 그날도 같은 명단이었다는 증거고, 깨지면
바로 그날 구성종목이 바뀌었다는 증거다. 변경일을 달력에서 찾아 헤맬 필요가 없다.

바뀐 내용은 세 경로로 메운다.
1. **공시 델타** — KRX 지수운영공지 첨부의 편입·편출 명단(`krx_notices`).
2. **전체 명단 앵커** — 정기변경 첨부에 들어 있는 변경 후 구성종목 전량.
3. **역산** — 1종목 편입·편출·1:1 교체는 차액이 곧 그 종목의 시총이므로
   유일해가 있으면 특정할 수 있다.

어느 경로로 메웠든 **결과는 다시 검산한다.** 0원이 아니면 받아들이지 않고
`UNRESOLVED`로 남긴다. 조용히 넘어가면 그 구간의 유니버스가 통째로 틀린 채
Stage 5~9로 흘러간다.
"""

from __future__ import annotations

import os
from datetime import date

from .krx_openapi import market_for

from .krx_openapi import market_for

__all__ = ["RebuildResult", "UniverseRebuilder", "load_notice_sources",
           "locate_anchors", "FULL_LIST_SIZE", "DELTA_MAX_SIZE"]

#: 이벤트 종류
ANNOUNCED = "ANNOUNCED"        # 공시 델타를 역적용해 검산 통과
ANCHORED = "ANCHORED"          # 첨부의 전체 명단으로 교체해 검산 통과
SOLVED = "SOLVED"              # 항등식 역산으로 특정
NOT_LISTED = "NOT_LISTED"      # 그날 상장 전이라 명단에서 뺌
UNRESOLVED = "UNRESOLVED"      # 못 메움 — 그 구간은 신뢰할 수 없다

#: 전체 명단으로 인정할 종목 수 범위. 같은 파일에 섹터지수 시트(코스피200
#: 섹터 11종을 합치면 200을 넘는다)가 섞여 있어, 개수로 한 번 거른다.
FULL_LIST_SIZE = {"KOSPI200": (185, 215), "KOSDAQ150": (140, 160),
                  "KRX300": (280, 320)}

#: 한 번의 변경으로 바뀔 수 있는 종목 수 상한. 이보다 크면 다른 지수(섹터·
#: 팩터지수)의 시트를 잘못 읽은 것으로 본다. 코스피200 정기변경은 20년 동안
#: 한 번에 20종목을 넘긴 적이 없다(실측 최대 13종목).
DELTA_MAX_SIZE = {"KOSPI200": 30, "KOSDAQ150": 40, "KRX300": 60}


def _ymd(day: date) -> str:
    return day.strftime("%Y%m%d")


class RebuildResult:
    """복원 결과. 구간·이벤트·검산 통계."""

    def __init__(self):
        self.periods = []      # [(시작일, frozenset(코드))] — 오래된 순
        self.events = []
        self.checked = 0
        self.matched = 0
        self.unresolved = 0
        self.unresolved_dates = []     # 검산이 0원이 아니었던 거래일
        self.by_day = {}               # 거래일 → 그날 명단
        self.verified = set()          # 검산 0원으로 확인된 거래일

    def snapshots(self):
        return [(day, sorted(members)) for day, members in self.periods]

    def rebuild_periods(self, days):
        """`by_day`에서 구간을 다시 만든다(양방향 워크를 합친 뒤에 쓴다)."""
        periods, previous = [], None
        for day in days:
            members = self.by_day.get(day)
            if members is None:
                continue
            if previous is None or members != previous:
                periods.append((day, members))
                previous = members
        self.periods = periods
        return periods

    def unresolved_ranges(self):
        """검산이 깨진 날들을 연속 구간으로 묶는다.

        한 날을 못 메우면 그 이전 명단이 전부 어긋난 채로 이어진다. 그래서
        "며칠 실패"가 아니라 **"어느 구간을 믿을 수 없는가"**로 보고한다.
        """
        days = sorted(self.unresolved_dates)
        ranges = []
        for day in days:
            if ranges and (day - ranges[-1][1]).days <= 5:
                ranges[-1][1] = day
            else:
                ranges.append([day, day])
        return [(a.isoformat(), b.isoformat()) for a, b in ranges]

    def summary(self):
        return {
            "trading_days_checked": self.checked,
            "days_matched": self.matched,
            "periods": len(self.periods),
            "events": len(self.events),
            "unresolved": self.unresolved,
            "unresolved_ranges": self.unresolved_ranges(),
            "first_day": (self.periods[0][0].isoformat()
                          if self.periods else None),
            "match_rate": (round(self.matched / self.checked, 6)
                           if self.checked else None),
        }


class UniverseRebuilder:
    """오늘 명단에서 거꾸로 걸으며 과거 구성종목을 복원한다."""

    def __init__(self, api, deltas=None, anchors=None, index_name=None,
                 market="KOSPI200"):
        """
        api     : `KrxOpenApi`
        deltas  : {시행일(date): {"in": [코드], "out": [코드], "source_ref": str}}
        anchors : {시행일(date): {"members": [코드], "source_ref": str}}
        """
        self.api = api
        self.deltas = dict(deltas or {})
        self.anchors = dict(anchors or {})
        # 시장을 빠뜨리면 **코스닥 종목을 유가증권 응답에서 찾게 되어**
        # 명단 중 한 종목도 안 잡히고, 합이 0이라 차액이 영영 안 닫힌다.
        # 그래서 시장과 지수명을 여기서 한 번에 못박는다.
        self.market = market_for(market)
        self.index_name = index_name or self.market.index_name

    # ── 역산 ──────────────────────────────────────────────────────
    def solve_small(self, members, caps, diff, max_side: int = 2,
                    outside_pool: int = 400):
        """차액을 정확히 메우는 **최소·유일** 교체를 찾는다.

        한쪽 최대 2종목까지 본다. 2010년대 특례편입은 두 종목이 같은 날 들어오는
        일이 흔하다(실측: 2021-09-10 카카오뱅크·크래프톤 동시 편입으로 차액이
        56조였고, 1종목 탐색으로는 풀 수 없었다).

        같은 차액을 만드는 조합이 둘 이상이면 **포기한다**. 13자리 정수가 우연히
        맞을 확률은 매우 낮지만, 맞았을 때 조용히 틀린 명단을 심는 것보다
        미해결로 남기는 편이 낫다.

        편입 후보는 시총 상위 `outside_pool`개로 줄인다. 코스피200에 들어오는
        종목이 시장 시총 400위 밖인 경우는 없다.
        """
        if diff == 0:
            return None
        inside = [c for c in members if c in caps]
        outside = sorted((c for c in caps if c not in members),
                         key=lambda c: -caps[c])[:outside_pool]

        def subset_sums(codes, k):
            """부분합 → 조합 목록. 같은 합을 만드는 조합을 **버리지 않는다**.

            여기서 첫 조합만 남기면 모호한 경우를 모호한 줄 모르고 지나간다.
            """
            sums = {0: [()]}
            for i, a in enumerate(codes):
                sums.setdefault(caps[a], []).append((a,))
                if k < 2:
                    continue
                for b in codes[i + 1:]:
                    total = caps[a] + caps[b]
                    bucket = sums.setdefault(total, [])
                    if len(bucket) < 4:
                        bucket.append((a, b))
            return sums

        drop_sums = subset_sums(inside, max_side)
        add_sums = subset_sums(outside, max_side)

        solutions = []
        for s_drop, drop_combos in drop_sums.items():
            add_combos = add_sums.get(diff + s_drop)
            if not add_combos:
                continue
            for combo_drop in drop_combos:
                for combo_add in add_combos:
                    if not combo_add and not combo_drop:
                        continue
                    if set(combo_add) & set(combo_drop):
                        continue
                    solutions.append((len(combo_add) + len(combo_drop),
                                      combo_drop, combo_add))
        if not solutions:
            return None
        best_size = min(s[0] for s in solutions)
        finalists = {(d, a) for size, d, a in solutions if size == best_size}
        if len(finalists) != 1:
            return None            # 모호하면 손대지 않는다
        combo_drop, combo_add = finalists.pop()
        return list(combo_drop), list(combo_add)

    # ── 워크 ──────────────────────────────────────────────────────
    def walk_backward(self, members, days, on_progress=None):
        """`days`(오름차순 거래일)의 마지막 날 명단에서 첫 날까지 거꾸로 간다.

        하루마다 후보를 순서대로 시험하고 **검산이 0원인 것만** 받아들인다.
          1. 그대로 (변경 없음)
          2. 공시 델타 역적용
          3. 첨부의 전체 명단
          4. 역산(1종목 편입·편출·1:1 교체)
          5. 공시 델타 + 역산 (정기변경과 수시변경이 같은 날 겹친 경우)
        모두 실패하면 UNRESOLVED로 남긴다. 억지로 맞추지 않는다.
        """
        if not days:
            raise ValueError("거래일이 비어 있다")
        result = RebuildResult()
        current = set(members)
        cur_day = days[-1]
        # 출발점도 검산한다. 여기만 믿고 넘어가면 씨앗이 틀렸을 때 그 오류가
        # **가장 최근 구간**에 그대로 남는다 — 하필 실거래가 쓰는 구간이다.
        # (실측: 코스닥150 근사 스냅샷이 실제와 37종목 달랐는데, 역행 검산은
        #  전부 통과하고 마지막 하루만 -19.9조로 어긋났다.)
        seed = self.api.checksum(sorted(current), _ymd(cur_day),
                                 index_name=self.index_name,
                                 market=self.market)
        if seed["diff"] is not None:
            result.checked += 1
        if seed["diff"] == 0:
            result.matched += 1
            result.verified.add(cur_day)
        elif seed["diff"] is not None:
            result.unresolved_dates.append(cur_day)
            result.events.append({
                "date": cur_day.isoformat(),
                "effective_date": cur_day.isoformat(),
                "kind": "SEED_UNRESOLVED", "diff": seed["diff"],
                "added_backward": [], "removed_backward": [],
                "note": f"출발 명단이 검산과 어긋난다(차액 {seed['diff']:,})"})
            result.unresolved += 1
        result.by_day[cur_day] = frozenset(current)

        for prev_day in reversed(days[:-1]):
            bas = _ymd(prev_day)
            caps = self.api.stock_caps(bas, self.market)
            target = self.api.index_caps(
                bas, self.market).get(self.index_name)
            if not caps and target is None:
                # 응답 자체가 없는 날(미공표·휴장). 검산 대상이 아니다.
                result.events.append({"date": prev_day.isoformat(),
                                      "effective_date": cur_day.isoformat(),
                                      "kind": "NO_DATA", "diff": 0,
                                      "added_backward": [],
                                      "removed_backward": [],
                                      "members": len(current), "note": ""})
                cur_day = prev_day
                continue
            result.checked += 1
            if target is None:
                result.unresolved += 1
                result.unresolved_dates.append(prev_day)
                result.events.append({"date": prev_day.isoformat(),
                                      "effective_date": cur_day.isoformat(),
                                      "kind": UNRESOLVED, "diff": 0,
                                      "added_backward": [],
                                      "removed_backward": [],
                                      "members": len(current),
                                      "note": "지수 MKTCAP 없음"})
                cur_day = prev_day
                continue

            plain = {c for c in current if c in caps}
            gone = sorted(current - plain)
            delta = self.deltas.get(cur_day)
            anchor = self.anchors.get(prev_day)

            def _diff(codes):
                return target - sum(caps[c] for c in codes)

            attempts = [(plain, [])]
            if delta:
                with_delta = (plain - set(delta["in"])) | {
                    c for c in delta["out"] if c in caps}
                attempts.append((with_delta, [ANNOUNCED]))
            if anchor:
                attempts.append(({c for c in anchor["members"] if c in caps},
                                 [ANCHORED]))
            for base, kinds in list(attempts):
                gap = _diff(base)
                if gap == 0:
                    continue
                solved = self.solve_small(base, caps, gap)
                if solved:
                    drop, add = solved
                    attempts.append(((base - set(drop)) | set(add),
                                     kinds + [SOLVED]))

            chosen, kinds, gap = None, [], None
            for base, base_kinds in attempts:
                if _diff(base) == 0:
                    chosen, kinds, gap = base, list(base_kinds), 0
                    break
            if chosen is None:
                # 검산을 통과하지 못한 후보는 쓰지 않는다. 공시 델타라도
                # 마찬가지다 — 틀린 델타를 적용하면 그 이전 구간이 전부
                # 어긋난 채로 이어지고, 어디서 어긋났는지 알 수 없게 된다.
                chosen = plain
                kinds = [UNRESOLVED]
                gap = _diff(chosen)
                result.unresolved += 1
                result.unresolved_dates.append(prev_day)
            else:
                result.matched += 1
            if gone:
                kinds.insert(0, NOT_LISTED)

            notes = []
            if ANNOUNCED in kinds and delta:
                notes.append(delta.get("source_ref", ""))
            if ANCHORED in kinds and anchor:
                notes.append(anchor.get("source_ref", ""))

            if chosen != current or gap != 0:
                result.events.append({
                    "date": prev_day.isoformat(),
                    "effective_date": cur_day.isoformat(),
                    "kind": "+".join(kinds) if kinds else UNRESOLVED,
                    "added_backward": sorted(chosen - current),
                    "removed_backward": sorted(current - chosen),
                    "members": len(chosen),
                    "diff": int(gap),
                    "note": "; ".join(n for n in notes if n),
                })
            if chosen != current:
                # 지금 명단은 `cur_day`부터 유효했다 — 그 이전 거래일에는 달랐다.
                result.periods.append((cur_day, frozenset(current)))
            current = chosen
            result.by_day[prev_day] = frozenset(current)
            if gap == 0:
                result.verified.add(prev_day)
            cur_day = prev_day
            if on_progress:
                on_progress(prev_day, result)

        # 가장 오래된 명단의 시작일은 관측 구간의 첫 거래일까지만 알 수 있다.
        result.periods.append((days[0], frozenset(current)))
        result.periods.sort(key=lambda p: p[0])
        return result


    def fill_forward(self, result, days):
        """뒤로 걷다 막힌 구간을, 앞(과거)에서부터 다시 채운다.

        역방향 워크는 한 날을 못 풀면 그 이전이 전부 어긋난다. 그런데 막힌 구간의
        **더 과거 쪽 끝**은 다음 앵커에서 이미 복구돼 있는 경우가 많다. 그 지점에서
        앞으로 걸어오면 같은 구간을 반대편에서 메울 수 있다.

        실제로 이렇게 갈라지는 경우가 있다. 정기변경처럼 열 종목이 한꺼번에 바뀌는
        날은 역산이 못 풀지만, 그 하루만 앵커(변경 후 전체 명단)가 있으면 반대
        방향에서는 그 하루가 오히려 가장 쉬운 날이 된다.
        """
        order = {day: i for i, day in enumerate(days)}
        filled = []
        for lo_s, hi_s in result.unresolved_ranges():
            lo = _parse_iso(lo_s)
            hi = _parse_iso(hi_s)
            i_lo, i_hi = order.get(lo), order.get(hi)
            if i_lo is None or i_hi is None:
                continue

            start = None
            if i_lo > 0 and days[i_lo - 1] in result.verified:
                start = i_lo - 1
                current = set(result.by_day[days[i_lo - 1]])
            else:
                seeded = self._anchor_membership(days[i_lo])
                if seeded is not None:
                    start, current = i_lo, seeded
            if start is None:
                continue

            for i in range(start + 1, i_hi + 1):
                day = days[i]
                caps = self.api.stock_caps(_ymd(day), self.market)
                target = self.api.index_caps(
                    _ymd(day), self.market).get(self.index_name)
                if target is None or not caps:
                    break
                plain = {c for c in current if c in caps}
                attempts = [plain]
                delta = self.deltas.get(day)
                if delta:
                    attempts.append((plain - set(delta["out"]))
                                    | {c for c in delta["in"] if c in caps})
                anchor_set = self._anchor_membership(day, caps=caps, target=target)
                if anchor_set is not None:
                    attempts.append(anchor_set)
                for base in list(attempts):
                    gap = target - sum(caps[c] for c in base)
                    if gap == 0:
                        continue
                    fix = self.solve_small(base, caps, gap)
                    if fix:
                        drop, add = fix
                        attempts.append((base - set(drop)) | set(add))

                chosen = next((b for b in attempts
                               if target - sum(caps[c] for c in b) == 0), None)
                if chosen is None:
                    break
                current = chosen
                result.by_day[day] = frozenset(current)
                if day not in result.verified:
                    result.verified.add(day)
                    result.matched += 1
                    result.unresolved -= 1
                    if day in result.unresolved_dates:
                        result.unresolved_dates.remove(day)
                    filled.append(day)
        if filled:
            result.rebuild_periods(days)
        return filled

    def _anchor_membership(self, day, caps=None, target=None):
        """그날 앵커가 있고 검산을 통과하면 그 명단을 돌려준다."""
        anchor = self.anchors.get(day)
        if not anchor:
            return None
        bas = _ymd(day)
        caps = (caps if caps is not None
                else self.api.stock_caps(bas, self.market))
        if target is None:
            target = self.api.index_caps(
                bas, self.market).get(self.index_name)
        if target is None or not caps:
            return None
        base = {c for c in anchor["members"] if c in caps}
        gap = target - sum(caps[c] for c in base)
        if gap == 0:
            return base
        fix = self.solve_small(base, caps, gap)
        if not fix:
            return None
        drop, add = fix
        patched = (base - set(drop)) | set(add)
        if target - sum(caps[c] for c in patched) != 0:
            return None
        return patched


def _parse_iso(text):
    year, month, day = (int(x) for x in text.split("-"))
    return date(year, month, day)


def load_notice_sources(notices, trading_days, index_id: str = "KOSPI200"):
    """수집해 둔 공지 목록에서 델타·앵커를 만든다.

    시행일을 정하지 못한 첨부는 버리지 않고 `skipped`로 돌려준다. 날짜를 임의로
    찍어 넣으면 구간 경계가 조용히 틀어지고, 그 사실을 나중에 알 방법이 없다.
    """
    from .krx_index_files import parse_attachment
    from .krx_notices import parse_effective_dates

    day_set = set(trading_days)
    deltas, anchors, skipped = {}, {}, []
    for notice in notices:
        for att in notice.get("attachments", []):
            path = att.get("path")
            if not path or not os.path.exists(path):
                # 내려받지 못한 첨부. 수집 단계에서 이미 사유를 남겼다.
                continue
            try:
                parsed = parse_attachment(path)
            except Exception as exc:                      # noqa: BLE001
                skipped.append({"seq": notice["seq"], "file": att.get("name"),
                                "reason": f"파싱 실패: {exc}"})
                continue
            full = parsed["full"].get(index_id)
            delta = parsed["delta"].get(index_id)
            if not (full or delta):
                continue
            parsed_days = parse_effective_dates(notice["body"],
                                                 notice["posted_date"])
            hits = [d for d in parsed_days if d in day_set]
            if not hits:
                # 관측 구간보다 앞선 공지라도 **전체 명단**은 쓸 수 있다.
                # 그 사이 변경이 없었다면 구간 첫날에도 그대로 맞고, 몇 종목
                # 바뀌었다면 앵커 탐색이 역산으로 메운다. 어느 쪽이든 검산이
                # 최종 판정을 한다. 편출입은 시행일을 모르면 쓸 수 없다.
                older = [d for d in parsed_days if trading_days and d < trading_days[0]]
                if full and older and (trading_days[0] - max(older)).days <= 400:
                    eff = trading_days[0]
                    delta = None
                else:
                    skipped.append({"seq": notice["seq"],
                                    "file": att.get("name"),
                                    "reason": "시행일을 특정하지 못했다"})
                    continue
            else:
                eff = hits[0]
            ref = (f"KRX공지#{notice['seq']}({notice['posted_date']}) "
                   f"{att.get('name', '')}").strip()
            cap = DELTA_MAX_SIZE.get(index_id, 10 ** 6)
            if delta and max(len(delta["in"]), len(delta["out"])) > cap:
                skipped.append({"seq": notice["seq"], "file": att.get("name"),
                                "reason": f"변경 종목 수가 과하다("
                                          f"{len(delta['in'])}/{len(delta['out'])})"})
                delta = None
            if delta and (delta["in"] or delta["out"]):
                # 같은 날 정기변경과 수시변경이 겹칠 수 있다. 덮어쓰면 한쪽을
                # 잃으므로 합친다.
                slot = deltas.setdefault(eff, {"in": [], "out": [],
                                               "source_ref": ""})
                slot["in"] = list(dict.fromkeys(slot["in"] + delta["in"]))
                slot["out"] = list(dict.fromkeys(slot["out"] + delta["out"]))
                slot["source_ref"] = "; ".join(
                    x for x in (slot["source_ref"], ref) if x)
            low, high = FULL_LIST_SIZE.get(index_id, (1, 10 ** 6))
            if full and low <= len(full) <= high:
                keep = anchors.get(eff)
                if not keep or len(keep["members"]) < len(full):
                    anchors[eff] = {"members": full, "source_ref": ref}
    return deltas, anchors, skipped


def locate_anchors(api, anchors, days, window: int = 5,
                   index_name: str = None, solver=None,
                   market="KOSPI200"):
    """앵커의 시행일을 **검산으로** 확정한다.

    공지 본문의 날짜 표기는 20년 동안 제각각이라, 파싱한 날짜가 하루이틀
    어긋나거나 공지 작성일을 집는 경우가 있다. 그런데 시총 항등식이 있으므로
    날짜를 추측할 필요가 없다. 파싱한 날짜 주변을 훑어 **차액이 0원이 되는 가장
    이른 거래일**을 찾으면 그게 시행일이다.

    첨부 명단이 **한두 종목 모자란** 경우가 있다. 정기변경 명단을 공지한 뒤
    시행일 사이에 특례편입이 끼면 그렇다(실측: 2018-06-15 HDC현대산업개발
    294870이 분할 상장 직후 특례편입돼 실제 구성은 201종목이었다). 그래서
    "정확히 0원" 외에 "1~2종목 역산으로 0원"까지 시행일로 인정한다. 역산 자체가
    유일해를 요구하므로 느슨해지는 것은 아니다.

    한 날도 맞지 않으면 그 앵커는 버린다(`dropped`로 돌려준다). 명단이 틀렸거나
    다른 지수의 시트를 읽은 것이므로, 억지로 쓰면 구간이 통째로 오염된다.
    """
    mk = market_for(market)
    index_name = index_name or mk.index_name
    solve = (solver or UniverseRebuilder(
        api, index_name=index_name, market=mk)).solve_small
    order = {day: i for i, day in enumerate(days)}
    located, dropped = {}, []
    for parsed, anchor in sorted(anchors.items()):
        at = order.get(parsed)
        if at is None:
            dropped.append({"parsed_date": parsed.isoformat(),
                            "reason": "거래일이 아니다",
                            "source_ref": anchor.get("source_ref", "")})
            continue
        found, fixed = None, None
        lo = max(0, at - window)
        hi = min(len(days) - 1, at + window)
        window_days = days[lo:hi + 1]
        # 첨부 명단이 몇 종목 어긋나도 되는 한도. 200종목짜리 명단에서 5종목이다.
        patch_budget = len(anchor["members"]) // 40

        def _state(day):
            bas = _ymd(day)
            caps = api.stock_caps(bas, mk)
            target = api.index_caps(bas, mk).get(index_name)
            if target is None or not caps:
                return None, None, None
            members = [c for c in anchor["members"] if c in caps]
            return caps, target, target - sum(caps[c] for c in members)

        # 1차: 오차 0원인 날. 정확히 맞는 날이 있으면 보정한 날보다 우선한다.
        for day in window_days:
            caps, target, gap = _state(day)
            if caps is not None and gap == 0:
                found = day
                break

        # 2차: 1~몇 종목 역산으로 0원이 되는 날. 공지 뒤 특례편입이 끼면 그렇다.
        if found is None and patch_budget:
            for day in window_days:
                caps, target, gap = _state(day)
                if caps is None or gap == 0:
                    continue
                fix = solve(set(c for c in anchor["members"] if c in caps),
                            caps, gap)
                if not fix:
                    continue
                drop, add = fix
                if len(drop) + len(add) > patch_budget:
                    continue
                found, fixed = day, {"drop": drop, "add": add}
                break

        if found is None:
            dropped.append({"parsed_date": parsed.isoformat(),
                            "reason": f"±{window}거래일 안에 검산이 맞는 날이 없다",
                            "members": len(anchor["members"]),
                            "source_ref": anchor.get("source_ref", "")})
            continue

        record = dict(anchor)
        record["parsed_date"] = parsed.isoformat()
        record["located_by"] = "CHECKSUM" if not fixed else "CHECKSUM+SOLVED"
        if fixed:
            record["patch"] = fixed
        located[found] = record
    return located, dropped

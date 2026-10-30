# -*- coding: utf-8 -*-
"""KIS 일별 매수/매도 체결량 적재기 — FHKST03010800.

왜 이 테이블이 필요한가
----------------------
현재 CVD는 분봉 캔들 모양에서 방향을 **추정**한다
(`direction = clamp(0.6·CLV + 0.4·Body, -1, 1)`). 이 API는 공급자가 이미
매수·매도로 갈라 준 체결량을 준다. 실측 대조(2026-10-19)에서

  · 일별 흐름 상관 +0.6321 (R² 0.40), 부호 일치 72.4%
  · 흐름이 양수인 날 비율 — 추정 54.0% vs 체결량 39.5% (**추정이 양수로 치우침**)
  · 가속 A > 0 비율 — 추정 48.9% vs 체결량 50.1% (**차분에서는 편향이 사라진다**)
  · 이력 2010-05-24~ (약 16.4년). 저장 1분봉은 268거래일뿐이다

근거: `reports/KIS-체결량-일별CVD-대비검토-20261019.md`

실측으로 확인한 공급자 성질 (2026-10-19) — 둘 다 중요하다
----------------------------------------------------------
1. **수량은 원시(raw) 주식수다.** `silver.daily_bars`의 거래량은
   SPLIT_ADJUSTED인데 이 수량은 조정되지 않는다. 005930 액면분할(2018-05,
   50:1) 직전 거래일에서 `(buy+sell) / 일봉 거래량 = 0.0157 ≈ 1/50`이다.
   전 구간 2,414,363쌍에서 비율 5~95%가 [0.197, 1.879]로 벌어지고, 분할·병합
   종목 수백 개가 거의 모든 날에서 0.5 미만 또는 1.5 초과다.
   → **수량을 거래량과 비교하거나 거래량으로 쓰면 안 된다.**
   → `f = (buy−sell)/(buy+sell)`은 **같은 날 비율**이라 조정에 영향받지 않는다.
   → 창 가중 평균에 `(buy+sell)`을 가중치로 쓸 때, 창 안에 분할이 들어오면
     앞뒤 배율이 달라진다(종목당 10년에 한 번 수준, 영향 작음 — 그러나 사실).
2. **분해가 호출마다 조금씩 다르다.** 같은 (종목, 날)을 같은 파라미터로 5번
   불러 **3가지 (매수, 매도) 조합**이 나왔다. **합계는 항상 같았다**
   (005930 2026-10-16 = 16,284,669 고정, 매수 8,730,007~8,742,340).
   흐름으로 환산하면 f = +0.0722~+0.0737, 폭 0.0015다(f 표준편차 0.168의
   약 0.9%). 게이트 판정을 뒤집을 크기는 아니지만 **재현이 정확하지 않다** —
   같은 날을 다시 받으면 값이 미세하게 달라질 수 있다.

적재 규칙
--------
- **당일 행은 쓰지 않는다.** 장중에는 누적 중간값이 온다(실측). 정규장 완료
  판정(`bar_standard.classify_fetched_daily`)과 같은 사고로, 안전하게 어제까지만 쓴다
- 한 호출 최대 100건이므로 구간을 뒤로 쪼개 페이지한다(응답은 최신→과거 정렬)
- 두 수가 모두 0인 행은 **버리지 않고** `TRADE_SIDE_BOTH_ZERO`로 표시한다.
  공급자 이력 시작 전을 "수집 실패"와 구별하기 위함이다
- `market_div`는 섞지 않는다. 기본 `J`(KRX)

일일 적재 (2026-10-20, `plan_daily` · `cvdobv.py trade-sides daily`)
-------------------------------------------------------------------
`data-load`의 두 작업 끝에서 돈다. **2026-10-20부터 기본 수집 구분은 `UN`(KRX+NXT
통합)이다** — NXT 거래가 무시할 수준이 아니어서다(삼성전자 10/12~10/19 통합의 약 44%).
`J`·`NX`는 더 이상 매일 받지 않는다(이전 행은 남는다. `--market-div J,NX`로 받을 수는
있다). 아래 J 설명은 `UN`에도 그대로 적용된다 — `UN`은 `J`(애프터마켓 포함)에 NXT를
더한 값이고, NXT도 20:00에 끝나므로 16:10 값은 잠정이다.

- **16:10** — `UN` **오늘** 행을 **잠정**으로 쓴다(리비전 0, `TRADE_SIDE_PROVISIONAL`).
  아래 실측은 `J` 시절 근거다 — `UN`에 들어 있는 `J`도 같다.
  `J` 체결량에는 KRX 애프터마켓(16:00~20:00)이 들어 있어 16:10 값은 확정이 아니다
  (2026-10-20 실측: 애프터마켓 거래가 큰 종목·날에서 `J` 합계가 정규장 거래량이 아니라
  EXTENDED 거래량을 따라간다 — EXT/정규장 5.44일 때 J합/정규장 5.39)
- **익일 08:30** — `NX`(NXT) 전날 행과 `J` 전날 **확정** 행(리비전 1)을 쓴다. NXT는
  20:00에 끝나므로 그 전에는 받지 않는다
- 두 작업 모두 최근 30일 거래일 중 확정 행이 지수 구성종목의 90% 미만인 날부터 다시
  받는다 — 작업을 놓친 날을 메운다. 확정 행은 리비전 1이라 잠정 행(0)과 예전 행(0)을
  run_id 순서와 무관하게 이긴다
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta, timezone

from .. import SCHEMA_VERSION
from ..contracts.market_schema import (
    DAILY_TRADE_SIDE_COLUMNS, DAILY_TRADE_SIDE_PK, TRADE_SIDE_ZERO_FLAG,
)
from ..domain.errors import ConfigError
from ..storage.parquet_store import ParquetStore

__all__ = ["KIS_SOURCE", "TradeSidesLoader", "parse_rows", "read_trade_sides",
           "FLAG_PROVISIONAL", "REVISION_PROVISIONAL", "REVISION_FINAL",
           "NXT_START", "DAILY_MARKETS", "PROVISIONAL_MARKETS", "plan_daily"]

KIS_SOURCE = "KIS_FHKST03010800"
_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-trade-volume"
_TR = "FHKST03010800"
#: 공급자 이력 시작 — 005930 실측(2010-01~05-23은 0, 2010-05-24부터 값이 있다)
HISTORY_START = date(2010, 5, 24)
#: 한 호출 최대 건수(공식 문구 "최대 100건", 실측 확인)
PAGE_MAX = 100
#: 넥스트레이드 개시일 — 이 전에는 NX 수량이 존재하지 않는다
NXT_START = date(2025, 3, 4)
#: 16:10에 받은 J 당일 행 — 애프터마켓이 덜 들어 있다
FLAG_PROVISIONAL = "TRADE_SIDE_PROVISIONAL"
REVISION_PROVISIONAL = 0
#: 일일 적재의 확정 행. 예전 일괄 수집 행은 리비전 0이다(확정값이지만 표시는 없다)
REVISION_FINAL = 1
#: 하루를 "확정했다"고 보는 비율 — `load_state.MIN_CONFIRMED_RATIO`와 같은 생각
_MIN_DONE_RATIO = 0.9


def _kis():
    """`tools/daily_scanner.py`의 KIS 호출 헬퍼를 재사용한다 — 토큰 캐시가 하나여야
    한다. 같은 자격증명으로 토큰을 두 군데서 발급하면 서로를 무효화할 수 있다."""
    import sys
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    tools = os.path.join(root, "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import daily_scanner as dsc
    if not os.environ.get("KIS_APP_KEY") or not os.environ.get("KIS_APP_SECRET"):
        raise ConfigError("KIS_APP_KEY / KIS_APP_SECRET가 없다 — .env 확인")
    return dsc


def parse_rows(payload, code: str, market_div: str, run_id: str,
               now: datetime = None, stats: dict = None) -> list:
    """API output2 → 테이블 행.

    응답에는 **빈 행(패딩)** 이 섞인다 — `stck_bsop_date`가 `""` 또는 `"0"`이고
    수량도 0이다(실측 2026-10-19, 000030 등 100건을 채우려고 들어온다). 이런
    행은 날짜가 없어 저장할 수 없으므로 건너뛰되, `stats["padding"]`에 세어
    **조용히 사라지지 않게** 한다. 날짜 모양인데 파싱이 안 되는 값은 예외다 —
    공급자 형식이 바뀐 신호이므로 삼키면 안 된다.
    """
    now = now or datetime.now(timezone.utc)
    out = []
    for item in payload or []:
        s = str(item.get("stck_bsop_date", "")).strip()
        buy = int(item.get("total_shnu_qty") or 0)
        sell = int(item.get("total_seln_qty") or 0)
        blank_date = (not s) or set(s) == {"0"}
        if blank_date and buy == 0 and sell == 0:
            # 100건을 채우려 들어온 빈 행 — 세어서 드러내고 건너뛴다
            if stats is not None:
                stats["padding"] = stats.get("padding", 0) + 1
            continue
        if len(s) != 8 or not s.isdigit():
            # 날짜가 없는데 수량이 있으면 **시간에 배치할 수 없는 데이터**다.
            # 건너뛰면 조용한 손실이므로 멈춘다.
            raise ValueError(
                f"{code}: 거래일 형식이 아니다 — {s!r} "
                f"(매수 {buy:,} 매도 {sell:,})")
        d = date(int(s[:4]), int(s[4:6]), int(s[6:]))
        flags = [TRADE_SIDE_ZERO_FLAG] if (buy == 0 and sell == 0) else []
        out.append({
            "security_id": f"KRX:{code}",
            "trading_date": d,
            "buy_qty": buy,
            "sell_qty": sell,
            "market_div": market_div,
            "available_at_utc": now,
            "source": KIS_SOURCE,
            "quality_status": "PARTIAL" if flags else "SUCCESS",
            "quality_flags": flags,
            "run_id": run_id,
            "schema_version": SCHEMA_VERSION,
            "data_revision": 0,
        })
    return out


def read_trade_sides(store: ParquetStore = None, columns=None, filters=None):
    """리비전이 여럿이면 최신 하나만 돌려준다 — 직접 읽지 말고 이 함수를 쓴다."""
    from ..contracts.market_schema import DAILY_TRADE_SIDE_LOGICAL_KEY
    store = store or ParquetStore()
    cols = list(columns) if columns else [
        "security_id", "trading_date", "buy_qty", "sell_qty",
        "market_div", "quality_status"]
    need = list(dict.fromkeys(cols + list(DAILY_TRADE_SIDE_LOGICAL_KEY)
                              + ["data_revision"]))
    return store.read_arrow("silver", "daily_trade_sides", columns=need,
                            filters=filters,
                            latest_by=DAILY_TRADE_SIDE_LOGICAL_KEY)


class TradeSidesLoader:
    """종목별로 구간을 페이지하며 모아 `silver.daily_trade_sides`에 쓴다."""

    def __init__(self, store: ParquetStore = None, market_div: str = "J",
                 sleep: float = 0.05, retries: int = 3):
        self.store = store or ParquetStore()
        self.market_div = market_div
        self.sleep = sleep
        self.retries = retries
        self.calls = 0
        self.fails = 0
        # run()이 덮어쓴다. fetch_security를 단독으로 부를 때를 위한 기본값.
        self.run_id = f"kis_sides_adhoc_{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
        self.stats = {}

    # ── API ──
    def _call(self, code: str, d1: date, d2: date):
        dsc = _kis()
        params = {
            "FID_COND_MRKT_DIV_CODE": self.market_div,
            "FID_INPUT_ISCD": code,
            "FID_PERIOD_DIV_CODE": "D",
            "FID_INPUT_DATE_1": d1.strftime("%Y%m%d"),
            "FID_INPUT_DATE_2": d2.strftime("%Y%m%d"),
        }
        for attempt in range(self.retries):
            res = dsc._kis_request(_PATH, params, _TR)
            self.calls += 1
            if res and res.get("rt_cd") == "0":
                return res.get("output2") or []
            time.sleep(0.4 * (attempt + 1))
        self.fails += 1
        return None

    def fetch_security(self, code: str, start: date, end: date) -> list:
        """한 종목의 [start, end] 전부. 100건 한도를 뒤로 쪼개 페이지한다."""
        rows, cur_end, guard = [], end, 0
        while cur_end >= start and guard < 400:
            guard += 1
            payload = self._call(code, start, cur_end)
            if payload is None:
                break
            if not payload:
                break
            parsed = parse_rows(payload, code, self.market_div,
                                self.run_id, stats=self.stats)
            rows.extend(parsed)
            if not parsed:
                # 전부 패딩 — 더 과거로 갈 근거가 없다
                break
            oldest = min(r["trading_date"] for r in parsed)
            if len(payload) < PAGE_MAX or oldest <= start:
                break
            cur_end = oldest - timedelta(days=1)
            time.sleep(self.sleep)
        # 같은 (종목, 날)이 두 번 들어오면 뒤의 것을 버린다(페이지 경계 중복)
        seen, dedup = set(), []
        for r in rows:
            key = (r["security_id"], r["trading_date"])
            if key in seen:
                continue
            seen.add(key)
            dedup.append(r)
        return dedup

    # ── 적재 ──
    def run(self, codes, start: date = None, end: date = None,
            run_id: str = None, flush_every: int = 40, log=print,
            provisional_from: date = None, final_revision: int = None) -> dict:
        """`provisional_from` 이후 행은 잠정(리비전 0, 표시), 그 앞은
        `final_revision`(기본: 행이 가진 0 그대로 — 예전 일괄 수집과 같다)."""
        start = start or HISTORY_START
        # 당일은 장중 누적값이므로 쓰지 않는다(일일 적재는 잠정으로 명시해 쓴다)
        end = end or (date.today() - timedelta(days=1))
        if end < start:
            raise ConfigError(f"end({end}) < start({start})")
        self.run_id = run_id or (
            f"kis_sides_{datetime.now(timezone.utc):%Y%m%d%H%M%S}")

        total, written, buf, part = 0, 0, [], 0
        empty, zero_only = [], []
        t0 = time.monotonic()
        for i, code in enumerate(codes, 1):
            rows = self.fetch_security(code, start, end)
            rows = [r for r in rows if start <= r["trading_date"] <= end]
            for r in rows:
                if provisional_from is not None and r["trading_date"] >= provisional_from:
                    r["data_revision"] = REVISION_PROVISIONAL
                    r["quality_flags"] = sorted(set(r["quality_flags"]) | {FLAG_PROVISIONAL})
                    r["quality_status"] = "PARTIAL"
                elif final_revision is not None:
                    r["data_revision"] = final_revision
            if not rows:
                empty.append(code)
            elif all(TRADE_SIDE_ZERO_FLAG in r["quality_flags"] for r in rows):
                zero_only.append(code)
            buf.extend(rows)
            total += len(rows)
            if len(buf) >= flush_every * 1000:
                part += 1
                self._flush(buf, part)
                written += len(buf)
                buf = []
            if i % 25 == 0 or i == len(codes):
                el = time.monotonic() - t0
                eta = (f" · 남은 추정 {(len(codes) - i) / (i / el) / 60:.0f}분"
                       if el > 0 and i < len(codes) else "")
                log(f"  {i}/{len(codes)}종목 · 행 {total:,} · 호출 {self.calls:,}"
                    f" · 실패 {self.fails} · 경과 {el/60:.1f}분{eta}")
        if buf:
            part += 1
            self._flush(buf, part)
            written += len(buf)

        return {"run_id": self.run_id, "securities": len(codes),
                "padding_rows": self.stats.get("padding", 0),
                "rows": written, "calls": self.calls, "failed_calls": self.fails,
                "period": [str(start), str(end)],
                "provisional_from": str(provisional_from) if provisional_from else None,
                "market_div": self.market_div,
                "empty_securities": empty, "zero_only_securities": zero_only,
                "elapsed_sec": round(time.monotonic() - t0, 1)}

    def _flush(self, rows, part: int):
        self.store.write(
            "silver", "daily_trade_sides", rows,
            DAILY_TRADE_SIDE_COLUMNS, DAILY_TRADE_SIDE_PK,
            run_id=self.run_id,
            partition_fn=lambda r: [("year", r["trading_date"].year)],
            # 시장 구분을 넣는다 — 같은 run_id로 J·NX를 이어 쓰면 같은 파일 이름이 되어
            # 뒤의 것이 앞의 것을 덮어쓴다(2026-10-20 실측: J 10/19 행이 사라졌다)
            part_key=f"{self.market_div}-p{part:03d}",
            extra_manifest={"source": KIS_SOURCE, "tr_id": _TR,
                            "market_div": self.market_div,
                            "note": ("매수+매도는 정규장 거래량보다 작다 — "
                                     "거래량으로 쓰지 말 것")})


def _done_days(store, codes, market_div, start, end):
    """({날짜: 확정 행이 있는 종목 수}, 행이 한 번이라도 있는 종목) — 잠정 표시가 없는
    최신 리비전."""
    import pyarrow.dataset as ds
    from ..domain.errors import NoInputDataError

    sids = {f"KRX:{c}" for c in codes}
    try:
        t = read_trade_sides(
            store, columns=["security_id", "trading_date", "market_div",
                            "quality_flags"],
            filters=(ds.field("market_div") == market_div)
            & (ds.field("trading_date") >= start)
            & (ds.field("trading_date") <= end))
    except NoInputDataError:
        return {}, set()
    out, seen = {}, set()
    for sid, d, flags in zip(t.column("security_id").to_pylist(),
                             t.column("trading_date").to_pylist(),
                             t.column("quality_flags").to_pylist()):
        if sid not in sids:
            continue
        seen.add(sid)
        if FLAG_PROVISIONAL not in (flags or []):
            out[d] = out.get(d, 0) + 1
    return out, seen


#: 매일 받는 구분(2026-10-20부터 UN). 잠정(오늘) 행을 받는 구분도 UN 하나다 — 예전에
#: J가 함께 있었지만 매일 받는 구분이 UN뿐이라 쓰이지 않았다(2026-10-23 정리).
#: `--market-div J`로 직접 받으면 J는 확정 행만 받는다
DAILY_MARKETS = ("UN",)
PROVISIONAL_MARKETS = ("UN",)


def plan_daily(codes, now: datetime, store: ParquetStore = None,
               since: date = None, window_days: int = 30, registry=None,
               markets=DAILY_MARKETS) -> list:
    """일일 적재 계획 — [{market_div, start, end, provisional_from}].

    - 확정 대상: `since`(기본 30일 전) ~ 확정 시점(20:05 전이면 어제) 거래일 중 확정 행이
      90% 미만인 첫 날부터. `NX`는 넥스트레이드 개시 전 날을 보지 않는다
    - 잠정(`UN`): 오늘 정규장이 끝났고(15:35) 아직 확정 전(20:05)이며 저장소가
      오늘을 거래일로 알 때(1분봉 catchup이 먼저 돈다) — 오늘 행을 잠정으로
    - `UN`은 지수 구성종목 전부가 대상이다(NXT 미거래 종목은 UN = J)
    """
    from ..domain.ids import KST
    from ..domain.market_rules import default_registry
    from ..domain.status import Session
    from .load_state import trading_days

    store = store or ParquetStore()
    registry = registry or default_registry()
    now = now if now.tzinfo else now.replace(tzinfo=KST)
    today = now.astimezone(KST).date()
    rule = registry.resolve("KRX", today)
    confirmed_today = now >= rule.daily_confirmed_at(today)
    final_end = today if confirmed_today else today - timedelta(days=1)
    window_start = since or (today - timedelta(days=window_days))
    days = trading_days(store, window_start, final_end)

    plan = []
    for md in markets:
        lo = max(window_start, NXT_START) if md == "NX" else window_start
        base_lo = lo - timedelta(days=90)
        if md == "NX":
            base_lo = max(base_lo, NXT_START)
        done, seen = _done_days(store, codes, md, base_lo, final_end)
        # NXT는 지수 구성종목 일부만 거래한다(2026-10 실측 351개 중 299개). 기준은
        # 최근 90일 동안 그 구분 행이 있었던 종목 수다. 하나도 없으면 전체 종목.
        need = (len(seen) if (md == "NX" and seen) else len(codes)) * _MIN_DONE_RATIO
        undone = [d for d in days if d >= lo and done.get(d, 0) < need]
        item = {"market_div": md, "start": min(undone) if undone else None,
                "end": final_end if undone else None, "provisional_from": None,
                "undone_days": [str(d) for d in undone]}
        if md in PROVISIONAL_MARKETS and not confirmed_today:
            regular_done = rule.session_complete_at(today, Session.REGULAR)
            if (regular_done is not None and now >= regular_done
                    and trading_days(store, today, today)):
                item["start"] = item["start"] or today
                item["end"] = today
                item["provisional_from"] = today
        if item["start"] is not None:
            plan.append(item)
    return plan

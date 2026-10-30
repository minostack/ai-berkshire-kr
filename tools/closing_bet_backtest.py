#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
종가매수 스캐너 A·B·C형 백테스트 — 탐색용, 사전등록 아님

규칙
  신호   : 기준일 D에 `closing_bet_scanner.judge`가 A·B·C 중 하나라도 통과
           (D의 완료 봉까지만 사용 — 스캐너와 같은 판정·판정 가능 규칙을 그대로 호출.
            중간 거래일 누락·정규장 복원 불가 봉이 창에 있으면 그날 그 종목은 판정 불가)
  가격   : 2026-09-14 이후 봉은 1분봉 정규장으로 복원한 15:30 종가를 쓴다. 복원 못 한
           봉이 있는 첫 날 전날에서 기간을 끝낸다(--allow-extended로 해제)
  유니버스: D 시점의 실제 KOSPI200·KOSDAQ150 구성종목 (silver.universe_membership)
  진입   : D 종가 (15:30 확정 종가 → 15:40 시간외 종가매매로 체결 가정)
  청산   : D+1부터 매일 종가 < MA5 인 첫 날, 그날 종가(15:40 시간외 종가)로 매도
           기간 끝까지 청산되지 않으면 마지막 종가로 평가(미청산 표시)
           종목 일봉이 기간 중 끊기면(상장폐지 등) 마지막 봉 종가로 강제 청산
  비용   : configs/costs/krx_v1.json — 수수료 0.015%(양방향), 슬리피지 0.1%(양방향),
           매도세 0.18% → 왕복 약 0.41%

포트폴리오 시뮬레이션
  초기 1,000만 원, 최대 5종목, 종목당 "그날 평가금액 × 20%"(현금 한도 내)
  같은 날 15:40에 매도 → 매수 순서(매도 대금 즉시 재사용)
  빈 자리보다 신호가 많으면 20일 평균 거래대금 내림차순(스캐너 정렬)으로 상위부터
  이미 보유 중인 종목의 새 신호는 무시, 주식 수는 정수(1주 미만 버림)

사용법
    python tools/closing_bet_backtest.py                       # 최근 1년(로컬 일봉 최신일 기준)
    python tools/closing_bet_backtest.py --start 2025-09-23 --end 2026-09-23
    python tools/closing_bet_backtest.py --capital 10000000 --slots 5
"""

import argparse
import json
import math
import os
import sys
from datetime import date, datetime, timedelta

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))
sys.path.insert(0, os.path.join(_BASE, "tools"))

import closing_bet_scanner as scan  # noqa: E402

TYPES = ("A", "B", "C")
EXT_CUT_SHARE = 0.05    # 정규장 복원 불가 봉이 이 비율을 넘는 날부터 기간 제외
WARMUP_CAL_DAYS = 160   # 시작일 이전 워밍업 캘린더일
COST_FILE = os.path.join(_BASE, "configs", "costs", "krx_v1.json")


# ─────────────────────────────────────────────────────────────
# 데이터
# ─────────────────────────────────────────────────────────────

def load_intervals(indexes=("KOSPI200", "KOSDAQ150")) -> dict:
    """{security_id: [(지수, 시작, 종료|None) …]} — 유니버스 이력만(연구용,
    `cvd_obv.universe.flat_intervals`)."""
    from cvd_obv.universe import flat_intervals
    return flat_intervals(indexes)


def member_index(ivs, d: date):
    from cvd_obv.universe import member_index as _member_index
    return _member_index(ivs, d)


def load_benchmark(bid: str, start: date, end: date) -> dict:
    import pyarrow.dataset as ds
    from cvd_obv.storage.parquet_store import ParquetStore
    t = ParquetStore().read_arrow(
        "silver", "benchmark_bars", columns=["benchmark_id", "trading_date", "close"],
        filters=(ds.field("trading_date") >= start) & (ds.field("trading_date") <= end))
    out = {}
    for b, d, c in zip(*(t.column(c).to_pylist() for c in
                         ("benchmark_id", "trading_date", "close"))):
        if b == bid:
            out[d] = float(c)
    return out


# ─────────────────────────────────────────────────────────────
# 신호
# ─────────────────────────────────────────────────────────────

def generate_signals(bars: dict, intervals: dict, calendar: list, names: dict,
                     cal_index: dict, allow_extended: bool = False, excluded=None) -> dict:
    """{날짜: [신호 …]} — 날짜별로 그날 구성종목만 판정.

    cal_index는 전체 로드 구간의 거래일 달력(워밍업 포함)이어야 한다.
    excluded(Counter)를 주면 판정 불가 사유를 (구성종목·일) 단위로 센다.
    """
    pos = {c: {b["date"]: i for i, b in enumerate(bs)} for c, bs in bars.items()}
    out = {}
    for n, d in enumerate(calendar, 1):
        day = []
        for code, bs in bars.items():
            i = pos[code].get(d)
            if i is None:
                continue
            idx = member_index(intervals.get(f"KRX:{code}", ()), d)
            if idx is None:
                continue
            j = scan.judge(bs[max(0, i + 1 - scan.EVAL_BARS):i + 1], d, cal_index, 60,
                           allow_extended)
            if j["res"] is None:
                if excluded is not None:
                    excluded[j["status"]] += 1
                continue
            res = j["res"]
            hit = [t for t in TYPES if res.get(t)]
            if hit:
                day.append({"code": code, "name": names.get(code, code), "index": idx,
                            "types": hit, "liq": res["liq"], "close": bs[i]["close"],
                            "detail": {t: scan.describe(t, res[t]) for t in hit}})
        day.sort(key=lambda r: -r["liq"])
        out[d] = day
        if n % 50 == 0:
            print(f"  … 신호 계산 {n}/{len(calendar)}일", file=sys.stderr)
    return out


# ─────────────────────────────────────────────────────────────
# 청산 규칙 — 모든 신호에 대해 한 번 계산(포트폴리오 제약 없는 거래 통계용)
# ─────────────────────────────────────────────────────────────

def exit_for(bs: list, i_entry: int, last_day: date):
    """(청산 인덱스, 사유). 사유: MA5 / 기간종료 / 데이터종료"""
    closes = [b["close"] for b in bs]
    for k in range(i_entry + 1, len(bs)):
        if bs[k]["date"] > last_day:
            return k - 1, "기간종료"
        ma5 = scan.sma_at(closes, k, 5)
        if ma5 is not None and closes[k] < ma5:
            return k, "MA5"
    k = len(bs) - 1
    return k, ("기간종료" if bs[k]["date"] >= last_day else "데이터종료")


def net_return(buy: float, sell: float, cost: dict) -> float:
    fee, slip, tax = cost["commission_rate"], cost["slippage_rate"], cost["tax_rate_sell"]
    paid = buy * (1 + slip) * (1 + fee)
    got = sell * (1 - slip) * (1 - fee - tax)
    return got / paid - 1


# ─────────────────────────────────────────────────────────────
# 포트폴리오
# ─────────────────────────────────────────────────────────────

def simulate(signals, bars, calendar, cost, capital, slots, weight):
    fee, slip, tax = cost["commission_rate"], cost["slippage_rate"], cost["tax_rate_sell"]
    pos = {c: {b["date"]: i for i, b in enumerate(bs)} for c, bs in bars.items()}
    last_day = calendar[-1]
    cash, held, trades, curve, actions = float(capital), {}, [], [], {}

    def last_close(code, d):
        bs, p = bars[code], pos[code]
        i = p.get(d)
        if i is None:        # 거래정지 등 — 직전 봉
            i = max((j for dd, j in p.items() if dd <= d), default=None)
        return bs[i]["close"]

    for d in calendar:
        acts = actions.setdefault(d, [])
        # 1) 매도 — 보유 종목의 청산일이 오늘이면
        for code in list(held):
            h = held[code]
            if h["exit_date"] != d:
                continue
            px = h["exit_px"]
            proceeds = h["shares"] * px * (1 - slip) * (1 - fee - tax)
            cash += proceeds
            ret = proceeds / h["cost_basis"] - 1
            trades.append({**{k: h[k] for k in ("code", "name", "types", "entry_date",
                                                   "entry_px", "shares", "exit_reason")},
                           "exit_date": d, "exit_px": px, "pnl": proceeds - h["cost_basis"],
                           "ret": ret, "hold_td": h["hold_td"]})
            acts.append(f"매도 {code} {h['name']} {h['shares']}주 @{px:,.0f} "
                        f"({ret:+.2%}, {h['exit_reason']})")
            del held[code]

        # 2) 평가금액(오늘 종가) → 매수
        equity = cash + sum(h["shares"] * last_close(c, d) for c, h in held.items())
        for sig in signals.get(d, []):
            if len(held) >= slots:
                break
            code = sig["code"]
            if code in held:
                continue
            px = sig["close"]
            unit = px * (1 + slip) * (1 + fee)
            budget = min(equity * weight, cash)
            shares = int(budget // unit)
            if shares <= 0:
                continue
            i = pos[code][d]
            k, reason = exit_for(bars[code], i, last_day)
            cost_basis = shares * unit
            cash -= cost_basis
            held[code] = {"code": code, "name": sig["name"], "types": sig["types"],
                          "entry_date": d, "entry_px": px, "shares": shares,
                          "cost_basis": cost_basis, "exit_date": bars[code][k]["date"],
                          "exit_px": bars[code][k]["close"], "exit_reason": reason,
                          "hold_td": k - i}
            acts.append(f"매수 {code} {sig['name']} [{','.join(sig['types'])}] "
                        f"{shares}주 @{px:,.0f} = {cost_basis:,.0f}원")

        # 3) 마감 평가 (기간종료 청산은 마지막 날 평가에 반영)
        mv = sum(h["shares"] * last_close(c, d) for c, h in held.items())
        curve.append({"date": d, "cash": cash, "stock": mv, "equity": cash + mv,
                      "n_held": len(held)})

    # 기간 끝에 남은 포지션 — 청산 가정(비용 포함)으로 거래 목록에만 기록
    for code, h in held.items():
        px = last_close(code, last_day)
        proceeds = h["shares"] * px * (1 - slip) * (1 - fee - tax)
        trades.append({**{k: h[k] for k in ("code", "name", "types", "entry_date",
                                               "entry_px", "shares")},
                       "exit_reason": "미청산(평가)", "exit_date": last_day, "exit_px": px,
                       "pnl": proceeds - h["cost_basis"],
                       "ret": proceeds / h["cost_basis"] - 1, "hold_td": h["hold_td"]})
    return trades, curve, actions


# ─────────────────────────────────────────────────────────────
# 통계
# ─────────────────────────────────────────────────────────────

def trade_stats(rets: list) -> dict:
    n = len(rets)
    if n == 0:
        return {"n": 0}
    mean = sum(rets) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (n - 1)) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n > 1 else float("nan")
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    s = sorted(rets)
    return {"n": n, "mean": mean, "median": s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2,
            "win": len(wins) / n, "avg_win": sum(wins) / len(wins) if wins else 0.0,
            "avg_loss": sum(losses) / len(losses) if losses else 0.0,
            "ci_lo": mean - 1.96 * se, "ci_hi": mean + 1.96 * se,
            "pf": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else float("inf")}


def mdd(values: list) -> tuple:
    peak, worst, at = values[0], 0.0, 0
    for i, v in enumerate(values):
        peak = max(peak, v)
        dd = v / peak - 1
        if dd < worst:
            worst, at = dd, i
    return worst, at


# ─────────────────────────────────────────────────────────────
# 출력
# ─────────────────────────────────────────────────────────────

def _p(v, d=2):
    return "-" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v * 100:+.{d}f}%"


def _w(v):
    return f"{v:,.0f}"


def write_daily(path, signals, actions, calendar, meta):
    L = [f"# 종가매수 A·B·C형 — 일별 선정 종목 ({calendar[0]} ~ {calendar[-1]})", "",
         f"- 스캐너 설계 {scan.DESIGN_VERSION}, 유니버스 = 날짜별 실제 KOSPI200·KOSDAQ150 구성",
         f"- 정렬 = 20일 평균 거래대금 내림차순. 포트폴리오는 빈 자리 수만큼 위에서부터 매수",
         f"- 신호일 {meta['signal_days']}일 / 전체 {len(calendar)}거래일, 신호 {meta['n_signals']}건", "",
         "| 날짜 | 유형 | 코드 | 종목 | 지수 | 종가 | 20일 평균 거래대금 | 근거 |",
         "|---|---|---|---|---|---:|---:|---|"]
    for d in calendar:
        for s in signals.get(d, []):
            det = " / ".join(f"{t}: {s['detail'][t]}" for t in s["types"])
            L.append(f"| {d} | {','.join(s['types'])} | {s['code']} | {s['name']} | "
                     f"{s['index']} | {_w(s['close'])} | {s['liq'] / 1e8:,.0f}억 | {det} |")
    L += ["", "## 일별 포트폴리오 매매 (15:40 시간외 종가)", ""]
    for d in calendar:
        if actions.get(d):
            L.append(f"**{d}**")
            L += [f"- {a}" for a in actions[d]]
            L.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


def write_summary(path, meta, all_trades, trades, curve, bench, daily_name):
    eq = [c["equity"] for c in curve]
    worst, at = mdd(eq)
    final = eq[-1]
    cap = meta["capital"]
    def bench_at(d):
        ks = [k for k in bench if k <= d]
        return bench[max(ks)] if ks else None
    b0, b1 = bench_at(curve[0]["date"]), bench_at(curve[-1]["date"])
    bret = (b1 / b0 - 1) if b0 and b1 else None
    bend = max(bench) if bench else None
    bvals = [bench[c["date"]] for c in curve if c["date"] in bench]
    bmdd = mdd(bvals)[0] if bvals else None
    exposure = sum(c["n_held"] for c in curve) / (len(curve) * meta["slots"])

    L = [f"# 종가매수 A·B·C형 백테스트 — {meta['start']} ~ {meta['end']}", "",
         "> ⚠ **탐색 백테스트**(사전등록 아님). 스캐너 정의는 차트 관행을 옮긴 초안이며, "
         "같은 1년 데이터를 보고 파라미터를 고치면 과최적화가 된다. 1년·소표본이라 "
         "결과를 기대수익으로 읽으면 안 된다.", "",
         "## 규칙", "",
         f"- 신호: `tools/closing_bet_scanner.py`(설계 {scan.DESIGN_VERSION}) A·B·C형, "
         "신호일까지의 완료 봉만 사용",
         "- 유니버스: 날짜별 실제 KOSPI200·KOSDAQ150 구성종목(`silver.universe_membership`)",
         "- 매수: 신호일 종가(15:30 확정) → 15:40 시간외 종가매매 체결 가정",
         "- 매도: 다음 거래일부터 **종가 < 5일 이동평균**인 첫 날 종가(15:40)",
         f"- 비용: 수수료 {meta['cost']['commission_rate']:.3%}×2, 슬리피지 "
         f"{meta['cost']['slippage_rate']:.1%}×2, 매도세 {meta['cost']['tax_rate_sell']:.2%} "
         "(`configs/costs/krx_v1.json`, 왕복 약 0.41%)",
         f"- 포트폴리오: 초기 {_w(cap)}원, 최대 {meta['slots']}종목, 종목당 당일 평가금액 × "
         f"{meta['weight']:.0%}(현금 한도), 신호 과다 시 20일 평균 거래대금 상위 우선",
         f"- 데이터: 로컬 `silver.daily_bars` 수정주가. 일별 선정 종목은 [{daily_name}]({daily_name})",
         "",
         "## 1. 포트폴리오 시뮬레이션 (1,000만 원, 20% × 5종목)", "",
         "| 항목 | 값 |", "|---|---:|",
         f"| 기간 | {curve[0]['date']} ~ {curve[-1]['date']} ({len(curve)}거래일) |",
         f"| 초기 자본 | {_w(cap)}원 |",
         f"| 최종 평가금액 | **{_w(final)}원** |",
         f"| 누적 수익률 | **{_p(final / cap - 1)}** |",
         f"| 최대 낙폭(MDD) | {_p(worst)} ({curve[at]['date']}) |",
         f"| 코스피200 지수 같은 기간(가격지수, ~{bend}) | {_p(bret)} (MDD {_p(bmdd)}) |",
         f"| 구성종목 이력 전체 / 일봉 보유 | {meta['n_members']} / {meta['n_loaded']} "
         f"(기간 중 구성인데 일봉 없는 종목-일 {meta['missing_member_days']:,}건) |",
         f"| 판정 불가(구성종목·일) | " + (", ".join(f"{k} {v:,}" for k, v in meta["excluded"].items())
                                        or "없음") + " |",
         f"| 체결 거래 수 | {len(trades)} |",
         f"| 평균 슬롯 사용률 | {exposure:.0%} |", ""]
    ts = trade_stats([t["ret"] for t in trades])
    if ts["n"]:
        L += [f"포트폴리오 거래: 승률 {ts['win']:.1%}, 평균 {_p(ts['mean'])}, 중앙값 "
              f"{_p(ts['median'])}, 평균 이익 {_p(ts['avg_win'])} / 평균 손실 "
              f"{_p(ts['avg_loss'])}, 손익비(PF) {ts['pf']:.2f}", ""]

    # 월말 평가금액
    L += ["### 월별 평가금액", "", "| 월말 | 평가금액 | 월 수익률 | 누적 | 코스피200 누적 | 보유 |",
          "|---|---:|---:|---:|---:|---:|"]
    months = {}
    for c in curve:
        months[c["date"].strftime("%Y-%m")] = c
    prev = cap
    for m, c in months.items():
        bc = bench_at(c["date"])
        L.append(f"| {c['date']} | {_w(c['equity'])} | {_p(c['equity'] / prev - 1)} | "
                 f"{_p(c['equity'] / cap - 1)} | {_p(bc / b0 - 1) if bc and b0 else '-'} | "
                 f"{c['n_held']} |")
        prev = c["equity"]

    # 신호 전체(포트폴리오 제약 없음)
    L += ["", "## 2. 전체 신호 거래 통계 (슬롯 제약 없이 모든 신호를 1회씩 매매)", "",
          "포트폴리오는 신호 일부만 담는다. 규칙 자체의 성질은 모든 신호를 따로 본 이 표가 "
          "더 잘 보여준다. 한 종목이 여러 유형에 동시에 걸리면 각 유형 행에 모두 들어간다.", "",
          "| 구분 | 거래 수 | 승률 | 평균(순) | 95% 구간 | 중앙값 | 평균 이익 | 평균 손실 | PF | 평균 보유일 |",
          "|---|---:|---:|---:|---|---:|---:|---:|---:|---:|"]
    groups = [("A·B·C 전체(종목·일 중복 제거)", all_trades)] + \
             [(f"{t}형", [x for x in all_trades if t in x["types"]]) for t in TYPES]
    for label, g in groups:
        s = trade_stats([x["ret"] for x in g])
        if not s["n"]:
            L.append(f"| {label} | 0 | - | - | - | - | - | - | - | - |")
            continue
        hold = sum(x["hold_td"] for x in g) / len(g)
        pf = "∞" if math.isinf(s["pf"]) else f"{s['pf']:.2f}"
        L.append(f"| {label} | {s['n']} | {s['win']:.0%} | {_p(s['mean'])} | "
                 f"[{_p(s['ci_lo'])}, {_p(s['ci_hi'])}] | {_p(s['median'])} | "
                 f"{_p(s['avg_win'])} | {_p(s['avg_loss'])} | {pf} | {hold:.1f} |")
    gross = trade_stats([x["gross"] for x in all_trades])
    if gross["n"]:
        L += ["", f"비용 전(gross) 평균: {_p(gross['mean'])} — 비용이 거래당 약 0.41%p를 깎는다."]

    # 포트폴리오 거래 목록
    L += ["", "## 3. 포트폴리오 거래 목록", "",
          "| # | 코드 | 종목 | 유형 | 매수일 | 매수가 | 수량 | 매도일 | 매도가 | 보유일 | 수익률(순) | 손익(원) | 사유 |",
          "|---:|---|---|---|---|---:|---:|---|---:|---:|---:|---:|---|"]
    for n, t in enumerate(sorted(trades, key=lambda x: (x["entry_date"], x["code"])), 1):
        L.append(f"| {n} | {t['code']} | {t['name']} | {','.join(t['types'])} | "
                 f"{t['entry_date']} | {_w(t['entry_px'])} | {t['shares']} | {t['exit_date']} | "
                 f"{_w(t['exit_px'])} | {t['hold_td']} | {_p(t['ret'])} | {_w(t['pnl'])} | "
                 f"{t['exit_reason']} |")

    L += ["", "## 한계", "",
          "1. **1년·단일 국면**: 한 시장 국면의 결과다. 다른 해에 같은 결과가 나온다는 근거가 없다.",
          "2. **체결 가정**: 15:40 시간외 종가매매는 15:30 종가로 체결되지만 수량이 보장되지 않는다. "
          "슬리피지 0.1%를 양방향에 넣었지만 소형주·상한가 종목은 체결 자체가 안 될 수 있다.",
          "3. **정규장 복원**: 2026-09-14 이후 봉은 1분봉 정규장(09:00~15:30)으로 복원했다. "
          "복원 봉의 거래대금은 1분봉 종가×거래량 합(추정)이고, 거래량에 15:40 시간외 종가매매가 "
          "빠져 이전 일봉 관행보다 조금 작을 수 있다. " + meta["end_note"],
          "4. **순위 기준**: '상위 5개'는 스캐너 정렬(20일 평균 거래대금)이다. 신호 강도 순위가 아니다.",
          "5. **매도 판정**: 종가가 확정돼야 MA5 이탈을 안다 — 같은 날 15:40 매도는 실행 가능하지만, "
          "장중 이탈 후 회복한 경우는 매도하지 않는다.",
          f"6. 스캐너 상수는 {scan.DESIGN_VERSION} 그대로다. 이 결과를 보고 상수를 바꾸면 "
          "`/prereg-backtest`로 새 기간에서 다시 확인해야 한다.",
          "7. **투자 권유가 아니다.**"]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    return {"final": final, "ret": final / cap - 1, "mdd": worst, "bench": bret}


# ─────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="종가매수 A·B·C형 백테스트(탐색)")
    ap.add_argument("--start", help="신호 시작일 (기본: 종료일 1년 전)")
    ap.add_argument("--end", help="종료일 (기본: 로컬 일봉 최신일)")
    ap.add_argument("--capital", type=float, default=10_000_000)
    ap.add_argument("--slots", type=int, default=5)
    ap.add_argument("--weight", type=float, default=0.2)
    ap.add_argument("--allow-extended", action="store_true",
                    help="정규장 복원 못 한 애프터마켓 봉도 사용(기간을 자르지 않음)")
    args = ap.parse_args()
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    with open(COST_FILE, encoding="utf-8") as f:
        cost = json.load(f)
    end = date.fromisoformat(args.end) if args.end else date.today()
    start_guess = date.fromisoformat(args.start) if args.start else end - timedelta(days=366)

    intervals = load_intervals()
    codes = sorted(s.split(":", 1)[1] for s in intervals)
    scan.LOAD_CAL_DAYS = (end - start_guess).days + WARMUP_CAL_DAYS
    print(f"일봉 로드: {len(codes)}종목(기간 중 구성종목 전체) …", file=sys.stderr)
    bars = scan.load_local(codes, end)
    from collections import Counter
    all_days = scan.market_calendar(bars)
    cal_index = {d: i for i, d in enumerate(all_days)}
    end = all_days[-1]
    end_note = ""
    stray_ext = 0
    if not args.allow_extended:
        # 구성종목 봉 중 정규장 복원을 못 한 비율이 EXT_CUT_SHARE를 넘는 첫 날부터는
        # 15:30 종가를 사실상 모른다 → 그 전날에서 기간을 끝낸다. 그보다 드문 봉
        # (1분봉 15:30 봉 누락 등)은 신호 판정에서만 빠지고(judge) 개수를 보고한다.
        per_day, ext_day = Counter(), Counter()
        for c, bs in bars.items():
            for b in bs:
                if b["date"] >= scan.AFTERMARKET_START and                         member_index(intervals.get(f"KRX:{c}", ()), b["date"]):
                    per_day[b["date"]] += 1
                    ext_day[b["date"]] += b["scope"] == scan.SCOPE_EXTENDED
        bad = sorted(d for d in per_day if ext_day[d] > per_day[d] * EXT_CUT_SHARE)
        if bad:
            cut = max(d for d in all_days if d < bad[0])
            end_note = (f"{bad[0]}부터 구성종목 봉의 {ext_day[bad[0]] / per_day[bad[0]]:.0%}가 "
                        f"정규장 복원 불가(1분봉 미수집)라 기간을 {cut}에서 끝냈다.")
            print(f"  ⚠ {end_note}", file=sys.stderr)
            end = min(end, cut)
        stray_ext = sum(n for d, n in ext_day.items() if d <= end)
        if stray_ext:
            end_note += (f" 기간 안에 복원 못 한 봉 {stray_ext}개(구성종목·일)가 있다 — 그 봉이 "
                         f"창에 든 신호 판정은 제외했고, 보유 중 평가·청산 가격에는 애프터마켓 "
                         f"종가가 쓰였을 수 있다.")
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=365)
    calendar = [d for d in all_days if start <= d <= end]

    from cvd_obv.universe import current_universe
    cur = current_universe()
    names = {c: cur.name(c) for c in cur.codes()}
    try:
        from cvd_obv.storage.parquet_store import ParquetStore
        t = ParquetStore().read_arrow("silver", "security_master",
                                      columns=["security_id", "name"])
        for sid, n in zip(t.column("security_id").to_pylist(), t.column("name").to_pylist()):
            names.setdefault(sid.split(":", 1)[-1], n)
    except Exception:
        pass

    print(f"기간 {calendar[0]} ~ {calendar[-1]} ({len(calendar)}거래일), 일봉 {len(bars)}종목",
          file=sys.stderr)
    excluded = Counter()
    signals = generate_signals(bars, intervals, calendar, names, cal_index,
                               args.allow_extended, excluded)
    have = {c: {b["date"] for b in bs} for c, bs in bars.items()}
    missing_member_days = sum(
        1 for sid, ivs in intervals.items() for d in calendar
        if member_index(ivs, d) and d not in have.get(sid.split(":", 1)[1], ()))

    # 전체 신호 거래(슬롯 제약 없음)
    pos = {c: {b["date"]: i for i, b in enumerate(bs)} for c, bs in bars.items()}
    all_trades = []
    for d in calendar:
        for s in signals[d]:
            bs = bars[s["code"]]
            i = pos[s["code"]][d]
            k, reason = exit_for(bs, i, end)
            if k == i:      # 마지막 날 신호 — 보유 0일, 통계 제외
                continue
            all_trades.append({"types": s["types"], "ret": net_return(bs[i]["close"], bs[k]["close"], cost),
                               "gross": bs[k]["close"] / bs[i]["close"] - 1,
                               "hold_td": k - i, "reason": reason})

    trades, curve, actions = simulate(signals, bars, calendar, cost, args.capital,
                                      args.slots, args.weight)
    bench = load_benchmark("KRX:KOSPI200", calendar[0], calendar[-1])

    tag = f"{calendar[0]:%Y%m%d}-{calendar[-1]:%Y%m%d}"
    daily_name = f"종가매수-ABC-일별선정-{tag}.md"
    summ_name = f"종가매수-ABC-백테스트-{tag}.md"
    meta = {"start": calendar[0], "end": calendar[-1], "capital": args.capital,
            "slots": args.slots, "weight": args.weight, "cost": cost,
            "n_members": len(intervals), "n_loaded": len(bars),
            "missing_member_days": missing_member_days,
            "excluded": dict(excluded), "end_note": end_note or "기간 전체가 정규장 가격이다.",
            "signal_days": sum(1 for d in calendar if signals[d]),
            "n_signals": sum(len(signals[d]) for d in calendar)}
    write_daily(os.path.join(scan.REPORT_DIR, daily_name), signals, actions, calendar, meta)
    r = write_summary(os.path.join(scan.REPORT_DIR, summ_name), meta, all_trades, trades,
                      curve, bench, daily_name)

    print(f"\n기간 {calendar[0]} ~ {calendar[-1]}  신호 {meta['n_signals']}건 "
          f"({meta['signal_days']}일)  전체신호 거래 {len(all_trades)}  포트폴리오 거래 {len(trades)}")
    for label, g in [("전체", all_trades)] + [(t, [x for x in all_trades if t in x["types"]]) for t in TYPES]:
        s = trade_stats([x["ret"] for x in g])
        if s["n"]:
            print(f"  {label:<3} n={s['n']:>4} 승률 {s['win']:.0%} 평균 {_p(s['mean'])} "
                  f"[{_p(s['ci_lo'])}, {_p(s['ci_hi'])}]")
    print(f"  포트폴리오 {_w(args.capital)} → {_w(r['final'])}원 ({_p(r['ret'])}), "
          f"MDD {_p(r['mdd'])}, 코스피200 {_p(r['bench'])}")
    print(f"  저장: reports/{summ_name}\n        reports/{daily_name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

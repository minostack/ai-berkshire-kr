# -*- coding: utf-8 -*-
"""종목 수급 조회 — KIS REST로 투자자별 순매수·프로그램·공매도·대차·신용·투자의견을 한 번에.

news-pulse 4번 Agent(시장 심리)가 "펀더멘털 주도인가, 수급 주도인가"를 수치로
판단하도록 만든 도구다. 매매 판단 도구가 아니라 사실 확인 도구다.

사용:
    python tools/kis_flow.py 222800                 # 최근 14일(달력일)
    python tools/kis_flow.py 222800 --days 30
    python tools/kis_flow.py 222800 --from 20261101 --to 20261117
    python tools/kis_flow.py 222800 --market UN     # 투자자·프로그램을 KRX+NXT 통합으로
    python tools/kis_flow.py 222800 --json          # Agent용 JSON

TR — 필드·단위는 KIS 오픈API 전체문서(2026-09-07판)와 2026-11-17 실제 응답(심텍
222800)으로 확인했다. 문서와 실측이 다르면 실측을 따랐고 아래에 적었다.
---------------------------------------------------------------------------
| 데이터 | TR | 확인한 성질 |
|---|---|---|
| 투자자별 순매수(일별) | FHPTJ04160001 investor-trade-by-stock-daily | 기준일 이전 30행/호출. `FID_ETC_CLS_CODE="1"` 필수. **당일은 장 종료 후에만**(장중 당일 요청은 "TIME LIMIT 00:00~15:40" 거절) → 장중엔 전일 기준으로 부른다. 수량=주, 순매수 대금=백만원. 세부 주체(투신·사모·연기금(기금)·보험·은행·증권·기타법인) 포함. 문서상 `acml_tr_pbmn`은 백만원이지만 실측은 원 — 쓰지 않는다 |
| 투자자별 순매수(대체) | FHKST01010900 inquire-investor | 위 TR 실패 시만. 최근 30거래일 고정, 개인·외국인·기관만 |
| 프로그램매매(일별) | FHPPG04650201 program-trade-by-stock-daily | 기준일 이전 30행/호출. 대금=원(실측: 순매수 수량×종가와 일치). 당일 행은 장중 누적 |
| 장중 가집계 | HHPTJ04160200 investor-trend-estimate | 오늘 장중 외국인·기관 추정 순매수(주). 입력구분 1=09:30 2=10:00 3=11:20 4=13:20 5=14:30. 확정치 아님 |
| 공매도 | FHPST04830000 daily-short-sale | 기간 지정. `ssts_cntg_qty`=당일 공매도 수량, `ssts_vol_rlim`=거래량 대비 %. 당일 행은 0(익일 공표) |
| 대차거래 | HHPST074500C0 daily-loan-trans | 기간 지정, 최대 100행. `rmnd_amt`=잔고×종가, 백만원(실측 일치). 당일 행 없음 |
| 신용잔고 | FHPST04760000 daily-credit-balance | 기준(결제)일 이전 30행/호출. `deal_date`=매매일, `stlm_date`=결제일(T+2). 금액=만원(문서). 융자(신용매수)·대주(신용매도) |
| 투자의견 | FHKST663300C0 invest-opinion | 기간 지정. 증권사·의견·직전의견·목표가. 직전 목표가는 주지 않는다 |

`_kis_request`는 응답 헤더(`tr_cont`)를 돌려주지 않아 연속조회 대신 기준일을 앞으로
옮겨 다시 부른다. 거래량은 KRX 값이다(로컬 일봉 거래량은 8/3 이후 NXT 포함이라 다르다).
로컬 기준 일봉은 종가 교차 검증에만 쓴다(오차 >1%면 표시).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(ROOT, "tools"), os.path.join(ROOT, "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

Q = "/uapi/domestic-stock/v1/quotations"
BIG_MOVE_PCT = 5.0          # 급변일 기준(하루 ±5%, news-pulse 기준선과 같다)
PRICE_TOL_PCT = 1.0         # 로컬 일봉과 종가 오차 표시 기준(CLAUDE.md)
INVESTOR_CLOSE = (15, 40)   # 투자자 일별 TR의 당일 조회 가능 시각
ESTIMATE_SLOTS = {"1": "09:30", "2": "10:00", "3": "11:20", "4": "13:20", "5": "14:30"}

#: 기관 세부 주체 — (출력 키, 한글, 수량 필드, 대금 필드[백만원])
INST_DETAIL = [
    ("trust", "투신", "ivtr_ntby_qty", "ivtr_ntby_tr_pbmn"),
    ("pef", "사모", "pe_fund_ntby_vol", "pe_fund_ntby_tr_pbmn"),
    ("pension", "연기금", "fund_ntby_qty", "fund_ntby_tr_pbmn"),
    ("insurance", "보험", "insu_ntby_qty", "insu_ntby_tr_pbmn"),
    ("bank", "은행", "bank_ntby_qty", "bank_ntby_tr_pbmn"),
    ("securities", "증권", "scrt_ntby_qty", "scrt_ntby_tr_pbmn"),
    ("other_corp", "기타법인", "etc_corp_ntby_vol", "etc_corp_ntby_tr_pbmn"),
]


def _num(v):
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _div(v, k):
    return None if v is None else v / k


def _ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


def _prev_day(ymd: str) -> date:
    return datetime.strptime(ymd, "%Y%m%d").date() - timedelta(days=1)


def _client():
    import daily_scanner as dsc            # .env 로드·토큰 캐시를 함께 쓴다
    if not dsc._kis_enabled():
        raise SystemExit("KIS_APP_KEY / KIS_APP_SECRET가 없다 — .env 확인")
    return dsc._kis_client()


def _paged(cli, path, params_for, tr_id, list_key, date_key, start: date, cursor: date,
           max_pages: int = 8, cursor_key: str | None = None):
    """기준일 이전 N행씩 주는 TR을 기준일을 앞으로 옮겨 가며 부른다.
    `cursor_key`: 기준일이 행 날짜와 다른 필드일 때(신용 = 결제일) 그 필드로 다음 기준일을 잡는다."""
    cursor_key = cursor_key or date_key
    rows, error = [], None
    for _ in range(max_pages):
        res = cli.get(path, params_for(cursor), tr_id)
        if res is None:
            error = cli.last_error
            break
        out = [r for r in (res.get(list_key) or []) if len(str(r.get(date_key) or "")) == 8]
        new = [r for r in out if not rows or r[date_key] < rows[-1][date_key]]
        rows.extend(sorted(new, key=lambda r: r[date_key], reverse=True))
        if not new or min(r[date_key] for r in new) <= _ymd(start) or len(out) < 30:
            break
        oldest = min(new, key=lambda r: r[date_key])
        cursor = _prev_day(str(oldest.get(cursor_key) or oldest[date_key]))
    return rows, error


# ── 원천별 조회 ──────────────────────────────────────────────

def _investor_cursor(end: date, now: datetime) -> date:
    if end < now.date():
        return end
    return end if (now.hour, now.minute) >= INVESTOR_CLOSE else end - timedelta(days=1)


def fetch_investor(cli, code: str, start: date, end: date, market: str, now: datetime) -> dict:
    cursor = _investor_cursor(end, now)
    rows, err = _paged(
        cli, f"{Q}/investor-trade-by-stock-daily",
        lambda c: {"FID_COND_MRKT_DIV_CODE": market, "FID_INPUT_ISCD": code,
                   "FID_INPUT_DATE_1": _ymd(c), "FID_ORG_ADJ_PRC": "", "FID_ETC_CLS_CODE": "1"},
        "FHPTJ04160001", "output2", "stck_bsop_date", start, cursor)
    out = {}
    for r in rows:
        f, o = _num(r.get("frgn_ntby_qty")), _num(r.get("orgn_ntby_qty"))
        if f is None or o is None:
            continue
        row = {
            "close": _num(r.get("stck_clpr")), "chg_pct": _num(r.get("prdy_ctrt")),
            "individual": _num(r.get("prsn_ntby_qty")), "foreign": f, "institution": o,
            "individual_amt": _div(_num(r.get("prsn_ntby_tr_pbmn")), 100),   # 백만원→억원
            "foreign_amt": _div(_num(r.get("frgn_ntby_tr_pbmn")), 100),
            "institution_amt": _div(_num(r.get("orgn_ntby_tr_pbmn")), 100),
        }
        for key, _, qk, ak in INST_DETAIL:
            row[key] = _num(r.get(qk))
            row[f"{key}_amt"] = _div(_num(r.get(ak)), 100)
        out[r["stck_bsop_date"]] = row
    if out:
        return {"rows": out, "tr": f"FHPTJ04160001({market})", "error": err}
    return _fetch_investor_fallback(cli, code, err)


def _fetch_investor_fallback(cli, code: str, first_error) -> dict:
    res = cli.get(f"{Q}/inquire-investor",
                  {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}, "FHKST01010900")
    if res is None:
        return {"error": f"FHPTJ04160001: {first_error} / FHKST01010900: {cli.last_error}"}
    out = {}
    for r in res.get("output") or []:
        d = str(r.get("stck_bsop_date") or "")
        f, o = _num(r.get("frgn_ntby_qty")), _num(r.get("orgn_ntby_qty"))
        if len(d) != 8 or f is None or o is None:      # 장중 당일 공란은 버린다
            continue
        out[d] = {
            "close": _num(r.get("stck_clpr")),
            "individual": _num(r.get("prsn_ntby_qty")), "foreign": f, "institution": o,
            "individual_amt": _div(_num(r.get("prsn_ntby_tr_pbmn")), 100),
            "foreign_amt": _div(_num(r.get("frgn_ntby_tr_pbmn")), 100),
            "institution_amt": _div(_num(r.get("orgn_ntby_tr_pbmn")), 100),
        }
    return {"rows": out, "tr": "FHKST01010900(대체, 30거래일·세부 주체 없음)",
            "warning": f"투자자 일별 TR 실패로 대체 TR 사용: {first_error}"}


def fetch_program(cli, code: str, start: date, end: date, market: str) -> dict:
    rows, err = _paged(
        cli, f"{Q}/program-trade-by-stock-daily",
        lambda c: {"FID_COND_MRKT_DIV_CODE": market, "FID_INPUT_ISCD": code,
                   "FID_INPUT_DATE_1": _ymd(c)},
        "FHPPG04650201", "output", "stck_bsop_date", start, end)
    out = {r["stck_bsop_date"]: {
        "program_net": _num(r.get("whol_smtn_ntby_qty")),
        "program_net_amt": _div(_num(r.get("whol_smtn_ntby_tr_pbmn")), 1e8),   # 원→억원
    } for r in rows}
    return {"rows": out, "error": err if not out else None}


def fetch_estimate(cli, code: str) -> dict:
    res = cli.get(f"{Q}/investor-trend-estimate", {"MKSC_SHRN_ISCD": code}, "HHPTJ04160200")
    if res is None:
        return {"error": cli.last_error}
    slots = []
    for r in res.get("output2") or []:
        gb = str(r.get("bsop_hour_gb") or "")
        slots.append({"slot": gb, "time": ESTIMATE_SLOTS.get(gb, gb),
                      "foreign": _num(r.get("frgn_fake_ntby_qty")),
                      "institution": _num(r.get("orgn_fake_ntby_qty")),
                      "sum": _num(r.get("sum_fake_ntby_qty"))})
    slots.sort(key=lambda s: s["slot"])
    return {"slots": slots}


def fetch_short(cli, code: str, start: date, end: date, today: date) -> dict:
    res = cli.get(f"{Q}/daily-short-sale",
                  {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                   "FID_INPUT_DATE_1": _ymd(start), "FID_INPUT_DATE_2": _ymd(end)},
                  "FHPST04830000")
    if res is None:
        return {"error": cli.last_error}
    rows = {}
    for r in res.get("output2") or []:
        d = str(r.get("stck_bsop_date") or "")
        if len(d) != 8:
            continue
        qty = _num(r.get("ssts_cntg_qty"))
        pending = d == _ymd(today) and not qty          # 당일 공매도는 익일 공표
        rows[d] = {
            "close": _num(r.get("stck_clpr")), "chg_pct": _num(r.get("prdy_ctrt")),
            "volume": _num(r.get("acml_vol")),
            "short_qty": None if pending else qty,
            "short_pct": None if pending else _num(r.get("ssts_vol_rlim")),
            "short_amt": None if pending else (_num(r.get("ssts_tr_pbmn")) or 0) / 1e8,  # 원→억원
            "partial_day": d == _ymd(today),
        }
    return {"rows": rows}


def fetch_loan(cli, code: str, start: date, end: date) -> dict:
    res = cli.get(f"{Q}/daily-loan-trans",
                  {"MRKT_DIV_CLS_CODE": "3", "MKSC_SHRN_ISCD": code,
                   "START_DATE": _ymd(start), "END_DATE": _ymd(end), "CTS": ""},
                  "HHPST074500C0")
    if res is None:
        return {"error": cli.last_error}
    out = res.get("output1") or []
    rows = {}
    for r in out:
        d = str(r.get("bsop_date") or "")
        if len(d) != 8:
            continue
        rows[d] = {
            "loan_new": _num(r.get("new_stcn")), "loan_repay": _num(r.get("rdmp_stcn")),
            "loan_change": _num(r.get("prdy_rmnd_vrss")), "loan_balance": _num(r.get("rmnd_stcn")),
            "loan_balance_amt": _div(_num(r.get("rmnd_amt")), 100),      # 백만원→억원
        }
    warn = "대차 응답이 100행 — 기간 앞쪽이 잘렸을 수 있다" if len(out) >= 100 else None
    return {"rows": rows, "warning": warn}


def fetch_credit(cli, code: str, start: date, end: date) -> dict:
    rows, err = _paged(
        cli, f"{Q}/daily-credit-balance",
        lambda c: {"FID_COND_MRKT_DIV_CODE": "J", "FID_COND_SCR_DIV_CODE": "20476",
                   "FID_INPUT_ISCD": code, "FID_INPUT_DATE_1": _ymd(c)},
        # 기준일은 결제일(T+2)이다 — 종료일 매매분까지 받으려면 며칠 뒤를 기준으로 부른다
        "FHPST04760000", "output", "deal_date", start, min(end + timedelta(days=5), date.today()),
        cursor_key="stlm_date")
    out = {r["deal_date"]: {
        "settle_date": str(r.get("stlm_date") or ""),
        "credit_balance": _num(r.get("whol_loan_rmnd_stcn")),
        "credit_balance_amt": _div(_num(r.get("whol_loan_rmnd_amt")), 1e4),   # 만원→억원
        "credit_new": _num(r.get("whol_loan_new_stcn")),
        "credit_repay": _num(r.get("whol_loan_rdmp_stcn")),
        "credit_rate": _num(r.get("whol_loan_rmnd_rate")),      # 상장주식 대비 %
        "credit_supply_rate": _num(r.get("whol_loan_gvrt")),    # 거래량 대비 %
        "stock_loan_balance": _num(r.get("whol_stln_rmnd_stcn")),   # 대주(신용매도) 잔고, 주
    } for r in rows}
    return {"rows": out, "error": err if not out else None}


def fetch_opinion(cli, code: str, start: date, end: date) -> dict:
    res = cli.get(f"{Q}/invest-opinion",
                  {"FID_COND_MRKT_DIV_CODE": "J", "FID_COND_SCR_DIV_CODE": "16633",
                   "FID_INPUT_ISCD": code, "FID_INPUT_DATE_1": _ymd(start),
                   "FID_INPUT_DATE_2": _ymd(end)}, "FHKST663300C0")
    if res is None:
        return {"error": cli.last_error}
    rows = []
    for r in res.get("output") or []:
        d = str(r.get("stck_bsop_date") or "")
        if len(d) != 8:
            continue
        goal = _num(r.get("hts_goal_prc"))
        rows.append({"date": d, "broker": r.get("mbcr_name") or "",
                     "opinion": r.get("invt_opnn") or "", "prev_opinion": r.get("rgbf_invt_opnn") or "",
                     "target": goal or None, "prev_close": _num(r.get("stck_prdy_clpr"))})
    rows.sort(key=lambda x: x["date"], reverse=True)
    return {"rows": rows}


def fetch_local_close(code: str, start: date, end: date) -> dict:
    """로컬 기준 일봉 종가(bars-1.0) — 교차 검증용. 없으면 빈 dict."""
    try:
        import pyarrow.dataset as ds
        from cvd_obv.bar_standard import read_standard_daily
        flt = ((ds.field("trading_date") >= start) & (ds.field("trading_date") <= end)
               & (ds.field("security_id") == f"KRX:{code}"))
        t = read_standard_daily(columns=["security_id", "trading_date", "close"],
                                filters=flt, drop_failed=True)
        return {r["trading_date"].strftime("%Y%m%d"): float(r["close"]) for r in t.to_pylist()}
    except Exception as exc:                        # noqa: BLE001 — 교차 검증은 선택
        return {"_error": str(exc)}


# ── 병합·요약 ────────────────────────────────────────────────

def collect(code: str, start: date, end: date, market: str = "J",
            opinion_days: int = 90, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    today = now.date()
    cli = _client()
    inv = fetch_investor(cli, code, start, end, market, now)
    prog = fetch_program(cli, code, start, end, market)
    sht = fetch_short(cli, code, start, end, today)
    loan = fetch_loan(cli, code, start, end)
    cred = fetch_credit(cli, code, start, end)
    op_start = min(start, end - timedelta(days=opinion_days))
    opin = fetch_opinion(cli, code, op_start, end)
    est = fetch_estimate(cli, code) if end >= today else {}
    local = fetch_local_close(code, start, end)

    lo, hi = _ymd(start), _ymd(end)
    srcs = (sht, inv, prog, loan, cred)
    days = sorted({d for src in (inv, sht, loan, prog) for d in (src.get("rows") or {})
                   if lo <= d <= hi}, reverse=True)
    table = []
    for d in days:
        row = {"date": d}
        for src in srcs:
            for k, v in (src.get("rows") or {}).get(d, {}).items():
                row.setdefault(k, v)
        lc = local.get(d)
        if lc is not None and row.get("close"):
            row["local_close"] = lc
            row["close_diff_pct"] = round((lc / row["close"] - 1) * 100, 2)
        table.append(row)

    credit_rows = {d: v for d, v in (cred.get("rows") or {}).items() if lo <= d <= hi}
    named = (("investor", inv), ("program", prog), ("short", sht), ("loan", loan),
             ("credit", cred), ("opinion", opin), ("estimate", est))
    errors = {k: s["error"] for k, s in named if s.get("error")}
    notes = [s["warning"] for _, s in named if s.get("warning")]
    if "_error" in local:
        notes.append(f"로컬 일봉을 읽지 못해 종가 교차 검증을 건너뜀: {local['_error']}")
    inv_rows = inv.get("rows") or {}
    if inv_rows and min(inv_rows) > lo and "FHKST01010900" in inv.get("tr", ""):
        notes.append(f"투자자 순매수는 최근 30거래일만 제공 — {min(inv_rows)} 이전 없음")
    if end >= today and _ymd(today) not in inv_rows:
        notes.append("오늘 투자자별 확정치는 15:40 이후 조회 가능 — 장중엔 가집계(추정)만 있다")
    return {"code": code, "from": lo, "to": hi, "market": market,
            "rows": table, "summary": summarize(table, credit_rows),
            "opinions": opin.get("rows") or [], "opinion_from": _ymd(op_start),
            "estimate_today": est.get("slots") or [],
            "errors": errors, "notes": notes,
            "source": f"KIS REST — 투자자 {inv.get('tr', '실패')}, 프로그램 FHPPG04650201({market}), "
                      "공매도 FHPST04830000, 대차 HHPST074500C0, 신용 FHPST04760000, "
                      "투자의견 FHKST663300C0, 가집계 HHPTJ04160200"}


def _sum(rows, key):
    vals = [r[key] for r in rows if r.get(key) is not None]
    return sum(vals) if vals else None


def summarize(table: list, credit_rows: dict) -> dict:
    s = {}
    if not table:
        return s
    closes = [(r["date"], r["close"]) for r in table if r.get("close")]
    if len(closes) >= 2:
        (d1, c1), (d0, c0) = closes[0], closes[-1]
        note = "기간 첫 거래일 종가 기준(그 전날 종가 아님)"
        if table[0].get("partial_day"):
            note += ", 끝 값은 장중 잠정"
        s["price"] = {"from_date": d0, "from_close": c0, "to_date": d1, "to_close": c1,
                      "chg_pct": round((c1 / c0 - 1) * 100, 2), "note": note}
    done = [r for r in table if not r.get("partial_day")]     # 누적은 확정된 날만
    keys = ["individual", "foreign", "institution", "program_net"] + [k for k, *_ in INST_DETAIL]
    for k in keys:
        s[f"sum_{k}"] = _sum(done, k)
        s[f"sum_{k}_amt"] = _sum(done, f"{k}_amt")
    s["sum_short_qty"], s["sum_short_amt"] = _sum(done, "short_qty"), _sum(done, "short_amt")
    vols = [(r["short_qty"], r["volume"]) for r in done
            if r.get("short_qty") is not None and r.get("volume")]
    if vols:
        s["short_pct_period"] = round(sum(q for q, _ in vols) / sum(v for _, v in vols) * 100, 2)
        top = max((r for r in table if r.get("short_pct") is not None), key=lambda r: r["short_pct"])
        s["short_pct_max"] = {"date": top["date"], "pct": top["short_pct"]}
    loans = [r for r in table if r.get("loan_balance") is not None]
    if len(loans) >= 2:
        s["loan_balance"] = {"from_date": loans[-1]["date"], "from": loans[-1]["loan_balance"],
                             "to_date": loans[0]["date"], "to": loans[0]["loan_balance"],
                             "to_amt_eok": loans[0].get("loan_balance_amt"),
                             "chg_pct": round((loans[0]["loan_balance"] / loans[-1]["loan_balance"] - 1) * 100, 2)}
    if len(credit_rows) >= 2:
        ds_ = sorted(credit_rows)
        a, b = credit_rows[ds_[0]], credit_rows[ds_[-1]]
        if a.get("credit_balance") and b.get("credit_balance") is not None:
            s["credit_balance"] = {"from_date": ds_[0], "from": a["credit_balance"], "from_rate": a.get("credit_rate"),
                                   "to_date": ds_[-1], "to": b["credit_balance"], "to_rate": b.get("credit_rate"),
                                   "to_amt_eok": b.get("credit_balance_amt"),
                                   "chg_pct": round((b["credit_balance"] / a["credit_balance"] - 1) * 100, 2),
                                   "note": "매매일 기준, T+2 공표 — 최근 2거래일은 아직 없다"}
    s["big_moves"] = [r["date"] for r in table
                      if r.get("chg_pct") is not None and abs(r["chg_pct"]) >= BIG_MOVE_PCT]
    s["price_mismatch"] = [r["date"] for r in table
                           if r.get("close_diff_pct") is not None and abs(r["close_diff_pct"]) > PRICE_TOL_PCT]
    s["price_checked_days"] = sum(1 for r in table if r.get("close_diff_pct") is not None)
    return s


# ── 출력 ─────────────────────────────────────────────────────

def _f(v, nd=0, sign=False):
    if v is None:
        return "-"
    fmt = f"{{:{'+' if sign else ''},.{nd}f}}"
    return fmt.format(v)


def _date_label(r, s):
    return (f"{r['date']}{' ⚡' if r['date'] in s.get('big_moves', []) else ''}"
            f"{' (장중)' if r.get('partial_day') else ''}")


def render(res: dict) -> str:
    L = [f"## 수급 조회 {res['code']} ({res['from']}~{res['to']})", "", f"출처: {res['source']}", ""]
    s = res["summary"]
    if res["errors"]:
        L += ["**조회 실패**: " + ", ".join(f"{k}={v}" for k, v in res["errors"].items()), ""]
    if s:
        L.append("### 기간 요약")
        p = s.get("price")
        if p:
            L.append(f"- 종가 {p['from_date']} {_f(p['from_close'])} → {p['to_date']} {_f(p['to_close'])}"
                     f" ({_f(p['chg_pct'], 2, True)}%, {p['note']})")
        L.append(f"- 순매수 누적(억원, 장중 당일 제외): 개인 {_f(s.get('sum_individual_amt'), 1, True)} / 외국인 "
                 f"{_f(s.get('sum_foreign_amt'), 1, True)} / 기관 {_f(s.get('sum_institution_amt'), 1, True)}"
                 f" / 프로그램 {_f(s.get('sum_program_net_amt'), 1, True)}")
        detail = [f"{name} {_f(s.get(f'sum_{k}_amt'), 1, True)}" for k, name, *_ in INST_DETAIL
                  if s.get(f"sum_{k}_amt") is not None]
        if detail:
            L.append("- 기관·기타 세부(억원): " + " / ".join(detail))
        if s.get("short_pct_period") is not None:
            m = s["short_pct_max"]
            L.append(f"- 공매도: 누적 {_f(s.get('sum_short_qty'))}주·{_f(s.get('sum_short_amt'), 1)}억원, "
                     f"거래량 대비 {s['short_pct_period']}% (최고 {m['date']} {m['pct']}%)")
        lb = s.get("loan_balance")
        if lb:
            L.append(f"- 대차잔고: {lb['from_date']} {_f(lb['from'])}주 → {lb['to_date']} {_f(lb['to'])}주"
                     f" ({_f(lb['chg_pct'], 2, True)}%, 잔고 평가 {_f(lb['to_amt_eok'], 0)}억원)")
        cb = s.get("credit_balance")
        if cb:
            L.append(f"- 신용융자 잔고: {cb['from_date']} {_f(cb['from'])}주({cb['from_rate']}%) → "
                     f"{cb['to_date']} {_f(cb['to'])}주({cb['to_rate']}%, {_f(cb['to_amt_eok'], 0)}억원) "
                     f"({_f(cb['chg_pct'], 2, True)}%, {cb['note']})")
        if s.get("big_moves"):
            L.append(f"- 급변일(±{BIG_MOVE_PCT:g}% 이상): {', '.join(s['big_moves'])}")
        n = s.get("price_checked_days", 0)
        if s.get("price_mismatch"):
            L.append(f"- ⚠ 로컬 일봉 종가와 {PRICE_TOL_PCT:g}% 넘게 다른 날: {', '.join(s['price_mismatch'])}")
        elif n:
            L.append(f"- 로컬 기준 일봉과 종가 대조: {n}일 모두 오차 {PRICE_TOL_PCT:g}% 이내")
        else:
            L.append("- 로컬 기준 일봉이 이 기간에 없어 종가 교차 검증 못 함")
        L.append("")

    est = res.get("estimate_today") or []
    if est:
        last = est[-1]
        L += ["### 오늘 장중 가집계 (추정, 확정치 아님)",
              " / ".join(f"{e['time']} 외국인 {_f(e['foreign'], 0, True)}·기관 {_f(e['institution'], 0, True)}주"
                         for e in est),
              f"(최신 {last['time']} 입력 기준 합산 {_f(last['sum'], 0, True)}주)", ""]

    L += ["### 일별 ① 주체별 순매수 (최신 먼저)", "",
          "| 날짜 | 종가 | 등락% | 거래량 | 개인(억) | 외국인(억) | 기관(억) | 투신(억) | 사모(억) "
          "| 연기금(억) | 기타법인(억) | 프로그램(억) |",
          "|" + "---|" * 12]
    for r in res["rows"]:
        L.append("| " + " | ".join([
            _date_label(r, s), _f(r.get("close")), _f(r.get("chg_pct"), 2, True), _f(r.get("volume")),
            _f(r.get("individual_amt"), 1, True), _f(r.get("foreign_amt"), 1, True),
            _f(r.get("institution_amt"), 1, True), _f(r.get("trust_amt"), 1, True),
            _f(r.get("pef_amt"), 1, True), _f(r.get("pension_amt"), 1, True),
            _f(r.get("other_corp_amt"), 1, True), _f(r.get("program_net_amt"), 1, True)]) + " |")
    L += ["", "### 일별 ② 공매도·대차·신용 (최신 먼저)", "",
          "| 날짜 | 공매도(주) | 공매도% | 대차증감 | 대차잔고 | 신용잔고 | 신용잔고율% | 신용신규 | 신용상환 |",
          "|" + "---|" * 9]
    for r in res["rows"]:
        L.append("| " + " | ".join([
            _date_label(r, s), _f(r.get("short_qty")), _f(r.get("short_pct"), 2),
            _f(r.get("loan_change"), 0, True), _f(r.get("loan_balance")),
            _f(r.get("credit_balance")), _f(r.get("credit_rate"), 2),
            _f(r.get("credit_new")), _f(r.get("credit_repay"))]) + " |")

    ops = res.get("opinions") or []
    L += ["", f"### 증권사 투자의견 ({res['opinion_from']}~{res['to']}, 최신 먼저)", ""]
    if ops:
        L += ["| 날짜 | 증권사 | 의견(직전) | 목표가 | 전일종가 | 기간 내 |", "|---|---|---|---|---|---|"]
        for o in ops:
            prev = f" ({o['prev_opinion']})" if o["prev_opinion"] and o["prev_opinion"] != o["opinion"] else ""
            inside = "●" if res["from"] <= o["date"] <= res["to"] else ""
            L.append(f"| {o['date']} | {o['broker']} | {o['opinion']}{prev} | {_f(o['target'])} "
                     f"| {_f(o['prev_close'])} | {inside} |")
    else:
        L.append("- 이 기간 KIS에 등록된 투자의견 없음")

    L += ["", "### 읽는 법·한계",
          "- 순매수 금액은 억원(KIS 백만원 단위 환산). 기관 = 투신·사모·연기금·보험·은행·증권·종금 등의 합. "
          "기타법인은 기관에 들지 않는다",
          "- 프로그램 = 차익+비차익 순매수 대금. 외국인·기관 매매와 겹친다(따로 더하지 않는다)",
          "- 공매도% = 그날 KRX 거래량 대비 공매도 체결 비중. 당일 값은 익일 공표라 '-'",
          "- 대차잔고 = 빌려 간 주식 잔고(공매도 대기 물량의 상한 지표). 대차증감 = 전일 대비",
          "- 신용잔고 = 신용융자 매수 잔고(금액은 KIS 만원 단위 환산). 매매일 기준이며 T+2에 공표돼 최근 2거래일은 비어 있다",
          "- 투자의견 목표가는 증권사가 낸 값 그대로다. 직전 목표가는 KIS가 주지 않는다",
          "- 장중 실행 시 당일 행은 잠정이다(거래량·종가·프로그램이 아직 움직인다)"]
    L += [f"- {n}" for n in res["notes"]]
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="KIS 종목 수급 조회 (투자자·프로그램·공매도·대차·신용·투자의견)")
    ap.add_argument("code", help="6자리 종목코드")
    ap.add_argument("--days", type=int, default=14, help="오늘부터 거슬러 볼 달력일 (기본 14)")
    ap.add_argument("--from", dest="start", help="시작일 YYYYMMDD")
    ap.add_argument("--to", dest="end", help="종료일 YYYYMMDD (기본 오늘)")
    ap.add_argument("--market", default="J", choices=["J", "NX", "UN"],
                    help="투자자·프로그램 시장 구분: J=KRX(기본), NX=NXT, UN=통합")
    ap.add_argument("--opinion-days", type=int, default=90, help="투자의견을 거슬러 볼 달력일 (기본 90)")
    ap.add_argument("--json", action="store_true", help="JSON 출력")
    a = ap.parse_args(argv)
    code = a.code.strip()
    if not (len(code) == 6 and code.isalnum()):
        ap.error("종목코드는 6자리")
    end = datetime.strptime(a.end, "%Y%m%d").date() if a.end else date.today()
    start = datetime.strptime(a.start, "%Y%m%d").date() if a.start else end - timedelta(days=a.days)
    res = collect(code, start, end, market=a.market, opinion_days=a.opinion_days)
    print(json.dumps(res, ensure_ascii=False, indent=1) if a.json else render(res))


if __name__ == "__main__":
    main()

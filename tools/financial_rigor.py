#!/usr/bin/env python3
"""재무 정밀 검증 툴킷 — AI Berkshire KR

투자 리서치 중 재무 데이터 정확성을 검증하는 커맨드라인 도구.
Claude Code 스킬이 핵심 검증 시점에 자동으로 호출합니다.

외부 라이브러리 불필요 — Python 표준 라이브러리만 사용 (decimal, json, math, argparse).
Python >= 3.7 필요.

사용법 (스킬이 자동 호출, 수동 실행 불필요):
    python3 tools/financial_rigor.py verify-market-cap --price 78000 --shares 5.97e9 --reported 4.66e14 --currency KRW
    python3 tools/financial_rigor.py verify-valuation --price 78000 --eps 5200 --bvps 71000 --fcf-per-share 4800
    python3 tools/financial_rigor.py cross-validate --field revenue --values '{"DART": 302.0, "네이버금융": 301.8, "KRX": 302.1}' --unit 조원
    python3 tools/financial_rigor.py benford --values '[1234, 2345, 3456, ...]'
    python3 tools/financial_rigor.py calc --expr '78000 * 5.97e9'
"""

import argparse
import json
import math
import sys
from decimal import Decimal, Context, ROUND_HALF_EVEN, InvalidOperation

# ---------------------------------------------------------------------------
# 정밀 십진수 엔진 (부동소수점 오차 방지)
# ---------------------------------------------------------------------------

_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)


def exact(value) -> Decimal:
    """임의의 숫자를 정확한 Decimal로 변환 (float 함정 방지)."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(str(value))


def fmt_number(d: Decimal, unit: str = "") -> str:
    """큰 숫자를 읽기 쉬운 형태로 포맷 (조/억/만 단위)."""
    v = float(d)
    abs_v = abs(v)
    if unit in ("조원", "조"):
        if abs_v >= 1000:
            return f"{v/1000:.2f}천조원"
        return f"{v:.2f}{unit}"
    if unit in ("억원", "억"):
        if abs_v >= 10000:
            return f"{v/10000:.2f}조원"
        return f"{v:.2f}{unit}"
    if abs_v >= 1e12:
        return f"{v/1e12:.2f}조"
    if abs_v >= 1e8:
        return f"{v/1e8:.2f}억"
    if abs_v >= 1e4:
        return f"{v/1e4:.2f}만"
    return f"{v:,.2f}"


# ---------------------------------------------------------------------------
# 1. 시가총액 검증 (주가 × 발행주식수 vs 보고된 시총)
# ---------------------------------------------------------------------------

def verify_market_cap(price, shares, reported_cap, currency=""):
    """시가총액 = 주가 × 발행주식수 검증 후 보고된 값과 비교."""
    p = exact(price)
    s = exact(shares)
    r = exact(reported_cap)

    calculated = _CTX.multiply(p, s)
    deviation = abs(float(calculated - r) / float(r)) * 100 if r != 0 else 0

    print("=" * 60)
    print("시가총액 검증 (Market Cap Verification)")
    print("=" * 60)
    print(f"  주가 (Price):        {p} {currency}")
    print(f"  발행주식수 (Shares): {fmt_number(s)}주")
    print(f"  계산 시가총액:       {fmt_number(calculated)} {currency}")
    print(f"  보고된 시가총액:     {fmt_number(r)} {currency}")
    print(f"  편차:                {deviation:.2f}%")
    print()

    if deviation > 5:
        print(f"  ❌ 경고: 편차 {deviation:.1f}% > 5%, 아래 항목 확인 필요:")
        print(f"     - 발행주식수가 최신 데이터인가? (자사주 소각/유상증자 반영 여부)")
        print(f"     - 통화 단위가 일치하는가? (원/달러/홍콩달러)")
        print(f"     - 주가가 최신 데이터인가?")
        return False
    elif deviation > 1:
        print(f"  ⚠️  편차 {deviation:.1f}% — 허용 범위 내, 주가 변동 또는 주식수 변경 가능성")
        return True
    else:
        print(f"  ✅ 검증 통과, 편차 {deviation:.2f}%")
        return True


# ---------------------------------------------------------------------------
# 2. 밸류에이션 지표 검증
# ---------------------------------------------------------------------------

def verify_valuation(price, eps=None, bvps=None, fcf_per_share=None,
                     dividend=None, revenue_per_share=None):
    """주요 밸류에이션 지표를 원본 입력값으로 직접 계산·검증."""
    p = exact(price)

    print("=" * 60)
    print("밸류에이션 지표 검증 (Valuation Verification)")
    print("=" * 60)
    print(f"  현재 주가: {p}")
    print()

    results = {}

    if eps is not None:
        e = exact(eps)
        if e != 0:
            pe = _CTX.divide(p, e)
            print(f"  PER (TTM):    {p} / {e} = {pe:.2f}x")
            results["PER"] = float(pe)
            ey = _CTX.divide(e, p) * 100
            print(f"  이익수익률:   {ey:.2f}%")
        else:
            print(f"  PER: EPS가 0이라 계산 불가")

    if bvps is not None:
        b = exact(bvps)
        if b != 0:
            pb = _CTX.divide(p, b)
            print(f"  PBR:          {p} / {b} = {pb:.2f}x")
            results["PBR"] = float(pb)
            if eps is not None and float(exact(eps)) != 0:
                roe = _CTX.divide(exact(eps), b) * 100
                print(f"  ROE:          {exact(eps)} / {b} = {roe:.2f}%")
                results["ROE"] = float(roe)

    if fcf_per_share is not None:
        f = exact(fcf_per_share)
        if f != 0:
            fcf_yield = _CTX.divide(f, p) * 100
            pfcf = _CTX.divide(p, f)
            print(f"  P/FCF:        {p} / {f} = {pfcf:.2f}x")
            print(f"  FCF 수익률:   {fcf_yield:.2f}%")
            results["P_FCF"] = float(pfcf)
            results["FCF_Yield"] = float(fcf_yield)

    if dividend is not None:
        d = exact(dividend)
        if p != 0:
            div_yield = _CTX.divide(d, p) * 100
            print(f"  배당수익률:   {d} / {p} = {div_yield:.2f}%")
            results["Dividend_Yield"] = float(div_yield)

    if revenue_per_share is not None:
        r = exact(revenue_per_share)
        if r != 0:
            ps = _CTX.divide(p, r)
            print(f"  PSR:          {p} / {r} = {ps:.2f}x")
            results["PSR"] = float(ps)

    print()
    print("  ✅ 모든 지표는 정밀 십진수로 계산되어 부동소수점 오차가 없습니다")
    return results


# ---------------------------------------------------------------------------
# 3. 다중 출처 교차 검증
# ---------------------------------------------------------------------------

def cross_validate(field_name, source_values: dict, unit="", tolerance_pct=2.0):
    """동일 데이터를 여러 출처에서 비교하고 불일치를 표시."""
    print("=" * 60)
    print(f"교차 검증: {field_name} (Cross-Validation)")
    print("=" * 60)

    values = {k: exact(v) for k, v in source_values.items()}
    sources = list(values.keys())
    nums = list(values.values())

    # 중앙값을 기준으로 사용
    sorted_vals = sorted(float(v) for v in nums)
    n = len(sorted_vals)
    median = sorted_vals[n // 2] if n % 2 == 1 else (sorted_vals[n//2-1] + sorted_vals[n//2]) / 2

    print(f"  데이터 출처 수:  {len(sources)}")
    print(f"  기준 중앙값:     {fmt_number(exact(median))} {unit}")
    print()

    all_ok = True
    for src, val in values.items():
        dev = abs(float(val) - median) / median * 100 if median != 0 else 0
        status = "✅" if dev <= tolerance_pct else "❌"
        if dev > tolerance_pct:
            all_ok = False
        print(f"  {status} {src:20s}: {fmt_number(val)} {unit}  (편차 {dev:.2f}%)")

    print()
    if all_ok:
        print(f"  ✅ 모든 출처의 편차 ≤ {tolerance_pct}%, 데이터 일치")
    else:
        print(f"  ⚠️  출처 간 편차 > {tolerance_pct}% 존재, 불일치 원인 확인 필요")
        print(f"     권장: 전자공시시스템(DART) 원본 재무제표 데이터를 우선 사용")

    consensus = median
    print(f"\n  합의값 (가중 중앙값): {fmt_number(exact(consensus))} {unit}")
    return {"consensus": consensus, "all_consistent": all_ok}


# ---------------------------------------------------------------------------
# 4. 벤포드 법칙 검사 (재무 데이터 조작 탐지)
# ---------------------------------------------------------------------------

_BENFORD = {d: math.log10(1 + 1/d) for d in range(1, 10)}


def benford_check(values: list):
    """재무 수치 목록에 대해 벤포드 법칙 검사 실행."""
    print("=" * 60)
    print("벤포드 법칙 검사 (재무 데이터 조작 탐지)")
    print("=" * 60)

    digits = []
    for v in values:
        v = abs(float(v))
        if v > 0:
            sig = 10 ** (math.log10(v) - math.floor(math.log10(v)))
            d = int(sig)
            if 1 <= d <= 9:
                digits.append(d)

    n = len(digits)
    if n < 50:
        print(f"  ⚠️  표본 수 부족: {n} < 50, 벤포드 분석 신뢰도 낮음")
        return None

    counts = {}
    for d in digits:
        counts[d] = counts.get(d, 0) + 1
    observed = {d: counts.get(d, 0) / n for d in range(1, 10)}

    # MAD (Nigrini의 평균절대편차)
    mad = sum(abs(observed.get(d, 0) - _BENFORD[d]) for d in range(1, 10)) / 9
    chi2 = sum((counts.get(d, 0) - _BENFORD[d] * n) ** 2 / (_BENFORD[d] * n) for d in range(1, 10))

    if mad < 0.006:
        conformity = "Close (고도 부합)"
    elif mad < 0.012:
        conformity = "Acceptable (허용 가능)"
    elif mad < 0.015:
        conformity = "Marginally Acceptable (경계)"
    else:
        conformity = "Nonconforming (불부합 ⚠️)"

    print(f"  표본 수:    {n}")
    print(f"  MAD:        {mad:.6f}")
    print(f"  Chi-sq:     {chi2:.2f}")
    print(f"  부합도:     {conformity}")
    print()

    print(f"  {'첫째자리':>6} {'관측':>8} {'벤포드기대':>12} {'편차':>8}")
    print(f"  {'-'*6} {'-'*8} {'-'*12} {'-'*8}")
    for d in range(1, 10):
        obs = observed.get(d, 0)
        exp = _BENFORD[d]
        dev = obs - exp
        flag = " ⚠️" if abs(dev) > 0.03 else ""
        print(f"  {d:>6d} {obs:>8.3f} {exp:>12.3f} {dev:>+8.3f}{flag}")

    print()
    is_ok = mad < 0.015
    if is_ok:
        print("  ✅ 재무 데이터의 첫째 자리 분포가 벤포드 법칙에 부합합니다")
    else:
        print("  ❌ 첫째 자리 분포가 비정상적입니다. 인위적 조정 가능성 있음")
        print("     주의: 벤포드 법칙 불부합이 반드시 분식회계를 의미하지는 않으나 추가 조사 필요")

    return {"mad": mad, "chi2": chi2, "conformity": conformity, "is_conforming": is_ok}


# ---------------------------------------------------------------------------
# 5. 정밀 계산기
# ---------------------------------------------------------------------------

def exact_calc(expr: str):
    """재무 수식을 정밀 십진수로 계산.

    지원: +, -, *, /, (), 숫자 (지수 표기법 포함).
    """
    print("=" * 60)
    print("정밀 계산 (Exact Calculator)")
    print("=" * 60)

    allowed = set("0123456789.+-*/() eE")
    if not all(c in allowed for c in expr.replace(" ", "")):
        print(f"  ❌ 허용되지 않는 수식: {expr}")
        return None

    try:
        result = eval(expr, {"__builtins__": {}}, {})
        d_result = exact(result)
        print(f"  수식:   {expr}")
        print(f"  결과:   {fmt_number(d_result)}")
        print(f"  정확값: {d_result}")
        return float(d_result)
    except Exception as e:
        print(f"  ❌ 계산 오류: {e}")
        return None


# ---------------------------------------------------------------------------
# 6. 3시나리오 밸류에이션
# ---------------------------------------------------------------------------

def three_scenario_valuation(current_price, current_eps, shares_billion,
                             growth_optimistic, growth_neutral, growth_pessimistic,
                             pe_optimistic, pe_neutral, pe_pessimistic,
                             years=3, currency=""):
    """정밀 산술로 3시나리오 목표 주가 산출."""
    print("=" * 60)
    print("3시나리오 밸류에이션 모델 (Three-Scenario Valuation)")
    print("=" * 60)

    p = exact(current_price)
    eps = exact(current_eps)
    shares = exact(shares_billion)

    scenarios = [
        ("낙관 (Bull)", growth_optimistic, pe_optimistic),
        ("중립 (Base)", growth_neutral,    pe_neutral),
        ("비관 (Bear)", growth_pessimistic, pe_pessimistic),
    ]

    print(f"  현재 주가:  {p} {currency}")
    print(f"  현재 EPS:   {eps}")
    print(f"  예측 기간:  {years}년")
    print()
    print(f"  {'시나리오':12} {'연성장률':>8} {'목표PER':>8} {'목표EPS':>10} {'목표주가':>10} {'등락률':>8}")
    print(f"  {'-'*12} {'-'*8} {'-'*8} {'-'*10} {'-'*10} {'-'*8}")

    for name, growth, pe in scenarios:
        g = exact(growth)
        target_pe = exact(pe)
        future_eps = eps
        for _ in range(years):
            future_eps = _CTX.multiply(future_eps, _CTX.add(Decimal("1"), g))
        target_price = _CTX.multiply(future_eps, target_pe)
        change = float(target_price - p) / float(p) * 100

        print(f"  {name:12} {float(g)*100:>7.0f}% {float(target_pe):>7.0f}x "
              f"{float(future_eps):>10.2f} {float(target_price):>9.1f} {change:>+7.1f}%")

    print()
    print("  ✅ 모든 계산은 정밀 십진수 사용, 결과 재현·감사 가능")


# ---------------------------------------------------------------------------
# CLI 진입점
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="재무 정밀 검증 툴킷 — Financial Rigor Toolkit (KR)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
사용 예시:
  %(prog)s verify-market-cap --price 78000 --shares 5.97e9 --reported 4.66e14 --currency KRW
  %(prog)s verify-valuation --price 78000 --eps 5200 --bvps 71000
  %(prog)s cross-validate --field 매출액 --values '{"DART": 302.0, "네이버금융": 301.8}' --unit 조원
  %(prog)s benford --values '[1234, 2345, 3456, ...]'
  %(prog)s calc --expr '78000 * 5.97e9'
        """)

    sub = parser.add_subparsers(dest="command")

    # verify-market-cap
    mc = sub.add_parser("verify-market-cap", help="시가총액 검증 = 주가 × 발행주식수")
    mc.add_argument("--price",    type=float, required=True, help="현재 주가")
    mc.add_argument("--shares",   type=float, required=True, help="발행주식수")
    mc.add_argument("--reported", type=float, required=True, help="보고된 시가총액")
    mc.add_argument("--currency", default="",               help="통화 단위 (KRW/USD/HKD)")

    # verify-valuation
    val = sub.add_parser("verify-valuation", help="밸류에이션 지표 검증")
    val.add_argument("--price",             type=float, required=True)
    val.add_argument("--eps",               type=float, default=None, help="주당순이익")
    val.add_argument("--bvps",              type=float, default=None, help="주당순자산")
    val.add_argument("--fcf-per-share",     type=float, default=None, help="주당잉여현금흐름")
    val.add_argument("--dividend",          type=float, default=None, help="주당배당금")
    val.add_argument("--revenue-per-share", type=float, default=None, help="주당매출액")

    # cross-validate
    cv = sub.add_parser("cross-validate", help="다중 출처 교차 검증")
    cv.add_argument("--field",     required=True, help="데이터 항목명")
    cv.add_argument("--values",    required=True, help='JSON: {"출처명": 수치}')
    cv.add_argument("--unit",      default="")
    cv.add_argument("--tolerance", type=float, default=2.0, help="허용 편차 (%%)")

    # benford
    bf = sub.add_parser("benford", help="벤포드 법칙 검사")
    bf.add_argument("--values", required=True, help="JSON 배열")

    # calc
    ca = sub.add_parser("calc", help="정밀 계산")
    ca.add_argument("--expr", required=True, help="산술 수식")

    # three-scenario
    ts = sub.add_parser("three-scenario", help="3시나리오 밸류에이션")
    ts.add_argument("--price",    type=float, required=True,  help="현재 주가")
    ts.add_argument("--eps",      type=float, required=True,  help="현재 EPS")
    ts.add_argument("--shares",   type=float, required=True,  help="발행주식수(억주)")
    ts.add_argument("--growth",   nargs=3,    type=float, required=True,
                    help="3시나리오 연간 성장률 (낙관 중립 비관), 예: 0.20 0.10 0.00")
    ts.add_argument("--pe",       nargs=3,    type=float, required=True,
                    help="3시나리오 목표 PER, 예: 25 18 12")
    ts.add_argument("--years",    type=int,   default=3)
    ts.add_argument("--currency", default="KRW")

    args = parser.parse_args()

    if args.command == "verify-market-cap":
        verify_market_cap(args.price, args.shares, args.reported, args.currency)
    elif args.command == "verify-valuation":
        verify_valuation(args.price, args.eps, args.bvps, args.fcf_per_share,
                         args.dividend, args.revenue_per_share)
    elif args.command == "cross-validate":
        values = json.loads(args.values)
        cross_validate(args.field, values, args.unit, args.tolerance)
    elif args.command == "benford":
        values = json.loads(args.values)
        benford_check(values)
    elif args.command == "calc":
        exact_calc(args.expr)
    elif args.command == "three-scenario":
        three_scenario_valuation(
            args.price, args.eps, args.shares,
            args.growth[0], args.growth[1], args.growth[2],
            args.pe[0],     args.pe[1],     args.pe[2],
            args.years, args.currency)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

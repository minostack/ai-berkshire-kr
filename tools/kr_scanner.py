#!/usr/bin/env python3
"""
한국 주식 종목 발굴 스캐너 — KRX + DART + 네이버금융
kr-stock-scanner 스킬이 자동 호출합니다.

사용법:
    python3 tools/kr_scanner.py list-sectors          # KRX 섹터 목록
    python3 tools/kr_scanner.py sector 반도체         # 섹터 종목 리스트
    python3 tools/kr_scanner.py screen                # 전체 기본 필터 스캔
    python3 tools/kr_scanner.py screen --roe 12 --pbr 1.5  # 조건 지정 스캔
    python3 tools/kr_scanner.py info 009150           # 단일 종목 재무 요약
"""

import argparse
import json
import os
import subprocess
import sys
from urllib.parse import urlencode, quote

_TIMEOUT = 20


def _curl(url: str, referer: str = "https://finance.naver.com/") -> str:
    result = subprocess.run(
        ["curl", "-s", "--noproxy", "*",
         "-H", f"Referer: {referer}",
         "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
         "-H", "Accept: application/json, text/html",
         url],
        capture_output=True, timeout=_TIMEOUT
    )
    if result.returncode != 0:
        raise ConnectionError(f"요청 실패: {url}")
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return result.stdout.decode("euc-kr", errors="replace")


def _curl_json(url: str) -> dict | list:
    return json.loads(_curl(url))


def _fmt_krw(v) -> str:
    if v is None:
        return "-"
    try:
        v = float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return str(v)
    if abs(v) >= 1e12:
        return f"{v/1e12:.1f}조"
    if abs(v) >= 1e8:
        return f"{v/1e8:.0f}억"
    return f"{v:,.0f}"


# ---------------------------------------------------------------------------
# KRX 섹터 목록
# ---------------------------------------------------------------------------

KRX_SECTORS = {
    "KOSPI": [
        "음식료품", "섬유의복", "종이목재", "화학", "의약품",
        "비금속광물", "철강금속", "기계", "전기전자", "의료정밀",
        "운수장비", "유통업", "전기가스업", "건설업", "운수창고업",
        "통신업", "금융업", "은행", "증권", "보험",
        "서비스업", "제조업",
    ],
    "KOSDAQ": [
        "기술성장기업부", "IT부품", "IT소프트웨어", "IT하드웨어",
        "반도체", "디지털컨텐츠", "소프트웨어", "컴퓨터서비스",
        "통신장비", "통신서비스", "제약", "의료·바이오", "건강관리",
        "음식료·담배", "섬유·의류", "종이·목재", "화학", "비금속",
        "금속", "기계·장비", "일반전기전자", "운송장비·부품",
        "유통", "방송서비스", "에너지", "건설", "운송",
    ],
}


def cmd_list_sectors():
    """KRX 섹터 목록 출력."""
    print("=" * 60)
    print("KRX 섹터 목록")
    print("=" * 60)
    print("\n  [KOSPI 업종]")
    for s in KRX_SECTORS["KOSPI"]:
        print(f"    {s}")
    print("\n  [KOSDAQ 업종]")
    for s in KRX_SECTORS["KOSDAQ"]:
        print(f"    {s}")
    print()
    print("  사용법: python3 tools/kr_scanner.py sector 반도체")


# ---------------------------------------------------------------------------
# 네이버금융 업종별 종목 리스트
# ---------------------------------------------------------------------------

# 네이버금융 업종 코드 매핑 (주요 업종)
_NAVER_GROUP_CODES = {
    "전기전자":   "G25",
    "반도체":     "G25",
    "화학":       "G15",
    "의약품":     "G35",
    "바이오":     "G35",
    "제약":       "G35",
    "자동차":     "G25",
    "운수장비":   "G25",
    "건설":       "G15",
    "은행":       "G40",
    "금융":       "G40",
    "통신":       "G50",
    "소프트웨어": "G45",
    "IT":         "G45",
    "유통":       "G25",
    "철강":       "G15",
    "에너지":     "G10",
    "방산":       "G20",
}


def _get_sector_stocks_naver(sector: str) -> list:
    """네이버금융 업종 시세에서 종목 리스트 수집."""
    # 업종 코드 탐색
    group_code = None
    for key, code in _NAVER_GROUP_CODES.items():
        if key in sector or sector in key:
            group_code = code
            break

    results = []

    # 방법 1: 네이버금융 업종 시세 페이지 WebSearch 안내
    search_url = f"https://finance.naver.com/sise/sise_group_detail.naver?type=GROUP&group_code={group_code or 'G25'}"

    # 방법 2: 네이버금융 종목 검색 API
    try:
        url = f"https://ac.finance.naver.com/ac?q={quote(sector)}&q_enc=UTF-8&target=stock"
        raw  = _curl(url)
        data = json.loads(raw)
        items = data.get("items", [[]])[0]
        for item in items[:30]:
            if len(item) >= 2:
                results.append({
                    "name": item[0],
                    "code": item[1],
                    "market": "KOSPI/KOSDAQ",
                })
    except Exception:
        pass

    return results


def _get_stock_basic(code: str) -> dict:
    """네이버금융 실시간 API로 기본 재무 지표 수집."""
    try:
        url  = f"https://polling.finance.naver.com/api/realtime/domestic/stock/{code}"
        data = _curl_json(url)
        item = data.get("datas", [{}])[0]
        return {
            "name":       item.get("stockName", ""),
            "price":      item.get("closePrice", 0),
            "market_cap": item.get("marketValue", 0),
            "per":        item.get("per", None),
            "pbr":        item.get("pbr", None),
            "eps":        item.get("eps", None),
            "bps":        item.get("bps", None),
            "div_yield":  item.get("dividendYield", None),
            "foreign_r":  item.get("foreignRatio", None),
        }
    except Exception:
        return {}


def cmd_sector(sector: str):
    """섹터 종목 리스트 출력."""
    print("=" * 60)
    print(f"섹터 종목 리스트: {sector}")
    print("=" * 60)

    stocks = _get_sector_stocks_naver(sector)

    if not stocks:
        print(f"\n  ⚠️  '{sector}' 관련 종목을 자동으로 찾지 못했습니다.")
        print(f"  아래 출처에서 직접 확인하세요:")
        print(f"  - KRX: https://data.krx.co.kr/contents/MDC/STAT/standard/MDCSTAT03901.cmd")
        print(f"  - 네이버: https://finance.naver.com/sise/sise_group.naver?type=GROUP")
        return

    print(f"\n  검색된 종목: {len(stocks)}개\n")
    print(f"  {'종목명':<15} {'코드':<8} {'시장'}")
    print(f"  {'─'*15} {'─'*8} {'─'*10}")
    for s in stocks:
        print(f"  {s['name']:<15} {s['code']:<8} {s['market']}")

    print(f"\n  💡 상세 재무 확인: python3 tools/kr_scanner.py info {{종목코드}}")
    print(f"  💡 전체 스캔: python3 tools/kr_scanner.py screen --sector {sector}")


# ---------------------------------------------------------------------------
# 종목 재무 요약
# ---------------------------------------------------------------------------

def cmd_info(code: str):
    """단일 종목 재무 요약 출력 (스크리너 판단 지원)."""
    d = _get_stock_basic(code)

    if not d or not d.get("name"):
        print(f"  ❌ {code} 데이터 수집 실패")
        print(f"  네이버금융: https://finance.naver.com/item/coinfo.naver?code={code}")
        return

    print("=" * 60)
    print(f"종목 재무 요약: {d['name']} ({code})")
    print("=" * 60)

    # 기본 시세
    print(f"\n  [시세]")
    print(f"  현재가:      {d['price']}원")
    print(f"  시가총액:    {_fmt_krw(d['market_cap'])}원")

    # 밸류에이션
    print(f"\n  [밸류에이션]")
    per = d.get("per")
    pbr = d.get("pbr")
    div = d.get("div_yield")
    print(f"  PER:         {per if per else '-'}배")
    print(f"  PBR:         {pbr if pbr else '-'}배")
    print(f"  EPS:         {d.get('eps', '-')}원")
    print(f"  BPS:         {d.get('bps', '-')}원")
    print(f"  배당수익률:  {div if div else '-'}%")
    print(f"  외국인 비중: {d.get('foreign_r', '-')}%")

    # 1차 정량 필터 즉시 판단
    print(f"\n  [1차 정량 필터 즉시 판단]")
    flags = []
    if per and float(str(per).replace(",","")) < 5:
        flags.append("⚠️  PER 5배 미만 — 실적 악화 또는 극도 저평가")
    if per and float(str(per).replace(",","")) > 100:
        flags.append("⚠️  PER 100배 초과 — 고성장 기대 또는 거품 주의")
    if pbr and float(str(pbr).replace(",","")) < 0.5:
        flags.append("✅  PBR 0.5배 미만 — 자산 대비 극도 저평가")
    if pbr and float(str(pbr).replace(",","")) < 1.0:
        flags.append("✅  PBR 1배 미만 — 자산가치 이하")

    if flags:
        for f in flags:
            print(f"  {f}")
    else:
        print(f"  특이 신호 없음 — 심층 재무 분석 필요")

    # DART 원본 링크
    print(f"\n  [원본 데이터 확인]")
    print(f"  DART:   https://dart.fss.or.kr/dsab001/main.do (검색: {code})")
    print(f"  네이버: https://finance.naver.com/item/coinfo.naver?code={code}&target=finsum_more")
    print(f"  KRX:    http://www.krx.co.kr/")

    # 심층 분석 안내
    print(f"\n  [다음 단계]")
    print(f"  심층 재무: python3 tools/kstock_data.py financials {code}")
    print(f"  4대가 분석: /investment-team {d['name']}({code})")


# ---------------------------------------------------------------------------
# 전체 스캔 (기본 필터 적용)
# ---------------------------------------------------------------------------

def cmd_screen(
    min_roe: float = 10.0,
    max_pbr: float = 2.0,
    min_cap_bn: float = 500.0,
    sector: str = None,
    growth_mode: bool = False,
):
    """
    기본 조건으로 KOSPI/KOSDAQ 투자 후보 스캔.

    네이버금융 + DART 데이터 기반.
    완전 자동화는 API 한계로 불가능하므로,
    Claude가 WebSearch와 병행해서 사용하도록 설계.
    """
    print("=" * 60)
    print("한국 주식 투자 후보 스캔")
    print("=" * 60)
    print(f"\n  적용 필터:")
    if growth_mode:
        print(f"  모드:        성장주 트랙")
        print(f"  매출 성장률: ≥ 20% (2년 연평균)")
        print(f"  매출총이익률: ≥ 30%")
    else:
        print(f"  모드:        가치주 트랙")
        print(f"  최소 ROE:   {min_roe}%")
        print(f"  최대 PBR:   {max_pbr}배")
    print(f"  최소 시총:  {min_cap_bn:.0f}억원")
    if sector:
        print(f"  섹터:       {sector}")

    print(f"\n  ⚠️  자동 전수 스캔은 API 제약으로 제한됩니다.")
    print(f"  Claude가 아래 출처를 WebSearch로 보완해서 스캔합니다:\n")

    # WebSearch 가이드 출력 (Claude가 이 정보를 보고 검색함)
    print(f"  📌 [Claude WebSearch 가이드]")
    print(f"  검색 키워드 1: \"KOSPI KOSDAQ ROE {min_roe}% 이상 PBR {max_pbr}배 이하 종목\"")
    print(f"  검색 키워드 2: \"한국 가치주 저PBR 고ROE 스크리닝 {__import__('datetime').date.today().year}\"")
    if sector:
        print(f"  검색 키워드 3: \"{sector} 업종 재무우량주 시가총액 순위\"")
    print(f"  검색 키워드 4: \"KRX 업종별 ROE 순위 우량주\"")

    print(f"\n  📌 [데이터 출처 직접 접속]")
    print(f"  KRX 전종목 시세: https://data.krx.co.kr/contents/MDC/STAT/standard/MDCSTAT03901.cmd")
    print(f"  네이버 업종별:   https://finance.naver.com/sise/sise_group.naver?type=GROUP")
    print(f"  에프앤가이드:    https://www.fnguide.com/")

    print(f"\n  📌 [DART 재무비율 일괄 조회]")
    dart_key = os.environ.get("DART_API_KEY")
    if dart_key:
        print(f"  ✅ DART API 키 등록됨 — 재무비율 API 자동 조회 가능")
        print(f"  API: https://opendart.fss.or.kr/api/fnRatioAll.json")
        print(f"       ?crtfc_key={dart_key[:8]}... &bsns_year=2024 &reprt_code=11011")
    else:
        print(f"  ⚠️  DART API 키 미등록 — WebSearch로 대체")
        print(f"  키 등록: export DART_API_KEY=발급받은키")

    print(f"\n  💡 스캔 완료 후 후보 종목은 아래로 심층 분석하세요:")
    print(f"  /investment-team {{종목명}}({{코드}})     — 4대가 병렬 분석")
    print(f"  /investment-research {{종목명}}({{코드}}) — 단일 심층 분석")


# ---------------------------------------------------------------------------
# DART 재무비율 일괄 조회 (API 키 있을 때)
# ---------------------------------------------------------------------------

def cmd_dart_ratio(year: int = 2024, market: str = "Y"):
    """
    DART fnRatioAll API로 전 상장사 재무비율 일괄 조회.
    ROE·부채비율·영업이익률 기준 필터링.
    """
    dart_key = os.environ.get("DART_API_KEY")
    if not dart_key:
        print("  ❌ DART_API_KEY 환경변수가 필요합니다.")
        print("  export DART_API_KEY=발급받은키")
        return

    print("=" * 60)
    print(f"DART 전 상장사 재무비율 조회 ({year}년)")
    print("=" * 60)
    print(f"  데이터 로드 중...")

    url = "https://opendart.fss.or.kr/api/fnRatioAll.json"
    params = {
        "crtfc_key":  dart_key,
        "bsns_year":  str(year),
        "reprt_code": "11011",  # 사업보고서
    }

    try:
        data  = _curl_json(f"{url}?{urlencode(params)}")
        items = data.get("list", [])
        if not items:
            print(f"  ⚠️  데이터 없음 (status: {data.get('status')}, {data.get('message')})")
            return
    except Exception as e:
        print(f"  ❌ API 오류: {e}")
        return

    print(f"  전체 데이터: {len(items):,}개 상장사")
    print(f"  필터 적용 중...")

    # 필터: ROE ≥ 10%, 부채비율 ≤ 150%, 영업이익률 ≥ 8%
    passed = []
    for item in items:
        try:
            roe     = float(item.get("roe",    "0").replace(",", "") or "0")
            de      = float(item.get("de_rt",  "999").replace(",", "") or "999")
            op_rt   = float(item.get("sale_op_rt", "0").replace(",", "") or "0")
        except (ValueError, TypeError):
            continue

        if roe >= 10.0 and de <= 150.0 and op_rt >= 8.0:
            passed.append({
                "corp_name": item.get("corp_name", ""),
                "stock_code": item.get("stock_code", ""),
                "roe":    roe,
                "de_rt":  de,
                "op_rt":  op_rt,
            })

    # ROE 순 정렬
    passed.sort(key=lambda x: x["roe"], reverse=True)

    print(f"  필터 통과: {len(passed)}개\n")
    print(f"  {'종목명':<18} {'코드':<8} {'ROE':>6} {'부채비율':>8} {'영업이익률':>10}")
    print(f"  {'─'*18} {'─'*8} {'─'*6} {'─'*8} {'─'*10}")

    for p in passed[:50]:  # 상위 50개만 출력
        print(f"  {p['corp_name']:<18} {p['stock_code']:<8} "
              f"{p['roe']:>5.1f}% {p['de_rt']:>7.1f}% {p['op_rt']:>9.1f}%")

    if len(passed) > 50:
        print(f"\n  ... 외 {len(passed)-50}개 (ROE 순 상위 50개만 표시)")

    print(f"\n  📌 다음 단계: 위 종목을 /investment-team 으로 심층 분석하세요")


# ---------------------------------------------------------------------------
# CLI 진입점
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="한국 주식 종목 발굴 스캐너 — KRX + DART + 네이버금융",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
사용 예시:
  %(prog)s list-sectors              # KRX 섹터 목록 확인
  %(prog)s sector 반도체             # 섹터 종목 리스트
  %(prog)s info 009150               # 삼성전기 재무 요약
  %(prog)s screen                    # 기본 조건 스캔 가이드
  %(prog)s screen --roe 15 --pbr 1.0 # 조건 지정 스캔
  %(prog)s screen --growth           # 성장주 트랙 스캔
  %(prog)s dart-ratio                # DART 전 상장사 재무비율 (API 키 필요)
  %(prog)s dart-ratio --year 2023    # 특정 연도
        """,
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list-sectors", help="KRX 섹터 목록")

    p_sec = sub.add_parser("sector", help="섹터 종목 리스트")
    p_sec.add_argument("sector", help="섹터명 (예: 반도체, 바이오)")

    p_inf = sub.add_parser("info", help="단일 종목 재무 요약")
    p_inf.add_argument("code", help="종목 코드 (예: 009150)")

    p_scr = sub.add_parser("screen", help="투자 후보 스캔")
    p_scr.add_argument("--roe",    type=float, default=10.0, help="최소 ROE %% (기본: 10)")
    p_scr.add_argument("--pbr",    type=float, default=2.0,  help="최대 PBR 배 (기본: 2.0)")
    p_scr.add_argument("--cap",    type=float, default=500.0,help="최소 시총 억원 (기본: 500)")
    p_scr.add_argument("--sector", default=None,             help="섹터 한정 (선택)")
    p_scr.add_argument("--growth", action="store_true",      help="성장주 트랙 모드")

    p_dr = sub.add_parser("dart-ratio", help="DART 전 상장사 재무비율 일괄 조회")
    p_dr.add_argument("--year", type=int, default=2024, help="사업연도 (기본: 2024)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "list-sectors":
        cmd_list_sectors()
    elif args.command == "sector":
        cmd_sector(args.sector)
    elif args.command == "info":
        cmd_info(args.code)
    elif args.command == "screen":
        cmd_screen(
            min_roe=args.roe,
            max_pbr=args.pbr,
            min_cap_bn=args.cap,
            sector=args.sector,
            growth_mode=args.growth,
        )
    elif args.command == "dart-ratio":
        cmd_dart_ratio(year=args.year)


if __name__ == "__main__":
    main()

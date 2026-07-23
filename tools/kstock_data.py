#!/usr/bin/env python3
"""한국주 데이터 도구 — DART + 네이버금융 + KRX, 외부 라이브러리 불필요.

Claude Code 스킬에 한국 상장주 실시간 시세·재무 데이터를 제공합니다.
설계 원칙: 독립 모듈, 기존 도구 영향 없음. curl 직접 호출로 프록시 우회.

사용법 (스킬이 자동 호출):
    python3 tools/kstock_data.py quote 005930           # 삼성전자 실시간 시세
    python3 tools/kstock_data.py financials 005930      # 핵심 재무 데이터 (최근 5년)
    python3 tools/kstock_data.py valuation 005930       # 밸류에이션 지표
    python3 tools/kstock_data.py search 삼성전자        # 종목 코드 검색

Python >= 3.8, 외부 라이브러리 불필요.
"""

import argparse
import json
import subprocess
import sys
from decimal import Decimal, ROUND_HALF_EVEN
from urllib.parse import urlencode

_TIMEOUT = 15


def _curl(url: str) -> str:
    """curl로 직접 요청. 프록시 환경 우회."""
    result = subprocess.run(
        ["curl", "-s", "--noproxy", "*",
         "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
         "-H", "Referer: https://finance.naver.com/",
         url],
        capture_output=True, timeout=_TIMEOUT,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ConnectionError(f"요청 실패: {url}")
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return result.stdout.decode("euc-kr", errors="replace")


def _curl_json(url: str, params: dict = None) -> dict:
    """curl로 JSON 응답 수신."""
    if params:
        url = f"{url}?{urlencode(params)}"
    return json.loads(_curl(url))


# ---------------------------------------------------------------------------
# 네이버금융 실시간 시세 API
# ---------------------------------------------------------------------------

def _naver_quote(code: str) -> dict:
    """네이버금융 시세 API에서 실시간 데이터 수신."""
    url = f"https://polling.finance.naver.com/api/realtime/domestic/stock/{code}"
    try:
        data = _curl_json(url)
        item = data.get("datas", [{}])[0]
        return {
            "name":          item.get("stockName", ""),
            "code":          code,
            "price":         item.get("closePrice", "-"),
            "change_pct":    item.get("fluctuationsRatio", "-"),
            "change_amt":    item.get("compareToPreviousClosePrice", "-"),
            "open":          item.get("openingPrice", "-"),
            "high":          item.get("highPrice", "-"),
            "low":           item.get("lowPrice", "-"),
            "prev_close":    item.get("previousClosingPrice", "-"),
            "volume":        item.get("accumulatedTradingVolume", "-"),
            "turnover_amt":  item.get("accumulatedTradingValue", "-"),
            "market_cap":    item.get("marketValue", "-"),
            "per":           item.get("per", "-"),
            "pbr":           item.get("pbr", "-"),
            "eps":           item.get("eps", "-"),
            "bps":           item.get("bps", "-"),
            "foreign_ratio": item.get("foreignRatio", "-"),
        }
    except Exception:
        return {}


def _fmt_krw(value) -> str:
    """원화 금액을 읽기 쉬운 형태로 포맷."""
    if value is None or value in ("-", ""):
        return "-"
    try:
        v = float(str(value).replace(",", ""))
    except (ValueError, TypeError):
        return str(value)
    if abs(v) >= 1e12:
        return f"{v/1e12:.2f}조원"
    if abs(v) >= 1e8:
        return f"{v/1e8:.2f}억원"
    if abs(v) >= 1e4:
        return f"{v/1e4:.0f}만원"
    return f"{v:,.0f}원"


def _fmt_pct(value) -> str:
    if value is None or value in ("-", ""):
        return "-"
    try:
        return f"{float(str(value).replace(',','')):.2f}%"
    except (ValueError, TypeError):
        return str(value)


# ---------------------------------------------------------------------------
# 명령 구현
# ---------------------------------------------------------------------------

def cmd_quote(code: str):
    """실시간 시세 스냅샷."""
    d = _naver_quote(code)
    if not d or not d.get("name"):
        # 네이버 API 실패 시 KRX 대체
        print(f"⚠️  네이버금융 API 응답 없음. KRX 또는 DART에서 직접 확인해 주세요.")
        print(f"   DART: https://dart.fss.or.kr/")
        print(f"   KRX:  http://www.krx.co.kr/")
        return

    print("=" * 60)
    print(f"실시간 시세: {d['name']} ({d['code']})")
    print("=" * 60)
    print(f"  현재가:      {d['price']}원")
    print(f"  등락률:      {d['change_pct']}%")
    print(f"  등락액:      {d['change_amt']}원")
    print(f"  시가:        {d['open']}원")
    print(f"  고가:        {d['high']}원")
    print(f"  저가:        {d['low']}원")
    print(f"  전일종가:    {d['prev_close']}원")
    print(f"  거래량:      {d['volume']}주")
    print(f"  거래대금:    {_fmt_krw(d['turnover_amt'])}")
    print(f"  시가총액:    {_fmt_krw(d['market_cap'])}")
    print(f"  PER:         {d['per']}배")
    print(f"  PBR:         {d['pbr']}배")
    print(f"  EPS:         {d['eps']}원")
    print(f"  BPS:         {d['bps']}원")
    print(f"  외국인 비중: {d['foreign_ratio']}%")


def cmd_valuation(code: str):
    """밸류에이션 지표 요약."""
    d = _naver_quote(code)
    if not d or not d.get("name"):
        print(f"❌ {code} 시세 조회 실패")
        return

    print("=" * 60)
    print(f"밸류에이션 지표: {d['name']} ({d['code']})")
    print("=" * 60)
    print(f"  현재가:      {d['price']}원")
    print(f"  시가총액:    {_fmt_krw(d['market_cap'])}")
    print(f"  PER:         {d['per']}배")
    print(f"  PBR:         {d['pbr']}배")
    print(f"  EPS:         {d['eps']}원")
    print(f"  BPS:         {d['bps']}원")
    print(f"  외국인 비중: {d['foreign_ratio']}%")

    # 시가총액 간이 검증
    try:
        price = Decimal(str(d["price"]).replace(",", ""))
        cap_raw = str(d["market_cap"]).replace(",", "")
        cap = Decimal(cap_raw)
        shares = cap / price
        print(f"\n  추산 발행주식수: {float(shares)/1e8:.2f}억주")
        print(f"  ✅ 시가총액 간이 검증 완료 (정밀 검증: financial_rigor.py verify-market-cap 사용)")
    except Exception:
        pass


def cmd_financials(code: str):
    """DART 전자공시 기반 핵심 재무 데이터 (최근 5년)."""
    # DART Open API 사용 (무료 API 키 필요: https://opendart.fss.or.kr/)
    # API 키가 없으면 네이버금융 재무 요약 페이지로 대체
    print("=" * 60)
    print(f"핵심 재무 데이터: {code}")
    print("=" * 60)

    # 네이버금융 재무 요약 (HTML 파싱 없이 URL 안내)
    naver_url = f"https://finance.naver.com/item/coinfo.naver?code={code}&target=finsum_more"
    dart_url  = f"https://dart.fss.or.kr/dsab001/main.do"

    print(f"\n  📌 권장 데이터 출처 (정확도 순위):")
    print(f"  1순위 (공식): DART 전자공시 → {dart_url}")
    print(f"     검색: '{code}' → 사업보고서 → 재무제표")
    print(f"  2순위 (요약): 네이버금융 → {naver_url}")
    print()

    # DART API 키 환경변수 확인
    import os
    dart_key = os.environ.get("DART_API_KEY")
    if dart_key:
        _fetch_dart_financials(code, dart_key)
    else:
        print(f"  💡 DART Open API 자동 조회를 원하면 환경변수를 설정하세요:")
        print(f"     export DART_API_KEY=발급받은키")
        print(f"     API 키 발급: https://opendart.fss.or.kr/intro/main.do")
        print()
        _fetch_naver_financials_summary(code)


def _fetch_dart_financials(code: str, api_key: str):
    """DART Open API로 재무 데이터 조회."""
    # 기업 고유번호 조회
    try:
        corp_url = "https://opendart.fss.or.kr/api/company.json"
        params = {"crtfc_key": api_key, "stock_code": code}
        data = _curl_json(corp_url, params)
        corp_no = data.get("corp_code", "")
        corp_name = data.get("corp_name", code)

        if not corp_no:
            print(f"  ⚠️  DART 기업 코드 조회 실패. DART 웹사이트에서 직접 확인하세요.")
            return

        # 재무제표 조회 (최근 5년 연간)
        fin_url = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json"
        current_year = 2025
        for year in range(current_year, current_year - 5, -1):
            params = {
                "crtfc_key": api_key,
                "corp_code":  corp_no,
                "bsns_year":  str(year),
                "reprt_code": "11011",  # 사업보고서
                "fs_div":     "CFS",    # 연결재무제표
            }
            try:
                fin_data = _curl_json(fin_url, params)
                items = fin_data.get("list", [])
                if not items:
                    continue

                print(f"\n  --- {year}년 연간 (연결재무제표) ---")
                key_items = {
                    "매출액":       "ifrs-full_Revenue",
                    "영업이익":     "dart_OperatingIncomeLoss",
                    "당기순이익":   "ifrs-full_ProfitLoss",
                    "자산총계":     "ifrs-full_Assets",
                    "부채총계":     "ifrs-full_Liabilities",
                    "자본총계":     "ifrs-full_Equity",
                }
                for label, account in key_items.items():
                    for item in items:
                        if item.get("account_id") == account and item.get("sj_div") in ("IS", "BS", "CF"):
                            val = item.get("thstrm_amount", "")
                            if val:
                                try:
                                    v = int(val.replace(",", ""))
                                    print(f"  {label:12}: {_fmt_krw(v)}")
                                except ValueError:
                                    print(f"  {label:12}: {val}")
                            break
            except Exception:
                continue

    except Exception as e:
        print(f"  ⚠️  DART API 오류: {e}")
        print(f"  DART 웹사이트에서 직접 확인하세요: https://dart.fss.or.kr/")


def _fetch_naver_financials_summary(code: str):
    """네이버금융 재무 데이터 요약 안내."""
    print(f"  📊 네이버금융 재무 요약 페이지:")
    print(f"  https://finance.naver.com/item/coinfo.naver?code={code}&target=finsum_more")
    print()
    print(f"  확인할 핵심 지표:")
    print(f"  ┌─────────────┬──────────────────────────────────────┐")
    print(f"  │ 항목        │ 확인 방법                            │")
    print(f"  ├─────────────┼──────────────────────────────────────┤")
    print(f"  │ 매출액      │ 손익계산서 → 매출액 (연간/분기)      │")
    print(f"  │ 영업이익    │ 손익계산서 → 영업이익                │")
    print(f"  │ 순이익      │ 손익계산서 → 당기순이익              │")
    print(f"  │ ROE         │ 주요 재무비율 → ROE                  │")
    print(f"  │ 부채비율    │ 주요 재무비율 → 부채비율             │")
    print(f"  │ EPS/BPS     │ 주요 재무비율 → EPS, BPS             │")
    print(f"  └─────────────┴──────────────────────────────────────┘")
    print()
    print(f"  ⚠️  네이버금융 데이터는 DART 원본과 1% 이상 차이날 수 있습니다.")
    print(f"     중요 수치는 반드시 DART 원본 재무제표와 교차 검증하세요.")


def cmd_search(keyword: str):
    """종목 코드 검색."""
    # 네이버금융 종목 검색 API
    url = f"https://ac.finance.naver.com/ac?q={keyword}&q_enc=UTF-8&target=stock"
    try:
        raw = _curl(url)
        data = json.loads(raw)
        items = data.get("items", [[]])[0]

        if not items:
            print(f"❌ '{keyword}'에 해당하는 종목을 찾을 수 없습니다")
            print(f"   KRX 종목 검색: http://www.krx.co.kr/")
            return

        print("=" * 60)
        print(f"종목 검색 결과: '{keyword}'")
        print("=" * 60)
        for item in items[:10]:
            # [종목명, 코드, ...]
            if len(item) >= 2:
                name = item[0]
                code = item[1]
                print(f"  {code}  {name}")
    except Exception as e:
        print(f"⚠️  검색 실패: {e}")
        print(f"   KRX 종목 검색: http://www.krx.co.kr/")
        print(f"   DART 기업 검색: https://dart.fss.or.kr/")


# ---------------------------------------------------------------------------
# CLI 진입점
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="한국주 데이터 도구 — DART + 네이버금융 + KRX",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
사용 예시:
  %(prog)s quote 005930          # 삼성전자 실시간 시세
  %(prog)s financials 005930     # 핵심 재무 데이터
  %(prog)s valuation 000660      # SK하이닉스 밸류에이션
  %(prog)s search 카카오         # 카카오 종목 코드 검색

DART API 자동 연동:
  export DART_API_KEY=발급받은키
  python3 tools/kstock_data.py financials 005930

DART API 키 발급: https://opendart.fss.or.kr/intro/main.do
        """,
    )
    sub = parser.add_subparsers(dest="command")

    p_quote = sub.add_parser("quote",      help="실시간 시세")
    p_quote.add_argument("code",           help="종목 코드 (예: 005930)")

    p_fin = sub.add_parser("financials",   help="핵심 재무 데이터 (최근 5년)")
    p_fin.add_argument("code",             help="종목 코드")

    p_val = sub.add_parser("valuation",    help="밸류에이션 지표")
    p_val.add_argument("code",             help="종목 코드")

    p_search = sub.add_parser("search",    help="종목 코드 검색")
    p_search.add_argument("keyword",       help="회사명 또는 키워드")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    cmds = {
        "quote":      lambda: cmd_quote(args.code),
        "financials": lambda: cmd_financials(args.code),
        "valuation":  lambda: cmd_valuation(args.code),
        "search":     lambda: cmd_search(args.keyword),
    }
    cmds[args.command]()


if __name__ == "__main__":
    main()

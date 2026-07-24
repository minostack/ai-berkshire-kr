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
import os
import subprocess
import sys
import zipfile
import io
from decimal import Decimal, ROUND_HALF_EVEN
from urllib.parse import urlencode

_TIMEOUT = 15
_CORP_LIST_CACHE = os.path.join(os.path.dirname(__file__), "..", ".dart_corp_cache.json")


def _curl_raw(url: str, extra_headers: list = None) -> bytes:
    """curl로 직접 요청, 바이트 반환."""
    cmd = [
        "curl", "-s", "--noproxy", "*",
        "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "-H", "Referer: https://finance.naver.com/",
    ]
    if extra_headers:
        for h in extra_headers:
            cmd += ["-H", h]
    cmd.append(url)
    result = subprocess.run(cmd, capture_output=True, timeout=_TIMEOUT)
    if result.returncode != 0 or not result.stdout.strip():
        raise ConnectionError(f"요청 실패: {url}")
    return result.stdout


def _curl(url: str) -> str:
    raw = _curl_raw(url)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("euc-kr", errors="replace")


def _curl_json(url: str, params: dict = None) -> dict:
    if params:
        url = f"{url}?{urlencode(params)}"
    return json.loads(_curl(url))


# ---------------------------------------------------------------------------
# DART 기업 코드 매핑 (ZIP 파일 다운로드 방식 — 공식 방법)
# ---------------------------------------------------------------------------

def _load_corp_map(api_key: str) -> dict:
    """
    DART 전체 기업 목록(ZIP)을 다운로드해서 {stock_code: corp_code} 매핑 반환.
    캐시 파일이 있으면 재사용합니다.
    """
    # 캐시 확인 (24시간 이내)
    import time
    if os.path.exists(_CORP_LIST_CACHE):
        age = time.time() - os.path.getmtime(_CORP_LIST_CACHE)
        if age < 86400:  # 24시간
            with open(_CORP_LIST_CACHE, encoding="utf-8") as f:
                return json.load(f)

    print("  📥 DART 기업 목록 다운로드 중... (최초 1회, 이후 24시간 캐시)")

    url = f"https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key={api_key}"
    try:
        raw = _curl_raw(url)
    except Exception as e:
        print(f"  ❌ DART 기업 목록 다운로드 실패: {e}")
        return {}

    # ZIP 파일 파싱
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            xml_name = z.namelist()[0]
            xml_bytes = z.read(xml_name)
    except Exception as e:
        print(f"  ❌ ZIP 파싱 실패: {e}")
        return {}

    # XML 파싱 (표준 라이브러리)
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml_bytes.decode("utf-8"))
    except Exception as e:
        print(f"  ❌ XML 파싱 실패: {e}")
        return {}

    corp_map = {}
    for item in root.findall("list"):
        stock_code = (item.findtext("stock_code") or "").strip()
        corp_code  = (item.findtext("corp_code")  or "").strip()
        corp_name  = (item.findtext("corp_name")  or "").strip()
        if stock_code and corp_code:
            corp_map[stock_code] = {"corp_code": corp_code, "corp_name": corp_name}

    # 캐시 저장
    os.makedirs(os.path.dirname(_CORP_LIST_CACHE), exist_ok=True)
    with open(_CORP_LIST_CACHE, "w", encoding="utf-8") as f:
        json.dump(corp_map, f, ensure_ascii=False)

    print(f"  ✅ 기업 목록 로드 완료: {len(corp_map):,}개 상장사")
    return corp_map


# ---------------------------------------------------------------------------
# 네이버금융 실시간 시세 API
# ---------------------------------------------------------------------------

def _naver_quote(code: str) -> dict:
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


# ---------------------------------------------------------------------------
# 명령 구현
# ---------------------------------------------------------------------------

def cmd_quote(code: str):
    d = _naver_quote(code)
    if not d or not d.get("name"):
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

    try:
        price   = Decimal(str(d["price"]).replace(",", ""))
        cap_raw = str(d["market_cap"]).replace(",", "")
        cap     = Decimal(cap_raw)
        shares  = cap / price
        print(f"\n  추산 발행주식수: {float(shares)/1e8:.2f}억주")
        print(f"  ✅ 시가총액 간이 검증 완료")
    except Exception:
        pass


def cmd_financials(code: str):
    """DART Open API 기반 재무 데이터 조회."""
    dart_key = os.environ.get("DART_API_KEY")

    print("=" * 60)
    print(f"핵심 재무 데이터: {code}")
    print("=" * 60)

    if not dart_key:
        # API 키 없음 → 안내 출력
        print(f"\n  ⚠️  [데이터 출처] DART API 키 미등록 → 네이버금융 URL 안내")
        print(f"\n  💡 DART Open API 자동 조회를 원하면 환경변수를 설정하세요:")
        print(f"     export DART_API_KEY=발급받은키")
        print(f"     API 키 발급: https://opendart.fss.or.kr/intro/main.do")
        print()
        _fetch_naver_financials_summary(code)
        return

    # API 키 있음 → DART에서 직접 조회
    print(f"\n  ✅ [데이터 출처] DART Open API (공식 재무제표)")

    # 1단계: 전체 기업 목록에서 corp_code 찾기
    corp_map = _load_corp_map(dart_key)
    if not corp_map:
        print(f"  ❌ 기업 목록 로드 실패. 네이버금융으로 대체합니다.")
        _fetch_naver_financials_summary(code)
        return

    corp_info = corp_map.get(code)
    if not corp_info:
        print(f"  ❌ 종목코드 {code}를 DART 기업 목록에서 찾을 수 없습니다.")
        print(f"     비상장 또는 ETF일 수 있습니다.")
        _fetch_naver_financials_summary(code)
        return

    corp_code = corp_info["corp_code"]
    corp_name = corp_info["corp_name"]
    print(f"  기업명:   {corp_name}")
    print(f"  고유번호: {corp_code}")

    # 2단계: 재무제표 조회 (최근 5년 연간)
    fin_url = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json"
    import datetime
    current_year = datetime.datetime.now().year - 1  # 직전 완료 연도

    found_any = False
    for year in range(current_year, current_year - 5, -1):
        params = {
            "crtfc_key":  dart_key,
            "corp_code":  corp_code,
            "bsns_year":  str(year),
            "reprt_code": "11011",   # 사업보고서
            "fs_div":     "CFS",     # 연결재무제표
        }
        try:
            fin_data = _curl_json(fin_url, params)
            status   = fin_data.get("status", "")
            message  = fin_data.get("message", "")

            if status != "000":
                # 연결재무제표 없으면 별도재무제표 시도
                params["fs_div"] = "OFS"
                fin_data = _curl_json(fin_url, params)
                status   = fin_data.get("status", "")
                if status != "000":
                    continue
                fs_label = "별도재무제표"
            else:
                fs_label = "연결재무제표"

            items = fin_data.get("list", [])
            if not items:
                continue

            found_any = True
            print(f"\n  ── {year}년 연간 ({fs_label}) ──")

            key_accounts = {
                "매출액":     ["ifrs-full_Revenue",
                                "ifrs_Revenue",
                                "dart_TotalSalesAndRevenue"],
                "영업이익":   ["dart_OperatingIncomeLoss",
                                "ifrs-full_ProfitLossFromOperatingActivities"],
                "당기순이익": ["ifrs-full_ProfitLoss",
                                "ifrs_ProfitLoss"],
                "자산총계":   ["ifrs-full_Assets",
                                "ifrs_Assets"],
                "부채총계":   ["ifrs-full_Liabilities",
                                "ifrs_Liabilities"],
                "자본총계":   ["ifrs-full_Equity",
                                "ifrs_Equity"],
            }

            # account_map: {account_id: {sj_div: item}} — sj_div별로 분리 보관
            # (같은 account_id가 IS/BS/CF에 중복 출현하므로 덮어쓰기 방지)
            account_map = {}
            for item in items:
                aid    = item.get("account_id", "")
                sj_div = item.get("sj_div", "")
                if aid and sj_div:
                    account_map.setdefault(aid, {})[sj_div] = item

            # 항목별 우선 sj_div 지정
            # IS = 손익계산서, BS = 재무상태표, CF = 현금흐름표
            sj_priority = {
                "매출액":     ["IS"],
                "영업이익":   ["IS"],
                "당기순이익": ["IS"],       # CF의 ProfitLoss와 혼동 방지
                "자산총계":   ["BS"],
                "부채총계":   ["BS"],
                "자본총계":   ["BS"],       # IS의 Equity 변동과 혼동 방지
            }

            for label, account_ids in key_accounts.items():
                found = False
                priority_divs = sj_priority.get(label, ["IS", "BS", "CF"])
                for aid in account_ids:
                    if aid not in account_map:
                        continue
                    div_map = account_map[aid]
                    # 우선순위 sj_div 순서대로 탐색
                    item = None
                    for div in priority_divs:
                        if div in div_map:
                            item = div_map[div]
                            break
                    # 우선순위에 없으면 첫 번째 것 사용
                    if item is None:
                        item = next(iter(div_map.values()))
                    val = item.get("thstrm_amount", "")
                    if val:
                        try:
                            v = int(val.replace(",", ""))
                            print(f"  {label:10}: {_fmt_krw(v)}")
                        except ValueError:
                            print(f"  {label:10}: {val}")
                        found = True
                        break
                if not found:
                    print(f"  {label:10}: (데이터 없음)")

        except Exception as e:
            continue

    if not found_any:
        print(f"\n  ⚠️  DART에서 재무제표를 찾을 수 없습니다.")
        print(f"     사업보고서가 미제출 상태이거나 최근 데이터가 없을 수 있습니다.")
        _fetch_naver_financials_summary(code)

    print(f"\n  📌 원본 확인: https://dart.fss.or.kr/dsab001/main.do (검색: {code})")


def _fetch_naver_financials_summary(code: str):
    print(f"\n  📊 네이버금융 재무 요약:")
    print(f"  https://finance.naver.com/item/coinfo.naver?code={code}&target=finsum_more")
    print()
    print(f"  확인 항목: 매출액 / 영업이익 / 순이익 / ROE / 부채비율 / EPS / BPS")
    print(f"  ⚠️  네이버금융 데이터는 DART 원본과 차이가 있을 수 있습니다.")


def cmd_search(keyword: str):
    url = f"https://ac.finance.naver.com/ac?q={keyword}&q_enc=UTF-8&target=stock"
    try:
        raw  = _curl(url)
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
            if len(item) >= 2:
                print(f"  {item[1]}  {item[0]}")
    except Exception as e:
        print(f"⚠️  검색 실패: {e}")
        print(f"   KRX 종목 검색: http://www.krx.co.kr/")


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

    p_fin   = sub.add_parser("financials", help="핵심 재무 데이터 (최근 5년)")
    p_fin.add_argument("code",             help="종목 코드")

    p_val   = sub.add_parser("valuation",  help="밸류에이션 지표")
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

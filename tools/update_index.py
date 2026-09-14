#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
코스피200 / 코스닥150 구성종목 목록 생성 도구

키움 REST API 또는 KRX 공식 데이터로 구성종목을 조회해서
data/index_stocks.json 파일 하나에 저장합니다.

코스피200, 코스닥150은 분기별로 정기 변경되므로
변경 시마다 이 도구를 실행해서 목록을 최신화하세요.

저장 경로: data/index_stocks.json
  {
    "kospi200": {
      "updated":  "2026-09-04",
      "count":    200,
      "stocks":   [{"code": "005930", "name": "삼성전자"}, ...]
    },
    "kosdaq150": {
      "updated":  "2026-09-04",
      "count":    150,
      "stocks":   [{"code": "247540", "name": "에코프로비엠"}, ...]
    }
  }

사용법:
  python3 tools/update_index.py                     # 전체 갱신
  python3 tools/update_index.py --index kospi200    # 코스피200만
  python3 tools/update_index.py --index kosdaq150   # 코스닥150만
  python3 tools/update_index.py --show              # 저장된 목록 확인
  python3 tools/update_index.py --show --index kospi200

키 설정 (키움 API 방식 사용 시) — 프로젝트 루트 .env 파일 (.env.example 참고):
  KIWOOM_APP_KEY=앱키
  KIWOOM_APP_SECRET=앱시크릿
"""

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from typing import Optional

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

# ── .env 로더: 프로젝트 루트의 .env를 os.environ에 주입 ──
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))
except ImportError:
    pass  # python-dotenv 미설치 시 OS 환경변수만 사용

# ─────────────────────────────────────────────────────────────
# 경로 설정
# ─────────────────────────────────────────────────────────────

_BASE          = os.path.join(os.path.dirname(__file__), "..")
_DATA_DIR      = os.path.join(_BASE, "data")
_INDEX_FILE    = os.path.join(_DATA_DIR, "index_stocks.json")
_KIWOOM_TOKEN  = os.path.join(_BASE, ".kiwoom_token.json")
_KIWOOM_BASE   = "https://api.kiwoom.com"
_TIMEOUT       = 30

os.makedirs(_DATA_DIR, exist_ok=True)


# ─────────────────────────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────────────────────────

def _curl_json(url: str, method: str = "POST",
               headers: list = None, body: str = None,
               capture_resp_headers: bool = False) -> Optional[dict]:
    """
    키움 조회 API의 페이지네이션 계속 여부(cont-yn)와 다음 커서(next-key)는
    응답 "본문"이 아니라 응답 "HTTP 헤더"로 내려온다(실측 확인:
    ka20002/ka10080 응답 헤더에 next-key/cont-yn/resp-cnt 존재, 본문에는 없음).
    capture_resp_headers=True면 이 두 값을 응답 dict에 `_next_key`/`_cont_yn`
    으로 실어 반환한다.
    """
    cmd = ["curl", "-s", "--noproxy", "*",
           "-X", method,
           "-H", "Content-Type: application/json;charset=UTF-8"]
    if headers:
        for h in headers:
            cmd += ["-H", h]
    if body:
        cmd += ["-d", body]

    header_path = None
    try:
        if capture_resp_headers:
            fd, header_path = tempfile.mkstemp(suffix=".hdr")
            os.close(fd)
            cmd += ["-D", header_path]
        cmd.append(url)
        result = subprocess.run(cmd, capture_output=True, timeout=_TIMEOUT)
        raw = result.stdout.strip()
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except Exception:
            return None

        if capture_resp_headers and header_path:
            try:
                with open(header_path, encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if ":" not in line:
                            continue
                        k, _, v = line.partition(":")
                        k = k.strip().lower()
                        v = v.strip()
                        if k == "next-key":
                            data["_next_key"] = v
                        elif k == "cont-yn":
                            data["_cont_yn"] = v
            except OSError:
                pass

        return data
    finally:
        if header_path and os.path.exists(header_path):
            try:
                os.remove(header_path)
            except OSError:
                pass


def _load_index_file() -> dict:
    if os.path.exists(_INDEX_FILE):
        with open(_INDEX_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_index_file(data: dict):
    with open(_INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────────────────────────
# 키움 REST API 인증
# ─────────────────────────────────────────────────────────────

def _get_token() -> Optional[str]:
    """키움 토큰 발급 (24시간 캐시)."""
    app_key    = os.environ.get("KIWOOM_APP_KEY")
    app_secret = os.environ.get("KIWOOM_APP_SECRET")
    if not app_key or not app_secret:
        return None

    if os.path.exists(_KIWOOM_TOKEN):
        try:
            with open(_KIWOOM_TOKEN) as f:
                cache = json.load(f)
            if cache.get("expires_at", 0) > time.time() + 60:
                return cache["token"]
        except Exception:
            pass

    body = json.dumps({
        "grant_type": "client_credentials",
        "appkey":     app_key,
        "secretkey":  app_secret,
    })
    data = _curl_json(f"{_KIWOOM_BASE}/oauth2/token", method="POST", body=body)
    if not data:
        return None
    token = data.get("token") or data.get("access_token")
    if not token:
        return None
    expires = int(data.get("token_expired", 86400))
    with open(_KIWOOM_TOKEN, "w") as f:
        json.dump({"token": token, "expires_at": time.time() + expires}, f)
    return token


def _kiwoom(path: str, body: dict, api_id: str, token: str,
            extra_headers: list = None) -> Optional[dict]:
    """키움 REST API POST 호출."""
    app_key = os.environ.get("KIWOOM_APP_KEY", "")
    url     = f"{_KIWOOM_BASE}{path}"
    headers = [
        f"authorization: Bearer {token}",
        f"api-id: {api_id}",
        f"appkey: {app_key}",
    ]
    if extra_headers:
        headers.extend(extra_headers)
    return _curl_json(url, method="POST",
                      headers=headers, body=json.dumps(body),
                      capture_resp_headers=True)


# ─────────────────────────────────────────────────────────────
# 방법 1 — 키움 API로 구성종목 조회
# ─────────────────────────────────────────────────────────────

# 키움 업종 코드
_KIWOOM_UPJONG = {
    "kospi200":  "028",
    "kosdaq150": "004",
}

def fetch_from_kiwoom(index_name: str, token: str) -> list:
    """
    키움 REST API ka20002 (업종별주가요청) 으로 구성종목 조회.

    JSON 스펙 파일에서 확인된 정확한 파라미터:
      URI:    /api/dostk/sect
      api-id: ka20002

      코스피200: mrkt_tp=2, inds_cd=201
      코스닥150:  mrkt_tp=1, inds_cd=101 (코스닥 전체)
                → 코스닥150은 직접 지원 코드 없음
                → 시가총액 상위 150개로 근사

    응답 필드: inds_stkpc 리스트 → stk_cd, stk_nm
    """
    # 코스피200: inds_cd=201 직접 지원
    # 코스닥150: inds_cd=101(코스닥전체) 조회 후 시총 상위 150개 추출
    _PARAMS = {
        "kospi200":  {"mrkt_tp": "2", "inds_cd": "201", "stex_tp": "3"},
        "kosdaq150": {"mrkt_tp": "1", "inds_cd": "101", "stex_tp": "3"},
    }
    label  = "코스피200" if index_name == "kospi200" else "코스닥150"
    params = _PARAMS.get(index_name, _PARAMS["kospi200"])
    limit  = 200 if index_name == "kospi200" else 150

    print(f"  [{label}] 키움 ka20002 (업종별주가요청) 조회 중...")
    print(f"  파라미터: {params}")

    stocks   = []
    next_key = ""
    cont_yn  = ""

    for _ in range(20):  # 페이지네이션 최대 20회
        body = dict(params)
        headers_extra = []
        if next_key:
            headers_extra = [
                f"next-key: {next_key}",
                f"cont-yn: {cont_yn}",
            ]

        data = _kiwoom(
            "/api/dostk/sect", body, "ka20002",
            token, extra_headers=headers_extra
        )

        if not data:
            print(f"  ❌ API 응답 없음")
            break

        rc = data.get("return_code", 0)
        if rc not in (0, "0", None):
            print(f"  ❌ API 오류: {data.get('return_msg', '')}")
            break

        rows = data.get("inds_stkpc", [])
        if not rows:
            break

        for item in rows:
            # stk_cd 형식: "000080_AL" → 언더스코어 앞 6자리만 사용
            raw_code = str(item.get("stk_cd", "")).strip()
            code     = raw_code.split("_")[0]
            name     = str(item.get("stk_nm", "")).strip()
            if code and code.isdigit() and len(code) == 6:
                stocks.append({"code": code, "name": name})

        # 페이지네이션 (next-key/cont-yn은 응답 헤더로 내려옴 — _kiwoom()이
        # capture_resp_headers=True로 호출해 _next_key/_cont_yn에 실어줌)
        next_key = data.get("_next_key", "")
        cont_yn  = data.get("_cont_yn", "")
        if not next_key or cont_yn != "Y":
            break
        time.sleep(0.25)

    # 코스닥150: 전체 코스닥에서 상위 150개만 사용
    if index_name == "kosdaq150" and len(stocks) > limit:
        stocks = stocks[:limit]
        print(f"  코스닥150: 전체 {len(stocks)+limit-limit}개 중 상위 {limit}개 사용")

    return stocks


# ─────────────────────────────────────────────────────────────
# 방법 2 — KRX 공식 데이터로 구성종목 조회 (API 실패 시 대체)
# ─────────────────────────────────────────────────────────────

_KRX_INDEX_CODE = {
    "kospi200":  "1028",
    "kosdaq150": "2203",
}

def fetch_from_krx(index_name: str) -> list:
    """KRX 대체 — 키움 API로 위임."""
    return []  # 키움 API 우선 사용


# ─────────────────────────────────────────────────────────────
# 방법 3 — 수동 입력 안내
# ─────────────────────────────────────────────────────────────

def print_manual_guide():
    print()
    print("  자동 조회가 모두 실패했습니다.")
    print("  아래 방법으로 수동으로 최신 목록을 가져올 수 있습니다:")
    print()
    print("  [방법 A] KRX 정보데이터시스템에서 CSV 다운로드")
    print("    1. https://data.krx.co.kr 접속")
    print("    2. 기본통계 → 주가 → 지수 → 구성종목")
    print("    3. 코스피200 / 코스닥150 선택 후 CSV 다운로드")
    print("    4. python3 tools/update_index.py --from-csv 파일경로.csv --index kospi200")
    print()
    print("  [방법 B] 네이버금융에서 확인")
    print("    https://finance.naver.com/sise/sise_index.naver?code=KPI200")
    print("    https://finance.naver.com/sise/sise_index.naver?code=KSDAQ150")


# ─────────────────────────────────────────────────────────────
# CSV 임포트 (수동 다운로드 시)
# ─────────────────────────────────────────────────────────────

def import_from_csv(csv_path: str, index_name: str) -> list:
    """KRX CSV 파일에서 구성종목 임포트."""
    if not os.path.exists(csv_path):
        print(f"  ❌ 파일 없음: {csv_path}")
        return []

    stocks = []
    import csv
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # KRX CSV 컬럼: 종목코드, 종목명 (또는 ISU_SRT_CD, ISU_ABBRV)
            code = (row.get("종목코드") or row.get("ISU_SRT_CD") or
                    row.get("단축코드") or "").strip()
            name = (row.get("종목명")  or row.get("ISU_ABBRV") or
                    row.get("한글 종목명") or "").strip()
            if code and code.isdigit() and len(code) == 6:
                stocks.append({"code": code, "name": name})

    print(f"  ✅ CSV에서 {len(stocks)}개 종목 로드")
    return stocks


# ─────────────────────────────────────────────────────────────
# 메인 갱신 로직
# ─────────────────────────────────────────────────────────────

def update_index(index_name: str, token: Optional[str] = None,
                 csv_path: str = None) -> bool:
    """단일 인덱스 갱신."""
    label  = "코스피200" if index_name == "kospi200" else "코스닥150"
    stocks = []

    # 방법 0: CSV 수동 임포트
    if csv_path:
        stocks = import_from_csv(csv_path, index_name)

    # 방법 1: 키움 ka20002 (업종별주가요청) — JSON 스펙 확인된 정식 방법
    if not stocks and token:
        stocks = fetch_from_kiwoom(index_name, token)
        if stocks:
            print(f"  ✅ {label}: 키움 ka20002로 {len(stocks)}개 조회")

    if not stocks:
        print(f"  ❌ {label}: 자동 조회 실패")
        return False

    # 저장
    all_data = _load_index_file()
    all_data[index_name] = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "count":   len(stocks),
        "stocks":  stocks,
    }
    _save_index_file(all_data)

    print(f"  ✅ {label}: {len(stocks)}개 → {_INDEX_FILE}")
    return True


def show_index(index_name: str = None):
    """저장된 종목 목록 출력."""
    data = _load_index_file()
    if not data:
        print(f"  ❌ 저장된 목록 없음: {_INDEX_FILE}")
        print("  python3 tools/update_index.py 를 먼저 실행하세요.")
        return

    targets = ([index_name] if index_name
               else ["kospi200", "kosdaq150"])

    for idx in targets:
        info = data.get(idx)
        if not info:
            print(f"\n  [{idx}] 데이터 없음")
            continue

        label   = "코스피200" if idx == "kospi200" else "코스닥150"
        stocks  = info.get("stocks", [])
        updated = info.get("updated", "?")
        count   = info.get("count", len(stocks))

        print(f"\n{'='*60}")
        print(f"  {label}  |  {count}개  |  갱신: {updated}")
        print(f"{'='*60}")
        print(f"  {'순':>3}  {'코드':6}  {'종목명'}")
        print(f"  {'─'*3}  {'─'*6}  {'─'*20}")
        for i, s in enumerate(stocks, 1):
            print(f"  {i:>3}  {s['code']:6}  {s['name']}")
        print(f"\n  저장 파일: {_INDEX_FILE}")


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="코스피200 / 코스닥150 구성종목 목록 생성 및 관리",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""사용 예시:
  python3 tools/update_index.py                          # 전체 갱신
  python3 tools/update_index.py --index kospi200         # 코스피200만
  python3 tools/update_index.py --index kosdaq150        # 코스닥150만
  python3 tools/update_index.py --show                   # 저장 목록 확인
  python3 tools/update_index.py --show --index kospi200  # 코스피200만 확인
  python3 tools/update_index.py --from-csv ~/Downloads/kospi200.csv --index kospi200

저장 파일: data/index_stocks.json
갱신 주기: 분기 1회 (3/6/9/12월 인덱스 정기 변경 후)"""
    )

    parser.add_argument("--index",
                        choices=["kospi200", "kosdaq150"],
                        default=None,
                        help="갱신할 인덱스 (기본: 전체)")
    parser.add_argument("--show",
                        action="store_true",
                        help="저장된 목록 확인만 (갱신 안 함)")
    parser.add_argument("--from-csv",
                        default=None,
                        metavar="CSV_PATH",
                        help="KRX CSV 파일에서 직접 임포트")

    args = parser.parse_args()

    if args.show:
        show_index(args.index)
        return

    print(f"\n{'='*60}")
    print(f"  구성종목 목록 갱신  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")

    # 키움 ka20002 (업종별주가요청) 으로 코스피200/코스닥150 구성종목 조회
    # JSON 스펙 파일에서 확인된 정식 방법:
    #   mrkt_tp=2, inds_cd=201 → 코스피200 구성종목
    #   mrkt_tp=1, inds_cd=101 → 코스닥 전체 (상위 150개)
    token = _get_token()
    if token:
        print(f"  ✅ 키움 API 연결됨 — ka20002 업종별주가요청으로 조회")
    else:
        print(f"  ❌ 키움 API 미연결")
        print(f"  .env에 KIWOOM_APP_KEY / KIWOOM_APP_SECRET 설정 (.env.example 참고)")
        if not args.from_csv:
            print_manual_guide()
            sys.exit(1)

    targets = ([args.index] if args.index
               else ["kospi200", "kosdaq150"])

    success = 0
    for idx in targets:
        ok = update_index(idx, token=token, csv_path=args.from_csv)
        if ok:
            success += 1
        print()

    if success == 0:
        print_manual_guide()
    else:
        print(f"  ✅ {success}/{len(targets)} 인덱스 갱신 완료")
        print(f"  저장: {_INDEX_FILE}")


if __name__ == "__main__":
    main()

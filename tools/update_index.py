#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
코스피200 / 코스닥150 구성종목 목록 생성 도구

기본 출처는 **유니버스 이력**(`silver.universe_membership`)이다. KRX 지수운영
공지 첨부와 시총 항등식 검산으로 복원한 구성종목 이력에서, 마지막 관측일의
구성을 꺼내 data/index_stocks.json 파일 하나에 저장한다.

왜 이력이 기본인가 (2026-09-27)
-------------------------------
예전 기본값이던 키움 ka20002는 코스닥150 전용 코드가 없어 **코스닥 시총 상위
150개로 근사**했다. 2026-09-23 명단은 실제 구성과 37종목이 달랐고, 일봉 수집기가
이 파일로 대상을 고르므로 실제 구성종목 39개의 일봉이 조용히 빠졌다. 이력은
백테스트·스캐너가 쓰는 유니버스와 **같은 원천**이라 둘이 어긋나지 않는다.

코스피200, 코스닥150은 매년 6·12월 정기 변경되고, 그 사이 수시변경(합병·상장폐지 등)이 있다. 변경 후에는 **유니버스 이력을
먼저 갱신**하고(아래 "이력 갱신") 이 도구를 실행한다.

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
  python3 tools/update_index.py                     # 전체 갱신(유니버스 이력)
  python3 tools/update_index.py --index kospi200    # 코스피200만
  python3 tools/update_index.py --as-of 2026-06-12  # 특정 날짜 구성
  python3 tools/update_index.py --show              # 저장된 목록 확인
  python3 tools/update_index.py --verify            # KRX 지수 시총과 대조
  python3 tools/update_index.py --source kiwoom     # (근사) 키움 ka20002
  python3 tools/update_index.py --from-csv 파일.csv --index kospi200

이력 갱신 (정기 변경 후 — 이 도구보다 먼저, docs/STAGE_STATUS_V2.md "Stage 3 전체 재현 순서"):
  python cvdobv.py daily-cache build --index KOSPI200 --start {최근일-7} --end {어제} --run-id topup-{오늘}-kospi200
  python cvdobv.py calendar build                        # 일봉에서 거래일 달력 연장 — rebuild가 달력 마지막 날에 씨앗을 놓는다
  python cvdobv.py universe snapshot --index KOSPI200    # 오늘 구성(KIS, 2026-10-23~) — 역행 복원의 출발점. KRX 시총 검산 차이 0원이어야 정상
  python cvdobv.py universe snapshot --index KOSDAQ150   # KIS는 코스닥150도 찍는다(키움은 불가)
  python cvdobv.py universe krx-cache --start {이력 마지막일} --market BOTH    # 검산 재료
  python cvdobv.py universe notices                      # 지수운영공지·첨부(정기변경 명단)
  python cvdobv.py universe rebuild --index KOSPI200     # 매 거래일 시총 항등식 검산하며 복원
  python cvdobv.py universe rebuild --index KOSDAQ150
  python cvdobv.py universe verify --index KOSPI200      # 독립 재검산(수십 분)
  python cvdobv.py universe verify --index KOSDAQ150

키 설정 (`--source kiwoom`일 때만) — 프로젝트 루트 .env 파일 (.env.example 참고):
  KIWOOM_APP_KEY=앱키
  KIWOOM_APP_SECRET=앱시크릿
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime
from typing import Optional

if sys.platform == "win32":
    # 스트림을 새로 감싸지 않고 인코딩만 바꾼다 — 감싸면 import하는 쪽(pytest 등)의
    # 출력 캡처가 닫힌 스트림을 잡게 된다.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

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

#: 이력 마지막 관측일이 이보다 오래되면 경고한다 — 정기 변경(6·12월)이나
#: 그 사이 수시변경을 놓쳤을 수 있다.
_HISTORY_STALE_DAYS = 80
_INDEX_ID = {"kospi200": "KOSPI200", "kosdaq150": "KOSDAQ150"}


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
# 방법 0 (기본) — 유니버스 이력에서 구성종목
# ─────────────────────────────────────────────────────────────

def _src_path():
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    if src not in sys.path:
        sys.path.insert(0, src)


def _security_names() -> dict:
    """{security_id: 종목명} — 종목 마스터, 같은 종목이면 가장 최근 valid_from."""
    _src_path()
    try:
        from cvd_obv.storage.parquet_store import ParquetStore
        t = ParquetStore().read_arrow("silver", "security_master",
                                      columns=["security_id", "name", "valid_from"])
    except Exception:                                     # noqa: BLE001
        return {}
    rows = sorted(zip(t.column("security_id").to_pylist(), t.column("name").to_pylist(),
                      t.column("valid_from").to_pylist()),
                  key=lambda r: (r[0], str(r[2])))
    return {sid: name for sid, name, _ in rows if name}


def members_to_stocks(members: list, names: dict, fallback: dict = None) -> list:
    """["KRX:005930", …] → [{"code", "name"}, …] (코드 오름차순).

    이름은 종목 마스터 → 기존 파일 → 코드 순으로 채운다.
    """
    fallback = fallback or {}
    out = []
    for sid in sorted(members):
        code = sid.split(":", 1)[-1]
        out.append({"code": code, "name": names.get(sid) or fallback.get(code) or code})
    return out


def fetch_from_history(index_name: str, as_of: Optional[date] = None,
                       repo=None, names: dict = None) -> tuple:
    """유니버스 이력에서 구성종목. 반환 (stocks, meta) — 실패 시 ([], meta).

    `as_of`를 비우면 이력의 **마지막 관측일**. 관측 구간 밖 날짜는 거부한다 —
    가장 가까운 관측으로 대체하면 그 사실이 파일에 남지 않기 때문이다.
    """
    _src_path()
    index_id = _INDEX_ID[index_name]
    meta = {"source": "history", "index_id": index_id}
    try:
        if repo is None:
            from cvd_obv.ingestion.universe_history import UniverseRepository
            repo = UniverseRepository()
        cov = repo.coverage(index_id)
    except Exception as exc:                              # noqa: BLE001
        meta["error"] = f"유니버스 이력을 읽지 못했다: {exc}"
        return [], meta
    start, end = cov["observed_start"], cov["observed_end"]
    target = as_of or end
    if not (start <= target <= end):
        meta["error"] = (f"{target}는 {index_id} 이력 관측 구간({start} ~ {end}) 밖이다 "
                         f"— 이력을 먼저 갱신하라")
        return [], meta
    got = repo.get_members(index_id, target)
    if got.get("survivorship_status") != "HISTORICAL":
        meta["error"] = f"{index_id} {target}: 이력이 아닌 대체 구성({got.get('survivorship_status')})"
        return [], meta
    if names is None:
        names = _security_names()
    old = {s["code"]: s.get("name") for s in
           (_load_index_file().get(index_name) or {}).get("stocks", [])}
    stocks = members_to_stocks(got["members"], names, old)
    meta.update(as_of=target.isoformat(), observed_end=end.isoformat(),
                universe_version=got.get("universe_version"),
                stale_days=(date.today() - end).days)
    return stocks, meta


# ─────────────────────────────────────────────────────────────
# 방법 1 — 키움 API로 구성종목 조회 (`--source kiwoom`, 근사)
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

    # 코스닥150은 ka20002가 코스닥 "전체"를 주므로 150종목으로 줄여야 한다.
    # 예전에는 `stocks[:limit]`로 잘랐는데 응답 순서가 **종목코드 오름차순**
    # 이라 시총 상위가 아니라 코드가 작은 150종목이 뽑혔다. 2026-09-21 실측:
    # 그 명단의 시총 합이 공식 코스닥150 지수의 **11%**밖에 안 됐고 알테오젠·
    # 에코프로 같은 상위 종목이 전부 빠져 있었다. 코드 순서를 시총 순서로
    # 착각한 것이다. 이제 자르지 않고 KRX 시총으로 다시 정렬한다.
    if index_name == "kosdaq150" and len(stocks) > limit:
        ranked = rank_by_krx_mktcap(stocks, limit)
        if ranked:
            print(f"  코스닥150: 전체 {len(stocks)}개 → KRX 시총 상위 {limit}개")
            stocks = ranked
        else:
            print(f"  ⚠ 코스닥150: KRX 시총을 못 받았다. "
                  f"코드순 절단은 틀린 명단을 만들므로 저장하지 않는다.")
            return []

    return stocks


# ─────────────────────────────────────────────────────────────
# 방법 2 — KRX 공식 데이터로 구성종목 조회 (API 실패 시 대체)
# ─────────────────────────────────────────────────────────────

_KRX_INDEX_CODE = {
    "kospi200":  "1028",
    "kosdaq150": "2203",
}

def _krx_api():
    """검산용 KRX Open API 어댑터. 없으면 None."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = os.path.join(root, "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    try:
        from cvd_obv.ingestion.krx_openapi import KrxOpenApi, market_for
    except Exception:                                     # noqa: BLE001
        return None, None
    try:
        return KrxOpenApi(min_interval_sec=0.3), market_for
    except Exception:                                     # noqa: BLE001
        return None, None


def _recent_bas_dd(api, market, back: int = 10):
    """오늘부터 거꾸로 걸으며 응답이 있는 첫 거래일."""
    from datetime import date, timedelta
    # 오늘은 장 마감 전이면 빈 배열이 온다. 어제부터 걷는다.
    day = date.today() - timedelta(days=1)
    for _ in range(back):
        bas = day.strftime("%Y%m%d")
        try:
            if api.fetch(market.stock_service, bas, retries=1):
                return bas
        except Exception:                                 # noqa: BLE001
            pass
        day -= timedelta(days=1)
    return None


def rank_by_krx_mktcap(stocks: list, limit: int) -> list:
    """코스닥 종목들을 KRX 상장시가총액 순으로 정렬해 상위 `limit`개.

    **이것은 근사다.** 코스닥150은 유동주식 비율과 산업군 배분을 반영하므로
    단순 시총 상위와 다르다. 2026-09-18 실측으로 시총 상위 150종의 합은 공식
    지수보다 **7.2% 크다.** 그래서 저장 후 `verify_index`로 오차를 찍어
    "근사임을 아는 상태"로 쓴다. 공식 명단이 필요하면 KRX에서 CSV를 받아
    `--csv`로 넣는다.
    """
    api, market_for = _krx_api()
    if api is None:
        return []
    market = market_for("KOSDAQ150")
    bas = _recent_bas_dd(api, market)
    if not bas:
        return []
    try:
        caps = api.stock_caps(bas, market)
    except Exception:                                     # noqa: BLE001
        return []
    scored = [(caps.get(s["code"], 0), s) for s in stocks]
    scored.sort(key=lambda x: (-x[0], x[1]["code"]))
    return [s for _, s in scored[:limit]]


def verify_index(index_name: str) -> Optional[dict]:
    """저장된 명단을 KRX 공식 지수 시가총액과 대조한다.

    명단을 **믿는 대신 확인하는** 경로다. `diff_pct`가 0에 가까우면 명단이
    맞고, 크게 벌어지면 틀린 것이다(2026-09-21에 코스닥150 명단이 -88.8%로
    틀렸다는 것을 이 대조로 찾았다).
    """
    api, market_for = _krx_api()
    if api is None:
        print("  ⚠ KRX 어댑터를 불러올 수 없어 검산을 건너뛴다")
        return None
    market = market_for("KOSPI200" if index_name == "kospi200" else "KOSDAQ150")
    data = _load_index_file().get(index_name) or {}
    codes = [s["code"] for s in data.get("stocks", [])]
    if not codes:
        print(f"  ⚠ {index_name}: 저장된 명단이 없다")
        return None
    bas = _recent_bas_dd(api, market)
    if not bas:
        print("  ⚠ 최근 거래일 응답을 못 받아 검산을 건너뛴다")
        return None
    try:
        result = api.checksum(codes, bas, market=market)
    except Exception as exc:                              # noqa: BLE001
        print(f"  ⚠ 검산 실패: {exc}")
        return None
    target, ours = result["target"], result["ours"]
    pct = None if not target else (ours - target) / target * 100.0
    result["diff_pct"] = pct
    mark = "✅" if pct is not None and abs(pct) < 1.0 else "⚠"
    print(f"  {mark} 검산 {bas} [{market.index_name}] "
          f"명단합 {ours:,} / 지수 {target:,}"
          + (f" / 오차 {pct:+.2f}%" if pct is not None else ""))
    if pct is not None and abs(pct) >= 1.0:
        print("     → 근사이거나 명단이 틀렸다. 정확한 명단은 KRX CSV(--csv)로 넣는다.")
    return result


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
                 csv_path: str = None, source: str = "history",
                 as_of: Optional[date] = None) -> bool:
    """단일 인덱스 갱신.

    source: history(기본, 유니버스 이력) / kiwoom(근사) — `csv_path`가 있으면 CSV.
    키움으로 자동 대체하지 않는다: 이력이 없을 때 조용히 근사 명단을 쓰면
    그것이 바로 이 도구가 고치려던 문제다.
    """
    label  = "코스피200" if index_name == "kospi200" else "코스닥150"
    stocks, extra = [], {}

    if csv_path:
        stocks = import_from_csv(csv_path, index_name)
        extra = {"source": f"KRX CSV {os.path.basename(csv_path)}"}
    elif source == "history":
        stocks, meta = fetch_from_history(index_name, as_of)
        if stocks:
            print(f"  ✅ {label}: 유니버스 이력 {meta['as_of']} 구성 {len(stocks)}개")
            extra = {"as_of": meta["as_of"],
                     "source": f"silver.universe_membership {meta['universe_version']} "
                               f"(KRX 지수 공시 기반 구성 이력)"}
            if not as_of and meta["stale_days"] > _HISTORY_STALE_DAYS:
                print(f"  ⚠ 이력 마지막 관측이 {meta['observed_end']} — "
                      f"{meta['stale_days']}일 전이다. 정기 변경을 놓쳤을 수 있다.")
                print("    이력 갱신: daily-cache → calendar build → universe snapshot → "
                      "krx-cache → notices → rebuild → verify "
                      f"(--index {_INDEX_ID[index_name]}) — 순서는 이 파일 머리말 참고")
        else:
            print(f"  ❌ {label}: {meta.get('error', '이력 조회 실패')}")
    elif source == "kiwoom":
        if token:
            stocks = fetch_from_kiwoom(index_name, token)
        if stocks:
            print(f"  ✅ {label}: 키움 ka20002로 {len(stocks)}개 조회")
            if index_name == "kosdaq150":
                print("  ⚠ 코스닥150은 시총 상위 근사다 — 실제 구성과 다를 수 있다")
            extra = {"source": "kiwoom ka20002"
                     + (" (코스닥 시총 상위 근사)" if index_name == "kosdaq150" else "")}

    if not stocks:
        print(f"  ❌ {label}: 갱신 실패 — 기존 파일을 그대로 둔다")
        return False

    # 저장 — 이전 출처 표시가 남지 않도록 블록을 통째로 바꾼다
    all_data = _load_index_file()
    all_data[index_name] = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "count":   len(stocks),
        **extra,
        "stocks":  stocks,
    }
    _save_index_file(all_data)

    print(f"  ✅ {label}: {len(stocks)}개 → {_INDEX_FILE}")
    verify_index(index_name)
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
        print(f"  {label}  |  {count}개  |  갱신: {updated}"
              + (f"  |  기준일: {info['as_of']}" if info.get("as_of") else ""))
        if info.get("source"):
            print(f"  출처: {info['source']}")
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
  python3 tools/update_index.py                          # 전체 갱신(유니버스 이력)
  python3 tools/update_index.py --index kosdaq150        # 코스닥150만
  python3 tools/update_index.py --as-of 2026-06-12       # 특정 날짜 구성
  python3 tools/update_index.py --show                   # 저장 목록 확인
  python3 tools/update_index.py --verify                 # 명단을 KRX 지수와 대조
  python3 tools/update_index.py --source kiwoom          # (근사) 키움 ka20002
  python3 tools/update_index.py --from-csv ~/Downloads/kospi200.csv --index kospi200

저장 파일: data/index_stocks.json
갱신 주기: 6·12월 정기 변경 후, 또는 이력이 80일 넘게 오래됐을 때 (유니버스 이력을 먼저 갱신)
  daily-cache → calendar build → universe snapshot → krx-cache → notices → rebuild → verify
  (명령 전체는 python tools/update_index.py 머리말 docstring 참고)"""
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
    parser.add_argument("--verify",
                        action="store_true",
                        help="저장된 목록을 KRX 공식 지수 시총과 대조만 (갱신 안 함)")
    parser.add_argument("--source",
                        choices=["history", "kiwoom"],
                        default="history",
                        help="history(기본, 유니버스 이력) / kiwoom(ka20002, 코스닥150은 시총 근사)")
    parser.add_argument("--as-of",
                        default=None,
                        metavar="YYYY-MM-DD",
                        help="이력의 이 날짜 구성 (기본: 이력 마지막 관측일)")

    args = parser.parse_args()

    if args.show:
        show_index(args.index)
        return

    if args.verify:
        for idx in ([args.index] if args.index else ["kospi200", "kosdaq150"]):
            print("\n  [%s]" % idx)
            verify_index(idx)
        print()
        return

    print(f"\n{'='*60}")
    print(f"  구성종목 목록 갱신  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")

    as_of = datetime.strptime(args.as_of, "%Y-%m-%d").date() if args.as_of else None
    token = None
    if args.from_csv:
        print(f"  출처: KRX CSV {args.from_csv}")
    elif args.source == "history":
        print("  출처: 유니버스 이력(silver.universe_membership)")
    else:
        # 키움 ka20002 (업종별주가요청):
        #   mrkt_tp=2, inds_cd=201 → 코스피200 구성종목
        #   mrkt_tp=1, inds_cd=101 → 코스닥 전체 (시총 상위 150개로 근사)
        token = _get_token()
        if token:
            print(f"  ✅ 키움 API 연결됨 — ka20002 업종별주가요청으로 조회")
        else:
            print(f"  ❌ 키움 API 미연결")
            print(f"  .env에 KIWOOM_APP_KEY / KIWOOM_APP_SECRET 설정 (.env.example 참고)")
            print_manual_guide()
            sys.exit(1)

    targets = ([args.index] if args.index
               else ["kospi200", "kosdaq150"])

    success = 0
    for idx in targets:
        ok = update_index(idx, token=token, csv_path=args.from_csv,
                          source=args.source, as_of=as_of)
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

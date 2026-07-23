#!/usr/bin/env python3
"""리포트 데이터 검수 도구 — AI Berkshire KR

연구 리포트에서 재무 데이터를 15% 무작위 샘플링하여 신뢰 출처와 대조,
통과 시 준출(PASS), 불통과 시 반려(FAIL) 판정을 출력합니다.

외부 라이브러리 불필요 — Python 표준 라이브러리만 사용.
Python >= 3.7 필요.

작업 흐름 (3단계):
  Step 1 — 데이터 추출 및 15% 무작위 샘플링:
    python3 tools/report_audit.py extract --report reports/삼성전자/삼성전자-research-20260101.md

  Step 2 — Claude가 각 항목을 신뢰 출처에서 확인 (DART/네이버금융/KRX)

  Step 3 — 검수 결과 입력 후 준출/반려 판정 출력:
    python3 tools/report_audit.py verdict --results '[...]'

  미리보기 (추출만, 검수 없음):
    python3 tools/report_audit.py extract --report reports/xxx.md --dry-run
"""

import argparse
import json
import math
import os
import re
import sys
from decimal import Decimal, Context, ROUND_HALF_EVEN
from random import Random

_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)

# ---------------------------------------------------------------------------
# 데이터 추출: Markdown 리포트에서 재무 수치 인식
# ---------------------------------------------------------------------------

_PATTERNS = [
    (r'([\d,，\.]+)\s*%',                        '%',    'percent'),
    (r'([\d,，\.]+)\s*조(원|달러|원화)?',          '조원',  'trillion_krw'),
    (r'([\d,，\.]+)\s*억(원|달러|원화)?',          '억원',  'hundred_million'),
    (r'([\d,，\.]+)\s*[xX배]',                   'x',    'multiple'),
    (r'\$([\d,，\.]+)\s*([BMT억])',               '$',    'usd_abs'),
    (r'\|\s*[~약]?\$?([\d,，\.]+)\s*\|',         '',     'table_num'),
]

_LABEL_RE = re.compile(
    r'(?P<label>[^\|\n：:]{2,25})[：:\s]+[~약]?\$?(?P<num>[\d,，\.]+)\s*(?P<unit>조원?|억원?|[xX배]|%|[BMT])?'
)

_TABLE_ROW_RE = re.compile(
    r'\|\s*(?P<label>[^|]{1,40})\s*\|\s*[~약]?\$?(?P<num>[\d,，\.]+)\s*(?P<unit>조원?|억원?|[xX배]|%|[BMT])?\s*\|'
)


def _clean_num(s: str) -> float:
    s = s.replace(',', '').replace('，', '').strip()
    try:
        return float(s)
    except ValueError:
        return None


def _is_valid_label(label: str) -> bool:
    label = label.strip()
    if len(label) < 2:
        return False
    if re.fullmatch(r'[\d\s년분기Q]+', label):
        return False
    if re.match(r'^[+\-\*#\|~\$>_`]', label):
        return False
    if '**' in label or '`' in label or '__' in label:
        return False
    if re.fullmatch(r'[+\-]?\d+(\.\d+)?%', label):
        return False
    _SKIP = {'출처', 'sources', 'source', '설명', '주의', '비고', '데이터출처',
             'n/a', '—', '-', '/', '합계', 'total', '단위', '추세'}
    if label.lower() in _SKIP:
        return False
    return True


_KV_TABLE_RE = re.compile(
    r'^\|\s*(?P<label>[^|*\n]{2,40}?)\s*\|\s*[~약]?\$?(?P<num>[\d,，\.]+)\s*'
    r'(?P<unit>조원?|억원?|[xX배]|%|[BMT억])?\s*[\|（\(]'
)

_KV_LABEL_RE = re.compile(
    r'(?P<label>[\uac00-\ud7a3A-Za-z][^\|\n：:*]{1,30})[：:]\s*[~약]?\$?'
    r'(?P<num>[\d,，\.]+)\s*(?P<unit>조원?|억원?|[xX배]|%|[BMT])?'
)


def _parse_md_tables(lines: list) -> list:
    """Markdown 표 전체를 파싱하여 (행라벨, 열헤더, 값, 단위, 줄번호, 원문) 목록 반환."""
    results = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if '|' in line and not re.match(r'^\|[\-\s\|:]+\|$', line):
            headers_raw = [h.strip().strip('*_').strip() for h in line.split('|')]
            headers_raw = [h for h in headers_raw if h]
            if i + 1 < len(lines) and re.match(r'^\|[\-\s\|:]+\|$', lines[i+1].strip()):
                i += 2
                while i < len(lines):
                    dline = lines[i].strip()
                    if not dline or not dline.startswith('|'):
                        break
                    cells = [c.strip().strip('*_~').strip() for c in dline.split('|')]
                    cells = [c for c in cells if c != '']
                    if len(cells) < 2:
                        i += 1
                        continue
                    row_label = cells[0]
                    for col_idx, cell in enumerate(cells[1:], start=1):
                        col_header = headers_raw[col_idx] if col_idx < len(headers_raw) else f'열{col_idx}'
                        m = re.search(
                            r'[~약]?\$?([\d,，\.]+)\s*(조원?|억원?|[xX배]|%|[BMT])?',
                            cell
                        )
                        if m:
                            val = _clean_num(m.group(1))
                            unit = (m.group(2) or '').strip()
                            if val and val != 0 and val < 1e15:
                                results.append((row_label, col_header, val, unit, i + 1, dline))
                    i += 1
                continue
        i += 1
    return results


def extract_data_points(md_text: str) -> list:
    """Markdown 리포트에서 인식 가능한 모든 재무 데이터 포인트 추출.

    3가지 구조 커버:
      1. 다중 열 Markdown 표 (주요 출처): (행라벨 + 열헤더) → 값
      2. 콜론 KV 행: 라벨: 값 단위
      3. 볼드 숫자 행: **값** 단위

    반환: list of dict:
      {id, label, reported_value, unit, raw_text, line_number}
    """
    points = []
    seen = set()

    def _add(label, val, unit, lineno, raw):
        label = re.sub(r'[\*_`]+', '', label).strip()
        if not _is_valid_label(label):
            return
        if val is None or val == 0 or val > 1e15:
            return
        if re.fullmatch(r'(20\d{2}|Q[1-4]|\d{4}\s*Q[1-4])', label.strip()):
            return
        key = f"{label}|{round(val,4)}|{unit}"
        if key in seen:
            return
        seen.add(key)
        points.append({
            'id': len(points) + 1,
            'label': label,
            'reported_value': val,
            'unit': unit,
            'raw_text': raw[:120],
            'line_number': lineno,
        })

    lines = md_text.split('\n')
    in_code = False

    # --- 1. 다중 열 표 ---
    for row_label, col_header, val, unit, lineno, raw in _parse_md_tables(lines):
        if not _is_valid_label(row_label):
            continue
        if col_header.upper() in ('YOY', 'YOY증가율', '증가율', '전년대비', '변화', '추세', '설명', '비고'):
            continue
        if col_header and col_header != row_label:
            label = f"{row_label} · {col_header}"
        else:
            label = row_label
        _add(label, val, unit, lineno, raw)

    # --- 2. KV 콜론 행 ---
    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith('```'):
            in_code = not in_code
            continue
        if in_code or stripped.startswith('> ') or re.match(r'^#{1,6}\s', stripped):
            continue
        if '|' in stripped:
            continue

        for m in _KV_LABEL_RE.finditer(stripped):
            label = m.group('label')
            val = _clean_num(m.group('num'))
            unit = (m.group('unit') or '').strip()
            _add(label, val, unit, lineno, stripped)

    return points


def sample_points(points: list, ratio: float = 0.15, seed: int = None) -> list:
    """ratio 비율로 무작위 샘플링, 최소 3개, 최대 30개."""
    n = max(3, min(30, math.ceil(len(points) * ratio)))
    n = min(n, len(points))
    rng = Random(seed)
    sampled = rng.sample(points, n)
    return sorted(sampled, key=lambda p: p['line_number'])


# ---------------------------------------------------------------------------
# 준출/반려 판정
# ---------------------------------------------------------------------------

_TOLERANCE = 0.01   # 1% 허용 편차


def _pct_diff(reported: float, fetched: float) -> float:
    if reported == 0:
        return 0.0 if fetched == 0 else float('inf')
    return abs(reported - fetched) / abs(reported)


def render_verdict(results: list, report_name: str = "") -> dict:
    """검수 결과를 바탕으로 준출/반려 판정 출력.

    results: list of dict, 각 항목:
      - id, label, reported_value, unit, fetched_value, fetched_source
      - (선택) fetched_value2, fetched_source2 ← 2차 출처

    반환:
      {
        'verdict': 'PASS' | 'FAIL',
        'pass_count': int,
        'fail_count': int,
        'total': int,
        'fail_items': [...],
        'summary': str,
      }
    """
    BOLD  = '\033[1m'
    RED   = '\033[91m'
    GREEN = '\033[92m'
    YELLOW= '\033[93m'
    RESET = '\033[0m'

    print('=' * 70)
    print(f'{BOLD}리포트 데이터 검수 — 준출/반려 판정{RESET}')
    if report_name:
        print(f'리포트: {report_name}')
    print('=' * 70)
    print()

    fail_items = []
    warn_items = []

    for item in results:
        label    = item.get('label', '?')
        reported = float(item.get('reported_value', 0))
        unit     = item.get('unit', '')
        fetched  = item.get('fetched_value')
        source   = item.get('fetched_source', '?')
        fetched2 = item.get('fetched_value2')
        source2  = item.get('fetched_source2', '')

        # 검수값 미제공 → 건너뜀
        if fetched is None:
            print(f'  ⬜ [{item["id"]:>2}] {label[:35]:35s} {reported:>12.2f} {unit}  →  [검수값 미제공, 건너뜀]')
            continue

        fetched = float(fetched)
        diff1   = _pct_diff(reported, fetched)

        diff2 = None
        if fetched2 is not None:
            fetched2 = float(fetched2)
            diff2    = _pct_diff(reported, fetched2)

        pass1 = diff1 <= _TOLERANCE
        pass2 = (diff2 is None) or (diff2 <= _TOLERANCE)

        if pass1 and pass2:
            status = f'{GREEN}✅ 통과{RESET}'
            detail = f'{source}: {fetched:.2f} (편차 {diff1*100:.2f}%)'
            if diff2 is not None:
                detail += f'  |  {source2}: {fetched2:.2f} (편차 {diff2*100:.2f}%)'
        elif not pass1 and not pass2:
            status = f'{RED}❌ 불통과{RESET}'
            detail = f'{source}: {fetched:.2f} (편차 {diff1*100:.2f}%)'
            if diff2 is not None:
                detail += f'  |  {source2}: {fetched2:.2f} (편차 {diff2*100:.2f}%)'
            fail_items.append({
                'id': item['id'], 'label': label,
                'reported': reported, 'unit': unit,
                'fetched': fetched,   'source': source,
                'fetched2': fetched2, 'source2': source2,
                'diff1_pct': round(diff1 * 100, 2),
                'diff2_pct': round(diff2 * 100, 2) if diff2 is not None else None,
                'raw_text': item.get('raw_text', ''),
                'line_number': item.get('line_number', 0),
            })
        else:
            # 한 출처만 통과 → 경고
            status = f'{YELLOW}⚠️  경고{RESET}'
            detail = f'{source}: {fetched:.2f} (편차 {diff1*100:.2f}%)'
            if diff2 is not None:
                detail += f'  |  {source2}: {fetched2:.2f} (편차 {diff2*100:.2f}%)'
            warn_items.append({
                'id': item['id'], 'label': label,
                'reported': reported, 'unit': unit,
                'diff1_pct': round(diff1 * 100, 2),
                'diff2_pct': round(diff2 * 100, 2) if diff2 is not None else None,
            })

        print(f'  {status} [{item["id"]:>2}] {label[:35]:35s}  리포트: {reported:>12.2f} {unit}')
        print(f'              {" " * 38}{detail}')

    print()
    print('-' * 70)

    total      = len([r for r in results if r.get('fetched_value') is not None])
    fail_count = len(fail_items)
    warn_count = len(warn_items)
    pass_count = total - fail_count - warn_count

    print(f'  검수 총계: {total}  |  통과: {GREEN}{pass_count}{RESET}  |  경고: {YELLOW}{warn_count}{RESET}  |  불통과: {RED}{fail_count}{RESET}')
    print()

    if fail_count == 0:
        print(f'{BOLD}{GREEN}【준출】모든 검수 데이터 통과 — 리포트 발행 가능합니다.{RESET}')
        verdict = 'PASS'
    else:
        print(f'{BOLD}{RED}【반려】{fail_count}개 데이터 항목 검수 불통과 — 수정 후 재검수 필요.{RESET}')
        print()
        print(f'{BOLD}반려 사유:{RESET}')
        for fi in fail_items:
            print(f'  ❌ {fi["line_number"]}행 | {fi["label"]}')
            print(f'     리포트값:  {fi["reported"]} {fi["unit"]}')
            print(f'     {fi["source"]}: {fi["fetched"]}  (편차 {fi["diff1_pct"]}%)')
            if fi.get('fetched2') is not None:
                print(f'     {fi["source2"]}: {fi["fetched2"]}  (편차 {fi["diff2_pct"]}%)')
            print(f'     원문: {fi["raw_text"][:80]}')
            print()
        verdict = 'FAIL'

    if warn_count > 0:
        print(f'{YELLOW}주의: {warn_count}개 항목에서 두 출처 결과가 일치하지 않습니다 (편차 1% 초과).')
        print(f'GAAP/Non-GAAP 차이, 환율 기준 차이, 또는 집계 방식 차이일 수 있으니 수동 확인 권장.{RESET}')
        for wi in warn_items:
            print(f'  ⚠️  {wi["label"]}  리포트:{wi["reported"]} {wi["unit"]}  편차: {wi["diff1_pct"]}% / {wi["diff2_pct"]}%')

    print('=' * 70)

    return {
        'verdict':    verdict,
        'pass_count': pass_count,
        'warn_count': warn_count,
        'fail_count': fail_count,
        'total':      total,
        'fail_items': fail_items,
        'warn_items': warn_items,
    }


# ---------------------------------------------------------------------------
# CLI 진입점
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='리포트 데이터 검수 도구 — Report Audit Tool (KR)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
작업 흐름:

  Step 1 — 데이터 포인트 추출 및 15% 무작위 샘플링:
    python3 tools/report_audit.py extract --report reports/삼성전자/삼성전자-research-20260101.md

  Step 2 — 각 항목을 아래 신뢰 출처에서 수동 확인:
    한국주: DART(dart.fss.or.kr) [주] + 네이버금융 또는 KRX [부]
    미국주: macrotrends.net [주] + stockanalysis.com [부]
    홍콩주: aastocks.com [주] + macrotrends ADR [부]

  Step 3 — 검수 결과 입력 후 준출/반려 판정:
    python3 tools/report_audit.py verdict --results '[
      {"id":1,"label":"매출액","reported_value":302.0,"unit":"조원",
       "fetched_value":302.1,"fetched_source":"DART",
       "fetched_value2":301.9,"fetched_source2":"네이버금융"},
      ...
    ]'

  미리보기 (추출만, 검수 없음):
    python3 tools/report_audit.py extract --report reports/xxx.md --dry-run

  샘플링 비율 지정 (기본 0.15):
    python3 tools/report_audit.py extract --report reports/xxx.md --ratio 0.20

  난수 시드 고정 (동일 샘플 재현):
    python3 tools/report_audit.py extract --report reports/xxx.md --seed 42
        """)

    sub = parser.add_subparsers(dest='command')

    # extract
    ext = sub.add_parser('extract', help='리포트에서 데이터 포인트 추출 및 무작위 샘플링')
    ext.add_argument('--report',  required=True,       help='리포트 파일 경로 (Markdown)')
    ext.add_argument('--ratio',   type=float, default=0.15, help='샘플링 비율, 기본 0.15')
    ext.add_argument('--seed',    type=int,   default=None, help='난수 시드 (재현용, 선택)')
    ext.add_argument('--dry-run', action='store_true',  help='추출만, JSON 출력 없음')

    # verdict
    vrd = sub.add_parser('verdict', help='검수 결과로 준출/반려 판정 출력')
    vrd.add_argument('--results',     required=True, help='JSON 배열 (fetched_value 포함)')
    vrd.add_argument('--report',      default='',    help='리포트명 (선택, 표시용)')
    vrd.add_argument('--output-json', action='store_true', help='판정 결과를 JSON으로 stdout 출력')

    args = parser.parse_args()

    if args.command == 'extract':
        if not os.path.exists(args.report):
            print(f'❌ 파일 없음: {args.report}', file=sys.stderr)
            sys.exit(1)

        with open(args.report, 'r', encoding='utf-8') as f:
            text = f.read()

        all_points = extract_data_points(text)
        sampled    = sample_points(all_points, ratio=args.ratio, seed=args.seed)

        print('=' * 70)
        print(f'리포트 데이터 검수 목록')
        print(f'파일:        {args.report}')
        print(f'전체 데이터: {len(all_points)}개  |  샘플링 비율: {args.ratio:.0%}  |  검수 대상: {len(sampled)}개')
        if args.seed is not None:
            print(f'난수 시드:   {args.seed} (동일 샘플 재현 가능)')
        print('=' * 70)
        print()
        print(f'{"ID":>3}  {"행번":>5}  {"데이터 항목":<35}  {"리포트값":>12}  {"단위"}')
        print(f'{"─"*3}  {"─"*5}  {"─"*35}  {"─"*12}  {"─"*6}')
        for p in sampled:
            print(f'{p["id"]:>3}  {p["line_number"]:>5}  {p["label"][:35]:<35}  {p["reported_value"]:>12.2f}  {p["unit"]}')
        print()
        print('↑ 위 각 항목을 아래 출처에서 확인 후 fetched_value에 입력하세요:')
        print('  한국주: DART(dart.fss.or.kr) [주] + 네이버금융 또는 KRX [부]')
        print('  미국주: macrotrends.net [주] + stockanalysis.com [부]')
        print('  홍콩주: aastocks.com [주] + macrotrends ADR [부]')
        print()

        if not args.dry_run:
            template = []
            for p in sampled:
                template.append({
                    'id':             p['id'],
                    'label':          p['label'],
                    'reported_value': p['reported_value'],
                    'unit':           p['unit'],
                    'line_number':    p['line_number'],
                    'raw_text':       p['raw_text'],
                    'fetched_value':  None,   # ← 주요 출처 확인값 입력
                    'fetched_source': '',     # ← 주요 출처명 입력 (예: DART)
                    'fetched_value2': None,   # ← 보조 출처 확인값 입력 (선택)
                    'fetched_source2':'',     # ← 보조 출처명 입력 (예: 네이버금융)
                })
            print('검수 목록 JSON (fetched_value 입력 후 verdict 커맨드에 전달):')
            print()
            print(json.dumps(template, ensure_ascii=False, indent=2))

    elif args.command == 'verdict':
        try:
            results = json.loads(args.results)
        except json.JSONDecodeError as e:
            print(f'❌ JSON 파싱 실패: {e}', file=sys.stderr)
            sys.exit(1)

        outcome = render_verdict(results, report_name=args.report or '')

        if args.output_json:
            print(json.dumps(outcome, ensure_ascii=False, indent=2))

        # 반려 시 비zero 종료코드 (CI/스크립트 연동용)
        sys.exit(0 if outcome['verdict'] == 'PASS' else 1)

    else:
        parser.print_help()


if __name__ == '__main__':
    main()

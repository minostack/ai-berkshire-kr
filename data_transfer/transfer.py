#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PC 간 수집 데이터 이관 — 실행 입구.

    python -m data_transfer.transfer status                         # 최근 14일 수집 현황
    python -m data_transfer.transfer status --start 2026-10-26 --end 2026-11-03
    python -m data_transfer.transfer export --date 2026-11-02       # 지정일
    python -m data_transfer.transfer export --start 2026-10-26 --end 2026-11-02
    python -m data_transfer.transfer list                           # DOWNLOAD의 묶음
    python -m data_transfer.transfer import --latest --dry-run      # 무엇이 들어올지만
    python -m data_transfer.transfer import --latest                # 가장 최근 묶음
    python -m data_transfer.transfer import DOWNLOAD\\bars_20261102_PC1_20261103090000.zip

내보내기·가져오기는 적재 잠금(`data/manifests/data_load.lock`)을 잡는다. 정기 적재가
도는 중이면 끝날 때까지 기다린다(`--lock-wait`초, 기본 3600).
가져오기가 행을 썼으면 끝에 `calendar build`로 거래일 달력을 다시 만든다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "data_transfer"

from data_transfer import DOWNLOAD_DIR  # noqa: E402
from data_transfer.package import (  # noqa: E402
    TABLES, PackageError, collect_status, default_status_range, export_package,
    find_packages, import_package, read_package,
)

EXIT_OK, EXIT_ERROR, EXIT_USAGE = 0, 1, 2


def _day(text):
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"날짜 형식은 YYYY-MM-DD: {text}") from exc


def _range(args, need: bool):
    """--date / --start·--end → (start, end). 없으면 need에 따라 오류 또는 None."""
    if args.date and (args.start or args.end):
        raise SystemExit("--date와 --start/--end는 함께 쓰지 않는다")
    if args.date:
        return args.date, args.date
    if args.start:
        return args.start, args.end or args.start
    if args.end:
        raise SystemExit("--end만 줄 수 없다 — --start도 준다")
    if need:
        raise SystemExit("--date 또는 --start [--end]가 필요하다")
    return None


def _ljust(text, width):
    """한글은 두 칸으로 세어 왼쪽 정렬한다."""
    shown = sum(2 if ord(ch) > 0x1100 else 1 for ch in text)
    return text + " " * max(0, width - shown)


def _mb(n):
    return f"{n / 1_048_576:,.1f}MB"


# ── status ──

def print_status(status: dict):
    led = status["ledger"]
    print(f"수집 현황 — {status['host']}  {status['checked_at']} KST")
    print(f"  저장소: {status['store_root']}")
    print(f"  장부: confirmed_through={led.get('confirmed_through', '(없음)')}  "
          f"마지막 확정 {led.get('last_final_run', '-')}  "
          f"마지막 잠정 {led.get('last_provisional_run', '-')}")
    print(f"  [날짜별] 지수 구성종목 {status['members']}개 기준(1분봉·잠정·확정·상태) "
          "+ 저장된 전체 종목 수(KRX·NXT 1분봉, 체결량 UN, 지수)")
    print("    날짜        1분봉  잠정  확정  " + _ljust("상태", 18)
          + "KRX분봉 NXT분봉 체결량UN 지수")
    for r in status["days"]:
        print(f"    {r['date']}  {r['minutes']:5} {r['provisional']:5} {r['final']:5}  "
              f"{_ljust(r['status'], 18)}{r['krx_minutes_all']:7} {r['nxt_minutes_all']:7} "
              f"{r['sides_un']:8} {r['benchmarks']:4}")
    print("  [테이블] 파일 수 · 용량")
    for name, t in status["tables"].items():
        print(f"    {name:18} {t['files']:>7,}개  {_mb(t['bytes']):>10}")


def cmd_status(args) -> int:
    rng = _range(args, need=False) or default_status_range(days=args.days)
    status = collect_status(*rng)
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2, default=str))
    else:
        print_status(status)
    return EXIT_OK


# ── export ──

def _lock(label, wait):
    from cvd_obv.ingestion.run_lock import exclusive
    return exclusive(label, wait_sec=wait)


def cmd_export(args) -> int:
    from cvd_obv.ingestion.run_lock import LockTimeout
    start, end = _range(args, need=True)
    tables = _tables(args.tables)
    print(f"내보내기 {start} ~ {end} — 테이블 {', '.join(tables)}")
    try:
        with _lock("data-transfer export", args.lock_wait):
            path, manifest = export_package(start, end, tables=tables,
                                            out_dir=args.out)
    except LockTimeout as exc:
        print(f"  ❌ {exc}")
        return EXIT_ERROR
    total = sum(t["rows"] for t in manifest["tables"].values())
    print_daily_kinds(manifest)
    print(f"  저장: {path} ({_mb(os.path.getsize(path))}, {total:,}행)")
    if total == 0:
        print("  ⚠ 구간에 저장된 행이 없다 — 날짜를 확인한다")
    return EXIT_OK


def print_daily_kinds(manifest):
    kinds = manifest["tables"].get("daily_bars", {}).get("daily_kinds", {})
    if not kinds:
        return
    print("  [일봉 정규장 행] 날짜 · 잠정 · 확정 (리비전 전부 — 받는 PC는 최신을 고른다)")
    for d, k in kinds.items():
        print(f"    {d}  {k['provisional']:5} {k['final']:5}")


def _tables(text):
    if not text:
        return list(TABLES)
    names = [x.strip() for x in text.split(",") if x.strip()]
    bad = [n for n in names if n not in TABLES]
    if bad:
        raise SystemExit(f"모르는 테이블: {bad} — 쓸 수 있는 것: {', '.join(TABLES)}")
    return names


# ── list ──

def cmd_list(args) -> int:
    paths = find_packages(args.dir)
    if not paths:
        print(f"묶음이 없다: {args.dir or DOWNLOAD_DIR}")
        return EXIT_OK
    print(f"묶음 {len(paths)}개 — {args.dir or DOWNLOAD_DIR}")
    for path in paths:
        try:
            m = read_package(path, verify=False)
        except PackageError as exc:
            print(f"  {os.path.basename(path)}  ⚠ {exc}")
            continue
        total = sum(t["rows"] for t in m["tables"].values())
        print(f"  {os.path.basename(path)}  {m['start']}~{m['end']}  "
              f"{m['source_host']}  {total:,}행  {_mb(os.path.getsize(path))}")
    return EXIT_OK


# ── import ──

def _resolve_package(args):
    if args.latest:
        paths = find_packages(args.dir)
        if not paths:
            raise SystemExit(f"묶음이 없다: {args.dir or DOWNLOAD_DIR}")
        return paths[-1]
    if not args.package:
        raise SystemExit("가져올 묶음 경로 또는 --latest가 필요하다")
    path = args.package
    if not os.path.exists(path):
        candidate = os.path.join(args.dir or DOWNLOAD_DIR, os.path.basename(path))
        if os.path.exists(candidate):
            path = candidate
        elif os.path.exists(candidate + ".zip"):
            path = candidate + ".zip"
        else:
            raise SystemExit(f"묶음을 찾지 못했다: {args.package}")
    return path


def cmd_import(args) -> int:
    from cvd_obv.ingestion.run_lock import LockTimeout
    path = _resolve_package(args)
    head = "가져오기(확인만)" if args.dry_run else "가져오기"
    print(f"{head} — {os.path.basename(path)}")
    try:
        manifest = read_package(path, verify=True)
    except PackageError as exc:
        print(f"  ❌ {exc}")
        return EXIT_ERROR
    led = manifest.get("source_ledger") or {}
    print(f"  보낸 PC {manifest['source_host']} · {manifest['created_at']} · "
          f"구간 {manifest['start']} ~ {manifest['end']} · "
          f"보낸 PC 장부 confirmed_through={led.get('confirmed_through', '-')}")
    print_daily_kinds(manifest)
    try:
        with _lock("data-transfer import", args.lock_wait):
            summary = import_package(path, dry_run=args.dry_run)
    except LockTimeout as exc:
        print(f"  ❌ {exc}")
        return EXIT_ERROR
    except PackageError as exc:
        print(f"  ❌ {exc}")
        return EXIT_ERROR
    new = sum(t["new_rows"] for t in summary["tables"].values())
    if summary["already_imported"]:
        print("  · 이 묶음은 전에 가져온 기록이 있다 — 없는 행만 확인했다")
    if args.dry_run:
        print(f"  확인만 했다 — 쓰면 새 행 {new:,}개")
        return EXIT_OK
    if new == 0:
        print("  새 행 0 — 이미 모두 있다(쓴 파일 없음)")
        return EXIT_OK
    print(f"  ✅ 새 행 {new:,}개를 썼다 — 기록 {summary['record']}")
    if args.no_calendar:
        print("  달력 갱신을 건너뛰었다 — 필요하면 `python cvdobv.py calendar build`")
        return EXIT_OK
    print("  ---- calendar build ----")
    from cvd_obv.cli.main import main as cvd_main
    rc = cvd_main(["calendar", "build"])
    if rc:
        print(f"  ⚠ calendar build 종료 코드 {rc} — 행은 이미 썼다")
    return EXIT_OK if not rc else EXIT_ERROR


def build_parser():
    p = argparse.ArgumentParser(
        prog="python -m data_transfer.transfer",
        description="PC 간 수집 데이터(잠정·확정 봉) 이관")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_range(sp):
        sp.add_argument("--date", type=_day, help="지정일 YYYY-MM-DD")
        sp.add_argument("--start", type=_day, help="시작일 YYYY-MM-DD")
        sp.add_argument("--end", type=_day, help="종료일 YYYY-MM-DD(기본: 시작일)")

    st = sub.add_parser("status", help="날짜별 수집 현황")
    add_range(st)
    st.add_argument("--days", type=int, default=14,
                    help="구간을 안 주면 오늘까지 며칠(달력일, 기본 14)")
    st.add_argument("--json", action="store_true", help="JSON으로 출력")
    st.set_defaults(func=cmd_status)

    ex = sub.add_parser("export", help="지정일·구간을 묶음(zip)으로 내보내기")
    add_range(ex)
    ex.add_argument("--tables", help=f"쉼표 구분(기본 전부: {','.join(TABLES)})")
    ex.add_argument("--out", help=f"저장 폴더(기본 {DOWNLOAD_DIR})")
    ex.add_argument("--lock-wait", type=float, default=3600,
                    help="적재가 돌고 있으면 기다릴 초(기본 3600)")
    ex.set_defaults(func=cmd_export)

    ls = sub.add_parser("list", help="DOWNLOAD의 묶음 목록")
    ls.add_argument("--dir", help=f"묶음 폴더(기본 {DOWNLOAD_DIR})")
    ls.set_defaults(func=cmd_list)

    im = sub.add_parser("import", help="묶음 가져오기(없는 행만)")
    im.add_argument("package", nargs="?", help="묶음 zip 경로 또는 DOWNLOAD 안 파일명")
    im.add_argument("--latest", action="store_true", help="DOWNLOAD의 가장 최근 묶음")
    im.add_argument("--dir", help=f"묶음 폴더(기본 {DOWNLOAD_DIR})")
    im.add_argument("--dry-run", action="store_true", help="쓰지 않고 새 행 수만")
    im.add_argument("--no-calendar", action="store_true",
                    help="가져온 뒤 calendar build를 하지 않는다")
    im.add_argument("--lock-wait", type=float, default=3600,
                    help="적재가 돌고 있으면 기다릴 초(기본 3600)")
    im.set_defaults(func=cmd_import)
    return p


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

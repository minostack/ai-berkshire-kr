#!/usr/bin/env python3
"""生成 star 增长曲线 SVG（浅色/深色两版），替代不稳定的第三方 star-history 服务。

数据来源：GitHub REST API stargazers（Accept: application/vnd.github.star+json 带 starred_at 时间戳）。
star 数大时不逐条拉取，按页均匀采样（每页100条，取每页首条时间戳即可还原累计曲线）。

用法：
    GITHUB_TOKEN=xxx python3 tools/star_history_chart.py
    # 或本地：GITHUB_TOKEN=$(gh auth token) python3 tools/star_history_chart.py

输出：assets/star-history.svg 和 assets/star-history-dark.svg
"""

import json
import math
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = "xbtlin/ai-berkshire"
API = "https://api.github.com"
MAX_SAMPLE_PAGES = 36  # 采样页数上限，曲线平滑度足够且请求量可控

# 配色：浅/深两套，分别针对 GitHub README 的白底(#ffffff)与深底(#0d1117)校验过对比度
THEMES = {
    "light": {
        "line": "#2a78d6",
        "area": "#2a78d6",
        "area_opacity": "0.10",
        "grid": "#e1e0d9",
        "axis": "#c3c2b7",
        "tick_text": "#898781",
        "label_text": "#0b0b0b",
        "suffix": "",
    },
    "dark": {
        "line": "#3987e5",
        "area": "#3987e5",
        "area_opacity": "0.14",
        "grid": "#2c2c2a",
        "axis": "#383835",
        "tick_text": "#898781",
        "label_text": "#ffffff",
        "suffix": "-dark",
    },
}

WIDTH, HEIGHT = 800, 420
M_LEFT, M_RIGHT, M_TOP, M_BOTTOM = 56, 96, 32, 44


def gh_get(path, accept="application/vnd.github+json"):
    req = urllib.request.Request(API + path)
    req.add_header("Accept", accept)
    req.add_header("User-Agent", "star-history-chart")
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def fetch_points():
    repo = gh_get(f"/repos/{REPO}")
    total = repo["stargazers_count"]
    if total == 0:
        sys.exit("仓库没有 star，无需生成图表")

    last_page = math.ceil(total / 100)
    n = min(last_page, MAX_SAMPLE_PAGES)
    pages = sorted({round(1 + (last_page - 1) * i / (n - 1)) for i in range(n)}) if n > 1 else [1]

    points = []
    for p in pages:
        data = gh_get(
            f"/repos/{REPO}/stargazers?per_page=100&page={p}",
            accept="application/vnd.github.star+json",
        )
        if data:
            t = datetime.fromisoformat(data[0]["starred_at"].replace("Z", "+00:00"))
            points.append((t, (p - 1) * 100 + 1))
    points.append((datetime.now(timezone.utc), total))
    points.sort()
    return points, total


def nice_ticks(vmax):
    for step in (s * 10 ** k for k in range(0, 6) for s in (1, 2, 2.5, 5)):
        if vmax / step <= 6:
            top = math.ceil(vmax / step) * step
            return [int(step * i) for i in range(int(top / step) + 1)]
    return [0, vmax]


def fmt_k(v):
    return f"{v / 1000:g}k" if v >= 1000 else str(v)


def month_ticks(t0, t1):
    ticks, y, m = [], t0.year, t0.month
    while (y, m) <= (t1.year, t1.month):
        m += 1
        if m > 12:
            y, m = y + 1, 1
        t = datetime(y, m, 1, tzinfo=timezone.utc)
        if t0 <= t <= t1:
            ticks.append(t)
    step = max(1, math.ceil(len(ticks) / 8))
    return ticks[::step]


def render(points, total, theme):
    t0, t1 = points[0][0], points[-1][0]
    span = (t1 - t0).total_seconds() or 1
    yticks = nice_ticks(total)
    ymax = yticks[-1]
    plot_w = WIDTH - M_LEFT - M_RIGHT
    plot_h = HEIGHT - M_TOP - M_BOTTOM

    def x(t):
        return M_LEFT + plot_w * (t - t0).total_seconds() / span

    def y(v):
        return M_TOP + plot_h * (1 - v / ymax)

    coords = [(x(t), y(v)) for t, v in points]
    line = " ".join(f"{px:.1f},{py:.1f}" for px, py in coords)
    area = f"{coords[0][0]:.1f},{y(0):.1f} {line} {coords[-1][0]:.1f},{y(0):.1f}"

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
        f'viewBox="0 0 {WIDTH} {HEIGHT}" font-family="system-ui, -apple-system, \'Segoe UI\', sans-serif">',
        f'<title>Star history of {REPO}: {total:,} stars</title>',
    ]
    for v in yticks[1:]:
        parts.append(
            f'<line x1="{M_LEFT}" y1="{y(v):.1f}" x2="{WIDTH - M_RIGHT}" y2="{y(v):.1f}" '
            f'stroke="{theme["grid"]}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{M_LEFT - 8}" y="{y(v):.1f}" text-anchor="end" dominant-baseline="middle" '
            f'font-size="12" fill="{theme["tick_text"]}">{fmt_k(v)}</text>'
        )
    parts.append(
        f'<line x1="{M_LEFT}" y1="{y(0):.1f}" x2="{WIDTH - M_RIGHT}" y2="{y(0):.1f}" '
        f'stroke="{theme["axis"]}" stroke-width="1"/>'
    )
    for t in month_ticks(t0, t1):
        label = t.strftime("%b %Y") if t.month == 1 or t == month_ticks(t0, t1)[0] else t.strftime("%b")
        parts.append(
            f'<text x="{x(t):.1f}" y="{y(0) + 20:.1f}" text-anchor="middle" '
            f'font-size="12" fill="{theme["tick_text"]}">{label}</text>'
        )
    parts.append(f'<polygon points="{area}" fill="{theme["area"]}" fill-opacity="{theme["area_opacity"]}"/>')
    parts.append(
        f'<polyline points="{line}" fill="none" stroke="{theme["line"]}" '
        f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
    )
    ex, ey = coords[-1]
    parts.append(f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="4" fill="{theme["line"]}"/>')
    parts.append(
        f'<text x="{ex + 10:.1f}" y="{ey:.1f}" dominant-baseline="middle" font-size="14" '
        f'font-weight="600" fill="{theme["label_text"]}">{total:,}</text>'
    )
    parts.append("</svg>")
    return "\n".join(parts)


def main():
    points, total = fetch_points()
    out_dir = Path(__file__).resolve().parent.parent / "assets"
    for theme in THEMES.values():
        path = out_dir / f"star-history{theme['suffix']}.svg"
        path.write_text(render(points, total, theme), encoding="utf-8")
        print(f"已生成 {path}（{total:,} stars，{len(points)} 个采样点）")


if __name__ == "__main__":
    main()

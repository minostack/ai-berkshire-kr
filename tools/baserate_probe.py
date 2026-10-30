# -*- coding: utf-8 -*-
"""코스피200 무조건부 상승 확률(기준선) 실측 + 유효표본 검토."""
import sys, json, math, statistics, collections
sys.path.insert(0, 'tools')
import kiwoom_cvd_obv_scanner as S

tok = S._get_token()
sys.path.insert(0, 'src')
from cvd_obv.universe import current_universe  # noqa: E402  유니버스 기준 universe-1.0
_u = current_universe(("KOSPI200",))
codes = [(m.code, m.name) for m in _u.members()]
print('universe', len(codes), flush=True)

HOR = (5, 10, 20)
series = {}
for i, (code, name) in enumerate(codes, 1):
    try:
        rows = S.fetch_daily_bars(code, tok, days=520)
    except Exception as e:
        print('skip', code, e, flush=True)
        continue
    rows = sorted(rows, key=lambda r: r['dt'][:8])
    if len(rows) < 60:
        continue
    series[code] = [(r['dt'][:8], r['close']) for r in rows]
    if i % 40 == 0:
        print('  fetched', i, flush=True)

print('series', len(series), flush=True)

# 공통 거래일 축
alldates = sorted({d for s in series.values() for d, _ in s})
alldates = alldates[-260:]
print('date span', alldates[0], '~', alldates[-1], len(alldates), flush=True)

res = {h: [] for h in HOR}          # (date, code, up?)  전체 표본
daily_up = {h: collections.defaultdict(list) for h in HOR}  # 날짜별 상승비율
for code, s in series.items():
    m = dict(s)
    ds = [d for d, _ in s]
    idx = {d: k for k, d in enumerate(ds)}
    for d in alldates:
        if d not in idx:
            continue
        k = idx[d]
        for h in HOR:
            if k + h >= len(ds):
                continue
            r = m[ds[k + h]] / m[d] - 1.0
            res[h].append((d, code, 1 if r > 0 else 0, r))
            daily_up[h][d].append(1 if r > 0 else 0)

print()
print('=== 무조건부 기준선: P(h영업일 후 종가 > 당일 종가) ===')
for h in HOR:
    v = [x[2] for x in res[h]]
    rr = [x[3] for x in res[h]]
    n = len(v)
    p = sum(v) / n
    se_naive = math.sqrt(p * (1 - p) / n)
    # 날짜별 상승비율의 분산 -> 시장공통요인 크기
    dv = [statistics.mean(daily_up[h][d]) for d in daily_up[h] if daily_up[h][d]]
    print(' h=%2d일  n=%6d  P(상승)=%.1f%%  중앙수익률=%+.2f%%  평균=%+.2f%%  단순SE=%.2f%%p'
          % (h, n, p * 100, statistics.median(rr) * 100, statistics.mean(rr) * 100, se_naive * 100))
    print('        날짜별 상승비율: 중앙 %.0f%%  최저 %.0f%%  최고 %.0f%%  표준편차 %.1f%%p  (거래일 %d)'
          % (statistics.median(dv) * 100, min(dv) * 100, max(dv) * 100, statistics.pstdev(dv) * 100, len(dv)))

print()
print('=== 유효표본 검토 (h=20일) ===')
h = 20
dv = {d: statistics.mean(daily_up[h][d]) for d in daily_up[h] if daily_up[h][d]}
ds = sorted(dv)
n_obs = len(res[h])
n_dates = len(ds)
n_indep_windows = n_dates // h
sd_between = statistics.pstdev(list(dv.values()))
p = sum(x[2] for x in res[h]) / n_obs
# 클러스터(=날짜) 보정: design effect = 1 + (m-1)*ICC
m = n_obs / n_dates
icc = max(0.0, (sd_between ** 2) / (p * (1 - p)))
deff = 1 + (m - 1) * icc
n_eff = n_obs / deff
print(' 관측 %d건 = 거래일 %d일 × 평균 %.0f종목' % (n_obs, n_dates, m))
print(' 날짜간 상승비율 표준편차 %.1f%%p -> 날짜 내 상관(ICC) 추정 %.3f' % (sd_between * 100, icc))
print(' design effect %.1f  ->  유효표본 n_eff ≈ %.0f' % (deff, n_eff))
print(' 비중복 %d영업일 윈도우 수 = %d' % (h, n_indep_windows))
for ne in (n_eff, n_indep_windows * 10):
    half = 1.96 * math.sqrt(0.55 * 0.45 / ne) * 100
    print('  n=%6.0f 일 때 관측 55%% 의 95%%CI = %.1f%% ~ %.1f%%' % (ne, 55 - half, 55 + half))
print()
print(' 참고: 55%% vs 50%% 를 80%% 검정력으로 구분하려면 독립표본 약 780건 필요')

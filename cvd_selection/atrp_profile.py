# -*- coding: utf-8 -*-
"""ATRP14 기준값 참고 분석 — 월별 분포와 ATRP 구간별 -5% / +8% 도달률.

    python cvd_selection/atrp_profile.py

선정 조건 없이 **모든 종목·거래일**을 종가에 샀다고 가정한 기준선이다(선정 성과가 아니다).
- 모집단: 현재 구성종목(`current_universe`) — 생존 편향 있음
- ATRP14 = 전일까지 Wilder ATR(14) / 전일 종가, 유동성 = 직전 20일 평균 거래대금(추정 포함)
- 같은 날 고가 +8%와 저가 -5%가 모두 닿으면 손절로 센다(보수적, 일봉은 선후를 모른다)
청산 수치(-5%, +8%)는 명세 고정값이다. 이 결과로 ATRP 임계값을 정하지 말 것 — 명세 11장대로
사전 후보를 정하고 미사용 구간으로 평가한다.
"""
import collections
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cvd_selection  # noqa: E402,F401
from cvd_selection import data as D
from cvd_obv.universe import current_universe
from datetime import date
sys.stdout.reconfigure(encoding="utf-8")
u=current_universe(); sids=sorted(u.security_ids())
daily=D.load_daily(sids,date(2023,6,1),date(2026,10,23))
rows=[]  # (date, atrp, avg_value, hit_stop5_in10, arm_before_stop, cond)
for c,bs in daily.items():
    H=[b["high"] for b in bs];L=[b["low"] for b in bs];C=[b["close"] for b in bs]
    V=[b["value"] for b in bs]
    # incremental Wilder ATR through i-1
    n=14
    tr=[H[0]-L[0]]+[max(H[i]-L[i],abs(H[i]-C[i-1]),abs(L[i]-C[i-1])) for i in range(1,len(bs))]
    atr=None
    for i in range(len(bs)):
        # atr over tr[0..i-1]
        if i==n: atr=sum(tr[:n])/n
        elif i>n: atr=(atr*(n-1)+tr[i-1])/n
        if atr is None or i<20 or i+10>=len(bs): continue
        if bs[i]["date"]<date(2024,1,2): continue
        atrp=100*atr/C[i-1]
        a20=sum(v or 0 for v in V[i-20:i])/20
        P0=C[i]; stop=P0*0.95; arm=P0*1.08
        res="open"
        for k in range(i+1,min(i+21,len(bs))):
            if L[k]<=stop: res="stop"; break        # 같은 날 둘 다면 보수적으로 손절
            if H[k]>=arm: res="arm"; break
        hit10=any(L[k]<=stop for k in range(i+1,i+11))
        rows.append((bs[i]["date"],atrp,a20,res,hit10))
print("표본",len(rows))
# 월별 분포
bym=collections.defaultdict(list)
for d,a,*_ in rows: bym[d.strftime("%Y-%m")].append(a)
print("월 | ATRP 중앙 | ≤4% 비율 | ≤5% | ≤6%")
for m in sorted(bym):
    xs=bym[m]
    if m<"2024-07" and m[-2:] not in ("01","07"): continue
    print(m, round(statistics.median(xs),2), f"{sum(x<=4 for x in xs)/len(xs):.0%}", f"{sum(x<=5 for x in xs)/len(xs):.0%}", f"{sum(x<=6 for x in xs)/len(xs):.0%}")
# 유동성 100억 이상 버킷별
buck=[(0,2),(2,3),(3,4),(4,5),(5,6),(6,8),(8,99)]
liq=[r for r in rows if r[2]>=1e10]
print("유동성≥100억 표본",len(liq))
print("ATRP 구간 | 표본 | 10일 내 -5% 도달 | 20일 내 먼저 +8% | 먼저 -5% | 미결")
for lo,hi in buck:
    g=[r for r in liq if lo<=r[1]<hi]
    if not g: continue
    cnt=collections.Counter(r[3] for r in g)
    print(f"{lo}~{hi}%",len(g),f"{sum(r[4] for r in g)/len(g):.0%}",f"{cnt['arm']/len(g):.0%}",f"{cnt['stop']/len(g):.0%}",f"{cnt['open']/len(g):.0%}")
# 최근 구간만
rec=[r for r in liq if r[0]>=date(2026,7,1)]
print("2026-07 이후 유동성≥100억",len(rec))
for lo,hi in buck:
    g=[r for r in rec if lo<=r[1]<hi]
    if not g: continue
    cnt=collections.Counter(r[3] for r in g)
    print(f"{lo}~{hi}%",len(g),f"{sum(r[4] for r in g)/len(g):.0%}",f"{cnt['arm']/len(g):.0%}",f"{cnt['stop']/len(g):.0%}",f"{cnt['open']/len(g):.0%}")

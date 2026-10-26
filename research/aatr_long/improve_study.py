"""ETH 개선 방향 3가지 검토: ① 레버리지 축소 ② 진입 ATR 배수 조정 ③ 진입가 기준 고정 % 손절.
4H 레포 데이터 (Binance 선물), 2021-03-08 ~ 2026-08-31. 과최적화 점검을 위해
전반(2021-03~2023) / 후반(2024~2026-08) 분할과 BTC·11종목 대조를 함께 본다.
"""
import sys
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from backtest import load, run  # noqa: E402

OUT = Path(__file__).parent / 'results'
SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'ADAUSDT',
           'DOGEUSDT', 'LINKUSDT', 'LTCUSDT', 'BCHUSDT', 'AVAXUSDT']
ts = lambda x: int(pd.Timestamp(x, tz='UTC').timestamp() * 1000)
PERIODS = {'전체': (ts('2021-03-08'), None), '전반': (ts('2021-03-08'), ts('2024-01-01')),
           '후반': (ts('2024-01-01'), None)}
data = {s: load(s) for s in SYMBOLS}
pd.set_option('display.width', 250); pd.set_option('display.max_columns', 40)
f2 = lambda x: f'{x:,.2f}'


def ev(sym, period='전체', funding=False, **kw):
    a, b = PERIODS[period]
    m, tr, _ = run(data[sym], start=a, end=b, funding=funding, **kw)
    m['stops'] = sum(1 for x in tr if x['reason'] == 'STOP')
    return m


def table(kws, label, syms=('ETHUSDT', 'BTCUSDT')):
    rows = []
    for name, kw in kws:
        r = {label: name}
        for s in syms:
            m = ev(s, **kw); mf = ev(s, funding=True, **kw)
            tag = s.replace('USDT', '')
            r.update({f'{tag} CAGR': m['cagr'], f'{tag} MDD': m['mdd'], f'{tag} MAR': m['mar'],
                      f'{tag} 펀딩후CAGR': mf['cagr']})
            for p in ('전반', '후반'):
                r[f'{tag} {p}CAGR'] = ev(s, period=p, **kw)['cagr']
        allm = [ev(s, **kw) for s in SYMBOLS]
        r['11종 CAGR중앙'] = np.median([m['cagr'] for m in allm])
        r['11종 MDD중앙'] = np.median([m['mdd'] for m in allm])
        r['11종 MAR중앙'] = np.median([m['mar'] for m in allm])
        rows.append(r)
    return pd.DataFrame(rows)


# ① 레버리지
lev = table([(f'{x}x', dict(lev=x)) for x in (1.0, 1.25, 1.5, 2.0)], '레버리지')
lev.to_csv(OUT / 'improve_leverage.csv', index=False)
print('# ① 레버리지'); print(lev.to_string(index=False, float_format=f2))

# ③ 고정 % 손절 (SMA720 청산과 병행)
stp = table([('없음', dict())] + [(f'{int(x*100)}%', dict(stop=x)) for x in (0.02, 0.03, 0.04, 0.05, 0.07, 0.10, 0.15)],
            '손절')
for s in ('ETHUSDT', 'BTCUSDT'):
    stp[f"{s.replace('USDT', '')} 손절횟수"] = [0] + [ev(s, stop=x)['stops'] for x in (0.02, 0.03, 0.04, 0.05, 0.07, 0.10, 0.15)]
stp.to_csv(OUT / 'improve_stop.csv', index=False)
print('\n# ③ 고정 % 손절'); print(stp.to_string(index=False, float_format=f2))

# ② 진입 ATR 배수 격자
KU = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0]
KD = [0.75, 1.25, 1.75, 2.25, 3.0]
rows = []
for s in SYMBOLS:
    for ku in KU:
        for kd in KD:
            for p in PERIODS:
                m = ev(s, period=p, k_up=ku, k_dn=kd)
                rows.append(dict(symbol=s, k_up=ku, k_dn=kd, period=p, cagr=m['cagr'], mdd=m['mdd'], mar=m['mar'],
                                 trades=m['trades']))
g = pd.DataFrame(rows); g.to_csv(OUT / 'improve_atr_grid.csv', index=False)
for s in ('ETHUSDT', 'BTCUSDT'):
    for met in ('cagr', 'mar'):
        print(f'\n# ② {s} 전체 {met} (행 k_up, 열 k_dn) — 원본 0.75/1.25')
        print(g[(g.symbol == s) & (g.period == '전체')].pivot(index='k_up', columns='k_dn', values=met).to_string(float_format=f2))
print('\n# ② 11종 중앙 MAR (전체)')
print(g[g.period == '전체'].groupby(['k_up', 'k_dn']).mar.median().unstack().to_string(float_format=f2))
# 전반 최적 → 후반 검증
print('\n# ② 전반 MAR 최적 배수를 후반에 적용')
for s in ('ETHUSDT', 'BTCUSDT'):
    x = g[(g.symbol == s)]
    best = x[x.period == '전반'].sort_values('mar', ascending=False).iloc[0]
    oos = x[(x.period == '후반') & (x.k_up == best.k_up) & (x.k_dn == best.k_dn)].iloc[0]
    base = x[(x.period == '후반') & (x.k_up == 0.75) & (x.k_dn == 1.25)].iloc[0]
    rank = x[x.period == '후반'].mar.rank(ascending=False)[oos.name]
    print(f'{s}: 전반 최적 {best.k_up}/{best.k_dn} (전반 MAR {best.mar:.2f}) → 후반 MAR {oos.mar:.2f}, '
          f'CAGR {oos.cagr:.2f} | 원본 후반 MAR {base.mar:.2f}, CAGR {base.cagr:.2f} | 후반 순위 {int(rank)}/{len(KU)*len(KD)}')

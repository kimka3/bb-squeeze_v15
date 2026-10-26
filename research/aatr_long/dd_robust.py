"""dd_study 후보의 견고성 점검: 분기 시작점 29개, 11종목(레포 선물 2021~, 1x), BTC+ETH 묶음."""
import sys
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from spot import load_spot  # noqa: E402
from backtest import load  # noqa: E402
from dd_engine import run2, stats  # noqa: E402

ts = lambda x: int(pd.Timestamp(x, tz='UTC').timestamp() * 1000)
END = ts('2026-10-26')
D = {s: load_spot(s) for s in ('BTCUSDT', 'ETHUSDT')}
T15, T20 = dict(trail=('pct', .15), reentry=180), dict(trail=('pct', .20), reentry=180)
CANDS = {'ETHUSDT': [('원본 1x', dict(lev=1.0)), ('트레일15%+재진입180', dict(lev=1.0, **T15)),
                     ('트레일20%+재진입180', dict(lev=1.0, **T20)), ('트레일25%+재진입60', dict(lev=1.0, trail=('pct', .25), reentry=60)),
                     ('트레일6ATR+재진입180', dict(lev=1.0, trail=('atr', 6), reentry=180))],
         'BTCUSDT': [('원본 2x', dict(lev=2.0)), ('변동성목표100%', dict(lev=2.0, vol_target=1.0)),
                     ('변동성목표80%', dict(lev=2.0, vol_target=0.8)), ('레버리지1.5x', dict(lev=1.5)),
                     ('트레일20%+재진입180', dict(lev=2.0, **T20))]}
print('# 분기 시작점 29개 (2018-01 ~ 2025-01) — 원본 대비')
for s, cl in CANDS.items():
    rows = []
    for st in pd.date_range('2018-01-01', '2025-01-01', freq='QS'):
        rows.append({n: stats(*(lambda e, tr, t: (e, t, tr))(*run2(D[s], ts(st), END, **kw))) for n, kw in cl})
    base = cl[0][0]
    for n, _ in cl[1:]:
        f = lambda k: np.mean([r[n][k] for r in rows]); g = lambda k: np.mean([r[base][k] for r in rows])
        print(f"{s} {n}: CAGR↑ {sum(r[n]['cagr'] > r[base]['cagr'] for r in rows)}/29, "
              f"MDD 얕음 {sum(r[n]['mdd'] > r[base]['mdd'] + 1e-9 for r in rows)}/29, MAR↑ {sum(r[n]['mar'] > r[base]['mar'] for r in rows)}/29 | "
              f"평균 CAGR {f('cagr'):.2f} vs {g('cagr'):.2f}, 평균 MDD {f('mdd'):.2f} vs {g('mdd'):.2f}")

print('\n# 11종목 (레포 선물 4H 2021-03 ~ 2026-08, 1x)')
rows = []
for s in ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'ADAUSDT', 'DOGEUSDT', 'LINKUSDT', 'LTCUSDT', 'BCHUSDT', 'AVAXUSDT']:
    d = load(s); r = {'symbol': s}
    for n, kw in [('원본', {}), ('T15', T15), ('T20', T20)]:
        e, tr, t = run2(d, ts('2021-03-08'), ts('2026-09-01'), lev=1.0, **kw); m = stats(e, t, tr)
        r[f'{n} CAGR'] = m['cagr']; r[f'{n} MDD'] = m['mdd']
    rows.append(r)
o = pd.DataFrame(rows); print(o.to_string(index=False, float_format=lambda x: f'{x:,.2f}'))
for n in ('T15', 'T20'):
    print(n, 'MDD 얕음', int((o[f'{n} MDD'] > o['원본 MDD']).sum()), '/ CAGR↑', int((o[f'{n} CAGR'] > o['원본 CAGR']).sum()), '/ 11')

print('\n# BTC 2x + ETH 1x 묶음 (월 리밸런싱)')
cv = lambda d, **kw: (lambda e, tr, t: pd.Series(e, index=pd.to_datetime(t, unit='ms')))(*run2(d, ts('2017-08-18'), END, **kw))
b2, e1, e1t = cv(D['BTCUSDT'], lev=2.0), cv(D['ETHUSDT'], lev=1.0), cv(D['ETHUSDT'], lev=1.0, **T20)


def port(a, b, w, start):
    r = pd.concat([a.pct_change(), b.pct_change()], axis=1).dropna(); r = r[r.index >= start]
    va, vb, prev, eq = w, 1 - w, None, [1.0]
    for (ra, rb), mm in zip(r.values, r.index.to_period('M')):
        if prev is not None and mm != prev:
            tot = va + vb; va, vb = tot * w, tot * (1 - w)
        prev = mm; va *= 1 + ra; vb *= 1 + rb; eq.append(va + vb)
    eq = np.array(eq); yrs = (r.index[-1] - r.index[0]).days / 365.25
    c, m = eq[-1] ** (1 / yrs) - 1, (eq / np.maximum.accumulate(eq) - 1).min()
    return c, m


for st in ('2017-12-15', '2020-01-01'):
    for nm, a, b, w in [('BTC2x 단독', b2, b2, 1.0), ('BTC2x+ETH1x 50/50', b2, e1, .5),
                        ('BTC2x+ETH1x(트레일20) 50/50', b2, e1t, .5), ('BTC2x 70 + ETH1x(트레일20) 30', b2, e1t, .7)]:
        c, m = port(a, b, w, st); print(st, nm, f'CAGR {c:.2f} MDD {m:.2f} MAR {c / -m:.2f}')

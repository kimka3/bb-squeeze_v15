"""평가손익 기준 MDD 축소 방법 비교 (Binance 현물 4H, BTC 원본 2x / ETH 원본 1x).
공정 비교: 각 방법의 (MDD, CAGR) 을 '레버리지만 낮춘 원본' 곡선과 비교한다.
같은 MDD 에서 레버리지만 낮춘 원본보다 CAGR 이 높아야 의미가 있다 (= 초과 CAGR > 0).
"""
import sys
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from spot import load_spot  # noqa: E402
from dd_engine import run2, stats  # noqa: E402

OUT = Path(__file__).parent / 'results'
ts = lambda x: int(pd.Timestamp(x, tz='UTC').timestamp() * 1000)
END = ts('2026-10-26')
PERIODS = {'2017~': (ts('2017-08-18'), END), '2020~': (ts('2020-01-01'), END),
           '전반 2017~21': (ts('2017-08-18'), ts('2022-01-01')), '후반 2022~': (ts('2022-01-01'), END)}
BASE_LEV = {'BTCUSDT': 2.0, 'ETHUSDT': 1.0}
data = {s: load_spot(s) for s in BASE_LEV}
pd.set_option('display.width', 250); pd.set_option('display.max_columns', 40)


def ev(sym, period, **kw):
    a, b = PERIODS[period]
    eq, tr, t = run2(data[sym], a, b, **kw)
    return stats(eq, t, tr)


# 레버리지 곡선 (기준선)
frontier = {}
for s in data:
    for p in PERIODS:
        pts = sorted((ev(s, p, lev=L)['mdd'], ev(s, p, lev=L)['cagr']) for L in np.arange(0.25, BASE_LEV[s] + 0.01, 0.05))
        frontier[(s, p)] = (np.array([x[0] for x in pts]), np.array([x[1] for x in pts]))


def excess(s, p, mdd, cagr):
    m, c = frontier[(s, p)]
    if mdd < m.min() - 1e-6:  # 기준선 범위 밖 (최대 레버리지보다 MDD 가 큼)
        return np.nan
    return cagr - float(np.interp(mdd, m, c))


METHODS = [('원본', {})]
for x in (0.15, 0.20, 0.25, 0.30, 0.40):
    for re in (None, 60, 180):
        METHODS.append((f'트레일 {int(x*100)}%' + (f' +재진입{re}' if re else ''), dict(trail=('pct', x), reentry=re)))
for k in (6, 10, 15):
    for re in (None, 60, 180):
        METHODS.append((f'트레일 {k}ATR' + (f' +재진입{re}' if re else ''), dict(trail=('atr', k), reentry=re)))
for p in (0.3, 0.5, 1.0):
    for keep in (0.5, 0.67):
        METHODS.append((f'이익 {int(p*100)}%에 {int(keep*100)}%만 유지', dict(derisk=(p, keep))))
VT = {'BTCUSDT': (0.8, 1.0, 1.2), 'ETHUSDT': (0.5, 0.6, 0.7)}

rows = []
for s in data:
    meths = METHODS + [(f'변동성 목표 {int(v*100)}%', dict(vol_target=v)) for v in VT[s]] \
        + [(f'레버리지 {L}x', dict(lev=L)) for L in ((1.0, 1.5) if s == 'BTCUSDT' else (0.5, 0.75))]
    for name, kw in meths:
        kw = {'lev': BASE_LEV[s], **kw}
        r = dict(symbol=s, method=name)
        for p in PERIODS:
            m = ev(s, p, **kw)
            r[f'{p} CAGR'] = m['cagr']; r[f'{p} MDD'] = m['mdd']
            r[f'{p} 초과'] = excess(s, p, m['mdd'], m['cagr'])
        r['거래(2017~)'] = ev(s, '2017~', **kw)['trades']
        r['초과>0 기간수'] = int(sum((r[f'{p} 초과'] > 0.005) for p in PERIODS if not np.isnan(r[f'{p} 초과'])))
        rows.append(r)
res = pd.DataFrame(rows); res.to_csv(OUT / 'dd_methods.csv', index=False)
cols = ['method', '2017~ CAGR', '2017~ MDD', '2017~ 초과', '2020~ CAGR', '2020~ MDD', '2020~ 초과',
        '전반 2017~21 초과', '후반 2022~ 초과', '거래(2017~)', '초과>0 기간수']
for s in data:
    print(f'\n# {s} (기준 {BASE_LEV[s]}x)')
    print(res[res.symbol == s][cols].to_string(index=False, float_format=lambda x: f'{x:,.2f}'))

"""Binance 현물 4H (2017-08 ~ 2026-10) 로 다시 하는 종합 검증.
TradingView 'BINANCE:BTCUSDT / ETHUSDT' 와 같은 데이터. 펀딩 없음 (현물 기준).
MDD 는 두 가지로 낸다: mtm = 4H 종가 평가 자산 기준(실제 체감), closed = 청산 거래 누적 기준(TV 표기와 가까움).
먼저 python3 research/aatr_long/fetch_binance_vision.py BTCUSDT ETHUSDT 로 데이터를 받아야 한다.
"""
import sys
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from backtest import run  # noqa: E402
from spot import load_spot  # noqa: E402

OUT = Path(__file__).parent / 'results'
ts = lambda x: int(pd.Timestamp(x, tz='UTC').timestamp() * 1000)
END = ts('2026-10-26')
data = {s: load_spot(s) for s in ('BTCUSDT', 'ETHUSDT')}
pd.set_option('display.width', 250); pd.set_option('display.max_columns', 40)
f2 = lambda x: f'{x:,.2f}'


def closed_mdd(tr, cap=1e5):
    ce = np.r_[cap, cap + np.cumsum([x['pnl'] for x in tr if x['reason'] != 'OPEN'])]
    return float((ce / np.maximum.accumulate(ce) - 1).min())


def strat(sym, start, end=END, **kw):
    m, tr, _ = run(data[sym], start=ts(start) if isinstance(start, str) else start, end=end, **kw)
    return dict(ret=m['ret'], cagr=m['cagr'], mdd=m['mdd'], mdd_closed=closed_mdd(tr), mar=m['mar'],
                trades=m['trades'], pf=m['pf'], win=m['win'])


def bh(sym, start, end=END, fee=0.001):
    d = data[sym]; t = d.time.to_numpy()
    lo = int(np.searchsorted(t, ts(start) if isinstance(start, str) else start)); hi = int(np.searchsorted(t, end))
    eq = d.close.to_numpy()[lo:hi] / d.open.to_numpy()[lo] * (1 - fee)
    yrs = (t[hi - 1] - t[lo]) / (365.25 * 86400000)
    mdd = float((eq / np.maximum.accumulate(eq) - 1).min())
    cagr = eq[-1] ** (1 / yrs) - 1
    return dict(ret=eq[-1] - 1, cagr=cagr, mdd=mdd, mdd_closed=np.nan, mar=cagr / -mdd, trades=1, pf=np.nan, win=np.nan)


CFG = [('원본 0.75 · 2x', dict()), ('k_up 0.25 · 2x', dict(k_up=0.25)),
       ('원본 0.75 · 1x', dict(lev=1.0)), ('k_up 0.25 · 1x', dict(k_up=0.25, lev=1.0))]
STARTS = {'BTCUSDT': ['2017-08-17', '2020-01-01'], 'ETHUSDT': ['2017-08-17', '2020-01-01']}

# 1) 주요 비교
rows = []
for s in data:
    for st in STARTS[s]:
        for name, kw in CFG:
            rows.append(dict(symbol=s, start=st, cfg=name, **strat(s, st, **kw)))
        rows.append(dict(symbol=s, start=st, cfg='BH 1x', **bh(s, st)))
main = pd.DataFrame(rows); main.to_csv(OUT / 'spot_main.csv', index=False)
print('# 1) 주요 비교 (종료 2026-10-25)'); print(main.to_string(index=False, float_format=f2))

# 2) k_up 격자 (2x), 전반 2017-08~2021 / 후반 2022~2026-10
rows = []
for s in data:
    for ku in (0.0, 0.1, 0.25, 0.5, 0.75, 1.0, 1.25):
        r = dict(symbol=s, k_up=ku)
        for label, a, b in [('전체', '2017-08-17', END), ('전반', '2017-08-17', ts('2022-01-01')), ('후반', '2022-01-01', END)]:
            m = strat(s, a, b, k_up=ku)
            r.update({f'{label} CAGR': m['cagr'], f'{label} MDD': m['mdd']})
        r['전체 MAR'] = r['전체 CAGR'] / -r['전체 MDD']
        rows.append(r)
grid = pd.DataFrame(rows); grid.to_csv(OUT / 'spot_kup_grid.csv', index=False)
print('\n# 2) k_up 격자 (2x)'); print(grid.to_string(index=False, float_format=f2))

# 3) 시작 시점 민감도 (분기마다, 2018-01 ~ 2025-01)
rows = []
for s in data:
    for st in pd.date_range('2018-01-01', '2025-01-01', freq='QS'):
        a = ts(st); r = dict(symbol=s, start=st.date())
        for name, kw in CFG[:2]:
            m = strat(s, a, **kw); r[f'{name} CAGR'] = m['cagr']; r[f'{name} MDD'] = m['mdd']
        b = bh(s, a); r['BH CAGR'] = b['cagr']; r['BH MDD'] = b['mdd']
        rows.append(r)
roll = pd.DataFrame(rows); roll.to_csv(OUT / 'spot_rolling_starts.csv', index=False)
print('\n# 3) 시작 시점 민감도 요약 (29개 시작점)')
for s in data:
    x = roll[roll.symbol == s]
    print(s, '| 0.25>0.75 CAGR', int((x['k_up 0.25 · 2x CAGR'] > x['원본 0.75 · 2x CAGR']).sum()),
          '| 0.25 MDD 더 얕음', int((x['k_up 0.25 · 2x MDD'] > x['원본 0.75 · 2x MDD']).sum()),
          '| 원본>BH CAGR', int((x['원본 0.75 · 2x CAGR'] > x['BH CAGR']).sum()),
          '| 0.25>BH CAGR', int((x['k_up 0.25 · 2x CAGR'] > x['BH CAGR']).sum()), '/', len(x))

# 4) 앞서 기각한 변형 재확인 (2017-08 ~, 2x)
rows = []
for s in data:
    for name, kw in [('원본', {}), ('TP15', dict(tp=0.15)), ('S240 또는 S720 (E)', dict(exit_len=240, either=True)),
                     ('손절 5%', dict(stop=0.05)), ('손절 10%', dict(stop=0.10))]:
        rows.append(dict(symbol=s, cfg=name, **strat(s, '2017-08-17', **kw)))
rej = pd.DataFrame(rows); rej.to_csv(OUT / 'spot_rejected_recheck.csv', index=False)
print('\n# 4) 기각 변형 재확인 (2017-08 ~, 2x)'); print(rej.to_string(index=False, float_format=f2))

# 5) 연도별 (2x)
rows = []
for s in data:
    t = data[s].time.to_numpy(); lo, hi = np.searchsorted(t, ts('2017-08-17')), np.searchsorted(t, END)
    idx = pd.to_datetime(t[lo:hi], unit='ms')
    cols = {}
    for name, kw in CFG[:2]:
        cols[name] = run(data[s], start=ts('2017-08-17'), end=END, **kw)[2]
    cols['BH 1x'] = data[s].close.to_numpy()[lo:hi]
    y = pd.DataFrame(cols, index=idx).resample('YE').last()
    first = pd.DataFrame([[1e5, 1e5, data[s].open.to_numpy()[lo]]], columns=y.columns)
    yr = pd.concat([first, y.reset_index(drop=True)]).pct_change().iloc[1:]; yr.index = y.index.year
    yr.insert(0, 'symbol', s); rows.append(yr)
yearly = pd.concat(rows); yearly.to_csv(OUT / 'spot_yearly.csv')
print('\n# 5) 연도별'); print(yearly.to_string(float_format=f2))

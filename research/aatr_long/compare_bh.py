"""E안 vs 바이앤홀드 비교. 결과는 results/compare_bh*.csv.

바이앤홀드
  BH 1x : 첫 봉 시가 매수 후 보유 (현물 가정, 수수료 0.1% 1회, 펀딩 없음)
  BH 2x : 첫 봉 시가에 자산 200% 롱 1회 진입 후 리밸런싱 없이 보유. 자산이 0 이하가 되면 청산(소멸)
          펀딩은 E안과 같은 규칙으로 별도 반영 열을 둔다.
E안은 레버리지 1x·2x 둘 다 계산해 같은 레버리지끼리 비교한다.
"""
import sys
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from backtest import load, run, metrics  # noqa: E402

OUT = Path(__file__).parent / 'results'
SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'ADAUSDT',
           'DOGEUSDT', 'LINKUSDT', 'LTCUSDT', 'BCHUSDT', 'AVAXUSDT']
START = int(pd.Timestamp('2021-03-08', tz='UTC').timestamp() * 1000)
SPLIT = int(pd.Timestamp('2024-01-01', tz='UTC').timestamp() * 1000)
E = dict(exit_len=240, either=True)


def buy_hold(d, lev, start, end=None, funding=False, fee=0.001, cap=100000.0):
    t = d.time.to_numpy()
    lo = int(np.searchsorted(t, start)); hi = len(d) if end is None else int(np.searchsorted(t, end))
    o, l, c, f = (d[k].to_numpy()[lo:hi] for k in ['open', 'low', 'close', 'fund'])
    qty = lev * cap / o[0]
    cash = cap - qty * o[0] * (1 + fee)
    paid = np.cumsum(np.r_[0.0, qty * o[1:] * f[1:]]) if lev > 1 or funding else np.zeros(len(o))
    adj = paid if funding else 0.0
    eq = cash + qty * c - adj
    eql = cash + qty * l - adj
    dead = np.argmax(eql <= 0) if (eql <= 0).any() else None
    if dead is not None:
        eq[dead:] = 0.0; eql[dead:] = 0.0
    tr = [dict(entry_t=t[lo], exit_t=t[hi - 1], pnl=eq[-1] - cap, reason='OPEN')]
    return metrics(t[lo:hi], np.maximum(eq, 1e-9), np.maximum(eql, 1e-9), tr, cap, paid[-1] if lev > 1 else 0.0)


if __name__ == '__main__':
    rows = []
    for s in SYMBOLS:
        d = load(s)
        for label, a, b in [('전체 2021-03~2026-08', START, None),
                            ('2021-03~2023', START, SPLIT), ('2024~2026-08', SPLIT, None)]:
            for name, fn in [
                ('BH 1x', lambda fu: buy_hold(d, 1, a, b, fu)),
                ('E 1x', lambda fu: run(d, start=a, end=b, lev=1.0, funding=fu, **E)[0]),
                ('BH 2x', lambda fu: buy_hold(d, 2, a, b, fu)),
                ('E 2x', lambda fu: run(d, start=a, end=b, lev=2.0, funding=fu, **E)[0]),
            ]:
                m = fn(False); mf = fn(True)
                rows.append(dict(symbol=s, period=label, strategy=name, ret=m['ret'], cagr=m['cagr'],
                                 mdd=m['mdd'], mar=m['mar'], sharpe=m['sharpe'], exposure=m['exposure'],
                                 cagr_funding=mf['cagr'] if name != 'BH 1x' else m['cagr']))
    df = pd.DataFrame(rows)
    df.to_csv(OUT / 'compare_bh.csv', index=False)

    # BTC 연도별
    d = load('BTCUSDT')
    t = pd.to_datetime(d.time[d.time >= START].to_numpy(), unit='ms')
    _, _, e2 = run(d, start=START, lev=2.0, **E)
    _, _, e1 = run(d, start=START, lev=1.0, **E)
    px = d.close[d.time >= START].to_numpy()
    yr = pd.DataFrame({'E 2x': e2, 'E 1x': e1, 'BH 1x': px}, index=t).resample('YE').last()
    first = pd.DataFrame({'E 2x': [1e5], 'E 1x': [1e5], 'BH 1x': [d.open[d.time >= START].iloc[0]]})
    yr_ret = pd.concat([first, yr.reset_index(drop=True)]).pct_change().iloc[1:]
    yr_ret.index = yr.index.year
    yr_ret.to_csv(OUT / 'compare_bh_btc_yearly.csv')

    pd.set_option('display.width', 250)
    f = lambda x: f'{x:,.2f}'
    cols = ['strategy', 'ret', 'cagr', 'mdd', 'mar', 'sharpe', 'exposure', 'cagr_funding']
    for p in df.period.unique():
        print(f'\n# BTC {p}')
        print(df[(df.symbol == 'BTCUSDT') & (df.period == p)][cols].to_string(index=False, float_format=f))
    print('\n# BTC 연도별'); print(yr_ret.to_string(float_format=f))
    full = df[df.period.str.startswith('전체')]
    print('\n# 11종목 중앙값 (전체)')
    print(full.groupby('strategy', sort=False)[['cagr', 'mdd', 'mar', 'sharpe', 'cagr_funding']].median().to_string(float_format=f))
    w = full.pivot(index='symbol', columns='strategy', values='cagr')
    m = full.pivot(index='symbol', columns='strategy', values='mdd')
    print('\n# 종목별 CAGR'); print(w[['BH 1x', 'E 1x', 'BH 2x', 'E 2x']].to_string(float_format=f))
    print('\n# 종목별 MDD'); print(m[['BH 1x', 'E 1x', 'BH 2x', 'E 2x']].to_string(float_format=f))
    print('\nE2x > BH1x CAGR:', int((w['E 2x'] > w['BH 1x']).sum()), '/ 11',
          '| E1x > BH1x CAGR:', int((w['E 1x'] > w['BH 1x']).sum()), '/ 11',
          '| E1x MDD 더 얕음:', int((m['E 1x'] > m['BH 1x']).sum()), '/ 11')

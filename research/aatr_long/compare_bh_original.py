"""원본(S720 청산) vs 바이앤홀드.

1) 4H 레포 데이터 (Binance 선물, 2021-03-08 ~ 2026-08-31): Pine 규칙 그대로 재현
2) 일봉 근사 (Alpha Vantage BTC/USD 일봉 CSV 경로를 인자로): 2018 ~ 2026-10 로 기간을 넓혀
   시작 시점 민감도를 본다. SMA720(4H)→SMA120(일), ATR 은 4H/일 비율 0.413 으로 보정.
사용: python3 research/aatr_long/compare_bh_original.py <btc_daily.csv>
"""
import sys
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from backtest import load, run, rma, cross_over, cross_under  # noqa: E402
from compare_bh import buy_hold  # noqa: E402

pd.set_option('display.width', 250)
f = lambda x: f'{x:,.2f}'


def stats(t, eq):
    yrs = (t[-1] - t[0]).days / 365.25
    cagr = (eq[-1] / eq[0]) ** (1 / yrs) - 1 if eq[-1] > 0 else -1.0
    mdd = (eq / np.maximum.accumulate(eq) - 1).min()
    r = pd.Series(eq, index=t).pct_change().dropna()
    return dict(ret=eq[-1] / eq[0] - 1, cagr=cagr, mdd=mdd, mar=cagr / -mdd if mdd < 0 else np.nan,
                sharpe=r.mean() / r.std() * np.sqrt(365) if r.std() > 0 else np.nan)


# ── 1) 4H 레포 데이터
START = int(pd.Timestamp('2021-03-08', tz='UTC').timestamp() * 1000)
d4 = load('BTCUSDT')
rows = []
for lev in (1.0, 2.0):
    m, _, _ = run(d4, start=START, lev=lev)
    mf, _, _ = run(d4, start=START, lev=lev, funding=True)
    rows.append(dict(strategy=f'원본 {lev:.0f}x', cagr=m['cagr'], mdd=m['mdd'], mar=m['mar'], sharpe=m['sharpe'],
                     exposure=m['exposure'], cagr_funding=mf['cagr']))
for lev in (1, 2):
    m = buy_hold(d4, lev, START); mf = buy_hold(d4, lev, START, funding=True)
    rows.append(dict(strategy=f'BH {lev}x', cagr=m['cagr'], mdd=m['mdd'], mar=m['mar'], sharpe=m['sharpe'],
                     exposure=1.0, cagr_funding=mf['cagr'] if lev > 1 else m['cagr']))
print('# 1) 4H 레포 BTC 2021-03-08 ~ 2026-08-31')
print(pd.DataFrame(rows).to_string(index=False, float_format=f))

# ── 2) 일봉 근사
D = pd.read_csv(sys.argv[1], parse_dates=['timestamp']).sort_values('timestamp').reset_index(drop=True)
D = D[D.timestamp >= '2016-06-01'].reset_index(drop=True)
o, h, l, c = (D[k].to_numpy(float) for k in ['open', 'high', 'low', 'close'])
pc = np.r_[np.nan, c[:-1]]
tr = np.where(np.isnan(pc), h - l, np.maximum.reduce([h - l, np.abs(h - pc), np.abs(l - pc)]))
atr = rma(tr, 20) * 0.413
s720 = pd.Series(c).rolling(120).mean().to_numpy()
band = s720 + np.where(s720 > np.r_[np.full(5, np.nan), s720[:-5]], 0.75, 1.25) * atr
ent, xs = cross_over(c, band), cross_under(c, s720)
T = D.timestamp


def strat(lo, hi, lev):
    cash, q, pe, px, eq = 1e5, 0.0, 0.0, False, []
    for i in range(lo, hi):
        if px and q > 0: cash += q * o[i] * 0.999; q = 0.0
        if pe and q == 0: q = pe; cash -= q * o[i] * 1.001
        pe, px = 0.0, False
        e = cash + q * c[i]
        if e <= 0: eq += [1e-9] * (hi - i); break
        eq.append(e)
        if q > 0: px = bool(xs[i])
        elif ent[i]: pe = lev * e / c[i]
    return np.array(eq)


def bh(lo, hi, lev):
    q = lev * 1e5 / o[lo]; cash = 1e5 - q * o[lo] * 1.001
    eq = cash + q * c[lo:hi]
    if (cash + q * l[lo:hi] <= 0).any():
        k = np.argmax(cash + q * l[lo:hi] <= 0); eq[k:] = 1e-9
    return eq


def idx(a, b):
    return int(np.searchsorted(T, pd.Timestamp(a))), int(np.searchsorted(T, pd.Timestamp(b)))


print('\n# 2a) 일봉 근사 2020-01-01 ~ 2026-10-25 (TradingView 와 같은 시작일)')
lo, hi = idx('2020-01-01', '2026-10-26'); t = T[lo:hi].to_numpy()
rows = [dict(strategy=n, **stats(pd.DatetimeIndex(t), fn(lo, hi, lv))) for n, fn, lv in
        [('원본 1x', strat, 1), ('원본 2x', strat, 2), ('BH 1x', bh, 1), ('BH 2x', bh, 2)]]
print(pd.DataFrame(rows).to_string(index=False, float_format=f))

print('\n# 2b) 연도별 수익률 (일봉 근사, 2018~)')
lo, hi = idx('2018-01-01', '2026-10-26')
yr = pd.DataFrame({'원본 2x': strat(lo, hi, 2), '원본 1x': strat(lo, hi, 1), 'BH 1x': bh(lo, hi, 1)},
                  index=T[lo:hi]).resample('YE').last()
yr = pd.concat([pd.DataFrame([[1e5] * 3], columns=yr.columns), yr.reset_index(drop=True)]).pct_change().iloc[1:]
yr.index = range(2018, 2027)
print(yr.to_string(float_format=f))

print('\n# 2c) 시작 시점 민감도: 분기마다 시작 → 2026-10-25 까지')
rows = []
for s in pd.date_range('2018-01-01', '2025-01-01', freq='QS'):
    lo, hi = idx(s, '2026-10-26'); tt = pd.DatetimeIndex(T[lo:hi])
    r = {'start': s.date()}
    for n, fn, lv in [('원본2x', strat, 2), ('원본1x', strat, 1), ('BH1x', bh, 1)]:
        st = stats(tt, fn(lo, hi, lv)); r[f'{n}_cagr'] = st['cagr']; r[f'{n}_mdd'] = st['mdd']
    rows.append(r)
s = pd.DataFrame(rows)
print(s.to_string(index=False, float_format=f))
print('원본2x CAGR > BH1x:', int((s['원본2x_cagr'] > s['BH1x_cagr']).sum()), '/', len(s),
      '| 원본1x CAGR > BH1x:', int((s['원본1x_cagr'] > s['BH1x_cagr']).sum()), '/', len(s))

"""평가손익 기준 MDD 축소 방법을 시험하는 확장 엔진 (현물, 펀딩 없음).

원본 규칙 (진입: SMA720 + k*ATR 상향 교차, 청산: SMA720 하향 교차, 다음 봉 시가 체결) 위에 아래를 얹는다.
모든 판단은 4H 종가에서 하고 체결은 다음 봉 시가다 (Pine 으로 그대로 옮길 수 있게).
  trail      : ('pct', x) 진입 후 최고 종가 대비 x 하락 / ('atr', k) 최고 종가 - k*ATR 하향 이탈 시 청산
  reentry    : 트레일 청산 뒤 무포지션이고 종가 > 진입선 일 때, 종가가 직전 N 봉 최고 종가를 넘으면 재진입
  derisk     : (p, keep) 진입가 대비 종가 수익률이 p 이상이 되면 다음 시가에 수량을 keep 비율만 남긴다 (1회)
  vol_target : 진입 시 레버리지 = min(lev, 목표변동성 / 최근 180봉 실현변동성)
"""
import numpy as np
import pandas as pd
from backtest import indicators, cross_over, cross_under, rma

H4 = 14400000


def atr20(d):
    c, h, l = d.close.to_numpy(), d.high.to_numpy(), d.low.to_numpy()
    pc = np.r_[np.nan, c[:-1]]
    tr = np.where(np.isnan(pc), h - l, np.maximum.reduce([h - l, np.abs(h - pc), np.abs(l - pc)]))
    return rma(tr, 20)


def run2(d, start, end, lev=2.0, k_up=0.75, trail=None, reentry=None, derisk=None, vol_target=None,
         fee=0.001, cap=100000.0):
    o, h, l, c = (d[k].to_numpy() for k in ['open', 'high', 'low', 'close'])
    t = d.time.to_numpy()
    sma, band = indicators(d, k_up=k_up)
    ent = cross_over(c, band); xs = cross_under(c, sma)
    atr = atr20(d)
    lr = np.r_[np.nan, np.diff(np.log(c))]
    vol = pd.Series(lr).rolling(180).std().to_numpy() * np.sqrt(6 * 365)
    hh = pd.Series(c).rolling(reentry, min_periods=1).max().shift(1).to_numpy() if reentry else None
    lo, hi = int(np.searchsorted(t, start)), int(np.searchsorted(t, end))
    cash, qty = cap, 0.0
    pend_q = 0.0; pend_exit = False; pend_cut = 0.0
    peak = entry_px = 0.0; cut_done = False; trail_out = False
    eq = np.empty(hi - lo); trades = []; cur = None
    for j, i in enumerate(range(lo, hi)):
        if pend_exit and qty > 0:
            cash += qty * o[i] * (1 - fee); cur['pnl'] = cash - cur['eq0']; cur['exit_t'] = t[i]
            trades.append(cur); qty = 0.0
        if pend_cut and qty > 0:
            sell = qty * (1 - pend_cut); cash += sell * o[i] * (1 - fee); qty -= sell
        if pend_q and qty == 0:
            eq0 = cash; qty = pend_q; cash -= qty * o[i] * (1 + fee)
            entry_px = o[i]; peak = c[i - 1]; cut_done = False
            cur = dict(entry_t=t[i], eq0=eq0)
        pend_q = 0.0; pend_exit = False; pend_cut = 0.0
        eq[j] = cash + qty * c[i]
        if qty > 0:
            peak = max(peak, c[i])
            ex = bool(xs[i]); why = 'sma'
            if trail and not ex:
                kind, x = trail
                stop = peak * (1 - x) if kind == 'pct' else peak - x * atr[i]
                if c[i] < stop:
                    ex = True; why = 'trail'
            if ex:
                pend_exit = True; trail_out = why == 'trail'
            elif derisk and not cut_done and c[i] / entry_px - 1 >= derisk[0]:
                pend_cut = derisk[1]; cut_done = True
        else:
            go = bool(ent[i])
            if not go and trail_out and reentry and c[i] > band[i] and c[i] > hh[i]:
                go = True
            if xs[i]:
                trail_out = False  # SMA720 아래로 내려가면 일반 상태로 복귀 (재진입은 진입선 교차로만)
            if go and i + 1 < hi:
                L = lev if not vol_target or np.isnan(vol[i]) else min(lev, vol_target / vol[i])
                pend_q = L * eq[j] / c[i]; trail_out = False
    if qty > 0:
        cur['pnl'] = eq[-1] - cur['eq0']; cur['exit_t'] = t[hi - 1]; trades.append(cur)
    return eq, trades, t[lo:hi]


def stats(eq, t, trades):
    yrs = (t[-1] - t[0]) / (365.25 * 86400000)
    cagr = (eq[-1] / eq[0]) ** (1 / yrs) - 1 if eq[-1] > 0 else -1.0
    mdd = float((eq / np.maximum.accumulate(eq) - 1).min())
    pn = np.array([x['pnl'] for x in trades]) if trades else np.zeros(1)
    ce = np.r_[eq[0], eq[0] + np.cumsum(pn)]
    mddc = float((ce / np.maximum.accumulate(ce) - 1).min())
    ulcer = float(np.sqrt(np.mean((eq / np.maximum.accumulate(eq) - 1) ** 2)))
    return dict(cagr=cagr, mdd=mdd, mdd_closed=mddc, mar=cagr / -mdd if mdd < 0 else np.nan, ulcer=ulcer,
                trades=len(trades))

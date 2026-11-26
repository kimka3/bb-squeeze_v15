"""Would a looser squeeze filter help? Tested, not assumed.

The short signal needs 3 of the prior 5 bars "in squeeze": BB width in the
bottom 20% AND ATR% in the bottom 30% of the last 120 bars. A crash that runs in
already-expanded volatility never qualifies, which is why 2026-11 produced no
signal. This varies only the squeeze thresholds; every other condition and the
engine are unchanged. The squeeze is independent of the other columns, so the
indicators are computed once and only the squeeze is recomputed per setting.

    python src/live/squeeze_study.py

Findings (data to 2026-11-22, every ambiguous 4h bar resolved on 5m):
loosening the ATR threshold or the bar count sometimes raises CAGR but deepens
the drawdown more, so CAGR/MDD is highest at ATR 0.30 / 3 bars for every BB
width tested. Loosening only the BB-width threshold to 25-40% helps in both
halves, in 4-5 of 6 years and in the 6/10/11-symbol universes, but leaves the
2026-10/11 episode unchanged (7 shorts, -6.8R). None of the 36 grid settings
made money in that episode.
"""
import itertools
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np, pandas as pd
import bb_squeeze_v5 as short
import data_io, frontier_engine
from data_io import START, END, SYMBOLS, records
from frontier_engine import Strategy, run, Market
from analyze_results import SPLIT, period
from run_frontier import prepared
from data_io import load_micro
from live.config import LiveConfig
from live.cost_study import Books

ROOT = Path(__file__).resolve().parents[2]
UNI = list(LiveConfig().universe)
BASE = Strategy(name='g', risk=.02, short_btc_bull_risk=.5, fee=0., slippage=.0002, total_risk=.04)
EP0, EP1 = 1792022400000, END     # 2026-10-15 ~ 끝: 이번 하락 국면
SNAP = 'results/books/lighter_20261110.json'

data, funding, marks, audit = prepared()
micro = load_micro(ROOT / 'data')
IND = {}
for s in UNI:
    raw = records(ROOT / 'data/raw', s, '4h')
    d = pd.DataFrame([r[:6] for r in raw], columns=['time','open','high','low','close','volume'])
    d['time'] = d.time.astype('int64')
    for c in ['open','high','low','close','volume']: d[c] = pd.to_numeric(d[c])
    d = d.sort_values('time').drop_duplicates('time').reset_index(drop=True)
    d['timestamp'] = pd.to_datetime(d.time, unit='ms', utc=True)
    x = short.add_indicators(d, short.P)
    x.index = d.time.values
    IND[s] = x
ORIG = {s: data[s]['short_signal'].copy() for s in UNI}

def signals(bbw_max, atr_max, window, min_bars):
    out = {}
    for s, x in IND.items():
        core = (x.bbw_pctile <= bbw_max) & (x.atr_pctile <= atr_max)
        prior = core.shift(1, fill_value=False).astype(float)
        ready = prior.rolling(window).sum() >= min_bars
        out[s] = (ready & x.short_breakout & x.expansion_ok & x.slope_ok).fillna(False)
    return out

def market_with(sig):
    for s in UNI:
        data[s]['short_signal'] = sig[s].reindex(data[s].index).fillna(False).astype(bool).values
    return Market(data, funding, marks, micro)

def engine(m, **kw):
    saved = frontier_engine.SYMBOLS, frontier_engine.LONGS
    frontier_engine.SYMBOLS = UNI; frontier_engine.LONGS = [s for s in data_io.LONGS if s in UNI]
    try:
        return run(m, BASE, START, END, **kw)
    finally:
        frontier_engine.SYMBOLS, frontier_engine.LONGS = saved

def summarize(r):
    m, e, t = r['metrics'], r['equity'], r['trades']
    dev, val = period(e, START, SPLIT), period(e, SPLIT, END)
    sh = t[t.side == 'SHORT']
    ep = sh[sh.entry_ms >= EP0]
    return dict(cagr=m['cagr_pct'], mdd=m['mdd_close_pct'], cal=m['cagr_pct']/abs(m['mdd_close_pct']),
                pf=m['profit_factor'], shorts=len(sh), short_r=sh.r_multiple.mean(),
                dev=dev['cagr_pct'], val=val['cagr_pct'], dev_mdd=dev['mdd_pct'], val_mdd=val['mdd_pct'],
                ep_n=len(ep), ep_r=ep.r_multiple.sum(), coarse=r['stats']['coarse_position_bars'])


def main():
    from equity_replay import equity_5m
    base = signals(.20, .30, 5, 3)
    bad = sum(int((ORIG[s].astype(bool) != base[s].reindex(ORIG[s].index).fillna(False)).sum()) for s in UNI)
    print(f'reproduces original short_signal: {"yes" if bad == 0 else f"NO ({bad} bars)"}\n')
    print(f"{'BBW':>5s}{'ATR':>5s}{'bars':>5s}{'shorts':>7s}{'CAGR':>8s}{'MDD4h':>8s}"
          f"{'2021-23':>9s}{'2024-':>8s}{'episode R':>10s}")
    for bbw, atr, mb in itertools.product((.20, .25, .30, .40), (.30, .40, .50), (3, 2, 1)):
        f = summarize(engine(market_with(signals(bbw, atr, 5, mb))))
        print(f"{bbw:>5.2f}{atr:>5.2f}{mb:>5d}{f['shorts']:>7d}{f['cagr']:>7.2f}%{f['mdd']:>7.2f}%"
              f"{f['dev']:>8.1f}%{f['val']:>7.1f}%{f['ep_r']:>+10.1f}")
    print('\nexact 5m equity, ATR 0.30, 3 of 5 bars')
    for bbw in (.20, .25, .30, .40, .50, .60, .80):
        m = market_with(signals(bbw, .30, 5, 3))
        r = engine(m)
        fine, dd5 = equity_5m(m, r)
        f = summarize(r)
        c = engine(m, slippage_of=Books(SNAP, capital=50_000).rate)
        _, cdd = equity_5m(m, c)
        print(f"BBW {bbw:.2f}  CAGR {f['cagr']:6.2f}%  MDD5m {dd5:7.2f}%  Calmar {f['cagr']/abs(dd5):.2f}  "
              f"measured@50k CAGR {c['metrics']['cagr_pct']:6.2f}%  episode {f['ep_n']} / {f['ep_r']:+.1f}R")


if __name__ == '__main__':
    main()

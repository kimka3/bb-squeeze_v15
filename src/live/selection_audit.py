"""Is dropping ADA, LTC, AVAX and LINK a cost decision or hindsight?

The exclusion was argued from book depth, but the person arguing it had already
seen the backtest. So the claim "this is cost, not selection" needs tests that
could fail:

  1. PERMUTATION  Every way of dropping 4 of the 9 non-BTC symbols (126 sets),
     each run end to end. At a FLAT cost the chosen set should be unremarkable;
     if it sits near the top there, the choice tracks sample P&L. At MEASURED
     cost it should sit near the top, because that is what it was chosen for.
  2. CONFOUND     Are the expensive symbols also the sample's losers? If cost
     rank and P&L rank line up, a cost rule and a hindsight rule pick the same
     names and the two explanations cannot be separated by the outcome alone.
  3. STABILITY    Does the flat-cost difference hold in both halves of the
     sample, or flip sign (noise)?
  4. RULE         Would a P&L-blind threshold on round-trip cost per 1R pick
     the same four on two independent book snapshots?

    python src/live/selection_audit.py
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np                                                   # noqa: E402
import pandas as pd                                                  # noqa: E402

import data_io                                                       # noqa: E402
import frontier_engine                                               # noqa: E402
from analyze_results import SPLIT, period                            # noqa: E402
from data_io import END, START                                       # noqa: E402
from frontier_engine import Strategy, run                            # noqa: E402
from live.config import LiveConfig                                   # noqa: E402
from live.cost_study import Books                                    # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CHOSEN = ('ADAUSDT', 'LTCUSDT', 'AVAXUSDT', 'LINKUSDT')
BASE = Strategy(name='audit', risk=.02, short_btc_bull_risk=.5, fee=0.,
                slippage=.0002, total_risk=.04)


def engine(market, symbols, **kw):
    saved = frontier_engine.SYMBOLS, frontier_engine.LONGS
    frontier_engine.SYMBOLS = list(symbols)
    frontier_engine.LONGS = [s for s in data_io.LONGS if s in symbols]
    try:
        return run(market, BASE, START, END, **kw)
    finally:
        frontier_engine.SYMBOLS, frontier_engine.LONGS = saved


def score(r):
    m = r['metrics']
    return m['cagr_pct'], m['mdd_close_pct'], m['cagr_pct'] / abs(m['mdd_close_pct'])


def rank_of(values, chosen):
    """1 = best. Ties broken pessimistically for the chosen set."""
    return 1 + sum(v > chosen for v in values)


def permutation(market, universe, books, capital):
    pool = [s for s in universe if s != 'BTCUSDT']
    sets = list(itertools.combinations(pool, len(CHOSEN)))
    print(f'\n1. 순열 검정 — {len(pool)}종목 중 {len(CHOSEN)}개를 빼는 {len(sets)}가지 조합 전부')
    print('   (4시간봉 MDD 기준. 5분봉 경로가 없는 조합이 있어 run()만 사용)\n')
    rows = []
    for i, drop in enumerate(sets):
        keep = [s for s in universe if s not in drop]
        f = score(engine(market, keep))
        b = Books(books, capital=capital)
        c = score(engine(market, keep, slippage_of=b.rate))
        rows.append({'drop': drop, 'f_cagr': f[0], 'f_mdd': f[1], 'f_cal': f[2],
                     'c_cagr': c[0], 'c_mdd': c[1], 'c_cal': c[2]})
        if (i + 1) % 21 == 0:
            print(f'   ... {i + 1}/{len(sets)}', flush=True)
    d = pd.DataFrame(rows)
    me = d[d['drop'].map(lambda x: set(x) == set(CHOSEN))].iloc[0]
    n = len(d)
    print(f"\n   {'':24s}{'선택한 조합':>12s}{'126개 중앙값':>14s}{'순위':>10s}{'상위':>8s}")
    for key, label in (('f_cagr', '일률 2bp  CAGR'), ('f_cal', '일률 2bp  Calmar'),
                       ('c_cagr', f'실측비용  CAGR'), ('c_cal', '실측비용  Calmar')):
        rk = rank_of(d[key], me[key])
        unit = '%' if 'cagr' in key else ''
        print(f"   {label:24s}{me[key]:>11.2f}{unit}{d[key].median():>13.2f}{unit}"
              f"{rk:>6d}/{n}{rk / n * 100:>7.0f}%")
    best_f = d.sort_values('f_cal', ascending=False).head(5)
    print('\n   일률 2bp에서 Calmar가 가장 좋은 조합 5개 (사후 선택이라면 이쪽을 골랐을 것):')
    for _, r in best_f.iterrows():
        print(f"      -{','.join(s.replace('USDT', '') for s in r['drop']):22s}"
              f" CAGR {r.f_cagr:6.2f}%  Calmar {r.f_cal:.3f}")
    # Does cost explain the cross-section of measured-cost results?
    rate = {s: Books(books, capital=capital).rate(s, 100_000) * 1e4 for s in pool}
    d['dropped_cost'] = d['drop'].map(lambda x: sum(rate[s] for s in x))
    d['uplift'] = d.c_cal - d.f_cal
    print(f"\n   뺀 종목들의 슬리피지 합 vs (실측비용 Calmar − 2bp Calmar) 상관: "
          f"{np.corrcoef(d.dropped_cost, d.uplift)[0, 1]:+.3f}")
    print(f"   뺀 종목들의 슬리피지 합 vs 2bp Calmar 상관:                   "
          f"{np.corrcoef(d.dropped_cost, d.f_cal)[0, 1]:+.3f}"
          '   <- 0에 가까워야 비용과 표본 성과가 독립')
    return d


def confound(market, universe, books_paths):
    print('\n2. 교란 검정 — 비싼 종목이 곧 표본에서 못 번 종목인가 (10종목, 일률 2bp)\n')
    t = engine(market, universe)['trades']
    t = t.assign(val=t.entry_ms >= SPLIT)
    b = Books(books_paths[-1])
    g = t.groupby('symbol').agg(거래=('r_multiple', 'size'), 평균R=('r_multiple', 'mean'),
                                총R=('r_multiple', 'sum'), 손익=('net_pnl', 'sum'))
    g['전반R'] = t[~t.val].groupby('symbol').r_multiple.sum()
    g['후반R'] = t[t.val].groupby('symbol').r_multiple.sum()
    g['비용bp'] = [b.rate(s, 100_000) * 1e4 for s in g.index]
    g = g.fillna(0.).sort_values('비용bp', ascending=False)
    print(f"   {'symbol':9s}{'비용@100k':>10s}{'거래':>6s}{'평균R':>8s}{'총R':>8s}"
          f"{'전반 총R':>10s}{'후반 총R':>10s}{'순손익':>12s}")
    for s, r in g.iterrows():
        mark = ' <- 제외' if s in CHOSEN else ''
        print(f"   {s:9s}{r['비용bp']:>9.2f}{int(r['거래']):>7d}{r['평균R']:>+8.3f}{r['총R']:>+8.1f}"
              f"{r['전반R']:>+10.1f}{r['후반R']:>+10.1f}{r['손익']:>+12,.0f}{mark}")
    return g


def spearman(a, b):
    ra, rb = pd.Series(a).rank().to_numpy(), pd.Series(b).rank().to_numpy()
    return float(np.corrcoef(ra, rb)[0, 1])


def main(args) -> int:
    from run_frontier import market_data
    market = market_data()
    universe = list(LiveConfig().universe)
    snaps = [ROOT / p for p in args.books]

    d = permutation(market, universe, snaps[-1], args.capital)
    d.assign(drop=d['drop'].map(','.join)).to_csv(ROOT / args.out, index=False)

    g = confound(market, universe, snaps)
    ex = g[g.index != 'BTCUSDT']
    print(f"\n   비용 순위 vs 평균R 순위 (Spearman, BTC 제외 9종목): "
          f"{spearman(ex['비용bp'], ex['평균R']):+.3f}")
    print(f"   비용 순위 vs 총R 순위:                              "
          f"{spearman(ex['비용bp'], ex['총R']):+.3f}")
    drop_r = g.loc[list(CHOSEN)]
    print(f"   제외 4종목 합계: 총R {drop_r['총R'].sum():+.1f}  순손익 {drop_r['손익'].sum():+,.0f}"
          f"  (전반 {drop_r['전반R'].sum():+.1f}R / 후반 {drop_r['후반R'].sum():+.1f}R)")

    print('\n3. 안정성 — 일률 2bp에서 6종목과 10종목의 차이가 표본 전·후반에 같은 부호인가\n')
    from equity_replay import equity_5m
    six = [s for s in universe if s not in CHOSEN]
    res = {}
    for name, uni in (('10종목', universe), ('6종목', six)):
        saved = frontier_engine.SYMBOLS, frontier_engine.LONGS
        frontier_engine.SYMBOLS = list(uni)
        frontier_engine.LONGS = [s for s in data_io.LONGS if s in uni]
        try:
            r = run(market, BASE, START, END)
            fine, _ = equity_5m(market, r)
        finally:
            frontier_engine.SYMBOLS, frontier_engine.LONGS = saved
        res[name] = (period(fine, START, SPLIT), period(fine, SPLIT, END))
    print(f"   {'':10s}{'전반 CAGR':>11s}{'전반 MDD':>10s}{'후반 CAGR':>11s}{'후반 MDD':>10s}")
    for name, (a, b) in res.items():
        print(f"   {name:10s}{a['cagr_pct']:>10.2f}%{a['mdd_pct']:>9.2f}%"
              f"{b['cagr_pct']:>10.2f}%{b['mdd_pct']:>9.2f}%")
    da = res['6종목'][0]['cagr_pct'] - res['10종목'][0]['cagr_pct']
    db = res['6종목'][1]['cagr_pct'] - res['10종목'][1]['cagr_pct']
    print(f"   6종목 − 10종목 CAGR: 전반 {da:+.2f}%p / 후반 {db:+.2f}%p")

    print('\n4. 규칙 검정 — 손익을 보지 않는 비용 기준이 두 스냅샷에서 같은 4개를 고르는가')
    print('   왕복 비용/1R = bp x 0.00856 (위험 2%, 명목 0.856배, 자산 100k)\n')
    pool = [s for s in universe if s != 'BTCUSDT']
    tabs = {}
    for p in snaps:
        b = Books(p)
        tabs[p.stem] = {s: b.rate(s, 100_000) * 1e4 * 0.00856 for s in pool}
    names = list(tabs)
    print(f"   {'symbol':9s}" + ''.join(f'{n[-8:]:>12s}' for n in names))
    for s in sorted(pool, key=lambda x: -tabs[names[-1]][x]):
        mark = ' <- 제외' if s in CHOSEN else ''
        print(f"   {s:9s}" + ''.join(f'{tabs[n][s]:>11.3f}R' for n in names) + mark)
    for thr in (0.06, 0.07, 0.08, 0.09, 0.10):
        picks = [tuple(sorted(s.replace('USDT', '') for s in pool if tabs[n][s] > thr))
                 for n in names]
        same = '동일' if len(set(picks)) == 1 else '다름'
        print(f"   기준 {thr:.2f}R 초과 제외 -> " +
              ' | '.join(','.join(p) or '(없음)' for p in picks) + f'   [{same}]')
    return 0


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--books', nargs='*', default=['results/books/lighter_20260912.json',
                                                   'results/books/lighter_20261110.json'])
    ap.add_argument('--capital', type=float, default=100_000.)
    ap.add_argument('--out', default='results_live6_20261109/selection_audit.csv')
    raise SystemExit(main(ap.parse_args()))

"""What does this strategy actually earn on Lighter once the book is charged?

Lighter's Standard account pays no fee, so on this venue the entire execution
cost is slippage. That makes one number decide the answer, and it is not a
constant: slippage grows with position size, and this strategy compounds 100k
into 812k. The median entry in the audited run happens at 202,545 of equity and
the largest at 957,263 — so a rate measured once at 100k describes almost none
of the sample.

Three cost models, run end to end on the traded universe (BCH excluded):

  FLAT      the audited 2bp assumption, for reference
  FIXED     measured from the real book at one equity level, applied throughout
  SCALED    re-measured from the real book at the size the account can take at
            that moment — the honest one for a compounding account

    python src/live/cost_study.py --books /path/snap.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import data_io                                                       # noqa: E402
import frontier_engine                                               # noqa: E402
from analyze_results import SPLIT, period                            # noqa: E402
from data_io import END, START                                       # noqa: E402
from equity_replay import equity_5m                                  # noqa: E402
from frontier_engine import Strategy, run                            # noqa: E402
from live.config import LiveConfig                                   # noqa: E402

# Median position notional as a multiple of equity at entry, measured over the
# audited 427 trades (mean 0.954, p90 1.544). The median is the honest central
# case; --multiple re-runs the whole study at another point of that spread.
NOTIONAL_MULTIPLE = 0.856


class Books:
    """Measured slippage from a real depth snapshot, cached by size.

    `scale` is how the engine's fixed 100k start maps onto a real account. Apart
    from slippage this strategy is scale-free — every size is a fraction of
    equity — so running it on 25k is the same run with every book walk taken at a
    quarter of the size. That makes starting capital answerable without touching
    the engine, and it is the question that decides whether the thing is worth
    running at all.
    """

    ENGINE_START = 100_000.

    def __init__(self, path: Path, multiple: float = NOTIONAL_MULTIPLE,
                 capital: float = ENGINE_START):
        snap = json.loads(Path(path).read_text())
        self.price = snap['price']
        self.book = snap['book']
        self.multiple = multiple
        self.scale = capital / self.ENGINE_START
        self.exhausted: dict[str, int] = {}
        self._cache: dict[tuple, float] = {}

    def _walk(self, levels: list, qty: float) -> tuple[float, bool]:
        cost = filled = 0.
        for price, size in levels:
            if filled >= qty:
                break
            take = min(qty - filled, size)
            cost += take * price
            filled += take
        if filled <= 0:
            return 0., True
        return cost / filled, filled < qty * (1 - 1e-9)

    def rate(self, symbol: str, equity: float) -> float:
        """Slippage per side as a fraction, both directions averaged.

        A round trip pays one sell and one buy whichever way the trade goes, so
        charging only the sell side would price half the trade.
        """
        equity = equity * self.scale
        key = (symbol, round(equity / 1000))          # 1k buckets, plenty fine
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        price, book = self.price.get(symbol), self.book.get(symbol)
        if not price or not book:
            return 0.0002
        qty = equity * self.multiple / price
        bids, asks = book['bids'], book['asks']
        if not bids or not asks:
            return 0.0002
        mid = (bids[0][0] + asks[0][0]) / 2
        sell_vwap, sell_out = self._walk(bids, qty)
        buy_vwap, buy_out = self._walk(asks, qty)
        if sell_out or buy_out:
            self.exhausted[symbol] = self.exhausted.get(symbol, 0) + 1
        sell = max((mid - sell_vwap) / mid, 0.)
        buy = max((buy_vwap - mid) / mid, 0.)
        rate = (sell + buy) / 2
        self._cache[key] = rate
        return rate


def run_universe(market, symbols, base, **kw) -> dict:
    """Run the engine over a subset. BTC stays in market.rows either way — the
    regime filter reads it directly, so excluding BTC from TRADING would not
    remove the SMA200 signal the other symbols depend on."""
    saved_symbols, saved_longs = frontier_engine.SYMBOLS, frontier_engine.LONGS
    try:
        frontier_engine.SYMBOLS = list(symbols)
        frontier_engine.LONGS = [s for s in data_io.LONGS if s in symbols]
        r = run(market, base, START, END, **kw)
        fine, dd5 = equity_5m(market, r)
    finally:
        frontier_engine.SYMBOLS, frontier_engine.LONGS = saved_symbols, saved_longs
    m = r['metrics']
    return {'cagr': m['cagr_pct'], 'mdd4h': m['mdd_close_pct'], 'mdd5m': dd5,
            'final': m['final_equity'], 'trades': m['trades'],
            'pf': m['profit_factor'], 'funding': m['funding_pnl'],
            'calmar': m['cagr_pct'] / abs(dd5), 'fine': fine}


MAJORS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'BNBUSDT')

# Thinnest book first. Lighter's ten split cleanly into five markets under $1.5M
# of visible bid depth and five over $7.5M — a five-fold gap with nothing in it,
# so the interesting cuts are inside the thin layer.
BY_DEPTH = ('ADAUSDT', 'LTCUSDT', 'DOGEUSDT', 'LINKUSDT', 'AVAXUSDT')


def ladder(market, universe, base, books_for, caps) -> None:
    """Every plausible exclusion set, priced at every capital.

    Each row also carries what the same cut is worth at a FLAT 2bp, which is the
    only way to tell a capacity decision from hindsight: if a cut helps when cost
    is held equal, that part of it is knowing which symbols did well.
    """
    sets = {
        '10종목 (현재 기본)': list(universe),
        '9종목 -LTC': [s for s in universe if s != 'LTCUSDT'],
        '7종목 -LTC,AVAX,LINK': [s for s in universe
                                 if s not in ('LTCUSDT', 'AVAXUSDT', 'LINKUSDT')],
        '6종목 +ADA 제외': [s for s in universe
                          if s not in ('LTCUSDT', 'AVAXUSDT', 'LINKUSDT', 'ADAUSDT')],
        '5종목 (메이저만)': [s for s in universe if s in MAJORS],
    }
    flat = {n: run_universe(market, u, base) for n, u in sets.items()}
    b0 = flat['10종목 (현재 기본)']['calmar']

    print('\n대조군 (일률 2bp) — 비용이 같을 때도 좋아지면 그만큼은 사후 선택이다')
    print(f"\n{'유니버스':22s}{'CAGR':>9s}{'MDD':>10s}{'Calmar':>9s}"
          f"{'거래':>7s}{'선택성분':>10s}")
    print('-' * 68)
    for n in sets:
        r = flat[n]
        print(f"{n:22s}{r['cagr']:>8.2f}%{r['mdd5m']:>9.2f}%{r['calmar']:>9.3f}"
              f"{r['trades']:>7d}{r['calmar'] - b0:>+10.3f}")

    grid = {}
    for n, uni in sets.items():
        for cap in caps:
            b = books_for(cap)
            grid[n, cap] = (run_universe(market, uni, base, slippage_of=b.rate),
                            bool(b.exhausted))

    for key, label, fmt in (('cagr', 'CAGR', '{:>+9.2f}'),
                            ('mdd5m', 'MDD(5분)', '{:>9.2f}'),
                            ('calmar', 'Calmar', '{:>9.2f}')):
        print(f'\n{label} — 자산연동 실측 슬리피지\n')
        print(f"{'유니버스':22s}" + ''.join(f'{c // 1000:>10.0f}k' for c in caps))
        print('-' * (22 + 11 * len(caps)))
        for n in sets:
            cells = ''.join(fmt.format(grid[n, c][0][key]) + ('*' if grid[n, c][1] else ' ')
                            for c in caps)
            print(f'{n:22s}{cells}')
    print('\n* = 보이는 호가가 바닥난 구간이 있어 실제는 더 나쁨 (하한값)')
    print('\n정적 bp표는 시작 자본 크기로 잰 값이라 가장 얇은 종목을 과소평가한다.')
    print('실행 중 자산이 불어나 포지션은 그보다 훨씬 커진다 — 시작 400k에서도')
    print('포지션 명목 중앙값이 68만 달러, 최대 365만 달러다.')


def diagnose(market, universe, base, books_for, caps) -> None:
    """Why large capital breaks this strategy, in the two ways it actually breaks.

    Worth separating, because they call for different fixes: a return drag is
    priced and survivable, a broken risk unit is not.
    """
    import data_io
    import frontier_engine
    from frontier_engine import run

    def raw(symbols, **kw):
        saved = frontier_engine.SYMBOLS, frontier_engine.LONGS
        frontier_engine.SYMBOLS = list(symbols)
        frontier_engine.LONGS = [s for s in data_io.LONGS if s in symbols]
        try:
            return run(market, base, START, END, **kw)
        finally:
            frontier_engine.SYMBOLS, frontier_engine.LONGS = saved

    # ---- 1. who carries the notional, and whose book cannot carry it ----
    b0 = books_for(100_000)
    tr = raw(universe, slippage_of=b0.rate)['trades']
    tr = tr.assign(notional=tr.initial_qty * tr.entry_price)
    w = tr.groupby('symbol').notional.sum()
    w = w / w.sum()
    lo, hi = 100_000, 800_000
    rows = [(s, w.get(s, 0.), b0.rate(s, lo) * 1e4, b0.rate(s, hi) * 1e4)
            for s in universe]
    inc = sum(x[1] * (x[3] - x[2]) for x in rows)

    print('\n1. 자본이 커질 때 추가 비용을 누가 내는가')
    print(f"\n{'symbol':9s}{'명목비중':>10s}{'@100k':>9s}{'@800k':>9s}"
          f"{'배율':>7s}{'증가분 점유':>13s}")
    print('-' * 60)
    for s, ws, a, z in sorted(rows, key=lambda x: -(x[1] * (x[3] - x[2]))):
        print(f'{s:9s}{ws * 100:>9.1f}%{a:>9.2f}{z:>9.2f}{z / a:>6.1f}x'
              f'{ws * (z - a) / inc * 100:>12.1f}%')

    # ---- 2. is it capacity, or is it hindsight? ----
    flat_all = run_universe(market, universe, base)
    flat_maj = run_universe(market, list(MAJORS), base)
    selection = flat_maj['calmar'] - flat_all['calmar']

    print('\n2. 얇은 알트를 빼면 대자본에서 살아나는가 — 그리고 그것은 비용인가')
    print('   대조군은 일률 2bp. 비용이 같은데도 좋아지는 만큼은 사후 선택이다.')
    print(f"\n   일률 2bp   전체 C/M {flat_all['calmar']:.3f}   "
          f"메이저 C/M {flat_maj['calmar']:.3f}   차이 {selection:+.3f}")
    print(f"\n{'시작자본':>10s}{'전체 CAGR':>11s}{'메이저 CAGR':>13s}"
          f"{'전체 C/M':>10s}{'메이저 C/M':>11s}{'비용 성분':>11s}")
    print('-' * 68)
    for cap in caps:
        a = run_universe(market, universe, base, slippage_of=books_for(cap).rate)
        m = run_universe(market, list(MAJORS), base, slippage_of=books_for(cap).rate)
        print(f"{cap:>10,}{a['cagr']:>10.2f}%{m['cagr']:>12.2f}%"
              f"{a['calmar']:>10.3f}{m['calmar']:>11.3f}"
              f"{m['calmar'] - a['calmar'] - selection:>+11.3f}")

    # ---- 3. does the risk unit survive? ----
    print('\n3. 위험모형이 버티는가 — 손절은 설계상 -1R이다')
    print(f"\n{'비용 모델':18s}{'평균 손실R':>12s}{'최악 R':>9s}"
          f"{'-1R 초과':>10s}{'평균 이익R':>12s}{'승/패':>8s}")
    print('-' * 70)
    for label, cap in [('일률 2bp', None)] + [(f'@{c // 1000:,.0f}k', c) for c in caps]:
        kw = {} if cap is None else {'slippage_of': books_for(cap).rate}
        t = raw(universe, **kw)['trades']
        lose, win = t[t.r_multiple < 0].r_multiple, t[t.r_multiple > 0].r_multiple
        print(f'{label:18s}{lose.mean():>12.3f}{lose.min():>9.2f}'
              f'{(lose < -1).mean() * 100:>9.0f}%{win.mean():>12.3f}'
              f'{abs(win.mean() / lose.mean()):>8.3f}')
    print('\n손실만 커지고 이익은 그대로다. 이익은 진입에서 멀리 떨어진 추적청산까지')
    print('달리므로 비용이 작은 비중이지만, 손실은 정확히 1R 자리에서 잘리기 때문이다.')
    print('그래서 낙폭이 수익 감소보다 빠르게 나빠진다 — 위험단위 자체가 무너진 것이다.')


def main(args) -> int:
    from run_frontier import market_data

    books = Books(args.books, args.multiple, args.capital)
    universe = list(LiveConfig().universe)
    excluded = [s for s in data_io.SYMBOLS if s not in universe]
    base = Strategy(name='lighter_cost', risk=.02, short_btc_bull_risk=.5,
                    fee=0., slippage=.0002, total_risk=.04)
    market = market_data()

    print(f'Lighter · 수수료 0 · 거래 {len(universe)}종목 (제외: {", ".join(excluded)})')
    print(f'시작 자본 {args.capital:,.0f} (엔진은 100k로 돌고 호가 소진 크기만 '
          f'{books.scale:.2f}배로 맞춘다)')
    print(f'포지션 명목 = 자산의 {args.multiple:.3f}배 (감사된 427건의 중앙값)')
    print(f'슬리피지는 호가 스냅샷을 실제로 소진시켜 측정, 매수·매도 평균\n')

    print(f"{'비용 모델':28s}{'CAGR':>9s}{'MDD(5분)':>11s}{'MDD(4시간)':>12s}"
          f"{'최종자산':>13s}{'Calmar':>9s}{'거래':>7s}")
    print('-' * 89)

    rows = []

    flat = run_universe(market, universe, base)
    rows.append(('A. 일률 2bp (백테스트 가정)', flat))

    for eq in args.fixed:
        rate = {s: books.rate(s, eq) for s in universe}
        r = run_universe(market, universe, base, slippage_by_symbol=rate)
        avg = sum(rate.values()) / len(rate) * 1e4
        rows.append((f'B. 고정 실측 @{eq // 1000:.0f}k (평균 {avg:.1f}bp)', r))

    scaled = run_universe(market, universe, base, slippage_of=books.rate)
    rows.append(('C. 자산연동 실측', scaled))

    for label, r in rows:
        print(f"{label:28s}{r['cagr']:>8.2f}%{r['mdd5m']:>10.2f}%{r['mdd4h']:>11.2f}%"
              f"{r['final']:>13,.0f}{r['calmar']:>9.3f}{r['trades']:>7d}")

    print(f"\n펀딩 {flat['funding']:+,.0f} (Binance 실측). 거래 수가 모델마다 다른 것은"
          " 비용이 자산을 바꾸고,")
    print('자산이 위험한도 소진 속도를 바꿔 진입이 잘리는 지점이 달라지기 때문이다.')

    print('\n자산 구간별 실측 슬리피지 (편도, bp)')
    print(f"\n{'symbol':9s}" + ''.join(f'{e // 1000:>8.0f}k' for e in args.curve))
    for s in universe:
        print(f'{s:9s}' + ''.join(f'{books.rate(s, e) * 1e4:>9.2f}' for e in args.curve))

    if books.exhausted:
        print('\n⚠ 보이는 호가가 바닥난 종목·구간이 있습니다. 그 구간의 비용은')
        print('  아래 값보다 나쁘며, 여기 숫자는 하한입니다:')
        for s, n in sorted(books.exhausted.items(), key=lambda kv: -kv[1]):
            print(f'   {s:9s} {n}회')

    print('\n구간별 — 이 수익이 한 국면에 몰려 있는가')
    print(f"\n{'비용 모델':28s}{'개발구간 CAGR':>15s}{'검증구간 CAGR':>15s}{'검증 MDD':>11s}")
    for label, r in rows:
        dev = period(r['fine'], START, SPLIT)
        val = period(r['fine'], SPLIT, END)
        print(f"{label:28s}{dev['cagr_pct']:>14.2f}%{val['cagr_pct']:>14.2f}%"
              f"{val['mdd_pct']:>10.2f}%")

    # The curve carries time_ms, not a datetime index, and it has ~590k rows —
    # bucket it with one vectorised groupby rather than a scan per year.
    import pandas as pd

    def yearly(fine):
        year = pd.to_datetime(fine.time_ms, unit='ms', utc=True).dt.year
        last = fine.equity.groupby(year).last()
        opening = pd.concat([pd.Series([100000.]), last]).iloc[:-1]
        return (last.to_numpy() / opening.to_numpy() - 1) * 100

    print('\n연도별 수익률')
    years = sorted(pd.to_datetime(rows[0][1]['fine'].time_ms, unit='ms',
                                  utc=True).dt.year.unique())
    print(f"\n{'비용 모델':28s}" + ''.join(f'{y:>9d}' for y in years))
    for label, r in rows:
        print(f'{label:28s}' + ''.join(f'{v:>+8.1f}%' for v in yearly(r['fine'])))

    a, c = rows[0][1], rows[-1][1]
    print(f'\n2bp 가정 대비 자산연동 실측: CAGR {a["cagr"]:.2f}% → {c["cagr"]:.2f}% '
          f'({c["cagr"] - a["cagr"]:+.2f}%p), MDD {a["mdd5m"]:.2f}% → {c["mdd5m"]:.2f}%')
    print(f'최종자산 {a["final"]:,.0f} → {c["final"]:,.0f} '
          f'({c["final"] / a["final"] - 1:+.1%})')

    if args.ladder:
        ladder(market, universe, base,
               lambda c: Books(args.books, args.multiple, c), args.capitals)

    if args.diagnose:
        diagnose(market, universe, base,
                 lambda c: Books(args.books, args.multiple, c), args.capitals)

    print('\n한계: 호가 스냅샷 1회다. 시간대·변동성에 따라 달라진다. 자기 주문이')
    print('남기는 가격 충격과 4시간봉 사이의 가격 변화는 포함하지 않았다.')
    print('펀딩은 Binance 실측이며 Lighter의 시간당 펀딩과 다르다.')
    return 0


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--books', required=True)
    ap.add_argument('--multiple', type=float, default=NOTIONAL_MULTIPLE)
    ap.add_argument('--capital', type=float, default=Books.ENGINE_START,
                    help='real starting capital; the engine always starts at '
                         '100k, this scales every book walk to match')
    ap.add_argument('--fixed', type=float, nargs='*',
                    default=[100_000., 200_000.])
    ap.add_argument('--ladder', action='store_true',
                    help='every plausible exclusion set at every capital, with '
                         'the flat-cost control that separates capacity from '
                         'hindsight')
    ap.add_argument('--diagnose', action='store_true',
                    help='why large capital breaks it: cost attribution, the '
                         'thin-alt counterfactual with a selection control, and '
                         'whether the 1R risk unit survives')
    ap.add_argument('--capitals', type=float, nargs='*',
                    default=[100_000., 400_000., 1_000_000., 2_000_000.])
    ap.add_argument('--curve', type=float, nargs='*',
                    default=[100_000., 200_000., 400_000., 800_000.])
    raise SystemExit(main(ap.parse_args()))

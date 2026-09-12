"""Which venue is actually cheapest for THIS strategy: fee plus slippage.

Zero fees only help if the book is deep enough to use them. Lighter charges
nothing and Hyperliquid charges 4.5bp taker, so Hyperliquid has to be more than
4.5bp better on depth before it wins. This measures both at the sizes the
strategy really trades, per symbol, and adds the fee.

Binance cannot be measured from every location — fapi.binance.com answers 451
from restricted regions. Where that happens the report still bounds it: Binance
pays 5bp taker per side before any slippage at all, so any venue filling inside
5bp beats Binance no matter how deep Binance is.

    python src/live/venue_compare.py --equity 100000
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live.book import walk                                          # noqa: E402
from live.config import MAINNET, SYMBOL_TO_LIGHTER                  # noqa: E402
from live.markets import load_markets, marks                        # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
TRADES = ROOT / 'results/cases/zero_fee_cap4_risk200/trades.csv'

HYPERLIQUID = 'https://api.hyperliquid.xyz/info'
BINANCE_FAPI = 'https://fapi.binance.com/fapi/v1/depth'

# Taker fee per side, basis points. Verified 2026-09-12:
#   Lighter      Standard account, 0 maker / 0 taker (docs + orderBooks payload)
#   Hyperliquid  base tier 0.045% taker, 0.015% maker rebate (docs)
#   Binance      0.05% taker, the rate the backtest's binance case already used
TAKER_BPS = {'lighter': 0.0, 'hyperliquid': 4.5, 'binance': 5.0}


def _post(url: str, payload: dict, timeout: int = 25) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _get(url: str, timeout: int = 25) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def _hl_raw(coin: str, sig_figs: int | None) -> dict | None:
    payload = {'type': 'l2Book', 'coin': coin}
    if sig_figs is not None:
        payload['nSigFigs'] = sig_figs
    try:
        d = _post(HYPERLIQUID, payload)
    except Exception:
        return None
    levels = d.get('levels') or []
    if len(levels) < 2:
        return None
    def side(rows):
        return [{'price': r['px'], 'remaining_base_amount': r['sz']} for r in rows]
    return {'bids': side(levels[0]), 'asks': side(levels[1])}


def hyperliquid_book(coin: str, need: float = 0.) -> tuple[dict | None, float]:
    """L2 book deep enough to fill `need`, plus the price granularity it cost.

    l2Book returns only 20 levels per side, which on a thin alt is far less
    notional than Lighter's 250 individual orders — comparing those directly
    would flatter Hyperliquid by measuring a partial fill. nSigFigs aggregates
    prices into wider buckets, so 20 levels reach much deeper.

    The trade is precision: inside a bucket every order is priced at the bucket,
    so an aggregated walk is slightly OPTIMISTIC. Returns the granularity in bps
    so that bias can be bounded rather than ignored.
    """
    best = None
    for sig in (None, 4, 3):
        book = _hl_raw(coin, sig)
        if not book or not book.get('bids'):
            continue
        best = book
        depth = sum(float(r['price']) * float(r['remaining_base_amount'])
                    for r in book['bids'])
        prices = [float(r['price']) for r in book['bids']]
        gaps = [abs(a - b) for a, b in zip(prices, prices[1:])]
        granularity = (min(gaps) / prices[0] * 1e4) if gaps else 0.
        if depth >= need:
            return book, granularity
    return best, float('nan')


def binance_book(symbol: str) -> dict | None:
    try:
        d = _get(f'{BINANCE_FAPI}?symbol={symbol}&limit=1000')
    except urllib.error.HTTPError:
        return None                      # 451 from a restricted region, or symbol gone
    except Exception:
        return None
    return {'bids': [{'price': p, 'remaining_base_amount': q} for p, q in d.get('bids', [])],
            'asks': [{'price': p, 'remaining_base_amount': q} for p, q in d.get('asks', [])]}


def lighter_book(market_id: int) -> dict | None:
    from live.book import fetch_depth
    try:
        return fetch_depth(MAINNET, market_id)
    except Exception:
        return None


def cost_bps(book: dict | None, qty: float, venue: str) -> tuple[float | None, bool]:
    """(fee + slippage vs mid, ran out of visible depth). Sell side — the
    strategy is short-heavy and sells to open far more often than it buys."""
    if not book or not book.get('bids') or not book.get('asks'):
        return None, False
    mid = (float(book['bids'][0]['price']) + float(book['asks'][0]['price'])) / 2
    q = walk(book, qty, buying=False)
    if q.filled <= 0:
        return None, True
    return q.slippage_bps(mid, False) + TAKER_BPS[venue], q.exhausted


def notional_share() -> dict[str, float]:
    share, total = {}, 0.
    with TRADES.open() as fh:
        for row in csv.DictReader(fh):
            n = float(row['initial_qty']) * float(row['entry_price'])
            share[row['symbol']] = share.get(row['symbol'], 0.) + n
            total += n
    return {k: v / total for k, v in share.items()}


def main(equity: float, multiple: float = 0.86) -> int:
    markets = load_markets(MAINNET, SYMBOL_TO_LIGHTER)
    px = marks(MAINNET, markets)
    share = notional_share()

    print(f'venue comparison   equity {equity:,.0f}   position {multiple:.2f}x '
          f'= {equity * multiple:,.0f} USDT notional   sell side')
    print('cost = slippage vs mid + taker fee, basis points per side\n')
    print(f"{'symbol':9s}{'weight':>8s}{'lighter':>10s}{'hyperlq':>10s}{'binance':>10s}"
          f"{'  cheapest':>12s}")

    totals = {'lighter': 0., 'hyperliquid': 0., 'binance': 0.}
    covered = {'lighter': 0., 'hyperliquid': 0., 'binance': 0.}
    missing: dict[str, list[str]] = {'hyperliquid': [], 'binance': []}
    grain: dict[str, float] = {}

    for symbol, market in markets.items():
        price = px.get(symbol)
        if not price:
            continue
        qty = market.round_size(equity * multiple / price)
        coin = SYMBOL_TO_LIGHTER[symbol]
        w = share.get(symbol, 0.)

        need = equity * multiple
        hl_book, hl_grain = hyperliquid_book(coin, need)
        results = {}
        results['lighter'], le = cost_bps(lighter_book(market.market_id), qty, 'lighter')
        results['hyperliquid'], he = cost_bps(hl_book, qty, 'hyperliquid')
        results['binance'], be = cost_bps(binance_book(symbol), qty, 'binance')
        grain[symbol] = hl_grain
        for venue in ('hyperliquid', 'binance'):
            if results[venue] is None:
                missing[venue].append(symbol)

        for venue, value in results.items():
            if value is not None:
                totals[venue] += w * value
                covered[venue] += w

        priced = {k: v for k, v in results.items() if v is not None}
        best = min(priced, key=priced.get) if priced else '-'

        def cell(v, exhausted):
            if v is None:
                return '     n/a'
            return f'{v:>8.2f}{"*" if exhausted else " "}'

        print(f'{symbol:9s}{w * 100:>7.1f}%{cell(results["lighter"], le)}'
              f'{cell(results["hyperliquid"], he)}{cell(results["binance"], be)}'
              f'{best:>12s}')

    print('\n* = the visible book ran out before the size was filled')
    coarse = {k: v for k, v in grain.items() if v == v and v > 0.5}
    if coarse:
        print('\nHyperliquid needed aggregated levels to show enough depth. Inside a '
              'bucket every order')
        print('is priced at the bucket, so those figures are OPTIMISTIC by up to the '
              'granularity below:')
        for symbol, g in sorted(coarse.items(), key=lambda kv: -kv[1]):
            print(f'   {symbol:9s} up to {g:5.2f}bp understated')
    print()
    print(f"{'venue':14s}{'weighted cost':>16s}{'coverage':>11s}{'fee':>8s}")
    for venue in ('lighter', 'hyperliquid', 'binance'):
        if covered[venue] <= 0:
            print(f'{venue:14s}{"not measurable here":>16s}{"0%":>11s}'
                  f'{TAKER_BPS[venue]:>7.1f}bp')
            continue
        # Renormalise over the symbols that priced, so partial coverage is not
        # silently read as a cheaper venue.
        print(f'{venue:14s}{totals[venue] / covered[venue]:>14.2f}bp'
              f'{covered[venue] * 100:>10.0f}%{TAKER_BPS[venue]:>7.1f}bp')

    for venue, symbols in missing.items():
        if symbols:
            print(f'\n{venue}: no book for {", ".join(symbols)}')
    if len(missing['binance']) == len(markets):
        print('\nBinance returned nothing from this location (451 in restricted '
              'regions), so its depth is unmeasured here.')
        print('Bound that still holds: Binance pays 5.0bp taker per side before any '
              'slippage, so a venue')
        print('filling inside 5.0bp is cheaper than Binance can possibly be.')
    return 0


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--equity', type=float, default=100_000.)
    ap.add_argument('--multiple', type=float, default=0.86)
    a = ap.parse_args()
    raise SystemExit(main(a.equity, a.multiple))

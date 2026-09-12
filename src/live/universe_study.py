"""Does dropping the expensive symbols improve the strategy?

Not obviously. A symbol that costs a lot to trade may still earn more than it
costs, and removing it frees risk budget that the remaining symbols take up —
the backtest skipped hundreds of signals on caps, so the book reshuffles rather
than simply shrinking. Both effects need the engine, not arithmetic.

Each universe is re-run end to end with per-symbol slippage measured from real
book depth, so a symbol is judged at the price it actually costs.

    python src/live/universe_study.py --equity 100000
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import data_io                                                      # noqa: E402
import frontier_engine                                              # noqa: E402
from data_io import END, START                                      # noqa: E402
from frontier_engine import Strategy, run                           # noqa: E402
from live.book import fetch_depth, walk                             # noqa: E402
from live.config import MAINNET, SYMBOL_TO_LIGHTER                  # noqa: E402
from live.markets import load_markets, marks                        # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
TRADES = ROOT / 'results/cases/zero_fee_cap4_risk200/trades.csv'
ALL = list(SYMBOL_TO_LIGHTER)


def measure_slippage(equity: float, multiple: float = 0.86) -> dict[str, float]:
    """Per-symbol cost of the strategy's own position size, as a fraction."""
    markets = load_markets(MAINNET, SYMBOL_TO_LIGHTER)
    px = marks(MAINNET, markets)
    out = {}
    for symbol, market in markets.items():
        price = px.get(symbol)
        if not price:
            continue
        depth = fetch_depth(MAINNET, market.market_id)
        bids, asks = depth.get('bids') or [], depth.get('asks') or []
        if not bids or not asks:
            continue
        mid = (float(bids[0]['price']) + float(asks[0]['price'])) / 2
        q = walk(depth, market.round_size(equity * multiple / price), buying=False)
        out[symbol] = max(q.slippage_bps(mid, False), 0.) / 1e4
    return out


def per_symbol_pnl() -> dict[str, dict]:
    rows: dict[str, dict] = {}
    with TRADES.open() as fh:
        for r in csv.DictReader(fh):
            s = rows.setdefault(r['symbol'], {'trades': 0, 'net': 0., 'notional': 0.})
            s['trades'] += 1
            s['net'] += float(r['net_pnl'])
            s['notional'] += float(r['initial_qty']) * float(r['entry_price'])
    return rows


def run_universe(market, symbols: list[str], slippage: dict[str, float],
                 base: Strategy) -> dict:
    """Run the engine over a subset. BTC stays in market.rows either way: the
    regime filter reads it directly, so excluding BTC from TRADING does not
    remove the SMA200 signal the other symbols depend on."""
    saved_symbols, saved_longs = frontier_engine.SYMBOLS, frontier_engine.LONGS
    try:
        frontier_engine.SYMBOLS = list(symbols)
        frontier_engine.LONGS = [s for s in data_io.LONGS if s in symbols]
        r = run(market, base, START, END, slippage_by_symbol=slippage)
    finally:
        frontier_engine.SYMBOLS, frontier_engine.LONGS = saved_symbols, saved_longs
    m = r['metrics']
    return {'cagr': m['cagr_pct'], 'mdd': m['mdd_close_pct'], 'pf': m['profit_factor'],
            'trades': m['trades'], 'final': m['final_equity'],
            'calmar': m['cagr_pct'] / abs(m['mdd_close_pct']) if m['mdd_close_pct'] else None}


def main(equity: float) -> int:
    from run_frontier import market_data

    slip = measure_slippage(equity)
    pnl = per_symbol_pnl()
    base = Strategy(name='universe', risk=.02, short_btc_bull_risk=.5,
                    fee=0., slippage=.0002, total_risk=.04)
    market = market_data()

    print(f'측정 자산 {equity:,.0f}   포지션 0.86배   수수료 0 (Lighter)')
    print('\n종목별 실측 슬리피지와 백테스트 기여도 (2bp 가정 하의 손익)')
    print(f"\n{'symbol':10s}{'slip bps':>10s}{'trades':>8s}{'net pnl':>13s}"
          f"{'명목금액':>14s}{'실측비용 추정':>15s}{'비용 후':>13s}")
    ranked = []
    for s in ALL:
        bps = slip.get(s, 0.) * 1e4
        d = pnl.get(s, {'trades': 0, 'net': 0., 'notional': 0.})
        # Round trip at the measured rate, minus what 2bp already charged.
        extra = d['notional'] * 2 * (bps - 2.0) / 1e4
        after = d['net'] - extra
        ranked.append((bps, s, d, extra, after))
        print(f"{s:10s}{bps:>10.2f}{d['trades']:>8d}{d['net']:>+13,.0f}"
              f"{d['notional']:>14,.0f}{-extra:>+15,.0f}{after:>+13,.0f}")

    ranked.sort(reverse=True)

    # A forward-looking criterion, independent of what each symbol earned in the
    # sample: how much of the 2% risk budget a round trip hands to the book.
    #   cost/R = 2 * bps/1e4 * notional_multiple / risk_per_trade
    print('\n비용을 위험예산 기준으로 본 것 — 왕복 슬리피지가 1R의 몇 %인가')
    print('(백테스트 손익과 무관한 선행 지표. 종목 선택은 이 기준으로만 해야 한다)')
    print(f"\n{'symbol':10s}{'slip bps':>10s}{'왕복 비용/1R':>14s}")
    for bps, symbol, _, _, _ in ranked:
        print(f'{symbol:10s}{bps:>10.2f}{bps * 0.0086:>13.3f}R')

    print('\n슬리피지 높은 순으로 제외하며 두 비용 가정에서 각각 재실행')
    print('  A = 실측 종목별 슬리피지   B = 전 종목 일률 2bp(원래 가정)')
    print('  B에서의 개선은 비용이 아니라 사후 종목 선택 효과 = 과최적화')
    print(f"\n{'universe':32s}{'A CAGR':>9s}{'A C/M':>8s}{'B CAGR':>9s}{'B C/M':>8s}{'비용손실':>10s}")

    rows = []
    for k in range(len(ranked) + 1):
        drop = [symbol for _, symbol, _, _, _ in ranked[:k]]
        keep = [s for s in ALL if s not in drop]
        if len(keep) < 4:
            break
        a = run_universe(market, keep, slip, base)
        b = run_universe(market, keep, None, base)
        label = '전체 11종목' if not drop else '-' + ','.join(
            x.replace('USDT', '') for x in drop)
        drag = b['calmar'] - a['calmar']
        rows.append((label, a, b, drag))
        print(f"{label:32s}{a['cagr']:>8.2f}%{a['calmar']:>8.3f}"
              f"{b['cagr']:>8.2f}%{b['calmar']:>8.3f}{drag:>10.3f}")

    full_a, full_b = rows[0][1], rows[0][2]
    best = max(rows, key=lambda r: r[1]['calmar'])
    label, a, b, drag = best
    total = a['calmar'] - full_a['calmar']
    selection = b['calmar'] - full_b['calmar']
    cost = (full_b['calmar'] - full_a['calmar']) - drag
    print(f'\n실측 기준 최선: {label}   CAGR {a["cagr"]:.2f}%  MDD {a["mdd"]:.2f}%  '
          f'C/M {a["calmar"]:.3f}')
    print(f'전체 대비 C/M 개선 {total:+.3f} 의 분해')
    print(f'   비용 절감 (정당)        {cost:+.3f}  ({cost / total * 100:.0f}%)')
    print(f'   사후 종목 선택 (과최적화) {selection:+.3f}  ({selection / total * 100:.0f}%)')
    print('\n비용 절감분만 표본 밖에서 기대할 수 있다. 나머지는 이 표본에서')
    print('어떤 종목이 잘됐는지를 안 상태로 고른 결과이므로 재현을 기대하면 안 된다.')
    print('\n슬리피지는 호가 스냅샷 1회, 손익은 2021-01~2026-08 한 표본이다.')
    return 0


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--equity', type=float, default=100_000.)
    raise SystemExit(main(ap.parse_args().equity))

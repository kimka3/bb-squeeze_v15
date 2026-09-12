"""CLI. Defaults to shadow mode; live requires an explicit flag and a key.

    python src/live/run.py --once                  decide on the latest closed bar, no orders
    python src/live/run.py --loop                  keep running on the 4h boundary
    python src/live/run.py --mode paper --loop     live data, simulated fills (stage 2)
    python src/live/run.py --mode paper --report   what paper mode has measured so far
    python src/live/run.py --mode live --loop      real orders (needs LIGHTER_API_PRIVATE_KEY)

Paper mode runs two clocks, like production: decisions on the 4h boundary, and a
fast poll in between that checks resting stops against the MARK price — the
trigger basis Lighter actually uses, which the backtest does not model.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data_io import H4, SYMBOLS                                    # noqa: E402
from live.broker import DryRunBroker, LighterBroker                # noqa: E402
from live.paper import PaperBroker                                 # noqa: E402
from live.config import LiveConfig, SYMBOL_TO_LIGHTER              # noqa: E402
from live.journal import Journal                                   # noqa: E402
from live.markets import load_markets, marks                       # noqa: E402
from live.trader import Trader                                     # noqa: E402
from live import feed                                              # noqa: E402


def build(config: LiveConfig):
    markets = load_markets(config.base_url, config.lighter_universe)
    journal = Journal(config.state_dir)
    if config.mode == 'live':
        broker = LighterBroker(config.base_url, config.account_index,
                               config.private_key, config.api_key_index,
                               stop_limit_pct=config.max_slippage_pct,
                               passive_take_profit=config.passive_take_profit)
    elif config.mode == 'paper':
        broker = PaperBroker(config.base_url, markets,
                             config.state_dir / 'paper.json',
                             breakeven_on_fill=config.breakeven_on_fill,
                             passive_take_profit=config.passive_take_profit)
        broker.refresh_marks()
    else:
        broker = DryRunBroker()
    return Trader(config, broker, markets, journal), markets


def poll_until(trader, deadline: float, config) -> None:
    """Fast loop between 4h boundaries: let resting stops fire on the mark.

    Only paper mode needs this. In live the exchange does it; in shadow there are
    no positions to protect.
    """
    broker = trader.broker
    if not isinstance(broker, PaperBroker):
        time.sleep(max(1.0, deadline - time.time()))
        return
    while time.time() < deadline:
        try:
            for fill in broker.poll():
                print(f'   FILL  {fill.symbol:9s} {fill.role:5s} qty {fill.qty:.6f} '
                      f'@ {fill.price:.6f}  mark {fill.mark:.6f}  '
                      f'slip {fill.slippage_bps:+.2f}bp')
                trader.journal.append('paper_fill', **{k: v for k, v in
                                                       fill.__dict__.items()})
        except Exception as exc:
            trader.journal.append('poll_error', error=str(exc))
        time.sleep(max(5.0, min(config.poll_seconds, deadline - time.time())))


def report(trader) -> None:
    broker = trader.broker
    if not isinstance(broker, PaperBroker):
        print('report is only meaningful in paper mode')
        return
    m = broker.measurements()
    print(f"equity {m['equity']:,.2f}   cash {m['cash']:,.2f}   "
          f"open {m['open_positions']}   fills {m['fills']}")
    print(f"\nslippage vs the backtest's flat {m['backtest_assumption_bps']:.1f}bp assumption")
    for key in ('slippage_entry', 'slippage_stop', 'slippage_take_profit',
                'slippage_close_rule_exit'):
        s = m[key]
        label = key.replace('slippage_', '')
        if not s:
            print(f'   {label:18s} no fills yet')
            continue
        print(f"   {label:18s} n={s['n']:<4d} mean {s['mean_bps']:+7.2f}bp   "
              f"median {s['median_bps']:+7.2f}bp   worst {s['worst_bps']:+7.2f}bp")
    g = m['trigger_to_fill_bps']
    if g['n']:
        print(f"\ntrigger -> fill gap  n={g['n']}  mean {g['mean']:.2f}bp  worst {g['worst']:.2f}bp")
        print('   how far past the mark trigger the book actually filled')
    if m['depth_exhausted_fills']:
        print(f"\nWARNING {m['depth_exhausted_fills']} fill(s) ran out of visible depth — "
              'the strategy wanted more size than the book showed')
    print(f"\nfunding accrued on open positions {m['funding_paid']:+,.4f}")


def depth_scan(markets, config, equity: float, notional_multiple: float = 0.86) -> None:
    """What today's book would charge for the sizes this strategy actually takes.

    The backtest charges a flat 2bp per side on every symbol. This prices the
    median backtest position (0.86x equity in notional) against real resting
    depth, per market. A snapshot, not a distribution — run it repeatedly through
    the paper phase to get one.
    """
    from live.book import fetch_depth, walk
    px = marks(config.base_url, markets)
    print(f'depth scan   equity {equity:,.0f}   position notional '
          f'{notional_multiple:.2f}x = {equity * notional_multiple:,.0f} USDT')
    print(f"\n{'symbol':10s}{'qty':>18s}{'sell vwap':>15s}{'sell':>9s}{'buy':>9s}{'depth':>8s}")
    worst = []
    for symbol, market in markets.items():
        price = px.get(symbol)
        if not price:
            continue
        qty = market.round_size(equity * notional_multiple / price)
        depth = fetch_depth(config.base_url, market.market_id)
        bids = [float(o['price']) for o in depth.get('bids') or []][:1]
        asks = [float(o['price']) for o in depth.get('asks') or []][:1]
        if not bids or not asks:
            print(f'{symbol:10s}  no visible book')
            continue
        mid = (bids[0] + asks[0]) / 2
        sell = walk(depth, qty, buying=False)
        buy = walk(depth, qty, buying=True)
        s_bps = sell.slippage_bps(mid, False)
        b_bps = buy.slippage_bps(mid, True)
        worst.append((max(s_bps, b_bps), symbol))
        flag = 'OUT' if (sell.exhausted or buy.exhausted) else 'ok'
        print(f'{symbol:10s}{qty:>18.5f}{sell.vwap:>15.6f}'
              f'{s_bps:>8.2f}{b_bps:>9.2f}{flag:>8s}')
    worst.sort(reverse=True)
    print(f"\nassumed in the backtest: 2.00bp per side, every symbol")
    over = [(b, s) for b, s in worst if b > 2.0]
    if over:
        print('worse than that assumption:')
        for bps, symbol in over:
            print(f'   {symbol:10s} {bps:6.2f}bp   {bps / 2.0:5.1f}x the assumption')
    print('\nOUT means the visible book held less than the strategy wanted — that '
          'size cannot be filled at any of these prices.')


def capacity(markets, config, multiple: float = 0.86) -> None:
    """How the strategy's own slippage grows with the account behind it.

    Weighted by each symbol's share of the backtest's traded notional, so the
    number answers the question that matters: what does THIS strategy pay, not
    what does an average trade pay. Slippage is a capacity constraint — the
    assumption the backtest was built on only holds up to a certain size.
    """
    import csv
    from live.book import fetch_depth, walk

    ROOT = Path(__file__).resolve().parents[2]
    trades = ROOT / 'results/cases/zero_fee_cap4_risk200/trades.csv'
    share, total = {}, 0.
    if trades.exists():
        with trades.open() as fh:
            for row in csv.DictReader(fh):
                n = float(row['initial_qty']) * float(row['entry_price'])
                share[row['symbol']] = share.get(row['symbol'], 0.) + n
                total += n
        share = {k: v / total for k, v in share.items()}
    else:
        share = {s: 1 / len(markets) for s in markets}

    px = marks(config.base_url, markets)
    depth = {s: fetch_depth(config.base_url, m.market_id) for s, m in markets.items()}

    print(f'capacity   position notional {multiple:.2f}x equity   '
          f'weighted by the backtest\'s traded notional')
    print(f"\n{'equity':>12s}{'weighted slippage':>20s}{'vs 2bp':>10s}")
    for equity in (5_000, 10_000, 25_000, 50_000, 100_000, 250_000, 500_000, 1_000_000):
        weighted, problems = 0., []
        for symbol, market in markets.items():
            price = px.get(symbol)
            if not price:
                continue
            qty = market.round_size(equity * multiple / price)
            book = depth[symbol]
            bids, asks = book.get('bids') or [], book.get('asks') or []
            if qty <= 0 or not bids or not asks:
                problems.append(f'{symbol}:small')
                continue
            mid = (float(bids[0]['price']) + float(asks[0]['price'])) / 2
            q = walk(book, qty, buying=False)
            if q.exhausted:
                problems.append(f'{symbol}:OUT')
            weighted += share.get(symbol, 0.) * q.slippage_bps(mid, False)
        note = ('   ' + ' '.join(problems)) if problems else ''
        print(f'{equity:>12,}{weighted:>18.2f}bp{weighted / 2.0:>9.1f}x{note}')
    print('\nOne depth snapshot. Run it repeatedly across the paper phase: depth '
          'moves with time of day and volatility.')
    print('The backtest assumed 2.00bp per side on every symbol at every size.')


def run_once(trader, markets, config) -> None:
    frames = feed.prepare(list(config.universe))
    bar_ms, rows = feed.latest_closed(frames)
    current = marks(config.base_url, markets)
    outcome = trader.on_bar(bar_ms, rows, current)
    stamp = time.strftime('%Y-%m-%d %H:%M', time.gmtime(bar_ms / 1000))
    print(f'[{config.mode}] bar {stamp}Z  equity {outcome.equity:,.2f}  '
          f'positions {len(trader.positions)}')
    for e in outcome.exits:
        print(f'   EXIT  {e["symbol"]:9s} {e["reason"]:16s} qty {e["qty"]:.6f}')
    for e in outcome.entries:
        print(f'   ENTER {e["symbol"]:9s} {e["side"]:5s} qty {e["qty"]:.6f} '
              f'@ {e["price"]:.6f}  stop {e["stop"]:.6f}')
    for symbol, why in outcome.skipped:
        print(f'   skip  {symbol:9s} {why}')
    for r in outcome.reasons:
        print(f'   note  {r}')
    if outcome.halted:
        print('   HALTED — entries stopped, protective orders left in place. '
              'A human needs to look at this.')


def next_boundary(now_ms: int) -> int:
    return (now_ms // H4 + 1) * H4


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--config')
    ap.add_argument('--mode', choices=('shadow', 'paper', 'live'))
    ap.add_argument('--state-dir')
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--loop', action='store_true')
    ap.add_argument('--report', action='store_true')
    ap.add_argument('--depth-scan', action='store_true', dest='depth_scan')
    ap.add_argument('--capacity', action='store_true')
    ap.add_argument('--equity', type=float, default=100_000.)
    ap.add_argument('--i-understand-this-places-real-orders', action='store_true',
                    dest='confirmed')
    a = ap.parse_args()

    overrides = {}
    if a.mode:
        overrides['mode'] = a.mode
    if a.state_dir:
        overrides['state_dir'] = a.state_dir
    config = LiveConfig.load(a.config, **overrides)

    if config.mode == 'live' and not a.confirmed:
        print('Refusing to start: mode is live but --i-understand-this-places-real-orders '
              'was not passed.', file=sys.stderr)
        return 2

    if a.depth_scan or a.capacity:
        loaded = load_markets(config.base_url, config.lighter_universe)
        if a.depth_scan:
            depth_scan(loaded, config, a.equity)
        if a.capacity:
            capacity(loaded, config)
        return 0

    trader, markets = build(config)

    if a.report:
        report(trader)
        return 0

    trader.journal.append('start', config=config.describe())

    if a.once or not a.loop:
        run_once(trader, markets, config)
        return 0

    while True:
        target = next_boundary(int(time.time() * 1000))
        # Let the venue publish the closed bar before acting on it.
        wake = target / 1000 + config.bar_grace_seconds / 4
        poll_until(trader, wake, config)
        try:
            run_once(trader, markets, config)
        except Exception as exc:
            trader.journal.append('bar_error', error=str(exc))
            print(f'   ERROR {exc}', file=sys.stderr)


if __name__ == '__main__':
    raise SystemExit(main())

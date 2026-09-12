"""CLI. Defaults to shadow mode; live requires an explicit flag and a key.

    python src/live/run.py --once                  decide on the latest closed bar, no orders
    python src/live/run.py --loop                  keep running on the 4h boundary
    python src/live/run.py --mode live --loop      real orders (needs LIGHTER_API_PRIVATE_KEY)
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data_io import H4, SYMBOLS                                    # noqa: E402
from live.broker import DryRunBroker, LighterBroker                # noqa: E402
from live.config import LiveConfig, SYMBOL_TO_LIGHTER              # noqa: E402
from live.journal import Journal                                   # noqa: E402
from live.markets import load_markets, marks                       # noqa: E402
from live.trader import Trader                                     # noqa: E402
from live import feed                                              # noqa: E402


def build(config: LiveConfig):
    markets = load_markets(config.base_url, SYMBOL_TO_LIGHTER)
    journal = Journal(config.state_dir)
    if config.mode == 'live':
        broker = LighterBroker(config.base_url, config.account_index,
                               config.private_key, config.api_key_index,
                               stop_limit_pct=config.max_slippage_pct)
    else:
        broker = DryRunBroker()
    return Trader(config, broker, markets, journal), markets


def run_once(trader, markets, config) -> None:
    frames = feed.prepare(SYMBOLS)
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

    trader, markets = build(config)
    trader.journal.append('start', config=config.describe())

    if a.once or not a.loop:
        run_once(trader, markets, config)
        return 0

    while True:
        target = next_boundary(int(time.time() * 1000))
        # Let the venue publish the closed bar before acting on it.
        wake = target / 1000 + config.bar_grace_seconds / 4
        time.sleep(max(1.0, wake - time.time()))
        try:
            run_once(trader, markets, config)
        except Exception as exc:
            trader.journal.append('bar_error', error=str(exc))
            print(f'   ERROR {exc}', file=sys.stderr)


if __name__ == '__main__':
    raise SystemExit(main())

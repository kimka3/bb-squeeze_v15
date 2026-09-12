"""Stage 1 of the rollout: drive the live Trader over history and compare its
decisions with the backtest.

The architecture calls for shadow execution to match a backtest re-run over the
same bars before any money is involved. This is that gate, runnable offline.

ReplayBroker stands in for Lighter. It holds the protective orders the Trader
places and advances them through each bar with trade_core.intrabar — the same
execution model the backtest uses — so a difference in the comparison is a
difference in the live orchestration, not in the fill simulator.

    python src/live/replay.py --bars 400
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np                                                  # noqa: E402

from data_io import H4, SYMBOLS                                     # noqa: E402
from frontier_engine import Strategy, run                           # noqa: E402
from trade_core import Costs, close_fill, intrabar                  # noqa: E402
from live.broker import AccountState, Fill, OrderRef                # noqa: E402
from live.config import LiveConfig, SYMBOL_TO_LIGHTER               # noqa: E402
from live.journal import Journal                                    # noqa: E402
from live.markets import Market as LiveMarket                       # noqa: E402
from live.trader import Trader                                      # noqa: E402

REVERSE = {v: k for k, v in SYMBOL_TO_LIGHTER.items()}


class ReplayBroker:
    """Lighter stand-in driven by historical bars.

    Holds one position and its resting stop/take-profit per market, exactly as
    the exchange would, and fills them from the price path.
    """

    def __init__(self, market, config: Strategy, equity: float = 100_000.0):
        self.market = market
        self.cfg = config
        self.costs = Costs(config.fee, config.slippage)
        self.cash = equity
        self.book: dict[str, dict] = {}          # strategy symbol -> position-like dict
        self.levels: dict[str, dict] = {}        # strategy symbol -> {'stop':, 'tp':}
        self._next_index = 1
        self.closed: list[dict] = []
        self._marks: dict[str, float] = {}

    # -- Broker interface ---------------------------------------------------- #

    def account_state(self) -> AccountState:
        positions = {SYMBOL_TO_LIGHTER[s]: {'side': p['side'], 'qty': p['qty'],
                                            'entry': p['entry']}
                     for s, p in self.book.items() if p['qty'] > 1e-12}
        return AccountState(equity=self.equity(), maintenance_margin=0.0, positions=positions)

    def equity(self, prices: dict | None = None) -> float:
        if not prices:
            return self.cash + sum((p['last'] - p['entry']) * p['qty']
                                   * (1 if p['side'] == 'LONG' else -1)
                                   for p in self.book.values())
        return self.cash + sum((prices[s] - p['entry']) * p['qty']
                               * (1 if p['side'] == 'LONG' else -1)
                               for s, p in self.book.items())

    def market_order(self, market, symbol, position_side, qty, closing, coi, ref_price) -> Fill:
        filled = market.round_size(qty)
        if closing:
            p = self.book.get(symbol)
            if p and p['qty'] > 0:
                self.cash += close_fill(p, p['qty'], ref_price, self.costs, 0, 'CLOSE')
                self.closed.append({'symbol': symbol, 'reason': 'CLOSE'})
            self.book.pop(symbol, None)
            self.levels.pop(symbol, None)
            return Fill(symbol, position_side, filled, ref_price, 'EXIT')
        fee = filled * ref_price * self.costs.fee
        self.cash -= fee
        # Marked at MARK price, not the fill price: an entry filled through
        # slippage shows that cost as unrealised loss the instant it opens, which
        # is how the exchange reports account value and how the backtest marks it.
        self.book[symbol] = {'side': position_side, 'entry': ref_price, 'qty': filled,
                             'initial_qty': filled,
                             'last': float(self._marks.get(symbol, ref_price)),
                             'partial_taken': False, 'stop': 0., 'tp2r': 0.,
                             'gross_pnl': 0., 'fees': fee, 'fills': []}
        return Fill(symbol, position_side, filled, ref_price, 'ENTRY')

    def place_stop(self, market, symbol, position_side, qty, trigger, coi) -> OrderRef:
        self.levels.setdefault(symbol, {})['stop'] = trigger
        if symbol in self.book:
            self.book[symbol]['stop'] = trigger
        return self._ref(coi, 'SL')

    def place_take_profit(self, market, symbol, position_side, qty, trigger, coi) -> OrderRef:
        self.levels.setdefault(symbol, {})['tp'] = trigger
        if symbol in self.book:
            self.book[symbol]['tp2r'] = trigger
        return self._ref(coi, 'TP')

    def modify_stop(self, market, ref, qty, trigger) -> None:
        for symbol, lv in self.levels.items():
            if lv.get('ref') == ref.order_index:
                lv['stop'] = trigger
                if symbol in self.book:
                    self.book[symbol]['stop'] = trigger
                return
        # Fall back to purpose-matching when the ref was rebuilt from a snapshot.
        for symbol, p in self.book.items():
            p['stop'] = trigger

    def cancel(self, market, ref) -> None:
        return None

    def _ref(self, coi, purpose) -> OrderRef:
        ref = OrderRef(coi, self._next_index, purpose)
        self._next_index += 1
        return ref

    # -- price path ---------------------------------------------------------- #

    def mark(self, prices: dict) -> None:
        """Re-mark open positions before equity is read.

        At a real boundary the account value the exchange reports is marked at
        the current price, which is the new bar's open. The backtest marks the
        same instant the same way, so the replay must too or the two size their
        entries off different equity.
        """
        self._marks = dict(prices)
        for symbol, p in self.book.items():
            if symbol in prices:
                p['last'] = float(prices[symbol])

    def advance(self, ts: int, rows: dict) -> None:
        """Walk every open position through this bar, filling stops and partials."""
        for symbol in list(self.book):
            p = self.book[symbol]
            r = rows[symbol]
            oh = [float(r[k]) for k in ['open', 'high', 'low', 'close']]
            micro = self.market.micro.get((symbol, ts))
            path = micro if micro is not None else np.array([[ts, *oh]])
            for sub in path:
                t = int(sub[0])
                o, h, l, c = map(float, sub[1:])
                change, _ = intrabar(p, [o, h, l, c], self.costs,
                                     self.cfg.tp_fraction, t, self.cfg.ambiguity)
                self.cash += change
                if p['qty'] <= 1e-12:
                    self.closed.append({'symbol': symbol, 'reason': p['fills'][-1]['role']
                                        if p['fills'] else 'STOP'})
                    self.book.pop(symbol, None)
                    self.levels.pop(symbol, None)
                    break
            if symbol in self.book:
                self.book[symbol]['last'] = oh[3]


def fake_markets() -> dict[str, LiveMarket]:
    """Exchange rules generous enough that rounding never changes a size — the
    point of this replay is the decision path, not the lot grid."""
    return {s: LiveMarket(symbol=SYMBOL_TO_LIGHTER[s], market_id=i, size_decimals=10,
                          price_decimals=6, min_base_amount=0., min_quote_amount=0.,
                          maintenance_margin_fraction=.06, initial_margin_fraction=.10,
                          status='active')
            for i, s in enumerate(SYMBOLS)}


def compare(bars: int = 400) -> int:
    from run_frontier import market_data

    # funding=False on BOTH sides. ReplayBroker does not model funding cashflows,
    # and leaving them on only in the backtest would shift its equity and so its
    # position sizes — hiding or inventing a difference in the decision path,
    # which is the only thing this gate is meant to measure.
    strategy = Strategy(name='replay', risk=.02, short_btc_bull_risk=.5,
                        fee=0., slippage=.0002, total_risk=.04, funding=False)
    market = market_data()
    timeline = [int(t) for t in market.timestamps]
    window = timeline[-bars:]
    start, end = window[0], window[-1] + H4

    reference = run(market, strategy, start, end)
    ref_entries = [(int(t['entry_ms']), t['symbol'], t['side'], round(t['initial_qty'], 8))
                   for t in reference['trades'].to_dict('records')]

    with tempfile.TemporaryDirectory() as tmp:
        config = LiveConfig(mode='paper', state_dir=tmp, strategy=strategy,
                            max_basis_divergence_pct=1e9, min_notional_usd=0.)
        broker = ReplayBroker(market, strategy)
        trader = Trader(config, broker, fake_markets(), Journal(tmp))
        live_entries = []
        for k in range(len(window) - 1):
            ts = window[k]            # the bar that just closed: signals come from it
            nxt = window[k + 1]       # the bar now opening: fills land here
            rows = {s: market.rows[s][ts] for s in SYMBOLS}
            opens = {s: float(market.rows[s][nxt]['open']) for s in SYMBOLS}
            broker.mark(opens)
            outcome = trader.on_bar(ts, rows, opens, now_ms=ts + H4 + 1000)
            for e in outcome.entries:
                live_entries.append((nxt, e['symbol'], e['side'], round(e['qty'], 8)))
            broker.advance(nxt, {s: market.rows[s][nxt] for s in SYMBOLS})

    ref_set = {(t, s, side) for t, s, side, _ in ref_entries}
    live_set = {(t, s, side) for t, s, side, _ in live_entries}
    only_ref = sorted(ref_set - live_set)
    only_live = sorted(live_set - ref_set)
    shared = ref_set & live_set

    ref_qty = {(t, s, side): q for t, s, side, q in ref_entries}
    live_qty = {(t, s, side): q for t, s, side, q in live_entries}
    worst = 0.0
    for key in shared:
        a, b = ref_qty[key], live_qty[key]
        if a:
            worst = max(worst, abs(a - b) / abs(a))

    print(f'window  {bars} bars  {window[0]} .. {window[-1]}')
    print(f'backtest entries {len(ref_set)}   live entries {len(live_set)}   '
          f'matched {len(shared)}')

    # A. Did the live path choose the same trades at the same bars?
    selection_ok = not only_ref and not only_live
    print(f'\nA. SELECTION  (bar, symbol, side)            '
          f'{"PASS" if selection_ok else "FAIL"}')
    for t, s, side in only_ref[:10]:
        print(f'     backtest only  {t}  {s:9s} {side}')
    for t, s, side in only_live[:10]:
        print(f'     live only      {t}  {s:9s} {side}')

    # B. Did it size them identically? This one also exercises ReplayBroker's own
    #    cash ledger, so a failure here is not automatically a Trader bug.
    sizing_ok = worst < 1e-9
    print(f'B. SIZING     worst relative difference {worst:.3e}   '
          f'{"PASS" if sizing_ok else "FAIL"}')
    if not sizing_ok:
        print('     Sizing runs off account equity. ReplayBroker keeps its own cash\n'
              '     ledger to stand in for the exchange, so a residual here can be\n'
              '     that ledger rather than the decision path. Live reads equity from\n'
              '     Lighter, not from this simulator. Chase it down before trusting\n'
              '     stage 1 as complete, but read A as the stronger signal.')

    ok = selection_ok and sizing_ok
    print(f'\nSTAGE 1 GATE: {"PASS" if ok else "INCOMPLETE"}')
    return 0 if ok else 1


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--bars', type=int, default=400)
    raise SystemExit(compare(ap.parse_args().bars))

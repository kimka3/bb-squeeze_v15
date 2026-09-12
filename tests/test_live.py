"""Live execution tests.

The one that matters most is test_live_path_matches_backtest_decisions: it drives
the live Trader over synthetic bars with a dry-run broker and checks the book it
builds against the backtest engine on the same bars. If that ever fails, live and
backtest have diverged and nothing else in this suite is meaningful.
"""
from pathlib import Path
import sys
import unittest

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from data_io import H4, START, SYMBOLS
from frontier_engine import Market, Strategy, run
from live.broker import DryRunBroker
from live.config import LiveConfig, SYMBOL_TO_LIGHTER
from live.guards import check_sizing, reconcile
from live.journal import Journal, client_order_index
from live.markets import Market as LiveMarket
from live.trader import Trader
import live_core
from trade_core import Costs


def bar(k, **over):
    row = {'time': START + k * H4, 'open': 100., 'high': 102., 'low': 99., 'close': 100.,
           's_atr': 4., 'l_atr': 4., 'short_signal': False, 'long_breakout': False,
           'l_bb_upper': 110., 'l_bb_mid': 90., 'l_ma200': 95., 'l_ma200_slope': 1.,
           'x_bb_lower': 99.5, 'x_ma200': 110., 'x_rvol': 1.}
    row.update(over)
    return row


def frames(signal_symbols, bars=3, bull=False):
    data = {}
    for s in SYMBOLS:
        rows = [bar(k, short_signal=(s in signal_symbols and k == 0),
                    x_ma200=90. if bull else 110.) for k in range(bars)]
        data[s] = pd.DataFrame(rows).set_index('time', drop=False)
    return data


def fake_markets():
    return {s: LiveMarket(symbol=SYMBOL_TO_LIGHTER[s], market_id=i, size_decimals=6,
                          price_decimals=4, min_base_amount=0., min_quote_amount=0.,
                          maintenance_margin_fraction=.06, initial_margin_fraction=.10,
                          status='active')
            for i, s in enumerate(SYMBOLS)}


def live_config(tmp, **over):
    # Existing tests exercise the full eleven; the BCH exclusion is a live
    # default, tested separately in TradedUniverse.
    kw = dict(mode='shadow', state_dir=tmp, universe=tuple(SYMBOLS),
              strategy=Strategy(risk=.02, short_btc_bull_risk=.5, fee=0., slippage=0.,
                                funding=False),
              max_basis_divergence_pct=1e9, min_notional_usd=0.)
    kw.update(over)
    return LiveConfig(**kw)


class CoreSharing(unittest.TestCase):
    def test_backtest_engine_calls_the_shared_core(self):
        import frontier_engine
        for name in ('plan_entry', 'open_position', 'update_on_bar_close', 'scan_signals'):
            self.assertIs(getattr(frontier_engine, name), getattr(live_core, name),
                          f'{name} is not the shared implementation')

    def test_tp_transition_is_one_implementation(self):
        import trade_core
        self.assertIs(live_core.apply_tp_fill, trade_core.mark_partial_taken)

    def test_apply_tp_fill_moves_stop_to_breakeven(self):
        p = {'stop': 110., 'entry': 100., 'partial_taken': False}
        live_core.apply_tp_fill(p)
        self.assertTrue(p['partial_taken'])
        self.assertEqual(p['stop'], 100.)
        p2 = {'stop': 95., 'entry': 100., 'partial_taken': False}
        live_core.apply_tp_fill(p2)
        self.assertEqual(p2['stop'], 95., 'breakeven must never loosen an already tighter stop')


class LiveMatchesBacktest(unittest.TestCase):
    def _live_book(self, tmp, signals, bull=False, bars=3):
        data = frames(signals, bars=bars, bull=bull)
        config = live_config(tmp)
        trader = Trader(config, DryRunBroker(100_000.), fake_markets(), Journal(tmp))
        for k in range(bars):
            ts = START + k * H4
            rows = {s: data[s].loc[ts].to_dict() for s in SYMBOLS}
            trader.on_bar(ts, rows, {s: 100. for s in SYMBOLS}, now_ms=ts + H4 + 1000)
        return trader

    def _backtest_book(self, signals, bull=False, bars=3):
        market = Market(frames(signals, bars=bars, bull=bull),
                        {s: pd.DataFrame() for s in SYMBOLS},
                        {s: pd.DataFrame() for s in SYMBOLS})
        return run(market, Strategy(risk=.02, short_btc_bull_risk=.5, fee=0., slippage=0.,
                                    funding=False), START, START + bars * H4)

    def test_live_path_matches_backtest_decisions(self):
        """Same signals in, same symbols and same sizes out."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            live = self._live_book(tmp, tuple(SYMBOLS))
        back = self._backtest_book(tuple(SYMBOLS))

        entered_live = sorted(live.positions)
        entered_back = sorted({t['symbol'] for t in back['trades'].to_dict('records')}
                              | set(back['trades'].symbol.tolist()))
        self.assertEqual(entered_live, entered_back,
                         'live and backtest opened a different set of symbols')

        by_symbol = {t['symbol']: t for t in back['trades'].to_dict('records')}
        for symbol, p in live.positions.items():
            self.assertAlmostEqual(p['initial_risk'], by_symbol[symbol]['initial_risk'],
                                   places=6, msg=f'{symbol} initial risk differs')
            self.assertAlmostEqual(p['initial_qty'], by_symbol[symbol]['initial_qty'],
                                   places=6, msg=f'{symbol} size differs')

    def test_total_risk_cap_holds_in_the_live_path(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            live = self._live_book(tmp, tuple(SYMBOLS))
        self.assertEqual(len(live.positions), 2, 'two 2% positions fill the 4% budget')
        total = sum(live_core.remaining_risk(p) for p in live.positions.values())
        self.assertAlmostEqual(total, 4000., places=6)

    def test_bull_regime_halves_new_short_risk_in_the_live_path(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            live = self._live_book(tmp, ('ETHUSDT',), bull=True)
        p = live.positions['ETHUSDT']
        self.assertAlmostEqual(p['initial_risk'], 1000., places=6)


class ProtectiveOrders(unittest.TestCase):
    def test_stop_covers_the_whole_position_and_tp_only_half(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            config = live_config(tmp)
            broker = DryRunBroker(100_000.)
            trader = Trader(config, broker, fake_markets(), Journal(tmp))
            data = frames(('ETHUSDT',))
            ts = START
            trader.on_bar(ts, {s: data[s].loc[ts].to_dict() for s in SYMBOLS},
                          {s: 100. for s in SYMBOLS}, now_ms=ts + H4 + 1000)
            ts2 = START + H4
            trader.on_bar(ts2, {s: data[s].loc[ts2].to_dict() for s in SYMBOLS},
                          {s: 100. for s in SYMBOLS}, now_ms=ts2 + H4 + 1000)
            stops = [o for o in broker.log if o['op'] == 'stop']
            tps = [o for o in broker.log if o['op'] == 'tp']
            self.assertTrue(stops, 'a stop must be placed with every entry')
            self.assertTrue(tps, 'a short entry must carry its 2R partial')
            self.assertAlmostEqual(tps[0]['qty'], stops[0]['qty'] * .5, places=6)

    def test_loosening_stop_amend_is_refused(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            config = live_config(tmp)
            broker = DryRunBroker(100_000.)
            trader = Trader(config, broker, fake_markets(), Journal(tmp))
            trader.positions['ETHUSDT'] = {'side': 'SHORT', 'qty': 1., 'stop': 104.,
                                           'entry': 100., 'bars': 1}
            trader.orders['ETHUSDT'] = {'SL': {'client_order_index': 1, 'order_index': 1,
                                               'purpose': 'SL', 'trigger': 102.}}
            out = type('O', (), {'reasons': []})()
            trader._sync_protective_orders(out, START)
            self.assertTrue(any('loosening' in r for r in out.reasons))
            self.assertFalse([o for o in broker.log if o['op'] == 'modify'])


class ReplayBrokerFidelity(unittest.TestCase):
    """The replay broker stands in for the exchange in the stage 1 gate. A bug
    here corrupts the gate itself, so its order bookkeeping is tested directly."""

    def _broker(self):
        from live.replay import ReplayBroker
        return ReplayBroker(market=None, config=Strategy(fee=0., slippage=0., funding=False))

    def test_stop_amend_moves_only_its_own_position(self):
        """A trailing amend for one symbol must not touch any other.

        This regression is why the stage 1 sizing gate failed: modify_stop fell
        through to a fallback that wrote the trigger onto every open position,
        cross-contaminating stops between symbols and producing exits at levels
        no strategy rule had asked for.
        """
        broker = self._broker()
        markets = fake_markets()
        for symbol, trigger in (('ETHUSDT', 110.), ('BTCUSDT', 220.)):
            broker.market_order(markets[symbol], symbol, 'SHORT', 1., False, 1, 100.)
            broker.place_stop(markets[symbol], symbol, 'SHORT', 1., trigger, 1)
        eth_ref = [r for i, r in broker._owner.items() if r == 'ETHUSDT'][0]
        eth_index = [i for i, r in broker._owner.items() if r == 'ETHUSDT'][0]

        from live.broker import OrderRef
        broker.modify_stop(markets['ETHUSDT'], OrderRef(1, eth_index, 'SL'), 1., 105.)

        self.assertEqual(broker.book['ETHUSDT']['stop'], 105.)
        self.assertEqual(broker.book['BTCUSDT']['stop'], 220.,
                         'amending one stop must leave every other position alone')

    def test_unattributable_amend_raises_rather_than_guessing(self):
        broker = self._broker()
        markets = fake_markets()
        broker.market_order(markets['ETHUSDT'], 'ETHUSDT', 'SHORT', 1., False, 1, 100.)
        broker.place_stop(markets['ETHUSDT'], 'ETHUSDT', 'SHORT', 1., 110., 1)
        from live.broker import OrderRef
        with self.assertRaises(KeyError):
            broker.modify_stop(markets['ETHUSDT'], OrderRef(9, 9999, 'SL'), 1., 105.)

    def test_entry_is_marked_at_mark_not_fill_price(self):
        """Slippage shows as unrealised loss the instant the position opens, so
        the next entry in the same bar sizes against the smaller account."""
        broker = self._broker()
        markets = fake_markets()
        broker.mark({'ETHUSDT': 100.})
        broker.market_order(markets['ETHUSDT'], 'ETHUSDT', 'SHORT', 10., False, 1, 99.98)
        self.assertEqual(broker.book['ETHUSDT']['last'], 100.)
        self.assertAlmostEqual(broker.equity(), 100_000. - 0.2, places=9)


class Idempotency(unittest.TestCase):
    def test_client_order_index_is_deterministic_and_in_range(self):
        a = client_order_index(START, 'BTCUSDT', 'ENTRY')
        b = client_order_index(START, 'BTCUSDT', 'ENTRY')
        self.assertEqual(a, b, 'a retry must reuse the same id so the exchange dedupes it')
        self.assertNotEqual(a, client_order_index(START + H4, 'BTCUSDT', 'ENTRY'))
        self.assertNotEqual(a, client_order_index(START, 'BTCUSDT', 'SL'))
        self.assertTrue(0 < a < 2 ** 63 - 1)


class MarketRules(unittest.TestCase):
    def setUp(self):
        self.m = LiveMarket('DOGE', 3, size_decimals=0, price_decimals=6,
                            min_base_amount=100., min_quote_amount=10.,
                            maintenance_margin_fraction=.0399,
                            initial_margin_fraction=.0666, status='active')

    def test_size_rounds_down_so_the_plan_is_never_exceeded(self):
        self.assertEqual(self.m.round_size(150.9), 150.)

    def test_rejects_below_exchange_minimums(self):
        self.assertIn('min_base_amount', self.m.rejects(50., .2))
        self.assertEqual(self.m.rejects(200., .2), '')

    def test_price_outside_uint32_is_refused(self):
        with self.assertRaises(ValueError):
            self.m.scale_price(1e9)       # 1e9 * 1e6 overflows the uint32 price field


class Reconcile(unittest.TestCase):
    def test_untracked_exchange_position_halts(self):
        from live.broker import AccountState
        state = AccountState(equity=1e5, maintenance_margin=1.,
                             positions={'ETH': {'side': 'SHORT', 'qty': 1., 'entry': 100.}})
        v = reconcile({}, state, SYMBOL_TO_LIGHTER)
        self.assertTrue(v.halt)

    def test_matching_books_pass(self):
        from live.broker import AccountState
        state = AccountState(equity=1e5, maintenance_margin=1.,
                             positions={'ETH': {'side': 'SHORT', 'qty': 1., 'entry': 100.}})
        v = reconcile({'ETHUSDT': {'side': 'SHORT', 'qty': 1.}}, state, SYMBOL_TO_LIGHTER)
        self.assertFalse(v.halt)


class BookWalk(unittest.TestCase):
    """Slippage is measured off real depth instead of assumed."""

    DEPTH = {'asks': [{'price': '100.0', 'remaining_base_amount': '1'},
                      {'price': '101.0', 'remaining_base_amount': '2'},
                      {'price': '105.0', 'remaining_base_amount': '10'}],
             'bids': [{'price': '99.0', 'remaining_base_amount': '1'},
                      {'price': '98.0', 'remaining_base_amount': '2'},
                      {'price': '90.0', 'remaining_base_amount': '10'}]}

    def test_buy_walks_asks_upward(self):
        from live.book import walk
        q = walk(self.DEPTH, 3., buying=True)
        self.assertAlmostEqual(q.vwap, (1 * 100. + 2 * 101.) / 3)
        self.assertEqual(q.top, 100.)
        self.assertEqual(q.worst, 101.)
        self.assertFalse(q.exhausted)

    def test_sell_walks_bids_downward(self):
        from live.book import walk
        q = walk(self.DEPTH, 3., buying=False)
        self.assertAlmostEqual(q.vwap, (1 * 99. + 2 * 98.) / 3)

    def test_size_beyond_visible_depth_is_flagged(self):
        from live.book import walk
        q = walk(self.DEPTH, 999., buying=True)
        self.assertTrue(q.exhausted)
        self.assertLess(q.filled, q.requested)

    def test_slippage_is_signed_as_a_cost(self):
        from live.book import walk
        buy = walk(self.DEPTH, 3., buying=True)
        self.assertGreater(buy.slippage_bps(100., buying=True), 0,
                           'paying above the reference is a positive cost')
        sell = walk(self.DEPTH, 3., buying=False)
        self.assertGreater(sell.slippage_bps(99., buying=False), 0,
                           'selling below the reference is also a positive cost')


class PaperTriggers(unittest.TestCase):
    """Lighter fires stops on MARK price. The backtest fires on traded price.
    Reproducing the mark basis is the whole point of stage 2."""

    def _broker(self, tmp, **kw):
        from live.paper import PaperBroker, PaperPosition
        b = PaperBroker.__new__(PaperBroker)          # no network in unit tests
        b.base_url = 'http://unused'
        b.markets = fake_markets()
        b.state_path = Path(tmp) / 'paper.json'
        b.cash = 100_000.
        b.book = {}
        b.fills = []
        b.breakeven_on_fill = kw.get('breakeven_on_fill', True)
        b.passive_take_profit = kw.get('passive_take_profit', False)
        b.funding_rate_is_percent = True
        b._owner = {}
        b._next_index = 1
        b._marks = {}
        b._last_funding_hour = 10 ** 9                # funding already settled
        b._quote = lambda symbol, qty, buying: _FakeQuote(qty)
        b.refresh_marks = lambda: b._marks
        b._accrue_funding = lambda marks: None
        b.save = lambda: None
        b.book['ETHUSDT'] = PaperPosition(side='SHORT', entry=100., qty=10.,
                                          initial_qty=10., stop=104., tp=96.)
        return b

    def test_short_stop_fires_when_mark_crosses_up(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(tmp)
            b._marks = {'ETHUSDT': 103.9}
            self.assertEqual(b.poll(), [], 'below the trigger nothing fires')
            b._marks = {'ETHUSDT': 104.1}
            fired = b.poll()
            self.assertEqual([f.role for f in fired], ['STOP'])
            self.assertNotIn('ETHUSDT', b.book)

    def test_take_profit_takes_half_and_arms_breakeven(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(tmp)
            b._marks = {'ETHUSDT': 95.9}
            fired = b.poll()
            self.assertEqual([f.role for f in fired], ['TP2R'])
            p = b.book['ETHUSDT']
            self.assertAlmostEqual(p.qty, 5.)
            self.assertTrue(p.partial_taken)
            self.assertEqual(p.stop, 100., 'stop must move to breakeven on the fill')

    def test_breakeven_can_be_disabled_to_measure_the_delay(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(tmp, breakeven_on_fill=False)
            b._marks = {'ETHUSDT': 95.9}
            b.poll()
            self.assertEqual(b.book['ETHUSDT'].stop, 104.,
                             'without a fill subscription the stop stays put')

    def test_stop_wins_when_both_could_fire(self):
        """The backtest resolves that ambiguity stop-first; paper must not be
        more optimistic than the model it is being compared against.

        A short can reach this state: the trail drags the stop down past the 2R
        level, so one mark sits at or beyond both triggers at once.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(tmp)
            b.book['ETHUSDT'].stop = 95.              # trailed below the 2R target
            b.book['ETHUSDT'].tp = 96.
            b._marks = {'ETHUSDT': 95.5}              # >= stop AND <= tp
            fired = b.poll()
            self.assertEqual([f.role for f in fired], ['STOP'])
            self.assertNotIn('ETHUSDT', b.book, 'the stop closes the whole position')


class _FakeQuote:
    def __init__(self, qty, vwap=100.):
        self.vwap = vwap
        self.filled = qty
        self.requested = qty
        self.top = 100.
        self.worst = 100.
        self.exhausted = False

    def slippage_bps(self, reference, buying):
        return 0.0


class PassiveTakeProfit(unittest.TestCase):
    """The take-profit is the only leg worth resting. See execution_study.py:
    entries lose more to adverse selection than passivity saves, and a stop that
    does not fill is not a stop."""

    def _broker(self, tmp, **kw):
        from live.paper import PaperBroker, PaperPosition
        b = PaperBroker.__new__(PaperBroker)
        b.base_url = 'http://unused'
        b.markets = fake_markets()
        b.state_path = Path(tmp) / 'paper.json'
        b.cash = 100_000.
        b.book = {}
        b.fills = []
        b.breakeven_on_fill = True
        b.passive_take_profit = kw.get('passive_take_profit', True)
        b.funding_rate_is_percent = True
        b._owner = {}
        b._next_index = 1
        b._marks = {}
        b._last_funding_hour = 10 ** 9
        b._quote = lambda symbol, qty, buying: _FakeQuote(qty, vwap=95.5)
        b.refresh_marks = lambda: b._marks
        b._accrue_funding = lambda marks: None
        b.save = lambda: None
        b.book['ETHUSDT'] = PaperPosition(side='SHORT', entry=100., qty=10.,
                                          initial_qty=10., stop=104., tp=96.)
        return b

    def test_resting_take_profit_fills_at_its_own_price(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(tmp)
            b._marks = {'ETHUSDT': 95.9}
            fill = b.poll()[0]
            self.assertEqual(fill.role, 'TP2R')
            self.assertEqual(fill.price, 96., 'a maker order fills at its own limit')
            self.assertEqual(fill.slippage_bps, 0.0, 'and pays no spread')

    def test_crossing_take_profit_pays_the_book(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(tmp, passive_take_profit=False)
            b._marks = {'ETHUSDT': 95.9}
            fill = b.poll()[0]
            self.assertEqual(fill.price, 95.5, 'a taker fill walks the book instead')

    def test_breakeven_waits_for_the_whole_2R_leg(self):
        """A resting limit can fill in pieces. Arming breakeven off a sliver
        would tighten the stop on a position still carrying its full risk."""
        import tempfile
        from live.broker import AccountState
        with tempfile.TemporaryDirectory() as tmp:
            trader = Trader(live_config(tmp), DryRunBroker(100_000.),
                            fake_markets(), Journal(tmp))
            trader.positions['ETHUSDT'] = {'side': 'SHORT', 'qty': 10., 'initial_qty': 10.,
                                           'stop': 104., 'entry': 100.,
                                           'partial_taken': False, 'bars': 1}
            sliver = AccountState(equity=1e5, maintenance_margin=1.,
                                  positions={'ETH': {'side': 'SHORT', 'qty': 9.,
                                                     'entry': 100.}})
            trader._sync_fills(sliver)
            p = trader.positions['ETHUSDT']
            self.assertFalse(p['partial_taken'], '10% of the leg is not the leg')
            self.assertEqual(p['stop'], 104., 'stop must not tighten yet')

            done = AccountState(equity=1e5, maintenance_margin=1.,
                                positions={'ETH': {'side': 'SHORT', 'qty': 5.,
                                                   'entry': 100.}})
            trader._sync_fills(done)
            p = trader.positions['ETHUSDT']
            self.assertTrue(p['partial_taken'])
            self.assertEqual(p['stop'], 100.)


class TradedUniverse(unittest.TestCase):
    """BCH is excluded by default on cost grounds: 44.6bp round trip is 0.383R,
    i.e. 38% of the risk budget handed to the book before the trade has a view."""

    def test_bch_is_excluded_by_default(self):
        from live.config import DEFAULT_UNIVERSE, EXCLUDED
        self.assertIn('BCHUSDT', EXCLUDED)
        self.assertNotIn('BCHUSDT', DEFAULT_UNIVERSE)
        self.assertEqual(len(DEFAULT_UNIVERSE), len(SYMBOLS) - 1)

    def test_excluded_symbol_never_produces_an_entry(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            config = live_config(tmp, universe=LiveConfig().universe)
            trader = Trader(config, DryRunBroker(100_000.), fake_markets(), Journal(tmp))
            data = frames(tuple(SYMBOLS))          # every symbol signals
            for k in range(2):
                ts = START + k * H4
                rows = {s: data[s].loc[ts].to_dict() for s in SYMBOLS}
                trader.on_bar(ts, rows, {s: 100. for s in SYMBOLS}, now_ms=ts + H4 + 1000)
            self.assertNotIn('BCHUSDT', trader.positions)
            self.assertNotIn('BCHUSDT', trader.pending)

    def test_btc_cannot_be_dropped(self):
        """Every new short consults BTC's SMA200 regime."""
        with self.assertRaises(ValueError):
            LiveConfig(universe=('ETHUSDT', 'SOLUSDT'))

    def test_unknown_symbol_is_refused(self):
        with self.assertRaises(ValueError):
            LiveConfig(universe=('BTCUSDT', 'NOSUCHUSDT'))


class ConfigSafety(unittest.TestCase):
    def test_live_mode_requires_an_account_index(self):
        with self.assertRaises(ValueError):
            LiveConfig(mode='live')

    def test_default_mode_never_touches_the_exchange(self):
        self.assertEqual(LiveConfig().mode, 'shadow')


if __name__ == '__main__':
    unittest.main()

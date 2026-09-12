"""Execution failures must preserve exposure, evidence, and protective orders."""
from copy import deepcopy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from data_io import H4, START
from live.broker import AccountState, DryRunBroker, Fill, LighterBroker, OrderRef
from live.guards import reconcile
from live.journal import Journal
from live.trader import BarOutcome, Trader
from test_live import bar, fake_markets, live_config


class LedgerBroker(DryRunBroker):
    """Synchronous confirmed ledger, with injectable partials and lost replies."""

    def __init__(self):
        super().__init__()
        self.results = {}
        self.close_quantities = []
        self.lose_entry_reply = False
        self.fail_tp = False
        self.fail_amend = False
        self.verified_orders = False
        self.owner = {}
        self._marks = {'ETHUSDT': 100.}

    def account_state(self):
        active = {}
        for idx, ref in self.orders.items():
            symbol = self.owner.get(idx, 'ETH')
            active.setdefault(symbol, []).append(deepcopy(ref.__dict__))
        return AccountState(self._equity, 0., deepcopy(self.positions), active,
                            execution_changes_verified=True,
                            active_orders_verified=self.verified_orders)

    def market_order(self, market, symbol, position_side, qty, closing, coi, ref_price):
        if coi in self.results:
            return self.results[coi]
        if closing:
            p = self.positions.get(market.symbol)
            filled = self.close_quantities.pop(0) if self.close_quantities else qty
            filled = min(filled, p['qty']) if p else 0.
            self.log.append({'op': 'market', 'closing': True, 'qty': filled, 'coi': coi})
            if p:
                p['qty'] -= filled
                if p['qty'] <= 1e-12:
                    self.positions.pop(market.symbol)
            fill = Fill(symbol, position_side, filled, ref_price, 'EXIT')
        else:
            fill = super().market_order(market, symbol, position_side, qty, closing, coi, ref_price)
        self.results[coi] = fill
        if not closing and self.lose_entry_reply:
            self.lose_entry_reply = False
            raise ConnectionError('accepted entry, reply lost')
        return fill

    def market_order_result(self, coi):
        return self.results.get(coi)

    def place_stop(self, market, symbol, position_side, qty, trigger, coi):
        ref = super().place_stop(market, symbol, position_side, qty, trigger, coi)
        self.owner[ref.order_index] = market.symbol
        return ref

    def place_take_profit(self, market, symbol, position_side, qty, trigger, coi):
        if self.fail_tp:
            raise ConnectionError('TP rejected')
        ref = super().place_take_profit(market, symbol, position_side, qty, trigger, coi)
        self.owner[ref.order_index] = market.symbol
        return ref

    def modify_stop(self, market, ref, qty, trigger):
        if self.fail_amend:
            raise ConnectionError('amend reply lost')
        super().modify_stop(market, ref, qty, trigger)
        self.orders[ref.order_index].trigger = trigger
        self.orders[ref.order_index].qty = qty


def trader_for(tmp_path, broker=None):
    broker = broker or LedgerBroker()
    return Trader(live_config(tmp_path), broker, fake_markets(), Journal(tmp_path))


def seed(trader):
    trader.positions['ETHUSDT'] = {
        'side': 'SHORT', 'qty': 10., 'initial_qty': 10., 'entry': 100.,
        'stop': 104., 'tp2r': 92., 'partial_taken': False, 'bars': 1,
    }
    trader.broker.positions['ETH'] = {'side': 'SHORT', 'qty': 10., 'entry': 100.,
                                      'partial_taken': False}
    trader._place_protective('ETHUSDT', START)


@pytest.mark.parametrize('filled', [0., 4.])
def test_incomplete_close_keeps_residual_and_protection(tmp_path, filled):
    trader = trader_for(tmp_path)
    seed(trader)
    before = deepcopy(trader.orders['ETHUSDT'])
    trader.broker.close_quantities = [filled]
    trader._exit('ETHUSDT', 'TIME', {'ETHUSDT': 100.}, START, BarOutcome(START, 100000.))
    assert trader.positions['ETHUSDT']['qty'] == 10. - filled
    assert trader.orders['ETHUSDT'] == before
    assert not any(row['op'] == 'cancel' for row in trader.broker.log)
    assert 'ETHUSDT' in trader.residual_exits
    assert Journal(tmp_path).load_snapshot()['positions']['ETHUSDT']['qty'] == 10. - filled


def test_fast_service_retries_only_residual_with_new_order_id(tmp_path):
    trader = trader_for(tmp_path)
    seed(trader)
    trader.broker.close_quantities = [4., 6.]
    trader._exit('ETHUSDT', 'TIME', {'ETHUSDT': 100.}, START, BarOutcome(START, 100000.))
    trader.service(now_ms=START + H4)
    assert 'ETHUSDT' not in trader.positions
    assert trader.residual_exits == {}
    calls = [row for row in trader.broker.log if row['op'] == 'market']
    assert [row['qty'] for row in calls] == [4., 6.]
    assert len({row['coi'] for row in calls}) == 2
    assert trader.broker.log.index(next(row for row in trader.broker.log if row['op'] == 'cancel')) > 1


def test_smaller_unattributed_position_halts_without_fabricating_tp(tmp_path):
    trader = trader_for(tmp_path)
    seed(trader)
    state = AccountState(100000., 0., {'ETH': {'side': 'SHORT', 'qty': 5., 'entry': 100.}})
    trader._sync_fills(state)
    assert trader.halted
    assert trader.positions['ETHUSDT']['qty'] == 10.
    assert trader.positions['ETHUSDT']['partial_taken'] is False
    assert trader_for(tmp_path, trader.broker).halted


def test_verified_stop_partial_does_not_arm_breakeven(tmp_path):
    trader = trader_for(tmp_path)
    seed(trader)
    trader.broker.positions['ETH']['qty'] = 5.
    trader._sync_fills(trader.broker.account_state())
    assert trader.positions['ETHUSDT']['qty'] == 5.
    assert trader.positions['ETHUSDT']['partial_taken'] is False
    assert trader.positions['ETHUSDT']['stop'] == 104.


def test_tp_event_moves_stop_in_fast_loop_without_waiting_for_bar(tmp_path):
    trader = trader_for(tmp_path)
    seed(trader)
    trader.broker.positions['ETH'].update(qty=5., partial_taken=True)
    trader.service(now_ms=START)
    assert trader.positions['ETHUSDT']['partial_taken'] is True
    assert trader.orders['ETHUSDT']['SL']['trigger'] == 100.
    assert trader.orders['ETHUSDT']['SL']['qty'] == 5.


def test_unknown_disappearance_is_not_silently_healed(tmp_path):
    trader = trader_for(tmp_path)
    seed(trader)
    trader._sync_fills(AccountState(100000., 0.))
    assert trader.halted
    assert 'ETHUSDT' in trader.positions
    assert 'SL' in trader.orders['ETHUSDT']


def enter(trader):
    rows = {'BTCUSDT': bar(0), 'ETHUSDT': bar(0)}
    trader._enter('ETHUSDT', ('SHORT', 4., START), {'BTCUSDT': 100., 'ETHUSDT': 100.},
                  rows, START + H4, BarOutcome(START, 100000.), 100000.)


def test_lost_entry_reply_recovers_fill_and_protection_without_reentry(tmp_path):
    trader = trader_for(tmp_path)
    trader.broker.lose_entry_reply = True
    enter(trader)
    assert trader.pending_operation['kind'] == 'entry'
    restarted = trader_for(tmp_path, trader.broker)
    restarted.service(now_ms=START + H4)
    assert restarted.pending_operation is None
    assert 'SL' in restarted.orders['ETHUSDT']
    assert len([row for row in trader.broker.log if row['op'] == 'market']) == 1
    assert restarted.halted  # Recovery does not silently clear the durable alert.


def test_tp_registration_failure_preserves_entry_and_accepted_stop(tmp_path):
    trader = trader_for(tmp_path)
    trader.broker.fail_tp = True
    enter(trader)
    snap = Journal(tmp_path).load_snapshot()
    assert snap['pending_operation']['phase'] == 'protect'
    assert 'ETHUSDT' in snap['positions']
    assert 'SL' in snap['orders']['ETHUSDT']
    trader.broker.fail_tp = False
    restarted = trader_for(tmp_path, trader.broker)
    restarted.service(now_ms=START + H4)
    assert restarted.pending_operation is None
    assert set(restarted.orders['ETHUSDT']) == {'SL', 'TP'}
    assert len([row for row in trader.broker.log if row['op'] == 'stop']) == 1


def test_unknown_pending_intent_never_retransmits(tmp_path):
    trader = trader_for(tmp_path)
    trader.pending_operation = {'kind': 'entry', 'coi': 77, 'symbol': 'ETHUSDT'}
    trader._save()
    restarted = trader_for(tmp_path, trader.broker)
    restarted.service(now_ms=START)
    assert restarted.halted
    assert restarted.pending_operation is not None
    assert trader.broker.log == []


def test_interrupted_bar_is_not_replayed_after_restart(tmp_path):
    trader = trader_for(tmp_path)
    trader.inflight_bar_ms = START
    trader._save()
    restarted = trader_for(tmp_path, trader.broker)
    result = restarted.on_bar(START, {}, {}, now_ms=START + H4)
    assert restarted.last_bar_ms == START
    assert 'already processed' in result.reasons[0]
    assert trader.broker.log == []


def test_missing_stop_is_not_treated_as_successful_close(tmp_path):
    trader = trader_for(tmp_path)
    seed(trader)
    trader.broker.verified_orders = True
    sl = trader.orders['ETHUSDT']['SL']
    trader.broker.orders.pop(sl['order_index'])
    trader.broker.close_quantities = [0.]
    result = trader.service(now_ms=START + H4)
    assert result.halted
    assert trader.positions['ETHUSDT']['qty'] == 10.
    assert 'ETHUSDT' in trader.residual_exits
    assert 'SL' in trader.orders['ETHUSDT']  # Failed reduction gets replacement protection.


def test_stop_amend_failure_has_durable_unknown_outcome(tmp_path):
    trader = trader_for(tmp_path)
    seed(trader)
    trader.broker.fail_amend = True
    trader.positions['ETHUSDT']['stop'] = 103.
    trader._sync_protective_orders(BarOutcome(START, 100000.), START)
    assert trader.halted
    assert trader.orders['ETHUSDT']['SL']['trigger'] == 104.
    assert Journal(tmp_path).load_snapshot()['pending_operation']['kind'] == 'amend'


@pytest.mark.parametrize('method,args', [
    ('market_order', ('ETHUSDT', 'SHORT', 10., False, 1, 100.)),
    ('place_stop', ('ETHUSDT', 'SHORT', 10., 104., 1)),
    ('place_take_profit', ('ETHUSDT', 'SHORT', 5., 92., 1)),
    ('modify_stop', (OrderRef(1, 1, 'SL'), 10., 103.)),
    ('cancel', (OrderRef(1, 1, 'SL'),)),
])
def test_live_writes_refused_before_any_signer_access(method, args):
    broker = LighterBroker.__new__(LighterBroker)  # No SDK, keys, or network.
    with pytest.raises(RuntimeError, match='live execution disabled'):
        getattr(broker, method)(fake_markets()['ETHUSDT'], *args)


def test_unknown_market_exposure_and_small_quantity_mismatch_halt():
    assert reconcile({}, AccountState(100000., 0., {'UNKNOWN': {'qty': 1.}}), {}).halt
    assert reconcile({'ETHUSDT': {'side': 'SHORT', 'qty': 10.}},
                     AccountState(100000., 0., {'ETH': {'side': 'SHORT', 'qty': 9.99}}),
                     {'ETHUSDT': 'ETH'}).halt


def test_failed_cancel_is_retained_after_flat_and_retried(tmp_path):
    trader = trader_for(tmp_path)
    seed(trader)
    original = trader.broker.cancel
    trader.broker.cancel = lambda *args: (_ for _ in ()).throw(ConnectionError('cancel unavailable'))
    trader._exit('ETHUSDT', 'TIME', {'ETHUSDT': 100.}, START, BarOutcome(START, 100000.))
    assert 'ETHUSDT' not in trader.positions
    assert set(trader.orphan_orders['ETHUSDT']) == {'SL', 'TP'}
    assert trader.halted
    trader.broker.cancel = original
    restarted = trader_for(tmp_path, trader.broker)
    restarted.service(now_ms=START + H4)
    assert restarted.orphan_orders == {}
    assert restarted.halted


def test_actual_entry_fill_anchors_stop_and_risk_without_changing_atr_distance(tmp_path):
    trader = trader_for(tmp_path)
    original = trader.broker.market_order

    def slipped(market, symbol, side, qty, closing, coi, ref_price):
        return original(market, symbol, side, qty, closing, coi, 99.)

    trader.broker.market_order = slipped
    enter(trader)
    p = trader.positions['ETHUSDT']
    assert p['entry'] == 99.
    assert p['stop'] - p['entry'] == p['risk_unit'] == 4.
    assert p['initial_risk'] == p['qty'] * 4.
    assert p['tp2r'] == p['entry'] - 2 * p['risk_unit']


@pytest.mark.parametrize('explicit_empty', [False, True])
def test_service_does_not_close_residual_or_create_protection_with_stale_marks(tmp_path, explicit_empty):
    trader = trader_for(tmp_path)
    seed(trader)
    trader.broker.verified_orders = True
    sl = trader.orders['ETHUSDT']['SL']
    trader.broker.orders.pop(sl['order_index'])
    trader.broker._mark_sample_ms = {'ETHUSDT': START}
    late = START + int(trader.config.poll_seconds * 2 * 1000) + 1
    count_before = len(trader.broker.log)
    result = trader.service(now_ms=late, marks={} if explicit_empty else None)
    assert result.halted
    assert len(trader.broker.log) == count_before
    assert trader.positions['ETHUSDT']['qty'] == 10.
    assert 'SL' not in trader.orders['ETHUSDT']
    trader.broker._mark_sample_ms['ETHUSDT'] = late
    trader.service(now_ms=late)
    assert 'ETHUSDT' not in trader.positions


def test_confirmed_entry_recovery_waits_for_fresh_mark_before_placing_stop(tmp_path):
    trader = trader_for(tmp_path)
    trader.broker.lose_entry_reply = True
    enter(trader)
    restarted = trader_for(tmp_path, trader.broker)
    restarted.service(now_ms=START + H4, marks={})
    assert restarted.pending_operation['phase'] == 'protect'
    assert 'ETHUSDT' in restarted.positions
    assert not restarted.orders
    restarted.service(now_ms=START + H4, marks={'ETHUSDT': 100.})
    assert restarted.pending_operation is None
    assert 'SL' in restarted.orders['ETHUSDT']


def test_protection_recovery_accepts_ledger_confirmed_flat_without_reopening(tmp_path):
    trader = trader_for(tmp_path)
    trader.broker.fail_tp = True
    enter(trader)
    trader.broker.positions.pop('ETH')  # Accepted SL completed before restart.
    restarted = trader_for(tmp_path, trader.broker)
    restarted.service(now_ms=START + H4, marks={})
    assert restarted.pending_operation is None
    assert restarted.positions == {}
    assert restarted.orders == {}
    assert len([row for row in trader.broker.log if row['op'] == 'market']) == 1


def test_missed_bar_gap_persists_and_keeps_fast_protection_running(tmp_path):
    trader = trader_for(tmp_path)
    seed(trader)
    trader.last_bar_ms = START
    trader.broker.positions['ETH'].update(qty=5., partial_taken=True)
    result = trader.on_bar(START + 3 * H4, {}, {'ETHUSDT': 100.}, now_ms=START + 4 * H4)
    assert result.halted
    assert trader.positions['ETHUSDT']['bars'] == 1  # No invented replay of missing bars.
    assert trader.positions['ETHUSDT']['partial_taken'] is True
    assert trader.orders['ETHUSDT']['SL']['trigger'] == 100.
    snap = Journal(tmp_path).load_snapshot()
    assert snap['data_gaps'][0]['unobserved_ms'] == 2 * H4
    restarted = trader_for(tmp_path, trader.broker)
    assert restarted.halted and restarted.data_gaps
    result = restarted.on_bar(START + 4 * H4, {}, {'ETHUSDT': 100.}, now_ms=START + 5 * H4)
    assert result.halted
    assert result.entries == []


def real_paper_trader(tmp_path):
    from live.paper import PaperBroker
    from live.book import Quote
    paper = PaperBroker('http://unused', fake_markets(), tmp_path / 'paper.json')
    paper._marks = {'ETHUSDT': 100., 'BTCUSDT': 100.}
    paper._mark_sample_ms = {'ETHUSDT': START + H4, 'BTCUSDT': START + H4}
    paper._quote = lambda symbol, qty, buying, **kwargs: Quote(100., qty, qty, 100., 100., False)
    return trader_for(tmp_path, paper)


def test_real_paper_ioc_partial_reprotects_with_new_id_at_same_timestamp(tmp_path):
    from live.book import Quote
    trader = real_paper_trader(tmp_path)
    enter(trader)
    paper = trader.broker
    original_qty = trader.positions['ETHUSDT']['qty']
    original_coi = trader.orders['ETHUSDT']['SL']['client_order_index']
    paper._marks['ETHUSDT'] = 104.5
    paper._quote = lambda symbol, qty, buying, **kwargs: Quote(104.5, qty * .4, qty, 104.5, 104.5, True)
    paper._poll_orders(paper._marks)
    paper.save()
    assert paper.book['ETHUSDT'].stop == 0.
    paper._quote = lambda symbol, qty, buying, **kwargs: Quote(float('nan'), 0., qty, float('nan'), float('nan'), True)
    outcome = trader.service(now_ms=START + H4)
    assert outcome.halted
    assert trader.positions['ETHUSDT']['qty'] == pytest.approx(original_qty * .6)
    assert trader.positions['ETHUSDT']['partial_taken'] is False
    new_sl = trader.orders['ETHUSDT']['SL']
    assert new_sl['client_order_index'] != original_coi
    active = paper.account_state().active_orders['ETH']
    assert any(o['client_order_index'] == new_sl['client_order_index'] for o in active)
    paper._quote = lambda symbol, qty, buying, **kwargs: Quote(104.5, qty, qty, 104.5, 104.5, False)
    trader.service(now_ms=START + H4)
    assert trader.positions == {}
    assert paper.book == {}


def test_real_paper_tp_partial_ledger_survives_restart_and_arms_be_only_when_complete(tmp_path):
    from live.book import Quote
    from live.paper import PaperBroker
    trader = real_paper_trader(tmp_path)
    enter(trader)
    paper = trader.broker
    paper._marks['ETHUSDT'] = 91.
    paper._quote = lambda symbol, qty, buying, **kwargs: Quote(91., qty * .25, qty, 91., 91., True)
    paper._poll_orders(paper._marks)
    paper.save()
    trader.service(now_ms=START + H4)
    assert trader.positions['ETHUSDT']['partial_taken'] is False
    assert trader.positions['ETHUSDT']['stop'] == 104.
    paper2 = PaperBroker('http://unused', fake_markets(), tmp_path / 'paper.json')
    restarted = trader_for(tmp_path, paper2)
    paper2._quote = lambda symbol, qty, buying, **kwargs: Quote(91., qty, qty, 91., 91., False)
    paper2._poll_orders(paper2._marks)
    paper2.save()
    restarted.service(now_ms=START + H4)
    assert restarted.positions['ETHUSDT']['partial_taken'] is True
    assert restarted.orders['ETHUSDT']['SL']['trigger'] == 100.
    assert paper2.book['ETHUSDT'].partial_taken is True
    assert restarted.positions['ETHUSDT']['qty'] == pytest.approx(paper2.book['ETHUSDT'].qty)

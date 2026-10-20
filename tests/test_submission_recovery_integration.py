"""F0/F2 regressions with an in-memory venue; never use network or credentials.

The expectations distinguish proven pre-submission refusal from an attempted
submission whose exchange evidence is delayed or missing. Neither permits a
duplicate entry, and unrelated confirmed positions must retain risk service.
"""
from decimal import Decimal

import pytest

from test_strategy_integration import START, H4, Venue, rows, setup
from live.authenticated import APIReadError
from live.execution_evidence import NotSubmitted
from live.request_budget import RequestDeferred


MARKS = {'BTCUSDT': 100, 'ETHUSDT': 100}


def transient_read():
    return APIReadError('API read failed (HTTP 503); response suppressed',
        code='http_503', endpoint='/api/v1/trades',
        automatic_recovery_allowed=True, http_status=503)


def service_tick(venue, broker, trader, seconds=61):
    venue.now += seconds * 1000
    broker.poll()
    return trader.service(now_ms=venue.now)


def test_deadline_crossing_after_trader_intent_is_not_unknown_submission(tmp_path):
    venue, broker, trader = setup(tmp_path)
    offer = broker.offer_entry_state

    def cross_deadline(state):
        offer(state)
        venue.now = START + H4 + trader.config.bar_grace_seconds * 1000

    broker.offer_entry_state = cross_deadline
    try:
        trader.on_bar(START, rows(), MARKS, now_ms=venue.now)
        assert venue.sent == []
        assert trader.pending_operation is None
        assert not trader.halted
        service_tick(venue, broker, trader)
        trader.on_bar(START, rows(), MARKS, now_ms=venue.now)
        assert venue.sent == []  # The expired candidate is never replayed.
        assert trader.pending_operation is None
    finally:
        broker.close()


def test_pause_race_before_broker_submission_preserves_manual_pause(tmp_path):
    venue, broker, trader = setup(tmp_path)
    broker.entry_guard = lambda: True
    offer = broker.offer_entry_state

    def pause_after_offer(state):
        offer(state)
        broker.entry_guard = lambda: False

    broker.offer_entry_state = pause_after_offer
    try:
        trader.on_bar(START, rows(), MARKS, now_ms=venue.now)
        assert venue.sent == []
        assert trader.pending_operation is None
        assert broker.entry_guard() is False
        service_tick(venue, broker, trader)
        assert venue.sent == [] and broker.entry_guard() is False
    finally:
        broker.close()


def test_unverified_exposure_before_bind_does_not_create_phantom_entry(tmp_path):
    venue, broker, trader = setup(tmp_path)
    offer = broker.offer_entry_state

    def unverified_after_offer(state):
        state.execution_changes_verified = False
        offer(state)

    broker.offer_entry_state = unverified_after_offer
    try:
        trader.on_bar(START, rows(), MARKS, now_ms=venue.now)
        assert venue.sent == []
        assert trader.pending_operation is None
        assert broker.engine.store.db.execute('SELECT COUNT(*) FROM intents').fetchone()[0] == 0
        service_tick(venue, broker, trader)
        assert venue.sent == []
    finally:
        broker.close()


def test_exit_preflight_read_failure_retains_close_and_protection_then_recovers(tmp_path):
    venue, broker, trader = setup(tmp_path)
    try:
        trader.on_bar(START, rows(), MARKS, now_ms=venue.now)
        trader.positions['ETHUSDT']['bars'] = 89
        stop_coi = trader.orders['ETHUSDT']['SL']['client_order_index']
        count = len(venue.sent)
        original = broker.account_state
        calls = 0

        def fail_exit_preflight():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise transient_read()
            return original()

        broker.account_state = fail_exit_preflight
        venue.now = START + 2 * H4 + 1_000
        with pytest.raises(APIReadError):
            # Preserve the typed read failure for the runner's existing
            # exchange-recovery controller, after fixing local operation state.
            trader.on_bar(START + H4, rows(short=False), MARKS, now_ms=venue.now)
        broker.account_state = original
        assert len(venue.sent) == count
        assert trader.pending_operation is None
        assert trader.residual_exits['ETHUSDT']['reason'] == 'TIME'
        assert venue.rows[stop_coi]['status'] == 'pending'
        service_tick(venue, broker, trader)
        assert 'ETHUSDT' not in trader.positions
        closes = [intent for intent in venue.sent if intent.order_type == 'market' and intent.reduce_only]
        assert len(closes) == 1
        assert venue.rows[stop_coi]['status'] == 'canceled'
    finally:
        broker.close()


def test_intent_storage_failure_cannot_be_treated_as_harmless_refusal(tmp_path, monkeypatch):
    venue, broker, trader = setup(tmp_path)

    def storage_failure(intent):
        raise OSError('test: durable intent storage failed')

    monkeypatch.setattr(broker.engine.store, 'reserve', storage_failure)
    try:
        trader.on_bar(START, rows(), MARKS, now_ms=venue.now)
        assert venue.sent == []
        assert trader.halted
        assert not trader.positions
        assert broker.engine.store.db.execute('SELECT COUNT(*) FROM intents').fetchone()[0] == 0
    finally:
        broker.close()


def test_exchange_id_collision_without_local_intent_stays_fail_closed(tmp_path, monkeypatch):
    venue, broker, trader = setup(tmp_path)
    entry_coi = trader._coi(START + H4, 'ETHUSDT', 'ENTRY')
    original = venue.order

    def foreign_order(coi):
        if coi == entry_coi:
            return {'client_order_index': coi, 'owner_account_index': 999}
        return original(coi)

    monkeypatch.setattr(venue, 'order', foreign_order)
    try:
        trader.on_bar(START, rows(), MARKS, now_ms=venue.now)
        assert venue.sent == []
        assert trader.halted
        assert not trader.positions
    finally:
        broker.close()


class DelayedEvidenceVenue(Venue):
    """One transaction may arrive later; no empty row is proof of rejection."""
    def __init__(self, target_kind, target_market=0):
        super().__init__()
        self.target_kind = target_kind
        self.target_market = target_market
        self.delayed = None
        self.attempts = []

    def submit(self, intent, base, price, trigger):
        self.attempts.append(intent)
        if (self.delayed is None and intent.order_type == self.target_kind
                and intent.market_index == self.target_market):
            self.delayed = (intent, base, price, trigger)
            raise RuntimeError('test: ambiguous send; evidence has not appeared')
        return super().submit(intent, base, price, trigger)

    @property
    def delayed_coi(self):
        return self.delayed[0].client_order_index

    def publish_late(self):
        # Deliver the same attempted transaction, not a new send from the bot.
        return Venue.submit(self, *self.delayed)


class RefusedBeforeDispatchVenue(Venue):
    """Positive local non-dispatch proof, unlike DelayedEvidenceVenue."""
    def __init__(self, target_kind):
        super().__init__()
        self.target_kind = target_kind
        self.refused = None
        self.calls = []

    def submit(self, intent, base, price, trigger):
        self.calls.append(intent)
        if intent.order_type == self.target_kind and self.refused is None:
            self.refused = intent
            raise NotSubmitted('request_budget_deferred', retryable=True,
                               cause=RequestDeferred(1))
        return super().submit(intent, base, price, trigger)


def test_durable_but_definitely_unsent_entry_is_not_replayed_after_deadline(tmp_path):
    venue, broker, trader = setup(tmp_path, RefusedBeforeDispatchVenue('market'))
    try:
        trader.on_bar(START, rows(), MARKS, now_ms=venue.now)
        coi = venue.refused.client_order_index
        assert broker.engine.store.known_not_submitted(coi)
        assert broker.engine.store.load(coi)[1] is None  # Not a fabricated canceled/no-fill result.
        assert not trader.positions and venue.sent == []
        assert trader.pending_operation is None
        service_tick(venue, broker, trader)
        trader.retry_deferred_entries()
        assert venue.sent == [] and not trader.positions
        assert sum(i.client_order_index == coi for i in venue.calls) == 1
    finally:
        broker.close()


@pytest.mark.parametrize('target_kind', ['stop-loss', 'limit'])
def test_definitely_unsent_protection_can_recover_without_duplicate_dispatch(tmp_path, target_kind):
    venue, broker, trader = setup(tmp_path, RefusedBeforeDispatchVenue(target_kind))
    try:
        trader.on_bar(START, rows(), MARKS, now_ms=venue.now)
        coi = venue.refused.client_order_index
        assert broker.engine.store.known_not_submitted(coi)
        assert sum(i.client_order_index == coi for i in venue.sent) == 0
        trader._halt('pre-existing safety hold')
        service_tick(venue, broker, trader)
        assert 'ETHUSDT' in trader.positions
        assert set(trader.orders['ETHUSDT']) >= {'SL', 'TP'}
        assert sum(i.client_order_index == coi for i in venue.sent) == 1
        assert not broker.engine.store.known_not_submitted(coi)
        service_tick(venue, broker, trader)
        assert sum(i.client_order_index == coi for i in venue.sent) == 1
        assert trader.halted
        assert 'pre-existing safety hold' in trader.halt_reasons
    finally:
        broker.close()


def test_unknown_entry_survives_restart_and_late_fill_is_protected_once(tmp_path):
    venue, broker, trader = setup(tmp_path, DelayedEvidenceVenue('market'))
    try:
        trader.on_bar(START, rows(), MARKS, now_ms=venue.now)
        coi = venue.delayed_coi
        assert trader.halted
        assert not trader.positions
        assert broker.engine.store.load(coi)[1] is None
        service_tick(venue, broker, trader)
        assert sum(i.client_order_index == coi for i in venue.attempts) == 1
        assert trader.halted
        assert str(coi) in broker.account_state().ledger_report['unresolved_orders']
    finally:
        broker.close()

    venue, broker, trader = setup(tmp_path, venue)
    try:
        service_tick(venue, broker, trader)
        assert sum(i.client_order_index == coi for i in venue.attempts) == 1
        venue.publish_late()
        service_tick(venue, broker, trader)
        assert 'ETHUSDT' in trader.positions
        assert set(trader.orders['ETHUSDT']) >= {'SL', 'TP'}
        assert sum(i.client_order_index == coi for i in venue.attempts) == 1
        assert sum(i.order_type == 'stop-loss' for i in venue.attempts) == 1
        assert sum(i.order_type == 'limit' for i in venue.attempts) == 1
        service_tick(venue, broker, trader)
        assert sum(i.order_type == 'stop-loss' for i in venue.attempts) == 1
        assert trader.halted  # Resolving evidence does not auto-clear a safety HALT.
    finally:
        broker.close()


@pytest.mark.parametrize('missing_kind', ['market', 'limit'])
def test_unknown_other_market_keeps_confirmed_stop_management_alive(tmp_path, monkeypatch, missing_kind):
    venue, broker, trader = setup(tmp_path, DelayedEvidenceVenue(missing_kind))
    monkeypatch.setattr('live.trader.scan_signals', lambda ts, *args:
        ({'BTCUSDT': ('LONG', 4, ts), 'ETHUSDT': ('SHORT', 4, ts)}, 0))
    try:
        trader.on_bar(START, rows(short=False), MARKS, now_ms=venue.now)
        assert trader.halted
        unknown_coi = venue.delayed_coi
        btc_stop = trader.orders['BTCUSDT']['SL']['client_order_index']
        previous = Decimal(venue.rows[btc_stop]['trigger_price'])
        trader.positions['BTCUSDT']['stop'] = float(previous + 1)
        trader._save()
        state = broker.account_state()
        assert not state.execution_changes_verified
        assert str(unknown_coi) in state.ledger_report['unresolved_orders']
        assert 'BTC' in state.ledger_report['verified_symbols']
        service_tick(venue, broker, trader)
        assert Decimal(venue.rows[btc_stop]['trigger_price']) == previous + 1
        assert venue.rows[btc_stop]['status'] == 'pending'
        assert sum(i.client_order_index == unknown_coi for i in venue.attempts) == 1
        if missing_kind == 'limit':
            eth_stop = trader.orders['ETHUSDT']['SL']['client_order_index']
            assert venue.rows[eth_stop]['status'] == 'pending'
            assert 'ETHUSDT' in trader.positions
    finally:
        broker.close()


@pytest.mark.parametrize('restart_before_late_record', [False, True])
def test_unknown_stop_closes_confirmed_exposure_without_resending_and_cancels_late_stop(tmp_path, restart_before_late_record):
    venue, broker, trader = setup(tmp_path, DelayedEvidenceVenue('stop-loss'))
    try:
        trader.on_bar(START, rows(), MARKS, now_ms=venue.now)
        unknown_coi = venue.delayed_coi
        assert 'ETHUSDT' in trader.positions
        assert unknown_coi not in venue.rows
        service_tick(venue, broker, trader)
        assert 'ETHUSDT' not in trader.positions
        assert not venue.position.get(0)
        assert sum(i.client_order_index == unknown_coi for i in venue.attempts) == 1
        exits = [i for i in venue.attempts if i.order_type == 'market' and i.reduce_only]
        assert len(exits) == 1
        assert broker.engine.store.load(unknown_coi)[1] is None
        state = broker.account_state()
        assert str(unknown_coi) in state.ledger_report['unresolved_orders']
        assert not state.execution_changes_verified  # Flat is not proof the SL was never accepted.

        if restart_before_late_record:
            broker.close()
            venue, broker, trader = setup(tmp_path, venue)
            service_tick(venue, broker, trader)
            assert not trader.positions
            assert sum(i.client_order_index == unknown_coi for i in venue.attempts) == 1
        venue.publish_late()
        service_tick(venue, broker, trader)
        assert venue.rows[unknown_coi]['status'] == 'canceled'
        assert broker.engine.store.load(unknown_coi)[1]['terminal']
        assert sum(i.client_order_index == unknown_coi for i in venue.attempts) == 1
        assert trader.halted and not trader.positions
    finally:
        broker.close()


def test_unknown_stop_never_emergency_closes_unreconciled_quantity(tmp_path):
    venue, broker, trader = setup(tmp_path, DelayedEvidenceVenue('stop-loss'))
    try:
        trader.on_bar(START, rows(), MARKS, now_ms=venue.now)
        known_qty = trader.positions['ETHUSDT']['qty']
        venue.position[0] -= Decimal('.01')  # An unexplained extra short is not owned evidence.
        state = broker.account_state()
        assert 'ETH' not in state.ledger_report['verified_symbols']
        service_tick(venue, broker, trader)
        assert trader.halted
        assert trader.positions['ETHUSDT']['qty'] == known_qty
        assert not any(i.order_type == 'market' and i.reduce_only for i in venue.attempts)
        assert sum(i.client_order_index == venue.delayed_coi for i in venue.attempts) == 1
    finally:
        broker.close()


def test_unknown_tp_late_open_row_is_adopted_with_original_stop_and_no_resubmit(tmp_path):
    venue, broker, trader = setup(tmp_path, DelayedEvidenceVenue('limit'))
    try:
        trader.on_bar(START, rows(), MARKS, now_ms=venue.now)
        unknown_tp = venue.delayed_coi
        known_stop = trader.orders['ETHUSDT']['SL']['client_order_index']
        service_tick(venue, broker, trader)
        assert venue.rows[known_stop]['status'] == 'pending'
        assert sum(i.client_order_index == unknown_tp for i in venue.attempts) == 1
        venue.publish_late()
        service_tick(venue, broker, trader)
        assert trader.orders['ETHUSDT']['TP']['client_order_index'] == unknown_tp
        assert trader.orders['ETHUSDT']['SL']['client_order_index'] == known_stop
        assert trader.pending_operation is None
        assert venue.rows[known_stop]['status'] == 'pending'
        assert sum(i.client_order_index == unknown_tp for i in venue.attempts) == 1
        assert not any(i.order_type == 'market' and i.reduce_only for i in venue.attempts)
    finally:
        broker.close()


def test_unknown_eth_order_does_not_hide_verified_btc_tp_and_breakeven(tmp_path, monkeypatch):
    venue, broker, trader = setup(tmp_path, DelayedEvidenceVenue('limit'))
    monkeypatch.setattr('live.trader.scan_signals', lambda ts, *args:
        ({'BTCUSDT': ('SHORT', 4, ts), 'ETHUSDT': ('SHORT', 4, ts)}, 0))
    try:
        trader.on_bar(START, rows(short=False), MARKS, now_ms=venue.now)
        old_qty = trader.positions['BTCUSDT']['qty']
        take_profit = trader.orders['BTCUSDT']['TP']['client_order_index']
        stop = trader.orders['BTCUSDT']['SL']['client_order_index']
        venue.now += 1_000
        venue.fill(take_profit, Decimal(str(old_qty)) / 2)
        service_tick(venue, broker, trader)
        assert trader.positions['BTCUSDT']['partial_taken'] is True
        assert trader.positions['BTCUSDT']['qty'] == pytest.approx(old_qty / 2)
        assert trader.positions['BTCUSDT']['stop'] == 100
        assert Decimal(venue.rows[stop]['trigger_price']) == Decimal('100')
        assert Decimal(venue.rows[stop]['initial_base_amount']) == Decimal(str(old_qty)) / 2
        assert sum(i.client_order_index == venue.delayed_coi for i in venue.attempts) == 1
        assert trader.halted
    finally:
        broker.close()


def test_unknown_stop_amendment_is_not_recreated_each_cycle_or_blocks_other_market(tmp_path, monkeypatch):
    venue, broker, trader = setup(tmp_path)
    monkeypatch.setattr('live.trader.scan_signals', lambda ts, *args:
        ({'BTCUSDT': ('SHORT', 4, ts), 'ETHUSDT': ('SHORT', 4, ts)}, 0))
    original_amend = venue.amend
    calls = []

    def unknown_eth_amend(market, order, base, price, trigger):
        calls.append((market, order, base, price, trigger))
        if market == 0:
            raise RuntimeError('test: amendment dispatch outcome unknown')
        return original_amend(market, order, base, price, trigger)

    monkeypatch.setattr(venue, 'amend', unknown_eth_amend)
    try:
        trader.on_bar(START, rows(short=False), MARKS, now_ms=venue.now)
        eth_stop = trader.orders['ETHUSDT']['SL']['client_order_index']
        btc_stop = trader.orders['BTCUSDT']['SL']['client_order_index']
        trader.positions['ETHUSDT']['stop'] = 103
        service_tick(venue, broker, trader)
        assert trader.halted
        assert Decimal(venue.rows[eth_stop]['trigger_price']) == Decimal('104')
        trader.positions['BTCUSDT']['stop'] = 103
        for _ in range(2):
            service_tick(venue, broker, trader)
        assert Decimal(venue.rows[btc_stop]['trigger_price']) == Decimal('103')
        assert sum(call[0] == 0 for call in calls) == 1
        assert venue.rows[eth_stop]['status'] == 'pending'
        assert trader.halted
    finally:
        broker.close()


class PartialNonterminalEntryVenue(Venue):
    def fill(self, coi, qty):
        row = self.rows[coi]
        if row['type'] == 'market' and not row['reduce_only']:
            qty = Decimal(qty) / 2
        return super().fill(coi, qty)


def test_verified_partial_nonterminal_entry_is_protected_without_new_entry_send(tmp_path):
    venue, broker, trader = setup(tmp_path, PartialNonterminalEntryVenue())
    try:
        trader.on_bar(START, rows(), MARKS, now_ms=venue.now)
        assert abs(venue.position[0]) > 0
        service_tick(venue, broker, trader)
        actual = abs(venue.position.get(0, Decimal(0)))
        live_stops = [row for row in venue.active_orders()
                      if row['market_index'] == 0 and row['type'] == 'stop-loss'
                      and row['reduce_only']]
        # Verified fills already create real exposure even if the entry's final
        # status is unpublished. Either confirmed flatness or armed coverage is
        # required; absence of a terminal entry is not a safe waiting state.
        assert actual == 0 or any(Decimal(row['remaining_base_amount']) >= actual
                                  for row in live_stops)
        assert sum(not intent.reduce_only for intent in venue.sent) == 1
        assert trader.halted
    finally:
        broker.close()


def test_late_amended_stop_after_emergency_flat_is_canceled_not_forgotten(tmp_path, monkeypatch):
    venue, broker, trader = setup(tmp_path)
    delayed = {}

    def disappear_during_amend(market, order, base, price, trigger):
        coi, row = next((coi, row) for coi, row in venue.rows.items() if row['order_index'] == order)
        delayed['coi'] = coi
        delayed['row'] = dict(row, initial_base_amount=str(Decimal(base) / 10000),
                              remaining_base_amount=str(Decimal(base) / 10000),
                              price=str(Decimal(price) / 100), trigger_price=str(Decimal(trigger) / 100))
        venue.rows.pop(coi)
        raise RuntimeError('test: amended stop evidence temporarily disappears')

    monkeypatch.setattr(venue, 'amend', disappear_during_amend)
    try:
        trader.on_bar(START, rows(), MARKS, now_ms=venue.now)
        trader.positions['ETHUSDT']['stop'] = 103
        service_tick(venue, broker, trader)
        service_tick(venue, broker, trader)
        assert not trader.positions and not venue.position.get(0)
        assert trader.unresolved_operations
        venue.rows[delayed['coi']] = delayed['row']
        service_tick(venue, broker, trader)
        assert venue.rows[delayed['coi']]['status'] == 'canceled'
        assert not any(row['type'] == 'stop-loss' for row in venue.active_orders())
        assert trader.halted
    finally:
        broker.close()

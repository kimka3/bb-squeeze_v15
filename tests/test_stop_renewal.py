"""Expiry renewal with the real broker/engine and an in-memory venue only."""
from copy import deepcopy
from decimal import Decimal
import json

import pytest

from test_strategy_integration import START, Venue, rows, setup
from live.execution_evidence import NotSubmitted, UnverifiedExecution, verify_order
from live.stop_renewal import RENEWAL_LEAD_MS, renewal_due, start_renewal, recover_renewal
from live.trader import BarOutcome


class ExpiryVenue(Venue):
    def __init__(self):
        super().__init__()
        self.events = []
        self.delay_new = False
        self.refuse_new = False
        self.pending_send = None
        self.cancel_unknown = False
        self.after_new = None

    def submit(self, intent, *args):
        self.events.append(('create', intent.client_order_index))
        if intent.order_type == 'stop-loss' and self.refuse_new:
            self.refuse_new = False
            raise NotSubmitted('request_budget_deferred', retryable=True)
        if intent.order_type == 'stop-loss' and self.delay_new:
            self.delay_new = False
            self.pending_send = (intent, args)
            raise RuntimeError('simulated lost send outcome')
        super().submit(intent, *args)
        self.rows[intent.client_order_index]['order_expiry'] = (
            0 if intent.order_type == 'market' else self.now + 28 * RENEWAL_LEAD_MS)
        if intent.order_type == 'stop-loss' and self.after_new:
            callback, self.after_new = self.after_new, None
            callback(intent)

    def publish_late(self):
        intent, args = self.pending_send
        self.pending_send = None
        # Venue publication of the original attempt is not another bot send.
        count = len(self.events)
        self.submit(intent, *args)
        self.events.pop(count)

    def cancel(self, market, oid):
        self.events.append(('cancel', oid))
        if self.cancel_unknown:
            raise RuntimeError('simulated lost cancel outcome')
        super().cancel(market, oid)


def opened(tmp_path):
    venue, broker, trader = setup(tmp_path, ExpiryVenue())
    trader.on_bar(START, rows(), {'BTCUSDT': 100, 'ETHUSDT': 100}, now_ms=venue.now)
    old = trader.orders['ETHUSDT']['SL']['client_order_index']
    expiry = venue.now + RENEWAL_LEAD_MS // 2
    venue.rows[old]['order_expiry'] = expiry
    broker.account_state()
    trader.orders['ETHUSDT']['SL']['order_expiry_ms'] = expiry
    trader._save()
    broker.poll()
    venue.events.clear()
    return venue, broker, trader, old


def start(venue, trader):
    out = BarOutcome(venue.now, 300)
    return start_renewal(trader, 'ETHUSDT', venue.now, out), out


def recover(venue, trader):
    trader.broker.poll()
    return recover_renewal(trader, trader.pending_operation, BarOutcome(venue.now, 300),
                           now_ms=venue.now, fresh_symbols={'ETHUSDT'})


def test_due_requires_observed_future_expiry():
    now = 1_800_000_000_000
    for expiry in (None, 0, -1, True, '1800000000000', now, now-1, now+RENEWAL_LEAD_MS+1):
        assert not renewal_due({'order_expiry_ms': expiry}, now)
    assert renewal_due({'order_expiry_ms': now+1}, now)
    assert renewal_due({'order_expiry_ms': now+RENEWAL_LEAD_MS}, now)


def test_same_stop_terms_renew_before_old_cancel_and_keep_tp(tmp_path):
    venue, broker, trader, old = opened(tmp_path)
    try:
        original = deepcopy(trader.orders['ETHUSDT']['SL'])
        tp = trader.orders['ETHUSDT']['TP']['client_order_index']
        assert start(venue, trader)[0]
        new = trader.orders['ETHUSDT']['SL']['client_order_index']
        assert new != old and trader.pending_operation is None
        assert venue.events == [('create', new), ('cancel', original['order_index'])]
        assert venue.rows[old]['status'] == 'canceled'
        assert venue.rows[new]['status'] == 'pending'
        assert venue.rows[new]['trigger_price'] == venue.rows[old]['trigger_price']
        assert venue.rows[new]['initial_base_amount'] == venue.rows[old]['initial_base_amount']
        assert venue.rows[new]['reduce_only'] is True
        assert trader.orders['ETHUSDT']['TP']['client_order_index'] == tp
        assert not start(venue, trader)[0]  # no repeated renewal of fresh expiry
    finally:
        broker.close()


@pytest.mark.parametrize('mode', ['missing', 'unchanged', 'shorter', 'not_armed'])
def test_replacement_without_verified_extended_expiry_never_cancels_old(tmp_path, mode):
    venue, broker, trader, old = opened(tmp_path)
    def distort(intent):
        row = venue.rows[intent.client_order_index]
        if mode == 'missing': row.pop('order_expiry')
        elif mode == 'unchanged': row['order_expiry'] = venue.rows[old]['order_expiry']
        elif mode == 'shorter': row['order_expiry'] = venue.now+1
        else: row['trigger_status'] = 'parent-order'
    venue.after_new = distort
    try:
        with pytest.raises(UnverifiedExecution):
            start(venue, trader)
        assert not any(e[0] == 'cancel' for e in venue.events)
        assert venue.rows[old]['status'] == 'pending'
        assert trader.pending_operation['kind'] == 'stop_renewal'
    finally:
        broker.close()


def test_unknown_new_order_survives_restart_without_second_submission(tmp_path):
    venue, broker, trader, old = opened(tmp_path)
    venue.delay_new = True
    with pytest.raises(UnverifiedExecution): start(venue, trader)
    op = deepcopy(trader.pending_operation)
    assert venue.events == [('create', op['coi'])]
    trader._save()
    broker.close()
    venue, broker, trader = setup(tmp_path, venue)
    try:
        with pytest.raises(UnverifiedExecution): recover(venue, trader)
        assert venue.events == [('create', op['coi'])]
        assert venue.rows[old]['status'] == 'pending'
        venue.publish_late()
        recover(venue, trader)
        assert trader.pending_operation is None
        assert sum(event[0] == 'create' for event in venue.events) == 1
        assert venue.rows[old]['status'] == 'canceled'
    finally:
        broker.close()


def test_known_not_submitted_can_retry_same_id_only(tmp_path):
    venue, broker, trader, old = opened(tmp_path)
    venue.refuse_new = True
    try:
        with pytest.raises(NotSubmitted): start(venue, trader)
        coi = trader.pending_operation['coi']
        assert broker.engine.store.known_not_submitted(coi)
        assert venue.rows[old]['status'] == 'pending'
        recover(venue, trader)
        assert [e for e in venue.events if e[0] == 'create'] == [('create', coi), ('create', coi)]
        assert trader.pending_operation is None
    finally:
        broker.close()


def test_unknown_old_cancel_keeps_both_ids_and_is_not_resent(tmp_path):
    venue, broker, trader, old = opened(tmp_path)
    venue.cancel_unknown = True
    try:
        with pytest.raises(UnverifiedExecution): start(venue, trader)
        op = deepcopy(trader.pending_operation)
        assert op['phase'] == 'cancel_old'
        assert trader.orders['ETHUSDT']['SL']['client_order_index'] == op['coi']
        before = list(venue.events)
        with pytest.raises(UnverifiedExecution): recover(venue, trader)
        assert venue.events == before
        venue.cancel_unknown = False
        Venue.cancel(venue, 0, op['old_ref']['order_index'])  # late venue completion
        recover(venue, trader)
        assert trader.pending_operation is None
        assert len([e for e in venue.events if e[0] == 'cancel']) == 1
    finally:
        broker.close()


def test_flat_then_late_new_order_cleans_both_exact_ids(tmp_path):
    venue, broker, trader, old = opened(tmp_path)
    venue.delay_new = True
    try:
        with pytest.raises(UnverifiedExecution): start(venue, trader)
        new = trader.pending_operation['coi']
        venue.fill(old, Decimal(venue.rows[old]['initial_base_amount']))
        with pytest.raises(UnverifiedExecution): recover(venue, trader)
        assert trader.pending_operation and not any(e[0] == 'cancel' for e in venue.events)
        venue.publish_late()
        recover(venue, trader)
        assert trader.pending_operation is None
        assert venue.rows[new]['status'] == 'canceled'
        assert venue.rows[old]['status'] == 'filled'
        assert sum(e[0] == 'create' for e in venue.events) == 1
    finally:
        broker.close()


def test_partial_tp_during_new_create_waits_then_tightens_new_before_cancel(tmp_path):
    venue, broker, trader, old = opened(tmp_path)
    tp = trader.orders['ETHUSDT']['TP']['client_order_index']
    venue.after_new = lambda _: venue.fill(tp, Decimal(venue.rows[tp]['initial_base_amount']))
    try:
        start(venue, trader)
        assert trader.pending_operation is not None
        assert not any(e[0] == 'cancel' for e in venue.events)
        trader._sync_fills(broker.account_state())
        recover(venue, trader)
        new = trader.orders['ETHUSDT']['SL']['client_order_index']
        assert trader.pending_operation is None
        assert Decimal(venue.rows[new]['trigger_price']) == 100
        assert Decimal(venue.rows[new]['initial_base_amount']) == Decimal(str(trader.positions['ETHUSDT']['qty']))
        assert venue.rows[old]['status'] == 'canceled'
    finally:
        broker.close()


def test_write_ahead_identity_survives_crash_before_dispatch(tmp_path, monkeypatch):
    venue, broker, trader, old = opened(tmp_path)
    real = broker.place_stop
    def crash(*args, **kwargs):
        raise KeyboardInterrupt('simulated process death before create')
    monkeypatch.setattr(broker, 'place_stop', crash)
    try:
        with pytest.raises(KeyboardInterrupt): start(venue, trader)
        op = deepcopy(trader.pending_operation)
        assert trader.journal.load_snapshot()['pending_operation'] == op
        assert venue.events == []
        monkeypatch.setattr(broker, 'place_stop', real)
        recover(venue, trader)
        assert venue.events[0] == ('create', op['coi'])
        assert trader.pending_operation is None
    finally:
        broker.close()


def test_replacement_ack_lost_before_snapshot_recovers_existing_order(tmp_path, monkeypatch):
    venue, broker, trader, old = opened(tmp_path)
    real = broker.place_stop
    def crash(*args, **kwargs):
        real(*args, **kwargs)
        raise KeyboardInterrupt('simulated process death after accepted new stop')
    monkeypatch.setattr(broker, 'place_stop', crash)
    try:
        with pytest.raises(KeyboardInterrupt): start(venue, trader)
        assert 'new_ref' not in trader.pending_operation
        before = list(venue.events)
        monkeypatch.setattr(broker, 'place_stop', real)
        recover(venue, trader)
        assert len([e for e in venue.events if e[0] == 'create']) == 1
        assert venue.events[:len(before)] == before
        assert trader.pending_operation is None
    finally:
        broker.close()


@pytest.mark.parametrize('after_cancel', [False, True])
def test_crash_around_old_cancel_restores_replacement_without_duplicate(tmp_path, monkeypatch, after_cancel):
    venue, broker, trader, old = opened(tmp_path)
    real = broker.cancel
    def crash(*args, **kwargs):
        if after_cancel:
            real(*args, **kwargs)
        raise KeyboardInterrupt('simulated process death around old cancel')
    monkeypatch.setattr(broker, 'cancel', crash)
    try:
        with pytest.raises(KeyboardInterrupt): start(venue, trader)
        snapshot = trader.journal.load_snapshot()
        op = snapshot['pending_operation']
        assert op['phase'] == 'cancel_old'
        assert snapshot['orders']['ETHUSDT']['SL']['client_order_index'] == op['coi']
        monkeypatch.setattr(broker, 'cancel', real)
        recover(venue, trader)
        assert trader.pending_operation is None
        assert len([e for e in venue.events if e[0] == 'create']) == 1
        assert len([e for e in venue.events if e[0] == 'cancel']) == 1
    finally:
        broker.close()


def test_fresh_mark_required_and_existing_halt_is_never_cleared(tmp_path):
    venue, broker, trader, old = opened(tmp_path)
    try:
        broker._mark_sample_ms.clear()
        trader.halted = True
        assert start(venue, trader)[0]
        assert trader.pending_operation is not None and venue.events == []
        recover(venue, trader)
        assert trader.pending_operation is None
        assert trader.halted is True
    finally:
        broker.close()


def test_identity_collision_and_looser_target_do_not_cancel_old(tmp_path, monkeypatch):
    venue, broker, trader, old = opened(tmp_path)
    original = trader._coi
    try:
        monkeypatch.setattr(trader, '_coi', lambda *a, **k: old)
        with pytest.raises(UnverifiedExecution, match='collision'): start(venue, trader)
        assert venue.events == []
        monkeypatch.setattr(trader, '_coi', original)
        trader.positions['ETHUSDT']['stop'] += 1  # short stop would be loosened
        with pytest.raises(UnverifiedExecution, match='loosen'): start(venue, trader)
        assert venue.events == []
        assert venue.rows[old]['status'] == 'pending'
    finally:
        broker.close()


def test_new_cycle_cannot_recover_old_rollover(tmp_path):
    venue, broker, trader, old = opened(tmp_path)
    venue.delay_new = True
    try:
        with pytest.raises(UnverifiedExecution): start(venue, trader)
        trader.positions['ETHUSDT']['entry_ms'] += 1
        with pytest.raises(UnverifiedExecution, match='different position cycle'): recover(venue, trader)
        assert not any(e[0] == 'cancel' for e in venue.events)
    finally:
        broker.close()


def test_legacy_snapshot_backfills_observed_expiry_then_renews(tmp_path):
    venue, broker, trader, old = opened(tmp_path)
    try:
        trader.orders['ETHUSDT']['SL'].pop('order_expiry_ms')
        assert start(venue, trader)[0]
        assert venue.rows[old]['status'] == 'canceled'
    finally:
        broker.close()


def test_missing_raw_expiry_never_infers_a_new_expiry(tmp_path):
    venue, broker, trader, old = opened(tmp_path)
    try:
        trader.orders['ETHUSDT']['SL'].pop('order_expiry_ms')
        venue.rows[old].pop('order_expiry')
        assert not start(venue, trader)[0]
        assert venue.events == []
        assert trader.orders['ETHUSDT']['SL'].get('order_expiry_ms') is None
    finally:
        broker.close()


def test_legacy_terminal_result_can_gain_expiry_metadata(tmp_path):
    venue, broker, trader, old = opened(tmp_path)
    try:
        entry = next(i for i in venue.sent if not i.reduce_only)
        intent, result = broker.engine.store.load(entry.client_order_index)
        result.pop('order_expiry_ms')
        with broker.engine.store.db:
            broker.engine.store.db.execute('UPDATE intents SET result=? WHERE coi=?',
                (json.dumps(result), str(entry.client_order_index)))
        refreshed = broker.engine.store.record(entry.client_order_index, venue.rows[entry.client_order_index])
        assert refreshed['terminal'] and refreshed['order_expiry_ms'] is None
        broker.cancel(trader.markets['ETHUSDT'], trader._order_ref(trader.orders['ETHUSDT']['SL']))
        _, terminal = broker.engine.store.load(old)
        terminal['order_expiry_ms'] = None
        with broker.engine.store.db:
            broker.engine.store.db.execute('UPDATE intents SET result=? WHERE coi=?',
                (json.dumps(terminal), str(old)))
        assert broker.engine.store.record(old, venue.rows[old])['order_expiry_ms'] == venue.rows[old]['order_expiry']
        for bad in (True, '123', -1, 1.5):
            row = dict(venue.rows[entry.client_order_index], order_expiry=bad)
            with pytest.raises(UnverifiedExecution, match='expiry'): verify_order(row, intent)
    finally:
        broker.close()

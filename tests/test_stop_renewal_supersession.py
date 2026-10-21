"""Renewal supersession requires positive non-dispatch and preserves every ID."""
from copy import deepcopy
from decimal import Decimal
import sqlite3

import pytest

from test_stop_renewal import opened, recover, start
from test_strategy_integration import setup
from live.execution_evidence import NotSubmitted, UnverifiedExecution


def refused(venue, trader):
    venue.refuse_new = True
    with pytest.raises(NotSubmitted):
        start(venue, trader)
    return trader.pending_operation['coi']


def tp_fill(venue, broker, trader, fraction):
    tp = trader.orders['ETHUSDT']['TP']['client_order_index']
    venue.now += 1000
    venue.fill(tp, Decimal(venue.rows[tp]['initial_base_amount']) * fraction)
    trader._sync_fills(broker.account_state())
    broker.poll()


@pytest.mark.parametrize('fraction', [Decimal('.5'), Decimal('1')])
def test_refused_renewal_then_tp_uses_new_id_current_quantity_and_preserves_old(tmp_path, fraction):
    venue, broker, trader, old = opened(tmp_path)
    try:
        retired = refused(venue, trader)
        original = deepcopy(broker.engine.store.load(retired))
        tp_fill(venue, broker, trader, fraction)
        recover(venue, trader)
        new = trader.orders['ETHUSDT']['SL']['client_order_index']
        assert new not in (old, retired)
        assert trader.pending_operation is None and not trader.halted
        assert broker.engine.store.load(retired) == original
        assert broker.engine.store.known_not_submitted(retired)
        assert Decimal(venue.rows[new]['initial_base_amount']) == Decimal(str(trader.positions['ETHUSDT']['qty']))
        assert Decimal(venue.rows[new]['trigger_price']) == Decimal(str(trader.positions['ETHUSDT']['stop']))
        assert venue.rows[old]['status'] == 'canceled'
        assert [event for event in venue.events if event[0] == 'create'] == [('create', retired), ('create', new)]
        assert broker.account_state().execution_changes_verified
        link = broker.engine.store.supersessions()[retired]
        assert link['new_coi'] == new and link['proof']['entry_cycle'] == broker._cycle('ETHUSDT')
        with pytest.raises(UnverifiedExecution, match='superseded'):
            broker.engine.create(original[0], 4, 2, stop_quantity=original[0].quantity,
                                 retry_not_submitted=True)
        with pytest.raises(UnverifiedExecution):
            broker.engine.store.mark_dispatching(retired)
    finally:
        broker.close()


def test_tighter_stop_without_quantity_change_uses_new_immutable_intent(tmp_path):
    venue, broker, trader, old = opened(tmp_path)
    try:
        retired = refused(venue, trader)
        original = deepcopy(broker.engine.store.load(retired))
        trader.positions['ETHUSDT']['stop'] -= 1
        recover(venue, trader)
        new = trader.orders['ETHUSDT']['SL']['client_order_index']
        assert new != retired and broker.engine.store.load(retired) == original
        assert Decimal(venue.rows[new]['trigger_price']) == Decimal(original[0].trigger_price) - 1
        assert venue.rows[old]['status'] == 'canceled'
    finally:
        broker.close()


@pytest.mark.parametrize('crash_at', ['before_link', 'after_link', 'before_new_dispatch', 'after_new_dispatch'])
def test_crash_points_preserve_supersession_identity_and_do_not_duplicate(tmp_path, monkeypatch, crash_at):
    venue, broker, trader, old = opened(tmp_path)
    retired = refused(venue, trader)
    tp_fill(venue, broker, trader, Decimal('1'))
    real_link = broker.engine.store.supersede_not_submitted
    real_place = broker.place_stop
    def link(*args):
        if crash_at == 'before_link':
            raise KeyboardInterrupt('before atomic link')
        real_link(*args)
        if crash_at == 'after_link':
            raise KeyboardInterrupt('after atomic link')
    def place(*args, **kwargs):
        if crash_at == 'before_new_dispatch':
            raise KeyboardInterrupt('after durable new intent before dispatch')
        result = real_place(*args, **kwargs)
        if crash_at == 'after_new_dispatch':
            raise KeyboardInterrupt('after accepted replacement')
        return result
    monkeypatch.setattr(broker.engine.store, 'supersede_not_submitted', link)
    monkeypatch.setattr(broker, 'place_stop', place)
    with pytest.raises(KeyboardInterrupt):
        recover(venue, trader)
    snap = trader.journal.load_snapshot()
    op = snap['pending_operation']
    new = op['replacement_plan']['intent']['client_order_index'] if 'replacement_plan' in op else op['coi']
    broker.close()
    venue, broker, trader = setup(tmp_path, venue)
    try:
        recover(venue, trader)
        assert trader.pending_operation is None
        assert trader.orders['ETHUSDT']['SL']['client_order_index'] == new
        assert broker.engine.store.supersessions()[retired]['new_coi'] == new
        assert [e for e in venue.events if e[0] == 'create'] == [('create', retired), ('create', new)]
        assert len([e for e in venue.events if e[0] == 'cancel']) == 1
        assert venue.rows[old]['status'] == 'canceled'
    finally:
        broker.close()


def test_unknown_renewal_after_tp_never_creates_superseding_order(tmp_path):
    venue, broker, trader, old = opened(tmp_path)
    try:
        venue.delay_new = True
        with pytest.raises(UnverifiedExecution):
            start(venue, trader)
        unknown = trader.pending_operation['coi']
        tp_fill(venue, broker, trader, Decimal('1'))
        before = list(venue.events)
        with pytest.raises(UnverifiedExecution):
            recover(venue, trader)
        assert broker.engine.store.supersessions() == {} and venue.events == before
        assert trader.pending_operation['coi'] == unknown
        assert venue.rows[old]['status'] == 'pending'
    finally:
        broker.close()


@pytest.mark.parametrize('refusal', ['external_quantity', 'expiry', 'collision', 'nonretryable', 'looser_stop'])
def test_no_supersession_without_current_safe_evidence(tmp_path, monkeypatch, refusal):
    venue, broker, trader, old = opened(tmp_path)
    try:
        retired = refused(venue, trader)
        if refusal == 'external_quantity':
            venue.position[0] /= 2
        else:
            tp_fill(venue, broker, trader, Decimal('1'))
            if refusal == 'expiry':
                venue.now = venue.rows[old]['order_expiry']
            elif refusal == 'collision':
                monkeypatch.setattr(trader, '_coi', lambda *args, **kwargs: old)
            elif refusal == 'nonretryable':
                with broker.engine.store.db:
                    broker.engine.store.db.execute('UPDATE intent_dispatch SET retryable=0 WHERE coi=?', (str(retired),))
            else:
                trader.positions['ETHUSDT']['stop'] = trader.pending_operation['old_ref']['trigger'] + 1
        before = list(venue.events)
        with pytest.raises(UnverifiedExecution):
            recover(venue, trader)
        assert venue.events == before and not broker.engine.store.supersessions()
        assert venue.rows[old]['status'] == 'pending'
    finally:
        broker.close()


def test_plan_snapshot_failure_cannot_dispatch_replacement(tmp_path, monkeypatch):
    venue, broker, trader, old = opened(tmp_path)
    try:
        refused(venue, trader)
        tp_fill(venue, broker, trader, Decimal('1'))
        before = list(venue.events)
        monkeypatch.setattr(trader, '_save', lambda: (_ for _ in ()).throw(OSError('disk failure')))
        with pytest.raises(OSError):
            recover(venue, trader)
        assert venue.events == before and not broker.engine.store.supersessions()
        assert venue.rows[old]['status'] == 'pending'
    finally:
        broker.close()


def test_second_tp_fill_while_superseded_reservation_waits_forms_chain(tmp_path):
    venue, broker, trader, old = opened(tmp_path)
    try:
        first = refused(venue, trader)
        tp_fill(venue, broker, trader, Decimal('.5'))
        venue.refuse_new = True
        with pytest.raises(NotSubmitted):
            recover(venue, trader)
        second = trader.pending_operation['coi']
        assert second != first
        tp_fill(venue, broker, trader, Decimal('.5'))
        recover(venue, trader)
        final = trader.orders['ETHUSDT']['SL']['client_order_index']
        links = broker.engine.store.supersessions()
        assert links[first]['new_coi'] == second and links[second]['new_coi'] == final
        assert len(links) == 2 and final not in (first, second, old)
        assert broker.account_state().execution_changes_verified and not trader.halted
        assert len([e for e in venue.events if e[0] == 'create']) == 3
    finally:
        broker.close()


def test_full_service_recovers_reduced_renewal_without_spurious_halt(tmp_path):
    venue, broker, trader, old = opened(tmp_path)
    try:
        venue.refuse_new = True
        trader.service(now_ms=venue.now, marks={'ETHUSDT': 100})
        assert trader.unresolved_operations and not trader.halted
        first = next(iter(trader.unresolved_operations.values()))['coi']
        tp = trader.orders['ETHUSDT']['TP']['client_order_index']
        venue.now += 1000
        venue.fill(tp, Decimal(venue.rows[tp]['initial_base_amount']))
        for _ in range(2):
            broker.poll()
            trader.service(now_ms=venue.now, marks={'ETHUSDT': 100})
        assert not trader.unresolved_operations and trader.pending_operation is None and not trader.halted
        assert trader.orders['ETHUSDT']['SL']['client_order_index'] != first
        assert broker.account_state().execution_changes_verified
    finally:
        broker.close()


@pytest.mark.parametrize('fraction', [Decimal('.5'), Decimal('1')])
def test_service_restart_reconciles_tp_before_superseding_unsent_stop(tmp_path, fraction):
    venue, broker, trader, old = opened(tmp_path)
    venue.refuse_new = True
    trader.service(now_ms=venue.now, marks={'ETHUSDT': 100})
    assert len(trader.unresolved_operations) == 1 and not trader.halted
    retired = next(iter(trader.unresolved_operations.values()))['coi']
    original = deepcopy(broker.engine.store.load(retired))
    tp = trader.orders['ETHUSDT']['TP']['client_order_index']
    broker.close()
    # The native TP executes while the bot is offline. Restart must synchronize
    # its owned fills before constructing the replacement's smaller quantity.
    venue.now += 1000
    venue.fill(tp, Decimal(venue.rows[tp]['initial_base_amount']) * fraction)
    venue, broker, trader = setup(tmp_path, venue)
    try:
        assert trader._needs_recovery and trader.unresolved_operations
        for _ in range(3):
            broker.poll()
            trader.service(now_ms=venue.now, marks={'ETHUSDT': 100})
        new = trader.orders['ETHUSDT']['SL']['client_order_index']
        assert new not in (old, retired)
        assert not trader.halted and not trader.unresolved_operations and trader.pending_operation is None
        assert broker.engine.store.load(retired) == original
        assert broker.engine.store.supersessions()[retired]['new_coi'] == new
        assert Decimal(venue.rows[new]['initial_base_amount']) == abs(venue.position[0])
        assert Decimal(venue.rows[new]['trigger_price']) == Decimal(str(trader.positions['ETHUSDT']['stop']))
        assert venue.rows[old]['status'] == 'canceled'
        assert [event for event in venue.events if event[0] == 'create'] == [('create', retired), ('create', new)]
        assert broker.account_state().execution_changes_verified
    finally:
        broker.close()


def test_atomic_link_failure_rolls_back_new_reservation_and_recovers_same_plan(tmp_path):
    venue, broker, trader, old = opened(tmp_path)
    try:
        retired = refused(venue, trader)
        tp_fill(venue, broker, trader, Decimal('1'))
        broker.engine.store.db.execute("CREATE TRIGGER reject_link BEFORE INSERT ON intent_supersessions BEGIN SELECT RAISE(ABORT, 'simulated disk transaction failure'); END")
        with pytest.raises(sqlite3.IntegrityError):
            recover(venue, trader)
        new = trader.pending_operation['replacement_plan']['intent']['client_order_index']
        assert broker.engine.store.load(new) is None and not broker.engine.store.supersessions()
        assert venue.events == [('create', retired)] and venue.rows[old]['status'] == 'pending'
        broker.engine.store.db.execute('DROP TRIGGER reject_link')
        recover(venue, trader)
        assert trader.orders['ETHUSDT']['SL']['client_order_index'] == new
        assert broker.engine.store.supersessions()[retired]['new_coi'] == new
    finally:
        broker.close()


@pytest.mark.parametrize('after_link_state', ['flat', 'old_expired', 'prior_halt'])
def test_changed_exposure_or_halt_after_durable_link_is_rechecked(tmp_path, monkeypatch, after_link_state):
    venue, broker, trader, old = opened(tmp_path)
    try:
        retired = refused(venue, trader)
        tp_fill(venue, broker, trader, Decimal('1'))
        real = broker.engine.store.supersede_not_submitted
        def interrupted(*args):
            real(*args)
            raise KeyboardInterrupt('crash after link, before snapshot advance')
        monkeypatch.setattr(broker.engine.store, 'supersede_not_submitted', interrupted)
        with pytest.raises(KeyboardInterrupt):
            recover(venue, trader)
        new = broker.engine.store.supersessions()[retired]['new_coi']
        monkeypatch.setattr(broker.engine.store, 'supersede_not_submitted', real)
        if after_link_state == 'flat':
            venue.fill(old, abs(venue.position[0]))
            recover(venue, trader)
            assert trader.pending_operation is None and venue.position[0] == 0
            assert new not in venue.rows and broker.engine.store.known_not_submitted(new)
            assert retired in broker.engine.store.supersessions()
        elif after_link_state == 'old_expired':
            venue.now = venue.rows[old]['order_expiry']
            with pytest.raises(UnverifiedExecution, match='unexpired'):
                recover(venue, trader)
            assert new not in venue.rows and venue.rows[old]['status'] == 'pending'
        else:
            trader._halt('prior independent safety issue')
            recover(venue, trader)
            assert trader.pending_operation is None and trader.halted
            assert trader.halt_reasons == ['prior independent safety issue']
            assert trader.orders['ETHUSDT']['SL']['client_order_index'] == new
    finally:
        broker.close()


def test_late_venue_row_for_superseded_id_is_a_hard_conflict(tmp_path):
    venue, broker, trader, old = opened(tmp_path)
    try:
        retired = refused(venue, trader)
        retired_intent = broker.engine.store.load(retired)[0]
        tp_fill(venue, broker, trader, Decimal('1'))
        recover(venue, trader)
        new = trader.orders['ETHUSDT']['SL']['client_order_index']
        # An impossible late appearance contradicts the original non-dispatch
        # evidence; neither replacement success nor flatness can forgive it.
        venue.submit(retired_intent, 1, 1, 1)
        with pytest.raises(UnverifiedExecution, match='confirmed local non-submission'):
            broker.account_state()
        assert venue.rows[new]['status'] == 'pending'
    finally:
        broker.close()

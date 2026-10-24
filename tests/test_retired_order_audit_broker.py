"""Broker regressions for bounded retired-ID reads, using an offline venue only.

The latency/count comparison below is deterministic simulated work, not a VPS
benchmark or a claim that the production account was already overloaded.
"""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from data_io import START
from live.execution_evidence import NotSubmitted, UnverifiedExecution
from live.markets import Market
from live.request_budget import RequestDeferred
from live.strategy_broker import StrategyBroker
from test_strategy_integration import Venue
from test_strategy_broker_uncertainty import enter, missing


INTERVAL_MS = 60_100


def make_broker(tmp_path, venue=None):
    venue = venue or Venue()
    markets = {symbol: Market(symbol[:-4], market, 4, 2, .0001, 0, .06, .1, 'active')
               for symbol, market in [('BTCUSDT', 1), ('ETHUSDT', 0)]}
    broker = StrategyBroker(venue, markets, tmp_path / 'execution', start_ms=START,
                            clock=lambda: venue.now / 1000, writes_enabled=True, timeout=0)
    return venue, broker, markets


@pytest.fixture
def setup(tmp_path):
    venue, broker, markets = make_broker(tmp_path)
    try:
        yield venue, broker, markets
    finally:
        broker.close()


def unsent(broker, markets, coi, role='ENTRY', *, symbol='ETHUSDT', retryable=True):
    intent = missing(broker, markets, coi, symbol, role)
    broker.engine.store.mark_dispatching(coi)
    broker.engine.store.mark_not_submitted(coi, NotSubmitted('request_budget_deferred', retryable=retryable))
    return intent


def observe_lookups(venue, monkeypatch):
    calls = []
    original = venue.order

    def order(coi):
        calls.append(coi)
        return original(coi)

    monkeypatch.setattr(venue, 'order', order)
    return calls


def audit(broker):
    broker.account_state()
    broker.audit_retired_orders()
    return broker.last_report['retired_order_audit']


def exchange_row(intent, *, status='canceled'):
    return {'owner_account_index': intent.account_index, 'market_index': intent.market_index,
            'client_order_index': intent.client_order_index, 'order_index': intent.client_order_index + 1000,
            'is_ask': intent.is_ask, 'reduce_only': intent.reduce_only, 'type': intent.order_type,
            'time_in_force': intent.time_in_force, 'price': intent.price,
            'trigger_price': intent.trigger_price, 'initial_base_amount': intent.quantity,
            'filled_base_amount': '0', 'filled_quote_amount': '0', 'remaining_base_amount': '0',
            'status': status, 'trigger_status': 'na', 'trigger_time': 0, 'parent_order_index': 0}


def test_many_unsent_ids_leave_foreground_and_have_one_bounded_measured_read(setup, monkeypatch):
    venue, broker, markets = setup
    ids = list(range(100, 200))
    for coi in ids:
        unsent(broker, markets, coi)
    original = {coi: deepcopy(broker.engine.store.load(coi)) for coi in ids}
    calls = observe_lookups(venue, monkeypatch)
    # Synthetic fixed request latency makes measurement assertions repeatable.
    import live.order_lookup_audit as audit_module
    elapsed = [0.0]
    monkeypatch.setattr(audit_module.time, 'monotonic', lambda: elapsed[0])
    underlying = venue.order

    def measured(coi):
        elapsed[0] += .125
        return underlying(coi)

    monkeypatch.setattr(venue, 'order', measured)
    for _ in range(10):
        state = broker.account_state()
        assert state.execution_changes_verified and not state.positions
        assert state.ledger_report['fill_count'] == 0
    assert calls == []  # Mandatory reconciliation does not do optional reads.
    for _ in range(10):
        report = audit(broker)
    assert len(calls) == 1 and calls[0] in ids
    assert report['candidate_count'] == 100 and report['checked_count'] == 1
    assert report['background']['logical_calls'] == 1
    assert report['background']['request_seconds']['total'] == pytest.approx(.125)
    assert report['foreground']['logical_calls'] == 0
    assert report['background']['http_attempts'] is None  # Venue has no real HTTP telemetry.
    assert {coi: broker.engine.store.load(coi) for coi in ids} == original
    assert venue.sent == [] and broker.ledger.events('trade') == []


def test_audit_rotates_without_burst_and_restart_preserves_due_time(tmp_path, monkeypatch):
    venue, broker, markets = make_broker(tmp_path)
    try:
        for coi in (10, 20, 30):
            unsent(broker, markets, coi)
        calls = observe_lookups(venue, monkeypatch)
        audit(broker)
        assert calls == [10]
        broker.close()
        venue, broker, markets = make_broker(tmp_path, venue)
        audit(broker)
        assert calls == [10]
        venue.now += INTERVAL_MS - 1
        audit(broker)
        assert calls == [10]
        venue.now += 1
        audit(broker)
        assert calls == [10, 20]
        venue.now += INTERVAL_MS
        audit(broker)
        assert calls == [10, 20, 30]
        venue.now += INTERVAL_MS
        audit(broker)
        assert calls == [10, 20, 30, 10]
    finally:
        broker.close()


@pytest.mark.parametrize('dispatch_state', ['reserved', 'dispatching', 'acknowledged', 'legacy'])
def test_ambiguous_orders_are_always_foreground_and_never_retired(setup, monkeypatch, dispatch_state):
    venue, broker, markets = setup
    missing(broker, markets, 10, 'ETHUSDT', 'ENTRY')
    if dispatch_state in ('dispatching', 'acknowledged'):
        broker.engine.store.mark_dispatching(10)
    if dispatch_state == 'acknowledged':
        broker.engine.store.mark_acknowledged(10)
    if dispatch_state == 'legacy':
        with broker.db:
            broker.db.execute('DELETE FROM intent_dispatch WHERE coi=?', ('10',))
    calls = observe_lookups(venue, monkeypatch)
    for _ in range(3):
        state = broker.account_state()
        assert not state.execution_changes_verified
        assert state.ledger_report['unresolved_orders']['10']['reason'] == 'owned_order_outcome_absent'
        broker.audit_retired_orders()
    assert calls == [10, 10, 10]
    assert broker.last_report['retired_order_audit']['candidate_count'] == 0
    assert broker.engine.store.load(10)[1] is None and venue.sent == []


@pytest.mark.parametrize('role', ['SL', 'TP', 'EXIT'])
def test_current_retryable_protection_and_exit_ids_keep_immediate_reads(setup, monkeypatch, role):
    venue, broker, markets = setup
    enter(broker, markets)
    unsent(broker, markets, 11, role)
    calls = observe_lookups(venue, monkeypatch)
    for _ in range(3):
        broker.account_state()
        broker.audit_retired_orders()
    assert calls == [11, 11, 11]
    assert broker.last_report['retired_order_audit']['candidate_count'] == 0
    assert broker.engine.store.known_not_submitted(11)
    assert broker.engine.store.load(11)[1] is None and len(venue.sent) == 1


@pytest.mark.parametrize('role', ['SL', 'TP', 'EXIT'])
def test_nonretryable_unsent_ids_are_audited_without_fabricated_results(setup, monkeypatch, role):
    venue, broker, markets = setup
    unsent(broker, markets, 10)
    unsent(broker, markets, 11, role, retryable=False)
    calls = observe_lookups(venue, monkeypatch)
    for _ in range(3):
        audit(broker)
    assert calls == [10]
    venue.now += INTERVAL_MS
    report = audit(broker)
    assert calls == [10, 11] and report['candidate_count'] == 2
    assert broker.engine.store.load(11)[1] is None
    assert broker.last_report['not_submitted_orders']['11']['retryable'] is False
    assert venue.sent == []


@pytest.mark.parametrize('evidence', ['active', 'trade'])
def test_all_retired_ids_still_checked_against_active_orders_and_collected_trades(setup, monkeypatch, evidence):
    venue, broker, markets = setup
    unsent(broker, markets, 10)
    intent = unsent(broker, markets, 20)
    calls = observe_lookups(venue, monkeypatch)
    audit(broker)
    assert calls == [10]  # 20 has never received its scheduled individual read.
    if evidence == 'active':
        venue.rows[20] = exchange_row(intent, status='open')
    else:
        venue.trades.append({'trade_id': 1, 'type': 'trade', 'market_id': 0, 'size': '1',
            'price': '100', 'usd_amount': '100', 'ask_account_id': 200, 'bid_account_id': 100,
            'is_maker_ask': True, 'bid_client_id': 20, 'bid_id': 1020,
            'timestamp': venue.now, 'taker_fee': 0, 'integrator_taker_fee': 0})
    with pytest.raises(UnverifiedExecution, match='confirmed local non-submission'):
        broker.account_state()
    assert calls == [10]
    assert '20' in broker.last_report['retired_order_audit']['conflicts']
    assert broker.engine.store.load(20)[1] is None


def test_late_inactive_order_conflict_latches_across_cadence_and_restart(tmp_path, monkeypatch):
    venue, broker, markets = make_broker(tmp_path)
    try:
        intent = unsent(broker, markets, 10)
        calls = observe_lookups(venue, monkeypatch)
        audit(broker)
        venue.rows[10] = exchange_row(intent)
        venue.now += INTERVAL_MS
        broker.account_state()
        with pytest.raises(UnverifiedExecution, match='confirmed local non-submission'):
            broker.audit_retired_orders()
        assert calls == [10, 10] and broker.engine.store.load(10)[1] is None
        venue.rows.clear()  # Disappearing venue evidence cannot clear the latch.
        with pytest.raises(UnverifiedExecution, match='confirmed local non-submission'):
            broker.account_state()
        broker.close()
        venue, broker, markets = make_broker(tmp_path, venue)
        with pytest.raises(UnverifiedExecution, match='confirmed local non-submission'):
            broker.account_state()
        assert calls == [10, 10]
        assert '10' in broker.last_report['retired_order_audit']['conflicts']
    finally:
        broker.close()


def test_unresolved_current_protection_defers_optional_queries(setup, monkeypatch):
    venue, broker, markets = setup
    unsent(broker, markets, 20, symbol='BTCUSDT')
    enter(broker, markets)
    missing(broker, markets, 11, 'ETHUSDT', 'SL')
    calls = observe_lookups(venue, monkeypatch)
    state = broker.account_state()
    assert not state.execution_changes_verified and 'ETH' in state.ledger_report['verified_symbols']
    broker.audit_retired_orders()
    assert calls == [11]
    assert broker.last_report['retired_order_audit']['candidate_count'] == 1
    assert broker.last_report['retired_order_audit']['background']['logical_calls'] == 0
    # Independent owned reduction is still possible before optional work.
    fill = broker.market_order(markets['ETHUSDT'], 'ETHUSDT', 'LONG', 1, True, 12, 100)
    assert fill.qty == 1 and venue.position[0] == 0 and venue.sent[-1].reduce_only
    assert 20 not in calls


def test_entry_window_defers_background_but_keeps_foreground_and_entry_guard(setup, monkeypatch):
    venue, broker, markets = setup
    unsent(broker, markets, 10)
    calls = observe_lookups(venue, monkeypatch)
    broker.entry_deadline_ms = venue.now + 60_000
    report = audit(broker)
    assert calls == [] and report['deferred_reason'] == 'entry_window'
    broker.entry_deadline_ms = None
    broker.entry_guard = lambda: False
    with pytest.raises(ValueError, match='paused new entries'):
        broker.validate_entry(markets['BTCUSDT'], 'LONG', 1, 100, 0, 110)
    assert venue.sent == []
    audit(broker)
    assert calls == [10]


def test_background_budget_reserve_is_21_and_denial_does_not_block_current_state(setup, monkeypatch):
    venue, broker, markets = setup
    unsent(broker, markets, 10)
    calls = observe_lookups(venue, monkeypatch)
    assert broker.account_state().execution_changes_verified

    class Budget:
        blocked = True

        def __init__(self):
            self.calls = []

        def check(self, slots=1, consume=False):
            self.calls.append((slots, consume))
            if self.blocked:
                raise RequestDeferred(1)

    venue.budget = Budget()
    broker.audit_retired_orders()
    assert calls == [] and venue.budget.calls == [(21, False)]
    assert broker.last_report['retired_order_audit']['deferred_reason'] == 'request_budget'
    venue.budget.blocked = False
    broker.audit_retired_orders()
    assert calls == [10] and venue.budget.calls[-1] == (21, False)
    venue.budget.calls.clear()
    broker.validate_entry(markets['BTCUSDT'], 'LONG', 1, 100, 0, 110)
    assert venue.budget.calls == [(24, False)]


def test_prewrite_reconciliation_cannot_leave_audit_eligibility_after_failed_mutation(setup, monkeypatch):
    venue, broker, markets = setup
    unsent(broker, markets, 10)
    calls = observe_lookups(venue, monkeypatch)
    reconciliations = []
    original_state = broker.account_state

    def reconciled():
        state = original_state()
        reconciliations.append(state)
        assert broker._retired_candidates == {10: {'classification': 'unsent_entry'}}
        return state

    def fail_create(*args, **kwargs):
        assert reconciliations  # Fresh ownership was checked before attempting the mutation.
        raise OSError('controlled write failure after fresh read')

    monkeypatch.setattr(broker, 'account_state', reconciled)
    monkeypatch.setattr(broker.engine, 'create', fail_create)
    with pytest.raises(NotSubmitted):
        broker.market_order(markets['BTCUSDT'], 'BTCUSDT', 'LONG', 1, False, 20, 100)
    assert broker._retired_candidates is None
    broker.audit_retired_orders()
    assert calls == [] and venue.sent == []
    assert broker.last_report['retired_order_audit']['background']['logical_calls'] == 0


def test_failed_second_reconciliation_cannot_reuse_first_read_for_optional_work(setup, monkeypatch):
    venue, broker, markets = setup
    unsent(broker, markets, 10)
    calls = observe_lookups(venue, monkeypatch)
    original_once = broker._account_state_once
    attempts = []

    def read_once():
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            state, _ = original_once()
            assert broker._retired_candidates == {10: {'classification': 'unsent_entry'}}
            return state, True  # A mismatch forces the ordinary confirming read.
        raise OSError('controlled failure during confirming read')

    monkeypatch.setattr(broker, '_account_state_once', read_once)
    with pytest.raises(OSError, match='confirming read'):
        broker.account_state()
    assert attempts == [1, 2] and broker._retired_candidates is None
    broker.audit_retired_orders()
    assert calls == [] and broker.engine.store.load(10)[1] is None


def test_validated_superseded_stop_id_is_deferred_but_current_native_stop_stays_live(tmp_path, monkeypatch):
    from test_stop_renewal import opened, recover
    from test_stop_renewal_supersession import refused, tp_fill

    venue, broker, trader, old = opened(tmp_path)
    try:
        retired = refused(venue, trader)
        tp_fill(venue, broker, trader, Decimal('1'))
        recover(venue, trader)
        new = trader.orders['ETHUSDT']['SL']['client_order_index']
        calls = observe_lookups(venue, monkeypatch)
        for _ in range(3):
            state = broker.account_state()
            assert state.execution_changes_verified
            assert any(row['client_order_index'] == new and row['armed'] for row in state.active_orders['ETH'])
        assert retired not in calls
        broker.audit_retired_orders()
        assert calls.count(retired) == 1
        broker.account_state()
        broker.audit_retired_orders()
        assert calls.count(retired) == 1
        assert broker.engine.store.known_not_submitted(retired)
        assert broker.engine.store.load(retired)[1] is None
        assert venue.rows[new]['status'] == 'pending' and venue.rows[old]['status'] == 'canceled'
    finally:
        broker.close()


@pytest.mark.parametrize('failure,pending,now_seconds', [
    (None, None, 1000), ('poll', None, 1000), ('service', None, 1000), ('audit', None, 1000),
    (None, 'pending_operation', 1000), (None, 'unresolved_operations', 1000),
    (None, 'residual_exits', 1000), (None, 'orphan_orders', 1000),
    (None, None, 14390), (None, None, 14430), (None, None, 14389)])
def test_runner_services_current_risk_before_audit_and_routes_actual_audit_failures(tmp_path, monkeypatch, failure, pending, now_seconds):
    import live.run as run
    from live.config import LiveConfig

    events = []

    class StopSignal:
        stopped = False

        def is_set(self): return self.stopped
        def set(self): self.stopped = True
        def wait(self, seconds): self.stopped = True

    stop = StopSignal()
    monkeypatch.setattr(run, 'STOP_REQUESTED', stop)
    monkeypatch.setattr(run, 'time', SimpleNamespace(time=lambda: float(now_seconds)))

    def stage(name):
        events.append(name)
        if name == 'service' and pending:
            setattr(trader, pending, {'ETHUSDT': 'pending protection work'})
        if failure == name:
            raise OSError('controlled offline read failure')

    class Broker:
        def poll(self):
            stage('poll')
            return []

        def audit_retired_orders(self, *, blocked_reason=None):
            events.append(('audit_wait', blocked_reason))
            stage('audit')

    class Controller:
        def failure(self, error, stream): events.append(('failure', stream))
        def cycle(self, trader, coverage, **kwargs): events.append(('cycle', kwargs))

    class Future:
        def done(self): return False

    class Pool:
        def __init__(self, **kwargs): pass
        def submit(self, *args): return Future()
        def shutdown(self, **kwargs): pass

    monkeypatch.setattr(run, 'StrategyBroker', Broker)
    monkeypatch.setattr(run, 'ThreadPoolExecutor', Pool)
    monkeypatch.setattr(run, 'RuntimeHealth', lambda *args: SimpleNamespace(
        success=lambda *args: None, failure=lambda *args: None))
    reports = []

    def persist(*args, **kwargs):
        reports.append(kwargs)
        stop.set()

    monkeypatch.setattr(run, 'persist_report', persist)
    trader = SimpleNamespace(broker=Broker(), service=lambda **kwargs: stage('service'),
        exchange_recovery=Controller(), last_bar_ms=0,
        journal=SimpleNamespace(append=lambda *args, **kwargs: None))
    run.run_loop(trader, {}, LiveConfig(state_dir=tmp_path), float('inf'))
    names = [event for event in events if isinstance(event, str)]
    assert names == (['poll', 'service'] if failure == 'service' else ['poll', 'service', 'audit'])
    if failure != 'service':
        expected_wait = ('poll_failed' if failure == 'poll' else
                         'entry_window' if now_seconds in (14390, 14430) else
                         'current_protection_pending' if pending else None)
        assert ('audit_wait', expected_wait) in events
    cycle = next(event[1] for event in events if isinstance(event, tuple) and event[0] == 'cycle')
    assert cycle['poll_ok'] is (failure != 'poll')
    assert cycle['service_ok'] is (failure not in ('service', 'audit'))
    assert reports[-1]['service_errors'] == int(failure in ('service', 'audit'))
    if failure == 'audit':
        assert ('failure', 'service') in events

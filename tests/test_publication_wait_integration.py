"""Persistent owned-detail lag blocks entry without stopping confirmed risk work."""
import json
from decimal import Decimal
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from data_io import START, H4
from live.execution_evidence import NotSubmitted
from live.recovery import ExchangeRecovery, operator_pause
from live.run import persist_report
from test_exchange_recovery import Coverage
from test_strategy_integration import setup, rows

MARKS = {'BTCUSDT': 100, 'ETHUSDT': 100}


def attach(trader, venue, directory):
    recovery = ExchangeRecovery(directory, 'publication-reviewed',
        clock=lambda: venue.now / 1000, monotonic=lambda: venue.now / 1000)
    trader.exchange_recovery = recovery
    trader.broker.entry_guard = recovery.can_enter
    return recovery


def cycle(trader, venue, recovery, seconds=60):
    venue.now += seconds * 1000
    trader.broker.poll()
    outcome = trader.service(now_ms=venue.now, marks=MARKS)
    recovery.cycle(trader, Coverage(), poll_ok=True, service_ok=True, now_ms=venue.now)
    return outcome


@pytest.fixture
def running(tmp_path):
    venue, broker, trader = setup(tmp_path)
    trader.on_bar(START, rows(), MARKS, now_ms=venue.now)
    recovery = attach(trader, venue, tmp_path)
    for seconds in (0, 60, 60):
        cycle(trader, venue, recovery, seconds)
    assert recovery.can_enter() and not trader.halted
    yield venue, broker, trader, recovery
    broker.close()


def delayed_tp(venue, trader, fraction=Decimal('.5')):
    original = Decimal(str(trader.positions['ETHUSDT']['qty']))
    tp = trader.orders['ETHUSDT']['TP']['client_order_index']
    venue.now += 1000
    venue.fill(tp, original * fraction)
    return venue.trades.pop(), original


def test_persistent_tp_lag_risk_service_then_unchanged_three_cycle_recovery(running, tmp_path):
    venue, broker, trader, recovery = running
    delayed, original = delayed_tp(venue, trader)
    cycle(trader, venue, recovery, 0)
    assert trader.publication_pending and not trader.halted
    assert (tmp_path / 'EXCHANGE_PAUSED').exists() and not recovery.can_enter()
    assert trader.positions['ETHUSDT']['qty'] == pytest.approx(float(original / 2))
    assert trader.positions['ETHUSDT']['partial_taken']
    sl = trader.orders['ETHUSDT']['SL']
    assert Decimal(venue.rows[sl['client_order_index']]['trigger_price']) == 100
    assert Decimal(venue.rows[sl['client_order_index']]['initial_base_amount']) == original / 2
    for _ in range(4):
        cycle(trader, venue, recovery)
        assert not trader.halted and recovery.count == 0
        assert trader.publication_pending and not broker.last_report['execution_changes_verified']
    assert not (tmp_path / 'ENTRY_PAUSED').exists()
    venue.trades.append(delayed)
    for index in range(3):
        cycle(trader, venue, recovery)
        assert not trader.publication_pending and not trader.halted
        assert recovery.count == index + 1
        assert recovery.can_enter() is (index == 2)
    assert not (tmp_path / 'EXCHANGE_PAUSED').exists()
    assert len([intent for intent in venue.sent if not intent.reduce_only]) == 1


def test_partial_tp_is_not_complete_tp_while_details_lag(running):
    venue, broker, trader, recovery = running
    delayed, original = delayed_tp(venue, trader, Decimal('.25'))
    cycle(trader, venue, recovery, 0)
    p = trader.positions['ETHUSDT']
    assert trader.publication_pending and not trader.halted
    assert p['qty'] == pytest.approx(float(original * Decimal('.75')))
    assert not p['partial_taken'] and p['stop'] != 100
    assert Decimal(venue.rows[trader.orders['ETHUSDT']['SL']['client_order_index']]['initial_base_amount']) == original * Decimal('.75')


def test_pending_restart_rebuilds_risk_and_stability_without_replaying_entry(running, tmp_path):
    venue, broker, trader, recovery = running
    delayed, _ = delayed_tp(venue, trader)
    cycle(trader, venue, recovery, 0)
    sent = len(venue.sent)
    broker.close()
    _, restarted_broker, restarted = setup(tmp_path, venue)
    try:
        restarted_recovery = attach(restarted, venue, tmp_path)
        assert restarted.publication_pending and not restarted_recovery.can_enter()
        cycle(restarted, venue, restarted_recovery)
        assert not restarted.halted and len(venue.sent) == sent
        venue.trades.append(delayed)
        for index in range(3):
            cycle(restarted, venue, restarted_recovery)
            assert restarted_recovery.can_enter() is (index == 2)
        assert restarted.deferred_entries is None and len(venue.sent) == sent
    finally:
        restarted_broker.close()


def test_long_pending_keeps_closed_bar_clock_without_data_gap_or_late_entry(running):
    venue, broker, trader, recovery = running
    delayed, _ = delayed_tp(venue, trader)
    cycle(trader, venue, recovery, 0)
    first_bars = trader.positions['ETHUSDT']['bars']
    for n in (1, 2):
        venue.now = START + (n + 1) * H4 + 1000
        broker.poll()
        outcome = trader.on_bar(START + n * H4, rows(), MARKS,
                               now_ms=venue.now, allow_entries=recovery.can_enter())
        assert trader.last_bar_ms == START + n * H4
        assert not trader.data_gaps and not trader.halted and not outcome.entries
        assert trader.positions['ETHUSDT']['bars'] == first_bars + n
        duplicate = trader.on_bar(START + n * H4, rows(), MARKS, now_ms=venue.now)
        assert not duplicate.entries and trader.positions['ETHUSDT']['bars'] == first_bars + n
    assert trader.publication_pending and trader.deferred_entries is None
    venue.trades.append(delayed)
    for _ in range(3):
        cycle(trader, venue, recovery)
    assert recovery.can_enter() and not trader.pending and trader.deferred_entries is None
    assert len([intent for intent in venue.sent if not intent.reduce_only]) == 1


def test_publication_recovery_never_clears_manual_pause_or_existing_halt(running, tmp_path):
    venue, broker, trader, recovery = running
    delayed, _ = delayed_tp(venue, trader)
    operator_pause(tmp_path, 'operator pause')
    marker = (tmp_path / 'ENTRY_PAUSED').read_bytes()
    cycle(trader, venue, recovery, 0)
    venue.trades.append(delayed)
    for _ in range(3):
        cycle(trader, venue, recovery)
    assert recovery.ready and not recovery.can_enter()
    assert (tmp_path / 'ENTRY_PAUSED').read_bytes() == marker
    trader._halt('pre-existing unknown submission')
    recovery.publication_wait([{'type': 'history_head_backlog'}])
    for _ in range(4):
        cycle(trader, venue, recovery)
    assert trader.halted and not recovery.ready and recovery.count == 0
    assert (tmp_path / 'ENTRY_PAUSED').read_bytes() == marker


@pytest.mark.parametrize('hard_issue', ['external_position', 'foreign_roundtrip', 'unknown_write'])
def test_lag_cannot_soften_external_or_unknown_execution(running, hard_issue):
    venue, broker, trader, recovery = running
    delayed, _ = delayed_tp(venue, trader)
    if hard_issue == 'external_position':
        venue.position[0] /= 2
    elif hard_issue == 'foreign_roundtrip':
        venue.trades.extend([
            {**delayed, 'trade_id': 900, 'bid_client_id': 900, 'bid_id': 1900},
            {**delayed, 'trade_id': 901, 'ask_client_id': 901, 'ask_id': 1901,
             'ask_account_id': 100, 'bid_account_id': 200, 'is_maker_ask': False}])
    else:
        from test_strategy_broker_uncertainty import missing
        missing(broker, trader.markets, 900, 'BTCUSDT', 'ENTRY')
    cycle(trader, venue, recovery, 0)
    assert trader.halted and not trader.publication_pending and not recovery.can_enter()
    assert recovery.count == 0


def test_partially_filled_stop_with_unpublished_fill_closes_only_proved_residual(running):
    venue, broker, trader, recovery = running
    p = trader.positions['ETHUSDT']
    original = Decimal(str(p['qty']))
    sl = trader.orders['ETHUSDT']['SL']['client_order_index']
    venue.now += 1000
    venue.fill(sl, original / 4)
    venue.trades.pop()
    before = len(venue.sent)
    cycle(trader, venue, recovery, 0)
    assert trader.halted and not recovery.can_enter()
    assert venue.position[0] == 0 and not trader.positions
    closes = venue.sent[before:]
    assert len(closes) == 1 and closes[0].reduce_only
    assert Decimal(closes[0].quantity) == original * Decimal('.75')


def test_malformed_soft_classification_remains_hard(running, monkeypatch):
    venue, broker, trader, recovery = running
    real_state = broker.account_state
    def malformed():
        state = real_state()
        state.execution_changes_verified = False
        state.ledger_report.update(publication_pending=True, publication_pending_evidence=[])
        return state
    monkeypatch.setattr(broker, 'account_state', malformed)
    cycle(trader, venue, recovery, 0)
    assert trader.halted and not trader.publication_pending and not recovery.can_enter()


def test_history_head_backlog_blocks_entry_with_no_false_halt(running, monkeypatch):
    venue, broker, trader, recovery = running
    real_report = broker.ledger.report
    monkeypatch.setattr(broker.ledger, 'report', lambda: {
        **real_report(), 'history_collection': {'head_complete': False, 'pending_reason': 'head_backlog'}})
    cycle(trader, venue, recovery, 0)
    assert trader.publication_pending and not trader.halted and not recovery.can_enter()
    assert trader.positions['ETHUSDT']['qty'] > 0
    assert broker.last_report['publication_pending_evidence'] == [
        {'type': 'history_head_backlog', 'pending_reason': 'head_backlog'}]


def test_persist_report_captures_wait_discovered_during_internal_protection(running, tmp_path):
    venue, broker, trader, recovery = running
    delayed_tp(venue, trader)
    broker.account_state()
    assert not trader.publication_pending
    persist_report(trader, trader.config)
    assert trader.publication_pending and not recovery.can_enter()
    status = json.loads((tmp_path / 'runner_status.json').read_text())
    assert status['publication_pending'] and status['entries_paused']
    assert not status['halted'] and status['publication_pending_evidence']


def test_entry_predispatch_publication_race_is_not_unknown_order(tmp_path, monkeypatch):
    venue, broker, trader = setup(tmp_path)
    try:
        trader.last_bar_ms = START - H4
        recovery = attach(trader, venue, tmp_path)
        # Guard-ready is supplied only for the initial test call; this test
        # isolates a genuine NotSubmitted after earlier account sizing passes.
        broker.entry_guard = lambda: True
        def race(*args, **kwargs):
            broker.last_report = {**broker.last_report, 'publication_pending': True,
                'publication_pending_evidence': [{'type': 'history_head_backlog'}],
                'risk_verified_symbols': ['BTC', 'ETH'], 'unresolved_orders': {}, 'unsafe_protection': {}}
            raise NotSubmitted('account_publication_pending', retryable=True)
        monkeypatch.setattr(broker, 'market_order', race)
        outcome = trader.on_bar(START, rows(), MARKS, now_ms=venue.now)
        assert not trader.halted and trader.publication_pending and not outcome.entries
        assert trader.pending_operation is None and not trader.unresolved_operations
        assert not trader.deferred_entries and venue.sent == []
        assert (tmp_path / 'EXCHANGE_PAUSED').exists()
    finally:
        broker.close()


def test_publication_delay_without_recovery_controller_retains_explicit_halt(tmp_path):
    venue, broker, trader = setup(tmp_path)
    try:
        trader.on_bar(START, rows(), MARKS, now_ms=venue.now)
        broker.account_state()
        delayed, _ = delayed_tp(venue, trader)
        broker.poll()
        trader.service(now_ms=venue.now, marks=MARKS)
        assert trader.halted and not trader.publication_pending
        assert any('without automatic recovery' in reason for reason in trader.halt_reasons)
        assert trader.positions['ETHUSDT']['partial_taken']
        venue.trades.append(delayed)
        venue.now += 60000
        broker.poll()
        trader.service(now_ms=venue.now, marks=MARKS)
        assert broker.last_report['execution_changes_verified'] and trader.halted
        assert len([intent for intent in venue.sent if not intent.reduce_only]) == 1
    finally:
        broker.close()

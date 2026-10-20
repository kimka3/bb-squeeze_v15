"""Retry-read failures use existing recovery; no network or order submission."""
import json
from types import SimpleNamespace

import pytest

from test_exchange_recovery import Clock, temporary_failure
from test_strategy_integration import setup
from data_io import H4
from live import run
from live.authenticated import APIReadError
from live.execution_evidence import UnverifiedExecution
from live.journal import Journal
from live.recovery import ExchangeRecovery
from live.request_budget import RequestDeferred
from live.runtime_health import RuntimeHealth


class RetryTrader:
    def __init__(self, directory, clock, controller):
        self.clock = clock
        self.journal = Journal(directory)
        self.exchange_recovery = controller
        self.broker = SimpleNamespace(entry_guard=controller.can_enter,
            transport=SimpleNamespace(read_diagnostics_error=None),
            last_report={'execution_changes_verified': True})
        self.config = SimpleNamespace(bar_grace_seconds=60)
        self.halted = False
        self.halt_reasons = []
        self.pending_operation = None
        self.unresolved_operations = {}
        self.residual_exits = {}
        self.orphan_orders = {}
        self.data_gaps = []
        self.last_bar_ms = clock.ms // H4 * H4 - H4
        self.deferred_entries = None

    def _halt(self, reason):
        self.halted = True
        self.halt_reasons.append(reason)

    def _defer_entries(self, bar_ms, rows, requests, error):
        self.deferred_entries = {'bar_ms': bar_ms, 'rows': rows, 'requests': requests,
            'deadline_ms': bar_ms+H4+60_000,
            'retry_at_ms': self.clock.ms + int(error.retry_after*1000)}


def ready_environment(directory):
    clock = Clock()
    clock.ms = H4*100-120_000
    controller = ExchangeRecovery(directory, 'reviewed', clock=clock.wall,
                                  monotonic=clock.monotonic)
    trader = RetryTrader(directory, clock, controller)
    coverage = RuntimeHealth(directory, trader.journal, 60, now_ms=clock.ms)
    for index in range(3):
        if index:
            clock.advance(60)
        trader.last_bar_ms = clock.ms // H4 * H4-H4
        coverage.success('poll', clock.ms)
        coverage.success('service', clock.ms)
        controller.cycle(trader, coverage, poll_ok=True, service_ok=True, now_ms=clock.ms)
    assert controller.can_enter()
    clock.advance(1)
    trader._defer_entries(trader.last_bar_ms, {}, {'ETHUSDT': ['SHORT', 4, trader.last_bar_ms]},
                         RequestDeferred(1))
    return trader, controller, clock, coverage


def events(trader):
    return [json.loads(line) for line in trader.journal.path.read_text().splitlines()]


def test_retry_read_failure_enters_existing_recovery_and_drops_old_candidate(tmp_path):
    trader, controller, clock, coverage = ready_environment(tmp_path)
    counters = {}
    run.handle_entry_retry_failure(trader, temporary_failure(), coverage, counters, now_ms=clock.ms)
    assert trader.deferred_entries is None and not trader.halted
    assert not controller.can_enter() and controller.path.exists()
    assert not (tmp_path/'ENTRY_PAUSED').exists()
    assert coverage.snapshot()['active_gap_streams'] == ['service']
    assert counters['entry_retry_errors'] == counters['service_errors'] == 1
    failure = [e for e in events(trader) if e['kind'] == 'exchange_recovery_failure'][-1]
    assert failure['stream'] == 'entry_retry' and failure['automatic_recovery_allowed']
    for index in range(3):
        clock.advance(60)
        coverage.success('poll', clock.ms)
        coverage.success('service', clock.ms)
        controller.cycle(trader, coverage, poll_ok=True, service_ok=True, now_ms=clock.ms)
        assert controller.can_enter() == (index == 2)
    assert trader.deferred_entries is None  # the stale signal is never replayed
    ready = [e for e in events(trader) if e['kind'] == 'exchange_recovery_ready'][-1]
    assert ready['consecutive_successes'] == 3 and ready['stable_seconds'] == 120


@pytest.mark.parametrize('failure', [
    APIReadError('nontransient schema error', code='schema', endpoint='/api/v1/account',
                 automatic_recovery_allowed=False),
    UnverifiedExecution('order outcome unknown'), ValueError('schema mismatch'),
    OSError('diagnostic persistence failed')])
def test_nontransient_or_uncertain_failure_keeps_hard_halt(tmp_path, failure):
    trader, controller, clock, coverage = ready_environment(tmp_path)
    run.handle_entry_retry_failure(trader, failure, coverage, {}, now_ms=clock.ms)
    assert trader.halted and trader.deferred_entries is None
    assert not controller.can_enter() and (tmp_path/'ENTRY_PAUSED').exists()
    assert coverage.snapshot()['active_gap_streams'] == ['service']


def test_transient_read_without_controller_cannot_silently_resume(tmp_path):
    trader, _, clock, coverage = ready_environment(tmp_path)
    trader.exchange_recovery = None
    run.handle_entry_retry_failure(trader, temporary_failure(), coverage, {}, now_ms=clock.ms)
    assert trader.halted and trader.deferred_entries is None


@pytest.mark.parametrize('where', ['pending', 'unresolved'])
def test_transient_read_does_not_override_an_uncertain_order(tmp_path, where):
    trader, controller, clock, coverage = ready_environment(tmp_path)
    if where == 'pending':
        trader.pending_operation = {'kind': 'entry', 'coi': 7}
    else:
        trader.unresolved_operations = {'7': {'kind': 'entry', 'coi': 7}}
    run.handle_entry_retry_failure(trader, temporary_failure(), coverage, {}, now_ms=clock.ms)
    assert trader.halted and trader.deferred_entries is None and not controller.can_enter()
    assert trader.pending_operation or trader.unresolved_operations


def test_budget_wait_is_not_exchange_failure_and_keeps_original_deadline(tmp_path):
    trader, controller, clock, coverage = ready_environment(tmp_path)
    original_deadline = trader.deferred_entries['deadline_ms']
    counters = {}
    run.handle_entry_retry_failure(trader, RequestDeferred(5), coverage, counters, now_ms=clock.ms)
    assert controller.can_enter() and not trader.halted
    assert trader.deferred_entries['deadline_ms'] == original_deadline
    assert trader.deferred_entries['retry_at_ms'] == clock.ms+5000
    assert counters['entry_retry_budget_deferrals'] == 1
    assert 'service_errors' not in counters and not coverage.snapshot()['active_gap_streams']
    assert not any(e['kind'] == 'exchange_recovery_failure' for e in events(trader))
    clock.ms = original_deadline
    run.handle_entry_retry_failure(trader, RequestDeferred(5), coverage, counters, now_ms=clock.ms)
    assert trader.deferred_entries is None and not trader.halted


def test_budget_error_during_unknown_operation_does_not_requeue_entry(tmp_path):
    trader, controller, clock, coverage = ready_environment(tmp_path)
    trader.pending_operation = {'kind': 'entry', 'coi': 7}
    run.handle_entry_retry_failure(trader, RequestDeferred(5), coverage, {}, now_ms=clock.ms)
    assert trader.halted and trader.deferred_entries is None
    assert not any(e['kind'] == 'exchange_recovery_failure' for e in events(trader))


def test_runner_and_account_report_include_unresolved_operations(tmp_path):
    _, broker, trader = setup(tmp_path)
    try:
        trader.unresolved_operations = {'7': {'kind': 'protection', 'symbol': 'ETHUSDT'}}
        run.persist_report(trader, trader.config)
        for name in ('runner_status.json', 'report.json'):
            assert json.loads((tmp_path/name).read_text())['unresolved_operations'] == trader.unresolved_operations
    finally:
        broker.close()


def test_run_loop_routes_retry_failure_and_records_cumulative_counter(tmp_path, monkeypatch):
    import time
    _, broker, trader = setup(tmp_path)
    calls = []
    saved = []
    trader.deferred_entries = {'bar_ms': trader.last_bar_ms, 'rows': {}, 'requests': {'ETHUSDT': []}}
    trader.exchange_recovery = SimpleNamespace(
        failure=lambda exc, stream: calls.append(stream), cycle=lambda *args, **kwargs: None)
    monkeypatch.setattr(run, 'prepare_frames', lambda *args: {})
    monkeypatch.setattr(run, 'run_once', lambda *args, **kwargs: None)
    monkeypatch.setattr(broker, 'poll', lambda: [])
    monkeypatch.setattr(trader, 'service', lambda **kwargs: None)
    def failure():
        raise temporary_failure()
    monkeypatch.setattr(trader, 'retry_deferred_entries', failure)
    def persist(*args, **kwargs):
        saved.append(kwargs)
        if kwargs.get('entry_retry_errors') == 1:
            run.STOP_REQUESTED.set()
    monkeypatch.setattr(run, 'persist_report', persist)
    run.STOP_REQUESTED.clear()
    try:
        run.run_loop(trader, trader.markets, trader.config, time.time()+2)
        assert calls == ['entry_retry'] and trader.deferred_entries is None and not trader.halted
        assert saved[-1]['entry_retry_errors'] == saved[-1]['service_errors'] == 1
    finally:
        run.STOP_REQUESTED.clear()
        broker.close()

"""Exercise normal bar rollover through the real runner and execution adapter.

All exchange reads, fills and protective orders use the in-memory test Venue.
Only the signal feed and clocks are substituted; no external requests are made.
"""
import json
import time
from types import SimpleNamespace

import pytest

from test_strategy_integration import H4, START, rows, setup
from live import run
from live.authenticated import APIReadError
from live.recovery import ExchangeRecovery


class Coverage:
    def snapshot(self):
        return {'active_gap_streams': [], 'coverage_complete': False}


@pytest.fixture
def running_before_boundary(tmp_path, monkeypatch):
    venue, broker, trader = setup(tmp_path)
    boundary = START + H4
    venue.now = boundary - 180_000
    trader.last_bar_ms = START - H4
    controller = ExchangeRecovery(
        tmp_path, 'synthetic-reviewed-release',
        clock=lambda: venue.now / 1000,
        monotonic=lambda: (venue.now - boundary + 180_000) / 1000)
    broker.entry_guard = controller.can_enter
    previous_stop = run.STOP_REQUESTED.is_set()
    run.STOP_REQUESTED.clear()
    monkeypatch.setattr(run, 'time', SimpleNamespace(
        time=lambda: venue.now / 1000, gmtime=time.gmtime, strftime=time.strftime))
    monkeypatch.setattr(run.feed, 'latest_closed', lambda _: (START, rows()))

    def cycle(offset_ms):
        venue.now = boundary + offset_ms
        broker.poll()
        protected = trader.service(now_ms=venue.now)
        assert not protected.halted
        assert broker.last_report['execution_changes_verified'] is True
        controller.cycle(trader, Coverage(), poll_ok=True, service_ok=True,
                         now_ms=venue.now)

    def process_bar(offset_ms):
        venue.now = boundary + offset_ms
        return run.run_once(trader, trader.markets, trader.config, frames={})

    try:
        for offset in (-180_000, -120_000, -60_000):
            cycle(offset)
        assert controller.can_enter()
        assert not venue.sent
        yield SimpleNamespace(
            venue=venue, broker=broker, trader=trader, controller=controller,
            directory=tmp_path, boundary=boundary,
            cycle=cycle, process_bar=process_bar)
    finally:
        broker.close()
        if previous_stop:
            run.STOP_REQUESTED.set()
        else:
            run.STOP_REQUESTED.clear()


def test_ready_rollover_then_fresh_bar_enters_once_with_protection(running_before_boundary):
    state = running_before_boundary
    state.cycle(1_000)
    assert state.trader.last_bar_ms == START - H4
    assert state.controller.can_enter()
    assert not (state.directory / 'EXCHANGE_PAUSED').exists()

    # run_once checks the recovery guard before on_bar advances last_bar_ms.
    result = state.process_bar(7_000)
    assert len(result.entries) == 1
    assert not result.halted
    assert state.trader.last_bar_ms == START
    assert [order.order_type for order in state.venue.sent] == [
        'market', 'stop-loss', 'limit']
    assert sum(not order.reduce_only for order in state.venue.sent) == 1
    assert all(order.reduce_only for order in state.venue.sent[1:])
    position = state.trader.positions['ETHUSDT']
    assert position['initial_risk'] == pytest.approx(6)
    assert state.trader.orders['ETHUSDT']['SL']['qty'] == pytest.approx(position['qty'])
    assert state.trader.orders['ETHUSDT']['TP']['qty'] == pytest.approx(position['qty'] / 2)
    assert state.broker.account_state().execution_changes_verified

    duplicate = state.process_bar(8_000)
    assert not duplicate.entries
    assert len(state.venue.sent) == 3
    state.cycle(9_000)
    assert state.controller.can_enter()
    events = [json.loads(line) for line in
              (state.directory / 'journal.jsonl').read_text().splitlines()]
    # Startup verification is the sole readiness transition; rollover adds none.
    assert sum(event['kind'] == 'exchange_recovery_ready' for event in events) == 1


@pytest.mark.parametrize('offset_ms', [60_000, 60_001])
def test_rollover_does_not_extend_existing_entry_deadline(running_before_boundary, offset_ms):
    state = running_before_boundary
    state.cycle(1_000)
    assert state.controller.can_enter()
    result = state.process_bar(offset_ms)
    assert not result.entries
    assert not result.halted
    assert not state.venue.sent
    assert not state.trader.positions
    assert state.trader.last_bar_ms == START
    assert result.skipped
    assert not state.trader.deferred_entries
    assert state.broker.entry_deadline_ms is None


def test_actual_read_failure_at_boundary_still_requires_current_bar_and_stable_recovery(
        running_before_boundary):
    state = running_before_boundary
    state.venue.now = state.boundary + 1_000
    state.controller.failure(APIReadError(
        'temporary exchange maintenance', code='temporary_http',
        endpoint='/api/v1/account', automatic_recovery_allowed=True,
        http_status=503), 'service')

    # A genuine failure cannot borrow the ready-state normal-rollover allowance.
    state.cycle(1_000)
    assert not state.controller.can_enter()
    assert state.controller.snapshot()['consecutive_successes'] == 0
    assert 'confirmed_bar_not_current' in state.controller.snapshot()['blockers']
    result = state.process_bar(7_000)
    assert not result.entries
    assert state.trader.last_bar_ms == START
    assert not state.venue.sent

    for count, offset in enumerate((8_000, 68_000, 128_000), start=1):
        state.cycle(offset)
        assert state.controller.snapshot()['consecutive_successes'] == count
        assert state.controller.can_enter() is (count == 3)
    assert not (state.directory / 'EXCHANGE_PAUSED').exists()
    assert not (state.directory / 'ENTRY_PAUSED').exists()
    assert state.venue.now > state.boundary + state.trader.config.bar_grace_seconds * 1000

    # Recovery restores permission for future signals, without replaying this bar.
    duplicate = state.process_bar(129_000)
    state.trader.retry_deferred_entries()
    assert not duplicate.entries
    assert not state.venue.sent
    events = [json.loads(line) for line in
              (state.directory / 'journal.jsonl').read_text().splitlines()]
    ready = [event for event in events if event['kind'] == 'exchange_recovery_ready']
    assert len(ready) == 2
    assert ready[-1]['stable_seconds'] == 120
    assert ready[-1]['consecutive_successes'] == 3

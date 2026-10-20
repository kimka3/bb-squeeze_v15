"""Normal rollover cannot weaken outage or execution safety checks."""
import json
from types import SimpleNamespace

import pytest

from test_exchange_recovery import (
    H4, ExchangeRecovery, environment, cycle, stable, temporary_failure,
)


@pytest.fixture
def before_rollover(environment):
    controller, clock, trader, _ = environment
    boundary = H4 * 101
    clock.ms = boundary - 180_000
    trader.last_bar_ms = boundary - 2 * H4
    trader.config = SimpleNamespace(bar_grace_seconds=60)
    stable(environment)
    assert controller.can_enter()
    clock.advance(61)
    assert clock.ms == boundary + 1000
    return environment


@pytest.mark.parametrize('offset', [0, 1000, 59_999])
def test_ready_permission_survives_only_normal_transition(before_rollover, offset):
    controller, clock, trader, _ = before_rollover
    clock.ms = clock.ms // H4 * H4 + offset
    cycle(before_rollover)
    assert controller.can_enter()
    assert not controller.path.exists()
    assert trader.last_bar_ms == clock.ms // H4 * H4 - 2 * H4
    assert controller.count == 3
    events = [json.loads(line) for line in
              (controller.directory / 'journal.jsonl').read_text().splitlines()]
    assert sum(event['kind'] == 'exchange_recovery_ready' for event in events) == 1


@pytest.mark.parametrize('offset', [60_000, 60_001, 120_000])
def test_bar_still_missing_at_deadline_closes_gate(before_rollover, offset):
    controller, clock, _, _ = before_rollover
    clock.ms = clock.ms // H4 * H4 + offset
    cycle(before_rollover)
    assert not controller.can_enter()
    assert controller.count == 0
    assert 'confirmed_bar_not_current' in controller.blockers


@pytest.mark.parametrize('grace', [None, False, True, 0, -1, '60', float('inf'), float('nan')])
def test_invalid_grace_never_creates_transition_allowance(before_rollover, grace):
    controller, _, trader, _ = before_rollover
    trader.config.bar_grace_seconds = grace
    cycle(before_rollover)
    assert not controller.can_enter()
    assert 'confirmed_bar_not_current' in controller.blockers


def test_transition_uses_existing_configured_window(before_rollover):
    controller, clock, trader, _ = before_rollover
    trader.config.bar_grace_seconds = 5
    cycle(before_rollover)
    assert controller.can_enter()
    clock.advance(4)
    cycle(before_rollover)
    assert not controller.can_enter()


@pytest.mark.parametrize('offset', [-H4, 2 * H4])
def test_older_or_future_bar_never_exempt(before_rollover, offset):
    controller, _, trader, _ = before_rollover
    trader.last_bar_ms += offset
    cycle(before_rollover)
    assert not controller.can_enter()
    assert controller.count == 0


@pytest.mark.parametrize('elapsed_since_last', [90.001, -0.001])
def test_stale_or_reversed_clock_cannot_count_old_bar_success(before_rollover, elapsed_since_last):
    controller, clock, _, _ = before_rollover
    clock.elapsed = controller.last_success + elapsed_since_last
    cycle(before_rollover)
    assert not controller.can_enter()
    assert controller.count == 0
    assert 'confirmed_bar_not_current' in controller.blockers


def test_one_monotonic_sample_for_transition_and_cycle_freshness(before_rollover, monkeypatch):
    controller, _, _, _ = before_rollover
    original_count = controller.count
    last_success = controller.last_success
    samples = []

    def monotonic():
        value = last_success + 90 + len(samples) * .001
        samples.append(value)
        return value

    monkeypatch.setattr(controller, 'monotonic', monotonic)
    cycle(before_rollover)
    assert len(samples) == 1
    assert controller.ready
    assert controller.count == original_count
    assert not controller.path.exists()
    assert controller.last_success == samples[0]


@pytest.mark.parametrize('transition', ['failure', 'restart', 'partial_poll', 'partial_service'])
def test_unready_controller_still_requires_current_bar(before_rollover, transition):
    controller, clock, trader, coverage = before_rollover
    if transition == 'failure':
        controller.failure(temporary_failure(), 'poll')
    elif transition == 'restart':
        controller = ExchangeRecovery(controller.directory, controller.fingerprint,
                                      clock=clock.wall, monotonic=clock.monotonic)
    else:
        controller.cycle(trader, coverage, now_ms=clock.ms,
                         poll_ok=transition != 'partial_poll',
                         service_ok=transition != 'partial_service')
    controller.cycle(trader, coverage, now_ms=clock.ms, poll_ok=True, service_ok=True)
    assert not controller.can_enter()
    assert controller.count == 0
    assert 'confirmed_bar_not_current' in controller.blockers


@pytest.mark.parametrize('field,value', [
    ('halted', True), ('pending_operation', {'phase': 'unknown'}),
    ('residual_exits', {'ETH': {}}), ('orphan_orders', {'ETH': {}}),
    ('data_gaps', ['missing']),
])
def test_other_safety_blockers_still_win_at_rollover(before_rollover, field, value):
    controller, _, trader, _ = before_rollover
    setattr(trader, field, value)
    cycle(before_rollover)
    assert not controller.can_enter()
    assert field in controller.blockers


@pytest.mark.parametrize('problem', ['reconciliation', 'coverage', 'diagnostics', 'storage', 'manual'])
def test_external_gates_still_win_at_rollover(before_rollover, problem):
    controller, _, trader, coverage = before_rollover
    marker = controller.directory / 'ENTRY_PAUSED'
    retained = b'{"reason":"manual pause","at_ms":1}'
    if problem == 'reconciliation':
        trader.broker.last_report['execution_changes_verified'] = False
    elif problem == 'coverage':
        coverage.gaps = ['poll']
    elif problem == 'diagnostics':
        trader.broker.transport.read_diagnostics_error = 'OSError'
    elif problem == 'storage':
        controller.storage_error = 'OSError'
    else:
        marker.write_bytes(retained)
    cycle(before_rollover)
    assert not controller.can_enter()
    if problem == 'manual':
        assert marker.read_bytes() == retained

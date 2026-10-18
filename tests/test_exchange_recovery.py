"""Exchange outage recovery uses local evidence, never test/live requests."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
from data_io import H4
from live.authenticated import APIReadError
from live import recovery
from live.recovery import ExchangeRecovery, OWNER
from live.process_lock import AlreadyRunningError
from live.request_budget import RequestDeferred


class Clock:
    def __init__(self):
        self.ms = H4 * 100 + 300_000
        self.elapsed = 0.0

    def wall(self):
        return self.ms / 1000

    def monotonic(self):
        return self.elapsed

    def advance(self, seconds=60):
        self.ms += int(seconds * 1000)
        self.elapsed += seconds


class Coverage:
    def __init__(self):
        self.gaps = []

    def snapshot(self):
        return {'active_gap_streams': list(self.gaps),
                'coverage_complete': False, 'failure_count': 19}


def temporary_failure():
    return APIReadError('temporary read failure', code='temporary_http',
        endpoint='/api/v1/account', automatic_recovery_allowed=True,
        http_status=503)


@pytest.fixture
def environment(tmp_path):
    clock = Clock()
    controller = ExchangeRecovery(tmp_path, 'reviewed-fingerprint',
        clock=clock.wall, monotonic=clock.monotonic)
    transport = SimpleNamespace(read_diagnostics_error=None)
    trader = SimpleNamespace(halted=False, pending_operation=None,
        residual_exits={}, orphan_orders={}, data_gaps=[],
        last_bar_ms=clock.ms // H4 * H4 - H4,
        broker=SimpleNamespace(transport=transport,
            last_report={'execution_changes_verified': True}))
    return controller, clock, trader, Coverage()


def cycle(environment, *, poll_ok=True, service_ok=True):
    controller, clock, trader, coverage = environment
    controller.cycle(trader, coverage, poll_ok=poll_ok,
                     service_ok=service_ok, now_ms=clock.ms)


def stable(environment):
    for index in range(3):
        if index:
            environment[1].advance()
        cycle(environment)


def test_startup_gate_then_three_normal_cycles_enable_future_entries(environment):
    controller, clock, trader, coverage = environment
    assert controller.path.exists()
    assert not controller.can_enter()
    cycle(environment)
    assert controller.snapshot()['consecutive_successes'] == 1
    clock.advance()
    cycle(environment)
    assert controller.path.exists() and not controller.can_enter()
    clock.advance()
    cycle(environment)
    assert controller.can_enter() and not controller.path.exists()
    assert controller.snapshot()['state'] == 'ready'
    assert trader.last_bar_ms == clock.ms // H4 * H4 - H4


def test_temporary_outage_recovers_without_discarding_historical_gaps(environment):
    controller, clock, trader, coverage = environment
    stable(environment)
    controller.failure(temporary_failure(), 'poll')
    assert controller.path.exists() and not controller.can_enter()
    assert not (controller.directory / 'ENTRY_PAUSED').exists()
    clock.advance()
    stable(environment)
    assert controller.can_enter()
    # Historical sampled coverage remains incomplete; only current gaps matter.
    assert coverage.snapshot()['failure_count'] == 19
    assert coverage.snapshot()['coverage_complete'] is False


def test_failure_between_successes_restarts_confirmation_period(environment):
    controller, clock, _, _ = environment
    cycle(environment)
    clock.advance()
    cycle(environment)
    controller.failure(temporary_failure(), 'service')
    assert controller.snapshot()['consecutive_successes'] == 0
    clock.advance()
    cycle(environment)
    clock.advance()
    cycle(environment)
    assert not controller.can_enter()
    clock.advance()
    cycle(environment)
    assert controller.can_enter()


@pytest.mark.parametrize('poll_ok,service_ok', [(False, True), (True, False), (False, False)])
def test_partial_cycle_never_counts_as_success(environment, poll_ok, service_ok):
    controller, clock, _, _ = environment
    cycle(environment)
    clock.advance()
    cycle(environment, poll_ok=poll_ok, service_ok=service_ok)
    assert controller.snapshot()['consecutive_successes'] == 0
    assert not controller.can_enter()


def test_duplicate_cycle_timestamp_does_not_advance_recovery(environment):
    controller, clock, _, _ = environment
    cycle(environment)
    for _ in range(4):
        cycle(environment)
    assert controller.snapshot()['consecutive_successes'] == 1
    assert not controller.can_enter()
    clock.advance()
    cycle(environment)
    clock.advance()
    cycle(environment)
    assert controller.can_enter()


def test_fast_successes_cannot_bypass_stability_duration(environment):
    controller, clock, _, _ = environment
    for _ in range(5):
        cycle(environment)
        clock.advance(1)
    assert not controller.can_enter()


def test_long_gap_resets_consecutive_recovery_and_expires_ready_state(environment):
    controller, clock, _, _ = environment
    cycle(environment)
    clock.advance(91)
    cycle(environment)
    assert controller.snapshot()['consecutive_successes'] == 1
    clock.advance()
    cycle(environment)
    clock.advance()
    cycle(environment)
    assert controller.can_enter()
    clock.advance(91)
    assert not controller.can_enter()
    cycle(environment)
    assert not controller.can_enter()
    assert controller.path.exists()


def test_monotonic_clock_reversal_cannot_enable_entries(environment):
    controller, clock, _, _ = environment
    cycle(environment)
    clock.advance()
    cycle(environment)
    clock.elapsed -= 10
    clock.ms += 1000
    cycle(environment)
    assert controller.snapshot()['consecutive_successes'] == 1
    assert not controller.can_enter()


def test_restart_does_not_adopt_previous_success_streak(environment):
    controller, clock, trader, coverage = environment
    cycle(environment)
    clock.advance()
    cycle(environment)
    restarted = ExchangeRecovery(controller.directory, controller.fingerprint,
        clock=clock.wall, monotonic=clock.monotonic)
    assert restarted.session != controller.session
    assert restarted.snapshot()['consecutive_successes'] == 0
    assert not restarted.can_enter()
    clock.advance()
    restarted.cycle(trader, coverage, poll_ok=True, service_ok=True, now_ms=clock.ms)
    assert restarted.snapshot()['consecutive_successes'] == 1
    assert not restarted.can_enter()


@pytest.mark.parametrize('reason', ['operator pause', 'operator stop requested', 'operational monitor'])
def test_automatic_recovery_keeps_separate_pause_byte_for_byte(environment, reason):
    controller, _, _, _ = environment
    path = controller.directory / 'ENTRY_PAUSED'
    contents = json.dumps({'reason': reason, 'at_ms': 17, 'review': 'retain'})
    path.write_text(contents)
    stable(environment)
    assert path.read_text() == contents
    assert not controller.can_enter()


def test_unknown_failure_creates_nonautomatic_pause_and_never_clears_it(environment):
    controller, clock, _, _ = environment
    controller.failure(ValueError('invalid account identity'), 'service')
    path = controller.directory / 'ENTRY_PAUSED'
    assert path.exists()
    original = path.read_bytes()
    stable(environment)
    assert path.read_bytes() == original
    assert not controller.can_enter()


def test_local_budget_deferral_can_recover_without_manual_pause(environment):
    controller, _, _, _ = environment
    controller.failure(RequestDeferred(60), 'poll')
    assert not (controller.directory / 'ENTRY_PAUSED').exists()
    stable(environment)
    assert controller.can_enter()


@pytest.mark.parametrize('field,value', [
    ('halted', True),
    ('pending_operation', {'kind': 'entry', 'phase': 'unknown'}),
    ('residual_exits', {'ETH': {'reason': 'PROTECTION_LOST'}}),
    ('orphan_orders', {'ETH': {'SL': 123}}),
    ('data_gaps', [{'status': 'unresolved'}]),
])
def test_strategy_safety_states_block_automatic_resume(environment, field, value):
    controller, _, trader, _ = environment
    setattr(trader, field, value)
    stable(environment)
    assert not controller.can_enter()
    assert field in controller.snapshot()['blockers']
    assert getattr(trader, field) == value


@pytest.mark.parametrize('verified', [False, None, 1, 'true'])
def test_execution_reconciliation_must_be_explicitly_true(environment, verified):
    controller, _, trader, _ = environment
    trader.broker.last_report['execution_changes_verified'] = verified
    stable(environment)
    assert not controller.can_enter()
    assert 'execution_not_reconciled' in controller.snapshot()['blockers']


def test_current_runtime_gap_blocks_even_when_http_poll_succeeds(environment):
    controller, _, _, coverage = environment
    coverage.gaps = ['bar']
    stable(environment)
    assert not controller.can_enter()
    assert 'active_data_gap' in controller.snapshot()['blockers']


@pytest.mark.parametrize('status', [None, [], {}, {'active_gap_streams': None},
                                  {'active_gap_streams': ''}])
def test_missing_or_malformed_coverage_is_not_recovery_evidence(environment, monkeypatch, status):
    controller, _, _, coverage = environment
    monkeypatch.setattr(coverage, 'snapshot', lambda: status)
    stable(environment)
    assert not controller.can_enter()
    assert 'active_data_gap' in controller.snapshot()['blockers']


@pytest.mark.parametrize('offset', [-H4, H4])
def test_confirmed_bar_must_be_exact_current_closed_bar(environment, offset):
    controller, _, trader, _ = environment
    trader.last_bar_ms += offset
    stable(environment)
    assert not controller.can_enter()
    assert 'confirmed_bar_not_current' in controller.snapshot()['blockers']


def test_diagnostics_loss_blocks_auto_recovery(environment):
    controller, _, trader, _ = environment
    trader.broker.transport.read_diagnostics_error = 'OSError'
    stable(environment)
    assert not controller.can_enter()
    assert 'diagnostics' in controller.snapshot()['blockers']


def test_storage_failure_latches_and_later_success_does_not_clear_it(environment, monkeypatch):
    controller, clock, _, _ = environment
    original = recovery.atomic

    def broken(*args, **kwargs):
        raise OSError('storage unavailable')

    monkeypatch.setattr(recovery, 'atomic', broken)
    cycle(environment)
    assert controller.snapshot()['storage_error'] == 'OSError'
    assert not controller.can_enter()
    monkeypatch.setattr(recovery, 'atomic', original)
    clock.advance()
    stable(environment)
    assert controller.snapshot()['storage_error'] == 'OSError'
    assert not controller.can_enter()


def test_recovery_event_write_failure_keeps_durable_gate(environment, monkeypatch):
    controller, clock, _, _ = environment
    cycle(environment)
    clock.advance()
    cycle(environment)
    original = controller.journal.append

    def append(kind, **kwargs):
        if kind == 'exchange_recovery_ready':
            raise OSError('event storage unavailable')
        return original(kind, **kwargs)

    monkeypatch.setattr(controller.journal, 'append', append)
    clock.advance()
    cycle(environment)
    assert controller.path.exists()
    assert controller.snapshot()['storage_error'] == 'OSError'
    assert not controller.can_enter()


def test_failure_event_storage_loss_survives_restart(environment,monkeypatch):
    controller,clock,_,_=environment
    original=controller.journal.append
    def broken(*args,**kwargs):raise OSError('journal unavailable')
    monkeypatch.setattr(controller.journal,'append',broken)
    controller.failure(APIReadError('temporary',code='http_503',endpoint='/api/v1/account',
                       automatic_recovery_allowed=True),'poll')
    assert json.loads(controller.path.read_text())['storage_error']=='OSError'
    monkeypatch.setattr(controller.journal,'append',original)
    restarted=ExchangeRecovery(controller.directory,controller.fingerprint,
                               clock=controller.clock,monotonic=controller.monotonic)
    assert (controller.directory/'ENTRY_PAUSED').exists()
    assert not restarted.can_enter()


def test_normal_cycles_after_ready_do_not_recreate_outage_marker(environment):
    controller, clock, _, _ = environment
    stable(environment)
    for _ in range(4):
        clock.advance()
        cycle(environment)
        assert controller.can_enter()
        assert not controller.path.exists()


@pytest.mark.parametrize('contents', [
    '{', 'null', '[]',
    json.dumps({'owner': 'unknown', 'fingerprint': 'reviewed-fingerprint'}),
    json.dumps({'owner': OWNER, 'fingerprint': 'foreign-release'}),
])
def test_unknown_or_malformed_marker_is_preserved_at_start(tmp_path, contents):
    path = tmp_path / 'EXCHANGE_PAUSED'
    path.write_text(contents)
    with pytest.raises((RuntimeError, ValueError, TypeError, AttributeError)):
        ExchangeRecovery(tmp_path, 'reviewed-fingerprint')
    assert path.read_text() == contents


@pytest.mark.parametrize('replacement', [
    '{',
    json.dumps({'owner': OWNER, 'fingerprint': 'other', 'session': 'other'}),
])
def test_changed_marker_during_recovery_is_not_overwritten_or_cleared(environment, replacement):
    controller, clock, _, _ = environment
    cycle(environment)
    clock.advance()
    cycle(environment)
    controller.path.write_text(replacement)
    clock.advance()
    cycle(environment)
    assert not controller.can_enter()
    assert controller.path.read_text() == replacement


def test_operator_pause_arriving_during_completion_still_blocks_entries(environment, monkeypatch):
    controller, clock, _, _ = environment
    cycle(environment)
    clock.advance()
    cycle(environment)
    original = controller.journal.append
    marker = controller.directory / 'ENTRY_PAUSED'

    def append(kind, **kwargs):
        if kind == 'exchange_recovery_ready':
            marker.write_text('{"reason":"operator stop requested"}')
        return original(kind, **kwargs)

    monkeypatch.setattr(controller.journal, 'append', append)
    clock.advance()
    cycle(environment)
    assert marker.exists()
    assert not controller.can_enter()


@pytest.mark.parametrize('failure_fields', [
    {'reason': 'manual_inspection_required'},
    {'reason': 'confirming_stable_recovery', 'storage_error': 'OSError'},
    {'reason': 'recovery_checks_pending', 'blockers': ['diagnostics']},
])
def test_restart_recreates_manual_latch_before_resetting_crash_marker(tmp_path, monkeypatch, failure_fields):
    clock = Clock()
    prior = {'owner': OWNER, 'fingerprint': 'reviewed-fingerprint',
             'session': 'crashed-session', 'consecutive_successes': 2,
             **failure_fields}
    path = tmp_path / 'EXCHANGE_PAUSED'
    path.write_text(json.dumps(prior))
    assert not (tmp_path / 'ENTRY_PAUSED').exists()
    original = recovery.atomic
    observed = []

    def checked_atomic(target, value):
        if Path(target) == path:
            observed.append((tmp_path / 'ENTRY_PAUSED').exists())
            assert observed[-1], 'manual latch must exist before crash marker is replaced'
        return original(target, value)

    monkeypatch.setattr(recovery, 'atomic', checked_atomic)
    controller = ExchangeRecovery(tmp_path, 'reviewed-fingerprint',
        clock=clock.wall, monotonic=clock.monotonic)
    assert observed == [True]
    assert controller.session != prior['session']
    assert controller.snapshot()['consecutive_successes'] == 0
    assert not controller.can_enter()
    retained = (tmp_path / 'ENTRY_PAUSED').read_bytes()
    trader = SimpleNamespace(halted=False, pending_operation=None,
        residual_exits={}, orphan_orders={}, data_gaps=[],
        last_bar_ms=clock.ms // H4 * H4 - H4,
        broker=SimpleNamespace(transport=SimpleNamespace(read_diagnostics_error=None),
            last_report={'execution_changes_verified': True}))
    stable((controller, clock, trader, Coverage()))
    assert (tmp_path / 'ENTRY_PAUSED').read_bytes() == retained
    assert not controller.can_enter()


def test_failure_lock_contention_latches_without_raising(environment, monkeypatch):
    controller, clock, _, _ = environment

    def locked(*args, **kwargs):
        raise AlreadyRunningError('operator holds entry control lock')

    monkeypatch.setattr(recovery, 'safety_pause', locked)
    controller.failure(ValueError('invalid account schema'), 'service')
    assert controller.snapshot()['storage_error'] == 'AlreadyRunningError'
    assert json.loads(controller.path.read_text())['reason'] == 'manual_inspection_required'
    clock.advance()
    stable(environment)
    assert not controller.can_enter()

"""Runner/production resolution integration without live credentials or IO."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
from data_io import H4
from live import run, recovery
from live.config import LiveConfig
from live.journal import Journal
from live.recovery import ExchangeRecovery
from live.resolution_policy import classify_reason
from live.trader import Trader
from test_exchange_recovery import Clock, Coverage, temporary_failure
from test_strategy_integration import setup


def execution_operation(kind):
    common = {'kind': kind, 'symbol': 'ETHUSDT', 'bar_ms': H4}
    if kind == 'entry':
        common.update(coi=101, side='LONG', phase='submit')
    elif kind == 'exit':
        common.update(coi=101, side='LONG', qty=1.)
    elif kind == 'amend':
        common.update(coi=101, qty=1., trigger=100.)
    elif kind == 'stop_renewal':
        common.update(coi=101, side='LONG', qty=1., trigger=100.,
                      old_ref={'client_order_index': 100}, cycle_ms=0,
                      entry_cycle=99, phase='create')
    return common


@pytest.mark.parametrize('kind,prefix', [
    ('entry', 'entry/protection outcome unresolved'),
    ('exit', 'exit outcome unresolved'),
    ('protection', 'protective placement unresolved'),
    ('amend', 'stop amendment outcome unresolved'),
    ('stop_renewal', 'stop renewal requires reconciliation'),
])
@pytest.mark.parametrize('during_recovery', [False, True])
def test_execution_halt_has_stable_cause_and_retains_original_detail(tmp_path, kind, prefix, during_recovery):
    venue, broker, trader = setup(tmp_path)
    try:
        operation = execution_operation(kind)
        trader._begin_operation(operation)
        original = ('operation recovery failed' if during_recovery else f'ETHUSDT: {prefix}') + ': response unavailable'
        canonical = f'ETHUSDT: {prefix}; execution evidence required'
        trader._halt(original)
        assert trader.halt_reasons == [canonical]
        assert classify_reason(canonical, trader.universe) == 'order_outcome'
        assert trader.pending_operation == operation and venue.sent == []
        events = list(trader.journal.events())
        detail = next(event for event in events if event['kind'] == 'execution_halt_detail')
        assert detail['reason'] == original and detail['operation'] == operation
        assert detail['canonical_reason'] == canonical
        assert events.index(detail) < next(i for i, event in enumerate(events) if event['kind'] == 'safety_halt')
        saved = trader.journal.load_snapshot()
        assert saved['halted'] is True and saved['halt_reasons'] == [canonical]
        assert saved['pending_operation'] == operation
    finally:
        broker.close()


@pytest.mark.parametrize('change,reason', [
    ('absent', 'ETHUSDT: entry/protection outcome unresolved: timeout'),
    ('wrong_symbol', 'ETHUSDT: entry/protection outcome unresolved: timeout'),
    ('wrong_kind', 'ETHUSDT: entry/protection outcome unresolved: timeout'),
    ('bad_bar', 'ETHUSDT: entry/protection outcome unresolved: timeout'),
    ('bad_coi', 'ETHUSDT: entry/protection outcome unresolved: timeout'),
    ('bad_phase', 'operation recovery failed: timeout'),
    ('none', 'ETHUSDT: entry/protection outcome unresolved: '),
    ('none', 'prefix ETHUSDT: entry/protection outcome unresolved: timeout'),
    ('none', 'ETHUSDT: stop amendment outcome unresolved: timeout'),
    ('none', 'authentication failed: token rejected'),
    ('none', 'schema validation failed: unknown field'),
])
def test_unbound_or_general_failures_do_not_gain_release_classification(tmp_path, change, reason):
    venue, broker, trader = setup(tmp_path)
    try:
        op = execution_operation('entry')
        if change == 'absent': op = None
        if change == 'wrong_symbol': op['symbol'] = 'BTCUSDT'
        if change == 'wrong_kind': op['kind'] = 'unexpected'
        if change == 'bad_bar': op['bar_ms'] = True
        if change == 'bad_coi': op['coi'] = True
        if change == 'bad_phase': op['phase'] = 'unknown'
        trader.pending_operation = op
        trader._halt(reason)
        assert trader.halt_reasons == [reason]
        assert classify_reason(reason, trader.universe) is None
        assert list(trader.journal.events('execution_halt_detail')) == []
        assert venue.sent == []
    finally:
        broker.close()


def test_resolution_receipts_survive_snapshot_restart_without_resetting_safety(tmp_path):
    venue, broker, trader = setup(tmp_path)
    try:
        assert trader.safety_resolutions_applied == {}
        trader.safety_resolutions_applied['case-id'] = {'case_digest': 'reviewed', 'applied_ms': 100}
        trader.halted = True
        trader.halt_reasons = ['retained reason']
        trader._save()
        restarted = Trader(trader.config, broker, trader.markets, trader.journal)
        assert restarted.safety_resolutions_applied == trader.safety_resolutions_applied
        assert restarted.halted and restarted.halt_reasons == ['retained reason']
        assert venue.sent == []
    finally:
        broker.close()


def test_malformed_resolution_receipts_are_not_coerced_to_an_empty_object(tmp_path):
    venue, broker, trader = setup(tmp_path)
    try:
        trader._save()
        saved = trader.journal.load_snapshot()
        saved['safety_resolutions_applied'] = []
        trader.journal.save_snapshot(saved)
        with pytest.raises(ValueError, match='resolution receipts'):
            Trader(trader.config, broker, trader.markets, trader.journal)
        assert venue.sent == []
    finally:
        broker.close()


class StopSignal:
    def __init__(self): self.stopped = False
    def is_set(self): return self.stopped
    def set(self): self.stopped = True
    def wait(self, seconds): self.stopped = True


@pytest.mark.parametrize('failure', [None, 'poll', 'service', 'before', 'after'])
def test_resolution_hooks_run_inside_writer_in_protection_order(tmp_path, monkeypatch, failure):
    calls = []
    health_events = []
    stop = StopSignal()
    monkeypatch.setattr(run, 'STOP_REQUESTED', stop)
    monkeypatch.setattr(run, 'time', SimpleNamespace(time=lambda: 1000.))

    def stage(name):
        calls.append(name)
        if failure == name:
            raise OSError('controlled failure')

    class Broker:
        def poll(self):
            stage('poll')
            return []
        def audit_retired_orders(self, *, blocked_reason=None): pass
    monkeypatch.setattr(run, 'StrategyBroker', Broker)
    class Controller:
        def failure(self, exc, stream): calls.append(('failure', stream))
        def cycle(self, trader, coverage, **kwargs): calls.append(('cycle', kwargs))
    class Resolution:
        def before_service(self): stage('before')
        def after_service(self, coverage):
            assert ('success', 'poll') in health_events
            assert ('success', 'service') in health_events
            stage('after')
    class Health:
        def success(self, stream, *args): health_events.append(('success', stream))
        def failure(self, stream, *args): health_events.append(('failure', stream))
    class Future:
        def done(self): return False
    class Pool:
        def __init__(self, **kwargs): pass
        def submit(self, *args): return Future()
        def shutdown(self, **kwargs): pass
    monkeypatch.setattr(run, 'ThreadPoolExecutor', Pool)
    monkeypatch.setattr(run, 'RuntimeHealth', lambda *args: Health())
    reports = []
    def persist(*args, **kwargs):
        reports.append(kwargs)
        stop.set()
    monkeypatch.setattr(run, 'persist_report', persist)
    trader = SimpleNamespace(broker=Broker(), service=lambda **kwargs: stage('service'),
        exchange_recovery=Controller(), safety_resolution=Resolution(), last_bar_ms=0,
        journal=SimpleNamespace(append=lambda *args, **kwargs: None))
    config = LiveConfig(state_dir=tmp_path)
    run.run_loop(trader, {}, config, float('inf'))
    names = [value for value in calls if isinstance(value, str)]
    assert names[0:2] == ['poll', 'before']
    assert 'service' in names  # Even a malformed operator case cannot skip risk service.
    assert ('after' in names) is (failure in (None, 'after'))
    cycle = next(value[1] for value in calls if isinstance(value, tuple) and value[0] == 'cycle')
    assert cycle['poll_ok'] is (failure != 'poll')
    assert cycle['service_ok'] is (failure not in ('before', 'service', 'after'))
    if failure:
        stream = ('poll' if failure == 'poll' else
                  'safety_resolution' if failure in ('before', 'after') else 'service')
        assert ('failure', stream) in calls
        assert ('failure', 'poll' if failure == 'poll' else 'service') in health_events
        if failure in ('before', 'after'):
            assert health_events[-1] == ('failure', 'service')
    else:
        assert names == ['poll', 'before', 'service', 'after']


def test_reports_expose_resolution_status_without_changing_strategy(tmp_path):
    venue, broker, trader = setup(tmp_path)
    try:
        status = {'pending': 0, 'last_decision': 'none'}
        trader.safety_resolution = SimpleNamespace(status=lambda: status.copy())
        run.persist_report(trader, trader.config)
        for filename in ('runner_status.json', 'report.json'):
            report = json.loads((tmp_path / filename).read_text())
            assert report['safety_resolution'] == status
        assert venue.sent == [] and not trader.halted
    finally:
        broker.close()


def test_production_binds_resolution_to_approved_campaign_identity(tmp_path, monkeypatch):
    from live import production
    live = LiveConfig(mode='live', account_index=100, api_key_index=4, state_dir=tmp_path)
    cfg = {'account_lock_path': str(tmp_path / 'account.lock')}
    fingerprint = 'reviewed-code-and-config'
    approval = {'approved': True, 'account_index': 100, 'fingerprint': fingerprint,
                'automatic_recovery_enabled': True, 'started_at_ms': 123,
                'initial_equity': '250.00'}
    (tmp_path / 'activation.json').write_text(json.dumps(approval))
    transport = SimpleNamespace(read_diagnostics_error=None, close=lambda: None)
    broker = SimpleNamespace(close=lambda: None)
    trader = SimpleNamespace(broker=broker, journal=SimpleNamespace(append=lambda *a, **kw: None))
    calls = []
    manager = object()
    def create(*args):
        assert trader.exchange_recovery is not None
        calls.append(args)
        return manager
    def loop(current, markets, config, deadline):
        assert current.safety_resolution is manager
        assert not current.broker.entry_guard()
    monkeypatch.setattr(production, 'SafetyResolution', create)
    monkeypatch.setattr(production.signal, 'signal', lambda *a: None)
    monkeypatch.setattr(production, 'connect', lambda *a: transport)
    monkeypatch.setattr(run, 'build_authenticated_strategy', lambda *a, **kw: (trader, {}))
    monkeypatch.setattr(run, 'run_loop', loop)
    production.run_service(cfg, live, fingerprint)
    assert calls == [(trader, fingerprint, 100, 'mainnet', 123)]
    assert (tmp_path / 'EXCHANGE_PAUSED').exists()


def recovered(directory):
    clock = Clock()
    controller = ExchangeRecovery(directory, 'reviewed', clock=clock.wall,
                                  monotonic=clock.monotonic)
    trader = SimpleNamespace(halted=False, pending_operation=None, unresolved_operations={},
        residual_exits={}, orphan_orders={}, data_gaps=[], publication_pending=False,
        last_bar_ms=clock.ms // H4 * H4 - H4,
        broker=SimpleNamespace(transport=SimpleNamespace(read_diagnostics_error=None),
                               last_report={'execution_changes_verified': True}))
    coverage = Coverage()
    for number in range(3):
        if number: clock.advance(60)
        controller.cycle(trader, coverage, poll_ok=True, service_ok=True, now_ms=clock.ms)
    assert controller.can_enter()
    return controller, clock, trader, coverage


def cycle(controller, clock, trader, coverage, advance=60):
    clock.advance(advance)
    trader.last_bar_ms = clock.ms // H4 * H4 - H4
    controller.cycle(trader, coverage, poll_ok=True, service_ok=True, now_ms=clock.ms)


def test_release_requires_three_new_cycles_and_preserves_manual_pause(tmp_path):
    controller, clock, trader, coverage = recovered(tmp_path)
    marker = b'{"reason":"operator pause"}'
    (tmp_path / 'ENTRY_PAUSED').write_bytes(marker)
    controller.require_revalidation()
    assert controller.path.exists() and not controller.ready
    assert controller.count == 0 and controller.first_success is None and controller.last_success is None
    assert controller.reason == 'safety_resolution_verification'
    for number in range(3):
        cycle(controller, clock, trader, coverage)
        assert controller.ready is (number == 2)
        assert not controller.can_enter()
    assert (tmp_path / 'ENTRY_PAUSED').read_bytes() == marker
    ready = list(Journal(tmp_path).events('exchange_recovery_ready'))[-1]
    assert ready['consecutive_successes'] == 3 and ready['stable_seconds'] == 120


@pytest.mark.parametrize('interruption', ['long_gap', 'failure', 'restart'])
def test_release_revalidation_cannot_reuse_successes_across_interruptions(tmp_path, interruption):
    controller, clock, trader, coverage = recovered(tmp_path)
    controller.require_revalidation()
    cycle(controller, clock, trader, coverage)
    assert controller.count == 1
    if interruption == 'failure':
        controller.failure(temporary_failure(), 'poll')
    elif interruption == 'restart':
        controller = ExchangeRecovery(tmp_path, 'reviewed', clock=clock.wall,
                                      monotonic=clock.monotonic)
    cycle(controller, clock, trader, coverage, advance=91 if interruption == 'long_gap' else 60)
    assert controller.count == 1 and not controller.can_enter()
    cycle(controller, clock, trader, coverage)
    assert not controller.can_enter()
    cycle(controller, clock, trader, coverage)
    assert controller.can_enter()


def test_revalidation_never_clears_halt_or_bypasses_current_confirmed_bar(tmp_path):
    controller, clock, trader, coverage = recovered(tmp_path)
    controller.require_revalidation()
    trader.halted = True
    for _ in range(3): cycle(controller, clock, trader, coverage)
    assert trader.halted and not controller.ready and controller.count == 0
    trader.halted = False
    trader.last_bar_ms -= H4
    clock.advance(60)
    controller.cycle(trader, coverage, poll_ok=True, service_ok=True, now_ms=clock.ms)
    assert not controller.ready and controller.count == 0


def test_revalidation_storage_failure_is_a_hard_failure(tmp_path, monkeypatch):
    controller, clock, trader, coverage = recovered(tmp_path)
    def fail(*args): raise OSError('controlled storage failure')
    monkeypatch.setattr(recovery, 'atomic', fail)
    with pytest.raises(RuntimeError, match='safety release blocked'):
        controller.require_revalidation()
    assert controller.storage_error and not controller.can_enter() and controller.count == 0
    with pytest.raises(RuntimeError, match='safety release blocked'):
        controller.require_revalidation()


def test_revalidation_will_not_adopt_another_owner_marker(tmp_path):
    controller, _, _, _ = recovered(tmp_path)
    marker = b'{"owner":"different","session":"different","fingerprint":"different"}'
    controller.path.write_bytes(marker)
    with pytest.raises(RuntimeError, match='safety release blocked'):
        controller.require_revalidation()
    assert controller.path.read_bytes() == marker and not controller.ready

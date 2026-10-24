"""Offline production/runner integration: no credentials, exchange, or orders."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from live import production, run
from live.authenticated import APIReadError
from live.config import LiveConfig
from live.recovery import OWNER


NOW = 1_000_000
FINGERPRINT = 'reviewed-code-and-config'


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


def temporary_error():
    return APIReadError('safe read error', code='http_503', endpoint='/api/v1/account',
                        automatic_recovery_allowed=True, http_status=503)


@pytest.fixture
def campaign(tmp_path, monkeypatch):
    live = LiveConfig(mode='live', account_index=100, api_key_index=4,
                      state_dir=tmp_path / 'state')
    cfg = {'account_lock_path': str(tmp_path / 'account.lock')}
    approved = {'approved': True, 'account_index': live.account_index,
                'fingerprint': FINGERPRINT, 'automatic_recovery_enabled': True,
                'started_at_ms': 100, 'initial_equity': '250.00'}
    recovery = {'owner': OWNER, 'fingerprint': FINGERPRINT, 'session': 'test-session',
                'enabled': True, 'state': 'paused', 'storage_error': None}
    state = {'updated_ms': NOW, 'last_successful_service_ms': NOW - 181_000,
             'last_successful_poll_ms': NOW - 181_000, 'automatic_recovery': recovery}
    put(live.state_dir / 'activation.json', approved)
    put(live.state_dir / 'runner_status.json', state)
    put(live.state_dir / 'EXCHANGE_PAUSED', recovery)
    monkeypatch.setattr(production, 'load', lambda path: (cfg, live, FINGERPRINT))
    monkeypatch.setattr(production, 'time', SimpleNamespace(time=lambda: NOW / 1000))
    def systemctl_read_only(command, **kwargs):
        assert command == ['systemctl', 'is-active', '--quiet', production.UNIT]
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(production.subprocess, 'run', systemctl_read_only)
    monkeypatch.setattr(production, 'connect', lambda cfg: pytest.fail('monitor must not connect'))
    return cfg, live, approved, recovery, state


def command(monkeypatch, name):
    monkeypatch.setattr(sys, 'argv', ['production', name, '--config', 'not-read-by-test.json'])
    production.main()


@pytest.mark.parametrize('stamp',[None,True,'invalid'])
def test_initial_loop_outage_can_wait_without_inventing_previous_success(campaign,monkeypatch,stamp):
    _,live,_,recovery,state=campaign
    recovery['phase']='bootstrap_wait'
    state['last_successful_poll_ms']=stamp
    state['last_successful_service_ms']=stamp
    put(live.state_dir/'runner_status.json',state)
    put(live.state_dir/'EXCHANGE_PAUSED',recovery)
    command(monkeypatch,'health')
    assert (live.state_dir/'ENTRY_PAUSED').exists() is (stamp is not None)


def test_timer_preserves_temporary_pause_without_permanent_operator_latch(campaign, monkeypatch):
    _, live, _, _, _ = campaign
    original = (live.state_dir / 'EXCHANGE_PAUSED').read_bytes()
    command(monkeypatch, 'health')
    assert not (live.state_dir / 'ENTRY_PAUSED').exists()
    assert (live.state_dir / 'EXCHANGE_PAUSED').read_bytes() == original
    attention = json.loads((live.state_dir / 'attention.json').read_text())
    assert attention['healthy'] is False and attention['entries_paused'] is True


@pytest.mark.parametrize('change', [
    {'updated_ms': NOW - 181_000}, {'updated_ms': None},
    {'halted': True, 'halt_reasons': ['uncertain execution']},
    {'pending_operation': {'id': 1}}, {'residual_exits': ['ETH']},
    {'orphan_orders': ['unknown']}, {'data_gaps': ['missing']},
    {'api_read_diagnostics_error': 'OSError'},
])
def test_timer_safety_faults_still_create_permanent_pause(campaign, monkeypatch, change):
    _, live, _, _, state = campaign
    put(live.state_dir / 'runner_status.json', {**state, **change})
    command(monkeypatch, 'health')
    assert (live.state_dir / 'ENTRY_PAUSED').exists()
    assert (live.state_dir / 'EXCHANGE_PAUSED').exists()


@pytest.mark.parametrize('replacement', [None, [], 'invalid',
    {'approved': True, 'account_index': 100, 'fingerprint': 'different',
     'automatic_recovery_enabled': True},
    {'approved': True, 'account_index': 100, 'fingerprint': FINGERPRINT},
    {'approved': True, 'account_index': 100, 'fingerprint': FINGERPRINT,
     'automatic_recovery_enabled': 'true'},
])
def test_monitor_cannot_adopt_unapproved_or_malformed_activation(campaign, monkeypatch, replacement):
    _, live, _, _, _ = campaign
    put(live.state_dir / 'activation.json', replacement)
    command(monkeypatch, 'health')
    assert (live.state_dir / 'ENTRY_PAUSED').exists()


@pytest.mark.parametrize('change', [None, [], 'invalid',
    {'owner': 'other'}, {'session': 'different'}, {'fingerprint': 'different'},
    {'state': 'ready'},
])
def test_monitor_cannot_adopt_malformed_or_changed_exchange_marker(campaign, monkeypatch, change):
    _, live, _, recovery, _ = campaign
    put(live.state_dir / 'EXCHANGE_PAUSED', {**recovery, **change} if isinstance(change, dict) else change)
    command(monkeypatch, 'health')
    assert (live.state_dir / 'ENTRY_PAUSED').exists()


@pytest.mark.parametrize('filename', ['EXCHANGE_PAUSED', 'activation.json'])
def test_monitor_truncated_json_still_creates_safety_pause(campaign, monkeypatch, filename):
    _, live, _, _, _ = campaign
    (live.state_dir / filename).write_text('{truncated', encoding='utf-8')
    command(monkeypatch, 'health')
    assert (live.state_dir / 'ENTRY_PAUSED').exists()


@pytest.mark.parametrize('change', [{'enabled': False}, {'enabled': 'true'},
    {'storage_error': 'OSError'}, {'fingerprint': 'different'}, {'state': 'ready'}])
def test_monitor_requires_matching_enabled_runtime_recovery(campaign, monkeypatch, change):
    _, live, _, recovery, state = campaign
    put(live.state_dir / 'runner_status.json', {**state, 'automatic_recovery': {**recovery, **change}})
    command(monkeypatch, 'health')
    assert (live.state_dir / 'ENTRY_PAUSED').exists()


def test_status_remains_read_only_and_does_not_resume(campaign, monkeypatch):
    _, live, _, _, _ = campaign
    put(live.state_dir / 'ENTRY_PAUSED', {'reason': 'operator pause'})
    before = {p.name: p.read_bytes() for p in live.state_dir.iterdir()}
    command(monkeypatch, 'status')
    after = {p.name: p.read_bytes() for p in live.state_dir.iterdir()}
    assert after == before


def test_bootstrap_monitor_accepts_missing_initial_success_only_during_bootstrap(campaign, monkeypatch):
    _, live, _, recovery, state = campaign
    put(live.state_dir / 'runner_status.json', {**state,
        'last_successful_service_ms': None, 'last_successful_poll_ms': None,
        'automatic_recovery': {**recovery, 'phase': 'bootstrap_wait'}})
    command(monkeypatch, 'health')
    assert not (live.state_dir / 'ENTRY_PAUSED').exists()


class StopSignal:
    def __init__(self, wait_results=()):
        self.wait_results = iter(wait_results)
        self.waits = []
        self.stopped = False
    def clear(self): self.stopped = False
    def set(self): self.stopped = True
    def is_set(self): return self.stopped
    def wait(self, seconds):
        self.waits.append(seconds)
        self.stopped = next(self.wait_results)
        return self.stopped


def bootstrap_fixture(monkeypatch, campaign, *, waits=(False,)):
    cfg, live, approval, _, _ = campaign
    # This fixture isolates bootstrap/recovery orchestration. Resolution has
    # separate identity and queue integration coverage; no cases are queued.
    monkeypatch.setattr(production, 'SafetyResolution', lambda *args: SimpleNamespace(
        before_service=lambda: None, after_service=lambda coverage: None,
        status=lambda: {'pending': 0}))
    # Real controller must create its own session and own its files, not inherit
    # the monitor fixture's synthetic marker.
    (live.state_dir / 'EXCHANGE_PAUSED').unlink()
    stop = StopSignal(waits)
    monkeypatch.setattr(run, 'STOP_REQUESTED', stop)
    monkeypatch.setattr(production.signal, 'signal', lambda *args: None)
    events = []
    transport = SimpleNamespace(read_diagnostics_error=None,
                                close=lambda: events.append('transport_close'))
    broker = SimpleNamespace(poll=lambda: events.append('initial_poll'),
                             close=lambda: events.append('broker_close'))
    trader = SimpleNamespace(broker=broker,
                             service=lambda: events.append('initial_service'),
                             journal=SimpleNamespace(append=lambda *a, **kw: events.append('journal')))
    def loop(t, markets, live_arg, deadline):
        assert t is trader and live_arg is live
        events.append('loop')
        assert t.broker.initial_allocated_equity == approval['initial_equity']
        if t.exchange_recovery:
            assert not t.broker.entry_guard(), 'startup verification must block new entries'
    monkeypatch.setattr(run, 'run_loop', loop)
    return cfg, live, stop, transport, trader, events


@pytest.mark.parametrize('failure_stage', ['connect', 'market_load'])
def test_bootstrap_temporary_read_failure_waits_then_starts_normal_loop(campaign, monkeypatch, failure_stage):
    cfg, live, stop, transport, trader, events = bootstrap_fixture(monkeypatch, campaign)
    calls = {'connect': 0, 'market_load': 0}
    def connect(_):
        calls['connect'] += 1
        if failure_stage == 'connect' and calls['connect'] == 1:
            raise temporary_error()
        return transport
    def build(*args, **kwargs):
        calls['market_load'] += 1
        if failure_stage == 'market_load' and calls['market_load'] == 1:
            raise temporary_error()
        return trader, {}
    monkeypatch.setattr(production, 'connect', connect)
    monkeypatch.setattr(run, 'build_authenticated_strategy', build)
    production.run_service(cfg, live, FINGERPRINT)
    assert stop.waits == [60]
    assert events == ['journal', 'loop', 'broker_close', 'transport_close']
    assert calls['connect'] == (2 if failure_stage == 'connect' else 1)
    assert calls['market_load'] == (2 if failure_stage == 'market_load' else 1)
    assert (live.state_dir / 'EXCHANGE_PAUSED').exists()
    assert not (live.state_dir / 'ENTRY_PAUSED').exists()


@pytest.mark.parametrize('failure_stage', ['connect', 'market_load'])
def test_bootstrap_stop_request_interrupts_wait_without_orders_or_loop(campaign, monkeypatch, failure_stage):
    cfg, live, stop, transport, _, events = bootstrap_fixture(monkeypatch, campaign, waits=(True,))
    def connect(_):
        if failure_stage == 'connect': raise temporary_error()
        return transport
    def build(*args, **kwargs): raise temporary_error()
    monkeypatch.setattr(production, 'connect', connect)
    monkeypatch.setattr(run, 'build_authenticated_strategy', build)
    production.run_service(cfg, live, FINGERPRINT)
    assert stop.waits == [60] and stop.is_set()
    assert events == ([] if failure_stage == 'connect' else ['transport_close'])


def test_nonopted_in_legacy_start_does_not_retry_connection_failure(campaign, monkeypatch):
    cfg, live, _, _, _, _ = bootstrap_fixture(monkeypatch, campaign, waits=())
    approval = json.loads((live.state_dir / 'activation.json').read_text())
    approval.pop('automatic_recovery_enabled')
    put(live.state_dir / 'activation.json', approval)
    def fail(_): raise temporary_error()
    monkeypatch.setattr(production, 'connect', fail)
    with pytest.raises(APIReadError): production.run_service(cfg, live, FINGERPRINT)
    assert not (live.state_dir / 'EXCHANGE_PAUSED').exists()


@pytest.mark.parametrize('failure', [RuntimeError('unclassified failure'),
    APIReadError('nonce failure', code='http_503', endpoint='/api/v1/nextNonce',
                 automatic_recovery_allowed=False)])
def test_opted_in_bootstrap_still_refuses_unknown_or_unsafe_retry(campaign, monkeypatch, failure):
    cfg, live, stop, transport, _, events = bootstrap_fixture(monkeypatch, campaign, waits=())
    monkeypatch.setattr(production, 'connect', lambda _: transport)
    def fail(*args, **kwargs): raise failure
    monkeypatch.setattr(run, 'build_authenticated_strategy', fail)
    with pytest.raises(RuntimeError): production.run_service(cfg, live, FINGERPRINT)
    assert stop.waits == [] and events == ['transport_close']


def test_legacy_start_retains_initial_poll_and_service(campaign, monkeypatch):
    cfg, live, stop, transport, trader, events = bootstrap_fixture(monkeypatch, campaign, waits=())
    approval = json.loads((live.state_dir / 'activation.json').read_text())
    approval.pop('automatic_recovery_enabled')
    put(live.state_dir / 'activation.json', approval)
    monkeypatch.setattr(production, 'connect', lambda _: transport)
    monkeypatch.setattr(run, 'build_authenticated_strategy', lambda *args, **kwargs: (trader, {}))
    production.run_service(cfg, live, FINGERPRINT)
    assert events == ['initial_poll', 'initial_service', 'journal', 'loop', 'broker_close', 'transport_close']
    assert stop.waits == [] and trader.exchange_recovery is None


@pytest.mark.parametrize('failure_stage', ['poll', 'service', 'bar', None])
def test_loop_reports_failures_and_counts_cycles_only_after_poll_and_service(tmp_path, monkeypatch, failure_stage):
    events = []
    clock = SimpleNamespace(now=1000.0)
    stop = StopSignal()
    def wait(seconds):
        stop.waits.append(seconds)
        clock.now += 60
        stop.stopped = len(stop.waits) >= 3
        return stop.stopped
    stop.wait = wait
    monkeypatch.setattr(run, 'time', SimpleNamespace(time=lambda: clock.now))
    monkeypatch.setattr(run, 'STOP_REQUESTED', stop)
    counts = {'poll': 0, 'service': 0, 'bar': 0}
    class Broker:
        def poll(self):
            counts['poll'] += 1
            events.append('poll')
            if failure_stage == 'poll' and counts['poll'] == 1: raise temporary_error()
            return []
        def audit_retired_orders(self, *, blocked_reason=None): pass
    monkeypatch.setattr(run, 'StrategyBroker', Broker)
    class Recovery:
        def failure(self, error, stream): events.append(('failure', stream))
        def cycle(self, trader, coverage, **kwargs): events.append(('cycle', kwargs))
    def service(**kwargs):
        counts['service'] += 1
        events.append(('service', kwargs))
        if failure_stage == 'service' and counts['service'] == 1: raise temporary_error()
    trader = SimpleNamespace(broker=Broker(), exchange_recovery=Recovery(), service=service,
        journal=SimpleNamespace(append=lambda *args, **kwargs: None), last_bar_ms=0)
    class Future:
        def done(self): return True
        def result(self):
            counts['bar'] += 1
            if failure_stage == 'bar' and counts['bar'] == 1: raise temporary_error()
            return {}
    class Pool:
        def __init__(self, **kwargs): pass
        def submit(self, *args): return Future()
        def shutdown(self, **kwargs): events.append('shutdown')
    monkeypatch.setattr(run, 'ThreadPoolExecutor', Pool)
    monkeypatch.setattr(run, 'RuntimeHealth', lambda *args: SimpleNamespace(
        success=lambda *args: None, failure=lambda *args: None))
    monkeypatch.setattr(run, 'run_once', lambda *args, **kwargs: None)
    reports = []
    monkeypatch.setattr(run, 'persist_report', lambda *args, **kwargs: reports.append(kwargs))
    live = LiveConfig(mode='live', account_index=100, api_key_index=4, state_dir=tmp_path)
    run.run_loop(trader, {}, live, float('inf'))
    cycles = [event[1] for event in events if isinstance(event, tuple) and event[0] == 'cycle']
    assert len(cycles) == 3
    assert cycles[0]['poll_ok'] is (failure_stage != 'poll')
    assert cycles[0]['service_ok'] is (failure_stage != 'service')
    assert all(c['poll_ok'] and c['service_ok'] for c in cycles[1:])
    if failure_stage:
        assert ('failure', failure_stage) in events
    else:
        assert not any(isinstance(e, tuple) and e[0] == 'failure' for e in events)
    ordered = [e if isinstance(e, str) else e[0] for e in events]
    assert [e for e in ordered if e in ('poll', 'service', 'cycle')] == ['poll', 'service', 'cycle'] * 3
    assert counts['poll'] == counts['service'] == 3
    assert reports[-1]['poll_errors'] == int(failure_stage == 'poll')
    assert reports[-1]['service_errors'] == int(failure_stage == 'service')

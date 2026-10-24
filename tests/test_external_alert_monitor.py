"""Offline observer tests: no exchange, HTTP request, or trading mutation."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'ops' / 'alerts' / 'monitor.py'
SPEC = importlib.util.spec_from_file_location('external_alert_monitor_under_test', SOURCE)
monitor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = monitor
SPEC.loader.exec_module(monitor)

UUID = '12345678-1234-4234-8234-123456789abc'
FINGERPRINT = 'a' * 64
BOUNDARY = 1_800_000_000_000 // monitor.BAR_MS * monitor.BAR_MS
NOW = BOUNDARY + 180_000
START = NOW - 30 * monitor.BAR_MS


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('network attempted in offline tests')
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)


def write(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')
    if os.name == 'posix':
        path.chmod(0o600)


def probe(config):
    return {'active': True, 'pid': 42, 'timer_active': True,
            'health_service_ok': True, 'health_service_fresh': True}


@pytest.fixture
def state(tmp_path):
    directory = tmp_path / 'trading'
    directory.mkdir()
    secret = tmp_path / 'healthchecks.uuid'
    secret.write_text(UUID, encoding='ascii')
    if os.name == 'posix':
        secret.chmod(0o600)
    config = monitor.Config(directory, tmp_path / 'observer', secret, FINGERPRINT, 123)
    coverage = {'updated_ms': NOW, 'active_gap_streams': [], 'coverage_complete': False,
                'failure_count': 27,
                'streams': {name: {'active_gap': None} for name in ('poll', 'service', 'bar', 'observation')}}
    snapshot = {'last_bar_ms': BOUNDARY - monitor.BAR_MS, 'halted': False,
                'pending_operation': None, 'unresolved_operations': {}, 'residual_exits': {},
                'orphan_orders': {}, 'data_gaps': [], 'publication_pending': False,
                'positions': {}, 'orders': {}}
    runner = {**deepcopy(snapshot), 'updated_ms': NOW, 'pid': 42, 'mode': 'live',
              'last_successful_poll_ms': NOW - 1000, 'last_successful_service_ms': NOW - 500,
              'entries_paused': False, 'api_read_diagnostics_error': None,
              'runtime_coverage': deepcopy(coverage), 'observations': 0,
              'last_error': 'historic response containing PRIVATE-RAW-ERROR',
              'automatic_recovery': {'owner': monitor.OWNER, 'fingerprint': FINGERPRINT,
                 'session': 'synthetic-session', 'enabled': True, 'state': 'ready',
                 'phase': 'running', 'blockers': [], 'storage_error': None, 'last_cycle_ms': NOW - 500}}
    report = {'updated_ms': NOW, 'start_ms': START, 'classification': 'authenticated_account_ledger',
              'execution_changes_verified': True, 'owned_exposure_matches_account': True,
              'historical_evidence_complete': True, 'trader_halted': False,
              'publication_pending': False, 'unsafe_protection': {}, 'unresolved_orders': {},
              'unresolved_operations': {}, 'strategy_data_gaps': [], 'historical_issues': [],
              'history_collection': {'head_complete': True}, 'runtime_coverage': deepcopy(coverage),
              'performance_complete': False, 'actual_account_equity': 'SECRET-BALANCE'}
    activation = {'approved': True, 'automatic_recovery_enabled': True,
                  'fingerprint': FINGERPRINT, 'account_index': 123, 'started_at_ms': START}
    documents = {'runner_status.json': runner, 'report.json': report,
                 'snapshot.json': snapshot, 'activation.json': activation}
    for name, value in documents.items():
        write(directory / name, value)
    return config, documents


def assess(state, now=NOW, **kwargs):
    config, documents = state
    for name, value in documents.items():
        write(config.state_dir / name, value)
    return monitor.evaluate(config, now_ms=now, service_probe=kwargs.pop('service_probe', probe), **kwargs)


def test_healthy_ignores_historical_errors_fees_and_old_snapshot_mtime(state):
    config, _ = state
    os.utime(config.state_dir / 'snapshot.json', (1, 1))
    result = monitor.evaluate(config, now_ms=NOW, service_probe=probe)
    assert result['healthy'] is True and result['reason_codes'] == []
    assert result['checked_at_ms'] == NOW
    assert set(result) == {'healthy', 'reason_codes', 'checked_at_ms', 'health_tracking'}
    assert 'PRIVATE' not in json.dumps(result) and 'SECRET' not in json.dumps(result)


@pytest.mark.parametrize('name', ['runner_status.json', 'snapshot.json', 'report.json', 'activation.json'])
def test_missing_document_never_passes(state, name):
    config, _ = state
    (config.state_dir / name).unlink()
    assert monitor.evaluate(config, now_ms=NOW, service_probe=probe)['reason_codes'] == ['STATE_MISSING']


@pytest.mark.parametrize('raw', ['[]', '{', '{"x":NaN}', '{"x":1,"x":2}'])
def test_malformed_duplicate_or_nonfinite_json_is_not_accepted(state, raw):
    config, _ = state
    (config.state_dir / 'runner_status.json').write_text(raw)
    assert monitor.evaluate(config, now_ms=NOW, service_probe=probe)['reason_codes'] == ['STATE_MALFORMED']


def test_bounded_file_read(state):
    config, _ = state
    (config.state_dir / 'report.json').write_bytes(b' ' * (monitor.MAX_JSON_BYTES + 1))
    assert monitor.evaluate(config, now_ms=NOW, service_probe=probe)['reason_codes'] == ['STATE_TOO_LARGE']


@pytest.mark.parametrize('document,key', [
    ('runner_status.json', 'updated_ms'), ('report.json', 'updated_ms'),
    ('runner_status.json', 'last_successful_poll_ms'), ('runner_status.json', 'last_successful_service_ms')])
@pytest.mark.parametrize('value,code', [(NOW - 180001, 'STATE_STALE'), (NOW + 1, 'STATE_FUTURE'),
                                      (None, 'STATE_MALFORMED'), (True, 'STATE_MALFORMED')])
def test_status_and_reconciliation_timestamps_fail_closed(state, document, key, value, code):
    state[1][document][key] = value
    result = assess(state)
    assert not result['healthy'] and code in result['reason_codes']


@pytest.mark.parametrize('document,key,value,code', [
    ('runner_status.json', 'halted', True, 'STRATEGY_HALTED'),
    ('snapshot.json', 'halted', True, 'STRATEGY_HALTED'),
    ('report.json', 'trader_halted', True, 'STRATEGY_HALTED'),
    ('runner_status.json', 'entries_paused', True, 'ENTRIES_PAUSED'),
    ('snapshot.json', 'pending_operation', {'private': 'SECRET'}, 'PENDING_OPERATION'),
    ('runner_status.json', 'unresolved_operations', {'x': 1}, 'UNRESOLVED_ORDERS'),
    ('snapshot.json', 'residual_exits', {'x': 1}, 'RESIDUAL_EXIT'),
    ('snapshot.json', 'orphan_orders', {'x': 1}, 'ORPHAN_ORDERS'),
    ('snapshot.json', 'data_gaps', ['SECRET'], 'DATA_GAP'),
    ('report.json', 'unsafe_protection', {'x': 1}, 'PROTECTION_UNSAFE'),
    ('report.json', 'unresolved_orders', {'x': 1}, 'UNRESOLVED_ORDERS'),
    ('report.json', 'execution_changes_verified', False, 'EXECUTION_UNVERIFIED'),
    ('report.json', 'owned_exposure_matches_account', False, 'OWNERSHIP_MISMATCH'),
    ('report.json', 'historical_evidence_complete', False, 'HISTORY_UNVERIFIED'),
    ('report.json', 'historical_issues', [{'raw': 'SECRET'}], 'HISTORY_UNVERIFIED'),
    ('runner_status.json', 'publication_pending', True, 'PUBLICATION_PENDING'),
    ('runner_status.json', 'api_read_diagnostics_error', 'SECRET', 'STORAGE_ERROR'),
    ('runner_status.json', 'halted', 0, 'STATE_MALFORMED'),
    ('report.json', 'unsafe_protection', None, 'STATE_MALFORMED'),
])
def test_safety_flags_are_strict_and_redacted(state, document, key, value, code):
    state[1][document][key] = value
    result = assess(state)
    assert not result['healthy'] and code in result['reason_codes']
    assert 'SECRET' not in json.dumps(result)


@pytest.mark.parametrize('name', ['runner_status.json', 'report.json'])
def test_active_gap_checked_even_if_summary_is_empty(state, name):
    state[1][name]['runtime_coverage']['streams']['service']['active_gap'] = {}
    assert 'CURRENT_COVERAGE_GAP' in assess(state)['reason_codes']


@pytest.mark.parametrize('key,value,code', [
    ('state', 'paused', 'RECOVERY_NOT_READY'), ('enabled', False, 'RECOVERY_NOT_READY'),
    ('phase', 'bootstrap_wait', 'RECOVERY_NOT_READY'), ('blockers', ['SECRET'], 'RECOVERY_NOT_READY'),
    ('storage_error', 'SECRET', 'STORAGE_ERROR'), ('fingerprint', 'b' * 64, 'OWNERSHIP_MISMATCH'),
    ('owner', 'other', 'OWNERSHIP_MISMATCH'), ('last_cycle_ms', NOW - 90001, 'RECOVERY_STALE'),
    ('last_cycle_ms', NOW + 1, 'STATE_FUTURE')])
def test_ready_permission_requires_current_owned_evidence(state, key, value, code):
    state[1]['runner_status.json']['automatic_recovery'][key] = value
    assert code in assess(state)['reason_codes']


def test_recovery_does_not_recount_cycles_or_accept_head_backlog(state):
    state[1]['runner_status.json']['automatic_recovery'].update(consecutive_successes=999, stable_seconds=999)
    state[1]['report.json']['history_collection']['head_complete'] = False
    assert 'HISTORY_UNVERIFIED' in assess(state)['reason_codes']


@pytest.mark.parametrize('key,value,code', [('approved', False, 'ACTIVATION_INVALID'),
    ('fingerprint', 'b' * 64, 'OWNERSHIP_MISMATCH'), ('account_index', 999, 'OWNERSHIP_MISMATCH'),
    ('started_at_ms', START + 1, 'OWNERSHIP_MISMATCH')])
def test_activation_identity_and_campaign_must_match(state, key, value, code):
    state[1]['activation.json'][key] = value
    assert code in assess(state)['reason_codes']


def test_matching_but_future_campaign_start_is_not_accepted(state):
    state[1]['activation.json']['started_at_ms'] = NOW + 1
    state[1]['report.json']['start_ms'] = NOW + 1
    assert 'STATE_FUTURE' in assess(state)['reason_codes']


@pytest.mark.parametrize('name,code', [('ENTRY_PAUSED', 'ENTRIES_PAUSED'),
                                     ('EXCHANGE_PAUSED', 'EXCHANGE_PAUSED'), ('HALT', 'STRATEGY_HALTED')])
def test_markers_always_block_up_even_when_runner_says_ready(state, name, code):
    config, _ = state
    write(config.state_dir / name, {'owner': monitor.OWNER, 'fingerprint': FINGERPRINT, 'session': 'synthetic-session'})
    assert code in assess(state)['reason_codes']


def test_marker_created_during_read_cannot_be_reported_up(state):
    config, _ = state
    def racing_probe(cfg):
        write(config.state_dir / 'ENTRY_PAUSED', {'reason': 'PRIVATE'})
        return probe(cfg)
    result = assess(state, service_probe=racing_probe)
    assert {'ENTRIES_PAUSED', 'STATE_INCONSISTENT'} <= set(result['reason_codes'])


@pytest.mark.parametrize('offset,runner_old,snapshot_old,healthy', [
    (59_999, True, True, True), (60_000, True, True, False),
    (119_999, False, True, True), (120_000, False, True, False),
    (180_000, False, False, True)])
def test_bar_boundary_and_snapshot_save_race_have_bounded_grace(state, offset, runner_old, snapshot_old, healthy):
    now = BOUNDARY + offset
    for doc in state[1].values():
        if 'updated_ms' in doc:
            doc['updated_ms'] = now
        if 'runtime_coverage' in doc:
            doc['runtime_coverage']['updated_ms'] = now
    runner = state[1]['runner_status.json']
    runner['last_successful_poll_ms'] = runner['last_successful_service_ms'] = now
    runner['automatic_recovery']['last_cycle_ms'] = now
    runner['last_bar_ms'] -= monitor.BAR_MS if runner_old else 0
    state[1]['snapshot.json']['last_bar_ms'] -= monitor.BAR_MS if snapshot_old else 0
    assert assess(state, now)['healthy'] is healthy


@pytest.mark.parametrize('delta,code', [(-2 * monitor.BAR_MS, 'BAR_NOT_CURRENT'), (monitor.BAR_MS, 'STATE_FUTURE')])
def test_old_or_future_bar_is_not_hidden_by_transition(state, delta, code):
    state[1]['runner_status.json']['last_bar_ms'] += delta
    assert code in assess(state)['reason_codes']


@pytest.mark.parametrize('value,code', [({'active': False, 'pid': 42, 'timer_active': True}, 'SERVICE_INACTIVE'),
    ({'active': True, 'pid': 43, 'timer_active': True}, 'SERVICE_IDENTITY_MISMATCH'),
    ({'active': True, 'pid': 42, 'timer_active': False}, 'HEALTH_TIMER_INACTIVE')])
def test_systemd_and_runner_must_agree(state, value, code):
    assert code in assess(state, service_probe=lambda _: {**probe(None), **value})['reason_codes']


def test_systemd_probe_is_read_only_and_bounded(state, monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, b'Id=bb-squeeze-live.service\nActiveState=active\nSubState=running\nMainPID=42\n\nId=bb-squeeze-live-health.timer\nActiveState=active\nSubState=waiting\nMainPID=0\n\nId=bb-squeeze-live-health.service\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nExecMainExitTimestampMonotonic=999999000000\n')
    monkeypatch.setattr(monitor.subprocess, 'run', run)
    monkeypatch.setattr(monitor.time, 'monotonic_ns', lambda: 1_000_000_000_000_000)
    service = monitor.probe_services(state[0])
    assert {key: service[key] for key in probe(None)} == probe(state[0])
    assert calls[0][0][0:2] == ['systemctl', 'show'] and calls[0][1]['timeout'] == 5
    assert calls[0][1].get('shell') is None


@pytest.mark.parametrize('active,result,status,age_us,ok,fresh', [
    ('inactive', 'success', 0, 180_000_000, True, True),
    ('inactive', 'success', 0, 180_000_001, True, False),
    ('activating', 'success', 0, 60_000_000, True, False),
    ('activating', 'success', 0, 190_000_000, True, False),
    ('failed', 'exit-code', 1, 1_000_000, False, True),
    ('inactive', 'signal', 9, 1_000_000, False, True),
    ('inactive', 'success', 0, -1, True, False),
])
def test_health_timer_does_not_hide_failed_or_stalled_oneshot(state, monkeypatch, active, result, status, age_us, ok, fresh):
    now_us = 1_000_000_000_000
    payload = ('Id=bb-squeeze-live.service\nActiveState=active\nSubState=running\nMainPID=42\n\n'
               'Id=bb-squeeze-live-health.timer\nActiveState=active\nSubState=waiting\nMainPID=0\n\n'
               f'Id=bb-squeeze-live-health.service\nActiveState={active}\nResult={result}\n'
               f'ExecMainStatus={status}\nExecMainExitTimestampMonotonic={now_us - age_us}\n').encode()
    monkeypatch.setattr(monitor.subprocess, 'run', lambda args, **kwargs: subprocess.CompletedProcess(args, 0, payload))
    monkeypatch.setattr(monitor.time, 'monotonic_ns', lambda: now_us * 1000)
    service = monitor.probe_services(state[0])
    assert service['health_service_ok'] is ok and service['health_service_fresh'] is fresh
    assessment = assess(state, service_probe=lambda _: service)
    assert assessment['healthy'] is (ok and fresh)
    if not ok:
        assert 'HEALTH_SERVICE_FAILED' in assessment['reason_codes']
    if not fresh:
        assert 'HEALTH_SERVICE_STALE' in assessment['reason_codes']


@pytest.fixture
def transitioning_health(monkeypatch):
    """Model the observed systemd zero-exit window without services or sleeps."""
    clock = {'us': 1_000_000_000_000, 'start': 999_999_900_000, 'calls': [], 'sleeps': []}
    monkeypatch.setattr(monitor.time, 'monotonic_ns', lambda: clock['us'] * 1000)
    def sleep(seconds):
        clock['sleeps'].append(seconds)
        clock['us'] += round(seconds * 1_000_000)
    monkeypatch.setattr(monitor.time, 'sleep', sleep)
    def install(responder):
        def run(args, **kwargs):
            clock['calls'].append(kwargs['timeout'])
            fields = {'ActiveState':'activating', 'SubState':'start', 'Result':'success',
                      'ExecMainStatus':0, 'ExecMainStartTimestampMonotonic':clock['start'],
                      'ExecMainExitTimestampMonotonic':0}
            updates = responder(clock)
            if isinstance(updates, Exception): raise updates
            fields.update(updates)
            trade_active = fields.pop('trade_active', 'active')
            pid = fields.pop('trade_pid', 42)
            timer_active = fields.pop('timer_active', 'active')
            raw = (f'Id={monitor.SERVICE}\nActiveState={trade_active}\nSubState=running\nMainPID={pid}\n\n'
                   f'Id={monitor.HEALTH_TIMER}\nActiveState={timer_active}\n\n'
                   f'Id={monitor.HEALTH_SERVICE}\n' + ''.join(f'{k}={v}\n' for k,v in fields.items())).encode()
            return subprocess.CompletedProcess(args, 0, raw)
        monkeypatch.setattr(monitor.subprocess, 'run', run)
        return clock
    return install


def completed_health(clock):
    return {'ActiveState':'inactive', 'SubState':'dead',
            'ExecMainExitTimestampMonotonic':clock['us'] - 1000}


@pytest.mark.parametrize('duration_us', [1_106_264, 2_199_175, 3_300_283])
def test_observed_oneshot_race_waits_for_same_success_and_sends_only_up(state, transitioning_health, duration_us):
    clock = transitioning_health(lambda c: completed_health(c) if c['us']-c['start'] >= duration_us else {})
    sent = []
    result = monitor.run_once(state[0], now_ms=NOW, service_probe=monitor.probe_services,
                             sender=lambda cfg, healthy, codes: sent.append((healthy,codes)) or True)
    assert result['healthy'] and sent == [(True, [])]
    assert len(clock['calls']) > 1 and sum(clock['sleeps']) <= 5
    assert all(0 < t <= 5 for t in clock['calls'])


def test_normal_running_observation_is_pending_not_a_stale_or_up_decision(state, transitioning_health):
    clock = transitioning_health(lambda c: {})
    service = monitor.probe_services(state[0])
    assert not service['health_service_fresh']
    assert clock['us'] - clock['start'] <= 5_100_000
    assert len(clock['calls']) <= monitor.HEALTH_RECHECKS + 1
    result = assess(state, service_probe=lambda _:service)
    assert result['reason_codes'] == ['HEALTH_CHECK_RUNNING'] and not result['healthy']


@pytest.mark.parametrize('update', [
    {'ExecMainStartTimestampMonotonic':999_819_999_999},
    {'ExecMainStartTimestampMonotonic':1_000_000_000_001},
    {'ExecMainStartTimestampMonotonic':0},
    {'ExecMainStartTimestampMonotonic':'invalid'},
    {'ActiveState':'inactive', 'SubState':'dead'},
    {'SubState':'start-pre'}, {'Result':'exit-code','ExecMainStatus':1},
    {'trade_active':'inactive'}, {'timer_active':'inactive'},
])
def test_no_retry_for_old_invalid_failed_or_unhealthy_start(state, transitioning_health, update):
    clock = transitioning_health(lambda c:update)
    result = assess(state, service_probe=monitor.probe_services)
    assert not result['healthy'] and len(clock['calls']) == 1 and not clock['sleeps']


@pytest.mark.parametrize('update,code', [
    ({'ActiveState':'failed','Result':'exit-code','ExecMainStatus':1},'HEALTH_SERVICE_FAILED'),
    ({'ActiveState':'failed','Result':'timeout','ExecMainStatus':0},'HEALTH_SERVICE_FAILED'),
    ({'ActiveState':'inactive','Result':'signal','ExecMainStatus':9},'HEALTH_SERVICE_FAILED'),
    ({'timer_active':'inactive'},'HEALTH_TIMER_INACTIVE'),
    ({'trade_active':'inactive'},'SERVICE_INACTIVE'),
    ({'trade_pid':43},'SERVICE_IDENTITY_MISMATCH'),
    ({'ExecMainStartTimestampMonotonic':1_000_000_100_000},'HEALTH_SERVICE_STALE'),
    ({'ExecMainExitTimestampMonotonic':999_999_899_999},'HEALTH_SERVICE_STALE'),
])
def test_transition_does_not_hide_failure_or_changed_invocation(state, transitioning_health, update, code):
    clock = transitioning_health(lambda c: {} if len(c['calls'])==1 else {**completed_health(c),**update})
    result = assess(state, service_probe=monitor.probe_services)
    assert not result['healthy'] and code in result['reason_codes']
    assert len(clock['calls']) == 2


def test_timeout_during_recheck_is_not_normal_completion(state, transitioning_health):
    clock = transitioning_health(lambda c:{} if len(c['calls'])==1 else subprocess.TimeoutExpired('systemctl', .5))
    assert assess(state, service_probe=monitor.probe_services)['reason_codes'] == ['SERVICE_UNAVAILABLE']
    assert len(clock['calls']) == 2 and clock['calls'][1] < 5


def test_first_slow_read_cannot_reset_overall_five_second_budget(state, transitioning_health):
    def respond(c):
        c['us'] += 4_900_000
        return {'ExecMainStartTimestampMonotonic':c['us']-100_000}
    clock = transitioning_health(respond)
    assert not monitor.probe_services(state[0])['health_service_fresh']
    assert len(clock['calls']) == 1 and not clock['sleeps']


def refresh_observed_state(state, now):
    """Only test fixtures advance; no production or exchange operation is run."""
    for document in state[1].values():
        if 'updated_ms' in document:
            document['updated_ms'] = now
        if 'runtime_coverage' in document:
            document['runtime_coverage']['updated_ms'] = now
    runner = state[1]['runner_status.json']
    runner['last_successful_poll_ms'] = runner['last_successful_service_ms'] = now
    runner['automatic_recovery']['last_cycle_ms'] = now
    for name, value in state[1].items():
        write(state[0].state_dir / name, value)


def test_running_beyond_short_probe_wait_does_not_send_down_or_unverified_up(state, transitioning_health):
    clock = transitioning_health(lambda c: {})
    sent = []
    result = monitor.run_once(state[0], now_ms=NOW, service_probe=monitor.probe_services,
                             sender=lambda cfg, healthy, codes: sent.append((healthy, codes)) or True)
    assert result['healthy'] is False and result['reason_codes'] == ['HEALTH_CHECK_RUNNING']
    assert result['delivery'] == 'deferred' and not sent
    assert not (state[0].output_dir / 'send-window.json').exists()
    assert monitor.read_status(state[0]) == result
    pending = result['health_tracking']
    assert pending['pending_since_ms'] is not None and pending['pending_since_mono_ns'] is not None
    assert len(clock['calls']) <= monitor.HEALTH_RECHECKS + 1


def test_later_completed_invocation_clears_pending_and_sends_verified_up(state, transitioning_health):
    clock = transitioning_health(lambda c: {})
    sent = []
    sender = lambda cfg, healthy, codes: sent.append((healthy, codes)) or True
    first = monitor.run_once(state[0], now_ms=NOW, service_probe=monitor.probe_services, sender=sender)
    assert first['delivery'] == 'deferred' and not sent
    clock['us'] += 30_000_000
    transitioning_health(lambda c: completed_health(c))
    refresh_observed_state(state, NOW + 30_000)
    result = monitor.run_once(state[0], now_ms=NOW + 30_000,
                              service_probe=monitor.probe_services, sender=sender)
    assert result['healthy'] and sent == [(True, [])]
    assert result['health_tracking']['pending_since_ms'] is None
    assert result['health_tracking']['pending_since_mono_ns'] is None
    assert result['health_tracking']['last_completion_ms'] is not None
    assert result['health_tracking']['last_ping_accepted_at_ms'] == NOW + 30_000


@pytest.mark.parametrize('mutation,code', [
    ('HALT', 'STRATEGY_HALTED'), ('ENTRY_PAUSED', 'ENTRIES_PAUSED'),
    ('EXCHANGE_PAUSED', 'EXCHANGE_PAUSED'), ('unsafe', 'PROTECTION_UNSAFE'),
    ('unresolved', 'UNRESOLVED_ORDERS'), ('stale_runner', 'STATE_STALE')])
def test_running_health_does_not_defer_any_real_trading_fault(state, transitioning_health, mutation, code):
    transitioning_health(lambda c: {})
    if mutation in ('HALT', 'ENTRY_PAUSED', 'EXCHANGE_PAUSED'):
        write(state[0].state_dir / mutation, {})
    elif mutation == 'unsafe':
        state[1]['report.json']['unsafe_protection'] = {'market': 'unprotected'}
    elif mutation == 'unresolved':
        state[1]['snapshot.json']['unresolved_operations'] = {'order': 'unknown'}
    else:
        state[1]['runner_status.json']['updated_ms'] = NOW - 180001
    for name, value in state[1].items():
        write(state[0].state_dir / name, value)
    sent = []
    result = monitor.run_once(state[0], now_ms=NOW, service_probe=monitor.probe_services,
                              sender=lambda cfg, healthy, codes: sent.append((healthy, codes)) or True)
    assert not result['healthy'] and code in result['reason_codes']
    assert result['delivery'] == 'ping_accepted' and len(sent) == 1 and sent[0][0] is False


def test_pending_then_real_health_failure_sends_down_without_waiting_stale_limit(state, transitioning_health):
    transitioning_health(lambda c: {})
    sent = []
    sender = lambda cfg, healthy, codes: sent.append((healthy, codes)) or True
    first = monitor.run_once(state[0], now_ms=NOW, service_probe=monitor.probe_services, sender=sender)
    transitioning_health(lambda c: {'ActiveState': 'failed', 'SubState': 'failed',
                                     'Result': 'exit-code', 'ExecMainStatus': 1})
    refresh_observed_state(state, NOW + 30_000)
    result = monitor.run_once(state[0], now_ms=NOW + 30_000,
                              service_probe=monitor.probe_services, sender=sender)
    assert 'HEALTH_SERVICE_FAILED' in result['reason_codes']
    assert result['delivery'] == 'ping_accepted' and sent[0][0] is False and len(sent) == 1
    assert result['health_tracking']['pending_since_ms'] == first['health_tracking']['pending_since_ms']


def test_previous_down_is_not_cleared_by_a_running_health_check(state, transitioning_health):
    sent = []
    sender = lambda cfg, healthy, codes: sent.append((healthy, codes)) or True
    write(state[0].state_dir / 'HALT', {})
    monitor.run_once(state[0], now_ms=NOW, service_probe=probe, sender=sender)
    (state[0].state_dir / 'HALT').unlink()
    transitioning_health(lambda c: {})
    refresh_observed_state(state, NOW + 30_000)
    result = monitor.run_once(state[0], now_ms=NOW + 30_000,
                              service_probe=monitor.probe_services, sender=sender)
    assert not result['healthy'] and result['delivery'] == 'deferred'
    assert len(sent) == 1 and sent[0][0] is False


def test_pending_deadline_survives_new_health_invocations_and_unrelated_down(state, transitioning_health):
    clock = transitioning_health(lambda c: {})
    sent = []
    sender = lambda cfg, healthy, codes: sent.append((healthy, codes)) or True
    first = monitor.run_once(state[0], now_ms=NOW, service_probe=monitor.probe_services, sender=sender)
    original = first['health_tracking'].copy()
    for offset in (30_000, 90_000, 150_000):
        clock['us'] = 1_000_000_000_000 + offset * 1000
        clock['start'] = clock['us'] - 100_000
        refresh_observed_state(state, NOW + offset)
        if offset == 90_000:
            write(state[0].state_dir / 'HALT', {})
        else:
            (state[0].state_dir / 'HALT').unlink(missing_ok=True)
        result = monitor.run_once(state[0], now_ms=NOW + offset,
                                  service_probe=monitor.probe_services, sender=sender)
        for key in ('pending_since_ms', 'pending_since_mono_ns'):
            assert result['health_tracking'][key] == original[key]
    clock['us'] = 1_000_000_000_000 + 181_000_000
    clock['start'] = clock['us'] - 100_000
    refresh_observed_state(state, NOW + 181_000)
    result = monitor.run_once(state[0], now_ms=NOW + 181_000,
                              service_probe=monitor.probe_services, sender=sender)
    assert not result['healthy'] and 'HEALTH_SERVICE_STALE' in result['reason_codes']
    assert result['delivery'] == 'ping_accepted' and sent[-1][0] is False
    assert all(healthy is False for healthy, _ in sent)


@pytest.mark.parametrize('marker', ['HALT', 'ENTRY_PAUSED'])
def test_stop_created_during_health_recheck_still_sends_down(state, transitioning_health, marker):
    def respond(c):
        if len(c['calls']) == 1: return {}
        write(state[0].state_dir / marker, {})
        return completed_health(c)
    transitioning_health(respond)
    sent=[]
    result=monitor.run_once(state[0], now_ms=NOW, service_probe=monitor.probe_services,
                            sender=lambda cfg, healthy, codes:sent.append((healthy,codes)) or True)
    assert not result['healthy'] and len(sent)==1 and sent[0][0] is False
    assert ('STRATEGY_HALTED' if marker=='HALT' else 'ENTRIES_PAUSED') in result['reason_codes']


@pytest.mark.parametrize('response_status,response_body,expected', [(200, b'OK', True),
    (200, b'OK (not found)', False), (200, b'OK (rate limited)', False),
    (200, b'OK\n', False), (302, b'OK', False), (500, b'PRIVATE ERROR', False),
    (200, b'OK' + b'x' * 1000, False)])
def test_ping_exact_ack_tls_and_fixed_origin(monkeypatch, response_status, response_body, expected):
    calls = []
    class Connection:
        def __init__(self, host, port, **kwargs):
            calls.append(('connection', host, port, kwargs))
        def request(self, method, path, **kwargs):
            calls.append(('request', method, path, kwargs))
        def getresponse(self):
            class Response:
                status = response_status
                def read(self, limit):
                    assert limit == 65
                    return response_body[:limit]
            return Response()
        def close(self):
            calls.append(('close',))
    monkeypatch.setattr(monitor.http.client, 'HTTPSConnection', Connection)
    assert monitor._http_ping(UUID, False, ['PROTECTION_UNSAFE']) is expected
    assert calls[0][1:3] == ('hc-ping.com', 443)
    assert calls[0][3]['context'].check_hostname is True
    assert calls[1][1:3] == ('POST', '/' + UUID + '/fail')
    body = json.loads(calls[1][3]['body'])
    assert body == {'monitor': 'trading-monitor', 'state': 'down', 'reason_codes': ['PROTECTION_UNSAFE']}
    assert len([c for c in calls if c[0] == 'request']) == 1


def test_transport_worker_uuid_not_in_command_and_timeout_covers_dns(state, monkeypatch):
    commands = []
    class Process:
        returncode = 0
        killed = False
        def __init__(self, args, **kwargs):
            commands.append(args)
        def communicate(self, payload=None, timeout=None):
            if payload:
                assert json.loads(payload)['uuid'] == UUID
                assert timeout == 10
                raise subprocess.TimeoutExpired('redacted', timeout)
            return b'', None
        def kill(self):
            self.killed = True
    monkeypatch.setattr(monitor.subprocess, 'Popen', Process)
    assert monitor.send_ping(state[0], True, []) is False
    assert UUID not in repr(commands)


@pytest.mark.parametrize('uuid_text', ['https://hc-ping.com/' + UUID, '../' + UUID,
                                      UUID + '/fail', '0' * 36, UUID + '?x=secret'])
def test_secret_only_accepts_opaque_uuid(state, uuid_text):
    state[0].ping_uuid_file.write_text(uuid_text)
    with pytest.raises(monitor.MonitorError, match='CONFIG_INVALID'):
        monitor.load_uuid(state[0])


def test_payload_cannot_export_arbitrary_diagnostics():
    with pytest.raises(monitor.MonitorError):
        monitor._body(False, ['PRIVATE ERROR'])


def test_normal_heartbeat_sent_each_time_and_only_own_directory_changes(state):
    config, _ = state
    before = {p.name: p.read_bytes() for p in config.state_dir.iterdir()}
    calls = []
    def sender(cfg, healthy, codes):
        calls.append((healthy, codes))
        return True
    for offset in (0, 30_000):
        result = monitor.run_once(config, sender=sender, service_probe=probe, now_ms=NOW + offset)
        assert result['healthy'] and result['delivery'] == 'ping_accepted'
        assert result['telegram_delivery_verified'] is False
    assert calls == [(True, []), (True, [])]
    assert {p.name: p.read_bytes() for p in config.state_dir.iterdir()} == before
    assert len((config.output_dir / 'events.jsonl').read_text().splitlines()) == 1
    assert UUID not in repr(monitor.read_status(config))


def test_unknown_send_error_never_marks_ping_or_telegram_received(state):
    def fail(*args):
        raise RuntimeError('https://hc-ping.com/' + UUID)
    result = monitor.run_once(state[0], sender=fail, service_probe=probe, now_ms=NOW)
    assert result['delivery'] == 'failed' and result['telegram_delivery_verified'] is False
    assert UUID not in repr(result)
    assert UUID not in (state[0].output_dir / 'events.jsonl').read_text()


def test_manual_test_only_down_next_normal_cycle_decides_up(state):
    calls = []
    sender = lambda cfg, healthy, codes: calls.append((healthy, codes)) or True
    result = monitor.run_once(state[0], test_notification=True, sender=sender, service_probe=probe, now_ms=NOW)
    assert result['test_notification'] is True and calls == [(False, ['TEST_ALERT'])]
    write(state[0].state_dir / 'ENTRY_PAUSED', {})
    monitor.run_once(state[0], sender=sender, service_probe=probe, now_ms=NOW + 30_000)
    assert len(calls) == 2 and calls[-1][0] is False


def test_explicit_test_cli_success_is_ping_acceptance_not_healthy_up(state, monkeypatch, capsys):
    monkeypatch.setattr(monitor, 'load_config', lambda _: state[0])
    monkeypatch.setattr(monitor, 'run_once', lambda *args, **kwargs: {
        'healthy': False, 'reason_codes': ['TEST_ALERT'], 'test_notification': True,
        'delivery': 'ping_accepted', 'telegram_delivery_verified': False})
    assert monitor.main(['--config', 'unused', '--test-notification']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['healthy'] is False and result['telegram_delivery_verified'] is False


def test_pending_cli_is_successfully_observed_but_does_not_claim_healthy_or_delivered(state, monkeypatch, capsys):
    monkeypatch.setattr(monitor, 'load_config', lambda _: state[0])
    monkeypatch.setattr(monitor, 'run_once', lambda *args, **kwargs: {
        'healthy': False, 'reason_codes': ['HEALTH_CHECK_RUNNING'], 'test_notification': False,
        'delivery': 'deferred', 'telegram_delivery_verified': False})
    assert monitor.main(['--config', 'unused', '--once']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['healthy'] is False and result['delivery'] == 'deferred'


def test_manual_repeats_are_durably_rate_limited(state):
    calls = []
    sender = lambda *args: calls.append(1) or True
    for i in range(6):
        result = monitor.run_once(state[0], test_notification=True, sender=sender, service_probe=probe, now_ms=NOW + i)
    assert len(calls) == 5 and result['delivery'] == 'rate_limited'
    monitor.run_once(state[0], sender=sender, service_probe=probe, now_ms=NOW + 60_001)
    assert len(calls) == 6


def test_concurrent_observer_fails_without_send_or_state_corruption(state):
    calls = []
    with monitor.own_lock(state[0]):
        with pytest.raises(monitor.MonitorError, match='MONITOR_BUSY'):
            monitor.run_once(state[0], sender=lambda *args: calls.append(1), service_probe=probe, now_ms=NOW)
    assert not calls and not (state[0].output_dir / 'status.json').exists()


def test_local_storage_failure_does_not_send_untracked_healthy_ping(state, monkeypatch):
    calls = []
    monkeypatch.setattr(monitor, '_atomic', lambda *args: (_ for _ in ()).throw(OSError('PRIVATE')))
    result = monitor.run_once(state[0], sender=lambda *args: calls.append(1), service_probe=probe, now_ms=NOW)
    assert not result['healthy'] and 'MONITOR_STORAGE_ERROR' in result['reason_codes'] and not calls


def test_status_refuses_unknown_fields_instead_of_echoing_them(state):
    monitor.run_once(state[0], sender=lambda *args: True, service_probe=probe, now_ms=NOW)
    path = state[0].output_dir / 'status.json'
    saved = json.loads(path.read_text())
    saved['private'] = 'SECRET'
    write(path, saved)
    with pytest.raises(monitor.MonitorError):
        monitor.read_status(state[0])


def test_legacy_status_schema_remains_readable_without_inventing_pending_completion(state, transitioning_health):
    config, _ = state
    monitor.run_once(config, sender=lambda *args: True, service_probe=probe, now_ms=NOW)
    path = config.output_dir / 'status.json'
    previous = json.loads(path.read_text())
    del previous['health_tracking']
    write(path, previous)
    assert monitor.read_status(config) == previous
    transitioning_health(lambda c: {})
    refresh_observed_state(state, NOW + 30_000)
    sent = []
    result = monitor.run_once(config, now_ms=NOW + 30_000,
                              service_probe=monitor.probe_services, sender=lambda *args: sent.append(args))
    assert not result['healthy'] and result['delivery'] == 'deferred' and not sent
    assert result['health_tracking']['pending_since_ms'] <= NOW
    assert result['health_tracking']['last_ping_accepted_at_ms'] == NOW


@pytest.mark.parametrize('key', ['pending_since_ms', 'pending_since_mono_ns',
                                 'last_completion_ms', 'last_ping_accepted_at_ms'])
@pytest.mark.parametrize('invalid', [True, False, 0, -1, 1.5, 'SECRET'])
def test_health_tracking_values_reject_malformed_or_sensitive_fields(state, key, invalid):
    config, _ = state
    monitor.run_once(config, sender=lambda *args: True, service_probe=probe, now_ms=NOW)
    path = config.output_dir / 'status.json'
    saved = json.loads(path.read_text())
    saved['health_tracking'][key] = invalid
    write(path, saved)
    with pytest.raises(monitor.MonitorError, match='STATE_MALFORMED'):
        monitor.read_status(config)


@pytest.mark.parametrize('mutation', ['unknown', 'missing', 'half_pending', 'healthy_pending',
                                     'deferred_without_pending', 'deferred_healthy', 'deferred_fault'])
def test_health_tracking_and_deferred_status_require_closed_consistent_schema(state, mutation):
    config, _ = state
    monitor.run_once(config, sender=lambda *args: True, service_probe=probe, now_ms=NOW)
    path = config.output_dir / 'status.json'
    saved = json.loads(path.read_text())
    tracking = saved['health_tracking']
    if mutation == 'unknown':
        tracking['raw_private_diagnostic'] = 'SECRET'
    elif mutation == 'missing':
        del tracking['last_completion_ms']
    elif mutation == 'half_pending':
        tracking['pending_since_ms'] = NOW
    elif mutation == 'healthy_pending':
        tracking.update(pending_since_ms=NOW, pending_since_mono_ns=1)
    else:
        saved.update(delivery='deferred', healthy=False, reason_codes=['HEALTH_CHECK_RUNNING'])
        if mutation != 'deferred_without_pending':
            tracking.update(pending_since_ms=NOW, pending_since_mono_ns=1)
        if mutation == 'deferred_healthy':
            saved['healthy'] = True
        elif mutation == 'deferred_fault':
            saved['reason_codes'].append('PROTECTION_UNSAFE')
    write(path, saved)
    with pytest.raises(monitor.MonitorError, match='STATE_MALFORMED'):
        monitor.read_status(config)


@pytest.mark.parametrize('test_notification', [False, True])
def test_corrupt_pending_checkpoint_is_not_overwritten_or_used_to_send_up(state, transitioning_health, test_notification):
    transitioning_health(lambda c: {})
    config, _ = state
    monitor.run_once(config, now_ms=NOW, service_probe=monitor.probe_services,
                     sender=lambda *args: pytest.fail('pending must not send'))
    path = config.output_dir / 'status.json'
    path.write_text('{invalid', encoding='utf-8')
    before = path.read_bytes()
    result = monitor.run_once(config, now_ms=NOW + 30_000, service_probe=probe,
                              test_notification=test_notification,
                              sender=lambda *args: pytest.fail('corrupt checkpoint must not send'))
    assert not result['healthy'] and result['delivery'] == 'failed'
    assert 'MONITOR_STORAGE_ERROR' in result['reason_codes']
    assert path.read_bytes() == before


@pytest.mark.parametrize('clock_change', ['wall_reversed', 'monotonic_reboot'])
def test_pending_does_not_extend_through_clock_reversal_or_reboot(state, transitioning_health, clock_change):
    clock = transitioning_health(lambda c: {})
    sent = []
    sender = lambda cfg, healthy, codes: sent.append((healthy, codes)) or True
    monitor.run_once(state[0], now_ms=NOW, service_probe=monitor.probe_services, sender=sender)
    now = NOW + 30_000
    clock['us'] += 30_000_000
    if clock_change == 'wall_reversed':
        now = NOW - 30_000
    else:
        clock['us'] = 1_000_000
    clock['start'] = clock['us'] - 100_000
    refresh_observed_state(state, now)
    result = monitor.run_once(state[0], now_ms=now, service_probe=monitor.probe_services, sender=sender)
    assert not result['healthy'] and 'HEALTH_SERVICE_STALE' in result['reason_codes']
    assert len(sent) == 1 and sent[0][0] is False


def test_last_verified_completion_constrains_wait_even_if_new_health_start_is_recent(state, transitioning_health):
    clock = transitioning_health(lambda c: completed_health(c))
    sent = []
    sender = lambda cfg, healthy, codes: sent.append((healthy, codes)) or True
    monitor.run_once(state[0], now_ms=NOW, service_probe=monitor.probe_services, sender=sender)
    clock['us'] += 181_000_000
    clock['start'] = clock['us'] - 100_000
    transitioning_health(lambda c: {})
    refresh_observed_state(state, NOW + 181_000)
    result = monitor.run_once(state[0], now_ms=NOW + 181_000,
                              service_probe=monitor.probe_services, sender=sender)
    assert 'HEALTH_SERVICE_STALE' in result['reason_codes'] and not result['healthy']
    assert len(sent) == 2 and sent[-1][0] is False


@pytest.mark.parametrize('offset,current,code', [(180_000, True, None),
    (180_001, False, 'STATE_STALE'), (-1, False, 'STATE_FUTURE')])
def test_status_view_cannot_echo_stale_or_future_up(state, offset, current, code):
    monitor.run_once(state[0], sender=lambda *args: True, service_probe=probe, now_ms=NOW)
    result = monitor.status_view(state[0], now_ms=NOW + offset)
    assert result['current_observation'] is current
    assert result['last_observation_age_ms'] == offset
    assert result['healthy'] is current
    if code:
        assert code in result['reason_codes']
    # Reading status does not rewrite or erase the earlier observation.
    assert monitor.read_status(state[0])['healthy'] is True


def test_config_checks_never_contact_network_and_disallow_trade_output(state, tmp_path, capsys):
    config, _ = state
    path = tmp_path / 'config.json'
    data = {'version': 1, 'state_dir': str(config.state_dir), 'output_dir': str(config.output_dir),
            'ping_uuid_file': str(config.ping_uuid_file), 'expected_fingerprint': FINGERPRINT,
            'expected_account_index': 123}
    write(path, data)
    assert monitor.main(['--config', str(path), '--check-config']) == 0
    output = capsys.readouterr().out
    assert json.loads(output) == {'config_valid': True} and UUID not in output
    data['output_dir'] = str(config.state_dir / 'subdirectory')
    write(path, data)
    assert monitor.main(['--config', str(path), '--check-config']) == 2
    assert 'CONFIG_INVALID' in capsys.readouterr().out

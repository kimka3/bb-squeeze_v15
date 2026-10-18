"""Fault injection for bounded reads: no exchange access or signing credentials."""
from collections import deque
import contextlib
import http.client
import io
import json
from pathlib import Path
import socket
import ssl
import sys
from types import SimpleNamespace
import urllib.error

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from live import authenticated
from live.authenticated import APIReadError, LighterTransport, is_transient_read_error
from live.execution_evidence import Intent
from live.read_diagnostics import ReadDiagnostics
from live.request_budget import RequestBudget, RequestDeferred


class Clock:
    def __init__(self):
        self.now = 1000.0

    def advance(self, seconds):
        self.now += seconds

    def time(self):
        return self.now

    def monotonic(self):
        return self.now


class Response(io.StringIO):
    def __init__(self, data=None, *, headers=None, read_hook=None):
        super().__init__(json.dumps({'code': 200, 'value': 'fresh'} if data is None else data))
        self.status = 200
        self.headers = headers or {}
        self.read_hook = read_hook

    def read(self, *args):
        if self.read_hook is not None:
            hook, self.read_hook = self.read_hook, None
            hook()
        return super().read(*args)


class Opener:
    def __init__(self, actions):
        self.actions = list(actions)
        self.calls = []

    def open(self, request, timeout=None):
        self.calls.append({'url': request.full_url, 'timeout': timeout,
                           'authorization': request.get_header('Authorization')})
        assert self.actions, 'unexpected additional network attempt'
        action = self.actions.pop(0)
        if callable(action):
            action = action()
        if isinstance(action, BaseException):
            raise action
        return action


def http_error(code, headers=None):
    return urllib.error.HTTPError('https://secret.invalid/?SECRET_QUERY', code,
                                  'SECRET_HTTP_MESSAGE', headers or {},
                                  io.StringIO('SECRET_RESPONSE_BODY'))


@pytest.fixture
def transport_factory(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(authenticated, 'time', clock)
    monkeypatch.setattr(authenticated, 'quiet_native', contextlib.nullcontext)
    transports = []

    def make(actions):
        t = object.__new__(LighterTransport)
        t.host = 'https://example.invalid'
        t.api_key_index = 4
        t.client = SimpleNamespace(create_auth_token_with_expiry=lambda **kwargs:
                                  ('SECRET_AUTH_TOKEN', None))
        t.opener = Opener(actions)
        t.request_samples = deque(maxlen=1000)
        t.budget = RequestBudget(tmp_path / f'budget-{len(transports)}.sqlite', clock=clock.time)
        t.read_diagnostics = ReadDiagnostics(tmp_path / f'reads-{len(transports)}.jsonl')
        t.read_diagnostics_error = None
        t._last_read_retry = float('-inf')
        transports.append(t)
        return t, clock

    yield make
    for t in transports:
        t.budget.close()
        if getattr(t, 'loop', None) is not None and not t.loop.is_closed():
            t.loop.close()


def consumed(t):
    return t.budget.db.execute('SELECT COUNT(*) FROM requests').fetchone()[0]


def records(t):
    return [json.loads(line) for line in t.read_diagnostics.path.read_text().splitlines()]


@pytest.mark.parametrize('code', [502, 503, 504])
def test_fast_transient_read_retries_once_and_records_every_attempt(transport_factory, code):
    t, _ = transport_factory([http_error(code), Response(headers={'x-request-id': 'request-123'})])
    assert t.get('/api/v1/account?secret=SECRET_QUERY')['value'] == 'fresh'
    assert len(t.opener.calls) == consumed(t) == 2
    rows = records(t)
    assert [r['status'] for r in rows] == [code, 200]
    assert [r['attempt'] for r in rows] == [1, 2]
    assert [r['outcome'] for r in rows] == ['http_error', 'success']
    assert all(r['endpoint'] == '/api/v1/account' for r in rows)
    assert rows[-1]['request_ids']['x-request-id'] == 'request-123'
    serialized = t.read_diagnostics.path.read_text()
    assert 'SECRET_' not in serialized and 'Authorization' not in serialized
    assert t.opener.calls[0]['authorization'] == 'SECRET_AUTH_TOKEN'


def test_persistent_failure_stops_after_two_attempts_without_body_or_query_leak(transport_factory):
    t, _ = transport_factory([http_error(503), http_error(503)])
    with pytest.raises(RuntimeError, match='HTTP 503') as error:
        t.get('/api/v1/account?secret=SECRET_QUERY')
    assert len(t.opener.calls) == consumed(t) == 2
    assert 'SECRET_' not in str(error.value)
    assert [r['retry_reason'] for r in records(t)] == ['transient_http', 'retry_limit']


def test_extra_attempt_is_limited_across_endpoints_until_window_expires(transport_factory):
    t, clock = transport_factory([http_error(503), Response(), http_error(502),
                                  http_error(504), Response()])
    t.get('/api/v1/orderBookDetails', private=False)
    clock.advance(60.0)
    with pytest.raises(RuntimeError, match='retry_limit'):
        t.get('/api/v1/account', private=False)
    assert len(t.opener.calls) == 3
    clock.advance(0.2)
    assert t.get('/api/v1/accountActiveOrders', private=False)['code'] == 200
    assert len(t.opener.calls) == 5


@pytest.mark.parametrize('prior_requests,retried,remaining', [(33, True, 20), (34, False, 20), (54, False, 0)])
def test_optional_retry_preserves_twenty_slots(transport_factory, prior_requests, retried, remaining):
    t, _ = transport_factory([http_error(503), Response()])
    for _ in range(prior_requests):
        t.budget.check(consume=True)
    if retried:
        assert t.get('/api/v1/account', private=False)['code'] == 200
    else:
        with pytest.raises(RuntimeError, match='budget_deferred'):
            t.get('/api/v1/account', private=False)
    assert len(t.opener.calls) == (2 if retried else 1)
    assert 55 - consumed(t) == remaining


@pytest.mark.parametrize('header', ['0', '1', '120', 'invalid', 'Sun, 18 Oct 2026 12:00:00 GMT'])
def test_server_retry_after_suppresses_immediate_retry(transport_factory, header):
    t, _ = transport_factory([http_error(503, {'Retry-After': header})])
    with pytest.raises(RuntimeError, match='not_retryable'):
        t.get('/api/v1/account', private=False)
    assert len(t.opener.calls) == consumed(t) == 1


@pytest.mark.parametrize('code', [405, 429])
def test_rate_limit_retains_durable_cooldown_and_no_retry(transport_factory, code):
    t, _ = transport_factory([http_error(code, {'Retry-After': '70'})])
    with pytest.raises(RuntimeError, match=f'HTTP {code}'):
        t.get('/api/v1/account', private=False)
    with pytest.raises(RequestDeferred):
        t.get('/api/v1/account', private=False)
    assert len(t.opener.calls) == consumed(t) == 1


@pytest.mark.parametrize('failure', [TimeoutError('SECRET_TIMEOUT'),
    urllib.error.URLError('SECRET_DNS'), ssl.SSLError('SECRET_TLS'),
    http_error(401), http_error(403), http_error(500)])
def test_non_immediate_retry_errors_never_retry_or_reveal_messages(transport_factory, failure):
    t, _ = transport_factory([failure])
    with pytest.raises(RuntimeError) as error:
        t.get('/api/v1/account?secret=SECRET_QUERY', private=False)
    assert len(t.opener.calls) == 1 and 'SECRET_' not in str(error.value)
    assert 'SECRET_' not in t.read_diagnostics.path.read_text()


@pytest.mark.parametrize('data', [[], {'code': 503}, {'code': '200'}, {'SECRET_PAYLOAD': True}])
def test_schema_and_application_failure_do_not_retry(transport_factory, data):
    t, _ = transport_factory([Response(data)])
    with pytest.raises(RuntimeError):
        t.get('/api/v1/account', private=False)
    assert len(t.opener.calls) == 1
    assert records(t)[0]['outcome'] == 'payload_error'
    assert 'SECRET_' not in t.read_diagnostics.path.read_text()


def test_returned_authentication_error_does_not_send_or_leak(transport_factory):
    t, _ = transport_factory([])
    t.client.create_auth_token_with_expiry = lambda **kwargs: (None, 'SECRET_AUTH_ERROR')
    with pytest.raises(RuntimeError, match='authentication token generation failed') as error:
        t.get('/api/v1/account?secret=SECRET_QUERY')
    assert t.opener.calls == [] and 'SECRET_' not in str(error.value)
    assert records(t)[0]['outcome'] == 'auth_error'
    assert 'SECRET_' not in t.read_diagnostics.path.read_text()


def test_thrown_authentication_error_does_not_send_or_leak(transport_factory):
    t, _ = transport_factory([])
    def broken_auth(**kwargs):
        raise ValueError('SECRET_SIGNING_ERROR')
    t.client.create_auth_token_with_expiry = broken_auth
    with pytest.raises(RuntimeError, match='authentication token generation failed') as error:
        t.get('/api/v1/account?secret=SECRET_QUERY')
    assert t.opener.calls == [] and 'SECRET_' not in str(error.value)
    assert records(t)[0]['outcome'] == 'auth_error'
    assert 'SECRET_' not in t.read_diagnostics.path.read_text()


@pytest.mark.parametrize('failure', [http.client.RemoteDisconnected('SECRET_DISCONNECT'),
                                    ConnectionResetError('SECRET_RESET')])
def test_direct_connection_loss_get_can_retry_once(transport_factory, failure):
    t, _ = transport_factory([failure, Response()])
    assert t.get('/api/v1/account', private=False)['code'] == 200
    assert len(t.opener.calls) == 2
    assert records(t)[0]['retry_reason'] == 'transport_error'


@pytest.mark.parametrize('endpoint,entry_deadline', [('/api/v1/nextNonce', None),
                                                    ('/api/v1/account', 1001000)])
def test_nonce_and_active_entry_window_cannot_gain_optional_retry(transport_factory, endpoint, entry_deadline):
    t, _ = transport_factory([http_error(503)])
    t.new_entry_deadline_ms = entry_deadline
    with pytest.raises(RuntimeError, match='not_retryable'):
        t.get(endpoint, private=False)
    assert len(t.opener.calls) == consumed(t) == 1


def test_slow_failure_does_not_start_retry_when_less_than_one_second_remains(transport_factory):
    t, clock = transport_factory([])
    def slow_error():
        clock.advance(9.1)
        return http_error(503)
    t.opener.actions.append(slow_error)
    with pytest.raises(RuntimeError, match='retry=deadline'):
        t.get('/api/v1/account', private=False)
    assert len(t.opener.calls) == 1


@pytest.mark.parametrize('retry', [False, True])
def test_late_success_payload_is_discarded_instead_of_used(transport_factory, retry):
    t, clock = transport_factory([])
    if retry:
        def quick_error():
            clock.advance(0.25)
            return http_error(503)
        t.opener.actions.append(quick_error)
    t.opener.actions.append(Response(read_hook=lambda: clock.advance(9.75 if retry else 10)))
    with pytest.raises(RuntimeError, match='TimeoutError'):
        t.get('/api/v1/account', private=False)
    assert len(t.opener.calls) == (2 if retry else 1)
    assert records(t)[-1]['outcome'] == 'transport_error'
    assert t.opener.calls[-1]['timeout'] == (9.75 if retry else 10)


def test_log_failure_does_not_prevent_reads_but_stays_latched_after_logging_recovers(transport_factory):
    t, _ = transport_factory([Response(), Response()])
    real = t.read_diagnostics
    class Broken:
        def record(self, sample):
            raise OSError('SECRET_LOG_PATH_OR_MESSAGE')
    t.read_diagnostics = Broken()
    assert t.get('/api/v1/account', private=False)['code'] == 200
    assert t.read_diagnostics_error == 'OSError'
    t.read_diagnostics = real
    assert t.get('/api/v1/account', private=False)['code'] == 200
    assert t.read_diagnostics_error == 'OSError'
    assert 'SECRET_' not in real.path.read_text()


def test_ambiguous_mutating_send_is_never_automatically_retried(transport_factory, monkeypatch):
    t, _ = transport_factory([])
    class ImmediateLoop:
        def run_until_complete(self, coro):
            try:
                return coro.send(None)
            except StopIteration as done:
                return done.value
        def is_closed(self):
            return True
    t.loop = ImmediateLoop()
    monkeypatch.setattr(authenticated, 'asyncio', SimpleNamespace(wait_for=lambda coro, **kwargs: coro))
    calls = []
    async def lost_ack():
        calls.append('mutation')
        raise http.client.RemoteDisconnected('SECRET_LOST_ACK')
    with pytest.raises(RuntimeError, match='requires reconciliation') as error:
        t._send(lost_ack())
    assert calls == ['mutation'] and consumed(t) == 1
    assert t.opener.calls == [] and 'SECRET_' not in str(error.value)


def test_diagnostic_loss_during_nonce_blocks_new_entry_but_allows_reduce_only_protection(transport_factory):
    t, _ = transport_factory([])
    calls = []
    class Signer:
        ORDER_TYPE_MARKET = 1
        ORDER_TYPE_LIMIT = 0
        ORDER_TYPE_STOP_LOSS = 2
        ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL = 0
        ORDER_TIME_IN_FORCE_POST_ONLY = 2
        ORDER_TIME_IN_FORCE_GOOD_TILL_TIME = 1
        DEFAULT_IOC_EXPIRY = 0
        DEFAULT_28_DAY_ORDER_EXPIRY = -1
        def create_order(self, **kwargs):
            calls.append(kwargs)
            return kwargs
    t.client = Signer()
    def nonce_with_log_failure():
        t.read_diagnostics_error = 'OSError'
        return 42
    t._next_nonce = nonce_with_log_failure
    sent = []
    t._send = lambda coro, **kwargs: sent.append(coro)
    entry = Intent(100, 0, 1, False, False, '0.004', 'market',
                   'immediate-or-cancel', '2512.50')
    with pytest.raises(RuntimeError, match='diagnostic evidence lost'):
        t.submit(entry, 40, 251250, 0)
    assert calls == sent == []
    protective_stop = Intent(100, 0, 2, True, True, '0.004', 'stop-loss',
                             'immediate-or-cancel', '2462.62', '2475')
    t.submit(protective_stop, 40, 246262, 247500)
    assert len(calls) == len(sent) == 1 and sent[0]['reduce_only'] is True


def test_malformed_endpoint_is_rejected_before_auth_or_wire(transport_factory):
    t, _ = transport_factory([])
    for path in ('/api/v1/account/SECRET_KEY', '/api/v1/account#SECRET',
                 '/api/v1/account?x=1\nAuthorization: SECRET'):
        with pytest.raises(ValueError, match='invalid API path'):
            t.get(path)
    assert t.opener.calls == [] and consumed(t) == 0


@pytest.mark.parametrize('failure,code', [
    (http_error(405), 'http_405'),
    (http_error(429), 'http_429'),
    (http_error(502), 'http_502'),
    (http_error(503), 'http_503'),
    (http_error(504), 'http_504'),
    (TimeoutError('SECRET_TIMEOUT'), 'read_timeout'),
    (socket.timeout('SECRET_TIMEOUT'), 'read_timeout'),
    (http.client.RemoteDisconnected('SECRET_DISCONNECT'), 'remote_disconnected'),
    (ConnectionResetError('SECRET_RESET'), 'connection_reset'),
    (ConnectionAbortedError('SECRET_ABORT'), 'connection_aborted'),
    (socket.gaierror('SECRET_DNS'), 'dns_error'),
    (ConnectionRefusedError('SECRET_REFUSED'), 'connection_refused'),
    (urllib.error.URLError(TimeoutError('SECRET_TIMEOUT')), 'read_timeout'),
    (urllib.error.URLError(ConnectionResetError('SECRET_RESET')), 'connection_reset'),
    (urllib.error.URLError(ConnectionAbortedError('SECRET_ABORT')), 'connection_aborted'),
    (urllib.error.URLError(socket.gaierror('SECRET_DNS')), 'dns_error'),
    (urllib.error.URLError(ConnectionRefusedError('SECRET_REFUSED')), 'connection_refused'),
    (urllib.error.URLError(urllib.error.URLError(TimeoutError('SECRET_TIMEOUT'))), 'read_timeout'),
])
def test_only_typed_transport_network_failures_are_recoverable(transport_factory, failure, code):
    t, clock = transport_factory([failure])
    t._last_read_retry = clock.monotonic()  # classify without changing immediate retry policy
    with pytest.raises(APIReadError) as caught:
        t.get('/api/v1/account?secret=SECRET_QUERY', private=False)
    error = caught.value
    assert type(error) is APIReadError and isinstance(error, RuntimeError)
    assert is_transient_read_error(error)
    assert error.code == code and error.endpoint == '/api/v1/account'
    assert error.http_status == (failure.code if isinstance(failure, urllib.error.HTTPError) else None)
    assert 'SECRET_' not in str(error) and 'SECRET_' not in repr(vars(error))
    assert 'SECRET_' not in t.read_diagnostics.path.read_text()
    assert len(t.opener.calls) == 1


@pytest.mark.parametrize('failure', [
    http_error(400), http_error(401), http_error(403), http_error(500),
    ssl.SSLError('SECRET_TLS'), ssl.SSLCertVerificationError('SECRET_CERT'),
    urllib.error.URLError(ssl.SSLError('SECRET_TLS')),
    urllib.error.URLError(urllib.error.URLError(ssl.SSLCertVerificationError('SECRET_CERT'))),
    urllib.error.URLError(http_error(401)),
    urllib.error.URLError('SECRET_DNS_AS_TEXT'),
    urllib.error.URLError(PermissionError('SECRET_FILE')),
    PermissionError('SECRET_FILE'), FileNotFoundError('SECRET_FILE'),
    OSError('SECRET_IO'), ValueError('SECRET_PAYLOAD'),
    json.JSONDecodeError('SECRET_JSON', 'SECRET_BODY', 0),
])
def test_tls_auth_payload_and_local_failures_never_become_recoverable(transport_factory, failure):
    t, _ = transport_factory([failure])
    with pytest.raises(APIReadError) as caught:
        t.get('/api/v1/account?secret=SECRET_QUERY', private=False)
    assert not is_transient_read_error(caught.value)
    assert caught.value.automatic_recovery_allowed is False
    assert 'SECRET_' not in str(caught.value) and 'SECRET_' not in repr(vars(caught.value))
    assert len(t.opener.calls) == 1


@pytest.mark.parametrize('failure', [http_error(503), http_error(429),
    TimeoutError('SECRET'), ConnectionResetError('SECRET'),
    urllib.error.URLError(socket.gaierror('SECRET'))])
def test_nonce_failure_is_never_automatic_recovery_eligible(transport_factory, failure):
    t, _ = transport_factory([failure])
    with pytest.raises(APIReadError) as caught:
        t.get('/api/v1/nextNonce?account_index=SECRET_QUERY', private=False)
    assert not is_transient_read_error(caught.value)
    assert len(t.opener.calls) == 1


def test_unknown_or_cyclic_url_reason_does_not_become_recoverable(transport_factory):
    cyclic = urllib.error.URLError('SECRET')
    cyclic.reason = cyclic
    t, _ = transport_factory([cyclic])
    with pytest.raises(APIReadError) as caught:
        t.get('/api/v1/account', private=False)
    assert not is_transient_read_error(caught.value)


def test_classifier_does_not_accept_text_attributes_or_subclasses():
    forged = RuntimeError('API read failed (HTTP 503)')
    forged.automatic_recovery_allowed = True
    assert not is_transient_read_error(forged)
    assert not is_transient_read_error(TimeoutError('temporary'))
    class OtherError(APIReadError):
        pass
    subtype = OtherError('temporary', code='http_503', endpoint='/api/v1/account',
                         automatic_recovery_allowed=True)
    assert not is_transient_read_error(subtype)


@pytest.mark.parametrize('failure', [ValueError('SECRET_SIGNING_ERROR'), TimeoutError('SECRET_SIGNING_ERROR')])
def test_auth_generation_cannot_be_misclassified_by_network_exception_name(transport_factory, failure):
    t, _ = transport_factory([])
    def broken_auth(**kwargs):
        raise failure
    t.client.create_auth_token_with_expiry = broken_auth
    with pytest.raises(RuntimeError) as caught:
        t.get('/api/v1/account')
    assert not is_transient_read_error(caught.value)
    assert t.opener.calls == []


@pytest.mark.parametrize('failure_kind,cleanup_fails,diagnostics_failed', [
    ('transient', False, False), ('transient', True, False), ('local', False, False),
    ('budget', False, False), ('budget', True, False),
    ('transient', False, True), ('transient', True, True),
    ('budget', False, True), ('budget', True, True),
])
def test_constructor_preserves_only_typed_transient_read_or_budget_deferral(tmp_path, monkeypatch,
                                                     failure_kind, cleanup_fails, diagnostics_failed):
    monkeypatch.setattr(authenticated.importlib.metadata, 'version', lambda name: authenticated.SDK_VERSION)
    monkeypatch.setattr(authenticated, 'read_key', lambda path: 'SECRET_KEY')
    monkeypatch.setattr(authenticated, 'quiet_native', contextlib.nullcontext)
    class ImmediateLoop:
        def run_until_complete(self, coro):
            try:
                return coro.send(None)
            except StopIteration as done:
                return done.value
    monkeypatch.setattr(authenticated.asyncio, 'new_event_loop', ImmediateLoop)
    monkeypatch.setitem(sys.modules, 'lighter', SimpleNamespace(
        SignerClient=lambda **kwargs: SimpleNamespace(check_client=lambda: None)))
    if failure_kind == 'transient':
        failure = APIReadError('safe GET error', code='http_503', endpoint='/api/v1/account',
                              automatic_recovery_allowed=True)
    elif failure_kind == 'budget':
        failure = RequestDeferred(41.5)
        failure.details = {'used': 55, 'required': 1, 'limit': 55}
    else:
        failure = ValueError('SECRET_LOCAL')
    def account_failure(self):
        if diagnostics_failed:
            class BrokenDiagnostics:
                def record(self, sample): raise OSError('SECRET_DIAGNOSTIC_PATH')
            self.read_diagnostics = BrokenDiagnostics()
            self._record_read({'timestamp_ms': 1000, 'endpoint': '/api/v1/account',
                               'status': 503, 'attempt': 1, 'outcome': 'http_error'})
        raise failure
    monkeypatch.setattr(LighterTransport, 'account', account_failure)
    cleanup = []
    def close(self):
        self.budget.close()
        cleanup.append(True)
        if cleanup_fails:
            raise RuntimeError('SECRET_CLEANUP')
    monkeypatch.setattr(LighterTransport, 'close', close)
    with pytest.raises(RuntimeError) as caught:
        LighterTransport('mainnet', 100, 4, tmp_path / 'key', '0x' + '0' * 40)
    assert cleanup == [True]
    assert is_transient_read_error(caught.value) is (failure_kind == 'transient' and not diagnostics_failed)
    if diagnostics_failed:
        assert type(caught.value) is RuntimeError
        assert not isinstance(caught.value, (APIReadError, RequestDeferred))
        assert 'diagnostic evidence lost' in str(caught.value)
        assert 'manual inspection required' in str(caught.value)
    elif failure_kind in ('transient', 'budget'):
        assert caught.value is failure
    else:
        assert 'authenticated connection failed' in str(caught.value)
    if failure_kind == 'budget' and not diagnostics_failed:
        assert type(caught.value) is RequestDeferred
        assert not isinstance(caught.value, APIReadError)
        assert caught.value.retry_after == 41.5
        assert caught.value.details == {'used': 55, 'required': 1, 'limit': 55}
    assert 'SECRET_' not in str(caught.value)

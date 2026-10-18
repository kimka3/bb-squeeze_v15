"""Trusted feed outage classification must never publish an invalid bar frame."""
import http.client
import json
from pathlib import Path
import socket
import ssl
import sys
import urllib.error

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
from data_io import H4
from live import feed
from live.authenticated import APIReadError, is_transient_read_error


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self):
        return self.payload


def http_error(status):
    return urllib.error.HTTPError('https://feed.invalid/secret-query', status,
                                  'untrusted response body', {}, None)


def call_endpoint(endpoint):
    if endpoint == '/fapi/v1/klines':
        return feed._klines_raw('BTCUSDT', '4h', 599, 10, 2)
    return feed.clock_skew_seconds('https://feed.invalid')


@pytest.mark.parametrize('endpoint', ['/fapi/v1/klines', '/fapi/v1/time'])
@pytest.mark.parametrize('status', [429, 502, 503, 504])
def test_only_selected_temporary_http_errors_allow_recovery(monkeypatch, endpoint, status):
    calls = []
    def fail(*args, **kwargs):
        calls.append(args)
        raise http_error(status)
    monkeypatch.setattr(feed.urllib.request, 'urlopen', fail)
    monkeypatch.setattr(feed.time, 'sleep', lambda _: None)
    with pytest.raises(APIReadError) as caught:
        call_endpoint(endpoint)
    assert is_transient_read_error(caught.value)
    assert caught.value.endpoint == endpoint
    assert caught.value.http_status == status
    assert len(calls) == (2 if endpoint.endswith('klines') else 1)
    assert 'secret-query' not in str(caught.value)
    assert 'untrusted response' not in str(caught.value)


@pytest.mark.parametrize('endpoint', ['/fapi/v1/klines', '/fapi/v1/time'])
@pytest.mark.parametrize('status', [400, 401, 403, 405, 451, 500])
def test_auth_access_and_unknown_http_errors_are_not_recoverable(monkeypatch, endpoint, status):
    calls = []
    def fail(*args, **kwargs):
        calls.append(args)
        raise http_error(status)
    monkeypatch.setattr(feed.urllib.request, 'urlopen', fail)
    monkeypatch.setattr(feed.time, 'sleep', lambda _: pytest.fail('nontransient retry'))
    with pytest.raises(RuntimeError) as caught:
        call_endpoint(endpoint)
    assert not is_transient_read_error(caught.value)
    assert len(calls) == 1


@pytest.mark.parametrize('endpoint', ['/fapi/v1/klines', '/fapi/v1/time'])
@pytest.mark.parametrize('error', [
    TimeoutError('sensitive details'),
    urllib.error.URLError(TimeoutError('sensitive details')),
    http.client.RemoteDisconnected('sensitive details'),
    ConnectionResetError('sensitive details'),
    socket.gaierror(-2, 'sensitive details'),
])
def test_typed_network_errors_survive_wrapping_without_raw_detail(monkeypatch, endpoint, error):
    monkeypatch.setattr(feed.urllib.request, 'urlopen', lambda *a, **k: (_ for _ in ()).throw(error))
    monkeypatch.setattr(feed.time, 'sleep', lambda _: None)
    with pytest.raises(APIReadError) as caught:
        call_endpoint(endpoint)
    assert is_transient_read_error(caught.value)
    assert caught.value.endpoint == endpoint
    assert 'sensitive details' not in str(caught.value)


@pytest.mark.parametrize('endpoint', ['/fapi/v1/klines', '/fapi/v1/time'])
@pytest.mark.parametrize('error', [
    ssl.SSLError('certificate problem'),
    urllib.error.URLError(ssl.SSLError('certificate problem')),
    urllib.error.URLError('timed out, but no typed evidence'),
    PermissionError('local permission error'),
])
def test_unknown_tls_or_local_errors_never_become_automatic_outages(monkeypatch, endpoint, error):
    monkeypatch.setattr(feed.urllib.request, 'urlopen', lambda *a, **k: (_ for _ in ()).throw(error))
    monkeypatch.setattr(feed.time, 'sleep', lambda _: pytest.fail('nontransient retry'))
    with pytest.raises(Exception) as caught:
        call_endpoint(endpoint)
    assert caught.value is error
    assert not is_transient_read_error(caught.value)


def test_generic_oserror_keeps_bounded_read_retry_without_automatic_classification(monkeypatch):
    error = OSError('connection reset, but untyped')
    calls = []
    def fail(*args, **kwargs):
        calls.append(args)
        raise error
    monkeypatch.setattr(feed.urllib.request, 'urlopen', fail)
    monkeypatch.setattr(feed.time, 'sleep', lambda _: None)
    with pytest.raises(OSError) as caught:
        call_endpoint('/fapi/v1/klines')
    assert caught.value is error
    assert len(calls) == 2
    assert not is_transient_read_error(caught.value)


@pytest.mark.parametrize('endpoint', ['/fapi/v1/klines', '/fapi/v1/time'])
@pytest.mark.parametrize('payload', [b'broken json', b'{}', b'[]'])
def test_payload_schema_failures_remain_nontransient(monkeypatch, endpoint, payload):
    monkeypatch.setattr(feed.urllib.request, 'urlopen', lambda *a, **k: Response(payload))
    monkeypatch.setattr(feed.time, 'sleep', lambda _: pytest.fail('invalid schema retry'))
    with pytest.raises(Exception) as caught:
        call_endpoint(endpoint)
    assert not is_transient_read_error(caught.value)


def kline_rows(indices):
    return [[i*H4, '100', '102', '99', '101', '20'] for i in indices]


@pytest.mark.parametrize('indices', [range(599), range(1, 599)])
def test_exact_one_newest_bar_absence_waits_without_accepting_stale_frame(monkeypatch, indices):
    monkeypatch.setattr(feed, '_klines_raw', lambda *a, **k: kline_rows(indices))
    monkeypatch.setattr(feed.frozen, 'prepare_symbol_data', lambda *a: pytest.fail('stale frame accepted'))
    with pytest.raises(APIReadError, match='closed-bar window mismatch') as caught:
        feed.prepare(['BTCUSDT'], now_ms=600*H4+100)
    assert is_transient_read_error(caught.value)
    assert caught.value.code == 'feed_not_ready'


@pytest.mark.parametrize('indices', [range(598), range(2, 600), [*range(1, 450), *range(451, 600)]])
def test_old_or_short_or_internally_missing_history_is_not_publication_lag(monkeypatch, indices):
    monkeypatch.setattr(feed, '_klines_raw', lambda *a, **k: kline_rows(indices))
    monkeypatch.setattr(feed.frozen, 'prepare_symbol_data', lambda *a: pytest.fail('invalid frame accepted'))
    with pytest.raises(RuntimeError) as caught:
        feed.prepare(['BTCUSDT'], now_ms=600*H4+100)
    assert not is_transient_read_error(caught.value)


@pytest.mark.parametrize('bad_value', ['NaN', '-1', '1000'])
def test_invalid_ohlc_even_with_missing_newest_bar_is_never_temporary(monkeypatch, bad_value):
    rows = kline_rows(range(599))
    rows[-1][4] = bad_value
    monkeypatch.setattr(feed, '_klines_raw', lambda *a, **k: rows)
    with pytest.raises(RuntimeError) as caught:
        feed.prepare(['BTCUSDT'], now_ms=600*H4+100)
    assert not is_transient_read_error(caught.value)


def test_normal_public_time_response_still_uses_request_midpoint(monkeypatch):
    times = iter([10, 12])
    monkeypatch.setattr(feed.time, 'time', lambda: next(times))
    monkeypatch.setattr(feed.urllib.request, 'urlopen',
                        lambda *a, **k: Response(json.dumps({'serverTime': 12_000}).encode()))
    assert feed.clock_skew_seconds('https://feed.invalid') == 1

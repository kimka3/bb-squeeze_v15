"""Pinned Lighter transport. Construction and queries never submit transactions.

Only explicit send/cancel/amend calls mutate exchange state. Secrets are read
from a protected local file, never serialized into a configuration or journal.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import stat
import sys
import time
import hashlib
import http.client
import math
import re
from collections import deque
import urllib.error
import urllib.request
from .request_budget import RequestBudget, RequestDeferred
from .read_diagnostics import ReadDiagnostics, sanitize_sample

HOSTS = {'mainnet': 'https://mainnet.zklighter.elliot.ai',
         'testnet': 'https://testnet.zklighter.elliot.ai'}
SDK_VERSION = '1.1.6'


@contextlib.contextmanager
def quiet_native():
    """Native signer diagnostics must not expose key material to a terminal."""
    sys.stdout.flush()
    sys.stderr.flush()
    out, err = os.dup(1), os.dup(2)
    null = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null, 1)
        os.dup2(null, 2)
        yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(out, 1)
        os.dup2(err, 2)
        os.close(out)
        os.close(err)
        os.close(null)


def read_key(path: Path) -> str:
    if os.name != 'posix':
        raise RuntimeError('authenticated execution requires the Linux credential checks')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or
                stat.S_IMODE(info.st_mode) != 0o600):
            raise RuntimeError('key must be a regular 0600 file owned by the running user')
        key = handle.read(2049).strip()
    clean = key[2:] if key.startswith('0x') else key
    if not clean or len(clean) > 2048 or any(c not in '0123456789abcdefABCDEF' for c in clean):
        raise RuntimeError('invalid API key format')
    return key


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError('authenticated redirect refused')


class LighterTransport:
    def __init__(self, network, account_index, api_key_index, key_file, expected_address):
        if network not in HOSTS:
            raise ValueError('unsupported network')
        if type(account_index) is not int or account_index < 0:
            raise ValueError('invalid account index')
        if type(api_key_index) is not int or not 4 <= api_key_index <= 254:
            raise ValueError('use a dedicated API key index from 4 through 254')
        if not expected_address.startswith('0x') or len(expected_address) != 42:
            raise ValueError('expected wallet address required')
        if importlib.metadata.version('lighter-sdk') != SDK_VERSION:
            raise RuntimeError('SDK version differs from validated dependency pin')
        self.host, self.network = HOSTS[network], network
        self.account_index, self.api_key_index = account_index, api_key_index
        self.expected_address = expected_address.lower()
        scope = hashlib.sha256((network + self.expected_address).encode()).hexdigest()[:24]
        self.budget = RequestBudget(Path(key_file).parent / ('requests-' + scope + '.sqlite'))
        self.request_samples = deque(maxlen=1000)
        self.read_diagnostics = ReadDiagnostics(Path(key_file).parent / ('api-reads-' + scope + '.jsonl'))
        self.read_diagnostics_error = None
        self._last_read_retry = float('-inf')
        self.loop = asyncio.new_event_loop()
        self.client = None
        self.opener = urllib.request.build_opener(NoRedirect())
        logging.getLogger('lighter').setLevel(logging.CRITICAL)
        key = read_key(Path(key_file))
        async def connect():
            import lighter
            self.client = lighter.SignerClient(url=self.host, account_index=account_index,
                                               api_private_keys={api_key_index: key})
            if self.client.check_client() is not None:
                raise RuntimeError('API key/account validation failed')
        try:
            with quiet_native():
                self.loop.run_until_complete(connect())
            self.account()  # Bind the signer to the explicitly expected wallet.
        except Exception:
            self.close()
            raise RuntimeError('authenticated connection failed; secrets suppressed') from None

    def close(self):
        if self.client is not None:
            with quiet_native():
                self.loop.run_until_complete(self.client.close())
            self.client = None
        if not self.loop.is_closed():
            self.loop.close()
        if getattr(self, 'budget', None) is not None:
            self.budget.close()
            self.budget = None

    def _record_read(self, sample):
        sample = sanitize_sample(sample)
        self.request_samples.append(sample)
        diagnostics = getattr(self, 'read_diagnostics', None)
        if diagnostics is not None:
            try:
                diagnostics.record(sample)
            except Exception as exc:
                # Loss of observability blocks NEW entries through the health
                # guard, but must not prevent existing position protection.
                self.read_diagnostics_error = type(exc).__name__
                # Latched for this process: a later successful write cannot
                # restore the missing evidence. Operator inspection/restart
                # is required; never silently erase it within one poll.

    def _read_retry_reason(self, endpoint, started, attempt, transient, retry_after):
        if (not transient or retry_after is not None or endpoint == '/api/v1/nextNonce'
                or getattr(self, 'new_entry_deadline_ms', None) is not None):
            return 'not_retryable'
        if attempt >= 2 or time.monotonic() - getattr(self, '_last_read_retry', float('-inf')) < 60.1:
            return 'retry_limit'
        if time.monotonic() - started >= 9:
            return 'deadline'
        try:
            self.budget.check(21)  # one extra read, keep 20 slots for protection
        except RequestDeferred:
            return 'budget_deferred'
        return None

    def get(self, path, *, private=True):
        endpoint = path.split('?')[0]
        if not re.fullmatch(r'/api/v1/[A-Za-z0-9_]+', endpoint) or any(c in path for c in ('\r', '\n', '#')):
            raise ValueError('invalid API path')
        started = time.monotonic()
        for attempt in (1, 2):
            # Never retry a transaction. Only quick, transient GET failures may
            # use one additional read, at most once per 60.1s per transport.
            self.budget.check(consume=True)
            headers = {}
            if private:
                auth_error = None
                try:
                    with quiet_native():
                        auth, err = self.client.create_auth_token_with_expiry(
                            deadline=120, api_key_index=self.api_key_index)
                    if err or not auth: auth_error = 'AuthenticationError'
                except Exception as exc:
                    auth_error = type(exc).__name__
                if auth_error:
                    self._record_read({'timestamp_ms':int(time.time()*1000), 'endpoint':endpoint,
                        'status':'error', 'seconds':time.monotonic()-started, 'attempt':attempt,
                        'outcome':'auth_error', 'error_type':auth_error})
                    raise RuntimeError('authentication token generation failed; details suppressed') from None
                headers['Authorization'] = auth
            sample = {'timestamp_ms':int(time.time()*1000), 'endpoint':endpoint,
                      'status':'error', 'attempt':attempt, 'request_ids':{}}
            retry_after = None
            transient = False
            attempt_started = time.monotonic()
            try:
                remaining = 10 - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError('read deadline elapsed')
                with self.opener.open(urllib.request.Request(self.host + path, headers=headers), timeout=remaining) as r:
                    sample['status'] = r.status
                    response_headers = getattr(r, 'headers', {})
                    sample['request_ids'] = {k:response_headers.get(k) for k in (
                        'x-request-id','x-correlation-id','x-amz-cf-id','x-amzn-requestid','cf-ray')}
                    data = json.load(r)
                # urllib timeout is a socket timeout, not a hard wall-clock
                # cancellation. Never use a payload that returns too late.
                if time.monotonic() - started >= 10:
                    raise TimeoutError('read deadline elapsed')
                if not isinstance(data, dict) or data.get('code') != 200:
                    sample['outcome'] = 'payload_error'
                    raise ValueError('non-success payload')
            except urllib.error.HTTPError as exc:
                sample.update(status=exc.code, outcome='http_error', error_type='HTTPError')
                response_headers = exc.headers or {}
                sample['request_ids'] = {k:response_headers.get(k) for k in (
                    'x-request-id','x-correlation-id','x-amz-cf-id','x-amzn-requestid','cf-ray')}
                retry_after = response_headers.get('Retry-After')
                if exc.code in (405, 429):
                    try: delay = float(retry_after or 60)
                    except (TypeError, ValueError): delay = 60
                    if not math.isfinite(delay) or delay < 0: delay = 60
                    self.budget.defer(delay)
                transient = exc.code in (502, 503, 504)
                exc.close()
            except Exception as exc:
                sample.update(error_type=type(exc).__name__)
                sample.setdefault('outcome', 'transport_error')
                transient = isinstance(exc, (http.client.RemoteDisconnected, ConnectionResetError))
            else:
                sample.update(outcome='success', seconds=time.monotonic()-attempt_started)
                self._record_read(sample)
                return data
            reason = self._read_retry_reason(endpoint, started, attempt, transient, retry_after)
            sample.update(seconds=time.monotonic()-attempt_started,
                          retry_reason=reason or ('transient_http' if sample['outcome']=='http_error' else 'transport_error'))
            self._record_read(sample)
            if reason is None:
                self._last_read_retry = time.monotonic()
                continue
            kind = f"HTTP {sample['status']}" if sample['outcome']=='http_error' else sample['error_type']
            raise RuntimeError(f'API read failed ({kind}) endpoint={endpoint} attempt={attempt} retry={reason}; response suppressed') from None

    def account(self):
        data = self.get(f'/api/v1/account?by=index&value={self.account_index}')
        accounts = [a for a in data.get('accounts', [])
                    if a.get('index', a.get('account_index')) == self.account_index]
        if len(accounts) != 1 or accounts[0].get('l1_address', '').lower() != self.expected_address:
            raise RuntimeError('exchange account/wallet identity mismatch')
        if not isinstance(accounts[0].get('positions'), list):
            raise RuntimeError('account position list missing')
        return accounts[0]

    def active_orders(self):
        data = self.get(f'/api/v1/accountActiveOrders?account_index={self.account_index}')
        if not isinstance(data.get('orders'), list) or data.get('next_cursor'):
            raise RuntimeError('active order snapshot incomplete')
        return data['orders']

    def order(self, coi):
        if type(coi) is not int or coi < 0:
            raise ValueError('invalid order ID')
        data = self.get(f'/api/v1/accountOrders?account_index={self.account_index}&client_order_indexes={coi}')
        if not isinstance(data.get('orders'), list) or data.get('next_cursor'):
            raise RuntimeError('order lookup incomplete')
        rows = data['orders']
        if len(rows) > 1 or (rows and rows[0].get('client_order_index') != coi):
            raise RuntimeError('order lookup identity ambiguous')
        return rows[0] if rows else None

    def _send(self, coro, *, deadline_ms=None):
        # An error or lost response is ambiguous. The execution engine must
        # reconcile the durable intent; it must never blindly resend it.
        try:
            try:
                self.budget.check(consume=True)
                if deadline_ms is not None and time.time()*1000>=deadline_ms:
                    raise RuntimeError('entry deadline elapsed before transaction send')
            except Exception:
                coro.close()
                raise
            with quiet_native():
                _, response, error = self.loop.run_until_complete(asyncio.wait_for(coro, timeout=15))
            if error is not None or response is None or response.code != 200:
                raise RuntimeError('submission not acknowledged')
        except Exception:
            raise RuntimeError('submission outcome requires reconciliation') from None

    def submit(self, intent, base_amount, price, trigger):
        c = self.client
        types = {'market': c.ORDER_TYPE_MARKET, 'limit': c.ORDER_TYPE_LIMIT,
                 'stop-loss': c.ORDER_TYPE_STOP_LOSS}
        times = {'immediate-or-cancel': c.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
                 'post-only': c.ORDER_TIME_IN_FORCE_POST_ONLY,
                 'good-till-time': c.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME}
        nonce=self._next_nonce()
        if not intent.reduce_only and getattr(self, 'read_diagnostics_error', None):
            raise RuntimeError('API diagnostic evidence lost; new entry refused')
        deadline=getattr(self,'new_entry_deadline_ms',None)
        if not intent.reduce_only and deadline is not None and time.time()*1000>=deadline:
            raise RuntimeError('entry deadline elapsed; transaction not sent')
        self._send(c.create_order(market_index=intent.market_index,
            client_order_index=intent.client_order_index, base_amount=base_amount,
            price=price, trigger_price=trigger, is_ask=intent.is_ask,
            order_type=types[intent.order_type], time_in_force=times[intent.time_in_force],
            reduce_only=intent.reduce_only, api_key_index=self.api_key_index, nonce=nonce,
            order_expiry=c.DEFAULT_IOC_EXPIRY if intent.order_type == 'market'
                         else c.DEFAULT_28_DAY_ORDER_EXPIRY),
                   deadline_ms=deadline if not intent.reduce_only else None)

    def cancel(self, market_index, order_index):
        self._send(self.client.cancel_order(market_index=market_index,
            order_index=order_index, api_key_index=self.api_key_index, nonce=self._next_nonce()))

    def amend(self, market_index, order_index, base_amount, price, trigger):
        self._send(self.client.modify_order(market_index=market_index,
            order_index=order_index, base_amount=base_amount, price=price,
            trigger_price=trigger, api_key_index=self.api_key_index, nonce=self._next_nonce()))

    def _next_nonce(self):
        # SDK explicit api_key_index uses caller-managed nonces. Query the
        # server under ExecutionEngine's single-writer lock for each mutation.
        data = self.get(f'/api/v1/nextNonce?account_index={self.account_index}&api_key_index={self.api_key_index}')
        nonce = data.get('nonce')
        if type(nonce) is not int or nonce < 0:
            raise RuntimeError('invalid next nonce; submission refused')
        return nonce

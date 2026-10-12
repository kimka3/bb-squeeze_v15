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
from collections import deque
import urllib.error
import urllib.request
from .request_budget import RequestBudget

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

    def get(self, path, *, private=True):
        if not path.startswith('/api/v1/') or any(c in path for c in ('\r', '\n', '#')):
            raise ValueError('invalid API path')
        self.budget.check(consume=True)
        started = time.monotonic()
        status = 'error'
        headers = {}
        if private:
            with quiet_native():
                auth, err = self.client.create_auth_token_with_expiry(
                    deadline=120, api_key_index=self.api_key_index)
            if err or not auth:
                raise RuntimeError('authentication token generation failed')
            headers['Authorization'] = auth
        try:
            with self.opener.open(urllib.request.Request(self.host + path, headers=headers), timeout=10) as r:
                data = json.load(r)
                status = r.status
        except urllib.error.HTTPError as exc:
            status = exc.code
            if exc.code in (405, 429):
                try: delay = float(exc.headers.get('Retry-After', 60))
                except (TypeError, ValueError): delay = 60
                self.budget.defer(delay)
            raise RuntimeError(f'API read failed (HTTP {exc.code}); response suppressed') from None
        except Exception as exc:
            raise RuntimeError(f'API read failed ({type(exc).__name__}); response suppressed') from None
        finally:
            self.request_samples.append({'endpoint': path.split('?')[0], 'status': status,
                                         'seconds': time.monotonic()-started})
        if not isinstance(data, dict) or data.get('code') != 200:
            raise RuntimeError('API returned a non-success payload')
        return data

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

    def _send(self, coro):
        # An error or lost response is ambiguous. The execution engine must
        # reconcile the durable intent; it must never blindly resend it.
        try:
            try: self.budget.check(consume=True)
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
        self._send(c.create_order(market_index=intent.market_index,
            client_order_index=intent.client_order_index, base_amount=base_amount,
            price=price, trigger_price=trigger, is_ask=intent.is_ask,
            order_type=types[intent.order_type], time_in_force=times[intent.time_in_force],
            reduce_only=intent.reduce_only, api_key_index=self.api_key_index, nonce=self._next_nonce(),
            order_expiry=c.DEFAULT_IOC_EXPIRY if intent.order_type == 'market'
                         else c.DEFAULT_28_DAY_ORDER_EXPIRY))

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

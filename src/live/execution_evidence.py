"""Strict execution evidence for the forthcoming authenticated adapter.

This module cannot submit orders and is not wired into the disabled live CLI.
An acknowledgement or an absent order is never treated as proof of execution.
Schema: official elliottech/lighter-python lighter/models/order.py, 2026-10-12.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path


class UnverifiedExecution(RuntimeError):
    pass


def number(value) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise UnverifiedExecution('missing or boolean numeric field')
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise UnverifiedExecution('invalid numeric field') from exc
    if not result.is_finite() or result < 0:
        raise UnverifiedExecution('negative or nonfinite numeric field')
    return result


@dataclass(frozen=True)
class Intent:
    account_index: int
    market_index: int
    client_order_index: int
    is_ask: bool
    reduce_only: bool
    quantity: str
    order_type: str
    time_in_force: str
    price: str
    trigger_price: str = '0'

    def __post_init__(self):
        for field in ('account_index', 'market_index', 'client_order_index'):
            value = getattr(self, field)
            if type(value) is not int or not 0 <= value < 2**63:
                raise ValueError('order identity must be a nonnegative int64')
        if type(self.is_ask) is not bool or type(self.reduce_only) is not bool:
            raise ValueError('order flags must be booleans')
        if number(self.quantity) <= 0 or number(self.price) <= 0:
            raise ValueError('positive quantity and price required')
        if self.order_type not in ('market', 'limit', 'stop-loss', 'stop-loss-limit'):
            raise ValueError('unsupported order type')
        if self.time_in_force not in ('immediate-or-cancel', 'post-only', 'good-till-time'):
            raise ValueError('unsupported time in force')
        if self.order_type.startswith('stop-loss'):
            if not self.reduce_only or number(self.trigger_price) <= 0:
                raise ValueError('stop must be reduce-only with positive trigger')
        elif number(self.trigger_price) != 0:
            raise ValueError('unexpected trigger price')


TERMINAL = frozenset(('filled', 'canceled', 'canceled-post-only', 'canceled-reduce-only',
    'canceled-position-not-allowed', 'canceled-margin-not-allowed',
    'canceled-too-much-slippage', 'canceled-not-enough-liquidity', 'canceled-self-trade',
    'canceled-expired', 'canceled-oco', 'canceled-child', 'canceled-liquidation',
    'canceled-invalid-balance'))


def verify_order(row: dict, intent: Intent) -> dict:
    """Verify account, identity, submitted terms and observed cumulative fills.

    Canceled orders may contain partial fills. Order totals do not establish
    per-trade fees/funding, account reconciliation or terminal position state.
    """
    if not isinstance(row, dict):
        raise UnverifiedExecution('order row is not an object')
    for key, expected in (
        ('owner_account_index', intent.account_index), ('market_index', intent.market_index),
        ('client_order_index', intent.client_order_index), ('is_ask', intent.is_ask),
        ('reduce_only', intent.reduce_only), ('type', intent.order_type),
        ('time_in_force', intent.time_in_force)):
        if type(row.get(key)) is not type(expected) or row[key] != expected:
            raise UnverifiedExecution(f'order identity/terms mismatch: {key}')
    if type(row.get('order_index')) is not int or row['order_index'] < 0:
        raise UnverifiedExecution('missing exchange order index')
    for key, expected in (('initial_base_amount', intent.quantity),
                          ('price', intent.price), ('trigger_price', intent.trigger_price)):
        if number(row.get(key)) != number(expected):
            raise UnverifiedExecution(f'order amount/price mismatch: {key}')
    qty, remaining, quote = (number(row.get(k)) for k in
                            ('filled_base_amount', 'remaining_base_amount', 'filled_quote_amount'))
    initial = number(intent.quantity)
    if qty > initial or remaining > initial or qty + remaining > initial:
        raise UnverifiedExecution('inconsistent order quantities')
    if (qty == 0) != (quote == 0):
        raise UnverifiedExecution('inconsistent base and quote execution totals')
    status = row.get('status')
    if status not in TERMINAL | {'open', 'pending', 'in-progress'}:
        raise UnverifiedExecution('unknown order status')
    if status == 'filled' and (qty != initial or remaining != 0):
        raise UnverifiedExecution('filled status with inconsistent quantity')
    if status in {'open', 'pending', 'in-progress'} and qty + remaining != initial:
        raise UnverifiedExecution('active order quantities do not balance')
    return {'order_index': row['order_index'], 'status': status,
            'terminal': status in TERMINAL, 'filled_qty': str(qty),
            'filled_quote': str(quote), 'remaining_qty': str(remaining),
            'average_price': str(quote / qty) if qty else None,
            'fees_verified': False, 'position_verified': False}


def verify_stop(row: dict, intent: Intent, position_quantity: str) -> dict:
    result = verify_order(row, intent)
    # Lighter standalone conditional orders await the mark-price trigger in
    # pending state. Parent-order dependencies are not armed protection.
    pending_mark = (row.get('status') == 'pending' and
                    row.get('trigger_status') == 'mark-price' and
                    type(row.get('trigger_time')) is int and row['trigger_time'] == 0 and
                    type(row.get('parent_order_index')) is int and row['parent_order_index'] == 0)
    open_ready = row.get('status') == 'open' and row.get('trigger_status') == 'ready'
    if (not intent.order_type.startswith('stop-loss') or not intent.reduce_only or
            not (pending_mark or open_ready) or number(intent.trigger_price) <= 0 or
            number(result['filled_qty']) != 0 or
            number(position_quantity) <= 0 or
            number(result['remaining_qty']) < number(position_quantity)):
        raise UnverifiedExecution('stop is not confirmed ready for the full position')
    return result


class IntentStore:
    """Durable identity reservation; an unresolved intent can never be resent.

    Reserve must commit before calling a future submit transport. A crash even
    before send leaves an uncertain intent: reconcile, never blindly retry.
    No token, private key, signed transaction, or HTTP headers belong here.
    """
    def __init__(self, path: Path, network: str, account_index: int):
        if network not in ('mainnet', 'testnet') or type(account_index) is not int or account_index < 0:
            raise ValueError('valid network and account required')
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS identity (id INTEGER PRIMARY KEY, value TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS intents (coi TEXT PRIMARY KEY, intent TEXT NOT NULL, result TEXT)')
        identity = json.dumps([network, account_index])
        try:
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO identity VALUES (1, ?)', (identity,))
                if self.db.execute('SELECT value FROM identity WHERE id=1').fetchone()[0] != identity:
                    raise UnverifiedExecution('state belongs to another network/account')
        except Exception:
            self.db.close()
            raise
        self.account_index = account_index

    def close(self):
        self.db.close()

    def reserve(self, intent: Intent) -> None:
        if intent.account_index != self.account_index:
            raise UnverifiedExecution('intent belongs to another account')
        try:
            with self.db:
                self.db.execute('INSERT INTO intents VALUES (?, ?, NULL)',
                                (str(intent.client_order_index), json.dumps(asdict(intent), sort_keys=True)))
        except sqlite3.IntegrityError as exc:
            raise UnverifiedExecution('intent already reserved; reconcile instead of resending') from exc

    def record(self, coi: int, row: dict) -> dict:
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            found = self.db.execute('SELECT intent, result FROM intents WHERE coi=?', (str(coi),)).fetchone()
            if not found:
                raise UnverifiedExecution('unknown local intent')
            result = verify_order(row, Intent(**json.loads(found[0])))
            if found[1]:
                previous = json.loads(found[1])
                if (result['order_index'] != previous['order_index'] or
                        number(result['filled_qty']) < number(previous['filled_qty']) or
                        number(result['filled_quote']) < number(previous['filled_quote']) or
                        (previous['terminal'] and result != previous)):
                    raise UnverifiedExecution('regressed or conflicting exchange evidence')
            self.db.execute('UPDATE intents SET result=? WHERE coi=?',
                            (json.dumps(result), str(coi)))
        return result

    def terminal_result(self, coi: int) -> dict | None:
        row = self.db.execute('SELECT result FROM intents WHERE coi=?', (str(coi),)).fetchone()
        result = json.loads(row[0]) if row and row[0] else None
        return result if result and result['terminal'] else None

    def load(self, coi: int):
        row = self.db.execute('SELECT intent, result FROM intents WHERE coi=?', (str(coi),)).fetchone()
        return (Intent(**json.loads(row[0])), json.loads(row[1]) if row[1] else None) if row else None

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


class NotSubmitted(UnverifiedExecution):
    """Positive local evidence that this attempt never entered dispatch.

    This is not inferred from an absent order or a timeout. ``reason`` is a
    fixed diagnostic code, never a response body; ``cause`` retains the typed
    local failure so callers cannot silently weaken a safety halt.
    """
    def __init__(self, reason, *, retryable=False, cause=None):
        self.reason = str(reason)
        self.retryable = bool(retryable)
        self.cause = cause
        self.retry_after = getattr(cause, 'retry_after', None)
        super().__init__(f'transaction not submitted: {self.reason.replace("_", " ")}')


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
    expiry = row.get('order_expiry')
    if expiry is not None and (type(expiry) is not int or expiry < 0):
        raise UnverifiedExecution('invalid order expiry milliseconds')
    return {'order_index': row['order_index'], 'status': status,
            'terminal': status in TERMINAL, 'filled_qty': str(qty),
            'filled_quote': str(quote), 'remaining_qty': str(remaining),
            'average_price': str(quote / qty) if qty else None,
            'fees_verified': False, 'position_verified': False,
            'order_expiry_ms': expiry if expiry else None}


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
        # Keep the original intent schema and load() contract intact. A legacy
        # intent without this metadata is UNKNOWN, never known-not-submitted.
        self.db.execute('CREATE TABLE IF NOT EXISTS intent_dispatch '
                        '(coi TEXT PRIMARY KEY, state TEXT NOT NULL, reason TEXT, retryable INTEGER NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS intent_supersessions '
                        '(old_coi TEXT PRIMARY KEY, new_coi TEXT UNIQUE NOT NULL, proof TEXT NOT NULL)')
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
                self.db.execute('INSERT INTO intent_dispatch VALUES (?, ?, NULL, 0)',
                                (str(intent.client_order_index), 'reserved'))
        except sqlite3.IntegrityError as exc:
            raise UnverifiedExecution('intent already reserved; reconcile instead of resending') from exc

    def record(self, coi: int, row: dict) -> dict:
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            found = self.db.execute('SELECT intent, result FROM intents WHERE coi=?', (str(coi),)).fetchone()
            if not found:
                raise UnverifiedExecution('unknown local intent')
            if self.known_not_submitted(coi):
                raise UnverifiedExecution('exchange order conflicts with confirmed local non-submission')
            result = verify_order(row, Intent(**json.loads(found[0])))
            if found[1]:
                previous = json.loads(found[1])
                # Older terminal results predate expiry capture. Adding observed
                # metadata is not a regression in their immutable fill evidence.
                comparison, previous_comparison = dict(result), dict(previous)
                if previous.get('order_expiry_ms') is None:
                    comparison.pop('order_expiry_ms', None)
                    previous_comparison.pop('order_expiry_ms', None)
                if (result['order_index'] != previous['order_index'] or
                        number(result['filled_qty']) < number(previous['filled_qty']) or
                        number(result['filled_quote']) < number(previous['filled_quote']) or
                        (previous['terminal'] and comparison != previous_comparison)):
                    raise UnverifiedExecution('regressed or conflicting exchange evidence')
            self.db.execute('UPDATE intents SET result=? WHERE coi=?',
                            (json.dumps(result), str(coi)))
            self.db.execute('INSERT INTO intent_dispatch VALUES (?, ?, NULL, 0) '
                            'ON CONFLICT(coi) DO UPDATE SET state=excluded.state, reason=NULL, retryable=0',
                            (str(coi), 'evidenced'))
        return result

    def submission_details(self, coi: int):
        row = self.db.execute('SELECT d.state,d.reason,d.retryable FROM intents i '
            'LEFT JOIN intent_dispatch d ON d.coi=i.coi WHERE i.coi=?', (str(coi),)).fetchone()
        if row is None:
            return None
        return {'state': row[0] or 'unknown', 'reason': row[1], 'retryable': bool(row[2])}

    def submission_state(self, coi: int):
        details = self.submission_details(coi)
        return details['state'] if details else None

    def known_not_submitted(self, coi: int) -> bool:
        return self.submission_state(coi) == 'not_submitted'

    def supersessions(self):
        return {int(old): {'new_coi': int(new), 'proof': json.loads(proof)}
                for old, new, proof in self.db.execute(
                    'SELECT old_coi,new_coi,proof FROM intent_supersessions ORDER BY rowid')}

    def supersede_not_submitted(self, old_coi: int, replacement: Intent, proof: dict) -> None:
        """Reserve a new stop ID and its non-dispatch provenance atomically.

        Never rewrite the original order or manufacture terminal evidence. A
        crash after this transaction leaves the replacement positively unsent
        until mark_dispatching() changes that evidence before the actual send.
        """
        if (type(old_coi) is not int or not isinstance(proof, dict)
                or proof.get('kind') != 'stop_renewal'
                or not isinstance(proof.get('symbol'), str)
                or proof.get('side') not in ('LONG', 'SHORT')
                or any(type(proof.get(k)) is not int or proof[k] < 0
                       for k in ('entry_cycle', 'cycle_ms', 'old_protection_coi', 'created_ms'))):
            raise UnverifiedExecution('invalid stop supersession proof')
        encoded_proof = json.dumps(proof, sort_keys=True, allow_nan=False)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            old = self.load(old_coi)
            details = self.submission_details(old_coi)
            if (old is None or old[1] is not None or details is None
                    or details['state'] != 'not_submitted' or not details['retryable']):
                raise UnverifiedExecution('supersession requires retryable confirmed non-submission')
            original = old[0]
            if (replacement.client_order_index in (old_coi, proof['old_protection_coi'])
                    or not 0 <= replacement.client_order_index < 2**48
                    or replacement.account_index != self.account_index
                    or any(getattr(original, name) != getattr(replacement, name)
                           for name in ('account_index', 'market_index', 'is_ask', 'reduce_only',
                                        'order_type', 'time_in_force'))
                    or not original.reduce_only or original.order_type != 'stop-loss'
                    or original.time_in_force != 'immediate-or-cancel'
                    or original.is_ask != (proof['side'] == 'LONG')
                    or number(replacement.quantity) > number(original.quantity)
                    or (original.is_ask and number(replacement.trigger_price) < number(original.trigger_price))
                    or (not original.is_ask and number(replacement.trigger_price) > number(original.trigger_price))):
                raise UnverifiedExecution('stop supersession changes identity or loosens protection')
            prior = self.supersessions().get(old_coi)
            if prior is not None:
                if (prior != {'new_coi': replacement.client_order_index, 'proof': proof}
                        or self.load(replacement.client_order_index) is None
                        or self.load(replacement.client_order_index)[0] != replacement):
                    raise UnverifiedExecution('stop supersession identity changed')
                return
            if self.load(replacement.client_order_index) is not None:
                raise UnverifiedExecution('stop supersession replacement ID already reserved')
            self.db.execute('INSERT INTO intents VALUES (?, ?, NULL)',
                (str(replacement.client_order_index), json.dumps(asdict(replacement), sort_keys=True)))
            self.db.execute('INSERT INTO intent_dispatch VALUES (?, ?, ?, 1)',
                (str(replacement.client_order_index), 'not_submitted', 'renewal_supersession_reserved'))
            self.db.execute('INSERT INTO intent_supersessions VALUES (?, ?, ?)',
                (str(old_coi), str(replacement.client_order_index), encoded_proof))

    def mark_dispatching(self, coi: int) -> None:
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT result FROM intents WHERE coi=?', (str(coi),)).fetchone()
            if (row is None or row[0] is not None or
                    self.submission_state(coi) not in ('reserved', 'not_submitted') or
                    coi in self.supersessions()):
                raise UnverifiedExecution('dispatch requires an unused locally reserved intent')
            self.db.execute('UPDATE intent_dispatch SET state=?,reason=NULL,retryable=0 WHERE coi=?',
                            ('dispatching', str(coi)))

    def mark_not_submitted(self, coi: int, error: NotSubmitted) -> None:
        if type(error) is not NotSubmitted:
            raise TypeError('only typed pre-dispatch evidence can prove non-submission')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT result FROM intents WHERE coi=?', (str(coi),)).fetchone()
            if row is None or row[0] is not None or self.submission_state(coi) != 'dispatching':
                raise UnverifiedExecution('non-submission conflicts with existing dispatch evidence')
            self.db.execute('UPDATE intent_dispatch SET state=?,reason=?,retryable=? WHERE coi=?',
                            ('not_submitted', error.reason, int(error.retryable), str(coi)))

    def mark_acknowledged(self, coi: int) -> None:
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.submission_state(coi) != 'dispatching':
                raise UnverifiedExecution('acknowledgement without pending dispatch')
            self.db.execute('UPDATE intent_dispatch SET state=? WHERE coi=?', ('acknowledged', str(coi)))

    def terminal_result(self, coi: int) -> dict | None:
        row = self.db.execute('SELECT result FROM intents WHERE coi=?', (str(coi),)).fetchone()
        result = json.loads(row[0]) if row and row[0] else None
        return result if result and result['terminal'] else None

    def load(self, coi: int):
        row = self.db.execute('SELECT intent, result FROM intents WHERE coi=?', (str(coi),)).fetchone()
        return (Intent(**json.loads(row[0])), json.loads(row[1]) if row[1] else None) if row else None

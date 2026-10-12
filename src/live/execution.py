"""Single-writer authenticated order lifecycle with durable uncertainty.

This engine supports a user-run, bounded execution check. It deliberately does
not activate the strategy runner or convert order evidence into TP attribution.
"""
from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
import json
from pathlib import Path
import time

from .execution_evidence import Intent, IntentStore, UnverifiedExecution, number, verify_order, verify_stop
from .journal import Journal
from .process_lock import ProcessLock


def scaled(value, decimals):
    amount = number(value) * 10 ** decimals
    if amount != amount.to_integral_value():
        raise ValueError('value is not aligned to the market tick')
    return int(amount)


class ExecutionEngine:
    def __init__(self, transport, directory, *, timeout=30, sleep=time.sleep, clock=time.monotonic, lock_path=None):
        self.transport, self.directory = transport, Path(directory)
        self.timeout, self.sleep, self.clock = timeout, sleep, clock
        # All clients for the same API account must use the same parent lock.
        self.lock = ProcessLock(lock_path or self.directory.parent / f'{transport.network}-{transport.account_index}.lock').acquire()
        try:
            self.store = IntentStore(self.directory / 'execution.sqlite', transport.network, transport.account_index)
            self.journal = Journal(self.directory)
            self.store.db.execute('CREATE TABLE IF NOT EXISTS mutations '
                '(id TEXT PRIMARY KEY, value TEXT NOT NULL, complete INTEGER NOT NULL DEFAULT 0)')
            self.store.db.commit()
        except Exception:
            self.lock.release()
            raise

    def close(self):
        try:
            self.store.close()
        finally:
            self.lock.release()

    def _wait(self, coi, accept):
        end = self.clock() + self.timeout
        while True:
            # Missing orders and request failures do not prove no fill.
            try:
                row = self.transport.order(coi)
            except Exception:
                row = None
            if row is not None:
                result = self.store.record(coi, row)
                self.journal.append('exchange_order_evidence', coi=coi, row=row)
                if accept(row, result):
                    return result
            if self.clock() >= end:
                raise UnverifiedExecution('order outcome unknown; retained for recovery, not resubmitted')
            self.sleep(.5)

    def create(self, intent: Intent, size_decimals, price_decimals, *, stop_quantity=None, resting=False):
        base = scaled(intent.quantity, size_decimals)
        price = scaled(intent.price, price_decimals)
        trigger = scaled(intent.trigger_price, price_decimals)
        if not 0 < price < 2**32 or not 0 <= trigger < 2**32 or base <= 0:
            raise ValueError('invalid scaled order values')
        existing = self.store.load(intent.client_order_index)
        if existing:
            if existing[0] != intent:
                raise UnverifiedExecution('existing intent has different terms')
            if existing[1] and existing[1]['terminal']:
                if stop_quantity is not None:
                    raise UnverifiedExecution('protective order already terminal; inspect residual position')
                return existing[1]
        else:
            if self.transport.order(intent.client_order_index) is not None:
                raise UnverifiedExecution('exchange already has this ID without local intent; refusing submission')
            self.store.reserve(intent)
            self.journal.append('durable_order_intent', intent=asdict(intent))
            try:
                self.transport.submit(intent, base, price, trigger)
            except Exception:
                self.journal.append('submission_uncertain', coi=intent.client_order_index)
        def accept(row, result):
            if stop_quantity is None:
                return result['terminal'] or (resting and intent.order_type == 'limit' and row['status'] == 'open')
            if result['terminal']:
                raise UnverifiedExecution('stop already terminal; inspect position and close residual')
            try:
                verify_stop(row, intent, stop_quantity)
            except UnverifiedExecution:
                return False
            return True
        return self._wait(intent.client_order_index, accept)

    def recover(self, coi):
        existing = self.store.load(coi)
        if existing is None:
            raise UnverifiedExecution('no durable intent to recover')
        if existing[1] and existing[1]['terminal']:
            return existing[1]
        return self._wait(coi, lambda row, result: result['terminal'])

    def _mutation(self, identity, value):
        encoded = json.dumps(value, sort_keys=True)
        with self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            row = self.store.db.execute('SELECT value, complete FROM mutations WHERE id=?', (identity,)).fetchone()
            if row:
                if row[0] != encoded:
                    raise UnverifiedExecution('mutation identity collision')
                return False
            self.store.db.execute('INSERT INTO mutations VALUES (?, ?, 0)', (identity, encoded))
        self.journal.append('durable_mutation', mutation=identity, value=value)
        return True

    def reconcile_amendments(self):
        """Read-only recovery after venue acceptance but before SQLite commit.

        Do not verify an amended row against the stale pre-crash intent. Never
        retransmit an uncertain amendment from this reconciliation path.
        """
        pending=self.store.db.execute('SELECT id,value FROM mutations WHERE complete=0').fetchall()
        for mid,encoded in pending:
            if not mid.startswith('amend:'): continue
            replacement=Intent(**json.loads(encoded))
            old,previous=self.store.load(replacement.client_order_index)
            row=self.transport.order(replacement.client_order_index)
            try:
                result=verify_order(row,replacement)
                if not result['terminal']: verify_stop(row,replacement,replacement.quantity)
            except UnverifiedExecution:
                result=verify_order(row,old)
                if not result['terminal']:
                    verify_stop(row,old,old.quantity)  # uncertainty remains blocked
                    continue
                encoded=json.dumps(asdict(old),sort_keys=True)
            if previous and result['order_index']!=previous['order_index']:
                raise UnverifiedExecution('amend recovery changed order identity')
            with self.store.db:
                self.store.db.execute('UPDATE intents SET intent=?,result=? WHERE coi=?',
                    (encoded,json.dumps(result),str(replacement.client_order_index)))
                self.store.db.execute('UPDATE mutations SET complete=1 WHERE id=?',(mid,))
            self.journal.append('recovered_stop_amendment',coi=replacement.client_order_index,row=row)

    def cancel(self, coi):
        existing = self.store.load(coi)
        if not existing:
            raise UnverifiedExecution('cannot cancel an unknown intent')
        intent, result = existing
        if result and result['terminal']:
            return result
        row = self.transport.order(coi)
        if row is None:
            raise UnverifiedExecution('cancel target absent, outcome unknown')
        result = self.store.record(coi, row)
        if result['terminal']:
            return result
        mid = f'cancel:{coi}'
        if self._mutation(mid, {'market':intent.market_index, 'order':result['order_index']}):
            try:
                self.transport.cancel(intent.market_index, result['order_index'])
            except Exception:
                self.journal.append('cancel_uncertain', coi=coi)
        confirmed = self._wait(coi, lambda row, result: result['terminal'])
        with self.store.db:
            self.store.db.execute('UPDATE mutations SET complete=1 WHERE id=?', (mid,))
        return confirmed

    def amend_stop(self, coi, replacement: Intent, size_decimals, price_decimals):
        existing = self.store.load(coi)
        if not existing:
            raise UnverifiedExecution('cannot amend unknown stop')
        old, result = existing
        if (old.order_type != 'stop-loss' or not old.reduce_only or
                replacement.client_order_index != coi or
                any(getattr(old, key) != getattr(replacement, key) for key in
                    ('account_index','market_index','is_ask','reduce_only','order_type','time_in_force'))):
            raise UnverifiedExecution('stop amendment changes identity or direction')
        if ((old.is_ask and number(replacement.trigger_price) < number(old.trigger_price)) or
                (not old.is_ask and number(replacement.trigger_price) > number(old.trigger_price))):
            raise UnverifiedExecution('stop amendment would loosen protection')
        row = self.transport.order(coi)
        # Recovery can already observe the replacement after a lost reply.
        try:
            verify_stop(row, replacement, replacement.quantity)
            already = True
        except UnverifiedExecution:
            verify_stop(row, old, old.quantity)
            already = False
        oid = row['order_index']
        mid = f'amend:{coi}:{replacement.quantity}:{replacement.trigger_price}:{replacement.price}'
        fresh = self._mutation(mid, asdict(replacement))
        if fresh and not already:
            try:
                self.transport.amend(old.market_index, oid, scaled(replacement.quantity, size_decimals),
                    scaled(replacement.price, price_decimals), scaled(replacement.trigger_price, price_decimals))
            except Exception:
                self.journal.append('amend_uncertain', coi=coi)
        end = self.clock() + self.timeout
        while True:
            row = self.transport.order(coi)
            try:
                result = verify_stop(row, replacement, replacement.quantity)
                if row['order_index'] != oid:
                    raise UnverifiedExecution('amendment changed exchange order ID')
                break
            except UnverifiedExecution:
                if self.clock() >= end:
                    raise UnverifiedExecution('stop amendment not confirmed; no retry submission')
                self.sleep(.5)
        with self.store.db:
            self.store.db.execute('UPDATE intents SET intent=?, result=? WHERE coi=?',
                (json.dumps(asdict(replacement),sort_keys=True), json.dumps(result), str(coi)))
            self.store.db.execute('UPDATE mutations SET complete=1 WHERE id=?', (mid,))
        self.journal.append('confirmed_stop_amendment', coi=coi, row=row)
        return result


def positions(account):
    result = {}
    if not isinstance(account.get('positions'), list):
        raise UnverifiedExecution('position snapshot missing')
    for p in account['positions']:
        qty = number(p.get('position'))
        if qty == 0:
            continue
        if (type(p.get('market_id')) is not int or type(p.get('sign')) is not int
                or p.get('sign') not in (-1, 1)):
            raise UnverifiedExecution('invalid position identity')
        if p['market_id'] in result:
            raise UnverifiedExecution('duplicate position')
        result[p['market_id']] = Decimal(p['sign']) * qty
    return result

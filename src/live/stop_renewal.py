"""Evidence-based renewal of expiring live stops, without cancel-first gaps.

The one-day lead is an operational margin, not a strategy/risk parameter or an
exchange availability guarantee. Only the authenticated broker has the durable
dispatch evidence this workflow needs; paper orders without expiry are ignored.
"""
from __future__ import annotations

import time
from copy import deepcopy
from dataclasses import asdict

from .execution_evidence import Intent, UnverifiedExecution, number
from .live_check import price_tick


RENEWAL_LEAD_MS = 24 * 60 * 60 * 1000


def renewal_due(ref, now_ms):
    expiry = ref.get('order_expiry_ms') if ref else None
    return (type(expiry) is int and type(now_ms) is int and
            now_ms < expiry <= now_ms + RENEWAL_LEAD_MS)


def _active(state, trader, symbol, coi):
    rows = [row for row in state.active_orders.get(trader.config_symbol_map[symbol], [])
            if row.get('client_order_index') == coi]
    if len(rows) > 1:
        raise UnverifiedExecution('duplicate stop renewal order evidence')
    return rows[0] if rows else None


def _fresh(trader, symbol, now_ms, fresh_symbols):
    if fresh_symbols is not None:
        return symbol in fresh_symbols
    samples = getattr(trader.broker, '_mark_sample_ms', {})
    mark = getattr(trader.broker, '_marks', {}).get(symbol)
    age = now_ms - samples.get(symbol, -RENEWAL_LEAD_MS)
    return (mark is not None and number(mark) > 0 and
            0 <= age <= max(0., trader.config.poll_seconds) * 2000)


def _position(trader, op, state):
    symbol = op['symbol']
    if (op.get('kind') != 'stop_renewal' or type(op.get('coi')) is not int or
            op['coi'] == op['old_ref']['client_order_index']):
        raise UnverifiedExecution('invalid stop renewal identity')
    p = trader.positions.get(symbol)
    if p and (p['entry_ms'] != op['cycle_ms'] or p['side'] != op['side']):
        raise UnverifiedExecution('stop renewal belongs to a different position cycle')
    if not state.active_orders_verified or not trader._symbol_verified(state, symbol):
        raise UnverifiedExecution('stop renewal requires reconciled owned exposure')
    bindings = trader.broker._bindings()
    old = bindings.get(op['old_ref']['client_order_index'])
    if (not old or old['role'] != 'SL' or old['cycle'] != op['entry_cycle'] or
            old['symbol'] != symbol or old['side'] != op['side']):
        raise UnverifiedExecution('stop renewal old order ownership changed')
    new = bindings.get(op['coi'])
    if new and (new['role'] != 'SL' or new['cycle'] != op['entry_cycle'] or
                new['symbol'] != old['symbol'] or new['side'] != old['side']):
        raise UnverifiedExecution('stop renewal replacement ownership changed')
    actual = state.positions.get(trader.config_symbol_map[symbol])
    if actual and (not p or actual['side'] != op['side'] or
                   trader.broker._cycle(symbol) != op['entry_cycle']):
        raise UnverifiedExecution('stop renewal current position identity changed')
    return p, actual


def _store_ref(trader, coi):
    found = trader.broker.engine.store.load(coi)
    if not found:
        return None
    intent, result = found
    if result is None:
        return None
    return {'client_order_index': coi, 'order_index': result['order_index'],
            'purpose': 'SL', 'trigger': float(intent.trigger_price),
            'qty': float(intent.quantity), 'position_side': 'LONG' if intent.is_ask else 'SHORT',
            'order_expiry_ms': result.get('order_expiry_ms')}


def _target(trader, op, p):
    market = trader.markets[op['symbol']]
    trigger = price_tick(number(p['stop']), market.price_decimals, upward=p['side'] == 'LONG')
    old_trigger = number(op['old_ref']['trigger'])
    if ((p['side'] == 'LONG' and trigger < old_trigger) or
            (p['side'] == 'SHORT' and trigger > old_trigger)):
        raise UnverifiedExecution('stop renewal would loosen original protection')
    return trader.broker.quantity(market, p['qty']), trigger


def _complete(trader, op, *, flat=False):
    trader.journal.append('stop_renewal_completed', symbol=op['symbol'],
        old_coi=op['old_ref']['client_order_index'], new_coi=op['coi'],
        order_expiry_ms=(op.get('new_ref') or {}).get('order_expiry_ms'), flat=flat,
        superseded_cois=list(op.get('superseded_cois', [])))
    trader.pending_operation = None
    trader._save()


def _cleanup_flat(trader, op, state):
    """Keep both IDs until each has positive terminal/non-dispatch evidence."""
    op['phase'] = 'flat_cleanup'
    trader._save()
    store = trader.broker.engine.store
    for coi in (op['old_ref']['client_order_index'], *op.get('superseded_cois', []), op['coi']):
        found = store.load(coi)
        if found is None:
            if coi == op['coi'] and _active(state, trader, op['symbol'], coi) is None:
                continue  # Engine reserves durably before any possible dispatch.
            raise UnverifiedExecution('stop renewal cleanup lost its durable intent')
        if store.known_not_submitted(coi):
            if _active(state, trader, op['symbol'], coi) is not None:
                raise UnverifiedExecution('unsent renewal conflicts with active order')
            continue
        if found[1] and found[1]['terminal']:
            continue
        ref = _store_ref(trader, coi)
        if ref is None or _active(state, trader, op['symbol'], coi) is None:
            raise UnverifiedExecution('late stop renewal outcome remains unknown while flat')
        trader.broker.cancel(trader.markets[op['symbol']], trader._order_ref(ref))
    # Cancel acknowledgements are insufficient; engine.cancel waits for terminal
    # evidence. The next normal fill sync removes the old position/TP snapshot.
    _complete(trader, op, flat=True)


def _apply_replacement_plan(trader, op):
    """Finish a write-ahead supersession, including a crash after DB commit."""
    plan = op.get('replacement_plan')
    if plan is None:
        return
    replacement = Intent(**plan['intent'])
    proof = plan['proof']
    if (plan['old_coi'] != op['coi'] or proof.get('kind') != 'stop_renewal'
            or any(proof.get(key) != op[key] for key in ('symbol', 'side', 'entry_cycle', 'cycle_ms'))
            or proof.get('old_protection_coi') != op['old_ref']['client_order_index']):
        raise UnverifiedExecution('stop replacement plan belongs to a different renewal')
    trader.broker._bind(replacement.client_order_index, op['symbol'], op['side'], 'SL')
    trader.broker.engine.store.supersede_not_submitted(plan['old_coi'], replacement, proof)
    trader.journal.append('stop_renewal_superseded', symbol=op['symbol'],
        old_coi=plan['old_coi'], new_coi=replacement.client_order_index,
        entry_cycle=op['entry_cycle'], quantity=replacement.quantity,
        trigger=replacement.trigger_price, evidence='retryable_confirmed_non_submission')
    op.setdefault('superseded_cois', []).append(plan['old_coi'])
    op.update(coi=replacement.client_order_index, qty=float(replacement.quantity),
              trigger=float(replacement.trigger_price), phase='create')
    for key in ('replacement_plan', 'new_ref', 'amend_qty', 'amend_trigger'):
        op.pop(key, None)
    trader._save()


def _require_existing_protection(trader, op, state, now_ms):
    active = _active(state, trader, op['symbol'], op['old_ref']['client_order_index'])
    if (active is None or active.get('armed') is not True
            or type(active.get('order_expiry_ms')) is not int
            or active['order_expiry_ms'] <= now_ms):
        raise UnverifiedExecution('stop supersession requires existing unexpired protection')


def _supersede_changed_unsent(trader, op, state, now_ms, target_qty, target_trigger):
    """Only a proved unsent stop may receive a distinct ID for changed terms."""
    store = trader.broker.engine.store
    found = store.load(op['coi'])
    if found is None or not store.known_not_submitted(op['coi']):
        return
    original = found[0]
    if (number(original.quantity) == target_qty and
            number(original.trigger_price) == target_trigger):
        return  # Unchanged terms retain the existing strictly gated retry path.
    detail = store.submission_details(op['coi'])
    if not detail['retryable']:
        raise UnverifiedExecution('non-retryable stop refusal cannot be superseded')
    _require_existing_protection(trader, op, state, now_ms)
    generation = trader.protection_generation.get(op['symbol'], 0) + 1
    coi = trader._coi(op['cycle_ms'], op['symbol'], 'SL_RENEWAL', seq=generation)
    if (coi in (op['coi'], op['old_ref']['client_order_index'], *op.get('superseded_cois', []))
            or store.load(coi) is not None or coi in trader.broker._bindings()):
        raise UnverifiedExecution('stop supersession replacement identity collision')
    replacement = trader.broker._intent(trader.markets[op['symbol']], op['side'],
        target_qty, coi, closing=True, kind='stop-loss', price=target_trigger, trigger=target_trigger)
    proof = {'kind': 'stop_renewal', 'symbol': op['symbol'], 'side': op['side'],
             'entry_cycle': op['entry_cycle'], 'cycle_ms': op['cycle_ms'],
             'old_protection_coi': op['old_ref']['client_order_index'], 'created_ms': now_ms}
    op['replacement_plan'] = {'old_coi': op['coi'], 'intent': asdict(replacement), 'proof': proof}
    trader.protection_generation[op['symbol']] = generation
    trader._save()  # Preserve both IDs before binding/reserving any replacement.
    _apply_replacement_plan(trader, op)


def start_renewal(trader, symbol, now_ms, out, state=None):
    """Begin one live-only renewal; exceptions retain the write-ahead operation."""
    ref = trader.orders.get(symbol, {}).get('SL')
    if not ref or not hasattr(trader.broker, 'engine'):
        return False
    if trader.pending_operation is not None or symbol in trader.residual_exits:
        return False
    if any(v['symbol'] == symbol for v in trader.unresolved_operations.values()):
        return False
    state = state if state is not None else trader.broker.account_state()
    p = trader.positions[symbol]
    active = _active(state, trader, symbol, ref['client_order_index'])
    if active is None or active.get('armed') is not True:
        return False
    expiry = active.get('order_expiry_ms')
    if type(expiry) is not int or expiry <= 0:
        return False  # Legacy fixtures/unknown metadata are never infinite expiry.
    if ref.get('order_expiry_ms') != expiry:
        ref['order_expiry_ms'] = expiry
        trader._save()  # Backfill pre-upgrade snapshots from actual order evidence.
    if not renewal_due(active, now_ms):
        return False
    generation = trader.protection_generation.get(symbol, 0) + 1
    coi = trader._coi(p['entry_ms'], symbol, 'SL_RENEWAL', seq=generation)
    if (coi == ref['client_order_index'] or trader.broker.engine.store.load(coi) is not None
            or coi in trader.broker._bindings()):
        raise UnverifiedExecution('stop renewal client order identity collision')
    old_ref = dict(ref, **active)
    op = {'kind': 'stop_renewal', 'symbol': symbol, 'bar_ms': now_ms,
          'cycle_ms': p['entry_ms'], 'entry_cycle': trader.broker._cycle(symbol),
          'side': p['side'], 'old_ref': deepcopy(old_ref), 'coi': coi,
          'qty': p['qty'], 'trigger': active['trigger'], 'phase': 'create'}
    _position(trader, op, state)
    trader.protection_generation[symbol] = generation
    trader._begin_operation(op)  # old/new identity survives a crash before submit.
    trader.journal.append('stop_renewal_started', symbol=symbol,
        old_coi=ref['client_order_index'], new_coi=coi,
        old_expiry_ms=old_ref['order_expiry_ms'], operational_lead_ms=RENEWAL_LEAD_MS)
    recover_renewal(trader, op, out, state=state, now_ms=now_ms)
    return True


def recover_renewal(trader, op, out, state=None, *, now_ms=None, fresh_symbols=None):
    """Resume the same IDs; only proven pre-dispatch refusals may be retried.

    Callers park this operation on exceptions, just like other durable work.
    This routine never clears HALT or any manual/automatic pause flag.
    """
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    symbol, market = op['symbol'], trader.markets[op['symbol']]
    store = trader.broker.engine.store
    state = state if state is not None else trader.broker.account_state()
    p, actual = _position(trader, op, state)
    # This step is local-only and idempotent. Finish its identity link even if
    # the position became flat while the process was stopped, then clean every
    # recorded ID using the ordinary positive-evidence cleanup path.
    _apply_replacement_plan(trader, op)
    if actual is None:
        _cleanup_flat(trader, op, state)
        return
    if (number(actual['qty']) != number(p['qty']) or
            (actual.get('partial_taken') is True and not p['partial_taken'])):
        out.reasons.append(f'{symbol}: renewal waits for confirmed fill synchronization')
        return  # Normal service applies TP/quantity changes before the next cycle.
    if not _fresh(trader, symbol, now_ms, fresh_symbols):
        out.reasons.append(f'{symbol}: stop renewal awaits a fresh mark')
        return
    target_qty, target_trigger = _target(trader, op, p)
    _supersede_changed_unsent(trader, op, state, now_ms, target_qty, target_trigger)
    found = store.load(op['coi'])
    if found is None or store.known_not_submitted(op['coi']):
        if op.get('superseded_cois'):
            _require_existing_protection(trader, op, state, now_ms)
        # Existing durable terms are immutable on a known-not-submitted retry;
        # first creation takes the now-reconciled quantity, never stale exposure.
        if found is None:
            op.update(qty=float(target_qty), trigger=float(target_trigger))
            trader._save()
        ref = trader.broker.place_stop(market, symbol, op['side'], op['qty'],
                                      op['trigger'], op['coi'])
        op['new_ref'] = ref.__dict__
        op['phase'] = 'verify_new'
        trader._save()
    else:
        if found[1] is None:
            raise UnverifiedExecution('stop renewal replacement dispatch outcome unknown')
        if found[1]['terminal']:
            raise UnverifiedExecution('stop renewal replacement is no longer active')
        op['new_ref'] = _store_ref(trader, op['coi'])
        trader._save()

    # Each create/amend may race a genuine TP/SL fill. Do not use the pre-write
    # account snapshot to authorize the old stop cancellation.
    state = trader.broker.account_state()
    p, actual = _position(trader, op, state)
    if actual is None:
        _cleanup_flat(trader, op, state)
        return
    if (number(actual['qty']) != number(p['qty']) or
            (actual.get('partial_taken') is True and not p['partial_taken'])):
        return
    fresh = _active(state, trader, symbol, op['coi'])
    if fresh is None:
        raise UnverifiedExecution('stop renewal replacement not confirmed active')
    ref = dict(op['new_ref'], **fresh)
    target_qty, target_trigger = _target(trader, op, p)
    if ((p['side'] == 'LONG' and target_trigger < number(ref['trigger'])) or
            (p['side'] == 'SHORT' and target_trigger > number(ref['trigger']))):
        raise UnverifiedExecution('stop renewal would loosen existing protection')
    if target_trigger != number(ref['trigger']) or target_qty != number(ref['qty']):
        op.update(phase='amend_new', amend_qty=float(target_qty), amend_trigger=float(target_trigger))
        trader._save()
        trader.broker.modify_stop(market, trader._order_ref(ref), float(target_qty), float(target_trigger))
        state = trader.broker.account_state()
        p, actual = _position(trader, op, state)
        if actual is None:
            _cleanup_flat(trader, op, state)
            return
        if number(actual['qty']) != number(p['qty']):
            return
        fresh = _active(state, trader, symbol, op['coi'])
        if fresh is None:
            raise UnverifiedExecution('amended renewal stop not confirmed active')
        ref = dict(ref, **fresh)
    expiry = ref.get('order_expiry_ms')
    if (type(expiry) is not int or expiry <= op['old_ref']['order_expiry_ms'] or
            expiry <= now_ms + RENEWAL_LEAD_MS or ref.get('purpose') != 'SL' or
            ref.get('armed') is not True or
            ref.get('position_side') != op['side'] or
            number(ref.get('qty')) != number(actual['qty']) or
            number(ref.get('trigger')) != target_trigger):
        raise UnverifiedExecution('renewed stop expiry/coverage is not verified')
    op.update(new_ref=ref, phase='cancel_old')
    trader.orders.setdefault(symbol, {})['SL'] = dict(ref)
    trader._save()  # Persist replacement before the first possible old cancel.
    trader.broker.cancel(market, trader._order_ref(op['old_ref']))
    state = trader.broker.account_state()
    p, actual = _position(trader, op, state)
    if actual is None:
        _cleanup_flat(trader, op, state)
        return
    old = store.load(op['old_ref']['client_order_index'])
    if old is None or old[1] is None or not old[1]['terminal']:
        raise UnverifiedExecution('old stop cancellation is not terminal')
    remaining = _active(state, trader, symbol, op['coi'])
    if remaining is None or remaining.get('armed') is not True:
        raise UnverifiedExecution('renewed protection disappeared during old cancellation')
    _complete(trader, op)

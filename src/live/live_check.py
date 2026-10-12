"""User-operated minimum-notional execution check; defaults to read-only preview.

Not the strategy runner. No scheduled service is installed or activated. A real
order requires --execute and an interactive account-specific confirmation.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import json
import os
from pathlib import Path
import secrets
import sys
import time

from .authenticated import LighterTransport
from .execution import ExecutionEngine, positions
from .execution_evidence import Intent, UnverifiedExecution, number

MAX_NOTIONAL = Decimal('15')
SLIPPAGE = Decimal('0.005')
STOP_DISTANCE = Decimal('0.01')


def save(path, value):
    temp = path.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as f:
        json.dump(value, f, indent=2, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
    temp.replace(path)
    if os.name == 'posix':
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def price_tick(value, decimals, *, upward=False):
    tick = Decimal(1).scaleb(-decimals)
    return number(value).quantize(tick, rounding=ROUND_CEILING if upward else ROUND_FLOOR)


def market_detail(transport):
    data = transport.get('/api/v1/orderBookDetails', private=False)
    rows = [r for r in data.get('order_book_details', []) if r.get('symbol') == 'ETH' and r.get('market_type') == 'perp']
    if len(rows) != 1 or rows[0].get('status') != 'active' or rows[0].get('is_frozen'):
        raise RuntimeError('ETH perpetual market unavailable')
    return rows[0]


def minimum_plan(detail):
    mark = number(detail['mark_price'])
    if mark <= 0:
        raise ValueError('invalid mark')
    sd, pd = detail['supported_size_decimals'], detail['supported_price_decimals']
    if type(sd) is not int or type(pd) is not int or not 0 <= sd <= 12 or not 0 <= pd <= 12:
        raise ValueError('invalid market precision')
    tick = Decimal(1).scaleb(-sd)
    # Small minimum-quote buffer, rounded UP only for this isolated minimum test.
    qty = max(number(detail['min_base_amount']), number(detail['min_quote_amount']) * Decimal('1.01') / mark)
    qty = qty.quantize(tick, rounding=ROUND_CEILING)
    bound = price_tick(mark * (1 + SLIPPAGE), pd, upward=True)
    if qty <= 0 or qty * bound > MAX_NOTIONAL:
        raise RuntimeError('exchange minimum exceeds fixed 15 USDC check cap')
    return {'symbol':'ETH', 'market_id':detail['market_id'], 'quantity':str(qty),
            'reference_mark':str(mark), 'entry_price_bound':str(bound),
            'max_entry_notional':str(qty * bound), 'hard_notional_cap':str(MAX_NOTIONAL),
            'size_decimals':sd, 'price_decimals':pd,
            'stop_distance_fraction':str(STOP_DISTANCE), 'slippage_fraction':str(SLIPPAGE),
            'classification':'execution_check_not_strategy_signal',
            'cost_and_loss_possible':True}


def next_id(session):
    # Session IDs are drawn well within the exchange client ID integer range.
    return session['first_coi'] + len(session['close_ids']) + 2


def wait_position(transport, market_id, expected, timeout=15):
    end = time.monotonic() + timeout
    while True:
        actual = positions(transport.account())
        if any(k != market_id for k in actual):
            raise UnverifiedExecution('unexpected other position; account is not dedicated')
        if actual.get(market_id, Decimal(0)) == expected:
            return
        if time.monotonic() >= end:
            raise UnverifiedExecution('account position has not reconciled to confirmed fills')
        time.sleep(.5)


def cleanup(transport, engine, session, path):
    """Only reduce confirmed ETH long exposure. Never blindly resend a close."""
    entry_coi = session['first_coi']
    if engine.store.load(entry_coi) is None:
        if positions(transport.account()) or transport.active_orders():
            raise UnverifiedExecution('no entry intent but account is not empty')
        session['cleanup']='no_order_submitted'
        save(path, session)
        return
    try:
        entry = engine.recover(entry_coi)
    except UnverifiedExecution:
        # Cancel only the known entry ID; the engine verifies terminal outcome.
        entry = engine.cancel(entry_coi)
    filled = number(entry['filled_qty'])
    session['entry_result'] = entry
    market_id = session['plan']['market_id']
    # Recover any already-reserved close before considering a new one.
    for coi in session['close_ids']:
        if engine.store.load(coi) is not None:
            engine.recover(coi)
    def reconciled_quantity():
        remaining = filled
        stop_coi = entry_coi + 1
        if engine.store.load(stop_coi) is not None:
            stop_row = transport.order(stop_coi)
            if stop_row is None:
                saved = engine.store.terminal_result(stop_coi)
                if saved is None:
                    raise UnverifiedExecution('stop outcome unknown during cleanup')
            else:
                saved = engine.store.record(stop_coi, stop_row)
            remaining -= number(saved['filled_qty'])
        for close_id in session['close_ids']:
            if engine.store.load(close_id) is not None:
                remaining -= number(engine.recover(close_id)['filled_qty'])
        if remaining < 0:
            raise UnverifiedExecution('execution totals exceed original position')
        wait_position(transport, market_id, remaining)
        return remaining
    for attempt in range(3):
        qty = reconciled_quantity()
        if qty < 0 or qty > filled:
            raise UnverifiedExecution('unexpected exposure; cleanup cannot attribute it')
        if qty == 0:
            break
        detail = market_detail(transport)
        bound = price_tick(number(detail['mark_price']) * (1 - SLIPPAGE), detail['supported_price_decimals'])
        # If the process died after journaling a close ID but before reserving
        # it, reuse that ID. A reserved one was recovered above, never resent.
        coi = next((c for c in session['close_ids'] if engine.store.load(c) is None), None)
        if coi is None:
            coi = next_id(session)
            session['close_ids'].append(coi)
        intent = Intent(transport.account_index, market_id, coi, True, True,
                        str(qty), 'market', 'immediate-or-cancel', str(bound))
        save(path, session)
        result = engine.create(intent, detail['supported_size_decimals'], detail['supported_price_decimals'])
        session.setdefault('close_results', {})[str(coi)] = result
        save(path, session)
        # Account reads can lag the terminal order. Do not issue another close
        # against a stale quantity. A simultaneously triggered SL may reduce it
        # further; that requires separate reconciliation rather than a resend.
        reconciled_quantity()
    if reconciled_quantity() != 0:
        raise UnverifiedExecution('close retries exhausted with a confirmed residual')
    wait_position(transport, market_id, Decimal(0))
    stop_coi = entry_coi + 1
    if engine.store.load(stop_coi) is not None:
        session['stop_final_result'] = engine.cancel(stop_coi)
    active = transport.active_orders()
    if active:
        raise UnverifiedExecution('active orders remain; cleanup not complete')
    wait_position(transport, market_id, Decimal(0))
    account = transport.account()
    session['cleanup'] = 'confirmed_flat_no_active_orders'
    session['equity_after'] = account.get('total_asset_value')
    session['completed_at_ms'] = int(time.time()*1000)
    session['execution_check_passed'] = bool(filled > 0 and session.get('stop_registered'))
    session['limitations'] = ['no strategy profitability evidence', 'partial fills and crash recovery are unit-tested, not forced on mainnet',
        'account equity change is not a reconciled fee/funding ledger', 'strategy automation remains disabled']
    save(path, session)


def perform(transport, engine, plan, path):
    if path.exists():
        raise RuntimeError('check already exists; use --recover, never start it again')
    current = minimum_plan(market_detail(transport))
    for field in ('market_id','size_decimals','price_decimals','quantity'):
        if current[field] != plan[field]:
            raise RuntimeError('market/minimum changed; generate a new preview')
    if number(current['entry_price_bound']) > number(plan['entry_price_bound']):
        raise RuntimeError('price exceeds reviewed bound; generate a new preview')
    account = transport.account()
    if positions(account) or transport.active_orders():
        raise RuntimeError('check requires a flat account with no active orders')
    if number(account.get('available_balance')) < number(plan['max_entry_notional']) + 1:
        raise RuntimeError('insufficient unlevered collateral for the bounded check')
    session = {'plan':plan, 'first_coi':secrets.randbelow(2**40-100)+1, 'close_ids':[],
               'account_index':transport.account_index,'network':transport.network,
               'equity_before':account.get('total_asset_value'), 'started_at_ms':int(time.time()*1000),
               'stop_registered':False,'execution_check_passed':False}
    save(path, session)
    try:
        entry = Intent(transport.account_index, plan['market_id'], session['first_coi'], False, False,
            plan['quantity'], 'market', 'immediate-or-cancel', plan['entry_price_bound'])
        result = engine.create(entry, plan['size_decimals'], plan['price_decimals'])
        session['entry_result'] = result
        save(path, session)
        qty = number(result['filled_qty'])
        wait_position(transport, plan['market_id'], qty)
        if qty > 0:
            trigger = price_tick(number(result['average_price']) * (1 - STOP_DISTANCE), plan['price_decimals'])
            bound = price_tick(trigger * (1 - SLIPPAGE), plan['price_decimals'])
            stop = Intent(transport.account_index, plan['market_id'], session['first_coi']+1,
                          True, True, str(qty), 'stop-loss', 'immediate-or-cancel', str(bound), str(trigger))
            session['stop_intent'] = asdict(stop)
            save(path, session)
            session['stop_result'] = engine.create(stop, plan['size_decimals'], plan['price_decimals'], stop_quantity=str(qty))
            session['stop_registered'] = True
            save(path, session)
    except Exception as exc:
        session['failure_type'] = type(exc).__name__
        save(path, session)
    finally:
        try:
            cleanup(transport, engine, session, path)
        except Exception as exc:
            session['cleanup']='UNCONFIRMED_REQUIRES_USER_ATTENTION'
            session['cleanup_error_type']=type(exc).__name__
            save(path, session)
            raise RuntimeError('cleanup unconfirmed: inspect Lighter positions/orders and run --recover') from None
    return session


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--execute', action='store_true')
    mode.add_argument('--recover', action='store_true')
    args=parser.parse_args()
    config=json.loads(args.config.read_text(encoding='utf-8-sig'))
    directory=Path(config['check_directory'])
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    transport=LighterTransport(**{k:config[k] for k in
        ('network','account_index','api_key_index','key_file','expected_address')})
    engine=None
    try:
        if args.execute or args.recover:
            if not sys.stdin.isatty():
                raise RuntimeError('real-account operations require a human interactive terminal')
            print('REAL FUNDS: bounded ETH long execution check; costs/loss possible. Strategy is not started.')
            required=f'{"RECOVER" if args.recover else "EXECUTE"} {transport.account_index}'
            if input(f'Type {required}: ').strip() != required:
                raise RuntimeError('confirmation did not match; no submission')
            engine=ExecutionEngine(transport,directory)
            if args.recover:
                session=json.loads((directory/'session.json').read_text())
                if session['account_index']!=transport.account_index or session['network']!=transport.network:
                    raise RuntimeError('recovery account mismatch')
                cleanup(transport,engine,session,directory/'session.json')
            else:
                preview=json.loads((directory/'preview.json').read_text())
                if (preview['account_index'] != transport.account_index or preview['network'] != transport.network or
                        not 0 <= time.time()-preview['created_at'] <= 300):
                    raise RuntimeError('preview expired or belongs to another account/network')
                # Recompute, do not trust edited preview sizing fields.
                expected=minimum_plan(preview['market_detail'])
                if expected != preview['plan']:
                    raise RuntimeError('preview terms were edited')
                session=perform(transport,engine,preview['plan'],directory/'session.json')
            print(json.dumps(session,indent=2))
            return 0 if session.get('execution_check_passed') else 1
        if (directory/'session.json').exists():
            raise RuntimeError('existing session must be inspected/recovered; preview cannot reset it')
        account=transport.account()
        if positions(account) or transport.active_orders():
            raise RuntimeError('account is not flat and clear')
        detail=market_detail(transport)
        preview={'account_index':transport.account_index,'network':transport.network,
            'created_at':time.time(),'available_balance':account.get('available_balance'),
            'market_detail':detail,'plan':minimum_plan(detail),'orders_submitted':0}
        save(directory/'preview.json',preview)
        print(json.dumps({k:v for k,v in preview.items() if k!='market_detail'},indent=2))
        return 0
    finally:
        if engine is not None:
            engine.close()
        transport.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f'Check stopped: {exc}', file=sys.stderr)
        raise SystemExit(1)

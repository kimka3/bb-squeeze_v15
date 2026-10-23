"""Pure, conservative policy for explicitly reviewed safety-HALT releases.

Nothing here reads a file, sends an order, changes state or grants entry
permission. The account-owning runner supplies a fresh reconciled AccountState
after its ordinary protection service. Its existing exchange recovery gate
still determines whether future entries may resume.
"""
from __future__ import annotations

import hashlib
import json
import math

from data_io import H4
from . import guards


GLOBAL_REASONS = {
    'pending order outcome unknown; manual reconciliation required': 'order_outcome',
    'account execution history does not reconcile; new entries halted': 'account_execution',
}
SYMBOL_REASONS = {
    'unexplained disappearance; execution evidence required': 'account_execution',
    'unexplained reduction; execution evidence required': 'account_execution',
    'position side changed unexpectedly': 'account_execution',
    'protective stop missing; entries halted': 'protection',
    'protective cancellation unresolved; new entries halted': 'order_outcome',
    'entry/protection outcome unresolved; execution evidence required': 'order_outcome',
    'exit outcome unresolved; execution evidence required': 'order_outcome',
    'protective placement unresolved; execution evidence required': 'order_outcome',
    'stop amendment outcome unresolved; execution evidence required': 'order_outcome',
    'stop renewal requires reconciliation; execution evidence required': 'order_outcome',
}
HISTORY_PREFIX = 'unsupported history requires manual reconciliation; affected markets: '

# These are the fields owned by Trader._save. Volatile market marks and report
# timestamps are intentionally not part of a case; position/cycle/bar changes
# are. Missing state is not silently substituted with an empty safety state.
ANCHOR_FIELDS = (
    'positions', 'setups', 'pending', 'orders', 'last_bar_ms',
    'pending_operation', 'unresolved_operations', 'publication_pending',
    'publication_pending_evidence', 'inflight_bar_ms', 'halted', 'halt_reasons',
    'residual_exits', 'orphan_orders', 'data_gaps', 'protection_generation',
)
_MISSING = object()


def _string_list(value, *, nonempty=False):
    return (type(value) is list and (bool(value) or not nonempty)
            and all(type(item) is str and bool(item) for item in value)
            and len(value) == len(set(value)))


def classify_reason(reason, universe):
    """Recognize only stable reasons whose conditions this policy rechecks.

    Exception text, new reason formats and ambiguous legacy reasons do not
    acquire release authority through a substring or an operator annotation.
    """
    if type(reason) is not str:
        return None
    if reason in GLOBAL_REASONS:
        return GLOBAL_REASONS[reason]
    if reason.startswith(HISTORY_PREFIX):
        suffix = reason[len(HISTORY_PREFIX):]
        try:
            markets = json.loads(suffix)
        except (TypeError, ValueError):
            return None
        if (type(markets) is list and markets
                and all(type(market) is int and market >= 0 for market in markets)
                and markets == sorted(set(markets)) and suffix == str(markets)):
            return 'unsupported_history'
        return None
    if (not isinstance(universe, (tuple, list))
            or not all(type(symbol) is str and symbol for symbol in universe)):
        return None
    for symbol in universe:
        for suffix, category in SYMBOL_REASONS.items():
            if reason == symbol + ': ' + suffix:
                return category
    return None


def reason_id(reason):
    if type(reason) is not str or not reason:
        raise ValueError('nonempty exact halt reason required')
    return hashlib.sha256(reason.encode('utf-8')).hexdigest()


def _json_value(value):
    if value is None or type(value) in (str, bool, int):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) in (tuple, list):
        return all(_json_value(item) for item in value)
    if type(value) is dict:
        return all(type(key) is str and _json_value(item) for key, item in value.items())
    return False


def state_anchor(snapshot):
    """Digest exact persistent safety/strategy state; never invent defaults."""
    if (type(snapshot) is not dict or any(key not in snapshot for key in ANCHOR_FIELDS)
            or not all(_json_value(snapshot[key]) for key in ANCHOR_FIELDS)):
        raise ValueError('complete finite JSON strategy snapshot required')
    value = {key: snapshot[key] for key in ANCHOR_FIELDS}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def evaluate_release(trader, state, coverage, selected_reasons, now_ms):
    """Return fixed blocker codes without modifying any supplied object.

    `state` must be a fresh broker account_state result, not a saved report.
    Selection, expiry, activation identity and case anchoring are also checked
    by the durable request handler. Other retained HALT reasons are permitted;
    this function never approves their removal.
    """
    blockers = []

    def block(code):
        if code not in blockers:
            blockers.append(code)

    if type(now_ms) is not int or now_ms <= 0:
        block('INVALID_CURRENT_TIME')
    reasons = getattr(trader, 'halt_reasons', _MISSING)
    universe = getattr(trader, 'universe', _MISSING)
    if getattr(trader, 'halted', _MISSING) is not True or not _string_list(reasons, nonempty=True):
        block('INVALID_HALT_STATE')
    if not _string_list(selected_reasons, nonempty=True):
        block('INVALID_REASON_SELECTION')
    else:
        if not _string_list(reasons) or any(reason not in reasons for reason in selected_reasons):
            block('SELECTED_REASON_CHANGED')
        if any(classify_reason(reason, universe) is None for reason in selected_reasons):
            block('UNSUPPORTED_HALT_REASON')

    required_empty = {
        'unresolved_operations': (dict, 'UNRESOLVED_OPERATIONS'),
        'residual_exits': (dict, 'RESIDUAL_EXITS'),
        'orphan_orders': (dict, 'ORPHAN_ORDERS'),
        'data_gaps': (list, 'STRATEGY_DATA_GAPS'),
        'publication_pending_evidence': (list, 'PUBLICATION_PENDING'),
    }
    for name, (expected_type, code) in required_empty.items():
        value = getattr(trader, name, _MISSING)
        if type(value) is not expected_type or value:
            block(code)
    for name, code in (('pending_operation', 'PENDING_OPERATION'),
                       ('inflight_bar_ms', 'INFLIGHT_BAR'),
                       ('deferred_entries', 'DEFERRED_ENTRY')):
        if getattr(trader, name, _MISSING) is not None:
            block(code)
    if getattr(trader, 'publication_pending', _MISSING) is not False:
        block('PUBLICATION_PENDING')
    last_bar = getattr(trader, 'last_bar_ms', _MISSING)
    if (type(last_bar) is not int or type(now_ms) is not int or now_ms <= 0
            or last_bar != now_ms // H4 * H4 - H4):
        block('CONFIRMED_BAR_NOT_CURRENT')

    broker = getattr(trader, 'broker', None)
    transport = getattr(broker, 'transport', None)
    if getattr(transport, 'read_diagnostics_error', _MISSING) is not None:
        block('READ_DIAGNOSTICS_UNAVAILABLE')
    recovery = getattr(trader, 'exchange_recovery', None)
    if recovery is None or getattr(recovery, 'storage_error', _MISSING) is not None:
        block('RECOVERY_EVIDENCE_UNAVAILABLE')
    try:
        health = coverage.snapshot() if callable(getattr(coverage, 'snapshot', None)) else coverage
    except Exception:
        health = None
    if type(health) is not dict or type(health.get('active_gap_streams')) is not list:
        block('RUNTIME_COVERAGE_UNAVAILABLE')
    elif health['active_gap_streams']:
        block('ACTIVE_RUNTIME_GAP')

    if getattr(state, 'execution_changes_verified', _MISSING) is not True:
        block('EXECUTION_NOT_RECONCILED')
    if getattr(state, 'active_orders_verified', _MISSING) is not True:
        block('ACTIVE_ORDERS_NOT_VERIFIED')
    report = getattr(state, 'ledger_report', None)
    if type(report) is not dict:
        block('ACCOUNT_REPORT_UNAVAILABLE')
        report = {}
    for name, code in (('execution_changes_verified', 'EXECUTION_NOT_RECONCILED'),
                       ('owned_exposure_matches_account', 'OWNED_EXPOSURE_MISMATCH'),
                       ('historical_evidence_complete', 'HISTORICAL_EVIDENCE_INCOMPLETE')):
        if report.get(name) is not True:
            block(code)
    for name, expected_type, code in (
        ('historical_issues', list, 'HISTORICAL_EVIDENCE_INCOMPLETE'),
        ('unresolved_orders', dict, 'UNRESOLVED_EXCHANGE_ORDERS'),
        ('unsafe_protection', dict, 'UNSAFE_PROTECTION'),
        ('publication_pending_evidence', list, 'PUBLICATION_PENDING'),
    ):
        if type(report.get(name)) is not expected_type or report[name]:
            block(code)
    if type(report.get('quarantined_history_count')) is not int or report['quarantined_history_count'] != 0:
        block('HISTORICAL_EVIDENCE_INCOMPLETE')
    if report.get('publication_pending') is not False:
        block('PUBLICATION_PENDING')
    collection = report.get('history_collection')
    if type(collection) is not dict or collection.get('head_complete') is not True:
        block('HISTORY_HEAD_INCOMPLETE')

    internal = getattr(trader, 'positions', None)
    tracked_orders = getattr(trader, 'orders', None)
    actual = getattr(state, 'positions', None)
    active = getattr(state, 'active_orders', None)
    symbol_map = getattr(trader, 'config_symbol_map', None)
    valid_positions = (type(internal) is dict and type(actual) is dict and type(active) is dict
                       and type(tracked_orders) is dict and type(symbol_map) is dict)
    valid_universe = (isinstance(universe, (list, tuple)) and bool(universe)
                      and all(type(symbol) is str and bool(symbol) for symbol in universe)
                      and len(universe) == len(set(universe)) and type(symbol_map) is dict
                      and all(symbol in symbol_map and type(symbol_map[symbol]) is str
                              and bool(symbol_map[symbol]) for symbol in universe))
    if not valid_universe:
        block('UNIVERSE_SCHEMA_INVALID')
        valid_positions = False
    if valid_positions:
        allowed_symbols = {symbol_map[symbol] for symbol in universe}
        for symbol, pos in internal.items():
            if (type(symbol) is not str or symbol not in universe or type(pos) is not dict
                    or pos.get('side') not in ('LONG', 'SHORT') or not _positive(pos.get('qty'))
                    or not _positive(pos.get('stop'))):
                valid_positions = False
        for symbol, pos in actual.items():
            if (type(symbol) is not str or symbol not in allowed_symbols or type(pos) is not dict
                    or pos.get('side') not in ('LONG', 'SHORT') or not _positive(pos.get('qty'))):
                valid_positions = False
        for symbol, refs in tracked_orders.items():
            if symbol not in internal or type(refs) is not dict:
                valid_positions = False
                continue
            for role, ref in refs.items():
                if (role not in ('SL', 'TP') or type(ref) is not dict
                        or type(ref.get('client_order_index')) is not int
                        or ref['client_order_index'] < 0):
                    valid_positions = False
        active_ids = set()
        for symbol, rows in active.items():
            if type(symbol) is not str or symbol not in allowed_symbols or type(rows) is not list:
                valid_positions = False
                continue
            if rows and symbol not in actual:
                block('ACTIVE_ORDERS_WITHOUT_POSITION')
            for row in rows:
                if (type(row) is not dict or type(row.get('client_order_index')) is not int
                        or row['client_order_index'] < 0 or row['client_order_index'] in active_ids
                        or type(row.get('order_index')) is not int or row['order_index'] < 0
                        or row.get('purpose') not in ('ENTRY', 'EXIT', 'SL', 'TP')
                        or row.get('position_side') not in ('LONG', 'SHORT')
                        or not _positive(row.get('qty')) or type(row.get('armed')) is not bool
                        or (row.get('purpose') in ('SL', 'TP') and not _positive(row.get('trigger')))):
                    valid_positions = False
                    continue
                active_ids.add(row['client_order_index'])
    if not valid_positions:
        block('POSITION_OR_ORDER_SCHEMA_INVALID')
    else:
        try:
            if guards.reconcile(internal, state, symbol_map).halt:
                block('INTERNAL_ACCOUNT_MISMATCH')
            for symbol, pos in internal.items():
                refs = tracked_orders.get(symbol)
                ref = refs.get('SL') if type(refs) is dict else None
                coi = ref.get('client_order_index') if type(ref) is dict else None
                candidates = [row for row in active.get(symbol_map[symbol], [])
                              if type(coi) is int and coi >= 0 and row.get('client_order_index') == coi]
                if len(candidates) != 1:
                    block('NATIVE_STOP_NOT_VERIFIED')
                    continue
                stop = candidates[0]
                expiry = stop.get('order_expiry_ms')
                if (stop.get('purpose') != 'SL' or stop.get('armed') is not True
                        or stop.get('position_side') != pos['side']
                        or not _positive(stop.get('qty')) or stop['qty'] < pos['qty']
                        or not _positive(stop.get('trigger'))
                        or (pos['side'] == 'LONG' and stop['trigger'] < pos['stop'])
                        or (pos['side'] == 'SHORT' and stop['trigger'] > pos['stop'])
                        or (expiry is not None and (type(expiry) is not int
                            or type(now_ms) is not int or expiry <= now_ms))):
                    block('NATIVE_STOP_NOT_VERIFIED')
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            block('POSITION_OR_ORDER_SCHEMA_INVALID')
    try:
        if (type(getattr(state, 'equity', None)) not in (int, float)
                or type(getattr(state, 'maintenance_margin', None)) not in (int, float)):
            block('MARGIN_NOT_SAFE')
        elif guards.check_margin(state, trader.config).block_entries:
            block('MARGIN_NOT_SAFE')
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        block('MARGIN_NOT_SAFE')
    return blockers

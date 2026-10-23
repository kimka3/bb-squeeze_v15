"""Pure release policy: no live venue, credentials, filesystem or mutations."""
from copy import deepcopy
import hashlib
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
from data_io import H4
from live.broker import AccountState
from live.config import LiveConfig
from live.resolution_policy import (ANCHOR_FIELDS, classify_reason, evaluate_release,
                                    reason_id, state_anchor)

UNKNOWN = 'pending order outcome unknown; manual reconciliation required'
NOW = 100 * H4 + 50_000


def clean():
    trader = SimpleNamespace(halted=True, halt_reasons=[UNKNOWN], universe=['BTCUSDT', 'ETHUSDT'],
        pending_operation=None, unresolved_operations={}, residual_exits={}, orphan_orders={},
        data_gaps=[], publication_pending=False, publication_pending_evidence=[],
        inflight_bar_ms=None, deferred_entries=None, last_bar_ms=99 * H4,
        broker=SimpleNamespace(transport=SimpleNamespace(read_diagnostics_error=None)),
        exchange_recovery=SimpleNamespace(storage_error=None), positions={}, orders={},
        config_symbol_map={'BTCUSDT': 'BTC', 'ETHUSDT': 'ETH'}, config=LiveConfig())
    report = {'execution_changes_verified': True, 'owned_exposure_matches_account': True,
        'historical_evidence_complete': True, 'historical_issues': [], 'quarantined_history_count': 0,
        'unresolved_orders': {}, 'unsafe_protection': {}, 'publication_pending': False,
        'publication_pending_evidence': [], 'history_collection': {'head_complete': True},
        'performance_complete': False, 'unknown_fee_fills': 5}
    state = AccountState(300., 0., {}, {}, True, True, report)
    coverage = {'active_gap_streams': [], 'coverage_complete': False, 'failure_count': 19}
    return trader, state, coverage


def evaluate(items, selected=None):
    return evaluate_release(*items, [UNKNOWN] if selected is None else selected, NOW)


def snapshot():
    trader, _, _ = clean()
    result = {key: deepcopy(getattr(trader, key, {})) for key in ANCHOR_FIELDS}
    return result


@pytest.mark.parametrize('reason,category', [
    (UNKNOWN, 'order_outcome'),
    ('account execution history does not reconcile; new entries halted', 'account_execution'),
    ('unsupported history requires manual reconciliation; affected markets: [0, 2]', 'unsupported_history'),
    ('ETHUSDT: protective stop missing; entries halted', 'protection'),
    ('ETHUSDT: unexplained reduction; execution evidence required', 'account_execution'),
    ('ETHUSDT: protective cancellation unresolved; new entries halted', 'order_outcome'),
])
def test_only_stable_exact_supported_reasons(reason, category):
    assert classify_reason(reason, ['ETHUSDT']) == category


@pytest.mark.parametrize('reason', [None, '', UNKNOWN + ' extra', ' ' + UNKNOWN,
    'SOLUSDT: protective stop missing; entries halted',
    'ETHUSDT: entry/protection outcome unresolved: HTTP 503',
    'bar history gap: strategy processing paused; protection service remains active',
    'unsupported history requires manual reconciliation; affected markets: []',
    'unsupported history requires manual reconciliation; affected markets: [true]',
    'unsupported history requires manual reconciliation; affected markets: [1, 0]',
    'unsupported history requires manual reconciliation; affected markets: [0, 0]',
    'unsupported history requires manual reconciliation; affected markets: [0,2]',
    'unsupported history requires manual reconciliation; affected markets: [-1]',
    'unsupported history requires manual reconciliation; affected markets: [0.0]',
])
def test_unrecognized_reason_never_acquires_authority(reason):
    assert classify_reason(reason, ['ETHUSDT']) is None


def test_anchor_ignores_only_unrelated_live_fields_and_order_of_mapping():
    value = snapshot()
    before = state_anchor(value)
    value['live_price'] = 123.45
    value['updated_ms'] = 9000
    assert state_anchor(dict(reversed(list(value.items())))) == before
    assert len(before) == 64
    assert reason_id(UNKNOWN) == hashlib.sha256(UNKNOWN.encode()).hexdigest()


@pytest.mark.parametrize('field,value', [('last_bar_ms', 101 * H4), ('halt_reasons', [UNKNOWN, 'new']),
    ('positions', {'ETHUSDT': {'qty': 1}}), ('orders', {'ETHUSDT': {'SL': {'client_order_index': 3}}}),
    ('pending_operation', {'coi': 3}), ('setups', {'ETHUSDT': [1]}), ('inflight_bar_ms', 100 * H4),
    ('protection_generation', {'ETHUSDT': 2}), ('publication_pending', True)])
def test_anchor_changes_when_reviewed_strategy_or_safety_state_changes(field, value):
    saved = snapshot()
    before = state_anchor(saved)
    saved[field] = value
    assert state_anchor(saved) != before


@pytest.mark.parametrize('mutation', ['missing', 'nan', 'nonstringkey', 'object'])
def test_invalid_anchor_is_rejected(mutation):
    value = snapshot()
    if mutation == 'missing': value.pop('positions')
    if mutation == 'nan': value['positions'] = {'ETHUSDT': {'qty': float('nan')}}
    if mutation == 'nonstringkey': value['positions'] = {1: {}}
    if mutation == 'object': value['positions'] = object()
    with pytest.raises(ValueError): state_anchor(value)


def test_clean_verified_state_can_release_selected_reason_without_mutation():
    items = clean()
    before = deepcopy(items)
    assert evaluate(items) == []
    assert items == before
    assert items[0].halted and items[0].halt_reasons == [UNKNOWN]


@pytest.mark.parametrize('suffix', [
    'entry/protection outcome unresolved', 'exit outcome unresolved',
    'protective placement unresolved', 'stop amendment outcome unresolved',
    'stop renewal requires reconciliation',
])
def test_canonical_execution_cause_still_requires_all_current_reconciliation(suffix):
    items = clean()
    reason = f'ETHUSDT: {suffix}; execution evidence required'
    items[0].halt_reasons = [reason]
    assert evaluate(items, [reason]) == []
    items[0].pending_operation = {'kind': 'entry'}
    assert 'PENDING_OPERATION' in evaluate(items, [reason])
    items[0].pending_operation = None
    items[1].ledger_report['execution_changes_verified'] = False
    assert 'EXECUTION_NOT_RECONCILED' in evaluate(items, [reason])


def test_other_halt_reason_is_not_authorized_or_automatically_cleared():
    items = clean()
    items[0].halt_reasons.append('unrecognized manual safety condition')
    assert evaluate(items) == []
    assert items[0].halt_reasons[-1] == 'unrecognized manual safety condition'
    assert 'UNSUPPORTED_HALT_REASON' in evaluate(items, items[0].halt_reasons)


@pytest.mark.parametrize('selected', [[], [UNKNOWN, UNKNOWN], [None], UNKNOWN, ['absent']])
def test_invalid_or_changed_selection_blocks(selected):
    assert evaluate(clean(), selected)


@pytest.mark.parametrize('field,value,code', [
    ('pending_operation', {}, 'PENDING_OPERATION'),
    ('unresolved_operations', {'x': {}}, 'UNRESOLVED_OPERATIONS'),
    ('unresolved_operations', None, 'UNRESOLVED_OPERATIONS'),
    ('residual_exits', {'ETHUSDT': {}}, 'RESIDUAL_EXITS'),
    ('orphan_orders', {'ETHUSDT': {}}, 'ORPHAN_ORDERS'),
    ('data_gaps', [{}], 'STRATEGY_DATA_GAPS'),
    ('publication_pending', 0, 'PUBLICATION_PENDING'),
    ('publication_pending_evidence', [{}], 'PUBLICATION_PENDING'),
    ('inflight_bar_ms', NOW, 'INFLIGHT_BAR'),
    ('deferred_entries', {}, 'DEFERRED_ENTRY'),
    ('last_bar_ms', 98 * H4, 'CONFIRMED_BAR_NOT_CURRENT'),
    ('last_bar_ms', True, 'CONFIRMED_BAR_NOT_CURRENT'),
    ('halted', 1, 'INVALID_HALT_STATE'),
])
def test_active_or_malformed_strategy_blockers(field, value, code):
    items = clean()
    setattr(items[0], field, value)
    assert code in evaluate(items)


@pytest.mark.parametrize('field,value,code', [
    ('execution_changes_verified', 1, 'EXECUTION_NOT_RECONCILED'),
    ('owned_exposure_matches_account', False, 'OWNED_EXPOSURE_MISMATCH'),
    ('historical_evidence_complete', False, 'HISTORICAL_EVIDENCE_INCOMPLETE'),
    ('historical_issues', [{'id': '1'}], 'HISTORICAL_EVIDENCE_INCOMPLETE'),
    ('quarantined_history_count', True, 'HISTORICAL_EVIDENCE_INCOMPLETE'),
    ('quarantined_history_count', 1, 'HISTORICAL_EVIDENCE_INCOMPLETE'),
    ('unresolved_orders', {'1': {}}, 'UNRESOLVED_EXCHANGE_ORDERS'),
    ('unsafe_protection', {'1': {}}, 'UNSAFE_PROTECTION'),
    ('publication_pending', True, 'PUBLICATION_PENDING'),
    ('history_collection', {'head_complete': 1}, 'HISTORY_HEAD_INCOMPLETE'),
])
def test_fresh_account_report_must_explicitly_certify_each_condition(field, value, code):
    items = clean()
    items[1].ledger_report[field] = value
    assert code in evaluate(items)
    items[1].ledger_report.pop(field)
    assert code in evaluate(items)


def test_diagnostics_storage_and_runtime_gap_are_independent_blockers():
    items = clean()
    items[0].broker.transport.read_diagnostics_error = 'OSError'
    items[0].exchange_recovery.storage_error = 'OSError'
    items[2]['active_gap_streams'] = ['poll']
    assert {'READ_DIAGNOSTICS_UNAVAILABLE', 'RECOVERY_EVIDENCE_UNAVAILABLE',
            'ACTIVE_RUNTIME_GAP'} <= set(evaluate(items))


def test_damaged_coverage_does_not_become_clean():
    items = clean()
    class Coverage:
        def snapshot(self): raise OSError('secret')
    items = (items[0], items[1], Coverage())
    assert 'RUNTIME_COVERAGE_UNAVAILABLE' in evaluate(items)


def protected():
    items = clean()
    trader, state, _ = items
    trader.positions = {'ETHUSDT': {'qty': 2., 'side': 'LONG', 'stop': 90., 'entry_ms': H4}}
    trader.orders = {'ETHUSDT': {'SL': {'client_order_index': 5}}}
    state.positions = {'ETH': {'qty': 2., 'side': 'LONG'}}
    state.active_orders = {'ETH': [{'client_order_index': 5, 'order_index': 9, 'purpose': 'SL', 'armed': True,
        'position_side': 'LONG', 'qty': 2., 'trigger': 90., 'order_expiry_ms': NOW + 100000}]}
    return items


def test_current_owned_position_with_matching_native_stop_is_supported():
    assert evaluate(protected()) == []


@pytest.mark.parametrize('field,value', [('armed', False), ('armed', 1), ('qty', 1.),
    ('trigger', 89.), ('purpose', 'TP'), ('position_side', 'SHORT'),
    ('order_expiry_ms', NOW), ('order_expiry_ms', True), ('client_order_index', 7)])
def test_unprotected_or_wrong_owned_stop_cannot_release_halt(field, value):
    items = protected()
    items[1].active_orders['ETH'][0][field] = value
    assert {'NATIVE_STOP_NOT_VERIFIED', 'POSITION_OR_ORDER_SCHEMA_INVALID'} & set(evaluate(items))


@pytest.mark.parametrize('field,value', [('qty', None), ('order_index', True),
    ('client_order_index', True), ('purpose', 'unknown'), ('position_side', 'INVALID')])
def test_other_active_order_schema_cannot_hide_behind_valid_stop(field, value):
    items = protected()
    row = deepcopy(items[1].active_orders['ETH'][0])
    row.update(client_order_index=10, order_index=11, purpose='TP', trigger=110.)
    row[field] = value
    items[1].active_orders['ETH'].append(row)
    assert 'POSITION_OR_ORDER_SCHEMA_INVALID' in evaluate(items)


def test_duplicate_active_identity_is_rejected():
    items = protected()
    items[1].active_orders['ETH'].append(deepcopy(items[1].active_orders['ETH'][0]))
    assert 'POSITION_OR_ORDER_SCHEMA_INVALID' in evaluate(items)


def test_missing_universe_is_not_a_certifiable_state():
    items = clean()
    del items[0].universe
    assert 'UNIVERSE_SCHEMA_INVALID' in evaluate(items)


def test_missing_order_does_not_prove_flat_or_non_submission():
    items = protected()
    items[1].active_orders = {}
    assert 'NATIVE_STOP_NOT_VERIFIED' in evaluate(items)
    items[1].positions = {}
    assert 'INTERNAL_ACCOUNT_MISMATCH' in evaluate(items)


def test_side_quantity_and_ownership_mismatch_stay_blocked():
    items = protected()
    items[1].positions['ETH']['qty'] = 1.
    assert 'INTERNAL_ACCOUNT_MISMATCH' in evaluate(items)
    items[1].positions['ETH']['qty'] = float('nan')
    assert 'POSITION_OR_ORDER_SCHEMA_INVALID' in evaluate(items)


def test_live_active_order_in_flat_market_is_not_ignored():
    items = clean()
    items[1].active_orders = {'ETH': [{'client_order_index': 5}]}
    assert 'ACTIVE_ORDERS_WITHOUT_POSITION' in evaluate(items)


@pytest.mark.parametrize('equity,maintenance', [(0., 0.), (float('nan'), 0.),
    (300., 101.), (300., -1.), (True, 0.)])
def test_existing_margin_block_and_invalid_values_are_not_relaxed(equity, maintenance):
    items = clean()
    items[1].equity, items[1].maintenance_margin = equity, maintenance
    assert 'MARGIN_NOT_SAFE' in evaluate(items)


def test_existing_exact_margin_threshold_is_preserved():
    items = clean()
    items[1].maintenance_margin = 100.
    assert evaluate(items) == []


@pytest.mark.parametrize('now', [True, None, 0, NOW + H4])
def test_invalid_time_or_new_boundary_requires_new_review(now):
    assert evaluate_release(*clean(), [UNKNOWN], now)

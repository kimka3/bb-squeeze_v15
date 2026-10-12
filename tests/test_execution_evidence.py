import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from live.execution_evidence import Intent, IntentStore, UnverifiedExecution, verify_order, verify_stop


def intent(**kwargs):
    return Intent(**dict(dict(account_index=100, market_index=7, client_order_index=123,
        is_ask=True, reduce_only=False, quantity='10', order_type='market',
        time_in_force='immediate-or-cancel', price='2'), **kwargs))


def row(**kwargs):
    return dict(dict(owner_account_index=100, market_index=7, client_order_index=123,
        order_index=456, is_ask=True, reduce_only=False, initial_base_amount='10',
        type='market', time_in_force='immediate-or-cancel', price='2', trigger_price='0',
        filled_base_amount='4', filled_quote_amount='8.4', remaining_base_amount='6',
        status='canceled-not-enough-liquidity', trigger_status='na'), **kwargs)


def test_partial_canceled_is_real_partial_not_requested_quantity():
    r = verify_order(row(), intent())
    assert r['terminal'] and r['filled_qty'] == '4' and r['average_price'] == '2.1'
    assert r['fees_verified'] is False and r['position_verified'] is False


@pytest.mark.parametrize('field,value', [
    ('owner_account_index', 101), ('market_index', 8), ('client_order_index', 124),
    ('reduce_only', True), ('is_ask', False), ('is_ask', 1),
    ('initial_base_amount', '11'), ('price', '3'), ('trigger_price', '1'),
    ('type', 'limit'), ('time_in_force', 'post-only'), ('order_index', None),
    ('filled_base_amount', 'NaN'), ('filled_base_amount', '-1'),
    ('filled_base_amount', '11'), ('filled_quote_amount', None),
    ('filled_quote_amount', 'Infinity'), ('filled_base_amount', '0'),
    ('status', 'unknown'), ('status', 'filled'), ('remaining_base_amount', '7')])
def test_bad_or_mismatched_evidence_is_not_a_fill(field, value):
    with pytest.raises(UnverifiedExecution):
        verify_order(row(**{field:value}), intent())


def test_ack_and_absence_are_not_a_fill():
    for data in ({'code':200, 'tx_hash':'accepted'}, {}, None):
        with pytest.raises(UnverifiedExecution):
            verify_order(data, intent())


def test_zero_fill_cancellation_and_pending():
    r = row(filled_base_amount='0', filled_quote_amount='0', remaining_base_amount='10')
    assert verify_order(r, intent())['average_price'] is None
    r['status'] = 'pending'
    assert verify_order(r, intent())['terminal'] is False
    r['remaining_base_amount'] = '0'
    with pytest.raises(UnverifiedExecution):
        verify_order(r, intent())


def test_ready_full_reduce_only_stop_required():
    i = intent(reduce_only=True, order_type='stop-loss', trigger_price='2.2')
    r = row(reduce_only=True, type='stop-loss', trigger_price='2.2',
            filled_base_amount='0', filled_quote_amount='0', remaining_base_amount='10',
            status='open', trigger_status='ready')
    assert verify_stop(r, i, '10')['remaining_qty'] == '10'
    for changed in ({'status':'pending'}, {'trigger_status':'mark-price'},
                    {'reduce_only':False}, {'trigger_price':'2.3'}):
        with pytest.raises(UnverifiedExecution):
            verify_stop({**r, **changed}, i, '10')
    with pytest.raises(UnverifiedExecution):
        verify_stop(r, i, '11')


def test_restart_or_lost_reply_never_allows_duplicate_submission(tmp_path):
    p = tmp_path/'execution.sqlite'
    first = IntentStore(p, 'testnet', 100)
    first.reserve(intent())
    first.close()  # crash after reservation; no assumption whether order was sent
    second = IntentStore(p, 'testnet', 100)
    assert second.terminal_result(123) is None
    with pytest.raises(UnverifiedExecution):
        second.reserve(intent())
    second.record(123, row(status='open'))
    assert second.terminal_result(123) is None
    second.record(123, row())
    second.close()
    third = IntentStore(p, 'testnet', 100)
    assert third.terminal_result(123)['filled_qty'] == '4'
    third.record(123, row())  # idempotent observation
    for changed in ({'filled_base_amount':'3'}, {'status':'open'}, {'order_index':457}):
        with pytest.raises(UnverifiedExecution):
            third.record(123, row(**changed))
    third.close()


@pytest.mark.parametrize('changed', [None, {'trigger_status':'parent-order'},
    {'trigger_status':'ready'}, {'parent_order_index':7}, {'parent_order_index':None},
    {'trigger_time':1}, {'trigger_time':None}, {'trigger_time':False},
    {'status':'in-progress'}, {'remaining_base_amount':'9'}, {'reduce_only':False}])
def test_observed_standalone_pending_stop_requires_full_untriggered_protection(changed):
    i=intent(reduce_only=True,order_type='stop-loss',trigger_price='2.2')
    r=row(reduce_only=True,type='stop-loss',trigger_price='2.2',
        filled_base_amount='0',filled_quote_amount='0',remaining_base_amount='10',
        status='pending',trigger_status='mark-price',trigger_time=0,parent_order_index=0)
    if changed is None:
        assert verify_stop(r,i,'10')['remaining_qty']=='10'
    else:
        with pytest.raises(UnverifiedExecution):
            verify_stop({**r,**changed},i,'10')


def test_network_account_and_intent_identity_cannot_change(tmp_path):
    p = tmp_path/'execution.sqlite'
    store = IntentStore(p, 'testnet', 100)
    store.reserve(intent())
    with pytest.raises(UnverifiedExecution):
        store.reserve(intent(account_index=101, client_order_index=124))
    with pytest.raises(UnverifiedExecution):
        store.record(124, row(client_order_index=124))
    store.close()
    for network, account in [('mainnet',100), ('testnet',101)]:
        with pytest.raises(UnverifiedExecution):
            IntentStore(p, network, account)


def test_two_connections_cannot_reserve_same_order(tmp_path):
    p = tmp_path/'execution.sqlite'
    a, b = IntentStore(p, 'testnet', 100), IntentStore(p, 'testnet', 100)
    a.reserve(intent())
    with pytest.raises(UnverifiedExecution):
        b.reserve(intent())
    a.close()
    b.close()

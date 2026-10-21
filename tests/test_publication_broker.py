"""Owned detail publication lag is not arbitrary account discrepancy recovery."""
from decimal import Decimal

import pytest

from live.execution_evidence import NotSubmitted, UnverifiedExecution
from test_strategy_broker_uncertainty import broker, enter, missing


def hide_newest(x):
    return x.trades.pop()


def test_sustained_lag_is_explicit_bounded_and_blocks_new_entry(broker):
    x,b,m=broker
    enter(b,m)
    delayed=hide_newest(x)
    first=b.account_state()
    assert not first.execution_changes_verified
    assert first.ledger_report['publication_pending']
    assert first.ledger_report['reconciliation_read_attempts']==2
    assert first.ledger_report['risk_verified_symbols']==['BTC','ETH']
    evidence=first.ledger_report['publication_pending_evidence'][0]
    assert evidence['client_order_index']==10 and evidence['history_filled_qty']=='0'
    assert Decimal(evidence['order_filled_qty'])==1
    for _ in range(3):
        x.now+=61000
        state=b.account_state()
        assert state.ledger_report['publication_pending']
        assert state.ledger_report['reconciliation_read_attempts']==1
        with pytest.raises(NotSubmitted) as caught: enter(b,m,'BTCUSDT',20)
        assert caught.value.reason=='account_publication_pending'
        assert b.engine.store.load(20) is None and len(x.sent)==1
    x.trades.append(delayed)
    assert b.account_state().execution_changes_verified
    assert not b.last_report['publication_pending']


@pytest.mark.parametrize('change',['position','foreign','unknown','quote'])
def test_lag_with_hard_issue_never_becomes_auto_recoverable(broker,change):
    x,b,m=broker
    enter(b,m); delayed=hide_newest(x)
    if change=='position': x.position[0]=Decimal('.5')
    elif change=='foreign':
        x.trades.append({**delayed,'trade_id':999,'bid_client_id':999,'bid_id':9999})
    elif change=='unknown': missing(b,m,20,'BTCUSDT','ENTRY')
    else:
        # Full quantity with contradictory quote is not a missing subset.
        x.trades.append({**delayed,'price':'99','usd_amount':'99'})
    state=b.account_state()
    assert not state.execution_changes_verified and not state.ledger_report['publication_pending']
    if change!='unknown': assert 'ETH' not in state.ledger_report['risk_verified_symbols']


def test_unpublished_closed_trade_does_not_manufacture_zero_allocated_equity(broker):
    x,b,m=broker
    b.initial_allocated_equity=Decimal(300)
    enter(b,m); b.account_state()
    # Recorded entry, unrecorded owned close: history cash alone would be 200.
    b.market_order(m['ETHUSDT'],'ETHUSDT','LONG',1,True,12,100)
    hide_newest(x)
    state=b.account_state()
    assert state.positions=={} and state.ledger_report['publication_pending']
    assert state.equity==300
    assert state.ledger_report['pending_risk_cash_source'].startswith('verified_owned_order')
    assert not state.ledger_report['performance_complete']


def test_owned_tp_full_fill_can_protect_remaining_exposure_while_details_lag(broker):
    x,b,m=broker
    enter(b,m); b.account_state()
    b.place_take_profit(m['ETHUSDT'],'ETHUSDT','LONG',.5,110,11)
    x.fill(11,Decimal('.5')); delayed=hide_newest(x)
    state=b.account_state()
    assert state.ledger_report['publication_pending']
    assert state.positions['ETH']['partial_taken']
    assert state.positions['ETH']['qty']==.5
    x.trades.append(delayed)
    assert b.account_state().execution_changes_verified


def test_head_backlog_without_position_discrepancy_is_a_wait_not_complete(broker,monkeypatch):
    x,b,m=broker
    original=b.ledger.report
    def partial():
        return {**original(),'history_collection':{'head_complete':False,'pending_reason':'head_backlog'}}
    monkeypatch.setattr(b.ledger,'report',partial)
    state=b.account_state()
    assert not state.execution_changes_verified and state.ledger_report['publication_pending']
    assert state.ledger_report['publication_pending_evidence']==[{'type':'history_head_backlog','pending_reason':'head_backlog'}]
    assert state.ledger_report['risk_verified_symbols']==['BTC','ETH']

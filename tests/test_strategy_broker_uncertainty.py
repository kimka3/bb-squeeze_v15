"""Broker-only F0/F2 regressions with a synthetic venue; no network or keys."""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from data_io import START
from live.execution_evidence import NotSubmitted, UnverifiedExecution
from live.markets import Market
from live.request_budget import RequestDeferred
from live.strategy_broker import StrategyBroker
from test_strategy_integration import Venue


@pytest.fixture
def broker(tmp_path):
    venue=Venue()
    markets={s:Market(s[:-4],i,4,2,.0001,0,.06,.1,'active')
             for s,i in [('BTCUSDT',1),('ETHUSDT',0)]}
    b=StrategyBroker(venue,markets,tmp_path/'execution',start_ms=START,
                     clock=lambda:venue.now/1000,writes_enabled=True,timeout=0)
    try:
        yield venue,b,markets
    finally:
        b.close()


def enter(b,markets,symbol='ETHUSDT',coi=10):
    return b.market_order(markets[symbol],symbol,'LONG',1,False,coi,100)


def missing(b,markets,coi,symbol,role):
    b._bind(coi,symbol,'LONG',role)
    intent=b._intent(markets[symbol],'LONG',1,coi,closing=role!='ENTRY',
                     kind='stop-loss' if role=='SL' else 'market',price=94 if role=='SL' else 100,
                     trigger=94 if role=='SL' else 0)
    b.engine.store.reserve(intent)
    return intent


def reject_next(venue,monkeypatch,method='submit',retryable=True):
    original=getattr(venue,method)
    calls=[]
    def reject(*args):
        calls.append(args)
        if len(calls)==1:
            raise NotSubmitted('request_budget_deferred',retryable=retryable,
                               cause=RequestDeferred(1) if retryable else None)
        return original(*args)
    monkeypatch.setattr(venue,method,reject)
    return calls


def test_missing_entry_is_reported_without_certifying_its_market(broker):
    x,b,m=broker
    missing(b,m,10,'ETHUSDT','ENTRY')
    state=b.account_state()
    assert state.positions=={} and not state.execution_changes_verified
    assert state.ledger_report['verified_symbols']==['BTC']
    assert state.ledger_report['unresolved_orders']['10']=={
        'symbol':'ETHUSDT','role':'ENTRY','cycle':10,'reason':'owned_order_outcome_absent'}
    assert b.engine.store.load(10)[1] is None and x.sent==[]


@pytest.mark.parametrize('role',['ENTRY','EXIT'])
def test_unknown_market_operation_does_not_block_other_owned_market_exit(broker,role):
    x,b,m=broker
    enter(b,m)
    if role=='EXIT': enter(b,m,'BTCUSDT',20)
    missing(b,m,21,'BTCUSDT',role)
    state=b.account_state()
    assert state.ledger_report['verified_symbols']==['ETH']
    assert not state.execution_changes_verified
    fill=b.market_order(m['ETHUSDT'],'ETHUSDT','LONG',1,True,12,100)
    assert fill.qty==1 and x.position[0]==0
    assert x.sent[-1].reduce_only and x.sent[-1].is_ask
    assert b.engine.store.load(21)[1] is None


def test_missing_stop_keeps_verified_ownership_for_emergency_reduce_only(broker):
    x,b,m=broker
    enter(b,m)
    missing(b,m,11,'ETHUSDT','SL')
    state=b.account_state()
    assert not state.execution_changes_verified
    assert 'ETH' in state.ledger_report['verified_symbols']
    assert state.active_orders=={}  # Missing stop is never advertised as protection.
    fill=b.market_order(m['ETHUSDT'],'ETHUSDT','LONG',1,True,12,100)
    assert fill.qty==1 and x.position[0]==0
    assert len(x.sent)==2 and b.engine.store.load(11)[1] is None


def test_unknown_stop_does_not_authorize_manual_quantity_change(broker):
    x,b,m=broker
    enter(b,m)
    missing(b,m,11,'ETHUSDT','SL')
    x.position[0]=Decimal('.5')
    state=b.account_state()
    assert 'ETH' not in state.ledger_report['verified_symbols']
    with pytest.raises(NotSubmitted) as caught:
        b.market_order(m['ETHUSDT'],'ETHUSDT','LONG',.5,True,12,100)
    assert isinstance(caught.value.cause,UnverifiedExecution)
    assert len(x.sent)==1 and b.engine.store.load(12) is None


def test_unknown_entry_same_market_never_permits_close_from_position_alone(broker):
    x,b,m=broker
    missing(b,m,10,'ETHUSDT','ENTRY')
    x.position[0]=Decimal(1)
    with pytest.raises(NotSubmitted):
        b.market_order(m['ETHUSDT'],'ETHUSDT','LONG',1,True,12,100)
    assert x.sent==[]


def test_unknown_other_market_blocks_new_entry_globally(broker):
    x,b,m=broker
    missing(b,m,20,'BTCUSDT','ENTRY')
    with pytest.raises(NotSubmitted) as caught:
        enter(b,m)
    assert isinstance(caught.value.cause,UnverifiedExecution)
    assert x.sent==[] and b.engine.store.load(10) is None


@pytest.mark.parametrize('kind',['duplicate_active','wrong_identity','foreign_active','trade_side'])
def test_schema_identity_and_unowned_active_evidence_still_fail_hard(broker,monkeypatch,kind):
    x,b,m=broker
    enter(b,m)
    b.place_stop(m['ETHUSDT'],'ETHUSDT','LONG',1,94,11)
    if kind=='duplicate_active':
        original=x.active_orders
        monkeypatch.setattr(x,'active_orders',lambda:original()*2)
    elif kind=='wrong_identity': x.rows[11]['owner_account_index']=999
    elif kind=='foreign_active':
        x.rows[99]={**x.rows[11],'client_order_index':99,'order_index':1099}
    else:
        x.trades[0]['market_id']=1
    with pytest.raises(UnverifiedExecution): b.account_state()


def test_positive_non_submission_is_not_a_synthetic_zero_fill(broker,monkeypatch):
    x,b,m=broker
    reject_next(x,monkeypatch)
    with pytest.raises(NotSubmitted): enter(b,m)
    assert b.engine.store.known_not_submitted(10)
    with pytest.raises(NotSubmitted): b.market_order_result(10)
    state=b.account_state()
    assert state.ledger_report['unresolved_orders']=={}
    assert state.ledger_report['not_submitted_orders']['10']['retryable'] is True
    assert state.execution_changes_verified
    assert state.ledger_report['fill_count']==0
    assert b.engine.store.load(10)[1] is None and x.sent==[]


def test_entry_non_submission_does_not_implicitly_retry_same_id(broker,monkeypatch):
    x,b,m=broker
    calls=reject_next(x,monkeypatch)
    with pytest.raises(NotSubmitted): enter(b,m)
    with pytest.raises(NotSubmitted): enter(b,m)
    assert len(calls)==1 and x.sent==[]


@pytest.mark.parametrize('method',['place_stop','place_take_profit'])
def test_protection_known_unsent_retries_same_id_after_fresh_owned_reconciliation(broker,monkeypatch,method):
    x,b,m=broker
    enter(b,m)
    calls=reject_next(x,monkeypatch)
    place=getattr(b,method)
    trigger=94 if method=='place_stop' else 110
    with pytest.raises(NotSubmitted): place(m['ETHUSDT'],'ETHUSDT','LONG',1,trigger,11)
    state=b.account_state()
    assert state.ledger_report['not_submitted_orders']['11']['cycle']==10
    assert not state.execution_changes_verified and 'ETH' in state.ledger_report['verified_symbols']
    ref=place(m['ETHUSDT'],'ETHUSDT','LONG',1,trigger,11)
    assert ref.client_order_index==11 and len(calls)==2
    assert len(x.sent)==2 and x.sent[-1].reduce_only
    assert b.account_state().execution_changes_verified


def test_protection_nonretryable_pre_dispatch_failure_is_never_retried(broker,monkeypatch):
    x,b,m=broker
    enter(b,m)
    calls=reject_next(x,monkeypatch,retryable=False)
    with pytest.raises(NotSubmitted): b.place_stop(m['ETHUSDT'],'ETHUSDT','LONG',1,94,11)
    with pytest.raises(NotSubmitted): b.place_stop(m['ETHUSDT'],'ETHUSDT','LONG',1,94,11)
    assert len(calls)==1 and len(x.sent)==1


def test_protection_retry_refuses_changed_owned_quantity(broker,monkeypatch):
    x,b,m=broker
    enter(b,m)
    calls=reject_next(x,monkeypatch)
    with pytest.raises(NotSubmitted): b.place_stop(m['ETHUSDT'],'ETHUSDT','LONG',1,94,11)
    x.position[0]=Decimal('.5')
    with pytest.raises(NotSubmitted): b.place_stop(m['ETHUSDT'],'ETHUSDT','LONG',1,94,11)
    assert len(calls)==1 and len(x.sent)==1


def test_unknown_protection_is_queried_not_resubmitted(broker):
    x,b,m=broker
    enter(b,m)
    missing(b,m,11,'ETHUSDT','SL')
    with pytest.raises(UnverifiedExecution): b.place_stop(m['ETHUSDT'],'ETHUSDT','LONG',1,94,11)
    assert len(x.sent)==1


def test_owned_close_known_unsent_retries_without_new_id(broker,monkeypatch):
    x,b,m=broker
    enter(b,m)
    calls=reject_next(x,monkeypatch)
    with pytest.raises(NotSubmitted):
        b.market_order(m['ETHUSDT'],'ETHUSDT','LONG',1,True,12,100)
    fill=b.market_order(m['ETHUSDT'],'ETHUSDT','LONG',1,True,12,100)
    assert fill.qty==1 and len(calls)==2 and x.position[0]==0
    assert [i.client_order_index for i in x.sent]==[10,12]


def test_existing_unknown_entry_is_not_reclassified_when_guard_pauses(broker):
    x,b,m=broker
    missing(b,m,10,'ETHUSDT','ENTRY')
    b.entry_guard=lambda:False
    with pytest.raises(UnverifiedExecution) as caught: enter(b,m)
    assert not isinstance(caught.value,NotSubmitted)
    assert x.sent==[] and not b.engine.store.known_not_submitted(10)


@pytest.mark.parametrize('condition,reason',[('guard','new_entries_paused'),('deadline','entry_deadline_elapsed')])
def test_pre_submission_control_is_typed_without_reserving_intent(broker,condition,reason):
    x,b,m=broker
    if condition=='guard': b.entry_guard=lambda:False
    else: b.entry_deadline_ms=x.now
    with pytest.raises(NotSubmitted) as caught: enter(b,m)
    assert caught.value.reason==reason and caught.value.cause is None
    assert b.engine.store.load(10) is None and x.sent==[]


def test_pre_reservation_read_failure_keeps_its_original_cause(broker,monkeypatch):
    x,b,m=broker
    error=RequestDeferred(3)
    monkeypatch.setattr(x,'order',lambda coi: (_ for _ in ()).throw(error))
    with pytest.raises(NotSubmitted) as caught: enter(b,m)
    assert caught.value.cause is error and caught.value.retryable
    assert b.engine.store.load(10) is None and x.sent==[]


def test_uncertain_binding_collision_is_not_softened(broker):
    x,b,m=broker
    missing(b,m,10,'ETHUSDT','ENTRY')
    with pytest.raises(UnverifiedExecution) as caught:
        b.market_order(m['BTCUSDT'],'BTCUSDT','LONG',1,False,10,100)
    assert not isinstance(caught.value,NotSubmitted) and x.sent==[]


def test_cancel_known_unsent_retries_owned_order_only(broker,monkeypatch):
    x,b,m=broker
    enter(b,m)
    ref=b.place_stop(m['ETHUSDT'],'ETHUSDT','LONG',1,94,11)
    calls=reject_next(x,monkeypatch,'cancel')
    with pytest.raises(NotSubmitted): b.cancel(m['ETHUSDT'],ref)
    b.cancel(m['ETHUSDT'],ref)
    assert len(calls)==2 and x.rows[11]['status']=='canceled'


def test_stop_amend_known_unsent_retries_after_fresh_ownership(broker,monkeypatch):
    x,b,m=broker
    enter(b,m)
    ref=b.place_stop(m['ETHUSDT'],'ETHUSDT','LONG',1,94,11)
    calls=reject_next(x,monkeypatch,'amend')
    with pytest.raises(NotSubmitted): b.modify_stop(m['ETHUSDT'],ref,1,96)
    assert Decimal(x.rows[11]['trigger_price'])==94
    b.modify_stop(m['ETHUSDT'],ref,1,96)
    assert len(calls)==2 and Decimal(x.rows[11]['trigger_price'])==96


def test_uncertain_amendment_old_visible_stop_does_not_block_other_exit(broker,monkeypatch):
    x,b,m=broker
    enter(b,m)
    ref=b.place_stop(m['ETHUSDT'],'ETHUSDT','LONG',1,94,11)
    enter(b,m,'BTCUSDT',20)
    monkeypatch.setattr(x,'amend',lambda *args: (_ for _ in ()).throw(RuntimeError('unknown')))
    with pytest.raises(UnverifiedExecution): b.modify_stop(m['ETHUSDT'],ref,1,96)
    state=b.account_state()
    assert state.ledger_report['unresolved_orders']['11']['reason']=='stop_amendment_unconfirmed'
    assert 'BTC' in state.ledger_report['verified_symbols']
    b.market_order(m['BTCUSDT'],'BTCUSDT','LONG',1,True,21,100)
    assert x.position[1]==0 and x.rows[11]['trigger_price']=='94.00'


def test_missing_protection_with_new_unconfirmed_fill_cannot_certify_position(broker):
    x,b,m=broker
    enter(b,m)
    ref=b.place_stop(m['ETHUSDT'],'ETHUSDT','LONG',1,94,11)
    x.fill(11,Decimal('.5'))
    x.rows.pop(11)
    state=b.account_state()
    assert 'ETH' not in state.ledger_report['verified_symbols']
    assert not state.execution_changes_verified


@pytest.mark.parametrize('offset,held',[(-1,set()),(0,{'ETHUSDT'}),(1,{'ETHUSDT'}),
                                       (999,{'ETHUSDT'}),(1000,{'ETHUSDT'}),(1001,set())])
def test_boundary_owned_exposure_uses_fill_time_and_treats_equal_time_conservatively(broker,monkeypatch,offset,held):
    x,b,m=broker
    stamp=x.now
    enter(b,m)
    x.now+=1000
    b.market_order(m['ETHUSDT'],'ETHUSDT','LONG',1,True,12,100)
    b.account_state()
    def forbidden(*args,**kwargs): raise AssertionError('boundary lookup must use only saved ledger')
    for name in ('get','order','account','active_orders'):
        monkeypatch.setattr(x,name,forbidden)
    assert b.held_symbols_at(stamp+offset)==held


def test_boundary_short_partial_close_remains_held_until_fully_closed(broker):
    x,b,m=broker
    stamp=x.now
    b.market_order(m['ETHUSDT'],'ETHUSDT','SHORT',1,False,10,100)
    x.now+=1000
    b.market_order(m['ETHUSDT'],'ETHUSDT','SHORT',.5,True,12,100)
    b.account_state()
    assert b.held_symbols_at(stamp+1001)=={'ETHUSDT'}


def test_boundary_unowned_trade_and_wrong_market_remain_hard_failures(broker):
    x,b,m=broker
    enter(b,m)
    b.account_state()
    original=b.ledger.events
    rows=original('trade')
    b.ledger.events=lambda kind: [{**rows[0],'coi':999}]
    with pytest.raises(UnverifiedExecution,match='owned binding'): b.held_symbols_at(x.now+1)
    b.ledger.events=lambda kind: [{**rows[0],'market_id':1}]
    with pytest.raises(UnverifiedExecution,match='conflicts'): b.held_symbols_at(x.now+1)
    b.ledger.events=original


def test_confirmed_unsent_protection_retry_survives_broker_restart(tmp_path,monkeypatch):
    x=Venue()
    markets={'ETHUSDT':Market('ETH',0,4,2,.0001,0,.06,.1,'active')}
    def make():
        return StrategyBroker(x,markets,tmp_path/'execution',start_ms=START,clock=lambda:x.now/1000,
                              writes_enabled=True,timeout=0)
    b=make()
    try:
        enter(b,markets)
        calls=reject_next(x,monkeypatch)
        with pytest.raises(NotSubmitted): b.place_stop(markets['ETHUSDT'],'ETHUSDT','LONG',1,94,11)
    finally: b.close()
    b=make()
    try:
        assert b.account_state().ledger_report['not_submitted_orders']['11']['retryable']
        b.place_stop(markets['ETHUSDT'],'ETHUSDT','LONG',1,94,11)
        assert len(calls)==2 and [i.client_order_index for i in x.sent]==[10,11]
    finally: b.close()


def test_unknown_cancel_is_not_retransmitted_even_with_fresh_visible_order(broker,monkeypatch):
    x,b,m=broker
    enter(b,m)
    ref=b.place_stop(m['ETHUSDT'],'ETHUSDT','LONG',1,94,11)
    calls=[]
    def uncertain(*args):
        calls.append(args)
        raise RuntimeError('outcome unknown')
    monkeypatch.setattr(x,'cancel',uncertain)
    with pytest.raises(UnverifiedExecution): b.cancel(m['ETHUSDT'],ref)
    state=b.account_state()
    assert state.ledger_report['unresolved_orders']['11']['reason']=='cancel_outcome_unconfirmed'
    with pytest.raises(UnverifiedExecution): b.cancel(m['ETHUSDT'],ref)
    assert len(calls)==1 and x.rows[11]['status']=='pending'


def test_partial_entry_cancel_not_sent_never_becomes_entry_not_sent(broker,monkeypatch):
    x,b,m=broker
    original=x.fill
    monkeypatch.setattr(x,'fill',lambda coi,qty:original(coi,Decimal(qty)/2))
    with pytest.raises(UnverifiedExecution): enter(b,m)
    reject_next(x,monkeypatch,'cancel')
    with pytest.raises(UnverifiedExecution,match='remainder cancellation') as caught:
        b.market_order_result(10)
    assert not isinstance(caught.value,NotSubmitted)
    assert isinstance(caught.value.__cause__,NotSubmitted)
    assert not b.engine.store.known_not_submitted(10)
    assert b.engine.store.load(10)[1]['filled_qty']=='0.5000'
    assert x.position[0]==Decimal('.5') and len(x.sent)==1


def test_read_only_broker_cannot_cancel_partial_entry_during_result_lookup(broker,monkeypatch):
    x,b,m=broker
    original=x.fill
    monkeypatch.setattr(x,'fill',lambda coi,qty:original(coi,Decimal(qty)/2))
    with pytest.raises(UnverifiedExecution): enter(b,m)
    b.writes_enabled=False
    monkeypatch.setattr(x,'cancel',lambda *args:pytest.fail('read-only broker canceled an order'))
    with pytest.raises(RuntimeError,match='writes disabled'): b.market_order_result(10)
    assert x.position[0]==Decimal('.5') and x.rows[10]['status']=='open'


def test_mid_read_owned_stop_fill_gets_one_fresh_consistent_reconciliation(broker,monkeypatch):
    x,b,m=broker
    enter(b,m)
    b.place_stop(m['ETHUSDT'],'ETHUSDT','LONG',1,94,11)
    original=x.account; calls=[]
    def race():
        calls.append(True)
        if len(calls)==1:
            x.now+=1
            x.fill(11,Decimal(1))
        return original()
    monkeypatch.setattr(x,'account',race)
    state=b.account_state()
    assert state.execution_changes_verified and state.positions=={}
    assert state.ledger_report['reconciliation_read_attempts']==2 and len(calls)==2
    assert state.ledger_report['fill_count']==2 and len(x.sent)==2


def test_one_cycle_trade_publication_lag_gets_one_fresh_read(broker,monkeypatch):
    x,b,m=broker
    enter(b,m)
    original=x.get; pages=[]
    def late(path,**kwargs):
        if path.startswith('/api/v1/trades?'):
            pages.append(path)
            if len(pages)==1: return {'trades':[],'next_cursor':''}
        return original(path,**kwargs)
    monkeypatch.setattr(x,'get',late)
    state=b.account_state()
    assert state.execution_changes_verified
    assert state.ledger_report['reconciliation_read_attempts']==2 and len(pages)==2


def test_persistent_quantity_mismatch_stops_after_two_reads_and_stays_unverified(broker,monkeypatch):
    x,b,m=broker
    enter(b,m)
    x.position[0]=Decimal('.5')
    original=x.account; calls=[]
    def count():
        calls.append(True)
        return original()
    monkeypatch.setattr(x,'account',count)
    state=b.account_state()
    assert not state.execution_changes_verified and 'ETH' not in state.ledger_report['verified_symbols']
    assert state.ledger_report['reconciliation_read_attempts']==2 and len(calls)==2


def test_unowned_trade_does_not_gain_a_retry_or_ownership_certification(broker,monkeypatch):
    x,b,m=broker
    enter(b,m)
    x.trades.append({**x.trades[0],'trade_id':999,'bid_client_id':999,'bid_id':9999})
    original=x.account; calls=[]
    def count():
        calls.append(True)
        return original()
    monkeypatch.setattr(x,'account',count)
    state=b.account_state()
    assert not state.execution_changes_verified and 'ETH' not in state.ledger_report['verified_symbols']
    assert state.ledger_report['reconciliation_read_attempts']==1 and len(calls)==1


def test_unknown_order_excludes_ownership_mismatch_retry(broker,monkeypatch):
    x,b,m=broker
    enter(b,m)
    missing(b,m,20,'BTCUSDT','ENTRY')
    x.position[0]=Decimal('.5')
    original=x.account; calls=[]
    def count():
        calls.append(True)
        return original()
    monkeypatch.setattr(x,'account',count)
    state=b.account_state()
    assert not state.execution_changes_verified
    assert state.ledger_report['reconciliation_read_attempts']==1 and len(calls)==1


def test_second_reconciliation_budget_failure_is_not_swallowed_as_normal(broker,monkeypatch):
    x,b,m=broker
    enter(b,m)
    x.position[0]=Decimal('.5')
    original=x.get; pages=[]; error=RequestDeferred(5)
    def cooldown(path,**kwargs):
        if path.startswith('/api/v1/trades?'):
            pages.append(path)
            if len(pages)==2: raise error
        return original(path,**kwargs)
    monkeypatch.setattr(x,'get',cooldown)
    with pytest.raises(RequestDeferred) as caught: b.account_state()
    assert caught.value is error and len(pages)==2
    assert b.last_report['execution_changes_verified'] is False
    assert b.last_report['reconciliation_read_attempts']==1


def test_partial_stop_is_visible_but_not_armed_and_allows_verified_residual_close(broker):
    x,b,m=broker
    enter(b,m)
    enter(b,m,'BTCUSDT',20)
    ref=b.place_stop(m['ETHUSDT'],'ETHUSDT','LONG',1,94,11)
    x.fill(11,Decimal('.4'))
    state=b.account_state()
    assert not state.execution_changes_verified
    assert state.ledger_report['verified_symbols']==['BTC','ETH']
    unsafe=state.ledger_report['unsafe_protection']['11']
    assert unsafe['symbol']=='ETHUSDT' and unsafe['role']=='SL'
    assert unsafe['reason']=='stop_partially_filled' and unsafe['remaining_qty']==.6
    active=state.active_orders['ETH'][0]
    assert not active['armed'] and active['order_index']==1011 and active['position_side']=='LONG'
    # The exact original stop remains available for orphan-order cancellation.
    assert active['client_order_index']==ref.client_order_index
    b.market_order(m['BTCUSDT'],'BTCUSDT','LONG',1,True,21,100)
    fill=b.market_order(m['ETHUSDT'],'ETHUSDT','LONG',.6,True,12,100)
    assert fill.qty==.6 and x.position[0]==0 and x.position[1]==0
    b.cancel(m['ETHUSDT'],ref)
    assert x.rows[11]['status']=='canceled'


def test_partial_stop_with_owned_order_account_proof_can_reduce_despite_detail_lag(broker):
    x,b,m=broker
    enter(b,m)
    b.place_stop(m['ETHUSDT'],'ETHUSDT','LONG',1,94,11)
    x.fill(11,Decimal('.4'))
    x.trades.pop()
    state=b.account_state()
    assert 'ETH' not in state.ledger_report['verified_symbols']
    assert state.ledger_report['unsafe_protection']['11']['reason']=='stop_partially_filled'
    assert 'ETH' in state.ledger_report['risk_verified_symbols']
    assert not state.ledger_report['publication_pending']  # unsafe stop still requires safety handling
    fill=b.market_order(m['ETHUSDT'],'ETHUSDT','LONG',.6,True,12,100)
    assert fill.qty==.6 and x.position[0]==0 and x.sent[-1].reduce_only


def test_valid_parent_dependent_stop_is_unsafe_not_a_global_read_exception(broker):
    x,b,m=broker
    enter(b,m)
    b.place_stop(m['ETHUSDT'],'ETHUSDT','LONG',1,94,11)
    x.rows[11].update(trigger_status='parent-order',parent_order_index=99)
    state=b.account_state()
    assert 'ETH' in state.ledger_report['verified_symbols']
    assert state.ledger_report['unsafe_protection']['11']['reason']=='stop_not_confirmed_armed_for_residual'
    assert not state.active_orders['ETH'][0]['armed']


@pytest.mark.parametrize('changed',[
    {'trigger_status':None},{'trigger_status':'unrecognized'},
    {'trigger_time':True},{'trigger_time':-1},{'parent_order_index':None},
    {'owner_account_index':999},{'trigger_price':'95'},
])
def test_malformed_stop_evidence_still_fails_hard(broker,changed):
    x,b,m=broker
    enter(b,m)
    b.place_stop(m['ETHUSDT'],'ETHUSDT','LONG',1,94,11)
    x.rows[11].update(changed)
    with pytest.raises(UnverifiedExecution): b.account_state()


def test_protection_expiry_and_identity_are_carried_to_active_refs(broker):
    x,b,m=broker
    enter(b,m)
    b.place_stop(m['ETHUSDT'],'ETHUSDT','LONG',1,94,11)
    expiry=x.now+86400000
    x.rows[11]['order_expiry']=expiry
    ref=b.place_stop(m['ETHUSDT'],'ETHUSDT','LONG',1,94,11)
    assert ref.order_expiry_ms==expiry
    active=b.account_state().active_orders['ETH'][0]
    assert active['order_expiry_ms']==expiry and active['order_index']==1011
    assert active['position_side']=='LONG' and active['armed']


@pytest.mark.parametrize('retryable',[True,False])
def test_partial_entry_retries_only_confirmed_retryable_unsent_remainder_cancel(broker,monkeypatch,retryable):
    x,b,m=broker
    original=x.fill
    monkeypatch.setattr(x,'fill',lambda coi,qty:original(coi,Decimal(qty)/2))
    with pytest.raises(UnverifiedExecution): enter(b,m)
    calls=reject_next(x,monkeypatch,'cancel',retryable=retryable)
    with pytest.raises(UnverifiedExecution,match='remainder cancellation'):
        b.market_order_result(10)
    assert b.engine.mutation_submission_details('cancel:10')['state']=='not_submitted'
    if retryable:
        fill=b.market_order_result(10)
        assert fill.qty==.5 and x.rows[10]['status']=='canceled'
        ref=b.place_stop(m['ETHUSDT'],'ETHUSDT','LONG',fill.qty,94,11)
        assert ref.qty==.5 and len(calls)==2
        assert [i.client_order_index for i in x.sent]==[10,11]
    else:
        with pytest.raises(UnverifiedExecution,match='remainder cancellation'):
            b.market_order_result(10)
        assert len(calls)==1 and len(x.sent)==1 and x.rows[10]['status']=='open'
    assert not b.engine.store.known_not_submitted(10)


def test_partial_entry_unknown_remainder_cancel_is_never_retransmitted(broker,monkeypatch):
    x,b,m=broker
    original=x.fill
    monkeypatch.setattr(x,'fill',lambda coi,qty:original(coi,Decimal(qty)/2))
    with pytest.raises(UnverifiedExecution): enter(b,m)
    calls=[]
    def unknown(*args):
        calls.append(args)
        raise RuntimeError('unknown cancel outcome')
    monkeypatch.setattr(x,'cancel',unknown)
    for attempt in range(2):
        with pytest.raises(UnverifiedExecution): b.market_order_result(10)
    assert len(calls)==1 and len(x.sent)==1 and x.position[0]==Decimal('.5')


@pytest.mark.parametrize('accepted',[False,True])
def test_partial_stop_during_unconfirmed_amend_keeps_other_market_manageable(broker,monkeypatch,accepted):
    x,b,m=broker
    enter(b,m)
    enter(b,m,'BTCUSDT',20)
    ref=b.place_stop(m['ETHUSDT'],'ETHUSDT','LONG',1,94,11)
    original=x.amend; calls=[]
    def uncertain(*args):
        calls.append(args)
        if accepted: original(*args)
        x.fill(11,Decimal('.4'))
        raise RuntimeError('amend reply absent')
    monkeypatch.setattr(x,'amend',uncertain)
    with pytest.raises(UnverifiedExecution): b.modify_stop(m['ETHUSDT'],ref,1,96)
    state=b.account_state()
    assert not state.execution_changes_verified
    assert state.ledger_report['verified_symbols']==['BTC','ETH']
    assert state.ledger_report['unresolved_orders']['11']['reason']=='stop_amendment_unconfirmed'
    assert state.ledger_report['unsafe_protection']['11']['reason']=='stop_partially_filled'
    assert not state.active_orders['ETH'][0]['armed']
    assert float(b.engine.store.load(11)[0].trigger_price)==(96 if accepted else 94)
    assert b.engine.store.db.execute('SELECT complete FROM mutations WHERE id LIKE ?',('amend:11:%',)).fetchone()[0]==0
    b.market_order(m['BTCUSDT'],'BTCUSDT','LONG',1,True,21,100)
    b.market_order(m['ETHUSDT'],'ETHUSDT','LONG',.6,True,12,100)
    assert x.position[0]==0 and x.position[1]==0 and len(calls)==1


@pytest.mark.parametrize('changed',[
    {'trigger_status':None},{'trigger_status':'unknown'},
    {'trigger_time':None},{'parent_order_index':True},
    {'owner_account_index':999},{'order_index':9999},
])
def test_unconfirmed_amend_malformed_stop_still_raises(broker,monkeypatch,changed):
    x,b,m=broker
    enter(b,m)
    ref=b.place_stop(m['ETHUSDT'],'ETHUSDT','LONG',1,94,11)
    monkeypatch.setattr(x,'amend',lambda *args:None)  # acknowledgement never changes observed terms
    with pytest.raises(UnverifiedExecution): b.modify_stop(m['ETHUSDT'],ref,1,96)
    x.rows[11].update(changed)
    with pytest.raises(UnverifiedExecution): b.account_state()

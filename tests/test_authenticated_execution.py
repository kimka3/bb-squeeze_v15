import json
from pathlib import Path
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from live.execution import ExecutionEngine, positions, scaled
from live.execution_evidence import Intent, UnverifiedExecution
from live.live_check import minimum_plan, perform, cleanup

DETAIL = {'symbol':'ETH','market_type':'perp','market_id':0,'status':'active',
    'mark_price':'2500','supported_size_decimals':4,'supported_price_decimals':2,
    'min_base_amount':'0.002','min_quote_amount':'10'}


class Exchange:
    network='testnet'
    account_index=100
    def __init__(self):
        self.rows={}
        self.qty=Decimal(0)
        self.sent=[]
        self.lost_reply=False
        self.reject_stop=False
        self.partial_close=False
        self.hide_order=False
        self.cancel_calls=0

    def account(self):
        return {'positions':[] if not self.qty else [{'market_id':0,'position':str(self.qty),'sign':1}],
                'available_balance':'300','total_asset_value':'300'}

    def active_orders(self):
        return [r for r in self.rows.values() if r['status']=='open']

    def get(self, path, private=False):
        return {'order_book_details':[DETAIL]}

    def order(self, coi):
        return None if self.hide_order else self.rows.get(coi)

    def submit(self, intent, base, price, trigger):
        self.sent.append(intent)
        qty=Decimal(intent.quantity)
        fill=qty if intent.order_type=='market' else Decimal(0)
        if self.partial_close and intent.reduce_only and fill>Decimal('0.0001'):
            fill-=Decimal('0.0001')
        if intent.order_type=='market':
            self.qty+=-fill if intent.is_ask else fill
        status='filled' if fill==qty else ('canceled' if intent.order_type=='market' else 'open')
        if intent.order_type=='stop-loss' and self.reject_stop:
            status='canceled'
        self.rows[intent.client_order_index]={
            'owner_account_index':100,'market_index':0,'client_order_index':intent.client_order_index,
            'order_index':intent.client_order_index+1000,'is_ask':intent.is_ask,
            'reduce_only':intent.reduce_only,'type':intent.order_type,'time_in_force':intent.time_in_force,
            'price':intent.price,'trigger_price':intent.trigger_price,'initial_base_amount':intent.quantity,
            'filled_base_amount':str(fill),'filled_quote_amount':str(fill*2500),
            'remaining_base_amount':str(qty-fill),'status':status,'trigger_status':'ready'}
        if self.lost_reply:
            raise RuntimeError('simulated response lost after acceptance')

    def cancel(self, market, order):
        self.cancel_calls+=1
        r=next(r for r in self.rows.values() if r['order_index']==order)
        r['status']='canceled'
        if self.lost_reply:
            raise RuntimeError('cancel response lost')

    def amend(self, market, order, base, price, trigger):
        r=next(r for r in self.rows.values() if r['order_index']==order)
        r.update(initial_base_amount=str(Decimal(base)/10000), remaining_base_amount=str(Decimal(base)/10000),
                 price=str(Decimal(price)/100),trigger_price=str(Decimal(trigger)/100))


def buy(coi=1):
    return Intent(100,0,coi,False,False,'0.0041','market','immediate-or-cancel','2512.50')


def test_minimum_sizing_cap_and_tick():
    plan=minimum_plan(DETAIL)
    assert plan['quantity']=='0.0041'
    assert Decimal(plan['max_entry_notional'])<=15
    assert scaled(plan['quantity'],4)==41
    with pytest.raises(ValueError):
        scaled('0.00411',4)
    with pytest.raises(RuntimeError):
        minimum_plan({**DETAIL,'min_quote_amount':'20'})
    with pytest.raises(UnverifiedExecution):
        minimum_plan({**DETAIL,'mark_price':'NaN'})


def test_lost_ack_recovery_and_restart_do_not_resubmit(tmp_path):
    x=Exchange(); x.lost_reply=True
    e=ExecutionEngine(x,tmp_path/'check',timeout=0)
    assert e.create(buy(),4,2)['filled_qty']=='0.0041'
    e.close()
    e=ExecutionEngine(x,tmp_path/'check',timeout=0)
    assert e.create(buy(),4,2)['terminal']
    assert len(x.sent)==1
    e.close()


def test_missing_order_keeps_intent_unresolved_across_restarts(tmp_path):
    x=Exchange(); x.hide_order=True
    e=ExecutionEngine(x,tmp_path/'check',timeout=0)
    with pytest.raises(UnverifiedExecution):
        e.create(buy(),4,2)
    e.close()
    e=ExecutionEngine(x,tmp_path/'check',timeout=0)
    with pytest.raises(UnverifiedExecution):
        e.create(buy(),4,2)
    assert len(x.sent)==1
    x.hide_order=False
    assert e.recover(1)['filled_qty']=='0.0041'
    e.close()


@pytest.mark.parametrize('lost,partial,reject_stop',[(False,False,False),(True,False,False),
    (False,True,False),(False,False,True)])
def test_check_flat_cleanup_even_after_stop_failure(tmp_path,lost,partial,reject_stop):
    x=Exchange(); x.lost_reply=lost; x.partial_close=partial; x.reject_stop=reject_stop
    e=ExecutionEngine(x,tmp_path/'check',timeout=0)
    path=tmp_path/'check'/'session.json'
    result=perform(x,e,minimum_plan(DETAIL),path)
    assert x.qty==0 and x.active_orders()==[]
    assert result['cleanup']=='confirmed_flat_no_active_orders'
    assert result['execution_check_passed'] is not reject_stop
    assert sum(not i.reduce_only for i in x.sent)==1
    assert all(i.reduce_only for i in x.sent[1:])
    prior=len(x.sent)
    cleanup(x,e,result,path)
    assert len(x.sent)==prior
    with pytest.raises(RuntimeError):
        perform(x,e,minimum_plan(DETAIL),path)
    e.close()


def test_stop_amend_verified_and_no_loosening(tmp_path):
    x=Exchange(); e=ExecutionEngine(x,tmp_path/'check',timeout=0)
    stop=Intent(100,0,2,True,True,'0.0041','stop-loss','immediate-or-cancel','2462.62','2475')
    e.create(stop,4,2,stop_quantity='0.0041')
    tighter=Intent(100,0,2,True,True,'0.0041','stop-loss','immediate-or-cancel','2472.57','2485')
    assert e.amend_stop(2,tighter,4,2)['status']=='open'
    assert e.store.load(2)[0]==tighter
    with pytest.raises(UnverifiedExecution):
        e.amend_stop(2,stop,4,2)
    e.cancel(2)
    e.cancel(2)
    assert x.cancel_calls==1
    e.close()


def test_preview_does_not_authorize_more_than_cap_or_existing_position(tmp_path):
    x=Exchange(); x.qty=Decimal('1')
    e=ExecutionEngine(x,tmp_path/'check',timeout=0)
    with pytest.raises(RuntimeError):
        perform(x,e,minimum_plan(DETAIL),tmp_path/'check'/'session.json')
    assert x.sent==[]
    e.close()


def test_account_missing_positions_not_treated_as_flat():
    with pytest.raises(UnverifiedExecution):
        positions({})


def test_unknown_remote_id_refuses_new_submission(tmp_path):
    x=Exchange(); x.submit(buy(),41,251250,0)
    e=ExecutionEngine(x,tmp_path/'check',timeout=0)
    with pytest.raises(UnverifiedExecution):
        e.create(buy(),4,2)
    assert len(x.sent)==1
    e.close()


def test_transport_explicit_key_always_supplies_server_nonce():
    from live.authenticated import LighterTransport
    class Signer:
        ORDER_TYPE_MARKET=1
        ORDER_TYPE_LIMIT=0
        ORDER_TYPE_STOP_LOSS=2
        ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL=0
        ORDER_TIME_IN_FORCE_POST_ONLY=2
        ORDER_TIME_IN_FORCE_GOOD_TILL_TIME=1
        DEFAULT_IOC_EXPIRY=0
        DEFAULT_28_DAY_ORDER_EXPIRY=-1
        def create_order(self, **kwargs): return kwargs
        def cancel_order(self, **kwargs): return kwargs
        def modify_order(self, **kwargs): return kwargs
    t=LighterTransport.__new__(LighterTransport)
    t.account_index=100; t.api_key_index=4; t.client=Signer()
    t.get=lambda path:{'nonce':42}
    calls=[]; t._send=calls.append
    t.submit(buy(),41,251250,0)
    t.cancel(0,1001)
    t.amend(0,1001,41,249000,250000)
    assert all(c['nonce']==42 and c['api_key_index']==4 for c in calls)
    assert calls[0]['order_expiry']==0
    t.get=lambda path:{'nonce':True}
    with pytest.raises(RuntimeError):
        t.submit(buy(),41,251250,0)
    assert len(calls)==3

"""Fault injection exercises persisted production adapters, never a real venue."""
from collections import deque
from decimal import Decimal
import urllib.error
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
from test_strategy_integration import Venue, setup, rows, START
from live.request_budget import RequestBudget, RequestDeferred
from live.authenticated import LighterTransport


class Crash(BaseException): pass


@pytest.mark.parametrize('start,end',[(1500,1600),(1000,1000),(1000,2000)])
def test_funding_subsecond_and_exact_boundaries_are_padded_then_filtered(start,end):
    from urllib.parse import parse_qs,urlsplit
    from live.account_ledger import history
    class Read:
        account_index=100
        def get(self,path):
            q=parse_qs(urlsplit(path).query)
            assert int(q['start_timestamp'][0])<int(q['end_timestamp'][0])
            # Simulate strict server bounds, including both requested exact
            # boundary events, and verify no future/out-of-scope row escapes.
            return {'position_fundings':[{'timestamp':3},{'timestamp':2},{'timestamp':1},{'timestamp':0}]}
    assert history(Read(),'funding',start,end)==[{'timestamp':n} for n in (3,2,1,0) if start<=n*1000<=end]


@pytest.mark.parametrize('kind',['market','stop-loss','limit'])
def test_restart_after_venue_acceptance_before_local_ack(tmp_path,kind):
    x=Venue(); original=x.submit
    def interrupt(intent,*args):
        original(intent,*args)
        if intent.order_type==kind: raise Crash()
    x.submit=interrupt
    x,b,t=setup(tmp_path,x)
    try:
        with pytest.raises(Crash): t.on_bar(START,rows(),{'BTCUSDT':100,'ETHUSDT':100},now_ms=x.now)
    finally: b.close()
    before=len(x.sent); x.submit=original
    x,b,t=setup(tmp_path,x)
    try:
        b.poll()  # restart must acquire a fresh mark before recovering protection
        t.service(now_ms=x.now,marks={'ETHUSDT':100})
        assert not t.pending_operation and not t.halted
        assert sum(i.order_type=='market' for i in x.sent)==1
        assert sum(i.order_type=='stop-loss' for i in x.sent)==1
        assert sum(i.order_type=='limit' for i in x.sent)==1
        assert t.orders['ETHUSDT']['SL'] and b.account_state().execution_changes_verified
        again=len(x.sent); t.service(now_ms=x.now,marks={'ETHUSDT':100})
        assert len(x.sent)==again and before<=again
    finally: b.close()


def test_restart_after_amend_acceptance_before_sqlite_commit(tmp_path):
    x,b,t=setup(tmp_path)
    try:
        t.on_bar(START,rows(),{'BTCUSDT':100,'ETHUSDT':100},now_ms=x.now)
        t.positions['ETHUSDT']['stop']=100.003  # rounds down to 100.00 for a short
        original=x.amend; calls=[]
        def interrupt(*args):
            calls.append(args); original(*args); raise Crash()
        x.amend=interrupt
        with pytest.raises(Crash): t.service(now_ms=x.now,marks={'ETHUSDT':100})
    finally: b.close()
    x,b,t=setup(tmp_path,x)
    try:
        t.service(now_ms=x.now,marks={'ETHUSDT':100})
        assert not t.pending_operation and not t.halted and len(calls)==1
        stop=t.orders['ETHUSDT']['SL']['client_order_index']
        assert Decimal(b.engine.store.load(stop)[0].trigger_price)==100
        assert b.account_state().execution_changes_verified
    finally:b.close()


def test_restart_after_partial_tp_keeps_protection_and_no_duplicate_entry(tmp_path):
    x,b,t=setup(tmp_path)
    try:
        t.on_bar(START,rows(),{'BTCUSDT':100,'ETHUSDT':100},now_ms=x.now)
        qty=t.positions['ETHUSDT']['qty']; tp=t.orders['ETHUSDT']['TP']['client_order_index']
        x.now+=1000; x.fill(tp,Decimal(str(qty))/4)
        sent=len(x.sent)
    finally:b.close()
    x,b,t=setup(tmp_path,x)
    try:
        t.service(now_ms=x.now,marks={'ETHUSDT':100})
        assert t.positions['ETHUSDT']['qty']==pytest.approx(qty*.75)
        assert not t.positions['ETHUSDT']['partial_taken'] and len(x.sent)==sent
        assert not t.halted
    finally:b.close()


def test_stop_fills_while_process_down_after_amend_acceptance(tmp_path):
    x,b,t=setup(tmp_path)
    try:
        t.on_bar(START,rows(),{'BTCUSDT':100,'ETHUSDT':100},now_ms=x.now)
        t.positions['ETHUSDT']['stop']=100.003
        original=x.amend
        def interrupt(*args): original(*args); raise Crash()
        x.amend=interrupt
        with pytest.raises(Crash): t.service(now_ms=x.now,marks={'ETHUSDT':100})
        stop=t.orders['ETHUSDT']['SL']['client_order_index']; qty=t.positions['ETHUSDT']['qty']
    finally:b.close()
    x.now+=1000; x.fill(stop,str(qty)); sent=len(x.sent)
    x,b,t=setup(tmp_path,x)
    try:
        b.poll(); t.service(now_ms=x.now)
        assert not t.positions and not t.pending_operation and not t.halted
        assert len(x.sent)==sent and x.active_orders()==[]
    finally:b.close()


def test_insufficient_request_headroom_skips_entry_before_durable_order_intent(tmp_path):
    x,b,t=setup(tmp_path)
    x.budget=RequestBudget(tmp_path/'budget.sqlite')
    try:
        for _ in range(40): x.budget.check(consume=True)
        out=t.on_bar(START,rows(),{'BTCUSDT':100,'ETHUSDT':100},now_ms=x.now)
        assert out.skipped and not t.pending_operation and not t.halted and x.sent==[]
    finally:b.close(); x.budget.close()


def test_delayed_history_blocks_entries_but_keeps_native_stop(tmp_path):
    from live.recovery import ExchangeRecovery
    from test_exchange_recovery import Coverage
    x,b,t=setup(tmp_path)
    try:
        t.on_bar(START,rows(),{'BTCUSDT':100,'ETHUSDT':100},now_ms=x.now)
        recovery=ExchangeRecovery(tmp_path,'reviewed',clock=lambda:x.now/1000,monotonic=lambda:x.now/1000)
        t.exchange_recovery=recovery
        b.entry_guard=recovery.can_enter
        trades=x.trades; x.trades=[]; sent=len(x.sent)
        t.service(now_ms=x.now,marks={'ETHUSDT':100})
        assert not t.halted and t.publication_pending and len(x.sent)==sent
        assert t.publication_pending_evidence and not recovery.can_enter()
        assert (tmp_path/'EXCHANGE_PAUSED').exists() and not (tmp_path/'ENTRY_PAUSED').exists()
        assert any(r['type']=='stop-loss' for r in x.active_orders())
        x.trades=trades
        for index in range(3):
            x.now+=60000
            b.poll()
            t.service(now_ms=x.now,marks={'ETHUSDT':100})
            recovery.cycle(t,Coverage(),poll_ok=True,service_ok=True,now_ms=x.now)
            assert not t.halted and not t.publication_pending
            assert b.last_report['execution_changes_verified']
            assert recovery.can_enter() is (index==2)
        assert len(x.sent)==sent
    finally:b.close()


def test_request_budget_survives_restart_and_wall_clock_rollback(tmp_path):
    now=[100.]; path=tmp_path/'budget.sqlite'
    b=RequestBudget(path,clock=lambda:now[0])
    for _ in range(55): b.check(consume=True)
    b.close(); b=RequestBudget(path,clock=lambda:now[0])
    try:
        with pytest.raises(RequestDeferred): b.check(consume=True)
        now[0]=99
        with pytest.raises(RequestDeferred): b.check(consume=True)
        now[0]=161; b.check(consume=True)
        b.defer(90)
    finally:b.close()
    b=RequestBudget(path,clock=lambda:now[0])
    try:
        with pytest.raises(RequestDeferred): b.check(consume=True)
        now[0]=252; b.check(consume=True)
    finally:b.close()


@pytest.mark.parametrize('code',[405,429])
def test_rate_response_prevents_retry_storm_without_logging_secrets(tmp_path,code):
    calls=[]
    class Opener:
        def open(self,*args,**kwargs):
            calls.append(1)
            raise urllib.error.HTTPError('secret-url',code,'secret-response',{'Retry-After':'70'},None)
    t=object.__new__(LighterTransport)
    t.host='https://example.invalid'; t.opener=Opener(); t.request_samples=deque()
    t.budget=RequestBudget(tmp_path/'budget.sqlite')
    try:
        with pytest.raises(RuntimeError,match=f'HTTP {code}') as exc:
            t.get('/api/v1/account?secret=value',private=False)
        assert 'secret' not in str(exc.value)
        with pytest.raises(RequestDeferred): t.get('/api/v1/account',private=False)
        assert len(calls)==1 and t.request_samples[0]['endpoint']=='/api/v1/account'
    finally:t.budget.close()


def test_steady_poll_uses_five_reads_with_or_without_resting_orders(tmp_path):
    x,b,t=setup(tmp_path)
    calls=[]
    for name in ('get','account','active_orders','order'):
        original=getattr(x,name)
        def wrap(*a,_name=name,_original=original,**kw):
            calls.append(_name); return _original(*a,**kw)
        setattr(x,name,wrap)
    try:
        b.poll(); t.service(now_ms=x.now)
        assert len(calls)==5
        t.on_bar(START,rows(),{'BTCUSDT':100,'ETHUSDT':100},now_ms=x.now)
        calls.clear(); b.poll(); t.service(now_ms=x.now)
        assert len(calls)==5 and 'order' not in calls
    finally:b.close()


@pytest.mark.parametrize('kind',['market','stop-loss','limit','amend'])
def test_abrupt_process_exit_releases_lock_and_recovers_durable_state(tmp_path,kind):
    # Independent process exits without finally/close, including an actual held
    # OS account lock. The venue is a persisted synthetic fixture, not mainnet.
    code='''
import json,os,sys
from decimal import Decimal
from dataclasses import asdict
from pathlib import Path
from test_strategy_integration import setup,rows,START
p=Path(sys.argv[1]); kind=sys.argv[2]
x,b,t=setup(p)
def crash():
    (p/'venue.json').write_text(json.dumps({'rows':x.rows,'trades':x.trades,
        'position':{str(k):str(v) for k,v in x.position.items()},
        'sent':[asdict(i) for i in x.sent],'now':x.now,'next_trade':x.next_trade}))
    os._exit(77)
original=x.submit
def submit(intent,*args):
    original(intent,*args)
    if intent.order_type==kind: crash()
x.submit=submit
t.on_bar(START,rows(),{'BTCUSDT':100,'ETHUSDT':100},now_ms=x.now)
if kind=='amend':
    original_amend=x.amend
    def amend(*args):
        original_amend(*args); crash()
    x.amend=amend
    t.positions['ETHUSDT']['stop']=100.003
    t.service(now_ms=x.now,marks={'ETHUSDT':100})
'''
    env=dict(os.environ)
    env['PYTHONPATH']=os.pathsep.join([str(Path(__file__).parent),str(Path(__file__).parents[1]/'src'),env.get('PYTHONPATH','')])
    child=subprocess.run([sys.executable,'-c',code,str(tmp_path),kind],env=env,capture_output=True,text=True,timeout=30)
    assert child.returncode==77, child.stderr
    from live.execution_evidence import Intent
    saved=json.loads((tmp_path/'venue.json').read_text()); x=Venue()
    x.rows={int(k):v for k,v in saved['rows'].items()}; x.trades=saved['trades']
    x.position={int(k):Decimal(v) for k,v in saved['position'].items()}
    x.sent=[Intent(**i) for i in saved['sent']]; x.now=saved['now']; x.next_trade=saved['next_trade']
    x,b,t=setup(tmp_path,x)
    try:
        b.poll(); t.service(now_ms=x.now)
        assert not t.halted and t.pending_operation is None
        assert len(x.sent)==3 and sum(i.order_type=='market' for i in x.sent)==1
        assert b.account_state().execution_changes_verified
        assert any(r['type']=='stop-loss' for r in x.active_orders())
    finally:b.close()

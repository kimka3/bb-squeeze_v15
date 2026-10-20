from decimal import Decimal
import pytest
from test_strategy_integration import setup, rows, START, H4
from live.request_budget import RequestBudget


def defer(tmp_path):
    x,b,t=setup(tmp_path)
    x.budget=RequestBudget(tmp_path/'budget.sqlite',clock=lambda:x.now/1000)
    # Traffic from 50 seconds ago expires while the original entry window is open.
    with x.budget.db:
        x.budget.db.executemany('INSERT INTO requests VALUES(?)',[(x.now/1000-50,)]*40)
    out=t.on_bar(START,rows(),{'BTCUSDT':100,'ETHUSDT':100},now_ms=x.now)
    assert t.deferred_entries and not x.sent and not t.pending_operation and not t.halted
    return x,b,t


def close(x,b):
    b.close(); x.budget.close()


def test_deferred_entry_retries_in_original_window_once(tmp_path):
    x,b,t=defer(tmp_path)
    try:
        assert t.deferred_entries['deadline_ms']==START+H4+t.config.bar_grace_seconds*1000
        x.now+=11000
        result=t.retry_deferred_entries()
        assert len(result.entries)==1 and not t.deferred_entries
        assert [i.order_type for i in x.sent]==['market','stop-loss','limit']
        t.retry_deferred_entries()
        t.on_bar(START,rows(),{'BTCUSDT':100,'ETHUSDT':100},now_ms=x.now)
        assert len(x.sent)==3
    finally:close(x,b)


@pytest.mark.parametrize('reason',['deadline','pause','halt','clock','unknown_position'])
def test_deferred_candidate_rechecks_guards(tmp_path,reason):
    x,b,t=defer(tmp_path)
    try:
        x.now+=11000
        if reason=='deadline':x.now=START+H4+int(t.config.bar_grace_seconds*1000)
        if reason=='pause':b.entry_guard=lambda:False
        if reason=='halt':t._halt('test')
        if reason=='clock':x.now=START+H4-1
        if reason=='unknown_position':x.position[0]=Decimal('1')
        t.retry_deferred_entries()
        assert not x.sent and not t.deferred_entries
    finally:close(x,b)


def test_retry_wait_does_not_poll_and_expiration_does_not_poll(tmp_path):
    x,b,t=defer(tmp_path)
    try:
        b.poll=lambda:pytest.fail('unnecessary poll')
        t.retry_deferred_entries()
        x.now=START+H4+int(t.config.bar_grace_seconds*1000)
        t.retry_deferred_entries()
        assert not t.deferred_entries
    finally:close(x,b)


def test_restart_does_not_restore_unsubmitted_candidate(tmp_path):
    x,b,t=defer(tmp_path)
    b.close()
    x,b,t=setup(tmp_path,x)
    try:
        assert t.deferred_entries is None and not t.pending_operation
        x.now+=11000
        t.retry_deferred_entries()
        assert not x.sent
    finally:close(x,b)


def test_entry_uses_one_fresh_snapshot_for_sizing_and_preflight(tmp_path):
    x,b,t=setup(tmp_path)
    calls=[]; original=x.account
    x.account=lambda:(calls.append(1) or original())
    try:
        t.on_bar(START,rows(),{'BTCUSDT':100,'ETHUSDT':100},now_ms=x.now)
        assert len(x.sent)==3
        assert len(calls)==2  # initial bar reconciliation + entry sizing, no third scan
        assert b._entry_state is None
    finally:b.close()


def test_deferred_entry_sizes_against_refreshed_equity(tmp_path):
    x,b,t=defer(tmp_path)
    original=x.account
    def account():
        a=original(); a.update(total_asset_value='150',available_balance='150'); return a
    x.account=account
    try:
        x.now+=11000
        t.retry_deferred_entries()
        assert t.positions['ETHUSDT']['initial_risk']==pytest.approx(3)
    finally:close(x,b)


def test_retry_rechecks_basis_and_does_not_rescan_signals(tmp_path,monkeypatch):
    x,b,t=defer(tmp_path)
    try:
        monkeypatch.setattr('live.trader.scan_signals',lambda *a:pytest.fail('signal scan repeated'))
        t.config.max_basis_divergence_pct=.5
        original=x.get
        def get(path,**kw):
            if path=='/api/v1/orderBookDetails':
                return {'order_book_details':[{'market_id':i,'market_type':'perp','mark_price':'110'} for i in (0,1)]}
            return original(path,**kw)
        x.get=get; x.now+=11000
        out=t.retry_deferred_entries()
        assert out.skipped and not x.sent and not t.deferred_entries
    finally:close(x,b)


def test_slow_account_read_cannot_enter_after_deadline(tmp_path):
    x,b,t=defer(tmp_path)
    try:
        original=b.account_state; calls=[]
        def slow():
            value=original(); calls.append(1)
            if len(calls)==2:x.now=START+H4+int(t.config.bar_grace_seconds*1000)
            return value
        b.account_state=slow; x.now+=11000
        t.retry_deferred_entries()
        assert not x.sent and not t.pending_operation
    finally:close(x,b)


def test_expired_snapshot_is_not_reused(tmp_path):
    x,b,t=setup(tmp_path)
    calls=[]; original=x.account; offer=b.offer_entry_state
    x.account=lambda:(calls.append(1) or original())
    def stale(state):
        offer(state); x.now+=2000
    b.offer_entry_state=stale
    try:
        t.on_bar(START,rows(),{'BTCUSDT':100,'ETHUSDT':100},now_ms=x.now)
        assert len(calls)==3 and len(x.sent)==3
    finally:b.close()


def test_two_candidates_with_metered_reads_preserve_order_and_protection(tmp_path,monkeypatch):
    from live.markets import Market
    x,b,t=setup(tmp_path)
    x.budget=RequestBudget(tmp_path/'budget.sqlite',clock=lambda:x.now/1000)
    # Count each fake endpoint and nonce/send pair like the real adapter.
    for name in ('get','account','active_orders','order'):
        original=getattr(x,name)
        def counted(*args,_call=original,**kw):
            x.budget.check(consume=True); return _call(*args,**kw)
        setattr(x,name,counted)
    original=x.submit
    def submit(*args):
        x.budget.check(consume=True); x.budget.check(consume=True); return original(*args)
    x.submit=submit
    # Two canonical short candidates; lower risk only in this isolated fixture.
    from dataclasses import replace
    t.cfg=replace(t.cfg,risk=.01)
    monkeypatch.setattr('live.trader.scan_signals',lambda *a:({'ETHUSDT':('SHORT',4,START),'BTCUSDT':('SHORT',4,START)},0))
    try:
        t.on_bar(START,rows(),{'BTCUSDT':100,'ETHUSDT':100},now_ms=x.now)
        assert [i.market_index for i in x.sent if not i.reduce_only]==[1,0]
        assert [i.order_type for i in x.sent].count('stop-loss')==2
        assert not t.halted and not t.deferred_entries
        assert x.budget.limit==55
    finally:close(x,b)


def test_deadline_crossed_in_nonce_lookup_never_sends(monkeypatch):
    from types import SimpleNamespace
    from live.authenticated import LighterTransport
    from live.execution_evidence import Intent
    t=LighterTransport.__new__(LighterTransport)
    t.client=SimpleNamespace(ORDER_TYPE_MARKET=1,ORDER_TYPE_LIMIT=0,ORDER_TYPE_STOP_LOSS=4,
        ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL=0,ORDER_TIME_IN_FORCE_POST_ONLY=1,
        ORDER_TIME_IN_FORCE_GOOD_TILL_TIME=2)
    t.new_entry_deadline_ms=1000
    t._next_nonce=lambda:1
    monkeypatch.setattr('live.authenticated.time.time',lambda:1.001)
    i=Intent(100,0,123,True,False,'1','market','immediate-or-cancel','100')
    with pytest.raises(RuntimeError,match='deadline elapsed'):
        t.submit(i,1,100,0)


def test_runner_services_protection_then_retries_before_next_regular_poll(tmp_path,monkeypatch):
    import time
    from live import run
    x,b,t=setup(tmp_path)
    seen=[]
    t.deferred_entries={'retry_at_ms':0,'deadline_ms':0}
    monkeypatch.setattr(run,'prepare_frames',lambda *a:{})
    monkeypatch.setattr(run,'run_once',lambda *a,**kw:None)
    monkeypatch.setattr(run,'persist_report',lambda *a,**kw:None)
    monkeypatch.setattr(t,'service',lambda **kw:seen.append('protection'))
    def retry():
        seen.append('retry'); run.STOP_REQUESTED.set()
    monkeypatch.setattr(t,'retry_deferred_entries',retry)
    run.STOP_REQUESTED.clear()
    try:
        run.run_loop(t,t.markets,t.config,time.time()+2)
        assert seen==['protection','retry']
    finally:
        run.STOP_REQUESTED.clear();b.close()


def test_budget_delay_after_nonce_still_cannot_send_expired_entry(monkeypatch):
    from types import SimpleNamespace
    from live.authenticated import LighterTransport
    from live.execution_evidence import NotSubmitted
    stamp=[.9]; observed=[]
    class Budget:
        def check(self,**kw):stamp[0]=1.001
    t=LighterTransport.__new__(LighterTransport);t.budget=Budget()
    monkeypatch.setattr('live.authenticated.time.time',lambda:stamp[0])
    async def transaction():observed.append('sent')
    with pytest.raises(NotSubmitted) as caught:
        t._send(transaction(),deadline_ms=1000)
    assert caught.value.reason == 'entry_deadline_elapsed'
    assert observed==[]

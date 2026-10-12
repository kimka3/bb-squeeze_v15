import json
from decimal import Decimal
from pathlib import Path
import sys
from urllib.parse import urlsplit,parse_qs
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from data_io import START,H4
from live.account_ledger import AccountLedger,history,normalize_trade
from live.execution_evidence import UnverifiedExecution
from live.strategy_broker import StrategyBroker
from live.trader import Trader
from live.config import LiveConfig
from live.journal import Journal
from live.markets import Market
from frontier_engine import Strategy


class Venue:
    network='testnet'; account_index=100; host='https://testnet.zklighter.elliot.ai'
    def __init__(self):
        self.rows={}; self.trades=[]; self.funding=[]; self.position={}; self.sent=[]
        self.now=START+H4+1000; self.next_trade=1; self.lost=False; self.reject_tp=False
    def account(self):
        return {'total_asset_value':'300','available_balance':'300','positions':[
            {'market_id':m,'position':str(abs(q)),'sign':1 if q>0 else -1,
             'position_value':str(abs(q)*100),'avg_entry_price':'100'} for m,q in self.position.items() if q]}
    def active_orders(self): return [r for r in self.rows.values() if r['status'] in ('open','pending')]
    def order(self,coi): return self.rows.get(coi)
    def get(self,path,private=True):
        if path.startswith('/api/v1/trades?'): return {'trades':sorted(self.trades,key=lambda r:r['timestamp'],reverse=True),'next_cursor':''}
        if path.startswith('/api/v1/positionFunding?'): return {'position_fundings':sorted(self.funding,key=lambda r:r['timestamp'],reverse=True),'next_cursor':''}
        return {'order_book_details':[{'market_id':i,'market_type':'perp','mark_price':'100'} for i in (0,1)]}
    def submit(self,i,base,price,trigger):
        self.sent.append(i)
        self.rows[i.client_order_index]={'owner_account_index':100,'market_index':i.market_index,
            'client_order_index':i.client_order_index,'order_index':i.client_order_index+1000,
            'is_ask':i.is_ask,'reduce_only':i.reduce_only,'type':i.order_type,'time_in_force':i.time_in_force,
            'price':i.price,'trigger_price':i.trigger_price,'initial_base_amount':i.quantity,
            'filled_base_amount':'0','filled_quote_amount':'0','remaining_base_amount':i.quantity,
            'status':'pending' if i.order_type=='stop-loss' else 'open',
            'trigger_status':'mark-price' if i.order_type=='stop-loss' else 'na','trigger_time':0,'parent_order_index':0}
        if i.order_type=='market': self.fill(i.client_order_index,Decimal(i.quantity))
        if i.order_type=='limit' and self.reject_tp:
            self.rows[i.client_order_index].update(status='canceled',remaining_base_amount='0')
        if self.lost: raise RuntimeError('lost ack')
    def fill(self,coi,qty):
        r=self.rows[coi]; qty=Decimal(qty)
        prior=Decimal(r['filled_base_amount']); remaining=Decimal(r['initial_base_amount'])-prior-qty
        r.update(filled_base_amount=str(prior+qty),filled_quote_amount=str((prior+qty)*100),remaining_base_amount=str(remaining),
                 status='filled' if not remaining else 'open')
        m=r['market_index']; self.position[m]=self.position.get(m,Decimal(0))+qty*(-1 if r['is_ask'] else 1)
        ask=r['is_ask']; side='ask' if ask else 'bid'
        self.trades.append({'trade_id':self.next_trade,'type':'trade','market_id':m,'size':str(qty),'price':'100',
            'usd_amount':str(qty*100),'ask_account_id':100 if ask else 200,'bid_account_id':200 if ask else 100,
            'is_maker_ask':not ask,side+'_client_id':coi,side+'_id':r['order_index'],
            'timestamp':self.now,'taker_fee':0,'integrator_taker_fee':0})
        self.next_trade+=1
    def cancel(self,market,order):
        r=next(r for r in self.rows.values() if r['order_index']==order)
        r.update(status='canceled',remaining_base_amount='0')
    def amend(self,market,order,base,price,trigger):
        r=next(r for r in self.rows.values() if r['order_index']==order)
        r.update(initial_base_amount=str(Decimal(base)/10000),remaining_base_amount=str(Decimal(base)/10000),
                 price=str(Decimal(price)/100),trigger_price=str(Decimal(trigger)/100))


def setup(tmp_path,venue=None,writes=True):
    x=venue or Venue()
    markets={s:Market(s[:-4],m,4,2,.0001,0,.06,.1,'active') for s,m in [('BTCUSDT',1),('ETHUSDT',0)]}
    config=LiveConfig(state_dir=tmp_path,universe=tuple(markets),strategy=Strategy(risk=.02,total_risk=.04,
        short_btc_bull_risk=.5,fee=0,slippage=0),min_notional_usd=0,max_basis_divergence_pct=1e9)
    b=StrategyBroker(x,markets,tmp_path/'execution',start_ms=START,clock=lambda:x.now/1000,writes_enabled=writes,timeout=0)
    t=Trader(config,b,markets,Journal(tmp_path))
    return x,b,t


def rows(short=True):
    return {s:{'open':100,'high':102,'low':99,'close':100,'s_atr':4,'l_atr':4,
        'short_signal':short and s=='ETHUSDT','long_breakout':False,'l_bb_upper':110,
        'l_bb_mid':90,'l_ma200':95,'l_ma200_slope':1,'x_bb_lower':99.5,'x_ma200':110,'x_rvol':1} for s in ['BTCUSDT','ETHUSDT']}


def test_trader_entry_partial_tp_complete_tp_breakeven_and_restart(tmp_path):
    x,b,t=setup(tmp_path)
    try:
        out=t.on_bar(START,rows(),{'BTCUSDT':100,'ETHUSDT':100},now_ms=x.now)
        assert len(out.entries)==1 and not t.halted
        p=t.positions['ETHUSDT']; original=p['qty']
        assert p['initial_risk']==pytest.approx(6)
        assert all(0<=i.client_order_index<2**48 for i in x.sent)
        tp=t.orders['ETHUSDT']['TP']['client_order_index']
        x.now+=1000; x.fill(tp,Decimal(str(original))/4)
        t.service(now_ms=x.now,marks={'ETHUSDT':100})
        assert not p['partial_taken'] and p['qty']==pytest.approx(original*.75)
        x.now+=1000; x.fill(tp,Decimal(str(original))/4)
        t.service(now_ms=x.now,marks={'ETHUSDT':100})
        assert p['partial_taken'] and p['qty']==pytest.approx(original/2)
        assert p['stop']==100 and not t.halted
        assert Decimal(x.rows[t.orders['ETHUSDT']['SL']['client_order_index']]['trigger_price'])==100
        assert b.account_state().execution_changes_verified
        count=len(x.sent)
    finally: b.close()
    x,b,t=setup(tmp_path,x)
    try:
        t.service(now_ms=x.now,marks={'ETHUSDT':100})
        assert len(x.sent)==count and t.positions['ETHUSDT']['partial_taken']
    finally: b.close()


def test_unknown_reduction_halts_without_claiming_tp(tmp_path):
    x,b,t=setup(tmp_path)
    try:
        t.on_bar(START,rows(),{'BTCUSDT':100,'ETHUSDT':100},now_ms=x.now)
        x.position[0]/=2
        t.service(now_ms=x.now,marks={'ETHUSDT':100})
        assert t.halted and not t.positions['ETHUSDT']['partial_taken']
    finally: b.close()


def test_strategy_default_cannot_submit(tmp_path):
    x,b,t=setup(tmp_path,writes=False)
    try:
        t.on_bar(START,rows(),{'BTCUSDT':100,'ETHUSDT':100},now_ms=x.now)
        assert x.sent==[] and t.halted
    finally: b.close()


def test_lost_reply_and_rejected_tp_never_duplicate_entry(tmp_path):
    x=Venue(); x.lost=True; x.reject_tp=True
    x,b,t=setup(tmp_path,x)
    try:
        t.on_bar(START,rows(),{'BTCUSDT':100,'ETHUSDT':100},now_ms=x.now)
        assert t.halted and t.orders['ETHUSDT']['SL']
        before=len(x.sent); t.service(now_ms=x.now,marks={'ETHUSDT':100})
        assert len(x.sent)==before
        assert sum(not i.reduce_only for i in x.sent)==1
    finally: b.close()


def test_tiny_capital_partial_tp_minimum_does_not_upsize(tmp_path):
    x,b,t=setup(tmp_path)
    try:
        m=t.markets['ETHUSDT']
        from dataclasses import replace
        t.markets['ETHUSDT']=replace(m,min_quote_amount=100)
        t.on_bar(START,rows(),{'BTCUSDT':100,'ETHUSDT':100},now_ms=x.now)
        assert x.sent==[] and not t.positions
    finally: b.close()


def test_history_cursor_stall_and_missing_rows_fail():
    class Read:
        account_index=100
        def get(self,path): return {'trades':[{'timestamp':1000}],'next_cursor':'same'}
    with pytest.raises(UnverifiedExecution,match='stalled'): history(Read(),'trades',0,2000)
    Read.get=lambda self,path:{'next_cursor':''}
    with pytest.raises(UnverifiedExecution): history(Read(),'trades',0,2000)


def test_pagination_dedup_and_missing_fees_not_zero(tmp_path):
    x,b,t=setup(tmp_path)
    try:
        t.on_bar(START,rows(),{'BTCUSDT':100,'ETHUSDT':100},now_ms=x.now)
        row=dict(x.trades[0]); row.pop('taker_fee')
        assert normalize_trade(row,100)['fee_usdc'] is None
        b.ledger.collect(x,x.now); first=b.ledger.report()['fill_count']
        b.ledger.collect(x,x.now); assert b.ledger.report()['fill_count']==first
        with pytest.raises(UnverifiedExecution): b.ledger.ingest([row],[],x.now)
    finally: b.close()


def test_settled_funding_attributed_by_holding_interval(tmp_path):
    x,b,t=setup(tmp_path)
    try:
        t.on_bar(START,rows(),{'BTCUSDT':100,'ETHUSDT':100},now_ms=x.now)
        x.now+=60000
        x.funding=[{'timestamp':x.now//1000,'market_id':0,'funding_id':8,'change':'.12','discount':'0',
                    'position_size':'1.5','position_side':'short','rate':'.001'}]
        state=b.account_state()
        item=state.ledger_report['funding_attributions'][0]
        assert item['cycle']==b._cycle('ETHUSDT')
        assert item['cash_change']=='0.12'
    finally: b.close()


def test_close_rule_exit_cancels_protection_and_reconciles_flat(tmp_path):
    x,b,t=setup(tmp_path)
    try:
        t.on_bar(START,rows(),{'BTCUSDT':100,'ETHUSDT':100},now_ms=x.now)
        t.positions['ETHUSDT']['bars']=89
        x.now+=H4
        out=t.on_bar(START+H4,rows(False),{'BTCUSDT':100,'ETHUSDT':100},now_ms=x.now)
        assert out.exits and not t.positions
        assert x.active_orders()==[] and b.account_state().execution_changes_verified
        assert len(b.ledger.events('trade'))==2
    finally: b.close()


def test_history_fetches_all_pages_and_filters_campaign_scope():
    class Read:
        account_index=100
        def __init__(self): self.calls=0
        def get(self,path):
            self.calls+=1
            if 'cursor=' not in path: return {'trades':[{'timestamp':3000}],'next_cursor':'a+/='}
            assert parse_qs(urlsplit(path).query)['cursor']==['a+/=']
            return {'trades':[{'timestamp':2000},{'timestamp':1000}],'next_cursor':'older'}
    x=Read()
    assert history(x,'trades',1500,3000)==[{'timestamp':3000},{'timestamp':2000}]
    assert x.calls==2


def test_trade_side_mismatch_and_unknown_manual_round_trip_cannot_certify(tmp_path):
    x,b,t=setup(tmp_path)
    try:
        t.on_bar(START,rows(),{'BTCUSDT':100,'ETHUSDT':100},now_ms=x.now)
        x.now+=1
        extra={**x.trades[0],'trade_id':999,'timestamp':x.now,'ask_client_id':123,'ask_id':9999}
        x.trades.append(extra)
        assert not b.account_state().execution_changes_verified
        t.service(now_ms=x.now,marks={'ETHUSDT':100})
        assert t.halted
    finally: b.close()


def test_read_only_factory_connects_trader_and_blocks_writes(tmp_path,monkeypatch):
    import live.run as runner
    x,b,t=setup(tmp_path/'fixture'); markets=t.markets; b.close()
    config=LiveConfig(mode='live',base_url=x.host,account_index=100,api_key_index=4,
                      state_dir=tmp_path/'factory',universe=tuple(markets))
    monkeypatch.setattr(runner,'load_markets',lambda *a:markets)
    trader,_=runner.build_authenticated_strategy(config,x,start_ms=START)
    try:
        assert trader.broker.writes_enabled is False
        assert trader.config.strategy.risk==.02 and trader.config.strategy.total_risk==.04
    finally: trader.broker.close()


def test_read_only_audit_matches_fills_but_keeps_omitted_fees_unknown(tmp_path):
    from live.strategy_audit import audit
    x,b,t=setup(tmp_path/'strategy')
    try:
        t.on_bar(START,rows(),{'BTCUSDT':100,'ETHUSDT':100},now_ms=x.now)
        entry=b._cycle('ETHUSDT')
        t.positions['ETHUSDT']['bars']=89; x.now+=H4
        t.on_bar(START+H4,rows(False),{'BTCUSDT':100,'ETHUSDT':100},now_ms=x.now)
        exit_id=next(c for c,v in b._bindings().items() if v['role']=='EXIT')
        session={'account_index':100,'network':'testnet','started_at_ms':START,
            'first_coi':entry,'entry_result':b.engine.store.load(entry)[1],
            'close_results':{str(exit_id):b.engine.store.load(exit_id)[1]},'equity_before':'300'}
        for trade in x.trades: trade.pop('taker_fee')
        before=len(x.sent)
        result=audit(x,[session],tmp_path/'audit',clock=lambda:x.now/1000)
        assert result['all_order_fills_match'] and result['unknown_fee_fills']==2
        assert result['performance_complete'] is False
        assert result['open_position_count']==result['active_order_count']==0
        assert len(x.sent)==before
    finally: b.close()


def test_long_stop_and_close_use_sell_reduce_only(tmp_path):
    x,b,t=setup(tmp_path)
    try:
        m=t.markets['ETHUSDT']
        fill=b.market_order(m,'ETHUSDT','LONG',1,False,10,100)
        ref=b.place_stop(m,'ETHUSDT','LONG',fill.qty,94,11)
        b.modify_stop(m,ref,fill.qty,96)
        assert x.sent[0].is_ask is False and x.sent[0].reduce_only is False
        assert x.sent[1].is_ask is True and x.sent[1].reduce_only is True
        assert Decimal(x.rows[11]['trigger_price'])==96
        b.market_order(m,'ETHUSDT','LONG',1,True,12,100)
        b.cancel(m,ref)
        assert b.account_state().positions=={} and x.active_orders()==[]
    finally: b.close()

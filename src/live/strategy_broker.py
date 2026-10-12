"""Trader adapter for the verified execution engine. Writes disabled by default.

Construction is dependency-injected; the production CLI remains gated. Account
position changes must equal owned order fills and paginated trade history before
the Trader is allowed to attribute a partial exit to its TP order.
"""
from decimal import Decimal, ROUND_FLOOR
import json
import time

from .account_ledger import AccountLedger, signed
from .broker import AccountState, Fill, OrderRef, _is_ask
from .execution import ExecutionEngine, positions
from .execution_evidence import Intent, UnverifiedExecution, number, verify_order, verify_stop
from .live_check import price_tick


class StrategyBroker:
    max_client_order_index = 2**48
    def __init__(self,transport,markets,directory,*,start_ms,slippage='.005',
                 writes_enabled=False,clock=time.time,timeout=30,account_lock_path=None):
        self.transport,self.markets,self.clock=transport,markets,clock
        self.writes_enabled=writes_enabled
        self.slippage=number(slippage)
        if not 0<self.slippage<1: raise ValueError('invalid price bound')
        self.engine=ExecutionEngine(transport,directory,timeout=timeout,lock_path=account_lock_path)
        self.db=self.engine.store.db
        self.db.execute('CREATE TABLE IF NOT EXISTS bindings(coi TEXT PRIMARY KEY,value TEXT)')
        self.db.commit()
        self.ledger=AccountLedger(self.engine.directory/'account-ledger.sqlite',transport.network,transport.account_index,start_ms)
        self._marks={}; self._mark_sample_ms={}
        self.by_id={m.market_id:(s,m) for s,m in markets.items()}
        self.last_report={}

    def close(self):
        self.ledger.close(); self.engine.close()

    def _write(self):
        if not self.writes_enabled: raise RuntimeError('strategy writes disabled')

    def _bindings(self):
        return {int(c):json.loads(v) for c,v in self.db.execute('SELECT coi,value FROM bindings ORDER BY rowid')}

    def _cycle(self,symbol):
        matches=[c for c,v in self._bindings().items() if v['symbol']==symbol and v['role']=='ENTRY']
        if not matches: raise UnverifiedExecution('no owned entry cycle')
        return matches[-1]

    def _bind(self,coi,symbol,side,role):
        if type(coi) is not int or not 0<=coi<2**48:
            raise ValueError('Lighter client order index must fit 48 bits')
        existing=self._bindings().get(coi)
        value={'symbol':symbol,'side':side,'role':role,'cycle':coi if role=='ENTRY' else self._cycle(symbol)}
        if existing and existing!=value: raise UnverifiedExecution('order role/cycle changed')
        with self.db: self.db.execute('INSERT OR IGNORE INTO bindings VALUES(?,?)',(str(coi),json.dumps(value,sort_keys=True)))

    @staticmethod
    def quantity(market,qty):
        # Decimal avoids floating-point lot-size overshoot/extra truncation.
        return number(qty).quantize(Decimal(1).scaleb(-market.size_decimals),rounding=ROUND_FLOOR)

    def validate_entry(self,market,side,qty,price,tp_fraction,tp_price):
        q=self.quantity(market,qty)
        if market.rejects(float(q),price): raise ValueError('entry is below exchange minimum')
        if side=='SHORT' and tp_fraction>0:
            half=self.quantity(market,q*number(tp_fraction))
            if market.rejects(float(half),tp_price) or market.rejects(float(q-half),tp_price):
                raise ValueError('entry cannot support configured partial TP at exchange minimums')

    def _intent(self,market,side,qty,coi,*,closing,kind,price,trigger='0',tif='immediate-or-cancel'):
        if side not in ('LONG','SHORT'): raise ValueError('invalid position side')
        ask=_is_ask(side,closing)
        bound=number(price)*(1-self.slippage if ask else 1+self.slippage)
        return Intent(self.transport.account_index,market.market_id,coi,ask,closing,
            str(self.quantity(market,qty)),kind,tif,
            str(price_tick(bound,market.price_decimals,upward=not ask)),
            str(price_tick(number(trigger),market.price_decimals,upward=side=='LONG')))

    def market_order(self,market,symbol,position_side,qty,closing,coi,ref_price):
        self._write()
        if self.engine.store.load(coi) is None:
            state=self.account_state()
            if not state.execution_changes_verified:
                raise UnverifiedExecution('entry/exit requires reconciled owned exposure')
            actual=state.positions.get(market.symbol)
            if closing and (actual is None or actual['side']!=position_side or number(qty)>number(actual['qty'])):
                raise UnverifiedExecution('close exceeds confirmed owned exposure')
            if not closing and actual is not None:
                raise UnverifiedExecution('new entry into an existing position refused')
            if not closing and state.active_orders.get(market.symbol):
                raise UnverifiedExecution('old active orders must be resolved before re-entry')
        self._bind(coi,symbol,position_side,'EXIT' if closing else 'ENTRY')
        existing=self.engine.store.load(coi)
        intent=existing[0] if existing else self._intent(market,position_side,qty,coi,closing=closing,kind='market',price=ref_price)
        self.engine.create(intent,market.size_decimals,market.price_decimals)
        return self.market_order_result(coi)

    def market_order_result(self,coi):
        binding=self._bindings().get(coi)
        if not binding: return None
        result=self.engine.recover(coi)
        # A fill can precede publication of trade history. Protect it promptly;
        # leave the fee unknown and let reconciliation block further entries.
        totals=self.ledger.order_totals(coi,result['order_index'])
        fee=totals['fee_usdc'] if totals['qty']==number(result['filled_qty']) else None
        return Fill(binding['symbol'],binding['side'],float(result['filled_qty']),
                    float(result['average_price'] or 0),binding['role'],float(fee) if fee is not None else None)

    def place_stop(self,market,symbol,position_side,qty,trigger,coi):
        self._write(); self._bind(coi,symbol,position_side,'SL')
        old=self.engine.store.load(coi)
        intent=old[0] if old else self._intent(market,position_side,qty,coi,closing=True,kind='stop-loss',price=trigger,trigger=trigger)
        result=self.engine.create(intent,market.size_decimals,market.price_decimals,stop_quantity=intent.quantity)
        return OrderRef(coi,result['order_index'],'SL',float(intent.trigger_price),float(intent.quantity),position_side)

    def place_take_profit(self,market,symbol,position_side,qty,trigger,coi):
        self._write(); self._bind(coi,symbol,position_side,'TP')
        old=self.engine.store.load(coi)
        # Preserve the existing passive 2R target; never reinterpret a rejected
        # or unknown post-only result as an accepted TP or submit a second order.
        intent=old[0] if old else Intent(self.transport.account_index,market.market_id,coi,
            _is_ask(position_side,True),True,str(self.quantity(market,qty)),'limit','post-only',
            str(price_tick(number(trigger),market.price_decimals,upward=position_side=='LONG')))
        result=self.engine.create(intent,market.size_decimals,market.price_decimals,resting=True)
        if result['terminal'] and number(result['filled_qty'])!=number(intent.quantity):
            raise UnverifiedExecution('TP not resting or fully filled; keep SL and halt')
        return OrderRef(coi,result['order_index'],'TP',float(intent.price),float(intent.quantity),position_side)

    def modify_stop(self,market,ref,qty,trigger):
        self._write()
        old,_=self.engine.store.load(ref.client_order_index)
        side='LONG' if old.is_ask else 'SHORT'
        replacement=self._intent(market,side,qty,ref.client_order_index,closing=True,kind='stop-loss',price=trigger,trigger=trigger)
        self.engine.amend_stop(ref.client_order_index,replacement,market.size_decimals,market.price_decimals)

    def cancel(self,market,ref):
        self._write(); self.engine.cancel(ref.client_order_index)

    def account_state(self):
        bindings=self._bindings(); results={}; expected={}; tp_done={}; costs={}
        for coi,b in bindings.items():
            stored=self.engine.store.load(coi)
            if stored is None: continue  # persisted binding before reservation; no send yet
            intent,old=stored
            if old and old['terminal']: result=old
            else:
                row=self.transport.order(coi)
                if row is None: raise UnverifiedExecution('owned order outcome absent')
                result=self.engine.store.record(coi,row)
            results[coi]=result
            key=intent.market_index
            expected[key]=expected.get(key,Decimal(0))+number(result['filled_qty'])*(-1 if intent.is_ask else 1)
        self.ledger.collect(self.transport,int(self.clock()*1000))
        report=self.ledger.report(); verified=True
        trades=self.ledger.events('trade')
        if any(r['coi'] not in bindings for r in trades): verified=False
        for coi,result in results.items():
            b=bindings[coi]; intent=self.engine.store.load(coi)[0]
            if any(r['market_id']!=intent.market_index or r['is_ask']!=intent.is_ask
                   for r in trades if r['coi']==coi):
                raise UnverifiedExecution('trade side/market differs from owned intent')
            totals=self.ledger.order_totals(coi,result['order_index'])
            if totals['qty']!=number(result['filled_qty']) or totals['quote']!=number(result['filled_quote']): verified=False
            if b['role']=='TP' and result['status']=='filled' and totals['qty']==number(intent.quantity):
                tp_done[b['cycle']]=True
            costs[str(coi)]={'symbol':b['symbol'],'role':b['role'],'cycle':b['cycle'],
                            'filled_qty':str(totals['qty']),'filled_quote':str(totals['quote']),
                            'fee_usdc':str(totals['fee_usdc']) if totals['fee_usdc'] is not None else None}
        account=self.transport.account(); actual=positions(account)
        if {k:v for k,v in expected.items() if v}!=actual: verified=False
        active=self.transport.active_orders(); active_map={}
        for row in active:
            coi=row.get('client_order_index'); b=bindings.get(coi)
            if b is None: raise UnverifiedExecution('unowned active order; refuse to manage it')
            intent,_=self.engine.store.load(coi)
            verify_order(row,intent)
            purpose=b['role']
            if purpose=='SL':
                qty=abs(actual.get(intent.market_index,Decimal(0)))
                if number(qty)>0: verify_stop(row,intent,str(qty))
            _,m=self.by_id[intent.market_index]
            active_map.setdefault(m.symbol,[]).append({'client_order_index':coi,'purpose':purpose,
                'trigger':float(intent.trigger_price),'qty':float(intent.quantity)})
        out={}; maintenance=Decimal(0)
        for key,p in actual.items():
            if key not in self.by_id: raise UnverifiedExecution('position outside strategy universe')
            symbol,m=self.by_id[key]
            raw=next(r for r in account['positions'] if r['market_id']==key)
            maintenance+=abs(signed(raw['position_value']))*number(m.maintenance_margin_fraction)
            cycle=self._cycle(symbol) if any(b['symbol']==symbol and b['role']=='ENTRY' for b in bindings.values()) else None
            out[m.symbol]={'side':'LONG' if p>0 else 'SHORT','qty':float(abs(p)),'entry':float(number(raw['avg_entry_price'])),
                           'partial_taken':tp_done.get(cycle,False)}
        report.update(order_roles=costs,execution_changes_verified=verified,
                      funding_attributions=self.ledger.attribute_funding(bindings),
                      funding_scope='settled account cash changes attributed to owned holding intervals')
        self.last_report=report
        return AccountState(float(number(account['total_asset_value'])),float(maintenance),out,active_map,
                            verified,True,report)

    def poll(self):
        data=self.transport.get('/api/v1/orderBookDetails',private=False)
        rows={r['market_id']:r for r in data['order_book_details'] if r.get('market_type')=='perp'}
        now=int(self.clock()*1000)
        values={s:float(number(rows[m.market_id]['mark_price'])) for s,m in self.markets.items()}
        if any(v<=0 for v in values.values()): raise UnverifiedExecution('invalid mark')
        self._marks=values; self._mark_sample_ms={s:now for s in values}
        self.account_state()
        return []

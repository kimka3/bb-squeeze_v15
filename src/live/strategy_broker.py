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
from .execution_evidence import Intent, NotSubmitted, UnverifiedExecution, number, verify_order, verify_stop
from .live_check import price_tick
from .request_budget import RequestDeferred


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
        self.entry_guard=None
        self.initial_allocated_equity=None
        self._entry_state=None
        self.entry_deadline_ms=None

    def close(self):
        self.ledger.close(); self.engine.close()

    def _write(self):
        self._entry_state=None
        if not self.writes_enabled: raise RuntimeError('strategy writes disabled')

    def offer_entry_state(self, state):
        # Only the immediately preceding reconciled snapshot may be handed to
        # the entry adapter. Never cache across orders, cycles or restarts.
        self._entry_state=(self.clock(),state)

    def clear_entry_state(self):
        self._entry_state=None

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
        if self.entry_guard is not None and not self.entry_guard():
            raise ValueError('operator or operational guard has paused new entries')
        q=self.quantity(market,qty)
        if market.rejects(float(q),price): raise ValueError('entry is below exchange minimum')
        if side=='SHORT' and tp_fraction>0:
            half=self.quantity(market,q*number(tp_fraction))
            if market.rejects(float(half),tp_price) or market.rejects(float(q-half),tp_price):
                raise ValueError('entry cannot support configured partial TP at exchange minimums')
        if hasattr(self.transport,'budget'):
            from .request_budget import RequestDeferred, EntryDeferred
            try: self.transport.budget.check(24)
            except RequestDeferred as exc:
                raise EntryDeferred(exc) from None

    def _intent(self,market,side,qty,coi,*,closing,kind,price,trigger='0',tif='immediate-or-cancel'):
        if side not in ('LONG','SHORT'): raise ValueError('invalid position side')
        ask=_is_ask(side,closing)
        bound=number(price)*(1-self.slippage if ask else 1+self.slippage)
        return Intent(self.transport.account_index,market.market_id,coi,ask,closing,
            str(self.quantity(market,qty)),kind,tif,
            str(price_tick(bound,market.price_decimals,upward=not ask)),
            str(price_tick(number(trigger),market.price_decimals,upward=side=='LONG')))

    @staticmethod
    def _not_sent(exc):
        if isinstance(exc,NotSubmitted): raise exc
        raise NotSubmitted('request_budget_deferred' if isinstance(exc,RequestDeferred)
                           else 'pre_submission_check_failed',
                           retryable=isinstance(exc,RequestDeferred),cause=exc) from None

    def _verified_exposure(self,market,side,qty,*,flat_allowed=False):
        # Always refresh before a new reducing mutation/retry. A clean market
        # remains manageable when another market has an unknown owned order.
        state=self.account_state()
        if market.symbol not in state.ledger_report.get('verified_symbols',[]):
            raise UnverifiedExecution('market owned exposure is not reconciled')
        actual=state.positions.get(market.symbol)
        if actual is None and flat_allowed: return state
        if (actual is None or actual['side']!=side or
                number(qty)>number(actual['qty'])):
            raise UnverifiedExecution('reduce-only order exceeds confirmed owned exposure')
        return state

    def _retryable_not_sent(self,coi):
        if not self.engine.store.known_not_submitted(coi): return False
        details=self.engine.store.submission_details(coi)
        if not details['retryable']:
            raise NotSubmitted(details['reason'],retryable=False)
        return True

    def _create_order(self,intent,market,**options):
        try:
            return self.engine.create(intent,market.size_decimals,market.price_decimals,**options)
        except NotSubmitted:
            raise
        except Exception as exc:
            if (self.engine.store.load(intent.client_order_index) is None or
                    self.engine.store.known_not_submitted(intent.client_order_index)):
                self._not_sent(exc)
            raise

    def _check_call_identity(self,market,symbol,side,coi,role,qty=None):
        if (side not in ('LONG','SHORT') or role not in ('ENTRY','EXIT','SL','TP')
                or symbol not in self.markets or self.markets[symbol].market_id!=market.market_id
                or self.markets[symbol].symbol!=market.symbol):
            raise UnverifiedExecution('strategy symbol/market identity mismatch')
        binding=self._bindings().get(coi)
        old=self.engine.store.load(coi)
        if binding and (binding['symbol']!=symbol or binding['side']!=side or binding['role']!=role):
            raise UnverifiedExecution('order role/cycle changed')
        if old:
            intent=old[0]
            if (not binding or intent.account_index!=self.transport.account_index or
                    intent.market_index!=market.market_id or
                    intent.is_ask!=_is_ask(side,role!='ENTRY') or
                    intent.reduce_only!=(role!='ENTRY') or
                    (qty is not None and number(intent.quantity)!=self.quantity(market,qty))):
                raise UnverifiedExecution('existing intent has different ownership or terms')
        return old

    def held_symbols_at(self,cutoff_ms):
        """Owned exposure at the signal boundary, from persisted fills only.

        A stop filling after the boundary cannot make that symbol eligible for
        re-entry on the same closed bar. An exactly coincident fill has no
        provable ordering relative to the boundary, so retain that symbol.
        No market/account/history network request is made here.
        """
        if type(cutoff_ms) is not int or cutoff_ms<0:
            raise ValueError('signal boundary must be a nonnegative millisecond timestamp')
        bindings=self._bindings(); totals={}; coincident=set(); checked={}
        for trade in self.ledger.events('trade'):
            coi=trade['coi']; binding=bindings.get(coi)
            if binding is None: raise UnverifiedExecution('boundary fill lacks owned binding')
            symbol=binding.get('symbol')
            if symbol not in self.markets: raise UnverifiedExecution('boundary binding market is unknown')
            cycle=bindings.get(binding.get('cycle'))
            if (not cycle or cycle.get('role')!='ENTRY' or cycle.get('symbol')!=symbol
                    or cycle.get('side')!=binding.get('side')
                    or (binding.get('role')=='ENTRY' and binding.get('cycle')!=coi)):
                raise UnverifiedExecution('boundary fill cycle identity differs')
            if coi not in checked:
                stored=self._check_call_identity(self.markets[symbol],symbol,binding.get('side'),
                                                coi,binding.get('role'))
                if stored is None: raise UnverifiedExecution('boundary fill lacks durable intent')
                checked[coi]=stored
            intent,result=checked[coi]
            if (self.engine.store.known_not_submitted(coi) or trade['market_id']!=intent.market_index
                    or trade['is_ask']!=intent.is_ask or
                    (result is not None and trade['order_index']!=result['order_index'])):
                raise UnverifiedExecution('boundary fill conflicts with owned intent')
            stamp=trade['timestamp_ms']
            if type(stamp) is not int or stamp<0:
                raise UnverifiedExecution('boundary fill timestamp is invalid')
            if stamp==cutoff_ms: coincident.add(symbol)
            if stamp<cutoff_ms:
                totals[symbol]=totals.get(symbol,Decimal(0))+number(trade['qty'])*(-1 if intent.is_ask else 1)
        return {symbol for symbol,qty in totals.items() if qty}|coincident

    def market_order(self,market,symbol,position_side,qty,closing,coi,ref_price):
        offered=self._entry_state
        self._write()
        role='EXIT' if closing else 'ENTRY'
        existing=self._check_call_identity(market,symbol,position_side,coi,role,qty)
        retry=False
        if existing is None or (closing and self.engine.store.known_not_submitted(coi)):
            try:
                retry=closing and self._retryable_not_sent(coi)
                if closing:
                    self._verified_exposure(market,position_side,qty)
                else:
                    if self.entry_guard is not None and not self.entry_guard():
                        raise NotSubmitted('new_entries_paused')
                    state=(offered[1] if offered is not None and 0<=self.clock()-offered[0]<=1
                           else self.account_state())
                    if not state.execution_changes_verified:
                        raise UnverifiedExecution('entry requires reconciled owned exposure')
                    if state.positions.get(market.symbol) is not None:
                        raise UnverifiedExecution('new entry into an existing position refused')
                    if state.active_orders.get(market.symbol):
                        raise UnverifiedExecution('old active orders must be resolved before re-entry')
                    if hasattr(self.transport,'budget'):
                        self.transport.budget.check(20)
                    if self.entry_deadline_ms is not None and self.clock()*1000>=self.entry_deadline_ms:
                        raise NotSubmitted('entry_deadline_elapsed')
                self._bind(coi,symbol,position_side,role)
            except Exception as exc:
                self._not_sent(exc)
        try:
            intent=existing[0] if existing else self._intent(market,position_side,qty,coi,closing=closing,kind='market',price=ref_price)
        except Exception as exc:
            if existing is None: self._not_sent(exc)
            raise
        self.transport.new_entry_deadline_ms=self.entry_deadline_ms if not closing else None
        try:
            self._create_order(intent,market,retry_not_submitted=retry)
        finally:
            self.transport.new_entry_deadline_ms=None
        return self.market_order_result(coi)

    def market_order_result(self,coi):
        binding=self._bindings().get(coi)
        if not binding: return None
        stored=self.engine.store.load(coi)
        if stored and binding['role']=='ENTRY' and stored[1] is not None:
            intent,prior=stored
            if not prior['terminal'] and number(prior['filled_qty'])>0:
                # A verified partial fill is already exposure. Resolve only
                # this owned entry's remaining order before handing the exact
                # terminal filled quantity to the Trader for immediate SL.
                # The engine re-reads and validates the row, and an uncertain
                # cancellation is queried rather than submitted again.
                self._write()
                self._check_call_identity(self.markets[binding['symbol']],binding['symbol'],
                                          binding['side'],coi,'ENTRY')
                try:
                    detail=self.engine.mutation_submission_details(f'cancel:{coi}')
                    retry=bool(detail and detail['state']=='not_submitted' and detail['retryable'])
                    # The engine freshly verifies this exact owned entry row.
                    # Only positive local non-dispatch allows retrying the
                    # original cancellation; unknown cancellations stay queries.
                    self.engine.cancel(coi,retry_not_submitted=retry)
                except NotSubmitted as exc:
                    # This proof concerns the CANCEL attempt, not the already
                    # partially filled ENTRY. Never let callers clear the entry
                    # pending operation as though no entry was dispatched.
                    raise UnverifiedExecution('partial entry remainder cancellation unconfirmed') from exc
        result=self.engine.recover(coi)
        # A fill can precede publication of trade history. Protect it promptly;
        # leave the fee unknown and let reconciliation block further entries.
        totals=self.ledger.order_totals(coi,result['order_index'])
        fee=totals['fee_usdc'] if totals['qty']==number(result['filled_qty']) else None
        return Fill(binding['symbol'],binding['side'],float(result['filled_qty']),
                    float(result['average_price'] or 0),binding['role'],float(fee) if fee is not None else None)

    def place_stop(self,market,symbol,position_side,qty,trigger,coi):
        self._write(); self._bind(coi,symbol,position_side,'SL')
        old=self._check_call_identity(market,symbol,position_side,coi,'SL',qty)
        retry=self._retryable_not_sent(coi)
        if retry:
            try: self._verified_exposure(market,position_side,qty)
            except Exception as exc: self._not_sent(exc)
        intent=old[0] if old else self._intent(market,position_side,qty,coi,closing=True,kind='stop-loss',price=trigger,trigger=trigger)
        result=self._create_order(intent,market,stop_quantity=intent.quantity,retry_not_submitted=retry)
        return OrderRef(coi,result['order_index'],'SL',float(intent.trigger_price),float(intent.quantity),position_side,
                        order_expiry_ms=result.get('order_expiry_ms'))

    def place_take_profit(self,market,symbol,position_side,qty,trigger,coi):
        self._write(); self._bind(coi,symbol,position_side,'TP')
        old=self._check_call_identity(market,symbol,position_side,coi,'TP',qty)
        retry=self._retryable_not_sent(coi)
        if retry:
            try: self._verified_exposure(market,position_side,qty)
            except Exception as exc: self._not_sent(exc)
        # Preserve the existing passive 2R target; never reinterpret a rejected
        # or unknown post-only result as an accepted TP or submit a second order.
        intent=old[0] if old else Intent(self.transport.account_index,market.market_id,coi,
            _is_ask(position_side,True),True,str(self.quantity(market,qty)),'limit','post-only',
            str(price_tick(number(trigger),market.price_decimals,upward=position_side=='LONG')))
        result=self._create_order(intent,market,resting=True,retry_not_submitted=retry)
        if result['terminal'] and number(result['filled_qty'])!=number(intent.quantity):
            raise UnverifiedExecution('TP not resting or fully filled; keep SL and halt')
        return OrderRef(coi,result['order_index'],'TP',float(intent.price),float(intent.quantity),position_side,
                        order_expiry_ms=result.get('order_expiry_ms'))

    def modify_stop(self,market,ref,qty,trigger):
        self._write()
        old,_=self.engine.store.load(ref.client_order_index)
        side='LONG' if old.is_ask else 'SHORT'
        binding=self._bindings().get(ref.client_order_index)
        if not binding or binding['role']!='SL': raise UnverifiedExecution('stop amendment lacks owned binding')
        self._check_call_identity(market,binding['symbol'],side,ref.client_order_index,'SL')
        replacement=self._intent(market,side,qty,ref.client_order_index,closing=True,kind='stop-loss',price=trigger,trigger=trigger)
        mid=f'amend:{ref.client_order_index}:{replacement.quantity}:{replacement.trigger_price}:{replacement.price}'
        details=self.engine.mutation_submission_details(mid)
        retry=bool(details and details['state']=='not_submitted')
        if retry:
            if not details['retryable']: raise NotSubmitted(details['reason'])
            try: self._verified_exposure(market,side,qty)
            except Exception as exc: self._not_sent(exc)
        self.engine.amend_stop(ref.client_order_index,replacement,market.size_decimals,market.price_decimals,
                              retry_not_submitted=retry)

    def cancel(self,market,ref):
        self._write()
        binding=self._bindings().get(ref.client_order_index)
        if not binding: raise UnverifiedExecution('cancel lacks owned binding')
        self._check_call_identity(market,binding['symbol'],binding['side'],ref.client_order_index,binding['role'])
        details=self.engine.mutation_submission_details(f'cancel:{ref.client_order_index}')
        retry=bool(details and details['state']=='not_submitted')
        if retry:
            if not details['retryable']: raise NotSubmitted(details['reason'])
            try: self._verified_exposure(market,binding['side'],0,flat_allowed=True)
            except Exception as exc: self._not_sent(exc)
        self.engine.cancel(ref.client_order_index,retry_not_submitted=retry)

    def account_state(self):
        # REST order/history/account reads are not an atomic snapshot. Only a
        # mismatch between otherwise owned, verified records gets one fresh
        # reconciliation. Unknown submissions and identity/schema failures do
        # not qualify; exceptions, including budget cooldown, propagate.
        self._entry_state=None
        for attempt in (1,2):
            state,read_mismatch=self._account_state_once()
            state.ledger_report['reconciliation_read_attempts']=attempt
            self.last_report=state.ledger_report
            if not read_mismatch or attempt==2: return state

    def _account_state_once(self):
        self._entry_state=None
        unresolved_amendments=self.engine.reconcile_amendments(tolerate_absent=True)
        active=self.transport.active_orders()
        active_by_coi={}
        for row in active:
            coi=row.get('client_order_index') if isinstance(row,dict) else None
            if type(coi) is not int or coi<0 or coi in active_by_coi:
                raise UnverifiedExecution('active order identity missing or duplicated')
            active_by_coi[coi]=row
        bindings=self._bindings(); results={}; intents={}; expected={}; tp_done={}; costs={}
        unresolved={}; not_submitted={}; unverified_markets=set(); read_mismatches=set()
        unknown_ownership=False
        def diagnostic(coi,reason,**extra):
            b=bindings[coi]
            return {'symbol':b['symbol'],'role':b['role'],'cycle':b['cycle'],'reason':reason,**extra}
        for coi,b in bindings.items():
            if (b.get('symbol') not in self.markets or b.get('side') not in ('LONG','SHORT')
                    or b.get('role') not in ('ENTRY','EXIT','SL','TP')):
                raise UnverifiedExecution('owned binding identity is malformed')
            stored=self.engine.store.load(coi)
            if stored is None: continue  # persisted binding before reservation; no send yet
            intent,old=stored
            self._check_call_identity(self.markets[b['symbol']],b['symbol'],b['side'],coi,b['role'])
            cycle=bindings.get(b.get('cycle'))
            if (not cycle or cycle.get('role')!='ENTRY' or cycle.get('symbol')!=b['symbol']
                    or cycle.get('side')!=b['side'] or (b['role']=='ENTRY' and b['cycle']!=coi)):
                raise UnverifiedExecution('owned order cycle identity differs')
            intents[coi]=intent
            row=active_by_coi.get(coi)
            if row is None and not (old and old['terminal']): row=self.transport.order(coi)
            if self.engine.store.known_not_submitted(coi):
                if row is not None:
                    raise UnverifiedExecution('exchange order conflicts with confirmed local non-submission')
                detail=self.engine.store.submission_details(coi)
                not_submitted[str(coi)]=diagnostic(coi,detail['reason'],retryable=detail['retryable'])
                continue
            if row is not None:
                result=self.engine.store.record(coi,row)
            elif old and old['terminal']:
                result=old
            else:
                unresolved[str(coi)]=diagnostic(coi,'owned_order_outcome_absent')
                if b['role'] in ('ENTRY','EXIT'): unverified_markets.add(intent.market_index)
                # Preserve observed cumulative fills, not an assumption about
                # the currently missing row. Fresh ledger/account equality is
                # still required before this market can be reduced safely.
                if old is None: continue
                result=old
            if (b['role'] in ('ENTRY','EXIT') and not result['terminal']):
                unresolved[str(coi)]=diagnostic(coi,'market_order_not_terminal')
                unverified_markets.add(intent.market_index)
            if coi in unresolved_amendments:
                unresolved[str(coi)]=diagnostic(coi,'stop_amendment_unconfirmed')
            results[coi]=result
            key=intent.market_index
            expected[key]=expected.get(key,Decimal(0))+number(result['filled_qty'])*(-1 if intent.is_ask else 1)
        for coi in unresolved_amendments:
            if coi not in bindings or coi not in intents:
                raise UnverifiedExecution('amendment lacks owned intent')
            unresolved[str(coi)]=diagnostic(coi,'stop_amendment_unconfirmed')
        # A missing cancel acknowledgement is still unresolved even while the
        # original order remains visible. Never infer cancellation from absence.
        for mid,encoded in self.db.execute('SELECT id,value FROM mutations WHERE complete=0'):
            if not mid.startswith('cancel:'): continue
            coi=int(mid.split(':',1)[1])
            if coi not in bindings or coi not in intents:
                raise UnverifiedExecution('cancellation lacks owned intent')
            detail=self.engine.mutation_submission_details(mid)
            if detail['state']=='not_submitted': continue
            if not results.get(coi,{}).get('terminal'):
                unresolved[str(coi)]=diagnostic(coi,'cancel_outcome_unconfirmed')
        self.ledger.collect(self.transport,int(self.clock()*1000))
        report=self.ledger.report()
        trades=self.ledger.events('trade')
        grouped={}
        for trade in trades:
            grouped.setdefault(trade['coi'],[]).append(trade)
            if trade['coi'] not in intents:
                unverified_markets.add(trade['market_id'])
                unknown_ownership=True
        confirmed_entries=set()
        for coi,intent in intents.items():
            b=bindings[coi]; result=results.get(coi); rows=grouped.get(coi,[])
            if any(r['market_id']!=intent.market_index or r['is_ask']!=intent.is_ask for r in rows):
                raise UnverifiedExecution('trade side/market differs from owned intent')
            if str(coi) in not_submitted and rows:
                raise UnverifiedExecution('trade conflicts with confirmed local non-submission')
            if result and any(r['order_index']!=result['order_index'] for r in rows):
                raise UnverifiedExecution('trade/exchange order identity mismatch')
            totals={'qty':sum((number(r['qty']) for r in rows),Decimal(0)),
                    'quote':sum((number(r['quote']) for r in rows),Decimal(0)),
                    'fee_usdc':sum((signed(r['fee_usdc']) for r in rows),Decimal(0))
                        if all(r['fee_usdc'] is not None for r in rows) else None}
            matches=(result is not None and totals['qty']==number(result['filled_qty'])
                     and totals['quote']==number(result['filled_quote']))
            if result is not None and not matches:
                unverified_markets.add(intent.market_index)
                read_mismatches.add(intent.market_index)
            if result is None and rows:
                unverified_markets.add(intent.market_index)
                unknown_ownership=True
            if b['role']=='ENTRY' and matches and result['terminal'] and totals['qty']>0:
                confirmed_entries.add(coi)
            if b['role']=='TP' and matches and result['status']=='filled' and totals['qty']==number(intent.quantity):
                tp_done[b['cycle']]=True
            costs[str(coi)]={'symbol':b['symbol'],'role':b['role'],'cycle':b['cycle'],
                            'filled_qty':str(totals['qty']),'filled_quote':str(totals['quote']),
                            'fee_usdc':str(totals['fee_usdc']) if totals['fee_usdc'] is not None else None}
        account=self.transport.account(); actual=positions(account)
        for key in set(expected)|set(actual):
            if expected.get(key,Decimal(0))!=actual.get(key,Decimal(0)):
                unverified_markets.add(key)
                read_mismatches.add(key)
        for coi,item in unresolved.items():
            if item['role'] in ('SL','TP') and item['cycle'] not in confirmed_entries:
                unverified_markets.add(intents[int(coi)].market_index)
        active_map={}; unsafe_protection={}
        for row in active:
            coi=row.get('client_order_index'); b=bindings.get(coi)
            if b is None: raise UnverifiedExecution('unowned active order; refuse to manage it')
            stored=self.engine.store.load(coi)
            if stored is None: raise UnverifiedExecution('active order lacks durable intent')
            intent,_=stored
            evidence=verify_order(row,intent)
            purpose=b['role']
            armed=True
            if purpose=='SL':
                qty=abs(actual.get(intent.market_index,Decimal(0)))
                # A valid owned order may already be partially triggered or
                # awaiting a parent instead of protecting the residual. Expose
                # this per symbol without blocking independently owned exits.
                # Malformed conditional-order evidence remains a hard failure.
                if row.get('trigger_status') not in ('na','ready','mark-price','parent-order'):
                    raise UnverifiedExecution('invalid stop trigger status')
                for field in ('trigger_time','parent_order_index'):
                    if (field in row or row.get('status')=='pending') and (
                            type(row.get(field)) is not int or row[field]<0):
                        raise UnverifiedExecution(f'invalid stop conditional field: {field}')
                if number(qty)>0:
                    expiry=evidence.get('order_expiry_ms')
                    reason=('stop_expired' if expiry is not None and expiry<=int(self.clock()*1000) else None)
                    try:
                        verify_stop(row,intent,str(qty))
                    except UnverifiedExecution:
                        reason=reason or ('stop_partially_filled' if number(evidence['filled_qty'])>0
                                         else 'stop_not_confirmed_armed_for_residual')
                    if reason is not None:
                        armed=False
                        unsafe_protection[str(coi)]=diagnostic(coi,reason,
                            client_order_index=coi,order_index=evidence['order_index'],
                            position_side=b['side'],qty=float(intent.quantity),
                            remaining_qty=float(number(evidence['remaining_qty'])),
                            trigger=float(intent.trigger_price),
                            order_expiry_ms=evidence.get('order_expiry_ms'))
                else:
                    armed=False
            _,m=self.by_id[intent.market_index]
            active_map.setdefault(m.symbol,[]).append({'client_order_index':coi,'purpose':purpose,
                'trigger':float(intent.trigger_price),'qty':float(intent.quantity),
                'order_index':evidence['order_index'],'position_side':b['side'],'armed':armed,
                'order_expiry_ms':evidence.get('order_expiry_ms')})
        out={}; maintenance=Decimal(0)
        for key,p in actual.items():
            if key not in self.by_id: raise UnverifiedExecution('position outside strategy universe')
            symbol,m=self.by_id[key]
            raw=next(r for r in account['positions'] if r['market_id']==key)
            maintenance+=abs(signed(raw['position_value']))*number(m.maintenance_margin_fraction)
            cycle=self._cycle(symbol) if any(b['symbol']==symbol and b['role']=='ENTRY' for b in bindings.values()) else None
            out[m.symbol]={'side':'LONG' if p>0 else 'SHORT','qty':float(abs(p)),'entry':float(number(raw['avg_entry_price'])),
                           'partial_taken':tp_done.get(cycle,False)}
        verified_symbols=sorted(m.symbol for _,m in self.by_id.values() if m.market_id not in unverified_markets)
        pending_protection=any(item['role'] in ('SL','TP') and actual.get(intents[int(coi)].market_index)
                               for coi,item in not_submitted.items())
        verified=not unresolved and not unverified_markets and not pending_protection and not unsafe_protection
        report.update(order_roles=costs,execution_changes_verified=verified,
                      unresolved_orders=unresolved,not_submitted_orders=not_submitted,
                      unsafe_protection=unsafe_protection,
                      verified_symbols=verified_symbols,
                      funding_attributions=self.ledger.attribute_funding(bindings),
                      funding_scope='settled account cash changes attributed to owned holding intervals')
        equity=number(account['total_asset_value'])
        if self.initial_allocated_equity is not None:
            # Profit can compound, but an unrelated deposit must not silently
            # increase the user's authorized starting allocation. Unknown fees
            # cannot inflate sizing beyond the actual current account equity.
            cash=sum((number(r['quote'])*(1 if r['is_ask'] else -1)
                      for r in trades if r['coi'] in bindings),Decimal(0))
            holding=sum((abs(signed(r['position_value']))*(1 if r['sign']==1 else -1)
                         for r in account['positions'] if number(r['position'])>0),Decimal(0))
            funding=sum((signed(r['cash_change']) for r in report['funding_attributions']
                         if r['cycle'] is not None),Decimal(0))
            allocated=max(Decimal(0),number(self.initial_allocated_equity)+cash+holding+funding)
            report.update(actual_account_equity=str(equity),allocated_equity=str(min(equity,allocated)),
                          allocation_scope='initial approved equity plus owned trading and settled funding; capped by actual equity')
            equity=min(equity,allocated)
        candidate=(bool(read_mismatches) and not unknown_ownership and not unresolved
                   and unverified_markets<=read_mismatches)
        return (AccountState(float(equity),float(maintenance),out,active_map,
                             verified,True,report),candidate)

    def poll(self):
        data=self.transport.get('/api/v1/orderBookDetails')
        rows={r['market_id']:r for r in data['order_book_details'] if r.get('market_type')=='perp'}
        now=int(self.clock()*1000)
        values={s:float(number(rows[m.market_id]['mark_price'])) for s,m in self.markets.items()}
        if any(v<=0 for v in values.values()): raise UnverifiedExecution('invalid mark')
        self._marks=values; self._mark_sample_ms={s:now for s in values}
        return []

    def stop_amendment_confirmed(self,coi,qty,trigger):
        intent,result=self.engine.store.load(coi)
        _,market=self.by_id[intent.market_index]
        expected=price_tick(number(trigger),market.price_decimals,upward=intent.is_ask)
        return (result is not None and not result['terminal'] and
                number(intent.trigger_price)==expected and
                number(intent.quantity)==self.quantity(market,qty))

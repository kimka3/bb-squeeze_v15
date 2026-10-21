"""Account history accounting. Missing fee evidence remains unknown.

Raw trade fee fields have no documented currency scale in the SDK model. Only
explicit zero fee components are converted; nonzero/missing values are retained
and make performance incomplete rather than invented or estimated.
"""
from decimal import Decimal, InvalidOperation
import json
import sqlite3
from urllib.parse import urlencode

from .execution_evidence import UnverifiedExecution, number


def signed(value):
    if value is None or isinstance(value,bool):
        raise UnverifiedExecution('missing signed amount')
    try: n=Decimal(str(value))
    except InvalidOperation: raise UnverifiedExecution('invalid signed amount') from None
    if not n.is_finite(): raise UnverifiedExecution('nonfinite signed amount')
    return n


def integer(value):
    if type(value) is not int or value<0:
        raise UnverifiedExecution('invalid history identity/timestamp')
    return value


def history(transport, kind, start_ms, end_ms, *, page_limit=100):
    """Complete descending pages through the lower bound, with cursor checks.

Trade timestamps are milliseconds; positionFunding timestamps are seconds.
Bounds are applied locally as well as on funding requests. A truncated fetch
raises and never replaces a previous successful reconciliation.
"""
    if kind not in ('trades','funding') or not 0<=start_ms<=end_ms:
        raise ValueError('invalid history scope')
    key='trades' if kind=='trades' else 'position_fundings'
    endpoint='/api/v1/trades' if kind=='trades' else '/api/v1/positionFunding'
    params={'account_index':transport.account_index,'limit':100}
    if kind=='trades': params.update(sort_by='timestamp',sort_dir='desc',aggregate='false')
    else:
        # Same-second bounds produce HTTP 400 on the observed API. Pad the
        # server window and filter locally, preserving exact inclusive scope
        # even when the provider treats a timestamp boundary as exclusive.
        params.update(start_timestamp=max(0,start_ms//1000-1),end_timestamp=end_ms//1000+1)
    cursor=None; seen=set(); rows=[]; previous=None
    for _ in range(page_limit):
        data=transport.get(endpoint+'?'+urlencode({**params,**({'cursor':cursor} if cursor else {})}))
        page=data.get(key)
        if not isinstance(page,list): raise UnverifiedExecution('history list missing')
        crossed=False
        for row in page:
            stamp=integer(row.get('timestamp'))*(1 if kind=='trades' else 1000)
            if previous is not None and stamp>previous:
                raise UnverifiedExecution('history is not descending')
            previous=stamp
            if stamp<start_ms: crossed=True
            if start_ms<=stamp<=end_ms: rows.append(row)
        nxt=data.get('next_cursor')
        if nxt is not None and not isinstance(nxt,str): raise UnverifiedExecution('invalid history cursor')
        if crossed or not nxt: return rows
        if not page or nxt in seen: raise UnverifiedExecution('history cursor stalled')
        seen.add(nxt); cursor=nxt
    raise UnverifiedExecution('history page limit reached; coverage incomplete')


def normalize_trade(row, account):
    ask=row.get('ask_account_id')==account
    bid=row.get('bid_account_id')==account
    if ask==bid: raise UnverifiedExecution('unrelated or self-trade cannot be attributed')
    if row.get('type')!='trade' or type(row.get('is_maker_ask')) is not bool:
        raise UnverifiedExecution('unsupported trade kind/role')
    side='ask' if ask else 'bid'
    role='maker' if row['is_maker_ask']==ask else 'taker'
    qty=number(row.get('size')); price=number(row.get('price')); quote=number(row.get('usd_amount'))
    if qty<=0 or price<=0 or abs(qty*price-quote)>Decimal('0.000001'):
        raise UnverifiedExecution('trade size/quote mismatch')
    fees={k:row.get(k) for k in (role+'_fee','integrator_'+role+'_fee')}
    known=all(v is not None and signed(v)==0 for v in fees.values())
    return {'trade_id':integer(row.get('trade_id')),'market_id':integer(row.get('market_id')),
            'coi':integer(row.get(side+'_client_id')),'order_index':integer(row.get(side+'_id')),
            'timestamp_ms':integer(row.get('timestamp')),'is_ask':ask,'liquidity_role':role,
            'qty':str(qty),'quote':str(quote),'price':str(price),'fee_usdc':'0' if known else None,
            'fee_evidence':fees,'realized_pnl':str(signed(row[side+'_account_pnl'])) if side+'_account_pnl' in row else None}


def normalize_funding(row):
    side=row.get('position_side')
    if side not in ('long','short'): raise UnverifiedExecution('unknown funding side')
    return {'funding_id':integer(row.get('funding_id')),'market_id':integer(row.get('market_id')),
            'timestamp_ms':integer(row.get('timestamp'))*1000,'cash_change':str(signed(row.get('change'))),
            'discount_raw':str(signed(row.get('discount'))),'position_size':str(number(row.get('position_size'))),
            'position_side':side,'rate':str(signed(row.get('rate')))}


class AccountLedger:
    def __init__(self,path,network,account,start_ms):
        self.account,self.start_ms=account,integer(start_ms)
        self.db=sqlite3.connect(path)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS ledger_identity (id INTEGER PRIMARY KEY,value TEXT)')
        identity=json.dumps([network,account,start_ms])
        old=self.db.execute('SELECT value FROM ledger_identity WHERE id=1').fetchone()
        if old and old[0]!=identity: raise UnverifiedExecution('ledger account/campaign mismatch')
        self.db.execute('INSERT OR IGNORE INTO ledger_identity VALUES(1,?)',(identity,))
        self.db.execute('CREATE TABLE IF NOT EXISTS events(kind TEXT,id TEXT,value TEXT,PRIMARY KEY(kind,id))')
        self.db.execute('CREATE TABLE IF NOT EXISTS history_coverage(id INTEGER PRIMARY KEY,end_ms INTEGER)')
        self.db.commit()
        from .history_collection import HistoryCollector
        self.collector=HistoryCollector(self)

    def close(self): self.db.close()

    def collect(self,transport,end_ms):
        return self.collector.collect(transport,end_ms)

    def _normalize_rows(self,trades,funding):
        normalized=[('trade',str(v['trade_id']),v) for v in [normalize_trade(r,self.account) for r in trades]]
        return normalized + [('funding',f"{v['market_id']}:{v['funding_id']}",v) for v in map(normalize_funding,funding)]

    def _store_rows(self,normalized,end_ms):
        # Caller owns the transaction, including any cursor checkpoint. A
        # correction must never silently overwrite previously recorded evidence.
        for kind,identity,value in normalized:
            if not self.start_ms<=value['timestamp_ms']<=end_ms: raise UnverifiedExecution('history outside scope')
            encoded=json.dumps(value,sort_keys=True)
            prior=self.db.execute('SELECT value FROM events WHERE kind=? AND id=?',(kind,identity)).fetchone()
            if prior and prior[0]!=encoded: raise UnverifiedExecution('history event changed')
            self.db.execute('INSERT OR IGNORE INTO events VALUES(?,?,?)',(kind,identity,encoded))

    def ingest(self,trades,funding,end_ms):
        old=self.db.execute('SELECT end_ms FROM history_coverage WHERE id=1').fetchone()
        if old and end_ms<old[0]: raise UnverifiedExecution('coverage regression')
        normalized=self._normalize_rows(trades,funding)
        with self.db:
            self._store_rows(normalized,end_ms)
            self.db.execute('INSERT OR REPLACE INTO history_coverage VALUES(1,?)',(end_ms,))

    def events(self,kind):
        return [json.loads(r[0]) for r in self.db.execute('SELECT value FROM events WHERE kind=?',(kind,))]

    def order_totals(self,coi,order_index=None):
        rows=[r for r in self.events('trade') if r['coi']==coi]
        if order_index is not None and any(r['order_index']!=order_index for r in rows):
            raise UnverifiedExecution('trade/exchange order identity mismatch')
        known=all(r['fee_usdc'] is not None for r in rows)
        return {'qty':sum((number(r['qty']) for r in rows),Decimal(0)),
                'quote':sum((number(r['quote']) for r in rows),Decimal(0)),
                'fee_usdc':sum((signed(r['fee_usdc']) for r in rows),Decimal(0)) if known else None}

    def report(self):
        trades=self.events('trade'); funding=self.events('funding')
        missing=sum(r['fee_usdc'] is None for r in trades)
        by_role={}
        for r in trades:
            group=by_role.setdefault(r['liquidity_role'],{'fills':0,'unknown_fee_fills':0})
            group['fills']+=1; group['unknown_fee_fills']+=r['fee_usdc'] is None
        return {'start_ms':self.start_ms,'fill_count':len(trades),'unknown_fee_fills':missing,
                'known_fee_usdc':str(sum((signed(r['fee_usdc']) for r in trades if r['fee_usdc'] is not None),Decimal(0))),
                'funding_count':len(funding),'funding_cash_change':str(sum((signed(r['cash_change']) for r in funding),Decimal(0))),
                'funding_discount_interpretation_verified':not any(signed(r['discount_raw']) for r in funding),
                'liquidity_roles':by_role,'performance_complete':False,
                'history_collection':self.collector.report(),
                'limitations':['fee fields missing or nonzero are not converted without verified units',
                               'funding cash changes are account records, not modeled market rates',
                               'equity transfers and realized PnL must reconcile before performance certification']}

    def attribute_funding(self,bindings):
        """Map actual cash changes only to an unambiguous owned holding cycle.

No allocation by current position, current price or market funding rate. A fill
at the same timestamp as settlement makes ordering ambiguous and stays unknown.
"""
        # One ordered pass per market. Re-reading/deserializing the complete
        # trade table for every settlement made a growing live ledger quadratic.
        # Keep exact timestamp ambiguity and unowned-fill rules unchanged.
        fundings=self.events('funding'); trades_by_market={}; funding_by_market={}
        for trade in self.events('trade'):
            trades_by_market.setdefault(trade['market_id'],[]).append(trade)
        for index,funding in enumerate(fundings):
            funding_by_market.setdefault(funding['market_id'],[]).append((index,funding))
        out=[None]*len(fundings)
        for market,settlements in funding_by_market.items():
            trades=sorted(trades_by_market.get(market,[]),key=lambda row:row['timestamp_ms'])
            held={}; cursor=0; unowned_seen=False; latest_trade_ms=None
            for index,funding in sorted(settlements,key=lambda item:item[1]['timestamp_ms']):
                while cursor<len(trades) and trades[cursor]['timestamp_ms']<=funding['timestamp_ms']:
                    trade=trades[cursor]; cursor+=1; latest_trade_ms=trade['timestamp_ms']
                    binding=bindings.get(trade['coi'])
                    if binding is None:
                        unowned_seen=True
                        continue
                    cycle=binding['cycle']
                    quantity=held.get(cycle,Decimal(0))+number(trade['qty'])*(-1 if trade['is_ask'] else 1)
                    if quantity: held[cycle]=quantity
                    else: held.pop(cycle,None)
                ambiguous=unowned_seen or latest_trade_ms==funding['timestamp_ms']
                candidates=[c for c,q in held.items() if ('long' if q>0 else 'short')==funding['position_side']]
                out[index]={**funding,'cycle':candidates[0] if len(candidates)==1 and not ambiguous else None}
        return out

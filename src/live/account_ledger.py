"""Account history accounting. Missing fee evidence remains unknown.

Raw trade fee fields have no documented currency scale in the SDK model. Only
explicit zero fee components are converted; nonzero/missing values are retained
and make performance incomplete rather than invented or estimated.
"""
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlencode

from .execution_evidence import UnverifiedExecution, number


class UnsupportedHistoryRecord(UnverifiedExecution):
    """A well-formed, attributable record whose economic meaning is unknown.

    This exception is deliberately narrower than a schema/identity failure.
    Keeping its raw evidence does not make it a supported fill or settlement.
    """
    def __init__(self, kind, identity, market_id, timestamp_ms, reason, unsupported_value, raw):
        self.issue = {'kind':kind, 'id':identity, 'market_id':market_id,
                      'timestamp_ms':timestamp_ms, 'reason':reason,
                      'unsupported_value':unsupported_value}
        try:
            self.raw = json.loads(json.dumps(raw, sort_keys=True, allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise UnverifiedExecution('invalid raw history record') from exc
        super().__init__(reason)


class HistoricalEvidenceBlocked(UnverifiedExecution):
    """Preserved unsupported account history blocks strict certification."""
    def __init__(self, issues):
        self.issues = issues
        super().__init__('unsupported account history remains quarantined')


def _history_object(row):
    if not isinstance(row, dict) or any(not isinstance(key, str) for key in row):
        raise UnverifiedExecution('invalid history row')


def _history_token(value, label):
    if not isinstance(value, str) or not value.strip():
        raise UnverifiedExecution('invalid '+label)
    return value


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


def _history_json(value):
    return json.dumps(value,sort_keys=True,allow_nan=False,separators=(',',':'))


def _history_hash(value):
    return hashlib.sha256(_history_json(value).encode('utf-8')).hexdigest()


def _digest(value):
    return isinstance(value,str) and len(value)==64 and all(c in '0123456789abcdef' for c in value)


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
    _history_object(row)
    ask=integer(row.get('ask_account_id'))==integer(account)
    bid=integer(row.get('bid_account_id'))==account
    if ask==bid: raise UnverifiedExecution('unrelated or self-trade cannot be attributed')
    kind=_history_token(row.get('type'),'trade kind')
    if type(row.get('is_maker_ask')) is not bool:
        raise UnverifiedExecution('invalid trade liquidity role')
    side='ask' if ask else 'bid'
    role='maker' if row['is_maker_ask']==ask else 'taker'
    qty=number(row.get('size')); price=number(row.get('price')); quote=number(row.get('usd_amount'))
    if qty<=0 or price<=0 or abs(qty*price-quote)>Decimal('0.000001'):
        raise UnverifiedExecution('trade size/quote mismatch')
    fees={k:row.get(k) for k in (role+'_fee','integrator_'+role+'_fee')}
    fee_values=[signed(v) if v is not None else None for v in fees.values()]
    known=all(v is not None and v==0 for v in fee_values)
    normalized={'trade_id':integer(row.get('trade_id')),'market_id':integer(row.get('market_id')),
            'coi':integer(row.get(side+'_client_id')),'order_index':integer(row.get(side+'_id')),
            'timestamp_ms':integer(row.get('timestamp')),'is_ask':ask,'liquidity_role':role,
            'qty':str(qty),'quote':str(quote),'price':str(price),'fee_usdc':'0' if known else None,
            'fee_evidence':fees,'realized_pnl':str(signed(row[side+'_account_pnl'])) if side+'_account_pnl' in row else None}
    if kind!='trade':
        raise UnsupportedHistoryRecord('trade',str(normalized['trade_id']),normalized['market_id'],
            normalized['timestamp_ms'],'unsupported_trade_type',kind,row)
    return normalized


def normalize_funding(row):
    _history_object(row)
    side=_history_token(row.get('position_side'),'funding side')
    normalized={'funding_id':integer(row.get('funding_id')),'market_id':integer(row.get('market_id')),
            'timestamp_ms':integer(row.get('timestamp'))*1000,'cash_change':str(signed(row.get('change'))),
            'discount_raw':str(signed(row.get('discount'))),'position_size':str(number(row.get('position_size'))),
            'position_side':side,'rate':str(signed(row.get('rate')))}
    if side not in ('long','short'):
        raise UnsupportedHistoryRecord('funding',f"{normalized['market_id']}:{normalized['funding_id']}",
            normalized['market_id'],normalized['timestamp_ms'],'unsupported_funding_side',side,row)
    return normalized


def resolution_candidates_readonly(path,network,account,start_ms):
    """Preview a running or older ledger without creating tables or files."""
    ledger=AccountLedger.__new__(AccountLedger)
    ledger.network,ledger.account,ledger.start_ms=network,integer(account),integer(start_ms)
    ledger.db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)
    try:
        ledger.db.execute('PRAGMA query_only=ON')
        # A single read snapshot prevents mixing a concurrently committed
        # receipt with its pre-commit event or quarantine state.
        ledger.db.execute('BEGIN')
        row=ledger.db.execute('SELECT value FROM ledger_identity WHERE id=1').fetchone()
        if row is None or row[0]!=json.dumps([network,account,start_ms]):
            raise UnverifiedExecution('ledger account/campaign mismatch')
        return ledger.resolution_candidates()
    finally:
        ledger.db.close()


class AccountLedger:
    def __init__(self,path,network,account,start_ms):
        self.network=network
        self.account,self.start_ms=account,integer(start_ms)
        self.db=sqlite3.connect(path)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS ledger_identity (id INTEGER PRIMARY KEY,value TEXT)')
        identity=json.dumps([network,account,start_ms])
        old=self.db.execute('SELECT value FROM ledger_identity WHERE id=1').fetchone()
        if old and old[0]!=identity: raise UnverifiedExecution('ledger account/campaign mismatch')
        self.db.execute('INSERT OR IGNORE INTO ledger_identity VALUES(1,?)',(identity,))
        self.db.execute('CREATE TABLE IF NOT EXISTS events(kind TEXT,id TEXT,value TEXT,PRIMARY KEY(kind,id))')
        self.db.execute('CREATE TABLE IF NOT EXISTS history_quarantine '
                        '(kind TEXT,id TEXT,value TEXT,PRIMARY KEY(kind,id))')
        self.db.execute('CREATE TABLE IF NOT EXISTS history_resolutions '
                        '(kind TEXT,id TEXT,value TEXT,PRIMARY KEY(kind,id))')
        self.db.execute('CREATE TABLE IF NOT EXISTS history_resolution_requests '
                        '(id TEXT PRIMARY KEY,value TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS history_coverage(id INTEGER PRIMARY KEY,end_ms INTEGER)')
        self.db.commit()
        from .history_collection import HistoryCollector
        self.collector=HistoryCollector(self)

    def close(self): self.db.close()

    def collect(self,transport,end_ms):
        return self.collector.collect(transport,end_ms)

    def _normalize_rows(self,trades,funding):
        normalized=[]; quarantined=[]
        retained=self._resolution_view()
        for kind,rows in (('trade',trades),('funding',funding)):
            for row in rows:
                _history_object(row)
                identity=(str(integer(row.get('trade_id'))) if kind=='trade' else
                          f"{integer(row.get('market_id'))}:{integer(row.get('funding_id'))}")
                saved=retained.get((kind,identity))
                if saved:
                    try:
                        same=_history_json(row)==_history_json(saved['saved']['raw'])
                    except (TypeError,ValueError) as exc:
                        raise UnverifiedExecution('invalid raw history record') from exc
                    if not same:
                        raise UnverifiedExecution('quarantined history event changed')
                try:
                    value=normalize_trade(row,self.account) if kind=='trade' else normalize_funding(row)
                except UnsupportedHistoryRecord as exc:
                    quarantined.append((kind,exc.issue['id'],{**exc.issue,'raw':exc.raw}))
                    continue
                identity=str(value['trade_id']) if kind=='trade' else f"{value['market_id']}:{value['funding_id']}"
                normalized.append((kind,identity,value))
        return normalized,quarantined

    def _store_rows(self,normalized,end_ms,quarantined=()):
        # Caller owns the transaction, including any cursor checkpoint. A
        # correction must never silently overwrite previously recorded evidence.
        retained=self._resolution_view()
        for kind,identity,value in normalized:
            if not self.start_ms<=value['timestamp_ms']<=end_ms: raise UnverifiedExecution('history outside scope')
            if (kind,identity) in retained and retained[(kind,identity)]['proof'] is None:
                raise UnverifiedExecution('history event conflicts with quarantined evidence')
            encoded=json.dumps(value,sort_keys=True)
            prior=self.db.execute('SELECT value FROM events WHERE kind=? AND id=?',(kind,identity)).fetchone()
            if prior and prior[0]!=encoded: raise UnverifiedExecution('history event changed')
            self.db.execute('INSERT OR IGNORE INTO events VALUES(?,?,?)',(kind,identity,encoded))
        for kind,identity,value in quarantined:
            if not self.start_ms<=value['timestamp_ms']<=end_ms: raise UnverifiedExecution('history outside scope')
            if self.db.execute('SELECT 1 FROM events WHERE kind=? AND id=?',(kind,identity)).fetchone():
                raise UnverifiedExecution('quarantined history conflicts with supported evidence')
            encoded=json.dumps(value,sort_keys=True,allow_nan=False)
            prior=self.db.execute('SELECT value FROM history_quarantine WHERE kind=? AND id=?',(kind,identity)).fetchone()
            if prior and prior[0]!=encoded: raise UnverifiedExecution('quarantined history event changed')
            self.db.execute('INSERT OR IGNORE INTO history_quarantine VALUES(?,?,?)',(kind,identity,encoded))

    def ingest(self,trades,funding,end_ms):
        old=self.db.execute('SELECT end_ms FROM history_coverage WHERE id=1').fetchone()
        if old and end_ms<old[0]: raise UnverifiedExecution('coverage regression')
        normalized,quarantined=self._normalize_rows(trades,funding)
        with self.db:
            self._store_rows(normalized,end_ms,quarantined)
            self.db.execute('INSERT OR REPLACE INTO history_coverage VALUES(1,?)',(end_ms,))

    def events(self,kind):
        self._resolution_view()
        return [json.loads(r[0]) for r in self.db.execute('SELECT value FROM events WHERE kind=?',(kind,))]

    def _resolution_view(self):
        """Validate retained raw, current interpretation, event and audit together.

        A new parser never silently retires a quarantine. Only an explicit,
        atomic resolution receipt permits the original record and a canonical
        event to coexist. Every read revalidates that interpretation; changing
        deployed code does not turn an old receipt into a permanent waiver.
        """
        out={}
        ledger_identity=[self.network,self.account,self.start_ms]
        current_identity=self.db.execute('SELECT value FROM ledger_identity WHERE id=1').fetchone()
        if current_identity is None or current_identity[0]!=json.dumps(ledger_identity):
            raise UnverifiedExecution('ledger account/campaign mismatch')
        tables={row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        proofs=(dict(((kind,identity),encoded) for kind,identity,encoded in self.db.execute(
            'SELECT kind,id,value FROM history_resolutions')) if 'history_resolutions' in tables else {})
        requests={}
        request_rows=(self.db.execute('SELECT id,value FROM history_resolution_requests')
                      if 'history_resolution_requests' in tables else ())
        for request_id,encoded in request_rows:
            try:
                receipt=json.loads(encoded)
                if (set(receipt)!={'version','request_id','fingerprint','operator_reason','resolved_at_ms',
                                   'ledger_identity','records'} or type(receipt['version']) is not int or
                        receipt['version']!=1 or receipt['request_id']!=request_id or
                        not isinstance(request_id,str) or not request_id.strip() or len(request_id)>128 or
                        not _digest(receipt['fingerprint']) or
                        not isinstance(receipt['operator_reason'],str) or not receipt['operator_reason'].strip() or
                        len(receipt['operator_reason'])>1000 or
                        _history_json(receipt['ledger_identity'])!=_history_json(ledger_identity) or
                        integer(receipt['resolved_at_ms'])<self.start_ms or
                        not isinstance(receipt['records'],list) or not receipt['records']):
                    raise ValueError('receipt')
                identities=[]
                for record in receipt['records']:
                    if (not isinstance(record,dict) or set(record)!={'kind','id','record_sha256','raw_sha256','canonical_sha256'}
                            or record['kind'] not in ('trade','funding') or not isinstance(record['id'],str)
                            or any(not _digest(record[key]) for key in ('record_sha256','raw_sha256','canonical_sha256'))):
                        raise ValueError('record')
                    identities.append((record['kind'],record['id']))
                if len(set(identities))!=len(identities) or any(key not in proofs for key in identities):
                    raise ValueError('missing or duplicate resolution')
                requests[request_id]=receipt
            except (KeyError,TypeError,ValueError) as exc:
                raise UnverifiedExecution('invalid history resolution receipt') from exc
        for kind,identity,encoded in self.db.execute('SELECT kind,id,value FROM history_quarantine ORDER BY kind,id'):
            try:
                saved=json.loads(encoded)
                if (kind not in ('trade','funding') or not isinstance(saved,dict) or
                        set(saved)!={'kind','id','market_id','timestamp_ms','reason','unsupported_value','raw'}):
                    raise UnverifiedExecution('invalid quarantined history record')
                raw=saved['raw']
                _history_object(raw)
                market=integer(raw.get('market_id'))
                stamp=integer(raw.get('timestamp'))*(1 if kind=='trade' else 1000)
                source_id=(str(integer(raw.get('trade_id'))) if kind=='trade' else
                           f"{market}:{integer(raw.get('funding_id'))}")
                unsupported=_history_token(raw.get('type' if kind=='trade' else 'position_side'),'original unsupported value')
                expected={'kind':kind,'id':source_id,'market_id':market,'timestamp_ms':stamp,
                          'reason':'unsupported_trade_type' if kind=='trade' else 'unsupported_funding_side',
                          'unsupported_value':unsupported,'raw':raw}
                if (_history_json(saved)!=_history_json(expected) or identity!=source_id or
                        stamp<self.start_ms or (kind=='trade' and unsupported=='trade') or
                        (kind=='funding' and unsupported in ('long','short'))):
                    raise UnverifiedExecution('quarantined history identity mismatch')
                canonical=None
                try:
                    canonical=normalize_trade(raw,self.account) if kind=='trade' else normalize_funding(raw)
                except UnsupportedHistoryRecord as exc:
                    if _history_json(saved)!=_history_json({**exc.issue,'raw':exc.raw}):
                        raise UnverifiedExecution('quarantined history identity mismatch')
                if canonical is not None:
                    canonical_id=(str(integer(canonical['trade_id'])) if kind=='trade' else
                                  f"{integer(canonical['market_id'])}:{integer(canonical['funding_id'])}")
                    if (canonical_id!=identity or integer(canonical['market_id'])!=market or
                            integer(canonical['timestamp_ms'])!=stamp):
                        raise UnverifiedExecution('reparsed history identity differs')
                event=self.db.execute('SELECT value FROM events WHERE kind=? AND id=?',(kind,identity)).fetchone()
                proof=None
                row_proof=proofs.pop((kind,identity),None)
                if row_proof is not None:
                    proof=json.loads(row_proof)
                    if (not isinstance(proof,dict) or set(proof)!={'version','request_id','kind','id',
                            'record_sha256','raw_sha256','canonical_sha256','fingerprint','operator_reason',
                            'resolved_at_ms','ledger_identity'} or type(proof['version']) is not int or proof['version']!=1):
                        raise UnverifiedExecution('invalid history resolution proof')
                    receipt=requests.get(proof['request_id'])
                    record={'kind':kind,'id':identity,
                            'record_sha256':hashlib.sha256(encoded.encode('utf-8')).hexdigest(),
                            'raw_sha256':_history_hash(raw),'canonical_sha256':_history_hash(canonical)}
                    if (canonical is None or receipt is None or record not in receipt['records'] or
                            integer(proof['resolved_at_ms'])<stamp or
                            any(proof[key]!=value for key,value in record.items()) or
                            any(_history_json(proof[key])!=_history_json(receipt[key]) for key in
                                ('fingerprint','operator_reason','resolved_at_ms','ledger_identity')) or
                            event is None or event[0]!=json.dumps(canonical,sort_keys=True,allow_nan=False)):
                        raise UnverifiedExecution('history resolution evidence mismatch')
                elif event is not None:
                    raise UnverifiedExecution('quarantined history conflicts with supported evidence')
                out[(kind,identity)]={'saved':saved,'canonical':canonical,'proof':proof,
                    'record_sha256':hashlib.sha256(encoded.encode('utf-8')).hexdigest()}
            except (KeyError, TypeError, ValueError) as exc:
                raise UnverifiedExecution('invalid quarantined history record') from exc
        if proofs:
            raise UnverifiedExecution('history resolution missing original quarantine')
        for receipt in requests.values():
            if any(out[(record['kind'],record['id'])]['proof']['request_id']!=receipt['request_id']
                   for record in receipt['records']):
                raise UnverifiedExecution('history resolution request linkage mismatch')
        return out

    def quarantined_events(self):
        """Unresolved records; resolved originals remain stored and validated."""
        return [item['saved'] for item in self._resolution_view().values() if item['proof'] is None]

    def resolution_candidates(self):
        """Read-only preview. Unsupported meaning stays explicitly blocked."""
        return [{**{key:value for key,value in item['saved'].items() if key!='raw'},
                 'record_sha256':item['record_sha256'],'raw_sha256':_history_hash(item['saved']['raw']),
                 'parser_supported':item['canonical'] is not None,
                 'canonical_sha256':_history_hash(item['canonical']) if item['canonical'] is not None else None,
                 'resolved':item['proof'] is not None,
                 'blocked_reason':('already_resolved' if item['proof'] is not None else
                                   'installed_parser_does_not_support_original_record' if item['canonical'] is None else None)}
                for item in self._resolution_view().values()]

    def resolve_quarantined(self,records,*,fingerprint,request_id,operator_reason,now_ms):
        """Explicitly promote only the original raw via the installed parser.

        The caller must hold the running account's serialization lock. This
        method neither clears a HALT nor changes positions, orders or cursors.
        """
        if (not _digest(fingerprint) or not isinstance(request_id,str) or not request_id.strip() or
                len(request_id)>128 or not isinstance(operator_reason,str) or not operator_reason.strip() or
                len(operator_reason)>1000 or integer(now_ms)<self.start_ms or
                not isinstance(records,list) or not records):
            raise UnverifiedExecution('invalid history resolution request')
        selected=[]
        for row in records:
            if (not isinstance(row,dict) or set(row)!={'kind','id','record_sha256'} or
                    row['kind'] not in ('trade','funding') or not isinstance(row['id'],str) or
                    not _digest(row['record_sha256'])):
                raise UnverifiedExecution('invalid history resolution selection')
            selected.append(dict(row))
        selected.sort(key=lambda row:(row['kind'],row['id']))
        if len({(row['kind'],row['id']) for row in selected})!=len(selected):
            raise UnverifiedExecution('duplicate history resolution selection')
        if self.db.in_transaction:
            raise UnverifiedExecution('history resolution requires its own transaction')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            retained=self._resolution_view()
            old=self.db.execute('SELECT value FROM history_resolution_requests WHERE id=?',(request_id,)).fetchone()
            if old is not None:
                receipt=json.loads(old[0])
                previous=[{key:row[key] for key in ('kind','id','record_sha256')} for row in receipt['records']]
                if (previous!=selected or receipt['fingerprint']!=fingerprint or receipt['operator_reason']!=operator_reason):
                    raise UnverifiedExecution('history resolution request identity reused')
                self.db.commit()
                return receipt
            resolved=[]
            for row in selected:
                item=retained.get((row['kind'],row['id']))
                if item is None or item['record_sha256']!=row['record_sha256']:
                    raise UnverifiedExecution('history resolution original record differs')
                if item['proof'] is not None:
                    raise UnverifiedExecution('history already resolved by another request')
                if item['canonical'] is None:
                    raise UnverifiedExecution('installed parser does not support original history record')
                if item['saved']['timestamp_ms']>now_ms:
                    raise UnverifiedExecution('history resolution precedes original record')
                resolved.append({**row,'raw_sha256':_history_hash(item['saved']['raw']),
                                 'canonical_sha256':_history_hash(item['canonical'])})
            receipt={'version':1,'request_id':request_id,'fingerprint':fingerprint,
                     'operator_reason':operator_reason,'resolved_at_ms':now_ms,
                     'ledger_identity':[self.network,self.account,self.start_ms],'records':resolved}
            self.db.execute('INSERT INTO history_resolution_requests VALUES(?,?)',(request_id,_history_json(receipt)))
            for row in resolved:
                item=retained[(row['kind'],row['id'])]
                proof={key:value for key,value in receipt.items() if key!='records'}
                proof.update(row)
                self.db.execute('INSERT INTO events VALUES(?,?,?)',
                    (row['kind'],row['id'],json.dumps(item['canonical'],sort_keys=True,allow_nan=False)))
                self.db.execute('INSERT INTO history_resolutions VALUES(?,?,?)',
                    (row['kind'],row['id'],_history_json(proof)))
            self._resolution_view()
            self.db.commit()
            return receipt
        except BaseException:
            self.db.rollback()
            raise

    def historical_issues(self):
        return [{key:value for key,value in row.items() if key!='raw'} for row in self.quarantined_events()]

    def strict_report(self):
        report=self.report()
        if report['historical_issues']:
            raise HistoricalEvidenceBlocked(report['historical_issues'])
        return report

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
        historical_issues=self.historical_issues()
        retained=self._resolution_view()
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
                'historical_evidence_complete':not historical_issues,'historical_issues':historical_issues,
                'historical_evidence_scope':'supported interpretation of observed records; collection coverage is separate',
                'quarantined_history_count':len(historical_issues),
                'retained_quarantined_history_count':len(retained),
                'resolved_quarantined_history_count':sum(item['proof'] is not None for item in retained.values()),
                'history_collection':self.collector.report(),
                'limitations':['fee fields missing or nonzero are not converted without verified units',
                               'funding cash changes are account records, not modeled market rates',
                               'quarantined unsupported records are excluded from monetary totals and block strict certification',
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
        untrusted_markets={issue['market_id'] for issue in self.historical_issues()}
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
                ambiguous=market in untrusted_markets or unowned_seen or latest_trade_ms==funding['timestamp_ms']
                candidates=[c for c,q in held.items() if ('long' if q>0 else 'short')==funding['position_side']]
                out[index]={**funding,'cycle':candidates[0] if len(candidates)==1 and not ambiguous else None}
        return out

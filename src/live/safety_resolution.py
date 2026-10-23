"""Audited operator cases consumed only by the existing account-owning runner.

No method here submits, cancels or invents an order. Historical promotion and
selective HALT release are separate cases. Neither case clears ENTRY_PAUSED.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import uuid

from .account_ledger import resolution_candidates_readonly
from .resolution_policy import ANCHOR_FIELDS, classify_reason, reason_id, state_anchor, evaluate_release
from .request_budget import RequestDeferred

CASE_TTL_MS = 300_000
MAX_BYTES = 16_000_000
CASE_ID = re.compile(r'^[0-9a-f]{32}$')


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def read_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError('resolution evidence file unavailable or unsafe')
    return json.loads(path.read_text(encoding='utf-8'))


def sync_dir(path):
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def immutable(path, value):
    """Atomic create, never replace an existing request/proof with new content."""
    path = Path(path)
    data = encoded(value)
    if len(data) > MAX_BYTES:
        raise ValueError('resolution evidence exceeds bounded file size')
    if path.exists():
        if read_json(path) != value:
            raise ValueError('immutable resolution evidence differs')
        return
    fd, name = tempfile.mkstemp(prefix='.resolution-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        # Both branches publish exclusively on the same filesystem. Windows
        # rename refuses an existing target; POSIX rename would overwrite it.
        try:
            if os.name == 'nt':
                os.rename(name, path)
            else:
                os.link(name, path)
        except FileExistsError:
            if read_json(path) != value:
                raise ValueError('resolution publication race') from None
        sync_dir(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def database_evidence(path, tables):
    """Read selected noncredential tables in a single SQLite read snapshot."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('required evidence database unavailable')
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        db.execute('PRAGMA query_only=ON'); db.execute('BEGIN')
        existing = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result = {}
        for table in tables:  # Fixed identifiers supplied only by this module.
            if table not in existing:
                result[table] = None
                continue
            cur = db.execute('SELECT * FROM ' + table + ' ORDER BY rowid LIMIT 50001')
            rows = cur.fetchall()
            if len(rows) > 50000:
                raise ValueError('evidence too large; scoped offline review required')
            result[table] = {'columns':[c[0] for c in cur.description], 'rows':rows}
        if len(encoded(result)) > MAX_BYTES:
            raise ValueError('evidence too large; scoped offline review required')
        return result
    finally:
        db.close()


class ResolutionStore:
    def __init__(self, directory, fingerprint, account, network, start_ms):
        self.directory = Path(directory)
        self.root = self.directory / 'safety-resolution'
        self.identity = {'fingerprint':fingerprint, 'account':account,
                         'network':network, 'start_ms':start_ms}

    def initialize(self):
        for path in (self.root, *(self.root / n for n in ('cases', 'requests', 'prepared', 'outcomes'))):
            if path.is_symlink():
                raise ValueError('unsafe resolution directory')
            path.mkdir(mode=0o700, exist_ok=True)
            if os.name != 'nt' and (path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o077):
                raise ValueError('resolution directory must be private to service owner')

    def path(self, category, case_id):
        if (category not in ('cases', 'requests', 'prepared', 'outcomes') or
                type(case_id) is not str or not CASE_ID.fullmatch(case_id)):
            raise ValueError('invalid resolution case identifier')
        return self.root / category / (case_id + '.json')

    def inspect(self):
        snap = read_json(self.directory / 'snapshot.json')
        state_anchor(snap)
        report = read_json(self.directory / 'report.json')
        ledger = self.directory / 'execution' / 'account-ledger.sqlite'
        candidates = resolution_candidates_readonly(ledger, self.identity['network'],
                                                    self.identity['account'], self.identity['start_ms'])
        execution = database_evidence(self.directory / 'execution' / 'execution.sqlite',
            ('intents', 'intent_dispatch', 'mutations', 'mutation_dispatch', 'bindings', 'intent_supersessions'))
        accounting = database_evidence(ledger,
            ('ledger_identity', 'events', 'history_quarantine', 'history_resolutions',
             'history_resolution_requests', 'history_collection_state', 'history_collection_cursors'))
        # A changed snapshot means this was not one reviewable safety state.
        if state_anchor(read_json(self.directory / 'snapshot.json')) != state_anchor(snap):
            raise ValueError('strategy state changed during inspection; inspect again')
        return {'identity':self.identity, 'snapshot':snap, 'report':report,
                'history_candidates':candidates, 'execution_evidence':execution,
                'accounting_evidence':accounting}

    def prepare(self, action, selections, operator_reason, universe, now_ms=None):
        now_ms = int(time.time()*1000) if now_ms is None else now_ms
        if (action not in ('release-halt', 'reparse-history') or type(selections) is not list
                or not selections or len(selections) != len(set(selections))
                or not isinstance(operator_reason, str) or not operator_reason.strip()
                or len(operator_reason) > 1000):
            raise ValueError('explicit action, unique selections and operator reason required')
        evidence = self.inspect(); snap = evidence['snapshot']
        reasons = []; records = []
        if action == 'release-halt':
            available = {reason_id(r):r for r in snap['halt_reasons']}
            for key in selections:
                reason = available.get(key)
                if reason is None or classify_reason(reason, universe) is None:
                    raise ValueError('selected HALT is absent or requires specialist repair')
                reasons.append(reason)
        else:
            available = {r['kind'] + ':' + r['id']:r for r in evidence['history_candidates']}
            for key in selections:
                row = available.get(key)
                if row is None or row['resolved'] or not row['parser_supported']:
                    raise ValueError('original history not supported by installed parser; verified parser repair required')
                records.append({k:row[k] for k in ('kind', 'id', 'record_sha256')})
        case_id = uuid.uuid4().hex
        case = {'version':1, 'case_id':case_id, 'identity':self.identity, 'action':action,
                'created_ms':now_ms, 'expires_ms':now_ms+CASE_TTL_MS,
                'operator_reason':operator_reason, 'halt_reasons':reasons, 'records':records,
                'state_anchor':state_anchor(snap), 'evidence_sha256':digest(evidence), 'evidence':evidence}
        self.initialize(); immutable(self.path('cases', case_id), case)
        return case

    def submit(self, case_id, now_ms=None):
        now_ms = int(time.time()*1000) if now_ms is None else now_ms
        case = read_json(self.path('cases', case_id))
        self.validate(case, now_ms)
        if state_anchor(read_json(self.directory/'snapshot.json')) != case['state_anchor']:
            raise ValueError('reviewed state changed; prepare a new case')
        self.initialize()
        request = {'version':1, 'case_id':case_id, 'case_sha256':digest(case)}
        immutable(self.path('requests', case_id), request)
        return {'case_id':case_id, 'state':'queued', 'action':case['action'],
                'meaning':'runner must independently reconcile; no HALT released by submission'}

    def validate(self, case, now_ms, *, allow_expired=False):
        if (type(case) is not dict or set(case) != {'version', 'case_id', 'identity', 'action',
                'created_ms', 'expires_ms', 'operator_reason', 'halt_reasons', 'records',
                'state_anchor', 'evidence_sha256', 'evidence'}
                or type(case.get('version')) is not int or case.get('version') != 1
                or encoded(case.get('identity')) != encoded(self.identity)
                or case.get('action') not in ('release-halt', 'reparse-history')
                or type(case.get('case_id')) is not str or not CASE_ID.fullmatch(case['case_id'])
                or type(case.get('created_ms')) is not int or type(case.get('expires_ms')) is not int
                or case['created_ms'] < self.identity['start_ms']
                or case['expires_ms']-case['created_ms'] != CASE_TTL_MS
                or not isinstance(case.get('operator_reason'), str) or not case['operator_reason'].strip()
                or len(case['operator_reason']) > 1000
                or type(case.get('evidence')) is not dict
                or encoded(case['evidence'].get('identity')) != encoded(self.identity)
                or digest(case.get('evidence')) != case.get('evidence_sha256')
                or state_anchor(case['evidence']['snapshot']) != case.get('state_anchor')):
            raise ValueError('case identity or reviewed evidence invalid')
        if type(now_ms) is not int:
            raise ValueError('invalid resolution clock')
        if not allow_expired and not case['created_ms'] <= now_ms < case['expires_ms']:
            raise ValueError('case expired or clock moved backwards; prepare a new case')
        if case['action'] == 'release-halt':
            reasons = case.get('halt_reasons')
            if (case.get('records') != [] or type(reasons) is not list or not reasons
                    or any(type(reason) is not str or not reason for reason in reasons)
                    or len(reasons) != len(set(reasons))
                    or any(reason not in case['evidence']['snapshot']['halt_reasons'] for reason in reasons)):
                raise ValueError('invalid release case')
        else:
            records = case.get('records')
            candidates = case['evidence'].get('history_candidates')
            if case.get('halt_reasons') != [] or type(records) is not list or not records or type(candidates) is not list:
                raise ValueError('invalid history case')
            selected = set()
            for record in records:
                if (type(record) is not dict or set(record) != {'kind', 'id', 'record_sha256'}
                        or record.get('kind') not in ('trade', 'funding') or type(record.get('id')) is not str
                        or type(record.get('record_sha256')) is not str
                        or not re.fullmatch('[0-9a-f]{64}', record['record_sha256'])):
                    raise ValueError('invalid history selection')
                key = (record['kind'], record['id'])
                matches = [row for row in candidates if type(row) is dict
                           and all(row.get(name) == value for name, value in record.items())
                           and row.get('parser_supported') is True and row.get('resolved') is False]
                if key in selected or len(matches) != 1:
                    raise ValueError('history selection differs from reviewed evidence')
                selected.add(key)


class SafetyResolution:
    def __init__(self, trader, fingerprint, account, network, start_ms):
        self.trader = trader
        self.store = ResolutionStore(trader.config.state_dir, fingerprint, account, network, start_ms)
        self.last = None
        self.store.initialize()

    def status(self):
        return {'enabled':True, 'last_result':self.last,
                'automatic_halt_release':False, 'entry_pause_release':False}

    def _snapshot(self):
        return {k:getattr(self.trader, k) for k in ANCHOR_FIELDS}

    def _historical_case(self,case_id):
        case=read_json(self.store.path('cases',case_id))
        identity=case.get('identity') if type(case) is dict else None
        if (type(identity) is not dict or type(identity.get('fingerprint')) is not str or
                not re.fullmatch('[0-9a-f]{64}',identity['fingerprint'])):
            raise ValueError('historical resolution original fingerprint invalid')
        historical=ResolutionStore(self.store.directory,identity['fingerprint'],
            self.store.identity['account'],self.store.identity['network'],self.store.identity['start_ms'])
        historical.validate(case,int(time.time()*1000),allow_expired=True)
        if case['case_id']!=case_id:
            raise ValueError('historical resolution filename identity mismatch')
        request=read_json(self.store.path('requests',case_id))
        if encoded(request)!=encoded({'version':1,'case_id':case_id,'case_sha256':digest(case)}):
            raise ValueError('historical resolution request mismatch')
        return case

    def _validate_outcome(self,case_id):
        case=self._historical_case(case_id)
        result=read_json(self.store.path('outcomes',case_id))
        if (type(result) is not dict or type(result.get('version')) is not int or result['version']!=1 or
                result.get('case_id')!=case_id or result.get('case_sha256')!=digest(case) or
                result.get('action')!=case['action'] or type(result.get('at_ms')) is not int or result['at_ms']<0 or
                result.get('state') not in ('rejected','released','history_reparsed')):
            raise ValueError('resolution outcome identity or state invalid')
        if result['state']=='rejected':
            reason=result.get('reason'); blockers=result.get('blockers')
            if not ((type(reason) is str and bool(reason)) or
                    (type(blockers) is list and bool(blockers) and all(type(x) is str and bool(x) for x in blockers))):
                raise ValueError('rejected resolution outcome lacks cause')
            if (case_id in self.trader.safety_resolutions_applied or
                    (case['action']=='reparse-history' and self.trader.broker.ledger.db.execute(
                        'SELECT 1 FROM history_resolution_requests WHERE id=?',(case_id,)).fetchone())):
                raise ValueError('rejected outcome conflicts with committed resolution')
        elif result['state']=='released':
            marker=self.trader.safety_resolutions_applied.get(case_id)
            proof=read_json(self.store.path('prepared',case_id))
            if (case['action']!='release-halt' or
                    marker!={'case_sha256':digest(case),'proof_sha256':digest(proof)} or
                    result.get('proof_sha256')!=digest(proof) or
                    result.get('released_reasons')!=case['halt_reasons']):
                raise ValueError('completed release outcome or snapshot receipt mismatch')
        else:
            if case['action']!='reparse-history':
                raise ValueError('history outcome action mismatch')
            ledger=self.trader.broker.ledger
            ledger.resolution_candidates()  # Revalidate original/canonical/proof, without mutations.
            row=ledger.db.execute('SELECT value FROM history_resolution_requests WHERE id=?',(case_id,)).fetchone()
            receipt=json.loads(row[0]) if row else None
            if (type(receipt) is not dict or encoded(result.get('receipt'))!=encoded(receipt) or
                    receipt.get('request_id')!=case_id or receipt.get('fingerprint')!=case['identity']['fingerprint'] or
                    receipt.get('operator_reason')!=case['operator_reason'] or
                    encoded(receipt.get('ledger_identity'))!=encoded([self.store.identity['network'],
                        self.store.identity['account'],self.store.identity['start_ms']]) or
                    sorted(({key:record[key] for key in ('kind','id','record_sha256')} for record in receipt['records']),
                           key=lambda record:(record['kind'],record['id']))!=
                    sorted(case['records'],key=lambda record:(record['kind'],record['id']))):
                raise ValueError('history outcome differs from committed ledger receipt')
        return result

    def _validate_applied(self):
        """Audit past commits too, without replaying any state change.

        Completed receipts retain their original source fingerprint across
        later approved deployments. Pending, unapplied cases still require
        this deployment's fingerprint in _validate_current.
        """
        markers=self.trader.safety_resolutions_applied
        if type(markers) is not dict:
            raise ValueError('applied resolution receipt map invalid')
        checked={}
        for case_id,marker in markers.items():
            if (type(marker) is not dict or set(marker)!={'case_sha256','proof_sha256'} or
                    any(type(value) is not str or not re.fullmatch('[0-9a-f]{64}',value)
                        for value in marker.values())):
                raise ValueError('applied resolution marker invalid')
            case=read_json(self.store.path('cases',case_id))
            identity=case.get('identity') if type(case) is dict else None
            if (type(identity) is not dict or type(identity.get('fingerprint')) is not str or
                    not re.fullmatch('[0-9a-f]{64}',identity['fingerprint'])):
                raise ValueError('applied resolution original fingerprint invalid')
            historical=ResolutionStore(self.store.directory,identity['fingerprint'],
                self.store.identity['account'],self.store.identity['network'],self.store.identity['start_ms'])
            historical.validate(case,int(time.time()*1000),allow_expired=True)
            if case['case_id']!=case_id or case['action']!='release-halt' or marker['case_sha256']!=digest(case):
                raise ValueError('applied resolution case mismatch')
            request=read_json(self.store.path('requests',case_id))
            if encoded(request)!=encoded({'version':1,'case_id':case_id,'case_sha256':digest(case)}):
                raise ValueError('applied resolution request mismatch')
            proof=read_json(self.store.path('prepared',case_id))
            original_reasons=case['evidence']['snapshot']['halt_reasons']
            remaining=[reason for reason in original_reasons if reason not in case['halt_reasons']]
            if (type(proof) is not dict or set(proof)!={'version','case_sha256','identity','operator_reason',
                    'released_reasons','remaining_reasons','evaluated_ms','state_anchor','fresh_evidence'} or
                    type(proof['version']) is not int or proof['version']!=1 or
                    marker['proof_sha256']!=digest(proof) or proof['case_sha256']!=digest(case) or
                    encoded(proof['identity'])!=encoded(case['identity']) or
                    proof['operator_reason']!=case['operator_reason'] or
                    proof['released_reasons']!=case['halt_reasons'] or proof['remaining_reasons']!=remaining or
                    type(proof['evaluated_ms']) is not int or
                    not case['created_ms']<=proof['evaluated_ms']<case['expires_ms'] or
                    proof['state_anchor']!=case['state_anchor'] or type(proof['fresh_evidence']) is not dict or
                    set(proof['fresh_evidence'])!={'ledger_report','positions','active_orders','equity',
                                                  'maintenance_margin','runtime_coverage'}):
                raise ValueError('applied resolution proof mismatch')
            checked[case_id]=proof
        for path in (self.store.root/'outcomes').glob('*.json'):
            result=self._validate_outcome(path.stem)
            if result['state']!='released': continue
            proof=checked.get(path.stem)
            if (proof is None or type(result.get('version')) is not int or result['version']!=1 or
                    result.get('case_id')!=path.stem or result.get('action')!='release-halt' or
                    result.get('case_sha256')!=proof['case_sha256'] or
                    result.get('proof_sha256')!=digest(proof) or
                    result.get('released_reasons')!=proof['released_reasons']):
                raise ValueError('completed release outcome or snapshot receipt mismatch')
        return checked

    def _result(self, case, state, **details):
        result = {'version':1, 'case_id':case['case_id'], 'case_sha256':digest(case),
                  'action':case['action'], 'state':state, 'at_ms':int(time.time()*1000), **details}
        path = self.store.path('outcomes', case['case_id'])
        if path.exists():
            old = self._validate_outcome(case['case_id'])
            if old.get('case_sha256') != digest(case):
                raise ValueError('resolution outcome identity mismatch')
            self.last = old; return
        immutable(path, result); self.last = result
        self.trader.journal.append('safety_resolution_result', **result)

    def _next(self, action):
        for path in sorted((self.store.root/'requests').glob('*.json')):
            case_id = path.stem
            if self.store.path('outcomes', case_id).exists():
                self._validate_outcome(case_id)
                continue
            request = read_json(path)
            case = read_json(self.store.path('cases', case_id))
            if encoded(request) != encoded({'version':1, 'case_id':case_id, 'case_sha256':digest(case)}):
                raise ValueError('queued request differs from reviewed case')
            if case['case_id'] != case_id:
                raise ValueError('case filename identity mismatch')
            if case['action'] == action:
                return case
        return None

    def _validate_current(self, case):
        self.store.validate(case, int(time.time()*1000))
        if state_anchor(self._snapshot()) != case['state_anchor']:
            raise ValueError('reviewed safety state changed; prepare a new case')

    def before_service(self):
        self._validate_applied()
        case = self._next('reparse-history')
        if case is None:
            return
        # Ledger transaction is the durable commit point; replay its exact receipt
        # after a crash even if the original operator case has since expired.
        old = self.trader.broker.ledger.db.execute(
            'SELECT value FROM history_resolution_requests WHERE id=?', (case['case_id'],)).fetchone()
        if old is None:
            try:
                self._validate_current(case)
            except ValueError as exc:
                self._result(case, 'rejected', reason=str(exc)); return
        else:
            case=self._historical_case(case['case_id'])
        receipt = self.trader.broker.ledger.resolve_quarantined(case['records'],
            fingerprint=case['identity']['fingerprint'], request_id=case['case_id'],
            operator_reason=case['operator_reason'], now_ms=int(time.time()*1000))
        self._result(case, 'history_reparsed', receipt=receipt,
                     meaning='original quarantine retained; HALT and pause unchanged')

    def after_service(self, coverage):
        applied=self._validate_applied()
        case = self._next('release-halt')
        if case is None:
            return
        marker = self.trader.safety_resolutions_applied.get(case['case_id'])
        if marker is not None:
            proof=applied[case['case_id']]
            self._result(case, 'released', proof_sha256=digest(proof),
                         released_reasons=proof['released_reasons'], replay_completed=True)
            return
        try:
            self._validate_current(case)
        except ValueError as exc:
            self._result(case, 'rejected', reason=str(exc)); return
        # Reads only. Keep normal protection service ahead of operator work and
        # leave the existing request budget/HTTP retry limits unchanged.
        try:
            state = self.trader.broker.account_state()
        except RequestDeferred:
            self.last = {'case_id':case['case_id'], 'state':'waiting_request_budget'}
            return
        now_ms = int(time.time()*1000)
        try:
            self._validate_current(case)
        except ValueError as exc:
            self._result(case, 'rejected', reason=str(exc)); return
        blockers = evaluate_release(self.trader, state, coverage, case['halt_reasons'], now_ms)
        if blockers:
            self._result(case, 'rejected', blockers=blockers); return
        old_reasons = list(self.trader.halt_reasons)
        remaining = [r for r in old_reasons if r not in case['halt_reasons']]
        proof = {'version':1, 'case_sha256':digest(case), 'identity':self.store.identity,
                 'operator_reason':case['operator_reason'], 'released_reasons':case['halt_reasons'],
                 'remaining_reasons':remaining, 'evaluated_ms':now_ms,
                 'state_anchor':state_anchor(self._snapshot()),
                 'fresh_evidence':{'ledger_report':state.ledger_report, 'positions':state.positions,
                     'active_orders':state.active_orders, 'equity':state.equity,
                     'maintenance_margin':state.maintenance_margin,
                     'runtime_coverage':coverage.snapshot() if hasattr(coverage, 'snapshot') else coverage}}
        proof_path = self.store.path('prepared', case['case_id'])
        if proof_path.exists():
            # A prior prepare without snapshot commit is not fresh permission.
            self._result(case, 'rejected', reason='interrupted preparation; prepare a new case'); return
        immutable(proof_path, proof)
        self.trader.journal.append('safety_resolution_prepared', case_id=case['case_id'],
                                   case_sha256=digest(case), proof_sha256=digest(proof))
        self.trader.exchange_recovery.require_revalidation()
        # Durable recovery gate precedes snapshot commit. Persist receipt with
        # selective removal, so crash replay cannot clear a recurring new cause.
        self.trader.halt_reasons = remaining
        self.trader.halted = bool(remaining)
        self.trader.safety_resolutions_applied[case['case_id']] = {
            'case_sha256':digest(case), 'proof_sha256':digest(proof)}
        try:
            self.trader._save()
        except BaseException:
            self.trader.halt_reasons = old_reasons; self.trader.halted = True
            self.trader.safety_resolutions_applied.pop(case['case_id'], None)
            raise
        self._result(case, 'released', proof_sha256=digest(proof),
                     released_reasons=case['halt_reasons'], remaining_reasons=remaining,
                     meaning='fresh recovery cycles still required; ENTRY_PAUSED unchanged')

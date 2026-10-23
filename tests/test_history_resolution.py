"""Immutable local quarantine reparsing; no venue client or live orders."""
import copy
import json
import sqlite3

import pytest

from test_history_collection import Paged, funding, make, trade
import live.account_ledger as module
from live.execution_evidence import UnverifiedExecution


FP='a'*64


def support(monkeypatch, *, trades=True, fundings=True):
    """Model a separately reviewed parser release, never an operator mapping."""
    normalize_trade,normalize_funding=module.normalize_trade,module.normalize_funding
    if trades:
        def new_trade(row,account):
            updated=copy.deepcopy(row)
            if updated.get('type')=='reviewed-trade': updated['type']='trade'
            return normalize_trade(updated,account)
        monkeypatch.setattr(module,'normalize_trade',new_trade)
    if fundings:
        def new_funding(row):
            updated=copy.deepcopy(row)
            if updated.get('position_side')=='reviewed-long': updated['position_side']='long'
            return normalize_funding(updated)
        monkeypatch.setattr(module,'normalize_funding',new_funding)


def selected(ledger):
    return [{key:row[key] for key in ('kind','id','record_sha256')}
            for row in ledger.resolution_candidates() if not row['resolved']]


def resolve(ledger,records=None,**extra):
    return ledger.resolve_quarantined(selected(ledger) if records is None else records,
        **{'fingerprint':FP,'request_id':'incident-1','operator_reason':'reviewed parser correction',
           'now_ms':5000,**extra})


def table(ledger,name):
    assert name in ('events','history_quarantine','history_resolutions','history_resolution_requests',
                    'history_coverage','history_collection_state','history_collection_cursors')
    return ledger.db.execute('SELECT * FROM '+name).fetchall()


def seed(ledger):
    ledger.collect(Paged([trade(1,2000,type='reviewed-trade',taker_fee=None)],
                         [funding(1,3000,position_side='reviewed-long')]),4000)


def test_unsupported_preview_has_no_raw_or_invented_interpretation(tmp_path):
    ledger=make(tmp_path)
    try:
        seed(ledger)
        before=table(ledger,'history_quarantine')
        rows=ledger.resolution_candidates()
        assert len(rows)==2 and all(not r['parser_supported'] and not r['resolved'] for r in rows)
        assert all(r['canonical_sha256'] is None and 'raw' not in r for r in rows)
        assert all(r['blocked_reason']=='installed_parser_does_not_support_original_record' for r in rows)
        with pytest.raises(UnverifiedExecution,match='does not support'): resolve(ledger)
        assert table(ledger,'history_quarantine')==before
        assert table(ledger,'events')==table(ledger,'history_resolutions')==table(ledger,'history_resolution_requests')==[]
        assert ledger.report()['quarantined_history_count']==2
    finally: ledger.close()


@pytest.mark.parametrize('legacy',[False,True])
def test_readonly_preview_does_not_migrate_or_write(tmp_path,legacy):
    ledger=make(tmp_path)
    seed(ledger)
    expected=ledger.resolution_candidates()
    if legacy:
        ledger.db.execute('DROP TABLE history_resolutions')
        ledger.db.execute('DROP TABLE history_resolution_requests')
        ledger.db.commit()
    ledger.close()
    path=tmp_path/'ledger.sqlite'
    before=path.read_bytes()
    assert module.resolution_candidates_readonly(path,'testnet',100,1000)==expected
    assert path.read_bytes()==before
    for args in (('mainnet',100,1000),('testnet',101,1000),('testnet',100,1001)):
        with pytest.raises(UnverifiedExecution,match='account/campaign'):
            module.resolution_candidates_readonly(path,*args)
    assert path.read_bytes()==before
    missing=tmp_path/'not-created.sqlite'
    with pytest.raises(sqlite3.OperationalError):
        module.resolution_candidates_readonly(missing,'testnet',100,1000)
    assert not missing.exists()


def test_reviewed_reparse_is_explicit_atomic_durable_and_does_not_certify_fees(tmp_path,monkeypatch):
    ledger=make(tmp_path)
    seed(ledger)
    original=table(ledger,'history_quarantine')
    coverage={name:table(ledger,name) for name in ('history_coverage','history_collection_state','history_collection_cursors')}
    support(monkeypatch)
    preview=ledger.resolution_candidates()
    assert all(r['parser_supported'] and not r['resolved'] for r in preview)
    assert ledger.report()['quarantined_history_count']==2  # no automatic promotion
    receipt=resolve(ledger)
    assert receipt['ledger_identity']==['testnet',100,1000]
    assert table(ledger,'history_quarantine')==original
    assert coverage=={name:table(ledger,name) for name in coverage}
    assert len(table(ledger,'history_resolutions'))==2
    assert len(table(ledger,'history_resolution_requests'))==1
    ledger.close()
    ledger=make(tmp_path)
    try:
        assert ledger.quarantined_events()==[]
        report=ledger.strict_report()
        assert report['historical_evidence_complete']
        assert report['retained_quarantined_history_count']==report['resolved_quarantined_history_count']==2
        assert report['quarantined_history_count']==0
        assert report['fill_count']==report['funding_count']==report['unknown_fee_fills']==1
        assert report['funding_cash_change']=='0.01' and not report['performance_complete']
        assert ledger.events('trade')[0]['fee_usdc'] is None
        for _ in range(3):
            ledger.collect(Paged([trade(1,2000,type='reviewed-trade',taker_fee=None)],
                                 [funding(1,3000,position_side='reviewed-long')]),6000)
        assert len(ledger.events('trade'))==len(ledger.events('funding'))==1
        assert table(ledger,'history_quarantine')==original
    finally: ledger.close()


def test_only_requested_record_resolves_remaining_market_stays_blocked(tmp_path,monkeypatch):
    ledger=make(tmp_path)
    try:
        seed(ledger)
        support(monkeypatch)
        requests=selected(ledger)
        resolve(ledger,[requests[0]])
        assert ledger.report()['quarantined_history_count']==1
        assert not ledger.report()['historical_evidence_complete']
        with pytest.raises(module.HistoricalEvidenceBlocked): ledger.strict_report()
        resolve(ledger,[requests[1]],request_id='incident-2')
        assert ledger.strict_report()['quarantined_history_count']==0
    finally: ledger.close()


def test_all_selection_fails_atomically_when_one_parser_is_still_unsupported(tmp_path,monkeypatch):
    ledger=make(tmp_path)
    try:
        seed(ledger)
        support(monkeypatch,fundings=False)
        with pytest.raises(UnverifiedExecution,match='does not support'): resolve(ledger)
        for name in ('events','history_resolutions','history_resolution_requests'): assert table(ledger,name)==[]
        assert len(ledger.quarantined_events())==2
    finally: ledger.close()


def test_same_request_replays_receipt_but_different_identity_cannot_reuse_it(tmp_path,monkeypatch):
    ledger=make(tmp_path)
    try:
        seed(ledger)
        support(monkeypatch)
        requests=selected(ledger)
        receipt=resolve(ledger,requests)
        assert resolve(ledger,requests,now_ms=9000)==receipt
        for kwargs in ({'operator_reason':'changed'},{'fingerprint':'b'*64}):
            with pytest.raises(UnverifiedExecution,match='identity reused'): resolve(ledger,requests,**kwargs)
        with pytest.raises(UnverifiedExecution,match='identity reused'): resolve(ledger,requests[:1])
        with pytest.raises(UnverifiedExecution,match='another request'): resolve(ledger,requests,request_id='different')
        assert len(table(ledger,'events'))==2 and len(table(ledger,'history_resolution_requests'))==1
    finally: ledger.close()


@pytest.mark.parametrize('change',[{'extra':'new provider detail'},{'type':'trade'},{'price':'101','usd_amount':'101'}])
def test_upstream_changed_original_still_conflicts_even_if_canonical_unchanged(tmp_path,monkeypatch,change):
    ledger=make(tmp_path)
    try:
        seed(ledger)
        support(monkeypatch)
        resolve(ledger)
        before=table(ledger,'events')
        with pytest.raises(UnverifiedExecution,match='changed'):
            ledger.ingest([trade(1,2000,type='reviewed-trade',taker_fee=None)|change],[],6000)
        assert table(ledger,'events')==before
    finally: ledger.close()


@pytest.mark.parametrize('mutation',[
    'raw','saved_identity','proof_hash','proof_fingerprint','proof_account','proof_time','proof_request',
    'event_missing','event_modified','receipt_missing','receipt_account','receipt_records','original_missing',
    'ledger_identity',
])
def test_resolution_evidence_corruption_blocks_all_reads(tmp_path,monkeypatch,mutation):
    ledger=make(tmp_path)
    try:
        seed(ledger)
        support(monkeypatch)
        resolve(ledger)
        if mutation=='ledger_identity':
            ledger.db.execute('UPDATE ledger_identity SET value=?',(json.dumps(['testnet',999,1000]),))
        elif mutation=='original_missing':
            ledger.db.execute("DELETE FROM history_quarantine WHERE kind='trade'")
        elif mutation=='event_missing':
            ledger.db.execute("DELETE FROM events WHERE kind='trade'")
        elif mutation=='receipt_missing':
            ledger.db.execute('DELETE FROM history_resolution_requests')
        else:
            name=('history_quarantine' if mutation in ('raw','saved_identity') else
                  'events' if mutation=='event_modified' else
                  'history_resolution_requests' if mutation.startswith('receipt_') else 'history_resolutions')
            where='' if name=='history_resolution_requests' else " WHERE kind='trade'"
            row=json.loads(ledger.db.execute('SELECT value FROM '+name+where).fetchone()[0])
            if mutation=='raw': row['raw']['extra']='changed'
            elif mutation=='saved_identity': row['market_id']=2
            elif mutation=='proof_hash': row['raw_sha256']='b'*64
            elif mutation=='proof_fingerprint': row['fingerprint']='b'*64
            elif mutation in ('proof_account','receipt_account'): row['ledger_identity'][1]=999
            elif mutation=='proof_time': row['resolved_at_ms']=9999
            elif mutation=='proof_request': row['request_id']='not-the-request'
            elif mutation=='event_modified': row['qty']='2'
            elif mutation=='receipt_records': row['records']=row['records'][:1]
            ledger.db.execute('UPDATE '+name+' SET value=?'+where,(json.dumps(row,sort_keys=True),))
        ledger.db.commit()
        for reader in (ledger.resolution_candidates,ledger.quarantined_events,ledger.report,
                       lambda:ledger.events('trade')):
            with pytest.raises(UnverifiedExecution): reader()
    finally: ledger.close()


def test_resolved_parser_regression_and_changed_economics_block(tmp_path,monkeypatch):
    ledger=make(tmp_path)
    try:
        seed(ledger)
        original=module.normalize_trade
        support(monkeypatch)
        supported=module.normalize_trade
        resolve(ledger)
        monkeypatch.setattr(module,'normalize_trade',original)
        with pytest.raises(UnverifiedExecution): ledger.report()
        def changed(row,account):
            result=supported(row,account)
            result['quote']='101'
            return result
        monkeypatch.setattr(module,'normalize_trade',changed)
        with pytest.raises(UnverifiedExecution): ledger.report()
    finally: ledger.close()


@pytest.mark.parametrize('fail_table',['events','history_resolutions','history_resolution_requests'])
def test_resolution_write_failure_rolls_back_every_table(tmp_path,monkeypatch,fail_table):
    ledger=make(tmp_path)
    try:
        seed(ledger)
        support(monkeypatch)
        before=table(ledger,'history_quarantine')
        ledger.db.execute('CREATE TRIGGER fail_resolution BEFORE INSERT ON '+fail_table+
                          " BEGIN SELECT RAISE(ABORT,'simulated resolution storage failure'); END")
        ledger.db.commit()
        with pytest.raises(sqlite3.DatabaseError,match='simulated resolution'): resolve(ledger)
        assert table(ledger,'history_quarantine')==before
        for name in ('events','history_resolutions','history_resolution_requests'): assert table(ledger,name)==[]
    finally: ledger.close()


@pytest.mark.parametrize('mutation',['stale_hash','missing_id','duplicate','economic_mapping','empty',
                                   'wrong_fingerprint','empty_reason','bad_time','boolean_time'])
def test_invalid_or_stale_resolution_selection_never_changes_ledger(tmp_path,monkeypatch,mutation):
    ledger=make(tmp_path)
    try:
        seed(ledger)
        support(monkeypatch)
        rows=selected(ledger); kwargs={}
        if mutation=='stale_hash': rows[0]['record_sha256']='0'*64
        elif mutation=='missing_id': rows[0]['id']='123'
        elif mutation=='duplicate': rows.append(rows[0])
        elif mutation=='economic_mapping': rows[0]['type']='trade'
        elif mutation=='empty': rows=[]
        elif mutation=='wrong_fingerprint': kwargs['fingerprint']='not-a-hash'
        elif mutation=='empty_reason': kwargs['operator_reason']=' '
        elif mutation=='bad_time': kwargs['now_ms']=1001
        elif mutation=='boolean_time': kwargs['now_ms']=True
        with pytest.raises(UnverifiedExecution): resolve(ledger,rows,**kwargs)
        assert table(ledger,'events')==table(ledger,'history_resolutions')==table(ledger,'history_resolution_requests')==[]
    finally: ledger.close()

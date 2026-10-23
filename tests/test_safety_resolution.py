"""Local operator-case coordinator tests; no credentials, orders or network."""
import copy
import json
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import live.safety_resolution as module
from live.account_ledger import AccountLedger
from live.execution_evidence import UnverifiedExecution
from live.request_budget import RequestDeferred
from live.resolution_policy import ANCHOR_FIELDS, reason_id
from test_history_collection import trade
from test_history_resolution import support
from test_resolution_policy import clean, NOW, UNKNOWN


FP='a'*64


def write(path,value):
    path.write_text(json.dumps(value,sort_keys=True),encoding='utf-8')


@pytest.fixture
def rig(tmp_path,monkeypatch):
    monkeypatch.setattr(module.time,'time',lambda:NOW/1000)
    trader,state,coverage=clean()
    trader.config.state_dir=tmp_path
    for name in ANCHOR_FIELDS:
        if not hasattr(trader,name): setattr(trader,name,{})
    trader.safety_resolutions_applied={}
    execution=tmp_path/'execution'; execution.mkdir()
    ledger=AccountLedger(execution/'account-ledger.sqlite','testnet',100,1000)
    sqlite3.connect(execution/'execution.sqlite').close()
    trader.broker.ledger=ledger
    calls={'account':0,'revalidation':0,'save':0,'journal':[]}
    def account_state():
        calls['account']+=1
        return state
    trader.broker.account_state=account_state
    def require_revalidation():
        calls['revalidation']+=1
        write(tmp_path/'EXCHANGE_PAUSED',{'state':'paused','reason':'safety_resolution_verification'})
    trader.exchange_recovery.require_revalidation=require_revalidation
    class Journal:
        def append(self,event,**values): calls['journal'].append((event,values))
    trader.journal=Journal()
    def save():
        calls['save']+=1
        write(tmp_path/'snapshot.json',{**{key:getattr(trader,key) for key in ANCHOR_FIELDS},
                                      'safety_resolutions_applied':trader.safety_resolutions_applied})
    trader._save=save
    save(); write(tmp_path/'report.json',state.ledger_report)
    controller=module.SafetyResolution(trader,FP,100,'testnet',1000)
    yield trader,state,coverage,controller,calls
    ledger.close()


def prepared(rig, *, action='release-halt', selections=None):
    trader,_,_,controller,_=rig
    return controller.store.prepare(action,selections or [reason_id(UNKNOWN)],
                                    'Reviewed exact evidence',trader.universe,now_ms=NOW)


def queued(rig,**kwargs):
    case=prepared(rig,**kwargs)
    rig[3].store.submit(case['case_id'],now_ms=NOW)
    return case


def outcome(controller,case):
    return module.read_json(controller.store.path('outcomes',case['case_id']))


def test_prepare_and_submit_only_store_case_without_releasing_halt(rig):
    trader,_,_,controller,calls=rig
    before=calls.copy()
    case=prepared(rig)
    assert case['evidence']['identity']==case['identity']
    assert controller.store.submit(case['case_id'],now_ms=NOW)['state']=='queued'
    assert trader.halted and trader.halt_reasons==[UNKNOWN]
    assert calls['account']==calls['revalidation']==0 and calls['save']==before['save']
    assert not controller.store.path('prepared',case['case_id']).exists()


def test_release_uses_fresh_read_retains_other_reason_manual_pause_and_recovery_gate(rig):
    trader,_,coverage,controller,calls=rig
    other='unrecognized manual safety condition'
    trader.halt_reasons.append(other); trader._save()
    marker=trader.config.state_dir/'ENTRY_PAUSED'; write(marker,{'reason':'operator pause'})
    before=marker.read_bytes()
    case=queued(rig)
    controller.after_service(coverage)
    assert calls['account']==calls['revalidation']==1
    assert trader.halted and trader.halt_reasons==[other]
    assert marker.read_bytes()==before
    assert module.read_json(trader.config.state_dir/'EXCHANGE_PAUSED')['state']=='paused'
    result=outcome(controller,case)
    assert result['state']=='released' and result['released_reasons']==[UNKNOWN]
    assert result['remaining_reasons']==[other]
    proof=module.read_json(controller.store.path('prepared',case['case_id']))
    assert proof['fresh_evidence']['ledger_report']['performance_complete'] is False
    assert trader.safety_resolutions_applied[case['case_id']]['proof_sha256']==module.digest(proof)


def test_release_of_last_selected_reason_still_requires_recovery_not_direct_resume(rig):
    trader,_,coverage,controller,calls=rig
    case=queued(rig)
    controller.after_service(coverage)
    assert not trader.halted and trader.halt_reasons==[]
    assert (trader.config.state_dir/'EXCHANGE_PAUSED').exists()
    assert outcome(controller,case)['meaning'].startswith('fresh recovery cycles still required')
    saved=calls.copy()
    controller.after_service(coverage)
    assert calls['account']==saved['account'] and calls['save']==saved['save']


@pytest.mark.parametrize('change',['expired','backward_clock','state','fingerprint','account'])
def test_stale_or_mismatched_case_cannot_apply(rig,monkeypatch,change):
    trader,_,coverage,controller,calls=rig
    case=queued(rig)
    if change=='expired': monkeypatch.setattr(module.time,'time',lambda:(NOW+module.CASE_TTL_MS)/1000)
    elif change=='backward_clock': monkeypatch.setattr(module.time,'time',lambda:(NOW-1)/1000)
    elif change=='state': trader.protection_generation={'BTCUSDT':1}
    elif change=='fingerprint': controller.store.identity={**controller.store.identity,'fingerprint':'b'*64}
    elif change=='account': controller.store.identity={**controller.store.identity,'account':999}
    controller.after_service(coverage)
    assert trader.halted and trader.halt_reasons==[UNKNOWN]
    assert calls['account']==calls['revalidation']==0
    assert outcome(controller,case)['state']=='rejected'


def test_changed_after_submission_fresh_read_is_rechecked_before_release(rig):
    trader,state,coverage,controller,calls=rig
    case=queued(rig)
    def account_state():
        calls['account']+=1
        trader.halt_reasons.append('new unsafe condition')
        return state
    trader.broker.account_state=account_state
    controller.after_service(coverage)
    assert calls['account']==1 and calls['revalidation']==0
    assert trader.halted and UNKNOWN in trader.halt_reasons
    assert outcome(controller,case)['state']=='rejected'


def test_actual_policy_denies_unverified_fresh_account(rig):
    trader,state,coverage,controller,calls=rig
    state.execution_changes_verified=False
    case=queued(rig)
    controller.after_service(coverage)
    assert trader.halted and calls['revalidation']==0
    assert 'EXECUTION_NOT_RECONCILED' in outcome(controller,case)['blockers']


def test_request_budget_wait_does_not_mutate_release_or_extend_expiry(rig,monkeypatch):
    trader,_,coverage,controller,calls=rig
    case=queued(rig)
    def deferred(): raise RequestDeferred(20)
    trader.broker.account_state=deferred
    controller.after_service(coverage)
    assert controller.last['state']=='waiting_request_budget'
    assert not controller.store.path('outcomes',case['case_id']).exists()
    assert trader.halted and calls['revalidation']==0
    monkeypatch.setattr(module.time,'time',lambda:(NOW+module.CASE_TTL_MS)/1000)
    controller.after_service(coverage)
    assert outcome(controller,case)['state']=='rejected' and trader.halted


def test_snapshot_write_failure_keeps_halt_and_prepared_proof_is_not_reused(rig):
    trader,_,coverage,controller,calls=rig
    case=queued(rig)
    def failing_save(): raise OSError('simulated snapshot failure')
    trader._save=failing_save
    with pytest.raises(OSError,match='snapshot'): controller.after_service(coverage)
    assert trader.halted and trader.halt_reasons==[UNKNOWN]
    assert trader.safety_resolutions_applied=={}
    assert calls['revalidation']==1
    assert controller.store.path('prepared',case['case_id']).exists()
    assert not controller.store.path('outcomes',case['case_id']).exists()
    controller.after_service(coverage)
    assert outcome(controller,case)['state']=='rejected'
    assert trader.halted and trader.halt_reasons==[UNKNOWN]


def test_recovery_marker_failure_prevents_snapshot_release(rig):
    trader,_,coverage,controller,calls=rig
    case=queued(rig)
    saves=calls['save']
    def failure(): raise OSError('simulated recovery marker failure')
    trader.exchange_recovery.require_revalidation=failure
    with pytest.raises(OSError,match='recovery marker'): controller.after_service(coverage)
    assert trader.halted and trader.safety_resolutions_applied=={} and calls['save']==saves
    assert controller.store.path('prepared',case['case_id']).exists()


def simulate_committed_crash(rig,monkeypatch):
    trader,_,coverage,controller,calls=rig
    case=queued(rig)
    original=controller._result
    monkeypatch.setattr(controller,'_result',lambda *a,**k:(_ for _ in ()).throw(OSError('crash after commit')))
    with pytest.raises(OSError,match='crash after commit'): controller.after_service(coverage)
    monkeypatch.setattr(controller,'_result',original)
    assert not trader.halted and case['case_id'] in trader.safety_resolutions_applied
    return case


def test_crash_after_snapshot_commit_replays_receipt_even_expired_never_clears_recurring_halt(rig,monkeypatch):
    trader,_,coverage,controller,calls=rig
    case=simulate_committed_crash(rig,monkeypatch)
    trader.halted=True; trader.halt_reasons=[UNKNOWN]
    trader._save()
    before=calls.copy()
    monkeypatch.setattr(module.time,'time',lambda:(NOW+module.CASE_TTL_MS+1000)/1000)
    restarted=module.SafetyResolution(trader,FP,100,'testnet',1000)
    restarted.after_service(coverage)
    assert trader.halted and trader.halt_reasons==[UNKNOWN]
    assert calls['account']==before['account'] and calls['revalidation']==before['revalidation']
    assert calls['save']==before['save']
    assert outcome(restarted,case)['replay_completed'] is True


@pytest.mark.parametrize('mutation',['proof_missing','proof_changed','marker_missing','request_missing',
                                   'outcome_hash','case_missing'])
def test_completed_release_proof_is_revalidated_on_every_boundary(rig,mutation):
    trader,_,coverage,controller,calls=rig
    case=queued(rig)
    controller.after_service(coverage)
    before=calls.copy()
    if mutation=='proof_missing': controller.store.path('prepared',case['case_id']).unlink()
    elif mutation=='proof_changed':
        path=controller.store.path('prepared',case['case_id'])
        proof=module.read_json(path); proof['remaining_reasons']=['tampered']; write(path,proof)
    elif mutation=='marker_missing': trader.safety_resolutions_applied={}
    elif mutation=='request_missing': controller.store.path('requests',case['case_id']).unlink()
    elif mutation=='case_missing': controller.store.path('cases',case['case_id']).unlink()
    elif mutation=='outcome_hash':
        path=controller.store.path('outcomes',case['case_id'])
        result=module.read_json(path); result['proof_sha256']='0'*64; write(path,result)
    for check in (controller.before_service,lambda:controller.after_service(coverage)):
        with pytest.raises(ValueError): check()
    assert calls['account']==before['account'] and calls['revalidation']==before['revalidation']
    assert calls['save']==before['save']


@pytest.mark.parametrize('committed_only',[False,True])
def test_later_approved_deployment_preserves_historical_receipt_without_new_authority(rig,monkeypatch,committed_only):
    trader,_,coverage,controller,calls=rig
    if committed_only:
        case=simulate_committed_crash(rig,monkeypatch)
    else:
        case=queued(rig); controller.after_service(coverage)
    trader.halted=True; trader.halt_reasons=[UNKNOWN]
    before=calls.copy()
    monkeypatch.setattr(module.time,'time',lambda:(NOW+module.CASE_TTL_MS+1000)/1000)
    newer=module.SafetyResolution(trader,'b'*64,100,'testnet',1000)
    newer.before_service(); newer.after_service(coverage)
    assert trader.halted and trader.halt_reasons==[UNKNOWN]
    assert calls['account']==before['account'] and calls['save']==before['save']
    assert calls['revalidation']==before['revalidation']
    assert outcome(newer,case)['state']=='released'


@pytest.mark.parametrize('mutation',['missing_proof','proof_changed','marker_changed','missing_marker'])
def test_missing_or_tampered_commit_proof_never_releases_again(rig,monkeypatch,mutation):
    trader,_,coverage,controller,calls=rig
    case=simulate_committed_crash(rig,monkeypatch)
    trader.halted=True; trader.halt_reasons=[UNKNOWN]
    before=calls['revalidation']
    path=controller.store.path('prepared',case['case_id'])
    if mutation=='missing_proof': path.unlink()
    elif mutation=='proof_changed':
        proof=module.read_json(path); proof['released_reasons']=['changed']; write(path,proof)
    elif mutation=='marker_changed': trader.safety_resolutions_applied[case['case_id']]['proof_sha256']='0'*64
    elif mutation=='missing_marker': trader.safety_resolutions_applied={}
    if mutation=='missing_marker':
        controller.after_service(coverage)
        assert outcome(controller,case)['state']=='rejected'
    else:
        with pytest.raises(ValueError): controller.after_service(coverage)
    assert trader.halted and trader.halt_reasons==[UNKNOWN] and calls['revalidation']==before


@pytest.mark.parametrize('mutation',['request_hash','case_evidence','case_filename'])
def test_changed_queued_request_or_case_is_rejected_before_account_reads(rig,mutation):
    trader,_,coverage,controller,calls=rig
    case=queued(rig)
    if mutation=='request_hash':
        path=controller.store.path('requests',case['case_id'])
        request=module.read_json(path); request['case_sha256']='0'*64; write(path,request)
    else:
        path=controller.store.path('cases',case['case_id'])
        if mutation=='case_evidence': case['evidence']['report']['equity']='tampered'
        else: case['case_id']='0'*32
        write(path,case)
    with pytest.raises(ValueError): controller.after_service(coverage)
    assert trader.halted and calls['account']==calls['revalidation']==0


@pytest.mark.parametrize('mutation',['extra','boolean_version','foreign_evidence_identity','wrong_selection','duplicate_selection'])
def test_case_validation_binds_exact_reviewed_evidence_and_selected_causes(rig,mutation):
    _,_,_,controller,_=rig
    case=prepared(rig)
    if mutation=='extra': case['override']=True
    elif mutation=='boolean_version': case['version']=True
    elif mutation=='foreign_evidence_identity':
        case['evidence']['identity']={**case['identity'],'account':999}
        case['evidence_sha256']=module.digest(case['evidence'])
    elif mutation=='wrong_selection': case['halt_reasons']=['unreviewed reason']
    elif mutation=='duplicate_selection': case['halt_reasons']*=2
    with pytest.raises(ValueError): controller.store.validate(case,NOW)


def test_readonly_database_evidence_uses_uri_and_never_modifies_input(tmp_path,monkeypatch):
    path=tmp_path/'example.sqlite'
    connection=sqlite3.connect(path)
    connection.execute('CREATE TABLE example(value TEXT)')
    connection.execute("INSERT INTO example VALUES('preserved')")
    connection.commit(); connection.close()
    before=path.read_bytes()
    connect=sqlite3.connect; seen=[]
    def observe(*args,**kwargs):
        seen.append((args,kwargs))
        return connect(*args,**kwargs)
    monkeypatch.setattr(module.sqlite3,'connect',observe)
    result=module.database_evidence(path,('example','absent'))
    assert seen[0][1]['uri'] is True and seen[0][0][0].endswith('?mode=ro')
    assert result['example']['rows']==[('preserved',)] and result['absent'] is None
    assert path.read_bytes()==before
    missing=tmp_path/'missing.sqlite'
    with pytest.raises(ValueError): module.database_evidence(missing,('example',))
    assert not missing.exists()


def test_history_preview_cannot_dismiss_unsupported_economic_meaning(rig):
    trader,_,_,controller,calls=rig
    trader.broker.ledger.ingest([trade(1,2000,type='reviewed-trade')],[],3000)
    with pytest.raises(ValueError,match='parser'):
        prepared(rig,action='reparse-history',selections=['trade:1'])
    assert trader.halted and trader.broker.ledger.report()['quarantined_history_count']==1
    assert calls['account']==calls['revalidation']==0


def test_history_reparse_promotes_only_installed_parser_preserves_halt_and_readonly_review(rig,monkeypatch):
    trader,_,_,controller,calls=rig
    trader.broker.ledger.ingest([trade(1,2000,type='reviewed-trade')],[],3000)
    support(monkeypatch)
    case=queued(rig,action='reparse-history',selections=['trade:1'])
    controller.before_service()
    assert outcome(controller,case)['state']=='history_reparsed'
    assert trader.broker.ledger.report()['resolved_quarantined_history_count']==1
    assert trader.halted and trader.halt_reasons==[UNKNOWN]
    assert trader.safety_resolutions_applied=={} and calls['account']==calls['revalidation']==0


def test_reparse_crash_receipt_replays_without_moving_halt_or_duplicate_event(rig,monkeypatch):
    trader,_,_,controller,_=rig
    trader.broker.ledger.ingest([trade(1,2000,type='reviewed-trade')],[],3000)
    support(monkeypatch)
    case=queued(rig,action='reparse-history',selections=['trade:1'])
    original=controller._result
    monkeypatch.setattr(controller,'_result',lambda *a,**k:(_ for _ in ()).throw(OSError('after ledger commit')))
    with pytest.raises(OSError,match='after ledger'): controller.before_service()
    monkeypatch.setattr(controller,'_result',original)
    monkeypatch.setattr(module.time,'time',lambda:(NOW+module.CASE_TTL_MS+1000)/1000)
    controller.before_service()
    assert outcome(controller,case)['state']=='history_reparsed'
    assert len(trader.broker.ledger.events('trade'))==1
    assert trader.halted and trader.halt_reasons==[UNKNOWN]


@pytest.mark.parametrize('state',['rejected','history_reparsed'])
@pytest.mark.parametrize('mutation',['filename','case_id','action','case_hash','version','state','request'])
def test_every_historical_outcome_is_bound_to_original_case_and_request(rig,monkeypatch,state,mutation):
    trader,_,coverage,controller,calls=rig
    if state=='history_reparsed':
        trader.broker.ledger.ingest([trade(1,2000,type='reviewed-trade')],[],3000)
        support(monkeypatch)
        case=queued(rig,action='reparse-history',selections=['trade:1'])
        controller.before_service()
    else:
        case=queued(rig)
        trader.protection_generation={'BTCUSDT':1}
        controller.after_service(coverage)
    assert outcome(controller,case)['state']==state
    path=controller.store.path('outcomes',case['case_id'])
    result=module.read_json(path)
    if mutation=='filename': path.rename(path.with_name('0'*32+'.json'))
    elif mutation=='request': controller.store.path('requests',case['case_id']).unlink()
    else:
        field={'case_id':'case_id','action':'action','case_hash':'case_sha256','version':'version','state':'state'}[mutation]
        result[field]={'case_id':'0'*32,'action':'different-action','case_hash':'0'*64,
                       'version':True,'state':'not-a-terminal-state'}[mutation]
        write(path,result)
    before=calls.copy()
    for check in (controller.before_service,lambda:controller.after_service(coverage)):
        with pytest.raises(ValueError): check()
    assert trader.halted and calls['account']==before['account'] and calls['save']==before['save']


@pytest.mark.parametrize('mutation',['receipt','ledger_missing','ledger_identity','downgrade'])
def test_history_outcome_must_match_persisted_ledger_commit(rig,monkeypatch,mutation):
    trader,_,coverage,controller,_=rig
    trader.broker.ledger.ingest([trade(1,2000,type='reviewed-trade')],[],3000)
    support(monkeypatch)
    case=queued(rig,action='reparse-history',selections=['trade:1'])
    controller.before_service()
    path=controller.store.path('outcomes',case['case_id'])
    result=module.read_json(path)
    if mutation=='receipt': result['receipt']['records'][0]['canonical_sha256']='0'*64
    elif mutation=='downgrade': result.update(state='rejected',reason='falsely downgraded')
    elif mutation=='ledger_missing':
        trader.broker.ledger.db.execute('DELETE FROM history_resolution_requests'); trader.broker.ledger.db.commit()
    elif mutation=='ledger_identity':
        trader.broker.ledger.db.execute('UPDATE ledger_identity SET value=?',(json.dumps(['testnet',999,1000]),))
        trader.broker.ledger.db.commit()
    write(path,result)
    for check in (controller.before_service,lambda:controller.after_service(coverage)):
        with pytest.raises((ValueError,UnverifiedExecution)): check()
    assert trader.halted


@pytest.mark.parametrize('crash_before_outcome',[False,True])
def test_completed_history_reparse_survives_later_approved_deployment(rig,monkeypatch,crash_before_outcome):
    trader,_,coverage,controller,calls=rig
    trader.broker.ledger.ingest([trade(1,2000,type='reviewed-trade')],[],3000)
    support(monkeypatch)
    case=queued(rig,action='reparse-history',selections=['trade:1'])
    if crash_before_outcome:
        monkeypatch.setattr(controller,'_result',lambda *a,**k:(_ for _ in ()).throw(OSError('after commit')))
        with pytest.raises(OSError): controller.before_service()
    else: controller.before_service()
    monkeypatch.setattr(module.time,'time',lambda:(NOW+module.CASE_TTL_MS+1000)/1000)
    newer=module.SafetyResolution(trader,'b'*64,100,'testnet',1000)
    newer.before_service(); newer.after_service(coverage)
    assert outcome(newer,case)['state']=='history_reparsed'
    assert outcome(newer,case)['receipt']['fingerprint']==FP
    assert len(trader.broker.ledger.events('trade'))==1
    assert trader.halted and calls['account']==calls['revalidation']==0


def test_immutable_file_refuses_replacement(tmp_path):
    path=tmp_path/'proof.json'
    module.immutable(path,{'proof':'original'})
    module.immutable(path,{'proof':'original'})
    with pytest.raises(ValueError,match='immutable'): module.immutable(path,{'proof':'changed'})
    assert module.read_json(path)=={'proof':'original'}

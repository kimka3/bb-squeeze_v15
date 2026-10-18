import json
from decimal import Decimal
from pathlib import Path
import sys
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]/'src'))
from live.config import LiveConfig
from live.production import load,activation,account_check,health
from live import production
from live.run import atomic_json,persist_report
from test_strategy_integration import Venue,setup,rows,START


def configuration(tmp_path):
    live=LiveConfig(mode='live',account_index=100,api_key_index=4,state_dir=tmp_path/'state')
    cfg={'authentication':{'network':'mainnet','account_index':100,'api_key_index':4,
        'key_file':'/private/key','expected_address':'0x'+'1'*40},'max_initial_capital':'298.56',
        'account_lock_path':str((tmp_path/'account.lock').resolve()),'trading':live.describe()}
    path=tmp_path/'config.json';path.write_text(json.dumps(cfg))
    return path,cfg,live


def healthy_status(now=400000,**changes):
    return {'updated_ms':now,'last_successful_service_ms':now,
            'last_successful_poll_ms':now,**changes}


def test_config_binds_approved_strategy_and_deployed_code(tmp_path):
    path,cfg,live=configuration(tmp_path)
    _,_,fingerprint=load(path)
    cfg['trading']['strategy']['risk']=.01;path.write_text(json.dumps(cfg))
    with pytest.raises(ValueError,match='strategy'):load(path)
    path,cfg,live=configuration(tmp_path)
    cfg['max_initial_capital']='999';path.write_text(json.dumps(cfg))
    assert load(path)[2]!=fingerprint


def test_no_activation_or_changed_config_cannot_start_worker(tmp_path,monkeypatch):
    path,cfg,live=configuration(tmp_path);_,_,fingerprint=load(path)
    monkeypatch.setattr(production,'connect',lambda c:pytest.fail('must reject before credentials/transport'))
    with pytest.raises(FileNotFoundError):production.run_service(cfg,live,fingerprint)
    atomic_json(live.state_dir/'activation.json',{'approved':True,'account_index':100,'fingerprint':'wrong'})
    with pytest.raises(RuntimeError,match='mismatched'):production.run_service(cfg,live,fingerprint)


def test_start_cannot_be_automated_through_noninteractive_stdin(tmp_path,monkeypatch):
    path,cfg,live=configuration(tmp_path)
    monkeypatch.setattr(sys,'argv',['production','start','--config',str(path)])
    monkeypatch.setattr(sys.stdin,'isatty',lambda:False)
    monkeypatch.setattr(production,'connect',lambda c:pytest.fail('no reads before human guard'))
    with pytest.raises(RuntimeError,match='human'):production.main()
    assert not (live.state_dir/'activation.json').exists()


def test_account_preflight_rejects_unapproved_capital_or_existing_exposure():
    class T:
        account_index=100
        def account(self):return {'total_asset_value':'298.55','available_balance':'298.55','positions':[]}
        def active_orders(self):return []
        def get(self,path):return {'user_tier':'standard','current_maker_fee_tick':0,'current_taker_fee_tick':0}
    t=T();assert account_check(t,'298.56')['equity']=='298.55'
    with pytest.raises(RuntimeError,match='authorization'):account_check(t,'100')
    t.active_orders=lambda:[{'order_index':1}]
    with pytest.raises(RuntimeError,match='flat'):account_check(t,'298.56')


def test_health_uses_service_success_not_just_recent_mark_poll(tmp_path):
    live=LiveConfig(state_dir=tmp_path)
    atomic_json(tmp_path/'runner_status.json',{'updated_ms':400000,'last_successful_poll_ms':400000,
        'last_successful_service_ms':100000})
    assert not health(live,now_ms=400000)['healthy']
    atomic_json(tmp_path/'runner_status.json',healthy_status())
    assert health(live,now_ms=400000)['healthy']
    assert not health(live,now_ms=400000,active=False)['healthy']


def test_operator_pause_blocks_entry_but_keeps_protection_service(tmp_path):
    x,b,t=setup(tmp_path)
    pause=[False];b.entry_guard=lambda:not pause[0]
    try:
        t.on_bar(START,rows(),{'BTCUSDT':100,'ETHUSDT':100},now_ms=x.now)
        before=len(x.sent); pause[0]=True
        b.poll(); t.service(now_ms=x.now)
        assert not t.halted and len(x.sent)==before and x.active_orders()
        with pytest.raises(ValueError,match='paused'):b.validate_entry(t.markets['ETHUSDT'],'SHORT',1,100,.5,90)
        persist_report(t,t.config)
        status=json.loads((tmp_path/'runner_status.json').read_text())
        assert status['entries_paused'] and status['entry_pause_observed_ms']
    finally:b.close()


def test_extra_deposit_does_not_enlarge_authorized_allocation(tmp_path):
    x,b,t=setup(tmp_path);original=x.account
    b.initial_allocated_equity='298.55'
    def account():return {**original(),'total_asset_value':'10298.55'}
    x.account=account
    try:
        state=b.account_state()
        assert state.equity==298.55 and state.ledger_report['actual_account_equity']=='10298.55'
        t.on_bar(START,rows(),{'BTCUSDT':100,'ETHUSDT':100},now_ms=x.now)
        assert 0<=298.55*.02-t.positions['ETHUSDT']['initial_risk']<.0004
        assert b.account_state().equity==pytest.approx(298.55)
        # Price marks in account position value change unrealized PnL, permitting
        # owned profit to compound but never admitting the unrelated deposit.
        before=x.account
        def profitable():
            a=before();a['positions'][0]['position_value']=str(Decimal(a['positions'][0]['position_value'])-10)
            return a
        x.account=profitable
        assert b.account_state().equity==pytest.approx(308.55)
    finally:b.close()


def test_health_monitor_does_not_arm_or_notify_before_activation(tmp_path,monkeypatch):
    path,cfg,live=configuration(tmp_path)
    monkeypatch.setattr(sys,'argv',['production','health','--config',str(path)])
    class Result:returncode=3
    monkeypatch.setattr(production.subprocess,'run',lambda *a,**kw:Result())
    production.main()
    assert not live.state_dir.exists()


def test_operational_alarm_pauses_entries_and_recovery_never_auto_resumes(tmp_path,monkeypatch):
    path,cfg,live=configuration(tmp_path)
    atomic_json(live.state_dir/'activation.json',{'approved':True})
    atomic_json(live.state_dir/'runner_status.json',{'updated_ms':0,'last_successful_service_ms':0})
    monkeypatch.setattr(sys,'argv',['production','health','--config',str(path)])
    class Result:returncode=0
    monkeypatch.setattr(production.subprocess,'run',lambda *a,**kw:Result())
    production.main()
    assert (live.state_dir/'ENTRY_PAUSED').exists()
    now=int(production.time.time()*1000)
    atomic_json(live.state_dir/'runner_status.json',healthy_status(now))
    production.main()
    assert json.loads((live.state_dir/'attention.json').read_text())['healthy']
    assert (live.state_dir/'ENTRY_PAUSED').exists()


@pytest.mark.parametrize('key',['updated_ms','last_successful_service_ms','last_successful_poll_ms'])
@pytest.mark.parametrize('stamp',[None,0,-1,'400000',True,400001,400000.5,float('nan'),float('inf')])
def test_health_invalid_or_future_timestamps_fail_closed(tmp_path,key,stamp):
    live=LiveConfig(state_dir=tmp_path)
    # Deliberately bypass the normal writer's allow_nan=False to simulate a
    # corrupted or externally rewritten status file.
    (tmp_path/'runner_status.json').write_text(json.dumps(healthy_status(**{key:stamp})))
    result=health(live,now_ms=400000)
    assert not result['healthy']
    assert any('timestamp' in issue for issue in result['issues'])


def test_poll_freshness_uses_existing_180_second_limit(tmp_path):
    live=LiveConfig(state_dir=tmp_path)
    atomic_json(tmp_path/'runner_status.json',healthy_status(last_successful_poll_ms=220000))
    assert health(live,now_ms=400000)['healthy']
    atomic_json(tmp_path/'runner_status.json',healthy_status(last_successful_poll_ms=219999))
    assert 'market poll has no recent successful read' in health(live,now_ms=400000)['issues']


@pytest.mark.parametrize('payload',['{broken','[]','null'])
def test_malformed_status_fails_closed_instead_of_crashing_monitor(tmp_path,payload):
    live=LiveConfig(state_dir=tmp_path)
    (tmp_path/'runner_status.json').write_text(payload)
    result=health(live,now_ms=400000)
    assert not result['healthy']
    assert 'runner status unavailable or malformed' in result['issues']


def test_monitor_latches_pause_when_status_is_malformed(tmp_path,monkeypatch):
    path,cfg,live=configuration(tmp_path)
    atomic_json(live.state_dir/'activation.json',{'approved':True})
    (live.state_dir/'runner_status.json').write_text('{broken')
    monkeypatch.setattr(sys,'argv',['production','health','--config',str(path)])
    class Result:returncode=0
    monkeypatch.setattr(production.subprocess,'run',lambda *a,**kw:Result())
    production.main()
    pause=json.loads((live.state_dir/'ENTRY_PAUSED').read_text())
    assert 'runner status unavailable or malformed' in pause['issues']
    assert not json.loads((live.state_dir/'attention.json').read_text())['healthy']


def test_historical_error_and_coverage_do_not_block_recovery_but_current_gap_blocks_resume(tmp_path):
    live=LiveConfig(state_dir=tmp_path)
    status=healthy_status(last_error='service: HTTP 502',service_errors=13,
                          runtime_coverage={'coverage_complete':False,'active_gap_streams':[]})
    atomic_json(tmp_path/'runner_status.json',status)
    assert health(live,now_ms=400000,for_resume=True)['healthy']
    status['runtime_coverage']['active_gap_streams']=['service']
    atomic_json(tmp_path/'runner_status.json',status)
    # Preserve the monitor's time threshold; resume needs affirmative recovery.
    assert health(live,now_ms=400000)['healthy']
    assert not health(live,now_ms=400000,for_resume=True)['healthy']


@pytest.mark.parametrize('change,issue',[
    ({'orphan_orders':{'ETHUSDT':{'stop':{'client_order_index':123}}}},'orphan orders'),
    ({'api_read_diagnostics_error':True},'diagnostics'),
    ({'api_read_diagnostics_error':'disk full'},'diagnostics'),
])
def test_health_rejects_unreconciled_orders_or_failed_api_evidence(tmp_path,change,issue):
    live=LiveConfig(state_dir=tmp_path)
    atomic_json(tmp_path/'runner_status.json',healthy_status(**change))
    result=health(live,now_ms=400000)
    assert not result['healthy'] and any(issue in item for item in result['issues'])


def prepare_resume(tmp_path,monkeypatch):
    path,cfg,live=configuration(tmp_path);_,_,fingerprint=load(path)
    atomic_json(live.state_dir/'activation.json',{'approved':True,'account_index':100,'fingerprint':fingerprint})
    atomic_json(live.state_dir/'runner_status.json',healthy_status())
    atomic_json(live.state_dir/'ENTRY_PAUSED',{'reason':'operational monitor'})
    monkeypatch.setattr(production.time,'time',lambda:400.0)
    monkeypatch.setattr(sys,'argv',['production','resume','--config',str(path)])
    monkeypatch.setattr(sys.stdin,'isatty',lambda:True)
    monkeypatch.setattr(production,'connect',lambda *a:pytest.fail('resume must not send orders'))
    return live


def test_resume_rechecks_actual_service_after_confirmation(tmp_path,monkeypatch):
    live=prepare_resume(tmp_path,monkeypatch);calls=[]
    def run(args,**kwargs):
        calls.append(args)
        class Result:returncode=0 if len(calls)==1 else 3
        return Result()
    monkeypatch.setattr(production.subprocess,'run',run)
    monkeypatch.setattr('builtins.input',lambda _: 'RESUME LIVE 100')
    with pytest.raises(RuntimeError,match='health changed'):production.main()
    assert len(calls)==2 and all(call==['systemctl','is-active','--quiet',production.UNIT] for call in calls)
    assert (live.state_dir/'ENTRY_PAUSED').exists()


def test_resume_rechecks_active_failure_after_confirmation(tmp_path,monkeypatch):
    live=prepare_resume(tmp_path,monkeypatch)
    class Result:returncode=0
    monkeypatch.setattr(production.subprocess,'run',lambda *a,**kw:Result())
    def confirm(_):
        atomic_json(live.state_dir/'runner_status.json',healthy_status(
            runtime_coverage={'active_gap_streams':['poll']}))
        return 'RESUME LIVE 100'
    monkeypatch.setattr('builtins.input',confirm)
    with pytest.raises(RuntimeError,match='health changed'):production.main()
    assert (live.state_dir/'ENTRY_PAUSED').exists()


@pytest.mark.parametrize('change',[
    {'halted':True,'halt_reasons':None},
    {'runtime_coverage':{'active_gap_streams':['service']}},
    {'orphan_orders':{'ETHUSDT':{'stop':123}}},
    {'api_read_diagnostics_error':True},
])
def test_resume_blocks_before_confirmation_when_safety_evidence_unresolved(tmp_path,monkeypatch,change):
    live=prepare_resume(tmp_path,monkeypatch)
    atomic_json(live.state_dir/'runner_status.json',healthy_status(**change))
    class Result:returncode=0
    monkeypatch.setattr(production.subprocess,'run',lambda *a,**kw:Result())
    monkeypatch.setattr('builtins.input',lambda _:pytest.fail('must reject before human confirmation'))
    with pytest.raises(RuntimeError,match='cannot resume'):production.main()
    assert (live.state_dir/'ENTRY_PAUSED').exists()


def test_human_resume_accepts_recovered_state_and_explains_latched_pause(tmp_path,monkeypatch):
    live=prepare_resume(tmp_path,monkeypatch)
    class Result:returncode=0
    monkeypatch.setattr(production.subprocess,'run',lambda *a,**kw:Result())
    monkeypatch.setattr('builtins.input',lambda _: 'RESUME LIVE 100')
    status=health(live,now_ms=400000)
    assert status['healthy'] and status['entries_paused']
    assert status['entry_pause_reason']=='operational monitor'
    production.main()
    assert not (live.state_dir/'ENTRY_PAUSED').exists()


def test_stop_with_open_exposure_only_pauses_and_never_stops_service(tmp_path,monkeypatch):
    path,cfg,live=configuration(tmp_path)
    monkeypatch.setattr(sys,'argv',['production','stop','--config',str(path)])
    class Result:returncode=3
    monkeypatch.setattr(production.subprocess,'run',lambda *a,**kw:Result())
    class T:
        def account(self):return {'positions':[{'market_id':0,'position':'1','sign':1}]}
        def close(self):pass
    monkeypatch.setattr(production,'connect',lambda c:T())
    monkeypatch.setattr(production,'systemctl',lambda *a:pytest.fail('must not stop with exposure'))
    with pytest.raises(RuntimeError,match='exposure'):production.main()
    assert (live.state_dir/'ENTRY_PAUSED').exists()


def test_service_recovers_existing_campaign_before_running_loop(tmp_path,monkeypatch):
    path,cfg,live=configuration(tmp_path);_,_,fingerprint=load(path)
    x,b,t=setup(tmp_path/'synthetic');b.close()
    calls=[]
    class Broker:
        def poll(self):calls.append('poll')
        def close(self):calls.append('broker_close')
    class J:
        def append(self,*a,**kw):calls.append('journal')
    class Trader:
        broker=Broker();journal=J()
        def service(self):calls.append('recover')
    class Transport:
        def close(self):calls.append('transport_close')
    atomic_json(live.state_dir/'activation.json',{'approved':True,'account_index':100,
        'fingerprint':fingerprint,'started_at_ms':1234,'initial_equity':'298.55'})
    monkeypatch.setattr(production,'connect',lambda c:Transport())
    def factory(*a,**kw):
        assert kw['start_ms']==1234 and kw['writes_enabled'] is True
        assert kw['account_lock_path']==cfg['account_lock_path']
        return Trader(),{}
    monkeypatch.setattr(production.runner,'build_authenticated_strategy',factory)
    monkeypatch.setattr(production.runner,'run_loop',lambda *a:calls.append('loop'))
    monkeypatch.setattr(production.signal,'signal',lambda *a:None)
    production.run_service(cfg,live,fingerprint)
    assert calls==['poll','recover','journal','loop','broker_close','transport_close']

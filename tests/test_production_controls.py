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
    atomic_json(tmp_path/'runner_status.json',{'updated_ms':400000,'last_successful_service_ms':400000})
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
    atomic_json(live.state_dir/'runner_status.json',{'updated_ms':now,'last_successful_service_ms':now})
    production.main()
    assert json.loads((live.state_dir/'attention.json').read_text())['healthy']
    assert (live.state_dir/'ENTRY_PAUSED').exists()


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

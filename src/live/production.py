"""User-operated production controls. Default/check/health never send orders.

Only `start`, explicitly typed in a human terminal, authorizes the systemd
service. Installation and preflight cannot create activation credentials.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from frontier_engine import Strategy
from .authenticated import HOSTS,LighterTransport
from .config import LiveConfig,DEFAULT_UNIVERSE
from .execution import positions
from .execution_evidence import number
from .process_lock import ProcessLock
from . import run as runner

UNIT='bb-squeeze-live.service'
TIMER='bb-squeeze-live-health.timer'
DISCLOSURE=('Individual fee fields remain unknown; REST/network outages may delay stop placement, '
            'amendments or residual close. Native stop registration is not guaranteed liquidation. '
            'This starts real automated orders, not a profitability test result.')


def load(path):
    path=Path(path)
    cfg=json.loads(path.read_text(encoding='utf-8-sig'))
    trading=dict(cfg['trading']); trading['strategy']=Strategy(**trading['strategy'])
    live=LiveConfig(**trading)
    auth=cfg['authentication']
    if (live.mode!='live' or live.base_url!=HOSTS[auth['network']] or
            live.account_index!=auth['account_index'] or live.api_key_index!=auth['api_key_index']):
        raise ValueError('production identity mismatch')
    default=LiveConfig()
    if live.universe!=DEFAULT_UNIVERSE or live.strategy!=default.strategy:
        raise ValueError('production must preserve the approved strategy and universe')
    if live.poll_seconds!=60 or not live.passive_take_profit or live.max_slippage_pct!=.005:
        raise ValueError('production execution settings differ from reviewed defaults')
    if number(cfg['max_initial_capital'])<=0:raise ValueError('initial capital ceiling required')
    if not Path(cfg['account_lock_path']).is_absolute():raise ValueError('shared account lock required')
    # Bind activation to both configuration and immutable deployed Python code.
    code=hashlib.sha256()
    for source in sorted(Path(__file__).parents[1].rglob('*.py')):
        code.update(str(source.relative_to(Path(__file__).parents[1])).encode())
        code.update(source.read_bytes())
    fingerprint=hashlib.sha256(path.read_bytes()+code.digest()).hexdigest()
    return cfg,live,fingerprint


def connect(cfg):return LighterTransport(**cfg['authentication'])


def account_check(t,ceiling):
    a=t.account(); active=t.active_orders(); limits=t.get(f'/api/v1/accountLimits?account_index={t.account_index}')
    if positions(a) or active:raise RuntimeError('first activation requires flat account and no active orders')
    equity=number(a['total_asset_value']); available=number(a['available_balance'])
    if not 0<equity<=number(ceiling) or available<equity:
        raise RuntimeError('initial equity/available balance exceeds authorization or is unavailable')
    if (limits.get('user_tier')!='standard' or limits.get('current_maker_fee_tick')!=0 or
            limits.get('current_taker_fee_tick')!=0):raise RuntimeError('fee tier differs from reviewed zero-rate policy')
    return {'equity':str(equity),'available':str(available),'open_positions':0,'active_orders':0,
            'fee_tier':'standard','individual_fee_evidence_complete':False}


def preflight(cfg,live,fingerprint):
    # Separate diagnostics: do not write a campaign snapshot or an activation.
    with ProcessLock(cfg['account_lock_path']):
        t=connect(cfg)
        try:
            account=account_check(t,cfg['max_initial_capital'])
            markets=runner.load_markets(live.base_url,live.lighter_universe,fetch=t.get)
            frames=runner.prepare_frames(live)
            stamp,_=runner.feed.latest_closed(frames)
            age=int(time.time()*1000)-(stamp+runner.H4)
            if not 0<=age<runner.H4:raise RuntimeError('confirmed signal data not current')
            return {'checked_at_ms':int(time.time()*1000),'ready_for_user_start':True,
                    'account':account,'market_count':len(markets),'latest_closed_bar_ms':stamp,
                    'bar_counts':{s:len(v) for s,v in frames.items()},'fingerprint':fingerprint,
                    'orders_submitted':0,'strategy_enabled':False,'limitations':DISCLOSURE}
        finally:t.close()


def activation(path,fingerprint,account):
    data=json.loads(Path(path).read_text())
    if data.get('fingerprint')!=fingerprint or data.get('account_index')!=account or data.get('approved') is not True:
        raise RuntimeError('missing or mismatched human activation')
    return data


def health(live,*,active=True,now_ms=None):
    now_ms=int(time.time()*1000) if now_ms is None else now_ms
    path=live.state_dir/'runner_status.json'; issues=[]
    state=json.loads(path.read_text()) if path.exists() else {}
    if not active:issues.append('strategy service is not active')
    if now_ms-state.get('updated_ms',0)>180000:issues.append('runner heartbeat stale')
    if now_ms-(state.get('last_successful_service_ms') or 0)>180000:issues.append('protection service has no recent successful reconciliation')
    if state.get('halted'):issues.append('persistent strategy HALT: '+'; '.join(state.get('halt_reasons',[])))
    if state.get('pending_operation'):issues.append('unresolved order operation')
    if state.get('residual_exits'):issues.append('residual position requires close reconciliation')
    if state.get('data_gaps'):issues.append('signal data gap unresolved')
    return {'checked_at_ms':now_ms,'healthy':not issues,'issues':issues,
            'entries_paused':(live.state_dir/'ENTRY_PAUSED').exists(),
            'last_successful_service_ms':state.get('last_successful_service_ms'),
            'alert_delivery':'local attention.json and system journal; no external notifications'}


def run_service(cfg,live,fingerprint):
    approval=activation(live.state_dir/'activation.json',fingerprint,live.account_index)
    with ProcessLock(live.state_dir/'runner.lock'):
        runner.STOP_REQUESTED.clear()
        signal.signal(signal.SIGTERM,lambda *_:runner.STOP_REQUESTED.set())
        signal.signal(signal.SIGINT,lambda *_:runner.STOP_REQUESTED.set())
        t=connect(cfg); trader=None
        try:
            trader,markets=runner.build_authenticated_strategy(live,t,start_ms=approval['started_at_ms'],
                writes_enabled=True,account_lock_path=cfg['account_lock_path'])
            trader.broker.initial_allocated_equity=approval['initial_equity']
            trader.broker.entry_guard=lambda:not (live.state_dir/'ENTRY_PAUSED').exists()
            # No on_bar/entry until first account reconciliation and recovery.
            trader.broker.poll(); trader.service()
            trader.journal.append('production_start',fingerprint=fingerprint,
                                  allocated_initial_equity=approval['initial_equity'])
            runner.run_loop(trader,markets,live,float('inf'))
        finally:
            if trader is not None:trader.broker.close()
            t.close()


def systemctl(*args):subprocess.run(['systemctl',*args],check=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=('check','start','resume','run','pause','stop','status','health','logs'),nargs='?',default='check')
    p.add_argument('--config',required=True,type=Path)
    args=p.parse_args(); cfg,live,fingerprint=load(args.config)
    if args.command=='check':
        result=preflight(cfg,live,fingerprint)
        print(json.dumps(result,indent=2)); return
    if args.command=='run':run_service(cfg,live,fingerprint);return
    if args.command=='logs':
        subprocess.run(['journalctl','-u',UNIT,'-n','60','--no-pager'],check=True);return
    if args.command=='pause':
        live.state_dir.mkdir(parents=True,exist_ok=True)
        runner.atomic_json(live.state_dir/'ENTRY_PAUSED',{'reason':'operator pause','at_ms':int(time.time()*1000)})
        print('New entries paused. Existing position protection continues; no liquidation requested.');return
    if args.command in ('status','health'):
        active=subprocess.run(['systemctl','is-active','--quiet',UNIT]).returncode==0
        result=health(live,active=active)
        if args.command=='health':
            # Monitor only an explicitly activated campaign; installation alone
            # must never create a false failure or authorize a writer.
            if not (live.state_dir/'activation.json').exists():return
            old=live.state_dir/'attention.json'
            previous=json.loads(old.read_text()) if old.exists() else {}
            if result['issues'] and not (live.state_dir/'ENTRY_PAUSED').exists():
                runner.atomic_json(live.state_dir/'ENTRY_PAUSED',{'reason':'operational monitor','issues':result['issues']})
                result['entries_paused']=True
            runner.atomic_json(old,result)
            if previous.get('issues')!=result['issues']:print(json.dumps(result),flush=True)
        else:
            result['service_active']=active
            result['activation_present']=(live.state_dir/'activation.json').exists()
            for key in ('report','runner_status'):
                file=live.state_dir/(key+'.json')
                result[key]=json.loads(file.read_text()) if file.exists() else None
            print(json.dumps(result,indent=2))
        return
    if args.command=='stop':
        # Pause first; stopping a process is not a confirmed position close.
        paused_at=int(time.time()*1000)
        runner.atomic_json(live.state_dir/'ENTRY_PAUSED',{'reason':'operator stop requested','at_ms':paused_at})
        active=subprocess.run(['systemctl','is-active','--quiet',UNIT]).returncode==0
        if active:
            end=time.monotonic()+180
            while time.monotonic()<end:
                file=live.state_dir/'runner_status.json'
                state=json.loads(file.read_text()) if file.exists() else {}
                if (state.get('entry_pause_observed_ms') or 0)>=paused_at:
                    if state.get('pending_operation') or state.get('residual_exits'):
                        raise RuntimeError('unresolved execution; protection service kept running')
                    break
                time.sleep(1)
            else:raise RuntimeError('pause not acknowledged; inspect service before stopping')
        t=connect(cfg)
        try:
            if positions(t.account()) or t.active_orders():
                raise RuntimeError('exposure/orders remain; entries paused, protection service kept running')
        finally:t.close()
        systemctl('stop',UNIT);systemctl('disable',UNIT)
        systemctl('disable','--now',TIMER)
        print('Service stopped. Activation/history retained; restart requires start command.');return
    if args.command=='resume':
        if not sys.stdin.isatty():raise RuntimeError('resume requires a human interactive terminal')
        activation(live.state_dir/'activation.json',fingerprint,live.account_index)
        active=subprocess.run(['systemctl','is-active','--quiet',UNIT]).returncode==0
        state=health(live,active=active)
        if not state['healthy']:raise RuntimeError('cannot resume: '+ '; '.join(state['issues']))
        text=f'RESUME LIVE {live.account_index}'
        if input('Type '+text+': ').strip()!=text:raise RuntimeError('entries remain paused')
        # Recheck after potentially delayed input; never clear a safety HALT.
        if not health(live,active=True)['healthy']:raise RuntimeError('health changed; entries remain paused')
        (live.state_dir/'ENTRY_PAUSED').unlink(missing_ok=True)
        print('Operator pause cleared. Existing strategy HALT is never reset.');return
    if args.command=='start':
        if not sys.stdin.isatty():raise RuntimeError('start requires a human interactive terminal')
        if (live.state_dir/'activation.json').exists():
            activation(live.state_dir/'activation.json',fingerprint,live.account_index)
            text=f'RESTART LIVE {live.account_index}'
            if input('Retain campaign, HALT and pause state. Type '+text+': ').strip()!=text:
                raise RuntimeError('not restarted')
            systemctl('enable','--now',UNIT);systemctl('enable','--now',TIMER)
            print('Existing campaign service started; no state reset. Inspect status before resume.');return
        checked=preflight(cfg,live,fingerprint)
        print(json.dumps(checked,indent=2));print(DISCLOSURE)
        text=f"START LIVE {live.account_index} {cfg['max_initial_capital']}"
        if input('Type '+text+': ').strip()!=text:raise RuntimeError('not activated')
        with ProcessLock(cfg['account_lock_path']):
            t=connect(cfg)
            try:account=account_check(t,cfg['max_initial_capital'])
            finally:t.close()
            live.state_dir.mkdir(parents=True,exist_ok=True)
            if any(live.state_dir.glob('execution/*')):raise RuntimeError('existing execution state; refusing new campaign')
            if (live.state_dir/'snapshot.json').exists():raise RuntimeError('existing strategy state')
            runner.atomic_json(live.state_dir/'activation.json',{'approved':True,'account_index':live.account_index,
                'fingerprint':fingerprint,'started_at_ms':int(time.time()*1000),'initial_equity':account['equity'],
                'capital_ceiling':cfg['max_initial_capital'],'limitations_acknowledged':DISCLOSURE})
        systemctl('enable','--now',UNIT);systemctl('enable','--now',TIMER)
        print('Human-authorized strategy service started. Use status/logs to verify actual health.')


if __name__=='__main__':main()

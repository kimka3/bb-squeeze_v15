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
from .recovery import ExchangeRecovery, operator_pause, safety_pause, OWNER
from .authenticated import is_transient_read_error
from .request_budget import RequestDeferred
from .safety_resolution import SafetyResolution

UNIT='bb-squeeze-live.service'
TIMER='bb-squeeze-live-health.timer'
DISCLOSURE=('Individual fee fields remain unknown; REST/network outages may delay stop placement, '
            'amendments or residual close. Native stop registration is not guaranteed liquidation. '
            'Temporary read outages automatically recover after three clean cycles over at least two minutes; '
            'operator pauses and safety HALTs are never automatically cleared. '
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
    if not isinstance(data,dict) or data.get('fingerprint')!=fingerprint or data.get('account_index')!=account or data.get('approved') is not True:
        raise RuntimeError('missing or mismatched human activation')
    return data


def health(live,*,active=True,now_ms=None,for_resume=False):
    path=live.state_dir/'runner_status.json'; issues=[]
    try:
        state=json.loads(path.read_text())
        if not isinstance(state,dict):raise ValueError('status must be an object')
    except (OSError,UnicodeError,ValueError):
        state={};issues.append('runner status unavailable or malformed')
    # Capture time after the atomic status read: an update racing the read must
    # not look like a timestamp from the future merely because we sampled first.
    now_ms=int(time.time()*1000) if now_ms is None else now_ms
    def recent(key,label,stale_message):
        stamp=state.get(key)
        if type(stamp) is not int or stamp<=0:
            issues.append(label+' timestamp missing or invalid')
        elif stamp>now_ms:
            issues.append(label+' timestamp is in the future')
        elif now_ms-stamp>180000:
            issues.append(stale_message)
    if not active:issues.append('strategy service is not active')
    recent('updated_ms','runner heartbeat','runner heartbeat stale')
    recent('last_successful_service_ms','protection service','protection service has no recent successful reconciliation')
    recent('last_successful_poll_ms','market poll','market poll has no recent successful read')
    if state.get('halted'):
        reasons=state.get('halt_reasons',[])
        detail='; '.join(reasons) if isinstance(reasons,list) and all(isinstance(r,str) for r in reasons) else 'reason unavailable'
        issues.append('persistent strategy HALT: '+detail)
    if state.get('pending_operation'):issues.append('unresolved order operation')
    if state.get('unresolved_operations'):issues.append('isolated unresolved order operations')
    if state.get('residual_exits'):issues.append('residual position requires close reconciliation')
    if state.get('orphan_orders'):issues.append('orphan orders require reconciliation')
    if state.get('data_gaps'):issues.append('signal data gap unresolved')
    if state.get('api_read_diagnostics_error'):issues.append('API read diagnostics could not be persisted')
    recovery=state.get('automatic_recovery')
    if isinstance(recovery,dict) and recovery.get('storage_error'):
        issues.append('automatic recovery evidence could not be persisted')
    # Existing deployments may not yet emit the optional coverage/diagnostic
    # fields. Historical failures do not prohibit recovery. A human resume,
    # however, must not clear the latch before a current failed stream recovers.
    coverage=state.get('runtime_coverage')
    if coverage is not None and not isinstance(coverage,dict):
        issues.append('runtime coverage malformed')
    elif for_resume and coverage is not None:
        gaps=coverage.get('active_gap_streams',[])
        if not isinstance(gaps,list):issues.append('runtime active gap status malformed')
        elif gaps:issues.append('runtime data collection has an active failure')
    paused=(live.state_dir/'ENTRY_PAUSED').exists() or (live.state_dir/'EXCHANGE_PAUSED').exists()
    pause_reason=None
    if (live.state_dir/'ENTRY_PAUSED').exists():
        try:
            pause=json.loads((live.state_dir/'ENTRY_PAUSED').read_text())
            reason=pause.get('reason') if isinstance(pause,dict) else None
            pause_reason=(reason if reason in ('operator pause','operational monitor','operator stop requested')
                          else 'unrecognized pause marker')
        except (OSError,UnicodeError,ValueError):pause_reason='unreadable pause marker'
    elif (live.state_dir/'EXCHANGE_PAUSED').exists():
        pause_reason=('owned execution publication pending' if state.get('publication_pending') is True
                      else 'exchange recovery checks pending')
    if for_resume and (live.state_dir/'EXCHANGE_PAUSED').exists():
        issues.append('automatic exchange recovery checks still pending')
    return {'checked_at_ms':now_ms,'healthy':not issues,'issues':issues,
            'entries_paused':paused,'entry_pause_reason':pause_reason,
            'automatic_recovery':recovery,
            'publication_pending':state.get('publication_pending', False),
            'publication_pending_evidence':state.get('publication_pending_evidence', []),
            'last_successful_service_ms':state.get('last_successful_service_ms'),
            'last_successful_poll_ms':state.get('last_successful_poll_ms'),
            'alert_delivery':'local attention.json and system journal; no external notifications'}


def monitor_managed_recovery(live, fingerprint, result):
    """Do not turn a supervised temporary outage into a permanent manual pause."""
    data=result.get('automatic_recovery')
    if not isinstance(data,dict) or data.get('storage_error') or data.get('enabled') is not True:return False
    if (data.get('owner')!=OWNER or data.get('fingerprint')!=fingerprint or
            data.get('state')!='paused'):return False
    try:
        marker=json.loads((live.state_dir/'EXCHANGE_PAUSED').read_text())
        if (not isinstance(marker,dict) or marker.get('session')!=data.get('session')
                or marker.get('owner')!=OWNER or marker.get('fingerprint')!=fingerprint
                or marker.get('state')!='paused'):return False
        approved=activation(live.state_dir/'activation.json',fingerprint,live.account_index)
        if approved.get('automatic_recovery_enabled') is not True:return False
    except (OSError,ValueError,RuntimeError):return False
    allowed={'protection service has no recent successful reconciliation',
             'market poll has no recent successful read'}
    if data.get('phase')=='bootstrap_wait':
        if result.get('last_successful_service_ms') is None:
            allowed.add('protection service timestamp missing or invalid')
        if result.get('last_successful_poll_ms') is None:
            allowed.add('market poll timestamp missing or invalid')
    return bool(result['issues']) and set(result['issues'])<=allowed


def bootstrap_status(live, recovery):
    previous=live.state_dir/'runner_status.json'
    old=json.loads(previous.read_text()) if previous.exists() else {}
    if not isinstance(old,dict):raise RuntimeError('malformed previous runner state')
    runner.atomic_json(previous,{**old,'updated_ms':int(time.time()*1000),
        'automatic_recovery':{**recovery.snapshot(),'phase':'bootstrap_wait'},
        'entries_paused':True,'entry_pause_observed_ms':int(time.time()*1000)})


def run_service(cfg,live,fingerprint):
    approval=activation(live.state_dir/'activation.json',fingerprint,live.account_index)
    with ProcessLock(live.state_dir/'runner.lock'):
        runner.STOP_REQUESTED.clear()
        signal.signal(signal.SIGTERM,lambda *_:runner.STOP_REQUESTED.set())
        signal.signal(signal.SIGINT,lambda *_:runner.STOP_REQUESTED.set())
        t=None; trader=None
        recovery=(ExchangeRecovery(live.state_dir,fingerprint)
                  if approval.get('automatic_recovery_enabled') is True else None)
        try:
            # A restart during exchange maintenance must not exhaust systemd's
            # finite crash-restart budget. Only classified read failures wait.
            while True:
                try:
                    if t is None:t=connect(cfg)
                    trader,markets=runner.build_authenticated_strategy(live,t,start_ms=approval['started_at_ms'],
                        writes_enabled=True,account_lock_path=cfg['account_lock_path'])
                    break
                except Exception as exc:
                    if recovery is None or not (is_transient_read_error(exc) or isinstance(exc,RequestDeferred)):
                        raise
                    recovery.failure(exc,'bootstrap')
                    bootstrap_status(live,recovery)
                    if runner.STOP_REQUESTED.wait(live.poll_seconds):return
            trader.broker.initial_allocated_equity=approval['initial_equity']
            trader.broker.entry_guard=lambda:(not (live.state_dir/'ENTRY_PAUSED').exists()
                                              and not getattr(t,'read_diagnostics_error',None)
                                              and (recovery.can_enter() if recovery else
                                                   not (live.state_dir/'EXCHANGE_PAUSED').exists()))
            trader.exchange_recovery=recovery
            trader.safety_resolution=SafetyResolution(trader, fingerprint, live.account_index,
                next(network for network, host in HOSTS.items() if host == live.base_url),
                approval['started_at_ms'])
            # No on_bar/entry until first account reconciliation and recovery.
            if recovery is None:
                trader.broker.poll(); trader.service()
            trader.journal.append('production_start',fingerprint=fingerprint,
                                  allocated_initial_equity=approval['initial_equity'])
            runner.run_loop(trader,markets,live,float('inf'))
        finally:
            if trader is not None:trader.broker.close()
            if t is not None:t.close()


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
        operator_pause(live.state_dir,'operator pause')
        print('New entries paused. Existing position protection continues; no liquidation requested.');return
    if args.command in ('status','health'):
        active=subprocess.run(['systemctl','is-active','--quiet',UNIT]).returncode==0
        result=health(live,active=active)
        if args.command=='health':
            # Monitor only an explicitly activated campaign; installation alone
            # must never create a false failure or authorize a writer.
            if not (live.state_dir/'activation.json').exists():return
            old=live.state_dir/'attention.json'
            try:previous=json.loads(old.read_text()) if old.exists() else {}
            except (OSError,ValueError):previous={}
            if not isinstance(previous,dict):previous={}
            if result['issues'] and not monitor_managed_recovery(live,fingerprint,result):
                safety_pause(live.state_dir,result['issues'])
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
        operator_pause(live.state_dir,'operator stop requested',at_ms=paused_at)
        active=subprocess.run(['systemctl','is-active','--quiet',UNIT]).returncode==0
        if active:
            end=time.monotonic()+180
            while time.monotonic()<end:
                file=live.state_dir/'runner_status.json'
                state=json.loads(file.read_text()) if file.exists() else {}
                if (state.get('entry_pause_observed_ms') or 0)>=paused_at:
                    if state.get('pending_operation') or state.get('unresolved_operations') or state.get('residual_exits'):
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
        state=health(live,active=active,for_resume=True)
        if not state['healthy']:raise RuntimeError('cannot resume: '+ '; '.join(state['issues']))
        text=f'RESUME LIVE {live.account_index}'
        if input('Type '+text+': ').strip()!=text:raise RuntimeError('entries remain paused')
        # Recheck after potentially delayed input; never clear a safety HALT.
        active=subprocess.run(['systemctl','is-active','--quiet',UNIT]).returncode==0
        if not health(live,active=active,for_resume=True)['healthy']:raise RuntimeError('health changed; entries remain paused')
        with ProcessLock(live.state_dir/'entry-controls.lock'):
            if not health(live,active=active,for_resume=True)['healthy']:
                raise RuntimeError('health changed; entries remain paused')
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
                'automatic_recovery_enabled':True,
                'capital_ceiling':cfg['max_initial_capital'],'limitations_acknowledged':DISCLOSURE})
        systemctl('enable','--now',UNIT);systemctl('enable','--now',TIMER)
        print('Human-authorized strategy service started. Use status/logs to verify actual health.')


if __name__=='__main__':main()

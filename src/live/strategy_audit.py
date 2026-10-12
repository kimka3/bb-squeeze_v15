"""Read-only reconciliation of user-operated checks. Never constructs a broker."""
import argparse
from decimal import Decimal
import json
from pathlib import Path
import time

from .account_ledger import AccountLedger,number,signed
from .authenticated import LighterTransport
from .execution import positions
from .live_check import save


def audit(transport,sessions,output,*,clock=time.time):
    if not sessions: raise ValueError('at least one saved execution session required')
    sessions=sorted(sessions,key=lambda s:s['started_at_ms'])
    if any(s['account_index']!=transport.account_index or s['network']!=transport.network for s in sessions):
        raise ValueError('session account/network mismatch')
    output=Path(output); output.mkdir(parents=True,exist_ok=True)
    ledger=AccountLedger(output/'audit-ledger.sqlite',transport.network,transport.account_index,sessions[0]['started_at_ms'])
    try:
        ledger.collect(transport,int(clock()*1000))
        evidence=[]; owned=set()
        for session in sessions:
            orders=[(session['first_coi'],'ENTRY',session['entry_result'])]
            orders += [(int(coi),'EXIT',r) for coi,r in session.get('close_results',{}).items()]
            if session.get('stop_final_result'):
                orders.append((session['first_coi']+1,'SL',session['stop_final_result']))
            for coi,role,result in orders:
                if coi in owned: raise ValueError('duplicate session order')
                owned.add(coi)
                totals=ledger.order_totals(coi,result['order_index'])
                evidence.append({'coi':coi,'role':role,'trade_qty':str(totals['qty']),
                    'order_qty':result['filled_qty'],'trade_quote':str(totals['quote']),
                    'matches':totals['qty']==number(result['filled_qty']) and totals['quote']==number(result['filled_quote']),
                    'fee_usdc':str(totals['fee_usdc']) if totals['fee_usdc'] is not None else None})
        trades=ledger.events('trade'); funding=ledger.events('funding')
        unowned=[r['trade_id'] for r in trades if r['coi'] not in owned]
        account=transport.account(); active=transport.active_orders()
        change=signed(account['total_asset_value'])-signed(sessions[0]['equity_before'])
        cash=sum((number(r['quote'])*(1 if r['is_ask'] else -1) for r in trades),Decimal(0))
        funding_cash=sum((signed(r['cash_change']) for r in funding),Decimal(0))
        result={**ledger.report(),'account_index':transport.account_index,'checked_at_ms':int(clock()*1000),
            'order_comparisons':evidence,'unowned_trade_ids':unowned,
            'all_order_fills_match':all(e['matches'] for e in evidence) and not unowned,
            'open_position_count':len(positions(account)),'active_order_count':len(active),
            'equity_before':sessions[0]['equity_before'],'equity_after':account['total_asset_value'],
            'equity_change':str(change),'trade_cash_change':str(cash),
            'residual_before_unknown_costs':str(change-cash-funding_cash),
            'orders_submitted_by_audit':0,'strategy_live_enabled':False}
        save(output/'report.json',result)
        return result
    finally: ledger.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True,type=Path)
    parser.add_argument('--sessions',nargs='+',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    cfg=json.loads(args.config.read_text(encoding='utf-8-sig'))
    transport=LighterTransport(**{k:cfg[k] for k in ('network','account_index','api_key_index','key_file','expected_address')})
    try:
        report=audit(transport,[json.loads(p.read_text()) for p in args.sessions],args.output)
        print(json.dumps(report,indent=2))
    finally: transport.close()


if __name__=='__main__': main()

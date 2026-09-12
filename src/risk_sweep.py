"""User-requested A risk sizing comparison; fixed portfolio caps, no orders."""
from dataclasses import asdict
import argparse,json
import numpy as np
import pandas as pd
from run_frontier import ROOT,market_data,save_case,jobs_for,write_csv_atomic
from frontier_engine import Strategy,run
from data_io import START,END
from analyze_results import period,SPLIT

RESULTS=ROOT/'results'
RISK_LEVELS=[.02]
MODELS={'binance':(.0005,.0002),'zero_fee':(0.,.0002),
        'binance_slip5':(.0005,.0005),'zero_fee_slip5':(0.,.0005)}

MODELS={f"{m}_cap{cap}":cost for m,cost in MODELS.items() for cap in [4,6]}

def write_json(p,obj):
    p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_suffix(p.suffix+'.tmp')
    tmp.write_text(json.dumps(obj,indent=2,allow_nan=False,default=lambda x:x.item()));tmp.replace(p)

def specs():
    return [Strategy(name=f'{model}_risk{round(risk*10000)}',risk=risk,
            short_btc_bull_risk=.5,fee=fee,slippage=slip,total_risk=int(model.rsplit("cap",1)[1])/100)
            for model,(fee,slip) in MODELS.items() for risk in RISK_LEVELS]

def design():
    write_json(ROOT/'research_design.json',{'date':'2026-09-11','scope':'A strategy base risk 2%, total initial risk cap 4% versus requested 6%',
      'unchanged':'BTC 4h SMA200 bull short risk half (1%); max 6 positions, gross cap 5x; entries/exits and initial-risk accounting unchanged',
      'period':['2021-01-01','2026-09-01 exclusive'],'funding':'Binance observed funding in all cases; zero_fee is cost proxy, not actual Lighter replay',
      'limits':'5m close MDD, no liquidation or orderbook engine; cap applies at new entries, not continuous rebalancing',
      'cases':[asdict(x) for x in specs()]})

def execute(models=None,discover=False):
    chosen=[x for x in specs() if models is None or x.name.rsplit('_risk',1)[0] in models]
    m=market_data();missing=set();marks=set()
    for cfg in chosen:
        if discover:
            r=run(m,cfg);row=None;need=r['active_bars']-set(m.micro)
        else:
            folder=RESULTS/'cases'/cfg.name;r,row,need=save_case(m,cfg,folder)
            if row:
                e=r['equity'];t=r['trades'];real=100*t.initial_risk/t.entry_equity
                utilization=real/t.requested_risk_pct
                lev=e.gross/e.equity
                row.update(model=cfg.name.rsplit('_risk',1)[0],base_risk_pct=cfg.risk*100,
                  bull_short_risk_pct=cfg.risk*50,
                  clipped_entries=int((utilization<1-1e-8).sum()),
                  average_requested_risk_pct=float(t.requested_risk_pct.mean()),
                  average_filled_risk_pct=float(real.mean()),
                  average_filled_fraction=float(utilization.mean()),
                  average_open_initial_risk_pct=float((100*e.initial_risk/e.equity).mean()),
                  max_open_initial_risk_at_4h_close_pct=float((100*e.initial_risk/e.equity).max()),
                  max_gross_at_4h_close=float(lev.max()),fraction_4h_above_1x=float((lev>1).mean()))
                write_json(folder/'metrics.json',row);write_csv_atomic(e,folder/'equity_4h_risk.csv')
                print(cfg.name,'CAGR',round(row['cagr_pct'],4),'MDD',round(row['mdd_5m_close_pct'],4),
                      'PF',round(row['profit_factor'],4),'trades',row['trades'],
                      'later',round(row['validation']['cagr_pct'],4),round(row['validation']['mdd_pct'],4),flush=True)
        missing|=need;marks|=r['missing_marks']
        if need:print('MISSING',cfg.name,len(need),flush=True)
    write_json(ROOT/'data/jobs_risk_sweep.json',jobs_for(missing))
    print('Missing execution windows',len(jobs_for(missing)),flush=True)
    if (missing or marks) and not discover:raise RuntimeError(f'Missing 5m bars {len(missing)}, marks {len(marks)}')

def analyze():
    rows={p.parent.name:json.loads(p.read_text()) for p in (RESULTS/'cases').glob('*/metrics.json')}
    assert set(rows)=={x.name for x in specs()}
    summary=[];years=[];details={};allocations=[]
    for cfg in specs():
        n=cfg.name;r=rows[n];folder=RESULTS/'cases'/n
        assert r['config']==asdict(cfg)
        e=pd.read_csv(folder/'equity_5m.csv');t=pd.read_csv(folder/'trades.csv');f=pd.read_csv(folder/'funding.csv')
        assert e.time_ms.is_monotonic_increasing and not e.time_ms.duplicated().any()
        assert int(e.time_ms.iloc[0])==START and int(e.time_ms.iloc[-1])==END
        assert np.isclose(e.equity.iloc[-1],r['final_equity'],rtol=1e-10)
        assert np.isclose(period(e,START,END)['mdd_pct'],r['mdd_5m_close_pct'],atol=1e-8)
        assert len(t)==r['trades'] and np.isclose(100000+t.net_pnl.sum(),r['final_equity'],rtol=1e-10)
        assert np.isclose(t.fees.sum(),r['fees'],rtol=1e-10,atol=1e-8)
        assert len(f)==r['funding_events'] and np.isclose(f.cashflow.sum(),r['funding_pnl'],atol=1e-7)
        assert r['coarse_position_bars']==0
        for tr in t.to_dict('records'):
            fills=json.loads(tr['fills'])
            assert np.isclose(sum(x['qty'] for x in fills if x['role']=='ENTRY'),sum(x['qty'] for x in fills if x['role']!='ENTRY'),rtol=1e-9)
            assert np.isclose(sum(x['gross_pnl']-x['fee'] for x in fills)+tr['funding_pnl'],tr['net_pnl'],atol=1e-7)
            assert tr['signal_ms']<tr['entry_ms']
            assert 100*tr['initial_risk']/tr['entry_equity']<=tr['requested_risk_pct']+1e-9
            expected=cfg.risk*100*(.5 if tr['side']=='SHORT' and tr['requested_risk_pct']<cfg.risk*100-1e-9 else 1.)
            assert np.isclose(tr['requested_risk_pct'],expected,atol=1e-8)
        d=e.equity/e.equity.cummax()-1;bottom=d.idxmin();top=e.loc[:bottom,'equity'].idxmax()
        r['max_drawdown_peak_utc']=str(pd.to_datetime(e.loc[top,'time_ms'],unit='ms',utc=True))
        r['max_drawdown_trough_utc']=str(pd.to_datetime(e.loc[bottom,'time_ms'],unit='ms',utc=True))
        r['drawdown_peak_to_trough_days']=float((e.loc[bottom,'time_ms']-e.loc[top,'time_ms'])/86400000)
        r['year_returns']={}
        for y in range(2021,2027):
            aa=int(pd.Timestamp(f'{y}-01-01',tz='UTC').timestamp()*1000)
            bb=min(END,int(pd.Timestamp(f'{y+1}-01-01',tz='UTC').timestamp()*1000))
            rr=period(e,aa,bb)
            years.append({'name':n,'model':r['model'],'base_risk_pct':r['base_risk_pct'],'year':y,**rr})
            r['year_returns'][str(y)]=rr['return_pct']
        base=rows[r['model'].rsplit('_cap',1)[0]+'_cap4_risk200']
        r['delta_cagr_pp']=r['cagr_pct']-base['cagr_pct']
        r['additional_mdd_pp']=abs(r['mdd_5m_close_pct'])-abs(base['mdd_5m_close_pct'])
        r['cagr_per_absolute_mdd']=r['cagr_pct']/abs(r['mdd_5m_close_pct'])
        r['long_trades']=int((t.side=='LONG').sum());r['short_trades']=int((t.side=='SHORT').sum())
        for symbol,g in t.groupby('symbol'):
            allocations.append({'name':n,'model':r['model'],'base_risk_pct':r['base_risk_pct'],'symbol':symbol,
                'trades':len(g),'long_trades':int((g.side=='LONG').sum()),'short_trades':int((g.side=='SHORT').sum()),
                'net_pnl':float(g.net_pnl.sum()),'sum_net_r':float(g.r_multiple.sum())})
        details[n]=r
        summary.append({k:r[k] for k in ['name','model','base_risk_pct','bull_short_risk_pct','cagr_pct','mdd_5m_close_pct',
           'profit_factor','trades','final_equity','fees','funding_pnl','delta_cagr_pp','additional_mdd_pp',
           'cagr_per_absolute_mdd','clipped_entries','skipped_cap','average_requested_risk_pct','average_filled_risk_pct',
           'average_filled_fraction','average_open_initial_risk_pct','avg_gross_leverage','max_gross_at_4h_close','fraction_4h_above_1x']})
        summary[-1].update(later_cagr_pct=r['validation']['cagr_pct'],later_mdd_pct=r['validation']['mdd_pct'])
    for model in ['binance','zero_fee','binance_slip5','zero_fee_slip5']:
        reference=json.loads((ROOT/f'reference/{model}_risk200.json').read_text())
        for k in ['cagr_pct','mdd_5m_close_pct','final_equity','trades','fees','funding_pnl']:
            assert np.isclose(details[model+'_cap4_risk200'][k],reference[k],rtol=1e-10,atol=1e-8)
    write_csv_atomic(pd.DataFrame(summary),RESULTS/'risk_comparison.csv')
    write_csv_atomic(pd.DataFrame(years),RESULTS/'annual_results.csv')
    write_csv_atomic(pd.DataFrame(allocations),RESULTS/'symbol_allocation.csv')
    write_json(RESULTS/'analysis.json',details)
    write_json(RESULTS/'audit.json',{'scenarios_checked':len(rows),'full_5m_paths':True,'endpoints_mdd_fill_quantity_fees_funding_cash_reconciled':True,'baseline_matches_previous_A':True})
    print(pd.DataFrame(summary)[['model','base_risk_pct','cagr_pct','mdd_5m_close_pct','profit_factor','trades','clipped_entries','skipped_cap','cagr_per_absolute_mdd']].to_string(index=False))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--design',action='store_true');p.add_argument('--discover',action='store_true')
    p.add_argument('--analyze',action='store_true');p.add_argument('--models',nargs='*',choices=list(MODELS));a=p.parse_args()
    if a.design:design()
    elif a.analyze:analyze()
    else:execute(a.models,a.discover)

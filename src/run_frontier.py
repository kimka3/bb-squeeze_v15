"""Declared one-factor strategy research at v15's declared per-trade risk.

No orders. Sparse execution-data discovery is not publishable performance.
"""
from pathlib import Path
from dataclasses import replace,asdict
import argparse,json,pickle,os
import numpy as np
import pandas as pd
from data_io import load_data,load_micro,records,SYMBOLS,H4,START,END
from frontier_engine import Strategy,Market,run
from equity_replay import equity_5m
from analyze_results import period,monthly,SPLIT

ROOT=Path(__file__).resolve().parents[1]

def cases(stage2=False):
    b=Strategy(name='v15_baseline')
    specs=[('short_tp25',{'tp_fraction':.25}),('short_tp0',{'tp_fraction':0.}),
     ('short_tp75',{'tp_fraction':.75}),
     ('long_atr25',{'long_exit':'atr','long_trail':2.5}),
     ('long_atr30',{'long_exit':'atr','long_trail':3.}),
     ('long_atr35',{'long_exit':'atr','long_trail':3.5}),
     ('long_confirm2',{'long_exit':'confirm2'}),('long_half_runner',{'long_exit':'half_runner'}),
     ('short_stop125',{'short_stop_atr':1.25}),
     ('short_trail25',{'short_trail_atr':2.5}),('short_trail30',{'short_trail_atr':3.}),
     ('short_trail_after1r',{'trail_activation_r':1.}),
     ('short_reject3',{'short_reject_bars':3}),('short_no_progress6',{'short_progress_bars':6}),
     ('short_atr_min075pct',{'short_min_atr_pct':.0075}),
     ('short_btc_bear',{'short_btc_bear':True}),
     ('long_retest2',{'long_retest_bars':2}),('long_retest4',{'long_retest_bars':4}),
     ('priority_trend',{'entry_priority':'trend'}),('priority_rvol',{'entry_priority':'rvol'}),
     ('short_cap2r',{'short_cap_r':2.}),('short_cap3r',{'short_cap_r':3.}),
     ('short_risk75',{'short_risk_multiple':.75})]
    if stage2:
        specs += [('short_btc_half_risk',{'short_btc_bull_risk':.5}),
          ('combo_btc_bear_atr35',{'short_btc_bear':True,'long_exit':'atr','long_trail':3.5}),
          ('combo_btc_half_atr35',{'short_btc_bull_risk':.5,'long_exit':'atr','long_trail':3.5}),
          ('combo_short75_atr35',{'tp_fraction':.75,'long_exit':'atr','long_trail':3.5}),
          ('combo_btc_bear_retest4',{'short_btc_bear':True,'long_retest_bars':4})]
    return [b]+[replace(b,name=n,**kw) for n,kw in specs]

def prepared():
    cache=ROOT/'data/prepared.pkl'
    if cache.exists():
        with cache.open('rb') as handle:data,funding,marks,audit=pickle.load(handle)
    else:
        data,funding,marks,audit=load_data(ROOT/'data')
        with cache.open('wb') as handle:pickle.dump((data,funding,marks,audit),handle)
    for s in SYMBOLS:
        raw=records(ROOT/'data/raw',s,'4h')
        x=pd.DataFrame([r[:6] for r in raw],columns=['time','open','high','low','close','volume']).astype(float).sort_values('time')
        x['time']=x.time.astype('int64');x=x.set_index('time')
        x['x_ma200']=x.close.rolling(200).mean()
        x['x_rvol']=x.volume/x.volume.rolling(20).mean().shift(1)
        x['x_bb_lower']=x.close.rolling(20).mean()-2*x.close.rolling(20).std(ddof=0)
        for c in ['x_ma200','x_rvol','x_bb_lower']:data[s][c]=x[c].reindex(data[s].index)
    return data,funding,marks,audit

def market_data():
    data,funding,marks,audit=prepared();micro=load_micro(ROOT/'data')
    m=Market(data,funding,marks,micro)
    for (s,t),fine in micro.items():
        if t not in m.rows[s]:continue
        row=m.rows[s][t];aggregate=[fine[0,1],fine[:,2].max(),fine[:,3].min(),fine[-1,4]]
        assert np.allclose(aggregate,[row[k] for k in ['open','high','low','close']],rtol=1e-8,atol=1e-7)
    return m

def native(x):
    if isinstance(x,np.generic):return x.item()
    raise TypeError(type(x).__name__)

def write_csv_atomic(frame,path):
    temp=path.with_name(path.name+'.tmp')
    with temp.open('w',encoding='utf-8',newline='') as handle:
        frame.to_csv(handle,index=False)
        handle.flush();os.fsync(handle.fileno())
    temp.replace(path)

def save_case(m,cfg,folder):
    r=run(m,cfg)
    missing=r['active_bars']-set(m.micro)
    if missing or r['missing_marks']:return r,None,missing
    fine,dd=equity_5m(m,r)
    row={'name':cfg.name,'config':asdict(cfg),**r['metrics'],**r['stats'],
         'mdd_5m_close_pct':dd,'calmar_5m':r['metrics']['cagr_pct']/abs(dd),
         'development':period(fine,START,SPLIT),'validation':period(fine,SPLIT,END)}
    folder.mkdir(parents=True,exist_ok=True)
    tr=r['trades'].copy();tr['fills']=tr.fills.map(json.dumps)
    write_csv_atomic(tr,folder/'trades.csv');write_csv_atomic(r['funding'],folder/'funding.csv')
    write_csv_atomic(fine,folder/'equity_5m.csv')
    (folder/'metrics.json').write_text(json.dumps(row,default=native,indent=2))
    return r,row,set()

def jobs_for(needed):
    jobs=[]
    for s in sorted({s for s,t in needed}):
        times=sorted(t for a,t in needed if a==s)
        while times:
            start=times[0];covered=[t for t in times if t<start+30*H4]
            stop=covered[-1]+H4
            jobs.append({'symbol':s,'startTime':start,'endTime':stop-1,'interval':'5m','limit':1500})
            times=times[len(covered):]
    return jobs

def execute(discover=False,selected=None,model='binance',stage2=False):
    m=market_data();needed=set();rows=[];configs=cases(stage2)
    if selected:configs=[c for c in configs if c.name in selected]
    fee=.0005 if model=='binance' else 0.
    (ROOT/'research_design.json').write_text(json.dumps({
      'primary_risk_per_trade':Strategy().risk,'max_total_risk':.04,'max_gross':5.,'max_positions':6,
      'start':'2021-01-01','end_exclusive':'2026-09-01','development':'2021-2023',
      'validation':'2024-2026-08; retrospective, not untouched OOS',
      'user_reference':{'binance':{'cagr':26.27,'mdd':-30.55,'pf':1.39},'lighter':{'cagr':33.85,'mdd':-27.90,'pf':1.52}},
      'goals':['higher CAGR at no larger MDD','smaller MDD at no lower CAGR','higher CAGR and smaller MDD'],
      'flat_tolerance_pp':.10,
      'selection':'development Pareto candidates, ordered by development Calmar; up to 4; no change to live settings',
      'predeclared_one_factor_cases':[asdict(c) for c in cases()],
      'stage2_exploratory_cases':[asdict(c) for c in cases(True)[len(cases()):]],
      'stage2_note':'Five exploratory tests motivated by stage1; not predeclared before stage1 and not untouched OOS'},indent=2))
    for cfg0 in configs:
        cfg=replace(cfg0,fee=fee)
        if discover:
            r=run(m,cfg);needed|=r['active_bars']-set(m.micro)
            print(model,cfg.name,'discovery only; missing',r['stats']['coarse_position_bars'],flush=True)
        else:
            r,row,missing=save_case(m,cfg,ROOT/'results'/model/cfg.name)
            needed|=missing
            if row is None:print('BLOCKED',model,cfg.name,len(missing),flush=True)
            else:
                rows.append(row)
                print(model,cfg.name,'CAGR',round(row['cagr_pct'],3),'MDD',round(row['mdd_5m_close_pct'],3),
                   'DEV',round(row['development']['cagr_pct'],3),round(row['development']['mdd_pct'],3),flush=True)
    (ROOT/f'data/jobs_{model}.json').write_text(json.dumps(jobs_for(needed)))
    if not discover:
        summary_file=ROOT/f'results/summary_{model}.json'
        if selected and summary_file.exists():
            old=json.loads(summary_file.read_text());names={r['name'] for r in rows}
            rows=[r for r in old if r['name'] not in names]+rows
        summary_file.write_text(json.dumps(rows,indent=2,default=native))
        if needed:raise RuntimeError(f'Missing 5m execution paths: {len(needed)} bars; fetch jobs and rerun')
    print('Missing windows',len(jobs_for(needed)),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--discover',action='store_true');p.add_argument('--model',default='binance',choices=['binance','lighter']);p.add_argument('--cases',nargs='*');p.add_argument('--stage2',action='store_true')
    a=p.parse_args();execute(a.discover,a.cases,a.model,a.stage2)

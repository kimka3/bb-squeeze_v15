from pathlib import Path
import json,math
import numpy as np
import pandas as pd
from data_io import START,END

ROOT=Path(__file__).resolve().parents[1]
SPLIT=int(pd.Timestamp('2024-01-01',tz='UTC').timestamp()*1000)

def period(e,start,end):
    before=e[e.time_ms<=start]
    initial=float(before.equity.iloc[-1]) if len(before) else 100000.
    values=np.r_[initial,e[(e.time_ms>start)&(e.time_ms<=end)].equity.to_numpy()]
    cagr=((values[-1]/initial)**(365.25*86400000/(end-start))-1)*100
    mdd=(values/np.maximum.accumulate(values)-1).min()*100
    return {'cagr_pct':float(cagr),'mdd_pct':float(mdd),'return_pct':float((values[-1]/initial-1)*100)}

def monthly(e):
    x=e.copy();x['time']=pd.to_datetime(x.time_ms,unit='ms',utc=True)
    s=x.set_index('time').equity.resample('ME').last()
    idx=pd.date_range('2021-01-31','2026-08-31',freq='ME',tz='UTC')
    s=s.reindex(idx).ffill().fillna(100000.)
    return np.log(s.to_numpy()/np.r_[100000.,s.to_numpy()[:-1]])

def analyze(root=ROOT):
    root=Path(root);summary=json.loads((root/'results/summary.json').read_text());out=[]
    base=pd.read_csv(root/'results/baseline/equity_5m.csv');base_logs=monthly(base)[36:]
    rng=np.random.default_rng(20260911);n=len(base_logs);starts=rng.integers(0,n,size=(2000,math.ceil(n/3)))
    idx=((starts[:,:,None]+np.arange(3))%n).reshape(2000,-1)[:,:n]
    bycase={}
    for r in summary:
        folder=root/'results'/r['name'];path=folder/'equity_5m.csv'
        if not path.exists():raise RuntimeError('Missing complete 5m equity for '+r['name'])
        e=pd.read_csv(path);val=period(e,SPLIT,END);dev=period(e,START,SPLIT)
        logs=monthly(e)[36:]
        delta=(np.exp(logs[idx].mean(axis=1)*12)-np.exp(base_logs[idx].mean(axis=1)*12))*100
        tr=pd.read_csv(folder/'trades.csv')
        sides={}
        for side,g in tr.groupby('side'):
            wins=g.loc[g.net_pnl>0,'net_pnl'];loss=-g.loc[g.net_pnl<0,'net_pnl']
            sides[side]={'trades':len(g),'net_pnl':float(g.net_pnl.sum()),'avg_r':float(g.r_multiple.mean()),
                'pf':float(wins.sum()/loss.sum()) if loss.sum()>0 else None,
                'top5_profit_share':float(wins.nlargest(5).sum()/wins.sum()) if wins.sum()>0 else None}
        years={}
        for y in range(2021,2027):
            a=int(pd.Timestamp(f'{y}-01-01',tz='UTC').timestamp()*1000);b=min(END,int(pd.Timestamp(f'{y+1}-01-01',tz='UTC').timestamp()*1000))
            years[str(y)]=period(e,a,b)['return_pct']
        row={**r,'development':dev,'validation':val,'validation_delta_bootstrap_95_pp':[float(x) for x in np.quantile(delta,[.025,.975])],'sides':sides,'year_returns':years}
        out.append(row);bycase[r['name']]=row
    (root/'results/analysis.json').write_text(json.dumps(out,indent=2,allow_nan=False))
    for r in out:print(r['name'],'CAGR',round(r['cagr_pct'],3),'5m MDD',round(r['mdd_5m_close_pct'],3),'DEV',round(r['development']['cagr_pct'],3),'VAL',round(r['validation']['cagr_pct'],3),'CI',[round(x,3) for x in r['validation_delta_bootstrap_95_pp']])
    return out

if __name__=='__main__':analyze()

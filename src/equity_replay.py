"""Reconstruct common-account equity at 5m closes from exact replay fills."""
import numpy as np
import pandas as pd
from data_io import H4,START,END

def equity_5m(market,result):
    trades=result['trades'];fees_events={};fund_events={};prices={};times=set()
    for tr in trades.to_dict('records'):
        s=tr['symbol'];sign=1 if tr['side']=='LONG' else -1
        for f in tr['fills']:
            t=int(f['time_ms']);fees_events.setdefault(t,[]).append((s,sign,tr['entry_price'],f))
            times.add(t)
        # Include each 5m mark while this position can be held, including exits.
        # A position fully closed at this interval's open needs no later mark.
        last=min(int(tr['exit_ms'])-1,END-1)
        for t4 in range(int(tr['entry_ms'])//H4*H4,last//H4*H4+H4,H4):
            key=(s,t4)
            if key not in market.micro:raise ValueError(f'Missing 5m path {key}')
            for row in market.micro[key]:
                t=int(row[0]);prices[(s,t)]=float(row[4]);times.add(t)
    for f in result['funding'].to_dict('records'):
        t=int(f['time_ms']);fund_events[t]=fund_events.get(t,0)+f['cashflow'];times.add(t)
    cash=100000.;positions={};rows=[{'time_ms':START,'equity':cash}]
    for t in sorted(times):
        cash+=fund_events.get(t,0)
        # Fills from the same 5m interval are recognized before its closing mark.
        # Within-interval excursions are NOT claimed to be exact drawdowns.
        for s,sign,entry,f in fees_events.get(t,[]):
            cash+=float(f['gross_pnl'])-float(f['fee'])
            if f['role']=='ENTRY':positions[s]=[float(f['qty']),entry,sign]
            else:
                positions[s][0]-=float(f['qty'])
                if positions[s][0]<=1e-10:positions.pop(s)
        wealth=cash
        for s,(q,entry,sign) in positions.items():
            if (s,t) not in prices:raise ValueError(f'Missing held-symbol 5m mark {s} {t}')
            wealth+=q*(prices[(s,t)]-entry)*sign
        rows.append({'time_ms':min(t+300000,END),'equity':wealth})
    if not np.isclose(cash,result['metrics']['final_equity'],atol=1e-6):raise AssertionError('5m equity fails cash reconciliation')
    rows.append({'time_ms':END,'equity':cash})
    eq=pd.DataFrame(rows).groupby('time_ms',as_index=False).last()
    peak=eq.equity.cummax();dd=eq.equity/peak-1
    return eq,float(dd.min()*100)

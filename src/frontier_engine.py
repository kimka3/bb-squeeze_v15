"""Shared-cash, costed BB Squeeze research and historical paper replay.

4h confirmed signals; next-open entries. Fine OHLC execution when cached.
No parameter optimization or exchange orders. All variants are predeclared.
"""
from dataclasses import dataclass,asdict,replace
from pathlib import Path
import json,copy,math
import numpy as np
import pandas as pd
from data_io import SYMBOLS,LONGS,H4,START,END
from trade_core import Costs,execution_price,remaining_initial_risk,intrabar,close_fill,funding_cashflow,tighten_stop
from live_core import plan_entry,open_position,update_on_bar_close,scan_signals,remaining_risk,btc_bull_at

@dataclass(frozen=True)
class Strategy:
    name: str = 'baseline'
    # Declared per-trade initial risk, raised from 1.25% to the 2% ceiling
    # accepted below. Portfolio caps (4% total, 5x gross, 6 positions) unchanged.
    risk: float = .02
    tp_fraction: float = .5
    long_exit: str = 'bb_mid'
    long_trail: float = 3.
    total_risk: float = .04
    gross_cap: float = 5.
    max_positions: int = 6
    short_risk_multiple: float = 1.
    short_cap_r: float = 0.
    correlated_cap_r: float = 0.
    ambiguity: str = 'stop_first'
    funding: bool = True
    fee: float = .0005
    slippage: float = .0002
    short_stop_atr: float = 1.
    long_stop_atr: float = 1.5
    short_trail_atr: float = 2.
    trail_activation_r: float = 0.
    long_retest_bars: int = 3
    short_reject_bars: int = 0
    short_progress_bars: int = 0
    short_min_atr_pct: float = 0.
    short_btc_bear: bool = False
    short_btc_bull_risk: float = 1.
    entry_priority: str = 'canonical'

    def __post_init__(self):
        if not 0<self.risk<=.02:raise ValueError('Offline user-requested risk sweep is limited to 2%; portfolio caps remain frozen')
        if not 0<=self.tp_fraction<=1:raise ValueError('Invalid partial fraction')
        if not 0<self.total_risk<=.06 or not 0<self.gross_cap<=5 or not 1<=self.max_positions<=6:raise ValueError('Portfolio caps exceed frozen limits')
        if self.long_exit not in {'bb_mid','atr','confirm2','half_runner'}:raise ValueError('Unknown long exit')
        if self.fee<0 or self.slippage<0:raise ValueError('Invalid research cost')
        if self.entry_priority not in {'canonical','trend','rvol'}:raise ValueError('Invalid entry priority')
        if min(self.short_stop_atr,self.long_stop_atr,self.short_trail_atr)<=0:raise ValueError('Invalid ATR distance')
        if not 1<=self.long_retest_bars<=5:raise ValueError('Invalid retest window')

class Market:
    def __init__(self,data,funding,marks,micro=None):
        self.timestamps=data[SYMBOLS[0]].index.to_numpy(dtype='int64')
        self.cols=list(data[SYMBOLS[0]].columns)
        self.rows={s:{int(t):r for t,r in d.to_dict('index').items()} for s,d in data.items()}
        self.funding={};self.marks=marks;self.micro=micro or {}
        for s,f in funding.items():
            for r in f.to_dict('records'):
                t=int(r['settlement_ms']);self.funding.setdefault((s,t//H4*H4),[]).append(r)
        self.returns=np.log(pd.DataFrame({s:d.close for s,d in data.items()})).diff().to_numpy()

def run(market,config,start=START,end=END,slippage_by_symbol=None):
    """slippage_by_symbol overrides config.slippage per symbol when given.

    Measured book depth is not the same on every market, so a flat rate either
    flatters the thin ones or punishes the deep ones. Omit it and every symbol
    uses config.slippage exactly as before — the default path is unchanged.
    """
    costs=Costs(config.fee,config.slippage);cash=100000.;positions={};setups={};pending={}
    per_symbol=({s:Costs(config.fee,slippage_by_symbol.get(s,config.slippage)) for s in SYMBOLS}
                if slippage_by_symbol else None)
    def cost_for(symbol):return per_symbol[symbol] if per_symbol else costs
    trades=[];equity=[];fund_ledger=[];need_micro=set();active_bars=set();missing_marks=set()
    stats={'skipped_cap':0,'skipped_filter':0,'ambiguous_subbars':0,'funding_price_fallbacks':0,
           'micro_position_bars':0,'coarse_position_bars':0,'funding_events':0}
    history=market.timestamps
    def eq(prices):
        return cash+sum((prices[s]-p['entry'])*p['qty']*(1 if p['side']=='LONG' else -1) for s,p in positions.items())
    risk=remaining_risk
    def finish(s,t,reason):
        p=positions.pop(s)
        net=p['gross_pnl']-p['fees']+p['funding']
        trades.append({'symbol':s,'side':p['side'],'entry_ms':p['entry_ms'],'exit_ms':int(t),
            'entry_price':p['entry'],'initial_qty':p['initial_qty'],'initial_risk':p['initial_risk'],
            'signal_ms':p['signal_ms'],'entry_equity':p['entry_equity'],'requested_risk_pct':p['requested_risk_pct'],
            'gross_pnl':p['gross_pnl'],'fees':p['fees'],'funding_pnl':p['funding'],
            'net_pnl':net,'r_multiple':net/p['initial_risk'],'bars':p['bars'],
            'partial_taken':p['partial_taken'],'reason':reason,'fills':p['fills'],
            'mfe_r':p['mfe']/p['risk_unit'],'mae_r':p['mae']/p['risk_unit']})
    def settle(s,p,fr,default_mark):
        nonlocal cash
        if not config.funding:return
        mark=float(fr['markPrice'])
        if not math.isfinite(mark) or mark<=0:
            # Historical funding record can lack mark. Use the observed mark
            # candle open at this settlement boundary; disclose the estimate.
            t=int(fr['settlement_ms']); md=market.marks[s]
            if t in md.index:mark=float(md.loc[t,'open'])
            else:
                missing_marks.add((s,t));mark=float(default_mark)
            stats['funding_price_fallbacks']+=1
        amount=funding_cashflow(p['side'],p['qty'],mark,float(fr['fundingRate']))
        cash+=amount;p['funding']+=amount;stats['funding_events']+=1
        fund_ledger.append({'symbol':s,'entry_ms':p['entry_ms'],'time_ms':int(fr['settlement_ms']),
            'exchange_time_ms':int(fr['fundingTime']),'qty':p['qty'],'side':p['side'],
            'rate':float(fr['fundingRate']),'mark':mark,'cashflow':amount,
            'price_source':'funding_record' if pd.notna(fr['markPrice']) else 'mark_candle_open'})

    for i,ts0 in enumerate(history):
        ts=int(ts0)
        if ts<start or ts>=end:continue
        rows={s:market.rows[s][ts] for s in SYMBOLS}
        opens={s:float(r['open']) for s,r in rows.items()}
        # Funding at boundary applies to positions carried into settlement,
        # BEFORE old close-signal exits and new next-open entries.
        for s,p in list(positions.items()):
            for fr in market.funding.get((s,ts),[]):
                if fr['settlement_ms']==ts:settle(s,p,fr,opens[s])
        # Execute all prior-close exits before any new entries.
        for s,p in list(positions.items()):
            action=p.pop('pending_exit',None)
            if action:
                q=p['qty']*(.5 if action=='BB_HALF' else 1.)
                cash+=close_fill(p,q,opens[s],cost_for(s),ts,action)
                if p['qty']<=1e-12:finish(s,ts,action)
                else:p['long_half_taken']=True
        def priority(symbol):
            req=pending.get(symbol)
            if req is None:return (1,0.,SYMBOLS.index(symbol))
            side,atr,signal_ms=req;known=market.rows[symbol][signal_ms]
            if config.entry_priority=='trend':
                score=(float(known['close'])-float(known['x_ma200']))/atr*(1 if side=='LONG' else -1)
            elif config.entry_priority=='rvol':score=float(known['x_rvol'])
            else:score=0.
            if not math.isfinite(score):score=-1e10
            return (0,-score,SYMBOLS.index(symbol))
        order=SYMBOLS if config.entry_priority=='canonical' else sorted(SYMBOLS,key=priority)
        for s in order:
            req=pending.pop(s,None)
            if req is None or s in positions:continue
            side,atr,signal_ms=req
            wealth=eq(opens)
            def correlated(want,_s=s,_i=i):
                past=market.returns[max(0,_i-180):_i];out=[]
                for other,p in positions.items():
                    if p['side']!=want:continue
                    pair=past[:,[SYMBOLS.index(_s),SYMBOLS.index(other)]]
                    pair=pair[np.isfinite(pair).all(axis=1)]
                    # Unknown correlation is treated as common risk.
                    corr=np.corrcoef(pair.T)[0,1] if len(pair)>=120 else 1.
                    if not np.isfinite(corr) or corr>=.7:out.append(p)
                return out
            plan=plan_entry(s,side,atr,wealth,opens,positions,config,cost_for(s),
                btc_bull=btc_bull_at(market.rows['BTCUSDT'][signal_ms]),correlated=correlated)
            if plan.skipped=='invalid':continue
            if plan.skipped=='cap':stats['skipped_cap']+=1;continue
            fee=plan.qty*plan.px*cost_for(s).fee;cash-=fee
            positions[s]=open_position(side,ts,signal_ms,plan,plan.qty,wealth,fee,
                float(market.rows[s][signal_ms].get('x_bb_lower',0.)))

        # OHLC for active positions only. Micro cache includes the FULL 4h bar.
        # No future micro prices enter the 4h signal or position sizing.
        for s,p in list(positions.items()):
            active_bars.add((s,ts));p['bars']+=1;r=rows[s]
            oh=[float(r[k]) for k in ['open','high','low','close']]
            micro=market.micro.get((s,ts));is_fine=micro is not None
            if is_fine:stats['micro_position_bars']+=1
            else:
                stats['coarse_position_bars']+=1
                micro=np.array([[ts,*oh]])
                if p['side']=='SHORT' and not p['partial_taken'] and oh[2]<=p['tp2r'] and oh[1]>=min(p['stop'],p['entry']):need_micro.add((s,ts))
            fs=[f for f in market.funding.get((s,ts),[]) if f['settlement_ms']>ts]
            if fs and not is_fine:need_micro.add((s,ts))
            fidx=0
            for sub in micro:
                t=int(sub[0]);o,h,l,c=map(float,sub[1:])
                while fidx<len(fs) and fs[fidx]['settlement_ms']<=t:
                    settle(s,p,fs[fidx],o);fidx+=1
                # Excursions are OHLC bounds; exit subbar extrema can include
                # movement after the exit and are labelled as such in reports.
                p['mfe']=max(p['mfe'],(h-p['entry']) if p['side']=='LONG' else (p['entry']-l))
                p['mae']=min(p['mae'],(l-p['entry']) if p['side']=='LONG' else (p['entry']-h))
                change,amb=intrabar(p,[o,h,l,c],cost_for(s),config.tp_fraction,t,config.ambiguity)
                cash+=change;stats['ambiguous_subbars']+=int(amb)
                if p['qty']<=1e-12:
                    finish(s,t,p['fills'][-1]['role']);break
            if s in positions:
                # Discovery pass only: unresolved intra-4h funding will force
                # micro download before a final run is accepted.
                while fidx<len(fs):settle(s,p,fs[fidx],oh[3]);fidx+=1

        # Close rules and monotone trails become active on next bar.
        for s,p in positions.items():
            update_on_bar_close(p,rows[s],config)
        # Generate new close signals and the long retest state machine.
        fresh,filtered=scan_signals(ts,rows,SYMBOLS,LONGS,positions,setups,config)
        pending.update(fresh);stats['skipped_filter']+=filtered
        closes={s:float(r['close']) for s,r in rows.items()};wealth=eq(closes)
        equity.append({'time_ms':ts+H4,'equity':wealth,'cash':cash,'positions':len(positions),
            'gross':sum(p['qty']*closes[s] for s,p in positions.items()),'initial_risk':sum(risk(p) for p in positions.values()),
            'short_initial_risk':sum(risk(p) for p in positions.values() if p['side']=='SHORT'),
            'short_gross':sum(p['qty']*closes[s] for s,p in positions.items() if p['side']=='SHORT')})
        if wealth<=0:raise RuntimeError('Account equity exhausted')
    last=int(equity[-1]['time_ms'])
    for s,p in list(positions.items()):
        close=market.rows[s][last-H4]['close'];cash+=close_fill(p,p['qty'],close,cost_for(s),last,'EOD');finish(s,last,'EOD')
    equity[-1].update(equity=cash,cash=cash,positions=0,gross=0,initial_risk=0,short_initial_risk=0,short_gross=0)
    eqdf=pd.DataFrame(equity);tdf=pd.DataFrame(trades)
    if not np.isclose(cash,100000+tdf.net_pnl.sum(),rtol=1e-10,atol=1e-6):raise AssertionError('Cash ledger does not reconcile')
    return {'config':asdict(config),'equity':eqdf,'trades':tdf,'funding':pd.DataFrame(fund_ledger),
            'stats':stats,'need_micro':need_micro,'active_bars':active_bars,'missing_marks':missing_marks,
            'metrics':metrics(eqdf,tdf,start,end)}

def metrics(eq,trades,start,end):
    path=np.r_[100000.,eq.equity.to_numpy()];peak=np.maximum.accumulate(path);dd=path/peak-1
    final=path[-1];years=(end-start)/(365.25*86400000);cagr=(final/100000)**(1/years)-1
    win=trades.loc[trades.net_pnl>0,'net_pnl'];loss=-trades.loc[trades.net_pnl<0,'net_pnl']
    return {'final_equity':final,'cagr_pct':cagr*100,'mdd_close_pct':float(dd.min()*100),
      'calmar':cagr/abs(dd.min()) if dd.min()<0 else None,'trades':len(trades),
      'win_rate_pct':float((trades.net_pnl>0).mean()*100),
      'profit_factor':float(win.sum()/loss.sum()) if loss.sum()>0 else None,
      'avg_r':float(trades.r_multiple.mean()),'fees':float(trades.fees.sum()),
      'funding_pnl':float(trades.funding_pnl.sum()),'long_pnl':float(trades.loc[trades.side=='LONG','net_pnl'].sum()),
      'short_pnl':float(trades.loc[trades.side=='SHORT','net_pnl'].sum()),
      'avg_gross_leverage':float((eq.gross/eq.equity).mean()),'max_positions':int(eq.positions.max())}

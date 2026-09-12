"""Deterministic execution primitives shared by historical and paper replay.

Linear USDT contracts. No exchange orders are sent by this module.
"""
from dataclasses import dataclass
import math

@dataclass(frozen=True)
class Costs:
    fee: float = .0005
    slippage: float = .0002

def execution_price(reference, side, opening, costs):
    buy = (side == 'LONG') == opening
    return float(reference) * (1 + costs.slippage if buy else 1 - costs.slippage)

def remaining_initial_risk(initial_risk, initial_qty, current_qty):
    if initial_qty <= 0 or current_qty < 0 or current_qty > initial_qty*(1+1e-7):
        raise ValueError('Invalid position quantities')
    return float(initial_risk) * float(current_qty) / float(initial_qty)

def close_fill(position, qty, reference, costs, time_ms, reason):
    """Book exactly filled quantity and its actual cash flows, including fees."""
    if not 0 < qty <= position['qty'] * (1+1e-9):
        raise ValueError('Exit quantity must be positive and not exceed remaining')
    qty = min(qty, position['qty'])
    price = execution_price(reference, position['side'], False, costs)
    gross = (price-position['entry'])*qty*(1 if position['side']=='LONG' else -1)
    fee = qty*price*costs.fee
    position['qty'] -= qty
    position['gross_pnl'] += gross
    position['fees'] += fee
    position['fills'].append({'time_ms':int(time_ms),'role':reason,'qty':qty,
                              'price':price,'fee':fee,'gross_pnl':gross})
    return gross-fee

def intrabar(position, bar, costs, tp_fraction, time_ms, ambiguity='stop_first'):
    """Execute one OHLC bar. Old stop first; newly armed BE rechecked.

    If TP and the newly armed BE are both touched in this bar, stop_first
    assumes TP then BE (pessimistic runner path). tp_first permits the
    pre-TP high to occur first, but MUST close if close itself re-crosses BE.
    Call at 5m resolution for 4h ambiguous candles to reduce this interval.
    Returns cash delta and whether within-bar ordering was ambiguous.
    """
    o,h,l,c = map(float,bar[:4])
    if min(o,h,l,c)<=0 or l>min(o,c) or h<max(o,c) or h<l:
        raise ValueError('Invalid OHLC')
    side=position['side']; stop=position['stop']; cash=0.; ambiguous=False
    if side=='LONG':
        if l<=stop:
            cash+=close_fill(position,position['qty'],min(o,stop),costs,time_ms,'STOP')
        return cash,False
    hit_stop=h>=stop
    hit_tp=not position['partial_taken'] and l<=position['tp2r']
    ambiguous = hit_stop and hit_tp
    tp_at_open=hit_tp and o<=position['tp2r']
    if hit_stop and not (hit_tp and o<stop and (ambiguity=='tp_first' or tp_at_open)):
        return close_fill(position,position['qty'],max(o,stop),costs,time_ms,'STOP'),ambiguous
    if hit_tp:
        qty=position['qty']*tp_fraction
        if qty>0:
            cash+=close_fill(position,qty,min(o,position['tp2r']),costs,time_ms,'TP2R')
        position['partial_taken']=True
        position['stop']=min(stop,position['entry'])
        if position['qty']>0 and h>=position['stop']:
            ambiguous=True
            if ambiguity=='stop_first' or hit_stop or tp_at_open or c>=position['stop']:
                cash+=close_fill(position,position['qty'],position['stop'],costs,time_ms,'BE_AFTER_TP')
    return cash,ambiguous

def funding_cashflow(side, qty, mark, rate):
    if not all(math.isfinite(float(x)) for x in [qty,mark,rate]) or qty<0 or mark<=0:
        raise ValueError('Invalid funding observation')
    return -qty*mark*rate*(1 if side=='LONG' else -1)

def tighten_stop(position, candidate):
    if not math.isfinite(candidate): return
    position['stop']=(max if position['side']=='LONG' else min)(position['stop'],candidate)

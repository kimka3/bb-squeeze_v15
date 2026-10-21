"""Preserve attribution evidence while bounding ledger table reads."""
from decimal import Decimal
import random

from live.account_ledger import AccountLedger


def original_attribution(trades,fundings,bindings):
    out=[]
    for funding in fundings:
        held={}; ambiguous=False
        for trade in trades:
            if trade['market_id']!=funding['market_id'] or trade['timestamp_ms']>funding['timestamp_ms']:
                continue
            if trade['timestamp_ms']==funding['timestamp_ms']: ambiguous=True
            binding=bindings.get(trade['coi'])
            if binding is None: ambiguous=True; continue
            cycle=binding['cycle']
            held[cycle]=held.get(cycle,Decimal(0))+Decimal(trade['qty'])*(-1 if trade['is_ask'] else 1)
        candidates=[c for c,q in held.items() if q and ('long' if q>0 else 'short')==funding['position_side']]
        out.append({**funding,'cycle':candidates[0] if len(candidates)==1 and not ambiguous else None})
    return out


def test_single_pass_matches_original_with_ties_multiple_cycles_and_unknown_owners():
    rng=random.Random(721)
    bindings={i:{'cycle':i//3} for i in range(30)}
    trades=[{'market_id':rng.randrange(4),'timestamp_ms':rng.randrange(50),
             'coi':rng.randrange(34),'qty':str(rng.randrange(1,6)/10),'is_ask':rng.choice([True,False])}
            for _ in range(1000)]
    fundings=[{'market_id':rng.randrange(5),'timestamp_ms':rng.randrange(60),
               'position_side':rng.choice(['long','short']),'funding_id':i} for i in range(600)]
    ledger=AccountLedger.__new__(AccountLedger); calls=[]
    def events(kind):
        calls.append(kind)
        return trades if kind=='trade' else fundings
    ledger.events=events
    ledger.historical_issues=lambda: []
    assert ledger.attribute_funding(bindings)==original_attribution(trades,fundings,bindings)
    assert calls==['funding','trade']

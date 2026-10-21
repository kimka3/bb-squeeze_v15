"""Known other-market close rules continue while quarantined exposure is frozen."""
import copy
from decimal import Decimal
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from live.trader import BarOutcome
from test_strategy_integration import START, H4, setup, rows

MARKS={'ETHUSDT':100,'BTCUSDT':100}


def opened(tmp_path):
    x,b,t=setup(tmp_path)
    signals=rows(); signals['BTCUSDT']['short_signal']=True
    t.on_bar(START,signals,MARKS,now_ms=x.now)
    assert set(t.positions)==set(MARKS)
    return x,b,t


def quarantine(x,kind='funding'):
    x.now+=61000
    if kind=='funding':
        x.funding.append({'funding_id':9001,'market_id':0,'timestamp':x.now//1000,
            'change':'-.1','discount':'0','rate':'.001','position_size':'1','position_side':'new-side'})
    else:
        raw=copy.deepcopy(next(r for r in x.trades if r['market_id']==0))
        raw.update(trade_id=9001,type='liquidation',timestamp=x.now)
        x.trades.append(raw)


def next_bar(x,b,t,signals=None):
    x.now=START+2*H4+1000
    b.poll()
    return t.on_bar(START+H4,signals or rows(short=False),MARKS,now_ms=x.now)


@pytest.mark.parametrize('actual_fraction',[Decimal('1'),Decimal('.5'),Decimal('0')])
def test_other_market_time_exit_runs_while_quarantined_market_bar_state_is_frozen(tmp_path,actual_fraction):
    x,b,t=opened(tmp_path)
    try:
        quarantine(x,'trade')
        x.position[0]*=actual_fraction
        t.positions['BTCUSDT']['bars']=89
        t.positions['ETHUSDT']['bars']=89
        t.setups['ETHUSDT']=[99,3]
        before_eth=copy.deepcopy(t.positions['ETHUSDT'])
        eth_stop=t.orders['ETHUSDT']['SL']['client_order_index']
        before=len(x.sent)
        out=next_bar(x,b,t)
        assert t.halted and out.halted
        assert 'BTCUSDT' not in t.positions
        assert t.positions['ETHUSDT']==before_eth
        assert t.setups['ETHUSDT']==[99,3]
        assert x.rows[eth_stop]['status']=='pending'
        assert out.exits and out.exits[0]['symbol']=='BTCUSDT' and out.exits[0]['reason']=='TIME'
        assert all(order.market_index==1 and order.reduce_only for order in x.sent[before:])
        assert out.entries==[]
    finally: b.close()


def test_other_market_trailing_runs_but_quarantined_stop_is_never_amended(tmp_path):
    x,b,t=opened(tmp_path)
    try:
        quarantine(x)
        old_eth=copy.deepcopy(t.positions['ETHUSDT'])
        eth_stop=t.orders['ETHUSDT']['SL']['client_order_index']
        btc_stop=t.orders['BTCUSDT']['SL']['client_order_index']
        eth_trigger=x.rows[eth_stop]['trigger_price']
        btc_trigger=Decimal(x.rows[btc_stop]['trigger_price'])
        signals=rows(short=False)
        for row in signals.values(): row.update(low=50,close=50)
        out=next_bar(x,b,t,signals)
        assert out.entries==[] and t.halted
        assert t.positions['ETHUSDT']==old_eth
        assert x.rows[eth_stop]['trigger_price']==eth_trigger
        assert Decimal(x.rows[btc_stop]['trigger_price'])<btc_trigger
        assert t.positions['BTCUSDT']['bars']==1
    finally: b.close()


def test_ordinary_unexplained_mismatch_still_stops_entire_bar(tmp_path):
    x,b,t=opened(tmp_path)
    try:
        x.position[0]/=2
        t.positions['BTCUSDT']['bars']=89
        before=copy.deepcopy(t.positions); sent=len(x.sent)
        out=next_bar(x,b,t)
        assert out.halted and t.halted
        assert t.positions==before and len(x.sent)==sent
        assert out.exits==[]
    finally: b.close()


def test_protective_sync_itself_filters_quarantined_market_without_allowlist(tmp_path):
    x,b,t=opened(tmp_path)
    try:
        quarantine(x)
        state=b.account_state()
        eth_stop=t.orders['ETHUSDT']['SL']['client_order_index']
        btc_stop=t.orders['BTCUSDT']['SL']['client_order_index']
        old_eth=x.rows[eth_stop]['trigger_price']
        for p in t.positions.values(): p['stop']=100
        t._sync_protective_orders(BarOutcome(x.now,state.equity),x.now,state=state)
        assert x.rows[eth_stop]['trigger_price']==old_eth
        assert Decimal(x.rows[btc_stop]['trigger_price'])==100
    finally: b.close()


def test_quarantined_orphan_is_retained_while_other_market_cleanup_runs(tmp_path):
    x,b,t=opened(tmp_path)
    try:
        quarantine(x)
        eth_stop=copy.deepcopy(t.orders['ETHUSDT']['SL'])
        btc_tp=copy.deepcopy(t.orders['BTCUSDT']['TP'])
        t.orphan_orders={'ETHUSDT':{'SL':eth_stop},'BTCUSDT':{'TP':btc_tp}}
        b.poll(); out=t.service(now_ms=x.now,marks=MARKS)
        assert t.halted and out.halted
        assert t.orphan_orders=={'ETHUSDT':{'SL':eth_stop}}
        assert x.rows[eth_stop['client_order_index']]['status']=='pending'
        assert x.rows[btc_tp['client_order_index']]['status']=='canceled'
    finally: b.close()


@pytest.mark.parametrize('kind',['entry','exit','protection','amend','stop_renewal'])
def test_quarantined_operation_recovery_never_mutates_or_discards_intent(tmp_path,kind):
    x,b,t=opened(tmp_path)
    try:
        quarantine(x)
        state=b.account_state()
        # No operation-specific fields are needed: quarantine must stop before
        # any lookup, mutation or inference using potentially obsolete terms.
        operation={'kind':kind,'symbol':'ETHUSDT','bar_ms':x.now,'coi':9010}
        t.pending_operation=copy.deepcopy(operation)
        before=copy.deepcopy(x.rows); sent=len(x.sent)
        out=BarOutcome(x.now,state.equity)
        t._recover_operation(out,set(MARKS),state=state)
        assert t.pending_operation==operation and x.rows==before and len(x.sent)==sent
        assert any('history quarantined' in reason for reason in out.reasons)
    finally: b.close()


def test_service_parks_quarantined_recovery_and_still_amends_other_stop(tmp_path):
    x,b,t=opened(tmp_path)
    try:
        quarantine(x)
        operation={'kind':'amend','symbol':'ETHUSDT','bar_ms':x.now,'coi':9020}
        t.unresolved_operations={'ETHUSDT:amend:9020':copy.deepcopy(operation)}
        t._needs_recovery=True
        btc_stop=t.orders['BTCUSDT']['SL']['client_order_index']
        t.positions['BTCUSDT']['stop']=100
        b.poll(); out=t.service(now_ms=x.now,marks=MARKS)
        assert t.unresolved_operations=={'ETHUSDT:amend:9020':operation}
        assert t.pending_operation is None
        assert Decimal(x.rows[btc_stop]['trigger_price'])==100
        assert out.halted and t.halted
    finally: b.close()

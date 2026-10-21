"""Offline account-history isolation; never constructs a live exchange client."""
import copy
import json
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from live.account_ledger import (HistoricalEvidenceBlocked,
    UnsupportedHistoryRecord, normalize_funding, normalize_trade)
from live.execution_evidence import UnverifiedExecution
from test_history_collection import Paged, funding, make, trade


@pytest.mark.parametrize('kind,value', [
    ('trade','liquidation'), ('trade','deleverage'), ('trade','market-settlement'),
    ('trade','future-unknown-kind'), ('funding','flat'), ('funding','future-unknown-side'),
])
def test_explicit_unsupported_value_has_typed_raw_evidence(kind,value):
    row=trade(1,2000,type=value) if kind=='trade' else funding(1,2000,position_side=value)
    with pytest.raises(UnsupportedHistoryRecord) as raised:
        normalize_trade(row,100) if kind=='trade' else normalize_funding(row)
    error=raised.value
    assert error.issue['kind']==kind and error.issue['market_id']==0
    assert error.issue['timestamp_ms']==2000 and error.issue['unsupported_value']==value
    assert error.raw==row
    row['extra']='changed outside exception'
    assert 'extra' not in error.raw


@pytest.mark.parametrize('kind,updates,removed', [
    ('trade',{'type':None},None), ('trade',{'type':''},None), ('trade',{'type':7},None),
    ('trade',{'type':'liquidation','is_maker_ask':None},None),
    ('trade',{'type':'liquidation','bid_account_id':True},None),
    ('trade',{'type':'liquidation','bid_account_id':300},None),
    ('trade',{'type':'liquidation','ask_account_id':100},None),
    ('trade',{'type':'liquidation','market_id':True},None),
    ('trade',{'type':'liquidation','timestamp':'2000'},None),
    ('trade',{'type':'liquidation','size':'NaN'},None),
    ('trade',{'type':'liquidation','usd_amount':'101'},None),
    ('trade',{'type':'liquidation','taker_fee':None,'integrator_taker_fee':'bad'},None),
    ('trade',{'type':'liquidation','bid_account_pnl':'NaN'},None),
    ('trade',{'type':'liquidation'},'bid_client_id'),
    ('trade',{'type':'liquidation'},'bid_id'),
    ('trade',{'type':'liquidation','extra':float('nan')},None),
    ('funding',{'position_side':None},None), ('funding',{'position_side':''},None),
    ('funding',{'position_side':True},None),
    ('funding',{'position_side':'flat','funding_id':True},None),
    ('funding',{'position_side':'flat','change':'bad'},None),
    ('funding',{'position_side':'flat','discount':None},None),
    ('funding',{'position_side':'flat','position_size':'-1'},None),
    ('funding',{'position_side':'flat'},'rate'),
])
def test_malformed_record_is_not_downgraded_to_quarantine(tmp_path,kind,updates,removed):
    ledger=make(tmp_path)
    row=trade(1,2000,**updates) if kind=='trade' else funding(1,2000,**updates)
    if removed: row.pop(removed)
    try:
        with pytest.raises(UnverifiedExecution) as raised:
            ledger.ingest([row] if kind=='trade' else [],[row] if kind=='funding' else [],3000)
        assert not isinstance(raised.value,UnsupportedHistoryRecord)
        assert ledger.events('trade')==ledger.events('funding')==ledger.quarantined_events()==[]
        assert ledger.db.execute('SELECT * FROM history_coverage').fetchall()==[]
    finally: ledger.close()


def test_mixed_multipage_history_survives_restart_with_raw_records_and_strict_block(tmp_path):
    trades=[trade(1,6000),trade(2,5000,type='liquidation',provider_note={'exact':'original'}),
            trade(3,4000),trade(4,3000,type='market-settlement'),trade(5,2000)]
    fundings=[funding(1,5000),funding(2,4000,position_side='flat'),funding(3,3000)]
    venue=Paged(trades,fundings,size=1)
    ledger=make(tmp_path)
    report=ledger.collect(venue,7000)
    assert not report['head_complete'] and not report['historical_evidence_complete']
    assert report['full_audit_complete_through_ms'] is None
    ledger.close()
    ledger=make(tmp_path)
    try:
        # Each completed lane is intentionally reread on the next call; allow
        # both different-sized one-row-page lanes to complete in the same call.
        for _ in range(10):
            before=len(venue.calls)
            report=ledger.collect(venue,7000)
            assert len(venue.calls)-before<=6
            if report['head_complete']: break
        assert report['head_complete'] and report['full_audit_complete_through_ms']==7000
        assert report['historical_evidence_complete'] is False
        assert {v['trade_id'] for v in ledger.events('trade')}=={1,3,5}
        assert {v['funding_id'] for v in ledger.events('funding')}=={1,3}
        saved=ledger.quarantined_events()
        assert {(v['kind'],v['id']) for v in saved}=={('trade','2'),('trade','4'),('funding','0:2')}
        assert next(v['raw'] for v in saved if v['kind']=='trade' and v['id']=='2')==trades[1]
        assert ledger.report()['funding_cash_change']=='0.02'
        assert ledger.report()['performance_complete'] is False
        assert all('raw' not in v for v in ledger.historical_issues())
        with pytest.raises(HistoricalEvidenceBlocked) as raised: ledger.strict_report()
        assert raised.value.issues==ledger.historical_issues()
        ledger.collect(venue,7000)
        assert ledger.quarantined_events()==saved
    finally: ledger.close()


def test_malformed_later_row_rolls_back_supported_and_unsupported_same_page(tmp_path):
    ledger=make(tmp_path)
    venue=Paged([trade(1,2000)])
    try:
        ledger.collect(venue,3000)
        venue.trades=[trade(2,6000),trade(3,5000,type='liquidation'),trade(4,4000,type='liquidation',size='bad')]
        with pytest.raises(UnverifiedExecution): ledger.collect(venue,7000)
        assert {row['trade_id'] for row in ledger.events('trade')}=={1}
        assert ledger.quarantined_events()==[]
        state=ledger.collector._load('trades')
        assert state['head']['pages']==0 and state['head']['cursor'] is None
        assert state['full_completed_end_ms']==3000
    finally: ledger.close()


@pytest.mark.parametrize('original_type,next_type,change', [
    ('trade','liquidation',{}), ('liquidation','trade',{}),
    ('liquidation','deleverage',{}), ('liquidation','liquidation',{'extra':'correction'}),
    ('liquidation','liquidation',{'market_id':1}),
])
def test_known_identity_never_overwritten_or_silently_promoted(tmp_path,original_type,next_type,change):
    ledger=make(tmp_path)
    venue=Paged([trade(1,2000,type=original_type)])
    try:
        ledger.collect(venue,3000)
        originals=copy.deepcopy(ledger.events('trade')),copy.deepcopy(ledger.quarantined_events())
        venue.trades=[trade(2,4000),trade(1,2000,type=next_type,**change)]
        with pytest.raises(UnverifiedExecution): ledger.collect(venue,5000)
        assert (ledger.events('trade'),ledger.quarantined_events())==originals
        assert ledger.collector._load('trades')['full_completed_end_ms']==3000
    finally: ledger.close()


def test_funding_quarantine_change_is_not_accepted(tmp_path):
    ledger=make(tmp_path)
    try:
        ledger.ingest([], [funding(1,2000,position_side='flat')],3000)
        before=ledger.quarantined_events()
        with pytest.raises(UnverifiedExecution,match='changed'):
            ledger.ingest([], [funding(2,2500),funding(1,2000,position_side='flat',change='0.5')],4000)
        assert ledger.events('funding')==[] and ledger.quarantined_events()==before
    finally: ledger.close()


def test_quarantine_storage_failure_rolls_back_page_and_cursor(tmp_path):
    ledger=make(tmp_path)
    venue=Paged([trade(1,3000),trade(2,2000,type='liquidation')])
    try:
        ledger.db.execute("CREATE TRIGGER fail_quarantine BEFORE INSERT ON history_quarantine "
                          "BEGIN SELECT RAISE(ABORT,'simulated full disk'); END")
        ledger.db.commit()
        with pytest.raises(sqlite3.DatabaseError,match='simulated full disk'): ledger.collect(venue,4000)
        assert ledger.events('trade')==ledger.quarantined_events()==[]
        state=ledger.collector._load('trades')
        assert state['head']['cursor'] is None and state['head']['pages']==0
        assert state['head_completed_end_ms'] is None and state['full_completed_end_ms'] is None
        assert ledger.db.execute('SELECT * FROM history_coverage').fetchall()==[]
    finally: ledger.close()


@pytest.mark.parametrize('mutation', ['wrong_identity','boolean_market','raw_changed','invalid_json','double_classification'])
def test_corrupted_quarantine_is_hard_failure(tmp_path,mutation):
    ledger=make(tmp_path)
    try:
        ledger.ingest([trade(1,2000,type='liquidation',market_id=1)],[],3000)
        value=ledger.quarantined_events()[0]
        if mutation=='wrong_identity': value['id']='99'
        if mutation=='boolean_market': value['market_id']=True
        if mutation=='raw_changed': value['raw']['timestamp']=4000
        encoded='not JSON' if mutation=='invalid_json' else json.dumps(value)
        ledger.db.execute('UPDATE history_quarantine SET value=?',(encoded,))
        if mutation=='double_classification':
            ledger.db.execute('INSERT INTO events VALUES(?,?,?)',('trade','1',json.dumps(normalize_trade(trade(1,2000,market_id=1),100))))
        ledger.db.commit()
        with pytest.raises(UnverifiedExecution): ledger.report()
    finally: ledger.close()


def test_unknown_market_history_blocks_only_its_funding_attribution(tmp_path):
    ledger=make(tmp_path)
    try:
        ledger.ingest([trade(1,2000,bid_client_id=10),trade(2,2000,market_id=1,bid_client_id=20),
                       trade(3,3000,type='liquidation')],
                      [funding(1,4000),funding(2,4000,market_id=1)],5000)
        results=ledger.attribute_funding({10:{'cycle':10},20:{'cycle':20}})
        assert next(v['cycle'] for v in results if v['market_id']==0) is None
        assert next(v['cycle'] for v in results if v['market_id']==1)==20
    finally: ledger.close()


def test_old_late_unsupported_record_discovered_by_repeating_full_audit(tmp_path):
    ledger=make(tmp_path)
    venue=Paged([trade(1,600000),trade(2,100000)],size=1)
    try:
        ledger.collect(venue,600000)
        assert ledger.historical_issues()==[]
        venue.trades.append(trade(3,200000,type='liquidation'))
        for end in (660000,720000,780000,840000): ledger.collect(venue,end)
        assert [v['id'] for v in ledger.historical_issues()]==['3']
        assert ledger.collector.report()['historical_evidence_complete'] is False
    finally: ledger.close()


def test_supported_ledger_keeps_strict_report_behavior_without_certifying_performance(tmp_path):
    ledger=make(tmp_path)
    try:
        ledger.collect(Paged([trade(1,2000)],[funding(1,2000)]),3000)
        report=ledger.strict_report()
        assert report['historical_evidence_complete'] and report['historical_issues']==[]
        assert report['performance_complete'] is False
    finally: ledger.close()

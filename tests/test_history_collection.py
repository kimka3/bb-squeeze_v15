"""Offline collector tests; no exchange client or credentials are constructed."""
import copy
import json
from pathlib import Path
import sqlite3
import sys
from urllib.parse import parse_qs, urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from live.account_ledger import AccountLedger
from live.execution_evidence import UnverifiedExecution
from live.request_budget import RequestDeferred


def trade(identity, stamp, **extra):
    return {'trade_id':identity, 'timestamp':stamp, 'market_id':0, 'type':'trade',
            'ask_account_id':200, 'bid_account_id':100, 'is_maker_ask':True,
            'size':'1', 'price':'100', 'usd_amount':'100',
            'bid_client_id':10, 'bid_id':1010, 'taker_fee':0, 'integrator_taker_fee':0, **extra}


def funding(identity, stamp, **extra):
    return {'funding_id':identity, 'timestamp':stamp//1000, 'market_id':0,
            'change':'0.01', 'discount':'0', 'position_size':'1',
            'position_side':'long', 'rate':'0.0001', **extra}


class Paged:
    account_index = 100
    def __init__(self, trades=(), fundings=(), size=100):
        self.trades, self.fundings = list(trades), list(fundings)
        self.size, self.calls = size, []

    def get(self, path):
        self.calls.append(path)
        parts = urlsplit(path)
        params = parse_qs(parts.query)
        assert params['account_index'] == ['100']
        assert params['limit'] == ['100']
        index = int(params.get('cursor', ['0'])[0])
        is_trade = parts.path.endswith('/trades')
        if is_trade:
            assert params['sort_by'] == ['timestamp'] and params['sort_dir'] == ['desc']
            assert params['aggregate'] == ['false']
            assert 'from' not in params and 'start_timestamp' not in params
            rows = sorted(self.trades, key=lambda row:row['timestamp'], reverse=True)
        else:
            start, end = int(params['start_timestamp'][0]), int(params['end_timestamp'][0])
            # Model exclusive bounds to exercise the existing padding contract.
            rows = sorted([row for row in self.fundings if start < row['timestamp'] < end],
                          key=lambda row:row['timestamp'], reverse=True)
        page = rows[index:index+self.size]
        nxt = str(index+self.size) if index+self.size < len(rows) else ''
        return {('trades' if is_trade else 'position_fundings'):copy.deepcopy(page), 'next_cursor':nxt}


def make(tmp_path, start=1000):
    return AccountLedger(tmp_path/'ledger.sqlite', 'testnet', 100, start)


def test_small_first_read_is_full_with_two_requests_and_no_duplicate_audit(tmp_path):
    ledger = make(tmp_path)
    venue = Paged([trade(1, 2000)], [funding(1, 2000)])
    try:
        report = ledger.collect(venue, 3000)
        assert report['head_complete'] and report['full_audit_complete_through_ms'] == 3000
        assert report['per_call_requests'] == len(venue.calls) == 2
        assert all(item['audit_requests'] == 0 for item in report['kinds'].values())
        assert ledger.report()['history_collection'] == report
    finally:
        ledger.close()


def test_head_backlog_resumes_after_restart_without_false_coverage(tmp_path):
    venue = Paged([trade(i, i*1000) for i in range(1, 8)], size=1)
    ledger = make(tmp_path)
    report = ledger.collect(venue, 9000)
    assert not report['head_complete'] and report['pending_reason'] == 'head_backlog'
    assert report['full_audit_complete_through_ms'] is None
    assert report['kinds']['trades']['head_scan']['cursor'] == '2'
    assert len(ledger.events('trade')) == 2
    ledger.close()
    ledger = make(tmp_path)
    try:
        for _ in range(3):
            before = len(venue.calls)
            report = ledger.collect(venue, 9000)
            assert len(venue.calls)-before <= 4
        assert report['head_complete'] and report['full_audit_complete_through_ms'] == 9000
        assert len(ledger.events('trade')) == 7
    finally:
        ledger.close()


def test_legacy_floor_migrates_but_old_history_is_revisited(tmp_path):
    db = sqlite3.connect(tmp_path/'ledger.sqlite')
    db.execute('CREATE TABLE history_coverage(id INTEGER PRIMARY KEY,end_ms INTEGER)')
    db.execute('INSERT INTO history_coverage VALUES(1,600000)')
    db.commit(); db.close()
    ledger = make(tmp_path)
    venue = Paged([trade(i, i*100000) for i in range(1, 7)],
                  [funding(i, i*100000) for i in range(1, 7)], size=2)
    try:
        report = ledger.collect(venue, 660000)
        assert report['head_complete']
        assert report['full_audit_complete_through_ms'] == 600000
        assert report['per_call_requests'] <= 6
        assert report['kinds']['funding']['full_audit_scan']['start_ms'] == 1000
        assert report['kinds']['funding']['full_audit_scan']['cursor'] == '2'
        before = len(venue.calls)
        again = ledger.collect(venue, 660001)
        assert again['kinds']['funding']['audit_requests'] == 0
        assert len(venue.calls)-before <= 4
        for end in (720000, 780000):
            report = ledger.collect(venue, end)
        assert {row['trade_id'] for row in ledger.events('trade')} == set(range(1, 7))
        assert {row['funding_id'] for row in ledger.events('funding')} == set(range(1, 7))
        assert report['full_audit_complete_through_ms'] >= 660000
    finally:
        ledger.close()


def test_arbitrarily_old_late_funding_is_found_on_repeated_full_pass(tmp_path):
    ledger = make(tmp_path)
    venue = Paged([], [funding(1, 100000), funding(2, 600000)], size=1)
    try:
        assert ledger.collect(venue, 600000)['head_complete']
        # This is well outside the recent overlap and was absent in the first pass.
        venue.fundings.append(funding(3, 200000))
        for end in (660000, 720000, 780000, 840000):
            ledger.collect(venue, end)
        assert {row['funding_id'] for row in ledger.events('funding')} == {1, 2, 3}
    finally:
        ledger.close()


def test_same_timestamp_ties_and_page_duplicates_do_not_skip_records(tmp_path):
    ledger = make(tmp_path)
    venue = Paged([trade(1, 5000), trade(2, 5000), trade(2, 5000), trade(3, 5000)], size=1)
    try:
        assert not ledger.collect(venue, 5000)['head_complete']
        assert ledger.collect(venue, 5000)['head_complete']
        assert {row['trade_id'] for row in ledger.events('trade')} == {1, 2, 3}
    finally:
        ledger.close()


def test_changed_known_record_rolls_back_whole_page_and_checkpoint(tmp_path):
    ledger = make(tmp_path)
    venue = Paged([trade(1, 5000)])
    try:
        ledger.collect(venue, 6000)
        venue.trades = [trade(2, 6000), trade(1, 5000, taker_fee=1)]
        with pytest.raises(UnverifiedExecution, match='history event changed'):
            ledger.collect(venue, 7000)
        assert {row['trade_id'] for row in ledger.events('trade')} == {1}
        report = ledger.collector.report()
        assert report['full_audit_complete_through_ms'] == 6000
        assert report['kinds']['trades']['head_scan']['cursor'] is None
        assert report['kinds']['trades']['head_completed_end_ms'] == 6000
    finally:
        ledger.close()


@pytest.mark.parametrize('response,error', [
    ({'next_cursor':''}, 'history list missing'),
    ({'trades':[], 'next_cursor':'more'}, 'cursor stalled'),
    ({'trades':[trade(1, 1000)], 'next_cursor':1}, 'invalid history cursor'),
    ({'trades':[trade(1, 1000), trade(2, 2000)], 'next_cursor':''}, 'not descending'),
])
def test_invalid_page_never_advances_coverage(tmp_path, response, error):
    ledger = make(tmp_path)
    venue = Paged()
    venue.get = lambda path:copy.deepcopy(response)
    try:
        with pytest.raises(UnverifiedExecution, match=error):
            ledger.collect(venue, 5000)
        assert not ledger.events('trade')
        assert ledger.collector.report()['full_audit_complete_through_ms'] is None
    finally:
        ledger.close()


def test_cursor_stall_is_detected_across_restart(tmp_path):
    ledger = make(tmp_path)
    venue = Paged([trade(i, i*1000) for i in range(1, 8)], size=1)
    ledger.collect(venue, 9000)
    ledger.close()
    ledger = make(tmp_path)
    venue.get = lambda path:{'trades':[trade(5, 5000)], 'next_cursor':'2'}
    try:
        with pytest.raises(UnverifiedExecution, match='cursor stalled'):
            ledger.collect(venue, 9000)
        assert len(ledger.events('trade')) == 2
    finally:
        ledger.close()


def test_background_budget_wait_preserves_twenty_slots_and_is_not_failure(tmp_path):
    ledger = make(tmp_path)
    venue = Paged([], [funding(1, 100000)])
    class Budget:
        def __init__(self): self.checks = []
        def check(self, slots):
            self.checks.append(slots)
            raise RequestDeferred(15)
    try:
        ledger.collect(venue, 600000)
        venue.budget = Budget()
        report = ledger.collect(venue, 660000)
        assert report['head_complete'] and venue.budget.checks == [21]
        assert report['kinds']['funding']['audit_requests'] == 0
        assert report['kinds']['funding']['audit_pending_reason'] == 'request_budget_deferred'
        # Same bucket, even after opening a new ledger instance, cannot retry audit.
        ledger.close(); ledger = make(tmp_path)
        ledger.collect(venue, 660001)
        assert venue.budget.checks == [21]
    finally:
        ledger.close()


def test_background_api_failure_propagates_and_does_not_advance_cursor(tmp_path):
    ledger = make(tmp_path)
    venue = Paged([], [funding(1, 100000)])
    try:
        ledger.collect(venue, 600000)
        original = venue.get
        def read(path):
            params = parse_qs(urlsplit(path).query)
            if path.startswith('/api/v1/positionFunding') and params['start_timestamp'] == ['0']:
                raise RuntimeError('read failure')
            return original(path)
        venue.get = read
        with pytest.raises(RuntimeError, match='read failure'):
            ledger.collect(venue, 660000)
        state = ledger.collector.report()['kinds']['funding']
        assert state['full_audit_complete_through_ms'] == 600000
        assert state['full_audit_scan']['cursor'] is None
    finally:
        ledger.close()


def test_fixed_page_end_survives_restart_and_future_rows_are_not_ingested(tmp_path):
    ledger = make(tmp_path)
    venue = Paged([trade(i, i*1000) for i in range(1, 7)], size=1)
    ledger.collect(venue, 5000)
    assert all(row['timestamp_ms'] <= 5000 for row in ledger.events('trade'))
    ledger.close(); ledger = make(tmp_path)
    try:
        ledger.collect(venue, 9000)
        assert ledger.collector.report()['kinds']['trades']['head_scan']['end_ms'] == 5000
        assert not ledger.collector.report()['head_complete']
    finally:
        ledger.close()


def test_damaged_checkpoint_fails_closed(tmp_path):
    ledger = make(tmp_path)
    try:
        ledger.db.execute('UPDATE history_collection_state SET value=? WHERE kind=?',
                          (json.dumps({'version':1}), 'trades'))
        ledger.db.commit()
        with pytest.raises(UnverifiedExecution, match='checkpoint'):
            ledger.collect(Paged(), 5000)
    finally:
        ledger.close()


def test_boolean_checkpoint_version_is_not_an_integer_version(tmp_path):
    ledger = make(tmp_path)
    try:
        state = ledger.collector._load('trades')
        state['version'] = True
        with ledger.db:
            ledger.db.execute('UPDATE history_collection_state SET value=? WHERE kind=?',
                              (json.dumps(state), 'trades'))
        with pytest.raises(UnverifiedExecution, match='checkpoint'):
            ledger.collect(Paged(), 5000)
    finally:
        ledger.close()

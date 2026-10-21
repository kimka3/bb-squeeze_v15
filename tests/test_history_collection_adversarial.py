"""Independent paginated-provider tests; no exchange access or signed orders.

The provider model deliberately separates event time from publication time.
Its stable offset cursors are an explicit fixture assumption, not a claim about
the exchange. Tests prove bounded local progress and repeated reconciliation
under that assumption; production cannot infer an upstream snapshot guarantee.
"""
import copy
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from live.account_ledger import AccountLedger
from live.execution_evidence import UnverifiedExecution
from live.request_budget import RequestBudget, RequestDeferred


START = 1_700_000_000_000


def trade(identity, stamp, *, market=1):
    return {'trade_id':identity, 'timestamp':stamp, 'type':'trade',
            'market_id':market, 'size':'1', 'price':'100', 'usd_amount':'100',
            'ask_account_id':200, 'bid_account_id':100, 'is_maker_ask':True,
            'bid_client_id':identity+1000, 'bid_id':identity+2000,
            'taker_fee':0, 'integrator_taker_fee':0}


def funding(identity, stamp, *, market=1):
    assert stamp % 1000 == 0
    return {'funding_id':identity, 'timestamp':stamp//1000,
            'market_id':market, 'position_side':'long', 'position_size':'1',
            'change':'-0.01', 'discount':'0', 'rate':'0.0001'}


class PagedHistoryVenue:
    """A paginated read API with real budget accounting and delayed records."""
    account_index = 100
    network = 'testnet'

    def __init__(self, tmp_path, now):
        self.now = now
        self.budget = RequestBudget(tmp_path/'budget.sqlite', clock=lambda:self.now/1000)
        self.records = {'trades':[], 'funding':[]}
        self.requests = []
        self.response_hook = None

    def add(self, kind, row, *, visible_at=None):
        self.records[kind].append({'row':copy.deepcopy(row),
                                   'visible_at':self.now if visible_at is None else visible_at})

    def advance(self, milliseconds=61_000):
        self.now += milliseconds

    def get(self, path):
        self.budget.check(consume=True)
        url = urlsplit(path)
        q = parse_qs(url.query)
        assert int(q['account_index'][0]) == self.account_index
        assert int(q['limit'][0]) == 100
        kind = 'trades' if url.path == '/api/v1/trades' else 'funding'
        assert url.path in ('/api/v1/trades', '/api/v1/positionFunding')
        cursor = q.get('cursor', [None])[0]
        if cursor is None:
            offset = 0
        else:
            # The punctuation ensures correct opaque-cursor URL encoding.
            assert cursor.startswith('offset+/=')
            offset = int(cursor[len('offset+/='):])
        rows = [copy.deepcopy(x['row']) for x in self.records[kind]
                if x['visible_at'] <= self.now]
        if kind == 'trades':
            assert q['sort_by'] == ['timestamp'] and q['sort_dir'] == ['desc']
            assert q['aggregate'] == ['false']
            rows.sort(key=lambda r:(r['timestamp'], r['trade_id']), reverse=True)
        else:
            lo, hi = int(q['start_timestamp'][0]), int(q['end_timestamp'][0])
            assert lo < hi
            # Strict upstream bounds: local exact filtering must still retain
            # the inclusive millisecond endpoints after request padding.
            rows = [r for r in rows if lo < r['timestamp'] < hi]
            rows.sort(key=lambda r:(r['timestamp'], r['market_id'], r['funding_id']), reverse=True)
        page = rows[offset:offset+100]
        nxt = f'offset+/={offset+100}' if offset+100 < len(rows) else ''
        key = 'trades' if kind == 'trades' else 'position_fundings'
        response = {key:page, 'next_cursor':nxt}
        self.requests.append({'kind':kind, 'cursor':cursor, 'query':q, 'at':self.now})
        if self.response_hook is not None:
            response = self.response_hook(kind, response, len(self.requests))
        return response


@pytest.fixture
def context(tmp_path):
    venue = PagedHistoryVenue(tmp_path, START+1_000_000)
    ledger = AccountLedger(tmp_path/'ledger.sqlite', 'testnet', 100, START)
    yield ledger, venue
    ledger.close()
    venue.budget.close()


def seed(venue, count, *, same_stamp=None, kinds=('trades','funding')):
    for identity in range(1, count+1):
        stamp = same_stamp if same_stamp is not None else START+identity*1000
        for kind in kinds:
            venue.add(kind, trade(identity, stamp) if kind == 'trades' else funding(identity, stamp))


def collect_bounded(ledger, venue, end):
    before = len(venue.requests)
    result = ledger.collect(venue, end)
    used = len(venue.requests)-before
    assert used == result['per_call_requests']
    assert used <= 6
    for kind in ('trades','funding'):
        row = result['kinds'][kind]
        assert row['head_requests'] <= 2 and row['audit_requests'] <= 1
    assert result == ledger.report()['history_collection']
    return result


def bootstrap(ledger, venue, end, *, maximum=100):
    for _ in range(maximum):
        result = collect_bounded(ledger, venue, end)
        if result['head_complete']:
            return result
        venue.advance()
    pytest.fail('bounded foreground passes did not make progress')


def ids(ledger, kind):
    key = 'trade_id' if kind == 'trade' else 'funding_id'
    return {r[key] for r in ledger.events(kind)}


def test_over_100_pages_complete_across_bounded_calls_and_db_reopen(tmp_path):
    # More than the previous complete-fetch hard cap, on both streams.
    venue = PagedHistoryVenue(tmp_path, START+20_000_000)
    path = tmp_path/'ledger.sqlite'
    ledger = AccountLedger(path, 'testnet', 100, START)
    try:
        seed(venue, 10_101)
        end = venue.now
        first = collect_bounded(ledger, venue, end)
        assert not first['head_complete'] and first['pending_reason'] == 'head_backlog'
        assert first['full_audit_complete_through_ms'] is None
        assert len(ledger.events('trade')) == len(ledger.events('funding')) == 200
        assert ledger.db.execute('SELECT end_ms FROM history_coverage').fetchall() == []
        ledger.close()
        ledger = AccountLedger(path, 'testnet', 100, START)
        venue.advance()
        result = bootstrap(ledger, venue, end, maximum=60)
        assert result['full_audit_complete_through_ms'] == end
        assert ids(ledger, 'trade') == ids(ledger, 'funding') == set(range(1,10_102))
        # Cursor resumes instead of restarting the same first pages forever.
        assert sum(r['cursor'] is None for r in venue.requests) == 2
        assert len(venue.requests) == 204
    finally:
        ledger.close()
        venue.budget.close()


def test_same_timestamp_across_pages_and_same_funding_id_in_other_market(context):
    ledger, venue = context
    seed(venue, 251, same_stamp=START+1000)
    venue.add('funding', funding(1, START+1000, market=2))
    first = collect_bounded(ledger, venue, venue.now)
    assert not first['head_complete']
    end = venue.now
    venue.advance()
    result = bootstrap(ledger, venue, end)
    assert result['head_complete']
    assert len(ledger.events('trade')) == 251 and len(ledger.events('funding')) == 252
    assert ids(ledger, 'trade') == set(range(1,252))
    assert len([r for r in ledger.events('funding') if r['funding_id'] == 1]) == 2


def test_late_records_older_than_overlap_found_by_repeated_full_audits(tmp_path):
    venue = PagedHistoryVenue(tmp_path, START+451_000)
    path = tmp_path/'ledger.sqlite'
    ledger = AccountLedger(path, 'testnet', 100, START)
    try:
        seed(venue, 450)
        initial_end = venue.now
        bootstrap(ledger, venue, initial_end)
        venue.advance()
        # Too old for the recent window and its first boundary page.
        venue.add('trades', trade(9001, START+2000))
        venue.add('funding', funding(9001, START+2000))
        first = collect_bounded(ledger, venue, venue.now)
        assert first['head_complete']
        assert 9001 not in ids(ledger, 'trade') and 9001 not in ids(ledger, 'funding')
        assert first['full_audit_complete_through_ms'] == initial_end
        for _ in range(15):
            ledger.close()
            ledger = AccountLedger(path, 'testnet', 100, START)
            venue.advance()
            collect_bounded(ledger, venue, venue.now)
            if 9001 in ids(ledger, 'trade') and 9001 in ids(ledger, 'funding'):
                break
        assert 9001 in ids(ledger, 'trade') and 9001 in ids(ledger, 'funding')
        # Publish a second old event only after the previous old audit passed.
        venue.add('trades', trade(9002, START+1000))
        venue.add('funding', funding(9002, START+1000))
        for _ in range(15):
            venue.advance()
            result = collect_bounded(ledger, venue, venue.now)
            if 9002 in ids(ledger, 'trade') and 9002 in ids(ledger, 'funding'):
                break
        assert ids(ledger, 'trade') == ids(ledger, 'funding') == set(range(1,451)) | {9001,9002}
        assert all(result['kinds'][k]['audit_passes'] >= 2 for k in ('trades','funding'))
    finally:
        ledger.close()
        venue.budget.close()


def test_publication_time_is_independent_of_event_timestamp(context):
    ledger, venue = context
    future_visibility = venue.now+61_000
    venue.add('trades', trade(1, START+1000), visible_at=future_visibility)
    venue.add('funding', funding(1, START+1000), visible_at=future_visibility)
    bootstrap(ledger, venue, venue.now)
    assert not ledger.events('trade') and not ledger.events('funding')
    venue.advance()
    result = collect_bounded(ledger, venue, venue.now)
    assert ids(ledger, 'trade') == ids(ledger, 'funding') == {1}
    assert 'not an upstream snapshot' in result['coverage_scope']


def test_page_movement_can_miss_a_new_old_record_but_next_full_pass_discovers_it(context):
    ledger, venue = context
    seed(venue, 450)
    inserted = False
    def publish_after_first_page(kind, response, call):
        nonlocal inserted
        if kind == 'trades' and not inserted:
            # Insert into the already-returned first page. Offset pagination
            # repeats a boundary row and cannot retrospectively return this
            # record in the current pass. It is also older than the overlap.
            venue.add('trades', trade(9001, START+425_000))
            inserted = True
        return response
    venue.response_hook = publish_after_first_page
    end = venue.now
    initial = bootstrap(ledger, venue, end)
    assert initial['full_audit_complete_through_ms'] == end
    assert 9001 not in ids(ledger, 'trade')
    assert 'not an upstream snapshot' in initial['coverage_scope']
    venue.response_hook = None
    for _ in range(8):
        venue.advance()
        collect_bounded(ledger, venue, venue.now)
        if 9001 in ids(ledger, 'trade'):
            break
    assert ids(ledger, 'trade') == set(range(1,451)) | {9001}


def test_changed_funding_record_is_not_silently_rewritten(context):
    ledger, venue = context
    venue.add('funding', funding(1, START+1000))
    end = venue.now
    bootstrap(ledger, venue, end)
    venue.records['funding'][0]['row']['change'] = '-0.02'
    venue.advance()
    # The record is outside the recent server window and is encountered by
    # the repeated full audit, which must retain the same strict conflict rule.
    with pytest.raises(UnverifiedExecution, match='history event changed'):
        ledger.collect(venue, venue.now)
    assert ledger.events('funding')[0]['cash_change'] == '-0.01'
    assert ledger.report()['history_collection']['full_audit_complete_through_ms'] == end


def test_background_budget_wait_preserves_twenty_slots_and_later_both_audits_progress(context):
    ledger, venue = context
    seed(venue, 450)
    bootstrap(ledger, venue, venue.now)
    venue.advance()
    for _ in range(33):
        venue.budget.check(consume=True)
    result = collect_bounded(ledger, venue, venue.now)
    assert result['per_call_requests'] == 2
    assert all(result['kinds'][k]['audit_pending_reason'] == 'request_budget_deferred'
               for k in ('trades','funding'))
    # Read capacity left for protection is actually consumable, not a mock count.
    for _ in range(20):
        venue.budget.check(consume=True)
    with pytest.raises(RequestDeferred):
        venue.budget.check(consume=True)
    venue.advance()
    result = collect_bounded(ledger, venue, venue.now)
    assert all(result['kinds'][k]['audit_requests'] == 1 for k in ('trades','funding'))


def test_foreground_budget_exhaustion_keeps_page_checkpoint_and_coverage_honest(context):
    ledger, venue = context
    seed(venue, 450)
    for _ in range(54):
        venue.budget.check(consume=True)
    with pytest.raises(RequestDeferred):
        ledger.collect(venue, venue.now)
    assert len(venue.requests) == 1
    assert len(ledger.events('trade')) == 100 and not ledger.events('funding')
    report = ledger.report()['history_collection']
    assert not report['head_complete'] and report['full_audit_complete_through_ms'] is None
    cursor = report['kinds']['trades']['head_scan']['cursor']
    end = venue.now
    venue.advance()
    bootstrap(ledger, venue, end)
    assert venue.requests[1]['cursor'] == cursor
    assert len(ledger.events('trade')) == len(ledger.events('funding')) == 450


def test_events_and_checkpoint_roll_back_together_after_crash(tmp_path, monkeypatch):
    class Crash(BaseException):
        pass
    venue = PagedHistoryVenue(tmp_path, START+1_000_000)
    path = tmp_path/'ledger.sqlite'
    ledger = AccountLedger(path, 'testnet', 100, START)
    try:
        seed(venue, 250)
        store = ledger._store_rows
        def interrupt_after_rows(*args):
            store(*args)
            raise Crash()
        monkeypatch.setattr(ledger, '_store_rows', interrupt_after_rows)
        with pytest.raises(Crash):
            ledger.collect(venue, venue.now)
        assert not ledger.events('trade')
        assert ledger.report()['history_collection']['kinds']['trades']['head_scan']['cursor'] is None
        ledger.close()
        ledger = AccountLedger(path, 'testnet', 100, START)
        bootstrap(ledger, venue, venue.now)
        assert len(ledger.events('trade')) == len(ledger.events('funding')) == 250
    finally:
        ledger.close()
        venue.budget.close()


def test_unchanged_records_are_idempotent_but_changed_id_record_fails_atomically(context):
    ledger, venue = context
    venue.add('trades', trade(1, START+1000))
    venue.add('funding', funding(1, START+1000))
    result = bootstrap(ledger, venue, venue.now)
    old_coverage = result['full_audit_complete_through_ms']
    collect_bounded(ledger, venue, venue.now)
    assert len(ledger.events('trade')) == len(ledger.events('funding')) == 1
    venue.records['trades'][0]['row']['taker_fee'] = '0.1'
    venue.add('trades', trade(2, START+2000))  # encountered before the conflict
    with pytest.raises(UnverifiedExecution, match='history event changed'):
        ledger.collect(venue, venue.now)
    assert ids(ledger, 'trade') == {1}
    assert ledger.events('trade')[0]['fee_usdc'] == '0'
    assert ledger.report()['history_collection']['full_audit_complete_through_ms'] == old_coverage


@pytest.mark.parametrize('anomaly', ['repeat_cursor','empty_with_cursor','ascending','invalid_cursor','oversized_page'])
def test_cursor_and_page_anomalies_are_hard_failures_without_false_coverage(context, anomaly):
    ledger, venue = context
    seed(venue, 250, kinds=('trades',))
    def corrupt(kind, response, call):
        if kind != 'trades':
            return response
        if anomaly == 'repeat_cursor' and call == 2:
            response['next_cursor'] = 'offset+/=100'
        elif anomaly == 'empty_with_cursor':
            response['trades'] = []
        elif anomaly == 'ascending':
            response['trades'][0],response['trades'][1] = response['trades'][1],response['trades'][0]
        elif anomaly == 'invalid_cursor':
            response['next_cursor'] = 123
        elif anomaly == 'oversized_page':
            response['trades'].append(copy.deepcopy(response['trades'][-1]))
        return response
    venue.response_hook = corrupt
    with pytest.raises(UnverifiedExecution):
        ledger.collect(venue, venue.now)
    report = ledger.report()['history_collection']
    assert not report['head_complete'] and report['full_audit_complete_through_ms'] is None
    assert len(ledger.events('trade')) == (100 if anomaly == 'repeat_cursor' else 0)


def test_persisted_cursor_identity_cannot_be_reopened_for_other_account_or_campaign(context, tmp_path):
    ledger, venue = context
    seed(venue, 250)
    collect_bounded(ledger, venue, venue.now)
    for account,start in [(101,START),(100,START+1000)]:
        with pytest.raises(UnverifiedExecution, match='ledger account/campaign mismatch'):
            AccountLedger(tmp_path/'ledger.sqlite', 'testnet', account, start)

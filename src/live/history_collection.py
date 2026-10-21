"""Bounded history reads with a durable, repeatedly restarted full audit.

A finished cursor pass is an observation, not an upstream completeness promise.
Lighter does not document a snapshot cursor or a maximum publication delay.
Recent overlap therefore never retires old history: full passes keep repeating.
Eventual discovery requires the provider to keep returning records and to allow
pagination to make progress. A changed known event remains a hard failure.
"""
import copy
import json
from urllib.parse import urlencode

from .execution_evidence import UnverifiedExecution
from .request_budget import RequestDeferred


KINDS = ('trades', 'funding')
OVERLAP_MS = 60_000
HEAD_PAGES = 2
AUDIT_INTERVAL_MS = 60_000


def _integer(value):
    if type(value) is not int or value < 0:
        raise UnverifiedExecution('invalid history collection checkpoint')
    return value


class HistoryCollector:
    def __init__(self, ledger):
        self.ledger, self.db = ledger, ledger.db
        with self.db:
            self.db.execute('CREATE TABLE IF NOT EXISTS history_collection_state '
                            '(kind TEXT PRIMARY KEY,value TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS history_collection_cursors '
                            '(kind TEXT,lane TEXT,cursor TEXT,PRIMARY KEY(kind,lane,cursor))')
            legacy = self.db.execute('SELECT end_ms FROM history_coverage WHERE id=1').fetchone()
            floor = _integer(legacy[0]) if legacy else None
            for kind in KINDS:
                initial = {'version':1, 'head':None, 'audit':None,
                           'head_completed_end_ms':floor, 'full_completed_end_ms':floor,
                           'last_requested_end_ms':None, 'audit_last_bucket':None,
                           'audit_pending_reason':None, 'head_passes':0, 'audit_passes':0,
                           'last_call':{'head_requests':0, 'audit_requests':0}}
                self.db.execute('INSERT OR IGNORE INTO history_collection_state VALUES(?,?)',
                                (kind, json.dumps(initial, sort_keys=True)))
        for kind in KINDS:
            self._load(kind)

    def _load(self, kind):
        try:
            row = self.db.execute('SELECT value FROM history_collection_state WHERE kind=?', (kind,)).fetchone()
            state = json.loads(row[0])
            if not isinstance(state, dict) or type(state['version']) is not int or state['version'] != 1:
                raise ValueError('version')
            for key in ('head_completed_end_ms', 'full_completed_end_ms',
                        'last_requested_end_ms', 'audit_last_bucket'):
                if state[key] is not None:
                    _integer(state[key])
            for key in ('head_passes', 'audit_passes'):
                _integer(state[key])
            for lane in ('head', 'audit'):
                checkpoint = state[lane]
                if checkpoint is None:
                    continue
                if not isinstance(checkpoint, dict):
                    raise ValueError('checkpoint')
                start, end = _integer(checkpoint['start_ms']), _integer(checkpoint['end_ms'])
                if not self.ledger.start_ms <= start <= end:
                    raise ValueError('scope')
                cursor = checkpoint['cursor']
                if cursor is not None and (not isinstance(cursor, str) or not cursor):
                    raise ValueError('cursor')
                previous, pages = checkpoint['previous_ms'], _integer(checkpoint['pages'])
                if previous is not None:
                    _integer(previous)
                if bool(pages) != bool(cursor) or bool(pages) != (previous is not None):
                    raise ValueError('progress')
            if not isinstance(state['last_call'], dict):
                raise ValueError('counts')
            for key in ('head_requests', 'audit_requests'):
                _integer(state['last_call'][key])
            return state
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise UnverifiedExecution('invalid history collection checkpoint') from exc

    def _save(self, kind, state):
        self.db.execute('UPDATE history_collection_state SET value=? WHERE kind=?',
                        (json.dumps(state, sort_keys=True), kind))

    def _clear_cursors(self, kind, lane):
        self.db.execute('DELETE FROM history_collection_cursors WHERE kind=? AND lane=?', (kind, lane))

    def _coverage(self):
        values = [self._load(kind)['full_completed_end_ms'] for kind in KINDS]
        if any(value is None for value in values):
            return
        observed = min(values)
        self.db.execute('INSERT INTO history_coverage VALUES(1,?) '
                        'ON CONFLICT(id) DO UPDATE SET end_ms=MAX(end_ms,excluded.end_ms)', (observed,))

    def _begin(self, kind, lane, state, end_ms):
        state = copy.deepcopy(state)
        start = self.ledger.start_ms
        if lane == 'head' and state['head_completed_end_ms'] is not None:
            start = max(start, state['head_completed_end_ms'] - OVERLAP_MS)
        state[lane] = {'start_ms':start, 'end_ms':end_ms, 'cursor':None,
                       'previous_ms':None, 'pages':0}
        with self.db:
            self._clear_cursors(kind, lane)
            self._save(kind, state)
        return state

    def _advance(self, transport, kind, lane, state, end_ms):
        if state[lane] is None:
            state = self._begin(kind, lane, state, end_ms)
        checkpoint = state[lane]
        params = {'account_index':transport.account_index, 'limit':100}
        if kind == 'trades':
            # /trades has no documented start/end timestamp filter. Its `from`
            # argument has no documented boundary semantics; do not guess them.
            params.update(sort_by='timestamp', sort_dir='desc', aggregate='false')
            endpoint, key, scale = '/api/v1/trades', 'trades', 1
        else:
            params.update(start_timestamp=max(0, checkpoint['start_ms']//1000-1),
                          end_timestamp=checkpoint['end_ms']//1000+1)
            endpoint, key, scale = '/api/v1/positionFunding', 'position_fundings', 1000
        if checkpoint['cursor'] is not None:
            params['cursor'] = checkpoint['cursor']
        state = copy.deepcopy(state)
        state['last_call'][lane+'_requests'] += 1
        with self.db:
            self._save(kind, state)
        data = transport.get(endpoint+'?'+urlencode(params))
        page = data.get(key) if isinstance(data, dict) else None
        if not isinstance(page, list):
            raise UnverifiedExecution('history list missing')
        if len(page) > 100:
            raise UnverifiedExecution('history page exceeds requested limit')
        previous, crossed, crossed_campaign, rows = checkpoint['previous_ms'], False, False, []
        for row in page:
            if not isinstance(row, dict):
                raise UnverifiedExecution('invalid history row')
            stamp = row.get('timestamp')
            if type(stamp) is not int or stamp < 0:
                raise UnverifiedExecution('invalid history identity/timestamp')
            stamp *= scale
            if previous is not None and stamp > previous:
                raise UnverifiedExecution('history is not descending')
            previous = stamp
            crossed |= stamp < checkpoint['start_ms']
            crossed_campaign |= stamp < self.ledger.start_ms
            # Preserve every observed campaign row, including older overlap
            # rows returned on the boundary page. Never delete an absent row.
            if self.ledger.start_ms <= stamp <= checkpoint['end_ms']:
                rows.append(row)
        nxt = data.get('next_cursor')
        if nxt is not None and not isinstance(nxt, str):
            raise UnverifiedExecution('invalid history cursor')
        if nxt and (not page or self.db.execute(
                'SELECT 1 FROM history_collection_cursors WHERE kind=? AND lane=? AND cursor=?',
                (kind, lane, nxt)).fetchone()):
            raise UnverifiedExecution('history cursor stalled')
        complete = crossed or not nxt
        # Funding's recent server window cannot establish campaign-wide scope.
        full = crossed_campaign or (not nxt and (kind == 'trades' or
                                                checkpoint['start_ms'] == self.ledger.start_ms))
        normalized = self.ledger._normalize_rows(rows if kind == 'trades' else [],
                                                 rows if kind == 'funding' else [])
        updated = copy.deepcopy(state)
        if complete:
            updated[lane] = None
            updated[lane+'_passes'] += 1
            if lane == 'head':
                updated['head_completed_end_ms'] = checkpoint['end_ms']
        else:
            updated[lane] = {**checkpoint, 'cursor':nxt, 'previous_ms':previous,
                             'pages':checkpoint['pages']+1}
        if full:
            updated['full_completed_end_ms'] = max(checkpoint['end_ms'], updated['full_completed_end_ms'] or 0)
            if lane == 'head' and (updated['audit'] is None or
                                  updated['audit']['end_ms'] <= checkpoint['end_ms']):
                updated['audit'] = None
        with self.db:
            self.ledger._store_rows(normalized, checkpoint['end_ms'])
            if complete:
                self._clear_cursors(kind, lane)
            elif nxt:
                self.db.execute('INSERT INTO history_collection_cursors VALUES(?,?,?)', (kind, lane, nxt))
            if full and lane == 'head' and updated['audit'] is None:
                self._clear_cursors(kind, 'audit')
            self._save(kind, updated)
            self._coverage()
        return updated

    def collect(self, transport, end_ms):
        end_ms = _integer(end_ms)
        if end_ms < self.ledger.start_ms or transport.account_index != self.ledger.account:
            raise UnverifiedExecution('history collection scope mismatch')
        for kind in KINDS:
            state = self._load(kind)
            prior = state['last_requested_end_ms']
            floor = state['head_completed_end_ms']
            if end_ms < max(prior or 0, floor or 0):
                raise UnverifiedExecution('coverage regression')
            state['last_requested_end_ms'] = end_ms
            state['last_call'] = {'head_requests':0, 'audit_requests':0}
            state['audit_pending_reason'] = None
            with self.db:
                self._save(kind, state)
        for kind in KINDS:
            state = self._load(kind)
            # Always visit the head, even at the same requested timestamp: a
            # record can be published between two otherwise identical reads.
            for index in range(HEAD_PAGES):
                if index and state['head'] is None and state['head_completed_end_ms'] >= end_ms:
                    break
                state = self._advance(transport, kind, 'head', state, end_ms)
        for kind in KINDS:
            state = self._load(kind)
            if (state['full_completed_end_ms'] or -1) >= end_ms:
                continue
            if state['head'] is not None and state['head']['start_ms'] == self.ledger.start_ms:
                state['audit_pending_reason'] = 'foreground_full_pass_in_progress'
            elif state['audit_last_bucket'] == end_ms // AUDIT_INTERVAL_MS:
                state['audit_pending_reason'] = 'audit_cadence'
            else:
                # Persist the attempt bucket as well as page progress. Restart
                # must not turn a bounded audit into a burst of duplicate reads.
                state['audit_last_bucket'] = end_ms // AUDIT_INTERVAL_MS
                with self.db:
                    self._save(kind, state)
                try:
                    budget = getattr(transport, 'budget', None)
                    if budget is not None:
                        budget.check(21)  # one read, retaining 20 for protection
                    state = self._advance(transport, kind, 'audit', state, end_ms)
                except RequestDeferred as exc:
                    state = self._load(kind)
                    state['audit_pending_reason'] = 'request_budget_deferred'
                    state['audit_retry_after_seconds'] = exc.retry_after
                # All schema, identity, ordering, storage and API failures are
                # propagated. Only background request-budget waiting is normal.
            with self.db:
                self._save(kind, state)
        return self.report()

    def report(self):
        kinds = {}
        for kind in KINDS:
            state = self._load(kind)
            target, completed = state['last_requested_end_ms'], state['head_completed_end_ms']
            head_complete = (target is not None and completed is not None and
                             completed >= target and state['head'] is None)
            kinds[kind] = {'head_complete':head_complete,
                           'head_completed_end_ms':completed,
                           'full_audit_complete_through_ms':state['full_completed_end_ms'],
                           'requested_end_ms':target, 'head_scan':state['head'],
                           'full_audit_scan':state['audit'],
                           'head_passes':state['head_passes'], 'audit_passes':state['audit_passes'],
                           'audit_last_bucket':state['audit_last_bucket'],
                           'audit_pending_reason':state['audit_pending_reason'],
                           **state['last_call']}
        complete = all(item['head_complete'] for item in kinds.values())
        coverage = self.db.execute('SELECT end_ms FROM history_coverage WHERE id=1').fetchone()
        return {'head_complete':complete, 'pending_reason':None if complete else 'head_backlog',
                'full_audit_complete_through_ms':coverage[0] if coverage else None,
                'kinds':kinds,
                'per_call_requests':sum(item['head_requests']+item['audit_requests'] for item in kinds.values()),
                'request_count_scope':'transport.get calls; existing bounded transport retry is separate',
                'limits':{'head_pages_per_kind':HEAD_PAGES, 'audit_pages_per_kind_per_bucket':1,
                          'audit_bucket_ms':AUDIT_INTERVAL_MS, 'recent_overlap_ms':OVERLAP_MS},
                'coverage_scope':'observed completed cursor passes, not an upstream snapshot or publication-delay guarantee',
                'eventual_discovery_requires':'records remain queryable and repeated full pagination makes progress'}

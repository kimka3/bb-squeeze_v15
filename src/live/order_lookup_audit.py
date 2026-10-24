"""Bounded repeated checks of positively unsent, non-current order identities.

The caller proves eligibility; a missing venue row never proves non-submission.
Active orders, collected trades and current/unknown operations remain foreground
checks. A discovered contradiction is durable and never expires with the scan.
"""
import json
import math
import time

from .execution_evidence import UnverifiedExecution
from .request_budget import RequestDeferred


INTERVAL_MS = 60_100
LANES = ('foreground', 'background')
SOURCES = ('active', 'trade', 'audit', 'foreground')


def _integer(value):
    if type(value) is not int or value < 0:
        raise ValueError('nonnegative integer required')
    return value


def _coi(value):
    if _integer(value) >= 2**48:
        raise ValueError('invalid client order identity')
    return value


def _seconds(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('invalid elapsed time')
    return value


def _metrics():
    return {'logical_calls': 0, 'http_attempts': 0, 'http_retries': 0,
            'unmeasured_calls': 0, 'last_seconds': None, 'total_seconds': 0.0,
            'max_seconds': 0.0, 'last_outcome': None}


class RetiredOrderAudit:
    def __init__(self, db, clock=time.time):
        self.db, self.clock = db, clock
        initial = {'version': 1, 'candidate_count': 0, 'candidate_ids': [], 'cursor': None,
                   'passes': 0, 'pass_ids': [], 'pass_index': 0,
                   'next_due_ms': 0, 'last_attempt_ms': None,
                   'last_outcome': None, 'deferred_reason': None,
                   'metrics': {lane: _metrics() for lane in LANES},
                   'last_lookup': None, 'last_run': None}
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS order_lookup_audit_state '
                       '(id INTEGER PRIMARY KEY CHECK(id=1),value TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS order_lookup_audit_checks '
                       '(coi TEXT PRIMARY KEY,value TEXT NOT NULL)')
            db.execute('INSERT OR IGNORE INTO order_lookup_audit_state VALUES(1,?)',
                       (json.dumps(initial, sort_keys=True),))
        self._load()
        self._checks()

    def _now(self):
        value = self.clock()
        _seconds(value)
        return int(value * 1000)

    def _load(self):
        try:
            row = self.db.execute('SELECT value FROM order_lookup_audit_state WHERE id=1').fetchone()
            state = json.loads(row[0])
            if type(state) is not dict or type(state['version']) is not int or state['version'] != 1:
                raise ValueError('version')
            for key in ('candidate_count', 'passes', 'pass_index', 'next_due_ms'):
                _integer(state[key])
            for key in ('cursor', 'last_attempt_ms'):
                if state[key] is not None:
                    (_coi if key == 'cursor' else _integer)(state[key])
            ids = state['pass_ids']
            if type(ids) is not list or len(ids) != len(set(ids)):
                raise ValueError('scan identities')
            for coi in ids:
                _coi(coi)
            roster = state['candidate_ids']
            if (type(roster) is not list or len(roster) != len(set(roster))
                    or len(roster) != state['candidate_count']):
                raise ValueError('candidate identities')
            for coi in roster:
                _coi(coi)
            if state['pass_index'] > len(ids):
                raise ValueError('scan progress')
            for key in ('last_outcome', 'deferred_reason'):
                if state[key] is not None and (type(state[key]) is not str or len(state[key]) > 100):
                    raise ValueError('diagnostic')
            if set(state['metrics']) != set(LANES):
                raise ValueError('metrics lanes')
            for lane in LANES:
                metric = state['metrics'][lane]
                for key in ('logical_calls', 'http_attempts', 'http_retries', 'unmeasured_calls'):
                    _integer(metric[key])
                if metric['unmeasured_calls'] > metric['logical_calls']:
                    raise ValueError('metric counts')
                for key in ('total_seconds', 'max_seconds'):
                    _seconds(metric[key])
                if metric['last_seconds'] is not None:
                    _seconds(metric['last_seconds'])
            for key in ('last_lookup', 'last_run'):
                if state[key] is not None and type(state[key]) is not dict:
                    raise ValueError('call metrics')
            return state
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise UnverifiedExecution('invalid order lookup audit checkpoint') from exc

    def _save(self, state):
        self.db.execute('UPDATE order_lookup_audit_state SET value=? WHERE id=1',
                        (json.dumps(state, sort_keys=True, allow_nan=False),))

    def _checks(self):
        try:
            result = {}
            for encoded_coi, encoded in self.db.execute('SELECT coi,value FROM order_lookup_audit_checks'):
                coi = int(encoded_coi)
                if str(_coi(coi)) != encoded_coi:
                    raise ValueError('identity')
                value = json.loads(encoded)
                if type(value) is not dict or type(value['version']) is not int or value['version'] != 1:
                    raise ValueError('check version')
                stamp = value['checked_at_ms']
                if stamp is not None:
                    _integer(stamp)
                classification = value['classification']
                if classification is not None and (type(classification) is not str or not classification):
                    raise ValueError('classification')
                conflict = value['conflict']
                if conflict is not None:
                    if type(conflict) is not dict or conflict['source'] not in SOURCES:
                        raise ValueError('conflict')
                    _integer(conflict['observed_at_ms'])
                result[coi] = value
            return result
        except (KeyError, TypeError, ValueError) as exc:
            raise UnverifiedExecution('invalid order lookup audit evidence') from exc

    @staticmethod
    def _candidates(candidates):
        if type(candidates) is not dict:
            raise UnverifiedExecution('invalid order lookup audit candidates')
        try:
            for coi, value in candidates.items():
                _coi(coi)
                label = value['classification']
                if type(value) is not dict or type(label) is not str or not label or len(label) > 100:
                    raise ValueError('classification')
        except (KeyError, TypeError, ValueError) as exc:
            raise UnverifiedExecution('invalid order lookup audit candidates') from exc
        return candidates

    def check_latched(self):
        self._load()
        if any(value['conflict'] is not None for value in self._checks().values()):
            raise UnverifiedExecution('order lookup audit retained a confirmed local non-submission conflict')

    def observe_conflict(self, coi, source):
        _coi(coi)
        if source not in SOURCES:
            raise ValueError('invalid order contradiction source')
        checks = self._checks()
        value = checks.get(coi, {'version': 1, 'checked_at_ms': None,
                                'classification': None, 'conflict': None})
        if value['conflict'] is None:
            value['conflict'] = {'source': source, 'observed_at_ms': self._now()}
            with self.db:
                self.db.execute('INSERT INTO order_lookup_audit_checks VALUES(?,?) '
                    'ON CONFLICT(coi) DO UPDATE SET value=excluded.value',
                    (str(coi), json.dumps(value, sort_keys=True)))
        state = self._load()
        state.update(last_outcome='conflict', deferred_reason=None)
        if state['last_run'] is not None and state['last_run'].get('coi') == coi:
            state['last_run']['outcome'] = 'conflict'
        with self.db:
            self._save(state)

    @staticmethod
    def _samples(transport, before):
        after = getattr(transport, 'read_sample_sequence', None)
        samples = getattr(transport, 'request_samples', None)
        if (type(before) is not int or type(after) is not int or after < before
                or samples is None):
            return None, None
        count = after - before
        if count > len(samples):
            return None, None
        observed = list(samples)[-count:] if count else []
        if any(type(row) is not dict for row in observed):
            return None, None
        # Authentication can fail before the HTTP attempt. Transport/read
        # errors after authentication are attempted HTTP reads, not fills.
        http = [row for row in observed if row.get('outcome') != 'auth_error']
        if any(type(row.get('attempt')) is not int or row['attempt'] < 1 for row in http):
            return None, None
        return len(http), sum(row['attempt'] > 1 for row in http)

    def lookup(self, coi, transport, lane='foreground'):
        _coi(coi)
        if lane not in LANES:
            raise ValueError('invalid order lookup lane')
        before = getattr(transport, 'read_sample_sequence', None)
        state = self._load()
        metric = state['metrics'][lane]
        metric['logical_calls'] += 1
        # Count the call before entering transport. A process crash leaves an
        # explicitly unmeasured call rather than a falsely complete HTTP sum.
        metric['unmeasured_calls'] += 1
        metric['last_outcome'] = 'in_progress'
        state['last_lookup'] = {'lane': lane, 'coi': coi, 'logical_calls': 1,
            'http_attempts': None, 'http_retries': None,
            'request_seconds': None, 'outcome': 'in_progress'}
        with self.db:
            self._save(state)
        started = time.monotonic()
        outcome = 'error'
        try:
            row = transport.order(coi)
            if row is not None and type(row) is not dict:
                raise UnverifiedExecution('invalid order lookup response')
            outcome = 'absent' if row is None else 'present'
            return row
        except RequestDeferred:
            outcome = 'budget_deferred'
            raise
        finally:
            seconds = max(0.0, time.monotonic() - started)
            attempts, retries = self._samples(transport, before)
            state = self._load()
            metric = state['metrics'][lane]
            if attempts is not None:
                metric['unmeasured_calls'] -= 1
                metric['http_attempts'] += attempts
                metric['http_retries'] += retries
            metric.update(last_seconds=seconds, total_seconds=metric['total_seconds'] + seconds,
                          max_seconds=max(metric['max_seconds'], seconds), last_outcome=outcome)
            state['last_lookup'] = {'lane': lane, 'coi': coi, 'logical_calls': 1,
                'http_attempts': attempts, 'http_retries': retries,
                'request_seconds': seconds, 'outcome': outcome}
            with self.db:
                self._save(state)

    def run(self, candidates, transport, blocked_reason=None):
        candidates = self._candidates(candidates)
        self.check_latched()
        state = self._load()
        now = self._now()
        state['candidate_count'] = len(candidates)
        state['candidate_ids'] = list(candidates)
        state['last_run'] = {'logical_calls': 0, 'coi': None, 'outcome': None}
        if blocked_reason is not None and (type(blocked_reason) is not str or not blocked_reason or len(blocked_reason) > 100):
            raise ValueError('invalid audit blocked reason')
        reason = blocked_reason or ('no_candidates' if not candidates else None)
        if reason is None and state['last_attempt_ms'] is not None and now < state['last_attempt_ms']:
            reason = 'clock_regression'
        if reason is None and now < state['next_due_ms']:
            reason = 'audit_cadence'
        if reason is not None:
            state.update(deferred_reason=reason)
            state['last_run']['outcome'] = reason
            with self.db:
                self._save(state)
            return self.report(candidates)
        # Freeze a pass roster. Appended candidates join the next pass, so a
        # growing backlog cannot indefinitely starve previously checked IDs.
        while state['pass_index'] < len(state['pass_ids']) and state['pass_ids'][state['pass_index']] not in candidates:
            state['pass_index'] += 1
        if state['pass_index'] == len(state['pass_ids']):
            if state['pass_ids']:
                state['passes'] += 1
            state.update(pass_ids=list(candidates), pass_index=0)
        coi = state['pass_ids'][state['pass_index']]
        try:
            budget = getattr(transport, 'budget', None)
            if budget is not None:
                budget.check(21)
        except RequestDeferred:
            state['deferred_reason'] = 'request_budget'
            state['last_run']['outcome'] = 'request_budget'
            with self.db:
                self._save(state)
            return self.report(candidates)
        # Attempt timing is committed before the GET. A crash cannot reset the
        # allowance; failed queries retain this ID and do not advance the pass.
        state.update(last_attempt_ms=now, next_due_ms=now + INTERVAL_MS,
                     deferred_reason=None, last_outcome='in_progress')
        state['last_run'] = {'logical_calls': 1, 'coi': coi, 'outcome': 'in_progress'}
        with self.db:
            self._save(state)
        try:
            row = self.lookup(coi, transport, lane='background')
        except Exception:
            state = self._load()
            state['last_outcome'] = state['last_lookup']['outcome'] if state['last_lookup'] else 'error'
            state['last_run']['outcome'] = state['last_outcome']
            with self.db:
                self._save(state)
            raise
        if row is not None:
            self.observe_conflict(coi, 'audit')
            self.check_latched()
        state = self._load()
        value = {'version': 1, 'checked_at_ms': self._now(),
                 'classification': candidates[coi]['classification'], 'conflict': None}
        state['cursor'] = coi
        state['pass_index'] += 1
        state['last_outcome'] = 'absent'
        state['last_run']['outcome'] = 'absent'
        if state['pass_index'] == len(state['pass_ids']):
            state.update(passes=state['passes'] + 1, pass_ids=[], pass_index=0)
        with self.db:
            self.db.execute('INSERT INTO order_lookup_audit_checks VALUES(?,?) '
                'ON CONFLICT(coi) DO UPDATE SET value=excluded.value',
                (str(coi), json.dumps(value, sort_keys=True)))
            self._save(state)
        return self.report(candidates)

    @staticmethod
    def _metric_report(metric):
        return {'logical_calls': metric['logical_calls'],
                'http_attempts': None if metric['unmeasured_calls'] else metric['http_attempts'],
                'http_retries': None if metric['unmeasured_calls'] else metric['http_retries'],
                'measured_http_attempts': metric['http_attempts'],
                'measured_http_retries': metric['http_retries'],
                'unmeasured_calls': metric['unmeasured_calls'],
                'request_seconds': {'last': metric['last_seconds'], 'total': metric['total_seconds'],
                                    'max': metric['max_seconds']},
                'last_outcome': metric['last_outcome']}

    def report(self, candidates=None):
        state, checks = self._load(), self._checks()
        selected = state['candidate_ids'] if candidates is None else self._candidates(candidates)
        stamps = [checks[coi]['checked_at_ms'] for coi in selected
                  if coi in checks and checks[coi]['checked_at_ms'] is not None]
        count = state['candidate_count'] if candidates is None else len(candidates)
        checked = min(len(stamps), count)
        metrics = {lane: self._metric_report(state['metrics'][lane]) for lane in LANES}
        total = _metrics()
        for metric in state['metrics'].values():
            for key in ('logical_calls', 'http_attempts', 'http_retries', 'unmeasured_calls', 'total_seconds'):
                total[key] += metric[key]
            total['max_seconds'] = max(total['max_seconds'], metric['max_seconds'])
        if state['last_lookup']:
            total['last_seconds'] = state['last_lookup']['request_seconds']
            total['last_outcome'] = state['last_lookup']['outcome']
        return {'version': 1, 'candidate_count': count, 'checked_count': checked,
                'pending_count': count - checked, 'oldest_check_ms': min(stamps) if stamps else None,
                'cursor': state['cursor'], 'passes': state['passes'],
                'next_due_ms': state['next_due_ms'], 'last_attempt_ms': state['last_attempt_ms'],
                'deferred_reason': state['deferred_reason'],
                'conflicts': {str(coi): value['conflict'] for coi, value in checks.items() if value['conflict']},
                'foreground': metrics['foreground'], 'background': metrics['background'],
                **self._metric_report(total), 'last_outcome': state['last_outcome'],
                'last_run': state['last_run'],
                'per_call': state['last_lookup'],
                'meaning': 'absence observations only; repeated audit is not non-submission or complete venue history proof'}

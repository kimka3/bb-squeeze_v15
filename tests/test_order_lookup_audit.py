"""Durable bounded background reads; no network, signer, credentials or orders."""
from collections import deque
import json
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from live.execution_evidence import UnverifiedExecution
from live.order_lookup_audit import INTERVAL_MS, RetiredOrderAudit
from live.request_budget import RequestDeferred


class Clock:
    def __init__(self):
        self.value = 1_800_000_000.0

    def __call__(self):
        return self.value

    def advance(self, seconds=INTERVAL_MS / 1000 + .001):
        self.value += seconds


class Transport:
    def __init__(self):
        self.calls = []
        self.rows = {}

    def order(self, coi):
        self.calls.append(coi)
        value = self.rows.get(coi)
        if isinstance(value, Exception):
            raise value
        return value


def candidates(*ids):
    return {coi: {'classification': 'superseded_stop'} for coi in ids}


@pytest.fixture
def setup(tmp_path):
    db = sqlite3.connect(tmp_path / 'execution.sqlite')
    clock, transport = Clock(), Transport()
    audit = RetiredOrderAudit(db, clock)
    yield db, clock, transport, audit
    db.close()


def test_one_get_per_real_interval_and_repeated_full_passes(setup):
    _, clock, transport, audit = setup
    roster = candidates(31, 12, 99)
    for expected in (31, 12, 99, 31, 12, 99):
        report = audit.run(roster, transport)
        assert transport.calls[-1] == expected
        count = len(transport.calls)
        assert audit.run(roster, transport)['deferred_reason'] == 'audit_cadence'
        clock.advance(60)
        assert audit.run(roster, transport)['deferred_reason'] == 'audit_cadence'
        assert len(transport.calls) == count
        clock.advance(.101)
    assert report['passes'] == 2
    assert report['checked_count'] == 3 and report['pending_count'] == 0
    assert report['logical_calls'] == 6 and report['background']['logical_calls'] == 6
    assert report['foreground']['logical_calls'] == 0
    assert report['http_attempts'] is None and report['unmeasured_calls'] == 6


def test_restart_preserves_allowance_and_cursor(setup):
    db, clock, transport, audit = setup
    audit.run(candidates(4, 9), transport)
    other = RetiredOrderAudit(db, clock)
    other.run(candidates(4, 9), transport)
    assert transport.calls == [4]
    clock.advance()
    other.run(candidates(4, 9), transport)
    assert transport.calls == [4, 9]
    assert other.report()['passes'] == 1


def test_persisted_allowance_survives_closed_connection(tmp_path):
    path = tmp_path / 'execution.sqlite'
    clock, transport = Clock(), Transport()
    db = sqlite3.connect(path)
    RetiredOrderAudit(db, clock).run(candidates(1, 2), transport)
    db.close()
    db = sqlite3.connect(path)
    try:
        restored = RetiredOrderAudit(db, clock)
        restored.run(candidates(1, 2), transport)
        clock.advance()
        restored.run(candidates(1, 2), transport)
        assert transport.calls == [1, 2]
    finally:
        db.close()


def test_new_candidates_cannot_extend_a_frozen_pass_forever(setup):
    _, clock, transport, audit = setup
    roster = candidates(1, 2)
    audit.run(roster, transport)
    for coi in range(3, 8):
        roster.update(candidates(coi))
        clock.advance()
        audit.run(roster, transport)
    assert transport.calls[:3] == [1, 2, 1]
    assert audit.report(roster)['passes'] == 2


def test_removed_candidates_are_not_queried_and_new_ones_progress(setup):
    _, clock, transport, audit = setup
    audit.run(candidates(1, 2, 3), transport)
    clock.advance()
    audit.run(candidates(3, 4), transport)
    clock.advance()
    audit.run(candidates(4), transport)
    assert transport.calls == [1, 3, 4]
    assert audit.report()['candidate_count'] == 1
    assert audit.report()['checked_count'] == 1


def test_budget_check_keeps_twenty_slots_and_does_not_advance(setup):
    _, clock, transport, audit = setup
    class Budget:
        def __init__(self):
            self.blocked = True
            self.slots = []
        def check(self, slots):
            self.slots.append(slots)
            if self.blocked:
                raise RequestDeferred(20)
    transport.budget = Budget()
    report = audit.run(candidates(1, 2), transport)
    assert report['deferred_reason'] == 'request_budget'
    assert report['logical_calls'] == 0 and report['cursor'] is None
    assert report['next_due_ms'] == 0 and transport.calls == []
    transport.budget.blocked = False
    audit.run(candidates(1, 2), transport)
    assert transport.calls == [1]
    assert transport.budget.slots == [21, 21]


@pytest.mark.parametrize('reason', ['entry_deadline', 'unresolved_orders', 'unsafe_protection', 'publication_pending'])
def test_operational_block_skips_only_extra_get(setup, reason):
    _, _, transport, audit = setup
    report = audit.run(candidates(1, 2), transport, blocked_reason=reason)
    assert report['candidate_count'] == 2 and report['pending_count'] == 2
    assert report['deferred_reason'] == reason
    assert report['logical_calls'] == 0 and transport.calls == []
    assert audit.lookup(8, transport) is None
    assert audit.report()['foreground']['logical_calls'] == 1


def test_absent_current_roster_is_not_reported_as_proven_no_candidates(setup):
    _, _, transport, audit = setup
    report = audit.run({}, transport, blocked_reason='no_current_reconciliation')
    assert report['deferred_reason'] == 'no_current_reconciliation'
    assert report['last_run']['outcome'] == 'no_current_reconciliation'
    assert report['logical_calls'] == 0 and transport.calls == []


@pytest.mark.parametrize('failure', [TimeoutError('failure'), RuntimeError('schema'), RequestDeferred(2)])
def test_failure_is_not_absence_and_retries_same_id_after_interval(setup, failure):
    db, clock, transport, audit = setup
    transport.rows[1] = failure
    with pytest.raises(type(failure)):
        audit.run(candidates(1, 2), transport)
    assert audit.report()['checked_count'] == 0 and audit.report()['cursor'] is None
    restored = RetiredOrderAudit(db, clock)
    restored.run(candidates(1, 2), transport)
    assert transport.calls == [1]
    clock.advance()
    del transport.rows[1]
    restored.run(candidates(1, 2), transport)
    assert transport.calls == [1, 1]
    assert restored.report()['cursor'] == 1


def test_attempt_is_committed_before_transport_call(setup):
    db, clock, transport, audit = setup
    def interrupted(coi):
        state = json.loads(db.execute('SELECT value FROM order_lookup_audit_state').fetchone()[0])
        assert state['last_attempt_ms'] == int(clock() * 1000)
        assert state['next_due_ms'] == int(clock() * 1000) + INTERVAL_MS
        assert state['cursor'] is None
        raise KeyboardInterrupt
    transport.order = interrupted
    with pytest.raises(KeyboardInterrupt):
        audit.run(candidates(1), transport)
    assert RetiredOrderAudit(db, clock).run(candidates(1), Transport())['deferred_reason'] == 'audit_cadence'


@pytest.mark.parametrize('row', [{'client_order_index': 1, 'status': 'filled'}, {}, {'unexpected': True}])
def test_any_present_row_latches_conflict_without_fabricating_result(setup, row):
    db, clock, transport, audit = setup
    transport.rows[1] = row
    with pytest.raises(UnverifiedExecution, match='confirmed local non-submission conflict'):
        audit.run(candidates(1, 2), transport)
    assert audit.report()['last_outcome'] == 'conflict'
    assert audit.report()['cursor'] is None and audit.report()['checked_count'] == 0
    transport.rows.clear()
    restored = RetiredOrderAudit(db, clock)
    for roster in (candidates(1, 2), {}):
        with pytest.raises(UnverifiedExecution, match='retained'):
            restored.run(roster, transport, blocked_reason='entry_deadline')
    clock.advance(86400)
    with pytest.raises(UnverifiedExecution, match='retained'):
        restored.check_latched()
    assert transport.calls == [1]


@pytest.mark.parametrize('source', ['active', 'trade', 'foreground'])
def test_foreground_contradiction_is_durable_and_keeps_first_evidence(setup, source):
    db, clock, _, audit = setup
    audit.observe_conflict(8, source)
    first = audit.report()['conflicts']['8']
    clock.advance()
    audit.observe_conflict(8, 'audit')
    assert audit.report()['conflicts']['8'] == first
    with pytest.raises(UnverifiedExecution, match='retained'):
        RetiredOrderAudit(db, clock).check_latched()


def test_invalid_payload_is_failure_not_positive_absence(setup):
    _, _, transport, audit = setup
    transport.rows[1] = []
    with pytest.raises(UnverifiedExecution, match='invalid order lookup response'):
        audit.run(candidates(1), transport)
    assert audit.report()['checked_count'] == 0
    assert audit.report()['last_outcome'] == 'error'


def test_http_attempts_retries_latency_and_foreground_are_separate(setup, monkeypatch):
    _, _, transport, audit = setup
    transport.request_samples = deque(maxlen=1000)
    transport.read_sample_sequence = 0
    ticks = iter((10.0, 10.25, 20.0, 20.6))
    monkeypatch.setattr('live.order_lookup_audit.time.monotonic', lambda: next(ticks))
    def sample_order(coi):
        transport.calls.append(coi)
        samples = ([{'attempt': 1, 'outcome': 'http_error'}, {'attempt': 2, 'outcome': 'success'}]
                   if coi == 1 else [{'attempt': 1, 'outcome': 'auth_error'}])
        transport.request_samples.extend(samples)
        transport.read_sample_sequence += len(samples)
        if coi == 2:
            raise RuntimeError('authentication failed')
    transport.order = sample_order
    audit.run(candidates(1), transport)
    with pytest.raises(RuntimeError):
        audit.lookup(2, transport)
    report = audit.report()
    assert report['http_attempts'] == 2 and report['http_retries'] == 1
    assert report['logical_calls'] == 2
    assert report['background']['http_attempts'] == 2
    assert report['foreground']['http_attempts'] == 0
    assert report['foreground']['logical_calls'] == 1
    assert report['request_seconds']['total'] == pytest.approx(.85)
    assert report['request_seconds']['last'] == pytest.approx(.6)
    assert report['request_seconds']['max'] == pytest.approx(.6)


def test_http_samples_with_wrapped_deque_use_sequence_delta(setup):
    _, _, transport, audit = setup
    transport.request_samples = deque([{'attempt': 1}] * 3, maxlen=3)
    transport.read_sample_sequence = 30
    def order(coi):
        transport.request_samples.append({'attempt': 1, 'outcome': 'success'})
        transport.read_sample_sequence += 1
    transport.order = order
    audit.lookup(1, transport)
    assert audit.report()['http_attempts'] == 1


def test_sample_loss_is_unknown_not_zero(setup):
    _, _, transport, audit = setup
    transport.request_samples = deque(maxlen=1)
    transport.read_sample_sequence = 0
    def order(coi):
        transport.request_samples.append({'attempt': 2, 'outcome': 'success'})
        transport.read_sample_sequence += 2
    transport.order = order
    audit.lookup(1, transport)
    assert audit.report()['http_attempts'] is None
    assert audit.report()['unmeasured_calls'] == 1


def test_logical_call_is_durable_and_unmeasured_before_get(setup):
    db, _, transport, audit = setup
    def order(coi):
        recorded = json.loads(db.execute('SELECT value FROM order_lookup_audit_state').fetchone()[0])
        assert recorded['metrics']['foreground']['logical_calls'] == 1
        assert recorded['metrics']['foreground']['unmeasured_calls'] == 1
        assert recorded['last_lookup']['outcome'] == 'in_progress'
        assert audit.report()['http_attempts'] is None
    transport.order = order
    audit.lookup(1, transport)


def test_clock_rollback_cannot_create_new_allowance(setup):
    _, clock, transport, audit = setup
    audit.run(candidates(1), transport)
    clock.value -= 3600
    report = audit.run(candidates(1), transport)
    assert report['deferred_reason'] == 'clock_regression'
    assert transport.calls == [1]


@pytest.mark.parametrize('bad', [{True: {'classification': 'x'}}, {2**48: {'classification': 'x'}},
                               {1: {'classification': ''}}, {1: None}, [], {1: {}}])
def test_invalid_candidates_fail_closed(setup, bad):
    _, _, transport, audit = setup
    with pytest.raises(UnverifiedExecution, match='candidates'):
        audit.run(bad, transport)
    assert transport.calls == []


@pytest.mark.parametrize('key,value', [('next_due_ms', -1), ('cursor', True), ('passes', -1),
                                    ('pass_ids', [1, 1]), ('candidate_ids', [4]), ('version', True)])
def test_corrupt_checkpoint_fails_closed(setup, key, value):
    db, clock, _, audit = setup
    raw = json.loads(db.execute('SELECT value FROM order_lookup_audit_state').fetchone()[0])
    raw[key] = value
    with db:
        db.execute('UPDATE order_lookup_audit_state SET value=?', (json.dumps(raw),))
    with pytest.raises(UnverifiedExecution, match='checkpoint'):
        RetiredOrderAudit(db, clock)


def test_corrupt_check_evidence_fails_closed(setup):
    db, clock, _, _ = setup
    with db:
        db.execute('INSERT INTO order_lookup_audit_checks VALUES(?,?)', ('9', '{}'))
    with pytest.raises(UnverifiedExecution, match='evidence'):
        RetiredOrderAudit(db, clock)


def test_storage_failure_before_get_never_claims_success(setup):
    db, _, transport, audit = setup
    with db:
        db.execute("CREATE TRIGGER refuse_audit_update BEFORE UPDATE ON order_lookup_audit_state "
                   "BEGIN SELECT RAISE(FAIL,'storage failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        audit.run(candidates(1), transport)
    assert transport.calls == []


def test_conflict_storage_failure_propagates(setup):
    db, _, _, audit = setup
    with db:
        db.execute("CREATE TRIGGER refuse_conflict BEFORE INSERT ON order_lookup_audit_checks "
                   "BEGIN SELECT RAISE(FAIL,'storage failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        audit.observe_conflict(1, 'active')


def test_check_storage_failure_does_not_advance_cursor(setup):
    db, _, transport, audit = setup
    with db:
        db.execute("CREATE TRIGGER refuse_check BEFORE INSERT ON order_lookup_audit_checks "
                   "BEGIN SELECT RAISE(FAIL,'storage failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        audit.run(candidates(1), transport)
    assert audit.report()['cursor'] is None
    assert audit.report()['checked_count'] == 0
    assert transport.calls == [1]

"""Local-only dispatch evidence, restart boundaries and bounded reconciliation."""
from dataclasses import replace
import ssl
from types import SimpleNamespace

import pytest

from test_authenticated_execution import Exchange, buy
from live.authenticated import LighterTransport
from live.execution import ExecutionEngine
from live.execution_evidence import Intent, NotSubmitted, UnverifiedExecution
from live.request_budget import RequestBudget, RequestDeferred


def stop():
    return Intent(100, 0, 2, True, True, '0.0041', 'stop-loss',
                  'immediate-or-cancel', '2462.62', '2475')


def tighter():
    return replace(stop(), price='2472.57', trigger_price='2485')


def deny_budget():
    raise NotSubmitted('request_budget_deferred', retryable=True, cause=RequestDeferred(60.1))


def test_local_budget_denial_is_typed_and_coroutine_never_started(tmp_path):
    t = LighterTransport.__new__(LighterTransport)
    t.budget = RequestBudget(tmp_path / 'budget.sqlite', clock=lambda: 100.)
    ran = []
    async def transaction():
        ran.append(True)
    coro = transaction()
    try:
        for _ in range(55):
            t.budget.check(consume=True)
        with pytest.raises(NotSubmitted) as error:
            t._send(coro)
        assert error.value.reason == 'request_budget_deferred'
        assert error.value.retryable and isinstance(error.value.cause, RequestDeferred)
        assert error.value.retry_after == pytest.approx(60.1)
        assert not ran and coro.cr_frame is None
    finally:
        t.budget.close()


def test_deadline_expiry_before_dispatch_is_typed(monkeypatch):
    t = LighterTransport.__new__(LighterTransport)
    t.budget = SimpleNamespace(check=lambda **kwargs: None)
    monkeypatch.setattr('live.authenticated.time.time', lambda: 2.)
    ran = []
    async def transaction():
        ran.append(True)
    coro = transaction()
    with pytest.raises(NotSubmitted) as error:
        t._send(coro, deadline_ms=1000)
    assert error.value.reason == 'entry_deadline_elapsed' and not error.value.retryable
    assert not ran and coro.cr_frame is None


@pytest.mark.parametrize('failure', [RequestDeferred(3), ssl.SSLError('test TLS error'),
                                   ValueError('test invalid nonce schema')])
def test_nonce_predispatch_failure_preserves_safety_cause(failure):
    t = LighterTransport.__new__(LighterTransport)
    def fail():
        raise failure
    t._next_nonce = fail
    with pytest.raises(NotSubmitted) as error:
        t._submission_nonce()
    assert error.value.cause is failure
    assert error.value.retryable == isinstance(failure, RequestDeferred)


@pytest.mark.parametrize('typed_inside', [False, True])
def test_failure_after_coroutine_starts_is_always_ambiguous(typed_inside, monkeypatch):
    t = LighterTransport.__new__(LighterTransport)
    t.budget = SimpleNamespace(check=lambda **kwargs: None)
    class ImmediateLoop:
        def run_until_complete(self, coro):
            try:
                return coro.send(None)
            except StopIteration as done:
                return done.value
    t.loop = ImmediateLoop()
    monkeypatch.setattr('live.authenticated.asyncio', SimpleNamespace(wait_for=lambda coro, **kwargs: coro))
    ran = []
    async def transaction():
        ran.append(True)
        if typed_inside:
            raise NotSubmitted('must_not_escape_after_dispatch')
        raise TimeoutError('synthetic lost acknowledgement')
    with pytest.raises(RuntimeError, match='requires reconciliation') as error:
        t._send(transaction())
    assert not isinstance(error.value, NotSubmitted) and ran == [True]


def test_known_not_submitted_requires_explicit_retry_and_same_terms(tmp_path):
    x = Exchange()
    engine = ExecutionEngine(x, tmp_path / 'execution', timeout=0)
    original = x.submit
    x.submit = lambda *args: deny_budget()
    try:
        with pytest.raises(NotSubmitted):
            engine.create(stop(), 4, 2, stop_quantity='0.0041')
        assert engine.store.known_not_submitted(2)
        assert engine.store.load(2) == (stop(), None)
        assert engine.store.submission_details(2) == {
            'state': 'not_submitted', 'reason': 'request_budget_deferred', 'retryable': True}
        x.submit = original
        with pytest.raises(NotSubmitted):
            engine.create(stop(), 4, 2, stop_quantity='0.0041')
        with pytest.raises(NotSubmitted):
            engine.recover(2)
        with pytest.raises(UnverifiedExecution, match='different terms'):
            engine.create(tighter(), 4, 2, stop_quantity='0.0041', retry_not_submitted=True)
        assert not x.sent
        assert engine.create(stop(), 4, 2, stop_quantity='0.0041', retry_not_submitted=True)['status'] == 'open'
        assert x.sent == [stop()]
        assert engine.store.submission_state(2) == 'evidenced'
    finally:
        engine.close()


def test_known_not_submitted_survives_restart_and_conflicting_row_blocks(tmp_path):
    x = Exchange()
    engine = ExecutionEngine(x, tmp_path / 'execution', timeout=0)
    original = x.submit
    x.submit = lambda *args: deny_budget()
    with pytest.raises(NotSubmitted):
        engine.create(buy(), 4, 2)
    engine.close()
    engine = ExecutionEngine(x, tmp_path / 'execution', timeout=0)
    try:
        assert engine.store.known_not_submitted(1)
        original(buy(), 41, 251250, 0)  # conflicting external writer / wrong evidence
        with pytest.raises(UnverifiedExecution, match='conflicts'):
            engine.create(buy(), 4, 2, retry_not_submitted=True)
        with pytest.raises(UnverifiedExecution, match='conflicts'):
            engine.recover(1)
        with pytest.raises(UnverifiedExecution, match='conflicts'):
            engine.store.record(1, x.rows[1])
        assert engine.store.known_not_submitted(1) and len(x.sent) == 1
    finally:
        engine.close()


@pytest.mark.parametrize('legacy', [False, True])
def test_reserved_or_legacy_intent_does_not_gain_retry_permission(tmp_path, legacy):
    x = Exchange()
    engine = ExecutionEngine(x, tmp_path / 'execution', timeout=0)
    engine.store.reserve(buy())
    if legacy:
        with engine.store.db:
            engine.store.db.execute('DELETE FROM intent_dispatch')
    engine.close()
    engine = ExecutionEngine(x, tmp_path / 'execution', timeout=0)
    try:
        assert engine.store.submission_state(1) == ('unknown' if legacy else 'reserved')
        assert not engine.store.known_not_submitted(1)
        with pytest.raises(UnverifiedExecution, match='unknown'):
            engine.create(buy(), 4, 2, retry_not_submitted=True)
        assert x.sent == []
    finally:
        engine.close()


def test_crash_after_dispatch_marker_never_retransmits(tmp_path):
    class Crash(BaseException):
        pass
    x = Exchange()
    engine = ExecutionEngine(x, tmp_path / 'execution', timeout=0)
    attempts = []
    def crash(*args):
        attempts.append(True)
        raise Crash()
    x.submit = crash
    with pytest.raises(Crash):
        engine.create(buy(), 4, 2)
    assert engine.store.submission_state(1) == 'dispatching'
    engine.close()
    engine = ExecutionEngine(x, tmp_path / 'execution', timeout=0)
    try:
        with pytest.raises(UnverifiedExecution, match='unknown'):
            engine.create(buy(), 4, 2, retry_not_submitted=True)
        assert attempts == [True]
    finally:
        engine.close()


def test_storage_failure_recording_non_submission_preserves_unknown(tmp_path, monkeypatch):
    x = Exchange()
    engine = ExecutionEngine(x, tmp_path / 'execution', timeout=0)
    x.submit = lambda *args: deny_budget()
    def storage_failed(*args):
        raise OSError('synthetic fsync failure')
    monkeypatch.setattr(engine.store, 'mark_not_submitted', storage_failed)
    try:
        with pytest.raises(OSError):
            engine.create(buy(), 4, 2)
        assert engine.store.submission_state(1) == 'dispatching'
        assert not engine.store.known_not_submitted(1)
    finally:
        engine.close()


def test_absent_order_wait_is_bounded_without_becoming_non_submission(tmp_path):
    x = Exchange()
    stamp = [100.]
    reads = []
    def order(coi):
        reads.append(coi)
        return None
    x.order = order
    x.submit = lambda *args: (_ for _ in ()).throw(RuntimeError('ambiguous dispatch'))
    engine = ExecutionEngine(x, tmp_path / 'execution', timeout=100,
        clock=lambda: stamp[0], sleep=lambda seconds: stamp.__setitem__(0, stamp[0]+seconds))
    try:
        with pytest.raises(UnverifiedExecution, match='unknown'):
            engine.create(buy(), 4, 2)
        assert len(reads) == 9  # one preflight + at most eight reconciliation reads
        assert stamp[0] == pytest.approx(119.5)
        assert engine.store.submission_state(1) == 'dispatching'
        assert not engine.store.known_not_submitted(1)
    finally:
        engine.close()


def test_reconciliation_budget_denial_returns_without_repeat_reads(tmp_path):
    x = Exchange()
    reads = []
    def deny(coi):
        reads.append(coi)
        raise RequestDeferred(60.1)
    engine = ExecutionEngine(x, tmp_path / 'execution', timeout=100)
    engine.store.reserve(buy())
    engine.store.mark_dispatching(1)
    x.order = deny
    try:
        with pytest.raises(RequestDeferred):
            engine.recover(1)
        assert reads == [1]
        assert engine.store.submission_state(1) == 'dispatching'
    finally:
        engine.close()


def test_cancel_known_non_submission_can_be_retried_only_explicitly(tmp_path):
    x = Exchange()
    engine = ExecutionEngine(x, tmp_path / 'execution', timeout=0)
    try:
        engine.create(stop(), 4, 2, stop_quantity='0.0041')
        original = x.cancel
        x.cancel = lambda *args: deny_budget()
        with pytest.raises(NotSubmitted):
            engine.cancel(2)
        assert engine.mutation_submission_details('cancel:2')['state'] == 'not_submitted'
        assert x.rows[2]['status'] == 'open'
        x.cancel = original
        with pytest.raises(NotSubmitted):
            engine.cancel(2)
        assert x.cancel_calls == 0
        engine.cancel(2, retry_not_submitted=True)
        assert x.cancel_calls == 1 and x.rows[2]['status'] == 'canceled'
    finally:
        engine.close()


def test_amend_known_non_submission_keeps_old_stop_and_requires_opt_in(tmp_path):
    x = Exchange()
    engine = ExecutionEngine(x, tmp_path / 'execution', timeout=0)
    try:
        engine.create(stop(), 4, 2, stop_quantity='0.0041')
        original = x.amend
        x.amend = lambda *args: deny_budget()
        with pytest.raises(NotSubmitted):
            engine.amend_stop(2, tighter(), 4, 2)
        assert engine.reconcile_amendments(tolerate_absent=True) == set()
        assert engine.store.load(2)[0] == stop() and x.rows[2]['trigger_price'] == '2475'
        x.amend = original
        with pytest.raises(NotSubmitted):
            engine.amend_stop(2, tighter(), 4, 2)
        engine.amend_stop(2, tighter(), 4, 2, retry_not_submitted=True)
        assert engine.store.load(2)[0] == tighter()
    finally:
        engine.close()


@pytest.mark.parametrize('kind', ['cancel', 'amend'])
def test_known_non_submission_mutation_survives_restart(tmp_path, kind):
    x = Exchange()
    engine = ExecutionEngine(x, tmp_path / 'execution', timeout=0)
    engine.create(stop(), 4, 2, stop_quantity='0.0041')
    original = getattr(x, kind)
    setattr(x, kind, lambda *args: deny_budget())
    def operation(current, **kwargs):
        if kind == 'cancel':
            return current.cancel(2, **kwargs)
        return current.amend_stop(2, tighter(), 4, 2, **kwargs)
    with pytest.raises(NotSubmitted):
        operation(engine)
    engine.close()
    engine = ExecutionEngine(x, tmp_path / 'execution', timeout=0)
    setattr(x, kind, original)
    try:
        with pytest.raises(NotSubmitted):
            operation(engine)
        result = operation(engine, retry_not_submitted=True)
        assert result['status'] == ('canceled' if kind == 'cancel' else 'open')
    finally:
        engine.close()


def test_unknown_amend_is_not_retransmitted_and_absent_can_be_reported(tmp_path):
    x = Exchange()
    engine = ExecutionEngine(x, tmp_path / 'execution', timeout=0)
    calls = []
    def unknown(*args):
        calls.append(True)
        raise RuntimeError('unknown amendment outcome')
    try:
        engine.create(stop(), 4, 2, stop_quantity='0.0041')
        x.amend = unknown
        with pytest.raises(UnverifiedExecution):
            engine.amend_stop(2, tighter(), 4, 2)
        with pytest.raises(UnverifiedExecution):
            engine.amend_stop(2, tighter(), 4, 2, retry_not_submitted=True)
        assert calls == [True]
        assert engine.reconcile_amendments() == {2}
        x.hide_order = True
        assert engine.reconcile_amendments(tolerate_absent=True) == {2}
        with pytest.raises(UnverifiedExecution):
            engine.reconcile_amendments()
        x.hide_order = False
        x.rows[2]['owner_account_index'] = 999
        with pytest.raises(UnverifiedExecution):
            engine.reconcile_amendments(tolerate_absent=True)
    finally:
        engine.close()


def test_amend_row_conflicting_with_non_submission_is_not_accepted(tmp_path):
    x = Exchange()
    engine = ExecutionEngine(x, tmp_path / 'execution', timeout=0)
    try:
        engine.create(stop(), 4, 2, stop_quantity='0.0041')
        original = x.amend
        x.amend = lambda *args: deny_budget()
        with pytest.raises(NotSubmitted):
            engine.amend_stop(2, tighter(), 4, 2)
        original(0, 1002, 41, 247257, 248500)
        with pytest.raises(UnverifiedExecution, match='conflicts'):
            engine.amend_stop(2, tighter(), 4, 2, retry_not_submitted=True)
        assert engine.store.load(2)[0] == stop()
    finally:
        engine.close()

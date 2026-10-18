"""Runner-owned exchange outage gate; never clears an operator pause or HALT.

This module sends no requests/orders. Normal poll + full protection reconciliation
provide recovery evidence. A human-approved deployment opts in via activation.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
import uuid

from data_io import H4
from .authenticated import is_transient_read_error
from .journal import Journal
from .process_lock import ProcessLock, AlreadyRunningError
from .request_budget import RequestDeferred

SUCCESS_CYCLES = 3
STABLE_SECONDS = 120
MAX_CYCLE_GAP_SECONDS = 90
OWNER = 'exchange-recovery-v1'


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name+'.', dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, allow_nan=False)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temp, path)
        if os.name != 'nt':
            directory = os.open(path.parent, os.O_RDONLY)
            try: os.fsync(directory)
            finally: os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


def operator_pause(directory, reason, **details):
    """Explicit operator requests replace prior markers; automatic callers don't."""
    directory = Path(directory)
    with ProcessLock(directory/'entry-controls.lock'):
        atomic(directory/'ENTRY_PAUSED', {'reason':reason, 'at_ms':int(time.time()*1000), **details})


def safety_pause(directory, issues):
    directory = Path(directory)
    with ProcessLock(directory/'entry-controls.lock'):
        if not (directory/'ENTRY_PAUSED').exists():
            atomic(directory/'ENTRY_PAUSED', {'reason':'operational monitor',
                   'at_ms':int(time.time()*1000), 'issues':issues})


class ExchangeRecovery:
    def __init__(self, directory, fingerprint, *, clock=time.time, monotonic=time.monotonic):
        self.directory, self.fingerprint = Path(directory), fingerprint
        self.clock, self.monotonic = clock, monotonic
        self.path = self.directory/'EXCHANGE_PAUSED'
        self.journal = Journal(self.directory)
        self.session = uuid.uuid4().hex
        self.ready = False
        self.storage_error = None
        self.count = 0
        self.first_success = self.last_success = None
        self.last_cycle_ms = None
        self.reason = 'startup_verification'
        self.blockers = []
        # Unknown/corrupt ownership is never adopted as a recoverable outage.
        if self.path.exists():
            prior = json.loads(self.path.read_text())
            if not isinstance(prior,dict) or prior.get('owner') != OWNER or prior.get('fingerprint') != fingerprint:
                raise RuntimeError('exchange pause ownership mismatch; inspect preserved state')
            # Crash between persisting a hard failure and creating the manual
            # latch must never silently turn that failure into a temporary one.
            if (prior.get('reason')=='manual_inspection_required' or prior.get('storage_error')
                    or 'diagnostics' in (prior.get('blockers') or [])):
                safety_pause(self.directory,['previous non-transient recovery failure; inspect before resume'])
        self._save()  # Fail startup before connecting if the durable gate cannot be made.
        self.journal.append('exchange_recovery_started', session=self.session,
                            required_cycles=SUCCESS_CYCLES, stable_seconds=STABLE_SECONDS)

    def snapshot(self):
        return {'owner':OWNER, 'fingerprint':self.fingerprint, 'session':self.session,
                'enabled':True, 'state':'ready' if self.ready else 'paused',
                'phase':'bootstrap_wait' if self.last_cycle_ms is None else 'running',
                'reason':self.reason, 'consecutive_successes':self.count,
                'required_successes':SUCCESS_CYCLES, 'stable_seconds':STABLE_SECONDS,
                'last_cycle_ms':self.last_cycle_ms, 'blockers':self.blockers,
                'storage_error':self.storage_error}

    def _save(self):
        atomic(self.path, {**self.snapshot(), 'updated_ms':int(self.clock()*1000)})

    def _reset(self):
        self.ready = False
        self.count = 0
        self.first_success = self.last_success = None

    def _persist(self):
        try:
            if self.path.exists():
                saved=json.loads(self.path.read_text())
                if (saved.get('owner')!=OWNER or saved.get('session')!=self.session
                        or saved.get('fingerprint')!=self.fingerprint):
                    raise ValueError('recovery marker changed')
            self._save()
        except (OSError,ValueError,AttributeError) as exc:
            self.storage_error = type(exc).__name__
            self._reset()

    def failure(self, exc, stream):
        self._reset()
        temporary = is_transient_read_error(exc) or isinstance(exc, RequestDeferred)
        self.reason = ('request_budget_wait' if isinstance(exc,RequestDeferred) else
                       'temporary_read_failure' if temporary else 'manual_inspection_required')
        self.blockers = [] if temporary else ['unclassified_or_nontransient_failure']
        self._persist()
        try:
            if not temporary:
                safety_pause(self.directory, ['non-transient '+stream+' failure; inspect before resume'])
            self.journal.append('exchange_recovery_failure', stream=stream,
                                automatic_recovery_allowed=temporary,
                                error_type=type(exc).__name__, code=getattr(exc, 'code', None))
        except (OSError,AlreadyRunningError) as error:
            self.storage_error = type(error).__name__
            # The event journal and marker are separate writes. Retain evidence
            # loss in the marker when that storage is still usable, so a process
            # restart cannot reinterpret this as an ordinary temporary outage.
            self._persist()

    def can_enter(self):
        return (self.ready and not self.storage_error and not self.path.exists()
                and not (self.directory/'ENTRY_PAUSED').exists()
                and self.last_success is not None
                and 0 <= self.monotonic()-self.last_success <= MAX_CYCLE_GAP_SECONDS)

    def cycle(self, trader, coverage, *, poll_ok, service_ok, now_ms):
        """Called once after a complete normal poll + service cycle, never by timer."""
        if self.storage_error:
            self._reset(); return
        if not poll_ok or not service_ok:
            self._reset(); self._persist(); return
        problems = []
        for name in ('halted','pending_operation','residual_exits','orphan_orders','data_gaps'):
            if getattr(trader, name, None): problems.append(name)
        if getattr(trader.broker.transport, 'read_diagnostics_error', None): problems.append('diagnostics')
        if trader.broker.last_report.get('execution_changes_verified') is not True:
            problems.append('execution_not_reconciled')
        current = coverage.snapshot()
        gaps=current.get('active_gap_streams') if isinstance(current,dict) else None
        if not isinstance(gaps,list) or gaps: problems.append('active_data_gap')
        # Latest completed signal bar must be processed. Never replay missed entries.
        expected = now_ms//H4*H4-H4
        if trader.last_bar_ms != expected: problems.append('confirmed_bar_not_current')
        if problems:
            self._reset(); self.blockers = problems
            self.reason = ('manual_inspection_required' if 'diagnostics' in problems
                           else 'recovery_checks_pending')
            self._persist()
            return
        self.blockers = []
        current_time = self.monotonic()
        if (self.last_cycle_ms is not None and now_ms <= self.last_cycle_ms):
            return  # Re-reading the same status can never count as a new cycle.
        if self.last_success is not None and not 0 <= current_time-self.last_success <= MAX_CYCLE_GAP_SECONDS:
            self._reset()
        self.last_cycle_ms = now_ms
        self.last_success = current_time
        if self.ready: return
        if self.first_success is None: self.first_success = current_time
        self.count += 1
        self.reason = 'confirming_stable_recovery'
        self._persist()
        if self.storage_error: return
        if self.count < SUCCESS_CYCLES or current_time-self.first_success < STABLE_SECONDS: return
        try:
            # Only this runner owns this file. Never unlink ENTRY_PAUSED.
            saved = json.loads(self.path.read_text())
            if saved.get('session') != self.session or saved.get('fingerprint') != self.fingerprint:
                raise ValueError('recovery marker changed')
            self.journal.append('exchange_recovery_ready', session=self.session,
                consecutive_successes=self.count, stable_seconds=current_time-self.first_success,
                operator_pause_retained=(self.directory/'ENTRY_PAUSED').exists())
            self.path.unlink()
            self.ready = True
            self.reason = 'recovered'
        except (OSError, ValueError) as exc:
            self.storage_error = type(exc).__name__
            self._reset()
            self._persist()

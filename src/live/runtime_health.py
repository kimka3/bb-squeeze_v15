"""Durable observation coverage, separate from the simulated account ledger.

A failed request proves a failed sample, not the exact start or duration of an
exchange outage.  Recovery bounds below deliberately retain that distinction.
Only the runner thread writes this object.  It must record its normal raw error
or bar journal event before calling the corresponding method here.
"""
from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path

from data_io import H4

STREAMS = ('poll', 'bar', 'observation', 'service')
HISTORY_LIMIT = 32
VERSION = 1


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, indent=2, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    if os.name != 'nt':
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _history() -> dict:
    return {'count': 0, 'first': None, 'recent': []}


def _remember(history: dict, record: dict) -> None:
    history['count'] += 1
    if history['first'] is None:
        history['first'] = copy.deepcopy(record)
    history['recent'].append(copy.deepcopy(record))
    del history['recent'][:-HISTORY_LIMIT]


def _stream() -> dict:
    return {'failure_count': 0, 'gap_count': 0, 'recovery_count': 0,
            'first_failure_ms': None, 'last_failure_ms': None,
            'last_success_ms': None, 'active_gap': None,
            'closed_gaps': _history()}


class RuntimeHealth:
    def __init__(self, state_dir: Path, journal, bar_grace_seconds: float,
                 *, now_ms: int | None = None):
        self.directory = Path(state_dir)
        self.path = self.directory / 'runtime_health.json'
        self.journal = journal
        self.journal_path = Path(getattr(journal, 'path', self.directory / 'journal.jsonl'))
        self.grace_ms = float(bar_grace_seconds) * 1000
        now = int(time.time() * 1000) if now_ms is None else int(now_ms)
        loaded = self.path.exists()
        if loaded:
            try:
                self.state = json.loads(self.path.read_text(encoding='utf-8'))
                self._validate()
            except (ValueError, TypeError, KeyError) as exc:
                raise RuntimeError('runtime coverage state is corrupt; preserve and investigate it') from exc
        else:
            self.state = {
                'version': VERSION, 'updated_ms': None, 'journal_offset': 0,
                'campaign_first_start_ms': None, 'last_bar_ms': None,
                'processed_bar_count': 0, 'streams': {name: _stream() for name in STREAMS},
                'late_bars': _history(), 'startup_catchup_bars': _history(),
                'restart_gaps': _history(),
                'reconstruction': {'performed': False, 'legacy_coverage_unknown': False,
                                   'source': 'journal.jsonl', 'legacy_event_count': 0},
            }
        previous_updated = self.state['updated_ms']
        self._replay(legacy=not loaded)
        if not loaded:
            previous_updated = self._recover_legacy_status()
            self.state['reconstruction']['performed'] = True
        if self.state['campaign_first_start_ms'] is None:
            self.state['campaign_first_start_ms'] = now
        if previous_updated is not None and now > previous_updated:
            record = {
                'last_saved_ms': previous_updated, 'session_started_ms': now,
                'unobserved_interval_upper_bound_ms': now - previous_updated,
                'meaning': 'conservative restart observation bound; not exact downtime',
            }
            _remember(self.state['restart_gaps'], record)
            self._append('runtime_restart_gap', record=record)
        self.save(now)

    def _validate(self) -> None:
        state = self.state
        if state['version'] != VERSION:
            raise ValueError('unsupported coverage state version')
        for key in ('journal_offset', 'processed_bar_count'):
            if not isinstance(state[key], int) or state[key] < 0:
                raise ValueError(key)
        for name in STREAMS:
            stream = state['streams'][name]
            for key in ('failure_count', 'gap_count', 'recovery_count'):
                if not isinstance(stream[key], int) or stream[key] < 0:
                    raise ValueError(key)
            self._validate_history(stream['closed_gaps'])
        for key in ('late_bars', 'startup_catchup_bars', 'restart_gaps'):
            self._validate_history(state[key])
        for key in ('updated_ms', 'last_bar_ms', 'campaign_first_start_ms'):
            if state[key] is not None and (not isinstance(state[key], int) or state[key] < 0):
                raise ValueError(key)
        if not isinstance(state['reconstruction'], dict):
            raise ValueError('reconstruction')

    @staticmethod
    def _validate_history(history: dict) -> None:
        if (not isinstance(history['count'], int) or history['count'] < 0
                or not isinstance(history['recent'], list)
                or len(history['recent']) > HISTORY_LIMIT
                or history['count'] < len(history['recent'])
                or (history['count'] > 0 and not isinstance(history['first'], dict))):
            raise ValueError('invalid coverage history')

    def _append(self, kind: str, **payload) -> None:
        self.journal.append(kind, **payload)

    def _replay(self, *, legacy: bool) -> None:
        offset = self.state['journal_offset']
        size = self.journal_path.stat().st_size if self.journal_path.exists() else 0
        if size < offset:
            raise RuntimeError('runtime coverage journal was truncated; preserve and investigate it')
        events = []
        if size:
            try:
                with self.journal_path.open('rb') as handle:
                    handle.seek(offset)
                    for line in handle:
                        if line.strip():
                            event = json.loads(line)
                            if not isinstance(event, dict) or not isinstance(event.get('ts_ms'), int):
                                raise ValueError('journal event has no valid timestamp')
                            events.append(event)
            except (ValueError, UnicodeError) as exc:
                raise RuntimeError('runtime coverage journal is corrupt; preserve and investigate it') from exc
        # A newer explicit pre-decision timestamp is stronger evidence than the
        # old bar event's post-decision completion timestamp.
        timings = {e['record']['bar_ms']: e['record'] for e in events
                   if e.get('kind') == 'runtime_bar_timing'}
        for event in events:
            kind, timestamp = event.get('kind'), event['ts_ms']
            if legacy and not str(kind).startswith('runtime_'):
                self.state['reconstruction']['legacy_event_count'] += 1
            if kind == 'start':
                if self.state['campaign_first_start_ms'] is None:
                    self.state['campaign_first_start_ms'] = timestamp
            elif kind in {name + '_error' for name in STREAMS}:
                self._failure(kind.removesuffix('_error'), timestamp, event.get('error', ''),
                              evidence='legacy_failed_sample' if legacy else 'journal_failed_sample')
            elif kind == 'bar':
                bar_ms = int(event['bar_ms'])
                self._success('bar', timestamp, evidence='journal_bar_completion', exact=False)
                timing = timings.get(bar_ms)
                self._bar(bar_ms, timing['processed_ms'] if timing else timestamp,
                          timing['blocked_candidates'] if timing else self._blocked(event),
                          evidence=timing['timestamp_evidence'] if timing else 'journal_completion_upper_bound')
            elif kind == 'runtime_gap_open':
                stream = event['stream']
                gap = event['gap']
                # The ordinary *_error event normally precedes this event.
                if self.state['streams'][stream]['active_gap'] is None:
                    self._failure(stream, gap['first_failure_ms'], gap['first_error'],
                                  evidence='runtime_failed_sample')
            elif kind == 'runtime_gap_recovery':
                self._success(event['stream'], event['success_ms'],
                              evidence=event['evidence'], exact=event['exact'])
            elif kind == 'runtime_bar_timing':
                record = event['record']
                self._bar(record['bar_ms'], record['processed_ms'], record['blocked_candidates'],
                          evidence=record['timestamp_evidence'])
            elif kind == 'runtime_restart_gap':
                _remember(self.state['restart_gaps'], event['record'])
        if legacy and any(e.get('kind') in {'bar', 'stop', *(name + '_error' for name in STREAMS)}
                          for e in events):
            # Prior versions did not record all successful samples or downtime.
            self.state['reconstruction']['legacy_coverage_unknown'] = True
        self.state['journal_offset'] = size

    @staticmethod
    def _blocked(event: dict) -> int:
        return sum('entries blocked by guard' in str(item) for item in event.get('skipped', []))

    def _recover_legacy_status(self) -> int | None:
        path = self.directory / 'runner_status.json'
        if not path.exists():
            return None
        try:
            old = json.loads(path.read_text(encoding='utf-8'))
            for stream, field in (('poll', 'last_successful_poll_ms'),
                                  ('bar', 'last_bar_success_ms'),
                                  ('observation', 'last_observation_ms')):
                timestamp = old.get(field)
                if timestamp is not None:
                    if not isinstance(timestamp, int) or timestamp < 0:
                        raise ValueError(field)
                    if timestamp > 0:
                        self._success(stream, timestamp, evidence='legacy_latest_status', exact=False)
            updated = old.get('updated_ms')
            if updated is not None and (not isinstance(updated, int) or updated < 0):
                raise ValueError('updated_ms')
            return updated
        except (ValueError, TypeError) as exc:
            raise RuntimeError('legacy runner status is corrupt; preserve and investigate it') from exc

    def _failure(self, stream: str, now_ms: int, error: str, *, evidence: str) -> bool:
        stats = self.state['streams'][stream]
        stats['failure_count'] += 1
        if stats['first_failure_ms'] is None:
            stats['first_failure_ms'] = now_ms
        stats['last_failure_ms'] = now_ms
        opened = stats['active_gap'] is None
        if opened:
            stats['gap_count'] += 1
            stats['active_gap'] = {
                'last_success_before_gap_ms': stats['last_success_ms'],
                'first_failure_ms': now_ms, 'last_failure_ms': now_ms,
                'failed_samples': 0, 'first_error': str(error)[:1000],
                'last_error': str(error)[:1000], 'recovery_ms': None,
                'recovered_by_ms': None, 'recovery_evidence': None,
                'failure_evidence': evidence, 'failure_span_ms': 0,
                'exact_outage_duration_ms': None,
            }
        gap = stats['active_gap']
        gap.update(last_failure_ms=now_ms, last_error=str(error)[:1000],
                   failure_span_ms=now_ms - gap['first_failure_ms'])
        gap['failed_samples'] += 1
        return opened

    def failure(self, stream: str, now_ms: int, error: str) -> None:
        """Call after the runner's ordinary stream_error journal append."""
        opened = self._failure(stream, int(now_ms), error, evidence='runtime_failed_sample')
        if opened:
            self._append('runtime_gap_open', stream=stream,
                         gap=copy.deepcopy(self.state['streams'][stream]['active_gap']))
        self.save(now_ms)

    def _success(self, stream: str, now_ms: int, *, evidence: str, exact: bool) -> bool:
        stats = self.state['streams'][stream]
        stats['last_success_ms'] = max(stats['last_success_ms'] or 0, now_ms)
        gap = stats['active_gap']
        if gap is None or now_ms < gap['last_failure_ms']:
            return False
        gap.update(recovery_ms=now_ms if exact else None, recovered_by_ms=now_ms,
                   recovery_evidence=evidence)
        _remember(stats['closed_gaps'], gap)
        stats['recovery_count'] += 1
        stats['active_gap'] = None
        return True

    def success(self, stream: str, now_ms: int) -> None:
        """Record a successful current sample; never backfill simulated fills."""
        if self._success(stream, int(now_ms), evidence='runtime_first_success', exact=True):
            self._append('runtime_gap_recovery', stream=stream, success_ms=int(now_ms),
                         evidence='runtime_first_success', exact=True)
        self.save(now_ms)

    def _bar(self, bar_ms: int, now_ms: int, blocked_candidates: int, *, evidence: str) -> dict | None:
        if self.state['last_bar_ms'] is not None and bar_ms <= self.state['last_bar_ms']:
            return None
        self.state['last_bar_ms'] = bar_ms
        self.state['processed_bar_count'] += 1
        delay_ms = now_ms - (bar_ms + H4)
        if delay_ms <= self.grace_ms:
            return None
        record = {'bar_ms': bar_ms, 'processed_ms': now_ms, 'delay_ms': delay_ms,
                  'grace_ms': self.grace_ms, 'blocked_candidates': blocked_candidates,
                  'timestamp_evidence': evidence,
                  'blocked_candidates_meaning': 'guard-blocked candidates; not proof latency caused the block'}
        first_start = self.state['campaign_first_start_ms']
        key = 'startup_catchup_bars' if first_start is not None and bar_ms + H4 < first_start else 'late_bars'
        record['classification'] = key
        _remember(self.state[key], record)
        return record

    def bar(self, bar_ms: int, now_ms: int, blocked_candidates: int = 0) -> None:
        """Record the decision-time timestamp used by the existing grace guard."""
        record = self._bar(int(bar_ms), int(now_ms), int(blocked_candidates),
                           evidence='runtime_decision_timestamp')
        if record is not None:
            self._append('runtime_bar_timing', record=record)
        self.save(now_ms)

    def snapshot(self) -> dict:
        result = copy.deepcopy(self.state)
        result.pop('journal_offset', None)
        failures = sum(item['failure_count'] for item in result['streams'].values())
        result['coverage_complete'] = not (
            failures or result['late_bars']['count'] or result['restart_gaps']['count']
            or result['reconstruction']['legacy_coverage_unknown'])
        result['failure_count'] = failures
        result['active_gap_streams'] = [name for name, item in result['streams'].items()
                                        if item['active_gap'] is not None]
        result['meaning'] = ('sampled operational coverage only; exact outage duration and real '
                             'exchange fills are not inferred; account ledger is unchanged')
        return result

    def save(self, now_ms: int) -> None:
        self.state['updated_ms'] = max(self.state['updated_ms'] or 0, int(now_ms))
        self.state['journal_offset'] = self.journal_path.stat().st_size if self.journal_path.exists() else 0
        _atomic_json(self.path, self.state)

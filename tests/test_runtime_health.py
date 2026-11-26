import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from data_io import H4
from live.journal import Journal
from live.runtime_health import HISTORY_LIMIT, RuntimeHealth


def write_events(directory, events):
    (directory / 'journal.jsonl').write_text(
        ''.join(json.dumps(e) + '\n' for e in events), encoding='utf-8')


def failed(health, journal, stream, timestamp, error='unavailable'):
    journal.append(stream + '_error', ts_ms=timestamp, error=error)
    health.failure(stream, timestamp, error)


def test_fresh_session_is_complete_until_a_failed_sample(tmp_path):
    journal = Journal(tmp_path)
    journal.append('start', ts_ms=100)
    health = RuntimeHealth(tmp_path, journal, 60, now_ms=100)
    assert health.snapshot()['coverage_complete']
    health.success('poll', 200)
    failed(health, journal, 'poll', 300, '503')
    failed(health, journal, 'poll', 400, '502')
    health.success('poll', 500)
    snapshot = health.snapshot()
    assert not snapshot['coverage_complete']
    stream = snapshot['streams']['poll']
    assert (stream['failure_count'], stream['gap_count'], stream['recovery_count']) == (2, 1, 1)
    gap = stream['closed_gaps']['first']
    assert gap['last_success_before_gap_ms'] == 200
    assert gap['first_failure_ms'] == 300
    assert gap['last_failure_ms'] == 400
    assert gap['failure_span_ms'] == 100
    assert gap['recovery_ms'] == gap['recovered_by_ms'] == 500
    assert gap['exact_outage_duration_ms'] is None
    # Repeated errors retain their ordinary audit records but no duplicate
    # opening/recovery transition is emitted for the same gap.
    assert len(list(journal.events('runtime_gap_open'))) == 1
    assert len(list(journal.events('runtime_gap_recovery'))) == 1


def test_restart_preserves_errors_and_records_conservative_gap(tmp_path):
    journal = Journal(tmp_path)
    health = RuntimeHealth(tmp_path, journal, 60, now_ms=100)
    health.success('poll', 200)
    failed(health, journal, 'poll', 300)
    health.save(350)
    restarted = RuntimeHealth(tmp_path, journal, 60, now_ms=1000)
    stream = restarted.snapshot()['streams']['poll']
    assert stream['failure_count'] == 1
    assert stream['active_gap']['first_failure_ms'] == 300
    gap = restarted.snapshot()['restart_gaps']['first']
    assert gap['last_saved_ms'] == 350
    assert gap['session_started_ms'] == 1000
    assert gap['unobserved_interval_upper_bound_ms'] == 650
    restarted.success('poll', 1100)
    assert restarted.snapshot()['streams']['poll']['recovery_count'] == 1
    assert not restarted.snapshot()['coverage_complete']


def test_legacy_errors_reconstruct_without_inventing_recovery(tmp_path):
    events = [{'kind': 'start', 'ts_ms': H4 + 5000}]
    events += [{'kind': 'bar', 'ts_ms': H4 + 6000, 'bar_ms': 0, 'skipped': []}]
    for bar in range(1, 5):
        events += [{'kind': 'bar_error', 'ts_ms': (bar + 1) * H4 + 6000, 'error': 'timeline mismatch'},
                   {'kind': 'bar', 'ts_ms': (bar + 1) * H4 + 67000, 'bar_ms': bar * H4,
                    'skipped': []}]
    events += [{'kind': 'poll_error', 'ts_ms': 6 * H4 + n * 60000, 'error': '503'}
               for n in range(15)]
    events += [{'kind': 'observation_error', 'ts_ms': 6 * H4 + 13 * 60000, 'error': '503'}]
    write_events(tmp_path, events)
    (tmp_path / 'runner_status.json').write_text(json.dumps({
        'last_successful_poll_ms': 7 * H4, 'last_observation_ms': 7 * H4 + 100,
        'last_bar_success_ms': 5 * H4 + 67000, 'updated_ms': 7 * H4 + 200,
    }))
    health = RuntimeHealth(tmp_path, Journal(tmp_path), 60, now_ms=7 * H4 + 500)
    snapshot = health.snapshot()
    assert snapshot['streams']['poll']['failure_count'] == 15
    assert snapshot['streams']['bar']['failure_count'] == 4
    assert snapshot['streams']['observation']['failure_count'] == 1
    assert snapshot['late_bars']['count'] == 4
    assert snapshot['late_bars']['first']['timestamp_evidence'] == 'journal_completion_upper_bound'
    # Initial catch-up delay in this fixture is inside the grace period.
    assert snapshot['startup_catchup_bars']['count'] == 0
    gap = snapshot['streams']['poll']['closed_gaps']['first']
    assert gap['first_failure_ms'] == 6 * H4
    assert gap['failure_span_ms'] == 14 * 60000
    assert gap['last_success_before_gap_ms'] is None
    assert gap['recovery_ms'] is None
    assert gap['recovered_by_ms'] == 7 * H4
    assert gap['recovery_evidence'] == 'legacy_latest_status'
    assert not snapshot['coverage_complete']


def test_legacy_no_recovery_evidence_keeps_gap_open(tmp_path):
    write_events(tmp_path, [{'kind': 'poll_error', 'ts_ms': 1000, 'error': '503'}])
    (tmp_path / 'runner_status.json').write_text(json.dumps({
        'last_successful_poll_ms': 500, 'updated_ms': 1000}))
    health = RuntimeHealth(tmp_path, Journal(tmp_path), 60, now_ms=1100)
    stream = health.snapshot()['streams']['poll']
    assert stream['active_gap']['recovery_ms'] is None
    assert stream['active_gap']['recovered_by_ms'] is None
    assert stream['recovery_count'] == 0


def test_late_decision_deduplicates_and_startup_is_separate(tmp_path):
    journal = Journal(tmp_path)
    journal.append('start', ts_ms=H4 + 100000)
    health = RuntimeHealth(tmp_path, journal, 60, now_ms=H4 + 100000)
    health.bar(0, H4 + 100100)
    health.bar(H4, 2 * H4 + 67000, blocked_candidates=2)
    health.bar(H4, 2 * H4 + 150000, blocked_candidates=7)
    health.bar(2 * H4, 3 * H4 + 59000)
    snapshot = health.snapshot()
    assert snapshot['processed_bar_count'] == 3
    assert snapshot['startup_catchup_bars']['count'] == 1
    assert snapshot['late_bars']['count'] == 1
    assert snapshot['late_bars']['first']['blocked_candidates'] == 2
    assert snapshot['late_bars']['first']['delay_ms'] == 67000
    assert snapshot['late_bars']['first']['timestamp_evidence'] == 'runtime_decision_timestamp'


def test_snapshot_copy_cannot_change_persisted_counters(tmp_path):
    journal = Journal(tmp_path)
    health = RuntimeHealth(tmp_path, journal, 60, now_ms=100)
    failed(health, journal, 'service', 200)
    snapshot = health.snapshot()
    snapshot['streams']['service']['failure_count'] = 0
    assert health.snapshot()['streams']['service']['failure_count'] == 1


def test_history_is_bounded_but_counts_and_first_evidence_survive(tmp_path):
    journal = Journal(tmp_path)
    health = RuntimeHealth(tmp_path, journal, 60, now_ms=100)
    for n in range(HISTORY_LIMIT + 5):
        failed(health, journal, 'observation', 200 + 2 * n)
        health.success('observation', 201 + 2 * n)
    stream = health.snapshot()['streams']['observation']
    assert stream['failure_count'] == HISTORY_LIMIT + 5
    assert stream['closed_gaps']['count'] == HISTORY_LIMIT + 5
    assert len(stream['closed_gaps']['recent']) == HISTORY_LIMIT
    assert stream['closed_gaps']['first']['first_failure_ms'] == 200


def test_raw_error_after_last_snapshot_is_replayed_exactly_once(tmp_path):
    journal = Journal(tmp_path)
    health = RuntimeHealth(tmp_path, journal, 60, now_ms=100)
    failed(health, journal, 'poll', 200)
    # Simulate death after the ordinary journal append, before health.failure.
    journal.append('poll_error', ts_ms=300, error='502')
    restarted = RuntimeHealth(tmp_path, journal, 60, now_ms=400)
    assert restarted.snapshot()['streams']['poll']['failure_count'] == 2
    again = RuntimeHealth(tmp_path, journal, 60, now_ms=500)
    assert again.snapshot()['streams']['poll']['failure_count'] == 2


def test_recovery_transition_after_last_snapshot_is_replayed(tmp_path, monkeypatch):
    journal = Journal(tmp_path)
    health = RuntimeHealth(tmp_path, journal, 60, now_ms=100)
    failed(health, journal, 'poll', 200)
    def interrupted(_):
        raise OSError('simulated process death before snapshot')
    monkeypatch.setattr(health, 'save', interrupted)
    with pytest.raises(OSError):
        health.success('poll', 300)
    restarted = RuntimeHealth(tmp_path, journal, 60, now_ms=400)
    stream = restarted.snapshot()['streams']['poll']
    assert stream['failure_count'] == 1
    assert stream['active_gap'] is None
    assert stream['closed_gaps']['first']['recovery_ms'] == 300


def test_open_transition_does_not_duplicate_raw_failure_with_different_timestamp(tmp_path, monkeypatch):
    journal = Journal(tmp_path)
    health = RuntimeHealth(tmp_path, journal, 60, now_ms=100)
    journal.append('poll_error', ts_ms=200, error='503')
    def interrupted(_):
        raise OSError('simulated death before snapshot')
    monkeypatch.setattr(health, 'save', interrupted)
    with pytest.raises(OSError):
        health.failure('poll', 201, '503')
    restarted = RuntimeHealth(tmp_path, journal, 60, now_ms=300)
    assert restarted.snapshot()['streams']['poll']['failure_count'] == 1
    assert restarted.snapshot()['streams']['poll']['gap_count'] == 1


@pytest.mark.parametrize('bad_file', ['runtime_health.json', 'journal.jsonl'])
def test_corruption_is_preserved_and_fails_closed(tmp_path, bad_file):
    path = tmp_path / bad_file
    path.write_text('{bad json\n')
    with pytest.raises(RuntimeError, match='corrupt'):
        RuntimeHealth(tmp_path, Journal(tmp_path), 60, now_ms=100)
    assert path.read_text() == '{bad json\n'


def test_truncated_journal_cannot_silently_reset_coverage(tmp_path):
    journal = Journal(tmp_path)
    health = RuntimeHealth(tmp_path, journal, 60, now_ms=100)
    failed(health, journal, 'bar', 200)
    journal.path.write_text('')
    with pytest.raises(RuntimeError, match='truncated'):
        RuntimeHealth(tmp_path, journal, 60, now_ms=300)

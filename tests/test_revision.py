"""An operational repair must preserve the experiment and reject silent drift."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from live import revision as r
from live import supervisor as s
from live.process_lock import AlreadyRunningError, ProcessLock


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'repo'
        self.root.mkdir()
        self.campaign = Path(self.temp.name) / 'campaign'
        self.state = self.campaign / 'state'
        self.state.mkdir(parents=True)
        for path in (self.campaign / 'supervisor.lock', self.state / 'runner.lock'):
            path.write_bytes(b'\0')
        self.frozen = {'mode': 'paper', 'state_dir': str(self.state),
                       'universe': ['BTCUSDT'], 'strategy': {'risk': .02}}
        s.atomic_json(self.campaign / 'config.frozen.json', self.frozen)
        s.atomic_json(self.root / 'paper.config.json', self.frozen)
        description = dict(self.frozen)
        description.pop('state_dir')
        s.atomic_json(self.state / 'identity.json', {
            'mode': 'paper',
            'config_sha256': hashlib.sha256(json.dumps(description, sort_keys=True).encode()).hexdigest(),
            'config': description,
        })
        (self.state / 'journal.jsonl').write_bytes(b'{"kind":"paper_fill","qty":1}\n')
        s.atomic_json(self.state / 'paper.json', {'cash': 1234, 'positions': {'BTCUSDT': {'qty': 1}}})
        s.atomic_json(self.state / 'snapshot.json', {'last_bar_ms': 1700000000000})
        s.atomic_json(self.campaign / 'status.json', {
            'phase': 'paused', 'forced_stop': False, 'child_pid': None,
            'restarts': 3, 'updated_at': s.utc_text(1700000100),
        })
        self.old_execution = {
            'source_sha256': {'src/live/run.py': 'old', 'src/frontier_engine.py': 'fixed'},
            'python_version': '3.12.3', 'packages': {'numpy': '2.3.5'},
        }
        self.new_execution = {
            **self.old_execution,
            'source_sha256': {**self.old_execution['source_sha256'], 'src/live/run.py': 'new',
                              'src/live/revision.py': 'repair-tool'},
        }
        self.manifest = {
            'schema_version': 1, 'mode': 'paper', 'campaign_id': 'fixed-campaign',
            'campaign_dir': str(self.campaign), 'started_at': s.utc_text(1700000000),
            'deadline': s.utc_text(1700000000 + s.CAMPAIGN_SECONDS),
            'duration_seconds': s.CAMPAIGN_SECONDS,
            'config_sha256': s.digest(self.campaign / 'config.frozen.json'),
            'git_revision': 'a' * 40, 'execution_manifest': self.old_execution,
            'restart_policy': {'max_restarts': 12},
        }
        s.atomic_json(self.campaign / 'campaign.json', self.manifest)
        self.before = (self.campaign / 'campaign.json').read_bytes()
        self.state_before = r.state_hashes(self.state)
        self.kwargs = dict(expected_campaign_id='fixed-campaign',
                           expected_manifest_sha256=s.digest(self.campaign / 'campaign.json'),
                           expected_old_git='a' * 40, expected_new_git='b' * 40,
                           reason='Repair confirmed candle scheduling', root=self.root,
                           now=1700000200)
        self.config_patch = patch.object(s, 'frozen_config', return_value=self.frozen)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.execution_patch = patch.object(s, 'execution_manifest', return_value=self.new_execution)
        self.execution_patch.start()
        self.addCleanup(self.execution_patch.stop)
        self.checkout_patch = patch.object(r, 'verify_checkout')
        self.checkout_patch.start()
        self.addCleanup(self.checkout_patch.stop)

    def test_repair_retains_ledger_period_identity_and_audits_source(self):
        result = r.revise(self.campaign, **self.kwargs)
        actual = s.read_json(self.campaign / 'campaign.json')
        expected = {**self.manifest, 'execution_manifest': self.new_execution, 'git_revision': 'b' * 40}
        self.assertEqual(actual, expected)
        self.assertEqual(r.state_hashes(self.state), self.state_before)
        audit = Path(result['audit_directory'])
        self.assertEqual((audit / 'campaign.before.json').read_bytes(), self.before)
        self.assertEqual((audit / 'campaign.after.json').read_bytes(),
                         (self.campaign / 'campaign.json').read_bytes())
        record = s.read_json(audit / 'prepared.json')
        self.assertEqual(record['state_file_sha256'], self.state_before)
        self.assertEqual(set(record['changed_paths']), {'src/live/run.py', 'src/live/revision.py'})
        self.assertEqual(s.read_json(audit / 'committed.json')['manifest_sha256'],
                         s.digest(self.campaign / 'campaign.json'))

    def test_wrong_expected_digest_does_not_publish_a_revision(self):
        self.kwargs['expected_manifest_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'inspected digest'):
            r.revise(self.campaign, **self.kwargs)
        self.assertFalse((self.campaign / 'revisions').exists())
        self.assertEqual((self.campaign / 'campaign.json').read_bytes(), self.before)

    def test_revised_manifest_resumes_but_further_code_drift_still_fails(self):
        r.revise(self.campaign, **self.kwargs)
        approved = s.initialize_campaign(self.campaign, self.root / 'paper.config.json')
        self.assertEqual(approved['execution_manifest'], self.new_execution)
        with patch.object(s, 'execution_manifest', return_value=self.old_execution), \
                self.assertRaisesRegex(ValueError, 'versions changed'):
            s.initialize_campaign(self.campaign, self.root / 'paper.config.json')

    def test_wrong_campaign_or_old_commit_refused(self):
        for field, value, message in (
                ('expected_campaign_id', 'another-campaign', 'identity'),
                ('expected_old_git', 'c' * 40, 'old commit')):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, message):
                r.revise(self.campaign, **{**self.kwargs, field: value})

    def test_active_or_forced_paused_worker_refused(self):
        for status in ({'phase': 'running', 'forced_stop': False},
                       {'phase': 'paused', 'forced_stop': True},
                       {'phase': 'completed', 'forced_stop': False}):
            s.atomic_json(self.campaign / 'status.json', status)
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, 'gracefully paused'):
                r.revise(self.campaign, **self.kwargs)

    def test_live_worker_birth_or_terminal_stop_refused(self):
        with patch.object(s, 'previous_child_is_alive', return_value=True), \
                self.assertRaisesRegex(ValueError, 'gracefully paused'):
            r.revise(self.campaign, **self.kwargs)
        (self.campaign / 'STOP').write_text('requested')
        with self.assertRaisesRegex(ValueError, 'gracefully paused'):
            r.revise(self.campaign, **self.kwargs)

    def test_both_writer_locks_are_required(self):
        for path in (self.campaign / 'supervisor.lock', self.state / 'runner.lock'):
            with self.subTest(path=path), ProcessLock(path), self.assertRaises(AlreadyRunningError):
                r.revise(self.campaign, **self.kwargs)

    def test_period_and_mode_changes_refused(self):
        for changes, message in (
                ({'deadline': s.utc_text(1700000000 + s.CAMPAIGN_SECONDS + 1)}, 'period'),
                ({'duration_seconds': 1}, 'period'), ({'mode': 'live'}, 'paper')):
            s.atomic_json(self.campaign / 'campaign.json', {**self.manifest, **changes})
            kwargs = {**self.kwargs, 'expected_manifest_sha256': s.digest(self.campaign / 'campaign.json')}
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, message):
                r.revise(self.campaign, **kwargs)

    def test_expired_campaign_cannot_be_reopened(self):
        with self.assertRaisesRegex(ValueError, 'period'):
            r.revise(self.campaign, **{**self.kwargs, 'now': 1700000000 + s.CAMPAIGN_SECONDS})

    def test_frozen_configuration_and_resolved_configuration_drift_refused(self):
        with patch.object(s, 'frozen_config', return_value={**self.frozen, 'paper_equity': 200000}), \
                self.assertRaisesRegex(ValueError, 'resolved configuration'):
            r.revise(self.campaign, **self.kwargs)
        s.atomic_json(self.campaign / 'config.frozen.json', {**self.frozen, 'paper_equity': 200000})
        with self.assertRaisesRegex(ValueError, 'frozen configuration changed'):
            r.revise(self.campaign, **self.kwargs)

    def test_state_identity_drift_refused(self):
        s.atomic_json(self.state / 'identity.json', {'mode': 'paper', 'config_sha256': 'wrong'})
        with self.assertRaisesRegex(ValueError, 'state configuration identity'):
            r.revise(self.campaign, **self.kwargs)

    def test_strategy_and_dependency_changes_refused(self):
        for execution, message in (
                ({**self.new_execution, 'packages': {'numpy': 'changed'}}, 'dependency'),
                ({**self.new_execution, 'source_sha256': {
                    **self.new_execution['source_sha256'], 'src/frontier_engine.py': 'changed'}}, 'allowlist')):
            with self.subTest(message=message), patch.object(s, 'execution_manifest', return_value=execution), \
                    self.assertRaisesRegex(ValueError, message):
                r.revise(self.campaign, **self.kwargs)

    def test_external_ledger_change_during_preparation_prevents_publication(self):
        original_write = r._write_new

        def corrupt(path, data):
            original_write(path, data)
            if path.name == 'prepared.json':
                (self.state / 'journal.jsonl').write_text('concurrent writer\n')

        with patch.object(r, '_write_new', side_effect=corrupt), \
                self.assertRaisesRegex(ValueError, 'trading state changed'):
            r.revise(self.campaign, **self.kwargs)
        self.assertEqual((self.campaign / 'campaign.json').read_bytes(), self.before)

    def test_rollback_is_a_second_audit_without_rewinding_ledger(self):
        first = r.revise(self.campaign, **self.kwargs)
        with (self.state / 'journal.jsonl').open('a') as handle:
            handle.write('{"kind":"later_fill","qty":2}\n')
        later_state = r.state_hashes(self.state)
        kwargs = {**self.kwargs, 'expected_manifest_sha256': s.digest(self.campaign / 'campaign.json'),
                  'expected_old_git': 'b' * 40, 'expected_new_git': 'a' * 40,
                  'operation': 'rollback', 'reason': 'Validated regression; restore previous runtime'}
        with patch.object(s, 'execution_manifest', return_value=self.old_execution):
            second = r.revise(self.campaign, **kwargs)
        self.assertNotEqual(first['revision_id'], second['revision_id'])
        self.assertEqual(r.state_hashes(self.state), later_state)
        self.assertEqual(s.read_json(self.campaign / 'campaign.json'), self.manifest)
        self.assertEqual(s.read_json(Path(second['audit_directory']) / 'prepared.json')['operation'], 'rollback')


class CheckoutTests(unittest.TestCase):
    def test_wrong_head_dirty_checkout_and_untracked_source_refused(self):
        expected = 'b' * 40
        manifest = {'source_sha256': {'src/live/run.py': 'hash'}}
        for outputs, message in (
                (['a' * 40], 'HEAD'), ([expected, ' M src/live/run.py'], 'clean'),
                ([expected, '', 'other.py'], 'untracked')):
            calls = [subprocess.CompletedProcess([], 0, out, '') for out in outputs]
            with self.subTest(message=message), patch.object(r.subprocess, 'run', side_effect=calls), \
                    self.assertRaisesRegex(ValueError, message):
                r.verify_checkout(Path('.').resolve(), expected, manifest)


if __name__ == '__main__':
    unittest.main()

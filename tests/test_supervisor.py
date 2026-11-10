"""Campaign deadline, bounded recovery, single writer and key-free launch tests."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from live import supervisor as s
from live.process_lock import AlreadyRunningError, ProcessLock


class ProcessLockTests(unittest.TestCase):
    def test_duplicate_writer_rejected_and_release_reusable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'runner.lock'
            with ProcessLock(path):
                with self.assertRaises(AlreadyRunningError):
                    with ProcessLock(path):
                        self.fail('duplicate entered')
            self.assertTrue(path.exists())
            with ProcessLock(path):
                pass

    def test_process_crash_releases_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'runner.lock'
            code = ('import os,sys;sys.path.insert(0,sys.argv[1]);'
                    'from live.process_lock import ProcessLock;'
                    'lock=ProcessLock(sys.argv[2]).acquire();'
                    'print("READY",flush=True);sys.stdin.readline();os._exit(23)')
            child = subprocess.Popen([sys.executable, '-c', code, str(s.ROOT / 'src'), str(path)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), 'READY')
                with self.assertRaises(AlreadyRunningError):
                    ProcessLock(path).acquire()
                # Crash the actual interpreter, including when sys.executable
                # is Windows' venv launcher. Killing only the launcher can leave
                # the interpreter alive and would not test OS lock release.
                child.communicate(input='crash\n', timeout=10)
                self.assertEqual(child.returncode, 23)
                with ProcessLock(path):
                    pass
            finally:
                if child.poll() is None:
                    child.communicate(input='crash\n', timeout=10)


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.campaign = Path(self.temp.name) / 'campaign'
        self.config = Path(self.temp.name) / 'paper.json'
        self.config.write_text('{"mode":"paper"}', encoding='utf-8')
        self.manifest_patch = patch.object(s, 'execution_manifest', return_value={'version': 'test'})
        self.manifest_patch.start()
        self.addCleanup(self.manifest_patch.stop)
        self.config_patch = patch.object(s, 'frozen_config', side_effect=lambda path, state: {
            'mode': 'paper', 'state_dir': str(state), 'account_index': None})
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)

    def test_restart_keeps_original_28_day_deadline(self):
        initial = s.initialize_campaign(self.campaign, self.config, now=1_700_000_000)
        resumed = s.initialize_campaign(self.campaign, self.config, now=1_700_086_400)
        self.assertEqual(resumed, initial)
        self.assertEqual(s.parse_utc(initial['deadline']) - s.parse_utc(initial['started_at']),
                         28 * 24 * 60 * 60)

    def test_real_resolved_config_resumes_and_deep_change_is_rejected(self):
        self.config_patch.stop()
        initial = s.initialize_campaign(self.campaign, self.config, now=1_700_000_000)
        resolved = s.read_json(self.campaign / 'config.frozen.json')
        self.assertIsInstance(resolved['universe'], list)
        self.assertEqual(resolved['state_dir'], str(self.campaign.resolve() / 'state'))
        self.assertEqual(s.initialize_campaign(self.campaign, self.config, now=1_700_086_400), initial)
        self.config.write_text('{"mode":"paper","strategy":{"risk":0.01}}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'differs from frozen'):
            s.initialize_campaign(self.campaign, self.config)

    def test_launch_scripts_are_frozen_in_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'src').mkdir()
            (root / 'src' / 'entry.py').write_text('pass\n', encoding='utf-8')
            (root / 'scripts').mkdir()
            (root / 'scripts' / 'entry.sh').write_text('#!/bin/bash\n', encoding='utf-8')
            (root / 'scripts' / 'paper.service').write_text('[Service]\n', encoding='utf-8')
            self.manifest_patch.stop()
            initial = s.execution_manifest(root)
            self.assertIn('scripts/entry.sh', initial['source_sha256'])
            self.assertIn('scripts/paper.service', initial['source_sha256'])
            (root / 'scripts' / 'entry.sh').write_text('#!/bin/bash\nexit 1\n', encoding='utf-8')
            self.assertNotEqual(s.execution_manifest(root), initial)

    def test_frozen_config_edit_and_code_drift_refuse_resume(self):
        s.initialize_campaign(self.campaign, self.config, now=1_700_000_000)
        with patch.object(s, 'execution_manifest', return_value={'version': 'changed'}):
            with self.assertRaisesRegex(ValueError, 'versions changed'):
                s.initialize_campaign(self.campaign, self.config)
        (self.campaign / 'config.frozen.json').write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'config changed'):
            s.initialize_campaign(self.campaign, self.config)

    def test_tampered_deadline_and_old_state_refused(self):
        initial = s.initialize_campaign(self.campaign, self.config, now=1_700_000_000)
        initial['deadline'] = s.utc_text(s.parse_utc(initial['deadline']) + 1)
        s.atomic_json(self.campaign / 'campaign.json', initial)
        with self.assertRaisesRegex(ValueError, 'duration'):
            s.initialize_campaign(self.campaign, self.config)
        other = Path(self.temp.name) / 'old'
        (other / 'state').mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'existing state'):
            s.initialize_campaign(other, self.config)

    def test_no_child_when_deadline_expired(self):
        initial = s.initialize_campaign(self.campaign, self.config, now=1_700_000_000)
        with patch.object(s.time, 'time', return_value=s.parse_utc(initial['deadline']) + 1), \
             patch.object(s.subprocess, 'Popen') as popen:
            self.assertEqual(s.supervise(self.campaign, self.config), 0)
            popen.assert_not_called()
        self.assertEqual(s.read_json(self.campaign / 'status.json')['phase'], 'completed')

    def test_durable_stop_prevents_any_new_child(self):
        s.initialize_campaign(self.campaign, self.config, now=1_700_000_000)
        s.atomic_json(self.campaign / 'STOP', {'requested_at': s.utc_text(1_700_000_000)})
        with patch.object(s.time, 'time', return_value=1_700_000_001), \
             patch.object(s.subprocess, 'Popen') as popen:
            self.assertEqual(s.supervise(self.campaign, self.config), 0)
            popen.assert_not_called()
        self.assertEqual(s.read_json(self.campaign / 'status.json')['phase'], 'stopped')

    def test_deadline_stops_running_child_and_records_forced_exit(self):
        initial = s.initialize_campaign(self.campaign, self.config, now=1_700_000_000)
        clock = [s.parse_utc(initial['deadline']) - 2]

        class RunningChild:
            pid = 87654321
            done = False

            def poll(self):
                return 0 if self.done else None

        worker = RunningChild()

        def forced_stop(child):
            child.done = True
            return True

        with patch.object(s.time, 'time', side_effect=lambda: clock[0]), \
             patch.object(s.time, 'sleep', side_effect=lambda n: clock.__setitem__(0, clock[0] + n)), \
             patch.object(s.subprocess, 'Popen', return_value=worker) as popen, \
             patch.object(s, 'stop_child', side_effect=forced_stop) as stop:
            self.assertEqual(s.supervise(self.campaign, self.config), 0)
        self.assertEqual(popen.call_count, 1)
        stop.assert_called_once_with(worker)
        status = s.read_json(self.campaign / 'status.json')
        self.assertEqual(status['phase'], 'completed')
        self.assertTrue(status['forced_stop'])
        self.assertIsNone(status['child_pid'])
        self.assertEqual(s.read_json(self.campaign / 'campaign.json'), initial)

    def test_spawn_failure_is_terminal_and_does_not_restart_forever(self):
        s.initialize_campaign(self.campaign, self.config, now=1_700_000_000)
        with patch.object(s.time, 'time', return_value=1_700_000_001), \
             patch.object(s.subprocess, 'Popen', side_effect=OSError('unit-test spawn failure')) as popen:
            self.assertEqual(s.supervise(self.campaign, self.config), s.EXIT_CONFIG)
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(s.read_json(self.campaign / 'status.json')['phase'], 'failed')

    def test_early_zero_exit_uses_finite_restart_budget(self):
        s.initialize_campaign(self.campaign, self.config, now=1_700_000_000)
        clock = [1_700_000_001.]

        class ExitedChild:
            pid = 87654321
            returncode = 0

            def poll(self):
                return 0

        with patch.object(s.time, 'time', side_effect=lambda: clock[0]), \
             patch.object(s.time, 'sleep', side_effect=lambda n: clock.__setitem__(0, clock[0] + n)), \
             patch.object(s.subprocess, 'Popen', return_value=ExitedChild()) as popen:
            self.assertEqual(s.supervise(self.campaign, self.config), s.EXIT_CONFIG)
        status = s.read_json(self.campaign / 'status.json')
        self.assertEqual(status['phase'], 'failed')
        self.assertEqual(status['restarts'], s.MAX_RESTARTS)
        self.assertEqual(popen.call_count, s.MAX_RESTARTS + 1)
        self.assertEqual(status['last_exit_code'], 0)

    def test_restart_budget_survives_supervisor_restart(self):
        s.initialize_campaign(self.campaign, self.config, now=1_700_000_000)
        s.atomic_json(self.campaign / 'status.json', {'phase': 'paused', 'restarts': s.MAX_RESTARTS})

        class FailedChild:
            pid = 87654321
            returncode = 1

            def poll(self):
                return 1

        with patch.object(s.time, 'time', return_value=1_700_000_001), \
             patch.object(s.time, 'sleep'), \
             patch.object(s.subprocess, 'Popen', return_value=FailedChild()) as popen:
            self.assertEqual(s.supervise(self.campaign, self.config), s.EXIT_CONFIG)
        self.assertEqual(popen.call_count, 1)

    def test_existing_verified_child_prevents_duplicate(self):
        s.initialize_campaign(self.campaign, self.config, now=1_700_000_000)
        s.atomic_json(self.campaign / 'status.json', {'phase': 'running', 'child_pid': os.getpid(),
                                                    'child_birth': s.process_birth(os.getpid())})
        self.assertIsNotNone(s.process_birth(os.getpid()))
        with patch.object(s.subprocess, 'Popen') as popen:
            with self.assertRaises(AlreadyRunningError):
                s.supervise(self.campaign, self.config)
            popen.assert_not_called()

    def test_paper_only_command_and_private_key_not_inherited(self):
        initial = s.initialize_campaign(self.campaign, self.config, now=1_700_000_000)
        command = s.child_command(self.campaign, initial)
        self.assertEqual(command[command.index('--mode') + 1], 'paper')
        self.assertEqual(command[command.index('--until') + 1], initial['deadline'])
        self.assertNotIn('--i-understand-this-places-real-orders', command)
        with patch.dict(os.environ, {'LIGHTER_API_PRIVATE_KEY': 'fake-unit-test-key'}):
            env = s.child_environment()
        self.assertNotIn('LIGHTER_API_PRIVATE_KEY', env)
        self.assertEqual(env['DRY_RUN'], 'true')

    def test_status_is_read_only_and_no_campaign_is_created(self):
        self.assertEqual(s.campaign_status(self.campaign)['phase'], 'not_initialized')
        self.assertFalse(self.campaign.exists())

    def test_no_secrets_nested_and_explicit_utc_required(self):
        with self.assertRaises(ValueError):
            s.assert_no_secrets({'execution': {'private_key': 'do-not-save'}})
        with self.assertRaises(ValueError):
            s.parse_utc('2026-09-12T00:00:00')
        with self.assertRaises(ValueError):
            s.parse_utc('2026-09-12T00:00:00+09:00')

    def test_atomic_json_rejects_nan_without_destroying_existing_status(self):
        path = Path(self.temp.name) / 'status.json'
        s.atomic_json(path, {'phase': 'running'})
        with self.assertRaises(ValueError):
            s.atomic_json(path, {'bad': float('nan')})
        self.assertEqual(s.read_json(path), {'phase': 'running'})
        self.assertEqual(list(path.parent.glob('*.tmp')), [])

    def test_backoff_is_bounded(self):
        self.assertEqual([s.restart_delay(n) for n in range(1, 7)], [30, 60, 120, 240, 300, 300])


if __name__ == '__main__':
    unittest.main()

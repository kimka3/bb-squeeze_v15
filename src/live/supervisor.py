"""Fixed 28-calendar-day, key-free paper campaign supervisor (stdlib control plane).

Preflight separately BEFORE start; the first successful initialization fixes UTC
start/deadline forever. Downtime is not credited as observed trading time.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from live.process_lock import AlreadyRunningError, ProcessLock  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
UTC = dt.timezone.utc
CAMPAIGN_SECONDS = 28 * 24 * 60 * 60
MAX_RESTARTS = 12
POLL_SECONDS = 5
EXIT_CONFIG = 78
TERMINAL = {'completed', 'stopped', 'failed'}


def utc_text(seconds: float) -> str:
    return dt.datetime.fromtimestamp(seconds, UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def parse_utc(value: str) -> float:
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise ValueError('campaign timestamps must explicitly use UTC')
    return parsed.timestamp()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def atomic_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f'.{os.getpid()}.{uuid.uuid4().hex}.tmp')
    try:
        with temporary.open('x', encoding='utf-8', newline='\n') as fh:
            json.dump(data, fh, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            fh.write('\n')
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def execution_manifest(root: Path = ROOT) -> dict:
    sources = sorted((root / 'src').rglob('*.py'))
    sources += sorted((root / 'scripts').glob('*.sh'))
    sources += sorted((root / 'scripts').glob('*.service'))
    sources += [p for p in (root / 'requirements.txt', root / 'requirements-paper.lock') if p.exists()]
    packages = {}
    for package in ('numpy', 'pandas', 'python-dateutil', 'pytz', 'tzdata', 'six'):
        try:
            packages[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            packages[package] = 'missing'
    return {
        'source_sha256': {str(p.relative_to(root)).replace('\\', '/'): digest(p) for p in sources},
        'python_version': sys.version.split()[0],
        'packages': packages,
    }


def git_revision(root: Path = ROOT) -> str | None:
    try:
        result = subprocess.run(['git', '-c', f'safe.directory={root.resolve().as_posix()}',
                                 'rev-parse', 'HEAD'], cwd=root,
                                capture_output=True, text=True, timeout=10, check=True)
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def assert_no_secrets(value):
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = key.lower()
            if any(word in lowered for word in ('private_key', 'password', 'secret', 'token')):
                raise ValueError('config must not contain credential fields; paper needs no keys')
            assert_no_secrets(item)
    elif isinstance(value, list):
        for item in value:
            assert_no_secrets(item)


def frozen_config(config_path: Path, state_dir: Path) -> dict:
    raw = read_json(config_path)
    assert_no_secrets(raw)
    if raw.get('mode', 'paper') == 'live':
        raise ValueError('a live config cannot initialize a paper campaign')
    from live.config import LiveConfig
    config = LiveConfig.load(config_path, mode='paper', state_dir=state_dir)
    data = config.describe()
    data['mode'] = 'paper'
    data['account_index'] = None
    data['api_key_index'] = 0
    assert_no_secrets(data)
    # Dataclass descriptions can contain tuples (notably universe), whereas the
    # persisted JSON contains lists. Compare the actual JSON representation on
    # resume, or an unchanged service --config would be rejected every reboot.
    return json.loads(json.dumps(data, allow_nan=False))


def initialize_campaign(campaign_dir: Path, config_path: Path | None, *, now=None) -> dict:
    """Call only under supervisor.lock. An existing deadline is never rewritten."""
    campaign_dir = campaign_dir.resolve()
    manifest_path = campaign_dir / 'campaign.json'
    if manifest_path.exists():
        campaign = read_json(manifest_path)
        if campaign.get('schema_version') != 1 or campaign.get('mode') != 'paper':
            raise ValueError('unrecognized or non-paper campaign manifest')
        started, deadline = parse_utc(campaign['started_at']), parse_utc(campaign['deadline'])
        if deadline - started != CAMPAIGN_SECONDS:
            raise ValueError('campaign duration differs from the fixed 28 days')
        if campaign['campaign_dir'] != str(campaign_dir):
            raise ValueError('campaign directory moved; restore its original absolute path')
        if digest(campaign_dir / 'config.frozen.json') != campaign['config_sha256']:
            raise ValueError('frozen campaign config changed; restore it before resuming')
        if execution_manifest() != campaign['execution_manifest']:
            raise ValueError('code or dependency versions changed; restore the recorded version before resuming')
        if config_path is not None:
            candidate = frozen_config(config_path, campaign_dir / 'state')
            if candidate != read_json(campaign_dir / 'config.frozen.json'):
                raise ValueError('supplied config differs from frozen campaign config')
        return campaign
    if config_path is None:
        raise ValueError('--config is required for a new campaign')
    # Do not attach a new duration to a directory containing old trading state.
    if (campaign_dir / 'state').exists() or (campaign_dir / 'status.json').exists():
        raise ValueError('new campaign needs a new directory; existing state was found')
    config = frozen_config(config_path, campaign_dir / 'state')
    campaign_dir.mkdir(parents=True, exist_ok=True)
    atomic_json(campaign_dir / 'config.frozen.json', config)
    started = int(time.time() if now is None else now)
    campaign = {
        'schema_version': 1, 'mode': 'paper', 'campaign_id': uuid.uuid4().hex,
        'campaign_dir': str(campaign_dir), 'started_at': utc_text(started),
        'deadline': utc_text(started + CAMPAIGN_SECONDS), 'duration_seconds': CAMPAIGN_SECONDS,
        'config_sha256': digest(campaign_dir / 'config.frozen.json'),
        'execution_manifest': execution_manifest(), 'git_revision': git_revision(),
        'restart_policy': {'max_restarts': MAX_RESTARTS, 'initial_backoff_seconds': 30,
                           'maximum_backoff_seconds': 300},
        'measurement_scope': 'public market observations and simulated fills; not actual exchange execution',
    }
    atomic_json(manifest_path, campaign)
    return campaign


def child_command(campaign_dir: Path, campaign: dict) -> list[str]:
    return [sys.executable, '-u', str(ROOT / 'src/live/run.py'), '--mode', 'paper', '--loop',
            '--until', campaign['deadline'], '--config', str(campaign_dir / 'config.frozen.json'),
            '--state-dir', str(campaign_dir / 'state')]


def child_environment() -> dict:
    env = dict(os.environ)
    # Never inherit a signing key accidentally from a developer's terminal.
    env.pop('LIGHTER_API_PRIVATE_KEY', None)
    env['PYTHONUNBUFFERED'] = '1'
    env['PYTHONUTF8'] = '1'
    env['DRY_RUN'] = 'true'
    return env


def process_birth(pid: int) -> str | None:
    """PID reuse guard. This value is metadata, never a credential."""
    try:
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
            kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return None
            try:
                stamps = [wintypes.FILETIME() for _ in range(4)]
                if not kernel.GetProcessTimes(handle, *(ctypes.byref(v) for v in stamps)):
                    return None
                return str((stamps[0].dwHighDateTime << 32) + stamps[0].dwLowDateTime)
            finally:
                kernel.CloseHandle(handle)
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] == 'Z':
            return None
        boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        return f'{boot_id}:{fields[19]}'  # field 22: process start ticks since boot
    except (OSError, ValueError, IndexError):
        return None


def previous_child_is_alive(status: dict) -> bool:
    pid, birth = status.get('child_pid'), status.get('child_birth')
    return bool(pid and birth and process_birth(pid) == birth)


def restart_delay(restarts: int) -> int:
    return min(300, 30 * 2 ** min(max(restarts - 1, 0), 4))


def stop_child(child: subprocess.Popen, grace_seconds: float = 90) -> bool:
    """Returns true if a force kill was required; caller records this evidence."""
    if child.poll() is not None:
        return False
    try:
        if os.name == 'nt':
            child.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=grace_seconds)
        return False
    except (OSError, subprocess.TimeoutExpired):
        if child.poll() is None:
            if os.name == 'nt':
                # A venv python.exe may be a redirector with the real Python as
                # its descendant. Stop the owned tree, not just its launcher.
                subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'],
                               capture_output=True, timeout=10,
                               creationflags=subprocess.CREATE_NO_WINDOW)
                if child.poll() is None:
                    child.kill()
            else:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            child.wait(timeout=10)
        return True


def campaign_status(campaign_dir: Path) -> dict:
    manifest_path = campaign_dir / 'campaign.json'
    if not manifest_path.exists():
        return {'phase': 'not_initialized', 'campaign_dir': str(campaign_dir)}
    campaign = read_json(manifest_path)
    status_path = campaign_dir / 'status.json'
    status = read_json(status_path) if status_path.exists() else {'phase': 'initialized'}
    status.update({'campaign_id': campaign['campaign_id'], 'mode': campaign['mode'],
                   'started_at': campaign['started_at'], 'deadline': campaign['deadline'],
                   'seconds_remaining': max(0, int(parse_utc(campaign['deadline']) - time.time())),
                   'worker_process_alive': previous_child_is_alive(status),
                   'stop_requested': (campaign_dir / 'STOP').exists(),
                   'calendar_duration_is_not_data_coverage': True})
    return status


def supervise(campaign_dir: Path, config_path: Path | None) -> int:
    campaign_dir = campaign_dir.resolve()
    with ProcessLock(campaign_dir / 'supervisor.lock'):
        campaign = initialize_campaign(campaign_dir, config_path)
        deadline = parse_utc(campaign['deadline'])
        status_path = campaign_dir / 'status.json'
        status = read_json(status_path) if status_path.exists() else {'restarts': 0}
        if status.get('phase') in TERMINAL:
            print(json.dumps(campaign_status(campaign_dir), ensure_ascii=False))
            return EXIT_CONFIG if status['phase'] == 'failed' else 0
        if previous_child_is_alive(status):
            # Do not guess whether the worker can be killed. systemd's
            # KillMode=control-group cleans orphans before restarting this unit.
            raise AlreadyRunningError('previous paper worker still exists; stop its service/process before resuming')
        interrupted = [False]

        def on_signal(*_):
            interrupted[0] = True

        old_handlers = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_handlers[sig] = signal.signal(sig, on_signal)
        if os.name == 'nt':
            old_handlers[signal.SIGBREAK] = signal.signal(signal.SIGBREAK, on_signal)

        child = None
        log = None

        def persist(phase: str, **fields):
            status.update(fields)
            status.update(phase=phase, updated_at=utc_text(time.time()), supervisor_pid=os.getpid())
            atomic_json(status_path, status)

        try:
            while True:
                now = time.time()
                if now >= deadline:
                    forced = stop_child(child) if child is not None else False
                    persist('completed', reason='fixed calendar deadline reached', forced_stop=forced,
                            child_pid=None, child_birth=None)
                    return 0
                if (campaign_dir / 'STOP').exists():
                    forced = stop_child(child) if child is not None else False
                    persist('stopped', reason='operator STOP request', forced_stop=forced,
                            child_pid=None, child_birth=None)
                    return 0
                if interrupted[0]:
                    forced = stop_child(child) if child is not None else False
                    persist('paused', reason='supervisor signal; restart resumes the same deadline',
                            forced_stop=forced, child_pid=None, child_birth=None)
                    return 0
                if child is not None and child.poll() is not None:
                    exit_code = child.returncode
                    child = None
                    if log is not None:
                        log.close()
                        log = None
                    # An early zero exit is also unexpected, not campaign success.
                    if status.get('restarts', 0) >= MAX_RESTARTS:
                        persist('failed', reason='finite restart budget exhausted', last_exit_code=exit_code,
                                child_pid=None, child_birth=None)
                        return EXIT_CONFIG
                    restarts = status.get('restarts', 0) + 1
                    persist('backoff', restarts=restarts, last_exit_code=exit_code, child_pid=None,
                            child_birth=None, next_start_at=utc_text(now + restart_delay(restarts)))
                if child is None:
                    resume_at = parse_utc(status['next_start_at']) if status.get('next_start_at') else 0
                    if now >= resume_at:
                        log_dir = campaign_dir / 'logs'
                        log_dir.mkdir(exist_ok=True)
                        log = (log_dir / f'worker-{time.strftime("%Y%m%d", time.gmtime(now))}.log').open('ab', buffering=0)
                        kwargs = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
                        try:
                            child = subprocess.Popen(child_command(campaign_dir, campaign), cwd=ROOT,
                                                     env=child_environment(), stdin=subprocess.DEVNULL,
                                                     stdout=log, stderr=subprocess.STDOUT, **kwargs)
                        except OSError:
                            # Treat spawn failure as terminal, not an unbounded service restart.
                            persist('failed', reason='could not spawn paper worker', child_pid=None, child_birth=None)
                            return EXIT_CONFIG
                        persist('running', child_pid=child.pid, child_birth=process_birth(child.pid),
                                next_start_at=None, worker_started_at=utc_text(now))
                if child is not None:
                    persist('running')
                time.sleep(min(POLL_SECONDS, max(0.01, deadline - time.time())))
        finally:
            if child is not None and child.poll() is None:
                stop_child(child)
            if log is not None:
                log.close()
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'status', 'stop'))
    parser.add_argument('--campaign-dir', required=True, type=Path)
    parser.add_argument('--config', type=Path, help='required only for a new campaign; frozen on first start')
    args = parser.parse_args(argv)
    campaign_dir = args.campaign_dir.resolve()
    try:
        if args.action == 'status':
            print(json.dumps(campaign_status(campaign_dir), ensure_ascii=False, indent=2))
            return 0
        if args.action == 'stop':
            if not (campaign_dir / 'campaign.json').exists():
                raise ValueError('campaign has not been initialized')
            atomic_json(campaign_dir / 'STOP', {'requested_at': utc_text(time.time())})
            print('STOP requested; the supervisor will stop the worker and preserve records')
            return 0
        return supervise(campaign_dir, args.config.resolve() if args.config else None)
    except AlreadyRunningError as exc:
        print(str(exc), file=sys.stderr)
        return 75
    except (ValueError, KeyError, OSError) as exc:
        print(f'paper supervisor refused: {exc}', file=sys.stderr)
        return EXIT_CONFIG


if __name__ == '__main__':
    raise SystemExit(main())

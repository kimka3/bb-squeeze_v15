"""Install the independent observer, without activating it or touching trading.

Run this reviewed bundle as root on Linux. Destinations are fixed, existing
files must match exactly, and the ping credential is inspected only by stat.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

STAGE = '/opt/bb-squeeze-alerts-20261023'
CONFIG_DIR = '/etc/bb-squeeze-alerts'
STATE_DIR = '/var/lib/bb-squeeze-alerts'
LIVE_DIR = '/var/lib/bb-squeeze-live/strategy-01'
SERVICE = 'bb-squeeze-alerts.service'
TIMER = 'bb-squeeze-alerts.timer'


class InstallError(Exception):
    pass


@dataclass(frozen=True)
class File:
    path: Path
    content: bytes
    mode: int


@dataclass(frozen=True)
class Plan:
    root: Path
    directories: tuple
    files: tuple
    activation: Path
    credential: Path
    fingerprint: str
    account_index: int


def validate_identity(fingerprint, account_index):
    if (not isinstance(fingerprint, str)
            or re.fullmatch('[0-9a-f]{64}', fingerprint) is None
            or type(account_index) is not int or account_index < 0):
        raise InstallError('invalid expected activation identity')


def _metadata(info, mode=None):
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
        raise InstallError('unsafe ownership or permissions')
    if mode is not None and stat.S_IMODE(info.st_mode) != mode:
        raise InstallError('existing mode does not match required mode')


def _inspect(path, *, directory=False, mode=None, missing_ok=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise InstallError('required path missing') from None
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode):
        raise InstallError('symlink or unexpected file type')
    if not directory and info.st_nlink != 1:
        raise InstallError('hard-linked file is not allowed')
    _metadata(info, mode)
    return info


def _read_regular(path, limit):
    """Bounded no-follow read; never used for the ping credential."""
    _inspect(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                 | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise InstallError('unexpected file type')
        _metadata(info)
        if info.st_size > limit:
            raise InstallError('file exceeds size limit')
        content = handle.read(limit + 1)
    if len(content) > limit:
        raise InstallError('file exceeds size limit')
    return content


def build_plan(source, fingerprint, account_index, *, root=Path('/')):
    """The root argument is a test seam; the CLI always uses the real root."""
    validate_identity(fingerprint, account_index)
    source, root = Path(source), Path(root)
    _inspect(source, directory=True)
    def target(value):
        return root / value.lstrip('/')
    config = {
        'version': 1, 'state_dir': LIVE_DIR, 'output_dir': STATE_DIR,
        'ping_uuid_file': CONFIG_DIR + '/healthchecks.uuid',
        'expected_fingerprint': fingerprint, 'expected_account_index': account_index,
        'service_unit': 'bb-squeeze-live.service',
        'health_timer_unit': 'bb-squeeze-live-health.timer',
    }
    files = [File(target(STAGE + '/' + name),
                  _read_regular(source / name, 2 * 1024 * 1024), 0o644)
             for name in ('monitor.py', 'configure.py')]
    for name, destination, mode in (
        (SERVICE + '.in', '/etc/systemd/system/' + SERVICE, 0o644),
        (TIMER, '/etc/systemd/system/' + TIMER, 0o644),
        ('bb-alerts.in', '/usr/local/sbin/bb-alerts', 0o755),
    ):
        try:
            content = _read_regular(source / name, 65536).decode('utf-8')
        except UnicodeError:
            raise InstallError('invalid template encoding') from None
        content = content.replace('\r\n', '\n').replace('@ALERT_STAGE@', STAGE)
        if '@ALERT_STAGE@' in content:
            raise InstallError('unresolved template placeholder')
        files.append(File(target(destination), content.encode('utf-8'), mode))
    files.append(File(target(CONFIG_DIR + '/config.json'),
                      (json.dumps(config, indent=2, sort_keys=True) + '\n').encode(), 0o600))
    return Plan(root, tuple((target(path), mode) for path, mode in (
        (STAGE, 0o755), (CONFIG_DIR, 0o700), (STATE_DIR, 0o700))), tuple(files),
        target(LIVE_DIR + '/activation.json'), target(CONFIG_DIR + '/healthchecks.uuid'),
        fingerprint, account_index)


def _ancestors(path, boundary):
    if path != boundary and boundary not in path.parents:
        raise InstallError('path outside installation root')
    for parent in reversed(path.parents):
        if parent == boundary or boundary in parent.parents:
            _inspect(parent, directory=True)


def check_activation(plan):
    _ancestors(plan.activation, plan.root)
    raw = _read_regular(plan.activation, 65536)
    def unique_pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result
    try:
        activation = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique_pairs,
                                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if (not isinstance(activation, dict) or activation.get('approved') is not True
                or activation.get('fingerprint') != plan.fingerprint
                or type(activation.get('account_index')) is not int
                or activation['account_index'] != plan.account_index):
            raise ValueError()
    except (ValueError, UnicodeError, RecursionError):
        raise InstallError('live activation does not match approved expected identity') from None
    return raw


def preflight(plan):
    planned = dict(plan.directories)
    for path, mode in plan.directories:
        _ancestors(path, plan.root)
        _inspect(path, directory=True, mode=mode, missing_ok=True)
    for entry in plan.files:
        # A planned direct parent may not yet exist; all its ancestors must.
        _ancestors(entry.path.parent if entry.path.parent in planned else entry.path, plan.root)
        info = _inspect(entry.path, mode=entry.mode, missing_ok=True)
        if info is not None and _read_regular(entry.path, len(entry.content)) != entry.content:
            raise InstallError('existing installation conflicts; no files changed')
    # Never read credential contents, including during an idempotent install.
    _inspect(plan.credential, mode=0o600, missing_ok=True)


def _command(args, runner):
    try:
        result = runner(args, capture_output=True, text=True, timeout=25, check=False)
    except (OSError, subprocess.SubprocessError):
        raise InstallError('installation command failed') from None
    if result.returncode != 0:
        raise InstallError('installation command failed')
    return result.stdout


def check_inactive(runner):
    for name in (SERVICE, TIMER):
        raw = _command(['/usr/bin/systemctl', 'show', name, '--no-pager',
                        '--property=LoadState,ActiveState,UnitFileState'], runner)
        values = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
        if (values.get('LoadState') not in ('loaded', 'not-found')
                or values.get('ActiveState') not in ('inactive', 'failed')
                or values.get('UnitFileState', '') not in ('', 'disabled', 'static')):
            raise InstallError('alert units must be inactive and disabled before installation')


def verify_units(plan, runner):
    # Validate before changing any installation destination. No unit is started.
    with tempfile.TemporaryDirectory(prefix='bb-alerts-verify-') as temporary:
        paths = []
        for entry in plan.files:
            if entry.path.name in (SERVICE, TIMER):
                path = Path(temporary) / entry.path.name
                path.write_bytes(entry.content)
                paths.append(str(path))
        _command(['/usr/bin/systemd-analyze', 'verify', *paths], runner)


def apply(plan, *, runner=subprocess.run):
    approval = check_activation(plan)
    preflight(plan)
    check_inactive(runner)
    verify_units(plan, runner)
    if check_activation(plan) != approval:
        raise InstallError('activation changed during installation preflight')
    preflight(plan)
    created_files, created_directories = [], []
    try:
        for directory, mode in plan.directories:
            if not directory.exists():
                directory.mkdir(mode=mode)
                created_directories.append(directory)
                os.chmod(directory, mode)
        for entry in plan.files:
            if entry.path.exists():
                continue  # Both preflights required an exact content/mode match.
            descriptor = os.open(entry.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | getattr(os, 'O_NOFOLLOW', 0), entry.mode)
            with os.fdopen(descriptor, 'wb') as handle:
                info = os.fstat(handle.fileno())
                created_files.append((entry.path, info.st_dev, info.st_ino))
                os.fchmod(handle.fileno(), entry.mode)
                handle.write(entry.content)
                handle.flush()
                os.fsync(handle.fileno())
        preflight(plan)
        _command(['/usr/bin/systemctl', 'daemon-reload'], runner)
    except BaseException:
        # Only new files with the same inode are rolled back. Existing files,
        # credentials, live files and enabled/active states are never touched.
        for path, device, inode in reversed(created_files):
            try:
                info = path.lstat()
                if stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == (device, inode):
                    path.unlink()
            except OSError:
                pass
        for directory in reversed(created_directories):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise
    return {'installed': True, 'new_files': len(created_files),
            'alerts_enabled': False, 'notification_sent': False,
            'trading_changed': False, 'credential_read': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fingerprint', required=True)
    parser.add_argument('--account-index', required=True, type=int)
    args = parser.parse_args(argv)
    if sys.platform != 'linux' or not hasattr(os, 'geteuid') or os.geteuid() != 0:
        print(json.dumps({'installed': False, 'reason': 'Linux root required'}))
        return 1
    try:
        plan = build_plan(Path(__file__).parent, args.fingerprint, args.account_index)
        result = apply(plan)
    except (InstallError, OSError, ValueError) as error:
        reason = str(error) if isinstance(error, InstallError) else 'installation filesystem validation failed'
        print(json.dumps({'installed': False, 'reason': reason,
                          'alerts_enabled': False, 'trading_changed': False}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

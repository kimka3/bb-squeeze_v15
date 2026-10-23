"""Installer scope, fail-before-write preflights, and disabled activation."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest


MODULE_PATH = Path(__file__).parents[1] / 'ops' / 'alerts' / 'install.py'
spec = importlib.util.spec_from_file_location('external_alert_install', MODULE_PATH)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)
check_metadata = m._metadata
FINGERPRINT = 'a' * 64


class Commands:
    def __init__(self, *, enabled=False, verify_failure=False, reload_failure=False):
        self.calls = []
        self.enabled = enabled
        self.verify_failure = verify_failure
        self.reload_failure = reload_failure
        self.verified_contents = {}

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        assert kwargs == {'capture_output': True, 'text': True, 'timeout': 25, 'check': False}
        if args[1] == 'show':
            state = 'enabled' if self.enabled else 'disabled'
            return SimpleNamespace(returncode=0, stdout=f'LoadState=loaded\nActiveState=inactive\nUnitFileState={state}\n')
        if args[1] == 'verify':
            self.verified_contents = {Path(p).name: Path(p).read_text() for p in args[2:]}
            return SimpleNamespace(returncode=int(self.verify_failure), stdout='')
        assert args == ['/usr/bin/systemctl', 'daemon-reload']
        return SimpleNamespace(returncode=int(self.reload_failure), stdout='')


@pytest.fixture
def environment(tmp_path, monkeypatch):
    # POSIX metadata is tested independently below; this allows the same
    # installation transaction tests to run on Windows without fake root.
    monkeypatch.setattr(m, '_metadata', lambda info, mode=None: None)
    if not hasattr(os, 'fchmod'):
        monkeypatch.setattr(m.os, 'fchmod', lambda fd, mode: None, raising=False)
    root, source = tmp_path / 'target', tmp_path / 'bundle'
    root.mkdir()
    source.mkdir()
    for relative in ('opt', 'etc/systemd/system', 'usr/local/sbin',
                     'var/lib/bb-squeeze-live/strategy-01'):
        (root / relative).mkdir(parents=True, exist_ok=True)
    for name, content in {
        'monitor.py': '# standalone monitor\n',
        'configure.py': '# hidden credential prompt\n',
        m.SERVICE + '.in': '[Service]\nExecStart=/usr/bin/python3 @ALERT_STAGE@/monitor.py\n',
        m.TIMER: '[Timer]\nUnit=bb-squeeze-alerts.service\n',
        'bb-alerts.in': '#!/bin/sh\nexec /usr/bin/python3 @ALERT_STAGE@/monitor.py\n',
    }.items():
        (source / name).write_text(content, encoding='utf-8')
    activation = root / m.LIVE_DIR.lstrip('/') / 'activation.json'
    activation.write_text(json.dumps({'approved': True, 'account_index': 716835,
                                      'fingerprint': FINGERPRINT}), encoding='utf-8')
    plan = m.build_plan(source, FINGERPRINT, 716835, root=root)
    return root, source, plan


def tree(root):
    return {str(path.relative_to(root)): None if path.is_dir() else path.read_bytes()
            for path in root.rglob('*')}


@pytest.mark.parametrize('fingerprint, account', [
    ('a' * 63, 716835), ('A' * 64, 716835), ('z' * 64, 716835),
    ('a' * 64 + '\n', 716835), (None, 716835),
    (FINGERPRINT, True), (FINGERPRINT, -1), (FINGERPRINT, '716835'),
])
def test_invalid_expected_identity(fingerprint, account):
    with pytest.raises(m.InstallError, match='identity'):
        m.validate_identity(fingerprint, account)


@pytest.mark.parametrize('changes', [
    {'approved': False}, {'approved': 1}, {'account_index': '716835'},
    {'account_index': True}, {'account_index': 22}, {'fingerprint': 'b' * 64},
])
def test_activation_mismatch_does_not_install(environment, changes):
    root, _, plan = environment
    value = json.loads(plan.activation.read_text())
    value.update(changes)
    plan.activation.write_text(json.dumps(value))
    before = tree(root)
    commands = Commands()
    with pytest.raises(m.InstallError, match='activation'):
        m.apply(plan, runner=commands)
    assert tree(root) == before
    assert commands.calls == []


@pytest.mark.parametrize('raw', [
    '{"approved":false,"approved":true,"account_index":716835,"fingerprint":"' + FINGERPRINT + '"}',
    '{broken', '[]', '{"approved":NaN}',
])
def test_malformed_or_duplicate_activation_rejected(environment, raw):
    root, _, plan = environment
    plan.activation.write_text(raw)
    before = tree(root)
    with pytest.raises(m.InstallError):
        m.apply(plan, runner=Commands())
    assert tree(root) == before


def test_late_conflict_has_no_partial_installation(environment):
    root, _, plan = environment
    target = plan.files[-1].path
    target.parent.mkdir()
    target.write_bytes(b'{"conflicting":true}')
    before = tree(root)
    commands = Commands()
    with pytest.raises(m.InstallError):
        m.apply(plan, runner=commands)
    assert tree(root) == before
    assert commands.calls == []


def test_directory_file_conflict_before_writes(environment):
    root, _, plan = environment
    plan.directories[-1][0].write_bytes(b'not a directory')
    before = tree(root)
    with pytest.raises(m.InstallError, match='file type'):
        m.apply(plan, runner=Commands())
    assert tree(root) == before


def test_symlink_ancestor_rejected(environment, monkeypatch):
    _, _, plan = environment
    original = Path.lstat
    bad = plan.root / 'etc'
    def spoof(path, *args, **kwargs):
        if path == bad:
            return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', spoof)
    with pytest.raises(m.InstallError, match='symlink'):
        m.preflight(plan)


def test_hardlinked_credential_rejected_without_read(environment, monkeypatch):
    _, _, plan = environment
    plan.credential.parent.mkdir()
    plan.credential.write_bytes(b'never read me')
    original = Path.lstat
    def spoof(path, *args, **kwargs):
        if path == plan.credential:
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=2)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', spoof)
    with pytest.raises(m.InstallError, match='hard-linked'):
        m.preflight(plan)


@pytest.mark.parametrize('uid, mode, required', [
    (1000, 0o600, 0o600), (0, 0o666, None), (0, 0o775, None),
    (0, 0o644, 0o600), (0, 0o755, 0o700),
])
def test_unsafe_posix_ownership_and_modes_fail(uid, mode, required):
    with pytest.raises(m.InstallError):
        check_metadata(SimpleNamespace(st_uid=uid, st_mode=stat.S_IFREG | mode), required)


def test_safe_posix_modes_allowed():
    check_metadata(SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o600), 0o600)
    check_metadata(SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o755))


def test_already_enabled_observer_not_stopped_or_modified(environment):
    root, _, plan = environment
    before = tree(root)
    commands = Commands(enabled=True)
    with pytest.raises(m.InstallError, match='inactive and disabled'):
        m.apply(plan, runner=commands)
    assert tree(root) == before
    assert all(command[1] == 'show' for command in commands.calls)


def test_unit_validation_failure_precedes_writes(environment):
    root, _, plan = environment
    before = tree(root)
    commands = Commands(verify_failure=True)
    with pytest.raises(m.InstallError, match='command failed'):
        m.apply(plan, runner=commands)
    assert tree(root) == before
    assert not any(command[1] == 'daemon-reload' for command in commands.calls)


def test_approval_change_during_verification_prevents_install(environment):
    root, _, plan = environment
    commands = Commands()
    def runner(args, **kwargs):
        result = commands(args, **kwargs)
        if args[1] == 'verify':
            value = json.loads(plan.activation.read_text())
            value['new_approval_timestamp'] = 123
            plan.activation.write_text(json.dumps(value))
        return result
    with pytest.raises(m.InstallError, match='activation changed'):
        m.apply(plan, runner=runner)
    assert not any(path.exists() for path, _ in plan.directories)
    assert not any(entry.path.exists() for entry in plan.files)


def test_install_is_disabled_and_idempotent_never_reads_secret(environment, monkeypatch):
    root, _, plan = environment
    plan.credential.parent.mkdir()
    plan.credential.write_bytes(b'not even a UUID: installer must never read this')
    secret_inode = plan.credential.stat().st_ino
    original_open = os.open
    def forbidden_secret_read(path, *args, **kwargs):
        assert Path(path) != plan.credential, 'credential must not be opened'
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(m.os, 'open', forbidden_secret_read)
    activation_before = plan.activation.read_bytes()
    commands = Commands()
    result = m.apply(plan, runner=commands)
    assert result == {'installed': True, 'new_files': 6, 'alerts_enabled': False,
                      'notification_sent': False, 'trading_changed': False, 'credential_read': False}
    assert set(commands.verified_contents) == {m.SERVICE, m.TIMER}
    assert all('@ALERT_STAGE@' not in value for value in commands.verified_contents.values())
    for entry in plan.files:
        assert entry.path.read_bytes() == entry.content
    assert plan.activation.read_bytes() == activation_before
    assert plan.credential.stat().st_ino == secret_inode
    first_files = {entry.path: entry.path.stat().st_ino for entry in plan.files}
    assert m.apply(plan, runner=commands)['new_files'] == 0
    assert first_files == {entry.path: entry.path.stat().st_ino for entry in plan.files}
    assert all(command[1] in ('show', 'verify', 'daemon-reload') for command in commands.calls)
    assert all('bb-squeeze-live.service' not in command for command in commands.calls)
    config = json.loads(plan.files[-1].path.read_text())
    assert config['state_dir'] == m.LIVE_DIR
    assert config['output_dir'] == m.STATE_DIR
    assert config['expected_fingerprint'] == FINGERPRINT


def test_reload_failure_rolls_back_only_new_files(environment):
    root, _, plan = environment
    plan.credential.parent.mkdir()
    plan.credential.write_bytes(b'preserve credential')
    existing = plan.files[-1]
    existing.path.write_bytes(existing.content)
    before = tree(root)
    with pytest.raises(m.InstallError, match='command failed'):
        m.apply(plan, runner=Commands(reload_failure=True))
    assert tree(root) == before


def test_command_timeout_is_sanitized_and_before_writes(environment):
    root, _, plan = environment
    before = tree(root)
    def runner(*args, **kwargs):
        raise subprocess.TimeoutExpired('do-not-expose-command', 25)
    with pytest.raises(m.InstallError) as error:
        m.apply(plan, runner=runner)
    assert 'do-not-expose' not in str(error.value)
    assert tree(root) == before


def test_main_rejects_non_linux_or_non_root_before_any_access(monkeypatch, capsys):
    monkeypatch.setattr(m.sys, 'platform', 'win32')
    monkeypatch.setattr(m, 'build_plan', lambda *a, **kw: pytest.fail('must not access files'))
    assert m.main(['--fingerprint', FINGERPRINT, '--account-index', '716835']) == 1
    assert json.loads(capsys.readouterr().out)['reason'] == 'Linux root required'


def test_cli_does_not_offer_alternate_root_or_destination(monkeypatch):
    with pytest.raises(SystemExit) as error:
        m.main(['--fingerprint', FINGERPRINT, '--account-index', '716835', '--root', '/tmp'])
    assert error.value.code == 2

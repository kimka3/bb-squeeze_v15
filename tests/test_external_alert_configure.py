"""Credential setup must never fall back to visible input or accept a false URL."""
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

SOURCE = Path(__file__).parents[1] / 'ops' / 'alerts' / 'configure.py'
spec = importlib.util.spec_from_file_location('external_alert_configure', SOURCE)
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)
IDENTIFIER = '12345678-1234-4234-8234-123456789abc'


@pytest.mark.parametrize('value', [IDENTIFIER, IDENTIFIER.upper(), 'https://hc-ping.com/' + IDENTIFIER])
def test_expected_uuid_or_https_url(value):
    assert configure.parse_ping_uuid(value) == IDENTIFIER


@pytest.mark.parametrize('value', [
    '00000000-0000-0000-0000-000000000000', 'http://hc-ping.com/',
    'https://hc-ping.com.evil.test/' + IDENTIFIER,
    'https://hc-ping.com@evil.test/' + IDENTIFIER,
    'https://hc-ping.com/' + IDENTIFIER + '/fail',
    'https://hc-ping.com/' + IDENTIFIER + '?secret=yes',
    'https://hc-ping.com/' + IDENTIFIER + '#fragment',
    'https://hc-ping.com:443/' + IDENTIFIER,
    IDENTIFIER.replace('-', ''), 'SECRET INVALID INPUT',
])
def test_rejects_misrouted_or_invalid_credentials_without_echo(value):
    with pytest.raises(ValueError) as caught:
        configure.parse_ping_uuid(value)
    assert value not in str(caught.value)


def test_non_terminal_never_prompts_or_saves(monkeypatch, capsys):
    monkeypatch.setattr(configure.os, 'geteuid', lambda: 0, raising=False)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)
    monkeypatch.setattr(configure.getpass, 'getpass', lambda *a: pytest.fail('prompted'))
    assert configure.main() == 1
    assert json.loads(capsys.readouterr().out)['configured'] is False


def test_echo_fallback_is_an_error(monkeypatch, capsys):
    monkeypatch.setattr(configure.os, 'geteuid', lambda: 0, raising=False)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    def fallback(*args):
        configure.warnings.warn('cannot hide input', configure.getpass.GetPassWarning)
        pytest.fail('continued after echo warning')
    monkeypatch.setattr(configure.getpass, 'getpass', fallback)
    assert configure.main() == 1
    assert json.loads(capsys.readouterr().out)['configured'] is False


@pytest.mark.skipif(os.name != 'posix' or getattr(os, 'geteuid', lambda: -1)() != 0,
                    reason='Linux root ownership and atomic credential installation')
def test_atomic_secret_permissions_and_invalid_input_preserves_existing(tmp_path):
    tmp_path.chmod(0o700)
    configure.save_credential(tmp_path, IDENTIFIER)
    target = tmp_path / 'healthchecks.uuid'
    assert target.read_text().strip() == IDENTIFIER
    assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        configure.save_credential(tmp_path, 'invalid')
    assert target.read_text().strip() == IDENTIFIER
    assert sorted(p.name for p in tmp_path.iterdir()) == ['healthchecks.uuid']


@pytest.mark.skipif(os.name != 'posix' or getattr(os, 'geteuid', lambda: -1)() != 0,
                    reason='Linux root-owned directory validation')
def test_symlink_secret_and_unsafe_directory_rejected(tmp_path):
    tmp_path.chmod(0o700)
    original = tmp_path / 'original'
    original.write_text('keep')
    (tmp_path / 'healthchecks.uuid').symlink_to(original)
    with pytest.raises(ValueError):
        configure.save_credential(tmp_path, IDENTIFIER)
    assert original.read_text() == 'keep'
    tmp_path.chmod(0o755)
    with pytest.raises(ValueError):
        configure.save_credential(tmp_path, IDENTIFIER)

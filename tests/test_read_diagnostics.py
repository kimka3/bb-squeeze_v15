import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
from live.read_diagnostics import ReadDiagnostics


def test_records_only_bounded_sanitized_fields(tmp_path):
    path = tmp_path / 'reads.jsonl'
    ReadDiagnostics(path).record({
        'timestamp_ms': 123456789, 'endpoint': '/api/v1/account?authorization=SECRET',
        'status': 503, 'seconds': .125, 'attempt': 1, 'outcome': 'http_error',
        'error_type': 'HTTPError', 'retry_reason': 'transient_http',
        'request_ids': {'X-Request-ID': 'req-123', 'x-amz-cf-id': 'a_B/+=',
                        'Authorization': 'SECRET', 'cookie': 'SECRET',
                        'cf-ray': 'unsafe\nSECRET', 'x-correlation-id': 'x'*257},
        'query': 'SECRET', 'body': 'SECRET', 'token': 'SECRET',
        'headers': {'Authorization': 'SECRET'}, 'key': 'SECRET',
        'exception': 'SECRET'})
    contents = path.read_text()
    assert 'SECRET' not in contents
    assert json.loads(contents) == {
        'timestamp_ms': 123456789, 'endpoint': '/api/v1/account', 'status': 503,
        'seconds': .125, 'attempt': 1, 'outcome': 'http_error',
        'error_type': 'HTTPError', 'retry_reason': 'transient_http',
        'request_ids': {'x-request-id': 'req-123', 'x-amz-cf-id': 'a_B/+='}}
    if os.name == 'posix':
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize('endpoint', [
    'https://host/api/v1/account', '/api/v1/account/SECRET',
    '/api/v1/account#SECRET', '/api/v1/../SECRET', '/api/v1/account\nSECRET'])
def test_invalid_endpoints_and_unstructured_metadata_are_not_logged(tmp_path, endpoint):
    path = tmp_path / 'reads.jsonl'
    ReadDiagnostics(path).record({'endpoint': endpoint, 'timestamp_ms': True,
        'seconds': float('inf'), 'status': '503 SECRET', 'attempt': '1 SECRET',
        'outcome': 'SECRET', 'retry_reason': 'SECRET', 'error_type': 'Error: SECRET',
        'request_ids': ['SECRET']})
    assert json.loads(path.read_text()) == {}


def test_rotation_retains_two_bounded_backups_and_complete_records(tmp_path, monkeypatch):
    monkeypatch.setattr(ReadDiagnostics, 'MAX_BYTES', 128)
    path = tmp_path / 'reads.jsonl'
    writer = ReadDiagnostics(path)
    for number in range(30):
        writer.record({'timestamp_ms': number, 'endpoint': '/api/v1/account',
                       'status': 200, 'outcome': 'success'})
    paths = [path, *writer.backups]
    assert set(tmp_path.iterdir()) == {*paths, writer.lock_path}
    assert writer.lock_path.read_bytes() == b'\0'
    for candidate in paths:
        assert 0 < candidate.stat().st_size <= 128
        for line in candidate.read_text().splitlines():
            assert json.loads(line)['endpoint'] == '/api/v1/account'
    assert json.loads(path.read_text())['timestamp_ms'] == 29
    assert json.loads(writer.backups[0].read_text())['timestamp_ms'] == 28
    assert json.loads(writer.backups[1].read_text())['timestamp_ms'] == 27


@pytest.mark.parametrize('suffix', ['', '.1', '.2', '.lock'])
def test_refuses_symlink_at_log_or_rotation_target(tmp_path, suffix):
    sentinel = tmp_path / 'sentinel'
    sentinel.write_text('do not change')
    path = tmp_path / 'reads.jsonl'
    link = path.with_name(path.name + suffix)
    try:
        link.symlink_to(sentinel)
    except (OSError, NotImplementedError):
        pytest.skip('symlink creation unavailable')
    with pytest.raises(OSError):
        ReadDiagnostics(path).record({'status': 200})
    assert sentinel.read_text() == 'do not change'
    assert link.is_symlink()
    if suffix:
        assert not path.exists()


def test_refuses_symlink_parent_and_missing_directory(tmp_path):
    target = tmp_path / 'target'
    target.mkdir()
    link = tmp_path / 'linked'
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip('symlink creation unavailable')
    with pytest.raises(OSError):
        ReadDiagnostics(link / 'reads.jsonl').record({'status': 200})
    assert not list(target.iterdir())


def test_missing_directory_is_not_implicitly_created(tmp_path):
    with pytest.raises(FileNotFoundError):
        ReadDiagnostics(tmp_path / 'missing' / 'reads.jsonl').record({'status': 200})


def test_extreme_numeric_metadata_is_omitted(tmp_path):
    path = tmp_path / 'reads.jsonl'
    ReadDiagnostics(path).record({'seconds': 10**1000, 'timestamp_ms': 10**1000,
                                  'attempt': -1, 'status': True})
    assert json.loads(path.read_text()) == {}


@pytest.mark.parametrize('suffix', ['', '.lock'])
def test_hardlinked_target_refused_without_changing_other_file(tmp_path, suffix):
    sentinel = tmp_path / 'sentinel'
    sentinel.write_text('do not change')
    path = tmp_path / 'reads.jsonl'
    try:
        os.link(sentinel, path.with_name(path.name + suffix))
    except OSError:
        pytest.skip('hardlink creation unavailable')
    with pytest.raises(OSError):
        ReadDiagnostics(path).record({'status': 200})
    assert sentinel.read_text() == 'do not change'


def test_process_contention_never_writes_or_rotates_until_lock_released(tmp_path, monkeypatch):
    path = tmp_path / 'reads.jsonl'
    writer = ReadDiagnostics(path)
    monkeypatch.setattr(ReadDiagnostics, 'MAX_BYTES', 128)
    writer.record({'timestamp_ms': 1, 'endpoint': '/api/v1/account', 'outcome': 'success'})
    before = path.read_bytes()
    script = '''
import sys
sys.path.insert(0, sys.argv[1])
from live.read_diagnostics import ReadDiagnostics
ReadDiagnostics.MAX_BYTES = 128
writer = ReadDiagnostics(sys.argv[2])
try:
    writer.record({'timestamp_ms': 2, 'endpoint': '/api/v1/account', 'outcome': 'success'})
except OSError:
    if sys.argv[3] != 'blocked': raise
    print('blocked')
else:
    if sys.argv[3] != 'write': raise AssertionError('contending writer acquired the lock')
    print('written')
'''
    command = [sys.executable, '-c', script, str(Path(__file__).parents[1] / 'src'), str(path)]
    with writer._lock():
        result = subprocess.run([*command, 'blocked'], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == 'blocked'
        assert path.read_bytes() == before
        assert not any(p.exists() for p in writer.backups)
    result = subprocess.run([*command, 'write'], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'written'
    assert json.loads(path.read_text())['timestamp_ms'] == 2
    assert writer.backups[0].read_bytes() == before
    assert writer.lock_path.read_bytes() == b'\0'
    if os.name == 'posix':
        assert stat.S_IMODE(writer.lock_path.stat().st_mode) == 0o600


def test_existing_oversize_lock_is_refused_before_log_write(tmp_path):
    path = tmp_path / 'reads.jsonl'
    writer = ReadDiagnostics(path)
    writer.lock_path.write_bytes(b'unchanged')
    with pytest.raises(OSError):
        writer.record({'status': 200})
    assert writer.lock_path.read_bytes() == b'unchanged'
    assert not path.exists()


def test_io_failures_propagate_and_existing_oversize_log_is_not_destroyed(tmp_path, monkeypatch):
    path = tmp_path / 'reads.jsonl'
    path.write_bytes(b'x' * 129)
    monkeypatch.setattr(ReadDiagnostics, 'MAX_BYTES', 128)
    with pytest.raises(OSError):
        ReadDiagnostics(path).record({'status': 200})
    assert path.read_bytes() == b'x' * 129
    path.unlink()
    def fail(*args):
        raise OSError('disk unavailable')
    monkeypatch.setattr(os, 'fsync', fail)
    with pytest.raises(OSError, match='disk unavailable'):
        ReadDiagnostics(path).record({'status': 200})

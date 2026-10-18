"""Bounded, credential-free read diagnostics shared by account read clients.

No response bodies, query strings, arbitrary headers, or exception messages are
accepted. I/O errors propagate so the transport can report degraded diagnostics
without preventing account/protection reads. The parent directory must exist.
A dedicated nonblocking process lock serializes append/rotation. Its stable
single-byte file is never removed; contention fails rather than delaying reads.
"""
import contextlib
import errno
import json
import math
import os
from pathlib import Path
import re
import stat


REQUEST_ID_KEYS = frozenset({'x-request-id', 'x-correlation-id', 'x-amz-cf-id',
                             'x-amzn-requestid', 'cf-ray'})
OUTCOMES = frozenset({'success', 'http_error', 'transport_error', 'payload_error',
                     'auth_error', 'budget_deferred'})
RETRY_REASONS = frozenset({'transient_http', 'transport_error', 'rate_limit',
                          'budget_deferred', 'deadline', 'not_retryable', 'retry_limit'})


def sanitize_sample(sample):
    clean = {}
    for key in ('timestamp_ms', 'attempt'):
        value = sample.get(key)
        if type(value) is int and 0 <= value <= 2**63-1:
            clean[key] = value
    endpoint = sample.get('endpoint')
    if isinstance(endpoint, str):
        endpoint = endpoint.split('?', 1)[0]
        if re.fullmatch(r'/api/v1/[A-Za-z0-9_]{1,80}', endpoint):
            clean['endpoint'] = endpoint
    status = sample.get('status')
    if ((type(status) is int and 100 <= status <= 599) or
            (isinstance(status, str) and status in ('error', 'deferred'))):
        clean['status'] = status
    seconds = sample.get('seconds')
    if type(seconds) in (int, float) and 0 <= seconds <= 86400 and math.isfinite(seconds):
        clean['seconds'] = seconds
    for key, choices in (('outcome', OUTCOMES), ('retry_reason', RETRY_REASONS)):
        value = sample.get(key)
        if isinstance(value, str) and value in choices:
            clean[key] = value
    error = sample.get('error_type')
    if isinstance(error, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', error):
        clean['error_type'] = error
    identifiers = sample.get('request_ids')
    if isinstance(identifiers, dict):
        safe_ids = {}
        for key, value in identifiers.items():
            if (isinstance(key, str) and key.lower() in REQUEST_ID_KEYS and
                    isinstance(value, str) and
                    re.fullmatch(r'[A-Za-z0-9._:/+=-]{1,256}', value)):
                safe_ids[key.lower()] = value
        if safe_ids:
            clean['request_ids'] = safe_ids
    return clean


class ReadDiagnostics:
    MAX_BYTES = 2 * 1024 * 1024

    def __init__(self, path):
        self.path = Path(path).absolute()
        self.backups = tuple(self.path.with_name(self.path.name + suffix) for suffix in ('.1', '.2'))
        self.lock_path = self.path.with_name(self.path.name + '.lock')

    @staticmethod
    def _check_file(path):
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError(errno.EINVAL, 'diagnostic target must be a regular single-link file', str(path))
        return info

    def _validate_paths(self):
        for directory in self.path.parents:
            if directory.is_symlink():
                raise OSError(errno.ELOOP, 'diagnostic directory symlink refused', str(directory))
        for path in (self.path, *self.backups, self.lock_path):
            info = self._check_file(path)
            limit = 1 if path == self.lock_path else self.MAX_BYTES
            if info and info.st_size > limit:
                raise OSError(errno.EFBIG, 'existing diagnostic file exceeds size limit', str(path))

    @contextlib.contextmanager
    def _lock(self):
        flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        fd = os.open(self.lock_path, flags, 0o600)
        acquired = False
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1:
                raise OSError(errno.EINVAL, 'invalid diagnostic lock target')
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            if os.name == 'posix':
                os.fchmod(fd, 0o600)
            if os.fstat(fd).st_size == 0:
                if os.write(fd, b'\0') != 1:
                    raise OSError(errno.EIO, 'diagnostic lock initialization failed')
            yield
        finally:
            try:
                if acquired:
                    if os.name == 'nt':
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _open(self):
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        flags |= getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError(errno.EINVAL, 'diagnostic target must be a regular single-link file')
            if os.name == 'posix':
                os.fchmod(fd, 0o600)
            return fd, info.st_size
        except BaseException:
            os.close(fd)
            raise

    def record(self, sample):
        data = (json.dumps(sanitize_sample(sample), separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
        if len(data) > self.MAX_BYTES:
            raise OSError(errno.EFBIG, 'diagnostic sample exceeds size limit')
        self._validate_paths()
        with self._lock():
            self._validate_paths()
            self._append(data)

    def _append(self, data):
        directory_changed = not self.path.exists()
        fd, size = self._open()
        try:
            if size + len(data) > self.MAX_BYTES:
                os.close(fd)
                fd = None
                # replace/unlink act on directory entries; they never follow a
                # symlink. Validate all destinations before changing any file.
                self._validate_paths()
                if self.backups[1].exists():
                    self.backups[1].unlink()
                if self.backups[0].exists():
                    os.replace(self.backups[0], self.backups[1])
                os.replace(self.path, self.backups[0])
                directory_changed = True
                fd, _ = self._open()
            remaining = memoryview(data)
            while remaining:
                written = os.write(fd, remaining)
                if not written:
                    raise OSError(errno.EIO, 'diagnostic write made no progress')
                remaining = remaining[written:]
            os.fsync(fd)
            if directory_changed and os.name == 'posix':
                directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if fd is not None:
                os.close(fd)

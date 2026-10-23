"""Save a Healthchecks ping credential from a hidden, human terminal prompt.

No HTTP, trading imports, service control, or trading state writes.
"""
import getpass
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import warnings
from urllib.parse import urlsplit
from uuid import UUID

DIRECTORY = Path('/etc/bb-squeeze-alerts')
CREDENTIAL = DIRECTORY / 'healthchecks.uuid'


def parse_ping_uuid(value):
    value = value.strip()
    if value.startswith('https://'):
        parts = urlsplit(value)
        if (parts.scheme != 'https' or parts.netloc != 'hc-ping.com'
                or parts.query or parts.fragment or not parts.path.startswith('/')):
            raise ValueError('invalid credential format')
        value = parts.path[1:]
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        raise ValueError('invalid credential format') from None
    if not parsed.int or str(parsed) != value.lower():
        raise ValueError('invalid credential format')
    return str(parsed)


def save_credential(directory, value):
    directory = Path(directory)
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError('configuration directory ownership or permissions invalid')
    target = directory / 'healthchecks.uuid'
    if target.is_symlink():
        raise ValueError('credential path must not be a symbolic link')
    descriptor, name = tempfile.mkstemp(prefix='.credential-', dir=directory)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w', encoding='ascii') as stream:
            stream.write(parse_ping_uuid(value) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, target)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main():
    if not hasattr(os, 'geteuid') or os.geteuid() != 0:
        print(json.dumps({'configured': False, 'reason': 'root terminal required'}))
        return 1
    try:
        if not sys.stdin.isatty():
            raise ValueError('interactive terminal required')
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            value = getpass.getpass('Healthchecks Ping URL (hidden; not stored in shell history): ')
        save_credential(DIRECTORY, value)
    except (OSError, ValueError, EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        print(json.dumps({'configured': False, 'reason': 'credential not saved; input or storage validation failed'}))
        return 1
    print(json.dumps({'configured': True, 'credential_printed': False,
                      'notification_sent': False, 'trading_changed': False}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())


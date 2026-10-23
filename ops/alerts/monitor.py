"""Read-only trading observer. No trading imports, credentials, or controls.

Healthchecks receives only a fixed label and allowlisted reason codes. Its
missing-ping detector is independent of this VPS. This observer never clears
pauses, certifies returns, or reconstructs the runner's recovery algorithm.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import http.client
import json
import os
from pathlib import Path
import re
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import uuid

STALE_MS = 180_000
RECOVERY_PERMISSION_MS = 90_000
BAR_MS = 4 * 60 * 60 * 1000
BAR_GRACE_MS = 60_000
SNAPSHOT_SAVE_ALLOWANCE_MS = 60_000
MAX_JSON_BYTES = 8 * 1024 * 1024
SEND_TIMEOUT_SECONDS = 10
SERVICE = 'bb-squeeze-live.service'
HEALTH_TIMER = 'bb-squeeze-live-health.timer'
HEALTH_SERVICE = 'bb-squeeze-live-health.service'
OWNER = 'exchange-recovery-v1'
LABEL = 'trading-monitor'
REASONS = frozenset({
    'CONFIG_INVALID', 'STATE_MISSING', 'STATE_MALFORMED', 'STATE_TOO_LARGE',
    'STATE_UNREADABLE', 'STATE_STALE', 'STATE_FUTURE', 'STATE_INCONSISTENT',
    'SERVICE_UNAVAILABLE', 'SERVICE_INACTIVE', 'SERVICE_IDENTITY_MISMATCH',
    'HEALTH_TIMER_INACTIVE', 'HEALTH_SERVICE_FAILED', 'HEALTH_SERVICE_STALE',
    'ACTIVATION_INVALID', 'OWNERSHIP_MISMATCH',
    'STRATEGY_HALTED', 'ENTRIES_PAUSED', 'EXCHANGE_PAUSED', 'RECOVERY_NOT_READY',
    'RECOVERY_STALE', 'STORAGE_ERROR', 'PENDING_OPERATION', 'UNRESOLVED_ORDERS',
    'RESIDUAL_EXIT', 'ORPHAN_ORDERS', 'DATA_GAP', 'CURRENT_COVERAGE_GAP',
    'PROTECTION_UNSAFE', 'EXECUTION_UNVERIFIED', 'PUBLICATION_PENDING',
    'HISTORY_UNVERIFIED', 'BAR_NOT_CURRENT', 'MONITOR_ERROR',
    'MONITOR_STORAGE_ERROR', 'DELIVERY_FAILED', 'DELIVERY_RATE_LIMITED',
    'MONITOR_BUSY', 'TEST_ALERT',
})


class MonitorError(Exception):
    def __init__(self, code):
        self.code = code if code in REASONS else 'MONITOR_ERROR'
        super().__init__(self.code)


@dataclass(frozen=True)
class Config:
    state_dir: Path
    output_dir: Path
    ping_uuid_file: Path
    expected_fingerprint: str
    expected_account_index: int
    service_unit: str = SERVICE
    health_timer_unit: str = HEALTH_TIMER


def _read(path, limit, *, secret=False):
    """Bounded regular-file read; no FIFOs, symlinks, or raw error messages."""
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                     | getattr(os, 'O_NONBLOCK', 0))
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or Path(path).is_symlink():
            raise MonitorError('STATE_UNREADABLE')
        if secret and os.name == 'posix' and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077):
            raise MonitorError('CONFIG_INVALID')
        if info.st_size > limit:
            raise MonitorError('STATE_TOO_LARGE')
        with os.fdopen(fd, 'rb') as handle:
            fd = None
            raw = handle.read(limit + 1)
        if len(raw) > limit:
            raise MonitorError('STATE_TOO_LARGE')
        return raw
    except FileNotFoundError:
        raise MonitorError('STATE_MISSING') from None
    except OSError:
        raise MonitorError('STATE_UNREADABLE') from None
    finally:
        if fd is not None:
            os.close(fd)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def read_json(path, limit=MAX_JSON_BYTES, *, secret=False):
    raw = _read(path, limit, secret=secret)
    try:
        value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise MonitorError('STATE_MALFORMED') from None


def load_config(path):
    try:
        value = read_json(path, 65536, secret=True)
        required = {'version', 'state_dir', 'output_dir', 'ping_uuid_file',
                    'expected_fingerprint', 'expected_account_index'}
        if set(value) - required - {'service_unit', 'health_timer_unit'} or not required <= set(value):
            raise ValueError()
        if type(value['version']) is not int or value['version'] != 1:
            raise ValueError()
        paths = {}
        for key in ('state_dir', 'output_dir', 'ping_uuid_file'):
            if not isinstance(value[key], str) or not Path(value[key]).is_absolute():
                raise ValueError()
            paths[key] = Path(value[key]).resolve()
        a, b = paths['state_dir'], paths['output_dir']
        if a == b or a in b.parents or b in a.parents:
            raise ValueError()
        if paths['ping_uuid_file'] == b or b in paths['ping_uuid_file'].parents:
            raise ValueError()
        fingerprint = value['expected_fingerprint']
        if not isinstance(fingerprint, str) or re.fullmatch('[0-9a-f]{64}', fingerprint) is None:
            raise ValueError()
        if type(value['expected_account_index']) is not int or value['expected_account_index'] < 0:
            raise ValueError()
        # Explicitly fixed units prevent a config from broadening command scope.
        if value.get('service_unit', SERVICE) != SERVICE or value.get('health_timer_unit', HEALTH_TIMER) != HEALTH_TIMER:
            raise ValueError()
        return Config(**paths, expected_fingerprint=fingerprint,
                      expected_account_index=value['expected_account_index'])
    except (MonitorError, ValueError, TypeError, OSError):
        raise MonitorError('CONFIG_INVALID') from None


def _uuid(value):
    if not isinstance(value, str) or re.fullmatch('[0-9a-fA-F-]{36}', value) is None:
        raise MonitorError('CONFIG_INVALID')
    parsed = uuid.UUID(value)
    if not parsed.int or str(parsed) != value.lower():
        raise MonitorError('CONFIG_INVALID')
    return str(parsed)


def load_uuid(config):
    try:
        return _uuid(_read(config.ping_uuid_file, 128, secret=True).decode('ascii').strip())
    except (MonitorError, ValueError, UnicodeError):
        raise MonitorError('CONFIG_INVALID') from None


def probe_services(config):
    """Read fixed systemd properties. Bounded wall time, no shell or controls."""
    try:
        result = subprocess.run(
            ['systemctl', 'show', config.service_unit, config.health_timer_unit, HEALTH_SERVICE,
             '--property=Id,ActiveState,SubState,MainPID,Result,ExecMainStatus,ExecMainExitTimestampMonotonic', '--no-pager'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
            check=False, env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
        if result.returncode or len(result.stdout) > 8192:
            raise ValueError()
        blocks = {}
        for block in result.stdout.decode('ascii').strip().split('\n\n'):
            fields = dict(line.split('=', 1) for line in block.splitlines())
            name = fields.get('Id')
            if name in blocks or name not in (config.service_unit, config.health_timer_unit, HEALTH_SERVICE):
                raise ValueError()
            blocks[name] = fields
        service, timer, health = blocks[config.service_unit], blocks[config.health_timer_unit], blocks[HEALTH_SERVICE]
        # Linux systemd and Python use CLOCK_MONOTONIC here. Unlike wall time,
        # this cannot mistake an old pre-adjustment clock value for fresh work.
        exit_us = int(health['ExecMainExitTimestampMonotonic'])
        exit_age_us = time.monotonic_ns() // 1000 - exit_us
        health_ok = (health.get('Result') == 'success' and int(health['ExecMainStatus']) == 0
                     and health.get('ActiveState') in ('inactive', 'active', 'activating'))
        return {'active': service.get('ActiveState') == 'active' and service.get('SubState') == 'running',
                'pid': int(service['MainPID']),
                'timer_active': timer.get('ActiveState') == 'active',
                'health_service_ok': health_ok,
                'health_service_fresh': exit_us > 0 and 0 <= exit_age_us <= STALE_MS * 1000}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, UnicodeError):
        raise MonitorError('SERVICE_UNAVAILABLE') from None


def _markers(directory):
    seen = {}
    for name in ('ENTRY_PAUSED', 'EXCHANGE_PAUSED', 'HALT'):
        path = directory / name
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise MonitorError('STATE_UNREADABLE') from None
        if name == 'HALT':
            _read(path, 65536)
            seen[name] = {}
        else:
            seen[name] = read_json(path, 65536)
    return seen


def evaluate(config, *, now_ms=None, service_probe=probe_services):
    """Return only a non-sensitive health decision and reason codes."""
    codes = set()
    try:
        before = _markers(config.state_dir)
        activation = read_json(config.state_dir / 'activation.json', 65536)
        snapshot = read_json(config.state_dir / 'snapshot.json')
        report = read_json(config.state_dir / 'report.json')
        runner = read_json(config.state_dir / 'runner_status.json')
        service = service_probe(config)
        after = _markers(config.state_dir)
        # Sample after all reads: a concurrent atomic save is not a future time.
        now = int(time.time() * 1000) if now_ms is None else now_ms
        if type(now) is not int or now <= 0:
            raise MonitorError('MONITOR_ERROR')

        def recent(value, age=STALE_MS, stale='STATE_STALE'):
            if type(value) is not int or value <= 0:
                codes.add('STATE_MALFORMED')
            elif value > now:
                codes.add('STATE_FUTURE')
            elif now - value > age:
                codes.add(stale)

        def empty(obj, key, expected, code, *, nullable=False):
            value = obj.get(key, object())
            if nullable and value is None:
                return
            if not isinstance(value, expected):
                codes.add('STATE_MALFORMED')
            elif value:
                codes.add(code)

        def boolean(obj, key, expected, code):
            value = obj.get(key)
            if type(value) is not bool:
                codes.add('STATE_MALFORMED')
            elif value != expected:
                codes.add(code)

        if service.get('active') is not True:
            codes.add('SERVICE_INACTIVE')
        if service.get('timer_active') is not True:
            codes.add('HEALTH_TIMER_INACTIVE')
        if service.get('health_service_ok') is not True:
            codes.add('HEALTH_SERVICE_FAILED')
        if service.get('health_service_fresh') is not True:
            codes.add('HEALTH_SERVICE_STALE')
        if type(runner.get('pid')) is not int or runner['pid'] <= 0 or runner['pid'] != service.get('pid'):
            codes.add('SERVICE_IDENTITY_MISMATCH')
        if runner.get('mode') != 'live' or report.get('classification') != 'authenticated_account_ledger':
            codes.add('OWNERSHIP_MISMATCH')
        if (activation.get('approved') is not True or activation.get('automatic_recovery_enabled') is not True):
            codes.add('ACTIVATION_INVALID')
        if (activation.get('fingerprint') != config.expected_fingerprint
                or type(activation.get('account_index')) is not int
                or activation['account_index'] != config.expected_account_index):
            codes.add('OWNERSHIP_MISMATCH')
        if (type(activation.get('started_at_ms')) is not int
                or type(report.get('start_ms')) is not int
                or activation['started_at_ms'] <= 0
                or activation['started_at_ms'] != report['start_ms']):
            codes.add('OWNERSHIP_MISMATCH')
        elif activation['started_at_ms'] > now:
            codes.add('STATE_FUTURE')
        for value in (runner.get('updated_ms'), report.get('updated_ms'),
                      runner.get('last_successful_poll_ms'), runner.get('last_successful_service_ms')):
            recent(value)
        for obj in (runner, snapshot):
            boolean(obj, 'halted', False, 'STRATEGY_HALTED')
            empty(obj, 'pending_operation', dict, 'PENDING_OPERATION', nullable=True)
            for key, code in (('unresolved_operations', 'UNRESOLVED_ORDERS'),
                              ('residual_exits', 'RESIDUAL_EXIT'), ('orphan_orders', 'ORPHAN_ORDERS')):
                empty(obj, key, dict, code)
            empty(obj, 'data_gaps', list, 'DATA_GAP')
            boolean(obj, 'publication_pending', False, 'PUBLICATION_PENDING')
        for key in ('positions', 'orders'):
            if not isinstance(snapshot.get(key), dict):
                codes.add('STATE_MALFORMED')
        boolean(runner, 'entries_paused', False, 'ENTRIES_PAUSED')
        if 'api_read_diagnostics_error' not in runner:
            codes.add('STATE_MALFORMED')
        elif runner['api_read_diagnostics_error'] is not None:
            codes.add('STORAGE_ERROR')
        for key, code in (('execution_changes_verified', 'EXECUTION_UNVERIFIED'),
                          ('owned_exposure_matches_account', 'OWNERSHIP_MISMATCH'),
                          ('historical_evidence_complete', 'HISTORY_UNVERIFIED')):
            boolean(report, key, True, code)
        boolean(report, 'trader_halted', False, 'STRATEGY_HALTED')
        boolean(report, 'publication_pending', False, 'PUBLICATION_PENDING')
        empty(report, 'unsafe_protection', dict, 'PROTECTION_UNSAFE')
        empty(report, 'unresolved_orders', dict, 'UNRESOLVED_ORDERS')
        empty(report, 'unresolved_operations', dict, 'UNRESOLVED_ORDERS')
        empty(report, 'strategy_data_gaps', list, 'DATA_GAP')
        empty(report, 'historical_issues', list, 'HISTORY_UNVERIFIED')
        history = report.get('history_collection')
        if not isinstance(history, dict):
            codes.add('STATE_MALFORMED')
        else:
            boolean(history, 'head_complete', True, 'HISTORY_UNVERIFIED')
        for obj in (runner, report):
            coverage = obj.get('runtime_coverage')
            if not isinstance(coverage, dict):
                codes.add('STATE_MALFORMED')
                continue
            recent(coverage.get('updated_ms'))
            empty(coverage, 'active_gap_streams', list, 'CURRENT_COVERAGE_GAP')
            streams = coverage.get('streams')
            if not isinstance(streams, dict):
                codes.add('STATE_MALFORMED')
                continue
            for name in ('poll', 'service', 'bar', 'observation'):
                stream = streams.get(name)
                if not isinstance(stream, dict) or 'active_gap' not in stream:
                    codes.add('STATE_MALFORMED')
                elif stream['active_gap'] is not None:
                    codes.add('CURRENT_COVERAGE_GAP')
        recovery = runner.get('automatic_recovery')
        if not isinstance(recovery, dict):
            codes.add('RECOVERY_NOT_READY')
        else:
            if (recovery.get('owner') != OWNER or recovery.get('fingerprint') != config.expected_fingerprint
                    or not isinstance(recovery.get('session'), str) or not recovery['session']):
                codes.add('OWNERSHIP_MISMATCH')
            if (recovery.get('enabled') is not True or recovery.get('state') != 'ready'
                    or recovery.get('phase') != 'running'):
                codes.add('RECOVERY_NOT_READY')
            empty(recovery, 'blockers', list, 'RECOVERY_NOT_READY')
            if 'storage_error' not in recovery:
                codes.add('STATE_MALFORMED')
            elif recovery['storage_error'] is not None:
                codes.add('STORAGE_ERROR')
            # Read the published permission's freshness; never count cycles or
            # release a marker here. Existing can_enter expires after 90 s.
            recent(recovery.get('last_cycle_ms'), RECOVERY_PERMISSION_MS, 'RECOVERY_STALE')
        for markers in (before, after):
            if 'HALT' in markers:
                codes.add('STRATEGY_HALTED')
            if 'ENTRY_PAUSED' in markers:
                codes.add('ENTRIES_PAUSED')
            if 'EXCHANGE_PAUSED' in markers:
                codes.add('EXCHANGE_PAUSED')
                marker = markers['EXCHANGE_PAUSED']
                if (not isinstance(recovery, dict) or marker.get('owner') != OWNER
                        or marker.get('fingerprint') != config.expected_fingerprint
                        or marker.get('session') != recovery.get('session')):
                    codes.add('OWNERSHIP_MISMATCH')
        if before != after:
            codes.add('STATE_INCONSISTENT')
        expected = now // BAR_MS * BAR_MS - BAR_MS
        since_boundary = now - (expected + BAR_MS)
        for name, obj in (('runner', runner), ('snapshot', snapshot)):
            bar = obj.get('last_bar_ms')
            if type(bar) is not int or bar <= 0 or bar % BAR_MS:
                codes.add('STATE_MALFORMED')
            elif bar > expected:
                codes.add('STATE_FUTURE')
            elif bar != expected:
                allowance = BAR_GRACE_MS
                if name == 'snapshot' and runner.get('last_bar_ms') == expected:
                    allowance += SNAPSHOT_SAVE_ALLOWANCE_MS
                if bar != expected - BAR_MS or since_boundary >= allowance:
                    codes.add('BAR_NOT_CURRENT')
        # Historical last_error, coverage_complete=false, observations=0 and
        # missing trade-fee performance certification are intentionally ignored.
        return {'healthy': not codes, 'reason_codes': sorted(codes), 'checked_at_ms': now}
    except MonitorError as exc:
        codes.add(exc.code)
    except Exception:
        codes.add('MONITOR_ERROR')
    return {'healthy': False, 'reason_codes': sorted(codes),
            'checked_at_ms': int(time.time() * 1000) if now_ms is None else now_ms}


def _body(healthy, codes):
    if type(healthy) is not bool or not isinstance(codes, list) or any(c not in REASONS for c in codes):
        raise MonitorError('MONITOR_ERROR')
    return json.dumps({'monitor': LABEL, 'state': 'up' if healthy else 'down',
                       'reason_codes': sorted(set(codes))}, separators=(',', ':')).encode('ascii')


def _http_ping(identifier, healthy, codes):
    """Worker-only HTTP. Fixed HTTPS origin, TLS validation, no redirects."""
    connection = None
    try:
        identifier = _uuid(identifier)
        body = _body(healthy, codes)
        connection = http.client.HTTPSConnection('hc-ping.com', 443, timeout=5,
                                                 context=ssl.create_default_context())
        connection.request('POST', '/' + identifier + ('' if healthy else '/fail'),
                           body=body, headers={'Content-Type': 'application/json',
                                               'User-Agent': 'trading-monitor/1'})
        response = connection.getresponse()
        # HTTP 200 alone may mean "OK (not found)" or "OK (rate limited)".
        return response.status == 200 and response.read(65) == b'OK'
    except Exception:
        return False
    finally:
        if connection is not None:
            connection.close()


def _ping_worker():
    try:
        data = json.loads(sys.stdin.buffer.read(4097))
        ok = _http_ping(data['uuid'], data['healthy'], data['reason_codes'])
    except Exception:
        ok = False
    sys.stdout.write('ACK' if ok else 'FAILED')
    return 0 if ok else 1


def send_ping(config, healthy, codes):
    """Bound DNS, connect and response together; UUID never appears in argv."""
    process = None
    try:
        payload = json.dumps({'uuid': load_uuid(config), 'healthy': healthy,
                              'reason_codes': codes}, separators=(',', ':')).encode()
        _body(healthy, codes)
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--ping-worker'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={'PATH': os.environ.get('PATH', ''), 'SYSTEMROOT': os.environ.get('SYSTEMROOT', ''),
                 'PYTHONDONTWRITEBYTECODE': '1'})
        stdout, _ = process.communicate(payload, timeout=SEND_TIMEOUT_SECONDS)
        return process.returncode == 0 and stdout == b'ACK'
    except subprocess.TimeoutExpired:
        if process is not None:
            process.kill()
            process.communicate()
        return False
    except Exception:
        return False


def _atomic(path, value):
    raw = (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()
    fd, temp = tempfile.mkstemp(prefix='.monitor-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        if os.name == 'posix':
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


@contextmanager
def own_lock(config):
    directory = config.output_dir
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.stat()
    if directory.is_symlink() or (os.name == 'posix' and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022)):
        raise MonitorError('MONITOR_STORAGE_ERROR')
    fd = os.open(directory / 'monitor.lock', os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    locked = False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise MonitorError('MONITOR_STORAGE_ERROR')
        try:
            if os.name == 'posix':
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt
                os.write(fd, b'0')
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            locked = True
        except OSError:
            raise MonitorError('MONITOR_BUSY') from None
        yield
    finally:
        if locked:
            if os.name == 'posix':
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
            else:
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)


def read_status(config):
    value = read_json(config.output_dir / 'status.json', 65536)
    # Only our closed schema can be echoed, even if a local file was replaced.
    if (set(value) != {'version', 'healthy', 'reason_codes', 'checked_at_ms', 'delivery', 'test_notification', 'telegram_delivery_verified'}
            or value['version'] != 1 or type(value['healthy']) is not bool
            or type(value['checked_at_ms']) is not int or type(value['test_notification']) is not bool
            or value['telegram_delivery_verified'] is not False
            or value['delivery'] not in ('pending', 'ping_accepted', 'failed', 'rate_limited')):
        raise MonitorError('STATE_MALFORMED')
    _body(value['healthy'], value['reason_codes'])
    return value


def status_view(config, *, now_ms=None):
    """Expose saved status as a dated observation, never an eternal Up."""
    value = read_status(config)
    now = int(time.time() * 1000) if now_ms is None else now_ms
    age = now - value['checked_at_ms']
    current = 0 <= age <= STALE_MS
    value.update(last_observation_age_ms=age, current_observation=current)
    if not current:
        code = 'STATE_FUTURE' if age < 0 else 'STATE_STALE'
        value.update(healthy=False, reason_codes=sorted(set(value['reason_codes']) | {code}))
    return value


def _reserve_ping(config, now_ms):
    path = config.output_dir / 'send-window.json'
    if path.exists():
        window = read_json(path, 8192)
        stamps = window.get('attempts_ms')
        if (set(window) != {'attempts_ms'} or not isinstance(stamps, list) or len(stamps) > 5
                or any(type(t) is not int or t <= 0 or t > now_ms for t in stamps)):
            raise MonitorError('MONITOR_STORAGE_ERROR')
    else:
        stamps = []
    stamps = [t for t in stamps if now_ms - t < 60_000]
    if len(stamps) >= 5:
        return False
    _atomic(path, {'attempts_ms': stamps + [now_ms]})
    return True


def _event(config, value, previous):
    if previous and all(previous.get(k) == value[k] for k in ('healthy', 'reason_codes', 'delivery', 'test_notification')):
        return
    path = config.output_dir / 'events.jsonl'
    if path.exists() and path.stat().st_size > 1024 * 1024:
        os.replace(path, config.output_dir / 'events.previous.jsonl')
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'ab') as handle:
        handle.write((json.dumps(value, sort_keys=True) + '\n').encode())
        handle.flush()
        os.fsync(handle.fileno())


def run_once(config, *, test_notification=False, service_probe=probe_services, sender=send_ping, now_ms=None):
    with own_lock(config):
        try:
            previous = read_status(config)
        except MonitorError:
            previous = None
        result = evaluate(config, now_ms=now_ms, service_probe=service_probe)
        if test_notification:
            result.update(healthy=False, reason_codes=['TEST_ALERT'])
        value = {'version': 1, **result, 'delivery': 'pending', 'test_notification': test_notification,
                 'telegram_delivery_verified': False}
        try:
            _atomic(config.output_dir / 'status.json', value)
            allowed = _reserve_ping(config, value['checked_at_ms'])
        except (OSError, MonitorError):
            value.update(healthy=False, reason_codes=sorted(set(value['reason_codes']) | {'MONITOR_STORAGE_ERROR'}),
                         delivery='failed')
            # With unreliable durable rate accounting, do not risk a send storm.
            return value
        if not allowed:
            value['delivery'] = 'rate_limited'
        else:
            try:
                accepted = sender(config, value['healthy'], value['reason_codes'])
            except Exception:
                accepted = False
            value['delivery'] = 'ping_accepted' if accepted is True else 'failed'
        try:
            _atomic(config.output_dir / 'status.json', value)
            _event(config, value, previous)
        except OSError:
            value.update(healthy=False, reason_codes=sorted(set(value['reason_codes']) | {'MONITOR_STORAGE_ERROR'}))
        return value


def main(argv=None):
    if argv is None and sys.argv[1:] == ['--ping-worker']:
        return _ping_worker()
    parser = argparse.ArgumentParser(description='Read-only trading health observer; sanitized output only.')
    parser.add_argument('--config', required=True, type=Path)
    group = parser.add_mutually_exclusive_group(required=True)
    for name in ('once', 'check-config', 'status', 'inspect', 'test-notification'):
        group.add_argument('--' + name, action='store_true')
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.check_config:
            load_uuid(config)
            result = {'config_valid': True}
        elif args.status:
            result = status_view(config)
        elif args.inspect:
            result = evaluate(config)
        else:
            result = run_once(config, test_notification=args.test_notification)
        print(json.dumps(result, sort_keys=True))
        success = (result.get('config_valid')
                   or (args.test_notification and result.get('delivery') == 'ping_accepted')
                   or (result.get('healthy') and result.get('delivery', 'ping_accepted') == 'ping_accepted'))
        return 0 if success else 1
    except Exception as exc:
        code = exc.code if isinstance(exc, MonitorError) else 'MONITOR_ERROR'
        print(json.dumps({'healthy': False, 'reason_codes': [code]}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

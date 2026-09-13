"""Explicit, offline operational repair of an existing paper campaign.

Stop the systemd service first (do not request the supervisor's terminal STOP).
The expected manifest SHA256 is the digest of the existing campaign.json bytes.
This tool never edits trading state, settings, start time or deadline. Each
upgrade AND rollback leaves a new before/after audit directory. If interrupted,
compare campaign.json with that directory's two manifests before any recovery;
never restore an old trading ledger to undo a code change.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from live import supervisor as s  # noqa: E402
from live.process_lock import ProcessLock  # noqa: E402

OPERATIONAL_PATHS = frozenset({
    'src/live/feed.py', 'src/live/run.py', 'src/live/runtime_health.py',
    'src/live/revision.py',
})


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _encoded(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + '\n').encode('utf-8')


def _write_new(path: Path, data: bytes) -> None:
    with path.open('xb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _sync_directory(path: Path) -> None:
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def state_hashes(directory: Path) -> dict:
    """Hash durable data; the held OS lock is coordination, not ledger content."""
    result = {}
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():
            raise ValueError('state must not contain symlinks during maintenance')
        if path == directory / 'runner.lock':
            # Windows byte-range locks prohibit even our second read handle.
            continue
        if path.is_file():
            result[path.relative_to(directory).as_posix()] = s.digest(path)
    return result


def verify_checkout(root: Path, expected_git: str, manifest: dict) -> None:
    def git(*args):
        return subprocess.run(
            ['git', '-c', f'safe.directory={root.as_posix()}', *args],
            cwd=root, check=True, capture_output=True, text=True, timeout=30,
        ).stdout.strip()

    if git('rev-parse', 'HEAD') != expected_git:
        raise ValueError('checkout HEAD differs from expected new commit')
    if git('status', '--porcelain', '--untracked-files=all'):
        raise ValueError('checkout must be clean before a revision')
    tracked = set(git('ls-files').splitlines())
    if not set(manifest['source_sha256']).issubset(tracked):
        raise ValueError('execution manifest contains an untracked source file')


def revise(campaign_dir: Path, *, expected_campaign_id: str,
           expected_manifest_sha256: str, expected_old_git: str,
           expected_new_git: str, reason: str, operation: str = 'upgrade',
           root: Path = s.ROOT, now: float | None = None) -> dict:
    """Authorize only the exact inspected source transition under both locks."""
    campaign_dir, root = campaign_dir.resolve(), root.resolve()
    if operation not in ('upgrade', 'rollback') or not reason.strip():
        raise ValueError('a revision needs an upgrade/rollback operation and reason')
    for value in (expected_old_git, expected_new_git):
        if not re.fullmatch(r'[0-9a-f]{40}', value):
            raise ValueError('expected commits must be full lowercase Git SHA values')
    if not re.fullmatch(r'[0-9a-f]{64}', expected_manifest_sha256):
        raise ValueError('expected manifest digest must be a SHA256 value')
    manifest_path = campaign_dir / 'campaign.json'
    state_dir = campaign_dir / 'state'
    # Never create a campaign, state directory, or missing writer locks here.
    for path in (manifest_path, campaign_dir / 'supervisor.lock',
                 state_dir / 'runner.lock', campaign_dir / 'status.json'):
        if not path.is_file() or path.is_symlink():
            raise ValueError('revision requires an existing initialized campaign and locks')

    with ProcessLock(campaign_dir / 'supervisor.lock'), ProcessLock(state_dir / 'runner.lock'):
        before_bytes = manifest_path.read_bytes()
        if _sha(before_bytes) != expected_manifest_sha256:
            raise ValueError('campaign manifest differs from the inspected digest')
        before = json.loads(before_bytes)
        status = s.read_json(campaign_dir / 'status.json')
        if before.get('mode') != 'paper' or before.get('schema_version') != 1:
            raise ValueError('only a recognized paper campaign can be revised')
        if (before.get('campaign_id') != expected_campaign_id or
                before.get('campaign_dir') != str(campaign_dir)):
            raise ValueError('campaign identity or original directory differs')
        started, deadline = s.parse_utc(before['started_at']), s.parse_utc(before['deadline'])
        observed_now = time.time() if now is None else now
        if (deadline - started != s.CAMPAIGN_SECONDS or
                before.get('duration_seconds') != s.CAMPAIGN_SECONDS or
                not started <= observed_now < deadline):
            raise ValueError('campaign period changed or is not currently open')
        if (status.get('phase') != 'paused' or status.get('forced_stop') is not False or
                s.previous_child_is_alive(status) or (campaign_dir / 'STOP').exists()):
            raise ValueError('campaign must be gracefully paused with no worker or STOP request')
        if before.get('git_revision') != expected_old_git:
            raise ValueError('recorded old commit differs from expected old commit')

        frozen_path = campaign_dir / 'config.frozen.json'
        if s.digest(frozen_path) != before['config_sha256']:
            raise ValueError('frozen configuration changed')
        frozen = s.read_json(frozen_path)
        if frozen.get('mode') != 'paper':
            raise ValueError('frozen configuration is not paper mode')
        candidate = s.frozen_config(root / 'paper.config.json', state_dir)
        if candidate != frozen:
            raise ValueError('resolved configuration changed; operational repair cannot migrate settings')
        description = dict(frozen)
        description.pop('state_dir', None)
        identity = s.read_json(state_dir / 'identity.json')
        expected_identity = _sha(json.dumps(description, sort_keys=True).encode())
        if (identity.get('mode') != 'paper' or identity.get('config_sha256') != expected_identity or
                identity.get('config') != description):
            raise ValueError('state configuration identity differs from the frozen settings')

        old_execution = before['execution_manifest']
        new_execution = s.execution_manifest(root)
        if ({k: v for k, v in old_execution.items() if k != 'source_sha256'} !=
                {k: v for k, v in new_execution.items() if k != 'source_sha256'}):
            raise ValueError('runtime or dependency version changes are not permitted')
        old_sources, new_sources = old_execution['source_sha256'], new_execution['source_sha256']
        changed = sorted(path for path in old_sources.keys() | new_sources.keys()
                         if old_sources.get(path) != new_sources.get(path))
        if not changed or not set(changed).issubset(OPERATIONAL_PATHS):
            raise ValueError(f'changed source paths are outside the operational repair allowlist: {changed}')
        verify_checkout(root, expected_new_git, new_execution)

        unchanged_state = state_hashes(state_dir)
        unchanged_config = s.digest(frozen_path)
        unchanged_status = s.digest(campaign_dir / 'status.json')
        after = dict(before)
        after.update(execution_manifest=new_execution, git_revision=expected_new_git)
        after_bytes = _encoded(after)
        revision_id = f'{int(observed_now)}-{uuid.uuid4().hex}'
        revisions_dir = campaign_dir / 'revisions'
        if revisions_dir.is_symlink():
            raise ValueError('revision audit directory must not be a symlink')
        revisions_dir.mkdir(exist_ok=True)
        audit_dir = revisions_dir / revision_id
        audit_dir.mkdir()
        record = {
            'schema_version': 1, 'revision_id': revision_id, 'operation': operation,
            'reason': reason.strip(), 'recorded_at': s.utc_text(observed_now),
            'campaign_id': expected_campaign_id, 'started_at': before['started_at'],
            'deadline': before['deadline'], 'paused_status': status,
            'old_git_revision': expected_old_git, 'new_git_revision': expected_new_git,
            'before_manifest_sha256': _sha(before_bytes), 'after_manifest_sha256': _sha(after_bytes),
            'old_execution_sha256': _sha(_encoded(old_execution)),
            'new_execution_sha256': _sha(_encoded(new_execution)),
            'changed_paths': {path: {'before': old_sources.get(path), 'after': new_sources.get(path)}
                              for path in changed},
            'config_sha256': unchanged_config, 'state_file_sha256': unchanged_state,
            'measurement_note': 'maintenance gap is not observed market time; trading ledger is unchanged',
        }
        _write_new(audit_dir / 'campaign.before.json', before_bytes)
        _write_new(audit_dir / 'campaign.after.json', after_bytes)
        _write_new(audit_dir / 'prepared.json', _encoded(record))
        _sync_directory(audit_dir)
        _sync_directory(revisions_dir)

        def assert_unchanged():
            if (s.digest(frozen_path) != unchanged_config or
                    s.digest(campaign_dir / 'status.json') != unchanged_status or
                    state_hashes(state_dir) != unchanged_state):
                raise ValueError('configuration, paused status or trading state changed during revision')

        assert_unchanged()
        if manifest_path.read_bytes() != before_bytes:
            raise ValueError('campaign manifest changed during revision')
        # The supervisor continues to enforce an exact execution fingerprint.
        # Only this explicit transaction changes that fingerprint, with originals kept.
        s.atomic_json(manifest_path, after)
        assert_unchanged()
        if manifest_path.read_bytes() != after_bytes:
            raise ValueError('published campaign manifest differs from the prepared revision')
        receipt = {'revision_id': revision_id, 'committed_at': s.utc_text(time.time()),
                   'manifest_sha256': s.digest(manifest_path),
                   'prepared_sha256': s.digest(audit_dir / 'prepared.json')}
        _write_new(audit_dir / 'committed.json', _encoded(receipt))
        _sync_directory(audit_dir)
        return {'revision_id': revision_id, 'audit_directory': str(audit_dir),
                'changed_paths': changed, **receipt}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--campaign-dir', required=True, type=Path)
    parser.add_argument('--expected-campaign-id', required=True)
    parser.add_argument('--expected-manifest-sha256', required=True)
    parser.add_argument('--expected-old-git', required=True)
    parser.add_argument('--expected-new-git', required=True)
    parser.add_argument('--reason', required=True)
    parser.add_argument('--operation', choices=('upgrade', 'rollback'), default='upgrade')
    args = vars(parser.parse_args(argv))
    try:
        print(json.dumps(revise(**args), ensure_ascii=False, indent=2))
        return 0
    except (ValueError, KeyError, OSError, subprocess.SubprocessError,
            s.AlreadyRunningError) as exc:
        print(f'paper revision refused: {exc}', file=sys.stderr)
        return s.EXIT_CONFIG


if __name__ == '__main__':
    raise SystemExit(main())

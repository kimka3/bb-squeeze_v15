"""Append-only audit journal plus an atomic, durable recovery snapshot.

Every decision and every exchange call is recorded before and after it happens,
so a bot that dies mid-allocation can be told on restart exactly how far it got.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

# ClientOrderIndex is c_longlong; stay well inside a positive int64.
CLIENT_ORDER_MODULO = 2 ** 62


def client_order_index(bar_ms: int, symbol: str, purpose: str, seq: int = 0) -> int:
    """Deterministic idempotency key.

    A retry of the same intent produces the same id. This identifies the intent;
    it does not by itself establish an exchange's duplicate-submission semantics.
    """
    raw = f'{bar_ms}:{symbol}:{purpose}:{seq}'.encode()
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], 'big') % CLIENT_ORDER_MODULO


class Journal:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / 'journal.jsonl'
        self.snapshot_path = self.dir / 'snapshot.json'

    def append(self, kind: str, **payload) -> dict:
        event = {'ts_ms': int(time.time() * 1000), 'kind': kind, **payload}
        line = json.dumps(event, default=str, ensure_ascii=False)
        with self.path.open('a', encoding='utf-8') as fh:
            fh.write(line + '\n')
            fh.flush()
            os.fsync(fh.fileno())
        return event

    def events(self, kind: str | None = None):
        if not self.path.exists():
            return
        with self.path.open(encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                if kind is None or event.get('kind') == kind:
                    yield event

    def save_snapshot(self, state: dict) -> None:
        tmp = self.snapshot_path.with_suffix('.tmp')
        with tmp.open('w', encoding='utf-8') as fh:
            json.dump(state, fh, indent=2, default=str, allow_nan=False)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(self.snapshot_path)
        # Persist the replacement directory entry on filesystems that support it.
        if os.name != 'nt':
            fd = os.open(self.dir, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def load_snapshot(self) -> dict:
        if not self.snapshot_path.exists():
            return {'positions': {}, 'setups': {}, 'pending': {}, 'orders': {}, 'last_bar_ms': 0}
        return json.loads(self.snapshot_path.read_text(encoding='utf-8'))

    def completed_intents(self, bar_ms: int) -> set[str]:
        """Intent ids already acknowledged for this bar — used to resume a bar
        that was interrupted partway through its allocation pass."""
        return {e['intent'] for e in self.events('order_ack')
                if e.get('bar_ms') == bar_ms and e.get('intent')}

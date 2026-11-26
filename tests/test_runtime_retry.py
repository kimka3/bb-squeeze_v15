import json
import sys
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from data_io import H4
from live import run
from live.config import LiveConfig
from live.journal import Journal
from live.paper import PaperBroker


def install_virtual_runtime(monkeypatch, now):
    monkeypatch.setattr(run.time, 'time', lambda: now[0])

    class ClockEvent:
        def is_set(self):
            return False
        def wait(self, seconds):
            now[0] += seconds

    class ImmediateExecutor:
        def __init__(self, **_):
            pass
        def submit(self, fn, *args):
            future = Future()
            try:
                future.set_result(fn(*args))
            except Exception as exc:
                future.set_exception(exc)
            return future
        def shutdown(self, **_):
            pass

    monkeypatch.setattr(run, 'STOP_REQUESTED', ClockEvent())
    monkeypatch.setattr(run, 'ThreadPoolExecutor', ImmediateExecutor)


def test_transient_bar_failure_recovers_inside_original_grace(tmp_path, monkeypatch):
    now = [H4 / 1000 + 5]
    install_virtual_runtime(monkeypatch, now)
    attempts = []
    def prepare(_):
        attempts.append(now[0])
        if len(attempts) < 3:
            raise RuntimeError('closed bar publication pending')
        return {}
    monkeypatch.setattr(run, 'prepare_frames', prepare)
    monkeypatch.setattr(run.feed, 'latest_closed', lambda _: (0, {}))
    monkeypatch.setattr(run, 'marks', lambda *_: {})
    monkeypatch.setattr(run.observation, 'collect', lambda *_: {})
    monkeypatch.setattr(run.observation, 'save', lambda *_: None)
    broker = PaperBroker('https://example.com', {}, tmp_path / 'paper.json')
    polls = []
    monkeypatch.setattr(broker, 'poll', lambda: polls.append(now[0]) or [])
    decisions = []
    trader = SimpleNamespace(broker=broker, journal=Journal(tmp_path),
                             service=lambda **_: None, last_bar_ms=-1, positions={})
    def on_bar(bar_ms, *_, **kw):
        decisions.append(kw)
        trader.last_bar_ms = bar_ms
        return SimpleNamespace(bar_ms=bar_ms, equity=100000, entries=[], exits=[], skipped=[], reasons=[], halted=False)
    trader.on_bar = on_bar
    config = LiveConfig(mode='paper', state_dir=tmp_path)
    run.run_loop(trader, {}, config, now[0] + 30)
    assert [t - H4 / 1000 for t in attempts] == [5, 10, 20]
    assert polls == [H4 / 1000 + 5]
    assert decisions[0]['allow_entries'] is True
    status = json.loads((tmp_path / 'runner_status.json').read_text())
    assert status['bar_errors'] == 2
    assert status['next_bar_retry_ms'] is None
    assert status['last_bar_ms'] == 0
    assert config.bar_grace_seconds == 60


def test_retry_backoff_is_bounded_for_persistent_outages():
    assert [run.bar_retry_seconds(n) for n in range(1, 7)] == [5, 10, 20, 30, 30, 30]


def test_duplicate_old_bar_does_not_inflate_late_coverage(monkeypatch):
    monkeypatch.setattr(run.time, 'time', lambda: H4 / 1000 + 120)
    monkeypatch.setattr(run.feed, 'latest_closed', lambda _: (0, {}))
    monkeypatch.setattr(run, 'marks', lambda *_: {})
    calls = []
    trader = SimpleNamespace(last_bar_ms=0, positions={}, on_bar=lambda *a, **kw:
        SimpleNamespace(equity=100000, entries=[], exits=[], skipped=[], reasons=[], halted=False))
    coverage = SimpleNamespace(bar=lambda *a, **kw: calls.append((a, kw)))
    run.run_once(trader, {}, LiveConfig(), frames={}, coverage=coverage)
    assert calls == []


def test_pinned_fetch_crossing_boundary_collects_new_bar_inside_grace(tmp_path, monkeypatch):
    boundary = 2 * H4 / 1000
    now = [boundary - 1]
    install_virtual_runtime(monkeypatch, now)
    attempts = []

    def prepare(_):
        attempts.append(now[0])
        pinned_bar = int(now[0] * 1000) // H4 * H4 - H4
        if len(attempts) == 1:
            # Startup/retry begins before the boundary but its I/O ends after.
            now[0] += 3
        return {'bar_ms': pinned_bar}

    monkeypatch.setattr(run, 'prepare_frames', prepare)
    monkeypatch.setattr(run.feed, 'latest_closed', lambda frames: (frames['bar_ms'], {}))
    monkeypatch.setattr(run, 'marks', lambda *_: {})
    monkeypatch.setattr(run.observation, 'collect', lambda *_: {})
    monkeypatch.setattr(run.observation, 'save', lambda *_: None)
    broker = PaperBroker('https://example.com', {}, tmp_path / 'paper.json')
    monkeypatch.setattr(broker, 'poll', lambda: [])
    trader = SimpleNamespace(broker=broker, journal=Journal(tmp_path),
                             service=lambda **_: None, last_bar_ms=0, positions={})
    decisions = []

    def on_bar(bar_ms, *_, **kw):
        decisions.append((bar_ms, now[0], kw['allow_entries']))
        trader.last_bar_ms = max(trader.last_bar_ms, bar_ms)
        return SimpleNamespace(bar_ms=bar_ms, equity=100000, entries=[], exits=[],
                               skipped=[], reasons=[], halted=False)

    trader.on_bar = on_bar
    run.run_loop(trader, {}, LiveConfig(mode='paper', state_dir=tmp_path), boundary + 15)
    assert attempts == [boundary - 1, boundary + 5]
    assert decisions == [(0, boundary + 2, False), (H4, boundary + 5, True)]
    assert trader.last_bar_ms == H4


def test_unprocessed_early_return_records_failure_without_bar_coverage(monkeypatch):
    now_ms = H4 + 120_000
    monkeypatch.setattr(run.time, 'time', lambda: now_ms / 1000)
    monkeypatch.setattr(run.feed, 'latest_closed', lambda _: (0, {}))
    monkeypatch.setattr(run, 'marks', lambda *_: {})
    processed, failures = [], []
    # A reconciliation/recovery guard can return an outcome without processing
    # the bar or scanning candidates. It must not look like zero missed signals.
    trader = SimpleNamespace(last_bar_ms=-1, positions={}, journal=SimpleNamespace(append=lambda *a, **kw: None), on_bar=lambda *a, **kw:
        SimpleNamespace(equity=100000, entries=[], exits=[], skipped=[],
                        reasons=['reconciliation guard'], halted=True))
    coverage = SimpleNamespace(bar=lambda *a, **kw: processed.append((a, kw)),
                               failure=lambda *a: failures.append(a))
    run.run_once(trader, {}, LiveConfig(), frames={}, coverage=coverage)
    assert processed == []
    assert failures == [('bar', now_ms, 'decision returned before bar was processed')]

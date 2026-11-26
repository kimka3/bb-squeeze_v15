import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from data_io import H4
from live import feed, run
from live.config import LiveConfig
from live.paper import PaperBroker


def rows(*times):
    return [[t, '100', '102', '99', '101', '2'] for t in times]


@pytest.mark.parametrize('payload,error', [
    (rows(0, 0), 'duplicate'),
    (rows(0, 2 * H4), 'gap'),
    (rows(1, H4 + 1), 'unaligned'),
    ([[0, 'nan', '102', '99', '101', '2']], 'non-finite'),
    ([[0, '100', '98', '99', '101', '2']], 'inconsistent'),
])
def test_bad_market_data_cannot_enter_indicators(monkeypatch, payload, error):
    monkeypatch.setattr(feed, '_klines_raw', lambda *a, **kw: payload)
    with pytest.raises(RuntimeError, match=error):
        feed.fetch_klines('BTCUSDT', now_ms=3 * H4)


def test_forming_bar_is_not_used(monkeypatch):
    monkeypatch.setattr(feed, '_klines_raw', lambda *a, **kw: rows(0, H4))
    got = feed.fetch_klines('BTCUSDT', now_ms=H4 + 100)
    assert got.time.tolist() == [0]


def test_example_config_annotation_loads_without_resetting_strategy():
    path = Path(__file__).resolve().parents[1] / 'live.config.example.json'
    config = LiveConfig.load(path)
    assert config.strategy.risk == .02
    assert 'BCHUSDT' not in config.universe


def test_campaign_identity_rejects_changed_mode_or_config(tmp_path):
    run.ensure_identity(LiveConfig(mode='paper', state_dir=tmp_path))
    with pytest.raises(RuntimeError, match='different'):
        run.ensure_identity(LiveConfig(mode='shadow', state_dir=tmp_path))
    with pytest.raises(RuntimeError, match='different'):
        run.ensure_identity(LiveConfig(mode='paper', state_dir=tmp_path, paper_equity=5000))


def test_legacy_state_is_preserved_not_silently_adopted(tmp_path):
    snapshot = tmp_path / 'snapshot.json'
    snapshot.write_text('{}')
    with pytest.raises(RuntimeError, match='legacy'):
        run.ensure_identity(LiveConfig(mode='paper', state_dir=tmp_path))
    assert snapshot.read_text() == '{}'


def test_offline_report_never_constructs_broker(tmp_path, monkeypatch, capsys):
    (tmp_path / 'report.json').write_text(json.dumps({'updated_ms': 1, 'mode': 'paper'}))
    monkeypatch.setattr(run, 'build', lambda *a: pytest.fail('network/broker called for offline report'))
    monkeypatch.setattr(sys, 'argv', ['run.py', '--report', '--state-dir', str(tmp_path)])
    assert run.main() == 0
    assert json.loads(capsys.readouterr().out)['mode'] == 'paper'


def test_elapsed_deadline_never_constructs_broker(tmp_path, monkeypatch):
    monkeypatch.setattr(run, 'build', lambda *a: pytest.fail('expired campaign started'))
    monkeypatch.setattr(sys, 'argv', ['run.py', '--mode', 'paper', '--loop',
                                     '--state-dir', str(tmp_path), '--until', '2021-01-01T00:00Z'])
    assert run.main() == 0
    assert not (tmp_path / 'identity.json').exists()


def test_late_signal_is_explicitly_suppressed(monkeypatch):
    captured = {}
    class Trader:
        positions = {}
        def on_bar(self, *args, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(equity=100000, entries=[], exits=[], skipped=[], reasons=[], halted=False)
    monkeypatch.setattr(run.feed, 'latest_closed', lambda _: (0, {}))
    monkeypatch.setattr(run, 'marks', lambda *a: {})
    monkeypatch.setattr(run.time, 'time', lambda: H4 / 1000 + 120)
    run.run_once(Trader(), {}, LiveConfig(), frames={})
    assert captured['allow_entries'] is False


def test_feed_download_cannot_block_paper_protection(tmp_path, monkeypatch):
    barrier = threading.Event()
    broker = PaperBroker('https://example.com', {}, tmp_path / 'paper.json')
    polled = []
    def poll():
        polled.append(True)
        barrier.set()
        return []
    def prepare(_):
        assert barrier.wait(timeout=2), 'feed blocked the protection poll'
        return {}
    monkeypatch.setattr(broker, 'poll', poll)
    monkeypatch.setattr(run, 'prepare_frames', prepare)
    monkeypatch.setattr(run, 'run_once', lambda *a, **kw: None)
    monkeypatch.setattr(run, 'persist_report', lambda *a, **k: None)
    monkeypatch.setattr(run.observation, 'collect', lambda *a: {})
    monkeypatch.setattr(run.observation, 'save', lambda *a: None)
    trader = SimpleNamespace(broker=broker, service=lambda **kw: None,
                             journal=SimpleNamespace(append=lambda *a, **k: None))
    run.STOP_REQUESTED.clear()
    run.run_loop(trader, {}, LiveConfig(mode='paper', state_dir=tmp_path), time.time() + .1)
    assert polled


def test_poll_failure_still_reconciles_with_no_stale_price(tmp_path, monkeypatch):
    broker = PaperBroker('https://example.com', {}, tmp_path / 'paper.json')
    def broken_poll():
        raise RuntimeError('later symbol failed after earlier fill')
    observed = []
    monkeypatch.setattr(broker, 'poll', broken_poll)
    monkeypatch.setattr(run, 'prepare_frames', lambda *a: {})
    monkeypatch.setattr(run, 'run_once', lambda *a, **kw: None)
    monkeypatch.setattr(run, 'persist_report', lambda *a, **k: None)
    monkeypatch.setattr(run.observation, 'collect', lambda *a: {})
    monkeypatch.setattr(run.observation, 'save', lambda *a: None)
    trader = SimpleNamespace(broker=broker, service=lambda **kw: observed.append(kw),
                             journal=SimpleNamespace(append=lambda *a, **k: None))
    run.STOP_REQUESTED.clear()
    run.run_loop(trader, {}, LiveConfig(mode='paper', state_dir=tmp_path), time.time() + .1)
    assert observed and observed[0]['marks'] == {}


def test_slow_mark_read_cannot_start_expired_decision(monkeypatch):
    now = [100.]
    monkeypatch.setattr(run.time, 'time', lambda: now[0])
    monkeypatch.setattr(run.feed, 'latest_closed', lambda _: (0, {}))
    def slow_marks(*_):
        now[0] = 200.
        return {}
    monkeypatch.setattr(run, 'marks', slow_marks)
    trader = SimpleNamespace(on_bar=lambda *a, **kw: pytest.fail('expired decision executed'))
    run.STOP_REQUESTED.clear()
    assert run.run_once(trader, {}, LiveConfig(), frames={}, deadline=150.) is None


def test_missing_marks_fail_closed(monkeypatch):
    from live import markets
    monkeypatch.setattr(markets, '_get', lambda _: {'order_book_details': []})
    with pytest.raises(RuntimeError, match='missing'):
        markets.marks('https://example.com', {'BTCUSDT': SimpleNamespace(symbol='BTC')})

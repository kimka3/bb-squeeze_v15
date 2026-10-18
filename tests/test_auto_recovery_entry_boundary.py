"""A recovered exchange gate cannot resurrect an expired signal candidate."""
import json

import pytest

from test_entry_retry import defer, close
from test_strategy_integration import rows, START, H4
from live.authenticated import APIReadError
from live.recovery import ExchangeRecovery


class Coverage:
    def snapshot(self):
        return {'active_gap_streams': [], 'coverage_complete': False}


@pytest.mark.parametrize('discard_before_deadline', [False, True])
def test_outage_discards_deferred_entry_and_recovery_never_replays_it(tmp_path, monkeypatch, discard_before_deadline):
    venue, broker, trader = defer(tmp_path)
    elapsed = [0.0]
    controller = ExchangeRecovery(tmp_path, 'synthetic-reviewed-release',
        clock=lambda: venue.now / 1000, monotonic=lambda: elapsed[0])
    broker.entry_guard = controller.can_enter
    original_deadline = trader.deferred_entries['deadline_ms']
    original_bar = trader.last_bar_ms
    failure = APIReadError('temporary exchange maintenance', code='temporary_http',
        endpoint='/api/v1/account', automatic_recovery_allowed=True, http_status=503)
    try:
        controller.failure(failure, 'service')
        assert controller.snapshot()['reason'] == 'temporary_read_failure'
        assert not controller.can_enter()
        if discard_before_deadline:
            venue.now += 11_000
            elapsed[0] += 11
            assert venue.now < original_deadline
            trader.retry_deferred_entries()
            assert trader.deferred_entries is None
            assert not venue.sent
        elapsed[0] += (original_deadline + 1 - venue.now) / 1000
        venue.now = int(original_deadline) + 1
        trader.retry_deferred_entries()
        assert trader.deferred_entries is None
        assert not venue.sent and not trader.pending_operation
        # Actual strategy broker/protection code runs against the in-memory Venue.
        for index in range(3):
            if index:
                venue.now += 60_000
                elapsed[0] += 60
            broker.poll()
            protected = trader.service(now_ms=venue.now)
            assert not protected.halted
            controller.cycle(trader, Coverage(), poll_ok=True, service_ok=True,
                             now_ms=venue.now)
        assert controller.can_enter()
        assert trader.last_bar_ms == original_bar == START
        assert venue.now > original_deadline
        # Re-offering the old bar and retry function must neither scan nor send.
        monkeypatch.setattr('live.trader.scan_signals',
            lambda *a: pytest.fail('recovered gate must not rescan old signal'))
        for _ in range(3):
            trader.retry_deferred_entries()
            outcome = trader.on_bar(START, rows(), {'BTCUSDT': 100, 'ETHUSDT': 100},
                                   now_ms=venue.now)
            assert not outcome.entries
        assert not venue.sent
        assert not trader.deferred_entries and not trader.pending_operation
        events = [json.loads(line) for line in (tmp_path / 'journal.jsonl').read_text().splitlines()]
        discarded = [event for event in events if event['kind'] == 'entry_deferred_discarded']
        assert len(discarded) == 1
    finally:
        close(venue, broker)

"""One-time completed-bar decisions survive later read/submission failures."""
from decimal import Decimal

import pytest

from test_strategy_integration import START, H4, Venue, rows, setup
from live.authenticated import APIReadError
from live.execution_evidence import NotSubmitted
from live.request_budget import RequestDeferred


MARKS = {'BTCUSDT': 100, 'ETHUSDT': 100}


def test_sizing_read_failure_does_not_repeat_aging_or_setup_after_retry_and_restart(tmp_path):
    venue, broker, trader = setup(tmp_path)
    bar = START + H4
    signal_rows = rows(short=False)
    signal_rows['BTCUSDT']['short_signal'] = True
    try:
        trader.on_bar(START, rows(), MARKS, now_ms=venue.now)
        trader.setups['BTCUSDT'] = [110., 3]
        venue.now = bar + H4 + 1_000
        original = broker.account_state
        calls = 0

        def fail_sizing_read():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise APIReadError('API read failed (HTTP 503); response suppressed',
                    code='http_503', endpoint='/api/v1/trades',
                    automatic_recovery_allowed=True, http_status=503)
            return original()

        broker.account_state = fail_sizing_read
        with pytest.raises(APIReadError):
            trader.on_bar(bar, signal_rows, MARKS, now_ms=venue.now)
        broker.account_state = original
        assert trader.last_bar_ms == bar
        assert trader.positions['ETHUSDT']['bars'] == 1
        assert trader.setups['BTCUSDT'] == [110., 2]
        snap = trader.journal.load_snapshot()
        assert snap['last_bar_ms'] == bar
        assert snap['positions']['ETHUSDT']['bars'] == 1
        assert snap['setups']['BTCUSDT'] == [110., 2]
        count = len(venue.sent)
        venue.now += 5_000
        trader.on_bar(bar, signal_rows, MARKS, now_ms=venue.now)
        assert trader.positions['ETHUSDT']['bars'] == 1
        assert trader.setups['BTCUSDT'] == [110., 2]
        assert len(venue.sent) == count
    finally:
        broker.close()

    venue, broker, trader = setup(tmp_path, venue)
    try:
        venue.now += 5_000
        broker.poll()
        trader.on_bar(bar, signal_rows, MARKS, now_ms=venue.now)
        assert trader.last_bar_ms == bar
        assert trader.positions['ETHUSDT']['bars'] == 1
        assert trader.setups['BTCUSDT'] == [110., 2]
        assert sum(not intent.reduce_only for intent in venue.sent) == 1
        assert not trader.inflight_bar_ms
    finally:
        broker.close()


class CloseFailureVenue(Venue):
    def __init__(self):
        super().__init__()
        self.unknown_btc_exit = None
        self.unsent_eth_exit = None
        self.calls = []

    def submit(self, intent, base, price, trigger):
        self.calls.append(intent)
        if intent.order_type == 'market' and intent.reduce_only:
            if intent.market_index == 1 and self.unknown_btc_exit is None:
                self.unknown_btc_exit = intent
                raise RuntimeError('test: close dispatch outcome unknown')
            if intent.market_index == 0 and self.unsent_eth_exit is None:
                self.unsent_eth_exit = intent
                raise NotSubmitted('request_budget_deferred', retryable=True,
                                   cause=RequestDeferred(1))
        return super().submit(intent, base, price, trigger)


def test_first_unknown_exit_preserves_second_close_decision_for_its_original_cycle(tmp_path, monkeypatch):
    venue, broker, trader = setup(tmp_path, CloseFailureVenue())
    monkeypatch.setattr('live.trader.scan_signals', lambda ts, *args:
        ({'BTCUSDT': ('SHORT', 4, ts), 'ETHUSDT': ('SHORT', 4, ts)}, 0)
        if ts == START else ({}, 0))
    try:
        trader.on_bar(START, rows(), MARKS, now_ms=venue.now)
        for position in trader.positions.values():
            position['bars'] = 89
        original_cycle = trader.positions['ETHUSDT']['entry_ms']
        venue.now = START + 2 * H4 + 1_000
        trader.on_bar(START + H4, rows(short=False), MARKS, now_ms=venue.now)
        assert venue.unknown_btc_exit is not None
        assert trader.last_bar_ms == START + H4
        request = trader.residual_exits['ETHUSDT']
        assert request['reason'] == 'TIME' and request['cycle_ms'] == original_cycle
        snap = trader.journal.load_snapshot()
        assert snap['residual_exits']['ETHUSDT']['cycle_ms'] == original_cycle
        venue.now += 61_000
        broker.poll()
        trader.service(now_ms=venue.now)
        assert 'ETHUSDT' not in trader.positions
        assert 'ETHUSDT' not in trader.residual_exits
        assert 'BTCUSDT' in trader.positions
        assert sum(i.client_order_index == venue.unknown_btc_exit.client_order_index
                   for i in venue.calls) == 1
        actual_eth_closes = [i for i in venue.sent if i.market_index == 0
                            and i.order_type == 'market' and i.reduce_only]
        assert len(actual_eth_closes) == 1
    finally:
        broker.close()


def test_stale_residual_exit_cannot_close_a_different_position_cycle(tmp_path):
    venue, broker, trader = setup(tmp_path)
    try:
        trader.on_bar(START, rows(), MARKS, now_ms=venue.now)
        position = trader.positions['ETHUSDT']
        trader.residual_exits['ETHUSDT'] = {'reason': 'TIME', 'bar_ms': START,
            'attempt': 0, 'cycle_ms': position['entry_ms'] - H4}
        trader._save()
        count = len(venue.sent)
        venue.now += 61_000
        broker.poll()
        trader.service(now_ms=venue.now)
        assert trader.halted
        assert 'ETHUSDT' in trader.positions
        assert len(venue.sent) == count
        stop = trader.orders['ETHUSDT']['SL']['client_order_index']
        assert venue.rows[stop]['status'] == 'pending'
        assert Decimal(venue.rows[stop]['remaining_base_amount']) == Decimal(str(position['qty']))
    finally:
        broker.close()

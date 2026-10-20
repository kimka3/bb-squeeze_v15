"""Independent final safety review regressions; in-memory venue only."""
from test_stop_renewal import opened
import pytest
from types import SimpleNamespace

from test_strategy_integration import START, H4, Venue, rows, setup
from live.authenticated import APIReadError, LighterTransport
from live.execution_evidence import NotSubmitted


MARKS = {'BTCUSDT': 100, 'ETHUSDT': 100}


def test_observed_expired_pending_stop_is_not_armed_protection(tmp_path):
    venue, broker, trader, old = opened(tmp_path)
    try:
        # Publication can lag expiry. The persisted, venue-provided expiry is
        # already past even though this read still labels the order pending.
        venue.now = venue.rows[old]['order_expiry'] + 1
        broker.poll()
        state = broker.account_state()
        stop = next(row for row in state.active_orders['ETH']
                    if row['client_order_index'] == old)
        assert stop['armed'] is False
        assert not state.execution_changes_verified
        assert str(old) in state.ledger_report['unsafe_protection']
        trader.service(now_ms=venue.now)
        assert 'ETHUSDT' not in trader.positions
        assert not venue.account()['positions']
        assert trader.halted  # No automatic clearing of the safety HALT.
    finally:
        broker.close()


def test_bar_cannot_bypass_residual_cycle_rejection_by_service(tmp_path):
    venue, broker, trader = setup(tmp_path)
    try:
        trader.on_bar(START, rows(), MARKS, now_ms=venue.now)
        position = trader.positions['ETHUSDT']
        trader.residual_exits['ETHUSDT'] = {'reason': 'TIME', 'bar_ms': START,
            'attempt': 0, 'cycle_ms': position['entry_ms'] - H4}
        trader._save()
        count = len(venue.sent)
        venue.now = START + 2 * H4 + 1_000
        broker.poll()
        trader.service(now_ms=venue.now)
        assert trader.halted and len(venue.sent) == count
        trader.on_bar(START + H4, rows(short=False), MARKS, now_ms=venue.now)
        assert 'ETHUSDT' in trader.positions
        assert len(venue.sent) == count
        assert trader.halted
    finally:
        broker.close()


class NonceReadFailureVenue(Venue):
    """Exercise the real transport's pre-dispatch nonce classifier without IO."""
    def __init__(self):
        super().__init__()
        self.fail_nonce = True

    def _next_nonce(self):
        if self.fail_nonce:
            self.fail_nonce = False
            raise APIReadError('API read failed (HTTP 503); response suppressed',
                code='http_503', endpoint='/api/v1/nextNonce',
                automatic_recovery_allowed=True, http_status=503)
        return 1

    def submit(self, intent, *args):
        if intent.order_type == 'market' and intent.reduce_only:
            LighterTransport._submission_nonce(self)
        return super().submit(intent, *args)


def test_transient_nonce_read_failure_does_not_permanently_block_proven_unsent_exit(tmp_path):
    venue, broker, trader = setup(tmp_path, NonceReadFailureVenue())
    try:
        trader.on_bar(START, rows(), MARKS, now_ms=venue.now)
        trader.positions['ETHUSDT']['bars'] = 89
        venue.now = START + 2 * H4 + 1_000
        with pytest.raises(APIReadError):
            trader.on_bar(START + H4, rows(short=False), MARKS, now_ms=venue.now)
        assert 'ETHUSDT' in trader.positions
        assert 'ETHUSDT' in trader.residual_exits
        assert not any(i.order_type == 'market' and i.reduce_only for i in venue.sent)
        venue.now += 61_000
        broker.poll()
        trader.service(now_ms=venue.now)
        assert 'ETHUSDT' not in trader.positions
        assert sum(i.order_type == 'market' and i.reduce_only for i in venue.sent) == 1
        assert not trader.halted
        venue.now += 61_000
        broker.poll()
        trader.service(now_ms=venue.now)
        assert sum(i.order_type == 'market' and i.reduce_only for i in venue.sent) == 1
    finally:
        broker.close()


@pytest.mark.parametrize('code', ['http_401', 'tls_error', 'schema_error'])
def test_nonce_failure_without_transient_read_whitelist_is_not_retryable(code):
    error = APIReadError('controlled nontransient read failure', code=code,
        endpoint='/api/v1/nextNonce', automatic_recovery_allowed=False)
    def fail():
        raise error
    with pytest.raises(NotSubmitted) as caught:
        LighterTransport._submission_nonce(SimpleNamespace(_next_nonce=fail))
    assert caught.value.retryable is False
    assert caught.value.cause is error

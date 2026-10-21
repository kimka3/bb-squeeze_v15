"""An unsent renewal must not hide loss of the existing native stop."""
from decimal import Decimal

import pytest

from test_stop_renewal import opened


@pytest.mark.parametrize('superseded', [False, True])
def test_expired_native_stop_with_unsent_reduced_renewal_closes_owned_residue(tmp_path, superseded):
    venue, broker, trader, old = opened(tmp_path)
    try:
        venue.refuse_new = True
        trader.service(now_ms=venue.now, marks={'ETHUSDT': 100})
        pending = next(iter(trader.unresolved_operations.values()))
        first_unsent = pending['coi']
        original_intent = broker.engine.store.load(first_unsent)[0]
        assert broker.engine.store.known_not_submitted(first_unsent)

        # The exchange-native TP fills independently while the renewal is
        # positively unsent. The normal service synchronizes the smaller size.
        tp = trader.orders['ETHUSDT']['TP']['client_order_index']
        venue.now += 1000
        venue.fill(tp, Decimal(venue.rows[tp]['initial_base_amount']))
        venue.refuse_new = True
        broker.poll()
        trader.service(now_ms=venue.now, marks={'ETHUSDT': 100})
        if superseded:
            trader.service(now_ms=venue.now, marks={'ETHUSDT': 100})
            assert first_unsent in broker.engine.store.supersessions()
        else:
            assert not broker.engine.store.supersessions()
        unsent_ids = {int(coi) for coi, item in broker.account_state().ledger_report['not_submitted_orders'].items()
                      if item['role'] == 'SL'}
        expected_residue = abs(venue.position[0])
        assert expected_residue > 0
        before = len(venue.events)

        # Valid order evidence explicitly says the old stop has expired. It is
        # still listed by this venue, so a generic "unsent SL exists" shortcut
        # must not mistake it for armed protection of the remaining position.
        venue.now = venue.rows[old]['order_expiry']
        broker.poll()
        trader.service(now_ms=venue.now, marks={'ETHUSDT': 100})
        assert venue.position[0] == 0
        assert trader.halted  # Risk management never clears the safety halt.
        assert broker.engine.store.load(first_unsent)[0] == original_intent
        assert broker.engine.store.known_not_submitted(first_unsent)
        submitted = [event[1] for event in venue.events[before:] if event[0] == 'create']
        assert len(submitted) == 1 and not unsent_ids.intersection(submitted)
        intent = broker.engine.store.load(submitted[0])[0]
        assert intent.order_type == 'market' and intent.reduce_only
        assert Decimal(intent.quantity) == expected_residue
        assert broker._bindings()[submitted[0]]['role'] == 'EXIT'
    finally:
        broker.close()

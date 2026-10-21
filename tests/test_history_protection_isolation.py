"""Unsupported historical economics must not disable verified other-market risk service."""
from decimal import Decimal
import pytest
from test_strategy_integration import START, H4, setup, rows
from live.execution_evidence import UnverifiedExecution
from live.authenticated import NotSubmitted

MARKS = {'ETHUSDT': 100, 'BTCUSDT': 100}


def opened(tmp_path):
    x, b, t = setup(tmp_path)
    signals = rows()
    signals['BTCUSDT']['short_signal'] = True
    t.on_bar(START, signals, MARKS, now_ms=x.now)
    assert set(t.positions) == set(MARKS)
    return x, b, t


def unsupported(x, kind='trade', market=0):
    x.now += 61000
    if kind == 'trade':
        row = dict(next(r for r in x.trades if r['market_id'] == market))
        row.update(trade_id=90001, type='liquidation', timestamp=x.now)
        x.trades.append(row)
    else:
        row = {'funding_id':90001, 'market_id':market, 'timestamp':x.now//1000,
               'change':'-0.4', 'discount':'0', 'rate':'.001',
               'position_size':'1', 'position_side':'provider-new-side'}
        x.funding.append(row)
    return row


def service(x, b, t):
    b.poll()
    return t.service(now_ms=x.now, marks=MARKS)


@pytest.mark.parametrize('kind', ['trade', 'funding'])
def test_quarantine_blocks_entries_but_other_market_tp_and_stop_continue(tmp_path, kind):
    x, b, t = opened(tmp_path)
    try:
        raw = unsupported(x, kind)
        original_eth = dict(t.positions['ETHUSDT'])
        btc = t.positions['BTCUSDT']
        original_btc = btc['qty']
        eth_stop = t.orders['ETHUSDT']['SL']['client_order_index']
        btc_stop = t.orders['BTCUSDT']['SL']['client_order_index']
        btc_tp = t.orders['BTCUSDT']['TP']['client_order_index']
        x.fill(btc_tp, Decimal(x.rows[btc_tp]['remaining_base_amount']))
        service(x, b, t)
        state = b.account_state()
        assert t.halted and not state.execution_changes_verified
        assert state.ledger_report['historical_evidence_complete'] is False
        assert state.ledger_report['performance_complete'] is False
        assert state.ledger_report['quarantined_history_count'] == 1
        assert not state.ledger_report['publication_pending']
        assert 'BTC' in state.ledger_report['risk_verified_symbols']
        assert 'ETH' not in state.ledger_report['risk_verified_symbols']
        assert not state.ledger_report['owned_exposure_matches_account']
        assert btc['qty'] == pytest.approx(original_btc/2)
        assert btc['partial_taken'] and btc['stop'] == 100
        assert Decimal(x.rows[btc_stop]['trigger_price']) == 100
        assert Decimal(x.rows[btc_stop]['remaining_base_amount']) == Decimal(str(btc['qty']))
        assert t.positions['ETHUSDT'] == original_eth
        assert x.rows[eth_stop]['status'] == 'pending'
        assert b.ledger.quarantined_events()[0]['raw'] == raw
        before = len(x.sent)
        with pytest.raises(NotSubmitted, match='pre submission check failed'):
            b.market_order(b.markets['ETHUSDT'], 'ETHUSDT', 'SHORT', .1, False, 99901, 100)
        assert len(x.sent) == before
    finally:
        b.close()


@pytest.mark.parametrize('actual_fraction', [Decimal('0'), Decimal('.5')])
def test_affected_market_unknown_reduction_never_claims_tp_or_removes_stop(tmp_path, actual_fraction):
    x, b, t = opened(tmp_path)
    try:
        unsupported(x)
        old_qty = t.positions['ETHUSDT']['qty']
        eth_stop = t.orders['ETHUSDT']['SL']['client_order_index']
        x.position[0] *= actual_fraction
        before = len(x.sent)
        service(x, b, t)
        assert t.halted and t.positions['ETHUSDT']['qty'] == old_qty
        assert not t.positions['ETHUSDT']['partial_taken']
        assert x.rows[eth_stop]['status'] == 'pending'
        assert len(x.sent) == before
    finally:
        b.close()


def test_other_market_residual_exit_continues_across_restart(tmp_path):
    x, b, t = opened(tmp_path)
    raw = unsupported(x)
    service(x, b, t)
    t.residual_exits['BTCUSDT'] = {'reason':'PROTECTION_LOST', 'bar_ms':x.now,
        'attempt':0, 'cycle_ms':t.positions['BTCUSDT']['entry_ms']}
    t._save()
    before = len(x.sent)
    b.close()
    x, b, t = setup(tmp_path, x)
    try:
        service(x, b, t)
        assert t.halted and 'BTCUSDT' not in t.positions
        assert 'ETHUSDT' in t.positions
        assert len(x.sent) == before+1 and x.sent[-1].reduce_only
        assert x.sent[-1].market_index == 1
        assert b.ledger.quarantined_events()[0]['raw'] == raw
        assert not b.account_state().execution_changes_verified
    finally:
        b.close()


def test_quarantine_cannot_manufacture_zero_allocation_or_certify_profit(tmp_path):
    x, b, t = opened(tmp_path)
    try:
        b.initial_allocated_equity = '298.56'
        unsupported(x)
        # An unrecognized economic event removes ETH exposure. The partial
        # normalized cash ledger alone is no longer a valid allocation ledger.
        x.position[0] = Decimal(0)
        state = b.account_state()
        assert state.equity == 298.56
        assert state.ledger_report['allocated_equity'] is None
        assert state.ledger_report['risk_evaluation_equity'] == '298.56'
        assert not state.execution_changes_verified
        x.account = lambda: {'total_asset_value':'120', 'available_balance':'100', 'positions':[
            {'market_id':1, 'position':str(abs(x.position[1])), 'sign':-1,
             'position_value':str(abs(x.position[1])*100), 'avg_entry_price':'100'}]}
        assert b.account_state().equity == 120
    finally:
        b.close()


def test_missing_identity_is_not_soft_quarantine(tmp_path):
    x, b, t = opened(tmp_path)
    try:
        row = unsupported(x)
        row.pop('ask_client_id')
        before = len(x.sent)
        with pytest.raises(UnverifiedExecution, match='identity'):
            service(x, b, t)
        assert len(x.sent) == before
        assert b.ledger.historical_issues() == []
    finally:
        b.close()


def test_audit_does_not_certify_known_fills_when_another_record_is_quarantined(tmp_path):
    from live.strategy_audit import audit
    x, b, t = setup(tmp_path/'strategy')
    try:
        t.on_bar(START, rows(), MARKS, now_ms=x.now)
        entry = b._cycle('ETHUSDT')
        t.positions['ETHUSDT']['bars'] = 89
        x.now += H4
        t.on_bar(START+H4, rows(False), MARKS, now_ms=x.now)
        exit_id = next(c for c, v in b._bindings().items() if v['role'] == 'EXIT')
        session = {'account_index':100, 'network':'testnet', 'started_at_ms':START,
            'first_coi':entry, 'entry_result':b.engine.store.load(entry)[1],
            'close_results':{str(exit_id):b.engine.store.load(exit_id)[1]}, 'equity_before':'300'}
        unsupported(x)
        before = len(x.sent)
        result = audit(x, [session], tmp_path/'audit', clock=lambda:x.now/1000)
        assert all(row['matches'] for row in result['order_comparisons'])
        assert not result['all_order_fills_match']
        assert not result['performance_complete'] and result['quarantined_history_count'] == 1
        assert len(x.sent) == before
    finally:
        b.close()

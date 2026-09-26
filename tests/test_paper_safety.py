"""Synthetic regression scenarios, never historical or live execution evidence."""
import json
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from live.book import Quote, walk
from live.markets import Market
from live.paper import PaperBroker, PaperFill

SYMBOL = 'ETHUSDT'
MARKET = Market('ETH', 1, 3, 2, 0., 0., .05, .1, 'active')


def broker(tmp_path, **kwargs):
    b = PaperBroker('https://unused', {SYMBOL: MARKET}, tmp_path / 'paper.json',
                    equity=1000., **kwargs)
    b._marks = {SYMBOL: 100.}
    b._indexes = {SYMBOL: 100.}
    b.refresh_marks = lambda: b._marks
    b._accrue_funding = lambda marks: None
    b._quote = lambda symbol, qty, buying, **options: Quote(100., qty, qty, 100., 100., False)
    return b


def enter(b, qty=10., side='SHORT', coi=1):
    return b.market_order(MARKET, SYMBOL, side, qty, False, coi, 100.)


def empty(symbol, qty, buying, **options):
    return Quote(float('nan'), 0., qty, float('nan'), float('nan'), True)


def test_empty_close_does_not_invent_fill_or_cash(tmp_path):
    b = broker(tmp_path)
    enter(b)
    b._quote = empty
    before_cash = b.cash
    f = b.market_order(MARKET, SYMBOL, 'SHORT', 10., True, 2, 101.)
    assert f.qty == 0.
    assert b.book[SYMBOL].qty == 10.
    assert b.cash == before_cash
    assert len(b.fills) == 1
    assert b.measurements()['attempts_by_symbol_role_direction']['ETHUSDT:EXIT:BUY']['zero_fills'] == 1


def test_close_caps_actual_depth_and_requested_quantity_keeps_stop(tmp_path):
    b = broker(tmp_path)
    enter(b)
    ref = b.place_stop(MARKET, SYMBOL, 'SHORT', 10., 104., 3)
    b._quote = lambda symbol, qty, buying, **options: Quote(101., .5, qty, 101., 101., True)
    f = b.market_order(MARKET, SYMBOL, 'SHORT', 1., True, 2, 101.)
    assert f.qty == .5
    assert b.book[SYMBOL].qty == 9.5
    assert b.book[SYMBOL].stop == 104.
    assert b._orders[ref.order_index]['status'] == 'active'
    assert b.cash == 999.5


@pytest.mark.parametrize('fill_qty', [0., 2.])
def test_ioc_stop_cancels_unfilled_residual_without_fabrication(tmp_path, fill_qty):
    b = broker(tmp_path)
    enter(b)
    ref = b.place_stop(MARKET, SYMBOL, 'SHORT', 10., 104., 3)
    b._quote = lambda symbol, qty, buying, **options: Quote(104., fill_qty, qty, 104., 104., True)
    b._marks[SYMBOL] = 104.
    fired = b.poll()
    assert sum(f.qty for f in fired) == fill_qty
    assert b.book[SYMBOL].qty == 10. - fill_qty
    assert b.book[SYMBOL].stop == 0.
    assert b._orders[ref.order_index]['status'] == 'canceled'
    assert b.account_state().active_orders == {}
    assert SYMBOL in b.measurements()['risk']['unprotected_positions']
    assert b.poll() == []  # a consumed IOC must not silently rearm itself


def test_cancel_removes_trigger_behavior_and_is_durable(tmp_path):
    b = broker(tmp_path)
    enter(b)
    sl = b.place_stop(MARKET, SYMBOL, 'SHORT', 10., 104., 3)
    tp = b.place_take_profit(MARKET, SYMBOL, 'SHORT', 4., 96., 4)
    b.cancel(MARKET, sl)
    b.cancel(MARKET, tp)
    restored = broker(tmp_path)
    restored._marks[SYMBOL] = 105.
    assert restored.poll() == []
    assert restored.book[SYMBOL].qty == 10.
    assert restored.account_state().active_orders == {}


def test_maker_touch_never_guarantees_fill(tmp_path):
    b = broker(tmp_path)
    enter(b)
    b.place_stop(MARKET, SYMBOL, 'SHORT', 10., 104., 3)
    b.place_take_profit(MARKET, SYMBOL, 'SHORT', 4., 96., 4)
    b._marks[SYMBOL] = 96.
    b._quote = PaperBroker._quote.__get__(b)
    with patch('live.paper.fetch_depth', return_value={
            'asks': [{'price': '96', 'remaining_base_amount': '50'}]}):
        assert b.poll() == []
    assert b.book[SYMBOL].qty == 10.
    assert not b.book[SYMBOL].partial_taken


def test_maker_partial_fills_respect_explicit_target_and_limit(tmp_path):
    b = broker(tmp_path)
    enter(b)
    b.place_stop(MARKET, SYMBOL, 'SHORT', 10., 104., 3)
    b.place_take_profit(MARKET, SYMBOL, 'SHORT', 4., 96., 4)
    b._marks[SYMBOL] = 98.  # resting limit may fill before mark reaches the target
    b._quote = PaperBroker._quote.__get__(b)
    with patch('live.paper.fetch_depth', return_value={
            'asks': [{'price': '95.5', 'remaining_base_amount': '1'},
                     {'price': '96.1', 'remaining_base_amount': '50'}]}):
        first = b.poll()[0]
    assert first.qty == 1.
    assert first.price == 96.
    assert first.fill_model == 'crossed_depth_limit'
    assert b.book[SYMBOL].qty == 9.
    assert b.book[SYMBOL].tp_filled_qty == 1.
    assert not b.book[SYMBOL].partial_taken
    assert b.book[SYMBOL].stop == 104.
    # More displayed depth must fill only the remaining target, not another
    # fraction of the current position (and not the original target again).
    with patch('live.paper.fetch_depth', return_value={
            'asks': [{'price': '95.5', 'remaining_base_amount': '50'}]}):
        second = b.poll()[0]
    assert second.qty == 3.
    assert b.book[SYMBOL].qty == 6.
    assert b.book[SYMBOL].partial_taken
    assert b.book[SYMBOL].stop == 100.
    assert b.account_state().positions['ETH']['partial_taken'] is True
    assert b.poll() == []


def test_observe_only_model_never_converts_opportunity_to_fill(tmp_path):
    b = broker(tmp_path, passive_fill_model='observe_only')
    enter(b)
    b.place_take_profit(MARKET, SYMBOL, 'SHORT', 4., 96., 4)
    b._quote = lambda symbol, qty, buying, **options: Quote(95., qty, qty, 95., 95., False)
    b._marks[SYMBOL] = 95.
    assert b.poll() == []
    assert b.book[SYMBOL].qty == 10.
    assert b.measurements()['observations']['event_counts']['maker_fill_unobserved'] == 1


def test_partial_trigger_tp_is_not_full_tp_and_remainder_cancels(tmp_path):
    b = broker(tmp_path, passive_take_profit=False)
    enter(b)
    b.place_stop(MARKET, SYMBOL, 'SHORT', 10., 104., 3)
    ref = b.place_take_profit(MARKET, SYMBOL, 'SHORT', 4., 96., 4)
    b._quote = lambda symbol, qty, buying, **options: Quote(96., 1., qty, 96., 96., True)
    b._marks[SYMBOL] = 95.
    assert b.poll()[0].qty == 1.
    assert b.book[SYMBOL].stop == 104.
    assert b.book[SYMBOL].tp_filled_qty == 1.
    assert not b.book[SYMBOL].partial_taken
    assert b.book[SYMBOL].tp is None
    assert b._orders[ref.order_index]['status'] == 'canceled'


def test_market_coi_outcome_survives_reload_and_refuses_different_intent(tmp_path):
    b = broker(tmp_path)
    first = enter(b)
    restored = broker(tmp_path)
    restored._quote = lambda *args, **kwargs: pytest.fail('duplicate must not hit the book')
    assert enter(restored) == first
    assert restored.market_order_result(1) == first
    assert len(restored.fills) == 1
    assert restored.market_order_result(999) is None
    with pytest.raises(ValueError, match='different intent'):
        enter(restored, qty=9.)


def test_zero_fill_outcome_is_also_durable_and_idempotent(tmp_path):
    b = broker(tmp_path)
    b._quote = empty
    first = enter(b)
    assert first.qty == 0.
    restored = broker(tmp_path)
    assert enter(restored).qty == 0.
    assert restored.book == {}


def test_save_failure_never_acknowledges_or_recovers_unsaved_fill(tmp_path):
    b = broker(tmp_path)
    b.save()
    before = b.state_path.read_bytes()
    with patch('live.paper.os.replace', side_effect=OSError('disk error')):
        with pytest.raises(OSError):
            enter(b)
    assert b.state_path.read_bytes() == before
    with pytest.raises(RuntimeError, match='persistence failed'):
        b.market_order_result(1)
    with pytest.raises(RuntimeError, match='persistence failed'):
        b.account_state()
    restored = broker(tmp_path)
    assert restored.market_order_result(1) is None
    assert restored.book == {}


def test_model_change_cannot_silently_contaminate_existing_campaign(tmp_path):
    b = broker(tmp_path)
    b.save()
    with pytest.raises(ValueError, match='model settings changed'):
        broker(tmp_path, passive_fill_model='observe_only')


def test_zero_remaining_depth_is_not_resurrected_from_initial_amount():
    depth = {'asks': [{'price': '100', 'remaining_base_amount': 0,
                       'initial_base_amount': '50'}]}
    assert walk(depth, 1., True).filled == 0.


def test_nonfinite_depth_is_not_usable():
    depth = {'asks': [{'price': 'nan', 'remaining_base_amount': '50'},
                      {'price': '100', 'remaining_base_amount': 'inf'}]}
    assert walk(depth, 1., True).filled == 0.


def test_undersized_stop_cannot_liquidate_more_than_its_order_quantity(tmp_path):
    b = broker(tmp_path)
    enter(b)
    b.place_stop(MARKET, SYMBOL, 'SHORT', 2., 104., 3)
    b._marks[SYMBOL] = 104.
    assert b.poll()[0].qty == 2.
    assert b.book[SYMBOL].qty == 8.
    assert b.book[SYMBOL].stop == 0.


def test_unavailable_book_does_not_pretend_an_ioc_was_observed(tmp_path):
    b = broker(tmp_path)
    enter(b)
    ref = b.place_stop(MARKET, SYMBOL, 'SHORT', 10., 104., 3)
    b._marks[SYMBOL] = 104.
    b._quote = lambda *args, **kw: (_ for _ in ()).throw(OSError('offline'))
    assert b.poll() == []
    assert b.book[SYMBOL].qty == 10.
    assert b._orders[ref.order_index]['status'] == 'active'
    assert b.measurements()['observations']['event_counts']['stop_quote_unavailable'] == 1


def funding_broker(tmp_path, now_hour=100):
    with patch('live.paper.time.time', return_value=now_hour * 3600 + 10):
        b = broker(tmp_path)
        enter(b, side='LONG')
    b._accrue_funding = PaperBroker._accrue_funding.__get__(b)
    return b


def test_closed_funding_is_preserved_and_not_paid_twice_after_restart(tmp_path):
    b = funding_broker(tmp_path)
    boundary = 101 * 3600
    b._mark_sample_ms[SYMBOL] = boundary * 1000
    b._funding_rate_at = lambda market, hour: .01
    with patch('live.paper.time.time', return_value=boundary + 1):
        b._accrue_funding(b._marks)
        b._accrue_funding(b._marks)
        assert b.funding_total == pytest.approx(-.1)
        b.market_order(MARKET, SYMBOL, 'LONG', 10., True, 2, 100.)
    report = b.measurements()
    assert report['funding_closed'] == pytest.approx(-.1)
    assert report['funding_open'] == 0.
    assert report['funding_paid'] == pytest.approx(-.1)
    assert b.cash == pytest.approx(999.9)
    restored = broker(tmp_path)
    assert restored.funding_total == pytest.approx(-.1)
    assert restored.funding_closed == pytest.approx(-.1)


def test_downtime_marks_every_missed_settlement_without_replaying_latest(tmp_path):
    b = funding_broker(tmp_path)
    now = 103 * 3600 + 1
    b._mark_sample_ms[SYMBOL] = now * 1000
    calls = []
    b._funding_rate_at = lambda market, hour: calls.append(hour) or .01
    with patch('live.paper.time.time', return_value=now):
        b._accrue_funding(b._marks)
    assert set(b._funding_events) == {'ETHUSDT:101', 'ETHUSDT:102', 'ETHUSDT:103'}
    assert b._funding_events['ETHUSDT:101']['status'] == 'missing_boundary_index'
    assert b._funding_events['ETHUSDT:102']['status'] == 'missing_boundary_index'
    assert calls == [103]
    assert b.funding_total == pytest.approx(-.1)
    assert not b.measurements()['funding_ledger_complete']


def test_missing_rate_remains_explicit_and_retries_without_double_payment(tmp_path):
    b = funding_broker(tmp_path)
    now = 101 * 3600 + 1
    b._mark_sample_ms[SYMBOL] = now * 1000
    b._funding_rate_at = lambda market, hour: None
    with patch('live.paper.time.time', return_value=now):
        b._accrue_funding(b._marks)
    assert b.funding_total == 0.
    assert b.measurements()['data_quality']['funding_unresolved_count'] == 1
    b._funding_rate_at = lambda market, hour: .01
    with patch('live.paper.time.time', return_value=now + 61):
        b._accrue_funding(b._marks)
        b._accrue_funding(b._marks)
    assert b.funding_total == pytest.approx(-.1)
    assert b.measurements()['funding_ledger_complete']


def test_funding_must_match_timestamp_not_just_latest_row(tmp_path):
    b = broker(tmp_path)
    b._get = lambda path: {'fundings': [{'timestamp': 100 * 3600, 'rate': .02}]}
    assert b._funding_rate_at(1, 101) is None
    b._get = lambda path: {'fundings': [{'timestamp': 101 * 3600 * 1000, 'rate': .02}]}
    # This synthetic old timestamp is below the epoch-ms heuristic, so test a
    # current-era timestamp for the actual ms contract separately.
    boundary = 1_800_000_000 // 3600 * 3600
    b._get = lambda path: {'fundings': [{'timestamp': boundary * 1000, 'rate': .02}]}
    assert b._funding_rate_at(1, boundary // 3600) == .02


def test_directional_api_funding_is_unresolved_and_raw_sample_preserved(tmp_path):
    b = funding_broker(tmp_path)
    now = 101 * 3600 + 1
    raw = {'timestamp': 101 * 3600, 'value': '0.01', 'rate': '0.01', 'direction': 'short'}
    b._mark_sample_ms[SYMBOL] = now * 1000
    b._get = lambda path: {'fundings': [raw]}
    with patch('live.paper.time.time', return_value=now):
        b._accrue_funding(b._marks)
    assert b.funding_total == 0.
    report = b.measurements()
    assert not report['performance_complete']
    assert report['equity_excludes_unresolved_funding']
    assert b._funding_events['ETHUSDT:101']['status'] == 'unverified_api_semantics'
    assert b._funding_events['ETHUSDT:101']['raw_funding']['raw'] == raw


def test_missing_index_never_falls_back_to_mark_for_funding(tmp_path):
    b = funding_broker(tmp_path)
    now = 101 * 3600 + 1
    b._indexes = {}
    b._mark_sample_ms[SYMBOL] = now * 1000
    b._funding_rate_at = lambda *args: pytest.fail('missing reference must not be charged')
    with patch('live.paper.time.time', return_value=now):
        b._accrue_funding(b._marks)
    assert b._funding_events['ETHUSDT:101']['status'] == 'missing_boundary_index'
    assert b.funding_total == 0.


def test_reports_label_fills_as_simulation_and_include_both_order_directions(tmp_path):
    b = broker(tmp_path)
    enter(b)
    b.market_order(MARKET, SYMBOL, 'SHORT', 10., True, 2, 100.)
    m = b.measurements()
    assert m['slippage_by_role_direction']['ENTRY:SELL']['n'] == 1
    assert m['slippage_by_role_direction']['EXIT:BUY']['n'] == 1
    assert not m['data_quality']['actual_fill_evidence']
    assert m['trigger_to_fill_bps']['evidence'] == 'SIMULATED_PRICE_GAP_NOT_LATENCY'
    # Strict JSON output: no NaNs masquerading as observations.
    json.dumps(m, allow_nan=False)

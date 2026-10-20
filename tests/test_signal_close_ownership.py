"""N1 regression using actual indicators over synthetic OHLCV, no live orders."""
from decimal import Decimal
import numpy as np
import pandas as pd
import pytest
from test_strategy_integration import START, H4, Venue, rows, setup
from test_live import frames
from data_io import SYMBOLS
from frontier_engine import Market, Strategy, run
import bb_squeeze_combined_v6e_dynamic as frozen

MARKS = {'BTCUSDT': 100, 'ETHUSDT': 100}


class ControlledPriceVenue(Venue):
    fill_price = Decimal('100')

    def fill(self, coi, qty):
        super().fill(coi, qty)
        row = self.rows[coi]
        row['filled_quote_amount'] = str(Decimal(row['filled_base_amount']) * self.fill_price)
        self.trades[-1].update(price=str(self.fill_price),
                              usd_amount=str(Decimal(qty) * self.fill_price))


@pytest.mark.parametrize("service_before_bar", [False, True])
def test_stop_after_signal_close_cannot_reenter_a_previously_held_bar(tmp_path, service_before_bar):
    # Produce consecutive short signals with the real, frozen indicator builder.
    # These are synthetic OHLCV bars, not forced signal flags or actual history.
    n = 600
    index = np.arange(n)
    amplitude = 4 * (1 - index / (n - 2)) + .02
    closes = np.linspace(120, 100, n) + np.sin(index / 3) * amplitude
    closes[-2:] = [99.5, 99.45]
    opens = np.r_[closes[0], closes[:-1]]
    times = START + (index - 598) * H4
    raw = pd.DataFrame({'timestamp': pd.to_datetime(times, unit='ms', utc=True),
        'open': opens, 'high': np.maximum(opens, closes) + amplitude * .1,
        'low': np.minimum(opens, closes) - amplitude * .1, 'close': closes,
        'volume': np.r_[np.full(n - 2, 100), 500, 600]})
    prepared = frozen.prepare_symbol_data(raw, True, True)
    prepared['time'] = times
    prepared['x_ma200'] = raw.close.rolling(200).mean()
    prepared['x_rvol'] = raw.volume / raw.volume.rolling(20).mean().shift(1)
    prepared['x_bb_lower'] = raw.close.rolling(20).mean() - 2 * raw.close.rolling(20).std(ddof=0)
    prepared = prepared.set_index('time', drop=False)
    assert prepared.loc[[START, START + H4], 'short_signal'].all()
    assert not prepared.loc[[START, START + H4], 'long_breakout'].any()
    first_rows = rows(short=False)
    first_rows['ETHUSDT'] = prepared.loc[START].to_dict()
    second_rows = rows(short=False)
    second_rows['ETHUSDT'] = prepared.loc[START + H4].to_dict()
    venue, broker, trader = setup(tmp_path, ControlledPriceVenue())
    try:
        venue.fill_price = Decimal('99.5')
        first = trader.on_bar(START, first_rows, {**MARKS, 'ETHUSDT': 99.5}, now_ms=venue.now)
        assert len(first.entries) == 1
        signal_bar = START + H4
        # Position is held at this signal close. Only AFTER the boundary does
        # the native stop fill, before delayed signal processing at +7 seconds.
        next_boundary = signal_bar + H4
        venue.now = next_boundary + 2_000
        venue.fill_price = Decimal('100')
        sl = trader.orders['ETHUSDT']['SL']['client_order_index']
        venue.fill(sl, Decimal(str(trader.positions['ETHUSDT']['qty'])))
        venue.now = next_boundary + 7_000
        if service_before_bar:
            trader.service(now_ms=venue.now, marks=MARKS)
        result = trader.on_bar(signal_bar, second_rows, MARKS, now_ms=venue.now)
        assert not trader.halted
        assert len(result.entries) == 0  # Candidate ownership belongs to the signal close.
        assert sum(not order.reduce_only for order in venue.sent) == 1

        data = frames((), bars=3)
        for ts in (START, signal_bar):
            for key in data['ETHUSDT'].columns:
                data['ETHUSDT'].loc[ts, key] = prepared.loc[ts, key]
        # New-bar open100 crosses the native stop around99.5987;
        # no previous-close candidate exists because ETH was held at that close.
        data['ETHUSDT'].loc[next_boundary, ['open', 'high', 'low', 'close']] = [100, 100.1, 100, 100]
        market = Market(data, {s: pd.DataFrame() for s in SYMBOLS},
                        {s: pd.DataFrame() for s in SYMBOLS})
        backtest = run(market, Strategy(risk=.02, total_risk=.04,
            short_btc_bull_risk=.5, fee=0, slippage=0, funding=False),
            START, START + 3 * H4)
        assert len(backtest['trades']) == 1
    finally:
        broker.close()

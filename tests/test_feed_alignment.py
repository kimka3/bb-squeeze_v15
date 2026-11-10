"""A common closed-bar window must survive asynchronous exchange responses."""
import json
import math
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from data_io import H4
from live import feed


def raw_rows(indices):
    rows = []
    for i in indices:
        price = 100 + .01 * i + math.sin(i / 9)
        rows.append([i * H4, str(price), str(price + 2), str(price - 2),
                     str(price + math.sin(i / 7)), str(100 + i % 31)])
    return rows


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def test_end_time_is_sent_unchanged_on_transport_retry(monkeypatch):
    requested = []

    def urlopen(url, **_):
        requested.append(parse_qs(urlsplit(url).query))
        if len(requested) == 1:
            raise OSError('temporary transport failure')
        return Response(raw_rows([1]))

    monkeypatch.setattr(feed.urllib.request, 'urlopen', urlopen)
    monkeypatch.setattr(feed.time, 'sleep', lambda _: None)
    feed._klines_raw('BTCUSDT', '4h', 599, 10, 2, end_time_ms=600 * H4 - 1)
    assert len(requested) == 2
    assert requested[0] == requested[1]
    assert requested[0]['endTime'] == [str(600 * H4 - 1)]
    assert requested[0]['limit'] == ['599']


def test_parallel_symbols_share_one_window_despite_forming_bar_availability(monkeypatch):
    cutoff = 600 * H4
    times_read = []
    requested = []

    def clock():
        times_read.append(True)
        # Any later independent clock read would cross into the next 4h bar.
        return (cutoff + (0 if len(times_read) == 1 else H4) + 100) / 1000

    def urlopen(url, **_):
        query = parse_qs(urlsplit(url).query)
        requested.append(query)
        # BTC already has the forming candle; ETH only has the just-closed one.
        stop = 601 if query['symbol'] == ['BTCUSDT'] else 600
        available = raw_rows(range(stop))
        end = int(query.get('endTime', [str(601 * H4)])[0])
        limit = int(query['limit'][0])
        return Response([row for row in available if row[0] <= end][-limit:])

    monkeypatch.setattr(feed.time, 'time', clock)
    monkeypatch.setattr(feed.urllib.request, 'urlopen', urlopen)
    frames = feed.prepare(['BTCUSDT', 'ETHUSDT'])
    expected = list(range(H4, cutoff, H4))
    assert len(times_read) == 1
    assert len(requested) == 2
    assert all(q['endTime'] == [str(cutoff - 1)] for q in requested)
    assert all(q['limit'] == ['599'] for q in requested)
    assert all(frame.index.tolist() == expected for frame in frames.values())
    assert feed.latest_closed(frames)[0] == cutoff - H4


@pytest.mark.parametrize('indices,error', [
    (range(599), 'closed-bar window mismatch'),  # 599 bars, but target is missing.
    (range(2, 600), 'closed-bar window mismatch'),  # Latest exists, warmup is short.
    ([i for i in range(600) if i != 300], 'gap in 4h klines'),
])
def test_stale_short_or_gapped_window_cannot_reach_indicators(monkeypatch, indices, error):
    monkeypatch.setattr(feed, '_klines_raw', lambda *a, **kw: raw_rows(indices))
    monkeypatch.setattr(feed.frozen, 'prepare_symbol_data',
                        lambda *a: pytest.fail('invalid data reached indicators'))
    with pytest.raises(RuntimeError, match=error) as caught:
        feed.prepare(['BTCUSDT'], now_ms=600 * H4 + 100)
    message = str(caught.value)
    assert f'closed_before_ms={600 * H4}' in message
    assert 'observed_count=' in message
    assert 'observed_first_ms=' in message
    assert 'observed_last_ms=' in message


def test_normal_599_bar_indicator_values_remain_unchanged(monkeypatch):
    # Old normal response: 600 rows including the forming candle, then dropped.
    monkeypatch.setattr(feed, '_klines_raw', lambda *a, **kw: raw_rows(range(1, 601)))
    old_input = feed.fetch_klines('BTCUSDT', now_ms=600 * H4 + 100)
    expected = feed.frozen.prepare_symbol_data(old_input, True, True)
    old_columns = list(expected.columns)
    monkeypatch.setattr(feed, '_klines_raw', lambda *a, **kw: raw_rows(range(1, 600)))
    actual = feed.prepare(['BTCUSDT'], now_ms=600 * H4 + 100)['BTCUSDT']
    assert len(actual) == len(old_input) == 599
    pd.testing.assert_frame_equal(actual[old_columns].reset_index(drop=True),
                                  expected.reset_index(drop=True), check_exact=True)
    assert actual.x_ma200.iloc[-1] == old_input.close.iloc[-200:].mean()

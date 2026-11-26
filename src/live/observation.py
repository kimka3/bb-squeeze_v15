"""Read-only public observations, separate from simulated account executions."""
from __future__ import annotations

import datetime as dt
import gzip
import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .book import fetch_depth, walk
from .markets import marks


def collect(config, markets, equity: float) -> dict:
    started = int(time.time() * 1000)
    if not math.isfinite(equity) or equity <= 0:
        raise ValueError('capacity observation requires positive finite equity')
    mark_prices = marks(config.base_url, markets)
    with ThreadPoolExecutor(max_workers=min(4, len(markets))) as pool:
        futures = {symbol: pool.submit(fetch_depth, config.base_url, market.market_id)
                   for symbol, market in markets.items()}
        depths = {symbol: future.result() for symbol, future in futures.items()}
    rows = []
    for symbol, market in markets.items():
        depth = depths[symbol]
        bids, asks = depth.get('bids') or [], depth.get('asks') or []
        row = {'symbol': symbol, 'market_id': market.market_id, 'mark': mark_prices[symbol],
               'raw_depth': depth, 'quotes': [], 'purpose': 'capacity_probe_not_trade'}
        if bids and asks:
            mid = (float(bids[0]['price']) + float(asks[0]['price'])) / 2
            qty = market.round_size(equity * config.observation_notional_multiple / mid)
            for buying in (False, True):
                quote = walk(depth, qty, buying)
                slip = quote.slippage_bps(mid, buying)
                row['quotes'].append({'direction': 'BUY' if buying else 'SELL',
                                     'requested_qty': qty, 'filled_qty': quote.filled,
                                     'reference_mid': mid, 'vwap': quote.vwap if math.isfinite(quote.vwap) else None,
                                     'slippage_bps': slip if math.isfinite(slip) else None,
                                     'depth_exhausted': quote.exhausted,
                                     'classification': 'snapshot_walk_simulation'})
        else:
            row['data_quality_error'] = 'empty side of order book'
        rows.append(row)
    return {'started_ms': started, 'finished_ms': int(time.time() * 1000),
            'equity_basis': equity, 'notional_multiple': config.observation_notional_multiple,
            'classification': 'observed_public_depth_and_simulated_capacity',
            'atomic_snapshot': False, 'markets': rows}


def save(directory: Path, observation: dict) -> None:
    day = dt.datetime.fromtimestamp(observation['started_ms'] / 1000, dt.timezone.utc).date()
    directory = Path(directory) / 'observations'
    directory.mkdir(parents=True, exist_ok=True)
    # Daily compressed append streams retain original levels for later audits.
    path = directory / f'{day}.jsonl.gz'
    with path.open('ab') as raw:
        with gzip.GzipFile(fileobj=raw, mode='wb') as compressed:
            compressed.write((json.dumps(observation, allow_nan=False) + '\n').encode('utf-8'))
        raw.flush()
        os.fsync(raw.fileno())

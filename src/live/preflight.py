"""Public-data readiness checks. Never constructs an authenticated broker."""
from __future__ import annotations

import importlib.metadata
import json
import math
import platform
import shutil
import tempfile
import time

from . import feed, observation
from .markets import load_markets, marks


def check(config) -> dict:
    result = {'mode': config.mode, 'checked_ms': int(time.time() * 1000),
              'ready': False, 'checks': {}, 'errors': [], 'config': config.describe(),
              'python': platform.python_version(),
              'packages': {p: importlib.metadata.version(p) for p in ('numpy', 'pandas')},
              'actual_exchange_fills_verified': False,
              'note': 'Readiness is not strategy profitability or live execution certification.'}
    if config.mode != 'paper':
        result['errors'].append('preflight requires --mode paper')
        return result
    def fail(stage, exc):
        result['errors'].append(f'{stage}: {type(exc).__name__}: {exc}')
    try:
        config.state_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=config.state_dir) as probe:
            probe.write(b'paper-readiness')
            probe.flush()
        result['checks']['storage'] = {'writable': True,
            'free_bytes': shutil.disk_usage(config.state_dir).free}
    except Exception as exc:
        fail('storage', exc)
    try:
        skew = feed.clock_skew_seconds(config.binance_base_url, config.request_timeout_seconds)
        result['checks']['clock_skew_seconds'] = skew
        if abs(skew) > config.max_clock_skew_seconds:
            raise RuntimeError(f'clock skew exceeds configured limit: {skew:.3f}s')
    except Exception as exc:
        fail('clock', exc)
    markets = None
    try:
        markets = load_markets(config.base_url, config.lighter_universe)
        current = marks(config.base_url, markets)
        if set(current) != set(config.universe) or any(not math.isfinite(x) or x <= 0 for x in current.values()):
            raise RuntimeError('missing or invalid current Lighter mark')
        result['checks']['market_symbols'] = list(markets)
    except Exception as exc:
        fail('markets', exc)
    try:
        frames = feed.prepare(list(config.universe), base_url=config.binance_base_url,
                              timeout=config.request_timeout_seconds, retries=config.feed_retries)
        stamp, rows = feed.latest_closed(frames)
        now_ms = int(time.time() * 1000)
        if now_ms - (stamp + feed.H4) >= feed.H4 or now_ms < stamp + feed.H4:
            raise RuntimeError('latest completed signal bar is stale or in the future')
        result['checks']['latest_closed_bar_ms'] = stamp
        result['checks']['bar_counts'] = {s: len(p) for s, p in frames.items()}
    except Exception as exc:
        fail('bars', exc)
    if markets:
        try:
            sample = observation.collect(config, markets, config.paper_equity)
            if any(r.get('data_quality_error') for r in sample['markets']):
                raise RuntimeError('public depth has an empty side')
            result['checks']['public_depth'] = {'both_sides': True,
                                                 'samples': len(sample['markets'])}
        except Exception as exc:
            fail('depth', exc)
    result['ready'] = not result['errors']
    return result

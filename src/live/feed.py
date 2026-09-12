"""Binance USD-M 4h bars, prepared by the same indicator code as the backtest.

Signals stay on Binance prices because every indicator threshold was validated
there and Lighter's markets are too young to carry the backtested regimes. Only
execution moves to Lighter; stop levels anchor to the Lighter fill price.
"""
from __future__ import annotations

import json
import time
import urllib.request

import pandas as pd

import bb_squeeze_combined_v6e_dynamic as frozen
from data_io import H4, LONGS

BINANCE_FAPI = 'https://fapi.binance.com/fapi/v1/klines'

# prepare_symbol_data needs MA200 plus the 120-bar squeeze percentile; the extra
# columns run_frontier adds need 200 too. 600 bars is a comfortable warmup.
WARMUP_BARS = 600


def fetch_klines(symbol: str, limit: int = WARMUP_BARS, interval: str = '4h',
                 timeout: int = 30, retries: int = 4) -> pd.DataFrame:
    """Closed 4h bars, newest last. The still-forming bar is dropped."""
    url = f'{BINANCE_FAPI}?symbol={symbol}&interval={interval}&limit={min(limit, 1500)}'
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                rows = json.loads(r.read())
            break
        except Exception as exc:                      # network flake, not a data problem
            last = exc
            time.sleep(2 ** attempt)
    else:
        raise RuntimeError(f'{symbol}: Binance klines unavailable: {last}')

    d = pd.DataFrame([r[:6] for r in rows],
                     columns=['time', 'open', 'high', 'low', 'close', 'volume'])
    d['time'] = d.time.astype('int64')
    for c in ['open', 'high', 'low', 'close', 'volume']:
        d[c] = pd.to_numeric(d[c])
    d = d.sort_values('time').drop_duplicates('time').reset_index(drop=True)
    # Binance returns the in-progress bar last. A bar is closed once its open
    # time is more than one interval behind now.
    now = int(time.time() * 1000)
    d = d[d.time + H4 <= now]
    if d.time.diff().dropna().ne(H4).any():
        raise RuntimeError(f'{symbol}: gap in 4h klines')
    d['timestamp'] = pd.to_datetime(d.time, unit='ms', utc=True)
    return d.reset_index(drop=True)


def prepare(symbols: list[str]) -> dict[str, pd.DataFrame]:
    """Indicator frames keyed by symbol, indexed by bar open time in ms.

    Calls the backtest's own prepare_symbol_data and reproduces the extra columns
    run_frontier.prepared() adds, so a live row is the same shape and the same
    numbers as a backtest row.
    """
    out = {}
    for s in symbols:
        d = fetch_klines(s)
        p = frozen.prepare_symbol_data(d, s in LONGS, True)
        p['time'] = d.time.to_numpy(dtype='int64')
        # Extras used by the BTC regime filter and entry priority.
        x = d.set_index('time')
        p['x_ma200'] = x.close.rolling(200).mean().to_numpy()
        p['x_rvol'] = (x.volume / x.volume.rolling(20).mean().shift(1)).to_numpy()
        p['x_bb_lower'] = (x.close.rolling(20).mean()
                           - 2 * x.close.rolling(20).std(ddof=0)).to_numpy()
        out[s] = p.set_index('time', drop=False)
    timeline = out[symbols[0]].index
    for s, p in out.items():
        if not p.index.equals(timeline):
            raise RuntimeError(f'{s}: bar timeline does not match {symbols[0]}')
    return out


def latest_closed(frames: dict[str, pd.DataFrame]) -> tuple[int, dict[str, dict]]:
    """(bar open time, row per symbol) for the most recently closed bar."""
    ts = int(max(frames[s].index[-1] for s in frames))
    rows = {}
    for s, p in frames.items():
        if int(p.index[-1]) != ts:
            raise RuntimeError(f'{s}: latest bar {int(p.index[-1])} behind {ts}')
        rows[s] = p.loc[ts].to_dict()
    return ts, rows

"""Binance USD-M 4h bars, prepared by the same indicator code as the backtest.

Signals stay on Binance prices because every indicator threshold was validated
there and Lighter's markets are too young to carry the backtested regimes. Only
execution moves to Lighter; stop levels anchor to the Lighter fill price.
"""
from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

import bb_squeeze_combined_v6e_dynamic as frozen
from data_io import H4, LONGS

# USD-M futures klines from the documented endpoint. Access eligibility must
# be checked from the actual VPS; endpoint denial is a readiness failure.
# Do not silently route policy restrictions through an undocumented web host.
BINANCE_HOSTS = ('https://fapi.binance.com',)

# prepare_symbol_data needs MA200 plus the 120-bar squeeze percentile; the extra
# columns run_frontier adds need 200 too. Historically limit=600 included the
# forming bar, leaving 599 closed bars. Keep that same indicator window.
WARMUP_BARS = 600
CLOSED_WARMUP_BARS = WARMUP_BARS - 1


def _klines_raw(symbol: str, interval: str, limit: int,
                timeout: int, retries: int, hosts=None,
                end_time_ms: int | None = None) -> list:
    """Raw kline rows, trying each host and retrying only what is worth retrying.

    The two failures are different and must not be conflated. A 451/403 is this
    location being restricted: the host will keep refusing, so retrying it wastes
    the whole backoff budget before the working host is ever tried. Anything else
    is a flake and deserves the backoff on the same host.
    """
    last = None
    for host in (hosts or BINANCE_HOSTS):
        url = (f'{host}/fapi/v1/klines?symbol={symbol}'
               f'&interval={interval}&limit={min(limit, 1500)}')
        if end_time_ms is not None:
            url += f'&endTime={int(end_time_ms)}'
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(url, timeout=timeout) as r:
                    payload = json.loads(r.read())
                if not isinstance(payload, list) or not payload:
                    raise ValueError('expected non-empty kline array')
                return payload
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code in (451, 403):
                    raise RuntimeError(f'{symbol}: documented Binance endpoint denied '
                                       f'access ({exc.code}); use an eligible host location') from exc
                if attempt + 1 < retries:
                    time.sleep(2 ** attempt)
            except Exception as exc:          # network flake, not a data problem
                last = exc
                if attempt + 1 < retries:
                    time.sleep(2 ** attempt)
    raise RuntimeError(f'{symbol}: Binance klines unavailable: {last}')


def fetch_klines(symbol: str, limit: int = WARMUP_BARS, interval: str = '4h',
                 timeout: int = 10, retries: int = 2, base_url: str | None = None,
                 now_ms: int | None = None,
                 end_time_ms: int | None = None) -> pd.DataFrame:
    """Closed 4h bars, newest last. The still-forming bar is dropped."""
    if interval != '4h':
        raise ValueError('strategy feed only supports 4h bars')
    now = int(time.time() * 1000) if now_ms is None else now_ms
    kwargs = {'hosts': (base_url,)} if base_url else {}
    if end_time_ms is not None:
        kwargs['end_time_ms'] = end_time_ms
    rows = _klines_raw(symbol, interval, limit, timeout, retries, **kwargs)

    d = pd.DataFrame([r[:6] for r in rows],
                     columns=['time', 'open', 'high', 'low', 'close', 'volume'])
    d['time'] = d.time.astype('int64')
    for c in ['open', 'high', 'low', 'close', 'volume']:
        d[c] = pd.to_numeric(d[c], errors='coerce')
    if d.time.duplicated().any():
        raise RuntimeError(f'{symbol}: duplicate kline timestamp')
    d = d.sort_values('time').reset_index(drop=True)
    if d.time.mod(H4).ne(0).any():
        raise RuntimeError(f'{symbol}: unaligned 4h timestamp')
    for c in ('open', 'high', 'low', 'close', 'volume'):
        if not d[c].map(math.isfinite).all():
            raise RuntimeError(f'{symbol}: non-finite {c}')
    if (d[['open', 'high', 'low', 'close']] <= 0).any().any() or (d.volume < 0).any():
        raise RuntimeError(f'{symbol}: invalid price or volume')
    if (d.high < d[['open', 'close', 'low']].max(axis=1)).any() or (d.low > d[['open', 'close', 'high']].min(axis=1)).any():
        raise RuntimeError(f'{symbol}: inconsistent OHLC')
    # Binance returns the in-progress bar last. A bar is closed once its open
    # time is more than one interval behind now.
    d = d[d.time + H4 <= now]
    if d.empty:
        raise RuntimeError(f'{symbol}: no closed bars; closed_before_ms={now}, '
                           f'end_time_ms={end_time_ms}, returned_count={len(rows)}')
    if d.time.diff().dropna().ne(H4).any():
        bad = d.loc[d.time.diff().ne(H4) & d.time.diff().notna(), 'time']
        raise RuntimeError(f'{symbol}: gap in 4h klines; closed_before_ms={now}, '
                           f'end_time_ms={end_time_ms}, observed_count={len(d)}, '
                           f'observed_first_ms={int(d.time.iloc[0])}, '
                           f'observed_last_ms={int(d.time.iloc[-1])}, '
                           f'gap_before_ms={int(bad.iloc[0])}')
    d['timestamp'] = pd.to_datetime(d.time, unit='ms', utc=True)
    return d.reset_index(drop=True)


def prepare(symbols: list[str], *, base_url=None, timeout=10, retries=2,
            now_ms: int | None = None) -> dict[str, pd.DataFrame]:
    """Indicator frames keyed by symbol, indexed by bar open time in ms.

    Calls the backtest's own prepare_symbol_data and reproduces the extra columns
    run_frontier.prepared() adds, so a live row is the same shape and the same
    numbers as a backtest row.
    """
    if not symbols:
        raise ValueError('no feed symbols')
    # Requests and retries may straddle a boundary or see different availability
    # of the newly forming candle. Pin their common window before starting I/O.
    # Binance identifies klines by open time; endTime excludes the current bar.
    now = int(time.time() * 1000) if now_ms is None else now_ms
    cutoff = now // H4 * H4
    expected_last = cutoff - H4
    expected_first = cutoff - CLOSED_WARMUP_BARS * H4
    # Data loading is read-only and parallel; strategy evaluation stays ordered.
    with ThreadPoolExecutor(max_workers=min(4, len(symbols))) as pool:
        futures = {s: pool.submit(fetch_klines, s, limit=CLOSED_WARMUP_BARS,
                                  timeout=timeout, retries=retries, base_url=base_url,
                                  now_ms=cutoff, end_time_ms=cutoff - 1) for s in symbols}
        loaded = {s: f.result() for s, f in futures.items()}
    # Never silently intersect timelines: a missing current candle must remain
    # a collection failure, not an apparently successful stale decision.
    for s, d in loaded.items():
        first = int(d.time.iloc[0]) if len(d) else None
        last = int(d.time.iloc[-1]) if len(d) else None
        if (len(d) != CLOSED_WARMUP_BARS or first != expected_first
                or last != expected_last):
            raise RuntimeError(f'{s}: closed-bar window mismatch; '
                               f'closed_before_ms={cutoff}, end_time_ms={cutoff - 1}, '
                               f'expected_count={CLOSED_WARMUP_BARS}, '
                               f'expected_first_ms={expected_first}, '
                               f'expected_last_ms={expected_last}, '
                               f'observed_count={len(d)}, observed_first_ms={first}, '
                               f'observed_last_ms={last}')
    out = {}
    for s in symbols:
        d = loaded[s]
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


def clock_skew_seconds(base_url: str, timeout: int = 10) -> float:
    """Compare public server UTC with local request midpoint; no credentials."""
    before = time.time()
    with urllib.request.urlopen(f'{base_url}/fapi/v1/time', timeout=timeout) as response:
        server = float(json.loads(response.read())['serverTime']) / 1000
    after = time.time()
    if not math.isfinite(server):
        raise RuntimeError('invalid Binance server time')
    return server - (before + after) / 2


def latest_closed(frames: dict[str, pd.DataFrame]) -> tuple[int, dict[str, dict]]:
    """(bar open time, row per symbol) for the most recently closed bar."""
    ts = int(max(frames[s].index[-1] for s in frames))
    rows = {}
    for s, p in frames.items():
        if int(p.index[-1]) != ts:
            raise RuntimeError(f'{s}: latest bar {int(p.index[-1])} behind {ts}')
        rows[s] = p.loc[ts].to_dict()
    return ts, rows

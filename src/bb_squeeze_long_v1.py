#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BB SQUEEZE LONG RIGHT-TAIL TEST v1
==================================

Motivation
----------
The short strategy validated well, but LONG and SHORT should not be forced into
symmetric exit logic.

LONG has a structural advantage:
    downside is bounded near -100% on unlevered notional,
    upside is theoretically unbounded.

Therefore this test is designed to preserve RIGHT-TAIL winners rather than
maximize short-term win rate.

Development set
---------------
BTCUSDT ETHUSDT SOLUSDT

Long holdout set
----------------
BNBUSDT XRPUSDT ADAUSDT DOGEUSDT LINKUSDT LTCUSDT BCHUSDT AVAXUSDT

Core long setup
---------------
- BBW percentile <= 20%
- ATR% percentile <= 30%
- >= 3 of previous 5 bars compressed
- close > upper Bollinger Band
- close > MA200
- BBW expansion >= 12% over 3 bars
- MA200 slope > 0

Entry styles
------------
IMMEDIATE:
    breakout close -> buy next bar open

RETEST_3:
    after breakout, wait up to 3 bars
    retest breakout-bar upper-BB level
    require:
        low <= reference * 1.005
        close > reference
        close > open
    then buy next bar open

Trend regime variants
---------------------
SLOPE:
    MA200 slope > 0

GOLDEN:
    MA200 slope > 0 AND MA50 > MA200

Stops
-----
ATR_1.5
ATR_2.0
STRUCT_LOW

STRUCT_LOW:
    lower of trigger-bar low and entry - 0.25 ATR
    (always below entry)

Exits
-----
BB_MID              reference / old behavior
EMA20               medium trend
EMA50               slow trend
ATR_TRAIL_3         long right-tail trend following
TP2R_25_ATR_TRAIL_3
    - stop checked first
    - take only 25% at +2R
    - move remaining stop to breakeven
    - let 75% run on 3 ATR trailing stop

No look-ahead
-------------
- signals are generated at close
- entries execute at next open
- trailing stops update after close and apply from next bar
- on ambiguous same bar stop+TP, STOP is assumed first

Metrics emphasize
-----------------
- PF
- expectancy
- MDD
- max winner
- 95th percentile trade
- skewness
- top-5 winning trades' share of gross profits
- capture of right tail
- holdout robustness

Example
-------
!python /content/bb_squeeze_long_v1.py \
    --start 2021-01-01 \
    --interval 4h

Outputs
-------
output_long_v1/
    full_summary.csv
    holdout_rankings.csv
    dev_rankings.csv
    all11_rankings.csv
    all_trades.csv
    yearly_trade_summary.csv
    top_long_v1.txt
"""

from __future__ import annotations

import argparse
import io
import time
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
try:
    import requests
except ImportError:
    requests = None  # Only legacy network download functions require requests.


BINANCE_FAPI = "https://fapi.binance.com/fapi/v1/klines"

INTERVAL_MS = {
    "1h": 60 * 60 * 1000,
    "2h": 2 * 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "6h": 6 * 60 * 60 * 1000,
    "8h": 8 * 60 * 60 * 1000,
    "12h": 12 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}

DEV_DEFAULT = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
]

HOLDOUT_DEFAULT = [
    "BNBUSDT",
    "XRPUSDT",
    "ADAUSDT",
    "DOGEUSDT",
    "LINKUSDT",
    "LTCUSDT",
    "BCHUSDT",
    "AVAXUSDT",
]

ENTRY_STYLES = [
    "IMMEDIATE",
    "RETEST_3",
]

REGIMES = [
    "SLOPE",
    "GOLDEN",
]

STOP_SCHEMES = [
    "ATR_1.5",
    "ATR_2.0",
    "STRUCT_LOW",
]

EXIT_SCHEMES = [
    "BB_MID",
    "EMA20",
    "EMA50",
    "ATR_TRAIL_3",
    "TP2R_25_ATR_TRAIL_3",
]


@dataclass(frozen=True)
class Params:
    bb_len: int = 20
    bb_std: float = 2.0

    atr_len: int = 14

    ma_fast_len: int = 50
    ma_len: int = 200
    ma_slope_bars: int = 5

    ema20_len: int = 20
    ema50_len: int = 50

    pct_lookback: int = 120

    bbw_pct_max: float = 0.20
    atr_pct_max: float = 0.30

    squeeze_window: int = 5
    squeeze_min_bars: int = 3

    expand_lookback: int = 3
    min_bbw_growth: float = 0.12

    retest_bars: int = 3
    retest_tolerance: float = 0.005

    structural_min_atr: float = 0.25
    trail_atr: float = 3.0

    fee_rate: float = 0.0005
    slippage_rate: float = 0.0002

    max_hold_bars: int = 180


P = Params()


# =============================================================================
# Data
# =============================================================================

def as_utc_timestamp(value: str) -> pd.Timestamp:
    ts = pd.Timestamp(value)

    if ts.tzinfo is None:
        return ts.tz_localize("UTC")

    return ts.tz_convert("UTC")


def utc_ms(value: str) -> int:
    return int(
        as_utc_timestamp(value).timestamp() *
        1000
    )


def _parse_binance_kline_csv_bytes(
    data: bytes,
) -> pd.DataFrame:
    cols = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "taker_base",
        "taker_quote", "ignore",
    ]

    raw = pd.read_csv(
        io.BytesIO(data),
        header=None,
    )

    if raw.shape[1] < 6:
        raise ValueError(
            "Unexpected Binance kline CSV format"
        )

    raw = raw.iloc[
        :,
        :min(raw.shape[1], len(cols)),
    ]

    raw.columns = cols[
        :raw.shape[1]
    ]

    raw["open_time"] = pd.to_numeric(
        raw["open_time"],
        errors="coerce",
    )

    raw = raw.loc[
        raw["open_time"].notna()
    ].copy()

    for c in [
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]:
        raw[c] = pd.to_numeric(
            raw[c],
            errors="coerce",
        )

    med = float(
        raw["open_time"]
        .abs()
        .median()
    )

    if med > 1e15:
        unit = "us"
    elif med > 1e12:
        unit = "ms"
    else:
        unit = "s"

    raw["timestamp"] = pd.to_datetime(
        raw["open_time"],
        unit=unit,
        utc=True,
    )

    return (
        raw[
            [
                "timestamp",
                "open",
                "high",
                "low",
                "close",
                "volume",
            ]
        ]
        .dropna()
        .drop_duplicates(
            "timestamp"
        )
        .sort_values(
            "timestamp"
        )
        .reset_index(
            drop=True
        )
    )


def _download_zip_csv(
    session: requests.Session,
    url: str,
) -> Optional[pd.DataFrame]:
    r = session.get(
        url,
        timeout=30,
    )

    if r.status_code == 404:
        return None

    r.raise_for_status()

    with zipfile.ZipFile(
        io.BytesIO(r.content)
    ) as zf:

        names = [
            n
            for n in zf.namelist()
            if n.lower().endswith(".csv")
        ]

        if not names:
            raise ValueError(
                f"No CSV in {url}"
            )

        data = zf.read(
            names[0]
        )

    return _parse_binance_kline_csv_bytes(
        data
    )


def fetch_binance_vision_klines(
    symbol: str,
    interval: str,
    start: str,
    end: Optional[str],
) -> pd.DataFrame:

    start_ts = as_utc_timestamp(
        start
    )

    end_ts = (
        pd.Timestamp.now(
            tz="UTC"
        )
        if end is None
        else as_utc_timestamp(
            end
        )
    )

    symbol = symbol.upper()

    sess = requests.Session()

    sess.headers.update({
        "User-Agent": "bb-squeeze-long-v1/1.0"
    })

    frames: List[pd.DataFrame] = []

    month_cursor = pd.Timestamp(
        year=start_ts.year,
        month=start_ts.month,
        day=1,
        tz="UTC",
    )

    while month_cursor < end_ts:
        if month_cursor.month == 12:
            next_month = pd.Timestamp(
                year=month_cursor.year + 1,
                month=1,
                day=1,
                tz="UTC",
            )
        else:
            next_month = pd.Timestamp(
                year=month_cursor.year,
                month=month_cursor.month + 1,
                day=1,
                tz="UTC",
            )

        yyyy = month_cursor.year
        mm = month_cursor.month

        monthly_url = (
            "https://data.binance.vision/"
            "data/futures/um/monthly/klines/"
            f"{symbol}/{interval}/"
            f"{symbol}-{interval}-{yyyy}-{mm:02d}.zip"
        )

        monthly = None

        try:
            monthly = _download_zip_csv(
                sess,
                monthly_url,
            )
        except requests.RequestException as e:
            print(
                f"[vision] monthly issue "
                f"{symbol} {yyyy}-{mm:02d}: "
                f"{type(e).__name__}"
            )

        if monthly is not None:
            frames.append(
                monthly
            )

            print(
                f"[vision] {symbol} {interval} "
                f"monthly {yyyy}-{mm:02d}"
            )

        else:
            d = max(
                start_ts,
                month_cursor,
            ).normalize()

            day_end = min(
                end_ts,
                next_month,
            )

            while d < day_end:
                daily_url = (
                    "https://data.binance.vision/"
                    "data/futures/um/daily/klines/"
                    f"{symbol}/{interval}/"
                    f"{symbol}-{interval}-{d:%Y-%m-%d}.zip"
                )

                daily = None

                try:
                    daily = _download_zip_csv(
                        sess,
                        daily_url,
                    )

                except requests.HTTPError as e:
                    status = (
                        e.response.status_code
                        if e.response is not None
                        else None
                    )

                    if status != 404:
                        raise

                if daily is not None:
                    frames.append(
                        daily
                    )

                d += pd.Timedelta(
                    days=1
                )

                time.sleep(
                    0.005
                )

        month_cursor = next_month

        time.sleep(
            0.01
        )

    if not frames:
        raise RuntimeError(
            f"No Binance Vision data "
            f"for {symbol}"
        )

    df = (
        pd.concat(
            frames,
            ignore_index=True,
        )
        .drop_duplicates(
            "timestamp"
        )
        .sort_values(
            "timestamp"
        )
        .reset_index(
            drop=True
        )
    )

    df = df[
        (df["timestamp"] >= start_ts) &
        (df["timestamp"] <= end_ts)
    ].copy()

    if df.empty:
        raise RuntimeError(
            f"No rows for {symbol}"
        )

    return df.reset_index(
        drop=True
    )


def fetch_binance_klines(
    symbol: str,
    interval: str = "4h",
    start: str = "2021-01-01",
    end: Optional[str] = None,
) -> pd.DataFrame:

    if interval not in INTERVAL_MS:
        raise ValueError(
            f"Unsupported interval: {interval}"
        )

    start_ms = utc_ms(
        start
    )

    end_ms = (
        int(
            pd.Timestamp.now(
                tz="UTC"
            ).timestamp() *
            1000
        )
        if end is None
        else utc_ms(
            end
        )
    )

    rows = []
    cur = start_ms

    sess = requests.Session()

    sess.headers.update({
        "User-Agent": "bb-squeeze-long-v1/1.0"
    })

    try:
        while cur < end_ms:
            params = {
                "symbol": symbol,
                "interval": interval,
                "startTime": cur,
                "endTime": end_ms,
                "limit": 1500,
            }

            r = sess.get(
                BINANCE_FAPI,
                params=params,
                timeout=20,
            )

            if r.status_code == 451:
                print(
                    "[info] Binance REST HTTP 451 "
                    "-> official Data Vision"
                )

                return fetch_binance_vision_klines(
                    symbol,
                    interval,
                    start,
                    end,
                )

            r.raise_for_status()

            batch = r.json()

            if not batch:
                break

            rows.extend(
                batch
            )

            last_open = int(
                batch[-1][0]
            )

            nxt = (
                last_open +
                INTERVAL_MS[interval]
            )

            if nxt <= cur:
                break

            cur = nxt

            if len(batch) < 1500:
                break

            time.sleep(
                0.03
            )

    except requests.RequestException as e:
        print(
            f"[info] REST unavailable "
            f"({type(e).__name__}) "
            "-> Data Vision"
        )

        return fetch_binance_vision_klines(
            symbol,
            interval,
            start,
            end,
        )

    if not rows:
        return fetch_binance_vision_klines(
            symbol,
            interval,
            start,
            end,
        )

    cols = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "taker_base",
        "taker_quote", "ignore",
    ]

    df = pd.DataFrame(
        rows,
        columns=cols,
    )

    df["timestamp"] = pd.to_datetime(
        df["open_time"],
        unit="ms",
        utc=True,
    )

    for c in [
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]:
        df[c] = pd.to_numeric(
            df[c],
            errors="coerce",
        )

    return (
        df[
            [
                "timestamp",
                "open",
                "high",
                "low",
                "close",
                "volume",
            ]
        ]
        .dropna()
        .drop_duplicates(
            "timestamp"
        )
        .sort_values(
            "timestamp"
        )
        .reset_index(
            drop=True
        )
    )


# =============================================================================
# Indicators
# =============================================================================

def rolling_last_percentile(
    s: pd.Series,
    window: int,
) -> pd.Series:

    def pct(
        arr: np.ndarray,
    ) -> float:

        if (
            len(arr) == 0 or
            np.isnan(
                arr[-1]
            )
        ):
            return np.nan

        valid = arr[
            ~np.isnan(
                arr
            )
        ]

        if len(valid) == 0:
            return np.nan

        return float(
            np.sum(
                valid <= arr[-1]
            ) /
            len(valid)
        )

    return s.rolling(
        window,
        min_periods=window,
    ).apply(
        pct,
        raw=True,
    )


def wilder_ema(
    s: pd.Series,
    n: int,
) -> pd.Series:

    return s.ewm(
        alpha=1.0 / n,
        adjust=False,
        min_periods=n,
    ).mean()


def add_indicators(
    raw: pd.DataFrame,
    p: Params = P,
) -> pd.DataFrame:

    x = raw.copy()

    c = x["close"]
    h = x["high"]
    l = x["low"]

    # Bollinger
    x["bb_mid"] = c.rolling(
        p.bb_len
    ).mean()

    sd = c.rolling(
        p.bb_len
    ).std(
        ddof=0
    )

    x["bb_upper"] = (
        x["bb_mid"] +
        p.bb_std *
        sd
    )

    x["bb_lower"] = (
        x["bb_mid"] -
        p.bb_std *
        sd
    )

    x["bbw"] = (
        (
            x["bb_upper"] -
            x["bb_lower"]
        ) /
        x["bb_mid"].replace(
            0,
            np.nan,
        )
    )

    # EMA exits
    x["ema20"] = c.ewm(
        span=p.ema20_len,
        adjust=False,
        min_periods=p.ema20_len,
    ).mean()

    x["ema50"] = c.ewm(
        span=p.ema50_len,
        adjust=False,
        min_periods=p.ema50_len,
    ).mean()

    # ATR
    prev_close = c.shift(
        1
    )

    tr = pd.concat(
        [
            (h - l).abs(),
            (h - prev_close).abs(),
            (l - prev_close).abs(),
        ],
        axis=1,
    ).max(
        axis=1
    )

    x["atr"] = wilder_ema(
        tr,
        p.atr_len,
    )

    x["atr_pct"] = (
        x["atr"] /
        c.replace(
            0,
            np.nan,
        )
    )

    # MAs
    x["ma50"] = c.rolling(
        p.ma_fast_len
    ).mean()

    x["ma200"] = c.rolling(
        p.ma_len
    ).mean()

    x["ma200_slope"] = (
        x["ma200"] -
        x["ma200"].shift(
            p.ma_slope_bars
        )
    )

    # Compression ranks
    x["bbw_pctile"] = (
        rolling_last_percentile(
            x["bbw"],
            p.pct_lookback,
        )
    )

    x["atr_pctile"] = (
        rolling_last_percentile(
            x["atr_pct"],
            p.pct_lookback,
        )
    )

    squeeze = (
        (
            x["bbw_pctile"] <=
            p.bbw_pct_max
        ) &
        (
            x["atr_pctile"] <=
            p.atr_pct_max
        )
    )

    prior = (
        squeeze
        .shift(
            1,
            fill_value=False,
        )
        .astype(
            float
        )
    )

    x["squeeze_count"] = (
        prior
        .rolling(
            p.squeeze_window
        )
        .sum()
    )

    x["squeeze_ready"] = (
        x["squeeze_count"] >=
        p.squeeze_min_bars
    )

    # Expansion
    x["bbw_growth"] = (
        x["bbw"] /
        x["bbw"].shift(
            p.expand_lookback
        ) -
        1.0
    )

    x["expansion_ok"] = (
        (
            x["bbw_growth"] >=
            p.min_bbw_growth
        ) &
        (
            x["bbw"] >
            x["bbw"].shift(
                1
            )
        )
    )

    # Long breakout CORE
    x["long_breakout"] = (
        x["squeeze_ready"] &
        x["expansion_ok"] &
        (
            c >
            x["bb_upper"]
        ) &
        (
            c >
            x["ma200"]
        ) &
        (
            x["ma200_slope"] >
            0
        )
    )

    return x


def regime_ok(
    bar: pd.Series,
    regime: str,
) -> bool:

    if regime == "SLOPE":
        return bool(
            bar["ma200_slope"] >
            0
        )

    if regime == "GOLDEN":
        return bool(
            (
                bar["ma200_slope"] >
                0
            ) and
            (
                bar["ma50"] >
                bar["ma200"]
            )
        )

    raise ValueError(
        regime
    )


# =============================================================================
# Stop / execution
# =============================================================================

def adverse_long_entry(
    raw_price: float,
    slippage: float,
) -> float:

    return raw_price * (
        1 +
        slippage
    )


def adverse_long_exit(
    raw_price: float,
    slippage: float,
) -> float:

    return raw_price * (
        1 -
        slippage
    )


def initial_stop(
    scheme: str,
    entry: float,
    atr_signal: float,
    trigger_low: float,
    p: Params = P,
) -> float:

    if scheme == "ATR_1.5":
        return (
            entry -
            1.5 *
            atr_signal
        )

    if scheme == "ATR_2.0":
        return (
            entry -
            2.0 *
            atr_signal
        )

    if scheme == "STRUCT_LOW":
        return min(
            float(
                trigger_low
            ),
            entry -
            p.structural_min_atr *
            atr_signal,
        )

    raise ValueError(
        scheme
    )


@dataclass
class Trade:
    symbol: str
    universe: str

    entry_style: str
    regime: str
    stop_scheme: str
    exit_scheme: str

    setup_time: pd.Timestamp
    trigger_time: pd.Timestamp
    entry_time: pd.Timestamp
    entry: float

    exit_time: pd.Timestamp
    exit: float

    bars: int
    reason: str

    initial_risk_pct: float

    gross_return: float
    net_return: float

    mfe: float
    mae: float

    partial_taken: bool
    partial_return: float


def backtest_long(
    df: pd.DataFrame,
    symbol: str,
    universe: str,
    entry_style: str,
    regime: str,
    stop_scheme: str,
    exit_scheme: str,
    initial_equity: float = 100_000.0,
    p: Params = P,
) -> Tuple[
    pd.DataFrame,
    pd.DataFrame,
    Dict,
]:

    warmup = max(
        p.ma_len +
        p.ma_slope_bars,
        p.pct_lookback +
        p.bb_len,
        250,
    )

    equity = (
        initial_equity
    )

    eq_rows = [{
        "timestamp": df.iloc[
            warmup
        ]["timestamp"],
        "equity": equity,
    }]

    trades: List[Trade] = []

    pos = None
    pending_entry = None
    pending_exit_reason = None

    # RETEST setup
    setup = None

    for i in range(
        warmup,
        len(df),
    ):
        bar = df.iloc[
            i
        ]

        # ---------------------------------------------------------
        # Execute pending exit at current OPEN
        # ---------------------------------------------------------
        if (
            pos is not None and
            pending_exit_reason is not None
        ):
            exit_px = adverse_long_exit(
                float(
                    bar["open"]
                ),
                p.slippage_rate,
            )

            final_leg = (
                exit_px /
                pos["entry"] -
                1.0
            )

            if pos["partial_taken"]:
                gross = (
                    0.25 *
                    pos["partial_return"] +
                    0.75 *
                    final_leg
                )
            else:
                gross = (
                    final_leg
                )

            net = (
                gross -
                2.0 *
                p.fee_rate
            )

            equity *= (
                1.0 +
                net
            )

            trades.append(
                Trade(
                    symbol=symbol,
                    universe=universe,
                    entry_style=entry_style,
                    regime=regime,
                    stop_scheme=stop_scheme,
                    exit_scheme=exit_scheme,
                    setup_time=pos["setup_time"],
                    trigger_time=pos["trigger_time"],
                    entry_time=pos["entry_time"],
                    entry=pos["entry"],
                    exit_time=bar["timestamp"],
                    exit=exit_px,
                    bars=pos["bars"],
                    reason=pending_exit_reason,
                    initial_risk_pct=pos["initial_risk_pct"],
                    gross_return=gross,
                    net_return=net,
                    mfe=pos["mfe"],
                    mae=pos["mae"],
                    partial_taken=pos["partial_taken"],
                    partial_return=pos["partial_return"],
                )
            )

            pos = None
            pending_exit_reason = None

        # ---------------------------------------------------------
        # Execute pending entry at current OPEN
        # ---------------------------------------------------------
        if (
            pos is None and
            pending_entry is not None
        ):
            entry = adverse_long_entry(
                float(
                    bar["open"]
                ),
                p.slippage_rate,
            )

            stop = initial_stop(
                scheme=stop_scheme,
                entry=entry,
                atr_signal=pending_entry["atr"],
                trigger_low=pending_entry["trigger_low"],
                p=p,
            )

            risk_abs = (
                entry -
                stop
            )

            if risk_abs <= 0:
                risk_abs = (
                    p.structural_min_atr *
                    pending_entry["atr"]
                )

                stop = (
                    entry -
                    risk_abs
                )

            pos = {
                "setup_time": pending_entry["setup_time"],
                "trigger_time": pending_entry["trigger_time"],
                "entry_time": bar["timestamp"],
                "entry": entry,
                "initial_stop": stop,
                "stop": stop,
                "initial_risk_abs": risk_abs,
                "initial_risk_pct": (
                    risk_abs /
                    entry
                ),
                "tp2r": (
                    entry +
                    2.0 *
                    risk_abs
                ),
                "bars": 0,
                "highest_high": float(
                    bar["high"]
                ),
                "mfe": 0.0,
                "mae": 0.0,
                "partial_taken": False,
                "partial_return": 0.0,
            }

            pending_entry = None
            setup = None

        # ---------------------------------------------------------
        # Intrabar stop and TP
        # Conservative: STOP first
        # ---------------------------------------------------------
        if pos is not None:
            pos["bars"] += 1

            ent = (
                pos["entry"]
            )

            favorable = (
                float(
                    bar["high"]
                ) /
                ent -
                1.0
            )

            adverse = (
                float(
                    bar["low"]
                ) /
                ent -
                1.0
            )

            pos["mfe"] = max(
                pos["mfe"],
                favorable,
            )

            pos["mae"] = min(
                pos["mae"],
                adverse,
            )

            if (
                float(
                    bar["low"]
                ) <=
                pos["stop"]
            ):
                raw_exit = min(
                    float(
                        bar["open"]
                    ),
                    pos["stop"],
                )

                exit_px = adverse_long_exit(
                    raw_exit,
                    p.slippage_rate,
                )

                final_leg = (
                    exit_px /
                    ent -
                    1.0
                )

                if pos["partial_taken"]:
                    gross = (
                        0.25 *
                        pos["partial_return"] +
                        0.75 *
                        final_leg
                    )
                else:
                    gross = (
                        final_leg
                    )

                net = (
                    gross -
                    2.0 *
                    p.fee_rate
                )

                equity *= (
                    1.0 +
                    net
                )

                trades.append(
                    Trade(
                        symbol=symbol,
                        universe=universe,
                        entry_style=entry_style,
                        regime=regime,
                        stop_scheme=stop_scheme,
                        exit_scheme=exit_scheme,
                        setup_time=pos["setup_time"],
                        trigger_time=pos["trigger_time"],
                        entry_time=pos["entry_time"],
                        entry=ent,
                        exit_time=bar["timestamp"],
                        exit=exit_px,
                        bars=pos["bars"],
                        reason="STOP",
                        initial_risk_pct=pos["initial_risk_pct"],
                        gross_return=gross,
                        net_return=net,
                        mfe=pos["mfe"],
                        mae=pos["mae"],
                        partial_taken=pos["partial_taken"],
                        partial_return=pos["partial_return"],
                    )
                )

                pos = None
                pending_exit_reason = None

            elif (
                pos is not None and
                exit_scheme ==
                "TP2R_25_ATR_TRAIL_3" and
                not pos["partial_taken"] and
                float(
                    bar["high"]
                ) >=
                pos["tp2r"]
            ):
                tp_px = adverse_long_exit(
                    pos["tp2r"],
                    p.slippage_rate,
                )

                pos["partial_return"] = (
                    tp_px /
                    ent -
                    1.0
                )

                pos["partial_taken"] = (
                    True
                )

                # Remaining 75% stop -> breakeven
                pos["stop"] = max(
                    pos["stop"],
                    ent,
                )

        # ---------------------------------------------------------
        # Close-based exit signals
        # ---------------------------------------------------------
        if pos is not None:
            if exit_scheme == "BB_MID":
                if (
                    float(
                        bar["close"]
                    ) <
                    float(
                        bar["bb_mid"]
                    )
                ):
                    pending_exit_reason = (
                        "BB_MID"
                    )

            elif exit_scheme == "EMA20":
                if (
                    float(
                        bar["close"]
                    ) <
                    float(
                        bar["ema20"]
                    )
                ):
                    pending_exit_reason = (
                        "EMA20"
                    )

            elif exit_scheme == "EMA50":
                if (
                    float(
                        bar["close"]
                    ) <
                    float(
                        bar["ema50"]
                    )
                ):
                    pending_exit_reason = (
                        "EMA50"
                    )

            elif exit_scheme in [
                "ATR_TRAIL_3",
                "TP2R_25_ATR_TRAIL_3",
            ]:
                pass

            else:
                raise ValueError(
                    exit_scheme
                )

            if (
                pos is not None and
                pos["bars"] >=
                p.max_hold_bars and
                pending_exit_reason is None
            ):
                pending_exit_reason = (
                    "TIME"
                )

        # ---------------------------------------------------------
        # Trailing stop update AFTER close
        # ---------------------------------------------------------
        if (
            pos is not None and
            exit_scheme in [
                "ATR_TRAIL_3",
                "TP2R_25_ATR_TRAIL_3",
            ]
        ):
            pos["highest_high"] = max(
                pos["highest_high"],
                float(
                    bar["high"]
                ),
            )

            candidate = (
                pos["highest_high"] -
                p.trail_atr *
                float(
                    bar["atr"]
                )
            )

            # Long trailing stop can only ratchet UP.
            pos["stop"] = max(
                pos["stop"],
                candidate,
            )

            if pos["partial_taken"]:
                pos["stop"] = max(
                    pos["stop"],
                    pos["entry"],
                )

        # ---------------------------------------------------------
        # Entry logic
        # ---------------------------------------------------------
        if (
            pos is None and
            pending_entry is None and
            pending_exit_reason is None and
            i + 1 < len(df)
        ):
            breakout = bool(
                bar["long_breakout"]
            )

            reg_ok = regime_ok(
                bar,
                regime,
            )

            if entry_style == "IMMEDIATE":
                if (
                    breakout and
                    reg_ok
                ):
                    pending_entry = {
                        "setup_time": bar["timestamp"],
                        "trigger_time": bar["timestamp"],
                        "atr": float(
                            bar["atr"]
                        ),
                        "trigger_low": float(
                            bar["low"]
                        ),
                    }

            elif entry_style == "RETEST_3":
                # New breakout starts / refreshes setup.
                if (
                    breakout and
                    reg_ok
                ):
                    setup = {
                        "setup_time": bar["timestamp"],
                        "reference": float(
                            bar["bb_upper"]
                        ),
                        "expires_i": (
                            i +
                            p.retest_bars
                        ),
                    }

                # Test existing setup from bars AFTER setup bar.
                if (
                    setup is not None and
                    bar["timestamp"] >
                    setup["setup_time"]
                ):
                    if i <= setup["expires_i"]:
                        reference = (
                            setup["reference"]
                        )

                        retest_ok = (
                            float(
                                bar["low"]
                            ) <=
                            reference *
                            (
                                1.0 +
                                p.retest_tolerance
                            )
                            and
                            float(
                                bar["close"]
                            ) >
                            reference
                            and
                            float(
                                bar["close"]
                            ) >
                            float(
                                bar["open"]
                            )
                            and
                            regime_ok(
                                bar,
                                regime,
                            )
                        )

                        if retest_ok:
                            pending_entry = {
                                "setup_time": setup["setup_time"],
                                "trigger_time": bar["timestamp"],
                                "atr": float(
                                    bar["atr"]
                                ),
                                "trigger_low": float(
                                    bar["low"]
                                ),
                            }

                            setup = None

                    else:
                        setup = None

            else:
                raise ValueError(
                    entry_style
                )

        # ---------------------------------------------------------
        # MTM equity
        # ---------------------------------------------------------
        mtm = (
            equity
        )

        if pos is not None:
            open_leg = (
                float(
                    bar["close"]
                ) /
                pos["entry"] -
                1.0
            )

            if pos["partial_taken"]:
                gross_open = (
                    0.25 *
                    pos["partial_return"] +
                    0.75 *
                    open_leg
                )
            else:
                gross_open = (
                    open_leg
                )

            mtm = (
                equity *
                (
                    1.0 +
                    gross_open
                )
            )

        eq_rows.append({
            "timestamp": bar["timestamp"],
            "equity": mtm,
        })

    # Final close
    if pos is not None:
        bar = df.iloc[
            -1
        ]

        exit_px = adverse_long_exit(
            float(
                bar["close"]
            ),
            p.slippage_rate,
        )

        final_leg = (
            exit_px /
            pos["entry"] -
            1.0
        )

        if pos["partial_taken"]:
            gross = (
                0.25 *
                pos["partial_return"] +
                0.75 *
                final_leg
            )
        else:
            gross = (
                final_leg
            )

        net = (
            gross -
            2.0 *
            p.fee_rate
        )

        equity *= (
            1.0 +
            net
        )

        trades.append(
            Trade(
                symbol=symbol,
                universe=universe,
                entry_style=entry_style,
                regime=regime,
                stop_scheme=stop_scheme,
                exit_scheme=exit_scheme,
                setup_time=pos["setup_time"],
                trigger_time=pos["trigger_time"],
                entry_time=pos["entry_time"],
                entry=pos["entry"],
                exit_time=bar["timestamp"],
                exit=exit_px,
                bars=pos["bars"],
                reason="EOD",
                initial_risk_pct=pos["initial_risk_pct"],
                gross_return=gross,
                net_return=net,
                mfe=pos["mfe"],
                mae=pos["mae"],
                partial_taken=pos["partial_taken"],
                partial_return=pos["partial_return"],
            )
        )

    tdf = pd.DataFrame(
        [
            asdict(
                t
            )
            for t in trades
        ]
    )

    edf = pd.DataFrame(
        eq_rows
    )

    metrics = calc_metrics(
        tdf,
        edf,
        initial_equity,
    )

    config = (
        f"{entry_style}|"
        f"{regime}|"
        f"{stop_scheme}|"
        f"{exit_scheme}"
    )

    metrics.update({
        "symbol": symbol,
        "universe": universe,
        "entry_style": entry_style,
        "regime": regime,
        "stop_scheme": stop_scheme,
        "exit_scheme": exit_scheme,
        "config": config,
    })

    return (
        tdf,
        edf,
        metrics,
    )


# =============================================================================
# Analytics
# =============================================================================

def profit_factor(
    r: pd.Series,
) -> float:

    gp = (
        r[
            r > 0
        ].sum()
    )

    gl = (
        r[
            r < 0
        ].sum()
    )

    if gl < 0:
        return float(
            gp /
            abs(
                gl
            )
        )

    return (
        np.inf
        if gp > 0
        else np.nan
    )


def calc_metrics(
    trades: pd.DataFrame,
    equity_curve: pd.DataFrame,
    initial_equity: float,
) -> Dict:

    if trades.empty:
        return {
            "trades": 0,
            "total_return_pct": 0.0,
            "win_rate_pct": np.nan,
            "profit_factor": np.nan,
            "expectancy_pct": np.nan,
            "max_drawdown_pct": 0.0,
            "avg_win_pct": np.nan,
            "avg_loss_pct": np.nan,
            "payoff_ratio": np.nan,
            "max_winner_pct": np.nan,
            "p95_trade_pct": np.nan,
            "skewness": np.nan,
            "top5_profit_share_pct": np.nan,
            "avg_hold_bars": np.nan,
            "median_hold_bars": np.nan,
            "avg_initial_risk_pct": np.nan,
            "partial_rate_pct": np.nan,
        }

    r = (
        trades["net_return"]
        .astype(
            float
        )
    )

    wins = r[
        r > 0
    ]

    losses = r[
        r < 0
    ]

    avg_win = (
        wins.mean()
        if len(wins)
        else np.nan
    )

    avg_loss = (
        losses.mean()
        if len(losses)
        else np.nan
    )

    payoff = (
        avg_win /
        abs(
            avg_loss
        )
        if (
            pd.notna(
                avg_win
            ) and
            pd.notna(
                avg_loss
            ) and
            avg_loss != 0
        )
        else np.nan
    )

    final_equity = (
        initial_equity *
        np.prod(
            1.0 +
            r.values
        )
    )

    eq = (
        equity_curve["equity"]
        .astype(
            float
        )
    )

    peak = (
        eq.cummax()
    )

    dd = (
        eq /
        peak -
        1.0
    )

    gross_profit = (
        wins.sum()
    )

    if gross_profit > 0:
        top5_share = (
            wins.nlargest(
                min(
                    5,
                    len(wins),
                )
            ).sum() /
            gross_profit
        )
    else:
        top5_share = (
            np.nan
        )

    skewness = (
        float(
            r.skew()
        )
        if len(r) >= 3
        else np.nan
    )

    return {
        "trades": int(
            len(
                trades
            )
        ),
        "total_return_pct": float(
            (
                final_equity /
                initial_equity -
                1.0
            ) *
            100
        ),
        "win_rate_pct": float(
            (
                r > 0
            ).mean() *
            100
        ),
        "profit_factor": profit_factor(
            r
        ),
        "expectancy_pct": float(
            r.mean() *
            100
        ),
        "max_drawdown_pct": float(
            dd.min() *
            100
        ),
        "avg_win_pct": float(
            avg_win *
            100
        ) if pd.notna(
            avg_win
        ) else np.nan,
        "avg_loss_pct": float(
            avg_loss *
            100
        ) if pd.notna(
            avg_loss
        ) else np.nan,
        "payoff_ratio": float(
            payoff
        ) if pd.notna(
            payoff
        ) else np.nan,
        "max_winner_pct": float(
            r.max() *
            100
        ),
        "p95_trade_pct": float(
            r.quantile(
                0.95
            ) *
            100
        ),
        "skewness": skewness,
        "top5_profit_share_pct": float(
            top5_share *
            100
        ) if pd.notna(
            top5_share
        ) else np.nan,
        "avg_hold_bars": float(
            trades["bars"].mean()
        ),
        "median_hold_bars": float(
            trades["bars"].median()
        ),
        "avg_initial_risk_pct": float(
            trades["initial_risk_pct"].mean() *
            100
        ),
        "partial_rate_pct": float(
            trades["partial_taken"].mean() *
            100
        ),
    }


def make_yearly_summary(
    all_trades: pd.DataFrame,
) -> pd.DataFrame:

    if all_trades.empty:
        return pd.DataFrame()

    x = all_trades.copy()

    x["entry_time"] = pd.to_datetime(
        x["entry_time"],
        utc=True,
    )

    x["year"] = (
        x["entry_time"]
        .dt.year
    )

    rows = []

    group_cols = [
        "symbol",
        "universe",
        "entry_style",
        "regime",
        "stop_scheme",
        "exit_scheme",
        "year",
    ]

    for keys, g in x.groupby(
        group_cols,
        sort=False,
    ):
        vals = dict(
            zip(
                group_cols,
                keys,
            )
        )

        r = (
            g["net_return"]
            .astype(
                float
            )
        )

        vals["config"] = (
            f"{vals['entry_style']}|"
            f"{vals['regime']}|"
            f"{vals['stop_scheme']}|"
            f"{vals['exit_scheme']}"
        )

        vals.update({
            "trades": int(
                len(
                    g
                )
            ),
            "win_rate_pct": float(
                (
                    r > 0
                ).mean() *
                100
            ),
            "profit_factor": profit_factor(
                r
            ),
            "expectancy_pct": float(
                r.mean() *
                100
            ),
            "compounded_return_pct": float(
                (
                    np.prod(
                        1.0 +
                        r.values
                    ) -
                    1.0
                ) *
                100
            ),
        })

        rows.append(
            vals
        )

    return pd.DataFrame(
        rows
    )


def rank_configs(
    full_summary: pd.DataFrame,
    universe: str,
) -> pd.DataFrame:

    if universe == "DEV":
        x = full_summary[
            full_summary["universe"] ==
            "DEV"
        ].copy()

    elif universe == "HOLDOUT":
        x = full_summary[
            full_summary["universe"] ==
            "HOLDOUT"
        ].copy()

    elif universe == "ALL":
        x = full_summary.copy()

    else:
        raise ValueError(
            universe
        )

    rows = []

    for config, g in x.groupby(
        "config",
        sort=False,
    ):
        pf = (
            g["profit_factor"]
            .replace(
                [
                    np.inf,
                    -np.inf,
                ],
                np.nan,
            )
            .dropna()
        )

        rows.append({
            "rank_universe": universe,
            "config": config,
            "entry_style": g["entry_style"].iloc[0],
            "regime": g["regime"].iloc[0],
            "stop_scheme": g["stop_scheme"].iloc[0],
            "exit_scheme": g["exit_scheme"].iloc[0],
            "symbols": int(
                g["symbol"].nunique()
            ),
            "total_trades": int(
                g["trades"].sum()
            ),
            "median_trades_per_symbol": float(
                g["trades"].median()
            ),
            "profitable_symbols": int(
                (
                    g["total_return_pct"] >
                    0
                ).sum()
            ),
            "pf_above_1_symbols": int(
                (
                    g["profit_factor"] >
                    1.0
                ).sum()
            ),
            "median_pf": float(
                pf.median()
            ) if len(
                pf
            ) else np.nan,
            "worst_pf": float(
                pf.min()
            ) if len(
                pf
            ) else np.nan,
            "median_expectancy_pct": float(
                g["expectancy_pct"].median()
            ),
            "worst_expectancy_pct": float(
                g["expectancy_pct"].min()
            ),
            "median_return_pct": float(
                g["total_return_pct"].median()
            ),
            "median_mdd_pct": float(
                g["max_drawdown_pct"].median()
            ),
            "worst_mdd_pct": float(
                g["max_drawdown_pct"].min()
            ),
            "median_max_winner_pct": float(
                g["max_winner_pct"].median()
            ),
            "median_p95_trade_pct": float(
                g["p95_trade_pct"].median()
            ),
            "median_skewness": float(
                g["skewness"].median()
            ),
            "median_top5_profit_share_pct": float(
                g["top5_profit_share_pct"].median()
            ),
        })

    out = pd.DataFrame(
        rows
    )

    # Sparse configs should not win simply from 2-3 lucky trades.
    out["trade_penalty"] = np.where(
        out["median_trades_per_symbol"] <
        8,
        6.0,
        0.0,
    )

    out["r_median_pf"] = (
        out["median_pf"]
        .rank(
            ascending=False,
            method="min",
            na_option="bottom",
        )
    )

    out["r_worst_pf"] = (
        out["worst_pf"]
        .rank(
            ascending=False,
            method="min",
            na_option="bottom",
        )
    )

    out["r_expectancy"] = (
        out["median_expectancy_pct"]
        .rank(
            ascending=False,
            method="min",
            na_option="bottom",
        )
    )

    out["r_mdd"] = (
        out["worst_mdd_pct"]
        .rank(
            ascending=False,
            method="min",
            na_option="bottom",
        )
    )

    out["r_profitable"] = (
        out["profitable_symbols"]
        .rank(
            ascending=False,
            method="min",
        )
    )

    # Right-tail component is deliberately low weight:
    # we want right-tail capture but don't want one lottery winner
    # to dominate selection.
    out["r_p95"] = (
        out["median_p95_trade_pct"]
        .rank(
            ascending=False,
            method="min",
            na_option="bottom",
        )
    )

    out["robust_score"] = (
        out[
            [
                "r_median_pf",
                "r_worst_pf",
                "r_expectancy",
                "r_mdd",
                "r_profitable",
            ]
        ].mean(
            axis=1
        ) +
        0.25 *
        out["r_p95"] +
        out["trade_penalty"]
    )

    out["robust_rank"] = (
        out["robust_score"]
        .rank(
            ascending=True,
            method="min",
        )
        .astype(
            int
        )
    )

    return (
        out.sort_values(
            [
                "robust_rank",
                "median_pf",
            ],
            ascending=[
                True,
                False,
            ],
        )
        .reset_index(
            drop=True
        )
    )


def write_report(
    hold_rank: pd.DataFrame,
    all_rank: pd.DataFrame,
    dev_rank: pd.DataFrame,
    path: Path,
) -> None:

    cols = [
        "robust_rank",
        "config",
        "total_trades",
        "profitable_symbols",
        "pf_above_1_symbols",
        "median_pf",
        "worst_pf",
        "median_expectancy_pct",
        "median_return_pct",
        "median_mdd_pct",
        "worst_mdd_pct",
        "median_max_winner_pct",
        "median_p95_trade_pct",
        "median_skewness",
    ]

    parts = [
        "BB SQUEEZE LONG RIGHT-TAIL v1\n"
    ]

    for title, df in [
        (
            "HOLDOUT TOP 12",
            hold_rank,
        ),
        (
            "ALL 11 TOP 12",
            all_rank,
        ),
        (
            "DEV TOP 10",
            dev_rank,
        ),
    ]:
        parts.append(
            f"\n=== {title} ===\n"
        )

        parts.append(
            df.head(
                12
            )[cols]
            .to_string(
                index=False,
                float_format=lambda v: f"{v:,.3f}",
            )
        )

        parts.append(
            "\n"
        )

    path.write_text(
        "\n".join(
            parts
        ),
        encoding="utf-8",
    )


# =============================================================================
# Main
# =============================================================================

def main():

    ap = argparse.ArgumentParser(
        description=(
            "BB squeeze LONG right-tail strategy research"
        )
    )

    ap.add_argument(
        "--dev-symbols",
        nargs="+",
        default=DEV_DEFAULT,
    )

    ap.add_argument(
        "--holdout-symbols",
        nargs="+",
        default=HOLDOUT_DEFAULT,
    )

    ap.add_argument(
        "--interval",
        default="4h",
    )

    ap.add_argument(
        "--start",
        default="2021-01-01",
    )

    ap.add_argument(
        "--end",
        default=None,
    )

    ap.add_argument(
        "--out",
        type=Path,
        default=Path(
            "output_long_v1"
        ),
    )

    ap.add_argument(
        "--initial-equity",
        type=float,
        default=100_000.0,
    )

    args, unknown = (
        ap.parse_known_args()
    )

    if unknown:
        print(
            f"[info] Ignored notebook/kernel args: "
            f"{unknown}"
        )

    args.out.mkdir(
        parents=True,
        exist_ok=True,
    )

    print(
        "\n======================================================"
    )

    print(
        "BB SQUEEZE LONG RIGHT-TAIL TEST v1"
    )

    print(
        "======================================================"
    )

    print(
        f"\nDEV: {' '.join(args.dev_symbols)}"
    )

    print(
        f"HOLDOUT: {' '.join(args.holdout_symbols)}"
    )

    n_configs = (
        len(
            ENTRY_STYLES
        ) *
        len(
            REGIMES
        ) *
        len(
            STOP_SCHEMES
        ) *
        len(
            EXIT_SCHEMES
        )
    )

    print(
        f"\nConfigs per symbol: {n_configs}"
    )

    print(
        "Goal: preserve right-tail winners, not maximize win rate."
    )

    summary_rows = []
    trade_frames = []

    jobs = (
        [
            (
                s,
                "DEV",
            )
            for s in args.dev_symbols
        ] +
        [
            (
                s,
                "HOLDOUT",
            )
            for s in args.holdout_symbols
        ]
    )

    for symbol, universe in jobs:

        print(
            f"\n[download] {symbol} ({universe}) "
            f"{args.interval} "
            f"{args.start} -> "
            f"{args.end or 'now'}"
        )

        try:
            raw = fetch_binance_klines(
                symbol=symbol,
                interval=args.interval,
                start=args.start,
                end=args.end,
            )

        except Exception as e:
            print(
                f"[WARN] skip {symbol}: "
                f"{type(e).__name__}: {e}"
            )

            continue

        print(
            f"[data] {symbol}: "
            f"{len(raw):,} bars | "
            f"{raw['timestamp'].iloc[0]} -> "
            f"{raw['timestamp'].iloc[-1]}"
        )

        base = add_indicators(
            raw,
            P,
        )

        for entry_style in ENTRY_STYLES:
            for regime in REGIMES:
                for stop_scheme in STOP_SCHEMES:
                    for exit_scheme in EXIT_SCHEMES:

                        trades, equity, metrics = (
                            backtest_long(
                                df=base,
                                symbol=symbol,
                                universe=universe,
                                entry_style=entry_style,
                                regime=regime,
                                stop_scheme=stop_scheme,
                                exit_scheme=exit_scheme,
                                initial_equity=args.initial_equity,
                                p=P,
                            )
                        )

                        summary_rows.append(
                            metrics
                        )

                        if not trades.empty:
                            trade_frames.append(
                                trades
                            )

        local = pd.DataFrame(
            [
                r
                for r in summary_rows
                if r["symbol"] ==
                symbol
            ]
        )

        if not local.empty:
            clean = local.replace(
                [
                    np.inf,
                    -np.inf,
                ],
                np.nan,
            )

            best = clean.sort_values(
                [
                    "profit_factor",
                    "expectancy_pct",
                ],
                ascending=[
                    False,
                    False,
                ],
            ).iloc[
                0
            ]

            print(
                f"[done] {symbol}: "
                f"{len(local)} configs | "
                f"best raw PF={best['profit_factor']:.3f} | "
                f"{best['config']}"
            )

    if not summary_rows:
        raise RuntimeError(
            "No completed backtests."
        )

    full_summary = (
        pd.DataFrame(
            summary_rows
        )
        .sort_values(
            [
                "universe",
                "symbol",
                "config",
            ]
        )
        .reset_index(
            drop=True
        )
    )

    full_summary.to_csv(
        args.out /
        "full_summary.csv",
        index=False,
    )

    if trade_frames:
        all_trades = pd.concat(
            trade_frames,
            ignore_index=True,
        )

        all_trades.to_csv(
            args.out /
            "all_trades.csv",
            index=False,
        )

    else:
        all_trades = pd.DataFrame()

    yearly = make_yearly_summary(
        all_trades
    )

    yearly.to_csv(
        args.out /
        "yearly_trade_summary.csv",
        index=False,
    )

    hold_rank = rank_configs(
        full_summary,
        "HOLDOUT",
    )

    all_rank = rank_configs(
        full_summary,
        "ALL",
    )

    dev_rank = rank_configs(
        full_summary,
        "DEV",
    )

    hold_rank.to_csv(
        args.out /
        "holdout_rankings.csv",
        index=False,
    )

    all_rank.to_csv(
        args.out /
        "all11_rankings.csv",
        index=False,
    )

    dev_rank.to_csv(
        args.out /
        "dev_rankings.csv",
        index=False,
    )

    write_report(
        hold_rank,
        all_rank,
        dev_rank,
        args.out /
        "top_long_v1.txt",
    )

    cols = [
        "robust_rank",
        "config",
        "total_trades",
        "profitable_symbols",
        "pf_above_1_symbols",
        "median_pf",
        "worst_pf",
        "median_expectancy_pct",
        "median_return_pct",
        "median_mdd_pct",
        "worst_mdd_pct",
        "median_max_winner_pct",
        "median_p95_trade_pct",
        "median_skewness",
        "median_top5_profit_share_pct",
    ]

    print(
        "\n\n======================================================"
    )

    print(
        "LONG HOLDOUT TOP 12  <-- MOST IMPORTANT"
    )

    print(
        "======================================================"
    )

    print(
        hold_rank.head(
            12
        )[cols]
        .to_string(
            index=False,
            float_format=lambda x: f"{x:,.3f}",
        )
    )

    print(
        "\n\n======================================================"
    )

    print(
        "LONG ALL 11 TOP 12"
    )

    print(
        "======================================================"
    )

    print(
        all_rank.head(
            12
        )[cols]
        .to_string(
            index=False,
            float_format=lambda x: f"{x:,.3f}",
        )
    )

    print(
        "\n\n======================================================"
    )

    print(
        "LONG DEV BTC/ETH/SOL TOP 10"
    )

    print(
        "======================================================"
    )

    print(
        dev_rank.head(
            10
        )[cols]
        .to_string(
            index=False,
            float_format=lambda x: f"{x:,.3f}",
        )
    )

    print(
        "\n\n=== COPY THESE THREE TABLES BACK TO CHATGPT ==="
    )

    print(
        f"\nSaved to: "
        f"{args.out.resolve()}"
    )

    print(
        "\nImportant files:"
    )

    print(
        "  holdout_rankings.csv"
    )

    print(
        "  all11_rankings.csv"
    )

    print(
        "  dev_rankings.csv"
    )

    print(
        "  yearly_trade_summary.csv"
    )

    print(
        "  all_trades.csv"
    )

    print(
        "  top_long_v1.txt"
    )


if __name__ == "__main__":
    main()

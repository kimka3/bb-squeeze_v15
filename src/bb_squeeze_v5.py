#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BB SQUEEZE SHORT STRATEGY ROBUSTNESS TEST v5
============================================

Why v5
------
v4 showed the strongest and most consistent edge in SHORT_ONLY strategies.

Selected entry candidates from v4:
    09_EXP_SLOPE
    11_EXP_ADX_SLOPE
    15_EXP_RVOL_ADX_SLOPE

v5 DOES NOT optimize entry thresholds further.

Instead it tests:
    - 6 stop structures
    - 4 exit structures
    - BTC / ETH / SOL (development set)
    - 8 previously-unused holdout coins
    - calendar-year stability
    - pre-2024 vs 2024+ temporal stability

This is intended to answer:
    "Is the edge real and robust, or did BTC/ETH/SOL happen to fit it?"

IMPORTANT:
The 2024+ split is only a temporal stability check, NOT a pristine OOS test,
because v4 candidate selection already used the whole BTC/ETH/SOL history.
The HOLDOUT coin set is the cleaner new cross-sectional validation.

Default development symbols
---------------------------
BTCUSDT ETHUSDT SOLUSDT

Default holdout symbols
-----------------------
BNBUSDT XRPUSDT ADAUSDT DOGEUSDT LINKUSDT LTCUSDT BCHUSDT AVAXUSDT

Entry core
----------
Always:
    BBW percentile <= 20%
    ATR% percentile <= 30%
    >= 3 of previous 5 bars compressed
    close below lower Bollinger Band
    close below MA200

Candidate optional filters:
    09: EXP + MA200 slope
    11: EXP + ADX + MA200 slope
    15: EXP + RVOL + ADX + MA200 slope

STOP GRID
---------
ATR_1.0
ATR_1.5
ATR_2.0
ATR_2.5
STRUCT_HIGH
HYBRID_2ATR_STRUCT

STRUCT_HIGH:
    stop = max(signal-bar high, entry + 0.25 ATR)

HYBRID_2ATR_STRUCT:
    tighter of 2 ATR stop and structural high,
    but never closer than entry + 0.25 ATR.

EXIT GRID
---------
BB_MID
EMA10
ATR_TRAIL_2
TP2R_HALF_BE_TRAIL

TP2R_HALF_BE_TRAIL:
    - conservative same-bar ordering: stop is checked BEFORE profit target
    - take 50% at +2R
    - move remaining stop to breakeven
    - trail remaining half using 2 ATR
    - no look-ahead: trailing stop is updated only after bar close

Execution
---------
signal close -> next bar open
fee 0.05% each side
slippage 0.02% each side
max hold 90 bars

Data
----
Binance USD-M REST; on HTTP 451 falls back to official data.binance.vision.

Example
-------
!python /content/bb_squeeze_v5.py \
    --start 2021-01-01 \
    --interval 4h

Outputs
-------
output_v5/
    full_summary.csv
    period_trade_summary.csv
    yearly_trade_summary.csv
    holdout_rankings.csv
    dev_rankings.csv
    all11_rankings.csv
    all_trades.csv
    top_v5.txt
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


# =============================================================================
# Constants / grids
# =============================================================================

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

STOP_SCHEMES = [
    "ATR_1.0",
    "ATR_1.5",
    "ATR_2.0",
    "ATR_2.5",
    "STRUCT_HIGH",
    "HYBRID_2ATR_STRUCT",
]

EXIT_SCHEMES = [
    "BB_MID",
    "EMA10",
    "ATR_TRAIL_2",
    "TP2R_HALF_BE_TRAIL",
]

ENTRY_VARIANTS = {
    # EXP + SLOPE
    "09_EXP_SLOPE": {
        "use_expansion": True,
        "use_rvol": False,
        "use_adx": False,
        "use_slope": True,
    },
    # EXP + ADX + SLOPE
    "11_EXP_ADX_SLOPE": {
        "use_expansion": True,
        "use_rvol": False,
        "use_adx": True,
        "use_slope": True,
    },
    # EXP + RVOL + ADX + SLOPE
    "15_EXP_RVOL_ADX_SLOPE": {
        "use_expansion": True,
        "use_rvol": True,
        "use_adx": True,
        "use_slope": True,
    },
}


@dataclass(frozen=True)
class Params:
    bb_len: int = 20
    bb_std: float = 2.0
    atr_len: int = 14
    adx_len: int = 14
    ema_exit_len: int = 10
    ma_len: int = 200
    volume_len: int = 20
    pct_lookback: int = 120

    bbw_pct_max: float = 0.20
    atr_pct_max: float = 0.30
    squeeze_window: int = 5
    squeeze_min_bars: int = 3

    expand_lookback: int = 3
    min_bbw_growth: float = 0.12

    min_rvol: float = 1.15
    min_adx: float = 18.0
    ma_slope_bars: int = 5

    trail_atr: float = 2.0
    structural_min_atr: float = 0.25

    fee_rate: float = 0.0005
    slippage_rate: float = 0.0002
    max_hold_bars: int = 90


P = Params()


# =============================================================================
# Binance data
# =============================================================================

def as_utc_timestamp(value: str) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def utc_ms(value: str) -> int:
    return int(as_utc_timestamp(value).timestamp() * 1000)


def _parse_binance_kline_csv_bytes(data: bytes) -> pd.DataFrame:
    cols = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "taker_base",
        "taker_quote", "ignore",
    ]

    raw = pd.read_csv(io.BytesIO(data), header=None)

    if raw.shape[1] < 6:
        raise ValueError("Unexpected Binance kline CSV format")

    raw = raw.iloc[:, :min(raw.shape[1], len(cols))]
    raw.columns = cols[:raw.shape[1]]

    raw["open_time"] = pd.to_numeric(raw["open_time"], errors="coerce")
    raw = raw.loc[raw["open_time"].notna()].copy()

    for c in ["open", "high", "low", "close", "volume"]:
        raw[c] = pd.to_numeric(raw[c], errors="coerce")

    med = float(raw["open_time"].abs().median())

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
        raw[["timestamp", "open", "high", "low", "close", "volume"]]
        .dropna()
        .drop_duplicates("timestamp")
        .sort_values("timestamp")
        .reset_index(drop=True)
    )


def _download_zip_csv(
    session: requests.Session,
    url: str,
) -> Optional[pd.DataFrame]:
    r = session.get(url, timeout=30)

    if r.status_code == 404:
        return None

    r.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = [
            n for n in zf.namelist()
            if n.lower().endswith(".csv")
        ]

        if not names:
            raise ValueError(f"No CSV in archive: {url}")

        data = zf.read(names[0])

    return _parse_binance_kline_csv_bytes(data)


def fetch_binance_vision_klines(
    symbol: str,
    interval: str,
    start: str,
    end: Optional[str],
) -> pd.DataFrame:
    start_ts = as_utc_timestamp(start)
    end_ts = (
        pd.Timestamp.now(tz="UTC")
        if end is None
        else as_utc_timestamp(end)
    )

    symbol = symbol.upper()

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "bb-squeeze-v5/1.0"
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
            "https://data.binance.vision/data/futures/um/monthly/klines/"
            f"{symbol}/{interval}/{symbol}-{interval}-{yyyy}-{mm:02d}.zip"
        )

        monthly = None

        try:
            monthly = _download_zip_csv(sess, monthly_url)
        except requests.RequestException as e:
            print(
                f"[vision] monthly issue {symbol} {yyyy}-{mm:02d}: "
                f"{type(e).__name__}"
            )

        if monthly is not None:
            frames.append(monthly)
            print(
                f"[vision] {symbol} {interval} monthly {yyyy}-{mm:02d}"
            )
        else:
            d = max(start_ts, month_cursor).normalize()
            day_end = min(end_ts, next_month)

            while d < day_end:
                daily_url = (
                    "https://data.binance.vision/data/futures/um/daily/klines/"
                    f"{symbol}/{interval}/"
                    f"{symbol}-{interval}-{d:%Y-%m-%d}.zip"
                )

                daily = None

                try:
                    daily = _download_zip_csv(sess, daily_url)
                except requests.HTTPError as e:
                    status = (
                        e.response.status_code
                        if e.response is not None
                        else None
                    )
                    if status != 404:
                        raise

                if daily is not None:
                    frames.append(daily)

                d += pd.Timedelta(days=1)
                time.sleep(0.005)

        month_cursor = next_month
        time.sleep(0.01)

    if not frames:
        raise RuntimeError(
            f"No Binance Data Vision data for {symbol} {interval}"
        )

    df = (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates("timestamp")
        .sort_values("timestamp")
        .reset_index(drop=True)
    )

    df = df[
        (df["timestamp"] >= start_ts) &
        (df["timestamp"] <= end_ts)
    ].copy()

    if df.empty:
        raise RuntimeError(f"No rows for {symbol}")

    return df.reset_index(drop=True)


def fetch_binance_klines(
    symbol: str,
    interval: str = "4h",
    start: str = "2021-01-01",
    end: Optional[str] = None,
) -> pd.DataFrame:
    if interval not in INTERVAL_MS:
        raise ValueError(f"Unsupported interval: {interval}")

    start_ms = utc_ms(start)
    end_ms = (
        int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
        if end is None
        else utc_ms(end)
    )

    rows = []
    cur = start_ms

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "bb-squeeze-v5/1.0"
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
                    "[info] Binance REST HTTP 451 -> Data Vision fallback"
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

            rows.extend(batch)

            last_open = int(batch[-1][0])
            nxt = last_open + INTERVAL_MS[interval]

            if nxt <= cur:
                break

            cur = nxt

            if len(batch) < 1500:
                break

            time.sleep(0.03)

    except requests.RequestException as e:
        print(
            f"[info] REST unavailable ({type(e).__name__}) -> Data Vision"
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

    df = pd.DataFrame(rows, columns=cols)

    df["timestamp"] = pd.to_datetime(
        df["open_time"],
        unit="ms",
        utc=True,
    )

    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    return (
        df[["timestamp", "open", "high", "low", "close", "volume"]]
        .dropna()
        .drop_duplicates("timestamp")
        .sort_values("timestamp")
        .reset_index(drop=True)
    )


# =============================================================================
# Indicators
# =============================================================================

def rolling_last_percentile(
    s: pd.Series,
    window: int,
) -> pd.Series:
    def pct(arr: np.ndarray) -> float:
        if len(arr) == 0 or np.isnan(arr[-1]):
            return np.nan

        valid = arr[~np.isnan(arr)]

        if len(valid) == 0:
            return np.nan

        return float(
            np.sum(valid <= arr[-1]) /
            len(valid)
        )

    return s.rolling(
        window,
        min_periods=window,
    ).apply(pct, raw=True)


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
    v = x["volume"]

    # Bollinger
    x["bb_mid"] = c.rolling(p.bb_len).mean()

    sd = c.rolling(p.bb_len).std(ddof=0)

    x["bb_upper"] = (
        x["bb_mid"] +
        p.bb_std * sd
    )

    x["bb_lower"] = (
        x["bb_mid"] -
        p.bb_std * sd
    )

    x["bbw"] = (
        (x["bb_upper"] - x["bb_lower"]) /
        x["bb_mid"].replace(0, np.nan)
    )

    # EMA exit
    x["ema_exit"] = c.ewm(
        span=p.ema_exit_len,
        adjust=False,
        min_periods=p.ema_exit_len,
    ).mean()

    # ATR
    prev_close = c.shift(1)

    tr = pd.concat(
        [
            (h - l).abs(),
            (h - prev_close).abs(),
            (l - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    x["atr"] = wilder_ema(
        tr,
        p.atr_len,
    )

    x["atr_pct"] = (
        x["atr"] /
        c.replace(0, np.nan)
    )

    # ADX
    up = h.diff()
    dn = -l.diff()

    plus_dm = pd.Series(
        np.where(
            (up > dn) & (up > 0),
            up,
            0.0,
        ),
        index=x.index,
    )

    minus_dm = pd.Series(
        np.where(
            (dn > up) & (dn > 0),
            dn,
            0.0,
        ),
        index=x.index,
    )

    atr_adx = wilder_ema(
        tr,
        p.adx_len,
    )

    plus_di = (
        100.0 *
        wilder_ema(plus_dm, p.adx_len) /
        atr_adx.replace(0, np.nan)
    )

    minus_di = (
        100.0 *
        wilder_ema(minus_dm, p.adx_len) /
        atr_adx.replace(0, np.nan)
    )

    dx = (
        100.0 *
        (plus_di - minus_di).abs() /
        (plus_di + minus_di).replace(0, np.nan)
    )

    x["adx"] = wilder_ema(
        dx,
        p.adx_len,
    )

    # Volume
    x["rvol"] = (
        v /
        v.rolling(
            p.volume_len
        ).mean().shift(1)
    )

    # MA200 and slope
    x["ma"] = c.rolling(
        p.ma_len
    ).mean()

    x["ma_slope"] = (
        x["ma"] -
        x["ma"].shift(
            p.ma_slope_bars
        )
    )

    # Compression percentiles
    x["bbw_pctile"] = rolling_last_percentile(
        x["bbw"],
        p.pct_lookback,
    )

    x["atr_pctile"] = rolling_last_percentile(
        x["atr_pct"],
        p.pct_lookback,
    )

    core_squeeze = (
        (x["bbw_pctile"] <= p.bbw_pct_max) &
        (x["atr_pctile"] <= p.atr_pct_max)
    )

    prior = (
        core_squeeze
        .shift(1, fill_value=False)
        .astype(float)
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

    x["bbw_growth"] = (
        x["bbw"] /
        x["bbw"].shift(
            p.expand_lookback
        ) -
        1.0
    )

    x["expansion_ok"] = (
        (x["bbw_growth"] >= p.min_bbw_growth) &
        (x["bbw"] > x["bbw"].shift(1))
    )

    x["rvol_ok"] = (
        x["rvol"] >= p.min_rvol
    )

    x["adx_ok"] = (
        (x["adx"] >= p.min_adx) &
        (x["adx"] > x["adx"].shift(1))
    )

    x["slope_ok"] = (
        x["ma_slope"] < 0
    )

    # SHORT core direction:
    # lower-band break + below MA200
    x["short_breakout"] = (
        (c < x["bb_lower"]) &
        (c < x["ma"])
    )

    return x


def build_short_signal(
    base: pd.DataFrame,
    variant: str,
) -> pd.DataFrame:
    cfg = ENTRY_VARIANTS[variant]
    x = base.copy()

    sig = (
        x["squeeze_ready"] &
        x["short_breakout"]
    )

    if cfg["use_expansion"]:
        sig &= x["expansion_ok"]

    if cfg["use_rvol"]:
        sig &= x["rvol_ok"]

    if cfg["use_adx"]:
        sig &= x["adx_ok"]

    if cfg["use_slope"]:
        sig &= x["slope_ok"]

    x["short_signal"] = sig

    return x


# =============================================================================
# Stop / exit mechanics
# =============================================================================

def adverse_short_entry(
    raw_price: float,
    slippage: float,
) -> float:
    # Short entry gets worse if sold slightly lower.
    return raw_price * (1 - slippage)


def adverse_short_exit(
    raw_price: float,
    slippage: float,
) -> float:
    # Short exit gets worse if bought slightly higher.
    return raw_price * (1 + slippage)


def stop_from_scheme(
    scheme: str,
    entry: float,
    atr_signal: float,
    signal_high: float,
    p: Params = P,
) -> float:
    if scheme.startswith("ATR_"):
        mult = float(
            scheme.split("_")[1]
        )
        return (
            entry +
            mult * atr_signal
        )

    structural = max(
        float(signal_high),
        entry +
        p.structural_min_atr * atr_signal,
    )

    if scheme == "STRUCT_HIGH":
        return structural

    if scheme == "HYBRID_2ATR_STRUCT":
        atr_stop = (
            entry +
            2.0 * atr_signal
        )

        tighter = min(
            atr_stop,
            structural,
        )

        return max(
            tighter,
            entry +
            p.structural_min_atr * atr_signal,
        )

    raise ValueError(
        f"Unknown stop scheme: {scheme}"
    )


@dataclass
class Trade:
    symbol: str
    universe: str
    entry_variant: str
    stop_scheme: str
    exit_scheme: str

    signal_time: pd.Timestamp
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


def backtest_short(
    df: pd.DataFrame,
    symbol: str,
    universe: str,
    entry_variant: str,
    stop_scheme: str,
    exit_scheme: str,
    initial_equity: float = 100_000.0,
    p: Params = P,
) -> Tuple[pd.DataFrame, pd.DataFrame, Dict]:

    warmup = max(
        p.ma_len + p.ma_slope_bars,
        p.pct_lookback + p.bb_len,
        250,
    )

    equity = initial_equity

    eq_rows = [{
        "timestamp": df.iloc[warmup]["timestamp"],
        "equity": initial_equity,
    }]

    trades: List[Trade] = []

    pos = None
    pending_entry = None
    pending_exit_reason = None

    for i in range(warmup, len(df)):
        bar = df.iloc[i]

        # ---------------------------------------------------------
        # Execute pending close-based exit at OPEN.
        # ---------------------------------------------------------
        if pos is not None and pending_exit_reason is not None:
            raw_exit = float(bar["open"])

            exit_px = adverse_short_exit(
                raw_exit,
                p.slippage_rate,
            )

            final_leg_return = (
                pos["entry"] /
                exit_px -
                1.0
            )

            if pos["partial_taken"]:
                gross = (
                    0.5 *
                    pos["partial_return"] +
                    0.5 *
                    final_leg_return
                )
            else:
                gross = final_leg_return

            net = (
                gross -
                2.0 * p.fee_rate
            )

            equity *= (
                1.0 + net
            )

            trades.append(
                Trade(
                    symbol=symbol,
                    universe=universe,
                    entry_variant=entry_variant,
                    stop_scheme=stop_scheme,
                    exit_scheme=exit_scheme,
                    signal_time=pos["signal_time"],
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
        # Execute pending entry at OPEN.
        # ---------------------------------------------------------
        if pos is None and pending_entry is not None:
            entry = adverse_short_entry(
                float(bar["open"]),
                p.slippage_rate,
            )

            stop = stop_from_scheme(
                scheme=stop_scheme,
                entry=entry,
                atr_signal=pending_entry["atr"],
                signal_high=pending_entry["signal_high"],
                p=p,
            )

            risk_abs = (
                stop -
                entry
            )

            # Safety guard
            if risk_abs <= 0:
                risk_abs = (
                    p.structural_min_atr *
                    pending_entry["atr"]
                )
                stop = (
                    entry +
                    risk_abs
                )

            initial_risk_pct = (
                risk_abs /
                entry
            )

            pos = {
                "signal_time": pending_entry["signal_time"],
                "entry_time": bar["timestamp"],
                "entry": entry,
                "initial_stop": stop,
                "stop": stop,
                "initial_risk_abs": risk_abs,
                "initial_risk_pct": initial_risk_pct,
                "tp2r": (
                    entry -
                    2.0 * risk_abs
                ),
                "bars": 0,
                "lowest_low": float(bar["low"]),
                "mfe": 0.0,
                "mae": 0.0,
                "partial_taken": False,
                "partial_return": 0.0,
            }

            pending_entry = None

        # ---------------------------------------------------------
        # Intrabar management.
        #
        # Conservative ordering:
        #   1) current stop first
        #   2) then TP if stop not hit
        #
        # Trailing stop used during this bar was determined BEFORE
        # this bar. Any new trailing update happens only after close.
        # ---------------------------------------------------------
        if pos is not None:
            pos["bars"] += 1

            ent = pos["entry"]

            favorable = (
                ent /
                float(bar["low"]) -
                1.0
            )

            adverse = (
                ent /
                float(bar["high"]) -
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

            # STOP FIRST
            if (
                float(bar["high"]) >=
                pos["stop"]
            ):
                raw_exit = max(
                    float(bar["open"]),
                    pos["stop"],
                )

                exit_px = adverse_short_exit(
                    raw_exit,
                    p.slippage_rate,
                )

                final_leg_return = (
                    ent /
                    exit_px -
                    1.0
                )

                if pos["partial_taken"]:
                    gross = (
                        0.5 *
                        pos["partial_return"] +
                        0.5 *
                        final_leg_return
                    )
                else:
                    gross = final_leg_return

                net = (
                    gross -
                    2.0 * p.fee_rate
                )

                equity *= (
                    1.0 + net
                )

                trades.append(
                    Trade(
                        symbol=symbol,
                        universe=universe,
                        entry_variant=entry_variant,
                        stop_scheme=stop_scheme,
                        exit_scheme=exit_scheme,
                        signal_time=pos["signal_time"],
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

            # TP after stop check
            elif (
                pos is not None and
                exit_scheme == "TP2R_HALF_BE_TRAIL" and
                not pos["partial_taken"] and
                float(bar["low"]) <=
                pos["tp2r"]
            ):
                raw_tp = pos["tp2r"]

                tp_px = adverse_short_exit(
                    raw_tp,
                    p.slippage_rate,
                )

                tp_ret = (
                    ent /
                    tp_px -
                    1.0
                )

                pos["partial_taken"] = True
                pos["partial_return"] = tp_ret

                # Move remaining stop to nominal breakeven.
                pos["stop"] = min(
                    pos["stop"],
                    ent,
                )

        # ---------------------------------------------------------
        # Close-based exits.
        # ---------------------------------------------------------
        if pos is not None:
            if exit_scheme == "BB_MID":
                if (
                    float(bar["close"]) >
                    float(bar["bb_mid"])
                ):
                    pending_exit_reason = "BB_MID"

            elif exit_scheme == "EMA10":
                if (
                    float(bar["close"]) >
                    float(bar["ema_exit"])
                ):
                    pending_exit_reason = "EMA10"

            elif exit_scheme in [
                "ATR_TRAIL_2",
                "TP2R_HALF_BE_TRAIL",
            ]:
                # No separate close-based exit.
                pass

            else:
                raise ValueError(
                    f"Unknown exit scheme: {exit_scheme}"
                )

            if (
                pos is not None and
                pos["bars"] >= p.max_hold_bars and
                pending_exit_reason is None
            ):
                pending_exit_reason = "TIME"

        # ---------------------------------------------------------
        # Update trailing stop AFTER bar close.
        # This new stop applies starting next bar.
        # ---------------------------------------------------------
        if (
            pos is not None and
            exit_scheme in [
                "ATR_TRAIL_2",
                "TP2R_HALF_BE_TRAIL",
            ]
        ):
            pos["lowest_low"] = min(
                pos["lowest_low"],
                float(bar["low"]),
            )

            candidate_trail = (
                pos["lowest_low"] +
                p.trail_atr *
                float(bar["atr"])
            )

            # Short trailing stop can only ratchet DOWN.
            pos["stop"] = min(
                pos["stop"],
                candidate_trail,
            )

            # After partial TP, keep at least breakeven-or-better.
            if pos["partial_taken"]:
                pos["stop"] = min(
                    pos["stop"],
                    pos["entry"],
                )

        # ---------------------------------------------------------
        # Entry signal at CLOSE -> next OPEN.
        # ---------------------------------------------------------
        if (
            pos is None and
            pending_entry is None and
            pending_exit_reason is None and
            i + 1 < len(df) and
            bool(bar["short_signal"])
        ):
            pending_entry = {
                "signal_time": bar["timestamp"],
                "atr": float(bar["atr"]),
                "signal_high": float(bar["high"]),
            }

        # ---------------------------------------------------------
        # Mark-to-market equity.
        # Approximation for partial mode:
        # if half taken, half return is locked and half is MTM.
        # ---------------------------------------------------------
        mtm = equity

        if pos is not None:
            open_leg = (
                pos["entry"] /
                float(bar["close"]) -
                1.0
            )

            if pos["partial_taken"]:
                gross_open = (
                    0.5 *
                    pos["partial_return"] +
                    0.5 *
                    open_leg
                )
            else:
                gross_open = open_leg

            mtm = (
                equity *
                (1.0 + gross_open)
            )

        eq_rows.append({
            "timestamp": bar["timestamp"],
            "equity": mtm,
        })

    # Final close
    if pos is not None:
        bar = df.iloc[-1]

        exit_px = adverse_short_exit(
            float(bar["close"]),
            p.slippage_rate,
        )

        final_leg_return = (
            pos["entry"] /
            exit_px -
            1.0
        )

        if pos["partial_taken"]:
            gross = (
                0.5 *
                pos["partial_return"] +
                0.5 *
                final_leg_return
            )
        else:
            gross = final_leg_return

        net = (
            gross -
            2.0 * p.fee_rate
        )

        equity *= (
            1.0 + net
        )

        trades.append(
            Trade(
                symbol=symbol,
                universe=universe,
                entry_variant=entry_variant,
                stop_scheme=stop_scheme,
                exit_scheme=exit_scheme,
                signal_time=pos["signal_time"],
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
        [asdict(t) for t in trades]
    )

    edf = pd.DataFrame(eq_rows)

    metrics = calc_full_metrics(
        tdf,
        edf,
        initial_equity,
    )

    metrics.update({
        "symbol": symbol,
        "universe": universe,
        "entry_variant": entry_variant,
        "stop_scheme": stop_scheme,
        "exit_scheme": exit_scheme,
        "config": (
            f"{entry_variant}|"
            f"{stop_scheme}|"
            f"{exit_scheme}"
        ),
    })

    return tdf, edf, metrics


# =============================================================================
# Metrics
# =============================================================================

def profit_factor(
    r: pd.Series,
) -> float:
    wins = r[r > 0].sum()
    losses = r[r < 0].sum()

    if losses < 0:
        return float(
            wins /
            abs(losses)
        )

    return (
        np.inf
        if wins > 0
        else np.nan
    )


def trade_sequence_mdd(
    r: pd.Series,
) -> float:
    if len(r) == 0:
        return 0.0

    eq = pd.Series(
        np.cumprod(
            1.0 + r.values
        )
    )

    peak = eq.cummax()

    dd = (
        eq / peak -
        1.0
    )

    return float(
        dd.min()
    )


def calc_trade_metrics(
    trades: pd.DataFrame,
) -> Dict:
    """
    Trade-sequence metrics.
    Used for time-slice stability reports.
    """
    if trades.empty:
        return {
            "trades": 0,
            "win_rate_pct": np.nan,
            "profit_factor": np.nan,
            "expectancy_pct": np.nan,
            "compounded_return_pct": 0.0,
            "trade_sequence_mdd_pct": 0.0,
            "avg_win_pct": np.nan,
            "avg_loss_pct": np.nan,
            "payoff_ratio": np.nan,
        }

    r = (
        trades["net_return"]
        .astype(float)
    )

    wins = r[r > 0]
    losses = r[r < 0]

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
        avg_win / abs(avg_loss)
        if (
            pd.notna(avg_win) and
            pd.notna(avg_loss) and
            avg_loss != 0
        )
        else np.nan
    )

    return {
        "trades": int(len(trades)),
        "win_rate_pct": float(
            (r > 0).mean() *
            100
        ),
        "profit_factor": profit_factor(r),
        "expectancy_pct": float(
            r.mean() *
            100
        ),
        "compounded_return_pct": float(
            (
                np.prod(
                    1.0 + r.values
                ) -
                1.0
            ) *
            100
        ),
        "trade_sequence_mdd_pct": float(
            trade_sequence_mdd(r) *
            100
        ),
        "avg_win_pct": float(
            avg_win *
            100
        ) if pd.notna(avg_win) else np.nan,
        "avg_loss_pct": float(
            avg_loss *
            100
        ) if pd.notna(avg_loss) else np.nan,
        "payoff_ratio": float(payoff)
        if pd.notna(payoff) else np.nan,
    }


def calc_full_metrics(
    trades: pd.DataFrame,
    equity_curve: pd.DataFrame,
    initial_equity: float,
) -> Dict:
    tm = calc_trade_metrics(
        trades
    )

    if trades.empty:
        tm.update({
            "total_return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "avg_hold_bars": np.nan,
            "avg_initial_risk_pct": np.nan,
            "partial_rate_pct": np.nan,
        })
        return tm

    r = (
        trades["net_return"]
        .astype(float)
    )

    final_equity = (
        initial_equity *
        np.prod(
            1.0 + r.values
        )
    )

    eq = (
        equity_curve["equity"]
        .astype(float)
    )

    peak = eq.cummax()

    dd = (
        eq / peak -
        1.0
    )

    tm.update({
        "total_return_pct": float(
            (
                final_equity /
                initial_equity -
                1.0
            ) *
            100
        ),
        "max_drawdown_pct": float(
            dd.min() *
            100
        ),
        "avg_hold_bars": float(
            trades["bars"].mean()
        ),
        "avg_initial_risk_pct": float(
            trades["initial_risk_pct"].mean() *
            100
        ),
        "partial_rate_pct": float(
            trades["partial_taken"].mean() *
            100
        ),
    })

    return tm


def make_period_trade_summary(
    all_trades: pd.DataFrame,
    split_date: str,
) -> pd.DataFrame:
    """
    Time-slice stability based on entry timestamps.

    NOTE:
    MDD here is CLOSED-TRADE-SEQUENCE MDD, not intratrade mark-to-market MDD.
    """
    if all_trades.empty:
        return pd.DataFrame()

    x = all_trades.copy()

    x["entry_time"] = pd.to_datetime(
        x["entry_time"],
        utc=True,
    )

    split = as_utc_timestamp(
        split_date
    )

    periods = {
        "PRE_SPLIT": (
            x["entry_time"] < split
        ),
        "POST_SPLIT": (
            x["entry_time"] >= split
        ),
    }

    rows = []

    group_cols = [
        "symbol",
        "universe",
        "entry_variant",
        "stop_scheme",
        "exit_scheme",
    ]

    for keys, g in x.groupby(
        group_cols,
        sort=False,
    ):
        base = dict(
            zip(
                group_cols,
                keys,
            )
        )

        base["config"] = (
            f"{base['entry_variant']}|"
            f"{base['stop_scheme']}|"
            f"{base['exit_scheme']}"
        )

        for period_name, mask in periods.items():
            sub = g.loc[
                mask.loc[g.index]
            ].copy()

            m = calc_trade_metrics(
                sub
            )

            rows.append({
                **base,
                "period": period_name,
                **m,
            })

    return pd.DataFrame(rows)


def make_yearly_trade_summary(
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
        "entry_variant",
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

        m = calc_trade_metrics(
            g
        )

        vals["config"] = (
            f"{vals['entry_variant']}|"
            f"{vals['stop_scheme']}|"
            f"{vals['exit_scheme']}"
        )

        rows.append({
            **vals,
            **m,
        })

    return pd.DataFrame(rows)


def rank_configs(
    full_summary: pd.DataFrame,
    universe_filter: str,
) -> pd.DataFrame:
    """
    Cross-symbol robustness ranking.

    universe_filter:
      DEV
      HOLDOUT
      ALL
    """
    if universe_filter == "DEV":
        x = full_summary[
            full_summary["universe"] == "DEV"
        ].copy()

    elif universe_filter == "HOLDOUT":
        x = full_summary[
            full_summary["universe"] == "HOLDOUT"
        ].copy()

    elif universe_filter == "ALL":
        x = full_summary.copy()

    else:
        raise ValueError(
            universe_filter
        )

    rows = []

    for config, g in x.groupby(
        "config",
        sort=False,
    ):
        pf = (
            g["profit_factor"]
            .replace(
                [np.inf, -np.inf],
                np.nan,
            )
        )

        valid_pf = pf.dropna()

        rows.append({
            "universe_ranked": universe_filter,
            "config": config,
            "entry_variant": g["entry_variant"].iloc[0],
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
                (g["total_return_pct"] > 0).sum()
            ),
            "pf_above_1_symbols": int(
                (pf > 1.0).sum()
            ),
            "median_pf": float(
                valid_pf.median()
            ) if len(valid_pf) else np.nan,
            "worst_pf": float(
                valid_pf.min()
            ) if len(valid_pf) else np.nan,
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
            "median_initial_risk_pct": float(
                g["avg_initial_risk_pct"].median()
            ),
        })

    out = pd.DataFrame(rows)

    # Trade-count quality guard:
    # below 5 median trades per symbol is too sparse.
    out["trade_count_penalty"] = np.where(
        out["median_trades_per_symbol"] < 5,
        5.0,
        0.0,
    )

    # Rank components.
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

    out["r_worst_mdd"] = (
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

    out["robust_score"] = (
        out[
            [
                "r_median_pf",
                "r_worst_pf",
                "r_expectancy",
                "r_worst_mdd",
                "r_profitable",
            ]
        ]
        .mean(axis=1) +
        out["trade_count_penalty"]
    )

    out["robust_rank"] = (
        out["robust_score"]
        .rank(
            ascending=True,
            method="min",
        )
        .astype(int)
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
        .reset_index(drop=True)
    )


def temporal_stability_for_configs(
    period_summary: pd.DataFrame,
    universe: str,
) -> pd.DataFrame:
    """
    Summarize PRE_SPLIT and POST_SPLIT cross-symbol stability for each config.
    """
    if period_summary.empty:
        return pd.DataFrame()

    if universe == "DEV":
        x = period_summary[
            period_summary["universe"] == "DEV"
        ].copy()

    elif universe == "HOLDOUT":
        x = period_summary[
            period_summary["universe"] == "HOLDOUT"
        ].copy()

    else:
        x = period_summary.copy()

    rows = []

    for (
        config,
        period,
    ), g in x.groupby(
        [
            "config",
            "period",
        ]
    ):
        pf = (
            g["profit_factor"]
            .replace(
                [np.inf, -np.inf],
                np.nan,
            )
            .dropna()
        )

        rows.append({
            "config": config,
            "period": period,
            "symbols": int(
                g["symbol"].nunique()
            ),
            "total_trades": int(
                g["trades"].sum()
            ),
            "median_pf": float(
                pf.median()
            ) if len(pf) else np.nan,
            "worst_pf": float(
                pf.min()
            ) if len(pf) else np.nan,
            "median_expectancy_pct": float(
                g["expectancy_pct"].median()
            ),
            "positive_expectancy_symbols": int(
                (g["expectancy_pct"] > 0).sum()
            ),
            "median_trade_mdd_pct": float(
                g["trade_sequence_mdd_pct"].median()
            ),
        })

    return pd.DataFrame(rows)


def write_top_report(
    dev_rank: pd.DataFrame,
    hold_rank: pd.DataFrame,
    all_rank: pd.DataFrame,
    temporal_hold: pd.DataFrame,
    path: Path,
) -> None:
    cols = [
        "robust_rank",
        "config",
        "total_trades",
        "profitable_symbols",
        "median_pf",
        "worst_pf",
        "median_expectancy_pct",
        "median_mdd_pct",
        "worst_mdd_pct",
    ]

    lines = []

    lines.append(
        "BB SQUEEZE v5 ROBUSTNESS REPORT\n"
    )

    for title, df in [
        ("HOLDOUT TOP 12", hold_rank),
        ("DEV TOP 12", dev_rank),
        ("ALL 11 COINS TOP 12", all_rank),
    ]:
        lines.append(
            f"\n=== {title} ===\n"
        )

        lines.append(
            df.head(12)[cols]
            .to_string(
                index=False,
                float_format=lambda v: f"{v:,.3f}",
            )
        )

        lines.append("\n")

    if not temporal_hold.empty:
        top_configs = (
            hold_rank
            .head(8)["config"]
            .tolist()
        )

        t = temporal_hold[
            temporal_hold["config"].isin(
                top_configs
            )
        ].copy()

        lines.append(
            "\n=== HOLDOUT TOP-8 TEMPORAL STABILITY ===\n"
        )

        lines.append(
            t[
                [
                    "config",
                    "period",
                    "total_trades",
                    "median_pf",
                    "worst_pf",
                    "median_expectancy_pct",
                    "positive_expectancy_symbols",
                    "median_trade_mdd_pct",
                ]
            ]
            .sort_values(
                [
                    "config",
                    "period",
                ]
            )
            .to_string(
                index=False,
                float_format=lambda v: f"{v:,.3f}",
            )
        )

    path.write_text(
        "\n".join(lines),
        encoding="utf-8",
    )


# =============================================================================
# Main
# =============================================================================

def main():
    ap = argparse.ArgumentParser(
        description=(
            "BB squeeze v5 - short-only stop/exit robustness + holdout validation"
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
        "--split-date",
        default="2024-01-01",
        help=(
            "Temporal stability split. "
            "Not a pristine OOS because v4 used full BTC/ETH/SOL history."
        ),
    )

    ap.add_argument(
        "--out",
        type=Path,
        default=Path("output_v5"),
    )

    ap.add_argument(
        "--initial-equity",
        type=float,
        default=100_000.0,
    )

    args, unknown = ap.parse_known_args()

    if unknown:
        print(
            f"[info] Ignored notebook/kernel args: {unknown}"
        )

    args.out.mkdir(
        parents=True,
        exist_ok=True,
    )

    print("\n==============================================================")
    print("BB SQUEEZE v5 - SHORT STOP/EXIT + HOLDOUT ROBUSTNESS")
    print("==============================================================")

    print(
        f"\nDEV     : {' '.join(args.dev_symbols)}"
    )

    print(
        f"HOLDOUT : {' '.join(args.holdout_symbols)}"
    )

    print(
        f"\nEntry variants: {', '.join(ENTRY_VARIANTS.keys())}"
    )

    print(
        f"Stops: {', '.join(STOP_SCHEMES)}"
    )

    print(
        f"Exits: {', '.join(EXIT_SCHEMES)}"
    )

    print(
        f"\nConfigs per symbol: "
        f"{len(ENTRY_VARIANTS)} x "
        f"{len(STOP_SCHEMES)} x "
        f"{len(EXIT_SCHEMES)} = "
        f"{len(ENTRY_VARIANTS)*len(STOP_SCHEMES)*len(EXIT_SCHEMES)}"
    )

    all_summary_rows = []
    all_trade_frames = []

    symbol_jobs = (
        [
            (s, "DEV")
            for s in args.dev_symbols
        ] +
        [
            (s, "HOLDOUT")
            for s in args.holdout_symbols
        ]
    )

    for symbol, universe in symbol_jobs:
        print(
            f"\n[download] {symbol} ({universe}) "
            f"{args.interval} {args.start} -> {args.end or 'now'}"
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
                f"[WARN] Skipping {symbol}: "
                f"{type(e).__name__}: {e}"
            )
            continue

        print(
            f"[data] {symbol}: {len(raw):,} bars | "
            f"{raw['timestamp'].iloc[0]} -> "
            f"{raw['timestamp'].iloc[-1]}"
        )

        base = add_indicators(
            raw,
            P,
        )

        for entry_variant in ENTRY_VARIANTS:
            signal_df = build_short_signal(
                base,
                entry_variant,
            )

            for stop_scheme in STOP_SCHEMES:
                for exit_scheme in EXIT_SCHEMES:
                    trades, equity, metrics = backtest_short(
                        df=signal_df,
                        symbol=symbol,
                        universe=universe,
                        entry_variant=entry_variant,
                        stop_scheme=stop_scheme,
                        exit_scheme=exit_scheme,
                        initial_equity=args.initial_equity,
                        p=P,
                    )

                    all_summary_rows.append(
                        metrics
                    )

                    if not trades.empty:
                        all_trade_frames.append(
                            trades
                        )

        # Show one progress line only.
        done_symbol = pd.DataFrame(
            [
                r for r in all_summary_rows
                if r["symbol"] == symbol
            ]
        )

        if not done_symbol.empty:
            best_pf_row = (
                done_symbol
                .replace(
                    [np.inf, -np.inf],
                    np.nan,
                )
                .sort_values(
                    "profit_factor",
                    ascending=False,
                )
                .iloc[0]
            )

            print(
                f"[done] {symbol}: "
                f"{len(done_symbol)} configs | "
                f"best raw PF={best_pf_row['profit_factor']:.3f} "
                f"({best_pf_row['config']})"
            )

    if not all_summary_rows:
        raise RuntimeError(
            "No backtests completed."
        )

    full_summary = (
        pd.DataFrame(
            all_summary_rows
        )
        .sort_values(
            [
                "universe",
                "symbol",
                "entry_variant",
                "stop_scheme",
                "exit_scheme",
            ]
        )
        .reset_index(drop=True)
    )

    full_summary.to_csv(
        args.out /
        "full_summary.csv",
        index=False,
    )

    if all_trade_frames:
        all_trades = pd.concat(
            all_trade_frames,
            ignore_index=True,
        )

        all_trades.to_csv(
            args.out /
            "all_trades.csv",
            index=False,
        )
    else:
        all_trades = pd.DataFrame()

    period_summary = make_period_trade_summary(
        all_trades,
        args.split_date,
    )

    period_summary.to_csv(
        args.out /
        "period_trade_summary.csv",
        index=False,
    )

    yearly_summary = make_yearly_trade_summary(
        all_trades,
    )

    yearly_summary.to_csv(
        args.out /
        "yearly_trade_summary.csv",
        index=False,
    )

    dev_rank = rank_configs(
        full_summary,
        "DEV",
    )

    hold_rank = rank_configs(
        full_summary,
        "HOLDOUT",
    )

    all_rank = rank_configs(
        full_summary,
        "ALL",
    )

    dev_rank.to_csv(
        args.out /
        "dev_rankings.csv",
        index=False,
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

    temporal_hold = temporal_stability_for_configs(
        period_summary,
        "HOLDOUT",
    )

    temporal_hold.to_csv(
        args.out /
        "holdout_temporal_stability.csv",
        index=False,
    )

    write_top_report(
        dev_rank=dev_rank,
        hold_rank=hold_rank,
        all_rank=all_rank,
        temporal_hold=temporal_hold,
        path=args.out /
        "top_v5.txt",
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
        "median_initial_risk_pct",
    ]

    print("\n\n==============================================================")
    print("HOLDOUT TOP 12  <-- MOST IMPORTANT")
    print("==============================================================")

    print(
        hold_rank.head(12)[cols]
        .to_string(
            index=False,
            float_format=lambda x: f"{x:,.3f}",
        )
    )

    print("\n\n==============================================================")
    print("ALL 11 COINS TOP 12")
    print("==============================================================")

    print(
        all_rank.head(12)[cols]
        .to_string(
            index=False,
            float_format=lambda x: f"{x:,.3f}",
        )
    )

    print("\n\n==============================================================")
    print("DEV BTC/ETH/SOL TOP 8")
    print("==============================================================")

    print(
        dev_rank.head(8)[cols]
        .to_string(
            index=False,
            float_format=lambda x: f"{x:,.3f}",
        )
    )

    # Temporal stability for the top holdout configs.
    top_configs = (
        hold_rank.head(8)["config"]
        .tolist()
    )

    temporal_top = temporal_hold[
        temporal_hold["config"].isin(
            top_configs
        )
    ].copy()

    print("\n\n==============================================================")
    print("HOLDOUT TOP-8: PRE vs POST SPLIT STABILITY")
    print(f"Split date: {args.split_date}")
    print("NOTE: trade-sequence MDD, not mark-to-market MDD")
    print("==============================================================")

    tcols = [
        "config",
        "period",
        "total_trades",
        "median_pf",
        "worst_pf",
        "median_expectancy_pct",
        "positive_expectancy_symbols",
        "median_trade_mdd_pct",
    ]

    print(
        temporal_top[
            tcols
        ]
        .sort_values(
            [
                "config",
                "period",
            ]
        )
        .to_string(
            index=False,
            float_format=lambda x: f"{x:,.3f}",
        )
    )

    print("\n\n=== COPY THESE 4 TABLES BACK TO CHATGPT ===")

    print(
        f"\nSaved to: {args.out.resolve()}"
    )

    print("\nImportant files:")
    print("  holdout_rankings.csv")
    print("  all11_rankings.csv")
    print("  dev_rankings.csv")
    print("  holdout_temporal_stability.csv")
    print("  yearly_trade_summary.csv")
    print("  all_trades.csv")
    print("  top_v5.txt")


if __name__ == "__main__":
    main()

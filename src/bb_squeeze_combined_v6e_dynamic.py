#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BB SQUEEZE COMBINED PORTFOLIO v6e DYNAMIC-UNIVERSE
================================

Combines the frozen strategies:

LONG (BTC / ETH / SOL only)
---------------------------
RETEST_3 | SLOPE | ATR_1.5 | BB_MID_100

SHORT (11-coin universe)
------------------------
09_EXP_SLOPE | ATR_1.0 | TP2R_HALF_BE_TRAIL

Purpose
-------
Run both in ONE futures account with:
- concurrent positions
- risk-based position sizing
- gross leverage cap
- portfolio risk cap
- no simultaneous long/short on the same symbol
- fees + slippage
- mark-to-market equity
- conservative intrabar drawdown estimate
- risk/leverage grid

This is the first version intended to answer:
    "What happens when Long + Short are actually combined?"
and:
    "What risk-per-trade / leverage cap is reasonable?"

Requires these files in the same Colab directory:
    bb_squeeze_long_v1.py
    bb_squeeze_v5.py
    bb_squeeze_combined_v6.py

Default universes
-----------------
LONG:
    BTCUSDT ETHUSDT SOLUSDT

SHORT:
    BTCUSDT ETHUSDT SOLUSDT BNBUSDT XRPUSDT ADAUSDT DOGEUSDT
    LINKUSDT LTCUSDT BCHUSDT AVAXUSDT

Default risk grid
-----------------
Risk per trade:
    0.25%, 0.50%, 0.75%, 1.00%, 1.25%, 1.50%, 2.00%

Gross exposure caps:
    1.0x, 1.5x, 2.0x, 3.0x, 5.0x

Other portfolio controls:
    max total initial risk = 4%
    max concurrent positions = 6

Important
---------
"max gross leverage" is portfolio NOTIONAL / equity.
It is NOT the exchange leverage selector.

Funding is NOT modeled.
Liquidation is NOT explicitly modeled.
Stops, slippage, fees and gap-safe fills are modeled.

Example
-------
!python /content/bb_squeeze_combined_v6.py \
    --start 2021-01-01 \
    --interval 4h

Outputs
-------
output_combined_v6/
    grid_summary.csv
    top_configs.csv
    baseline_1pct.csv
    best_equity.csv
    best_trades.csv
    top_v6.txt
"""

from __future__ import annotations

import argparse
import importlib
import math
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


# =============================================================================
# Import previously validated strategy modules
# =============================================================================

try:
    longmod = importlib.import_module("bb_squeeze_long_v1")
except ModuleNotFoundError as e:
    raise SystemExit(
        "\nMissing bb_squeeze_long_v1.py.\n"
        "Upload bb_squeeze_long_v1.py, bb_squeeze_v5.py, and "
        "bb_squeeze_combined_v6.py into the same Colab folder.\n"
    ) from e

try:
    shortmod = importlib.import_module("bb_squeeze_v5")
except ModuleNotFoundError as e:
    raise SystemExit(
        "\nMissing bb_squeeze_v5.py.\n"
        "Upload bb_squeeze_long_v1.py, bb_squeeze_v5.py, and "
        "bb_squeeze_combined_v6.py into the same Colab folder.\n"
    ) from e


LONG_SYMBOLS_DEFAULT = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
]

SHORT_SYMBOLS_DEFAULT = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "ADAUSDT",
    "DOGEUSDT",
    "LINKUSDT",
    "LTCUSDT",
    "BCHUSDT",
    "AVAXUSDT",
]

RISK_GRID_DEFAULT = [
    0.0025,
    0.0050,
    0.0075,
    0.0100,
    0.0125,
    0.0150,
    0.0200,
]

LEVERAGE_GRID_DEFAULT = [
    1.0,
    1.5,
    2.0,
    3.0,
    5.0,
]

FEE_RATE = 0.0005
SLIPPAGE_RATE = 0.0002


# =============================================================================
# Data structures
# =============================================================================

@dataclass
class PortfolioTrade:
    symbol: str
    side: str

    signal_time: pd.Timestamp
    entry_time: pd.Timestamp
    entry_price: float

    exit_time: pd.Timestamp
    exit_price: float

    initial_qty: float
    initial_notional: float
    initial_risk_dollars: float
    initial_risk_pct_equity: float

    bars: int
    reason: str

    partial_taken: bool

    gross_pnl: float
    fees: float
    net_pnl: float

    return_on_initial_notional_pct: float
    r_multiple: float


# =============================================================================
# Prepare signals
# =============================================================================

def prepare_symbol_data(
    raw: pd.DataFrame,
    need_long: bool,
    need_short: bool,
) -> pd.DataFrame:
    """
    Build one dataframe per symbol containing OHLC plus all required
    long/short signals and indicator values.
    """

    base = (
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
        .copy()
        .sort_values("timestamp")
        .drop_duplicates("timestamp")
        .reset_index(drop=True)
    )

    out = base.copy()

    # Short columns.
    if need_short:
        s = shortmod.add_indicators(
            base,
            shortmod.P,
        )

        s = shortmod.build_short_signal(
            s,
            "09_EXP_SLOPE",
        )

        out["s_atr"] = s["atr"].values
        out["short_signal"] = s["short_signal"].fillna(False).values

    else:
        out["s_atr"] = np.nan
        out["short_signal"] = False

    # Long columns.
    if need_long:
        l = longmod.add_indicators(
            base,
            longmod.P,
        )

        out["l_atr"] = l["atr"].values
        out["l_bb_upper"] = l["bb_upper"].values
        out["l_bb_mid"] = l["bb_mid"].values
        out["l_ma200"] = l["ma200"].values
        out["l_ma200_slope"] = l["ma200_slope"].values
        out["long_breakout"] = l["long_breakout"].fillna(False).values

    else:
        out["l_atr"] = np.nan
        out["l_bb_upper"] = np.nan
        out["l_bb_mid"] = np.nan
        out["l_ma200"] = np.nan
        out["l_ma200_slope"] = np.nan
        out["long_breakout"] = False

    return out


def load_all_data(
    long_symbols: List[str],
    short_symbols: List[str],
    interval: str,
    start: str,
    end: Optional[str],
) -> Dict[str, pd.DataFrame]:

    all_symbols = list(
        dict.fromkeys(
            long_symbols +
            short_symbols
        )
    )

    result: Dict[str, pd.DataFrame] = {}

    for symbol in all_symbols:
        print(
            f"\n[download] {symbol} {interval} "
            f"{start} -> {end or 'now'}"
        )

        raw = longmod.fetch_binance_klines(
            symbol=symbol,
            interval=interval,
            start=start,
            end=end,
        )

        print(
            f"[data] {symbol}: {len(raw):,} bars | "
            f"{raw['timestamp'].iloc[0]} -> "
            f"{raw['timestamp'].iloc[-1]}"
        )

        df = prepare_symbol_data(
            raw=raw,
            need_long=(
                symbol in long_symbols
            ),
            need_short=(
                symbol in short_symbols
            ),
        )

        df = df.set_index(
            "timestamp",
            drop=False,
        )

        result[symbol] = df

    return result


# =============================================================================
# Portfolio helpers
# =============================================================================

def adverse_entry(
    raw_price: float,
    side: str,
) -> float:

    if side == "LONG":
        return raw_price * (
            1.0 +
            SLIPPAGE_RATE
        )

    return raw_price * (
        1.0 -
        SLIPPAGE_RATE
    )


def adverse_exit(
    raw_price: float,
    side: str,
) -> float:

    if side == "LONG":
        return raw_price * (
            1.0 -
            SLIPPAGE_RATE
        )

    return raw_price * (
        1.0 +
        SLIPPAGE_RATE
    )


def unrealized_pnl(
    pos: dict,
    price: float,
) -> float:

    qty = pos["qty"]

    if pos["side"] == "LONG":
        return (
            price -
            pos["entry"]
        ) * qty

    return (
        pos["entry"] -
        price
    ) * qty


def gross_notional(
    positions: Dict[str, dict],
    bars: Dict[str, pd.Series],
    use_open: bool = False,
) -> float:

    total = 0.0

    for symbol, pos in positions.items():
        if symbol not in bars:
            price = pos["last_price"]
        else:
            price = float(
                bars[symbol][
                    "open"
                    if use_open
                    else "close"
                ]
            )

        total += (
            abs(
                pos["qty"]
            ) *
            price
        )

    return total


def portfolio_equity(
    cash: float,
    positions: Dict[str, dict],
    bars: Dict[str, pd.Series],
    use_open: bool = False,
) -> float:

    eq = cash

    for symbol, pos in positions.items():
        if symbol in bars:
            price = float(
                bars[symbol][
                    "open"
                    if use_open
                    else "close"
                ]
            )
            pos["last_price"] = price
        else:
            price = pos["last_price"]

        eq += unrealized_pnl(
            pos,
            price,
        )

    return eq


def conservative_intrabar_equity(
    cash: float,
    positions: Dict[str, dict],
    bars: Dict[str, pd.Series],
) -> float:
    """
    Conservative portfolio equity estimate within the current bar:
      LONG marked at LOW
      SHORT marked at HIGH

    Positions already stopped out during the bar have been removed before
    this function is called, so this does not let surviving positions exceed
    their stop.
    """

    eq = cash

    for symbol, pos in positions.items():

        if symbol in bars:
            if pos["side"] == "LONG":
                price = float(
                    bars[symbol]["low"]
                )
            else:
                price = float(
                    bars[symbol]["high"]
                )
        else:
            price = pos["last_price"]

        eq += unrealized_pnl(
            pos,
            price,
        )

    return eq


def open_initial_risk_dollars(
    positions: Dict[str, dict],
) -> float:

    total = 0.0

    for pos in positions.values():
        remaining_fraction = (
            pos["qty"] /
            pos["initial_qty"]
            if pos["initial_qty"] > 0
            else 0.0
        )

        total += (
            pos["initial_risk_dollars"] *
            remaining_fraction
        )

    return total


def position_count(
    positions: Dict[str, dict],
) -> int:
    return len(
        positions
    )


# =============================================================================
# Combined portfolio simulation
# =============================================================================


# =============================================================================
# Deterministic same-open execution priority
# =============================================================================
# IMPORTANT:
# v6 originally processed pending entries in dictionary insertion order.
# Therefore, when several symbols signaled for the same next-open and risk /
# leverage / max-position caps bound, merely reordering short_symbols could
# change which positions were filled.
#
# v6d fixes that implementation artifact by using one frozen canonical order.
# This preserves the original baseline priority for the original 11-symbol
# universe, while making results invariant to the *input list order*.
_EXECUTION_PRIORITY_SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "ADAUSDT",
    "DOGEUSDT",
    "LINKUSDT",
    "LTCUSDT",
    "BCHUSDT",
    "AVAXUSDT",
]
_EXECUTION_PRIORITY = {
    symbol: i
    for i, symbol in enumerate(_EXECUTION_PRIORITY_SYMBOLS)
}


def _pending_priority(item):
    symbol, _requests = item
    return (
        _EXECUTION_PRIORITY.get(symbol, 10_000),
        symbol,
    )


def run_portfolio(
    data: Dict[str, pd.DataFrame],
    long_symbols: List[str],
    short_symbols: List[str],
    initial_equity: float,
    risk_per_trade: float,
    max_gross_leverage: float,
    max_total_initial_risk: float,
    max_positions: int,
    short_symbol_gate: Optional[
        Callable[[pd.Timestamp, str], bool]
    ] = None,
) -> Tuple[
    pd.DataFrame,
    pd.DataFrame,
    Dict,
]:

    # Union of timestamps across all symbols.
    timestamps = sorted(
        set().union(
            *[
                set(df.index)
                for df in data.values()
            ]
        )
    )

    if not timestamps:
        raise RuntimeError(
            "No timestamps."
        )

    cash = initial_equity

    positions: Dict[str, dict] = {}

    pending_entries: Dict[
        Tuple[str, str],
        dict
    ] = {}

    # Long retest setup state.
    long_setups: Dict[str, dict] = {}

    trades: List[PortfolioTrade] = []

    equity_rows = []

    skipped_leverage = 0
    skipped_risk = 0
    skipped_positions = 0
    skipped_conflict = 0

    max_concurrent_seen = 0
    max_gross_seen = 0.0

    # Track side net PnL.
    long_net_pnl = 0.0
    short_net_pnl = 0.0

    def bars_at(ts) -> Dict[str, pd.Series]:
        out = {}

        for sym, df in data.items():
            if ts in df.index:
                out[sym] = df.loc[ts]

        return out

    def realize_full_exit(
        symbol: str,
        pos: dict,
        exit_px: float,
        exit_time,
        reason: str,
    ) -> None:
        nonlocal cash, long_net_pnl, short_net_pnl

        qty = pos["qty"]

        if pos["side"] == "LONG":
            gross = (
                exit_px -
                pos["entry"]
            ) * qty
        else:
            gross = (
                pos["entry"] -
                exit_px
            ) * qty

        exit_fee = (
            abs(
                qty *
                exit_px
            ) *
            FEE_RATE
        )

        cash += (
            gross -
            exit_fee
        )

        pos["gross_pnl_accum"] += gross
        pos["fees_accum"] += exit_fee

        net_total = (
            pos["gross_pnl_accum"] -
            pos["fees_accum"]
        )

        initial_risk = pos[
            "initial_risk_dollars"
        ]

        r_multiple = (
            net_total /
            initial_risk
            if initial_risk > 0
            else np.nan
        )

        trade = PortfolioTrade(
            symbol=symbol,
            side=pos["side"],
            signal_time=pos["signal_time"],
            entry_time=pos["entry_time"],
            entry_price=pos["entry"],
            exit_time=exit_time,
            exit_price=exit_px,
            initial_qty=pos["initial_qty"],
            initial_notional=pos["initial_notional"],
            initial_risk_dollars=initial_risk,
            initial_risk_pct_equity=pos[
                "initial_risk_pct_equity"
            ],
            bars=pos["bars"],
            reason=reason,
            partial_taken=pos["partial_taken"],
            gross_pnl=pos["gross_pnl_accum"],
            fees=pos["fees_accum"],
            net_pnl=net_total,
            return_on_initial_notional_pct=(
                100.0 *
                net_total /
                pos["initial_notional"]
                if pos["initial_notional"] > 0
                else np.nan
            ),
            r_multiple=r_multiple,
        )

        trades.append(
            trade
        )

        if pos["side"] == "LONG":
            long_net_pnl += net_total
        else:
            short_net_pnl += net_total

        del positions[
            symbol
        ]

    for ts_index, ts in enumerate(
        timestamps
    ):
        bars = bars_at(
            ts
        )

        # ---------------------------------------------------------
        # 1) Execute prior-close exits at OPEN
        # ---------------------------------------------------------
        for symbol in list(
            positions.keys()
        ):
            pos = positions[
                symbol
            ]

            if symbol not in bars:
                continue

            if pos.get(
                "pending_exit_reason"
            ) is not None:

                raw_exit = float(
                    bars[symbol]["open"]
                )

                exit_px = adverse_exit(
                    raw_exit,
                    pos["side"],
                )

                reason = pos[
                    "pending_exit_reason"
                ]

                realize_full_exit(
                    symbol=symbol,
                    pos=pos,
                    exit_px=exit_px,
                    exit_time=ts,
                    reason=reason,
                )

        # ---------------------------------------------------------
        # 2) Execute pending entries at OPEN
        # ---------------------------------------------------------
        # Resolve same-symbol opposite pending entries conservatively:
        # skip both.
        pending_symbols = {}

        for (
            symbol,
            side
        ), info in list(
            pending_entries.items()
        ):
            pending_symbols.setdefault(
                symbol,
                []
            ).append(
                (
                    side,
                    info,
                )
            )

        for symbol, requests in sorted(
            pending_symbols.items(),
            key=_pending_priority,
        ):
            if symbol not in bars:
                continue

            # Existing position blocks new position on same symbol.
            if symbol in positions:
                for side, _ in requests:
                    pending_entries.pop(
                        (
                            symbol,
                            side,
                        ),
                        None,
                    )
                skipped_conflict += len(
                    requests
                )
                continue

            sides = {
                side
                for side, _
                in requests
            }

            if (
                "LONG" in sides and
                "SHORT" in sides
            ):
                for side, _ in requests:
                    pending_entries.pop(
                        (
                            symbol,
                            side,
                        ),
                        None,
                    )

                skipped_conflict += 2
                continue

            # Only one request remains.
            side, info = requests[0]

            # Remove from queue regardless of fill decision.
            pending_entries.pop(
                (
                    symbol,
                    side,
                ),
                None,
            )

            if (
                position_count(
                    positions
                ) >=
                max_positions
            ):
                skipped_positions += 1
                continue

            raw_open = float(
                bars[symbol]["open"]
            )

            entry_px = adverse_entry(
                raw_open,
                side,
            )

            # Stop distance:
            # LONG  = 1.5 ATR(trigger)
            # SHORT = 1.0 ATR(signal)
            if side == "LONG":
                risk_abs_per_unit = (
                    1.5 *
                    info["atr"]
                )
                stop = (
                    entry_px -
                    risk_abs_per_unit
                )
            else:
                risk_abs_per_unit = (
                    1.0 *
                    info["atr"]
                )
                stop = (
                    entry_px +
                    risk_abs_per_unit
                )

            if (
                not np.isfinite(
                    risk_abs_per_unit
                ) or
                risk_abs_per_unit <= 0
            ):
                continue

            eq_open = portfolio_equity(
                cash,
                positions,
                bars,
                use_open=True,
            )

            if eq_open <= 0:
                continue

            desired_risk = (
                eq_open *
                risk_per_trade
            )

            desired_qty = (
                desired_risk /
                risk_abs_per_unit
            )

            # -----------------------------------------------------
            # Total initial-risk cap
            # -----------------------------------------------------
            current_risk = (
                open_initial_risk_dollars(
                    positions
                )
            )

            max_risk_dollars = (
                eq_open *
                max_total_initial_risk
            )

            available_risk = max(
                0.0,
                max_risk_dollars -
                current_risk,
            )

            qty_by_risk_cap = (
                available_risk /
                risk_abs_per_unit
            )

            # -----------------------------------------------------
            # Gross leverage cap
            # -----------------------------------------------------
            current_gross = gross_notional(
                positions,
                bars,
                use_open=True,
            )

            max_gross_dollars = (
                eq_open *
                max_gross_leverage
            )

            available_notional = max(
                0.0,
                max_gross_dollars -
                current_gross,
            )

            qty_by_leverage = (
                available_notional /
                entry_px
            )

            qty = min(
                desired_qty,
                qty_by_risk_cap,
                qty_by_leverage,
            )

            if qty <= 0:
                if qty_by_risk_cap <= 0:
                    skipped_risk += 1
                if qty_by_leverage <= 0:
                    skipped_leverage += 1
                continue

            # If the cap cuts desired size below 10%, skip noise-sized trade.
            if qty < (
                desired_qty *
                0.10
            ):
                if qty_by_risk_cap < desired_qty:
                    skipped_risk += 1
                if qty_by_leverage < desired_qty:
                    skipped_leverage += 1
                continue

            initial_notional = (
                qty *
                entry_px
            )

            entry_fee = (
                initial_notional *
                FEE_RATE
            )

            cash -= entry_fee

            actual_risk = (
                qty *
                risk_abs_per_unit
            )

            positions[symbol] = {
                "symbol": symbol,
                "side": side,
                "signal_time": info["signal_time"],
                "entry_time": ts,
                "entry": entry_px,
                "qty": qty,
                "initial_qty": qty,
                "initial_notional": initial_notional,
                "initial_stop": stop,
                "stop": stop,
                "initial_risk_dollars": actual_risk,
                "initial_risk_pct_equity": (
                    actual_risk /
                    eq_open
                ),
                "bars": 0,
                "partial_taken": False,
                "tp2r": (
                    entry_px -
                    2.0 *
                    risk_abs_per_unit
                    if side == "SHORT"
                    else np.nan
                ),
                "lowest_low": float(
                    bars[symbol]["low"]
                ),
                "last_price": float(
                    bars[symbol]["open"]
                ),
                "pending_exit_reason": None,
                "gross_pnl_accum": 0.0,
                "fees_accum": entry_fee,
            }

        # ---------------------------------------------------------
        # 3) Intrabar stops / short +2R partial
        # ---------------------------------------------------------
        for symbol in list(
            positions.keys()
        ):
            if symbol not in bars:
                continue

            pos = positions[
                symbol
            ]

            pos["bars"] += 1

            bar = bars[
                symbol
            ]

            if pos["side"] == "LONG":
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

                    exit_px = adverse_exit(
                        raw_exit,
                        "LONG",
                    )

                    realize_full_exit(
                        symbol=symbol,
                        pos=pos,
                        exit_px=exit_px,
                        exit_time=ts,
                        reason="LONG_ATR_STOP",
                    )

            else:
                # Conservative same-bar ordering:
                # STOP before +2R target.
                if (
                    float(
                        bar["high"]
                    ) >=
                    pos["stop"]
                ):
                    raw_exit = max(
                        float(
                            bar["open"]
                        ),
                        pos["stop"],
                    )

                    exit_px = adverse_exit(
                        raw_exit,
                        "SHORT",
                    )

                    realize_full_exit(
                        symbol=symbol,
                        pos=pos,
                        exit_px=exit_px,
                        exit_time=ts,
                        reason="SHORT_STOP",
                    )

                elif (
                    symbol in positions and
                    not pos["partial_taken"] and
                    float(
                        bar["low"]
                    ) <=
                    pos["tp2r"]
                ):
                    # 50% partial at +2R
                    partial_qty = (
                        pos["qty"] *
                        0.50
                    )

                    raw_tp = (
                        pos["tp2r"]
                    )

                    tp_px = adverse_exit(
                        raw_tp,
                        "SHORT",
                    )

                    gross = (
                        pos["entry"] -
                        tp_px
                    ) * partial_qty

                    exit_fee = (
                        partial_qty *
                        tp_px *
                        FEE_RATE
                    )

                    cash += (
                        gross -
                        exit_fee
                    )

                    pos["gross_pnl_accum"] += gross
                    pos["fees_accum"] += exit_fee

                    pos["qty"] -= partial_qty
                    pos["partial_taken"] = True

                    # Remaining runner stop -> BE or better.
                    pos["stop"] = min(
                        pos["stop"],
                        pos["entry"],
                    )

        # ---------------------------------------------------------
        # 4) Close-based exits
        # ---------------------------------------------------------
        for symbol in list(
            positions.keys()
        ):
            if symbol not in bars:
                continue

            pos = positions[
                symbol
            ]

            bar = bars[
                symbol
            ]

            if pos["side"] == "LONG":
                if (
                    np.isfinite(
                        bar["l_bb_mid"]
                    ) and
                    float(
                        bar["close"]
                    ) <
                    float(
                        bar["l_bb_mid"]
                    )
                ):
                    pos[
                        "pending_exit_reason"
                    ] = "LONG_BB_MID"

                elif (
                    pos["bars"] >=
                    180
                ):
                    pos[
                        "pending_exit_reason"
                    ] = "LONG_TIME"

            else:
                if (
                    pos["bars"] >=
                    90
                ):
                    pos[
                        "pending_exit_reason"
                    ] = "SHORT_TIME"

        # ---------------------------------------------------------
        # 5) Short 2ATR trail update AFTER close
        # ---------------------------------------------------------
        for symbol in list(
            positions.keys()
        ):
            if symbol not in bars:
                continue

            pos = positions[
                symbol
            ]

            if pos["side"] != "SHORT":
                continue

            bar = bars[
                symbol
            ]

            pos["lowest_low"] = min(
                pos["lowest_low"],
                float(
                    bar["low"]
                ),
            )

            candidate = (
                pos["lowest_low"] +
                2.0 *
                float(
                    bar["s_atr"]
                )
            )

            if np.isfinite(
                candidate
            ):
                pos["stop"] = min(
                    pos["stop"],
                    candidate,
                )

            if pos["partial_taken"]:
                pos["stop"] = min(
                    pos["stop"],
                    pos["entry"],
                )

        # ---------------------------------------------------------
        # 6) Generate NEW signals at CLOSE
        # ---------------------------------------------------------

        # SHORT 09 signals
        for symbol in short_symbols:
            if symbol not in bars:
                continue

            # Optional dynamic universe gate.
            # Evaluated at SIGNAL CLOSE using only information available
            # through this timestamp. Existing/open positions are never
            # forcibly closed merely because the regime later changes.
            if (
                short_symbol_gate is not None
                and
                not short_symbol_gate(
                    pd.Timestamp(ts),
                    symbol,
                )
            ):
                continue

            # Existing same-symbol position blocks new signal.
            if symbol in positions:
                continue

            # Do not stack duplicate pending request.
            if (
                (
                    symbol,
                    "SHORT",
                )
                in pending_entries
            ):
                continue

            bar = bars[
                symbol
            ]

            if bool(
                bar["short_signal"]
            ):
                atr = float(
                    bar["s_atr"]
                )

                if np.isfinite(
                    atr
                ):
                    pending_entries[
                        (
                            symbol,
                            "SHORT",
                        )
                    ] = {
                        "signal_time": ts,
                        "atr": atr,
                    }

        # LONG RETEST_3 setup / trigger
        for symbol in long_symbols:
            if symbol not in bars:
                continue

            # If position exists, v1 does not create a new setup.
            if symbol in positions:
                long_setups.pop(
                    symbol,
                    None,
                )
                continue

            bar = bars[
                symbol
            ]

            breakout = bool(
                bar["long_breakout"]
            )

            if breakout:
                long_setups[
                    symbol
                ] = {
                    "setup_time": ts,
                    "reference": float(
                        bar["l_bb_upper"]
                    ),
                    "remaining": 3,
                }

                # No same-bar retest.
                continue

            setup = long_setups.get(
                symbol
            )

            if setup is not None:
                if setup[
                    "remaining"
                ] <= 0:
                    long_setups.pop(
                        symbol,
                        None,
                    )

                else:
                    ref = setup[
                        "reference"
                    ]

                    retest_ok = (
                        float(
                            bar["low"]
                        ) <=
                        ref *
                        1.005
                        and
                        float(
                            bar["close"]
                        ) >
                        ref
                        and
                        float(
                            bar["close"]
                        ) >
                        float(
                            bar["open"]
                        )
                        and
                        float(
                            bar["close"]
                        ) >
                        float(
                            bar["l_ma200"]
                        )
                        and
                        float(
                            bar[
                                "l_ma200_slope"
                            ]
                        ) >
                        0.0
                    )

                    if retest_ok:
                        atr = float(
                            bar["l_atr"]
                        )

                        if np.isfinite(
                            atr
                        ):
                            pending_entries[
                                (
                                    symbol,
                                    "LONG",
                                )
                            ] = {
                                "signal_time": ts,
                                "atr": atr,
                            }

                        long_setups.pop(
                            symbol,
                            None,
                        )

                    else:
                        setup[
                            "remaining"
                        ] -= 1

                        if setup[
                            "remaining"
                        ] <= 0:
                            long_setups.pop(
                                symbol,
                                None,
                            )

        # ---------------------------------------------------------
        # 7) Portfolio marks
        # ---------------------------------------------------------
        eq_close = portfolio_equity(
            cash,
            positions,
            bars,
            use_open=False,
        )

        eq_worst = conservative_intrabar_equity(
            cash,
            positions,
            bars,
        )

        gross = gross_notional(
            positions,
            bars,
            use_open=False,
        )

        gross_lev = (
            gross /
            eq_close
            if eq_close > 0
            else np.nan
        )

        max_concurrent_seen = max(
            max_concurrent_seen,
            len(
                positions
            ),
        )

        if np.isfinite(
            gross_lev
        ):
            max_gross_seen = max(
                max_gross_seen,
                gross_lev,
            )

        equity_rows.append({
            "timestamp": ts,
            "equity": eq_close,
            "intrabar_worst_equity": eq_worst,
            "cash": cash,
            "positions": len(positions),
            "gross_notional": gross,
            "gross_leverage": gross_lev,
            "open_initial_risk_dollars": (
                open_initial_risk_dollars(
                    positions
                )
            ),
        })

        # Portfolio failure guard.
        if (
            eq_close <= 0 or
            eq_worst <= 0
        ):
            break

    # Close remaining positions at last known CLOSE.
    if equity_rows:
        final_ts = equity_rows[-1][
            "timestamp"
        ]

        final_bars = bars_at(
            final_ts
        )

        for symbol in list(
            positions.keys()
        ):
            pos = positions[
                symbol
            ]

            if symbol in final_bars:
                raw_exit = float(
                    final_bars[
                        symbol
                    ]["close"]
                )
            else:
                raw_exit = pos[
                    "last_price"
                ]

            exit_px = adverse_exit(
                raw_exit,
                pos["side"],
            )

            realize_full_exit(
                symbol=symbol,
                pos=pos,
                exit_px=exit_px,
                exit_time=final_ts,
                reason="EOD",
            )

        # Replace final row with flat realized equity.
        equity_rows[-1][
            "equity"
        ] = cash

        equity_rows[-1][
            "intrabar_worst_equity"
        ] = min(
            equity_rows[-1][
                "intrabar_worst_equity"
            ],
            cash,
        )

        equity_rows[-1][
            "cash"
        ] = cash

        equity_rows[-1][
            "positions"
        ] = 0

        equity_rows[-1][
            "gross_notional"
        ] = 0.0

        equity_rows[-1][
            "gross_leverage"
        ] = 0.0

        equity_rows[-1][
            "open_initial_risk_dollars"
        ] = 0.0

    eqdf = pd.DataFrame(
        equity_rows
    )

    tdf = pd.DataFrame(
        [
            asdict(
                t
            )
            for t in trades
        ]
    )

    metrics = portfolio_metrics(
        eqdf=eqdf,
        trades=tdf,
        initial_equity=initial_equity,
        risk_per_trade=risk_per_trade,
        max_gross_leverage=max_gross_leverage,
        max_total_initial_risk=max_total_initial_risk,
        max_positions=max_positions,
        skipped_leverage=skipped_leverage,
        skipped_risk=skipped_risk,
        skipped_positions=skipped_positions,
        skipped_conflict=skipped_conflict,
        max_concurrent_seen=max_concurrent_seen,
        max_gross_seen=max_gross_seen,
        long_net_pnl=long_net_pnl,
        short_net_pnl=short_net_pnl,
    )

    return (
        eqdf,
        tdf,
        metrics,
    )


# =============================================================================
# Metrics / ranking
# =============================================================================

def portfolio_metrics(
    *,
    eqdf: pd.DataFrame,
    trades: pd.DataFrame,
    initial_equity: float,
    risk_per_trade: float,
    max_gross_leverage: float,
    max_total_initial_risk: float,
    max_positions: int,
    skipped_leverage: int,
    skipped_risk: int,
    skipped_positions: int,
    skipped_conflict: int,
    max_concurrent_seen: int,
    max_gross_seen: float,
    long_net_pnl: float,
    short_net_pnl: float,
) -> Dict:

    if eqdf.empty:
        return {}

    final_equity = float(
        eqdf["equity"].iloc[-1]
    )

    total_return = (
        final_equity /
        initial_equity -
        1.0
    )

    start_ts = pd.Timestamp(
        eqdf["timestamp"].iloc[0]
    )

    end_ts = pd.Timestamp(
        eqdf["timestamp"].iloc[-1]
    )

    years = max(
        (
            end_ts -
            start_ts
        ).total_seconds() /
        (
            365.25 *
            24 *
            3600
        ),
        1.0 /
        365.25,
    )

    if final_equity > 0:
        cagr = (
            final_equity /
            initial_equity
        ) ** (
            1.0 /
            years
        ) - 1.0
    else:
        cagr = -1.0

    # Close-equity MDD.
    eq = eqdf[
        "equity"
    ].astype(
        float
    )

    peak = eq.cummax()

    close_dd = (
        eq /
        peak -
        1.0
    )

    mdd_close = float(
        close_dd.min()
    )

    # More conservative intrabar MDD.
    worst_eq = eqdf[
        "intrabar_worst_equity"
    ].astype(
        float
    )

    # Peak is based on prior/close wealth path, worst equity is bar adverse mark.
    intrabar_dd = (
        worst_eq /
        peak -
        1.0
    )

    mdd_intrabar = float(
        intrabar_dd.min()
    )

    calmar = (
        cagr /
        abs(
            mdd_intrabar
        )
        if mdd_intrabar < 0
        else np.nan
    )

    if trades.empty:
        ntr = 0
        win_rate = np.nan
        pf = np.nan
        avg_r = np.nan
        long_trades = 0
        short_trades = 0
    else:
        ntr = int(
            len(
                trades
            )
        )

        pnl = trades[
            "net_pnl"
        ].astype(
            float
        )

        win_rate = float(
            (
                pnl >
                0
            ).mean()
        )

        gp = pnl[
            pnl >
            0
        ].sum()

        gl = pnl[
            pnl <
            0
        ].sum()

        pf = (
            float(
                gp /
                abs(
                    gl
                )
            )
            if gl < 0
            else (
                np.inf
                if gp > 0
                else np.nan
            )
        )

        avg_r = float(
            trades[
                "r_multiple"
            ].replace(
                [
                    np.inf,
                    -np.inf,
                ],
                np.nan,
            ).mean()
        )

        long_trades = int(
            (
                trades[
                    "side"
                ] ==
                "LONG"
            ).sum()
        )

        short_trades = int(
            (
                trades[
                    "side"
                ] ==
                "SHORT"
            ).sum()
        )

    return {
        "risk_per_trade_pct": (
            risk_per_trade *
            100
        ),
        "max_gross_leverage_cap": (
            max_gross_leverage
        ),
        "max_total_initial_risk_pct": (
            max_total_initial_risk *
            100
        ),
        "max_positions_cap": (
            max_positions
        ),
        "final_equity": final_equity,
        "total_return_pct": (
            total_return *
            100
        ),
        "cagr_pct": (
            cagr *
            100
        ),
        "mdd_close_pct": (
            mdd_close *
            100
        ),
        "mdd_intrabar_pct": (
            mdd_intrabar *
            100
        ),
        "calmar": calmar,
        "trades": ntr,
        "long_trades": long_trades,
        "short_trades": short_trades,
        "win_rate_pct": (
            win_rate *
            100
            if pd.notna(
                win_rate
            )
            else np.nan
        ),
        "profit_factor": pf,
        "avg_r_multiple": avg_r,
        "max_concurrent_seen": (
            max_concurrent_seen
        ),
        "max_gross_leverage_seen": (
            max_gross_seen
        ),
        "avg_gross_leverage": float(
            eqdf[
                "gross_leverage"
            ].replace(
                [
                    np.inf,
                    -np.inf,
                ],
                np.nan,
            ).mean()
        ),
        "skipped_leverage": (
            skipped_leverage
        ),
        "skipped_risk": (
            skipped_risk
        ),
        "skipped_positions": (
            skipped_positions
        ),
        "skipped_conflict": (
            skipped_conflict
        ),
        "long_net_pnl": (
            long_net_pnl
        ),
        "short_net_pnl": (
            short_net_pnl
        ),
    }


def rank_grid(
    summary: pd.DataFrame,
) -> pd.DataFrame:

    x = summary.copy()

    # Hard failure / excessive drawdown penalty.
    x["eligible"] = (
        (x["final_equity"] > 0) &
        (x["mdd_intrabar_pct"] > -35.0) &
        (x["trades"] >= 30)
    )

    # Primary preference:
    # high CAGR + Calmar, but avoid huge MDD.
    x["rank_cagr"] = (
        x["cagr_pct"]
        .rank(
            ascending=False,
            method="min",
            na_option="bottom",
        )
    )

    x["rank_calmar"] = (
        x["calmar"]
        .rank(
            ascending=False,
            method="min",
            na_option="bottom",
        )
    )

    x["rank_mdd"] = (
        x["mdd_intrabar_pct"]
        .rank(
            ascending=False,
            method="min",
            na_option="bottom",
        )
    )

    x["robust_score"] = (
        0.45 *
        x["rank_calmar"]
        +
        0.35 *
        x["rank_cagr"]
        +
        0.20 *
        x["rank_mdd"]
        +
        np.where(
            x["eligible"],
            0.0,
            100.0,
        )
    )

    x["rank"] = (
        x["robust_score"]
        .rank(
            ascending=True,
            method="min",
        )
        .astype(int)
    )

    return (
        x.sort_values(
            [
                "rank",
                "calmar",
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


# =============================================================================
# Main
# =============================================================================

def parse_float_list(
    text: Optional[str],
    default: List[float],
) -> List[float]:

    if text is None:
        return default

    return [
        float(
            x.strip()
        )
        for x in text.split(",")
        if x.strip()
    ]


def main():

    ap = argparse.ArgumentParser(
        description=(
            "Combined Long+Short crypto portfolio "
            "with risk sizing and leverage sweep"
        )
    )

    ap.add_argument(
        "--long-symbols",
        nargs="+",
        default=LONG_SYMBOLS_DEFAULT,
    )

    ap.add_argument(
        "--short-symbols",
        nargs="+",
        default=SHORT_SYMBOLS_DEFAULT,
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
        "--initial-equity",
        type=float,
        default=100_000.0,
    )

    ap.add_argument(
        "--risk-grid",
        default=None,
        help=(
            "Comma-separated decimal risks, e.g. "
            "0.005,0.01,0.015"
        ),
    )

    ap.add_argument(
        "--leverage-grid",
        default=None,
        help=(
            "Comma-separated gross leverage caps, "
            "e.g. 1,1.5,2,3"
        ),
    )

    ap.add_argument(
        "--max-total-risk",
        type=float,
        default=0.04,
    )

    ap.add_argument(
        "--max-positions",
        type=int,
        default=6,
    )

    ap.add_argument(
        "--out",
        type=Path,
        default=Path(
            "output_combined_v6"
        ),
    )

    args, unknown = (
        ap.parse_known_args()
    )

    if unknown:
        print(
            f"[info] ignored notebook args: {unknown}"
        )

    args.out.mkdir(
        parents=True,
        exist_ok=True,
    )

    risks = parse_float_list(
        args.risk_grid,
        RISK_GRID_DEFAULT,
    )

    leverages = parse_float_list(
        args.leverage_grid,
        LEVERAGE_GRID_DEFAULT,
    )

    print("\n======================================================")
    print("BB SQUEEZE COMBINED PORTFOLIO v6e DYNAMIC-UNIVERSE")
    print("======================================================")

    print(
        "\nLONG frozen strategy:"
    )

    print(
        "  RETEST_3 | SLOPE | ATR1.5 | BB_MID_100"
    )

    print(
        "  universe: "
        +
        " ".join(
            args.long_symbols
        )
    )

    print(
        "\nSHORT frozen strategy:"
    )

    print(
        "  09_EXP_SLOPE | ATR1.0 | TP2R_HALF_BE_TRAIL"
    )

    print(
        "  universe: "
        +
        " ".join(
            args.short_symbols
        )
    )

    print(
        "\nRisk grid: "
        +
        ", ".join(
            f"{r*100:.2f}%"
            for r in risks
        )
    )

    print(
        "Gross leverage caps: "
        +
        ", ".join(
            f"{x:.1f}x"
            for x in leverages
        )
    )

    print(
        f"Portfolio risk cap: {args.max_total_risk*100:.1f}%"
    )

    print(
        f"Max positions: {args.max_positions}"
    )

    data = load_all_data(
        long_symbols=args.long_symbols,
        short_symbols=args.short_symbols,
        interval=args.interval,
        start=args.start,
        end=args.end,
    )

    results = []

    best_cache = None

    total_runs = (
        len(
            risks
        ) *
        len(
            leverages
        )
    )

    run_n = 0

    for risk in risks:
        for lev in leverages:
            run_n += 1

            print(
                f"\n[portfolio {run_n}/{total_runs}] "
                f"risk={risk*100:.2f}% "
                f"gross_cap={lev:.1f}x"
            )

            eqdf, trades, metrics = run_portfolio(
                data=data,
                long_symbols=args.long_symbols,
                short_symbols=args.short_symbols,
                initial_equity=args.initial_equity,
                risk_per_trade=risk,
                max_gross_leverage=lev,
                max_total_initial_risk=args.max_total_risk,
                max_positions=args.max_positions,
            )

            results.append(
                metrics
            )

            print(
                f"  CAGR={metrics['cagr_pct']:.2f}% "
                f"MDD={metrics['mdd_intrabar_pct']:.2f}% "
                f"Calmar={metrics['calmar']:.3f} "
                f"Trades={metrics['trades']} "
                f"PF={metrics['profit_factor']:.3f}"
            )

            # cache raw outputs with config identifiers
            if best_cache is None:
                best_cache = (
                    metrics,
                    eqdf,
                    trades,
                )

    summary = pd.DataFrame(
        results
    )

    ranked = rank_grid(
        summary
    )

    ranked.to_csv(
        args.out /
        "grid_summary.csv",
        index=False,
    )

    top = ranked.head(
        15
    ).copy()

    top.to_csv(
        args.out /
        "top_configs.csv",
        index=False,
    )

    baseline = (
        ranked[
            np.isclose(
                ranked[
                    "risk_per_trade_pct"
                ],
                1.0,
            )
        ]
        .sort_values(
            "max_gross_leverage_cap"
        )
        .copy()
    )

    baseline.to_csv(
        args.out /
        "baseline_1pct.csv",
        index=False,
    )

    # Rerun actual best-ranked config to save its detail.
    best = ranked.iloc[
        0
    ]

    best_eq, best_trades, best_metrics = run_portfolio(
        data=data,
        long_symbols=args.long_symbols,
        short_symbols=args.short_symbols,
        initial_equity=args.initial_equity,
        risk_per_trade=(
            best[
                "risk_per_trade_pct"
            ] /
            100.0
        ),
        max_gross_leverage=float(
            best[
                "max_gross_leverage_cap"
            ]
        ),
        max_total_initial_risk=args.max_total_risk,
        max_positions=args.max_positions,
    )

    best_eq.to_csv(
        args.out /
        "best_equity.csv",
        index=False,
    )

    best_trades.to_csv(
        args.out /
        "best_trades.csv",
        index=False,
    )

    cols = [
        "rank",
        "risk_per_trade_pct",
        "max_gross_leverage_cap",
        "total_return_pct",
        "cagr_pct",
        "mdd_intrabar_pct",
        "calmar",
        "trades",
        "long_trades",
        "short_trades",
        "win_rate_pct",
        "profit_factor",
        "avg_r_multiple",
        "max_concurrent_seen",
        "max_gross_leverage_seen",
        "avg_gross_leverage",
        "skipped_leverage",
        "skipped_risk",
        "skipped_positions",
    ]

    print("\n\n======================================================")
    print("TOP 15 COMBINED PORTFOLIO CONFIGS")
    print("======================================================")

    print(
        top[
            cols
        ].to_string(
            index=False,
            float_format=lambda x: f"{x:,.3f}",
        )
    )

    print("\n\n======================================================")
    print("1.00% RISK PER TRADE - LEVERAGE CAP COMPARISON")
    print("======================================================")

    print(
        baseline[
            cols
        ].to_string(
            index=False,
            float_format=lambda x: f"{x:,.3f}",
        )
    )

    report = (
        "BB SQUEEZE COMBINED v6\n\n"
        "TOP 15\n"
        +
        top[
            cols
        ].to_string(
            index=False,
            float_format=lambda x: f"{x:,.3f}",
        )
        +
        "\n\n1% RISK BASELINE\n"
        +
        baseline[
            cols
        ].to_string(
            index=False,
            float_format=lambda x: f"{x:,.3f}",
        )
    )

    (
        args.out /
        "top_v6.txt"
    ).write_text(
        report,
        encoding="utf-8",
    )

    print("\n\n=== COPY THE TWO TABLES ABOVE BACK TO CHATGPT ===")

    print(
        f"\nSaved to: {args.out.resolve()}"
    )

    print(
        "\nNote: funding and exchange liquidation are not modeled."
    )


if __name__ == "__main__":
    main()

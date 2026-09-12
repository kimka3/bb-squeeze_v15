"""Pre-trade and per-bar safety checks.

A guard that fires is not a routine event. Most of these conditions mean live
state has drifted from what the backtest assumed, and the right response is to
stop opening risk and get a human, not to improvise.
"""
from __future__ import annotations

import time
import math
from dataclasses import dataclass, field

from data_io import H4


@dataclass
class Verdict:
    block_entries: bool = False
    halt: bool = False
    reasons: list[str] = field(default_factory=list)

    def add(self, reason: str, *, halt: bool = False) -> None:
        self.reasons.append(reason)
        self.block_entries = True
        self.halt = self.halt or halt

    def __bool__(self) -> bool:
        return not (self.block_entries or self.halt)


def check_bar_freshness(bar_ms: int, config, now_ms: int | None = None) -> Verdict:
    """The bar we are about to act on must be the one that just closed."""
    v = Verdict()
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    age = now - (bar_ms + H4)
    if age < 0:
        v.add(f'bar {bar_ms} has not closed yet')
    elif age > config.bar_grace_seconds * 1000 + H4:
        v.add(f'latest closed bar is {age / 60000:.1f} min old — a bar was missed')
    return v


def check_margin(state, config) -> Verdict:
    """Cross margin is shared across the book, and the backtest models none of it."""
    v = Verdict()
    if (not math.isfinite(state.equity) or state.equity <= 0
            or not math.isfinite(state.maintenance_margin) or state.maintenance_margin < 0):
        v.add('invalid equity or maintenance margin; entries halted', halt=True)
        return v
    ratio = state.margin_ratio
    if ratio < config.margin_ratio_alert:
        v.add(f'margin ratio {ratio:.2f} below alert level {config.margin_ratio_alert}', halt=True)
    elif ratio < config.margin_ratio_block_entries:
        v.add(f'margin ratio {ratio:.2f} below entry block {config.margin_ratio_block_entries}')
    return v


def check_basis(closes: dict[str, float], marks: dict[str, float], config) -> tuple[Verdict, set[str]]:
    """Signals come from Binance, fills happen on Lighter. When the two prices
    disagree badly, the stop distance we computed does not describe the market we
    are about to trade, so that symbol sits out."""
    v = Verdict()
    blocked = set()
    for symbol, close in closes.items():
        mark = marks.get(symbol)
        if mark is None or mark <= 0:
            blocked.add(symbol)
            v.reasons.append(f'{symbol}: no Lighter mark price')
            continue
        divergence = abs(mark - close) / close * 100
        if divergence > config.max_basis_divergence_pct:
            blocked.add(symbol)
            v.reasons.append(f'{symbol}: basis {divergence:.2f}% exceeds '
                             f'{config.max_basis_divergence_pct}%')
    return v, blocked


def check_sizing(market, qty: float, price: float, config) -> str:
    """Exchange-level reasons this entry cannot be placed as sized."""
    reason = market.rejects(qty, price)
    if reason:
        return reason
    if market.round_size(qty) * price < config.min_notional_usd:
        return f'notional below {config.min_notional_usd}'
    return ''


def reconcile(internal_positions: dict, state, symbol_to_lighter: dict) -> Verdict:
    """Internal book against the exchange's. A mismatch halts the bot.

    We do not auto-heal. With at most six positions, the bug risk in automatic
    repair is larger than the cost of a human looking at it.
    """
    v = Verdict()
    exchange = {}
    reverse = {v_: k for k, v_ in symbol_to_lighter.items()}
    for lighter_symbol, pos in state.positions.items():
        strat_symbol = reverse.get(lighter_symbol)
        if strat_symbol:
            exchange[strat_symbol] = pos
        else:
            v.add(f'{lighter_symbol}: unknown exchange position', halt=True)

    for symbol, p in internal_positions.items():
        live = exchange.get(symbol)
        if live is None:
            v.add(f'{symbol}: tracked internally but flat on Lighter', halt=True)
        elif live['side'] != p['side']:
            v.add(f'{symbol}: side {p["side"]} internally, {live["side"]} on Lighter', halt=True)
        elif not math.isclose(float(live['qty']), float(p['qty']),
                              rel_tol=1e-9, abs_tol=1e-12):
            v.add(f'{symbol}: qty {p["qty"]:.6f} internally, {live["qty"]:.6f} on Lighter',
                  halt=True)
    for symbol in exchange:
        if symbol not in internal_positions:
            v.add(f'{symbol}: open on Lighter but not tracked internally', halt=True)
    return v

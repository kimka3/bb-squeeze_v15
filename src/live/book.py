"""Order book depth and what an order would actually pay for it.

Verified against /api/v1/orderBookOrders on 2026-09-12: the response carries
individual resting orders (not aggregated levels) under `bids` and `asks`, each
with `price` and `remaining_base_amount`, best-first, and `limit` caps at 250.

This is how paper mode measures slippage instead of assuming it. The backtest
charges a flat 2bp per side; walking real depth says what the book would have
charged for the size the strategy actually wanted.
"""
from __future__ import annotations

import json
import math
import urllib.request
from dataclasses import dataclass

MAX_LEVELS = 250


@dataclass
class Quote:
    """What filling `requested` would cost, given the depth we could see."""
    vwap: float                # volume-weighted fill price over the levels consumed
    filled: float              # base quantity the visible book could fill
    requested: float
    top: float                 # best price on the side we hit
    worst: float               # deepest level consumed
    exhausted: bool            # visible depth ran out before the size was filled

    @property
    def complete(self) -> bool:
        return not self.exhausted and self.filled >= self.requested * (1 - 1e-9)

    def slippage_bps(self, reference: float, buying: bool) -> float:
        """Cost against a reference price, in basis points, signed as a cost.

        Positive means the fill was worse than the reference.
        """
        if reference <= 0 or self.filled <= 0:
            return float('nan')
        diff = (self.vwap - reference) if buying else (reference - self.vwap)
        return diff / reference * 10_000


def fetch_depth(base_url: str, market_id: int, limit: int = MAX_LEVELS,
                timeout: int = 20) -> dict:
    url = f'{base_url.rstrip("/")}/api/v1/orderBookOrders?market_id={market_id}&limit={min(limit, MAX_LEVELS)}'
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def _levels(depth: dict, buying: bool) -> list[tuple[float, float]]:
    """(price, size) best-first for the side a taker would hit."""
    rows = depth.get('asks' if buying else 'bids') or []
    out = []
    for o in rows:
        try:
            price = float(o['price'])
            # A consumed order's explicit zero remaining quantity is NOT its
            # initial size. Falling back via `or` resurrects exhausted depth.
            size = float(o.get('remaining_base_amount', o.get('initial_base_amount', 0)))
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(price) and math.isfinite(size) and price > 0 and size > 0:
            out.append((price, size))
    # The API returns best-first already; sort defensively so a change in that
    # contract degrades into a worse quote rather than a wrong one.
    out.sort(key=lambda x: x[0], reverse=not buying)
    return out


def walk(depth: dict, qty: float, buying: bool) -> Quote:
    """Consume resting orders until `qty` is filled, or the visible book ends."""
    if not math.isfinite(qty) or qty < 0:
        raise ValueError('book quantity must be finite and nonnegative')
    levels = _levels(depth, buying)
    if not levels or qty <= 0:
        return Quote(float('nan'), 0., qty, float('nan'), float('nan'), True)
    remaining = qty
    cost = 0.
    filled = 0.
    worst = levels[0][0]
    for price, size in levels:
        take = min(remaining, size)
        cost += take * price
        filled += take
        worst = price
        remaining -= take
        if remaining <= 1e-12:
            break
    return Quote(vwap=cost / filled if filled else float('nan'),
                 filled=filled, requested=qty, top=levels[0][0],
                 worst=worst, exhausted=remaining > 1e-12)


def quote(base_url: str, market_id: int, qty: float, buying: bool) -> Quote:
    return walk(fetch_depth(base_url, market_id), qty, buying)

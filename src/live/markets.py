"""Lighter market metadata and the integer scaling the exchange expects.

Field names and value ranges below were read from the live public API on
2026-09-12 (/api/v1/orderBooks, /api/v1/orderBookDetails) and from the
lighter-sdk transaction struct, not from memory.
"""
from __future__ import annotations

import json
import math
import urllib.request
from dataclasses import dataclass

# CreateOrderTxReq packs Price and TriggerPrice as c_uint32. A scaled price above
# this silently wraps, so we refuse it instead.
UINT32_MAX = 2 ** 32 - 1


@dataclass(frozen=True)
class Market:
    symbol: str
    market_id: int
    size_decimals: int
    price_decimals: int
    min_base_amount: float
    min_quote_amount: float
    maintenance_margin_fraction: float   # fraction of notional, e.g. .06
    initial_margin_fraction: float
    status: str

    def scale_size(self, qty: float) -> int:
        """Quantity -> integer base_amount, rounded DOWN so we never exceed the plan."""
        return int(math.floor(qty * 10 ** self.size_decimals))

    def unscale_size(self, base_amount: int) -> float:
        return base_amount / 10 ** self.size_decimals

    def scale_price(self, price: float) -> int:
        scaled = int(round(price * 10 ** self.price_decimals))
        if not 0 < scaled <= UINT32_MAX:
            raise ValueError(
                f'{self.symbol}: price {price} scales to {scaled}, outside uint32')
        return scaled

    def round_size(self, qty: float) -> float:
        """The quantity the exchange will actually accept."""
        return self.unscale_size(self.scale_size(qty))

    def rejects(self, qty: float, price: float) -> str:
        """Why the exchange would refuse this order, or '' if it would accept it."""
        rounded = self.round_size(qty)
        if rounded <= 0:
            return f'size {qty} rounds to zero at {self.size_decimals} decimals'
        if rounded < self.min_base_amount:
            return f'size {rounded} below min_base_amount {self.min_base_amount}'
        if rounded * price < self.min_quote_amount:
            return f'notional {rounded * price:.2f} below min_quote_amount {self.min_quote_amount}'
        return ''


def _get(url: str, timeout: int = 30) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def load_markets(base_url: str, wanted: dict[str, str]) -> dict[str, Market]:
    """Fetch metadata for the strategy universe.

    wanted maps strategy symbol (BTCUSDT) -> Lighter symbol (BTC). Raises if any
    market is missing or inactive rather than silently trading a smaller universe.
    """
    details = {d['symbol']: d for d in
               _get(f'{base_url}/api/v1/orderBookDetails')['order_book_details']}
    books = {b['symbol']: b for b in _get(f'{base_url}/api/v1/orderBooks')['order_books']}
    out, missing = {}, []
    for strat_symbol, lighter_symbol in wanted.items():
        d, b = details.get(lighter_symbol), books.get(lighter_symbol)
        if d is None or b is None:
            missing.append(lighter_symbol)
            continue
        if b.get('status') != 'active':
            missing.append(f'{lighter_symbol}(status={b.get("status")})')
            continue
        out[strat_symbol] = Market(
            symbol=lighter_symbol,
            market_id=int(b['market_id']),
            size_decimals=int(b['supported_size_decimals']),
            price_decimals=int(b['supported_price_decimals']),
            min_base_amount=float(b['min_base_amount']),
            min_quote_amount=float(b['min_quote_amount']),
            # API reports margin fractions in 1/10000 of notional.
            maintenance_margin_fraction=float(d['maintenance_margin_fraction']) / 10000,
            initial_margin_fraction=float(d['default_initial_margin_fraction']) / 10000,
            status=b['status'])
    if missing:
        raise RuntimeError(f'Lighter markets unavailable: {missing}. Refusing to start.')
    return out


def marks(base_url: str, markets: dict[str, Market]) -> dict[str, float]:
    """Current mark price per strategy symbol — the basis check needs it, and so
    does any stop we are about to place, since Lighter triggers on mark."""
    details = {d['symbol']: d for d in
               _get(f'{base_url}/api/v1/orderBookDetails')['order_book_details']}
    result = {s: float(details[m.symbol]['mark_price'])
              for s, m in markets.items() if m.symbol in details}
    missing = set(markets) - set(result)
    invalid = [s for s, price in result.items() if not math.isfinite(price) or price <= 0]
    if missing or invalid:
        raise RuntimeError(f'incomplete/invalid Lighter marks: missing={sorted(missing)}, invalid={invalid}')
    return result

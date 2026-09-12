"""Exchange access. One interface, three implementations: shadow, paper, Lighter.

VERIFIED against the installed lighter-sdk (1.0.0) and the live public API on
2026-09-12: order type constants, create_order/create_sl_order/create_tp_order/
modify_order/cancel_order signatures, the c_uint32 price field, and the REST
shapes for orderBooks, orderBookDetails, account and accountActiveOrders.

NOT VERIFIED — no account was available to place a real order. Everything in
LighterBroker past the signature level is written to the documented contract and
must be confirmed on a funded test sub-account before `mode: live` is used.
The two specific things to watch on first contact:

  1. create_sl_order submits ORDER_TYPE_STOP_LOSS with time_in_force=IOC. Once
     the mark triggers it, the resulting order is an IOC bounded by `price`.
     If the book cannot fill at that bound the order CANCELS and the position is
     left unprotected. stop_limit_pct sets how far past the trigger we are
     willing to pay; too tight is a naked position, too wide is slippage.
  2. Whether a resting reduce-only SL is resized automatically after the TP
     partial fills. This code does NOT rely on it: the SL is placed for the full
     position and reduce-only clamps it to whatever is left.
"""
from __future__ import annotations

import asyncio
import json
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

from .markets import Market


@dataclass
class Fill:
    symbol: str
    side: str            # LONG | SHORT  (the position side, not the order side)
    qty: float
    price: float
    role: str            # ENTRY | STOP | TP2R | BB_MID | TIME | ...


@dataclass
class OrderRef:
    client_order_index: int
    order_index: int | None = None
    purpose: str = ''


@dataclass
class AccountState:
    equity: float
    maintenance_margin: float
    positions: dict[str, dict] = field(default_factory=dict)   # symbol -> {side, qty, entry}
    active_orders: dict[str, list] = field(default_factory=dict)

    @property
    def margin_ratio(self) -> float:
        if self.maintenance_margin <= 0:
            return float('inf')
        return self.equity / self.maintenance_margin


class Broker(Protocol):
    def account_state(self) -> AccountState: ...
    def market_order(self, market: Market, symbol: str, position_side: str,
                     qty: float, closing: bool, coi: int, ref_price: float) -> Fill: ...
    def place_stop(self, market: Market, symbol: str, position_side: str,
                   qty: float, trigger: float, coi: int) -> OrderRef: ...
    def place_take_profit(self, market: Market, symbol: str, position_side: str,
                          qty: float, trigger: float, coi: int) -> OrderRef: ...
    def modify_stop(self, market: Market, ref: OrderRef, qty: float, trigger: float) -> None: ...
    def cancel(self, market: Market, ref: OrderRef) -> None: ...


def _is_ask(position_side: str, closing: bool) -> bool:
    """True when the order sells. Opening a short sells; closing a short buys."""
    opening_sells = position_side == 'SHORT'
    return opening_sells if not closing else not opening_sells


# --------------------------------------------------------------------------- #
# Shadow / paper
# --------------------------------------------------------------------------- #

class DryRunBroker:
    """Never contacts the exchange. Fills at the reference price with no slippage
    beyond what the strategy config already models, so shadow output is directly
    comparable to a backtest run over the same bars.

    Keys positions by the LIGHTER symbol, exactly as LighterBroker does — the
    broker layer speaks the exchange's names and the Trader does the mapping.
    """

    def __init__(self, equity: float = 100_000.0):
        self._equity = equity
        self.positions: dict[str, dict] = {}
        self.orders: dict[int, OrderRef] = {}
        self._next_index = 1
        self.log: list[dict] = []

    def account_state(self) -> AccountState:
        return AccountState(equity=self._equity, maintenance_margin=0.0,
                            positions=dict(self.positions))

    def market_order(self, market, symbol, position_side, qty, closing, coi, ref_price) -> Fill:
        filled = market.round_size(qty)
        self.log.append({'op': 'market', 'symbol': symbol, 'side': position_side,
                         'qty': filled, 'price': ref_price, 'closing': closing, 'coi': coi})
        if closing:
            self.positions.pop(market.symbol, None)
        else:
            self.positions[market.symbol] = {'side': position_side, 'qty': filled,
                                             'entry': ref_price}
        return Fill(symbol, position_side, filled, ref_price, 'EXIT' if closing else 'ENTRY')

    def _ref(self, coi, purpose) -> OrderRef:
        ref = OrderRef(coi, self._next_index, purpose)
        self.orders[self._next_index] = ref
        self._next_index += 1
        return ref

    def place_stop(self, market, symbol, position_side, qty, trigger, coi) -> OrderRef:
        self.log.append({'op': 'stop', 'symbol': symbol, 'qty': market.round_size(qty),
                         'trigger': trigger, 'coi': coi})
        return self._ref(coi, 'SL')

    def place_take_profit(self, market, symbol, position_side, qty, trigger, coi) -> OrderRef:
        self.log.append({'op': 'tp', 'symbol': symbol, 'qty': market.round_size(qty),
                         'trigger': trigger, 'coi': coi})
        return self._ref(coi, 'TP')

    def modify_stop(self, market, ref, qty, trigger) -> None:
        self.log.append({'op': 'modify', 'order_index': ref.order_index, 'trigger': trigger})

    def cancel(self, market, ref) -> None:
        self.log.append({'op': 'cancel', 'order_index': ref.order_index})
        self.orders.pop(ref.order_index, None)


# --------------------------------------------------------------------------- #
# Lighter
# --------------------------------------------------------------------------- #

class LighterBroker:
    """Real orders. Read paths use the public REST API; writes use lighter-sdk.

    stop_limit_pct is the IOC bound past the trigger — see the module docstring.
    """

    def __init__(self, base_url: str, account_index: int, private_key: str,
                 api_key_index: int = 0, stop_limit_pct: float = .01,
                 passive_take_profit: bool = True):
        import lighter                                   # imported lazily: shadow mode needs no SDK
        self.base_url = base_url.rstrip('/')
        self.account_index = account_index
        self.stop_limit_pct = stop_limit_pct
        self.passive_take_profit = passive_take_profit
        self._post_only_rejections = 0
        self._loop = asyncio.new_event_loop()
        self._signer = lighter.SignerClient(
            url=self.base_url, account_index=account_index,
            api_private_keys={api_key_index: private_key})
        self._api_key_index = api_key_index

    # -- plumbing ---------------------------------------------------------- #

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def _send(self, coro, what: str):
        tx, resp, err = self._run(coro)
        if err:
            raise RuntimeError(f'{what} rejected by Lighter: {err}')
        return tx, resp

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(f'{self.base_url}{path}', timeout=30) as r:
            return json.loads(r.read())

    def close(self) -> None:
        try:
            self._run(self._signer.close())
        finally:
            self._loop.close()

    # -- reads -------------------------------------------------------------- #

    def account_state(self) -> AccountState:
        data = self._get(f'/api/v1/account?by=index&value={self.account_index}')
        accounts = data.get('accounts') or []
        if not accounts:
            raise RuntimeError(f'Lighter returned no account {self.account_index}')
        acct = accounts[0]
        positions = {}
        maintenance = 0.0
        for p in acct.get('positions', []):
            qty = abs(float(p.get('position', 0) or 0))
            if qty <= 0:
                continue
            # sign=1 long, sign=-1 short in the API payload
            side = 'LONG' if int(p.get('sign', 1)) > 0 else 'SHORT'
            positions[p['symbol']] = {
                'side': side, 'qty': qty,
                'entry': float(p.get('avg_entry_price', 0) or 0),
                'market_id': int(p.get('market_id', -1))}
            maintenance += float(p.get('maintenance_margin', 0) or 0)
        equity = float(acct.get('total_asset_value', acct.get('collateral', 0)) or 0)
        return AccountState(equity=equity, maintenance_margin=maintenance, positions=positions)

    def active_orders(self, market_id: int) -> list[dict]:
        data = self._get(
            f'/api/v1/accountActiveOrders?account_index={self.account_index}&market_id={market_id}')
        return data.get('orders', [])

    # -- writes ------------------------------------------------------------- #

    def market_order(self, market: Market, symbol: str, position_side: str,
                     qty: float, closing: bool, coi: int, ref_price: float) -> Fill:
        base = market.scale_size(qty)
        if base <= 0:
            raise ValueError(f'{symbol}: size {qty} rounds to zero')
        ask = _is_ask(position_side, closing)
        # avg_execution_price bounds an IOC market order; pad against the taker.
        bound = ref_price * (1 - self.stop_limit_pct) if ask else ref_price * (1 + self.stop_limit_pct)
        self._send(self._signer.create_market_order(
            market_index=market.market_id, client_order_index=coi,
            base_amount=base, avg_execution_price=market.scale_price(bound),
            is_ask=ask, reduce_only=closing), f'{symbol} market')
        return Fill(symbol, position_side, market.unscale_size(base), ref_price,
                    'EXIT' if closing else 'ENTRY')

    def place_stop(self, market: Market, symbol: str, position_side: str,
                   qty: float, trigger: float, coi: int) -> OrderRef:
        base = market.scale_size(qty)
        ask = _is_ask(position_side, closing=True)
        bound = trigger * (1 - self.stop_limit_pct) if ask else trigger * (1 + self.stop_limit_pct)
        self._send(self._signer.create_sl_order(
            market_index=market.market_id, client_order_index=coi, base_amount=base,
            trigger_price=market.scale_price(trigger), price=market.scale_price(bound),
            is_ask=ask, reduce_only=True), f'{symbol} stop')
        return OrderRef(coi, self._find_order_index(market, coi), 'SL')

    def place_take_profit(self, market: Market, symbol: str, position_side: str,
                          qty: float, trigger: float, coi: int) -> OrderRef:
        """Rest the 2R target in the book as a post-only maker order.

        The take-profit is the one leg where passivity is free: it sits at a
        price better than market, so it is a natural maker, and an unfilled
        take-profit is simply a target price that never arrived. Entries and
        stops get no such luxury — see src/live/execution_study.py for the
        measurements that rule them out.

        Post-only is rejected if the price would cross. That only happens when
        the market has already gone past the target in our favour, so the
        fallback takes the trigger order and accepts the crossing fill.
        """
        import lighter
        base = market.scale_size(qty)
        ask = _is_ask(position_side, closing=True)
        if self.passive_take_profit:
            tx, resp, err = self._run(self._signer.create_order(
                market_index=market.market_id, client_order_index=coi,
                base_amount=base, price=market.scale_price(trigger), is_ask=ask,
                order_type=lighter.SignerClient.ORDER_TYPE_LIMIT,
                time_in_force=lighter.SignerClient.ORDER_TIME_IN_FORCE_POST_ONLY,
                reduce_only=True))
            if not err:
                return OrderRef(coi, self._find_order_index(market, coi), 'TP')
            # Crossed, or post-only unsupported here: fall through to the trigger.
            self._post_only_rejections += 1
        bound = trigger * (1 - self.stop_limit_pct) if ask else trigger * (1 + self.stop_limit_pct)
        self._send(self._signer.create_tp_order(
            market_index=market.market_id, client_order_index=coi, base_amount=base,
            trigger_price=market.scale_price(trigger), price=market.scale_price(bound),
            is_ask=ask, reduce_only=True), f'{symbol} take-profit')
        return OrderRef(coi, self._find_order_index(market, coi), 'TP')

    def modify_stop(self, market: Market, ref: OrderRef, qty: float, trigger: float) -> None:
        """Amend in place. Never cancel-then-replace: a failure between the two
        would leave the position with no stop at all."""
        if ref.order_index is None:
            raise RuntimeError('cannot modify a stop with no exchange order index')
        ask_bound = trigger * (1 + self.stop_limit_pct)
        self._send(self._signer.modify_order(
            market_index=market.market_id, order_index=ref.order_index,
            base_amount=market.scale_size(qty), price=market.scale_price(ask_bound),
            trigger_price=market.scale_price(trigger)), 'modify stop')

    def cancel(self, market: Market, ref: OrderRef) -> None:
        if ref.order_index is None:
            return
        self._send(self._signer.cancel_order(
            market_index=market.market_id, order_index=ref.order_index), 'cancel')

    def _find_order_index(self, market: Market, coi: int) -> int | None:
        for o in self.active_orders(market.market_id):
            if int(o.get('client_order_index', -1)) == coi:
                return int(o['order_index'])
        return None

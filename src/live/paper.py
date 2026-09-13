"""Public market observations and a virtual account, never exchange orders.

All fills and funding cash flows below are SIMULATED, not execution evidence.
Market/trigger orders consume only visible qualifying depth. The default maker
model requires visible opposite-side depth strictly THROUGH the limit, caps the
fill at that depth, and books at the limit (no price improvement). It still does
not observe queue position, trade flow, latency or our own impact. `observe_only`
can collect maker opportunities without assuming any maker fill.

Funding is a timestamped, idempotent ledger. Missing rates/settlement indexes are
reported as unresolved; latest rates are never replayed over missed hours. A
near-boundary public index is an explicitly labelled estimate, not settlement
evidence. The official Funding schema leaves rate/value/direction semantics
undocumented: raw directional rows are retained, never silently signed or paid.
Operational tolerances are model settings, not measured guarantees.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import time
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path

from .book import Quote, fetch_depth, walk
from .broker import AccountState, Fill, OrderRef


@dataclass
class PaperFill:
    ts_ms: int
    symbol: str
    role: str
    side: str
    qty: float
    price: float
    reference: float
    slippage_bps: float
    mark: float
    trigger: float | None = None
    depth_exhausted: bool = False
    evidence: str = 'SIMULATED'
    fill_model: str = 'visible_depth'
    order_side: str = ''


@dataclass
class PaperPosition:
    side: str
    entry: float
    qty: float
    initial_qty: float
    stop: float = 0.
    tp: float | None = None
    partial_taken: bool = False
    realized: float = 0.
    funding: float = 0.
    opened_ms: int = 0
    tp_qty: float = 0.
    tp_filled_qty: float = 0.


class PaperBroker:
    """Durable synthetic ledger; model outcomes must not be called real fills."""

    def __init__(self, base_url: str, markets: dict, state_path: Path,
                 equity: float = 100_000.0, breakeven_on_fill: bool = True,
                 funding_rate_is_percent: bool = True,
                 passive_take_profit: bool = True,
                 passive_fill_model: str = 'crossed_depth',
                 stop_limit_pct: float = .01,
                 funding_mark_max_age_seconds: float = 120.,
                 request_timeout_seconds: float = 10.):
        if passive_fill_model not in ('crossed_depth', 'observe_only'):
            raise ValueError('unknown passive_fill_model')
        if not math.isfinite(equity) or equity <= 0:
            raise ValueError('paper equity must be positive and finite')
        if not 0 <= stop_limit_pct < 1:
            raise ValueError('invalid paper IOC price tolerance')
        self.base_url = base_url.rstrip('/')
        self.markets = markets
        self.state_path = Path(state_path)
        self.cash = self.initial_cash = equity
        self.book: dict[str, PaperPosition] = {}
        self.fills: list[PaperFill] = []
        self.breakeven_on_fill = breakeven_on_fill
        self.passive_take_profit = passive_take_profit
        self.passive_fill_model = passive_fill_model
        self.stop_limit_pct = stop_limit_pct
        self.funding_rate_is_percent = funding_rate_is_percent
        self.funding_mark_max_age_seconds = funding_mark_max_age_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self._owner: dict[int, str] = {}
        self._orders: dict[int, dict] = {}
        self._market_results: dict[int, dict] = {}
        self._next_index = 1
        self._marks: dict[str, float] = {}
        self._indexes: dict[str, float] = {}
        self._mark_sample_ms: dict[str, int] = {}
        self._previous_marks: dict[str, tuple[int, float]] = {}
        self._previous_indexes: dict[str, tuple[int, float]] = {}
        self._last_funding_hour = int(time.time()) // 3600
        self.funding_total = self.funding_closed = 0.
        self._funding_events: dict[str, dict] = {}
        self._funding_samples: dict[str, dict] = {}
        self._events: list[dict] = []
        self._event_counts: dict[str, int] = {}
        self._attempts: dict[str, dict] = {}
        self._risk = {'sampled_equity_peak': equity, 'sampled_mdd_pct': 0.}
        self._persistence_failed = False
        self.load()

    # State and order outcome are replaced together before an acknowledgment.
    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            'schema_version': 2, 'cash': self.cash, 'initial_cash': self.initial_cash,
            'book': {s: asdict(p) for s, p in self.book.items()},
            'owner': self._owner, 'orders': self._orders,
            'market_results': self._market_results, 'next_index': self._next_index,
            'last_funding_hour': self._last_funding_hour,
            'funding_total': self.funding_total, 'funding_closed': self.funding_closed,
            'funding_events': self._funding_events,
            'funding_samples': self._funding_samples,
            'fills': [asdict(f) for f in self.fills], 'marks': self._marks, 'indexes': self._indexes,
            'mark_sample_ms': self._mark_sample_ms, 'risk': self._risk,
            'events': self._events, 'event_counts': self._event_counts,
            'attempts': self._attempts, 'models': self.model_settings(),
        }
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                    dir=self.state_path.parent, prefix=self.state_path.name + '.',
                    suffix='.tmp', delete=False) as f:
                tmp = Path(f.name)
                json.dump(data, f, allow_nan=False, separators=(',', ':'))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_path)
            # POSIX directory fsync makes the rename durable. Windows lacks it.
            if os.name != 'nt':
                fd = os.open(self.state_path.parent, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except Exception:
            self._persistence_failed = True
            raise
        finally:
            if tmp is not None and tmp.exists():
                tmp.unlink()

    def _writable(self) -> None:
        if self._persistence_failed:
            raise RuntimeError('paper persistence failed; restart from durable state before continuing')

    def load(self) -> None:
        if not self.state_path.exists():
            return
        d = json.loads(self.state_path.read_text(encoding='utf-8'))
        models = d.get('models')
        if models and models != self.model_settings():
            raise ValueError('paper model settings changed: use a new state directory/run')
        self.cash = float(d['cash'])
        self.initial_cash = d.get('initial_cash', self.initial_cash)
        self.book = {s: PaperPosition(**p) for s, p in d.get('book', {}).items()}
        self._owner = {int(k): v for k, v in d.get('owner', {}).items()}
        self._orders = {int(k): v for k, v in d.get('orders', {}).items()}
        self._market_results = {int(k): v for k, v in d.get('market_results', {}).items()}
        self._next_index = d.get('next_index', 1)
        self._last_funding_hour = d.get('last_funding_hour', self._last_funding_hour)
        self.fills = [PaperFill(**f) for f in d.get('fills', [])]
        self.funding_total = d.get('funding_total', sum(p.funding for p in self.book.values()))
        self.funding_closed = d.get('funding_closed', 0.)
        self._funding_events = d.get('funding_events', {})
        self._funding_samples = d.get('funding_samples', {})
        self._marks = d.get('marks', {})
        self._indexes = d.get('indexes', {})
        self._mark_sample_ms = d.get('mark_sample_ms', {})
        self._risk = d.get('risk', self._risk)
        self._events = d.get('events', [])
        self._event_counts = d.get('event_counts', {})
        self._attempts = d.get('attempts', {})
        if d.get('schema_version', 1) < 2:
            self._event('legacy_state_unverified', reason='closed funding, requested TP quantity and order outcomes cannot be reconstructed reliably')
            # Do not silently invent an old TP target or active protective order.
            for p in self.book.values():
                p.stop = 0.
                p.tp = None

    def model_settings(self) -> dict:
        return {'passive_take_profit': self.passive_take_profit,
                'passive_fill_model': self.passive_fill_model,
                'stop_limit_pct': self.stop_limit_pct,
                'breakeven_on_fill': self.breakeven_on_fill,
                'funding_rate_is_percent': self.funding_rate_is_percent,
                'funding_price_source': 'public_index_price',
                'funding_direction_policy': 'unresolved_until_semantics_verified',
                'funding_mark_max_age_seconds': self.funding_mark_max_age_seconds}

    def _event(self, kind: str, **data) -> None:
        self._event_counts[kind] = self._event_counts.get(kind, 0) + 1
        self._events.append({'ts_ms': _now_ms(), 'kind': kind, **data})
        # Bounded diagnostic sample; aggregate event counters are never dropped.
        self._events = self._events[-200:]

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(f'{self.base_url}{path}', timeout=self.request_timeout_seconds) as r:
            return json.loads(r.read())

    def refresh_marks(self) -> dict[str, float]:
        details = {d['symbol']: d for d in
                   self._get('/api/v1/orderBookDetails')['order_book_details']}
        now = _now_ms()
        marks, indexes = {}, {}
        for s, market in self.markets.items():
            raw = details.get(market.symbol, {}).get('mark_price')
            try:
                mark = float(raw)
            except (TypeError, ValueError):
                mark = float('nan')
            if not math.isfinite(mark) or mark <= 0:
                self._event('missing_mark', symbol=s)
                continue
            if s in self._marks and s in self._mark_sample_ms:
                self._previous_marks[s] = (self._mark_sample_ms[s], self._marks[s])
            if s in self._indexes and s in self._mark_sample_ms:
                self._previous_indexes[s] = (self._mark_sample_ms[s], self._indexes[s])
            marks[s] = mark
            try:
                index = float(details[market.symbol].get('index_price', float('nan')))
            except (TypeError, ValueError):
                index = float('nan')
            if math.isfinite(index) and index > 0:
                indexes[s] = index
            elif s in self.book:
                self._event('missing_index_price', symbol=s)
            self._mark_sample_ms[s] = now
        self._marks = marks
        self._indexes = indexes
        return marks

    def _quote(self, symbol: str, qty: float, buying: bool,
               limit: float | None = None, strict: bool = False) -> Quote:
        depth = fetch_depth(self.base_url, self.markets[symbol].market_id,
                            timeout=self.request_timeout_seconds)
        if limit is not None:
            side = 'asks' if buying else 'bids'
            rows = []
            for row in depth.get(side, []):
                try:
                    px = float(row['price'])
                except (TypeError, ValueError, KeyError):
                    continue
                allowed = (px < limit if strict else px <= limit) if buying else (
                    px > limit if strict else px >= limit)
                if allowed:
                    rows.append(row)
            depth = {side: rows}
        return walk(depth, qty, buying)

    def account_state(self) -> AccountState:
        # An in-memory mutation following a failed durable write is not ledger
        # evidence. In particular service() must not reconcile against it.
        self._writable()
        positions, active_orders, maintenance = {}, {}, 0.
        for symbol, p in self.book.items():
            if p.qty <= 1e-12:
                continue
            market = self.markets[symbol]
            mark = self._marks.get(symbol, p.entry)
            positions[market.symbol] = {'side': p.side, 'qty': p.qty, 'entry': p.entry,
                'partial_taken': p.partial_taken, 'tp_filled_qty': p.tp_filled_qty}
            maintenance += p.qty * mark * market.maintenance_margin_fraction
        for order in self._orders.values():
            if order['status'] == 'active' and order['symbol'] in self.book:
                active_orders.setdefault(self.markets[order['symbol']].symbol, []).append(dict(order))
        return AccountState(equity=self.equity(), maintenance_margin=maintenance,
            positions=positions, active_orders=active_orders,
            execution_changes_verified=True, active_orders_verified=True)

    def equity(self) -> float:
        return self.cash + sum((self._marks.get(s, p.entry) - p.entry) * p.qty *
            (1 if p.side == 'LONG' else -1) for s, p in self.book.items())

    def market_order_result(self, coi: int) -> Fill | None:
        self._writable()
        outcome = self._market_results.get(int(coi))
        return Fill(**outcome['fill']) if outcome else None

    def market_order(self, market, symbol, position_side, qty, closing, coi,
                     ref_price) -> Fill:
        self._writable()
        if position_side not in ('LONG', 'SHORT') or not math.isfinite(qty) or qty < 0:
            raise ValueError('invalid paper order')
        if not math.isfinite(ref_price) or ref_price <= 0:
            raise ValueError('invalid paper reference price')
        want = market.round_size(qty)
        intent = {'symbol': symbol, 'side': position_side, 'qty': want, 'closing': bool(closing)}
        previous = self._market_results.get(int(coi))
        if previous:
            if previous['intent'] != intent:
                raise ValueError('client_order_index reused for a different intent')
            return Fill(**previous['fill'])
        if any(o['client_order_index'] == coi for o in self._orders.values()):
            raise ValueError('client_order_index already belongs to a protective order')
        p = self.book.get(symbol)
        if p and p.side != position_side:
            raise ValueError('paper order position side mismatch')
        if p and not closing:
            raise ValueError('paper duplicate entry requires original client_order_index')
        if closing:
            want = min(want, p.qty) if p else 0.
        buying = (position_side == 'LONG') != closing
        bound = ref_price * (1 + self.stop_limit_pct if buying else 1 - self.stop_limit_pct)
        q = self._quote(symbol, want, buying, limit=bound) if want > 0 else Quote(
            ref_price, 0., 0., ref_price, ref_price, False)
        filled = min(want, max(0., q.filled))
        mark = self._marks.get(symbol, ref_price)
        price = q.vwap if filled > 0 else ref_price
        role = 'EXIT' if closing else 'ENTRY'
        self._attempt(role, symbol, position_side, want, filled, q)
        if filled > 0:
            if not math.isfinite(price) or price <= 0:
                raise ValueError('positive paper fill requires a finite book price')
            if closing:
                self._book_close(symbol, p, filled, price, role, mark, None, q)
            else:
                self.book[symbol] = PaperPosition(position_side, price, filled, filled,
                                                  opened_ms=_now_ms())
                self._record(role, symbol, position_side, filled, price, mark, mark, q)
        outcome = Fill(symbol, position_side, filled, price, role)
        self._market_results[int(coi)] = {'intent': intent, 'fill': asdict(outcome)}
        self.save()
        return outcome

    def place_stop(self, market, symbol, position_side, qty, trigger, coi) -> OrderRef:
        return self._place(market, symbol, position_side, qty, trigger, coi, 'SL')

    def place_take_profit(self, market, symbol, position_side, qty, trigger, coi) -> OrderRef:
        return self._place(market, symbol, position_side, qty, trigger, coi, 'TP')

    def _place(self, market, symbol, side, qty, trigger, coi, purpose) -> OrderRef:
        self._writable()
        p = self.book.get(symbol)
        if p is None or p.side != side:
            raise ValueError('protective order requires a matching paper position')
        rounded = market.round_size(qty)
        if rounded <= 0 or not math.isfinite(trigger) or trigger <= 0:
            raise ValueError('invalid protective order quantity/trigger')
        intent = {'symbol': symbol, 'side': side, 'purpose': purpose,
                  'qty': rounded, 'trigger': trigger}
        if int(coi) in self._market_results:
            raise ValueError('client_order_index already belongs to a market order')
        for order in self._orders.values():
            if order['client_order_index'] == coi:
                if order['intent'] != intent:
                    raise ValueError('protective client_order_index reused for a different intent')
                return OrderRef(coi, order['order_index'], purpose)
        if any(o['symbol'] == symbol and o['purpose'] == purpose and o['status'] == 'active'
               for o in self._orders.values()):
            raise ValueError('a protective order for this role is already active')
        index = self._next_index
        self._next_index += 1
        self._orders[index] = {**intent, 'client_order_index': coi, 'order_index': index,
            'remaining_qty': min(rounded, p.qty), 'status': 'active', 'intent': intent}
        self._owner[index] = symbol
        if purpose == 'SL':
            p.stop = trigger
        else:
            p.tp, p.tp_qty, p.tp_filled_qty = trigger, min(rounded, p.qty), 0.
            p.partial_taken = False
        self.save()
        return OrderRef(coi, index, purpose)

    def modify_stop(self, market, ref, qty, trigger) -> None:
        self._writable()
        order = self._orders.get(ref.order_index)
        if not order or order['status'] != 'active' or order['purpose'] != 'SL':
            raise KeyError(f'no active stop owns order_index {ref.order_index}')
        p = self.book[order['symbol']]
        if not math.isfinite(trigger) or trigger <= 0:
            raise ValueError('invalid stop price')
        if (p.side == 'SHORT' and trigger > p.stop) or (p.side == 'LONG' and trigger < p.stop):
            raise ValueError('refusing to loosen paper stop')
        order['trigger'], order['remaining_qty'] = trigger, min(market.round_size(qty), p.qty)
        if order['remaining_qty'] <= 0:
            raise ValueError('stop amendment rounds to zero')
        p.stop = trigger
        self.save()

    def cancel(self, market, ref) -> None:
        self._writable()
        order = self._orders.get(ref.order_index)
        if order and order['status'] == 'active':
            order['status'] = 'canceled'
            p = self.book.get(order['symbol'])
            if p:
                if order['purpose'] == 'SL':
                    p.stop = 0.
                else:
                    p.tp = None
        self.save()

    def poll(self) -> list[PaperFill]:
        self._writable()
        marks = self.refresh_marks()
        try:
            fired = self._poll_orders(marks)
        finally:
            # A later symbol's unavailable book must not discard already applied
            # synthetic fills or leave their state waiting for another poll.
            self._observe_risk()
            self.save()
        # Funding is lower priority than protection. Exposure at each boundary
        # comes from timestamped fills, not the account after current exits.
        self._accrue_funding(marks)
        self.save()
        return fired

    def _poll_orders(self, marks) -> list[PaperFill]:
        fired = []
        for symbol in list(self.book):
            p = self.book[symbol]
            mark = marks.get(symbol)
            if mark is None or p.qty <= 1e-12:
                continue
            short = p.side == 'SHORT'
            stop_hit = p.stop > 0 and (mark >= p.stop if short else mark <= p.stop)
            if stop_hit:
                active_stops = [o for o in self._orders.values() if o['symbol'] == symbol
                                and o['purpose'] == 'SL' and o['status'] == 'active']
                requested = min(p.qty, active_stops[0]['remaining_qty']) if active_stops else p.qty
                try:
                    fill = self._fill_trigger(symbol, p, requested, p.stop, 'STOP', mark)
                except (OSError, ValueError, KeyError) as exc:
                    self._event('stop_quote_unavailable', symbol=symbol, error=str(exc))
                    continue
                if fill:
                    fired.append(fill)
                # An IOC trigger is consumed even if its residual cannot fill.
                self._consume_order(symbol, 'SL', 'filled' if symbol not in self.book else 'canceled')
                if symbol in self.book:
                    p.stop = 0.
                    self._event('unprotected_stop_residual', symbol=symbol, qty=p.qty)
                continue
            if p.tp is None or p.partial_taken or p.tp_qty <= p.tp_filled_qty:
                continue
            mark_hit = mark <= p.tp if short else mark >= p.tp
            if mark_hit:
                self._event('maker_mark_touch' if self.passive_take_profit else 'tp_mark_trigger', symbol=symbol)
            # A resting limit is independent of mark. Its executable depth is
            # checked even when the public mark has not reached the limit.
            if self.passive_take_profit or mark_hit:
                remaining = min(p.qty, max(0., p.tp_qty - p.tp_filled_qty))
                try:
                    fill = self._fill_trigger(symbol, p, remaining, p.tp, 'TP2R', mark)
                except (OSError, ValueError, KeyError) as exc:
                    self._event('tp_quote_unavailable', symbol=symbol, error=str(exc))
                    continue
                if fill:
                    fired.append(fill)
                    p.tp_filled_qty += fill.qty
                    tolerance = 10 ** -self.markets[symbol].size_decimals * 1e-6
                    p.partial_taken = p.tp_filled_qty >= p.tp_qty - tolerance
                    if p.partial_taken and symbol in self.book and self.breakeven_on_fill:
                        p.stop = min(p.stop, p.entry) if short and p.stop > 0 else (
                            max(p.stop, p.entry) if not short and p.stop > 0 else 0.)
                        for order in self._orders.values():
                            if order['symbol'] == symbol and order['purpose'] == 'SL' and order['status'] == 'active':
                                order['trigger'] = p.stop
                    for order in self._orders.values():
                        if order['symbol'] == symbol and order['purpose'] == 'TP' and order['status'] == 'active':
                            order['remaining_qty'] = max(0., p.tp_qty - p.tp_filled_qty)
                            if p.partial_taken:
                                order['status'] = 'filled'
                if not self.passive_take_profit:
                    self._consume_order(symbol, 'TP', 'filled' if p.partial_taken else 'canceled')
                    if not p.partial_taken:
                        p.tp = None
                        self._event('tp_ioc_residual_canceled', symbol=symbol, qty=remaining - (fill.qty if fill else 0.))
        return fired

    def _consume_order(self, symbol, purpose, status) -> None:
        for order in self._orders.values():
            if order['symbol'] == symbol and order['purpose'] == purpose and order['status'] == 'active':
                order['status'] = status

    def _fill_trigger(self, symbol, p: PaperPosition, qty: float, trigger: float,
                      role: str, mark: float) -> PaperFill | None:
        buying = p.side == 'SHORT'
        maker = role == 'TP2R' and self.passive_take_profit
        if maker:
            q = self._quote(symbol, qty, buying, limit=trigger, strict=True)
            if self.passive_fill_model == 'observe_only':
                self._event('maker_fill_unobserved', symbol=symbol, eligible_visible_qty=q.filled)
                return None
        else:
            bound = trigger * (1 + self.stop_limit_pct if buying else 1 - self.stop_limit_pct)
            q = self._quote(symbol, qty, buying, limit=bound)
        filled = min(qty, p.qty, max(0., q.filled))
        self._attempt(role, symbol, p.side, qty, filled, q)
        if filled <= 0:
            return None
        price = trigger if maker else q.vwap
        if not math.isfinite(price) or price <= 0:
            raise ValueError('positive trigger fill requires a finite book price')
        if maker:
            q = Quote(price, filled, qty, q.top, q.worst, filled < qty)
        return self._book_close(symbol, p, filled, price, role, mark, trigger, q,
                                'crossed_depth_limit' if maker else 'visible_depth_ioc')

    def _book_close(self, symbol, p, qty, price, role, mark, trigger, q,
                    model='visible_depth') -> PaperFill:
        if qty <= 0 or qty > p.qty + 1e-12:
            raise ValueError('invalid synthetic close quantity')
        pnl = (price - p.entry) * qty * (1 if p.side == 'LONG' else -1)
        self.cash += pnl
        p.realized += pnl
        p.qty = max(0., p.qty - qty)
        fill = self._record(role, symbol, p.side, qty, price, mark,
                            trigger if trigger is not None else mark, q, model)
        if p.qty <= 1e-12:
            self.funding_closed += p.funding
            self.book.pop(symbol, None)
            for order in self._orders.values():
                if order['symbol'] == symbol and order['status'] == 'active':
                    triggered_purpose = {'STOP': 'SL', 'TP2R': 'TP'}.get(role)
                    order['status'] = 'filled' if order['purpose'] == triggered_purpose else 'canceled_flat'
        return fill

    def _record(self, role, symbol, side, qty, price, mark, reference, q, model='visible_depth') -> PaperFill:
        buying = side == 'LONG' if role == 'ENTRY' else side == 'SHORT'
        fill = PaperFill(_now_ms(), symbol, role, side, qty, price, reference,
            q.slippage_bps(reference, buying), mark,
            None if role in ('ENTRY', 'EXIT') else reference, q.exhausted,
            fill_model=model, order_side='BUY' if buying else 'SELL')
        self.fills.append(fill)
        return fill

    def _attempt(self, role, symbol, side, requested, filled, q) -> None:
        direction = 'BUY' if (side == 'LONG' if role == 'ENTRY' else side == 'SHORT') else 'SELL'
        key = f'{symbol}:{role}:{direction}'
        row = self._attempts.setdefault(key, {'attempts': 0, 'zero_fills': 0,
            'partial_fills': 0, 'depth_exhausted': 0, 'requested_qty': 0., 'filled_qty': 0.})
        row['attempts'] += 1
        row['zero_fills'] += filled <= 0
        row['partial_fills'] += 0 < filled < requested
        row['depth_exhausted'] += bool(q.exhausted)
        row['requested_qty'] += requested
        row['filled_qty'] += filled

    def _boundary_position(self, symbol, boundary_ms):
        qty, side = 0., None
        for fill in self.fills:
            if fill.symbol != symbol or fill.ts_ms >= boundary_ms:
                continue
            if fill.role == 'ENTRY':
                qty, side = fill.qty, fill.side
            else:
                qty = max(0., qty - fill.qty)
        return side, qty

    def _boundary_funding_price(self, symbol, boundary_ms):
        candidates = []
        if symbol in self._indexes and symbol in self._mark_sample_ms:
            candidates.append((self._mark_sample_ms[symbol], self._indexes[symbol]))
        if symbol in self._previous_indexes:
            candidates.append(self._previous_indexes[symbol])
        candidates = [(ts, px) for ts, px in candidates
            if abs(ts - boundary_ms) <= self.funding_mark_max_age_seconds * 1000]
        return min(candidates, key=lambda x: abs(x[0] - boundary_ms)) if candidates else (None, None)

    def _accrue_funding(self, marks: dict[str, float]) -> None:
        now = int(time.time())
        hour = now // 3600
        for settlement in range(self._last_funding_hour + 1, hour + 1):
            boundary_ms = settlement * 3600 * 1000
            for symbol in self.markets:
                side, qty = self._boundary_position(symbol, boundary_ms)
                if qty <= 1e-12:
                    continue
                sample_ms, index = self._boundary_funding_price(symbol, boundary_ms)
                key = f'{symbol}:{settlement}'
                self._funding_events.setdefault(key, {'symbol': symbol,
                    'settlement_hour': settlement, 'side': side, 'qty': qty,
                    'reference_price': index, 'reference_sample_ms': sample_ms, 'amount': None,
                    'status': 'pending_rate' if index is not None else 'missing_boundary_index',
                    'pricing_source': 'near_boundary_index_estimate' if index is not None else 'unobserved',
                    'next_retry': 0})
        self._last_funding_hour = max(self._last_funding_hour, hour)
        requests = 0
        for event in self._funding_events.values():
            if event['status'] not in ('pending_rate', 'missing_rate') or event['next_retry'] > now:
                continue
            # Proposed operational budget: never let a settlement backlog make
            # this fast protection poll issue an unbounded series of requests.
            if requests >= 1:
                break
            requests += 1
            symbol = event['symbol']
            rate = self._funding_rate_at(self.markets[symbol].market_id, event['settlement_hour'])
            sample = self._funding_samples.get(f'{self.markets[symbol].market_id}:{event["settlement_hour"]}')
            if rate is None:
                event['status'] = 'unverified_api_semantics' if sample else 'missing_rate'
                if sample:
                    event['raw_funding'] = sample
                event['next_retry'] = now + 60
                continue
            fraction = rate / 100 if self.funding_rate_is_percent else rate
            amount = -event['qty'] * event['reference_price'] * fraction * (1 if event['side'] == 'LONG' else -1)
            self.cash += amount
            self.funding_total += amount
            p = self.book.get(symbol)
            if p and p.opened_ms < event['settlement_hour'] * 3600 * 1000:
                p.funding += amount
            else:
                self.funding_closed += amount
            event.update(status='booked_estimate', rate=rate, amount=amount, next_retry=0)

    def _funding_rate_at(self, market_id: int, settlement_hour: int) -> float | None:
        boundary = settlement_hour * 3600
        try:
            data = self._get(f'/api/v1/fundings?market_id={market_id}&resolution=1h'
                f'&start_timestamp={boundary - 3600}&end_timestamp={boundary}&count_back=2')
            for row in data.get('fundings', []):
                ts = float(row.get('timestamp', row.get('time', float('nan'))))
                if ts > 1e12:
                    ts /= 1000
                rate = float(row.get('rate', float('nan')))
                # Matching convention is explicit; never guess that rows[-1]
                # represents this settlement. API semantics remain to reconcile.
                if ts == boundary and math.isfinite(rate):
                    if row.get('direction') is not None:
                        self._funding_samples[f'{market_id}:{settlement_hour}'] = {
                            'observed_at_ms': _now_ms(), 'raw': dict(row),
                            'reason': 'official schema does not specify rate/value/direction sign and units'}
                        return None
                    return rate
        except (OSError, ValueError, TypeError, KeyError):
            pass
        return None

    def _observe_risk(self) -> None:
        equity = self.equity()
        peak = max(self._risk['sampled_equity_peak'], equity)
        self._risk['sampled_equity_peak'] = peak
        self._risk['sampled_mdd_pct'] = min(self._risk['sampled_mdd_pct'], (equity / peak - 1) * 100)

    def measurements(self) -> dict:
        def stats(rows):
            vals = sorted(f.slippage_bps for f in rows if math.isfinite(f.slippage_bps))
            if not vals:
                return None
            mid = len(vals) // 2
            median = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
            return {'n': len(vals), 'mean_bps': sum(vals) / len(vals),
                    'median_bps': median, 'worst_bps': vals[-1], 'best_bps': vals[0]}
        rows = {role: [f for f in self.fills if f.role == role] for role in ('ENTRY', 'STOP', 'TP2R', 'EXIT')}
        # Resting maker limits have no mark trigger; do not count their
        # mechanically zero limit-price gap as a trigger execution observation.
        gaps = [abs(f.price - f.trigger) / f.trigger * 10_000
                for f in rows['STOP'] + rows['TP2R']
                if f.trigger and f.fill_model != 'crossed_depth_limit']
        state = self.account_state()
        gross = sum(p.qty * self._marks.get(s, p.entry) for s, p in self.book.items())
        incomplete = [e for e in self._funding_events.values() if e['status'] != 'booked_estimate']
        legacy_gap = bool(self._event_counts.get('legacy_state_unverified'))
        execution_gaps = sum(self._event_counts.get(kind, 0) for kind in (
            'stop_quote_unavailable', 'tp_quote_unavailable', 'missing_mark'))
        return {'evidence': 'SIMULATED_ACCOUNT_WITH_PUBLIC_MARKET_OBSERVATIONS',
            'equity': state.equity, 'cash': self.cash, 'initial_cash': self.initial_cash,
            'open_positions': len(self.book), 'fills': len(self.fills),
            'slippage_entry': stats(rows['ENTRY']), 'slippage_stop': stats(rows['STOP']),
            'slippage_take_profit': stats(rows['TP2R']), 'slippage_close_rule_exit': stats(rows['EXIT']),
            'slippage_by_role_direction': {f'{role}:{direction}': stats([f for f in fills if f.order_side == direction])
                for role, fills in rows.items() for direction in ('BUY', 'SELL')},
            'attempts_by_symbol_role_direction': self._attempts,
            'trigger_to_fill_bps': {'evidence': 'SIMULATED_PRICE_GAP_NOT_LATENCY',
                'n': len(gaps), 'mean': sum(gaps) / len(gaps) if gaps else None,
                'worst': max(gaps) if gaps else None},
            'depth_exhausted_fills': sum(f.depth_exhausted for f in self.fills),
            'funding_paid': self.funding_total, 'funding_closed': self.funding_closed,
            'funding_open': sum(p.funding for p in self.book.values()),
            'funding_ledger_complete': not incomplete and not self._event_counts.get('legacy_state_unverified'),
            'performance_complete': not incomplete and not legacy_gap and not execution_gaps,
            'equity_excludes_unresolved_funding': bool(incomplete) or legacy_gap,
            'funding_unresolved': incomplete,
            'funding_events': list(self._funding_events.values()),
            'funding_evidence': 'UNRESOLVED directional API rows; synthetic signed-rate estimates use public index, never mark fallback',
            'raw_funding_samples': self._funding_samples,
            'observations': {'event_counts': self._event_counts, 'recent_events': self._events,
                'mark_age_ms': {s: _now_ms() - ts for s, ts in self._mark_sample_ms.items()}},
            'risk': {**self._risk, 'gross_notional': gross,
                'gross_leverage': gross / state.equity if state.equity > 0 else None,
                'maintenance_coverage': state.equity / state.maintenance_margin if state.maintenance_margin > 0 else None,
                'unprotected_positions': [s for s, p in self.book.items() if p.stop <= 0],
                'missing_current_marks': [s for s in self.book if s not in self._marks],
                'evidence': 'POLL_SAMPLED; does not simulate liquidation, ADL or intrapoll worst loss'},
            'model_settings': self.model_settings(),
            'unobserved': ['actual maker queue and fills', 'actual order rejection/fallback',
                'actual trigger-to-fill latency', 'self impact and persistent consumed depth',
                'real funding statement and liquidation engine'],
            'data_quality': {'funding_unresolved_count': len(incomplete),
                'legacy_state_unverified': legacy_gap,
                'execution_observation_gap_count': execution_gaps,
                'missing_current_marks': [s for s in self.book if s not in self._marks],
                'persistence_failed': self._persistence_failed,
                'actual_fill_evidence': False},
            'backtest_assumption_bps': 2.0}


def _now_ms() -> int:
    return int(time.time() * 1000)

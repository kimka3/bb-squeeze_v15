"""Stage 2: live data, simulated fills.

Stage 1 replayed history with the backtest's own execution model and proved the
decision path matches. Stage 2 exists to measure the things that model does NOT
capture, and it is deliberately built to Lighter's rules rather than the
backtest's:

  * stops and take-profits trigger on MARK price, not on traded price
  * fills are priced by walking the real order book, not by a flat 2bp
  * funding accrues HOURLY on Lighter's schedule, not 8-hourly on Binance's

Everything it books is virtual. No key, no orders, no exposure.

One behaviour worth stating plainly, because building this surfaced it: in the
backtest the stop jumps to breakeven the instant the 2R partial fills, inside
the same bar. With native exchange orders nothing moves it until the bot amends
it. PaperBroker reacts to the fill immediately (breakeven_on_fill, default on),
which is what the live bot must also do via a fill subscription — otherwise the
breakeven protection is late by up to four hours. Set breakeven_on_fill=False to
measure how much that delay costs.
"""
from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .book import Quote, fetch_depth, walk
from .broker import AccountState, Fill, OrderRef
from .config import SYMBOL_TO_LIGHTER


@dataclass
class PaperFill:
    """One simulated fill, kept for the stage 2 measurement report."""
    ts_ms: int
    symbol: str
    role: str                 # ENTRY | STOP | TP2R | EXIT
    side: str
    qty: float
    price: float              # what the book said it would pay
    reference: float          # what we compared against (mark, or the trigger)
    slippage_bps: float
    mark: float
    trigger: float | None = None
    depth_exhausted: bool = False


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


class PaperBroker:
    """Virtual account fed by live Lighter data."""

    def __init__(self, base_url: str, markets: dict, state_path: Path,
                 equity: float = 100_000.0, breakeven_on_fill: bool = True,
                 funding_rate_is_percent: bool = True,
                 passive_take_profit: bool = True):
        self.base_url = base_url.rstrip('/')
        self.markets = markets
        self.state_path = Path(state_path)
        self.cash = equity
        self.book: dict[str, PaperPosition] = {}
        self.fills: list[PaperFill] = []
        self.breakeven_on_fill = breakeven_on_fill
        self.passive_take_profit = passive_take_profit
        # /api/v1/fundings reports `rate` as a percent per hourly settlement
        # (BTC showed 0.0012, i.e. ~1bp per 8h, matching the published typical).
        # Flagged rather than asserted: paper mode reports accrued funding so the
        # convention can be checked against a real statement.
        self.funding_rate_is_percent = funding_rate_is_percent
        self._owner: dict[int, str] = {}
        self._next_index = 1
        self._marks: dict[str, float] = {}
        self._last_funding_hour: int = 0
        self.load()

    # -- persistence --------------------------------------------------------- #

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix('.tmp')
        tmp.write_text(json.dumps({
            'cash': self.cash,
            'book': {s: asdict(p) for s, p in self.book.items()},
            'owner': {str(k): v for k, v in self._owner.items()},
            'next_index': self._next_index,
            'last_funding_hour': self._last_funding_hour,
            'fills': [asdict(f) for f in self.fills],
        }, indent=2))
        tmp.replace(self.state_path)

    def load(self) -> None:
        if not self.state_path.exists():
            return
        d = json.loads(self.state_path.read_text())
        self.cash = d.get('cash', self.cash)
        self.book = {s: PaperPosition(**p) for s, p in (d.get('book') or {}).items()}
        self._owner = {int(k): v for k, v in (d.get('owner') or {}).items()}
        self._next_index = d.get('next_index', 1)
        self._last_funding_hour = d.get('last_funding_hour', 0)
        self.fills = [PaperFill(**f) for f in (d.get('fills') or [])]

    # -- market data --------------------------------------------------------- #

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(f'{self.base_url}{path}', timeout=30) as r:
            return json.loads(r.read())

    def refresh_marks(self) -> dict[str, float]:
        details = {d['symbol']: d for d in
                   self._get('/api/v1/orderBookDetails')['order_book_details']}
        self._marks = {s: float(details[m.symbol]['mark_price'])
                       for s, m in self.markets.items() if m.symbol in details}
        return self._marks

    def _quote(self, symbol: str, qty: float, buying: bool) -> Quote:
        market = self.markets[symbol]
        return walk(fetch_depth(self.base_url, market.market_id), qty, buying)

    # -- Broker protocol ----------------------------------------------------- #

    def account_state(self) -> AccountState:
        positions, maintenance = {}, 0.0
        for symbol, p in self.book.items():
            if p.qty <= 1e-12:
                continue
            market = self.markets[symbol]
            mark = self._marks.get(symbol, p.entry)
            positions[market.symbol] = {'side': p.side, 'qty': p.qty, 'entry': p.entry}
            maintenance += p.qty * mark * market.maintenance_margin_fraction
        return AccountState(equity=self.equity(), maintenance_margin=maintenance,
                            positions=positions)

    def equity(self) -> float:
        total = self.cash
        for symbol, p in self.book.items():
            mark = self._marks.get(symbol, p.entry)
            total += (mark - p.entry) * p.qty * (1 if p.side == 'LONG' else -1)
        return total

    def market_order(self, market, symbol, position_side, qty, closing, coi,
                     ref_price) -> Fill:
        buying = (position_side == 'LONG') != closing
        want = market.round_size(qty)
        q = self._quote(symbol, want, buying)
        mark = self._marks.get(symbol, ref_price)
        price = q.vwap if q.filled > 0 else ref_price
        filled = q.filled if q.filled > 0 else 0.

        if closing:
            p = self.book.get(symbol)
            if p:
                filled = min(filled or p.qty, p.qty)
                self._book_close(symbol, p, filled, price, 'EXIT', mark, None, q)
            return Fill(symbol, position_side, filled, price, 'EXIT')

        if filled <= 0:
            return Fill(symbol, position_side, 0., price, 'ENTRY')
        self.book[symbol] = PaperPosition(side=position_side, entry=price, qty=filled,
                                          initial_qty=filled, opened_ms=_now_ms())
        self._record('ENTRY', symbol, position_side, filled, price, mark, mark, q)
        return Fill(symbol, position_side, filled, price, 'ENTRY')

    def place_stop(self, market, symbol, position_side, qty, trigger, coi) -> OrderRef:
        if symbol in self.book:
            self.book[symbol].stop = trigger
        return self._ref(coi, 'SL', symbol)

    def place_take_profit(self, market, symbol, position_side, qty, trigger, coi) -> OrderRef:
        if symbol in self.book:
            self.book[symbol].tp = trigger
        return self._ref(coi, 'TP', symbol)

    def modify_stop(self, market, ref, qty, trigger) -> None:
        symbol = self._owner.get(ref.order_index)
        if symbol is None:
            raise KeyError(f'no position owns order_index {ref.order_index}')
        if symbol in self.book:
            self.book[symbol].stop = trigger

    def cancel(self, market, ref) -> None:
        self._owner.pop(ref.order_index, None)

    def _ref(self, coi, purpose, symbol) -> OrderRef:
        ref = OrderRef(coi, self._next_index, purpose)
        self._owner[self._next_index] = symbol
        self._next_index += 1
        return ref

    # -- the fast loop ------------------------------------------------------- #

    def poll(self) -> list[PaperFill]:
        """Check every resting order against the current mark and fill what fires.

        This is the loop the exchange runs for us in production. Lighter compares
        the MARK price to the trigger, so that is what is compared here — not the
        last trade, and not the 4h OHLC the backtest walks.
        """
        marks = self.refresh_marks()
        fired: list[PaperFill] = []
        for symbol in list(self.book):
            p = self.book[symbol]
            mark = marks.get(symbol)
            if mark is None or p.qty <= 1e-12:
                continue
            short = p.side == 'SHORT'

            # Take-profit first only when both could fire: the backtest resolves
            # that ambiguity stop-first, and paper must not be more optimistic.
            stop_hit = (mark >= p.stop) if short else (mark <= p.stop)
            tp_hit = (p.tp is not None and not p.partial_taken
                      and ((mark <= p.tp) if short else (mark >= p.tp)))

            if stop_hit:
                fired.append(self._fill_trigger(symbol, p, p.qty, p.stop, 'STOP', mark))
                continue
            if tp_hit:
                half = p.qty * .5
                fired.append(self._fill_trigger(symbol, p, half, p.tp, 'TP2R', mark))
                if symbol in self.book:
                    self.book[symbol].partial_taken = True
                    if self.breakeven_on_fill:
                        # What the live bot must do from its fill subscription.
                        # Without it the stop sits at its old level until the
                        # next 4h amend.
                        self.book[symbol].stop = min(p.stop, p.entry) if short \
                            else max(p.stop, p.entry)
        self._accrue_funding(marks)
        self.save()
        return fired

    def _fill_trigger(self, symbol, p: PaperPosition, qty: float, trigger: float,
                      role: str, mark: float) -> PaperFill:
        buying = p.side == 'SHORT'          # closing a short buys
        if role == 'TP2R' and self.passive_take_profit:
            # A resting maker order fills AT its own price. It pays no spread and
            # walks no depth — that is the whole point of leaving it in the book.
            q = _MakerQuote(qty)
            return self._book_close(symbol, p, min(qty, p.qty), trigger, role,
                                    mark, trigger, q)
        q = self._quote(symbol, qty, buying)
        price = q.vwap if q.filled > 0 else trigger
        filled = min(q.filled or qty, p.qty)
        return self._book_close(symbol, p, filled, price, role, mark, trigger, q)

    def _book_close(self, symbol, p: PaperPosition, qty: float, price: float,
                    role: str, mark: float, trigger: float | None, q: Quote) -> PaperFill:
        sign = 1 if p.side == 'LONG' else -1
        pnl = (price - p.entry) * qty * sign
        self.cash += pnl
        p.realized += pnl
        p.qty -= qty
        fill = self._record(role, symbol, p.side, qty, price, mark, trigger or mark, q)
        if p.qty <= 1e-12:
            self.book.pop(symbol, None)
        return fill

    def _record(self, role, symbol, side, qty, price, mark, reference, q: Quote) -> PaperFill:
        buying = (side == 'SHORT') if role != 'ENTRY' else (side == 'LONG')
        fill = PaperFill(ts_ms=_now_ms(), symbol=symbol, role=role, side=side,
                         qty=qty, price=price, reference=reference,
                         slippage_bps=q.slippage_bps(reference, buying),
                         mark=mark, trigger=None if role == 'ENTRY' else reference,
                         depth_exhausted=q.exhausted)
        self.fills.append(fill)
        return fill

    def _accrue_funding(self, marks: dict[str, float]) -> None:
        """Lighter settles funding every hour. Charge once per hour boundary."""
        hour = int(time.time()) // 3600
        if hour == self._last_funding_hour or not self.book:
            self._last_funding_hour = max(self._last_funding_hour, hour)
            return
        for symbol, p in self.book.items():
            market = self.markets[symbol]
            rate = self._latest_funding_rate(market.market_id)
            if rate is None:
                continue
            mark = marks.get(symbol, p.entry)
            fraction = rate / 100 if self.funding_rate_is_percent else rate
            # Positive rate: longs pay shorts.
            amount = -p.qty * mark * fraction * (1 if p.side == 'LONG' else -1)
            self.cash += amount
            p.funding += amount
        self._last_funding_hour = hour

    def _latest_funding_rate(self, market_id: int) -> float | None:
        now = int(time.time())
        try:
            d = self._get(f'/api/v1/fundings?market_id={market_id}&resolution=1h'
                          f'&start_timestamp={now - 7200}&end_timestamp={now}&count_back=2')
        except Exception:
            return None
        rows = d.get('fundings') or []
        if not rows:
            return None
        try:
            return float(rows[-1]['rate'])
        except (KeyError, TypeError, ValueError):
            return None

    # -- measurement --------------------------------------------------------- #

    def measurements(self) -> dict:
        """What stage 2 exists to produce."""
        def stats(rows):
            vals = sorted(f.slippage_bps for f in rows
                          if f.slippage_bps == f.slippage_bps)      # drop NaN
            if not vals:
                return None
            mid = len(vals) // 2
            return {'n': len(vals), 'mean_bps': sum(vals) / len(vals),
                    'median_bps': vals[mid], 'worst_bps': vals[-1], 'best_bps': vals[0]}

        entries = [f for f in self.fills if f.role == 'ENTRY']
        stops = [f for f in self.fills if f.role == 'STOP']
        tps = [f for f in self.fills if f.role == 'TP2R']
        exits = [f for f in self.fills if f.role == 'EXIT']
        gaps = [abs(f.price - f.trigger) / f.trigger * 10_000
                for f in stops + tps if f.trigger]
        return {
            'equity': self.equity(), 'cash': self.cash,
            'open_positions': len(self.book),
            'fills': len(self.fills),
            'slippage_entry': stats(entries),
            'slippage_stop': stats(stops),
            'slippage_take_profit': stats(tps),
            'slippage_close_rule_exit': stats(exits),
            'trigger_to_fill_bps': {
                'n': len(gaps),
                'mean': sum(gaps) / len(gaps) if gaps else None,
                'worst': max(gaps) if gaps else None,
            },
            'depth_exhausted_fills': sum(1 for f in self.fills if f.depth_exhausted),
            'funding_paid': sum(p.funding for p in self.book.values()),
            'backtest_assumption_bps': 2.0,
        }


class _MakerQuote:
    """A resting order that filled at its own price: no spread, no depth walked."""

    def __init__(self, qty: float):
        self.filled = qty
        self.requested = qty
        self.exhausted = False

    def slippage_bps(self, reference, buying):
        return 0.0


def _now_ms() -> int:
    return int(time.time() * 1000)

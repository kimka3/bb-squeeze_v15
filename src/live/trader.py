"""The 4h boundary orchestration.

Every decision here comes from live_core — the same functions the backtest runs.
This module only decides WHEN to call them and turns their output into orders.
If you find strategy logic in this file, it is a bug: move it into live_core so
the backtest exercises it too.

Order within a bar mirrors frontier_engine.run() exactly and is not negotiable:

    sync fills -> reconcile -> age positions -> close rules -> scan signals
               -> execute exits -> execute entries (canonical, sequential)
               -> place protective orders -> amend trails -> journal

Exits run before entries because risk freed by a close is available to the same
bar's entries. Entries run one at a time in canonical symbol order because the
risk and notional room shrink with each fill.
"""
from __future__ import annotations

from dataclasses import dataclass

from data_io import H4, LONGS, SYMBOLS
from live_core import (apply_tp_fill, btc_bull_at, open_position, plan_entry,
                       remaining_risk, scan_signals, update_on_bar_close)
from trade_core import Costs

from . import guards
from .broker import Fill
from .journal import client_order_index


@dataclass
class BarOutcome:
    bar_ms: int
    equity: float
    exits: list = None
    entries: list = None
    skipped: list = None
    blocked: list = None
    halted: bool = False
    reasons: list = None

    def __post_init__(self):
        for f in ('exits', 'entries', 'skipped', 'blocked', 'reasons'):
            if getattr(self, f) is None:
                setattr(self, f, [])


class Trader:
    def __init__(self, config, broker, markets, journal):
        self.config = config
        self.cfg = config.strategy
        self.broker = broker
        self.markets = markets
        self.journal = journal
        self.costs = Costs(self.cfg.fee, self.cfg.slippage)
        # Traded universe. Excluded symbols never produce a signal here, so a
        # position in one can only come from outside the bot — reconcile halts on it.
        self.universe = list(config.universe)
        self.longs = [s for s in LONGS if s in self.universe]

        snap = journal.load_snapshot()
        self.positions: dict[str, dict] = snap.get('positions', {})
        self.setups: dict[str, list] = snap.get('setups', {})
        self.pending: dict[str, tuple] = {k: tuple(v) for k, v in snap.get('pending', {}).items()}
        self.orders: dict[str, dict] = snap.get('orders', {})
        self.last_bar_ms: int = snap.get('last_bar_ms', 0)

    # -- state ------------------------------------------------------------- #

    def _save(self) -> None:
        self.journal.save_snapshot({
            'positions': self.positions, 'setups': self.setups,
            'pending': {k: list(v) for k, v in self.pending.items()},
            'orders': self.orders, 'last_bar_ms': self.last_bar_ms})

    def _sync_fills(self, state) -> list[str]:
        """Reflect what the exchange did between bars: stops and 2R partials.

        The exchange is the source of truth for fills. A position it no longer
        holds was stopped out; one at roughly half size took its 2R partial, so
        the breakeven transition has to be applied here exactly as intrabar()
        applies it in the backtest.
        """
        notes = []
        reverse = {v: k for k, v in self.config_symbol_map.items()}
        live = {}
        for lighter_symbol, pos in state.positions.items():
            strat = reverse.get(lighter_symbol)
            if strat:
                live[strat] = pos
        for symbol in list(self.positions):
            p = self.positions[symbol]
            actual = live.get(symbol)
            if actual is None:
                notes.append(f'{symbol}: closed on exchange (stop or protective fill)')
                self.positions.pop(symbol)
                self.orders.pop(symbol, None)
                continue
            if actual['qty'] < p['qty'] * .99:
                p['qty'] = actual['qty']
                if p['partial_taken']:
                    continue
                # A resting post-only take-profit can fill in pieces, unlike the
                # all-or-nothing trigger order it replaced. Arm breakeven only
                # once the whole 2R leg is done: moving the stop after a sliver
                # filled would tighten it on a position that still carries its
                # full intended risk.
                closed = p['initial_qty'] - actual['qty']
                leg = p['initial_qty'] * self.cfg.tp_fraction
                if leg > 0 and closed >= leg * .99:
                    apply_tp_fill(p)
                    notes.append(f'{symbol}: 2R partial filled, stop moved to breakeven')
                else:
                    notes.append(f'{symbol}: take-profit {closed / leg:.0%} filled — '
                                 'breakeven not armed until the leg completes')
        return notes

    @property
    def config_symbol_map(self) -> dict:
        from .config import SYMBOL_TO_LIGHTER
        return SYMBOL_TO_LIGHTER

    # -- the bar ------------------------------------------------------------ #

    def on_bar(self, bar_ms: int, rows: dict, marks: dict,
               now_ms: int | None = None) -> BarOutcome:
        """Run one 4h boundary. rows are the just-closed bar; marks are current.

        now_ms is injectable so a replay can drive this over historical bars.
        """
        state = self.broker.account_state()
        if bar_ms <= self.last_bar_ms:
            # A restart re-offers the bar it already handled. Report the real
            # equity anyway: over a multi-week run a human reads these lines, and
            # a placeholder zero here looks exactly like a wiped account.
            return BarOutcome(bar_ms, state.equity,
                              reasons=[f'bar {bar_ms} already processed'])
        # bar_ms is the bar that just CLOSED and produced the signals; fills land
        # in the bar now opening. The backtest stamps trades with the entry bar.
        entry_ms = bar_ms + H4

        out = BarOutcome(bar_ms, state.equity)

        fresh = guards.check_bar_freshness(bar_ms, self.config, now_ms)
        if not fresh:
            out.reasons += fresh.reasons
            out.halted = fresh.halt
            self.journal.append('bar_skipped', bar_ms=bar_ms, reasons=fresh.reasons)
            return out

        out.reasons += self._sync_fills(state)

        mismatch = guards.reconcile(self.positions, state, self.config_symbol_map)
        if mismatch.halt:
            out.halted = True
            out.reasons += mismatch.reasons
            self.journal.append('reconcile_halt', bar_ms=bar_ms, reasons=mismatch.reasons)
            return out

        margin = guards.check_margin(state, self.config)
        basis, blocked = guards.check_basis({s: float(r['close']) for s, r in rows.items()},
                                            marks, self.config)
        out.reasons += margin.reasons + basis.reasons
        out.blocked = sorted(blocked)
        out.halted = margin.halt

        # A position held through the closed bar ages by one bar before the close
        # rules run, matching the backtest's ordering.
        for p in self.positions.values():
            p['bars'] += 1

        for symbol, p in self.positions.items():
            update_on_bar_close(p, rows[symbol], self.cfg)

        # Signals come from the bar that just closed and are acted on at the
        # open now forming — the backtest's close(i-1) -> open(i) ordering.
        # Scanning BEFORE the exits matters: a symbol still held at that close
        # is skipped, exactly as the backtest skips it.
        # pending is derived fresh every bar, never carried across a restart.
        self.pending, filtered = scan_signals(bar_ms, rows, self.universe, self.longs,
                                              self.positions, self.setups, self.cfg)

        # The new bar's open is unobservable at the moment we act, so the live
        # reference is the current Lighter mark — which is also where the order
        # actually fills and how the notional cap should value the open book.
        # Exits and entries must both use it: an exit filled at the PREVIOUS
        # bar's close books the wrong realised PnL and mis-sizes every entry
        # that follows it in the same bar.
        opens = {s: float(marks.get(s, rows[s]['close'])) for s in rows}

        # 1. exits decided at this close, executed now at the new bar's open
        for symbol in list(self.positions):
            reason = self.positions[symbol].pop('pending_exit', None)
            if reason:
                self._exit(symbol, reason, opens, entry_ms, out)

        # 2. entries in canonical order, one at a time
        entry_block = out.halted or margin.block_entries
        for symbol in self.universe:
            req = self.pending.pop(symbol, None)
            if req is None or symbol in self.positions:
                continue
            if entry_block:
                out.skipped.append((symbol, 'entries blocked by guard'))
                continue
            if symbol in blocked:
                out.skipped.append((symbol, 'basis divergence'))
                continue
            # Equity is re-read before EVERY entry. A fill at a slipped price
            # shows that slippage as unrealised loss immediately, so the next
            # entry in the same bar sizes against a slightly smaller account —
            # which is exactly what the backtest does when it recomputes
            # wealth=eq(opens) inside its allocation loop.
            self._enter(symbol, req, opens, rows, entry_ms, out,
                        self.broker.account_state().equity)

        # 3. bookkeeping
        self.last_bar_ms = bar_ms

        # 4. trailing stops move once per bar, monotonically
        self._sync_protective_orders(out, entry_ms)

        self._save()
        self.journal.append('bar', bar_ms=bar_ms, equity=state.equity,
                            exits=out.exits, entries=out.entries, skipped=out.skipped,
                            blocked=out.blocked, filtered=filtered, reasons=out.reasons,
                            positions=len(self.positions))
        return out

    # -- actions ------------------------------------------------------------ #

    def _exit(self, symbol: str, reason: str, opens: dict, bar_ms: int, out: BarOutcome) -> None:
        p = self.positions[symbol]
        market = self.markets[symbol]
        price = float(opens[symbol])
        coi = client_order_index(bar_ms, symbol, f'EXIT_{reason}')
        self.journal.append('order_intent', bar_ms=bar_ms, intent=f'exit:{symbol}',
                            reason=reason, qty=p['qty'], coi=coi)
        # Drop the protective orders first: a reduce-only stop left behind after
        # the position is flat is harmless, but cancelling keeps the book clean.
        self._cancel_protective(symbol, market)
        fill = self.broker.market_order(market, symbol, p['side'], p['qty'],
                                        closing=True, coi=coi, ref_price=price)
        self.journal.append('order_ack', bar_ms=bar_ms, intent=f'exit:{symbol}',
                            filled=fill.qty, price=fill.price)
        self.positions.pop(symbol, None)
        self.orders.pop(symbol, None)
        out.exits.append({'symbol': symbol, 'reason': reason, 'qty': fill.qty})

    def _enter(self, symbol: str, req, opens: dict, rows: dict,
               bar_ms: int, out: BarOutcome, equity: float) -> None:
        side, atr, signal_ms = req
        market = self.markets[symbol]
        plan = plan_entry(symbol, side, atr, equity, opens, self.positions,
                          self.cfg, self.costs,
                          btc_bull=btc_bull_at(rows['BTCUSDT']),
                          correlated=lambda want: [])
        if plan.skipped:
            out.skipped.append((symbol, plan.skipped))
            return
        reason = guards.check_sizing(market, plan.qty, plan.px, self.config)
        if reason:
            out.skipped.append((symbol, reason))
            return

        coi = client_order_index(bar_ms, symbol, 'ENTRY')
        self.journal.append('order_intent', bar_ms=bar_ms, intent=f'entry:{symbol}',
                            side=side, qty=plan.qty, price=plan.px, coi=coi)
        fill = self.broker.market_order(market, symbol, side, plan.qty,
                                        closing=False, coi=coi, ref_price=plan.px)
        if fill.qty <= 0:
            out.skipped.append((symbol, 'no fill'))
            return
        self.journal.append('order_ack', bar_ms=bar_ms, intent=f'entry:{symbol}',
                            filled=fill.qty, price=fill.price)

        # Risk accounting keys off the FILLED quantity, never the requested one.
        fee = fill.qty * fill.price * self.costs.fee
        self.positions[symbol] = open_position(
            side, bar_ms, signal_ms, plan, fill.qty, equity, fee,
            float(rows[symbol].get('x_bb_lower', 0.)))
        self._place_protective(symbol, bar_ms)
        out.entries.append({'symbol': symbol, 'side': side, 'qty': fill.qty,
                            'price': fill.price, 'stop': self.positions[symbol]['stop']})

    def _place_protective(self, symbol: str, bar_ms: int) -> None:
        """Stop for the whole position, plus the 2R partial on shorts.

        The stop is reduce-only and sized to the full position, so when the
        take-profit removes half, the stop still covers what remains. There is no
        window in which the position is unprotected.
        """
        p = self.positions[symbol]
        market = self.markets[symbol]
        refs = {}
        sl_coi = client_order_index(bar_ms, symbol, 'SL')
        refs['SL'] = self.broker.place_stop(market, symbol, p['side'], p['qty'],
                                            p['stop'], sl_coi).__dict__
        if p['side'] == 'SHORT' and self.cfg.tp_fraction > 0:
            tp_coi = client_order_index(bar_ms, symbol, 'TP')
            refs['TP'] = self.broker.place_take_profit(
                market, symbol, p['side'], p['qty'] * self.cfg.tp_fraction,
                p['tp2r'], tp_coi).__dict__
        self.orders[symbol] = refs
        self.journal.append('protective_placed', bar_ms=bar_ms, symbol=symbol,
                            stop=p['stop'], tp=p.get('tp2r'), refs=refs)

    def _cancel_protective(self, symbol: str, market) -> None:
        from .broker import OrderRef
        for ref in (self.orders.get(symbol) or {}).values():
            try:
                self.broker.cancel(market, OrderRef(**ref))
            except Exception as exc:                     # a stale order is not fatal
                self.journal.append('cancel_failed', symbol=symbol, error=str(exc))

    def _sync_protective_orders(self, out: BarOutcome, bar_ms: int) -> None:
        """Amend resting stops to this bar's trailed level.

        Amend, never cancel-and-replace. And refuse any move that would loosen
        the stop: live_core already guarantees monotonicity, so a loosening
        request here means state is wrong and the safe response is to skip it.
        """
        from .broker import OrderRef
        for symbol, p in self.positions.items():
            refs = self.orders.get(symbol) or {}
            sl = refs.get('SL')
            if not sl:
                self._place_protective(symbol, bar_ms)
                continue
            placed = sl.get('trigger')
            if placed is not None:
                loosening = (p['stop'] < placed) if p['side'] == 'LONG' else (p['stop'] > placed)
                if loosening:
                    out.reasons.append(f'{symbol}: refused a loosening stop amend')
                    continue
                if abs(p['stop'] - placed) < 1e-12:
                    continue
            try:
                self.broker.modify_stop(self.markets[symbol], OrderRef(**{
                    k: v for k, v in sl.items() if k in ('client_order_index', 'order_index', 'purpose')
                }), p['qty'], p['stop'])
                sl['trigger'] = p['stop']
                self.journal.append('stop_amended', bar_ms=bar_ms, symbol=symbol, stop=p['stop'])
            except Exception as exc:
                # The previous stop is still resting; the position stays covered.
                out.reasons.append(f'{symbol}: stop amend failed ({exc}); previous stop stands')
                self.journal.append('stop_amend_failed', bar_ms=bar_ms, symbol=symbol,
                                    error=str(exc))

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
import math
import time

from data_io import H4, LONGS, SYMBOLS
from live_core import (EntryPlan, apply_tp_fill, btc_bull_at, open_position, plan_entry,
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
        self.pending_operation = snap.get('pending_operation')
        self.inflight_bar_ms = snap.get('inflight_bar_ms')
        self.halted = bool(snap.get('halted', False))
        self.halt_reasons = list(snap.get('halt_reasons', []))
        self.residual_exits = dict(snap.get('residual_exits', {}))
        self.orphan_orders = dict(snap.get('orphan_orders', {}))
        self.data_gaps = list(snap.get('data_gaps', []))
        self.protection_generation = dict(snap.get('protection_generation', {}))
        self._needs_recovery = bool(self.pending_operation or self.inflight_bar_ms)

    # -- state ------------------------------------------------------------- #

    def _save(self) -> None:
        self.journal.save_snapshot({
            'positions': self.positions, 'setups': self.setups,
            'pending': {k: list(v) for k, v in self.pending.items()},
            'orders': self.orders, 'last_bar_ms': self.last_bar_ms,
            'pending_operation': self.pending_operation,
            'inflight_bar_ms': self.inflight_bar_ms,
            'halted': self.halted, 'halt_reasons': self.halt_reasons,
            'residual_exits': self.residual_exits, 'orphan_orders': self.orphan_orders,
            'data_gaps': self.data_gaps, 'protection_generation': self.protection_generation})

    def _halt(self, reason: str) -> None:
        self.halted = True
        if reason not in self.halt_reasons:
            self.halt_reasons.append(reason)
            self.journal.append('safety_halt', reason=reason)
        self._save()

    def _begin_operation(self, operation: dict) -> None:
        if self.pending_operation is not None:
            raise RuntimeError('an unresolved operation already exists; refusing another order')
        self.pending_operation = operation
        self._save()  # Write-ahead intent: before ANY broker mutation.

    @staticmethod
    def _order_ref(ref):
        from .broker import OrderRef
        return OrderRef(**{k: v for k, v in ref.items()
                           if k in OrderRef.__dataclass_fields__})

    def _sync_fills(self, state) -> list[str]:
        """Apply ledger-confirmed changes; a smaller position is not TP proof."""
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
                if not state.execution_changes_verified:
                    self._halt(f'{symbol}: unexplained disappearance; execution evidence required')
                    continue
                notes.append(f'{symbol}: closure confirmed by execution ledger')
                self._cancel_protective(symbol, self.markets[symbol])
                self.positions.pop(symbol)
                self.orders.pop(symbol, None)
                self.residual_exits.pop(symbol, None)
                continue
            if actual['side'] != p['side']:
                self._halt(f'{symbol}: position side changed unexpectedly')
                continue
            if actual['qty'] < p['qty'] - 1e-12:
                if not state.execution_changes_verified:
                    self._halt(f'{symbol}: unexplained reduction; execution evidence required')
                    continue
                p['qty'] = float(actual['qty'])
                notes.append(f'{symbol}: ledger-confirmed residual quantity {p["qty"]}')
            if (state.execution_changes_verified and actual.get('partial_taken') is True
                    and not p['partial_taken']):
                apply_tp_fill(p)
                notes.append(f'{symbol}: complete TP leg confirmed; breakeven armed')
        return notes

    def service(self, now_ms: int | None = None, marks: dict | None = None) -> BarOutcome:
        """Fast risk service, also called while HALT blocks new entries.

        Paper results are recoverable by durable order id. Unknown submission
        outcomes stay halted and are never blindly retransmitted.
        """
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        marks = marks if marks is not None else getattr(self.broker, '_marks', {})
        samples = getattr(self.broker, '_mark_sample_ms', None)
        max_age = max(0., float(getattr(self.config, 'poll_seconds', 5.))) * 2 * 1000
        fresh_symbols = {s for s, value in marks.items()
                         if math.isfinite(value) and value > 0 and
                         (samples is None or
                          (s in samples and 0 <= now_ms - samples[s] <= max_age))}
        out = BarOutcome(now_ms, self.broker.account_state().equity)
        for symbol, refs in list(self.orphan_orders.items()):
            unresolved = {}
            for purpose, ref in refs.items():
                try:
                    self.broker.cancel(self.markets[symbol], self._order_ref(ref))
                except Exception as exc:
                    unresolved[purpose] = ref
                    out.reasons.append(f'{symbol}: orphan {purpose} cancellation failed: {exc}')
            if unresolved:
                self.orphan_orders[symbol] = unresolved
            else:
                self.orphan_orders.pop(symbol, None)
        if self._needs_recovery:
            self._recover_operation(out, fresh_symbols)
            if self.pending_operation is None:
                if self.inflight_bar_ms is not None:
                    self.last_bar_ms = max(self.last_bar_ms, self.inflight_bar_ms)
                    self.journal.append('interrupted_bar_skipped', bar_ms=self.inflight_bar_ms,
                                        reason='completed actions retained; remaining entries skipped')
                    self.inflight_bar_ms = None
                    self.pending.clear()
                self._needs_recovery = False
                self._save()
        state = self.broker.account_state()
        out.reasons += self._sync_fills(state)
        margin = guards.check_margin(state, self.config)
        if margin.halt:
            for reason in margin.reasons:
                self._halt(reason)
        mismatch = guards.reconcile(self.positions, state, self.config_symbol_map)
        if mismatch.halt:
            for reason in mismatch.reasons:
                self._halt(reason)
            out.reasons += mismatch.reasons
        if self.pending_operation is None and not mismatch.halt:
            # A missing SL after an IOC trigger is an emergency residual, not a
            # successful stop. Try only confirmed reduce-only close operations.
            if state.active_orders_verified:
                for symbol in list(self.positions):
                    active = state.active_orders.get(self.config_symbol_map[symbol], [])
                    sl = (self.orders.get(symbol) or {}).get('SL')
                    found = sl and any(o.get('client_order_index') == sl['client_order_index']
                                       and o.get('purpose') == 'SL' for o in active)
                    if not found:
                        self._halt(f'{symbol}: protective stop missing; entries halted')
                        if sl:
                            self.protection_generation[symbol] = self.protection_generation.get(symbol, 0) + 1
                        self.orders.setdefault(symbol, {}).pop('SL', None)
                        self.residual_exits.setdefault(symbol, {
                            'reason': 'PROTECTION_LOST', 'bar_ms': now_ms, 'attempt': 0})
            for symbol, request in list(self.residual_exits.items()):
                if self.pending_operation is not None:
                    break
                if symbol in self.positions and symbol in fresh_symbols:
                    self._exit(symbol, request['reason'], marks, request['bar_ms'], out)
                elif symbol in self.positions:
                    out.reasons.append(f'{symbol}: residual close awaits a fresh mark')
            if self.pending_operation is None:
                self._sync_protective_orders(out, now_ms, fresh_symbols=fresh_symbols)
        out.halted = (self.halted or self.pending_operation is not None
                      or bool(self.residual_exits) or bool(self.orphan_orders))
        out.reasons += self.halt_reasons
        self._save()
        return out

    def _recover_operation(self, out: BarOutcome, fresh_symbols: set[str]) -> None:
        op = self.pending_operation
        if op is None:
            return
        try:
            if op['kind'] in ('entry', 'exit') and op.get('phase') != 'protect':
                lookup = getattr(self.broker, 'market_order_result', None)
                fill = lookup(op['coi']) if lookup else None
                if fill is None:
                    self._halt('pending order outcome unknown; manual reconciliation required')
                    return
                if op['kind'] == 'entry':
                    self._finish_entry(op, fill, out, protect=op['symbol'] in fresh_symbols)
                else:
                    self._finish_exit(op, fill, out)
            elif op['kind'] == 'protection' or op.get('phase') == 'protect':
                state = self.broker.account_state()
                if (state.execution_changes_verified and
                        self.config_symbol_map[op['symbol']] not in state.positions):
                    # An already-accepted stop may have closed the paper position
                    # before restart could finish registering the other leg.
                    self._cancel_protective(op['symbol'], self.markets[op['symbol']])
                    self.positions.pop(op['symbol'], None)
                    self.orders.pop(op['symbol'], None)
                    self.residual_exits.pop(op['symbol'], None)
                    self.pending_operation = None
                    self._save()
                    return
                if op['symbol'] not in fresh_symbols:
                    out.reasons.append(f'{op["symbol"]}: protection recovery awaits a fresh mark')
                    return
                self._place_protective(op['symbol'], op['bar_ms'])
                self.pending_operation = None
                self._save()
            elif op['kind'] == 'amend':
                # A simulator exposes an exact active-order ledger. For other
                # adapters no acknowledgement is fabricated from position size.
                state = self.broker.account_state()
                active = state.active_orders.get(self.config_symbol_map[op['symbol']], [])
                match = next((o for o in active if o.get('client_order_index') == op['coi']
                              and o.get('trigger') == op['trigger']), None)
                if not state.active_orders_verified or match is None:
                    self._halt('stop amendment outcome unknown; manual reconciliation required')
                    return
                self.orders[op['symbol']]['SL'].update(trigger=op['trigger'], qty=op['qty'])
                self.pending_operation = None
                self._save()
        except Exception as exc:
            self._halt(f'operation recovery failed: {exc}')

    @property
    def config_symbol_map(self) -> dict:
        from .config import SYMBOL_TO_LIGHTER
        return SYMBOL_TO_LIGHTER

    # -- the bar ------------------------------------------------------------ #

    def on_bar(self, bar_ms: int, rows: dict, marks: dict,
               now_ms: int | None = None, allow_entries: bool = True) -> BarOutcome:
        """Run one 4h boundary. rows are the just-closed bar; marks are current.

        now_ms is injectable so a replay can drive this over historical bars.
        """
        if self._needs_recovery:
            recovered = self.service(now_ms=now_ms, marks=marks)
            if self.pending_operation is not None:
                return recovered
        state = self.broker.account_state()
        if bar_ms <= self.last_bar_ms:
            # A restart re-offers the bar it already handled. Report the real
            # equity anyway: over a multi-week run a human reads these lines, and
            # a placeholder zero here looks exactly like a wiped account.
            return BarOutcome(bar_ms, state.equity, halted=self.halted,
                              reasons=[f'bar {bar_ms} already processed'])
        if self.last_bar_ms and bar_ms - self.last_bar_ms > H4:
            gap = {'previous_bar_ms': self.last_bar_ms, 'observed_bar_ms': bar_ms,
                   'unobserved_ms': bar_ms - self.last_bar_ms - H4,
                   'status': 'unresolved',
                   'effect': 'missed triggers, trailing and setups cannot be reconstructed'}
            self.data_gaps.append(gap)
            self.journal.append('data_gap', **gap)
            self.last_bar_ms = bar_ms
            self._halt('bar history gap: strategy processing paused; protection service remains active')
        if self.data_gaps:
            # No synthetic catch-up, delayed entry, or invented trailing path.
            # This requires an explicit operator review of the missing history.
            self.last_bar_ms = max(self.last_bar_ms, bar_ms)
            self._save()
            protected = self.service(now_ms=now_ms, marks=marks)
            protected.bar_ms = bar_ms
            protected.halted = True
            return protected
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
            for reason in mismatch.reasons:
                self._halt(reason)
            return out

        self.inflight_bar_ms = bar_ms
        self._save()

        margin = guards.check_margin(state, self.config)
        basis, blocked = guards.check_basis({s: float(r['close']) for s, r in rows.items()},
                                            marks, self.config)
        out.reasons += margin.reasons + basis.reasons
        out.blocked = sorted(blocked)
        if margin.halt:
            for reason in margin.reasons:
                self._halt(reason)
        out.halted = self.halted or margin.halt

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
        entry_block = (out.halted or margin.block_entries or not allow_entries
                       or bool(self.residual_exits))
        for symbol in self.universe:
            req = self.pending.pop(symbol, None)
            if req is None or symbol in self.positions:
                continue
            if entry_block or self.halted or self.pending_operation is not None:
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

        if self.pending_operation is None:
            self.inflight_bar_ms = None
        out.halted = self.halted or self.pending_operation is not None or bool(self.residual_exits)
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
        attempt = self.residual_exits.get(symbol, {}).get('attempt', 0)
        coi = client_order_index(bar_ms, symbol, f'EXIT_{reason}', seq=attempt)
        op = {'kind': 'exit', 'symbol': symbol, 'bar_ms': bar_ms, 'reason': reason,
              'coi': coi, 'qty': p['qty'], 'side': p['side'], 'attempt': attempt}
        if self.pending_operation is not None:
            out.reasons.append(f'{symbol}: exit waits for unresolved operation')
            return
        self._begin_operation(op)
        self.journal.append('order_intent', bar_ms=bar_ms, intent=f'exit:{symbol}',
                            reason=reason, qty=p['qty'], coi=coi)
        try:
            # Keep SL/TP until flat is CONFIRMED. Partial and zero fills leave
            # the remaining position protected and explicitly queued for retry.
            fill = self.broker.market_order(market, symbol, p['side'], p['qty'],
                                            closing=True, coi=coi, ref_price=price)
            self._finish_exit(op, fill, out)
        except Exception as exc:
            self._needs_recovery = True
            self._halt(f'{symbol}: exit outcome unresolved: {exc}')

    def _finish_exit(self, op: dict, fill: Fill, out: BarOutcome) -> None:
        symbol = op['symbol']
        self._validate_fill(fill, op['symbol'], op['side'], op['qty'])
        state = self.broker.account_state()
        actual = state.positions.get(self.config_symbol_map[symbol])
        p = self.positions.get(symbol)
        if p is None:
            if actual is not None or not state.execution_changes_verified:
                raise RuntimeError('missing local close state requires reconciliation')
            self._cancel_protective(symbol, self.markets[symbol])
            self.orders.pop(symbol, None)
            self.residual_exits.pop(symbol, None)
            self.pending_operation = None
            self._save()
            return
        if actual is not None and (actual['side'] != p['side'] or
                                   actual['qty'] > op['qty'] + 1e-12):
            raise RuntimeError('position changed unexpectedly during close')
        if actual is None and fill.qty < op['qty'] - 1e-12 and not state.execution_changes_verified:
            raise RuntimeError('flat account lacks evidence for the full close')
        if actual is not None:
            remaining = float(actual['qty'])
            if not math.isclose(op['qty'] - fill.qty, remaining, rel_tol=1e-9, abs_tol=1e-12):
                if not state.execution_changes_verified:
                    raise RuntimeError('close fill and residual position disagree')
            p['qty'] = remaining
            self.residual_exits[symbol] = {
                'reason': op['reason'], 'bar_ms': op['bar_ms'], 'attempt': op['attempt'] + 1}
            out.reasons.append(f'{symbol}: close residual {remaining}; protection retained')
        else:
            self._cancel_protective(symbol, self.markets[symbol])
            self.positions.pop(symbol, None)
            self.orders.pop(symbol, None)
            self.residual_exits.pop(symbol, None)
        self.journal.append('order_ack', bar_ms=op['bar_ms'], intent=f'exit:{symbol}',
                            filled=fill.qty, price=fill.price, coi=op['coi'],
                            remaining=actual['qty'] if actual else 0.)
        out.exits.append({'symbol': symbol, 'reason': op['reason'], 'qty': fill.qty})
        self.pending_operation = None
        self._save()

    @staticmethod
    def _validate_fill(fill, symbol, side, requested):
        if (fill.symbol != symbol or fill.side != side or not math.isfinite(fill.qty)
                or fill.qty < 0 or fill.qty > requested * (1 + 1e-9)
                or (fill.qty > 0 and (not math.isfinite(fill.price) or fill.price <= 0))):
            raise RuntimeError('invalid confirmed fill')

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
        op = {'kind': 'entry', 'phase': 'submit', 'symbol': symbol, 'bar_ms': bar_ms,
              'coi': coi, 'side': side, 'plan': list(plan), 'signal_ms': signal_ms,
              'equity': equity, 'breakout': float(rows[symbol].get('x_bb_lower', 0.))}
        self._begin_operation(op)
        self.journal.append('order_intent', bar_ms=bar_ms, intent=f'entry:{symbol}',
                            side=side, qty=plan.qty, price=plan.px, coi=coi)
        try:
            fill = self.broker.market_order(market, symbol, side, plan.qty,
                                            closing=False, coi=coi, ref_price=plan.px)
            self._finish_entry(op, fill, out)
        except Exception as exc:
            self._needs_recovery = True
            self._halt(f'{symbol}: entry/protection outcome unresolved: {exc}')

    def _finish_entry(self, op: dict, fill: Fill, out: BarOutcome, protect: bool = True) -> None:
        symbol = op['symbol']
        plan = EntryPlan(*op['plan'])
        self._validate_fill(fill, symbol, op['side'], plan.qty)
        if fill.qty <= 0:
            self.pending_operation = None
            self._save()
            out.skipped.append((symbol, 'confirmed no fill'))
            return
        # Actual fill price anchors the new position; replay fills equal plan.px.
        plan = plan._replace(px=fill.price)
        self.positions[symbol] = open_position(
            op['side'], op['bar_ms'], op['signal_ms'], plan, fill.qty,
            op['equity'], fill.qty * fill.price * self.costs.fee, op['breakout'])
        op['phase'] = 'protect'
        self._save()  # Filled exposure survives a failure placing either leg.
        self.journal.append('order_ack', bar_ms=op['bar_ms'], intent=f'entry:{symbol}',
                            filled=fill.qty, price=fill.price, coi=op['coi'])
        if not protect:
            out.reasons.append(f'{symbol}: confirmed entry recovered; protection awaits fresh mark')
            return
        self._place_protective(symbol, op['bar_ms'])
        self.pending_operation = None
        self._save()
        out.entries.append({'symbol': symbol, 'side': op['side'], 'qty': fill.qty,
                            'price': fill.price, 'stop': self.positions[symbol]['stop']})

    def _place_protective(self, symbol: str, bar_ms: int) -> None:
        """Stop for the whole position, plus the 2R partial on shorts.

        The stop is reduce-only and sized to the full position. Each accepted
        leg is persisted separately; failure leaves a durable recovery intent.
        """
        p = self.positions[symbol]
        market = self.markets[symbol]
        own_operation = self.pending_operation is None
        if own_operation:
            self._begin_operation({'kind': 'protection', 'symbol': symbol, 'bar_ms': bar_ms})
        refs = self.orders.setdefault(symbol, {})
        sl_coi = client_order_index(bar_ms, symbol, 'SL', seq=self.protection_generation.get(symbol, 0))
        if 'SL' not in refs:
            refs['SL'] = self.broker.place_stop(market, symbol, p['side'], p['qty'],
                                                p['stop'], sl_coi).__dict__
            refs['SL'].update(trigger=p['stop'], qty=p['qty'], position_side=p['side'])
            self._save()  # A failed TP placement must not lose an accepted SL.
        if (p['side'] == 'SHORT' and self.cfg.tp_fraction > 0
                and not p['partial_taken'] and 'TP' not in refs):
            tp_coi = client_order_index(bar_ms, symbol, 'TP')
            refs['TP'] = self.broker.place_take_profit(
                market, symbol, p['side'], p['qty'] * self.cfg.tp_fraction,
                p['tp2r'], tp_coi).__dict__
            self._save()
        self.journal.append('protective_placed', bar_ms=bar_ms, symbol=symbol,
                            stop=p['stop'], tp=p.get('tp2r'), refs=refs)
        if own_operation:
            self.pending_operation = None
            self._save()

    def _cancel_protective(self, symbol: str, market) -> None:
        for purpose, ref in (self.orders.get(symbol) or {}).items():
            try:
                self.broker.cancel(market, self._order_ref(ref))
            except Exception as exc:
                # An old reduce-only order can affect a future position. Preserve
                # unresolved cancellations even after this position is flat.
                self.orphan_orders.setdefault(symbol, {})[purpose] = dict(ref)
                self.journal.append('cancel_failed', symbol=symbol, error=str(exc))
                self._halt(f'{symbol}: protective cancellation unresolved; new entries halted')

    def _sync_protective_orders(self, out: BarOutcome, bar_ms: int,
                                fresh_symbols: set[str] | None = None) -> None:
        """Amend resting stops to this bar's trailed level.

        Amend, never cancel-and-replace. And refuse any move that would loosen
        the stop: live_core already guarantees monotonicity, so a loosening
        request here means state is wrong and the safe response is to skip it.
        """
        for symbol, p in self.positions.items():
            if self.pending_operation is not None:
                return
            refs = self.orders.get(symbol) or {}
            sl = refs.get('SL')
            if not sl:
                if fresh_symbols is not None and symbol not in fresh_symbols:
                    out.reasons.append(f'{symbol}: missing stop awaits fresh mark; entries blocked')
                    continue
                try:
                    self._place_protective(symbol, bar_ms)
                except Exception as exc:
                    self._needs_recovery = True
                    self._halt(f'{symbol}: protective placement unresolved: {exc}')
                continue
            placed = sl.get('trigger')
            if placed is not None:
                loosening = (p['stop'] < placed) if p['side'] == 'LONG' else (p['stop'] > placed)
                if loosening:
                    out.reasons.append(f'{symbol}: refused a loosening stop amend')
                    continue
                if (abs(p['stop'] - placed) < 1e-12
                        and sl.get('qty') is not None
                        and math.isclose(p['qty'], sl['qty'], rel_tol=1e-9, abs_tol=1e-12)):
                    continue
            try:
                self._begin_operation({'kind': 'amend', 'symbol': symbol, 'bar_ms': bar_ms,
                                       'coi': sl['client_order_index'], 'qty': p['qty'],
                                       'trigger': p['stop']})
                self.broker.modify_stop(self.markets[symbol], self._order_ref(sl),
                                        p['qty'], p['stop'])
                sl['trigger'] = p['stop']
                sl['qty'] = p['qty']
                self.pending_operation = None
                self._save()
                self.journal.append('stop_amended', bar_ms=bar_ms, symbol=symbol, stop=p['stop'])
            except Exception as exc:
                self._needs_recovery = True
                self._halt(f'{symbol}: stop amendment outcome unresolved: {exc}')
                out.reasons.append(f'{symbol}: stop amend failed ({exc}); protection requires verification')
                self.journal.append('stop_amend_failed', bar_ms=bar_ms, symbol=symbol,
                                    error=str(exc))

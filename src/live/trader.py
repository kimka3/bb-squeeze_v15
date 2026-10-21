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
from .request_budget import EntryDeferred, RequestDeferred
from .execution_evidence import NotSubmitted
from .stop_renewal import start_renewal, recover_renewal


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
        self.unresolved_operations = dict(snap.get('unresolved_operations', {}))
        self.inflight_bar_ms = snap.get('inflight_bar_ms')
        self.halted = bool(snap.get('halted', False))
        self.halt_reasons = list(snap.get('halt_reasons', []))
        self.residual_exits = dict(snap.get('residual_exits', {}))
        self.orphan_orders = dict(snap.get('orphan_orders', {}))
        self.data_gaps = list(snap.get('data_gaps', []))
        self.protection_generation = dict(snap.get('protection_generation', {}))
        self.publication_pending = bool(snap.get('publication_pending', False))
        self.publication_pending_evidence = list(snap.get('publication_pending_evidence', []))
        self._needs_recovery = bool(self.pending_operation or self.unresolved_operations or self.inflight_bar_ms)
        # Unsubmitted candidates are deliberately not restored after a restart.
        self.deferred_entries = None

    def _entry_now_ms(self):
        return int(getattr(self.broker, 'clock', time.time)() * 1000)

    def _defer_entries(self, bar_ms, rows, requests, error):
        now = self._entry_now_ms()
        deadline = bar_ms + H4 + self.config.bar_grace_seconds * 1000
        retry_at = now + max(250, math.ceil(error.retry_after * 1000))
        self.deferred_entries = {'bar_ms':bar_ms, 'rows':rows.copy(),
                                 'requests':{s:requests[s] for s in self.universe if s in requests}, 'deadline_ms':deadline,
                                 'retry_at_ms':retry_at}
        self.journal.append('entry_deferred', bar_ms=bar_ms, symbols=list(requests),
                            deadline_ms=deadline, retry_at_ms=retry_at,
                            budget=getattr(error, 'details', {}))

    def retry_deferred_entries(self):
        """Re-evaluate only unsubmitted candidates, never re-run a closed bar."""
        batch = self.deferred_entries
        if batch is None:
            return None
        now = self._entry_now_ms()
        guard = getattr(self.broker, 'entry_guard', None)
        if (now >= batch['deadline_ms'] or now < batch['bar_ms']+H4
                or self.halted or self.pending_operation or self.unresolved_operations or self.data_gaps
                or self.publication_pending
                or (guard is not None and not guard())):
            self.journal.append('entry_deferred_discarded', bar_ms=batch['bar_ms'],
                                symbols=list(batch['requests']),
                                reason='deadline, clock or operational guard')
            self.deferred_entries = None
            return None
        if now < batch['retry_at_ms']:
            return None
        try:
            budget = getattr(getattr(self.broker, 'transport', None), 'budget', None)
            if budget is not None:
                # Five refresh/service reads plus four sizing reads, followed
                # by the unchanged 24-slot guard. Extra pages are checked again.
                budget.check(33)
            # Protection has priority. Refresh marks and service existing risk
            # before considering a deferred candidate. Never sleep here.
            self.broker.poll()
            protected = self.service(now_ms=self._entry_now_ms())
            if protected.halted:
                self.deferred_entries = None
                return protected
            marks = dict(self.broker._marks)
            rows = batch['rows']
            _, blocked = guards.check_basis({s:float(r['close']) for s,r in rows.items()},
                                             marks, self.config)
            out = BarOutcome(batch['bar_ms'], protected.equity)
            for symbol in list(batch['requests']):
                if self._entry_now_ms() >= batch['deadline_ms']:
                    break
                if symbol in self.positions or symbol in blocked or symbol not in marks:
                    batch['requests'].pop(symbol)
                    out.skipped.append((symbol, 'deferred candidate no longer eligible'))
                    continue
                state = self.broker.account_state()
                self.observe_publication(state.ledger_report)
                margin = guards.check_margin(state, self.config)
                mismatch = guards.reconcile(self.positions, state, self.config_symbol_map)
                if (margin.block_entries or mismatch.halt or not state.execution_changes_verified
                        or self.residual_exits or self.orphan_orders):
                    self.deferred_entries = None
                    out.skipped.append((symbol, 'deferred entry blocked by fresh account state'))
                    return out
                self.broker.entry_deadline_ms = batch['deadline_ms']
                try:
                    result = self._enter(symbol, batch['requests'][symbol], marks, rows,
                                         batch['bar_ms']+H4, out, state.equity, entry_state=state)
                finally:
                    self.broker.entry_deadline_ms = None
                if isinstance(result, EntryDeferred):
                    self._defer_entries(batch['bar_ms'], rows, batch['requests'], result)
                    return out
                batch['requests'].pop(symbol)
                if self.halted or self.pending_operation:
                    break
            self.journal.append('entry_retry_result', bar_ms=batch['bar_ms'],
                                entries=out.entries, skipped=out.skipped,
                                discarded=list(batch['requests']))
            self.deferred_entries = None
            return out
        except RequestDeferred as exc:
            self._defer_entries(batch['bar_ms'], batch['rows'], batch['requests'], exc)
            return None

    # -- state ------------------------------------------------------------- #

    def _save(self) -> None:
        self.journal.save_snapshot({
            'positions': self.positions, 'setups': self.setups,
            'pending': {k: list(v) for k, v in self.pending.items()},
            'orders': self.orders, 'last_bar_ms': self.last_bar_ms,
            'pending_operation': self.pending_operation,
            'unresolved_operations': self.unresolved_operations,
            'publication_pending': self.publication_pending,
            'publication_pending_evidence': self.publication_pending_evidence,
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

    def _park_operation(self):
        """Isolate an uncertain operation without losing its durable identity."""
        if self.pending_operation is not None:
            op = self.pending_operation
            key = f'{op["symbol"]}:{op["kind"]}:{op.get("coi", op["bar_ms"])}'
            previous = self.unresolved_operations.get(key)
            if previous is not None and previous != op:
                raise RuntimeError('unresolved operation identity collision')
            self.unresolved_operations[key] = op
            self.pending_operation = None
            self._needs_recovery = True
            self._save()

    def _symbol_verified(self, state, symbol):
        return (state.execution_changes_verified or
                self.config_symbol_map[symbol] in state.ledger_report.get('verified_symbols', []) or
                self.config_symbol_map[symbol] in state.ledger_report.get('risk_verified_symbols', []))

    def _history_quarantined_symbols(self, state=None):
        report = state.ledger_report if state is not None else getattr(self.broker, 'last_report', {})
        markets = {item['market_id'] for item in report.get('historical_issues', [])}
        return {symbol for symbol, market in self.markets.items() if market.market_id in markets}

    def _initial_stop_waits_for_dispatch(self, symbol, sl, unsent):
        pending_ids = {int(coi) for coi, item in unsent.items()
                       if item.get('symbol') == symbol and item.get('role') == 'SL'
                       and item.get('retryable') and item.get('superseded_by') is None}
        if sl is not None:
            return sl['client_order_index'] in pending_ids
        # A renewal replacement that has never been sent cannot excuse loss of
        # the old native stop. With no current SL reference, only the exact ID
        # of a durable INITIAL protection operation may await a safe retry.
        operations = list(self.unresolved_operations.values())
        if self.pending_operation is not None:
            operations.append(self.pending_operation)
        return any(op['symbol'] == symbol
                   and (op['kind'] == 'protection' or
                        (op['kind'] == 'entry' and op.get('phase') == 'protect'))
                   and self._coi(op['bar_ms'], symbol, 'SL',
                                 seq=self.protection_generation.get(symbol, 0)) in pending_ids
                   for op in operations)

    def observe_publication(self, report):
        """Record strictly classified detail-history lag without clearing HALT.

        The broker proves owned cumulative fills against the actual account.
        This permits risk management only; full history is still required for
        entries and for all three stable exchange-recovery success cycles.
        """
        evidence = report.get('publication_pending_evidence') if isinstance(report, dict) else None
        risk_symbols = report.get('risk_verified_symbols') if isinstance(report, dict) else None
        pending = (isinstance(report, dict) and report.get('publication_pending') is True
                   and isinstance(evidence, list) and bool(evidence)
                   and all(isinstance(item, dict) for item in evidence)
                   and isinstance(risk_symbols, list) and all(isinstance(s, str) for s in risk_symbols)
                   and not report.get('unresolved_orders') and not report.get('unsafe_protection'))
        recovery = getattr(self, 'exchange_recovery', None)
        if pending and recovery is None:
            # Without the durable controller there is no three-cycle stability
            # gate after convergence. Keep the existing explicit safety halt.
            pending = False
            self._halt('owned execution publication pending without automatic recovery; inspect before resume')
        changed = pending != self.publication_pending
        self.publication_pending = pending
        self.publication_pending_evidence = list(evidence) if pending else []
        if pending:
            if self.deferred_entries is not None:
                batch = self.deferred_entries
                self.deferred_entries = None
                self.journal.append('entry_deferred_discarded', bar_ms=batch['bar_ms'],
                    symbols=list(batch['requests']), reason='owned execution publication pending')
            recovery.publication_wait(self.publication_pending_evidence)
        if changed:
            self.journal.append('execution_publication_state', pending=pending,
                                evidence=self.publication_pending_evidence)
            self._save()
        return pending

    def _uncertain_market_order(self, symbol):
        ops = list(self.unresolved_operations.values()) + ([self.pending_operation] if self.pending_operation else [])
        return any(op['symbol'] == symbol and op['kind'] in ('entry', 'exit')
                   and op.get('phase') != 'protect' for op in ops)

    def _not_submitted(self, op, exc, out):
        """Positive pre-dispatch proof is not an unknown exchange outcome."""
        self.journal.append('operation_not_submitted', symbol=op['symbol'], operation_kind=op['kind'],
                            coi=op.get('coi'), reason=exc.reason, retryable=exc.retryable)
        if op['kind'] == 'entry' and op.get('phase') != 'protect':
            self.pending_operation = None
            out.skipped.append((op['symbol'], f'not submitted: {exc.reason}'))
        elif op['kind'] == 'exit':
            self.residual_exits[op['symbol']] = {
                'reason': op['reason'], 'bar_ms': op['bar_ms'], 'attempt': op['attempt'],
                'cycle_ms': self.positions[op['symbol']]['entry_ms']}
            self.pending_operation = None
        else:
            # Retain protective work, with the exact ID/terms, for a fresh
            # account check next service cycle. The engine permits retry only
            # when durable local evidence proves dispatch never started.
            self._park_operation()
        self._save()
        if exc.reason == 'account_publication_pending':
            if not self.observe_publication(getattr(self.broker, 'last_report', {})):
                self._halt('publication wait lacks verified classification; inspect account evidence')
            return
        cause = exc.cause
        if cause is not None and hasattr(cause, 'automatic_recovery_allowed'):
            # Preserve the existing classification and recovery thresholds.
            raise cause
        if not exc.retryable and exc.reason not in ('entry_deadline_elapsed', 'new_entries_paused'):
            self._halt(f'{op["symbol"]}: submission refused: {exc.reason}')

    def _coi(self, bar_ms, symbol, purpose, seq=0):
        value=client_order_index(bar_ms,symbol,purpose,seq)
        limit=getattr(self.broker,'max_client_order_index',None)
        return value % limit if limit else value

    @staticmethod
    def _order_ref(ref):
        from .broker import OrderRef
        return OrderRef(**{k: v for k, v in ref.items()
                           if k in OrderRef.__dataclass_fields__})

    def _sync_fills(self, state) -> list[str]:
        """Apply ledger-confirmed changes; a smaller position is not TP proof."""
        notes = []
        pending = self.observe_publication(state.ledger_report)
        historical_issues = state.ledger_report.get('historical_issues', [])
        if historical_issues:
            markets = sorted({item['market_id'] for item in historical_issues})
            self._halt(f'unsupported history requires manual reconciliation; affected markets: {markets}')
        elif state.ledger_report and not state.execution_changes_verified and not pending:
            self._halt('account execution history does not reconcile; new entries halted')
        reverse = {v: k for k, v in self.config_symbol_map.items()}
        live = {}
        for lighter_symbol, pos in state.positions.items():
            strat = reverse.get(lighter_symbol)
            if strat:
                live[strat] = pos
        for symbol in list(self.positions):
            p = self.positions[symbol]
            verified = self._symbol_verified(state, symbol)
            actual = live.get(symbol)
            if actual is None:
                if not verified:
                    self._halt(f'{symbol}: unexplained disappearance; execution evidence required')
                    continue
                notes.append(f'{symbol}: closure confirmed by owned execution evidence')
                self._cancel_protective(symbol, self.markets[symbol])
                self.positions.pop(symbol)
                self.orders.pop(symbol, None)
                self.residual_exits.pop(symbol, None)
                continue
            if actual['side'] != p['side']:
                self._halt(f'{symbol}: position side changed unexpectedly')
                continue
            if actual['qty'] < p['qty'] - 1e-12:
                if not verified:
                    self._halt(f'{symbol}: unexplained reduction; execution evidence required')
                    continue
                p['qty'] = float(actual['qty'])
                notes.append(f'{symbol}: residual quantity confirmed by owned execution evidence: {p["qty"]}')
            if (verified and actual.get('partial_taken') is True
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
        initial_state = self.broker.account_state()
        out = BarOutcome(now_ms, initial_state.equity)
        state_may_change = bool(self.orphan_orders or self._needs_recovery or self.unresolved_operations)
        for symbol, refs in list(self.orphan_orders.items()):
            if symbol in self._history_quarantined_symbols(initial_state):
                out.reasons.append(f'{symbol}: orphan cancellation deferred; history quarantined')
                continue
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
        if self._needs_recovery or self.unresolved_operations:
            self._park_operation()
            # Each symbol's intent is checkpointed before lookup; a crash at
            # any point retains either the parked or active representation.
            for key in list(self.unresolved_operations):
                self.pending_operation = self.unresolved_operations.pop(key)
                self._save()
                try:
                    self._recover_operation(out, fresh_symbols, state=initial_state)
                finally:
                    self._park_operation()
            if self.pending_operation is None:
                if self.inflight_bar_ms is not None:
                    self.last_bar_ms = max(self.last_bar_ms, self.inflight_bar_ms)
                    self.journal.append('interrupted_bar_skipped', bar_ms=self.inflight_bar_ms,
                                        reason='completed actions retained; remaining entries skipped')
                    self.inflight_bar_ms = None
                    self.pending.clear()
                self._needs_recovery = bool(self.unresolved_operations)
                self._save()
        state = self.broker.account_state() if state_may_change else initial_state
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
        if self.pending_operation is None:
            safe_symbols = set()
            for symbol, p in self.positions.items():
                actual = state.positions.get(self.config_symbol_map[symbol])
                if (self._symbol_verified(state, symbol) and actual
                        and actual['side'] == p['side']
                        and math.isclose(float(actual['qty']), float(p['qty']), rel_tol=1e-9, abs_tol=1e-12)
                        and not self._uncertain_market_order(symbol)):
                    safe_symbols.add(symbol)
            # A missing SL after an IOC trigger is an emergency residual, not a
            # successful stop. Try only confirmed reduce-only close operations.
            if state.active_orders_verified:
                for symbol in list(self.positions):
                    if symbol not in safe_symbols:
                        continue
                    active = state.active_orders.get(self.config_symbol_map[symbol], [])
                    sl = (self.orders.get(symbol) or {}).get('SL')
                    found = sl and any(o.get('client_order_index') == sl['client_order_index']
                                       and o.get('purpose') == 'SL' and o.get('armed', True) for o in active)
                    if not found:
                        # A positively unsent protective order can be retried;
                        # a missing outcome after dispatch cannot. Leave its
                        # durable operation for late evidence and close only
                        # the independently reconciled exposure below.
                        unsent = state.ledger_report.get('not_submitted_orders', {})
                        if self._initial_stop_waits_for_dispatch(symbol, sl, unsent):
                            continue
                        self._halt(f'{symbol}: protective stop missing; entries halted')
                        if sl:
                            self.protection_generation[symbol] = self.protection_generation.get(symbol, 0) + 1
                            self.orphan_orders.setdefault(symbol, {})[f'SL:{sl["client_order_index"]}'] = dict(sl)
                        self.orders.setdefault(symbol, {}).pop('SL', None)
                        self.residual_exits.setdefault(symbol, {
                            'reason': 'PROTECTION_LOST', 'bar_ms': now_ms, 'attempt': 0,
                            'cycle_ms': self.positions[symbol]['entry_ms']})
            for symbol, request in list(self.residual_exits.items()):
                if self.pending_operation is not None:
                    break
                if (symbol in self.positions and request.get('cycle_ms', self.positions[symbol]['entry_ms'])
                        != self.positions[symbol]['entry_ms']):
                    self._halt(f'{symbol}: residual exit belongs to a different position cycle')
                    continue
                if symbol in safe_symbols and symbol in fresh_symbols:
                    self._exit(symbol, request['reason'], marks, request['bar_ms'], out)
                elif symbol in self.positions:
                    out.reasons.append(f'{symbol}: residual close awaits a fresh mark')
            if self.pending_operation is None:
                self._sync_protective_orders(out, now_ms, fresh_symbols=fresh_symbols,
                                             allowed_symbols=safe_symbols, state=state)
        out.halted = (self.halted or self.pending_operation is not None
                      or self.publication_pending
                      or bool(self.unresolved_operations) or bool(self.residual_exits) or bool(self.orphan_orders))
        out.reasons += self.halt_reasons
        self._save()
        return out

    def _recover_operation(self, out: BarOutcome, fresh_symbols: set[str], state=None) -> None:
        op = self.pending_operation
        if op is None:
            return
        if op['symbol'] in self._history_quarantined_symbols(state):
            out.reasons.append(f'{op["symbol"]}: operation recovery deferred; history quarantined')
            return
        try:
            if op['kind'] == 'stop_renewal':
                recover_renewal(self, op, out, now_ms=out.bar_ms, fresh_symbols=fresh_symbols)
            elif op['kind'] in ('entry', 'exit') and op.get('phase') != 'protect':
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
                if (self._symbol_verified(state, op['symbol']) and
                        self.config_symbol_map[op['symbol']] not in state.positions):
                    # An already-accepted stop may have closed the paper position
                    # before restart could finish registering the other leg.
                    self._cancel_protective(op['symbol'], self.markets[op['symbol']])
                    # A late accepted SL/TP may never have reached orders[].
                    # Cancel that exact owned ID only after verified flatness;
                    # absence is still not proof an uncertain send was rejected.
                    for ref in state.active_orders.get(self.config_symbol_map[op['symbol']], []):
                        if ref.get('purpose') in ('SL', 'TP'):
                            self.broker.cancel(self.markets[op['symbol']], self._order_ref(ref))
                    self.positions.pop(op['symbol'], None)
                    self.orders.pop(op['symbol'], None)
                    self.residual_exits.pop(op['symbol'], None)
                    if any(v.get('symbol') == op['symbol'] and v.get('role') in ('SL', 'TP')
                           for v in state.ledger_report.get('unresolved_orders', {}).values()):
                        self._save()
                        return
                    self.pending_operation = None
                    self._save()
                    return
                if op['symbol'] not in fresh_symbols:
                    out.reasons.append(f'{op["symbol"]}: protection recovery awaits a fresh mark')
                    return
                if not self._symbol_verified(state, op['symbol']):
                    return
                if any(v.get('symbol') == op['symbol'] and v.get('role') == 'SL'
                       for v in state.ledger_report.get('unresolved_orders', {}).values()):
                    return  # Service will close verified exposure, never duplicate this SL.
                self._place_protective(op['symbol'], op['bar_ms'])
                self.pending_operation = None
                self._save()
            elif op['kind'] == 'amend':
                # A simulator exposes an exact active-order ledger. For other
                # adapters no acknowledgement is fabricated from position size.
                state = self.broker.account_state()
                if (self._symbol_verified(state, op['symbol']) and
                        self.config_symbol_map[op['symbol']] not in state.positions):
                    active = state.active_orders.get(self.config_symbol_map[op['symbol']], [])
                    late = next((row for row in active if row.get('client_order_index') == op['coi']), None)
                    if late is not None:
                        self.broker.cancel(self.markets[op['symbol']], self._order_ref(late))
                    elif str(op['coi']) in state.ledger_report.get('unresolved_orders', {}):
                        return  # A missing amended stop may still appear later.
                    self.pending_operation = None
                    self._save()  # subsequent fill sync cancels remaining protection
                    return
                active = state.active_orders.get(self.config_symbol_map[op['symbol']], [])
                match = next((o for o in active if o.get('client_order_index') == op['coi']
                              and o.get('trigger') == op['trigger']), None)
                check = getattr(self.broker, 'stop_amendment_confirmed', None)
                if check and check(op['coi'], op['qty'], op['trigger']):
                    match = next((o for o in active if o.get('client_order_index') == op['coi']), None)
                if not state.active_orders_verified or match is None:
                    if not self._symbol_verified(state, op['symbol']) or op['symbol'] not in fresh_symbols:
                        return
                    sl = self.orders.get(op['symbol'], {}).get('SL')
                    if not sl:
                        return
                    self.broker.modify_stop(self.markets[op['symbol']], self._order_ref(sl),
                                            op['qty'], op['trigger'])
                self.orders[op['symbol']]['SL'].update(trigger=op['trigger'], qty=op['qty'])
                self.pending_operation = None
                self._save()
        except NotSubmitted as exc:
            self._not_submitted(op, exc, out)
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
        self.deferred_entries = None  # no carry-over into another signal bar
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

        quarantine_active = bool(state.ledger_report.get('historical_issues'))
        quarantined_symbols = self._history_quarantined_symbols(state)
        held_at = getattr(self.broker, 'held_symbols_at', None)
        # Quarantine already blocks every entry. Do not require an unsupported
        # holding path merely to manage independently verified open positions.
        held_at_close = (held_at(entry_ms) if held_at is not None and not quarantine_active
                         else set(self.positions))
        out.reasons += self._sync_fills(state)

        mismatch = guards.reconcile(self.positions, state, self.config_symbol_map)
        if mismatch.halt:
            out.halted = True
            out.reasons += mismatch.reasons
            self.journal.append('reconcile_halt', bar_ms=bar_ms, reasons=mismatch.reasons)
            for reason in mismatch.reasons:
                self._halt(reason)
            if not quarantine_active:
                return out

        bar_symbols = set(self.positions)
        if quarantine_active:
            # Unknown reductions stay frozen, including their bar age and stop
            # path. Other markets need exact fresh owned exposure; a global
            # HALT alone must not suppress their existing close rules.
            bar_symbols = {symbol for symbol, position in self.positions.items()
                           if symbol not in quarantined_symbols and self._symbol_verified(state, symbol)
                           and not self._uncertain_market_order(symbol)
                           and (actual := state.positions.get(self.config_symbol_map[symbol])) is not None
                           and actual['side'] == position['side']
                           and math.isclose(float(actual['qty']), float(position['qty']),
                                            rel_tol=1e-9, abs_tol=1e-12)}

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
        for symbol in bar_symbols:
            self.positions[symbol]['bars'] += 1

        for symbol, p in self.positions.items():
            if symbol in bar_symbols:
                update_on_bar_close(p, rows[symbol], self.cfg)

        # Signals come from the bar that just closed and are acted on at the
        # open now forming — the backtest's close(i-1) -> open(i) ordering.
        # Scanning BEFORE the exits matters: a symbol still held at that close
        # is skipped, exactly as the backtest skips it.
        # pending is derived fresh every bar, never carried across a restart.
        signal_universe = [symbol for symbol in self.universe if symbol not in quarantined_symbols]
        self.pending, filtered = scan_signals(bar_ms, rows, signal_universe, self.longs,
                                              {**{s: {} for s in held_at_close}, **self.positions},
                                              self.setups, self.cfg)

        # The new bar's open is unobservable at the moment we act, so the live
        # reference is the current Lighter mark — which is also where the order
        # actually fills and how the notional cap should value the open book.
        # Exits and entries must both use it: an exit filled at the PREVIOUS
        # bar's close books the wrong realised PnL and mis-sizes every entry
        # that follows it in the same bar.
        opens = {s: float(marks.get(s, rows[s]['close'])) for s in rows}

        # Commit all close decisions and the one-time bar state before any
        # subsequent account read/order can fail. Remaining entries may be
        # skipped after a crash, but aging/setups must never be applied twice.
        for symbol, position in self.positions.items():
            if symbol not in bar_symbols:
                continue
            reason = position.pop('pending_exit', None)
            if reason:
                self.residual_exits.setdefault(symbol, {
                    'reason': reason, 'bar_ms': entry_ms, 'attempt': 0,
                    'cycle_ms': position['entry_ms']})
        self.last_bar_ms = bar_ms
        self._save()

        # 1. exits decided at this close, executed now at the new bar's open
        for symbol in list(self.positions):
            if symbol not in bar_symbols:
                continue
            request = self.residual_exits.get(symbol)
            if request:
                self._exit(symbol, request['reason'], opens, request['bar_ms'], out)

        # 2. entries in canonical order, one at a time
        entry_block = (out.halted or margin.block_entries or not allow_entries
                       or bool(self.residual_exits) or self.publication_pending)
        for symbol in self.universe:
            req = self.pending.pop(symbol, None)
            if req is None or symbol in self.positions:
                continue
            if entry_block or self.halted or self.pending_operation is not None or self.unresolved_operations:
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
            entry_state = self.broker.account_state()
            if self.observe_publication(entry_state.ledger_report):
                entry_block = True
                out.skipped.append((symbol, 'owned execution publication pending'))
                continue
            bounded = hasattr(self.broker, 'entry_deadline_ms')
            if bounded:
                self.broker.entry_deadline_ms = entry_ms + self.config.bar_grace_seconds*1000
            try:
                result = self._enter(symbol, req, opens, rows, entry_ms, out,
                                     entry_state.equity, entry_state=entry_state)
            finally:
                if bounded:
                    self.broker.entry_deadline_ms = None
            if isinstance(result, EntryDeferred):
                self._defer_entries(bar_ms, rows, {symbol:req, **self.pending}, result)
                self.pending.clear()
                break  # preserve canonical order for the remaining candidates

        # 3. bookkeeping
        self.last_bar_ms = bar_ms

        # 4. trailing stops move once per bar, monotonically
        self._sync_protective_orders(out, entry_ms,
                                     allowed_symbols=bar_symbols if quarantine_active else None, state=state)

        if self.pending_operation is None:
            self.inflight_bar_ms = None
        out.halted = (self.halted or self.pending_operation is not None
                      or bool(self.residual_exits) or self.publication_pending)
        self._save()
        self.journal.append('bar', bar_ms=bar_ms, equity=state.equity,
                            exits=out.exits, entries=out.entries, skipped=out.skipped,
                            blocked=out.blocked, filtered=filtered, reasons=out.reasons,
                            positions=len(self.positions))
        return out

    # -- actions ------------------------------------------------------------ #

    def _exit(self, symbol: str, reason: str, opens: dict, bar_ms: int, out: BarOutcome) -> None:
        p = self.positions[symbol]
        request = self.residual_exits.get(symbol, {})
        if request.get('cycle_ms', p['entry_ms']) != p['entry_ms']:
            self._halt(f'{symbol}: residual exit belongs to a different position cycle')
            return
        market = self.markets[symbol]
        price = float(opens[symbol])
        attempt = self.residual_exits.get(symbol, {}).get('attempt', 0)
        coi = self._coi(bar_ms, symbol, f'EXIT_{reason}', seq=attempt)
        op = {'kind': 'exit', 'symbol': symbol, 'bar_ms': bar_ms, 'reason': reason,
              'coi': coi, 'qty': p['qty'], 'side': p['side'], 'attempt': attempt}
        self.residual_exits.setdefault(symbol, {'reason': reason, 'bar_ms': bar_ms,
            'attempt': attempt, 'cycle_ms': p['entry_ms']})
        self._save()
        if self.pending_operation is not None or self._uncertain_market_order(symbol):
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
        except NotSubmitted as exc:
            self._not_submitted(op, exc, out)
        except Exception as exc:
            self._needs_recovery = True
            self._halt(f'{symbol}: exit outcome unresolved: {exc}')
            self._park_operation()

    def _finish_exit(self, op: dict, fill: Fill, out: BarOutcome) -> None:
        symbol = op['symbol']
        self._validate_fill(fill, op['symbol'], op['side'], op['qty'])
        state = self.broker.account_state()
        actual = state.positions.get(self.config_symbol_map[symbol])
        p = self.positions.get(symbol)
        if p is None:
            if actual is not None or not self._symbol_verified(state, symbol):
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
        if actual is None and fill.qty < op['qty'] - 1e-12 and not self._symbol_verified(state, symbol):
            raise RuntimeError('flat account lacks evidence for the full close')
        if actual is not None:
            remaining = float(actual['qty'])
            if not math.isclose(op['qty'] - fill.qty, remaining, rel_tol=1e-9, abs_tol=1e-12):
                if not self._symbol_verified(state, symbol):
                    raise RuntimeError('close fill and residual position disagree')
            p['qty'] = remaining
            self.residual_exits[symbol] = {
                'reason': op['reason'], 'bar_ms': op['bar_ms'], 'attempt': op['attempt'] + 1,
                'cycle_ms': p['entry_ms']}
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
               bar_ms: int, out: BarOutcome, equity: float, entry_state=None):
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

        validate=getattr(self.broker,'validate_entry',None)
        if validate:
            try:
                validate(market,side,plan.qty,plan.px,self.cfg.tp_fraction,plan.px-2*plan.unit)
            except EntryDeferred as exc:
                out.skipped.append((symbol,str(exc)))
                return exc
            except ValueError as exc:
                out.skipped.append((symbol,str(exc)))
                return

        deadline = getattr(self.broker, 'entry_deadline_ms', None)
        guard = getattr(self.broker, 'entry_guard', None)
        if ((deadline is not None and self._entry_now_ms() >= deadline)
                or (guard is not None and not guard())):
            out.skipped.append((symbol, 'entry deadline elapsed or entries paused'))
            return
        offer = getattr(self.broker, 'offer_entry_state', None)
        if offer and entry_state is not None:
            offer(entry_state)

        coi = self._coi(bar_ms, symbol, 'ENTRY')
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
        except NotSubmitted as exc:
            self._not_submitted(op, exc, out)
        except Exception as exc:
            self._needs_recovery = True
            self._halt(f'{symbol}: entry/protection outcome unresolved: {exc}')
            self._park_operation()

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
            op['equity'], (fill.fee_usdc or 0.) if hasattr(self.broker,'ledger') else
            fill.qty * fill.price * self.costs.fee, op['breakout'])
        if hasattr(self.broker,'ledger'):
            self.positions[symbol]['entry_fee_verified']=fill.fee_usdc is not None
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
        sl_coi = self._coi(bar_ms, symbol, 'SL', seq=self.protection_generation.get(symbol, 0))
        if 'SL' not in refs:
            refs['SL'] = self.broker.place_stop(market, symbol, p['side'], p['qty'],
                                                p['stop'], sl_coi).__dict__
            refs['SL'].update(trigger=p['stop'], qty=p['qty'], position_side=p['side'])
            self._save()  # A failed TP placement must not lose an accepted SL.
        if (p['side'] == 'SHORT' and self.cfg.tp_fraction > 0
                and not p['partial_taken'] and 'TP' not in refs):
            tp_coi = self._coi(bar_ms, symbol, 'TP')
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
                                fresh_symbols: set[str] | None = None,
                                allowed_symbols: set[str] | None = None, state=None) -> None:
        """Amend resting stops to this bar's trailed level.

        Amend ordinary changes; renew expiry only after replacement is verified.
        Refuse any move that would loosen
        the stop: live_core already guarantees monotonicity, so a loosening
        request here means state is wrong and the safe response is to skip it.
        """
        for symbol, p in self.positions.items():
            if self.pending_operation is not None:
                return
            if symbol in self._history_quarantined_symbols(state):
                continue
            if state is not None and not self._symbol_verified(state, symbol):
                continue
            if allowed_symbols is not None and symbol not in allowed_symbols:
                continue
            if self._uncertain_market_order(symbol):
                continue
            if any(op['symbol'] == symbol and op['kind'] == 'stop_renewal'
                   for op in self.unresolved_operations.values()):
                continue  # Only the durable renewal may change either stop ID.
            refs = self.orders.get(symbol) or {}
            sl = refs.get('SL')
            if sl and hasattr(self.broker, 'engine'):
                stored = self.broker.engine.store.load(sl['client_order_index'])
                if stored and stored[1] and stored[1].get('order_expiry_ms') is not None:
                    sl['order_expiry_ms'] = stored[1]['order_expiry_ms']
                if not any(op['symbol'] == symbol for op in self.unresolved_operations.values()):
                    try:
                        if start_renewal(self, symbol, self._entry_now_ms(), out, state=state):
                            self._park_operation()  # A healthy but incomplete renewal can wait for fresh evidence.
                            continue
                    except NotSubmitted as exc:
                        if self.pending_operation is not None:
                            self._not_submitted(self.pending_operation, exc, out)
                        else:
                            raise
                        continue
                    except Exception as exc:
                        self._halt(f'{symbol}: stop renewal requires reconciliation: {exc}')
                        self._park_operation()
                        continue
            if not sl:
                if any(op['symbol'] == symbol for op in self.unresolved_operations.values()):
                    continue  # Recover the original protection ID; no replacement guess.
                if fresh_symbols is not None and symbol not in fresh_symbols:
                    out.reasons.append(f'{symbol}: missing stop awaits fresh mark; entries blocked')
                    continue
                try:
                    self._place_protective(symbol, bar_ms)
                except NotSubmitted as exc:
                    self._not_submitted(self.pending_operation, exc, out)
                except Exception as exc:
                    self._needs_recovery = True
                    self._halt(f'{symbol}: protective placement unresolved: {exc}')
                    self._park_operation()
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
                if any(op['symbol'] == symbol and op['kind'] == 'amend'
                       for op in self.unresolved_operations.values()):
                    continue
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
            except NotSubmitted as exc:
                self._not_submitted(self.pending_operation, exc, out)
            except Exception as exc:
                self._needs_recovery = True
                self._halt(f'{symbol}: stop amendment outcome unresolved: {exc}')
                out.reasons.append(f'{symbol}: stop amend failed ({exc}); protection requires verification')
                self.journal.append('stop_amend_failed', bar_ms=bar_ms, symbol=symbol,
                                    error=str(exc))
                self._park_operation()

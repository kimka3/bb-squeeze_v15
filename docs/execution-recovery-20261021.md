# Execution evidence and recovery correction

The implementation separates a positively proven refusal before dispatch from an unknown result after dispatch. An absent API row, elapsed timeout, or exhausted read budget never proves that an order was not submitted. Old intent rows without dispatch metadata remain unknown. New metadata is additive; existing intent IDs and account ledger events are retained.

## Corrections

- F-0/F-2: durable dispatch evidence, bounded reads, independent unresolved operations, and per-market ownership reconciliation. A missing result blocks new entries while independently reconciled positions can still receive protection and reduce-only risk management. Unknown submissions are never resent. Only explicitly retryable, positively unsent operations can be retried using their original identity and fresh exposure checks. Expired or interrupted entry candidates are discarded.
- F-3: one additional full read for a narrowly identified order/trade/account publication mismatch before declaring a persistent mismatch. Invalid identity, schema, or foreign activity is still blocked.
- F-5/F-8: save close decisions and one-time bar state before later API operations; preserve residual exits across failures. A residual exit must belong to the current position cycle.
- F-6: transient reads during deferred entry processing use the existing exchange recovery controller. Request-budget waits remain distinct from exchange outages. Authentication, TLS, schema, ambiguous writes and storage failures remain fail-closed.
- F-7: use observed stop expiry, including backfilling legacy snapshots. Within an operational one-day lead, create and verify a new reduce-only stop before retiring the old stop. Keep both identities durable across crashes; do not loosen the stop. An already expired stop is unsafe even if a lagging API row says pending. This margin is an implementation choice, not a trading parameter or availability guarantee.
- N-1: use owned executions to preserve whether the symbol was held at the signal close, so a close immediately after that boundary cannot create a same-bar reentry that the backtest would skip.
- F-4 partial: funding attribution now scans ordered trades once per market instead of rescanning all trades per funding event. Full history API collection is deliberately retained: a shorter cursor/overlap alone cannot establish that arbitrarily late old trades or funding were captured. API work still grows with history; the incremental collector needs a separately verifiable completeness design.

## Preserved boundaries

Strategy indicators, universe, entry/exit formulas, risk ratios, capital and campaign identity are unchanged. The exchange recovery requirements remain three different successful full poll/service cycles, at least 120 stable seconds, and no gap over 90 seconds. Request limits and protective headroom remain 55 requests/60.1 seconds and 24/20 requests of entry headroom. Manual pause and safety HALT are never cleared by these changes.

Partial entries are canceled and reconciled before the confirmed quantity is protected. An ambiguous cancel is not retried blindly and is not treated as a zero fill. If neither the exchange nor its API provides enough evidence to resolve an order, manual reconciliation can still be necessary. The tests do not establish that future exchange outages or loss are impossible.

Tests use in-memory venues or offline fixtures. No real order, forced signal or deliberate exchange outage is used for validation. Deployment must use a new immutable stage, verified flat/no-order account state, owned maintenance pause, preserved campaign approval/state, full startup recovery, and post-deployment evidence. Test success alone is not deployment or real-incident recovery evidence.

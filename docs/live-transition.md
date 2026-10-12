# Live execution transition: not yet enabled

The completed paper campaign is preserved. Its completion does not establish
authenticated execution correctness. The CLI and broker write guards remain in
place; this branch cannot place an order. No new service has been started.

## Implemented evidence layer

`execution_evidence.py` validates an exchange order against a pre-recorded
intent: account, market, client ID, side, reduce-only flag, order type, time in
force, quantity and prices. It distinguishes an acknowledgement, a pending
order, a terminal partial fill and a confirmed zero-fill cancellation. Filled
base/quote totals determine the execution average, never requested quantity or
reference price. Fees and position reconciliation are explicitly unverified.

A full-position stop must be observed open, ready and reduce-only, with matching
terms and sufficient remaining size. Unknown response fields/statuses fail
closed. Intent reservation commits to SQLite before a future send operation;
an unresolved ID cannot be reserved again after restart or by another connection.
Regressed or conflicting observations are refused. This is an evidence library,
not yet an authenticated transport or a complete execution ledger.

Source contract inspected on 2026-10-12:
- [Official Order model](https://github.com/elliottech/lighter-python/blob/main/lighter/models/order.py)
- [Official authenticated order query](https://github.com/elliottech/lighter-python/blob/main/docs/OrderApi.md#account_orders)
- [Official Account Index explanation](https://apidocs.lighter.xyz/docs/account-index)

The documented client-ID query has a bounded history window. Missing results
must remain unknown; absence does not authorize retransmission. Matching order
totals alone cannot prove that a position change was a strategy TP, or reconcile
fees, funding, external/manual orders or liquidation.

Validation: 212 tests and 15 subtests passed locally, including 28 new execution
evidence cases. These use synthetic fixtures derived from the official schema;
they are not authenticated exchange tests and do not certify live readiness.

## Remaining implementation and operational gates

1. Bind a dedicated account and API key to the intended network; verify the
   real authentication response without logging tokens or private keys. Testnet
   and mainnet identities and state directories must remain separate.
2. Integrate a pinned SDK/authenticated transport, paginated trade/order history,
   durable send/outcome recovery, cumulative fill reconciliation and position
   attribution. Pin against actual testnet response fixtures before enabling.
3. Integrate the strategy service loop, verified stop placement/amendment,
   reduce-only residual liquidation and alerting. A partial IOC stop or an
   uncertain send must not permit another entry or a blind resend.
4. Reconcile account equity, fees, funding and margin; enforce the authorized
   capital allocation and existing strategy risk limits on actual exposure.
5. Exercise entry, partial TP, stop, cancellation, lost response and restart
   using a testnet account. Mainnet activation remains a distinct bounded
   deployment after these checks and account identification are complete.

Keep the existing per-trade risk, aggregate initial-risk cap and BTC-bull short
multiplier. A per-trade planned risk percentage is not an account-wide lifetime
loss limit and is not a guarantee of the final loss after execution costs.
Do not invent a new account-wide loss rule or silently change the universe.

Never put private keys, wallet recovery phrases, signed transactions, tokens,
private account snapshots or deployment credentials in GitHub. Public account
index and API key index are identifiers, not secret key material.

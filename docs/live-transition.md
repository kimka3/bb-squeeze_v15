# Live execution transition: not yet enabled

The completed paper campaign is preserved. Its completion does not establish
authenticated execution correctness. The strategy CLI and old broker write
guards remain in place. A separate user-operated `live.live_check` command can
place a bounded real order ONLY with `--execute` and interactive confirmation.
The default invocation is a read-only preview. No service is started.

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
Regressed or conflicting observations are refused. `authenticated.py` adds the
pinned SDK 1.1.6 transport, fixed exchange origins, credential file checks,
short-lived authorization and sanitized errors. `execution.py` adds durable
submission recovery, stop confirmation, non-loosening amendment and cancellation.
Uncertain writes are queried by their original ID, never blindly resent.

Source contract inspected on 2026-10-12:
- [Official Order model](https://github.com/elliottech/lighter-python/blob/main/lighter/models/order.py)
- [Official authenticated order query](https://github.com/elliottech/lighter-python/blob/main/docs/OrderApi.md#account_orders)
- [Official Account Index explanation](https://apidocs.lighter.xyz/docs/account-index)

The documented client-ID query has a bounded history window. Missing results
must remain unknown; absence does not authorize retransmission. Matching order
totals alone cannot prove that a position change was a strategy TP, or reconcile
fees, funding, external/manual orders or liquidation.

Validation: 227 tests and 15 subtests passed locally. Price movement within the
reviewed limit preserves the original order price and quantity; movement above
that limit is rejected before session creation or submission. New lifecycle cases cover
lost submission replies, restart, partial close residuals, stop rejection with
cleanup, repeated cleanup, unknown IDs, stop amendment and duplicate prevention.
These are synthetic exchange fixtures, not real execution results.

## User-operated minimum execution check

Configure the network, account index, dedicated API key index, key-file path,
expected public wallet address and a new `check_directory` in a private JSON
file. Credentials must be stored as a 0600 regular file owned by the Linux user
running the command. Install `requirements-execution.txt` in an isolated venv.

```bash
# Read-only preview: no order submission.
PYTHONPATH=src python -m live.live_check --config /private/check.json
# The user runs this and types EXECUTE <account-index> in their own terminal.
PYTHONPATH=src python -m live.live_check --config /private/check.json --execute
# Resume cleanup of the SAME session; never re-enter.
PYTHONPATH=src python -m live.live_check --config /private/check.json --recover
```

The preview expires after five minutes. The check uses an ETH long sized from
the current exchange minimum and decimal precision, with a small minimum-quote
buffer and a hard 15 USDC entry-notional ceiling. It requires a flat account,
no active orders and enough available collateral to cover the unlevered entry.
The check's 1% stop distance and 0.5% price bound are execution-test settings,
not changes to the trading strategy and not guaranteed loss limits. A price
move beyond the preview bound requires a new preview rather than a wider order.

The user-operated program confirms the terminal entry amount/price and account
position, registers a full-size reduce-only stop, verifies its ready state,
then immediately performs reduce-only cleanup. It reconciles cumulative entry,
stop and close fills to actual position quantity before another close. Stop
cancellation follows verified flat state. A completed check requires no active
orders and no position. Partial close retries are bounded. Unknown outcomes or
unconfirmed residuals stop with an explicit attention state; the same session
can be recovered, not restarted as a new entry. Exchange outages can still
prevent liquidation. The check is NOT an unattended production service.

The assistant may deploy code and run preview/read-only checks. Real buy/sell
execution is performed by the user, not by the assistant. Do not confuse local
tests, authenticated reads, stop registration, or a one-shot execution check
with proof of production strategy readiness or profitability.

## Remaining implementation and operational gates

1. Complete the human-operated bounded execution check; preserve authenticated
   response fixtures and cleanup proof. Testnet was declined by the user in
   favor of this mainnet check. Mainnet and testnet state remain separate.
2. Integrate paginated per-trade/fee/funding history, position/TP attribution,
   and the strategy service loop with the new execution engine. Existing order
   totals alone do not prove partial-TP attribution or a complete cost ledger.
3. Verify production residual protection, alerting and restart recovery against
   the actual exchange responses. Unknown outcomes must not permit new entries.
4. Reconcile account equity, fees, funding and margin; enforce the authorized
   capital allocation and existing strategy risk limits on actual exposure.
5. Keep strategy automation disabled until these remaining gates are satisfied.
   The minimum mainnet check does not start the full-capital strategy or force
   real partial fills, stop triggering or network outages to claim coverage.

Keep the existing per-trade risk, aggregate initial-risk cap and BTC-bull short
multiplier. A per-trade planned risk percentage is not an account-wide lifetime
loss limit and is not a guarantee of the final loss after execution costs.
Do not invent a new account-wide loss rule or silently change the universe.

Never put private keys, wallet recovery phrases, signed transactions, tokens,
private account snapshots or deployment credentials in GitHub. Public account
index and API key index are identifiers, not secret key material.

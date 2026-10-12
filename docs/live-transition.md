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

A full-position stop must be observed reduce-only with matching terms and
sufficient remaining size. Standalone untriggered stops use `pending/mark-price`
with zero trigger time and no parent dependency; `open/ready` is also recognized.
Unknown response fields/statuses fail
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
- [Official conditional order construction](https://github.com/elliottech/lighter-prover/blob/main/circuit/src/transactions/l2_create_order.rs): conditional orders are inserted as pending mark-price-triggered orders.

The documented client-ID query has a bounded history window. Missing results
must remain unknown; absence does not authorize retransmission. Matching order
totals alone cannot prove that a position change was a strategy TP, or reconcile
fees, funding, external/manual orders or liquidation.

Validation: 240 tests and 15 subtests passed locally. Price movement within the
reviewed limit preserves the original order price and quantity; movement above
that limit is rejected before session creation or submission. New lifecycle cases cover
lost submission replies, restart, partial close residuals, stop rejection with
cleanup, repeated cleanup, unknown IDs, stop amendment and duplicate prevention.
These are synthetic exchange fixtures, not real execution results.

A user-operated mainnet check on 2026-10-12 filled the entry and close, but the
old `open/ready`-only stop check timed out on `pending/mark-price`. Cleanup
confirmed a flat account and no active orders. That recorded check remains
failed; the fix does not rewrite its result or prove an actual stop-trigger fill.
Regression cases now include the observed pending state, parent dependencies,
missing trigger metadata, insufficient size, and pending-stop amendment/cancel.

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

## Authenticated strategy integration (2026-10-12)

`strategy_broker.py` now connects the existing `Trader` and shared `live_core`
to `ExecutionEngine`. `build_authenticated_strategy` constructs this path with
writes disabled by default. The runner's fast poll and report persistence accept
this adapter; the production CLI activation gate remains closed. No service is
installed or started by constructing the adapter. Account equity comes from
the exchange, not the old paper capital or the withdrawn 10,000 USDC plan.

The existing BCH-excluded universe, 2% per-trade risk, 4% aggregate initial risk,
BTC-bull short multiplier and entry/exit decision functions remain unchanged.
If the configured short partial TP cannot satisfy lot/minimum constraints, the
entry is skipped; sizing is never increased to meet an exchange minimum.
Strategy client IDs are bounded to Lighter's 48-bit input contract.

The adapter persists order purpose and entry-cycle ownership before submission.
Order totals and paginated account trades must agree on quantity, quote amount,
market, side and order identity. A full fill of the owned TP leg arms breakeven;
a manual reduction or an incomplete TP leg does not. Amendments retain the
existing stop until the new terms are confirmed. Unknown history, positions or
foreign active orders block reconciliation/new entries. A rejected passive TP
is not silently replaced with a different execution policy.

`account_ledger.py` stores immutable trade/funding events, rejects cursor loops,
deduplicates repeated pages and keeps historical records across restart. Funding
uses actual signed account cash changes and attributes them to an unambiguous
owned holding interval. Ambiguous settlement ordering remains unattributed.
Trade fee fields missing from the API, and nonzero fee fields with unverified
units, remain unknown. A matching account balance delta does not independently
certify each fee. Market-wide funding rates are never substituted for settlement.

`python -m live.strategy_audit --config PRIVATE_CONFIG --sessions SESSION_JSON ...
--output NEW_AUDIT_DIRECTORY` is read-only with respect to the exchange. It can
reconcile the user's saved bounded checks without constructing a writer or
altering those sessions. Private account histories/reports stay outside Git.

Additional primary schema references:
- [Account trade fields](https://github.com/elliottech/lighter-python/blob/main/docs/Trade.md)
- [Trade pagination](https://github.com/elliottech/lighter-python/blob/main/docs/OrderApi.md#trades)
- [Position funding fields](https://github.com/elliottech/lighter-python/blob/main/docs/PositionFunding.md)
- [Account funding query](https://github.com/elliottech/lighter-python/blob/main/docs/AccountApi.md#position_funding)

Synthetic integration tests cover full strategy entry, partial TP followed by
complete TP, breakeven amendment, time exit, lost replies, rejected TP, restart,
unknown reductions, account-history pagination and cash-funding attribution.
They do not certify mainnet partial-TP triggering or outage behavior. The current
full-history reconciliation is conservative and can require many reads; sustained
request-budget and publication-lag handling must be validated before activation.
The complete suite plus the added long-side regression totals 254 passing tests
(and 15 unittest subtests on Windows). Every account writer must use the same
account lock path; the factory refuses write-enabled construction without it.

## Remaining operational gates

1. The second human-operated bounded check passed with flat cleanup. Preserve
   both the first failed check and the successful second check. This does not
   certify triggered stop fills or partial TP on mainnet.
2. Resolve missing fee evidence and any nonzero fee-unit/discount interpretation;
   validate API publication delays and request limits over the intended runtime.
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

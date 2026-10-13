# Authenticated operation validation (2026-10-12)

This revision remains activation-gated. It does not change entry/exit rules,
universe, risk fractions, or the completed paper campaign. No real transactions
are submitted by the audit commands.

## Fee evidence

`live.strategy_audit` records the current authenticated `accountLimits` fee
ticks and account tier separately from trade evidence. A current zero rate does
not retrospectively fill omitted trade fee fields. `user_tier_last_update=0`
has no documented historical interpretation here. A flat-account equity/cash
residual of zero is supporting evidence, not proof of every fee: offsetting cash
transfers or rebates must also be excluded before certifying costs. Missing and
nonzero fee units remain unknown. A zero known subset is not the total fee.

Official references checked on this date:

- https://apidocs.lighter.xyz/docs/account-types : Standard normal maker/taker
  fees are zero; TWAP has a separate charge. The bot uses no TWAP.
- https://apidocs.lighter.xyz/docs/rate-limits : Standard has 60 REST requests
  per rolling minute, shared at L1 scope; HTTP 405/429 can indicate limiting.
- https://raw.githubusercontent.com/elliottech/lighter-python/main/docs/AccountLimits.md
  documents current fee ticks and tier fields, not historical fee certification.

## REST load and recovery

Previously a no-action poll/service cycle performed three full account-history
reconciliations (13 reads before nonterminal-order lookups). Now it performs one
(5 reads with single-page histories). Active orders supply current evidence for
resting orders; only missing owned nonterminal orders require individual lookup.
Histories still scan the complete campaign, deliberately retaining detection of
late-published records. Pagination and state changes add requests.

The existing default poll interval is 60 seconds; it has not been accelerated.
The authenticated factory refuses intervals shorter than 15 seconds. This is a
configuration guard, not a measured latency guarantee. History growth, multiple
fills, and failed queries can still exceed a polling budget.

Each transport keeps a durable 55-request/60.1-second budget beside its key file,
scoped by network and a hash of the L1 address. Every transport for that wallet
must use the same credential directory. Read and explicit write attempts count;
SDK bootstrap and other applications are outside this counter. The 5-request
headroom does not guarantee exchange acceptance. Before entry intent creation,
the adapter requires capacity for the preflight plus entry/protection calls.
Slow or failed acknowledgements can still exhaust that headroom.

HTTP 405/429 persists a cooldown of at least 60 seconds (or a longer numeric
Retry-After). Requests during cooldown fail locally, without repeated network
calls. No mutation is blindly retried. Query failures retain native protective
orders and durable uncertain intents, but they can delay protection placement
after a new fill or amendments and residual closes. This residual exposure risk
cannot be removed by a REST limiter and is not certified by these tests.

Fee-history publication lag continues to block further entries with a persistent
HALT; successful later reads do not silently clear that HALT. Order evidence can
establish the need for immediate protection before fee history arrives.

## Restart defect repaired

If an amendment was accepted but its SQLite commit had not completed, account
reconciliation used to validate the new venue row against the old trigger and
fail before recovery. It now reconciles the durable amendment first, including
market tick rounding and a stop that filled while the process was down. It does
not resend the amendment. An unchanged old stop remains unresolved rather than
being falsely acknowledged.

The VPS read-only cycle also reproduced a startup funding-query failure: equal
second-rounded start/end timestamps return HTTP 400, while one-second and
60-second windows succeed. The collector now pads both server boundaries by a
second and filters the returned timestamps to the exact requested millisecond
scope. This neither invents funding nor includes future/out-of-scope events.

Regression coverage includes entry/SL/TP acceptance before local acknowledgement,
amend acceptance before commit, partial TP before restart, stop fill during
downtime, delayed trade history, insufficient request headroom, durable limiter
and clock rollback, and HTTP 405/429 retry suppression. Four cases use a separate
process with `os._exit` while holding the account lock, then recreate the broker
from SQLite/JSON state and persisted synthetic venue records.

These are fault-injected local-venue tests, not evidence of a live exchange stop
trigger, liquidation recovery, process downtime duration, alert delivery, or
live fill publication latency. The production strategy service is not enabled.

## Read-only VPS checks

`python -m live.operations_audit --config CHECK_CONFIG --output NEW_DIRECTORY`
requires an empty account, constructs a writes-disabled strategy, and measures
three real poll/service cycles separated by 15 seconds. It does not call
`on_bar` or submit/cancel/amend. It records endpoint-only timings (no tokens or
query strings), count per cycle, final exposure, and equity. SDK bootstrap is
excluded from timing totals. Use a fresh output directory every run.

Store account/session identifiers, balances and full API responses in private
deployment evidence, not in this public repository. Run the full suite on the
target Linux environment before accepting a staged revision.

## Entry request headroom and bounded retries (2026-10-14)

A reconciled account snapshot used for entry sizing is now handed once to the
immediately following entry preflight. It expires after one second and is
invalidated by another account read or any mutation. Each subsequent symbol
still obtains fresh equity. A single-page reconciliation saves four duplicate
REST reads per new entry; pagination changes the number of reads saved.
The 55 requests / 60.1 seconds limiter, 24-slot validation and 20-slot submission
checks are unchanged. Capacity is checked, not atomically reserved; external
clients and SDK bootstrap remain outside the measured counter.

Only a pre-intent headroom rejection creates an in-memory deferred candidate.
Candidates and remaining symbols keep canonical order and the original signal
bar. The runner checks the queue without waiting inside risk service. Requests
are not sent until the limiter-provided retry time. Before retry it requires
33 slots (nine planned refresh/sizing reads plus the existing 24-slot guard),
refreshes marks, services protection, reconciles the account and re-evaluates
basis, margin, current equity, sizing, pauses and unresolved operations.
No signal scan, position ageing or completed-bar trailing is replayed.

The fixed cutoff remains signal-bar close plus bar_grace_seconds (60 seconds in
production). The deadline is checked after fresh reads and immediately before
local transaction dispatch, including after nonce lookup and budget acquisition.
Exchange receipt/processing can be later than local dispatch. Reduce-only
protection and exits are not stopped by the entry deadline. A post-intent unknown
outcome remains subject to the existing durable recovery/HALT behavior; it is
never converted into a new candidate or blindly resent.

Expired candidates, candidates blocked by operational controls and unsubmitted
candidates at process restart are discarded. There is no late catch-up trade.
Deferral records now include used/required/limit, cooldown and retry/deadline
timestamps. Runner status exposes the pending candidate list without embedding
market frames. Synthetic tests cover quota metering, multiple candidates,
expiry, pause, stale state, missing ownership, current equity, restart and
protection-before-retry scheduling. They do not prove every future signal can
execute within the unchanged entry window.

# Historical non-dispatch order checks

Account reconciliation does not individually query every positively unsent order
on every service cycle. Only IDs this adapter cannot send again are eligible:

- positively unsent ENTRY IDs (the original ID cannot be retried);
- positively unsent orders with `retryable=false`;
- retired SL IDs with validated durable supersession evidence.

Current retryable SL, TP and EXIT IDs, ambiguous submissions, legacy IDs without
dispatch metadata, amendments and cancellation uncertainty keep their existing
foreground reconciliation. Missing responses never establish non-submission or
create a terminal zero-fill result. All IDs are still compared with every active
order snapshot and collected trade history.

The live loop performs optional historical checks after current position risk
service succeeds. One eligible ID at most is queried every 60.1 seconds, with a
durable pre-request checkpoint and a frozen round-robin pass. New candidates join
the next pass. Restarting or repeated account reconciliation does not reset the
allowance. Failed requests do not advance the cursor. Before the extra GET,
`budget.check(21)` preserves the existing 20-request protection reserve. Entry
windows (including the preceding 10-second read budget), deferred entry candidates,
failed polls and unresolved current reconciliation defer the optional check.
Any intervening order mutation/recovery invalidates the optional scan's account
snapshot. Pending protection/residual work also takes priority.
Budget waiting is not an exchange error;
real read, schema, storage and identity failures retain existing error handling.

A late order or trade that contradicts positive non-dispatch evidence is retained
in `execution.sqlite`, table `order_lookup_audit_checks`. It blocks subsequent
account reconciliation, including after restart; a later absent response cannot
erase it or justify automatic recovery. This feature does not automatically
resolve contradictions, delete old intents or replace order evidence.

`report.json.retired_order_audit` contains candidate/checked/pending counts,
completed passes, cursor, next due time, oldest successful absence observation,
deferral reason and retained conflicts. Foreground and background measurements
separate logical order lookups, HTTP attempt samples/retries and elapsed seconds.
Calls interrupted before their measurements are saved remain unmeasured, rather
than being reported as zero HTTP work. The counters cover this reconciliation
path, not every exchange request or execution-engine recovery request. Existing
API read diagnostics provide endpoint-wide request-attempt latency samples.

An audit pass establishes only the absence observed at each individual check.
It is not an atomic venue snapshot or complete exchange history proof. Total scan
delay grows with candidate count and also depends on budgets, entry windows,
protection work, service frequency and provider availability/retention. Never
claim a fixed detection deadline from the 60.1-second minimum spacing.

The strategy, sizing, entry deadlines, 55 requests per 60.1 seconds, pre-entry
24-request and pre-dispatch 20-request reserves, and actual exchange-recovery
requirements (3 distinct successful cycles, 120 stable seconds, 90-second maximum
cycle gap) are unchanged. Tests use synthetic venues; they do not create live
orders or prove a naturally occurring historical-ID conflict has happened.

# Hourly funding repair (2026-10-11)

The old paper collector queried `[boundary - 3600, boundary]`, then required a
row at exactly `boundary`. The live API excludes `end_timestamp`. For the
verified historical XRP market (`market_id=7`), the old request returned only
timestamp 1791403200; moving the end from 1791406800 to 1791406801 returned the
required 1791406800 row. A missing first event was retried on every poll under
the one-request budget, starving later pending events.

A third gate intentionally refused every directional row because its semantics
had not been verified. Fixing the time window alone would still not book them.

## Primary evidence

- [Official funding API](https://apidocs.lighter.xyz/reference/fundings): hourly
  history endpoint and request fields. End exclusivity is established by the
  live paired requests, not inferred from this incomplete schema.
- [Official funding documentation](https://docs.lighter.xyz/trading/funding):
  hourly payment; positive rates mean longs pay shorts; cash flow is negative
  signed position times index price times funding fraction.
- [Official chart bundle](https://app.lighter.xyz/assets/FundingRateChart-DLz33lxq.js),
  retrieved 2026-10-11, SHA256
  `e204c683901f9680d941886f12ae5a160d588215df69a0b1db89fe6363261369`:
  the chart converts `direction === 'short'` to `-Number(rate)`, otherwise
  `Number(rate)`, and displays these values as percentages. The main official
  UI also formats `current_funding_rate` directly with a percent sign. We accept
  only the two verified directions, not the UI's permissive default.
- Live target row: timestamp 1791406800, rate `0.0010`, direction `short`, value
  `0.00001422`. The rate is a nonnegative hourly percent magnitude. `value` is
  preserved as evidence but is not substituted for the existing public-index
  cash-flow model.

These observations are new evidence, not values supplied by the handoff document.
Third-party descriptions were not used as authority for signing cash flows.

## Changes and boundaries

`funding.py` requests an end one second after the target and still insists on an
exact timestamp. It rejects duplicate rows, unknown direction, nonfinite or
negative magnitude and API errors. Signed rate = magnitude / 100 with positive
for paying longs and negative for paying shorts.

`PaperBroker` retains raw responses and distinct lookup/schema/missing-rate
diagnostics. Events are scheduled by earliest retry time so one missing hour
cannot monopolize the one-request-per-poll budget. Booked events remain
idempotent across saves and restarts. Exposure/index requirements are unchanged.

The model policy is now `payer_direction_percent_v1`. Loading an old state with
the former unverified policy is intentionally refused by the existing model
guard. Do not edit old model metadata to bypass it or restart a completed run.
Future authorized campaigns must use a new state directory.

For a completed flat campaign, the separate command below reads the original
report and writes a new correction artifact. It neither starts a worker nor
rewrites the ledger, execution manifest, historical prices, fills, or deadline.

```bash
PYTHONPATH=src python -m live.reconcile_funding \
  --report /path/to/original/report.json --market XRPUSDT=7 \
  --output /path/to/new/funding-correction.json
```

Verify each market mapping against the official `orderBooks` response first.
The output path must not already exist. Duplicate source settlements and
attempts to reconcile an existing correction are refused. Previously booked
events are not charged again. Failed lookups and missing boundary index values
stay unresolved. The source hash and response for each settlement are saved.

This remains a synthetic near-boundary index estimate, not an actual exchange
account statement. Even with every funding rate recovered, historical collection
gaps remain, and the funding-adjusted MDD is **unknown** without rebuilding the
equity path. The correction explicitly keeps `performance_complete=false` and
`corrected_mdd_pct=null`. Raw campaign data and private operational evidence must
stay outside GitHub.

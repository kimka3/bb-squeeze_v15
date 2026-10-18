# Human-operated production service

The legacy `live.run --mode live` gate remains closed. `live.production` provides
a separate reviewed operator entry point, with default read-only `check`. Code
installation, unit registration and diagnostic probes do not authorize orders.

## Configuration and activation

Keep the private JSON outside Git. It contains:

- `authentication`: network, account_index, api_key_index, key_file,
  expected_address; the key content stays in its existing protected file.
- `trading`: a complete `LiveConfig.describe()` dictionary with mode live and
  the approved account/state directory. The factory enforces the unchanged
  default strategy, BCH-excluded universe, 60-second poll and passive TP.
- `max_initial_capital`: the user's authorized starting-funds ceiling.
- `account_lock_path`: the same account lock used by all bounded check commands.

The user runs `bb-live check`, then `bb-live start` in their own root terminal.
Start runs a fresh preflight and requires typing the displayed `START LIVE ...`
phrase. It verifies a flat account, no active orders, available balance and the
current Standard zero-rate fee policy again after confirmation. Only this human
command writes `activation.json` and starts/enables the live service and timer.
Noninteractive start is refused before any connection or activation creation.

Activation is bound to account, config bytes and deployed Python source digest.
Its initial equity and ledger start timestamp survive all automatic restarts.
Changed code/config is not an implicit new authorization. A state directory
containing old execution or strategy records cannot become a new campaign.

The user is shown the remaining limitations: per-trade fee omissions, actual
stop-trigger and live publication latency not proven by synthetic tests, and
protection placement/amendment/residual-close delays during REST outages. These
limitations are retained with activation; neither preflight nor activation
certifies profitability or complete fee accounting.

## Capital and strategy

Signals, exits, 2% risk per trade, 4% initial portfolio risk, BTC-bull short
multiplier, gross exposure cap and maximum positions remain in `live_core`.
There is no newly invented 2% lifetime or daily account loss limit.

Sizing equity is the lower of actual exchange equity and initial authorized
equity plus owned trade cashflows, signed current position value and attributed
settled funding. This allows the bot's profit/loss to compound, without silently
allocating unrelated later deposits. Actual account equity is reported separately.
Unknown manual trades/positions remain reconciliation failures. Missing fees
remain unknown; the actual-equity bound prevents ignoring costs from inflating
the sizing base. These controls do not make a cross-margin wallet segregated:
do not add unrelated funds or trades to this test account.

## Operator commands

| Command | Effect |
| --- | --- |
| `bb-live check` | Read-only first-start preflight, including authenticated account and confirmed Binance bars. Requires writer lock to be free. |
| `bb-live start` | Human-confirmed first start, or human-confirmed restart of the same campaign. No state reset. |
| `bb-live status` | Service status and saved health/report, including timestamps. Does not query/submit orders. |
| `bb-live logs` | Last service journal lines. |
| `bb-live pause` | Pause only new entries; existing risk service and exits continue. |
| `bb-live resume` | Interactive resume of operator pause only if health is current and clean; never clears HALT. |
| `bb-live stop` | Pause first, await the runner's safe-boundary acknowledgement, then verify no positions/orders before stopping. It never liquidates a position. |

Stop refuses when exposure or an unresolved operation remains; it must not be
interpreted as liquidation. A stop request while the service is unresponsive
requires inspection; the tool does not blindly kill the protection process.
After a clean stop, activation/history remain. `start` restarts that campaign
and preserves pause/HALT; inspect status before an explicit `resume`.

## Supervision and local alarms

The service uses systemd Restart=on-failure, a 30-second delay, and at most three
starts in a 15-minute window. It checkpoints state and keeps native stops on
shutdown; a process exit does not itself close exchange positions. The worker
reconciles/recoveries before entering the main decision loop.

The timer runs local health checks every 60 seconds after an initial 90-second
delay. Health uses the last successful **protection/account reconciliation**,
not a successful mark-price fetch alone. Missing/stale service success, HALT,
pending operations, residual exits or data gaps writes `attention.json`, journals
changes, and pauses new entries. Temporary read failures use a separate runner-owned EXCHANGE_PAUSED gate when the human activation enables automatic recovery. Recovery never clears ENTRY_PAUSED.
The 180-second staleness threshold is an operational setting, not an observed
worst-case response bound. Alerts are local; no email, messenger or phone push
has been configured.

Health also checks fresh market polls, orphan orders and API diagnostic storage.
Malformed/future timestamps fail closed. Resume checks current collection gaps
and rereads the actual service status after the user's confirmation. Historical
recovered gaps do not block resume. `entry_pause_reason` identifies a latched
operator or monitor pause even after API reads recover.

## API interruptions and diagnostics

### Autonomous exchange recovery

The reviewed activation must contain `automatic_recovery_enabled: true`, bound
to the deployed code fingerprint. A code installation alone does not opt in or
start trading. After this one-time human activation the VPS runner handles
future temporary read outages continuously; the desktop app and daily audit
schedule are not part of this recovery mechanism.

Temporary network/HTTP read failures immediately close a separate `EXCHANGE_PAUSED`
entry gate. Existing position reconciliation and protection continue at the
configured polling cadence. The gate opens only after three distinct complete
market-poll and account/protection cycles spanning at least 120 monotonic seconds,
with no intervening failed cycle and no gap exceeding 90 seconds. Counters reset
on failure and restart. There are no extra diagnostic API calls or test orders.
Both the actual account/order ledger reconciliation and the latest completed
signal bar must be verified; HALT, unresolved orders, residual exits, orphan
orders, current collection gaps and diagnostic storage failures block recovery.

Temporary read failures during bootstrap also wait at the existing poll interval
instead of exhausting systemd crash restarts. A local request-budget cooldown
retains its own reason and is not evidence of an exchange outage. Authentication,
identity, schema, local storage and uncertain order-operation failures do not
gain automatic authorization to resume. The durable manual `ENTRY_PAUSED` latch,
including an operator stop and old monitor latches, is never removed by recovery.
Non-transient failure evidence survives a crash before the manual latch is made.

The health timer still flags unresolved issues. Only an activated runner's owned,
current recovery gate can explain stale poll/reconciliation timestamps without
creating an additional permanent manual pause. A stale runner heartbeat, invalid
activation or malformed marker remains a manual attention condition.

Recovered entry permission applies only to eligible future signals. Closed bars,
entry deadlines (60 seconds), budget reserves (24/20), sizing, exits and strategy
rules are unchanged. Signals that expired while offline are never replayed;
long outages causing genuine strategy data gaps/HALT still require inspection.
Successful reads cannot prove that a future transaction will be accepted. The
existing uncertain-submission handling and no-blind-resend rule still apply.

`runner_status.json.automatic_recovery`, `EXCHANGE_PAUSED`, and the journal events
`exchange_recovery_started`, `exchange_recovery_failure`, `exchange_recovery_ready`
explain the current stage and completion. Historical outage/performance records
are preserved. These controls do not guarantee zero downtime or zero missed signals.

Quick HTTP 502/503/504 or direct connection-close/reset failures may get one
additional GET attempt. At most one extra attempt per transport per 60.1 seconds
is allowed; every attempt consumes the shared 55-request budget, and the extra
attempt requires 21 available slots to leave 20 for protection. No sleeps are
introduced inside risk service. Retry-After responses, rate limits, timeouts,
authentication/payload errors, nextNonce and an active entry-send deadline do
not use this retry. Transactions are never blindly resent. The existing entry
deadline and 24/20-slot entry checks remain in place.

Retries must start with at least one second of the original ten-second read
budget remaining. The socket timeout uses the remaining budget and a response
arriving after it is discarded. urllib does not provide hard wall-clock
cancellation of every socket operation; this is not a hard real-time guarantee.

`api-reads-<wallet-scope>.jsonl` beside the protected API key stores allowlisted
endpoint, timestamp, status, duration, attempt, failure class and response request
IDs. Query strings, auth headers, response bodies and exception messages are
excluded. Files are 0600 on Linux and rotate at 2 MiB with two backups. A separate
nonblocking process lock serializes append/rotation across administrative and
service transports; lock contention is reported as diagnostic unavailability.
A storage
failure latches diagnostic loss for the process lifetime, blocks new entries
immediately, and appears in health; existing reduce-only protection remains
available. Repair storage and inspect the evidence before a human-operated
restart/resume. A later successful log write does not restore missing evidence.

An exchange maintenance window can disable API and transaction processing.
Retries cannot make such an exchange available or guarantee a native stop will
execute while matching is unavailable. Check the official status/announcement
channels for planned windows. Keep the safety pause throughout interruption and until recovery checks pass;
do not extend stale prices, entry deadlines or health thresholds to hide it.

Install units from `scripts/*.service.in` with explicit reviewed paths. Verify
with systemd-analyze before use. Services/timers stay disabled and inactive after
installation. A separate transient read-only probe can validate service
hardening and connectivity without starting `bb-squeeze-live.service`.

Private histories, deployment configs, account identifiers and activation files
belong outside the public repository. Do not copy an activation across accounts
or clear a campaign's SQLite/JSON files to recover from an error.

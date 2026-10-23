# Phase 4: Paper broker execution groundwork

## Current scope

Phase 4 adds a paper-only Alpaca REST boundary, account/position/open-order
inspection, a local order journal, explicit order reconciliation, and a gated
submission method for a fresh Risk Warden-approved **long** intent.
`PaperSessionController` now connects deterministic research, required Phase 3
analyst policy, Risk Warden and that broker boundary.
`PaperSessionSupervisor` provides an explicitly started async cadence runner.
Neither is exposed as an order-submitting CLI command or auto-starting service;
there is no live endpoint.

The adapter uses the fixed `https://paper-api.alpaca.markets` origin and only
supports USD stock/ETF long limit entries with an OTO stop-loss. It does not
accept endpoint overrides, live mode, short intents, leverage, market entries,
extended-hours orders, or risk decisions constructed from model output.

## Operator commands

The read commands require paper mode and `TRADER_BOT_PAPER_*` credentials:

```bash
TRADER_BOT_MODE=paper \
TRADER_BOT_PAPER_API_KEY=your-paper-key \
TRADER_BOT_PAPER_API_SECRET=your-paper-secret \
trader-bot paper-account

trader-bot paper-orders
trader-bot paper-reconcile
```

`paper-preview` runs a one-shot research, analyst, and risk evaluation using a
local OHLCV CSV and an evidence bundle. The bundle uses the existing analysis
snapshot format (`analyze-agents` input/output); the selected instrument and
session/quote values use the same flags as `analyze-csv`. For example:

```bash
TRADER_BOT_MODE=paper \
TRADER_BOT_PAPER_API_KEY=your-paper-key \
TRADER_BOT_PAPER_API_SECRET=your-paper-secret \
TRADER_BOT_WATCHLIST=DEMO \
trader-bot paper-preview \
  --bars data/demo.csv --symbol DEMO --venue NASDAQ --calendar XNAS \
  --session-open 2026-10-21T13:30:00Z --session-close 2026-10-21T20:00:00Z \
  --as-of 2026-10-21T15:00:00Z --bid 110 --ask 110.02 \
  --quote-at 2026-10-21T15:00:00Z --min-price 5 --min-volume 1000 \
  --max-spread-bps 20 --max-quote-age-seconds 30 \
  --max-bar-age-seconds 60 --verified --evidence local-data/analysis.json
```

The command reads account equity, positions, open orders, and journaled orders
from the paper API to establish/reconcile its baseline. It may persist local
journal and circuit-baseline state. It never submits, cancels, or modifies
broker orders, even if order-write gates are enabled. `--verified` is an
operator acknowledgment that the supplied market/session inputs were checked;
it is not a broker capability verification. If local inference is enabled,
pass the same pinned `--model` and `--model-version` values used for analysis.
With inference disabled or execution capabilities unverified, the preview can
report a hold rather than an order-sized preview.

## Bounded paper lifecycle test

For an explicitly authorized, one-order paper capability test, use
`paper-lifecycle-test` during an active exchange session. It requires a local
OHLCV CSV, matching analyst evidence, enabled pinned local inference, the
paper-write flag, and an exact typed confirmation. It computes a buy limit one
configured price tick below the observed bid and applies a deterministic
notional cap no greater than USD 100 inside Risk Warden sizing. It submits at
most one risk-approved OTO and makes one cancellation attempt if the order is
still unfilled. It does not retry or automatically liquidate a fill.

Do **not** set `TRADER_BOT_PAPER_EXECUTION_CAPABILITIES_VERIFIED` merely to run
this test; the dedicated command uses a separate, bounded test path so the
flag does not falsely claim prior verification. The passive limit may still
fill if the market moves down to it. If any fill is observed, the command
pauses new entries and stops for manual review; any partial fill or uncertain
cancellation also requires checking the account and order in Alpaca. An
unconfirmed/missing protective stop halts the journal and requires operator
intervention. A successful no-fill test validates only the account's observed
OTO submission and cancellation response, not partial-fill stop activation or
all restart/outage behavior.

```bash
TRADER_BOT_MODE=paper \
TRADER_BOT_PAPER_API_KEY=your-paper-key \
TRADER_BOT_PAPER_API_SECRET=your-paper-secret \
TRADER_BOT_PAPER_ORDER_SUBMISSION_ENABLED=true \
TRADER_BOT_WATCHLIST=HAS \
TRADER_BOT_LOCAL_MODEL_ENABLED=true \
trader-bot paper-lifecycle-test \
  --bars local-data/has-bars.csv --symbol HAS --venue NYSE --calendar XNYS \
  --session-open 2026-10-22T13:30:00Z --session-close 2026-10-22T20:00:00Z \
  --as-of 2026-10-22T15:00:00Z --bid 50 --ask 50.01 \
  --quote-at 2026-10-22T15:00:00Z --min-price 5 --min-volume 1000 \
  --max-spread-bps 20 --max-quote-age-seconds 30 \
  --max-bar-age-seconds 60 --verified --evidence local-data/has-analysis.json \
  --model your-pinned-local-model --model-version sha256:your-pinned-digest \
  --max-notional 100 --confirm-submit "SUBMIT HAS UP TO 100 USD"
```

Use current, independently verified local market data and session timestamps;
the example prices and dates are placeholders, not a trade recommendation.
The command refuses submission if the account is not reconciled and flat, the
risk approval does not pass, the one-share minimum would exceed the cap, the
order is not below the observed bid, or the confirmation does not match.

`paper-pause` sets a persistent local halt that blocks new entries without
requiring broker credentials. It does **not** cancel existing orders or close
positions. `paper-resume` requires paper credentials, reconciles the broker
state, verifies the paper account has no positions or open orders, and
explicitly acknowledges the halt; unresolved/unknown orders or non-flat broker
state prevent resumption. A latched daily drawdown circuit remains tripped after
this operator-level resume and continues to block risk approval.

`paper-reconcile` looks up unresolved journal entries by client order ID. To
request cancellation of expired entries, pass `--cancel-expired`; this requires
both paper-write and capability-verification flags. A cancellation response is
not treated as confirmation: the command re-queries the order and fails closed
if the resulting state is still unknown. A partially filled entry is canceled
at expiry, then its stop leg must be active and cover the filled quantity or
the journal halts further submissions. After resolving an earlier halt,
`paper-reconcile --acknowledge-reconciled` explicitly acknowledges the
successfully reconciled broker state and clears the persistent entry halt.
If cancellation times out or remains pending, the order is journaled as unknown
and entries stay halted; a later reconciliation can recover the broker's
terminal state, including after process restart, before the halt is explicitly
acknowledged.
An entry that is rejected after a partial fill remains journaled as `protected`
while its stop is open. A stop reported filled while its parent entry can still
fill is treated as unknown and halts entries; reconciliation does not assume the
remaining entry quantity was canceled.

The Python `AlpacaPaperBroker.submit_approved` method is the only order-creation
path. It accepts a fresh allowed `RiskOutcome`, verifies paper mode, signal
expiry, matching intent/decision fields, current account equity and buying power,
requires an otherwise flat account, writes a durable pending journal record, and
submits a limit OTO with a stop-loss child. It persists a filled entry as
`protected` while its stop remains open, and removes it from unresolved state
only after the stop is confirmed filled. It fails closed when broker fill
quantities are inconsistent or the stop/entry lifecycle is ambiguous. There is
intentionally no CLI command that accepts user-authored order quantities or
unchecked order JSON.
`PaperSessionController.run` connects this method to the research candidate and
required analyst policy. The caller supplies curated Phase 3 evidence and a
circuit-breaker session baseline; agents cannot approve or size an order.

`PaperSessionSupervisor.run_forever` invokes expiry supervision before each
session-input request, then runs the optional session and reports a cycle to a
caller-supplied callback. Its interval defaults to 300 seconds and cannot be
configured longer than one bar. The caller must explicitly start and stop it
and supply current instrument/evidence/circuit inputs asynchronously. Cycles are
sequential, so slow inputs or broker requests can delay later checks; this is
best-effort supervision, not a hard real-time guarantee. An uncaught provider
or broker error terminates the runner rather than triggering hidden retries.
The end-to-end controller tests use a simulated broker and synthetic analyst
evidence only.

Pass a `DesktopNotifier` to `PaperSessionController` to enable local alerts for
session outcomes, broker failures, expired-order reconciliation, and observed
partial-fill/fill/cancel/expiry/rejection state changes. Linux uses
`notify-send`; macOS uses `osascript`; Windows uses a local PowerShell Windows
Forms balloon notification in an interactive desktop session. Alerts are never
sent over the network.
Repeated order-state alerts are deduplicated for the lifetime of the controller
instance. On alert failure, the session result includes `alert_error`; an
already-approved safety decision or confirmed broker result is not altered.
Missing PowerShell or host delivery failures are surfaced as notification
errors. The current CLI does not construct the session controller/supervisor or
enable notifications automatically.

## Required write gates

The default remains read-only. Order submission requires both:

```text
TRADER_BOT_PAPER_ORDER_SUBMISSION_ENABLED=true
TRADER_BOT_PAPER_EXECUTION_CAPABILITIES_VERIFIED=true
```

The second setting is an operator acknowledgment, not an API capability probe.
Before enabling it, verify in the intended paper account that OTO stop coverage,
partial-fill handling, order cancellation, and restart/reconciliation behavior
meet the account's requirements. The code still halts if an accepted OTO response
does not expose the expected active stop leg. No credentials are needed by tests,
and this implementation has not sent a broker request.

The fixed order uses `time_in_force=day` and `extended_hours=false`. Its local
intent expiry is usually five minutes. A running supervisor requests
`paper-reconcile --cancel-expired` through the broker boundary each cycle, but
cannot enforce deadlines while stopped or during a slow/unavailable broker.
Do not treat the order as continuously supervised.

## Persistence and failure handling

The default SQLite journal is `local-data/paper-orders.sqlite3`, excluded from
Git. The database file is restricted to owner read/write permissions and uses
SQLite full synchronous writes. It retains order/safety/circuit state for
recovery and prunes operational event rows older than seven days when the
journal opens and when new journal events are recorded. It does not run a
background retention daemon. The journal is not encrypted; operators are
responsible for host protection and backup retention.

The client uses a ten-second timeout, bounded response bodies, no redirects, no
automatic retries, and sanitized broker errors. Client order IDs are queried
before submission. The journal serializes submissions and records a prepared
state before the POST. A timeout/ambiguous write, missing broker order, rejected
cancel confirmation, or unverified stop halts further entries; an ambiguous
write is never automatically resubmitted.

The session controller deliberately refuses submissions if the account has any
open position or open order. This avoids claiming that account-wide exposures
were included in the deterministic sizing decision. There is no background
broker streaming or automatic protective-exit recovery. Pause/resume is
available through explicit local CLI commands, and desktop alerts are injectable
in the programmatic controller but are not enabled by the CLI.
If a stop is rejected or not confirmed, the journal halts entries and the
controller can notify the operator, but cannot guarantee liquidation or a
protective stop; the operator must contact the paper broker and resolve the
position.

## Provider notes and remaining gates

The adapter follows Alpaca's [order API](https://docs.alpaca.markets/us/reference/postorder)
and [order-class documentation](https://docs.alpaca.markets/us/docs/orders-at-alpaca),
which describes OTO orders with an attached stop-loss and stop orders when no
stop-limit price is supplied. Documentation does not certify this specific
account's paper behavior, partial fills, market protection, or outages.

The local journal now persists the circuit baseline and latched trip per account
and exchange session; a changed baseline is rejected, and a trip cannot be
cleared by saving a fresh in-memory state. Remaining Phase 4 work includes an
periodic operator-controlled caller/scheduler for session and expiry
supervision, further recovery/fault-injection tests, and account-specific paper
capability validation. The one-shot `paper-preview` command is available for
non-submitting review; the bounded lifecycle test provides a narrowly scoped
account test but does not replace fill/partial-fill validation.
Account-specific capability tests still require the user's Alpaca paper
account and explicit authorization; no such account calls are made by
repository tests.
Protective-stop failures halt new entries but currently require operator
intervention rather than automatic repair. None of these tests predict
profitability or make live trading available.

## Tests

```bash
PYTHONPATH=src python -m unittest tests.test_paper_broker tests.test_paper_session tests.test_notifications tests.test_config -v
```

Tests use mocked transport and a simulated broker only. They verify the fixed
paper host, mode and feature gates, OTO request shape, risk/expiry checks,
flat-account restriction, private persistent journal, concurrent submission
exclusion, fail-closed handling of uncertain broker state, and the integrated
research/analysis/risk/broker decision flow, notification failures, and
seven-day event pruning on journal activity/startup. They do not certify
external provider behavior or perform a real paper order.

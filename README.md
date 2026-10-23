# Trader Bot

A local-first Python project for deterministic trading research, backtesting,
multi-agent context, and gated broker paper-trading groundwork. AI analysts
provide bounded context; deterministic strategy and risk code control trade
decisions. This is an experimental software project, not investment advice, and
it makes no promise of profitability.

## Features

- **Deterministic research:** validated market-data boundaries, CSV and Alpaca
  IEX providers, freshness/liquidity filters, Decimal EMA/ATR indicators, and
  an experimental crossover benchmark.
- **Backtesting and replay:** deterministic position sizing, exposure limits,
  a latched daily circuit breaker, causal fill simulation, and versioned local
  bundles for reproducible replay.
- **Multi-agent analysis:** separate technical, news/sentiment, and
  informational fundamental analysts; strict evidence/output validation;
  deterministic orchestration; optional local Ollama inference; and recorded
  analysis replay. Agents cannot create signals, approve trades, or place
  orders.
- **Paper execution groundwork:** paper-only Alpaca account/order inspection,
  a private local order journal, fail-closed reconciliation, and a gated
  long-only OTO submission path. A programmatic session controller connects
  research, analyst policy, risk approval, and the broker boundary; its
  integrated tests use a simulated broker. `paper-preview` runs a one-shot
  non-submitting evaluation from local evidence and market data;
  `paper-lifecycle-test` is a separately confirmed, single-order account test
  capped at USD 100, with no retries or automatic liquidation;
  `paper-pause` persists an entry halt, while `paper-resume` requires explicit
  reconciliation. Optional local Linux/macOS/Windows desktop alerts can be
  injected into the controller.
- **Local-first safety defaults:** no live execution, no paid runtime
  dependencies, local model inference disabled by default, paper writes
  disabled by default, and no broker requests in the offline quick start.

Phase 4 is not a fully supervised trading service: its async
`PaperSessionSupervisor` must be explicitly started by a caller and supplied
with current session inputs; periodic scheduling is not exposed through a CLI
command or auto-starting background service. The one-shot `paper-preview`
command reads account state but never submits or cancels orders. Scheduling is
best-effort, and account-specific Alpaca lifecycle behavior has not been
certified. See the
[Phase 4 guide](docs/phase-4.md) before configuring any broker access.

## Quick start

Requires Python 3.12 or newer. The project has no runtime package dependencies.
These commands use synthetic data only; they do not need credentials, AI
inference, or a broker connection.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .

# Verify the default offline/backtest configuration.
trader-bot config-check

# Run the synthetic backtest and verify its recorded result.
mkdir -p local-data
python examples/phase2_demo.py --output local-data/demo-input.json
trader-bot backtest \
  --input local-data/demo-input.json \
  --output local-data/demo-report.json
trader-bot replay --input local-data/demo-report.json
```

The demo intentionally produces a simulated loss, not a performance claim.
Bundle writers do not overwrite existing files; use new output filenames on
subsequent runs. `local-data/` is excluded from Git.

Run the test suite with:

```bash
python -m unittest discover -s tests -v
```

## Offline examples

For a synthetic CSV-based signal inspection, see the [Phase 1 guide](docs/phase-1.md)
and its `analyze-csv` example. It prints a research candidate, not a risk
approval or order.

To replay recorded synthetic analyst outcomes without invoking a model:

```bash
python examples/phase3_demo.py \
  --output local-data/agents-fixture.json \
  --fixture-report
trader-bot replay-analysis --input local-data/agents-fixture.json
```

This fixture is a test double, not AI analysis. Real local inference is opt-in;
see the [Phase 3 guide](docs/phase-3.md).

## Configuration and paper-trading boundary

The default mode is `backtest`. Check it with `trader-bot config-check`.
Backtest/replay commands are offline and require backtest mode. Paper
account/order inspection requires paper mode and paper-only
`TRADER_BOT_PAPER_*` credentials. Live mode is rejected, and live credentials
are not read.

Paper order writes require both
`TRADER_BOT_PAPER_ORDER_SUBMISSION_ENABLED=true` and
`TRADER_BOT_PAPER_EXECUTION_CAPABILITIES_VERIFIED=true`. The second flag is an
operator acknowledgment, not an automated broker capability test. Do not enable
order writes until the intended paper account's OTO stop coverage, partial-fill,
cancellation, and restart/reconciliation behavior have been independently
verified. Never put credentials in source control or share them in chat.

## Documentation

- [Design documentation index](docs/README.md) — architecture, use cases,
  workflows, contracts, security/operations, decisions, and phase guides.
- [Refined product and system specification](spec/trader-bot-refined.en.md) —
  goals, scope, safety principles, requirements, and readiness considerations.
- [Original English specification](spec/trader-bot.en.md)
  and [Spanish source specification](spec/trader-bot.md).
- [Implementation status and phase plan](docs/decisions-and-plan.md).
- [Phase 1: market data and deterministic research](docs/phase-1.md).
- [Phase 2: deterministic risk, backtesting, and replay](docs/phase-2.md).
- [Phase 3: multi-agent analysis and local-model replay](docs/phase-3.md).
- [Phase 4: paper broker and simulated session integration](docs/phase-4.md).

## License

See [LICENSE](LICENSE).

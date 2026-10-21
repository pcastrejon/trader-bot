# Trader Bot

Local-first, multi-agent trading research and paper-trading system. AI analysts provide bounded context; deterministic strategy and risk code control all trade decisions. This project does not promise profitability or provide investment advice.

## Current implementation status

**Phase 0 — foundation:** package metadata, mode/configuration validation, typed domain records, agent request/result contracts, and focused tests.

**Phase 1 — deterministic research:** read-only market-data interfaces, local CSV and Alpaca IEX adapters, bounded caching/usage limits, explicit sessions and eligibility filters, Decimal EMA/ATR indicators, and an experimental crossover benchmark.

**Phase 2 — offline risk and replay:** deterministic position sizing and exposure limits,
latched daily circuit breaker, bounded order intents, a causal single-symbol fill
simulator, and versioned local backtest/replay bundles.

No model calls, broker account/order connections, or order submission are implemented.
Phase 2 risk approvals are offline intents only, not executable broker orders.
`live` mode is explicitly disabled. The network data adapter is tested with mocked
HTTP; real account/feed access is not certified.

## Development setup

Requires Python 3.12 or newer. No runtime dependencies are currently required.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
```

To run tests without installing the package:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

## Configuration check

The default mode is `backtest`:

```bash
trader-bot config-check
```

To validate a paper configuration, provide paper-only credentials:

```bash
TRADER_BOT_MODE=paper \
TRADER_BOT_PAPER_API_KEY=your-paper-key \
TRADER_BOT_PAPER_API_SECRET=your-paper-secret \
trader-bot config-check
```

The application reads only the `TRADER_BOT_PAPER_*` credential variables, and only in paper mode. Backtest mode ignores all broker credential variables. Live mode is rejected before credentials are read.

## Offline Phase 1 example

This uses a **synthetic** fixture, not actual stock history. `DEMO` is not a verified
tradable security. These filters and session times are test inputs, not recommended
live-trading settings. No network access or credentials are required.

```bash
TRADER_BOT_WATCHLIST=DEMO trader-bot analyze-csv \
  --bars tests/fixtures/benchmark_long.csv \
  --symbol DEMO --venue NASDAQ --calendar XNAS --verified \
  --session-open 2026-01-02T14:30:00Z \
  --session-close 2026-01-02T21:00:00Z \
  --as-of 2026-01-02T19:00:00Z --quote-at 2026-01-02T19:00:00Z \
  --bid 110 --ask 110.02 \
  --min-price 5 --min-volume 1000 --max-spread-bps 20 \
  --max-quote-age-seconds 30 --max-bar-age-seconds 60
```

The command prints a candidate, a no-signal result, or an explicit rejection code.
Rejected/invalid research inputs exit with status 2. A candidate is **not** an
approved trade and contains no quantity or order.

See the [Phase 1 guide](docs/phase-1.md) for provider contracts, data validation,
indicator definitions, usage accounting, testing, and limitations.

## Offline Phase 2 example

Generate a synthetic dataset, simulate it, and verify an exact replay:

```bash
mkdir -p local-data
python examples/phase2_demo.py --output local-data/demo-input.json
trader-bot backtest --input local-data/demo-input.json --output local-data/demo-report.json
trader-bot replay --input local-data/demo-report.json
```

Use new filenames for subsequent runs: existing files are never overwritten.
This example produces a **simulated loss**, not a profitable strategy claim.
It uses only synthetic bars/quotes, no credentials or external services. Input and
report bundles are stored locally; `local-data/` is excluded from Git.

Read the [Phase 2 guide](docs/phase-2.md) for exact risk formulas, fill assumptions,
reproducibility checks, and limitations. Missing data or unresolved positions cause
a nonzero CLI exit rather than a successful-looking result.

See [docs/](docs/README.md) for architecture, use cases, workflows, contracts, safety decisions, and the phased implementation plan.

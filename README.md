# Trader Bot

Local-first, multi-agent trading research and paper-trading system. AI analysts provide bounded context; deterministic strategy and risk code control all trade decisions. This project does not promise profitability or provide investment advice.

## Current implementation status

**Phase 0 — foundation:** package metadata, mode/configuration validation, typed domain records, agent request/result contracts, and focused tests.

No model calls, data-provider integrations, broker connections, or order submission are implemented yet. `live` mode is explicitly disabled.

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

See [docs/](docs/README.md) for architecture, use cases, workflows, contracts, safety decisions, and the phased implementation plan.

# Design Documentation

This folder defines the design to review before implementation begins. The documents are intentionally technology-light where provider behavior or policy still needs validation. They describe a personal, local-first, multi-agent trading system; they do not claim that any strategy will be profitable.

## Reading order

1. [Architecture](./architecture.md) — system boundaries, components, deployment, and where agents are implemented.
2. [Use cases and requirements](./use-cases.md) — actors, supported scenarios, and observable outcomes.
3. [Workflows and sequence diagrams](./workflows.md) — signal-to-order, safety rejection, recovery, and backtesting flows.
4. [Data and agent contracts](./data-and-agent-contracts.md) — conceptual messages, ownership, validation, and agent responsibilities.
5. [Security and operations](./security-and-operations.md) — trust boundaries, failure behavior, audit, controls, and operational acceptance.
6. [Decisions and implementation plan](./decisions-and-plan.md) — accepted design choices, unresolved blockers, and phased delivery.

The product requirements source remains [the refined specification](../spec/trader-bot-refined.en.md). If a design document conflicts with it or with a new operator decision, resolve the conflict and update the documents before implementation. Mermaid diagrams are source diagrams; keep them synchronized with the text.

## Design baseline

- First release: reproducible backtesting/replay and broker paper trading; no live order capability.
- Multi-agent analysis: technical context, news/sentiment, and fundamental context agents coordinated through an orchestrator.
- Deterministic strategy and Python risk gate are separate from the AI agents. The risk gate alone authorizes candidate intents.
- Five-minute EMA(9)/EMA(21)/EMA(50) crossover with 2.0 × ATR(14) protective-stop distance is an experimental software-validation benchmark only.
- Shorting and leverage can be simulated only in paper mode, with explicit caps. They are disabled for live mode.
- Local-first operation, seven-day operational audit retention, desktop notifications, and no paid recurring service unless the operator explicitly approves it.

# Architecture Decisions and Implementation Plan

This record distinguishes **accepted** decisions from **provisional design defaults** and **open implementation gates**. A decision may be revised before coding; changes should be recorded with rationale and impact.

## 1. Accepted decisions

| ID | Decision | Rationale |
|---|---|---|
| D-01 | Multi-agent analysis is required: separate technical, news/sentiment, and fundamental analysts coordinated by an orchestrator. | Matches the user's architecture goal and supports bounded responsibilities. |
| D-02 | Agents cannot submit orders, set quantities, change risk limits, or approve trades. | Avoid LLM authority over financial execution. |
| D-03 | Deterministic strategy and Python Risk Warden are not LLM agents. | Numeric signal/risk decisions need reproducibility and testability. |
| D-04 | First release includes historical backtest/replay and paper trading; no live execution. | Supports validation and demonstration while avoiding premature live exposure. |
| D-05 | Shorting and leverage may be tested only in paper mode in first release. | Keeps live mode conservative while exercising risk controls. |
| D-06 | Local-first, local-only operational audit for seven days; desktop alerts first. | Aligns with privacy and operator preferences. |
| D-07 | No recurring paid service without explicit approval; $0 monthly cap. | Prevents accidental cost and shapes provider/model selection. |
| D-08 | Unsupported Mexican/international instruments are skipped and reported. | Market, broker, feed, currency, calendar, and account capabilities must be verified per instrument. |
| D-09 | CLI/configuration is the initial operator interface; dashboard deferred. | Minimizes first-release scope while retaining operational controls. |

## 2. Provisional design defaults (validate before paper orders)

| ID | Default | Validation required |
|---|---|---|
| P-01 | Benchmark: completed five-minute EMA(9)/EMA(21) crossover filtered by EMA(50), inverse short signal paper-only; stop = 2.0 × ATR(14). | Treat only as a software-validation benchmark; validate calculations and signal timing. |
| P-02 | Per-trade stop risk 0.25% equity; position notional 5%; combined planned risk 1%; daily drawdown breaker 1%. | Define equity/currency and daily P&L calculation; test boundary behavior. |
| P-03 | Paper gross exposure 1.25×; aggregate short exposure 10%; individual short 2.5%. | Verify broker simulation capabilities and include all exposure in deterministic calculations. |
| P-04 | Limit entries; cancel if unfilled after one five-minute bar; stop-market protective exits where supported. | Verify broker order lifecycle, bracket/OCO behavior, quote semantics, and stop acceptance. |
| P-05 | No new entries in last 30 minutes; target flat by five minutes before regular-session close; exchange calendar handles early closes. | Verify market timezone/calendar behavior and practical exit workflow. |
| P-06 | Backtest fills no earlier than next bar open; fees and slippage configurable. | Ensure no look-ahead and document unavailable historical bid/ask/volume assumptions. |
| P-07 | Locally hosted model optional and disabled by default; no paid inference calls under $0 cap. | Verify hardware/model availability and that no model is required for deterministic safety. |
| P-08 | Alpaca Paper Trading evaluated for orders; Alpha Vantage evaluated for supplemental data. | Verify current account eligibility, free coverage/quotas, licensing, data freshness, and actual costs. |

## 3. Open implementation gates

These do not prevent repository setup, contract design, offline fixtures, or backtest-engine scaffolding. They must be resolved before paper order submission.

1. **Provider capability:** Verify broker account jurisdiction, paper endpoint, order types, shorting/margin simulation, free market-data coverage, quotas, quote fields, and market calendar.
2. **Data freshness:** Set maximum age for quote, bar, news, fundamentals, and account state; reject data beyond threshold.
3. **Universe filters:** Set minimum price/volume/liquidity, maximum spread, and per-symbol/sector/correlation concentration limits. Exclude unsupported and illiquid instruments.
4. **Risk accounting:** Confirm equity/currency conversion, session boundary, realized/unrealized P&L, open risk aggregation, treatment of fees/slippage, and circuit-breaker reset/acknowledgment.
5. **Order lifecycle:** Validate bounded limit entry semantics, one-bar expiration, protective stop coverage after partial fills, idempotency, cancellation, retries, and failed-exit recovery against paper API.
6. **Shorting/leverage:** Confirm how broker simulator represents borrow availability/fees and buying power; fail closed if any short risk input is unsupported.
7. **Agent policy:** Phase 3 research defaults require technical/news results and honor either veto; fundamentals remain optional/informational. Any required failure holds. The explicit archived policy can change technical/news requirements. Confirm these research defaults before paper integration; see the [Phase 3 guide](./phase-3.md).
8. **AI runtime:** Verify a local model can run without recurring costs and whether its latency/hardware is suitable; otherwise keep AI analysis disabled while preserving architecture contracts.
9. **International scope:** Support only instrument/venue combinations individually verified for feed, currency conversion, session calendar, and paper broker access; otherwise skip and report.
10. **Retention:** Decide whether seven-day retention applies only to operational audit records or also to backtest source datasets/results; ensure backups do not silently extend retention.
11. **Paper evidence threshold:** Set minimum duration/trade/test evidence for a later live-readiness review. Passing it is not proof of future profitability.

## 4. Recommended implementation phases

| Phase | Deliverable | Includes agents? | Exit gate |
|---|---|---|---|
| 0. Design and safety foundation | Package layout, config validation, modes, typed domain records, agent contracts, threat boundaries, ADRs | Contracts only; no LLM calls | Backtest/paper cannot load live credentials; contracts and config have tests |
| 1. Data and deterministic baseline | Provider interfaces, normalization, watchlist filters, EMA/ATR calculations, benchmark strategy | No runtime agents; strategy stands alone | Deterministic fixture outputs; stale/unsupported data fails closed |
| 2. Risk, backtest, and replay | Risk Warden, paper-only caps, simulation, reproducible reports | No runtime agents | Numeric boundary, no-look-ahead, replay, and failure tests pass |
| 3. Multi-agent analysis | Technical, news/sentiment, fundamental context agents; local model adapter; orchestrator | Yes — this is the agent implementation phase | Independent agent tests, schema validation, timeout/veto/prompt-injection behavior verified |
| 4. Paper broker and controls | Paper broker adapter, order lifecycle, reconciliation, CLI, desktop alerts, audit retention | Orchestrator connects to flow; agents still cannot reach broker | Paper-only integration; outage and restart tests; protective-exit reconciliation |
| 5. Validation and docs | Fault injection, runbook, architecture docs, provider limitations, paper evidence | Agent evaluation and traceability included | Reviewer can run backtests/replay and paper mode without paid services |
| 6. Future live-readiness | Separate assessment and explicit operator approval | Not in first release | Checklist reviewed; live adapter remains absent until a new decision approves it |

Recommended build order matters: contracts and deterministic logic are established before AI calls; multi-agent analysis is introduced only after signals and risk are independently testable; broker orders are connected after risk behavior has a complete test base.

### Phase 2 implementation baseline

The [Phase 2 guide](./phase-2.md) documents the implemented risk gate and offline
runner. The risk gate supports normalized portfolio/reserved exposure and the
paper-only short/leverage caps. The first causal backtest runner deliberately
uses the existing long-only backtest strategy, one USD instrument, archived
quotes and explicitly supplied sessions. It does not certify broker behavior or
simulate portfolio-wide short borrow. Those limitations are included in reports.

### Phase 3 implementation baseline

The [Phase 3 guide](./phase-3.md) defines bounded local evidence, independently
replaceable analysts, deterministic parallel orchestration and failure policy,
strict prompt/output handling, an opt-in loopback Ollama adapter with pinned
model manifests, and recorded-outcome policy replay. No model is required by
the deterministic baseline. Real model quality/hardware, news/fundamental
provider entitlements and paper policy certification remain open gates.
Orchestrator-to-paper integration and operational audit retention remain Phase 4.

## 5. Initial engineering backlog

1. Add machine-readable configuration and mode isolation.
2. Define typed domain contracts and agent request/result schema.
3. Build deterministic market-data fixtures and instrument/calendar abstractions.
4. Implement EMA/ATR indicators and benchmark strategy with no-look-ahead tests.
5. Implement Risk Warden and reason codes with property/boundary tests.
6. Implement backtest/replay with versioned inputs and fee/slippage assumptions.
7. Implement analyst agents and orchestrator behind replaceable interfaces.
8. Integrate the verified paper broker; exercise orders in sandbox only.
9. Add local audit retention, CLI controls, desktop alerts, and recovery.
10. Complete design docs, setup/runbook, threat notes, and reviewer walkthrough.

## 6. Scope explicitly deferred

- Live order submission, live shorting/leverage, and any claim of expected profitability.
- High-frequency trading, options, OTC/penny securities, broad market scanning, and unsupported international markets.
- Paid AI/data/hosting services without explicit approval.
- Web dashboard, external logging, and mandatory cloud deployment.
- Trailing stop automation until its exact calculation and partial-fill behavior are specified and tested.

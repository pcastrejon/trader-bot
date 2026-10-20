# Trader Bot — Refined Product and System Specification

**Status:** Draft for review  
**Version:** 0.2  
**Language:** English  
**Purpose:** Personal-use, recruiter-ready AI trading-system project. This document describes a system to research, validate, and simulate trading strategies. It does not promise profitability or provide investment advice.

This draft is based on the initial Spanish concept in [trader-bot.md](./trader-bot.md), its faithful English translation in [trader-bot.en.md](./trader-bot.en.md), and decisions gathered in the requirements interview. The Spanish source ends with an unfinished heading; no content was inferred from that missing section.

## 1. Product goal

Build a well-documented Python system that uses specialized AI agents to produce explainable intraday trade proposals and a deterministic, non-AI risk engine to accept or reject them. The first release must support historical backtesting/replay and broker paper trading. Live trading must be disabled by default and unavailable until the operator explicitly approves a documented readiness checklist.

The system is an experimental decision-support and execution tool. It must not represent any strategy, backtest, agent opinion, or paper result as evidence that future returns are assured.

## 2. User decisions and initial scope

- **Use:** Personal system and a professionally documented demonstration of AI-solution architecture.
- **Trading horizon:** Intraday. High-frequency trading is out of scope.
- **Initial universe:** A manually configured watchlist of supported stocks and ETFs. Use liquidity, spread, price, volume, and market-hours filters. Do not scan every listed security by default.
- **Geography:** US-listed instruments are the initial target. Mexican and other international markets are conditional: enable only when the selected broker, account, market-data plan, instrument, order types, currency handling, and market calendar are verified. Otherwise skip the instrument and report why. Do not use scraping or silently substitute an unsupported data source.
- **Shorting and leverage:** May be exercised in paper trading in the first release where the broker simulator supports them, subject to the explicit paper-only caps in Section 4.3. They are disabled in live mode for the first release.
- **Rollout:** Backtest/replay and paper modes are required. Live mode is disabled by default and requires explicit operator enablement after the checklist in Section 9.
- **Broker/data starting point:** Evaluate Alpaca Paper Trading for simulated orders, using the broker feed for order-time price decisions. Evaluate the user's Alpha Vantage API key for supplemental fundamentals and news/sentiment, subject to actual plan limits, freshness, terms, and cost. Do not assume free coverage or API limits without verification. Use provider adapters so providers can be changed without rewriting strategy or risk logic.
- **Cost cap:** $0/month for paid data, hosting, or AI services unless the operator explicitly approves a cost. The system must disclose when this cap prevents a required capability and must not quietly incur charges.
- **Operator interface:** Local CLI/configuration and desktop notifications for the first release. A dashboard is not required for the initial release.
- **Audit retention/privacy:** Keep operational records locally, retain them for 7 days, and do not send them to external logging services. Never log credentials, API keys, or other secrets.

## 3. Safety principles

1. **The LLM is never an execution authority.** AI agents return constrained, structured analysis. They cannot call broker order APIs, change risk settings, or approve their own recommendations.
2. **A deterministic Python risk engine is the sole order gate.** It validates current broker/account state, market data, strategy recommendation, position sizing, exposure, session limits, and all configured controls. Any failed, missing, stale, malformed, or uncertain required input means reject/hold and explain why.
3. **Fail closed.** Do not open new positions when required data is stale or missing, the broker is disconnected, account/order state is uncertain, market hours are uncertain, a safety limit is breached, or cost/provider constraints prevent valid operation.
4. **Stops are protective triggers, not guaranteed loss caps.** Gaps, halts, liquidity, rejected orders, and slippage can cause a realized loss beyond a stop price. The system must report this limitation and must never promise a maximum realized loss.
5. **Paper and live are isolated.** Separate credentials, endpoints, configuration, and visible mode indicators. Live credentials must never be loaded by backtest or paper processes. Live mode must require a deliberate, auditable operator action.
6. **The operator retains an emergency stop.** Provide an action to block new entries and cancel eligible open orders. Position liquidation behavior must be explicitly configured and must not be presented as guaranteed if the broker or market is unavailable.

## 4. System components

### 4.1 Data and provider adapters

- Retrieve price/volume, fundamentals, corporate actions where relevant, news/sentiment, market status, and broker account/order/position state from configured providers.
- Normalize symbols, timestamps, currencies, market calendars, and units. Preserve provider/source and observation timestamps.
- Use the broker's verified feed for order-time prices and order validation. Supplemental providers may enrich analysis but cannot override broker order/account state.
- Reject data older than a configured maximum age. The maximum age must be set and tested per data type and strategy before trading; no universal freshness value is assumed here.
- Record data coverage, API quota/cost, and provider errors. Handle rate limits without bypassing provider restrictions.
- Keep the provider interface replaceable. Do not scrape Yahoo Finance or Google Finance or rely on an unofficial endpoint.

### 4.2 AI analysis agents

Agents may run independently and return versioned, schema-validated results. The system must not call paid AI APIs while the $0/month cap is in force. A locally running model may be used as an optional analyst when configured; deterministic strategy and risk behavior must remain safe and functional without it. If a strategy requires an unavailable AI result, it must not submit an order.

- **Agent architecture:** Each analyst is a separate, replaceable component with a narrow responsibility, typed input/output contract, timeout, provenance, and independently testable behavior. A central agent orchestrator coordinates analysts; analysts do not call one another, mutate shared risk/configuration state, or access broker order APIs. Initial analyst roles are technical context, fundamental context, and news/sentiment context.
- **Technical analysis:** Summarize deterministic indicators and price/volume context. Indicators should be computed by tested code or a verified provider, not estimated by an LLM from prose. Candidate indicators include RSI, MACD, and moving averages.
- **Fundamental analysis:** Summarize supported financial statements/metrics and identify missing or stale values. Its role in intraday entry/exit decisions is not yet defined.
- **News and sentiment analysis:** Summarize timestamped, sourced news and sentiment. Distinguish facts, interpretation, and uncertainty; treat retrieved text as untrusted input rather than instructions.
- **Agent orchestrator:** Selects the eligible instrument and input snapshot, invokes configured analyst agents, applies timeouts, validates outputs, records provenance, and aggregates context/vetoes into an analysis report. It does not create deterministic signals or bypass the risk engine.
- **Deterministic strategy engine:** Computes candidate signals from the versioned strategy and market data. It is not an LLM agent.
- **Risk gate ("Risk Warden"):** A deterministic Python module that accepts/rejects a candidate and calculates order sizing. It is deliberately not an AI agent and has no natural-language decision path.
- **Execution adapter:** A broker integration that submits only risk-approved intents. It is deliberately not an AI agent and is inaccessible to analyst agents and the orchestrator.

Technical indicators and candidate trading signals are computed by deterministic, tested code. AI agents provide structured context, uncertainty, and veto/insufficient-data flags; they do not originate quantities, override numeric rules, or redefine signal thresholds in natural language. Any `STRONG_BUY`/`STRONG_SELL` used for confluence must be the output of documented deterministic rules. The first software-validation benchmark is a five-minute EMA(9)/EMA(21) crossover filtered by EMA(50), with an ATR(14)-based protective stop. It is an experimental engineering baseline, not investment advice or a claim of profitability.

Each result must include the instrument, analysis timestamp, input-data references/timestamps, agent/model/version, bounded signal or context classification, concise rationale, uncertainty, and any data-quality warnings. Invalid output is rejected and logged.

### 4.3 Deterministic strategy and risk engine

The strategy layer converts validated data and agent results into a candidate action. The risk engine independently applies numeric rules and emits an auditable allow/reject decision with reason codes. Only an approved candidate can be handed to the execution adapter.

#### Provisional paper-test risk profile

These values are initial engineering controls for paper testing, not personalized investment advice or a claim that losses are capped:

- Planned loss at the protective stop per trade: at most **0.25% of account equity**.
- Maximum notional value of one position: **5% of account equity**.
- Combined planned open risk: at most **1% of account equity**.
- Daily drawdown circuit breaker: at **1%**, block new entries and require operator review.
- Maximum gross exposure in paper mode: **1.25x account equity**.
- Maximum total short exposure in paper mode: **10% of account equity**.
- Maximum notional value of an individual short position in paper mode: **2.5% of account equity**.

Shorting and leverage are permitted only in paper mode for the first release, subject to broker support and the caps above. Any unsupported borrow/availability, borrow cost, or short-specific risk input must reject the short proposal. The live mode must reject any configuration enabling shorting or leverage.

For an initial implementation, calculate the daily drawdown from start-of-session account equity using broker-reported equity (including realized and unrealized P&L); use the broker's US regular-session calendar and timezone. The 1% circuit breaker blocks new entries, cancels eligible unfilled entry orders, preserves protective exits on existing positions, and requires operator acknowledgment before new entries resume. It does not automatically liquidate positions solely because the breaker tripped. Confirm this accounting and response policy before paper execution is enabled.

For sizing, the risk engine should calculate quantity from the permitted risk budget and distance between proposed entry and protective exit, include configured fees/slippage assumptions where available, round down to valid increments, and then enforce notional and aggregate-risk limits. If required values cannot be calculated reliably, reject the trade.

#### Decision and exit rules

- Deterministic strategy rules generate the candidate signal. The first benchmark uses completed five-minute bars: a long signal occurs when EMA(9) crosses above EMA(21) while the close is above EMA(50); the inverse crossover with close below EMA(50) creates a short signal for paper mode only. AI context may veto a trade or mark it insufficient; it cannot create a signal independently. The initial protective-stop distance is 2.0 × ATR(14) measured at the completed signal bar. Signals may only use completed bars, and backtests must not fill an order using information from before the signal was available.
- Every position must have a broker-supported protective exit plan. Use a stop-market exit where supported to prioritize triggering an exit over price certainty. If unsupported, reject the trade until an alternative is explicitly reviewed. No order type guarantees both execution and a particular price.
- A trailing stop is not enabled until its calculation, update frequency, monotonicity, and behavior after partial fills are specified and tested.
- Use the verified exchange calendar, including early closes. For US regular sessions, do not open new positions in the final 30 minutes and target closing all positions by 5 minutes before session close. Do not intentionally hold overnight. If a position exit is unconfirmed, halt entries and alert the operator.
- Market orders are disallowed for entries by default. Use bounded limit orders for entries. Reconcile acknowledgments, rejections, partial fills, cancellations, and final positions against broker state.
- In backtests, submit an entry only after the signal bar completes and do not assume a fill earlier than the next bar's open; model configurable fees and slippage. In paper mode, derive a bounded entry limit from the current bid/ask and expire an unfilled entry after one five-minute bar.
- If an exit is rejected, unconfirmed, or cannot be submitted, block new entries, alert the operator, and continue reconciliation/retry only under explicit bounded rules. Do not silently report a successful exit.

### 4.4 Execution adapter

- Provide separate backtest, paper, and live adapters behind a common typed interface.
- Backtest and paper processes must not be able to access live credentials or endpoints.
- Submit orders only after risk approval and a final check of mode, market status, quote freshness, price bounds, buying power/position availability, and idempotency key.
- Record broker request/response identifiers and timestamps with secrets removed.
- Reconcile local state with broker positions and orders after each material event and on startup.
- Live adapter remains unavailable until live-readiness checks pass and the operator explicitly enables it.

## 5. Functional requirements

1. Configure a watchlist and per-instrument eligibility; report skipped instruments with actionable reasons (unsupported market/data, stale quote, liquidity/spread, market closed, or provider error).
2. Backtest a versioned strategy against historical data with documented data provenance, assumptions, fees/slippage settings, and reproducible results. Support replay of recorded inputs and decisions.
3. Run in paper mode using the broker sandbox. Clearly display that simulated fills and returns may differ materially from live results.
4. Generate structured proposals and explanations showing timestamps and data sources, concise agent conclusions, deterministic rule checks, proposed quantity/notional and planned risk, order/fill status, and rejection reason.
5. Apply and report all configured risk controls before order submission.
6. Support operator pause/resume, emergency entry-disable, cancellation of eligible open orders, and mode/status visibility.
7. Send desktop alerts for proposed/rejected trades, accepted/submitted orders, fills/partial fills, protective-exit failures, circuit-breaker activation, provider/broker outages, recovery, and required operator action.
8. Treat free email notifications as optional. They may be added only if a no-cost, secure method is available and explicitly configured; email failure must not affect trading safeguards.
9. Maintain local audit records for every decision and broker interaction for 7 days, then delete expired records. Log sufficient references for replay without storing secrets.
10. Enforce the configured zero recurring-cost ceiling. Require explicit approval before enabling any paid API plan, data feed, hosting, or model usage.

## 6. Recovery and exceptional conditions

On stale/missing data, API quota exhaustion, provider/broker disconnect, uncertain clock/calendar, application restart, invalid AI result, or unconfirmed orders/positions:

1. Stop new entries.
2. Preserve circuit-breaker and safety state locally.
3. Reconnect and reconcile account, orders, and positions against broker state.
4. Alert the operator and report the unresolved condition.
5. Require manual acknowledgment before resuming when state or protective coverage is unresolved.

No broad catch or success-shaped fallback may hide a provider, model, storage, or broker failure.

## 7. Non-functional requirements

- **Safety and correctness:** Risk calculations use decimal-safe monetary arithmetic, validated units, explicit rounding, and deterministic tests. Risk rules are versioned and auditable.
- **Reliability:** Idempotent order submission, bounded retries, explicit timeouts, state reconciliation, and safe restart behavior.
- **Security:** Store secrets outside source control and logs; least privilege; separate paper/live credentials; encrypt sensitive local state where practical; document backup/deletion behavior.
- **Explainability:** Every proposal, rejection, and execution attempt is traceable to timestamped inputs, versioned strategy/rules, agent outputs, and broker responses.
- **Cost control:** Show estimated/actual API usage and enforce the operator's $0 paid-service limit unless explicitly changed.
- **Maintainability:** Typed interfaces, provider/execution adapters, clear module boundaries, configuration validation, documented architecture decisions, and reproducible local setup.
- **Observability:** Structured local logs, health/status reporting, provider quota/error metrics, order lifecycle state, circuit-breaker state, and alerts without secrets.
- **Privacy/retention:** Local-only operational records with seven-day retention by default. Exact treatment of user configuration and any longer-lived backtest artifacts must be documented.
- **Performance:** Support the configured minimum five-minute decision cadence without high-frequency behavior. More stringent latency/availability targets are not set until the actual provider plan and deployment are selected.

## 8. Acceptance criteria for the first release

- Unit tests prove risk sizing, notional/open-risk limits, daily circuit breaker, session boundaries, and rejection behavior at and beyond each configured threshold.
- Tests prove stale/missing data, invalid agent output, provider errors, duplicate requests, partial fills, rejected orders, disconnects, and uncertain restart state cannot silently produce a new order.
- Agent-contract tests prove each analyst can be tested independently, the orchestrator validates and records each result, missing/conflicting/timed-out agents follow documented policy, and no agent can access risk configuration or broker order APIs.
- Tests prove shorting/leverage are available only in paper mode and fail closed if explicit paper caps or broker support are absent.
- Integration tests use paper credentials/endpoints only and verify broker order/position reconciliation.
- Backtests are reproducible from pinned data, strategy, configuration, and code versions; reported performance includes assumptions and limitations and is not labeled predictive.
- Audit records and explanations include required provenance and exclude secrets; retention behavior is tested.
- No paid service is invoked without an explicit operator-approved configuration.
- Live order submission is impossible by default and remains blocked until the checklist is approved.
- The setup and runbook let a reviewer configure a local environment, run tests/backtests, start paper mode, inspect decisions, pause entries, and recover from a simulated outage.

## 9. Live-readiness checklist

Live mode remains disabled unless the operator reviews and explicitly approves a written checklist that includes:

- Paper-trading validation with the actual strategy and documented duration/trade coverage. Minimums are an open decision; results do not guarantee future performance.
- Backtest and out-of-sample review, including data quality, fees, slippage assumptions, and known simulation bias.
- Passing safety, failure/recovery, order lifecycle, and security tests.
- Verification of broker eligibility, market-data freshness/rights, supported products/order types, market calendars, and all recurring costs.
- Approval of live-specific limits, including whether shorting/leverage remain disabled, all risk values, position/exposure limits, exit policy, and emergency procedures.
- Explicit acknowledgment that stops may execute at worse prices or fail and that losses may exceed planned risk.
- Deliberate operator action to enable live mode, with auditable timestamp and configuration version.

## 10. Documentation deliverables

- Architecture and data-flow diagram.
- Design rationale and architecture decision records for agent boundaries, risk gate, broker/data providers, order types, and mode separation.
- Local setup, configuration, testing, paper operation, pause/stop, and recovery runbook.
- Security and threat/data-flow notes covering secrets, untrusted news/social content, provider APIs, local logs, and retention.
- API/provider limitations, cost assumptions, market coverage, and data-freshness guide.
- Testing strategy, backtesting methodology, known simulation limitations, and observability/alerting guide.
- Assumptions, limitations, open decisions, and a clear statement that results are not guaranteed returns or investment advice.

## 11. Open decisions before implementation/live enablement

1. Validate the selected broker's exact limit/stop semantics and bid/ask behavior before paper integration; the benchmark uses a 2.0 × ATR(14) stop and next-bar-only backtest fills. No strategy is presumed profitable.
2. Set maximum data age for each feed and define a reliable quote for each supported instrument/market.
3. Define per-symbol, sector, and correlated-position concentration limits and liquidity/spread/price/volume filters.
4. Confirm broker account eligibility and supported instruments/feeds for the user's jurisdiction. Mexican/international instruments remain skipped unless individually verified.
5. Set paper-readiness checklist minimums (duration, trade count, test evidence) without treating them as proof of profitability.
6. Define local log encryption/backup behavior and whether seven-day retention applies to backtest datasets and performance summaries as well as operational audit records.
7. Confirm the provisional drawdown accounting and circuit-breaker behavior in Section 4.3 before paper orders are enabled.

## 12. Implementation plan

The phases are ordered to deliver useful, testable software before connecting any order-submission capability. Live execution is not a first-release implementation phase.

### Phase 0 — Repository and safety foundation

- Record the versioned EMA(9)/EMA(21)/EMA(50) five-minute benchmark as an experimental software-validation strategy, with a 2.0 × ATR(14) stop multiplier.
- Establish Python project structure, typed configuration, environment-based secret handling, lint/type/test commands, and local run instructions.
- Define domain models for instrument, quote/bar, signal, order proposal, risk decision, order state, fill, and audit event. Define versioned analyst-agent request/result contracts, error/timeout/insufficient-data states, and the orchestrator boundary, but do not add model calls yet.
- Define explicit backtest/paper/live modes and prove in tests that live credentials/endpoints cannot be loaded by backtest or paper mode.
- Record provider, order, and risk design decisions in short architecture decision records.

**Exit gate:** Configuration validation, agent-contract validation, and mode-isolation tests pass; analyst model calls and broker order submission do not exist yet.

### Phase 1 — Market data and deterministic baseline

- Implement provider interfaces and the first historical/intraday data adapter(s); begin with the verified US instrument scope.
- Normalize OHLCV, timestamps, market session, currency, symbol identity, and data-quality metadata.
- Add cache/quota/cost tracking and stale/missing-data rejection. Enforce the $0 paid-service cap.
- Implement deterministic EMA(9), EMA(21), EMA(50), and ATR(14) calculations and the explicitly versioned crossover benchmark with a configurable 2.0 × ATR stop. Compute signals only from completed five-minute bars; label the strategy experimental.
- Add configurable watchlist and basic market eligibility/liquidity filters.
- Keep deterministic strategy signals separate from the later AI analyst agents; the strategy must be runnable and testable without any model.

**Exit gate:** Recorded fixtures produce deterministic bars/indicators/signals; malformed, stale, unsupported, or unavailable data cannot create an eligible proposal.

### Phase 2 — Risk engine, backtesting, and replay

- Implement decimal-safe position sizing, stop-distance risk, notional/exposure checks, daily circuit breaker, paper-only short/leverage caps, and reason codes.
- Define entry and protective-exit order intents without submitting them.
- Implement reproducible backtesting/replay with versioned strategy/configuration, provenance, and explicit fee/slippage assumptions.
- Test boundaries, currency/session calculations, partial-fill scenarios, and failure cases.

**Exit gate:** Risk and backtest test suites pass; every proposal is explainable and replayable; no unsupported action is approved.

### Phase 3 — Multi-agent analysis and orchestration

- Implement the **technical context agent** first. It receives already-computed indicators and validated price/volume context; it does not calculate deterministic signals through natural language.
- Implement the **news/sentiment agent** next, using timestamped, permitted news sources. Include source references, uncertainty, and defenses against instructions embedded in retrieved content.
- Implement the **fundamental context agent** as a separate optional analyst. It supplies context and data-quality warnings only; its influence on intraday signals remains disabled until separately specified.
- Implement the **agent orchestrator** to invoke analysts through the Phase 0 contracts, apply timeouts, validate structured results, record provenance, and aggregate context/vetoes. Analyst tasks may run independently; aggregation must be deterministic and tested.
- Add a local-model adapter, disabled by default, behind the common agent interface. Agents remain replaceable and independently testable; deterministic signal and risk behavior works without a model.
- Add prompt/output validation, model/version provenance, timeout handling, and adversarial-input tests for news/social content.
- Ensure no paid model API is called while the $0 cap is active. Missing required agent output must follow an explicit fail-closed policy and cannot bypass a strategy requirement or safety control.
- Keep the **Risk Warden outside the agent framework**: Phase 2's deterministic risk engine is the sole order gate and receives only validated candidate intents and normalized state.

**Exit gate:** Each analyst can be run independently; orchestrated results are schema-validated and replayable; invalid, unavailable, conflicting, or prompt-injected agent output cannot alter numeric risk limits, generate a deterministic signal, or directly cause an order.

### Phase 4 — Paper broker and operator workflow

- Integrate the paper broker adapter using paper-only credentials and the broker's documented feed/API.
- Connect the orchestrator's validated analysis report to the deterministic strategy and risk gate; only risk-approved order intents reach the paper execution adapter. Analyst agents receive no broker credentials and cannot submit or amend orders.
- Implement bounded limit entries, supported stop-market protective exits, idempotency, order lifecycle handling, partial fills, and broker-state reconciliation.
- Enforce no new entries in the final 30 minutes, target flat by five minutes before regular-session close, and handle early closes using the verified calendar.
- Add CLI controls, visible mode/status, pause/resume, emergency entry-disable, eligible-order cancellation, local audit records, and desktop alerts.
- Test disconnections, rejected/late exits, restart recovery, unknown order state, API quota exhaustion, and duplicate submissions.

**Exit gate:** Integration tests use paper endpoints only; the system fails closed and reconciles broker state before resuming.

### Phase 5 — Validation, documentation, and paper release

- Run reproducible backtests and paper exercises; document assumptions, coverage, costs, data freshness, and the difference between simulation and live conditions.
- Complete fault-injection, retention, secret-scanning, and mode-isolation checks.
- Produce architecture/data-flow diagrams, ADRs, setup/runbook, threat/security notes, provider guide, testing/backtesting guide, and known-limitations documentation.
- Keep live trading unavailable; present the live-readiness checklist for a later, separate review.

**Exit gate:** A reviewer can configure, test, backtest/replay, start paper mode, inspect and explain decisions, pause safely, and recover from simulated failure without paid services.

### Phase 6 — Future live-readiness review (not in first release)

- Reassess broker/jurisdiction eligibility, data licensing and freshness, all live-specific risk/order behavior, operational monitoring, security, and costs.
- Review completed paper/backtest evidence and its limitations. Set minimum evidence thresholds in advance; do not interpret a passed threshold as proof of future profitability.
- Require explicit operator acknowledgment and a deliberate, auditable enablement action. Keep leverage and shorting disabled unless separately reviewed and approved.

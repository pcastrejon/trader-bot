# Data and Agent Contracts

This document defines conceptual contracts before selecting concrete Python libraries or serialization. All timestamps should be timezone-aware UTC instants plus the relevant exchange timezone/session identifier. Monetary values must use decimal-safe representations and explicit currency.

## 1. Contract rules

- Every payload has a schema version, correlation ID, creation time, and traceable source/version.
- Required fields are validated at boundaries. Unknown enum values or missing required values are rejected, not guessed.
- Provider observations include `observed_at`, `received_at`, `source`, and quality/freshness metadata.
- Agent outputs are untrusted analysis. They cannot carry executable broker commands or modify risk configuration.
- Order intents can only be constructed by deterministic strategy/risk services. Execution accepts only a risk-approved, unexpired intent with an idempotency key.
- Secret material must never appear in domain events, agent prompts, audit records, or error text.

## 2. Core conceptual records

| Record | Essential fields | Owner |
|---|---|---|
| `Instrument` | canonical ID, symbol, asset type, venue, currency, supported modes, market calendar | Instrument registry/provider adapter |
| `MarketBar` | instrument, interval, start/end timestamps, OHLCV, source, adjustment basis, quality flags | Market data adapter |
| `Quote` | instrument, bid/ask and sizes where available, exchange/source timestamps, received time, feed, condition flags | Market data adapter |
| `AccountSnapshot` | paper mode/account identifier, equity, buying power, currency, as-of time | Broker adapter |
| `AgentRequest` | schema version, correlation ID, agent role, instrument, immutable input references, deadline | Agent orchestrator |
| `AgentResult` | role, status, model/version, input references, timestamp, summary, uncertainty, veto/insufficient-data flags, source references | Analyst agent |
| `CandidateSignal` | strategy/version, instrument, direction, signal-bar end, deterministic rule evidence, stop reference, expiry | Strategy engine |
| `RiskDecision` | allow/reject, reason codes, limits evaluated, equity basis, quantity/notional/risk calculation, risk-rule version | Risk Warden |
| `OrderIntent` | instrument, side, order type, quantity, limit/stop parameters, time-in-force, protective plan, correlation/idempotency keys, expiry | Risk Warden |
| `BrokerOrderEvent` | broker order ID, local intent ID, lifecycle status, filled/remaining quantity, timestamps, sanitized response | Execution adapter |
| `AuditEvent` | event type, time, mode, correlation ID, component/version, sanitized references and decision evidence | Audit service |

## 3. Agent result envelope

Conceptual JSON shape:

```json
{
  "schema_version": "1",
  "correlation_id": "opaque-id",
  "agent_role": "news_sentiment",
  "status": "ok",
  "instrument_id": "canonical-instrument-id",
  "created_at": "timezone-aware timestamp",
  "model": {
    "provider": "local",
    "name": "configured-model-name",
    "version": "configured-model-version"
  },
  "input_refs": [
    {
      "source": "provider-name",
      "reference": "local-record-id",
      "observed_at": "timezone-aware timestamp"
    }
  ],
  "summary": "Concise evidence-based summary.",
  "uncertainty": "low | medium | high | unknown",
  "veto": false,
  "insufficient_data": false,
  "warnings": [],
  "source_refs": []
}
```

Statuses should include at least `ok`, `insufficient_data`, `timed_out`, `unavailable`, and `invalid`. A failed status is data, not a successful analysis. The orchestrator records failure and applies policy; it does not convert a failure to a neutral/positive signal.

### Role-specific constraints

- **Technical context:** Input consists of computed indicator values and normalized price/volume data. The agent can explain context but cannot define/alter the deterministic crossover signal.
- **News/sentiment:** Input contains timestamped retrieved content and provenance. Output distinguishes directly supported facts from interpretation. Retrieved text is untrusted and must not be followed as system instructions.
- **Fundamental context:** Input contains supported financial metrics/statements with period and as-of time. In the first release this is informational and a data-quality warning source, not an intraday signal generator.
- **Orchestrator:** Parallelizes independent analysis tasks, validates each result, handles timeout/invalid results explicitly, and aggregates only permitted context/veto fields. It has no broker credentials and no order tool.

## 4. Signal, risk, and execution contract

1. `CandidateSignal` records a deterministic strategy version and completed signal bar. It contains no AI-generated quantity.
2. `AgentAnalysisReport` references all agent outcomes; configured veto policy is explicit. Missing required analysis rejects/holds the candidate.
3. `RiskDecision` uses a snapshot of account/positions/orders, validated quote, session/calendar, mode, and versioned limits.
4. If rejected, no `OrderIntent` is emitted. The rejection contains stable reason codes and human-readable explanation.
5. If approved, `OrderIntent` is narrowly scoped, bounded, and expires. Its idempotency key prevents duplicate submission.
6. Execution validates mode, expiration, current broker state, quote price bounds, and protective exit support again before submission.
7. Broker responses are mapped into normalized lifecycle events and reconciled against account positions/orders.

Phase 2 implements the offline `OrderIntent`, `Exposure`, `CircuitState` and
`RiskOutcome` in [the risk module](../src/trader_bot/risk.py), with typed limit and
protective-stop parameters. Its [backtest/replay contract](./phase-2.md) is a local
research bundle, not a broker request. Broker response mapping and actual order
submission remain Phase 4 work.

Illustrative stable reason-code groups:

- `DATA_STALE`, `DATA_MISSING`, `DATA_INVALID`, `PROVIDER_UNAVAILABLE`
- `INSTRUMENT_UNSUPPORTED`, `MARKET_CLOSED`, `SESSION_CUTOFF`, `LIQUIDITY_FILTER`
- `AGENT_TIMEOUT`, `AGENT_INVALID`, `AGENT_VETO`, `AGENT_INSUFFICIENT_DATA`
- `MODE_FORBIDDEN`, `RISK_PER_TRADE`, `POSITION_NOTIONAL`, `OPEN_RISK`, `GROSS_EXPOSURE`, `SHORT_CAP`, `DAILY_DRAWDOWN`
- `BROKER_STATE_UNKNOWN`, `ORDER_UNSUPPORTED`, `PROTECTIVE_EXIT_UNAVAILABLE`, `DUPLICATE_INTENT`

## 5. Audit event contents

Every relevant event references:

- event and correlation IDs; UTC time; mode; instrument where applicable;
- configuration, code, strategy, risk-rule, and agent/model versions;
- source record IDs and observed/received timestamps;
- deterministic signal evidence, analysis summary/status, risk rule evaluations;
- amount, quantity, notional, currency, planned stop risk, and rejection reason as applicable;
- sanitized broker request/response identifiers and lifecycle states.

Do not store credentials, authorization headers, full secret-bearing request objects, or unnecessary personal account details. Operational audit retention is seven days, local-only. The exact retention of reusable historical datasets and aggregate backtest reports remains a separate product decision.

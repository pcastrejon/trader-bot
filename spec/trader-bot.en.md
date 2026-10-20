# Trader-bot

## Objective
Design an AI-agent-based algorithmic trading system that can identify the best time to buy or sell my stocks.
Responsibilities must be separated using a multi-agent architecture (one agent analyzes news, another looks at technical charts, and another manages risk).
Unbreakable golden rule: no LLM (such as Claude) may have the final say or direct control over executing a buy/sell order without a strict, deterministic algorithmic filter (plain Python code).

## 1. System Architecture (Multi-Agent)

For a robust Python solution, the ideal architecture divides the problem among specialized agents coordinated by an "Orchestrator."

[Data Sources (APIs)]
       │
       ▼
┌────────────────────────────────────────────────────────┐
│                 CENTRAL ORCHESTRATOR                   │
└──────┬───────────────────┬───────────────────┬─────────┘
       │                   │                   │
       ▼                   ▼                   ▼
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│  Agent 1:    │    │  Agent 2:    │    │  Agent 3:    │
│ Technical    │    │ Fundamental  │    │ Market       │
│ Analyst      │    │ Analyst      │    │ Sentiment    │
└──────┬───────┘    └──────┬───────┘    └──────┬───────┘
       │                   │                   │
       └───────────────────┼───────────────────┘
                           │ (Trade recommendations)
                           ▼
┌────────────────────────────────────────────────────────┐
│             AGENT 4: RISK MANAGER (Risk Warden)        │
│             *Applies strict mathematical rules*        │
└──────────────────────────┬─────────────────────────────┘
                           │ (Approved / Rejected order)
                           ▼
┌────────────────────────────────────────────────────────┐
│                 EXECUTION MODULE (Broker API)          │
└────────────────────────────────────────────────────────┘

Key Components:

1. Central Orchestrator: Collects market data and coordinates calls to the agents. Receives the final verdict.
2. Technical Analyst Agent (Claude API): Receives historical price data tables (velocity, RSI, MACD, moving averages) and looks for visual or trend patterns in text or JSON format.
3. Fundamental Analyst Agent (Claude API): Analyzes quarterly earnings reports and financial metrics (P/E ratio, debt, growth).
4. Sentiment Agent (Claude API): Scans recent news and social media (X/Twitter, Reddit) about the stock to determine whether market sentiment is bullish or bearish.
5. Risk Manager Agent (Plain Python - Critical): Moderates the AI agents' decisions. If the AI decides to buy but violates a capital-management rule, the trade is automatically canceled.

## 2. Golden Rules Engine (Protection and Execution)

These rules must be programmed deterministically in Python (using numeric if/else logic); AI must never be allowed to decide in natural language whether a rule has been met.

Protection Rules (Risk Management)

- Strict Maximum Stop-Loss (Your X% protection): Every trade must be opened with an automatic stop-loss. If a stock falls by X% (for example, 2% or 3%) from its purchase price, the broker immediately sells it automatically.
- Maximum Daily Loss Rule (Circuit Breaker): If the total portfolio loses more than Y% in a single day (e.g., 5%), the bot shuts down completely and cancels all open orders until the following day.
- Position Sizing: No individual trade may risk more than 1% to 2% of the total portfolio capital. If you have $10,000 USD, do not risk more than $200 USD per trade.

Decision Rules (Buying and Selling)

- Agent Confluence Rule: For the Orchestrator to approve a purchase, at least the Technical and Sentiment Agents must issue a "STRONG BUY" signal. If they conflict, the bot stays in cash.
- Dynamic Take-Profit (Trailing Stop): If a trade is profitable, the Stop-Loss automatically moves upward to lock in gains in the event of a sudden market reversal.

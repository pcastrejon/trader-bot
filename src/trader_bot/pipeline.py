"""Phase 1 data-to-candidate workflow; deliberately has no execution capability."""

from dataclasses import dataclass
from datetime import datetime
from collections.abc import Callable

from trader_bot.config import RuntimeConfig
from trader_bot.contracts import Instrument
from trader_bot.market_data import DataError, DataRequest, MarketDataClient
from trader_bot.strategy import (
    BenchmarkStrategy, EligibilityPolicy, Evaluation, EvaluationStatus, ExchangeSession,
    validate_instrument,
)


@dataclass(frozen=True, slots=True)
class ResearchPipeline:
    client: MarketDataClient
    strategy: BenchmarkStrategy
    runtime: RuntimeConfig
    policy: EligibilityPolicy
    sessions: tuple[ExchangeSession, ...]
    clock: Callable[[], datetime] | None = None

    def evaluate(
        self, instrument: Instrument, starts_at: datetime, as_of: datetime,
        correlation_id: str,
    ) -> Evaluation:
        try:
            validate_instrument(instrument, self.runtime, self.policy)
        except DataError as exc:
            return Evaluation(instrument.instrument_id, EvaluationStatus.REJECTED,
                              exc.code, str(exc))
        if self.client.policy.monthly_cost_cap > self.runtime.monthly_cost_cap:
            return Evaluation(instrument.instrument_id, EvaluationStatus.REJECTED,
                              "COST_CAP", "Provider budget exceeds approved runtime budget.")
        try:
            snapshot = self.client.fetch(DataRequest(instrument, starts_at, as_of), as_of)
            if any(
                bar.starts_at < starts_at or bar.ends_at > as_of
                for bar in snapshot.bars
            ):
                raise DataError("DATA_INVALID", "Provider returned bars outside requested range.")
        except DataError as exc:
            return Evaluation(instrument.instrument_id, EvaluationStatus.REJECTED,
                              exc.code, str(exc))
        return self.strategy.evaluate(
            instrument, snapshot, self.sessions, self.runtime, self.policy,
            self.clock() if self.clock is not None else as_of,
            correlation_id,
        )

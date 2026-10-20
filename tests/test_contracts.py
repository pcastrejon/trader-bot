from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from trader_bot.contracts import (
    AccountSnapshot,
    AgentAnalysisReport,
    AgentRequest,
    AgentResult,
    AgentRole,
    AgentStatus,
    AssetType,
    AuditEvent,
    ContractError,
    Fill,
    InputReference,
    Instrument,
    MarketBar,
    OrderProposal,
    OrderState,
    OrderStatus,
    Quote,
    RiskDecision,
    RuntimeMode,
    TradeDirection,
    Uncertainty,
)


UTC = timezone.utc
NOW = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)


class ContractTests(unittest.TestCase):
    def test_agent_request_requires_aware_ordered_deadline(self) -> None:
        request = AgentRequest(
            schema_version="1",
            correlation_id="corr-1",
            role=AgentRole.NEWS_SENTIMENT,
            instrument_id="us:aapl",
            created_at=NOW,
            deadline=NOW + timedelta(seconds=10),
            input_refs=(),
        )
        self.assertEqual(request.role, AgentRole.NEWS_SENTIMENT)

        with self.assertRaisesRegex(ContractError, "timezone-aware"):
            AgentRequest(
                schema_version="1",
                correlation_id="corr-1",
                role=AgentRole.NEWS_SENTIMENT,
                instrument_id="us:aapl",
                created_at=datetime(2026, 1, 2),
                deadline=NOW + timedelta(seconds=10),
                input_refs=(),
            )

    def test_non_ok_agent_result_cannot_set_veto(self) -> None:
        with self.assertRaisesRegex(ContractError, "valid agent result"):
            self._agent_result(status=AgentStatus.TIMED_OUT, veto=True)

    def test_insufficient_data_status_requires_flag(self) -> None:
        with self.assertRaisesRegex(ContractError, "requires insufficient_data"):
            self._agent_result(status=AgentStatus.INSUFFICIENT_DATA)

    def test_analysis_report_rejects_mismatched_agent_result(self) -> None:
        result = self._agent_result(status=AgentStatus.OK)
        report = AgentAnalysisReport(
            correlation_id="corr-1",
            instrument_id="us:aapl",
            created_at=NOW,
            results=(result,),
        )
        self.assertEqual(len(report.results), 1)

        with self.assertRaisesRegex(ContractError, "match the report"):
            AgentAnalysisReport(
                correlation_id="other-correlation",
                instrument_id="us:aapl",
                created_at=NOW,
                results=(result,),
            )

    def test_account_snapshot_rejects_live_mode(self) -> None:
        with self.assertRaisesRegex(ContractError, "not supported"):
            AccountSnapshot(
                account_reference="paper-account",
                mode=RuntimeMode.LIVE,
                equity=Decimal("10000"),
                buying_power=Decimal("10000"),
                currency="USD",
                as_of=NOW,
            )

    def test_market_bar_validates_ohlc_relationships(self) -> None:
        bar = MarketBar(
            instrument_id="us:aapl",
            interval_seconds=300,
            starts_at=NOW,
            ends_at=NOW + timedelta(minutes=5),
            open=Decimal("100"),
            high=Decimal("102"),
            low=Decimal("99"),
            close=Decimal("101"),
            volume=Decimal("1000"),
            source="fixture",
            adjustment_basis="unadjusted",
        )
        self.assertEqual(bar.interval_seconds, 300)

        with self.assertRaisesRegex(ContractError, "high must be"):
            MarketBar(
                instrument_id="us:aapl",
                interval_seconds=300,
                starts_at=NOW,
                ends_at=NOW + timedelta(minutes=5),
                open=Decimal("100"),
                high=Decimal("100"),
                low=Decimal("99"),
                close=Decimal("101"),
                volume=Decimal("1000"),
                source="fixture",
                adjustment_basis="unadjusted",
            )

    def test_quote_rejects_crossed_or_invalid_book(self) -> None:
        with self.assertRaisesRegex(ContractError, "bid must not exceed ask"):
            Quote(
                instrument_id="us:aapl",
                bid=Decimal("101"),
                ask=Decimal("100"),
                observed_at=NOW,
                received_at=NOW,
                source="fixture",
            )

    def test_instrument_requires_currency_code_and_modes(self) -> None:
        instrument = Instrument(
            instrument_id="us:aapl",
            symbol="AAPL",
            asset_type=AssetType.STOCK,
            venue="NASDAQ",
            currency="USD",
            supported_modes=("backtest", "paper"),
            market_calendar="XNYS",
        )
        self.assertEqual(instrument.currency, "USD")

        with self.assertRaisesRegex(ContractError, "currency"):
            Instrument(
                instrument_id="us:aapl",
                symbol="AAPL",
                asset_type=AssetType.STOCK,
                venue="NASDAQ",
                currency="US",
                supported_modes=("paper",),
                market_calendar="XNYS",
            )

    def test_rejected_risk_decision_requires_reason(self) -> None:
        with self.assertRaisesRegex(ContractError, "at least one reason"):
            RiskDecision(
                correlation_id="corr-1",
                mode=RuntimeMode.PAPER,
                allowed=False,
                reason_codes=(),
                rule_version="1",
                evaluated_at=NOW,
                account_equity=Decimal("10000"),
                currency="USD",
            )

    def test_allowed_risk_decision_requires_sizing(self) -> None:
        with self.assertRaisesRegex(ContractError, "require positive quantity"):
            RiskDecision(
                correlation_id="corr-1",
                mode=RuntimeMode.PAPER,
                allowed=True,
                reason_codes=(),
                rule_version="1",
                evaluated_at=NOW,
                account_equity=Decimal("10000"),
                currency="USD",
            )

    def test_order_state_cannot_overfill(self) -> None:
        with self.assertRaisesRegex(ContractError, "cannot exceed ordered"):
            OrderState(
                correlation_id="corr-1",
                broker_order_id="broker-1",
                status=OrderStatus.FILLED,
                submitted_at=NOW,
                updated_at=NOW,
                ordered_quantity=Decimal("2"),
                filled_quantity=Decimal("2"),
                remaining_quantity=Decimal("1"),
            )

    def test_proposal_fill_and_audit_records_validate(self) -> None:
        proposal = OrderProposal(
            correlation_id="corr-1",
            instrument_id="us:aapl",
            direction=TradeDirection.LONG,
            quantity=Decimal("2"),
            notional=Decimal("200"),
            currency="USD",
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
        fill = Fill(
            fill_id="fill-1",
            correlation_id=proposal.correlation_id,
            broker_order_id="broker-1",
            instrument_id=proposal.instrument_id,
            filled_at=NOW,
            quantity=Decimal("2"),
            price=Decimal("100"),
            currency="USD",
        )
        event = AuditEvent(
            event_id="event-1",
            event_type="proposal_created",
            occurred_at=NOW,
            mode=RuntimeMode.PAPER,
            correlation_id="corr-1",
            component="strategy",
            component_version="1",
        )

        self.assertEqual(fill.quantity, proposal.quantity)
        self.assertEqual(event.component, "strategy")

    def _agent_result(
        self,
        *,
        status: AgentStatus,
        veto: bool = False,
        insufficient_data: bool = False,
    ) -> AgentResult:
        return AgentResult(
            schema_version="1",
            correlation_id="corr-1",
            role=AgentRole.NEWS_SENTIMENT,
            status=status,
            instrument_id="us:aapl",
            created_at=NOW,
            model_provider="local",
            model_name="test-model",
            model_version="1",
            input_refs=(
                InputReference(
                    source="fixture",
                    reference="news-1",
                    observed_at=NOW,
                ),
            ),
            summary="Test result",
            uncertainty=Uncertainty.LOW,
            veto=veto,
            insufficient_data=insufficient_data,
        )


if __name__ == "__main__":
    unittest.main()

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from companion.brokers import (
    ALPACA_PAPER,
    DEMO,
    SCHWAB,
    AccountQuery,
    AccountSnapshot,
    AssetType,
    BalanceSnapshot,
    BracketOrderIntent,
    BrokerCapability,
    FakeBrokerAdapter,
    MarketSession,
    MarketSessionsQuery,
    MutationResult,
    MutationStatus,
    OptionContract,
    OptionType,
    OrderInstruction,
    OrderLegIntent,
    OrderType,
    SimpleOrderIntent,
    StopLossIntent,
    SubmitOrder,
    TimeInForce,
    UnsupportedBrokerOperation,
)


def equity_leg() -> OrderLegIntent:
    return OrderLegIntent("AAPL", AssetType.EQUITY, OrderInstruction.BUY, Decimal("10"))


def option_leg() -> OrderLegIntent:
    return OrderLegIntent(
        "AAPL261120C00230000",
        AssetType.OPTION,
        OrderInstruction.BUY_TO_OPEN,
        Decimal("1"),
    )


def bracket(leg: OrderLegIntent) -> SubmitOrder:
    entry = SimpleOrderIntent(
        leg,
        OrderType.LIMIT,
        TimeInForce.DAY,
        "edge-ticket-1",
        limit_price=Decimal("100"),
    )
    return SubmitOrder(BracketOrderIntent(entry, Decimal("110"), StopLossIntent(Decimal("95"))))


def test_every_broker_has_a_complete_capability_matrix():
    for identity in (SCHWAB, ALPACA_PAPER, DEMO):
        documented = identity.capabilities.supported | identity.capabilities.unsupported.keys()
        assert documented == set(BrokerCapability)


def test_alpaca_option_bracket_is_refused_before_the_adapter_runs():
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    adapter.given_command(
        SubmitOrder,
        MutationResult(MutationStatus.ACCEPTED, order_id="would-have-been-sent"),
    )

    with pytest.raises(UnsupportedBrokerOperation, match="option orders do not support the bracket"):
        adapter.execute(bracket(option_leg()))

    assert adapter.commands == []


def test_supported_equity_bracket_crosses_the_same_interface():
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    expected = MutationResult(MutationStatus.ACCEPTED, order_id="08f4d993-opaque")
    command = bracket(equity_leg())
    adapter.given_command(SubmitOrder, expected)

    assert adapter.execute(command) == expected
    assert adapter.commands == [command]


def test_fake_adapter_returns_canonical_reads_in_sequence():
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    account = AccountSnapshot("redacted", "active", BalanceSnapshot(equity=Decimal("100000")))
    sessions = (
        MarketSession(
            date(2026, 11, 10),
            datetime(2026, 11, 10, 14, 30, tzinfo=timezone.utc),
            datetime(2026, 11, 10, 21, 0, tzinfo=timezone.utc),
            True,
        ),
    )
    adapter.given_query(AccountQuery, account)
    adapter.given_query(MarketSessionsQuery, sessions)

    account_query = AccountQuery()
    sessions_query = MarketSessionsQuery(date(2026, 11, 10), date(2026, 11, 10))
    assert adapter.query(account_query) == account
    assert adapter.query(sessions_query) == sessions
    assert adapter.queries == [account_query, sessions_query]


def test_order_intents_enforce_broker_neutral_invariants():
    with pytest.raises(ValueError, match="require a limit price"):
        SimpleOrderIntent(equity_leg(), OrderType.LIMIT, TimeInForce.DAY, "ticket")
    with pytest.raises(ValueError, match="position quantity must be positive"):
        from companion.brokers import Position, PositionSide

        Position("AAPL", AssetType.EQUITY, PositionSide.LONG, Decimal("-1"), Decimal("100"))


def test_option_contract_and_order_ids_are_broker_neutral():
    contract = OptionContract(
        symbol="AAPL261120C00230000",
        underlying="AAPL",
        expiration=date(2026, 11, 20),
        strike=Decimal("230"),
        option_type=OptionType.CALL,
    )
    result = MutationResult(MutationStatus.ACCEPTED, order_id="uuid-or-numeric-text")
    assert contract.multiplier == Decimal("100")
    assert result.order_id == "uuid-or-numeric-text"

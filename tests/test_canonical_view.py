from datetime import datetime, timezone
from decimal import Decimal as D

from companion.brokers.canonical_view import build_canonical_state
from companion.brokers.capabilities import SCHWAB
from companion.brokers.fake import FakeBrokerAdapter
from companion.brokers.models import (
    AccountQuery, AccountSnapshot, AssetType, BalanceSnapshot, BrokerOrder, Fill, FillsQuery,
    OrdersQuery, QuotesQuery, OrderClass,
    OrderGroup, OrderInstruction, OrderLeg, OrderRole, OrderStatus, OrderType,
    Position, PositionSide, Quote, TimeInForce,
)

NOW = datetime(2026, 11, 10, 16, 0, tzinfo=timezone.utc)


def account():
    return AccountSnapshot("paper-account", "ACTIVE", BalanceSnapshot(equity=D("10000")),
                           (Position("AAPL", AssetType.EQUITY, PositionSide.LONG,
                                     D("2"), D("100")),))


def group():
    stop = BrokerOrder("stop-uuid", OrderStatus.WORKING, OrderType.STOP,
                       TimeInForce.GTC,
                       (OrderLeg("AAPL", AssetType.EQUITY, OrderInstruction.SELL, D("2")),),
                       OrderRole.STOP, stop_price=D("95"))
    target = BrokerOrder("target-uuid", OrderStatus.WORKING, OrderType.LIMIT,
                         TimeInForce.GTC,
                         (OrderLeg("AAPL", AssetType.EQUITY, OrderInstruction.SELL, D("2")),),
                         OrderRole.TARGET, limit_price=D("110"))
    return (OrderGroup("oco-uuid", OrderClass.OCO, OrderStatus.WORKING,
                       (stop, target)),)


def test_canonical_paper_state_has_position_protection_and_pnl():
    fills = (Fill("fill-uuid", "entry-uuid", "AAPL", AssetType.EQUITY,
                  OrderInstruction.BUY, D("2"), D("100"), NOW),)
    quote = Quote("AAPL", D("104"), D("106"), None, D("105"), NOW,
                  True, "Alpaca IEX")
    state = build_canonical_state(account(), group(), fills, {"AAPL": quote},
                                  now=NOW, history_complete=True)
    position = state["positions"][0]
    assert position["open_pnl"] == 10
    assert position["protection"] == "FULL"
    assert position["stop_price"] == 95
    assert position["target_price"] == 110
    assert position["ledger"] == "MATCH"
    assert {row["order_id"] for row in state["orders"]} == {"stop-uuid", "target-uuid"}
    assert state["summary"]["open_pnl"]["complete"] is True


def test_stale_orders_and_missing_quote_never_claim_protection_or_complete_pnl():
    state = build_canonical_state(account(), group(), (), {}, now=NOW,
                                  orders_fresh=False)
    position = state["positions"][0]
    assert position["protection"] == "UNKNOWN"
    assert position["open_pnl"] is None
    assert state["summary"]["open_pnl"]["complete"] is False
    assert state["summary"]["realized"]["complete"] is False


def test_partially_filled_native_order_still_counts_remaining_stop_protection():
    stop = BrokerOrder("stop-uuid", OrderStatus.PARTIALLY_FILLED, OrderType.STOP,
                       TimeInForce.GTC,
                       (OrderLeg("AAPL", AssetType.EQUITY, OrderInstruction.SELL,
                                 D("3"), filled_quantity=D("1")),),
                       OrderRole.STOP, stop_price=D("95"), status_detail="PARTIALLY_FILLED")
    groups = (OrderGroup("stop-uuid", OrderClass.SIMPLE, OrderStatus.PARTIALLY_FILLED,
                         (stop,)),)
    state = build_canonical_state(account(), groups, (), {}, now=NOW, orders_fresh=True)
    assert state["positions"][0]["protection"] == "FULL"


def test_fake_broker_can_drive_a_complete_dashboard_without_network():
    fake = FakeBrokerAdapter(SCHWAB)
    fills = (Fill("fill-1", "entry-1", "AAPL", AssetType.EQUITY,
                  OrderInstruction.BUY, D("2"), D("100"), NOW),)
    quote = Quote("AAPL", D("104"), D("106"), None, D("105"), NOW,
                  True, "Fake Schwab")
    fake.given_query(AccountQuery, account())
    fake.given_query(OrdersQuery, group())
    fake.given_query(FillsQuery, fills)
    fake.given_query(QuotesQuery, {"AAPL": quote})
    state = build_canonical_state(
        fake.query(AccountQuery()),
        fake.query(OrdersQuery(NOW, NOW)),
        fake.query(FillsQuery(NOW, NOW)),
        fake.query(QuotesQuery(("AAPL",), AssetType.EQUITY)),
        now=NOW, orders_fresh=True, history_complete=True)
    assert state["positions"][0]["protection"] == "FULL"
    assert state["positions"][0]["open_pnl"] == 10
    assert state["summary"]["realized"]["complete"]

"""The same caller expectations at the Schwab, Alpaca Paper, and fake seams."""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from companion.brokers import ALPACA_PAPER, AccountQuery, AccountSnapshot, AssetType
from companion.brokers import FakeBrokerAdapter, MutationResult, MutationStatus
from companion.brokers.models import (BalanceSnapshot, OrderClass, OrderInstruction,
                                      OrderLeg, OrderLegIntent, OrderRole, OrderStatus,
                                      OrderType, OrdersQuery, Position, PositionSide,
                                      SimpleOrderIntent, SubmitOrder, TimeInForce,
                                      BrokerOrder, OrderGroup)
from companion.brokers.alpaca_paper import AlpacaPaperAdapter
from companion.brokers.schwab_adapter import SchwabAdapter


NOW = datetime(2026, 11, 10, 20, tzinfo=timezone.utc)


class Response:
    def __init__(self, body=None, status=200, headers=None):
        self.body = body
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return self.body


def _adapter(kind: str, *, uncertain: bool = False):
    if kind == "schwab":
        class Client:
            def account_details(self, account, fields):
                assert account == "fixture-account" and fields == "positions"
                return Response({"securitiesAccount": {
                    "currentBalances": {"equity": 10000},
                    "positions": [{"instrument": {"symbol": "AAPL", "assetType": "EQUITY"},
                                   "longQuantity": 2, "shortQuantity": 0,
                                   "averagePrice": 100}]}})

            def account_orders(self, account, start, end):
                assert account == "fixture-account"
                return Response([{"orderId": 123, "status": "WORKING", "orderType": "LIMIT",
                                  "duration": "DAY", "quantity": 2, "price": "110",
                                  "orderLegCollection": [{"instruction": "SELL", "quantity": 2,
                                      "instrument": {"symbol": "AAPL", "assetType": "EQUITY"}}]}])

            def place_order(self, account, body):
                assert account == "fixture-account" and body["orderType"] == "LIMIT"
                if uncertain:
                    raise TimeoutError("connection dropped after send")
                return Response(status=201, headers={"Location": "/orders/124"})

        return SchwabAdapter(Client(), "fixture-account")

    if kind == "alpaca-paper":
        from alpaca.trading.models import Order

        order = Order.model_construct(
            id="paper-order", group_id=None, order_class="simple", group_role=None,
            status="new", type="limit", time_in_force="day", legs=[],
            symbol="AAPL", asset_class="us_equity", qty="2", filled_qty="0",
            side="sell", position_intent=None, limit_price="110", stop_price=None,
            trail_price=None, trail_percent=None, submitted_at=NOW, updated_at=NOW,
            client_order_id="fixture-exit")

        class Client:
            def get_account(self):
                return SimpleNamespace(id="fixture-account", status="ACTIVE", equity="10000",
                                       buying_power="20000", daytrading_buying_power=None,
                                       cash="5000", maintenance_margin="0",
                                       options_buying_power=None,
                                       options_trading_level=0, options_approved_level=0)

            def get_all_positions(self):
                return [SimpleNamespace(symbol="AAPL", asset_class="us_equity", qty="2",
                                        side="long", avg_entry_price="100")]

            def get_orders(self, request):
                assert request.nested is True
                return [order]

            def submit_order(self, request):
                assert request.symbol == "AAPL"
                if uncertain:
                    raise TimeoutError("connection dropped after send")
                return SimpleNamespace(id="paper-next")

        return AlpacaPaperAdapter(Client())

    assert kind == "fake"
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    adapter.given_query(AccountQuery, AccountSnapshot(
        "fixture-account", "ACTIVE", BalanceSnapshot(equity=Decimal(10000)),
        (Position("AAPL", AssetType.EQUITY, PositionSide.LONG,
                  Decimal(2), Decimal(100)),)))
    adapter.given_query(OrdersQuery, (OrderGroup("fake-order", OrderClass.SIMPLE,
        OrderStatus.WORKING, (BrokerOrder("fake-order", OrderStatus.WORKING,
            OrderType.LIMIT, TimeInForce.DAY,
            (OrderLeg("AAPL", AssetType.EQUITY, OrderInstruction.SELL, Decimal(2)),),
            OrderRole.EXIT, limit_price=Decimal(110)),)),))
    adapter.given_command(SubmitOrder, MutationResult(
        MutationStatus.UNKNOWN if uncertain else MutationStatus.ACCEPTED,
        None if uncertain else "fake-next"))
    return adapter


@pytest.mark.parametrize("kind", ["schwab", "alpaca-paper", "fake"])
def test_account_and_order_reads_have_one_canonical_shape(kind):
    adapter = _adapter(kind)
    account = adapter.query(AccountQuery())
    groups = adapter.query(OrdersQuery(NOW, NOW))

    assert isinstance(account, AccountSnapshot)
    assert account.account_ref == "fixture-account"
    assert account.balances.equity == Decimal(10000)
    assert [(p.symbol, p.side, p.quantity, p.average_price) for p in account.positions] == [
        ("AAPL", PositionSide.LONG, Decimal(2), Decimal(100))]
    assert len(groups) == 1
    assert groups[0].order_class == OrderClass.SIMPLE
    order = groups[0].orders[0]
    assert (order.status, order.order_type, order.limit_price) == (
        OrderStatus.WORKING, OrderType.LIMIT, Decimal(110))
    assert [(leg.symbol, leg.quantity, leg.instruction) for leg in order.legs] == [
        ("AAPL", Decimal(2), OrderInstruction.SELL)]


@pytest.mark.parametrize("kind", ["schwab", "alpaca-paper", "fake"])
@pytest.mark.parametrize("uncertain", [False, True])
def test_submit_result_is_canonical_even_after_an_uncertain_send(kind, uncertain):
    adapter = _adapter(kind, uncertain=uncertain)
    command = SubmitOrder(SimpleOrderIntent(
        OrderLegIntent("AAPL", AssetType.EQUITY, OrderInstruction.BUY, Decimal(1)),
        OrderType.LIMIT, TimeInForce.DAY, "fixture-entry", Decimal(101)))
    result = adapter.execute(command)

    assert isinstance(result, MutationResult)
    assert result.status == (MutationStatus.UNKNOWN if uncertain else MutationStatus.ACCEPTED)
    if uncertain:
        assert result.order_id is None
    else:
        assert result.order_id

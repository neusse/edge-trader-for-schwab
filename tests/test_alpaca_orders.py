from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from companion.brokers.alpaca_orders import build_order_request
from companion.brokers.alpaca_paper import AlpacaPaperAdapter
from companion.brokers.errors import UnsupportedBrokerOperation
from companion.brokers.models import (
    AssetType, BracketOrderIntent, CancelOrder, MultiLegOrderIntent,
    MutationStatus, OcoOrderIntent, OrderInstruction, OrderLegIntent, OrderType,
    ReplaceOrder, SimpleOrderIntent, StopLossIntent, SubmitOrder, TimeInForce,
    TrailingOrderIntent,
)


def equity(instruction=OrderInstruction.BUY, quantity="2"):
    return OrderLegIntent("AAPL", AssetType.EQUITY, instruction, D(quantity))


def option(symbol, instruction, quantity="1", ratio="1"):
    return OrderLegIntent(symbol, AssetType.OPTION, instruction, D(quantity), D(ratio))


def test_equity_request_shapes_include_broker_held_protection():
    from alpaca.trading.enums import OrderClass

    entry = SimpleOrderIntent(equity(), OrderType.LIMIT, TimeInForce.DAY,
                              "entry-key", limit_price=D("100.25"))
    bracket = build_order_request(BracketOrderIntent(
        entry, D("110"), StopLossIntent(D("95"))))
    assert bracket.order_class == OrderClass.BRACKET
    assert bracket.limit_price == 100.25
    assert bracket.take_profit.limit_price == 110
    assert bracket.stop_loss.stop_price == 95
    assert bracket.client_order_id == "entry-key"

    exits = build_order_request(OcoOrderIntent(
        equity(OrderInstruction.SELL), D("110"), StopLossIntent(D("95")),
        TimeInForce.GTC, "exit-key"))
    assert exits.order_class == OrderClass.OCO
    assert exits.take_profit.limit_price == 110
    assert exits.stop_loss.stop_price == 95
    trail = build_order_request(TrailingOrderIntent(
        equity(OrderInstruction.SELL), TimeInForce.GTC, "trail-key", trail_price=D("2")))
    assert trail.trail_price == 2
    assert trail.order_class is None


def test_single_option_and_spread_requests_preserve_position_intent():
    from alpaca.trading.enums import OrderClass, PositionIntent

    buy = option("AAPL261113C00100000", OrderInstruction.BUY_TO_OPEN, "2")
    sell = option("AAPL261113C00110000", OrderInstruction.SELL_TO_OPEN, "2")
    single = build_order_request(SimpleOrderIntent(
        buy, OrderType.LIMIT, TimeInForce.DAY, "option-key", limit_price=D("1.25")))
    assert single.position_intent == PositionIntent.BUY_TO_OPEN
    spread = build_order_request(MultiLegOrderIntent(
        (buy, sell), OrderType.LIMIT, TimeInForce.DAY, "spread-key", D("0.80")))
    assert spread.order_class == OrderClass.MLEG
    assert spread.qty == 2
    assert spread.legs[0].position_intent == PositionIntent.BUY_TO_OPEN
    assert spread.legs[1].position_intent == PositionIntent.SELL_TO_OPEN
    assert spread.limit_price == 0.8


def test_ambiguous_submit_is_never_retried():
    class Client:
        def __init__(self):
            self.calls = 0

        def submit_order(self, request):
            self.calls += 1
            raise TimeoutError("response lost")

    client = Client()
    intent = SimpleOrderIntent(equity(), OrderType.MARKET, TimeInForce.DAY, "stable-key")
    result = AlpacaPaperAdapter(client).execute(SubmitOrder(intent))
    assert result.status == MutationStatus.UNKNOWN
    assert "reconcile" in result.message
    assert client.calls == 1


def test_server_error_is_an_unknown_order_outcome():
    from alpaca.common.exceptions import APIError

    class Client:
        def submit_order(self, request):
            response = SimpleNamespace(status_code=503, headers={})
            raise APIError('{"code":503,"message":"unavailable"}',
                           SimpleNamespace(response=response))

    intent = SimpleOrderIntent(equity(), OrderType.MARKET, TimeInForce.DAY, "stable-key")
    result = AlpacaPaperAdapter(Client()).execute(SubmitOrder(intent))
    assert result.status == MutationStatus.UNKNOWN


def test_submit_cancel_and_replace_use_opaque_ids_without_retries():
    class Client:
        def __init__(self):
            self.calls = []

        def submit_order(self, request):
            self.calls.append(("submit", request.client_order_id))
            return SimpleNamespace(id="uuid-order")

        def cancel_order_by_id(self, order_id):
            self.calls.append(("cancel", order_id))

        def replace_order_by_id(self, order_id, request):
            self.calls.append(("replace", order_id, request.limit_price))
            return SimpleNamespace(id="uuid-replacement")

    client = Client()
    adapter = AlpacaPaperAdapter(client)
    intent = SimpleOrderIntent(equity(), OrderType.MARKET, TimeInForce.DAY, "stable-key")
    assert adapter.execute(SubmitOrder(intent)).order_id == "uuid-order"
    assert adapter.execute(CancelOrder("uuid-order")).accepted
    assert adapter.execute(ReplaceOrder("uuid-order", limit_price=D("100"))).order_id == "uuid-replacement"
    assert client.calls == [("submit", "stable-key"), ("cancel", "uuid-order"),
                            ("replace", "uuid-order", 100)]


def test_unsupported_option_bracket_fails_before_external_call():
    class Client:
        def submit_order(self, request):
            raise AssertionError("should not call Alpaca")

    entry = SimpleOrderIntent(option("AAPL261113C00100000", OrderInstruction.BUY_TO_OPEN),
                              OrderType.LIMIT, TimeInForce.DAY, "key", limit_price=D("1"))
    with pytest.raises(UnsupportedBrokerOperation):
        AlpacaPaperAdapter(Client()).execute(SubmitOrder(BracketOrderIntent(
            entry, D("2"), StopLossIntent(D("0.50")))))

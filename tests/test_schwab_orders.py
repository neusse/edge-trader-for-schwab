from decimal import Decimal as D
from dataclasses import replace

from companion.brokers.models import (
    AssetType, BracketOrderIntent, CancelOrder, MultiLegOrderIntent, MutationStatus,
    OrderInstruction, OrderLegIntent, OrderType, ReplaceOrder, SimpleOrderIntent, StopLossIntent,
    SubmitOrder, TimeInForce, OcoOrderIntent, TrailingStopSpec, TrailingOrderIntent,
)
from companion.brokers.schwab_adapter import SchwabAdapter
from companion.brokers.schwab_orders import build_schwab_order


def equity_bracket():
    entry = SimpleOrderIntent(
        OrderLegIntent("AAPL", AssetType.EQUITY, OrderInstruction.BUY, D("2")),
        OrderType.LIMIT, TimeInForce.DAY, "client-1", limit_price=D("100"))
    return BracketOrderIntent(entry, D("110"), StopLossIntent(D("95")))


def test_canonical_schwab_bracket_matches_trigger_oco_shape():
    order = build_schwab_order(equity_bracket())
    assert order["orderStrategyType"] == "TRIGGER"
    assert order["orderType"] == "LIMIT"
    assert order["price"] == "100"
    assert order["orderLegCollection"][0]["instruction"] == "BUY"
    exits = order["childOrderStrategies"][0]
    assert exits["orderStrategyType"] == "OCO"
    target, stop = exits["childOrderStrategies"]
    assert target["price"] == "110"
    assert stop["stopPrice"] == "95"
    assert all(row["orderLegCollection"][0]["instruction"] == "SELL"
               for row in (target, stop))


def test_credit_spread_keeps_one_paired_net_order():
    intent = credit_spread()
    order = build_schwab_order(intent)
    assert order["orderType"] == "NET_CREDIT"
    assert order["price"] == "1.25"
    assert order["quantity"] == 2
    assert len(order["orderLegCollection"]) == 2
    assert {leg["instruction"] for leg in order["orderLegCollection"]} == {
        "SELL_TO_OPEN", "BUY_TO_OPEN"}


def test_canonical_gtc_translates_to_schwab_duration():
    order = build_schwab_order(replace(credit_spread(), time_in_force=TimeInForce.GTC))
    assert order["duration"] == "GOOD_TILL_CANCEL"


def test_schwab_simple_equity_preserves_fractional_quantity():
    intent = SimpleOrderIntent(OrderLegIntent("AAPL", AssetType.EQUITY, OrderInstruction.SELL, D("0.5")),
                               OrderType.MARKET, TimeInForce.DAY, "fractional-close")
    assert build_schwab_order(intent)["orderLegCollection"][0]["quantity"] == 0.5


def test_schwab_trailing_intents_preserve_quote_basis_and_oco_shape():
    leg = OrderLegIntent("AAPL", AssetType.EQUITY, OrderInstruction.SELL, D("2"))
    standalone = build_schwab_order(TrailingOrderIntent(
        leg, TimeInForce.GTC, "trail", trail_price=D("1.25"), basis="BID"))
    assert standalone["stopPriceLinkBasis"] == "BID"
    assert standalone["stopPriceOffset"] == 1.25
    linked = build_schwab_order(OcoOrderIntent(
        leg, D("110"), TrailingStopSpec(trail_percent=D("2"), basis="ASK"), TimeInForce.GTC, "oco"))
    target, stop = linked["childOrderStrategies"]
    assert target["duration"] == "GOOD_TILL_CANCEL"
    assert stop["orderType"] == "TRAILING_STOP" and stop["stopPriceLinkBasis"] == "ASK"
    assert stop["stopPriceLinkType"] == "PERCENT" and stop["stopPriceOffset"] == 2.0


def credit_spread():
    return MultiLegOrderIntent((
        OrderLegIntent("AAPL261113P00100000", AssetType.OPTION,
                       OrderInstruction.SELL_TO_OPEN, D("2")),
        OrderLegIntent("AAPL261113P00095000", AssetType.OPTION,
                       OrderInstruction.BUY_TO_OPEN, D("2")),
    ), OrderType.LIMIT, TimeInForce.DAY, "client-spread", limit_price=D("-1.25"))


def test_schwab_adapter_reports_accepted_and_ambiguous_mutations():
    class Response:
        def __init__(self, status, headers=None):
            self.status_code = status
            self.headers = headers or {}

    class Client:
        calls = 0

        def place_order(self, account_hash, body):
            self.calls += 1
            assert account_hash == "hash"
            assert body["orderStrategyType"] == "TRIGGER"
            return Response(201, {"Location": "/orders/12345"})

        def cancel_order(self, account_hash, order_id):
            assert (account_hash, order_id) == ("hash", 12345)
            return Response(204)

        def replace_order(self, account_hash, order_id, body):
            assert (account_hash, order_id) == ("hash", 12345)
            assert body["orderStrategyType"] == "TRIGGER"
            return Response(201, {"Location": "/orders/12346"})

    client = Client()
    adapter = SchwabAdapter(client, "hash")
    placed = adapter.execute(SubmitOrder(equity_bracket()))
    assert placed.status == MutationStatus.ACCEPTED
    assert placed.order_id == "12345"
    assert adapter.execute(CancelOrder("12345")).status == MutationStatus.ACCEPTED
    assert adapter.execute(CancelOrder("not-schwab-id")).status == MutationStatus.REJECTED
    replaced = adapter.execute(ReplaceOrder("12345", replacement=equity_bracket()))
    assert replaced.status == MutationStatus.ACCEPTED
    assert replaced.order_id == "12346"
    assert adapter.execute(ReplaceOrder("12345", limit_price=D("101"))).status == MutationStatus.REJECTED
    assert client.calls == 1

    class Ambiguous(Client):
        def place_order(self, account_hash, body):
            raise TimeoutError("socket timed out after send")

    result = SchwabAdapter(Ambiguous(), "hash").execute(SubmitOrder(equity_bracket()))
    assert result.status == MutationStatus.UNKNOWN
    assert "socket" not in result.message


def test_schwab_spread_requires_verified_preview_before_mutation():
    class Response:
        status_code = 200
        headers = {"Location": "/orders/888"}

        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    class Client:
        placed = False
        preview_body = {"orderValidationResult": {"rejects": []}}

        def preview_order(self, account_hash, order):
            assert account_hash == "hash"
            assert order["orderType"] == "NET_CREDIT"
            return Response(self.preview_body)

        def place_order(self, account_hash, order):
            self.placed = True
            return Response({})

    client = Client()
    adapter = SchwabAdapter(client, "hash")
    assert adapter.execute(SubmitOrder(credit_spread())).status == MutationStatus.ACCEPTED
    assert client.placed
    client.placed = False
    client.preview_body = {"orderValidationResult": {"rejects": [{"message": "no"}]}}
    assert adapter.execute(SubmitOrder(credit_spread())).status == MutationStatus.REJECTED
    assert not client.placed
    client.preview_body = {}
    assert adapter.execute(SubmitOrder(credit_spread())).status == MutationStatus.REJECTED
    assert not client.placed

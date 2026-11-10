from datetime import date, datetime, timezone
from decimal import Decimal

from companion.brokers.models import (
    AccountQuery, AssetType, FillsQuery, MarketSessionsQuery, OrderClass, OrderInstruction, OrderRole,
    OrderStatus, OrdersQuery, PositionSide, QuotesQuery,
)
from companion.brokers.schwab_adapter import SchwabAdapter, normalize_account, normalize_fills, normalize_order


class Response:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


def test_schwab_account_keeps_unknown_basis_and_short_direction():
    raw = {"securitiesAccount": {"currentBalances": {"liquidationValue": 10000},
                                 "positions": [
                                     {"instrument": {"symbol": "AAPL", "assetType": "EQUITY"},
                                      "longQuantity": 0, "shortQuantity": 3},
                                     {"instrument": {"symbol": "MSFT", "assetType": "EQUITY"},
                                      "longQuantity": 2, "shortQuantity": 0, "averagePrice": 100},
                                 ]}}
    account = normalize_account(raw, "opaque-hash")
    assert account.account_ref == "opaque-hash"
    assert account.balances.liquidation_value == Decimal(10000)
    assert account.positions[0].side == PositionSide.SHORT
    assert account.positions[0].quantity == Decimal(3)
    assert account.positions[0].average_price is None
    assert account.positions[1].average_price == Decimal(100)


def test_schwab_bracket_tree_maps_entry_and_exit_roles():
    def leg(instruction):
        return {"instruction": instruction, "quantity": 2,
                "instrument": {"symbol": "AAPL", "assetType": "EQUITY"}}

    raw = {"orderId": 100, "orderStrategyType": "TRIGGER", "status": "FILLED",
           "orderType": "LIMIT", "duration": "DAY", "orderLegCollection": [leg("BUY")],
           "childOrderStrategies": [{"orderId": 101, "orderStrategyType": "OCO",
                                     "childOrderStrategies": [
                                         {"orderId": 102, "status": "WORKING", "orderType": "STOP",
                                          "stopPrice": 95, "orderLegCollection": [leg("SELL")]},
                                         {"orderId": 103, "status": "WORKING", "orderType": "LIMIT",
                                          "price": 110, "orderLegCollection": [leg("SELL")]},
                                     ]}]}
    group = normalize_order(raw)
    assert group.order_class == OrderClass.BRACKET
    assert [order.role for order in group.orders] == [OrderRole.ENTRY, OrderRole.STOP, OrderRole.TARGET]
    assert group.orders[1].parent_id == "100"
    assert group.orders[1].stop_price == Decimal(95)
    assert group.orders[2].legs[0].instruction == OrderInstruction.SELL


def test_schwab_adapter_returns_only_canonical_models():
    class Client:
        def linked_accounts(self):
            return Response([{"hashValue": "hash"}])

        def account_details(self, account_hash, fields):
            assert account_hash == "hash"
            assert fields == "positions"
            return Response({"securitiesAccount": {"currentBalances": {}, "positions": []}})

        def account_orders(self, account_hash, start, end):
            assert account_hash == "hash"
            return Response([{"orderId": 5, "orderStrategyType": "SINGLE", "status": "WORKING",
                              "orderType": "LIMIT", "orderLegCollection": [
                                  {"instruction": "BUY", "quantity": 1,
                                   "instrument": {"symbol": "MSFT", "assetType": "EQUITY"}}]}])

    adapter = SchwabAdapter(Client())
    assert adapter.query(AccountQuery()).account_ref == "hash"
    now = datetime.now(timezone.utc)
    groups = adapter.query(OrdersQuery(now, now))
    assert groups[0].orders[0].legs[0].asset_type == AssetType.EQUITY
    assert groups[0].status == OrderStatus.WORKING


def test_schwab_quote_and_early_close_are_normalized():
    class Client:
        def quotes(self, symbols):
            return Response({"AAPL": {"realtime": True, "quote": {
                "bidPrice": 100, "askPrice": 100.2, "mark": 100.1,
                "lastPrice": 100.05, "quoteTime": 1795550000000}}})

        def market_hours(self, markets, date):
            return Response({"equity": {"EQ": {"isOpen": True, "sessionHours": {
                "regularMarket": [{"start": "2026-11-27T09:30:00-05:00",
                                   "end": "2026-11-27T13:00:00-05:00"}]}}}})

    adapter = SchwabAdapter(Client(), account_hash="hash")
    quote = adapter.query(QuotesQuery(("AAPL",), AssetType.EQUITY))["AAPL"]
    assert quote.mark == Decimal("100.1")
    assert quote.realtime is True
    session = adapter.query(MarketSessionsQuery(date(2026, 11, 27), date(2026, 11, 27)))[0]
    assert session.is_open
    assert session.closes_at.hour == 13


def test_schwab_trade_items_keep_opaque_fill_ids_and_fees():
    trade = {"activityId": 123, "orderId": 456, "type": "TRADE", "status": "VALID",
             "time": "2026-11-10T15:30:00+00:00", "transferItems": [
                 {"amount": 2, "price": 100, "positionEffect": "OPENING",
                  "instrument": {"symbol": "AAPL", "assetType": "EQUITY"}},
                 {"amount": -1, "price": 1.25, "positionEffect": "CLOSING",
                  "instrument": {"symbol": "AAPL  261113C00100000", "assetType": "OPTION"}},
                 {"cost": -0.65, "instrument": {"assetType": "CURRENCY"}},
             ]}
    fills = normalize_fills([trade, trade])
    assert len(fills) == 2
    assert fills[0].activity_id == "123:0"
    assert fills[0].order_id == "456"
    assert fills[0].instruction == OrderInstruction.BUY_TO_OPEN
    assert fills[0].fees == Decimal("-0.65")
    assert fills[1].instruction == OrderInstruction.SELL_TO_CLOSE
    assert fills[1].fees == 0


def test_schwab_fill_query_bisects_when_response_hits_cap():
    class Client:
        calls = 0

        def transactions(self, account_hash, start, end, kind):
            self.calls += 1
            return Response([{}] * 3000 if self.calls == 1 else [])

    client = Client()
    adapter = SchwabAdapter(client, account_hash="hash")
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert adapter.query(FillsQuery(start, datetime(2026, 11, 10, tzinfo=timezone.utc))) == ()
    assert client.calls == 3

from types import SimpleNamespace

from companion.brokers.alpaca_paper import AlpacaPaperAdapter
from companion.brokers.models import AccountQuery, AssetType, ClientOrderQuery, PositionSide


def test_paper_account_read_normalizes_positions_without_sdk_objects():
    class Client:
        def get_account(self):
            return SimpleNamespace(id="account-uuid", status="ACTIVE", equity="10000.00",
                                   buying_power="20000", daytrading_buying_power=None,
                                   cash="5000", maintenance_margin="200")

        def get_all_positions(self):
            return [SimpleNamespace(symbol="AAPL", asset_class="us_equity", qty="-3",
                                    side="short", avg_entry_price="145.50")]

    snapshot = AlpacaPaperAdapter(Client()).query(AccountQuery())
    assert snapshot.account_ref == "account-uuid"
    assert snapshot.balances.equity == 10000
    assert snapshot.positions[0].asset_type == AssetType.EQUITY
    assert snapshot.positions[0].side == PositionSide.SHORT
    assert snapshot.positions[0].quantity == 3


def test_default_paper_runtime_never_routes_a_trade_to_schwab(monkeypatch):
    from fastapi.testclient import TestClient
    from companion import app as appmod

    monkeypatch.setattr(appmod, "BROKER_KEY", "alpaca-paper")
    monkeypatch.setattr(appmod, "DEMO", False)
    client = TestClient(appmod.app, base_url="http://127.0.0.1:8770")
    state = client.get("/api/state").json()
    assert state["broker"]["key"] == "alpaca-paper"
    assert state["broker"]["mode"] == "paper"
    response = client.post("/api/ticket/submit", json={}, headers={"X-Companion": "1"})
    assert response.json()["ok"] is False
    assert "no order was sent" in response.json()["error"]


def test_nested_multi_leg_order_keeps_both_option_legs():
    from alpaca.trading.models import Order
    from companion.brokers.alpaca_paper import _order_group
    from companion.brokers.models import OrderClass, OrderInstruction

    def order(**kwargs):
        return Order.model_construct(**kwargs)

    first = order(id="leg-1", symbol="AAPL260116C00200000", asset_class="us_option",
                  qty="1", filled_qty="0", side="buy", position_intent="buy_to_open")
    second = order(id="leg-2", symbol="AAPL260116C00210000", asset_class="us_option",
                   qty="1", filled_qty="0", side="sell", position_intent="sell_to_open")
    root = order(id="order-1", group_id=None, order_class="mleg", group_role=None,
                 status="new", type="limit", time_in_force="day", legs=[first, second],
                 limit_price="1.25", stop_price=None, trail_price=None, trail_percent=None,
                 submitted_at=None, updated_at=None, client_order_id="test")
    normalized = _order_group(root)
    assert normalized.order_class == OrderClass.MULTI_LEG
    assert len(normalized.orders) == 1
    assert [leg.instruction for leg in normalized.orders[0].legs] == [
        OrderInstruction.BUY_TO_OPEN, OrderInstruction.SELL_TO_OPEN]


def test_client_order_lookup_fetches_nested_group():
    from alpaca.trading.models import Order

    raw = Order.model_construct(
        id="paper-order-uuid", group_id=None, order_class="simple", group_role=None,
        status="new", type="limit", time_in_force="day", legs=[],
        symbol="AAPL", asset_class="us_equity", qty="1", filled_qty="0",
        side="buy", position_intent=None, limit_price="100", stop_price=None,
        trail_price=None, trail_percent=None, submitted_at=None, updated_at=None,
        client_order_id="stable-client-id")

    class Client:
        def get_order_by_client_id(self, client_id):
            assert client_id == "stable-client-id"
            return raw

        def get_order_by_id(self, order_id, request):
            assert order_id == "paper-order-uuid"
            assert request.nested is True
            return raw

    group = AlpacaPaperAdapter(Client()).query(ClientOrderQuery("stable-client-id"))
    assert group.orders[0].order_id == "paper-order-uuid"
    assert group.orders[0].client_order_id == "stable-client-id"


def test_fill_read_normalizes_opaque_ids_and_option_side():
    from datetime import datetime, timezone
    from companion.brokers.models import FillsQuery, OrderInstruction

    class Client:
        def get(self, path, params):
            assert path == "/account/activities/FILL"
            assert params["page_size"] == 100
            return [{"id": "2026::uuid", "order_id": "order-uuid",
                     "symbol": "AAPL260116C00200000", "side": "buy_to_open", "qty": "2",
                     "price": "1.25", "transaction_time": "2026-11-10T12:00:00Z"}]

    now = datetime.now(timezone.utc)
    fills = AlpacaPaperAdapter(Client()).query(FillsQuery(now, now))
    assert fills[0].activity_id == "2026::uuid"
    assert fills[0].order_id == "order-uuid"
    assert fills[0].asset_type == AssetType.OPTION
    assert fills[0].instruction == OrderInstruction.BUY_TO_OPEN


def test_broker_selection_persists_without_reusing_current_runtime(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from companion import app as appmod
    from companion.brokers.configuration import BrokerSelection

    selection = BrokerSelection(tmp_path / "broker-selection.json")
    monkeypatch.setattr(appmod, "BrokerSelection", lambda: selection)
    monkeypatch.setattr(appmod, "BROKER_KEY", "alpaca-paper")
    monkeypatch.setattr(appmod, "DEMO", False)
    client = TestClient(appmod.app, base_url="http://127.0.0.1:8770")
    response = client.post("/api/brokers/select", json={"broker": "schwab"},
                           headers={"X-Companion": "1"})
    assert response.json() == {"ok": True, "active": "alpaca-paper",
                               "selected": "schwab", "restart_required": True}
    assert selection.get() == "schwab"
    state = client.get("/api/state").json()
    assert state["broker"]["key"] == "alpaca-paper"
    assert state["pending_broker"] == "schwab"
    assert appmod.trader.status()["armed"] is False


def test_paper_poller_builds_read_only_snapshot_from_canonical_data(monkeypatch, tmp_path):
    from decimal import Decimal
    from companion.brokers.bootstrap import AlpacaBootstrapPoller
    from companion.brokers.models import (
        AccountSnapshot, BalanceSnapshot, BrokerOrder, OrderClass, OrderGroup, OrderLeg,
        OrderRole, OrderStatus, OrderType, OrdersQuery, OrderInstruction, Position,
        TimeInForce,
    )
    from companion.store import Store

    original = Store.for_broker
    monkeypatch.setattr(Store, "for_broker", classmethod(
        lambda cls, broker, account: original(broker, account, tmp_path)))
    account = AccountSnapshot("paper-account", "ACTIVE", BalanceSnapshot(
        equity=Decimal("10000"), available_funds=Decimal("5000"), buying_power=Decimal("20000")),
        (Position("AAPL", AssetType.EQUITY, PositionSide.LONG,
                  Decimal("2"), Decimal("150")),))
    leg = OrderLeg("AAPL", AssetType.EQUITY, OrderInstruction.BUY, Decimal("1"))
    order = BrokerOrder("order-uuid", OrderStatus.WORKING, OrderType.LIMIT,
                        TimeInForce.DAY, (leg,), OrderRole.ENTRY)

    class Adapter:
        def query(self, query):
            return account if isinstance(query, AccountQuery) else (
                OrderGroup("order-uuid", OrderClass.SIMPLE, OrderStatus.WORKING, (order,)),)

    poller = AlpacaBootstrapPoller(Adapter())
    from companion.trading import Trader
    poller.trader = Trader(poller.store)
    try:
        poller.poll_once()
        state = poller.snapshot()
        assert state["broker_ready"] is True
        assert state["alpaca"]["positions"][0]["symbol"] == "AAPL"
        assert state["alpaca"]["orders"][0]["id"] == "order-uuid"
        assert state["orders"] == []  # legacy trading controls receive no actionable order
        assert poller.store.scope == ("alpaca-paper", "paper-account")
        assert poller.trader.store is poller.store
        assert poller.trader.status()["armed"] is False
    finally:
        poller.stop()


def test_option_contract_query_paginates_and_normalizes():
    from datetime import date, datetime, timezone
    from decimal import Decimal as D
    from alpaca.trading.models import OptionContract as RawContract
    from companion.brokers.models import OptionChainQuery, OptionType

    class Client:
        def get_option_contracts(self, request):
            number = 2 if request.page_token else 1
            raw = RawContract.model_construct(
                symbol=f"AAPL261113C00{number}000000", underlying_symbol="AAPL",
                expiration_date=date(2026, 11, 13), strike_price=str(number * 100),
                type="call", multiplier="100", tradable=True, open_interest="10")
            return SimpleNamespace(option_contracts=[raw],
                                   next_page_token=None if number == 2 else "next")

    def snapshots(request):
        assert request.feed.value == "indicative"
        symbol = "AAPL261113C001000000"
        return {symbol: SimpleNamespace(
            latest_quote=SimpleNamespace(bid_price=1.0, ask_price=1.2,
                                         timestamp=datetime.now(timezone.utc)),
            latest_trade=None, implied_volatility=0.3,
            greeks=SimpleNamespace(delta=0.5, gamma=0.1, theta=-0.2, vega=0.4, rho=0.01))}

    chain = AlpacaPaperAdapter(Client(), option_data=SimpleNamespace(
        get_option_chain=snapshots)).query(
        OptionChainQuery("AAPL", date(2026, 11, 13), OptionType.CALL))
    assert len(chain.contracts) == 2
    assert all(contract.option_type == OptionType.CALL for contract in chain.contracts)
    assert chain.contracts[0].quote.mark == D("1.1")
    assert chain.contracts[0].quote.greeks.delta == D("0.5")
    assert chain.contracts[0].implied_volatility == D("0.3")

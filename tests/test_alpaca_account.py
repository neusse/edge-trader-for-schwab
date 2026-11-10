from types import SimpleNamespace

from companion.brokers.alpaca_paper import AlpacaPaperAdapter
from companion.brokers.models import AccountQuery, AssetType, PositionSide


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

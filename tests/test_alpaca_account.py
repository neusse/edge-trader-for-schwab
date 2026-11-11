from types import SimpleNamespace

import pytest

from companion.brokers.alpaca_paper import AlpacaPaperAdapter
from companion.brokers.models import AccountQuery, AssetType, ClientOrderQuery, OrderRole, PositionSide


def test_runtime_factory_selects_exact_broker_without_fallback(monkeypatch):
    from companion import app as appmod

    assert set(appmod.POLLERS) == set(appmod.BROKERS)
    created = []
    for key in ("alpaca-paper", "schwab", "demo"):
        monkeypatch.setitem(appmod.POLLERS, key,
                            lambda key=key: created.append(key) or object())
        appmod.make_poller(key)
    assert created == ["alpaca-paper", "schwab", "demo"]
    with pytest.raises(ValueError, match="unknown broker runtime"):
        appmod.make_poller("alpaca-live")


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


def test_paper_clock_is_canonical_and_account_bound():
    from datetime import datetime, timedelta, timezone
    from companion.brokers.models import MarketClockQuery

    now = datetime(2026, 11, 10, 16, tzinfo=timezone.utc)

    class Client:
        def get_clock(self):
            return SimpleNamespace(timestamp=now, is_open=True,
                                   next_open=now + timedelta(days=1),
                                   next_close=now + timedelta(hours=5))

    clock = AlpacaPaperAdapter(Client()).query(MarketClockQuery())
    assert clock.is_open is True
    assert clock.observed_at == now
    assert clock.next_close == now + timedelta(hours=5)


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


def test_oco_root_without_group_role_is_still_a_target():
    from alpaca.trading.models import Order
    from companion.brokers.alpaca_paper import _order_group

    stop = Order.model_construct(
        id="stop-id", group_id="oco-id", order_class="oco", group_role="stop_loss",
        status="new", type="stop", time_in_force="gtc", legs=[],
        symbol="AAPL", asset_class="us_equity", qty="2", filled_qty="0",
        side="sell", position_intent=None, limit_price=None, stop_price="95",
        trail_price=None, trail_percent=None, submitted_at=None, updated_at=None,
        client_order_id="stop-client")
    target = Order.model_construct(
        id="target-id", group_id="oco-id", order_class="oco", group_role=None,
        status="new", type="limit", time_in_force="gtc", legs=[stop],
        symbol="AAPL", asset_class="us_equity", qty="2", filled_qty="0",
        side="sell", position_intent=None, limit_price="110", stop_price=None,
        trail_price=None, trail_percent=None, submitted_at=None, updated_at=None,
        client_order_id="oco-client")
    group = _order_group(target)
    assert [order.role for order in group.orders] == [OrderRole.TARGET, OrderRole.STOP]


def test_order_history_pages_with_boundary_overlap_and_uuid_deduplication():
    from datetime import datetime, timedelta, timezone
    from alpaca.trading.models import Order
    from companion.brokers.models import OrdersQuery

    now = datetime(2026, 11, 10, 16, tzinfo=timezone.utc)
    raw = [Order.model_construct(
        id=f"paper-{i}", group_id=None, order_class="simple", group_role=None,
        status="new", type="limit", time_in_force="day", legs=[],
        symbol="AAPL", asset_class="us_equity", qty="1", filled_qty="0",
        side="buy", position_intent=None, limit_price="100", stop_price=None,
        trail_price=None, trail_percent=None, submitted_at=now - timedelta(hours=1),
        updated_at=None, client_order_id=f"client-{i}") for i in range(501)]

    class Client:
        calls = 0

        def get_orders(self, request):
            self.calls += 1
            assert request.nested is True
            assert request.direction.value == "asc"
            return raw[:500] if self.calls == 1 else raw[499:]

    client = Client()
    groups = AlpacaPaperAdapter(client).query(OrdersQuery(now - timedelta(days=1), now))
    assert client.calls == 2
    assert len(groups) == 501
    assert len({group.group_id for group in groups}) == 501


def test_open_order_read_preserves_unrecognized_broker_status_as_unknown():
    from datetime import datetime, timedelta, timezone
    from alpaca.trading.models import Order
    from companion.brokers.models import OrdersQuery, OrderStatus

    now = datetime.now(timezone.utc)
    raw = Order.model_construct(
        id="unrecognized-order", group_id=None, order_class="simple", group_role=None,
        status="new_broker_status", type="limit", time_in_force="day", legs=[],
        symbol="AAPL", asset_class="us_equity", qty="1", filled_qty="0",
        side="sell", position_intent=None, limit_price="110", stop_price=None,
        trail_price=None, trail_percent=None, submitted_at=now, updated_at=None,
        client_order_id="external")
    class Client:
        def get_orders(self, request):
            assert request.status.value == "open"
            return [raw]
    active = frozenset((OrderStatus.PENDING, OrderStatus.WORKING,
                        OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN))
    groups = AlpacaPaperAdapter(Client()).query(OrdersQuery(now - timedelta(days=1), now,
                                                             statuses=active))
    assert groups[0].orders[0].status == OrderStatus.UNKNOWN


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


def test_fill_read_paginates_without_losing_opaque_activities():
    from datetime import datetime, timezone
    from companion.brokers.models import FillsQuery

    rows = [{"id": f"activity::{i}", "order_id": "order-uuid", "symbol": "AAPL",
             "side": "buy", "qty": "1", "price": "100",
             "transaction_time": "2026-11-10T12:00:00Z"} for i in range(101)]

    class Client:
        calls = 0

        def get(self, path, params):
            self.calls += 1
            assert path == "/account/activities/FILL"
            if self.calls == 1:
                assert "page_token" not in params
                return rows[:100]
            assert params["page_token"] == "activity::99"
            return rows[100:]

    client = Client()
    now = datetime(2026, 11, 10, tzinfo=timezone.utc)
    fills = AlpacaPaperAdapter(client).query(FillsQuery(now, now))
    assert client.calls == 2
    assert len(fills) == 101
    assert len({fill.activity_id for fill in fills}) == 101


def test_broker_selection_replaces_runtime_and_clears_cached_state(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from companion import app as appmod
    from companion.brokers.configuration import BrokerSelection
    from companion.store import Store
    from companion.trading import Trader

    class RuntimePoller:
        def __init__(self, name):
            self.store = Store(tmp_path / f"{name}.db")
            self.trader = None
            self.terminal = set()
            self.stopped = False
            self.started = False
        def snapshot(self): return {"booting": True, "status": "fresh runtime"}
        def start(self): self.started = True
        def stop(self): self.stopped = True
        def _merged_orders(self): return []
        def note_external_request(self, retry_after=None): pass
        def cooldown_left(self): return 0.0
    old, new = RuntimePoller("old"), RuntimePoller("new")

    selection = BrokerSelection(tmp_path / "broker-selection.json")
    monkeypatch.setattr(appmod, "BrokerSelection", lambda: selection)
    monkeypatch.setattr(appmod, "BROKER_KEY", "alpaca-paper")
    monkeypatch.setattr(appmod, "DEMO", False)
    monkeypatch.setattr(appmod, "poller", old)
    monkeypatch.setattr(appmod, "trader", Trader(old.store))
    monkeypatch.setattr(appmod, "make_poller", lambda key: new)
    monkeypatch.setattr(appmod, "_feature_routes", None)
    client = TestClient(appmod.app, base_url="http://127.0.0.1:8770")
    response = client.post("/api/brokers/select", json={"broker": "schwab"},
                           headers={"X-Companion": "1"})
    assert response.json() == {"ok": True, "active": "schwab",
                               "selected": "schwab", "restart_required": False}
    assert selection.get() == "schwab"
    state = client.get("/api/state").json()
    assert state["broker"]["key"] == "schwab"
    assert state["pending_broker"] is None
    assert state["status"] == "fresh runtime"
    assert old.stopped and new.started
    assert appmod.trader.status()["armed"] is False
    old.store.close()
    new.store.close()


def test_broker_metadata_distinguishes_api_capability_from_enabled_workflow():
    from companion.app import broker_info

    paper = broker_info("alpaca-paper")
    assert "option-trading" in paper["capabilities"]
    assert paper["workflows"] == ("equity-bracket", "equity-oco", "equity-close", "equity-partial")
    assert "option-bracket" in paper["unsupported"]
    assert "Paper" in paper["help"]
    demo = broker_info("demo")
    assert demo["workflows"] == ()
    assert "No broker orders" in demo["help"]


def test_demo_override_never_shows_or_persists_another_broker(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from companion import app as appmod
    from companion.brokers.configuration import BrokerSelection

    selection = BrokerSelection(tmp_path / "broker-selection.json")
    monkeypatch.setattr(appmod, "BrokerSelection", lambda: selection)
    monkeypatch.setattr(appmod, "DEMO_OVERRIDE", True)
    monkeypatch.setattr(appmod, "BROKER_KEY", "demo")
    monkeypatch.setattr(appmod, "DEMO", True)
    client = TestClient(appmod.app, base_url="http://127.0.0.1:8770")
    assert client.get("/api/state").json()["pending_broker"] is None
    assert client.get("/api/brokers").json()["selected"] == "demo"
    result = client.post("/api/brokers/select", json={"broker": "alpaca-paper"},
                         headers={"X-Companion": "1"}).json()
    assert result["ok"] is False
    assert not selection.path.exists()


def test_paper_poller_builds_read_only_snapshot_from_canonical_data(monkeypatch, tmp_path):
    from decimal import Decimal
    from companion.brokers.bootstrap import AlpacaBootstrapPoller
    from companion.brokers.models import (
        AccountSnapshot, BalanceSnapshot, BrokerOrder, OrderClass, OrderGroup, OrderLeg,
        OrderRole, OrderStatus, OrderType, OrdersQuery, OrderInstruction, Position,
        FillsQuery, MarketClock, MarketClockQuery, MarketSessionsQuery, QuotesQuery,
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
        from companion.brokers.capabilities import ALPACA_PAPER
        identity = ALPACA_PAPER

        def query(self, query):
            if isinstance(query, AccountQuery):
                return account
            if isinstance(query, OrdersQuery):
                return (OrderGroup("order-uuid", OrderClass.SIMPLE,
                                   OrderStatus.WORKING, (order,)),)
            if isinstance(query, FillsQuery):
                return ()
            if isinstance(query, MarketClockQuery):
                from datetime import datetime, timedelta, timezone
                now = datetime.now(timezone.utc)
                return MarketClock(now, False, now + timedelta(days=1),
                                   now + timedelta(days=1, hours=6))
            if isinstance(query, MarketSessionsQuery):
                return ()
            if isinstance(query, QuotesQuery):
                return {}
            raise AssertionError(type(query))

    poller = AlpacaBootstrapPoller(Adapter())
    from companion.trading import Trader
    poller.trader = Trader(poller.store)
    try:
        poller.poll_once()
        state = poller.snapshot()
        assert state["broker_ready"] is True
        assert state["alpaca"]["positions"][0]["symbol"] == "AAPL"
        assert state["alpaca"]["orders"][0]["id"] == "order-uuid"
        assert state["positions"][0]["symbol"] == "AAPL"
        assert state["orders"][0]["order_id"] == "order-uuid"
        assert poller.store.scope == ("alpaca-paper", "paper-account")
        assert poller.trader.store is poller.store
        assert poller.trader.status()["armed"] is False
    finally:
        poller.stop()


def test_paper_read_rate_limit_keeps_snapshot_but_marks_it_stale(monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal
    from companion.brokers.bootstrap import AlpacaBootstrapPoller
    from companion.brokers.capabilities import ALPACA_PAPER
    from companion.brokers.models import (
        AccountSnapshot, BalanceSnapshot, FillsQuery, MarketClock, MarketClockQuery,
        MarketSessionsQuery, OrdersQuery, Position, Quote, QuotesQuery,
    )
    from companion.store import Store

    original = Store.for_broker
    monkeypatch.setattr(Store, "for_broker", classmethod(
        lambda cls, broker, account: original(broker, account, tmp_path)))
    now = datetime.now(timezone.utc)
    account = AccountSnapshot("paper-account", "ACTIVE", BalanceSnapshot(),
                              (Position("AAPL", AssetType.EQUITY, PositionSide.LONG,
                                        Decimal("1"), Decimal("100")),))

    class RateLimit(Exception):
        status_code = 429

    class Adapter:
        identity = ALPACA_PAPER
        fail = False
        calls = 0

        def query(self, query):
            self.calls += 1
            if isinstance(query, AccountQuery):
                if self.fail:
                    raise RateLimit()
                return account
            if isinstance(query, OrdersQuery):
                return ()
            if isinstance(query, MarketClockQuery):
                return MarketClock(now, True, now + timedelta(days=1), now + timedelta(hours=5))
            if isinstance(query, MarketSessionsQuery):
                return ()
            if isinstance(query, FillsQuery):
                return ()
            if isinstance(query, QuotesQuery):
                return {"AAPL": Quote("AAPL", Decimal("100"), Decimal("101"), None,
                                       Decimal("100.5"), now, True, "Alpaca IEX")}
            raise AssertionError(type(query))

    adapter = Adapter()
    poller = AlpacaBootstrapPoller(adapter)
    try:
        poller.poll_once()
        assert poller.snapshot()["broker_ready"] is True
        adapter.fail = True
        poller.poll_once()
        stale = poller.snapshot()
        assert stale["broker_ready"] is False
        assert stale["broker_health"]["state"] == "rate-limited"
        assert stale["positions"][0]["protection"] == "UNKNOWN"
        assert stale["summary"]["open_pnl"]["complete"] is False
        calls = adapter.calls
        poller.poll_once()
        assert adapter.calls == calls
        assert poller.cooldown_left() > 0
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

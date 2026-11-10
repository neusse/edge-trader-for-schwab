from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from companion.brokers.bootstrap import AlpacaBootstrapPoller
from companion.brokers.capabilities import ALPACA_PAPER
from companion.brokers.fake import FakeBrokerAdapter
from companion.brokers.models import (
    AccountQuery, AccountSnapshot, BalanceSnapshot, MarketSession,
    MarketSessionsQuery, MutationResult, MutationStatus, OrdersQuery, OrderQuery,
    OrderClass, OrderGroup, BrokerOrder, OrderLeg, OrderRole, OrderStatus,
    OrderInstruction, OrderType, TimeInForce, AssetType, CancelOrder, ClientOrderQuery,
    Position, PositionSide,
    ReplaceOrder, Quote, QuotesQuery,
    SubmitOrder,
)
from companion.brokers.order_service import BrokerOrderService
from companion.brokers.paper_equity import entry_plan, oco_plan
from companion.store import Store
from companion.trading import Trader


def quote(now, symbol="AAPL"):
    return Quote(symbol, Decimal("99.95"), Decimal("100.05"), Decimal("100"),
                 Decimal("100"), now, True, "Alpaca IEX")


def entry(**changes):
    body = {"symbol": "AAPL", "side": "long", "quantity": "2", "order_type": "limit",
            "limit_price": "100", "target_price": "110", "stop_price": "95",
            "request_id": str(uuid4())}
    body.update(changes)
    return body


def test_entry_plan_checks_bracket_direction_and_quote_freshness():
    now = datetime.now(timezone.utc)
    plan = entry_plan(entry(), quote(now), now)
    assert plan.entry.client_order_id.startswith("et-")
    assert plan.entry.leg.quantity == 2
    assert plan.take_profit_price == 110
    short = entry_plan(entry(side="short", target_price="90", stop_price="105"), quote(now), now)
    assert short.entry.leg.instruction.value == "sell-short"
    with pytest.raises(ValueError, match="stop < entry < target"):
        entry_plan(entry(stop_price="101"), quote(now), now)
    with pytest.raises(ValueError, match="stale"):
        entry_plan(entry(), quote(now - timedelta(seconds=31)), now)
    with pytest.raises(ValueError, match="whole shares"):
        entry_plan(entry(quantity="1.5"), quote(now), now)
    with pytest.raises(ValueError, match="positive number"):
        entry_plan(entry(target_price="NaN"), quote(now), now)


def test_oco_plan_closes_the_exact_whole_equity_position():
    now = datetime.now(timezone.utc)
    body = {"symbol": "AAPL", "target_price": "110", "stop_price": "95",
            "request_id": str(uuid4())}
    long = Position("AAPL", AssetType.EQUITY, PositionSide.LONG, Decimal(2), Decimal(100))
    plan = oco_plan(body, long, quote(now), now)
    assert plan.leg.quantity == 2 and plan.leg.instruction == OrderInstruction.SELL
    assert plan.client_order_id.startswith("et-oco-")
    short = Position("AAPL", AssetType.EQUITY, PositionSide.SHORT, Decimal(2), Decimal(100))
    cover = oco_plan({**body, "target_price": "90", "stop_price": "105"},
                     short, quote(now), now)
    assert cover.leg.instruction == OrderInstruction.BUY_TO_COVER
    with pytest.raises(ValueError, match="current mark"):
        oco_plan(body, short, quote(now), now)
    fractional = Position("AAPL", AssetType.EQUITY, PositionSide.LONG,
                          Decimal("1.5"), Decimal(100))
    with pytest.raises(ValueError, match="whole shares"):
        oco_plan(body, fractional, quote(now), now)


def test_paper_entry_route_arms_previews_sends_once_and_disarms_on_selection(monkeypatch, tmp_path):
    import companion.app as appmod

    now = datetime.now(timezone.utc)
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    adapter.given_query(QuotesQuery, *({"AAPL": quote(now)} for _ in range(3)))
    account = AccountSnapshot("paper-account", "ACTIVE", BalanceSnapshot())
    adapter.given_query(AccountQuery, account, account, account, account)
    adapter.given_query(OrdersQuery, (), ())
    session_day = now.astimezone(timezone(timedelta(hours=-5))).date()
    adapter.given_query(MarketSessionsQuery, (MarketSession(
        session_day, now - timedelta(hours=1), now + timedelta(hours=1), True),))
    adapter.given_command(SubmitOrder, MutationResult(MutationStatus.ACCEPTED, "order-1"))
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    poller = AlpacaBootstrapPoller(adapter=adapter)
    poller.order_service = BrokerOrderService(adapter, store, "paper-account", clock=lambda: now)
    poller._state = {"broker_ready": True, "alpaca": {"account_status": "ACTIVE",
                     "updated_at": now.isoformat(), "positions": [], "orders": []}}
    class Selection:
        selected = "alpaca-paper"
        def get(self): return self.selected
        def set(self, key): self.selected = key
    selected = Selection()
    class IdlePoller:
        def __init__(self):
            self.store = Store(tmp_path / "next.db")
            self.trader = None
            self.terminal = set()
            self.started = False
        def start(self): self.started = True
        def stop(self): self.store.close()
        def _merged_orders(self): return []
        def note_external_request(self, retry_after=None): pass
        def cooldown_left(self): return 0.0
    next_poller = IdlePoller()
    monkeypatch.setattr(appmod, "BROKER_KEY", "alpaca-paper")
    monkeypatch.setattr(appmod, "poller", poller)
    monkeypatch.setattr(appmod, "trader", Trader(poller.store))
    monkeypatch.setattr(appmod, "BrokerSelection", lambda: selected)
    monkeypatch.setattr(appmod, "make_poller", lambda key: next_poller)
    monkeypatch.setattr(appmod, "_feature_routes", None)
    client = TestClient(appmod.app, base_url=f"http://127.0.0.1:{appmod.PORT}")
    headers = {"X-Companion": "1"}
    body = entry()
    try:
        assert client.post("/api/paper/entry/preview", json=body, headers=headers).json()["ok"]
        assert client.post("/api/paper/entry/submit", json=body, headers=headers).json()["status"] == "rejected"
        assert client.post("/api/paper/arm", json={"armed": True}, headers=headers).json()["armed"]
        result = client.post("/api/paper/entry/submit", json=body, headers=headers).json()
        assert result["status"] == "accepted" and result["order_id"] == "order-1"
        assert len(adapter.commands) == 1
        switched = client.post("/api/brokers/select", json={"broker": "schwab"}, headers=headers).json()
        assert switched == {"ok": True, "active": "schwab", "selected": "schwab", "restart_required": False}
        assert next_poller.started
        assert not poller.order_service.armed
        assert client.post("/api/paper/entry/submit", json=body, headers=headers).status_code == 409
    finally:
        store.close()
        poller.store.close()
        next_poller.store.close()


def test_paper_cancel_route_rechecks_unfilled_entry_and_selected_broker(monkeypatch, tmp_path):
    import companion.app as appmod

    now = datetime.now(timezone.utc)
    order_id = str(uuid4())
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    def group(filled, status_detail=None):
        order = BrokerOrder(order_id, OrderStatus.WORKING, OrderType.LIMIT,
                            TimeInForce.DAY, (OrderLeg("AAPL", AssetType.EQUITY,
                            OrderInstruction.BUY, Decimal(2), filled_quantity=Decimal(filled)),),
                            OrderRole.ENTRY, status_detail=status_detail)
        return OrderGroup(order_id, OrderClass.BRACKET, OrderStatus.WORKING, (order,))
    adapter.given_query(OrderQuery, group(0), group(1), group(0, "PENDING_CANCEL"))
    adapter.given_query(AccountQuery, AccountSnapshot("paper-account", "ACTIVE", BalanceSnapshot()))
    adapter.given_command(CancelOrder, MutationResult(MutationStatus.ACCEPTED, order_id))
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    poller = AlpacaBootstrapPoller(adapter=adapter)
    poller.order_service = BrokerOrderService(adapter, store, "paper-account", clock=lambda: now)
    poller._state = {"broker_ready": True, "alpaca": {"account_status": "ACTIVE",
                     "updated_at": now.isoformat(), "positions": [], "orders": []}}
    class Selection:
        selected = "alpaca-paper"
        def get(self): return self.selected
    selected = Selection()
    monkeypatch.setattr(appmod, "BROKER_KEY", "alpaca-paper")
    monkeypatch.setattr(appmod, "poller", poller)
    monkeypatch.setattr(appmod, "BrokerSelection", lambda: selected)
    client = TestClient(appmod.app, base_url=f"http://127.0.0.1:{appmod.PORT}")
    headers = {"X-Companion": "1"}
    try:
        body = {"order_id": order_id, "request_id": str(uuid4())}
        accepted = client.post("/api/paper/entry/cancel", json=body, headers=headers).json()
        assert accepted["status"] == "accepted" and accepted["order_id"] == order_id
        assert len(adapter.commands) == 1
        refused = client.post("/api/paper/entry/cancel", json={**body, "request_id": str(uuid4())},
                              headers=headers).json()
        assert not refused["ok"] and "unfilled" in refused["error"]
        assert len(adapter.commands) == 1
        transitional = client.post("/api/paper/entry/cancel", json={**body, "request_id": str(uuid4())},
                                   headers=headers).json()
        assert not transitional["ok"] and "unfilled" in transitional["error"]
        assert len(adapter.commands) == 1
        selected.selected = "schwab"
        assert client.post("/api/paper/entry/cancel", json=body, headers=headers).status_code == 409
    finally:
        store.close()
        poller.store.close()


def test_paper_reprice_previews_bracket_and_reconciles_exact_replacement(monkeypatch, tmp_path):
    import companion.app as appmod

    now = datetime.now(timezone.utc)
    order_id, replacement_id, request_id = str(uuid4()), str(uuid4()), str(uuid4())
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    def entry_group():
        leg = OrderLeg("AAPL", AssetType.EQUITY, OrderInstruction.BUY, Decimal(2))
        entry_order = BrokerOrder(order_id, OrderStatus.WORKING, OrderType.LIMIT,
                                  TimeInForce.DAY, (leg,), OrderRole.ENTRY,
                                  limit_price=Decimal(100), status_detail="NEW")
        target = BrokerOrder(str(uuid4()), OrderStatus.PENDING, OrderType.LIMIT,
                             TimeInForce.GTC, (leg,), OrderRole.TARGET,
                             limit_price=Decimal(110))
        stop = BrokerOrder(str(uuid4()), OrderStatus.PENDING, OrderType.STOP,
                           TimeInForce.GTC, (leg,), OrderRole.STOP,
                           stop_price=Decimal(95))
        return OrderGroup(order_id, OrderClass.BRACKET, OrderStatus.WORKING,
                          (entry_order, target, stop))
    adapter.given_query(OrderQuery, entry_group(), entry_group(), entry_group())
    account = AccountSnapshot("paper-account", "ACTIVE", BalanceSnapshot())
    adapter.given_query(AccountQuery, account, account, account, account)
    adapter.given_command(ReplaceOrder, MutationResult(MutationStatus.UNKNOWN, replacement_id))
    client_id = f"et-reprice-{request_id}"
    confirmed = BrokerOrder(replacement_id, OrderStatus.WORKING, OrderType.LIMIT,
                            TimeInForce.DAY,
                            (OrderLeg("AAPL", AssetType.EQUITY, OrderInstruction.BUY, Decimal(2)),),
                            OrderRole.ENTRY, client_order_id=client_id,
                            limit_price=Decimal(99))
    adapter.given_query(ClientOrderQuery, OrderGroup(replacement_id, OrderClass.BRACKET,
                                                     OrderStatus.WORKING, (confirmed,)))
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    poller = AlpacaBootstrapPoller(adapter=adapter)
    poller.order_service = BrokerOrderService(adapter, store, "paper-account", clock=lambda: now)
    poller._state = {"broker_ready": True, "alpaca": {"account_status": "ACTIVE",
                     "updated_at": now.isoformat(), "positions": [], "orders": []}}
    class Selection:
        def get(self): return "alpaca-paper"
    monkeypatch.setattr(appmod, "BROKER_KEY", "alpaca-paper")
    monkeypatch.setattr(appmod, "poller", poller)
    monkeypatch.setattr(appmod, "BrokerSelection", Selection)
    client = TestClient(appmod.app, base_url=f"http://127.0.0.1:{appmod.PORT}")
    headers = {"X-Companion": "1"}
    body = {"order_id": order_id, "request_id": request_id, "limit_price": "99"}
    try:
        refused = client.post("/api/paper/entry/reprice/preview",
                              json={**body, "limit_price": "111"}, headers=headers).json()
        assert not refused["ok"] and "between" in refused["error"]
        preview = client.post("/api/paper/entry/reprice/preview",
                              json=body, headers=headers).json()
        assert preview["ok"] and preview["old_price"] == "100"
        assert preview["stop"] == "95" and preview["target"] == "110"
        sent = client.post("/api/paper/entry/reprice", json=body, headers=headers).json()
        assert sent["status"] == "unknown" and sent["order_id"] == replacement_id
        assert len(adapter.commands) == 1
        assert adapter.commands[0].client_order_id == client_id
        assert len(store.open_mutations()) == 1
        resolved = client.post("/api/paper/entry/reprice/reconcile",
                               json={"request_id": request_id}, headers=headers).json()
        assert resolved["status"] == "accepted" and resolved["order_id"] == replacement_id
        assert store.open_mutations() == []
    finally:
        store.close()
        poller.store.close()


def test_paper_oco_route_rechecks_position_and_order_free_symbol(monkeypatch, tmp_path):
    import companion.app as appmod

    now = datetime.now(timezone.utc)
    position = Position("AAPL", AssetType.EQUITY, PositionSide.LONG,
                        Decimal(2), Decimal(100))
    account = AccountSnapshot("paper-account", "ACTIVE", BalanceSnapshot(),
                              (position,))
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    adapter.given_query(AccountQuery, account, account, account)
    adapter.given_query(OrdersQuery, (), (), ())
    adapter.given_query(QuotesQuery, {"AAPL": quote(now)}, {"AAPL": quote(now)})
    et_day = now.astimezone(timezone(timedelta(hours=-5))).date()
    adapter.given_query(MarketSessionsQuery, (MarketSession(
        et_day, now - timedelta(hours=1), now + timedelta(hours=1), True),))
    adapter.given_command(SubmitOrder, MutationResult(MutationStatus.ACCEPTED, "oco-order"))
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    poller = AlpacaBootstrapPoller(adapter=adapter)
    poller.order_service = BrokerOrderService(adapter, store, "paper-account", clock=lambda: now)
    poller._state = {"broker_ready": True, "alpaca": {"account_status": "ACTIVE",
                     "updated_at": now.isoformat(), "positions": [], "orders": []}}
    class Selection:
        def get(self): return "alpaca-paper"
    monkeypatch.setattr(appmod, "BROKER_KEY", "alpaca-paper")
    monkeypatch.setattr(appmod, "poller", poller)
    monkeypatch.setattr(appmod, "BrokerSelection", Selection)
    client = TestClient(appmod.app, base_url=f"http://127.0.0.1:{appmod.PORT}")
    headers = {"X-Companion": "1"}
    body = {"symbol": "AAPL", "target_price": "110", "stop_price": "95",
            "request_id": str(uuid4())}
    try:
        preview = client.post("/api/paper/oco/preview", json=body, headers=headers).json()
        assert preview["ok"] and preview["quantity"] == "2"
        assert preview["protection"].startswith("Broker-held")
        poller.order_service.arm(True)
        sent = client.post("/api/paper/oco/submit", json=body, headers=headers).json()
        assert sent["status"] == "accepted" and sent["order_id"] == "oco-order"
        assert len(adapter.commands) == 1
        assert adapter.commands[0].intent.leg.instruction == OrderInstruction.SELL
        assert adapter.commands[0].intent.leg.quantity == 2
    finally:
        store.close()
        poller.store.close()


def test_paper_oco_submit_refuses_position_change_before_mutation(monkeypatch, tmp_path):
    import companion.app as appmod

    now = datetime.now(timezone.utc)
    held = Position("AAPL", AssetType.EQUITY, PositionSide.LONG, Decimal(2), Decimal(100))
    changed = Position("AAPL", AssetType.EQUITY, PositionSide.LONG, Decimal(1), Decimal(100))
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    adapter.given_query(AccountQuery,
                        AccountSnapshot("paper-account", "ACTIVE", BalanceSnapshot(), (held,)),
                        AccountSnapshot("paper-account", "ACTIVE", BalanceSnapshot(), (changed,)))
    adapter.given_query(OrdersQuery, ())
    adapter.given_query(QuotesQuery, {"AAPL": quote(now)})
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    poller = AlpacaBootstrapPoller(adapter=adapter)
    poller.order_service = BrokerOrderService(adapter, store, "paper-account", clock=lambda: now)
    poller.order_service.arm(True)
    poller._state = {"broker_ready": True, "alpaca": {"account_status": "ACTIVE",
                     "updated_at": now.isoformat(), "positions": [], "orders": []}}
    class Selection:
        def get(self): return "alpaca-paper"
    monkeypatch.setattr(appmod, "BROKER_KEY", "alpaca-paper")
    monkeypatch.setattr(appmod, "poller", poller)
    monkeypatch.setattr(appmod, "BrokerSelection", Selection)
    client = TestClient(appmod.app, base_url=f"http://127.0.0.1:{appmod.PORT}")
    try:
        sent = client.post("/api/paper/oco/submit", json={
            "symbol": "AAPL", "target_price": "110", "stop_price": "95",
            "request_id": str(uuid4())}, headers={"X-Companion": "1"}).json()
        assert sent["status"] == "rejected" and "position changed" in sent["message"]
        assert adapter.commands == []
    finally:
        store.close()
        poller.store.close()

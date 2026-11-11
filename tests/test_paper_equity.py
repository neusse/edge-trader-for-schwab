from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
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
from companion.brokers.paper_equity import (add_plan, close_plan, entry_plan,
                                             equity_exit_reprice, oco_plan, trailing_plan)
from companion.brokers.paper_options import covered_call_reserved_shares
from companion.store import Store
from companion.trading import Trader


def quote(now, symbol="AAPL"):
    return Quote(symbol, Decimal("99.95"), Decimal("100.05"), Decimal("100"),
                 Decimal("100"), now, True, "Alpaca IEX")


def test_held_short_call_reserves_equity_shares_from_exit_workflows(monkeypatch):
    import companion.app as appmod

    now = datetime.now(timezone.utc)
    shares = Position("AAPL", AssetType.EQUITY, PositionSide.LONG,
                      Decimal(200), Decimal(100))
    call = Position("AAPL261120C00110000", AssetType.OPTION, PositionSide.SHORT,
                    Decimal(1), Decimal(2))
    account = AccountSnapshot("paper-account", "ACTIVE", BalanceSnapshot(), (shares, call))
    assert covered_call_reserved_shares(account, "AAPL") == 100
    def query(value):
        if isinstance(value, AccountQuery): return account
        if isinstance(value, OrdersQuery): return ()
        if isinstance(value, QuotesQuery): return {"AAPL": quote(now)}
        raise AssertionError(type(value))
    service = SimpleNamespace(adapter=SimpleNamespace(query=query), account_ref="paper-account")
    monkeypatch.setattr(appmod, "_paper_service", lambda: (service, {}))
    client = TestClient(appmod.app, base_url=f"http://127.0.0.1:{appmod.PORT}")
    headers = {"X-Companion": "1"}
    for path in ("/api/paper/position/close/preview", "/api/paper/oco/preview",
                 "/api/paper/position/trailing/preview"):
        result = client.post(path, json={"symbol": "AAPL"}, headers=headers).json()
        assert not result["ok"] and "short calls reserve" in result["error"]


def test_new_short_call_before_equity_submit_blocks_share_sale(monkeypatch):
    import companion.app as appmod

    now = datetime.now(timezone.utc)
    shares = Position("AAPL", AssetType.EQUITY, PositionSide.LONG,
                      Decimal(200), Decimal(100))
    call = Position("AAPL261120C00110000", AssetType.OPTION, PositionSide.SHORT,
                    Decimal(1), Decimal(2))
    account = AccountSnapshot("paper-account", "ACTIVE", BalanceSnapshot(), (shares,))
    def query(value):
        if isinstance(value, AccountQuery): return account
        if isinstance(value, OrdersQuery): return ()
        if isinstance(value, QuotesQuery): return {"AAPL": quote(now)}
        raise AssertionError(type(value))
    def execute(command, request_id, preflight):
        nonlocal account
        account = AccountSnapshot("paper-account", "ACTIVE", BalanceSnapshot(),
                                  (shares, call))
        preflight(account)
        raise AssertionError("a covered-share sale must not reach the adapter")
    service = SimpleNamespace(adapter=SimpleNamespace(query=query),
                              account_ref="paper-account", execute=execute)
    monkeypatch.setattr(appmod, "_paper_service", lambda: (service, {}))
    client = TestClient(appmod.app, base_url=f"http://127.0.0.1:{appmod.PORT}")
    body = {"symbol": "AAPL", "shares": "1", "request_id": str(uuid4())}
    result = client.post("/api/paper/position/close/submit", json=body,
                         headers={"X-Companion": "1"}).json()
    assert not result["ok"] and "short calls reserve" in result["error"]


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
    # Broker timestamps may lead the local clock slightly; a large lead is invalid.
    entry_plan(entry(), quote(now + timedelta(seconds=2)), now)
    with pytest.raises(ValueError, match="stale"):
        entry_plan(entry(), quote(now + timedelta(seconds=6)), now)
    with pytest.raises(ValueError, match="whole shares"):
        entry_plan(entry(quantity="1.5"), quote(now), now)
    with pytest.raises(ValueError, match="positive number"):
        entry_plan(entry(target_price="NaN"), quote(now), now)


def test_simple_entry_requires_explicit_unprotected_choice():
    now = datetime.now(timezone.utc)
    body = entry(protection="none", target_price=None, stop_price=None)
    plan = entry_plan(body, quote(now), now)
    assert plan.order_type == OrderType.LIMIT and plan.limit_price == 100
    assert plan.leg.instruction == OrderInstruction.BUY
    market = entry_plan({**body, "side": "short", "order_type": "market",
                         "limit_price": None}, quote(now), now)
    assert market.order_type == OrderType.MARKET
    assert market.leg.instruction == OrderInstruction.SELL_SHORT
    with pytest.raises(ValueError, match="unprotected entry cannot include"):
        entry_plan(entry(protection="none"), quote(now), now)
    with pytest.raises(ValueError, match="protection must"):
        entry_plan(entry(protection="unknown"), quote(now), now)


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


def test_close_plan_handles_long_and_short_without_attaching_an_exit():
    now = datetime.now(timezone.utc)
    body = {"symbol": "AAPL", "request_id": str(uuid4())}
    for side, instruction in ((PositionSide.LONG, OrderInstruction.SELL),
                              (PositionSide.SHORT, OrderInstruction.BUY_TO_COVER)):
        position = Position("AAPL", AssetType.EQUITY, side, Decimal(2), Decimal(100))
        plan = close_plan(body, position, quote(now), now)
        assert plan.leg.quantity == 2 and plan.leg.instruction == instruction
        assert plan.order_type == OrderType.MARKET
        assert plan.client_order_id.startswith("et-close-")
    with pytest.raises(ValueError, match="stale"):
        close_plan(body, position, quote(now - timedelta(seconds=31)), now)
    with pytest.raises(ValueError, match="whole shares"):
        close_plan(body, Position("AAPL", AssetType.EQUITY, side,
                                  Decimal("1.5"), Decimal(100)), quote(now), now)
    partial = close_plan({**body, "shares": "1"}, position, quote(now), now)
    assert partial.leg.quantity == 1
    with pytest.raises(ValueError, match="no greater than"):
        close_plan({**body, "shares": "3"}, position, quote(now), now)
    with pytest.raises(ValueError, match="whole"):
        close_plan({**body, "shares": "1.5"}, position, quote(now), now)


def test_add_plan_handles_long_and_short_and_refuses_stale_or_fractional():
    now = datetime.now(timezone.utc)
    body = {"symbol": "AAPL", "shares": "2", "request_id": str(uuid4())}
    for side, instruction in ((PositionSide.LONG, OrderInstruction.BUY),
                              (PositionSide.SHORT, OrderInstruction.SELL_SHORT)):
        position = Position("AAPL", AssetType.EQUITY, side, Decimal(3), Decimal(100))
        plan = add_plan(body, position, quote(now), now)
        assert plan.leg.quantity == 2 and plan.leg.instruction == instruction
        assert plan.order_type == OrderType.MARKET and plan.client_order_id.startswith("et-add-")
    with pytest.raises(ValueError, match="stale"):
        add_plan(body, position, quote(now - timedelta(seconds=31)), now)
    with pytest.raises(ValueError, match="whole shares"):
        add_plan({**body, "shares": "1.5"}, position, quote(now), now)


def test_trailing_plan_uses_one_broker_held_exit_with_no_target():
    now = datetime.now(timezone.utc)
    body = {"symbol": "AAPL", "unit": "dollar", "distance": "2",
            "request_id": str(uuid4())}
    for side, instruction in ((PositionSide.LONG, OrderInstruction.SELL),
                              (PositionSide.SHORT, OrderInstruction.BUY_TO_COVER)):
        position = Position("AAPL", AssetType.EQUITY, side, Decimal(2), Decimal(100))
        trail = trailing_plan(body, position, quote(now), now)
        assert trail.leg.instruction == instruction and trail.leg.quantity == 2
        assert trail.trail_price == 2 and trail.trail_percent is None
        assert trail.client_order_id.startswith("et-trail-")
        pct = trailing_plan({**body, "unit": "percent", "distance": "1.5"},
                            position, quote(now), now)
        assert pct.trail_percent == Decimal("1.5") and pct.trail_price is None
    with pytest.raises(ValueError, match="valid stop"):
        trailing_plan({**body, "distance": "100"},
                      Position("AAPL", AssetType.EQUITY, PositionSide.LONG,
                               Decimal(2), Decimal(100)), quote(now), now)
    with pytest.raises(ValueError, match="stale"):
        trailing_plan(body, position, quote(now - timedelta(seconds=31)), now)


def test_paper_trailing_route_requires_order_free_position_and_rechecks(monkeypatch, tmp_path):
    import companion.app as appmod

    now = datetime.now(timezone.utc)
    held = Position("AAPL", AssetType.EQUITY, PositionSide.LONG, Decimal(2), Decimal(100))
    account = AccountSnapshot("paper-account", "ACTIVE", BalanceSnapshot(), (held,))
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    adapter.given_query(AccountQuery, account, account, account)
    adapter.given_query(OrdersQuery, (), (), ())
    adapter.given_query(QuotesQuery, {"AAPL": quote(now)}, {"AAPL": quote(now)})
    et_day = now.astimezone(timezone(timedelta(hours=-5))).date()
    adapter.given_query(MarketSessionsQuery, (MarketSession(
        et_day, now - timedelta(hours=1), now + timedelta(hours=1), True),))
    adapter.given_command(SubmitOrder, MutationResult(MutationStatus.ACCEPTED, "trail-order"))
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
    body = {"symbol": "AAPL", "unit": "dollar", "distance": "2", "request_id": str(uuid4())}
    try:
        preview = client.post("/api/paper/position/trailing/preview", json=body, headers=headers).json()
        assert preview["ok"] and preview["indicative_initial_stop"] == "98"
        assert "no target" in preview["protection"]
        poller.order_service.arm(True)
        sent = client.post("/api/paper/position/trailing/submit", json=body, headers=headers).json()
        assert sent["status"] == "accepted" and sent["order_id"] == "trail-order"
        assert len(adapter.commands) == 1
        assert adapter.commands[0].intent.trail_price == 2
    finally:
        store.close()
        poller.store.close()


def test_paper_add_route_previews_and_sends_once_for_short_position(monkeypatch, tmp_path):
    import companion.app as appmod

    now = datetime.now(timezone.utc)
    held = Position("AAPL", AssetType.EQUITY, PositionSide.SHORT, Decimal(2), Decimal(100))
    account = AccountSnapshot("paper-account", "ACTIVE", BalanceSnapshot(), (held,))
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    adapter.given_query(AccountQuery, account, account, account)
    adapter.given_query(OrdersQuery, (), (), ())
    adapter.given_query(QuotesQuery, {"AAPL": quote(now)}, {"AAPL": quote(now)})
    et_day = now.astimezone(timezone(timedelta(hours=-5))).date()
    adapter.given_query(MarketSessionsQuery, (MarketSession(
        et_day, now - timedelta(hours=1), now + timedelta(hours=1), True),))
    adapter.given_command(SubmitOrder, MutationResult(MutationStatus.ACCEPTED, "add-order"))
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
    body = {"symbol": "AAPL", "shares": "1", "request_id": str(uuid4())}
    try:
        preview = client.post("/api/paper/position/add/preview", json=body, headers=headers).json()
        assert preview["ok"] and preview["quantity"] == "1"
        assert preview["held_quantity"] == "2" and preview["total_if_filled"] == "3"
        poller.order_service.arm(True)
        sent = client.post("/api/paper/position/add/submit", json=body, headers=headers).json()
        assert sent["status"] == "accepted" and sent["order_id"] == "add-order"
        assert len(adapter.commands) == 1
        assert adapter.commands[0].intent.leg.instruction == OrderInstruction.SELL_SHORT
    finally:
        store.close()
        poller.store.close()


def oco_group(side=PositionSide.LONG, qty=Decimal(2), stop=Decimal(95),
              target=Decimal(110)):
    group_id, target_id, stop_id = (str(uuid4()) for _ in range(3))
    instruction = OrderInstruction.SELL if side == PositionSide.LONG else OrderInstruction.BUY
    leg = OrderLeg("AAPL", AssetType.EQUITY, instruction, qty)
    target_order = BrokerOrder(target_id, OrderStatus.WORKING, OrderType.LIMIT,
                               TimeInForce.GTC, (leg,), OrderRole.TARGET,
                               group_id=group_id, limit_price=target)
    stop_order = BrokerOrder(stop_id, OrderStatus.WORKING, OrderType.STOP,
                             TimeInForce.GTC, (leg,), OrderRole.STOP,
                             group_id=group_id, parent_id=target_id, stop_price=stop)
    return OrderGroup(group_id, OrderClass.OCO, OrderStatus.WORKING,
                      (target_order, stop_order))


def filled_bracket_group(side=PositionSide.LONG, qty=Decimal(2),
                         stop=Decimal(95), target=Decimal(110)):
    group_id, entry_id, target_id, stop_id = (str(uuid4()) for _ in range(4))
    entry_instruction = (OrderInstruction.BUY if side == PositionSide.LONG else
                         OrderInstruction.SELL_SHORT)
    exit_instruction = (OrderInstruction.SELL if side == PositionSide.LONG else
                        OrderInstruction.BUY)
    entry_order = BrokerOrder(entry_id, OrderStatus.FILLED, OrderType.MARKET,
                              TimeInForce.GTC,
                              (OrderLeg("AAPL", AssetType.EQUITY, entry_instruction,
                                        qty, filled_quantity=qty),), OrderRole.ENTRY,
                              client_order_id=f"et-{uuid4().hex}", group_id=group_id)
    exit_leg = OrderLeg("AAPL", AssetType.EQUITY, exit_instruction, qty)
    target_order = BrokerOrder(target_id, OrderStatus.WORKING, OrderType.LIMIT,
                               TimeInForce.GTC, (exit_leg,), OrderRole.TARGET,
                               group_id=group_id, parent_id=entry_id,
                               limit_price=target)
    stop_order = BrokerOrder(stop_id, OrderStatus.WORKING, OrderType.STOP,
                             TimeInForce.GTC, (exit_leg,), OrderRole.STOP,
                             group_id=group_id, parent_id=entry_id,
                             stop_price=stop)
    return OrderGroup(group_id, OrderClass.BRACKET, OrderStatus.WORKING,
                      (entry_order, target_order, stop_order))


@pytest.mark.parametrize("side,stop,target,new_stop,new_target", [
    (PositionSide.LONG, "95", "110", "97", "108"),
    (PositionSide.SHORT, "105", "90", "103", "92"),
])
def test_filled_bracket_exit_reprice_requires_exact_held_position(
        side, stop, target, new_stop, new_target):
    now = datetime.now(timezone.utc)
    position = Position("AAPL", AssetType.EQUITY, side, Decimal(2), Decimal(100))
    group = filled_bracket_group(side, stop=Decimal(stop), target=Decimal(target))
    body = {"symbol": "AAPL", "order_id": group.orders[2].order_id,
            "role": "stop", "price": new_stop, "request_id": str(uuid4())}
    command = equity_exit_reprice(body, position, group, quote(now), now)
    assert command.order_id == group.orders[2].order_id
    assert command.stop_price == Decimal(new_stop)
    assert command.client_order_id.startswith("et-exit-")
    target_command = equity_exit_reprice({**body, "order_id": group.orders[1].order_id,
                                          "role": "target", "price": new_target},
                                         position, group, quote(now), now)
    assert target_command.limit_price == Decimal(new_target)
    changed = replace(group, orders=(replace(group.orders[0], status=OrderStatus.PARTIALLY_FILLED),
                                     *group.orders[1:]))
    with pytest.raises(ValueError, match="Bracket entry or exits changed"):
        equity_exit_reprice(body, position, changed, quote(now), now)
    wrong_quantity = Position("AAPL", AssetType.EQUITY, side, Decimal(1), Decimal(100))
    with pytest.raises(ValueError, match="Bracket entry or exits changed"):
        equity_exit_reprice(body, wrong_quantity, group, quote(now), now)
    external = replace(group, orders=(replace(group.orders[0], client_order_id="outside"),
                                      *group.orders[1:]))
    with pytest.raises(ValueError, match="Bracket entry or exits changed"):
        equity_exit_reprice(body, position, external, quote(now), now)


def test_oco_exit_reprice_tightens_stop_and_moves_target_with_exact_ids():
    now = datetime.now(timezone.utc)
    position = Position("AAPL", AssetType.EQUITY, PositionSide.LONG, Decimal(2), Decimal(100))
    group = oco_group()
    body = {"symbol": "AAPL", "order_id": group.orders[1].order_id,
            "role": "stop", "price": "97", "request_id": str(uuid4())}
    stop = equity_exit_reprice(body, position, group, quote(now), now)
    assert stop.order_id == group.orders[1].order_id and stop.stop_price == 97
    assert stop.limit_price is None and stop.client_order_id.startswith("et-exit-")
    target = equity_exit_reprice({**body, "order_id": group.orders[0].order_id,
                               "role": "target", "price": "108"}, position, group, quote(now), now)
    assert target.order_id == group.orders[0].order_id and target.limit_price == 108
    with pytest.raises(ValueError, match="only tighten"):
        equity_exit_reprice({**body, "price": "94"}, position, group, quote(now), now)
    with pytest.raises(ValueError, match="fully working"):
        equity_exit_reprice(body, Position("AAPL", AssetType.EQUITY, PositionSide.LONG,
                                       Decimal(1), Decimal(100)), group, quote(now), now)


def test_oco_exit_reprice_short_uses_buy_side_and_rejects_looser_stop():
    now = datetime.now(timezone.utc)
    position = Position("AAPL", AssetType.EQUITY, PositionSide.SHORT, Decimal(2), Decimal(100))
    group = oco_group(PositionSide.SHORT, stop=Decimal(105), target=Decimal(90))
    body = {"symbol": "AAPL", "order_id": group.orders[1].order_id,
            "role": "stop", "price": "103", "request_id": str(uuid4())}
    assert equity_exit_reprice(body, position, group, quote(now), now).stop_price == 103
    with pytest.raises(ValueError, match="only tighten"):
        equity_exit_reprice({**body, "price": "106"}, position, group, quote(now), now)


def test_paper_close_route_requires_order_free_position_and_rechecks_before_send(monkeypatch, tmp_path):
    import companion.app as appmod

    now = datetime.now(timezone.utc)
    held = Position("AAPL", AssetType.EQUITY, PositionSide.SHORT, Decimal(2), Decimal(100))
    account = AccountSnapshot("paper-account", "ACTIVE", BalanceSnapshot(), (held,))
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    adapter.given_query(AccountQuery, account, account, account)
    adapter.given_query(OrdersQuery, (), (), ())
    adapter.given_query(QuotesQuery, {"AAPL": quote(now)}, {"AAPL": quote(now)})
    et_day = now.astimezone(timezone(timedelta(hours=-5))).date()
    adapter.given_query(MarketSessionsQuery, (MarketSession(
        et_day, now - timedelta(hours=1), now + timedelta(hours=1), True),))
    adapter.given_command(SubmitOrder, MutationResult(MutationStatus.ACCEPTED, "close-order"))
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
    body = {"symbol": "AAPL", "shares": "1", "request_id": str(uuid4())}
    try:
        preview = client.post("/api/paper/position/close/preview", json=body, headers=headers).json()
        assert preview["ok"] and preview["quantity"] == "1"
        assert preview["held_quantity"] == "2" and preview["remaining_if_filled"] == "1"
        assert preview["order_type"] == "market"
        poller.order_service.arm(True)
        sent = client.post("/api/paper/position/close/submit", json=body, headers=headers).json()
        assert sent["status"] == "accepted" and sent["order_id"] == "close-order"
        assert len(adapter.commands) == 1
        assert adapter.commands[0].intent.leg.instruction == OrderInstruction.BUY_TO_COVER
        assert adapter.commands[0].intent.leg.quantity == 1
    finally:
        store.close()
        poller.store.close()


def test_paper_close_refuses_order_that_appears_during_preflight(monkeypatch, tmp_path):
    import companion.app as appmod

    now = datetime.now(timezone.utc)
    held = Position("AAPL", AssetType.EQUITY, PositionSide.LONG, Decimal(2), Decimal(100))
    account = AccountSnapshot("paper-account", "ACTIVE", BalanceSnapshot(), (held,))
    leg = OrderLeg("AAPL", AssetType.EQUITY, OrderInstruction.SELL, Decimal(2))
    active = OrderGroup("existing", OrderClass.SIMPLE, OrderStatus.UNKNOWN,
                        (BrokerOrder("existing", OrderStatus.UNKNOWN, OrderType.LIMIT,
                                     TimeInForce.DAY, (leg,), OrderRole.EXIT),))
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    adapter.given_query(AccountQuery, account, account)
    adapter.given_query(OrdersQuery, (), (active,))
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
        result = client.post("/api/paper/position/close/submit", json={
            "symbol": "AAPL", "request_id": str(uuid4())},
            headers={"X-Companion": "1"}).json()
        assert result["status"] == "rejected" and "another order appeared" in result["message"]
        assert adapter.commands == []
    finally:
        store.close()
        poller.store.close()


@pytest.mark.parametrize("protection", ["bracket", "none"])
def test_paper_entry_route_arms_previews_sends_once_and_disarms_on_selection(monkeypatch, tmp_path,
                                                                               protection):
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
    body = entry() if protection == "bracket" else entry(
        protection="none", target_price=None, stop_price=None)
    try:
        preview = client.post("/api/paper/entry/preview", json=body, headers=headers).json()
        assert preview["ok"]
        if protection == "none":
            assert preview["target"] is None and preview["stop"] is None
            assert "UNPROTECTED" in preview["protection"]
        assert client.post("/api/paper/entry/submit", json=body, headers=headers).json()["status"] == "rejected"
        assert client.post("/api/paper/arm", json={"armed": True}, headers=headers).json()["armed"]
        result = client.post("/api/paper/entry/submit", json=body, headers=headers).json()
        assert result["status"] == "accepted" and result["order_id"] == "order-1"
        assert len(adapter.commands) == 1
        assert (hasattr(adapter.commands[0].intent, "take_profit_price") ==
                (protection == "bracket"))
        switched = client.post("/api/brokers/select", json={"broker": "schwab"}, headers=headers).json()
        assert switched == {"ok": True, "active": "schwab", "selected": "schwab", "restart_required": False}
        assert next_poller.started
        assert not poller.order_service.armed
        assert client.post("/api/paper/entry/submit", json=body, headers=headers).status_code == 409
    finally:
        store.close()
        poller.store.close()
        next_poller.store.close()


@pytest.mark.parametrize("protection", ["bracket", "none"])
def test_paper_cancel_route_rechecks_unfilled_entry_and_selected_broker(monkeypatch, tmp_path,
                                                                          protection):
    import companion.app as appmod

    now = datetime.now(timezone.utc)
    order_id = str(uuid4())
    client_id = f"et-{uuid4().hex}"
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    def group(filled, status_detail=None):
        order = BrokerOrder(order_id, OrderStatus.WORKING, OrderType.LIMIT,
                            TimeInForce.DAY, (OrderLeg("AAPL", AssetType.EQUITY,
                            OrderInstruction.BUY, Decimal(2), filled_quantity=Decimal(filled)),),
                            OrderRole.ENTRY, client_order_id=client_id,
                            status_detail=status_detail)
        kind = OrderClass.BRACKET if protection == "bracket" else OrderClass.SIMPLE
        return OrderGroup(order_id, kind, OrderStatus.WORKING, (order,))
    adapter.given_query(OrderQuery, group(0), group(1), group(0, "PENDING_CANCEL"))
    adapter.given_query(AccountQuery, AccountSnapshot("paper-account", "ACTIVE", BalanceSnapshot()))
    adapter.given_command(CancelOrder, MutationResult(MutationStatus.ACCEPTED, order_id))
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    assert store.record_mutation(str(uuid4()), "SubmitOrder", {}, client_id)
    store.finish_mutation(store.get_mutation_by_client_id(client_id)["request_id"],
                          "accepted", order_id)
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


@pytest.mark.parametrize("protection", ["bracket", "none"])
def test_paper_reprice_previews_bracket_and_reconciles_exact_replacement(monkeypatch, tmp_path,
                                                                           protection):
    import companion.app as appmod

    now = datetime.now(timezone.utc)
    order_id, replacement_id, request_id = str(uuid4()), str(uuid4()), str(uuid4())
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    original_client_id = f"et-{uuid4().hex}"
    def entry_group():
        leg = OrderLeg("AAPL", AssetType.EQUITY, OrderInstruction.BUY, Decimal(2))
        entry_order = BrokerOrder(order_id, OrderStatus.WORKING, OrderType.LIMIT,
                                  TimeInForce.DAY, (leg,), OrderRole.ENTRY,
                                  client_order_id=original_client_id,
                                  limit_price=Decimal(100), status_detail="NEW")
        if protection == "none":
            return OrderGroup(order_id, OrderClass.SIMPLE, OrderStatus.WORKING,
                              (entry_order,))
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
    assert store.record_mutation(str(uuid4()), "SubmitOrder", {}, original_client_id)
    store.finish_mutation(store.get_mutation_by_client_id(original_client_id)["request_id"],
                          "accepted", order_id)
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
        if protection == "bracket":
            refused = client.post("/api/paper/entry/reprice/preview",
                                  json={**body, "limit_price": "111"}, headers=headers).json()
            assert not refused["ok"] and "between" in refused["error"]
        preview = client.post("/api/paper/entry/reprice/preview",
                              json=body, headers=headers).json()
        assert preview["ok"] and preview["old_price"] == "100", preview
        assert preview["stop"] == ("95" if protection == "bracket" else None)
        assert preview["target"] == ("110" if protection == "bracket" else None)
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


@pytest.mark.parametrize("group_factory", [oco_group, filled_bracket_group])
def test_paper_exit_adjust_previews_rechecks_and_reconciles_exact_stop(
        monkeypatch, tmp_path, group_factory):
    import companion.app as appmod

    now = datetime.now(timezone.utc)
    position = Position("AAPL", AssetType.EQUITY, PositionSide.LONG, Decimal(2), Decimal(100))
    account = AccountSnapshot("paper-account", "ACTIVE", BalanceSnapshot(), (position,))
    group = group_factory()
    request_id = str(uuid4())
    replacement_id = str(uuid4())
    client_id = f"et-exit-{request_id.replace('-', '')}"
    replacement = BrokerOrder(replacement_id, OrderStatus.WORKING, OrderType.STOP,
                              TimeInForce.GTC,
                              (OrderLeg("AAPL", AssetType.EQUITY, OrderInstruction.SELL,
                                        Decimal(2)),), OrderRole.STOP,
                              client_order_id=client_id, stop_price=Decimal(97))
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    adapter.given_query(AccountQuery, account, account, account, account)
    adapter.given_query(OrdersQuery, (group,), (group,), (group,))
    adapter.given_query(QuotesQuery, {"AAPL": quote(now)}, {"AAPL": quote(now)},
                        {"AAPL": quote(now)})
    adapter.given_query(ClientOrderQuery, OrderGroup(group.group_id, group.order_class,
                                                     OrderStatus.WORKING, (replacement,)))
    adapter.given_command(ReplaceOrder, MutationResult(MutationStatus.UNKNOWN, replacement_id))
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
    body = {"symbol": "AAPL", "order_id": group.orders[-1].order_id,
            "role": "stop", "price": "97", "request_id": request_id}
    try:
        preview = client.post("/api/paper/equity/exit/adjust/preview",
                              json=body, headers=headers).json()
        assert preview["ok"] and preview["old_price"] == "95"
        assert preview["new_price"] == "97" and preview["other_price"] == "110"
        assert preview["group_class"] == group.order_class.value
        sent = client.post("/api/paper/equity/exit/adjust/submit",
                           json=body, headers=headers).json()
        assert sent["status"] == "unknown" and sent["order_id"] == replacement_id
        assert len(adapter.commands) == 1
        assert adapter.commands[0].stop_price == 97
        resolved = client.post("/api/paper/entry/reprice/reconcile",
                               json={"request_id": request_id}, headers=headers).json()
        assert resolved["status"] == "accepted" and resolved["order_id"] == replacement_id
    finally:
        store.close()
        poller.store.close()


@pytest.mark.parametrize("group_factory", [oco_group, filled_bracket_group])
def test_paper_exit_adjust_refuses_changed_pair_before_replace(
        monkeypatch, tmp_path, group_factory):
    import companion.app as appmod

    now = datetime.now(timezone.utc)
    position = Position("AAPL", AssetType.EQUITY, PositionSide.LONG, Decimal(2), Decimal(100))
    account = AccountSnapshot("paper-account", "ACTIVE", BalanceSnapshot(), (position,))
    group = group_factory()
    target_index = 0 if group.order_class == OrderClass.OCO else 1
    changed_orders = list(group.orders)
    changed_orders[target_index] = replace(changed_orders[target_index],
                                           limit_price=Decimal(111))
    changed = replace(group, orders=tuple(changed_orders))
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    adapter.given_query(AccountQuery, account, account)
    adapter.given_query(OrdersQuery, (group,), (changed,))
    adapter.given_query(QuotesQuery, {"AAPL": quote(now)}, {"AAPL": quote(now)})
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
    try:
        result = client.post("/api/paper/equity/exit/adjust/submit", json={
            "symbol": "AAPL", "order_id": group.orders[-1].order_id,
            "role": "stop", "price": "97", "request_id": str(uuid4())},
            headers={"X-Companion": "1"}).json()
        assert result["status"] == "rejected" and "Equity exit changed" in result["message"]
        assert adapter.commands == []
    finally:
        store.close()
        poller.store.close()

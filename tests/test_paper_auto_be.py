from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import RLock
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from companion.brokers.bootstrap import AlpacaBootstrapPoller
from companion.brokers.models import (
    AccountQuery, AccountSnapshot, AssetType, BalanceSnapshot, MarketSession,
    MutationResult, MutationStatus, OrderClass, OrderGroup, OrderInstruction,
    OrderLeg, OrderRole, OrderStatus, OrderType, OrdersQuery, Position,
    PositionSide, Quote, QuotesQuery, ReplaceOrder, TimeInForce, BrokerOrder,
)
from companion.brokers.paper_auto_be import PaperAutoBEWatch, oco_auto_be_plan
from companion.store import Store

D = Decimal


def pair(side=PositionSide.LONG, *, basis="100", stop="95", target="110", qty="2"):
    symbol = "AAPL"
    position = Position(symbol, AssetType.EQUITY, side, D(qty), D(basis))
    action = OrderInstruction.SELL if side == PositionSide.LONG else OrderInstruction.BUY
    leg = OrderLeg(symbol, AssetType.EQUITY, action, D(qty))
    group_id, stop_id, target_id = (str(uuid4()) for _ in range(3))
    target_order = BrokerOrder(target_id, OrderStatus.WORKING, OrderType.LIMIT,
                               TimeInForce.GTC, (leg,), OrderRole.TARGET,
                               group_id=group_id, limit_price=D(target),
                               client_order_id=f"et-oco-{uuid4().hex}")
    stop_order = BrokerOrder(stop_id, OrderStatus.WORKING, OrderType.STOP,
                             TimeInForce.GTC, (leg,), OrderRole.STOP,
                             group_id=group_id, parent_id=target_id,
                             stop_price=D(stop))
    group = OrderGroup(group_id, OrderClass.OCO, OrderStatus.WORKING,
                       (target_order, stop_order))
    return position, group


def bracket_pair(side=PositionSide.LONG, **kwargs):
    position, oco = pair(side, **kwargs)
    opening = OrderInstruction.BUY if side == PositionSide.LONG else OrderInstruction.SELL
    entry_leg = OrderLeg("AAPL", AssetType.EQUITY, opening,
                         position.quantity, filled_quantity=position.quantity)
    entry = BrokerOrder(oco.group_id, OrderStatus.FILLED, OrderType.LIMIT,
                        TimeInForce.DAY, (entry_leg,), OrderRole.ENTRY,
                        group_id=oco.group_id,
                        client_order_id=f"et-{uuid4().hex}")
    target = replace(oco.orders[0], client_order_id=None, parent_id=entry.order_id,
                     time_in_force=TimeInForce.DAY)
    stop = replace(oco.orders[1], parent_id=entry.order_id,
                   time_in_force=TimeInForce.DAY)
    return position, OrderGroup(oco.group_id, OrderClass.BRACKET,
                                 OrderStatus.FILLED, (entry, target, stop))


def quote(at, bid="105", ask="105.02", *, source="Alpaca IEX", verified=True):
    return Quote("AAPL", D(bid), D(ask), None, (D(bid) + D(ask)) / 2,
                 at, True, source, timestamp_verified=verified)


def observe(watch, row, position, group, value, at):
    return watch.observe(row["symbol"], position, (group,), value,
                         market_open=True, monitoring=True, now=at + timedelta(seconds=1))


def test_auto_be_plan_requires_owned_working_pair_and_tightens_long_or_short():
    long, group = pair()
    plan = oco_auto_be_plan(long, group)
    assert (plan["trigger"], plan["new_stop"], plan["quantity"]) == (D(105), D(100), D(2))
    short, group = pair(PositionSide.SHORT, stop="105", target="90")
    plan = oco_auto_be_plan(short, group)
    assert (plan["trigger"], plan["new_stop"]) == (D(95), D(100))
    with pytest.raises(ValueError, match="target is reached before"):
        oco_auto_be_plan(long, pair(target="104")[1])
    with pytest.raises(ValueError, match="app-created"):
        external = pair()[1]
        oco_auto_be_plan(long, replace(external, orders=(
            replace(external.orders[0], client_order_id="external"), external.orders[1])))


def test_auto_be_plan_accepts_filled_bracket_but_not_partial_entry():
    position, group = bracket_pair()
    plan = oco_auto_be_plan(position, group)
    assert plan["oco_client_id"] == group.orders[0].client_order_id
    assert (plan["trigger"], plan["new_stop"]) == (D(105), D(100))
    partial = replace(group, orders=(
        replace(group.orders[0], status=OrderStatus.PARTIALLY_FILLED),
        group.orders[1], group.orders[2]))
    with pytest.raises(ValueError, match="changed"):
        oco_auto_be_plan(position, partial)


def test_auto_be_requires_two_new_verified_quotes_and_blocks_unknown_clear(tmp_path):
    now = datetime.now(timezone.utc)
    position, group = pair()
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    try:
        watch = PaperAutoBEWatch(store)
        row = watch.set(oco_auto_be_plan(position, group), now - timedelta(seconds=10))
        first = quote(now - timedelta(seconds=4))
        second = quote(now - timedelta(seconds=1))
        assert observe(watch, row, position, group, first, now)["status"] == "watching"
        assert observe(watch, row, position, group, first, now)["status"] == "watching"
        assert observe(watch, row, position, group, second, now)["status"] == "sending"
        assert observe(watch, row, position, group, second, now)["fire"] is False
        assert not watch.clear("AAPL")
        watch.record_send("AAPL", row["request_id"], "unknown", "broker response lost")
        assert not watch.clear("AAPL")
        assert store.record_mutation(row["request_id"], "ReplaceOrder", {},
                                     f"et-autobe-{row['request_id'].replace('-', '')}")
        store.finish_mutation(row["request_id"], "unknown", None)
        restarted = PaperAutoBEWatch(store)
        assert not restarted.acknowledge_unknown("AAPL")
        store.finish_mutation(row["request_id"], "accepted", "replacement")
        restarted.resolve_request(row["request_id"], True, "Alpaca confirmed stop")
        assert restarted.get("AAPL")["status"] == "done"
        assert restarted.clear("AAPL")
    finally:
        store.close()


def test_auto_be_ignores_stale_indicative_or_changed_oco(tmp_path):
    now = datetime.now(timezone.utc)
    position, group = pair()
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    try:
        watch = PaperAutoBEWatch(store)
        row = watch.set(oco_auto_be_plan(position, group), now - timedelta(seconds=10))
        assert observe(watch, row, position, group,
                       quote(now - timedelta(seconds=30)), now)["status"] == "suspended"
        assert observe(watch, row, position, group,
                       quote(now, source="Alpaca indicative options"), now)["status"] == "suspended"
        assert observe(watch, row, position, group,
                       quote(now, verified=False), now)["status"] == "suspended"
        changed = pair(qty="3")[0]
        assert observe(watch, row, changed, group, quote(now), now)["status"] == "ended"
        assert watch.get("AAPL")["status"] == "ended"
        assert not observe(watch, row, position, group, quote(now), now)["fire"]
    finally:
        store.close()


def test_auto_be_accepts_small_broker_clock_lead(tmp_path):
    now = datetime.now(timezone.utc)
    position, group = pair()
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    try:
        watch = PaperAutoBEWatch(store)
        row = watch.set(oco_auto_be_plan(position, group), now - timedelta(seconds=10))
        assert observe(watch, row, position, group,
                       quote(now + timedelta(seconds=3)), now)["status"] == "watching"
        assert observe(watch, row, position, group,
                       quote(now + timedelta(seconds=7)), now)["status"] == "suspended"
    finally:
        store.close()


def test_auto_be_restart_after_claim_never_retries(tmp_path):
    now = datetime.now(timezone.utc)
    position, group = pair()
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    try:
        watch = PaperAutoBEWatch(store)
        row = watch.set(oco_auto_be_plan(position, group), now - timedelta(seconds=10))
        observe(watch, row, position, group, quote(now - timedelta(seconds=4)), now)
        assert observe(watch, row, position, group,
                       quote(now - timedelta(seconds=1)), now)["status"] == "sending"
        restarted = PaperAutoBEWatch(store)
        assert restarted.get("AAPL")["status"] == "unknown"
        assert not restarted.observe("AAPL", position, (group,), quote(now),
                                     market_open=True, monitoring=True, now=now)["fire"]
        assert not restarted.clear("AAPL")
        assert restarted.acknowledge_unknown("AAPL")
        assert restarted.clear("AAPL")
    finally:
        store.close()


def test_auto_be_ends_when_oco_target_changes(tmp_path):
    now = datetime.now(timezone.utc)
    position, group = pair()
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    try:
        watch = PaperAutoBEWatch(store)
        row = watch.set(oco_auto_be_plan(position, group), now - timedelta(seconds=10))
        changed = replace(group, orders=(
            replace(group.orders[0], limit_price=D(111)), group.orders[1]))
        result = observe(watch, row, position, changed, quote(now), now)
        assert result["status"] == "ended" and not result["fire"]
        assert watch.get("AAPL")["status"] == "ended"
    finally:
        store.close()


@pytest.mark.parametrize("recovered", [False, True])
@pytest.mark.parametrize("source", ["oco", "bracket"])
def test_auto_be_poller_checks_third_quote_before_one_replace(tmp_path, recovered, source):
    now = datetime.now(timezone.utc)
    position, group = bracket_pair() if source == "bracket" else pair()
    account = AccountSnapshot("paper-account", "ACTIVE", BalanceSnapshot(), (position,))
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    watch = PaperAutoBEWatch(store)
    row = watch.set(oco_auto_be_plan(position, group), now - timedelta(seconds=10))
    third = quote(now - timedelta(seconds=1), "103", "103.02") if recovered else quote(now - timedelta(seconds=1))
    sequence = [third]
    commands = []

    def query(value):
        if isinstance(value, AccountQuery): return account
        if isinstance(value, OrdersQuery): return (group,)
        if isinstance(value, QuotesQuery): return {"AAPL": sequence.pop(0)}
        raise AssertionError(type(value))

    def execute(command, request_id, preflight):
        preflight(account)
        commands.append(command)
        return MutationResult(MutationStatus.UNKNOWN, "pending-replacement")

    poller = AlpacaBootstrapPoller(adapter=SimpleNamespace(query=query))
    poller.auto_be = watch
    poller.order_service = SimpleNamespace(store=store, account_ref="paper-account",
                                           _market_open=lambda _: True, execute=execute)
    poller.action_lock = RLock()
    session = MarketSession(now.date(), now - timedelta(hours=1),
                            now + timedelta(hours=1), True)
    try:
        first = poller._auto_be_rows(account, (group,), {"AAPL": quote(now - timedelta(seconds=4))},
                                     SimpleNamespace(is_open=True), session, now)
        assert first[0]["status"] == "watching"
        second = poller._auto_be_rows(account, (group,), {"AAPL": quote(now - timedelta(seconds=1))},
                                      SimpleNamespace(is_open=True), session, now)
        assert second[0]["status"] == ("refused" if recovered else "unknown")
        assert len(commands) == (0 if recovered else 1)
        if commands:
            assert isinstance(commands[0], ReplaceOrder)
            assert commands[0].order_id == row["stop_id"]
            assert commands[0].stop_price == D(100)
        again = poller._auto_be_rows(account, (group,), {}, SimpleNamespace(is_open=True),
                                     session, now)
        assert again[0]["status"] == second[0]["status"] and len(commands) == (0 if recovered else 1)
    finally:
        poller.store.close()
        store.close()


@pytest.mark.parametrize("source", ["oco", "bracket"])
def test_auto_be_route_reviews_and_sets_only_owned_unchanged_oco(monkeypatch, tmp_path, source):
    import companion.app as appmod

    now = datetime.now(timezone.utc)
    position, group = bracket_pair() if source == "bracket" else pair()
    account = AccountSnapshot("paper-account", "ACTIVE", BalanceSnapshot(), (position,))
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    watch = PaperAutoBEWatch(store)
    service = SimpleNamespace(store=store, account_ref="paper-account",
                              _market_open=lambda _: True)

    def query(value):
        if isinstance(value, AccountQuery): return account
        if isinstance(value, OrdersQuery): return (group,)
        if isinstance(value, QuotesQuery): return {"AAPL": quote(now)}
        raise AssertionError(type(value))

    service.adapter = SimpleNamespace(query=query)
    monkeypatch.setattr(appmod, "_paper_service", lambda: (service, {}))
    monkeypatch.setattr(appmod, "poller", SimpleNamespace(auto_be=watch))
    client = TestClient(appmod.app, base_url=f"http://127.0.0.1:{appmod.PORT}")
    headers = {"X-Companion": "1"}
    route = "/api/paper/equity/auto-be" if source == "bracket" else "/api/paper/oco/auto-be"
    try:
        preview = client.post(f"{route}/preview",
                              json={"symbol": "AAPL"}, headers=headers).json()
        assert not preview["ok"] and "accepted through this app" in preview["error"]
        client_id = oco_auto_be_plan(position, group)["oco_client_id"]
        assert store.record_mutation(str(uuid4()), "SubmitOrder", {},
                                     client_id)
        owned = store.get_mutation_by_client_id(client_id)
        store.finish_mutation(owned["request_id"], "accepted", group.group_id)
        preview = client.post(f"{route}/preview",
                              json={"symbol": "AAPL"}, headers=headers).json()
        assert preview["ok"] and preview["trigger"] == "105"
        assert "APP-WATCHED" in preview["warning"]
        keys = ("quantity", "basis", "initial_stop", "target", "trigger",
                "new_stop", "group_id", "stop_id", "target_id")
        reviewed = {key: preview[key] for key in keys}
        refused = client.post(f"{route}/set",
                              json={"symbol": "AAPL", "reviewed": {**reviewed, "basis": "101"}},
                              headers=headers).json()
        assert not refused["ok"] and watch.get("AAPL") is None
        set_result = client.post(f"{route}/set",
                                 json={"symbol": "AAPL", "reviewed": reviewed},
                                 headers=headers).json()
        assert set_result["ok"] and set_result["status"] == "watching"
        duplicate = client.post(f"{route}/preview",
                                json={"symbol": "AAPL"}, headers=headers).json()
        assert not duplicate["ok"]
        cleared = client.post(f"{route}/clear",
                              json={"symbol": "AAPL"}, headers=headers).json()
        assert cleared["ok"] and watch.get("AAPL") is None
    finally:
        store.close()

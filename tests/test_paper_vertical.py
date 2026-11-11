from datetime import date, datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from companion.brokers.models import (
    AccountQuery, AccountSnapshot, AssetType, BalanceSnapshot, BrokerOrder,
    CancelOrder,
    MutationResult, MutationStatus, OptionChain, OptionChainQuery,
    OptionContract, OptionType, OrderClass, OrderGroup, OrderInstruction,
    OrderQuery,
    OrderLeg, OrderRole, OrderStatus, OrderType, OrdersQuery,
    Position, PositionSide, Quote, QuotesQuery, TimeInForce,
)
from companion.brokers.paper_vertical import vertical_close_plan, vertical_entry_plan
from companion.brokers.alpaca_orders import build_order_request

NOW = datetime(2026, 11, 11, 17, tzinfo=timezone.utc)
REQUEST_ID = "8d112e10-6866-4bd1-9606-0f4e81a0cf6f"


def _chain(kind=OptionType.CALL):
    cp = "C" if kind == OptionType.CALL else "P"
    contracts = []
    for strike, bid, ask in ((100, "2.00", "2.20"), (105, "0.90", "1.10")):
        symbol = f"AAPL261113{cp}{strike * 1000:08d}"
        quote = Quote(symbol, D(bid), D(ask), None, (D(bid) + D(ask)) / 2,
                      NOW, False, "Alpaca indicative options")
        contracts.append(OptionContract(symbol, "AAPL", date(2026, 11, 13),
                                        D(strike), kind, quote=quote))
    return OptionChain("AAPL", tuple(contracts), NOW)


def _body(long_strike=100, short_strike=105, kind="call", net="1.30"):
    cp = "C" if kind == "call" else "P"
    return {"underlying": "AAPL", "expiration": "2026-11-13", "option_type": kind,
            "long_contract": f"AAPL261113{cp}{long_strike * 1000:08d}",
            "short_contract": f"AAPL261113{cp}{short_strike * 1000:08d}",
            "quantity": "2", "net_price": net, "request_id": REQUEST_ID}


def _account(level=3, power="10000"):
    return AccountSnapshot("paper-account", "ACTIVE",
                           BalanceSnapshot(options_buying_power=D(power)), (),
                           options_trading_level=level, options_approved_level=3)


def _held_account(long_strike=100, short_strike=105, kind="call", quantity="2",
                  long_basis="2", short_basis="1"):
    body = _body(long_strike, short_strike, kind)
    positions = (Position(body["long_contract"], AssetType.OPTION, PositionSide.LONG,
                          D(quantity), D(long_basis)),
                 Position(body["short_contract"], AssetType.OPTION, PositionSide.SHORT,
                          D(quantity), D(short_basis)))
    return AccountSnapshot("paper-account", "ACTIVE", BalanceSnapshot(), positions)


def _quotes(kind=OptionType.CALL):
    return {contract.symbol: contract.quote for contract in _chain(kind).contracts}


def test_vertical_entry_plans_atomic_debit_and_credit_call_spreads():
    debit = vertical_entry_plan(_body(), _account(), _chain(), NOW)
    credit = vertical_entry_plan(_body(105, 100, net="-1.30"),
                                 _account(), _chain(), NOW)
    assert debit.order_type == OrderType.LIMIT and debit.time_in_force == TimeInForce.DAY
    assert debit.limit_price == D("1.30") and len(debit.legs) == 2
    assert [leg.instruction for leg in debit.legs] == [OrderInstruction.BUY_TO_OPEN,
                                                      OrderInstruction.SELL_TO_OPEN]
    assert [leg.quantity for leg in credit.legs] == [D(2), D(2)]
    assert credit.limit_price == D("-1.30")
    request = build_order_request(credit)
    assert request.limit_price == -1.3 and len(request.legs) == 2
    assert request.legs[0].position_intent.value == "buy_to_open"
    assert request.legs[1].position_intent.value == "sell_to_open"


def test_vertical_put_direction_uses_strike_order():
    debit = vertical_entry_plan(_body(105, 100, kind="put", net="1.30"),
                                _account(), _chain(OptionType.PUT), NOW)
    credit = vertical_entry_plan(_body(100, 105, kind="put", net="-1.30"),
                                 _account(), _chain(OptionType.PUT), NOW)
    assert debit.limit_price > 0 and credit.limit_price < 0


def test_vertical_entry_rejects_wrong_sign_level_and_collateral():
    with pytest.raises(ValueError, match="debit or credit"):
        vertical_entry_plan(_body(net="-1.30"), _account(), _chain(), NOW)
    with pytest.raises(ValueError, match="level 3"):
        vertical_entry_plan(_body(), _account(level=2), _chain(), NOW)
    with pytest.raises(ValueError, match="maximum loss"):
        vertical_entry_plan(_body(), _account(power="100"), _chain(), NOW)
    with pytest.raises(ValueError, match="distinct"):
        vertical_entry_plan(_body(100, 100), _account(), _chain(), NOW)


def test_vertical_close_uses_opposite_intents_and_exact_held_quantity():
    plan = vertical_close_plan(_body(net="-1.30"), _held_account(), _quotes(), NOW)
    assert plan.limit_price == D("-1.30") and plan.time_in_force == TimeInForce.DAY
    assert [leg.instruction for leg in plan.legs] == [OrderInstruction.SELL_TO_CLOSE,
                                                     OrderInstruction.BUY_TO_CLOSE]
    assert [leg.quantity for leg in plan.legs] == [D(2), D(2)]
    credit = vertical_close_plan(_body(105, 100, net="1.30"),
                                 _held_account(105, 100), _quotes(), NOW)
    assert credit.limit_price == D("1.30")
    with pytest.raises(ValueError, match="credit or debit"):
        vertical_close_plan(_body(net="1.30"), _held_account(), _quotes(), NOW)
    with pytest.raises(ValueError, match="equal whole"):
        unequal = _held_account()
        positions = (unequal.positions[0], Position(unequal.positions[1].symbol,
                     AssetType.OPTION, PositionSide.SHORT, D(1), D(1)))
        vertical_close_plan(_body(net="-1.30"), AccountSnapshot(
            "paper-account", "ACTIVE", BalanceSnapshot(), positions), _quotes(), NOW)


def test_vertical_target_requires_verified_profit_and_is_one_gtc_mleg():
    debit_target = vertical_close_plan({**_body(net="-1.30"), "role": "target"},
                                       _held_account(), _quotes(), NOW)
    assert debit_target.time_in_force == TimeInForce.GTC
    assert debit_target.client_order_id.startswith("et-vertical-target-")
    credit_target = vertical_close_plan({**_body(105, 100, net="0.70"), "role": "target"},
                                        _held_account(105, 100, long_basis="1",
                                                      short_basis="2"), _quotes(), NOW)
    assert credit_target.limit_price == D("0.70")
    with pytest.raises(ValueError, match="profit"):
        vertical_close_plan({**_body(net="-0.90"), "role": "target"},
                            _held_account(), _quotes(), NOW)
    with pytest.raises(ValueError, match="basis"):
        vertical_close_plan({**_body(105, 100, net="0.70"), "role": "target"},
                            _held_account(105, 100), _quotes(), NOW)


def test_vertical_entry_route_sends_one_mleg_and_rechecks_account(monkeypatch):
    import companion.app as appmod

    account = _account()
    commands = []
    def query(value):
        if isinstance(value, AccountQuery): return account
        if isinstance(value, OrdersQuery): return ()
        if isinstance(value, OptionChainQuery): return _chain()
        raise AssertionError(type(value))
    def execute(command, request_id, preflight):
        preflight(account)
        commands.append(command)
        return MutationResult(MutationStatus.ACCEPTED, "paper-vertical-order")
    service = SimpleNamespace(adapter=SimpleNamespace(query=query),
                              account_ref="paper-account", execute=execute)
    monkeypatch.setattr(appmod, "_paper_service", lambda: (service, {}))
    monkeypatch.setattr("companion.brokers.paper_options.datetime", SimpleNamespace(
        now=lambda _tz: NOW))
    monkeypatch.setattr("companion.brokers.paper_vertical.datetime", SimpleNamespace(
        now=lambda _tz: NOW))
    client = TestClient(appmod.app, base_url=f"http://127.0.0.1:{appmod.PORT}")
    headers = {"X-Companion": "1"}
    preview = client.post("/api/paper/vertical/entry/preview", json=_body(), headers=headers)
    assert preview.json()["ok"], preview.json()
    assert preview.json()["maximum_loss"] == "260.00"
    submitted = client.post("/api/paper/vertical/entry/submit", json=_body(), headers=headers)
    assert submitted.json()["ok"] and len(commands) == 1
    assert len(commands[0].intent.legs) == 2
    account = _account(power="100")
    rejected = client.post("/api/paper/vertical/entry/submit", json=_body(), headers=headers)
    assert not rejected.json()["ok"] and len(commands) == 1


def test_vertical_close_route_rechecks_exact_pair_and_one_mleg(monkeypatch):
    import companion.app as appmod

    account = _held_account()
    groups = ()
    commands = []
    def query(value):
        if isinstance(value, AccountQuery): return account
        if isinstance(value, OrdersQuery): return groups
        if isinstance(value, QuotesQuery): return _quotes()
        raise AssertionError(type(value))
    def execute(command, request_id, preflight):
        preflight(account)
        commands.append(command)
        return MutationResult(MutationStatus.ACCEPTED, "paper-vertical-close")
    service = SimpleNamespace(adapter=SimpleNamespace(query=query),
                              account_ref="paper-account", execute=execute)
    monkeypatch.setattr(appmod, "_paper_service", lambda: (service, {}))
    monkeypatch.setattr("companion.brokers.paper_vertical.datetime", SimpleNamespace(
        now=lambda _tz: NOW))
    client = TestClient(appmod.app, base_url=f"http://127.0.0.1:{appmod.PORT}")
    headers = {"X-Companion": "1"}
    body = _body(net="-1.30")
    preview = client.post("/api/paper/vertical/close/preview", json=body, headers=headers)
    assert preview.json()["ok"], preview.json()
    assert preview.json()["quantity"] == "2"
    sent = client.post("/api/paper/vertical/close/submit", json=body, headers=headers)
    assert sent.json()["ok"] and len(commands) == 1
    assert [leg.instruction for leg in commands[0].intent.legs] == [
        OrderInstruction.SELL_TO_CLOSE, OrderInstruction.BUY_TO_CLOSE]
    target = client.post("/api/paper/vertical/close/preview", json={
        **body, "role": "target"}, headers=headers)
    assert target.json()["ok"] and target.json()["time_in_force"] == "gtc"
    assert "No broker-held stop is paired" in target.json()["protection"]
    account = _held_account(quantity="1")
    later = client.post("/api/paper/vertical/close/preview", json=body, headers=headers)
    assert later.json()["ok"] and later.json()["quantity"] == "1"
    pending = BrokerOrder("active", OrderStatus.PARTIALLY_FILLED, OrderType.LIMIT,
                          TimeInForce.DAY,
                          (OrderLeg(body["long_contract"], AssetType.OPTION,
                                    OrderInstruction.SELL_TO_CLOSE, D(2), D(1)),),
                          OrderRole.EXIT)
    groups = (OrderGroup("active", OrderClass.MULTI_LEG,
                         OrderStatus.PARTIALLY_FILLED, (pending,)),)
    blocked = client.post("/api/paper/vertical/close/preview", json=body, headers=headers)
    assert not blocked.json()["ok"] and "active order" in blocked.json()["error"]


def test_vertical_cancel_only_unfilled_owned_mleg_remainder(monkeypatch):
    import companion.app as appmod

    order_id = "e74ad353-4d70-4705-b046-c5bde18bb55f"
    body = _body()
    order = BrokerOrder(order_id, OrderStatus.PARTIALLY_FILLED, OrderType.LIMIT,
                        TimeInForce.GTC,
                        (OrderLeg(body["long_contract"], AssetType.OPTION,
                                  OrderInstruction.SELL_TO_CLOSE, D(2), D(1)),
                         OrderLeg(body["short_contract"], AssetType.OPTION,
                                  OrderInstruction.BUY_TO_CLOSE, D(2), D(1))),
                        OrderRole.TARGET, client_order_id="et-vertical-target-owned")
    group = OrderGroup(order_id, OrderClass.MULTI_LEG,
                       OrderStatus.PARTIALLY_FILLED, (order,))
    commands = []
    adapter = SimpleNamespace(query=lambda query: group if isinstance(query, OrderQuery)
                              else _held_account())
    store = SimpleNamespace(get_mutation_by_client_id=lambda _key: {"status": "accepted"})
    service = SimpleNamespace(adapter=adapter, store=store,
                              execute=lambda command, request_id: (
                                  commands.append(command),
                                  MutationResult(MutationStatus.ACCEPTED, order_id))[1])
    monkeypatch.setattr(appmod, "_paper_service", lambda: (service, {}))
    client = TestClient(appmod.app, base_url=f"http://127.0.0.1:{appmod.PORT}")
    payload = {"order_id": order_id, "request_id": REQUEST_ID}
    headers = {"X-Companion": "1"}
    preview = client.post("/api/paper/vertical/cancel/preview", json=payload, headers=headers)
    assert preview.json()["ok"] and [leg["remaining"] for leg in preview.json()["legs"]] == ["1", "1"]
    sent = client.post("/api/paper/vertical/cancel/submit", json=payload, headers=headers)
    assert sent.json()["ok"] and len(commands) == 1 and isinstance(commands[0], CancelOrder)
    store.get_mutation_by_client_id = lambda _key: None
    refused = client.post("/api/paper/vertical/cancel/preview", json=payload, headers=headers)
    assert not refused.json()["ok"] and "not owned" in refused.json()["error"]

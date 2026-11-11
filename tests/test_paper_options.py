from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from companion.brokers.models import (AccountQuery, AccountSnapshot, AssetType,
                                      BalanceSnapshot, BrokerOrder, CancelOrder,
                                      MutationResult, MutationStatus, OrderClass,
                                      OptionChain, OptionChainQuery, OptionContract,
                                      OptionGreeks, OptionType, OrderGroup,
                                      OrderInstruction, OrderLeg, OrderQuery,
                                      OrderRole, OrderStatus, OrderType, OrdersQuery,
                                      Position, PositionSide, Quote, TimeInForce,
                                      QuotesQuery)
from companion.brokers.paper_options import chain_query, chain_response
from companion.brokers.paper_options import (expirations_query, expirations_response,
                                             long_entry_plan, option_close_plan,
                                             option_fixed_exit_plan, short_entry_plan)


NOW = datetime(2026, 11, 11, 17, 0, tzinfo=timezone.utc)
BODY = {"underlying": "AAPL", "expiration": "2026-11-13", "option_type": "call"}


def _chain():
    quote = Quote("AAPL261113C00225000", D("1.20"), D("1.40"), None,
                  D("1.30"), NOW, False, "Alpaca indicative options",
                  greeks=OptionGreeks(delta=D("0.45")))
    contract = OptionContract("AAPL  261113C00225000", "AAPL", date(2026, 11, 13),
                              D("225"), OptionType.CALL, quote=quote,
                              open_interest=D("111"), implied_volatility=D("0.27"))
    return OptionChain("AAPL", (contract,), NOW)


def _account(level=3, positions=()):
    return AccountSnapshot("paper-account", "ACTIVE",
                           BalanceSnapshot(buying_power=D("10000")), positions,
                           options_trading_level=level, options_approved_level=3)


def _entry_body():
    return {**BODY, "contract": "AAPL261113C00225000", "quantity": "2",
            "limit_price": "1.35", "request_id": "8d112e10-6866-4bd1-9606-0f4e81a0cf6f"}


def test_chain_query_requires_exact_near_expiry_and_type():
    query = chain_query(BODY, NOW)
    assert query.underlying == "AAPL" and query.expiration == date(2026, 11, 13)
    assert query.option_type == OptionType.CALL
    for wrong in ({**BODY, "underlying": "aapl"},
                  {**BODY, "expiration": "2027-03-13"},
                  {**BODY, "option_type": "both"}):
        with pytest.raises(ValueError):
            chain_query(wrong, NOW)


def test_expirations_query_and_response_are_bounded_and_sorted():
    query = expirations_query({"underlying": "AAPL"}, NOW)
    assert query.start == date(2026, 11, 11)
    result = expirations_response(query, (date(2026, 11, 20), date(2026, 11, 13),
                                          date(2026, 11, 13)))
    assert result["expirations"] == ["2026-11-13", "2026-11-20"]
    with pytest.raises(ValueError, match="requested range"):
        expirations_response(query, (date(2027, 4, 1),))


def test_chain_response_normalizes_occ_and_labels_indicative_quote():
    result = chain_response(chain_query(BODY, NOW), _chain())
    assert result["source"] == "Alpaca indicative options" and not result["realtime"]
    row = result["contracts"][0]
    assert row["symbol"] == "AAPL261113C00225000"
    assert row["bid"] == "1.20" and row["ask"] == "1.40"
    assert row["delta"] == "0.45" and row["implied_volatility"] == "0.27"


def test_chain_response_refuses_mismatched_terms_or_unexpected_quote():
    query = chain_query(BODY, NOW)
    contract = _chain().contracts[0]
    with pytest.raises(ValueError, match="requested OCC terms"):
        chain_response(query, OptionChain("AAPL", (
            OptionContract(contract.symbol, "AAPL", contract.expiration, D("226"),
                           contract.option_type),), NOW))
    with pytest.raises(ValueError, match="source"):
        wrong = Quote(contract.symbol, D("1"), D("2"), None, D("1.5"), NOW,
                      True, "Alpaca IEX")
        chain_response(query, OptionChain("AAPL", (
            OptionContract(contract.symbol, "AAPL", contract.expiration, contract.strike,
                           contract.option_type, quote=wrong),), NOW))


def test_long_option_entry_is_limit_day_bto_with_no_attached_exits():
    plan = long_entry_plan(_entry_body(), _account(), _chain(), NOW)
    assert plan.leg.instruction == OrderInstruction.BUY_TO_OPEN
    assert plan.leg.asset_type == AssetType.OPTION and plan.leg.quantity == 2
    assert plan.limit_price == D("1.35") and plan.client_order_id.startswith("et-opt-")


def test_long_option_entry_refuses_level_position_and_bad_premium():
    body = _entry_body()
    with pytest.raises(ValueError, match="level 2"):
        long_entry_plan(body, _account(level=1), _chain(), NOW)
    held = Position(body["contract"], AssetType.OPTION, PositionSide.LONG, D("1"), D("1"))
    with pytest.raises(ValueError, match="position already exists"):
        long_entry_plan(body, _account(positions=(held,)), _chain(), NOW)
    with pytest.raises(ValueError, match="buying power"):
        long_entry_plan({**body, "limit_price": "100"}, _account(), _chain(), NOW)
    with pytest.raises(ValueError, match="whole contracts"):
        long_entry_plan({**body, "quantity": "1.5"}, _account(), _chain(), NOW)


def test_short_call_requires_uncommitted_cover_and_level_one():
    body = _entry_body()
    shares = Position("AAPL", AssetType.EQUITY, PositionSide.LONG, D("200"), D("200"))
    plan = short_entry_plan(body, _account(level=1, positions=(shares,)), _chain(), NOW)
    assert plan.leg.instruction == OrderInstruction.SELL_TO_OPEN
    assert plan.leg.quantity == 2 and plan.time_in_force == TimeInForce.DAY
    with pytest.raises(ValueError, match="covered call"):
        short_entry_plan(body, _account(level=1), _chain(), NOW)
    prior = Position("AAPL261120C00230000", AssetType.OPTION,
                     PositionSide.SHORT, D("1"), D("1"))
    with pytest.raises(ValueError, match="uncommitted"):
        short_entry_plan(body, _account(level=1, positions=(shares, prior)), _chain(), NOW)
    with pytest.raises(ValueError, match="level 1"):
        short_entry_plan(body, _account(level=0, positions=(shares,)), _chain(), NOW)
    with pytest.raises(ValueError, match="current"):
        stale = _chain().contracts[0]
        quote = Quote(body["contract"], D("1.20"), D("1.40"), None, D("1.30"),
                      NOW - timedelta(seconds=61), False, "Alpaca indicative options")
        short_entry_plan(body, _account(level=1, positions=(shares,)), OptionChain(
            "AAPL", (OptionContract(stale.symbol, "AAPL", stale.expiration, stale.strike,
                                     stale.option_type, quote=quote),), NOW), NOW)


def test_short_put_requires_full_strike_collateral():
    body = {**_entry_body(), "option_type": "put", "contract": "AAPL261113P00225000",
            "quantity": "1"}
    call = _chain().contracts[0]
    quote = Quote(body["contract"], D("1.20"), D("1.40"), None, D("1.30"), NOW,
                  False, "Alpaca indicative options")
    contract = OptionContract("AAPL  261113P00225000", "AAPL", call.expiration,
                              call.strike, OptionType.PUT, quote=quote)
    chain = OptionChain("AAPL", (contract,), NOW)
    account = AccountSnapshot("paper-account", "ACTIVE",
                              BalanceSnapshot(options_buying_power=D("22500")), (),
                              options_trading_level=1, options_approved_level=1)
    plan = short_entry_plan(body, account, chain, NOW)
    assert plan.leg.instruction == OrderInstruction.SELL_TO_OPEN
    with pytest.raises(ValueError, match="options buying power"):
        short_entry_plan(body, _account(level=1), chain, NOW)
    with pytest.raises(ValueError, match="strike price"):
        short_entry_plan({**body, "limit_price": "226"}, account, chain, NOW)


def _held_option(side=PositionSide.LONG):
    return Position("AAPL261113C00225000", AssetType.OPTION, side, D("3"), D("1.10"))


def test_option_close_plan_maps_long_short_and_partial_without_overclosing():
    body = {"contract": "AAPL261113C00225000", "quantity": "2",
            "limit_price": "1.40", "request_id": _entry_body()["request_id"]}
    long = option_close_plan(body, _held_option(), NOW)
    short = option_close_plan(body, _held_option(PositionSide.SHORT), NOW)
    assert long.leg.instruction == OrderInstruction.SELL_TO_CLOSE
    assert short.leg.instruction == OrderInstruction.BUY_TO_CLOSE
    assert long.leg.quantity == 2 and long.limit_price == D("1.40")
    with pytest.raises(ValueError, match="held contracts"):
        option_close_plan({**body, "quantity": "4"}, _held_option(), NOW)
    with pytest.raises(ValueError, match="cent precision"):
        option_close_plan({**body, "limit_price": "1.405"}, _held_option(), NOW)


def test_option_fixed_exit_is_one_whole_position_stop_or_target():
    quote = _chain().contracts[0].quote
    base = {"contract": "AAPL261113C00225000", "request_id": _entry_body()["request_id"]}
    long_stop = option_fixed_exit_plan({**base, "role": "stop", "price": "1.00"},
                                       _held_option(), quote, NOW)
    long_target = option_fixed_exit_plan({**base, "role": "target", "price": "1.60"},
                                         _held_option(), quote, NOW)
    long_stop_limit = option_fixed_exit_plan(
        {**base, "role": "stop-limit", "price": "1.00", "limit_price": "0.90"},
        _held_option(), quote, NOW)
    short_stop = option_fixed_exit_plan({**base, "role": "stop", "price": "1.60"},
                                        _held_option(PositionSide.SHORT), quote, NOW)
    short_target = option_fixed_exit_plan({**base, "role": "target", "price": "1.00"},
                                          _held_option(PositionSide.SHORT), quote, NOW)
    assert long_stop.order_type == OrderType.STOP and long_stop.stop_price == D("1.00")
    assert long_stop.leg.quantity == 3 and long_stop.time_in_force == TimeInForce.GTC
    assert long_target.order_type == OrderType.LIMIT and long_target.limit_price == D("1.60")
    assert (long_stop_limit.order_type == OrderType.STOP_LIMIT and
            long_stop_limit.stop_price == D("1.00") and
            long_stop_limit.limit_price == D("0.90"))
    assert short_stop.leg.instruction == OrderInstruction.BUY_TO_CLOSE
    assert short_target.leg.instruction == OrderInstruction.BUY_TO_CLOSE
    assert short_target.limit_price == D("1.00")
    short_stop_limit = option_fixed_exit_plan(
        {**base, "role": "stop-limit", "price": "1.60", "limit_price": "1.70"},
        _held_option(PositionSide.SHORT), quote, NOW)
    assert short_stop_limit.leg.instruction == OrderInstruction.BUY_TO_CLOSE
    with pytest.raises(ValueError, match="sell limit"):
        option_fixed_exit_plan({**base, "role": "stop-limit", "price": "1.00",
                                "limit_price": "1.10"}, _held_option(), quote, NOW)
    with pytest.raises(ValueError, match="cent-precision"):
        option_fixed_exit_plan({**base, "role": "stop-limit", "price": "1.00",
                                "limit_price": "0.901"}, _held_option(), quote, NOW)
    with pytest.raises(ValueError, match="loss side"):
        option_fixed_exit_plan({**base, "role": "stop", "price": "1.50"},
                               _held_option(), quote, NOW)
    with pytest.raises(ValueError, match="current"):
        option_fixed_exit_plan({**base, "role": "stop", "price": "1.00"},
                               _held_option(), None, NOW)
    with pytest.raises(ValueError, match="stale"):
        option_fixed_exit_plan({**base, "role": "stop", "price": "1.00"},
                               _held_option(), quote, NOW + timedelta(seconds=61))


def test_option_discovery_route_uses_active_paper_adapter_without_mutation(monkeypatch):
    import companion.app as appmod

    observed = []
    adapter = SimpleNamespace(query=lambda query: (observed.append(query), _chain())[1])
    monkeypatch.setattr(appmod, "_paper_service", lambda: (SimpleNamespace(adapter=adapter), {}))
    monkeypatch.setattr("companion.brokers.paper_options.datetime", SimpleNamespace(
        now=lambda _tz: NOW))
    client = TestClient(appmod.app, base_url=f"http://127.0.0.1:{appmod.PORT}")
    result = client.post("/api/paper/options/chain", json=BODY,
                         headers={"X-Companion": "1"})
    assert result.status_code == 200 and result.json()["ok"]
    assert len(observed) == 1 and observed[0].underlying == "AAPL"
    assert result.json()["contracts"][0]["symbol"] == "AAPL261113C00225000"


def test_option_expirations_route_reads_adapter_without_mutation(monkeypatch):
    import companion.app as appmod

    observed = []
    adapter = SimpleNamespace(query=lambda query: (observed.append(query),
                                                     (date(2026, 11, 13),))[1])
    monkeypatch.setattr(appmod, "_paper_service", lambda: (SimpleNamespace(adapter=adapter), {}))
    monkeypatch.setattr("companion.brokers.paper_options.datetime", SimpleNamespace(
        now=lambda _tz: NOW))
    client = TestClient(appmod.app, base_url=f"http://127.0.0.1:{appmod.PORT}")
    result = client.post("/api/paper/options/expirations", json={"underlying": "AAPL"},
                         headers={"X-Companion": "1"})
    assert result.status_code == 200 and result.json()["expirations"] == ["2026-11-13"]
    assert len(observed) == 1 and observed[0].underlying == "AAPL"


def test_long_option_preview_and_submit_are_account_bound_and_one_shot(monkeypatch):
    import companion.app as appmod

    queries, commands = [], []
    account = _account()
    def query(value):
        queries.append(value)
        if isinstance(value, AccountQuery): return account
        if isinstance(value, OrdersQuery): return ()
        if isinstance(value, OptionChainQuery): return _chain()
        raise AssertionError(type(value))
    def execute(command, request_id, preflight):
        preflight(account)
        commands.append((command, request_id))
        return MutationResult(MutationStatus.ACCEPTED, "paper-option-order")
    service = SimpleNamespace(adapter=SimpleNamespace(query=query),
                              account_ref="paper-account", execute=execute)
    monkeypatch.setattr(appmod, "_paper_service", lambda: (service, {}))
    monkeypatch.setattr("companion.brokers.paper_options.datetime", SimpleNamespace(
        now=lambda _tz: NOW))
    client = TestClient(appmod.app, base_url=f"http://127.0.0.1:{appmod.PORT}")
    headers = {"X-Companion": "1"}
    preview = client.post("/api/paper/options/long/preview", json=_entry_body(), headers=headers)
    assert preview.json()["maximum_premium"] == "270.00"
    assert "No broker-held stop" in preview.json()["protection"]
    submit = client.post("/api/paper/options/long/submit", json=_entry_body(), headers=headers)
    assert submit.json()["ok"] and submit.json()["order_id"] == "paper-option-order"
    assert len(commands) == 1 and commands[0][0].intent.leg.instruction == OrderInstruction.BUY_TO_OPEN


def test_short_option_route_rechecks_covered_shares_before_send(monkeypatch):
    import companion.app as appmod

    shares = Position("AAPL", AssetType.EQUITY, PositionSide.LONG, D("200"), D("200"))
    account = _account(level=1, positions=(shares,))
    groups = ()
    commands = []
    def query(value):
        if isinstance(value, AccountQuery): return account
        if isinstance(value, OrdersQuery): return groups
        if isinstance(value, OptionChainQuery): return _chain()
        raise AssertionError(type(value))
    def execute(command, request_id, preflight):
        preflight(account)
        commands.append(command)
        return MutationResult(MutationStatus.ACCEPTED, "paper-covered-call")
    service = SimpleNamespace(adapter=SimpleNamespace(query=query),
                              account_ref="paper-account", execute=execute)
    monkeypatch.setattr(appmod, "_paper_service", lambda: (service, {}))
    monkeypatch.setattr("companion.brokers.paper_options.datetime", SimpleNamespace(
        now=lambda _tz: NOW))
    client = TestClient(appmod.app, base_url=f"http://127.0.0.1:{appmod.PORT}")
    headers = {"X-Companion": "1"}
    preview = client.post("/api/paper/options/short/preview", json=_entry_body(), headers=headers)
    assert preview.json()["ok"] and preview.json()["collateral_required"] == "200"
    assert preview.json()["collateral_type"] == "covered shares"
    accepted = client.post("/api/paper/options/short/submit", json=_entry_body(), headers=headers)
    assert accepted.json()["ok"] and len(commands) == 1
    assert commands[0].intent.leg.instruction == OrderInstruction.SELL_TO_OPEN
    pending = BrokerOrder("external", OrderStatus.WORKING, OrderType.LIMIT,
                          TimeInForce.DAY,
                          (OrderLeg("AAPL", AssetType.EQUITY, OrderInstruction.SELL,
                                    D("100")),), OrderRole.EXIT)
    groups = (OrderGroup("external", OrderClass.SIMPLE, OrderStatus.WORKING,
                         (pending,)),)
    conflict = client.post("/api/paper/options/short/preview", json=_entry_body(), headers=headers)
    assert not conflict.json()["ok"] and "active order" in conflict.json()["error"]
    groups = ()
    account = _account(level=1)
    refused = client.post("/api/paper/options/short/submit", json=_entry_body(), headers=headers)
    assert not refused.json()["ok"] and len(commands) == 1


def test_option_close_route_previews_and_rechecks_held_quantity(monkeypatch):
    import companion.app as appmod

    held = _held_option()
    account = _account(positions=(held,))
    commands = []
    def query(value):
        if isinstance(value, AccountQuery): return account
        if isinstance(value, OrdersQuery): return ()
        if isinstance(value, QuotesQuery): return {held.symbol: _chain().contracts[0].quote}
        raise AssertionError(type(value))
    def execute(command, request_id, preflight):
        preflight(account)
        commands.append(command)
        return MutationResult(MutationStatus.ACCEPTED, "paper-option-close")
    service = SimpleNamespace(adapter=SimpleNamespace(query=query),
                              account_ref="paper-account", execute=execute)
    monkeypatch.setattr(appmod, "_paper_service", lambda: (service, {}))
    monkeypatch.setattr("companion.brokers.paper_options.datetime", SimpleNamespace(
        now=lambda _tz: NOW))
    client = TestClient(appmod.app, base_url=f"http://127.0.0.1:{appmod.PORT}")
    body = {"contract": held.symbol, "quantity": "2", "limit_price": "1.40",
            "request_id": _entry_body()["request_id"]}
    headers = {"X-Companion": "1"}
    preview = client.post("/api/paper/options/close/preview", json=body, headers=headers)
    assert preview.json()["ok"] and preview.json()["remaining"] == "1"
    result = client.post("/api/paper/options/close/submit", json=body, headers=headers)
    assert result.json()["ok"] and len(commands) == 1
    assert commands[0].intent.leg.instruction == OrderInstruction.SELL_TO_CLOSE
    refused = client.post("/api/paper/options/close/preview",
                          json={**body, "quantity": "4"}, headers=headers)
    assert not refused.json()["ok"]


def test_option_cancel_previews_only_remaining_owned_contracts(monkeypatch):
    import companion.app as appmod

    order_id = "e74ad353-4d70-4705-b046-c5bde18bb55f"
    order = BrokerOrder(order_id, OrderStatus.PARTIALLY_FILLED, OrderType.LIMIT,
                        TimeInForce.DAY,
                        (OrderLeg("AAPL261113C00225000", AssetType.OPTION,
                                  OrderInstruction.BUY_TO_OPEN, D("3"), D("1")),),
                        OrderRole.ENTRY, client_order_id="et-opt-owned")
    group = OrderGroup(order_id, OrderClass.SIMPLE, OrderStatus.PARTIALLY_FILLED,
                       (order,))
    commands = []
    adapter = SimpleNamespace(query=lambda query: group if isinstance(query, OrderQuery)
                              else _account())
    service = SimpleNamespace(adapter=adapter, account_ref="paper-account",
                              execute=lambda command, request_id: (
                                  commands.append(command),
                                  MutationResult(MutationStatus.ACCEPTED, order_id))[1])
    monkeypatch.setattr(appmod, "_paper_service", lambda: (service, {}))
    client = TestClient(appmod.app, base_url=f"http://127.0.0.1:{appmod.PORT}")
    body = {"order_id": order_id, "request_id": _entry_body()["request_id"]}
    headers = {"X-Companion": "1"}
    preview = client.post("/api/paper/options/cancel/preview", json=body, headers=headers)
    assert preview.json()["ok"] and preview.json()["remaining"] == "2"
    assert preview.json()["filled"] == "1"
    result = client.post("/api/paper/options/cancel/submit", json=body, headers=headers)
    assert result.json()["ok"] and len(commands) == 1
    assert isinstance(commands[0], CancelOrder)
    external = BrokerOrder(order_id, OrderStatus.WORKING, OrderType.LIMIT,
                           TimeInForce.DAY, order.legs, OrderRole.ENTRY,
                           client_order_id="external-order")
    adapter.query = lambda query: OrderGroup(order_id, OrderClass.SIMPLE,
                                             OrderStatus.WORKING, (external,))
    refused = client.post("/api/paper/options/cancel/preview", json=body, headers=headers)
    assert not refused.json()["ok"]


def test_option_fixed_exit_route_rechecks_order_free_full_position(monkeypatch):
    import companion.app as appmod

    held = _held_option()
    account = _account(positions=(held,))
    commands = []
    def query(value):
        if isinstance(value, AccountQuery): return account
        if isinstance(value, OrdersQuery): return ()
        if isinstance(value, QuotesQuery): return {held.symbol: _chain().contracts[0].quote}
        raise AssertionError(type(value))
    def execute(command, request_id, preflight):
        preflight(account)
        commands.append(command)
        return MutationResult(MutationStatus.ACCEPTED, "paper-option-stop")
    service = SimpleNamespace(adapter=SimpleNamespace(query=query),
                              account_ref="paper-account", execute=execute)
    monkeypatch.setattr(appmod, "_paper_service", lambda: (service, {}))
    monkeypatch.setattr("companion.brokers.paper_options.datetime", SimpleNamespace(
        now=lambda _tz: NOW))
    client = TestClient(appmod.app, base_url=f"http://127.0.0.1:{appmod.PORT}")
    body = {"contract": held.symbol, "role": "stop", "price": "1.00",
            "request_id": _entry_body()["request_id"]}
    headers = {"X-Companion": "1"}
    preview = client.post("/api/paper/options/exit/preview", json=body, headers=headers)
    assert preview.json()["ok"], preview.json()
    assert preview.json()["quantity"] == "3"
    assert "no target is paired" in preview.json()["protection"]
    result = client.post("/api/paper/options/exit/submit", json=body, headers=headers)
    assert result.json()["ok"] and len(commands) == 1
    assert commands[0].intent.order_type == OrderType.STOP
    assert commands[0].intent.leg.quantity == 3
    stop_limit = client.post("/api/paper/options/exit/preview", json={
        **body, "role": "stop-limit", "limit_price": "0.90"}, headers=headers)
    assert stop_limit.json()["ok"] and stop_limit.json()["limit_price"] == "0.90"
    assert "may remain unfilled" in stop_limit.json()["protection"]

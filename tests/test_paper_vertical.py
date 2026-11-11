from datetime import date, datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from companion.brokers.models import (
    AccountQuery, AccountSnapshot, AssetType, BalanceSnapshot,
    MutationResult, MutationStatus, OptionChain, OptionChainQuery,
    OptionContract, OptionType, OrderInstruction, OrderType, OrdersQuery,
    Quote, TimeInForce,
)
from companion.brokers.paper_vertical import vertical_entry_plan
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
